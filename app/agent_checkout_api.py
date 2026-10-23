"""Versioned, tenant-scoped customer operations for the WhatsApp agent."""
from decimal import Decimal
from copy import deepcopy
from datetime import timezone
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .agent_order_changes import (attach_recent_agent_order_changes, kitchen_was_completed,
    record_agent_order_change)
from .agent_checkout import (assert_open, assert_registered, assert_sender, fingerprint, payment_request, pending_delivery_allowed,
    reject_agent_order_change,
    preview_items, priced_snapshot, requests_for_order, serialize_request, initial_payment_pending,
    INACTIVE_REQUEST_STATUSES)
from .auth import AuthContext, require_roles
from .database import get_db
from .models import (Branch, BranchSettings, KitchenTicket, OrderPaymentRequest, Payment, Product,
    DeliveryQuote, DeliveryAssignment, Courier, PaymentEvidence, utcnow)
from .schemas import OrderLineInput, OrderItemRevisionOperation
from .services import (append_order_item_batch, assert_version, audit,
    get_idempotent_response, load_order, money, save_idempotent_response, serialize_order,
    sync_order_payment_status, product_capacity)
from .auth import IntegrationAuthContext, require_integration_scope
from .realtime import hub
from .errors import CodedHTTPException

router = APIRouter(prefix="/api/v1", tags=["agent-checkout"])


class Preview(BaseModel):
    channel: Literal["counter", "takeaway", "delivery"]
    purpose: Literal["initial", "addition"] = "initial"
    payment_method: Literal["cash", "yape"] | None = None
    items: list[OrderLineInput] = Field(min_length=1, max_length=100)
    delivery_address: dict | None = None


class CustomerOperation(BaseModel):
    sender: str = Field(min_length=5, max_length=120)
    expected_version: int = Field(ge=1)


class Addition(CustomerOperation):
    items: list[OrderLineInput] = Field(min_length=1, max_length=100)
    expected_amount: Decimal = Field(gt=0)
    payment_method: Literal["cash", "yape"] | None = None


class Revision(CustomerOperation):
    operations: list[OrderItemRevisionOperation] = Field(min_length=1, max_length=100)


class Fulfillment(CustomerOperation):
    model_config = ConfigDict(extra="forbid")
    channel: Literal["takeaway", "delivery"]
    delivery_address: dict | None = None


class FulfillmentCommit(Fulfillment):
    quote_id: str = Field(min_length=1, max_length=36)
    expected_delivery_fee: Decimal | None = Field(default=None, ge=0)


class DeliveryFee(BaseModel):
    expected_version: int = Field(ge=1)
    amount: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    method: Literal["cash", "yape"] | None = None


class DeliveryMethod(CustomerOperation):
    method: Literal["cash", "yape"]


class StaffDeliveryMethod(BaseModel):
    expected_version: int = Field(ge=1)
    method: Literal["cash", "yape"]


class AdditionResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    action: Literal["decline"]
    note: str = Field(min_length=1, max_length=1000)


def scoped(db, integration, order_id, sender):
    from .api import ensure_integration_order_scope
    order = load_order(db, order_id, for_update=True)
    ensure_integration_order_scope(integration, order)
    assert_sender(order, sender)
    attach_recent_agent_order_changes(db, [order])
    return order


def paid_order_amount(db, order):
    return money(db.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(
        Payment.order_id == order.id, Payment.status == "confirmed")))


def replay(db, order, scope, key, payload):
    if not key or len(key) > 240:
        raise HTTPException(422, "Idempotency-Key is required (max 240 characters)")
    content = payload.model_dump(mode="json")
    if content.get("confirmed_amount") is None:
        content.pop("confirmed_amount", None)  # Older evidence-review fingerprints.
    # Older addition retries did not contain this optional field.
    if isinstance(payload, Addition) and payload.payment_method is None:
        content.pop("payment_method", None)
    digest = fingerprint(content)
    existing = get_idempotent_response(db, scope, key, order.business_id)
    if existing:
        if existing.get("request_digest") != digest:
            raise HTTPException(409, "Idempotency key belongs to different content")
        return existing["result"], digest
    assert_version(order.version, payload.expected_version)
    return None, digest


async def finish(db, order, scope, key, digest, result):
    save_idempotent_response(db, scope, key, order.business_id, {"request_digest": digest, "result": result})
    db.commit()
    await hub.broadcast(order.branch_id, "order.updated", serialize_order(order))
    if result.get("tickets"):
        await hub.broadcast(order.branch_id, "kitchen.ticket_created", result)
    return result


