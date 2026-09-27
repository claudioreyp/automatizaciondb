"""Versioned, tenant-scoped customer operations for the WhatsApp agent."""
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .agent_order_changes import (attach_recent_agent_order_changes, kitchen_was_completed,
    record_agent_order_change, update_active_ticket_fulfillment)
from .agent_checkout import (assert_open, assert_sender, fingerprint, payment_request, pending_delivery_allowed,
    preview_items, priced_snapshot, requests_for_order, serialize_request)
from .auth import AuthContext, require_roles
from .database import get_db
from .models import Branch, BranchSettings, DeliveryAssignment, KitchenTicket, OrderPaymentRequest, Payment, Product
from .schemas import OrderLineInput, OrderItemRevisionOperation
from .services import (append_order_item_batch, apply_order_item_revisions, assert_version, audit,
    get_idempotent_response, load_order, money, save_idempotent_response, serialize_order,
    sync_order_payment_status, product_capacity, active_order_items, service_channel_for_order,
    apply_unsent_agent_item_revisions)
from .auth import IntegrationAuthContext, require_integration_scope
from .realtime import hub
from .errors import CodedHTTPException
from .pos_printing import refresh_unclaimed_fulfillment_print_jobs
from .settings_service import sync_pending_kitchen_print_jobs

router = APIRouter(prefix="/api/v1", tags=["agent-checkout"])


class Preview(BaseModel):
    channel: Literal["counter", "takeaway", "delivery"]
    purpose: Literal["initial", "addition"] = "initial"
    payment_method: Literal["cash", "yape"] | None = None
    items: list[OrderLineInput] = Field(min_length=1, max_length=100)


class CustomerOperation(BaseModel):
    sender: str = Field(min_length=5, max_length=120)
    expected_version: int = Field(ge=1)


class Addition(CustomerOperation):
    items: list[OrderLineInput] = Field(min_length=1, max_length=100)
    expected_amount: Decimal = Field(gt=0)


class Revision(CustomerOperation):
    operations: list[OrderItemRevisionOperation] = Field(min_length=1, max_length=100)


class Fulfillment(CustomerOperation):
    channel: Literal["takeaway", "delivery"]
    delivery_address: dict | None = None


class DeliveryFee(BaseModel):
    expected_version: int = Field(ge=1)
    amount: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    method: Literal["cash", "yape"] | None = None


class DeliveryMethod(CustomerOperation):
    method: Literal["cash", "yape"]


class StaffDeliveryMethod(BaseModel):
    expected_version: int = Field(ge=1)
    method: Literal["cash", "yape"]


def scoped(db, integration, order_id, sender):
    from .api import ensure_integration_order_scope
    order = load_order(db, order_id, for_update=True)
    ensure_integration_order_scope(integration, order)
    assert_sender(order, sender)
    return order


def paid_order_amount(db, order):
    return money(db.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(
        Payment.order_id == order.id, Payment.status == "confirmed")))


def fulfillment_change_block_reason(db, order, target_channel=None):
    if order.status in {"dispatched", "delivered", "cancelled", "closed"} or order.table_released_at:
        return "order_closed"
    if kitchen_was_completed(db, order):
        return "kitchen_completed"
    if order.channel == "delivery":
        assigned = db.scalar(select(DeliveryAssignment.id).where(
            DeliveryAssignment.order_id == order.id, DeliveryAssignment.status != "cancelled").limit(1))
        request = db.scalar(select(OrderPaymentRequest).where(
            OrderPaymentRequest.order_id == order.id, OrderPaymentRequest.purpose == "delivery").limit(1))
        if assigned:
            return "delivery_locked"
        has_fee_commitment = bool(request or money(order.delivery_fee) > 0)
        if target_channel is not None and target_channel != "delivery" and has_fee_commitment:
            return "delivery_locked"
        if target_channel in {None, "delivery"} and has_fee_commitment:
            settings = db.scalar(select(BranchSettings).where(BranchSettings.branch_id == order.branch_id))
            if not settings or settings.delivery_mode not in {"fixed", "free"}:
                return "delivery_locked"
    pending_addition = db.scalar(select(OrderPaymentRequest.id).where(
        OrderPaymentRequest.order_id == order.id, OrderPaymentRequest.purpose == "addition",
        OrderPaymentRequest.status.in_(["pending", "under_review"])).limit(1))
    if pending_addition:
        return "delivery_locked"
    return None


