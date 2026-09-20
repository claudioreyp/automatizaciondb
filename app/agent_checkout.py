"""Narrow agent checkout rules, independent of the POS's paid-order editor."""
import hashlib
import json
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import func, select

from .errors import CodedHTTPException
from .models import (BranchSettings, IntegrationEvent, KitchenTicket, Order, OrderPaymentRequest,
                     Payment, PaymentEvidence, Product, ScheduleAssignment, ServiceSchedule, utcnow)
from .schemas import OrderLineInput, PaymentCreate
from .services import (active_order_items, add_payment, audit, build_order_item, commit_order_items_stock,
                       create_integration_event, create_kitchen_tickets, money, prepare_pos_print_intent,
                       product_capacity, recalculate_order, service_channel_for_order, sync_order_payment_status)
from .settings_service import schedule_is_open

AGENT_SOURCES = {"agent", "n8n", "whatsapp", "whatsapp_agent"}
TERMINAL = {"dispatched", "delivered", "cancelled", "closed"}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def assert_sender(order, sender):
    def normalize(value):
        value = (value or "").strip().lower().replace("@s.whatsapp.net", "@c.us")
        return value if value.endswith("@lid") else value.removesuffix("@c.us").lstrip("+")
    if not sender or normalize(sender) not in {normalize(order.whatsapp_chat_id), normalize(order.customer_phone)}:
        raise HTTPException(404, "Order not found")
    if order.source not in AGENT_SOURCES or order.table_id:
        raise CodedHTTPException(409, "This order requires a staff member", "AGENT_ORDER_NOT_EDITABLE")


def assert_open(order):
    if order.status in TERMINAL or order.table_released_at:
        raise CodedHTTPException(409, "Order already dispatched or completed; start a new purchase", "ORDER_ITEMS_LOCKED")


def pending_delivery_allowed(order):
    return order.source in AGENT_SOURCES and order.channel == "delivery" and order.delivery_fee_status == "pending_quote"


def preview_items(db, branch, channel, lines, method=None):
    if channel not in {"counter", "takeaway", "delivery"} or not lines:
        raise HTTPException(422, "Select a service and at least one product")
    settings = db.scalar(select(BranchSettings).where(BranchSettings.branch_id == branch.id))
    service = "pos_counter" if channel == "counter" else service_channel_for_order(channel, "whatsapp_agent")
    if settings:
        if not getattr(settings, service):
            raise CodedHTTPException(409, "Service is disabled", "SERVICE_DISABLED")
        if method and method not in (settings.payment_methods or {}).get(channel, []):
            raise CodedHTTPException(409, "Payment method is disabled", "PAYMENT_METHOD_DISABLED")
    draft = Order(business_id=branch.business_id, branch_id=branch.id, channel=channel,
                  source="whatsapp_agent", delivery_fee=0, manual_discount=0, status="draft")
    quantities = {}
    for line in lines:
        if not line.product_id or line.unit_price is not None:
            raise HTTPException(422, "Select a catalog product; price overrides are not allowed")
        item = build_order_item(db, branch.business_id, branch.id, line, channel, "whatsapp_agent")
        draft.items.append(item)
        quantities[line.product_id] = quantities.get(line.product_id, Decimal(0)) + line.quantity
    for product_id, quantity in quantities.items():
        product = db.get(Product, product_id)
        capacity = product_capacity(db, product)
        if not capacity["available"] or (product.track_stock and capacity["available_units"] is not None and quantity > capacity["available_units"]):
            raise CodedHTTPException(409, f"{product.name} is unavailable in that quantity", "PRODUCT_UNAVAILABLE")
    schedules = db.scalars(select(ServiceSchedule).where(
        ServiceSchedule.branch_id == branch.id, ServiceSchedule.active.is_(True),
        ServiceSchedule.archived_at.is_(None), ServiceSchedule.kind == "primary"))
    if any(not schedule_is_open(db, schedule) for schedule in schedules):
        raise CodedHTTPException(409, "Restaurant is currently closed", "SERVICE_CLOSED")
    additional = db.scalars(select(ServiceSchedule).join(ScheduleAssignment).where(
        ServiceSchedule.branch_id == branch.id, ServiceSchedule.active.is_(True),
        ServiceSchedule.archived_at.is_(None), ServiceSchedule.kind == "additional",
        ScheduleAssignment.product_id.in_(quantities)))
    if any(not schedule_is_open(db, schedule) for schedule in additional):
        raise CodedHTTPException(409, "Product schedule is closed", "PRODUCT_SCHEDULE_CLOSED")
    recalculate_order(db, draft)
    return draft