@router.get("/integrations/context/catalog")
def catalog(response: Response, integration: IntegrationAuthContext = Depends(require_integration_scope("menu:read")), db: Session = Depends(get_db)):
    from .api import integration_branch_from_auth, serialize_catalog
    _, branch = integration_branch_from_auth(db, integration, None)
    raw = serialize_catalog(db, branch, available_only=False, digital_only=True)
    categories = [{k: c[k] for k in ("id", "name")} for c in raw["categories"] if c["active"]]
    active_categories = {c["id"] for c in categories}
    products = []
    for p in raw["products"]:
        if p["category_id"] is not None and p["category_id"] not in active_categories:
            continue
        item = {k: p[k] for k in ("id", "category_id", "name", "description", "price", "service_channels", "product_type", "combo_components")}
        capacity = product_capacity(db, db.get(Product, p["id"]))
        item["available"] = bool(p["available"] and capacity["available"] and not p["unavailable_reason"])
        item["variants"] = [v for v in p["variants"] if v["active"]]
        item["modifier_groups"] = [{**{k: g[k] for k in ("id", "name", "minimum", "maximum", "required", "allow_repeats", "max_per_option")},
            "modifiers": [m for m in g["modifiers"] if m["active"]]} for g in p["modifier_groups"]]
        products.append(item)
    response.headers["Cache-Control"] = "no-store"
    from .agent_catalog import complete_catalog
    return complete_catalog(db, branch, {"business_id": branch.business_id, "branch_id": branch.id, "categories": categories,
            "products": products, "promotions": raw["promotions"]})