def replay(db, order, scope, key, payload):
    if not key or len(key) > 240:
        raise HTTPException(422, "Idempotency-Key is required (max 240 characters)")
    digest = fingerprint(payload.model_dump(mode="json"))
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
    return {"business_id": branch.business_id, "branch_id": branch.id, "categories": categories,
            "products": products, "promotions": raw["promotions"]}


@router.post("/integrations/orders/preview")
def preview(payload: Preview, response: Response, integration: IntegrationAuthContext = Depends(require_integration_scope("orders:write")), db: Session = Depends(get_db)):
    from .api import integration_branch_from_auth
    from .agent_context import agent_context
    _, branch = integration_branch_from_auth(db, integration, None)
    draft = preview_items(db, branch, payload.channel, payload.items, payload.payment_method)
    delivery = agent_context(db, branch)["delivery"]
    fee = Decimal(0)
    pending = False
    if payload.channel == "delivery" and payload.purpose == "initial":
        if not delivery["enabled"]:
            raise HTTPException(409, "Delivery is disabled")
        if delivery["mode"] == "quote":
            pending = True
        elif delivery["mode"] in {"fixed", "free"}:
            fee = money(delivery["fee"])
        else:
            raise HTTPException(409, "Destination-based delivery needs a staff quotation")
        minimum = money(delivery["minimum_order_amount"])
        if draft.subtotal < minimum:
            raise HTTPException(409, "Delivery minimum purchase not reached")
        if not pending and delivery["free_delivery_threshold"] is not None and draft.subtotal >= money(delivery["free_delivery_threshold"]):
            fee = Decimal(0)
    response.headers["Cache-Control"] = "no-store"
    return {"business_id": branch.business_id, "branch_id": branch.id, **priced_snapshot(draft),
        "known_total": float(draft.total + fee), "final_total": None if pending else float(draft.total + fee),
        "delivery_fee": None if pending else float(fee), "delivery_fee_status": "pending_quote" if pending else "final"}


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
    prepared_ids = {item.get("item_id") for ticket in tickets if ticket.status in {"ready", "served"}
                    for item in effective_ticket_items(ticket)}
    is_open = order.status not in {"dispatched", "delivered", "cancelled", "closed"} and not order.table_released_at
    requests = requests_for_order(db, order)
    kitchen_completed = kitchen_was_completed(db, order)
    delivery_block = fulfillment_change_block_reason(db, order, "delivery")
    takeaway_block = fulfillment_change_block_reason(db, order, "takeaway")
    return {**serialize_order(order), "paid_amount": float(paid),
        "remaining_amount": float(max(Decimal(0), money(order.total) - paid)),
        "kitchen_completed": kitchen_completed,
        "allowed_actions": {
            "add_items": bool(is_open and order.payment_method in {"cash", "yape"}),
            "change_fulfillment": delivery_block is None or takeaway_block is None,
            "change_to_delivery": delivery_block is None,
            "change_to_takeaway": takeaway_block is None,
            "fulfillment_change_block_reason": delivery_block,
            "editable_item_ids": [item.id for item in order.items if is_open and not kitchen_completed
                                  and item.status not in {"cancelled", "superseded"} and item.id not in prepared_ids],
            "choose_delivery_payment": bool(is_open and any(r.purpose == "delivery" and r.method == "unselected" for r in requests)),
        }, "payment_requests": [serialize_request(r) for r in requests],
        "payment_evidence": [{k: v for k, v in serialize_payment_evidence(e).items() if k in {
            "id", "payment_request_id", "expected_amount", "amount_detected", "operation_number", "security_code",
            "recipient", "status", "rejection_reason", "created_at", "reviewed_at"}} for e in db.scalars(select(PaymentEvidence).where(
            PaymentEvidence.order_id == order.id).order_by(PaymentEvidence.created_at, PaymentEvidence.id))],
        "tickets": [{"id": t.id, "status": t.status, "items": effective_ticket_items(t)} for t in tickets]}