def priced_snapshot(draft):
    return {
        "subtotal": str(draft.subtotal), "discount": str(draft.discount), "amount": str(draft.total),
        "promotions": draft.applied_promotions,
        "items": [{"product_id": x.product_id, "name": x.product_name, "variant_name": x.variant_name,
                   "quantity": str(x.quantity), "unit_price": str(x.unit_price), "modifiers": x.modifiers,
                   "notes": x.notes, "line_total": str(x.line_total),
                   "promotion_discount": str(x.promotion_discount), "promotion_snapshot": x.promotion_snapshot}
                  for x in draft.items],
    }


def serialize_request(request):
    return {"id": request.id, "order_id": request.order_id, "purpose": request.purpose,
            "method": request.method, "amount": float(request.amount), "status": request.status,
            "version": request.version, "items": (request.snapshot or {}).get("pricing", {}).get("items", []),
            "created_at": request.created_at, "payment_id": request.payment_id}


def requests_for_order(db, order):
    return list(db.scalars(select(OrderPaymentRequest).where(OrderPaymentRequest.order_id == order.id,
        OrderPaymentRequest.business_id == order.business_id).order_by(OrderPaymentRequest.created_at, OrderPaymentRequest.id)))


def payment_request(db, order, request_id):
    request = db.scalar(select(OrderPaymentRequest).where(OrderPaymentRequest.id == request_id,
        OrderPaymentRequest.order_id == order.id, OrderPaymentRequest.business_id == order.business_id,
        OrderPaymentRequest.branch_id == order.branch_id).with_for_update())
    if not request:
        raise HTTPException(404, "Payment request not found")
    return request


def ensure_request_upload(db, order, request):
    assert_open(order)
    if request.method != "yape" or request.status not in {"pending", "rejected"}:
        raise CodedHTTPException(409, "This payment request cannot receive another receipt", "PAYMENT_REQUEST_LOCKED")
    existing = db.scalar(select(PaymentEvidence.id).where(PaymentEvidence.payment_request_id == request.id,
        PaymentEvidence.status.in_(["evidence_received", "under_review", "paid"])))
    if existing:
        raise CodedHTTPException(409, "Resolve the current receipt first", "PAYMENT_EVIDENCE_UNDER_REVIEW")


def ensure_dispatch(db, order):
    requests = requests_for_order(db, order)
    if any(r.purpose == "addition" and r.status in {"pending", "under_review"} for r in requests):
        raise CodedHTTPException(409, "Resolve the pending addition before completion", "ADDITION_PENDING")
    if order.channel != "delivery":
        return
    if order.delivery_fee_status != "final":
        raise CodedHTTPException(409, "Confirm delivery fee before dispatch", "DELIVERY_FEE_PENDING")
    for request in requests:
        if request.purpose == "delivery" and (request.method == "unselected" or
                (request.method == "yape" and request.status != "paid")):
            raise CodedHTTPException(409, "Confirm how delivery will be paid and review its receipt", "DELIVERY_PAYMENT_PENDING")