@router.post("/integrations/orders/preview")
def preview(payload: Preview, response: Response, integration: IntegrationAuthContext = Depends(require_integration_scope("orders:write")), db: Session = Depends(get_db),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key")):
    from .api import integration_branch_from_auth
    from .agent_context import agent_context
    from .settings_service import create_delivery_quote, serialize_delivery_quote
    _, branch = integration_branch_from_auth(db, integration, None)
    scope = f"agent-checkout-preview:{branch.id}:{integration.sender or integration.credential_id}"
    digest = fingerprint(payload.model_dump(mode="json"))
    if idempotency_key:
        previous = get_idempotent_response(db, scope, idempotency_key, branch.business_id)
        if previous:
            if previous.get("request_digest") != digest:
                raise HTTPException(409, "Idempotency key belongs to different content")
            return previous["result"]
    draft = preview_items(db, branch, payload.channel, payload.items, payload.payment_method)
    is_restaurant_agent = integration.profile == "restaurant_agent"
    delivery = agent_context(db, branch, restaurant_agent=is_restaurant_agent)["delivery"]
    fee = Decimal(0)
    pending = False
    quote = None
    fee_status = "final"
    if payload.channel == "delivery" and payload.purpose == "initial":
        if not delivery["enabled"]:
            raise HTTPException(409, "Delivery is disabled")
        settings = db.scalar(select(BranchSettings).where(BranchSettings.branch_id == branch.id))
        if payload.delivery_address and settings:
            if not idempotency_key or len(idempotency_key) > 240:
                raise HTTPException(422, "Idempotency-Key is required for a delivery quote (max 240 characters)")
            destination = fulfillment_destination(Fulfillment(sender=integration.sender or "preview", expected_version=1,
                channel="delivery", delivery_address=payload.delivery_address))
            if is_restaurant_agent:
                from .agent_delivery import create_agent_delivery_quote
                quote = create_agent_delivery_quote(db, branch, settings, subtotal=draft.subtotal,
                    destination=destination, expected_configuration_version=settings.version, sender=integration.sender)
            else:
                quote = create_delivery_quote(db, branch, settings, subtotal=draft.subtotal,
                    destination=destination, expected_configuration_version=settings.version)
            quote.input_snapshot = {**quote.input_snapshot, "context": "agent_checkout", "sender": integration.sender}
            fee, pending = money(quote.fee), quote.fee is None
        elif delivery["mode"] == "quote":
            pending = True
        elif delivery["mode"] in {"fixed", "free"}:
            fee = money(delivery["fee"])
        else:
            pending = True
            fee_status = "destination_required"
        minimum = money(delivery["minimum_order_amount"])
        if draft.subtotal < minimum:
            raise HTTPException(409, "Delivery minimum purchase not reached")
        if quote is None and not pending and delivery["free_delivery_threshold"] is not None and draft.subtotal >= money(delivery["free_delivery_threshold"]):
            fee = Decimal(0)
    response.headers["Cache-Control"] = "no-store"
    result = {"business_id": branch.business_id, "branch_id": branch.id, **priced_snapshot(draft),
        "products_total": float(money(draft.subtotal - draft.discount)),
        "known_total": float(draft.total + fee), "final_total": None if pending else float(draft.total + fee),
        "delivery_fee": None if pending else float(fee),
        "delivery_fee_status": fee_status if fee_status == "destination_required" else "pending_quote" if pending else "final",
        "delivery_quote_id": quote.id if quote else None, "quote": serialize_delivery_quote(quote) if quote else None}
    if quote:
        save_idempotent_response(db, scope, idempotency_key, branch.business_id, {"request_digest": digest, "result": result})
        db.commit()
    return result


@router.get("/integrations/orders/{order_id}/customer-state")
def customer_state(order_id: int, sender: str, response: Response, integration: IntegrationAuthContext = Depends(require_integration_scope("orders:read")), db: Session = Depends(get_db)):
    order = scoped(db, integration, order_id, sender)
    attach_recent_agent_order_changes(db, [order])
    response.headers["Cache-Control"] = "no-store"
    from .command_revisions import effective_ticket_items
    from .models import PaymentEvidence
    from .api import serialize_payment_evidence
    paid = paid_order_amount(db, order)
    tickets = list(db.scalars(select(KitchenTicket).where(KitchenTicket.order_id == order.id)))
    is_open = order.status not in {"dispatched", "delivered", "cancelled", "closed"} and not order.table_released_at
    requests = requests_for_order(db, order)
    kitchen_completed = kitchen_was_completed(db, order)
    return {**serialize_order(order), "paid_amount": float(paid),
        "remaining_amount": float(max(Decimal(0), money(order.total) - paid)),
        "kitchen_completed": kitchen_completed,
        "allowed_actions": {
            "add_items": bool(is_open and order.status not in {"draft", "pending_confirmation"}
                              and order.payment_method in {"cash", "yape"}),
            "cancel_order": False,
            "revise_items": False,
            "change_fulfillment": is_open,
            "change_to_delivery": is_open,
            "change_to_takeaway": is_open,
            "fulfillment_change_block_reason": None if is_open else "order_dispatched_or_completed",
            "editable_item_ids": [],
            "choose_delivery_payment": bool(is_open and any(r.purpose == "delivery" and r.method == "unselected" for r in requests)),
        }, "payment_requests": [serialize_request(r) for r in requests],
        "payment_evidence": [{k: v for k, v in serialize_payment_evidence(e).items() if k in {
            "id", "payment_request_id", "expected_amount", "amount_detected", "operation_number", "security_code",
            "recipient", "status", "rejection_reason", "created_at", "reviewed_at"}} for e in db.scalars(select(PaymentEvidence).where(
            PaymentEvidence.order_id == order.id).order_by(PaymentEvidence.created_at, PaymentEvidence.id))],
        "tickets": [{"id": t.id, "status": t.status, "items": effective_ticket_items(t)} for t in tickets]}


def fulfillment_destination(payload):
    if payload.channel == "takeaway":
        if payload.delivery_address:
            raise HTTPException(422, "Pickup cannot include a delivery destination")
        return None
    from .integration_order_validation import validate_delivery_destination
    destination = deepcopy(payload.delivery_address or {})
    allowed = {"address", "full_address", "reference", "maps_url", "map_url", "latitude", "longitude",
               "delivery_service", "neighborhood", "street", "number", "cross_streets"}
    if set(destination) - allowed or destination.get("delivery_service", "own") != "own":
        raise HTTPException(422, "Only a structured own-delivery destination is allowed")
    if any(isinstance(value, str) and len(value) > 2000 for value in destination.values()):
        raise HTTPException(422, "Delivery destination is too long")
    validate_delivery_destination(destination)
    return destination


def assert_fulfillment_available(db, order, channel):
    from .services import service_channel_for_order, active_order_items
    assert_open(order)
    assignment = db.scalar(select(DeliveryAssignment).where(
        DeliveryAssignment.order_id == order.id).with_for_update())
    if assignment and (assignment.dispatched_at or assignment.delivered_at or assignment.status in {"dispatched", "delivered"}):
        raise CodedHTTPException(409, "Delivery was already dispatched", "ORDER_ITEMS_LOCKED")
    settings = db.scalar(select(BranchSettings).where(BranchSettings.branch_id == order.branch_id,
        BranchSettings.business_id == order.business_id).with_for_update())
    service = service_channel_for_order(channel, "whatsapp_agent")
    if settings and not getattr(settings, service):
        raise CodedHTTPException(409, "Service is disabled", "SERVICE_DISABLED")
    if channel != order.channel:
        for item in active_order_items(order):
            product = db.get(Product, item.product_id)
            if not product or service not in (product.service_channels or []):
                raise CodedHTTPException(409, "An ordered product is unavailable for this service", "PRODUCT_UNAVAILABLE_FOR_CHANNEL")
    return settings, assignment


@router.post("/integrations/orders/{order_id}/fulfillment-preview")
async def preview_fulfillment(order_id: int, payload: Fulfillment,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("orders:write")),
    db: Session = Depends(get_db)):
    from .settings_service import create_delivery_quote, serialize_delivery_quote
    from datetime import timedelta
    from uuid import uuid4
    order = scoped(db, integration, order_id, payload.sender)
    scope = f"agent-fulfillment-preview:{order.id}"
    previous, digest = replay(db, order, scope, idempotency_key, payload)
    if previous is not None:
        return previous
    settings, _ = assert_fulfillment_available(db, order, payload.channel)
    destination = fulfillment_destination(payload)
    branch = db.get(Branch, order.branch_id)
    if payload.channel == "delivery" and settings:
        if integration.profile == "restaurant_agent":
            from .agent_delivery import create_agent_delivery_quote
            quote = create_agent_delivery_quote(db, branch, settings, subtotal=order.subtotal,
                destination=destination, expected_configuration_version=settings.version, sender=integration.sender)
        else:
            quote = create_delivery_quote(db, branch, settings, subtotal=order.subtotal,
                destination=destination, expected_configuration_version=settings.version)
    else:
        if payload.channel == "delivery" and not branch.delivery_enabled:
            raise HTTPException(409, "Delivery is disabled")
        quote = DeliveryQuote(id=str(uuid4()), business_id=order.business_id, branch_id=order.branch_id,
            mode="fixed" if payload.channel == "delivery" else "free", subtotal=order.subtotal,
            fee=money(branch.delivery_fee) if payload.channel == "delivery" else Decimal(0),
            configuration_version=settings.version if settings else branch.version,
            input_snapshot={"destination": destination}, expires_at=utcnow() + timedelta(minutes=10))
        db.add(quote)
    quote.input_snapshot = {**(quote.input_snapshot or {}), "context": "agent_fulfillment",
        "order_id": order.id, "order_version": order.version, "sender": payload.sender,
        "channel": payload.channel, "settings_present": settings is not None,
        "branch_version": branch.version, "destination": destination,
        "delivery_charge_baseline": str(money(order.delivery_fee - sum((r.amount for r in requests_for_order(db, order)
            if r.purpose == "delivery" and r.status not in {*INACTIVE_REQUEST_STATUSES, "paid"}), Decimal(0))))}
    db.flush()
    result = {"order_id": order.id, "order_version": order.version, "quote": serialize_delivery_quote(quote),
        "channel": payload.channel, "delivery_address": destination,
        "previous_delivery_fee": float(order.delivery_fee),
        "products_total": float(money(order.subtotal - order.discount)),
        "final_total": float(money(order.subtotal - order.discount + quote.fee)) if quote.fee is not None else None}
    save_idempotent_response(db, scope, idempotency_key, order.business_id, {"request_digest": digest, "result": result})
    db.commit()
    return result