@router.patch("/integrations/orders/{order_id}/fulfillment")
async def change_fulfillment(order_id: int, payload: Fulfillment,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("orders:write")),
    db: Session = Depends(get_db)):
    from .agent_context import agent_context
    from .integration_order_validation import validate_delivery_destination

    order = scoped(db, integration, order_id, payload.sender)
    scope = f"agent-fulfillment:{order.id}"
    previous, digest = replay(db, order, scope, idempotency_key, payload)
    if previous is not None:
        return previous
    reason = fulfillment_change_block_reason(db, order, payload.channel)
    if reason == "kitchen_completed":
        raise CodedHTTPException(409, "The kitchen command was completed; only additions are allowed", "KITCHEN_ALREADY_COMPLETED")
    if reason == "order_closed":
        raise CodedHTTPException(409, "This order is already completed", "ORDER_ITEMS_LOCKED")
    if reason:
        raise CodedHTTPException(409, "Delivery or an addition already has a payment or assignment in progress", "ORDER_FULFILLMENT_LOCKED")

    branch = db.get(Branch, order.branch_id)
    settings = db.scalar(select(BranchSettings).where(
        BranchSettings.business_id == order.business_id, BranchSettings.branch_id == order.branch_id))
    if not branch or not branch.active:
        raise CodedHTTPException(409, "Service is disabled", "SERVICE_DISABLED")
    service_key = service_channel_for_order(payload.channel, order.source)
    if payload.channel == "delivery":
        if payload.delivery_address is None:
            raise CodedHTTPException(422, "Delivery destination is required", "DELIVERY_DESTINATION_REQUIRED")
        permitted = {"address", "full_address", "reference", "maps_url", "map_url", "latitude", "longitude", "delivery_service"}
        if set(payload.delivery_address) - permitted or payload.delivery_address.get("delivery_service", "own") != "own":
            raise CodedHTTPException(422, "Only own delivery details are accepted", "DELIVERY_DETAILS_INVALID")
        destination = {key: value for key, value in payload.delivery_address.items() if key != "delivery_service"}
        destination["delivery_service"] = "own"
        validate_delivery_destination(destination)
        delivery = agent_context(db, branch)["delivery"]
        if not delivery["enabled"]:
            raise CodedHTTPException(409, "Delivery is disabled", "SERVICE_DISABLED")
        minimum = money(delivery["minimum_order_amount"])
        if order.subtotal < minimum:
            raise CodedHTTPException(409, "Delivery minimum purchase not reached", "DELIVERY_MINIMUM_NOT_REACHED")
    else:
        if payload.delivery_address is not None:
            raise CodedHTTPException(422, "Only delivery orders can carry a destination", "DELIVERY_DETAILS_INVALID")
        destination = None
        enabled = getattr(settings, service_key, True) if settings else (
            branch.takeaway_enabled if payload.channel == "takeaway" else True)
        if not enabled:
            raise CodedHTTPException(409, "Service is disabled", "SERVICE_DISABLED")
        delivery = None
    for item in active_order_items(order):
        if item.product_id is None:
            continue
        product = db.get(Product, item.product_id)
        if not product or product.business_id != order.business_id or product.branch_id != order.branch_id or service_key not in (product.service_channels or []):
            raise CodedHTTPException(409, "An ordered product is unavailable for this service", "PRODUCT_UNAVAILABLE_FOR_CHANNEL")

    existing_delivery_request = db.scalar(select(OrderPaymentRequest).where(
        OrderPaymentRequest.order_id == order.id, OrderPaymentRequest.purpose == "delivery").limit(1))
    if payload.channel == order.channel and destination == order.delivery_address:
        attach_recent_agent_order_changes(db, [order])
        result = {"order": serialize_order(order), "payment_request": serialize_request(existing_delivery_request) if existing_delivery_request else None,
                  "recent_modification": getattr(order, "_recent_modification", None), "changed": False}
        return await finish(db, order, scope, idempotency_key, digest, result)

    before = {"channel": order.channel, "delivery_fee_status": order.delivery_fee_status, "notes": order.notes}
    order.channel = payload.channel
    order.delivery_address = destination
    order.delivery_quote_id = None
    if before["channel"] != order.channel and order.notes:
        old_marker = {"takeaway": "RECOJO EN LOCAL", "delivery": "DELIVERY PROPIO"}.get(before["channel"])
        if old_marker:
            parts = [part for part in order.notes.split(" | ") if part.strip().upper() != old_marker]
            order.notes = " | ".join(parts) or None
    payment_request = None
    if payload.channel == "delivery":
        mode = delivery["mode"]
        if before["channel"] == "delivery" and mode in {"fixed", "free"}:
            # A destination correction under fixed pricing keeps the original fee
            # and its independent payment request. A quoted fee is guarded above.
            payment_request = existing_delivery_request
        elif before["channel"] == "delivery":
            order.delivery_fee = Decimal(0)
            order.delivery_fee_status = "pending_quote"
        elif mode in {"fixed", "free"}:
            fee = money(delivery["fee"])
            threshold = delivery["free_delivery_threshold"]
            if threshold is not None and order.subtotal >= money(threshold):
                fee = Decimal(0)
            order.delivery_fee = fee
            order.delivery_fee_status = "final"
            if fee > 0:
                payment_request = OrderPaymentRequest(
                    business_id=order.business_id, branch_id=order.branch_id,
                    order_id=order.id, purpose="delivery", method="unselected", amount=fee,
                    snapshot={"reason": "agent_fulfillment_change"},
                )
                db.add(payment_request)
        else:
            order.delivery_fee = Decimal(0)
            order.delivery_fee_status = "pending_quote"
    else:
        order.delivery_fee = Decimal(0)
        order.delivery_fee_status = "final"
    order.total = max(money(order.subtotal - order.discount + order.delivery_fee), Decimal(0))
    sync_order_payment_status(db, order)
    order.version += 1
    user = AuthContext("integration", "owner", order.business_id, order.branch_id)
    updated_tickets = update_active_ticket_fulfillment(db, user, order)
    sync_pending_kitchen_print_jobs(db, order, updated_tickets, preserve_local=True)
    db.flush()
    refresh_unclaimed_fulfillment_print_jobs(db, order, updated_tickets)
    marker = record_agent_order_change(db, user, order, "agent.fulfillment_updated",
        {"before": before, "after": {"channel": order.channel, "delivery_fee_status": order.delivery_fee_status, "notes": order.notes},
         "updated_ticket_ids": [ticket.id for ticket in updated_tickets]})
    db.flush()
    result = {"order": serialize_order(order), "payment_request": serialize_request(payment_request) if payment_request else None,
              "recent_modification": marker, "changed": True}
    finished = await finish(db, order, scope, idempotency_key, digest, result)
    if updated_tickets:
        from .api import serialize_ticket
        for ticket in updated_tickets:
            await hub.broadcast(order.branch_id, "kitchen.ticket_updated",
                {"command": serialize_ticket(ticket, order=order), "order": result["order"]})
    return finished


