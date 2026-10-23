"""Server-owned temporary delivery policy for restaurant-agent credentials only.

The POS configuration and the general-purpose quotation engine are unchanged.
Distance/bands/neighborhoods deliberately require staff quotation for this agent.
"""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, select

from .errors import CodedHTTPException
from .models import BranchSettings, Business, DeliveryQuote, OrderPaymentRequest, Payment, utcnow
from .services import money

POLICY_VERSION = "restaurant-agent-manual-delivery-v1"
MANUAL_MODES = frozenset({"quote", "distance", "bands", "neighborhoods"})


def effective_mode(configured_mode):
    if configured_mode in MANUAL_MODES:
        return "quote"
    if configured_mode in {"free", "fixed"}:
        return configured_mode
    raise HTTPException(422, "Unsupported delivery mode")


def project_delivery(delivery):
    """Expose only currently applicable prices, keeping configured/effective distinct."""
    configured = delivery["mode"]
    mode = effective_mode(configured)
    enabled = delivery["enabled"]
    return {**delivery, "mode": mode, "configured_mode": configured,
        "agent_policy_version": POLICY_VERSION,
        "fee_status": "disabled" if not enabled else "pending_quote" if mode == "quote" else "configured",
        "requires_quote": bool(enabled and mode == "quote"),
        "requires_destination": bool(enabled),
        "fee": None if mode == "quote" else delivery["fee"],
        "distance_base_fee": None, "distance_fee_per_km": None, "distance_max_km": None,
        "free_delivery_threshold": None, "policy": None, "bands": []}


def create_agent_delivery_quote(db, branch, settings, *, subtotal, destination,
                                expected_configuration_version, sender):
    """Internal factory; callers must authenticate the restaurant_agent profile.

    Lock/version/destination/subtotal checks match strict POS quotes; no routing or
    threshold promotion is evaluated, and no saved POS configuration is changed.
    """
    from .settings_service import QUOTE_TTL_MINUTES
    current = db.scalar(select(BranchSettings).where(
        BranchSettings.branch_id == branch.id, BranchSettings.business_id == branch.business_id,
    ).with_for_update().execution_options(populate_existing=True))
    if current is None or current.version != expected_configuration_version:
        raise CodedHTTPException(409, "Delivery configuration changed; request a new quote", "DELIVERY_CONFIGURATION_STALE")
    if not current.digital_delivery:
        raise CodedHTTPException(409, "Delivery is disabled", "SERVICE_DISABLED")
    subtotal = money(subtotal)
    minimum = money(current.minimum_order_amount) if current.minimum_order_amount is not None else None
    if minimum is not None and subtotal < minimum:
        raise CodedHTTPException(409, "Delivery minimum purchase not reached", "DELIVERY_MINIMUM_REQUIRED")
    mode = effective_mode(current.delivery_mode)
    fee = None if mode == "quote" else Decimal(0) if mode == "free" else money(current.fixed_delivery_fee)
    quote = DeliveryQuote(id=str(uuid4()), business_id=branch.business_id, branch_id=branch.id,
        mode=mode, subtotal=subtotal, fee=fee, distance_km=None, minimum_order_amount=minimum,
        configuration_version=current.version, input_snapshot={
            "destination": deepcopy(destination), "pos_strict": True,
            "context": "agent_checkout", "sender": sender,
            "subtotal_basis": "before_discounts", "configured_mode": current.delivery_mode,
            "agent_policy_version": POLICY_VERSION,
        }, expires_at=utcnow() + timedelta(minutes=QUOTE_TTL_MINUTES))
    db.add(quote)
    db.flush()
    return quote


def delivery_fee_notification(db, order):
    """Minimal current financial state for a scoped, read-only event consumer."""
    request = db.scalar(select(OrderPaymentRequest).where(
        OrderPaymentRequest.order_id == order.id, OrderPaymentRequest.business_id == order.business_id,
        OrderPaymentRequest.branch_id == order.branch_id, OrderPaymentRequest.purpose == "delivery",
    ).order_by(OrderPaymentRequest.created_at.desc(), OrderPaymentRequest.id.desc()))
    business = db.get(Business, order.business_id)
    settings = db.scalar(select(BranchSettings).where(
        BranchSettings.branch_id == order.branch_id, BranchSettings.business_id == order.business_id))
    from .models import Branch
    branch = db.get(Branch, order.branch_id)
    methods = (settings.payment_methods or {}).get("delivery", []) if settings else (branch.accepted_payment_methods or [])
    paid = money(db.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(
        Payment.order_id == order.id, Payment.business_id == order.business_id, Payment.status == "confirmed")))
    from .cash_service import order_refunds
    refunded = sum((money(row["amount"]) for row in order_refunds(db, order)), Decimal(0))
    remaining = max(Decimal(0), money(order.total) - (paid - refunded))
    payable = min(money(request.amount), remaining) if request and request.status in {
        "pending", "under_review", "requires_review"} and order.channel == "delivery" and order.delivery_fee_status == "final" else Decimal(0)
    return {"quote_id": order.delivery_quote_id, "delivery_fee": float(order.delivery_fee),
        "currency": business.currency, "cash_available": "cash" in methods, "remaining_amount": float(remaining),
        "payment_request": {"id": request.id, "version": request.version, "purpose": request.purpose,
            "method": request.method, "status": request.status, "amount": float(request.amount),
            "payable_amount": float(payable),
            "quote_id": (request.snapshot or {}).get("quote_id")} if request else None}
