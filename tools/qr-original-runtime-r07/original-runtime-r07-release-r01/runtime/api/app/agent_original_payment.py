"""Customer choice of the original order's method; never payment approval.

Caller holds the same Order lock used by staff approval. No products, prices,
accepted payments, existing commands or historical print snapshots are edited.
"""
from decimal import Decimal
from sqlalchemy import select
from .errors import CodedHTTPException
from .models import (Branch, BranchSettings, KitchenTicket, OrderPaymentRequest,
                     Payment, PaymentEvidence, utcnow)
from .services import active_order_items, audit, confirm_order, money, send_order_to_kitchen
from .agent_checkout import fingerprint, INACTIVE_REQUEST_STATUSES, resume_base_additions

REVIEWABLE = {"evidence_received", "under_review"}
METHOD_CHANGE_KEY = "original_payment_method_change"


def require_accepted_original(order):
    # This operation must not turn an unaccepted checkout draft into a purchase
    # after closing. The ordinary checkout keeps its own fresh service guard.
    if order.status in {"draft", "pending_confirmation"}:
        raise CodedHTTPException(409, "Complete the current checkout before changing a registered order's payment method", "ORDER_NOT_REGISTERED")


def original_payment_amount(db, order, *, tickets=None, requests=None):
    """Use persisted additions and delivery obligations, never recalculate catalog.

    Pending additions are not part of order.total. Committed additions have
    their own command item identities, so their frozen net prices are excluded.
    A separately quoted delivery request is also independent of the original.
    """
    tickets = tickets if tickets is not None else list(db.scalars(select(KitchenTicket).where(
        KitchenTicket.order_id == order.id, KitchenTicket.business_id == order.business_id,
        KitchenTicket.branch_id == order.branch_id)))
    requests = requests if requests is not None else list(db.scalars(select(OrderPaymentRequest).where(
        OrderPaymentRequest.order_id == order.id, OrderPaymentRequest.business_id == order.business_id,
        OrderPaymentRequest.branch_id == order.branch_id)))
    items = {item.id: item for item in active_order_items(order)}
    def unverifiable():
        raise CodedHTTPException(409, "The original payment allocation needs staff review", "ORIGINAL_PAYMENT_ALLOCATION_UNVERIFIABLE")
    ticket_ids_by_request = {}
    direct_ids = set()
    for ticket in tickets:
        context = ticket.context_snapshot or {}
        if context.get("agent_addition") is not True:
            continue
        rows = ticket.items_snapshot or []
        if not rows or any(not isinstance(row, dict) or type(row.get("item_id")) is not int for row in rows):
            unverifiable()
        identities = {row["item_id"] for row in rows}
        if not identities.issubset(items):
            unverifiable()
        request_id = context.get("agent_payment_request_id")
        if request_id:
            ticket_ids_by_request.setdefault(request_id, set()).update(identities)
        else:
            direct_ids.update(identities)
    # A retained request is not yet part of the total. Once committed, its
    # durable item IDs and frozen amount identify its independent obligation.
    # Tickets also cover direct cash additions which have no payment request.
    claimed_ids = set()
    additions = Decimal(0)
    for request in requests:
        if request.purpose != "addition":
            continue
        appended = (request.snapshot or {}).get("appended_item_ids", [])
        ticket_ids = ticket_ids_by_request.pop(request.id, set())
        if request.status not in {"committed", "paid"}:
            if appended or ticket_ids:
                unverifiable()
            continue
        if (not isinstance(appended, list) or
                any(type(value) is not int for value in appended)):
            unverifiable()
        identities = set(appended) or ticket_ids
        if (not identities or len(set(appended)) != len(appended) or
                (appended and ticket_ids and set(appended) != ticket_ids) or
                not identities.issubset(items) or identities & claimed_ids):
            unverifiable()
        pricing = (request.snapshot or {}).get("pricing", {})
        if "amount" not in pricing or money(pricing["amount"]) != money(request.amount) or money(request.amount) <= 0:
            unverifiable()
        claimed_ids.update(identities)
        additions += money(request.amount)
    if ticket_ids_by_request or direct_ids & claimed_ids:
        unverifiable()
    additions += sum((money(items[item_id].line_total) - money(items[item_id].promotion_discount or 0)
                      for item_id in direct_ids), Decimal(0))
    delivery = sum((money(request.amount) for request in requests if request.purpose == "delivery"
                    and request.status not in INACTIVE_REQUEST_STATUSES), Decimal(0))
    if delivery < 0 or delivery > money(order.delivery_fee) or additions < 0:
        unverifiable()
    amount = money(order.total) - money(additions) - money(delivery)
    if amount <= 0 or amount > money(order.total):
        raise CodedHTTPException(409, "The original payment allocation needs staff review", "ORIGINAL_PAYMENT_ALLOCATION_UNVERIFIABLE")
    return money(amount)