@router.post("/integrations/orders/{order_id}/item-batches")
async def add_items(order_id: int, payload: Addition, idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("orders:write")), db: Session = Depends(get_db)):
    order = scoped(db, integration, order_id, payload.sender)
    scope = f"agent-addition:{order.id}"
    previous, digest = replay(db, order, scope, idempotency_key, payload)
    if previous is not None:
        return previous
    assert_open(order)
    draft = preview_items(db, db.get(Branch, order.branch_id), order.channel, payload.items, order.payment_method)
    if money(payload.expected_amount) != draft.total:
        raise HTTPException(409, "Review the updated addition price")
    user = AuthContext("integration", "owner", order.business_id, order.branch_id)
    if order.payment_method == "yape":
        request = OrderPaymentRequest(business_id=order.business_id, branch_id=order.branch_id,
            order_id=order.id, purpose="addition", method="yape", amount=draft.total,
            snapshot={"lines": [x.model_dump(mode="json") for x in payload.items], "pricing": priced_snapshot(draft)})
        db.add(request)
        db.flush()
        order.version += 1
        result = {"order": serialize_order(order), "payment_request": serialize_request(request), "sent_to_kitchen": False}
        audit(db, user, "agent.addition_requested", "order", order.id, order.business_id, {"request_id": request.id})
    else:
        if order.payment_method != "cash":
            raise HTTPException(409, "A staff member must handle this payment method")
        append_order_item_batch(db, user, order, payload.items, allow_paid_addition=True)
        record_agent_order_change(db, user, order, "agent.items_added")
        db.flush()
        result = {"order": serialize_order(order), "sent_to_kitchen": order.status == "sent_to_kitchen"}
    return await finish(db, order, scope, idempotency_key, digest, result)