def reconcile_delivery_requests(db, order, old_fee, fee, quote_id):
    """Preserve paid requests; retire only old unpaid quotes/evidence, never payments."""
    unpaid = Decimal(0)
    for request in requests_for_order(db, order):
        if request.purpose != "delivery" or request.status in {*INACTIVE_REQUEST_STATUSES, "paid"}:
            continue
        unpaid += request.amount
        request.status = "superseded"
        request.snapshot = {**(request.snapshot or {}), "superseded_by_quote": quote_id}
        request.version += 1
        for evidence in db.scalars(select(PaymentEvidence).where(PaymentEvidence.payment_request_id == request.id,
                PaymentEvidence.status.in_(["evidence_received", "under_review"])).with_for_update()):
            evidence.status = "superseded"
            evidence.rejection_reason = "Delivery quote changed; original receipt retained for human review"
            order._delivery_evidence_superseded = [*getattr(order, "_delivery_evidence_superseded", []), evidence.id]
    if fee is None:
        return None
    additional = max(Decimal(0), money(fee - old_fee + unpaid))
    # Existing money (or an unchanged original receipt still awaiting review)
    # must not be requested again after several delivery changes.
    from .cash_service import order_refunds
    covered = paid_order_amount(db, order) - sum((money(r["amount"]) for r in order_refunds(db, order)), Decimal(0))
    original_evidence = db.scalar(select(PaymentEvidence).where(PaymentEvidence.order_id == order.id,
        PaymentEvidence.payment_request_id.is_(None), PaymentEvidence.status.in_(["evidence_received", "under_review"])))
    if original_evidence:
        covered = max(covered, money((original_evidence.analysis or {}).get("expected_amount", 0)))
    additional = min(additional, max(Decimal(0), money(order.subtotal - order.discount + fee - covered)))
    if not additional:
        return None
    request = OrderPaymentRequest(business_id=order.business_id, branch_id=order.branch_id, order_id=order.id,
        purpose="delivery", method="unselected", amount=additional, status="pending",
        snapshot={"reason": "fulfillment_change", "quote_id": quote_id})
    db.add(request)
    return request