def original_payment_policy(db, order, target=None):
    """Current order capability under its lock, including actual payment rows."""
    payments = list(db.scalars(select(Payment).where(Payment.order_id == order.id,
        Payment.business_id == order.business_id).order_by(Payment.id).with_for_update()))
    evidence = list(db.scalars(select(PaymentEvidence).where(PaymentEvidence.order_id == order.id,
        PaymentEvidence.business_id == order.business_id).order_by(PaymentEvidence.id).with_for_update()))
    requests = list(db.scalars(select(OrderPaymentRequest).where(OrderPaymentRequest.order_id == order.id,
        OrderPaymentRequest.business_id == order.business_id, OrderPaymentRequest.branch_id == order.branch_id)
        .order_by(OrderPaymentRequest.id).with_for_update()))
    tickets = list(db.scalars(select(KitchenTicket).where(KitchenTicket.order_id == order.id,
        KitchenTicket.business_id == order.business_id, KitchenTicket.branch_id == order.branch_id)
        .order_by(KitchenTicket.sequence, KitchenTicket.id).with_for_update()))
    settings = db.scalar(select(BranchSettings).where(BranchSettings.business_id == order.business_id,
        BranchSettings.branch_id == order.branch_id))
    branch = db.get(Branch, order.branch_id)
    published = (settings.payment_methods or {}).get(order.channel, []) if settings else (branch.accepted_payment_methods or [])
    methods = [method for method in ["cash", "yape"] if method in published]
    approved = [payment for payment in payments if payment.status == "confirmed"]
    approved_evidence = any(row.status == "paid" for row in evidence)
    current_original = [row for row in evidence if row.payment_request_id is None]
    initial_paid = any(row.status == "paid" for row in current_original)
    current_receipt = any(row.status in REVIEWABLE and not (row.analysis or {}).get(METHOD_CHANGE_KEY)
                          for row in current_original)
    block = None
    if order.status in {"draft", "pending_confirmation"}:
        block = "ORDER_NOT_REGISTERED"
    elif order.status in {"cancelled", "closed", "delivered"}:
        block = "ORDER_PAYMENT_CLOSED"
    elif approved or approved_evidence:
        block = "ORIGINAL_PAYMENT_ALREADY_APPROVED"
    elif order.payment_method not in {"cash", "yape"}:
        block = "ORIGINAL_PAYMENT_METHOD_UNSUPPORTED"
    elif target is not None and target not in methods:
        block = "PAYMENT_METHOD_DISABLED"
    elif not tickets and order.status not in {"draft", "pending_confirmation", "confirmed"}:
        block = "KITCHEN_HISTORY_UNVERIFIABLE"
    amount = None
    try:
        amount = original_payment_amount(db, order, tickets=tickets, requests=requests)
    except CodedHTTPException:
        block = block or "ORIGINAL_PAYMENT_ALLOCATION_UNVERIFIABLE"
    info = {"allowed": block is None, "methods": methods, "block_reason": block,
        "requires_human_review": block is not None, "has_approved_payments": bool(approved),
        "has_approved_evidence": approved_evidence,
        "paid_amount": float(sum((money(payment.amount) for payment in approved), Decimal(0))),
        "initial_payment_amount": float(amount) if amount is not None else None,
        "kitchen_already_released": bool(tickets),
        "initial_receipt_required": order.status not in {"draft", "pending_confirmation", "cancelled", "closed", "delivered"} and amount is not None
            and block != "KITCHEN_HISTORY_UNVERIFIABLE"
            and order.payment_method == "yape" and not initial_paid and not current_receipt,
        "initial_receipt_under_review": current_receipt}
    snapshot = {"business_id": order.business_id, "branch_id": order.branch_id,
        "order_id": order.id, "order_version": order.version, "order_status": order.status,
        "from_method": order.payment_method, "to_method": target, "info": info,
        "settings_version": settings.version if settings else None,
        "payments": [(payment.id, payment.status, str(payment.amount)) for payment in payments],
        "evidence": [(row.id, row.status, row.payment_request_id, bool((row.analysis or {}).get(METHOD_CHANGE_KEY))) for row in evidence],
        "requests": [(row.id, row.version, row.method, row.status, str(row.amount), row.payment_id) for row in requests],
        "tickets": [(ticket.id, ticket.sequence, ticket.status, ticket.version) for ticket in tickets]}
    return info, fingerprint(snapshot), evidence, requests, tickets