@router.post("/integrations/orders/{order_id}/item-revisions")
async def revise_items(order_id: int, payload: Revision, idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("orders:write")), db: Session = Depends(get_db)):
    order = scoped(db, integration, order_id, payload.sender)
    scope = f"agent-revision:{order.id}"
    previous, digest = replay(db, order, scope, idempotency_key, payload)
    if previous is not None:
        return previous
    assert_open(order)
    if kitchen_was_completed(db, order):
        raise CodedHTTPException(409, "The kitchen command was completed; only additions are allowed", "KITCHEN_ALREADY_COMPLETED")
    from .command_revisions import effective_ticket_items
    protected = set()
    for t in db.scalars(select(KitchenTicket).where(KitchenTicket.order_id == order.id, KitchenTicket.status.in_(["ready", "served"]))):
        protected.update(x.get("item_id") for x in effective_ticket_items(t))
    if any(op.item_id in protected for op in payload.operations):
        raise HTTPException(409, "Prepared products require a staff member")
    replacements = [op.replacement for op in payload.operations if op.replacement is not None]
    if replacements:
        preview_items(db, db.get(Branch, order.branch_id), order.channel, replacements, order.payment_method)
    user = AuthContext("integration", "owner", order.business_id, order.branch_id)
    if order.status == "confirmed":
        apply_unsent_agent_item_revisions(db, user, order, payload.operations)
    else:
        apply_order_item_revisions(db, user, order, payload.operations,
                                   allow_paid_price_neutral=paid_order_amount(db, order) > 0)
    record_agent_order_change(db, user, order, "agent.items_revised")
    db.flush()
    return await finish(db, order, scope, idempotency_key, digest, {"order": serialize_order(order)})


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
    order.delivery_fee = money(payload.amount)
    order.delivery_fee_status = "final"
    order.total = money(order.subtotal - order.discount + order.delivery_fee)
    request = OrderPaymentRequest(business_id=order.business_id, branch_id=order.branch_id, order_id=order.id,
        purpose="delivery", method=payload.method or "unselected", amount=order.delivery_fee,
        status="pending" if order.delivery_fee else "waived", snapshot={})
    if not order.delivery_fee:
        request.method = "cash"
    db.add(request)
    order.version += 1
    sync_order_payment_status(db, order)
    db.flush()
    audit(db, user, "order.delivery_fee_confirmed", "order", order.id, order.business_id,
          {"amount": str(order.delivery_fee), "method": request.method}, branch_id=order.branch_id)
    return await finish(db, order, scope, idempotency_key, digest, {"order": serialize_order(order), "payment_request": serialize_request(request)})


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
    candidates = [r for r in requests_for_order(db, order) if r.purpose == "delivery"]
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