@router.patch("/integrations/orders/{order_id}/fulfillment")
async def change_fulfillment(order_id: int, payload: FulfillmentCommit,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("orders:write")),
    db: Session = Depends(get_db)):
    from .agent_attention import create_attention
    from .agent_order_changes import update_active_ticket_fulfillment
    from .pos_printing import refresh_unclaimed_fulfillment_print_jobs
    from .settings_service import sync_pending_kitchen_print_jobs
    from .cash_service import financial_summary, order_refunds
    order = scoped(db, integration, order_id, payload.sender)
    scope = f"agent-fulfillment:{order.id}"
    previous, digest = replay(db, order, scope, idempotency_key, payload)
    if previous is not None:
        return previous
    settings, assignment = assert_fulfillment_available(db, order, payload.channel)
    destination = fulfillment_destination(payload)
    quote = db.scalar(select(DeliveryQuote).where(DeliveryQuote.id == payload.quote_id,
        DeliveryQuote.business_id == order.business_id, DeliveryQuote.branch_id == order.branch_id))
    snapshot = (quote.input_snapshot or {}) if quote else {}
    branch = db.get(Branch, order.branch_id)
    if not quote or snapshot.get("context") != "agent_fulfillment" or snapshot.get("order_id") != order.id or snapshot.get("order_version") != order.version or snapshot.get("sender") != payload.sender:
        raise CodedHTTPException(409, "Request a current quote for this order", "DELIVERY_QUOTE_CONTEXT_MISMATCH")
    if integration.profile == "restaurant_agent" and payload.channel == "delivery" and settings:
        from .agent_delivery import POLICY_VERSION
        if snapshot.get("agent_policy_version") != POLICY_VERSION:
            raise CodedHTTPException(409, "Delivery policy changed; request a new quote", "DELIVERY_CONFIGURATION_STALE")
    if snapshot.get("destination") != destination or snapshot.get("channel") != payload.channel:
        raise CodedHTTPException(409, "Delivery destination changed", "DELIVERY_QUOTE_DESTINATION_CHANGED")
    if quote.expires_at.replace(tzinfo=timezone.utc) <= utcnow():
        raise CodedHTTPException(409, "Delivery quote expired", "DELIVERY_QUOTE_EXPIRED")
    if (bool(settings) != snapshot.get("settings_present") or
        quote.configuration_version != (settings.version if settings else branch.version) or
        snapshot.get("branch_version") != branch.version or money(quote.subtotal) != money(order.subtotal)):
        raise CodedHTTPException(409, "Delivery configuration changed", "DELIVERY_CONFIGURATION_STALE")
    if (quote.fee is None) != (payload.expected_delivery_fee is None) or (quote.fee is not None and money(quote.fee) != money(payload.expected_delivery_fee)):
        raise CodedHTTPException(409, "Review the quoted delivery fee", "DELIVERY_FEE_CHANGED")
    before = {"channel": order.channel, "delivery_address": deepcopy(order.delivery_address),
        "delivery_fee": str(order.delivery_fee), "delivery_fee_status": order.delivery_fee_status, "total": str(order.total)}
    old_fee = money(order.delivery_fee)
    user = AuthContext("integration", "owner", order.business_id, order.branch_id)
    order.channel, order.delivery_address, order.delivery_quote_id = payload.channel, destination, quote.id
    order.delivery_fee = money(quote.fee) if quote.fee is not None else Decimal(0)
    order.delivery_fee_status = "pending_quote" if quote.fee is None else "final"
    request = reconcile_delivery_requests(db, order, old_fee, quote.fee, quote.id) if old_fee != quote.fee else None
    order.total = money(order.subtotal - order.discount + order.delivery_fee)
    if assignment:
        if payload.channel == "delivery":
            assignment.address, assignment.fee = deepcopy(destination), order.delivery_fee
        else:
            courier_id = assignment.courier_id
            assignment.status, assignment.courier_id = "cancelled", None
            db.flush()
            if courier_id and not db.scalar(select(DeliveryAssignment.id).where(
                    DeliveryAssignment.courier_id == courier_id, DeliveryAssignment.id != assignment.id,
                    DeliveryAssignment.status.notin_(["cancelled", "delivered"]))):
                courier = db.get(Courier, courier_id)
                if courier:
                    courier.status = "available"
    order.version += 1
    paid = sync_order_payment_status(db, order)
    financial = financial_summary(order, paid, sum((money(r["amount"]) for r in order_refunds(db, order)), Decimal(0)))
    details = {"before": before, "after": {"channel": order.channel, "delivery_address": deepcopy(destination),
        "delivery_fee": str(order.delivery_fee), "delivery_fee_status": order.delivery_fee_status, "total": str(order.total)},
        "order_version": order.version, "quote_id": quote.id}
    financial_review = bool(getattr(order, "_delivery_evidence_superseded", []))
    for evidence in db.scalars(select(PaymentEvidence).where(PaymentEvidence.order_id == order.id,
            PaymentEvidence.payment_request_id.is_(None), PaymentEvidence.status.in_(["under_review", "evidence_received"])).with_for_update()):
        if quote.fee is not None and money((evidence.analysis or {}).get("expected_amount", before["total"])) > order.total:
            evidence.analysis = {**(evidence.analysis or {}), "financial_review": {
                "kind": "fulfillment_amount_changed", "original_expected_amount": (evidence.analysis or {}).get("expected_amount"),
                "current_total": float(order.total), "order_version": order.version, "quote_id": quote.id},
                "requires_financial_review": True}
            financial_review = True
    create_attention(db, business_id=order.business_id, branch_id=order.branch_id, sender=payload.sender,
        kind="fulfillment_change", summary="El cliente cambió la modalidad o el destino de entrega.",
        order_id=order.id, source_key=f"fulfillment:{quote.id}", details=details)
    if financial_review or (quote.fee is not None and financial["overpayment_amount"] > 0):
        create_attention(db, business_id=order.business_id, branch_id=order.branch_id, sender=payload.sender,
            kind="financial_review", summary="El cambio de entrega requiere revisar el importe cobrado o el comprobante.",
            order_id=order.id, source_key=f"fulfillment-finance:{quote.id}", details={**details, "financial_summary": financial})
    updated_tickets = update_active_ticket_fulfillment(db, user, order)
    sync_pending_kitchen_print_jobs(db, order, updated_tickets, preserve_local=True)
    db.flush()
    refresh_unclaimed_fulfillment_print_jobs(db, order, updated_tickets)
    marker = record_agent_order_change(db, user, order, "agent.fulfillment_updated", details)
    result = {"order": serialize_order(order), "financial_summary": financial, "changed": True,
        "payment_request": serialize_request(request) if request else None, "recent_modification": marker,
        "command_committed": False, "tickets": []}
    return await finish(db, order, scope, idempotency_key, digest, result)