def require_current_original_evidence(order, evidence):
    """A former receipt can never become valid again after method round trips."""
    if evidence.payment_request_id is None and (
            (evidence.analysis or {}).get(METHOD_CHANGE_KEY) or order.payment_method != evidence.provider):
        raise CodedHTTPException(409, "This receipt belongs to a former payment method; its history is preserved. Request a new receipt for the current method.", "PAYMENT_EVIDENCE_METHOD_CHANGED")


def apply_original_payment_method(db, user, order, sender, method, preview_fingerprint):
    """Return a transaction result; route owns idempotency, commit and broadcasts."""
    info, current_fingerprint, evidence, requests, existing_tickets = original_payment_policy(db, order, method)
    if current_fingerprint != preview_fingerprint:
        raise CodedHTTPException(409, "Payment conditions changed; obtain a current preview", "ORIGINAL_PAYMENT_PREVIEW_STALE")
    if method == order.payment_method:
        return {"changed": False, "requires_human_review": False, "attention": None,
            "payment_method": order.payment_method, "command_committed": False, "tickets": [],
            "appended_item_ids": [], "initial_receipt_required": info["initial_receipt_required"]}
    if order.status in {"cancelled", "closed", "delivered"}:
        raise CodedHTTPException(409, "This order is historical and its payment method cannot be changed", "ORDER_PAYMENT_CLOSED")
    if not info["allowed"]:
        from .agent_attention import create_attention
        attention = create_attention(db, business_id=order.business_id, branch_id=order.branch_id,
            sender=sender, order_id=order.id, kind="financial_review",
            summary="El consumidor solicita cambiar el medio de pago original; necesita revisión del encargado.",
            source_key=f"original-payment-review:{order.id}:{method}",
            details={"kind": "original_payment_method_change", "from_method": order.payment_method,
                "requested_method": method, "blocked_reason": info["block_reason"],
                "paid_amount": info["paid_amount"], "order_version": order.version})
        return {"changed": False, "requires_human_review": True, "attention": {"id": attention.id},
            "block_reason": info["block_reason"], "payment_method": order.payment_method,
            "command_committed": False, "tickets": [], "appended_item_ids": [],
            "initial_receipt_required": False}
    before = order.payment_method
    invalidated = []
    marker = {"from_method": before, "to_method": method, "order_version": order.version,
        "at": utcnow().isoformat(), "actor": "verified_customer_choice"}
    for row in evidence:
        if row.payment_request_id is None and row.status != "paid" and not (row.analysis or {}).get(METHOD_CHANGE_KEY):
            row.analysis = {**(row.analysis or {}), METHOD_CHANGE_KEY: marker}
            if row.status in REVIEWABLE:
                row.status = "superseded"
                row.rejection_reason = "Comprobante conservado como historial: el consumidor cambió el medio de pago. No fue aprobado ni rechazado como pago."
            invalidated.append(row.id)
    order.payment_method = method
    order.payment_status = "pending"
    db.flush()  # Superseded original evidence no longer blocks the same transaction.
    new_tickets = []
    if method == "cash":
        if not existing_tickets:
            if order.status in {"draft", "pending_confirmation"}:
                confirm_order(db, user, order)
            order.submitted_at = order.submitted_at or utcnow()
            new_tickets.extend(send_order_to_kitchen(db, user, order))
        # Durable addition choices keep their own methods and review dependency.
        new_tickets.extend(resume_base_additions(db, user, order))
    order.version += 1
    audit(db, user, "agent.original_payment_method_changed", "order", order.id, order.business_id,
        {**marker, "initial_payment_amount": info["initial_payment_amount"],
            "superseded_evidence_ids": invalidated, "new_ticket_ids": [ticket.id for ticket in new_tickets],
            "existing_ticket_ids_preserved": [ticket.id for ticket in existing_tickets]}, branch_id=order.branch_id)
    from .api import serialize_ticket
    return {"changed": True, "requires_human_review": False, "attention": None,
        "payment_method": method, "command_committed": bool(new_tickets),
        "tickets": [serialize_ticket(ticket, order=order) for ticket in new_tickets],
        "appended_item_ids": sorted({row["item_id"] for ticket in new_tickets
            if (ticket.context_snapshot or {}).get("agent_addition") is True
            for row in ticket.items_snapshot if type(row.get("item_id")) is int}),
        "initial_receipt_required": method == "yape", "initial_payment_amount": info["initial_payment_amount"],
        "kitchen_already_released": bool(existing_tickets), "superseded_evidence_ids": invalidated}