def review_request(db, user, order, evidence, payload):
    request = payment_request(db, order, evidence.payment_request_id)
    if evidence.status in {"paid", "rejected", "superseded"}:
        return None
    assert_open(order)
    if not payload.approve:
        evidence.status = "rejected"
        evidence.rejection_reason = payload.note or "Rejected by cashier"
        request.status = "rejected"
    else:
        if not evidence.image_sha256 or not evidence.storage_path or evidence.status == "not_a_receipt":
            raise HTTPException(409, "A persisted payment receipt is required")
        if order.payment_method == "yape" and not db.scalar(select(PaymentEvidence.id).where(
                PaymentEvidence.order_id == order.id, PaymentEvidence.payment_request_id.is_(None),
                PaymentEvidence.status == "paid")):
            raise CodedHTTPException(409, "Approve the initial receipt first", "INITIAL_PAYMENT_PENDING")
        if request.status not in {"pending", "under_review"}:
            raise CodedHTTPException(409, "Request is not pending", "PAYMENT_REQUEST_LOCKED")
        tickets = []
        if request.purpose == "addition":
            from .models import Branch
            lines = [OrderLineInput.model_validate(x) for x in request.snapshot["lines"]]
            draft = preview_items(db, db.get(Branch, order.branch_id), order.channel, lines, "yape")
            if fingerprint(priced_snapshot(draft)) != fingerprint(request.snapshot["pricing"]):
                raise CodedHTTPException(409, "Prices or promotions changed; a staff member must reconcile this receipt", "ADDITION_REQUIRES_REVIEW")
            # Freeze the original paid lines. Only the new batch receives current promotions.
            new_items = list(draft.items)
            for item in new_items:
                draft.items.remove(item)
                order.items.append(item)
            db.flush()
            commit_order_items_stock(db, user, order, new_items)
            order.subtotal = money(order.subtotal + draft.subtotal)
            order.discount = money(order.discount + draft.discount)
            order.promotion_discount = money(order.promotion_discount + draft.promotion_discount)
            order.applied_promotions = [*(order.applied_promotions or []), *(draft.applied_promotions or [])]
            order.total = money(order.total + request.amount)
            tickets = create_kitchen_tickets(db, order, new_items, kind="addition", context={"created_by": user.user_id})
            order.status = "sent_to_kitchen"
            for ticket in tickets:
                prepare_pos_print_intent(db, order, ticket)
        payment = add_payment(db, user, order, PaymentCreate(method="yape", amount=request.amount,
            register_id=payload.register_id, external_reference=evidence.operation_number,
            note=payload.note or f"Approved {request.purpose} receipt"))
        request.payment_id = payment.id
        request.status = "paid"
        evidence.status = "paid"
        event = create_integration_event(db, order, "payment.approved", {"evidence_id": evidence.id,
            "payment_id": payment.id, "payment_request_id": request.id, "purpose": request.purpose,
            "sent_to_kitchen": True}) if tickets else None
    evidence.reviewed_by = user.user_id
    evidence.reviewed_at = utcnow()
    request.version += 1
    order.version += 1
    audit(db, user, f"payment_request.{request.status}", "order_payment_request", request.id, order.business_id,
          {"order_id": order.id, "evidence_id": evidence.id, "amount": str(request.amount)}, branch_id=order.branch_id)
    return event if payload.approve else ready_event(db, order)


def ready_event(db, order):
    """One ready notice per prepared content, not per retry or reopen."""
    db.flush()
    tickets = list(db.scalars(select(KitchenTicket).where(KitchenTicket.order_id == order.id,
        KitchenTicket.status != "cancelled").order_by(KitchenTicket.id)))
    if not tickets or any(t.status not in {"ready", "served"} for t in tickets):
        return None
    if any(r.purpose == "addition" and r.status in {"pending", "under_review"} for r in requests_for_order(db, order)):
        return None
    from .command_revisions import effective_ticket_items
    digest = fingerprint([{ "id": t.id, "items": effective_ticket_items(t)} for t in tickets])
    prior = db.scalars(select(IntegrationEvent).where(IntegrationEvent.branch_id == order.branch_id,
        IntegrationEvent.aggregate_id == str(order.id), IntegrationEvent.event_type == "order.ready"))
    def already_notified(event):
        saved = (event.payload or {}).get("content_fingerprint")
        if saved:
            return saved == digest
        # Legacy ready events predate content fingerprints. Reopening their same
        # batches must not replay them; a newly created addition can notify again.
        return bool(event.created_at and all(t.created_at and
            t.created_at.replace(tzinfo=None) <= event.created_at.replace(tzinfo=None) for t in tickets))
    if any(already_notified(e) for e in prior):
        return None
    return create_integration_event(db, order, "order.ready", {"status": "ready", "content_fingerprint": digest})