@router.post("/integrations/orders/{order_id}/item-batches")
async def add_items(order_id: int, payload: Addition, idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("orders:write")), db: Session = Depends(get_db)):
    order = scoped(db, integration, order_id, payload.sender)
    scope = f"agent-addition:{order.id}"
    previous, digest = replay(db, order, scope, idempotency_key, payload)
    if previous is not None:
        return previous
    assert_registered(order)
    method = payload.payment_method or order.payment_method
    if method not in {"cash", "yape"} or order.payment_method not in {"cash", "yape"}:
        raise HTTPException(409, "A staff member must handle this payment method")
    draft = preview_items(db, db.get(Branch, order.branch_id), order.channel, payload.items, method)
    if money(payload.expected_amount) != draft.total:
        raise HTTPException(409, "Review the updated addition price")
    user = AuthContext("integration", "owner", order.business_id, order.branch_id)
    waiting_base = initial_payment_pending(db, order)
    if method == "yape" or waiting_base:
        request = OrderPaymentRequest(business_id=order.business_id, branch_id=order.branch_id,
            order_id=order.id, purpose="addition", method=method, amount=draft.total,
            status="awaiting_base_review" if waiting_base else "pending",
            snapshot={"lines": [x.model_dump(mode="json") for x in payload.items], "pricing": priced_snapshot(draft),
                "blocked_reason": "initial_payment_pending" if waiting_base else None})
        db.add(request)
        db.flush()
        order.version += 1
        result = {"order": serialize_order(order), "payment_request": serialize_request(request),
                  "sent_to_kitchen": False, "command_committed": False, "tickets": [], "appended_item_ids": [],
                  "payment_method": method, "addition_amount": float(draft.total)}
        audit(db, user, "agent.addition_requested", "order", order.id, order.business_id,
              {"request_id": request.id}, branch_id=order.branch_id)
    else:
        new_items, tickets = append_order_item_batch(db, user, order, payload.items,
            allow_paid_addition=True, preserve_existing_pricing=True, ticket_context={"agent_addition": True})
        record_agent_order_change(db, user, order, "agent.items_added",
            {"item_ids": [item.id for item in new_items], "ticket_ids": [ticket.id for ticket in tickets]})
        db.flush()
        from .api import serialize_ticket
        result = {"order": serialize_order(order), "sent_to_kitchen": bool(tickets), "command_committed": bool(tickets),
                  "tickets": [serialize_ticket(ticket, order=order) for ticket in tickets],
                  "appended_item_ids": [item.id for item in new_items],
                  "payment_method": method, "addition_amount": float(draft.total)}
    return await finish(db, order, scope, idempotency_key, digest, result)


@router.post("/integrations/orders/{order_id}/item-revisions")
async def revise_items(order_id: int, payload: Revision, idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("orders:write")), db: Session = Depends(get_db)):
    scoped(db, integration, order_id, payload.sender)
    if not idempotency_key or len(idempotency_key) > 240:
        raise HTTPException(422, "Idempotency-Key is required (max 240 characters)")
    reject_agent_order_change()



@router.post("/orders/{order_id}/addition-requests/{request_id}/resolve")
async def resolve_addition_request(order_id: int, request_id: str, payload: AdditionResolution,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager", "cashier")), db: Session = Depends(get_db)):
    from .api import scoped_order_for_user
    from .agent_checkout import ready_event
    order = scoped_order_for_user(db, user, order_id, for_update=True)
    scope = f"addition-resolution:{order.id}:{request_id}"
    previous, digest = replay(db, order, scope, idempotency_key, payload)
    if previous is not None:
        return previous
    request = payment_request(db, order, request_id)
    if (request.purpose != "addition" or request.payment_id is not None
            or request.status not in {"pending", "under_review", "awaiting_base_review", "requires_review", "rejected"}
            or (request.snapshot or {}).get("appended_item_ids")):
        raise CodedHTTPException(409, "Only an uncommitted addition request can be declined", "PAYMENT_REQUEST_LOCKED")
    note = payload.note.strip()
    if not note:
        raise HTTPException(422, "A reason is required")
    evidences = list(db.scalars(select(PaymentEvidence).where(
        PaymentEvidence.order_id == order.id, PaymentEvidence.payment_request_id == request.id).with_for_update()))
    if any(e.status == "paid" for e in evidences):
        raise CodedHTTPException(409, "A paid addition cannot be declined", "PAYMENT_REQUEST_LOCKED")
    previous_status = request.status
    resolved_at = utcnow()
    for evidence in evidences:
        if evidence.status in {"under_review", "evidence_received"}:
            evidence.status = "rejected"
            evidence.rejection_reason = note
            evidence.reviewed_by = user.user_id
            evidence.reviewed_at = resolved_at
    request.status = "declined"
    request.snapshot = {**(request.snapshot or {}), "blocked_reason": None, "resolution_note": note,
        "resolved_at": resolved_at.isoformat(), "resolved_by": user.user_id}
    request.version += 1
    order.version += 1
    audit(db, user, "payment_request.declined", "order_payment_request", request.id, order.business_id,
        {"order_id": order.id, "previous_status": previous_status, "reason": note}, branch_id=order.branch_id)
    event = ready_event(db, order)
    return await finish(db, order, scope, idempotency_key, digest, {"order": serialize_order(order),
        "payment_request": serialize_request(request), "event_id": event.id if event else None})


@router.patch("/orders/{order_id}/delivery-fee")
async def set_delivery_fee(order_id: int, payload: DeliveryFee, idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager", "cashier", "dispatcher")), db: Session = Depends(get_db)):
    from .api import scoped_order_for_user
    order = scoped_order_for_user(db, user, order_id, for_update=True)
    scope = f"agent-delivery-fee:{order.id}"
    previous, digest = replay(db, order, scope, idempotency_key, payload)
    if previous is not None:
        return previous
    assert_open(order)
    if not pending_delivery_allowed(order):
        raise HTTPException(409, "Only a pending WhatsApp delivery fee can be confirmed here")
    settings = db.scalar(select(BranchSettings).where(BranchSettings.branch_id == order.branch_id))
    if payload.method and settings and payload.method not in (settings.payment_methods or {}).get("delivery", []):
        raise HTTPException(409, "Payment method is disabled")
    quote = db.get(DeliveryQuote, order.delivery_quote_id) if order.delivery_quote_id else None
    change_quote = quote and (quote.input_snapshot or {}).get("context") == "agent_fulfillment"
    baseline = money(quote.input_snapshot.get("delivery_charge_baseline", 0)) if change_quote else Decimal(0)
    order.delivery_fee = money(payload.amount)
    order.delivery_fee_status = "final"
    order.total = money(order.subtotal - order.discount + order.delivery_fee)
    request = reconcile_delivery_requests(db, order, baseline, order.delivery_fee, order.delivery_quote_id) if change_quote else None
    if request is None:
        amount = Decimal(0) if change_quote else order.delivery_fee
        request = OrderPaymentRequest(business_id=order.business_id, branch_id=order.branch_id, order_id=order.id,
            purpose="delivery", method=payload.method or "unselected", amount=amount,
            status="pending" if amount else "waived", snapshot={"delivery_charge_baseline": str(baseline)})
        db.add(request)
    elif payload.method:
        request.method = payload.method
    request.snapshot = {**(request.snapshot or {}), "quote_id": order.delivery_quote_id}
    order.version += 1
    paid = sync_order_payment_status(db, order)
    assignment = db.scalar(select(DeliveryAssignment).where(DeliveryAssignment.order_id == order.id).with_for_update())
    if assignment:
        assignment.fee = order.delivery_fee
    if change_quote:
        from .agent_attention import create_attention
        from .cash_service import financial_summary, order_refunds
        financial = financial_summary(order, paid, sum((money(r["amount"]) for r in order_refunds(db, order)), Decimal(0)))
        needs_review = False
        for evidence in db.scalars(select(PaymentEvidence).where(PaymentEvidence.order_id == order.id,
            PaymentEvidence.payment_request_id.is_(None), PaymentEvidence.status.in_(["under_review", "evidence_received"])).with_for_update()):
            if money((evidence.analysis or {}).get("expected_amount", order.total)) > order.total:
                evidence.analysis = {**(evidence.analysis or {}), "requires_financial_review": True,
                    "financial_review": {"kind": "fulfillment_amount_changed", "current_total": float(order.total),
                        "order_version": order.version, "quote_id": quote.id,
                        "original_expected_amount": (evidence.analysis or {}).get("expected_amount")}}
                needs_review = True
        if needs_review or financial["overpayment_amount"] > 0:
            create_attention(db, business_id=order.business_id, branch_id=order.branch_id,
                sender=order.whatsapp_chat_id or order.customer_phone, kind="financial_review",
                summary="La tarifa confirmada requiere revisar el pago o el comprobante original.",
                order_id=order.id, source_key=f"delivery-confirm:{order.id}:{digest}",
                details={"financial_summary": financial, "quote_id": quote.id, "order_version": order.version})
    db.flush()
    audit(db, user, "order.delivery_fee_confirmed", "order", order.id, order.business_id,
          {"amount": str(order.delivery_fee), "method": request.method}, branch_id=order.branch_id)
    from .services import create_integration_event
    from .models import Business
    event = create_integration_event(db, order, "order.delivery_fee_confirmed", {
        "payment_request_id": request.id, "quote_id": order.delivery_quote_id,
        "delivery_fee": float(order.delivery_fee), "request_amount": float(request.amount),
        "currency": db.get(Business, order.business_id).currency,
        "order_version": order.version, "request_version": request.version,
    })
    return await finish(db, order, scope, idempotency_key, digest, {"order": serialize_order(order),
        "payment_request": serialize_request(request), "event_id": event.id})


@router.patch("/integrations/orders/{order_id}/delivery-payment")
async def choose_delivery_payment(order_id: int, payload: DeliveryMethod, idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("orders:write")), db: Session = Depends(get_db)):
    order = scoped(db, integration, order_id, payload.sender)
    scope = f"agent-delivery-method:{order.id}"
    previous, digest = replay(db, order, scope, idempotency_key, payload)
    if previous is not None:
        return previous
    user = AuthContext("integration", "owner", order.business_id, order.branch_id)
    request = select_delivery_method(db, user, order, payload.method)
    return await finish(db, order, scope, idempotency_key, digest, {"order": serialize_order(order), "payment_request": serialize_request(request)})


@router.patch("/orders/{order_id}/delivery-payment")
async def staff_delivery_payment(order_id: int, payload: StaffDeliveryMethod, idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager", "cashier", "dispatcher")), db: Session = Depends(get_db)):
    from .api import scoped_order_for_user
    order = scoped_order_for_user(db, user, order_id, for_update=True)
    scope = f"staff-delivery-method:{order.id}"
    previous, digest = replay(db, order, scope, idempotency_key, payload)
    if previous is not None:
        return previous
    request = select_delivery_method(db, user, order, payload.method)
    return await finish(db, order, scope, idempotency_key, digest, {"order": serialize_order(order), "payment_request": serialize_request(request)})


def select_delivery_method(db, user, order, method):
    assert_open(order)
    candidates = [r for r in requests_for_order(db, order) if r.purpose == "delivery" and r.status not in {*INACTIVE_REQUEST_STATUSES, "paid"}]
    if len(candidates) != 1:
        raise HTTPException(409, "The delivery fee must be confirmed first")
    request = payment_request(db, order, candidates[0].id)
    if request.method != "unselected":
        raise HTTPException(409, "Delivery payment already selected; ask a staff member to change it")
    settings = db.scalar(select(BranchSettings).where(BranchSettings.branch_id == order.branch_id))
    if settings and method not in (settings.payment_methods or {}).get("delivery", []):
        raise HTTPException(409, "Payment method is disabled")
    request.method = method
    request.version += 1
    order.version += 1
    audit(db, user, "order.delivery_payment_selected", "order", order.id, order.business_id,
          {"request_id": request.id, "method": method}, branch_id=order.branch_id)
    return request
