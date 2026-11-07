"""Staff reconciliation of delivery money, separate from physical delivery."""
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import func, select

from .errors import CodedHTTPException
from .models import OrderPaymentRequest, Payment, PaymentEvidence, utcnow
from .services import audit, money

REVIEWABLE = {"evidence_received", "under_review"}
INACTIVE = {"superseded", "waived", "cancelled", "declined"}


def active_delivery_requests(db, order):
    return list(db.scalars(select(OrderPaymentRequest).where(
        OrderPaymentRequest.order_id == order.id,
        OrderPaymentRequest.business_id == order.business_id,
        OrderPaymentRequest.branch_id == order.branch_id,
        OrderPaymentRequest.purpose == "delivery",
        OrderPaymentRequest.status.notin_(INACTIVE)).with_for_update()))


def ensure_transition_evidence(db, order, next_status):
    """Only dispatch may proceed while a delivery-only receipt is being checked."""
    pending = list(db.scalars(select(PaymentEvidence).where(
        PaymentEvidence.order_id == order.id,
        PaymentEvidence.business_id == order.business_id,
        PaymentEvidence.status.in_(REVIEWABLE))))
    delivery_ids = {r.id for r in active_delivery_requests(db, order)} if next_status == "dispatched" else set()
    if any(e.payment_request_id not in delivery_ids for e in pending):
        raise CodedHTTPException(409, "Resolve the payment evidence review before changing the order status", "PAYMENT_EVIDENCE_UNDER_REVIEW")


def confirm_courier_collection(db, user, order, request, *, reason, expected_version):
    if not {user.role, *(user.roles or ())} & {"superadmin", "owner", "manager", "cashier", "dispatcher"}:
        raise HTTPException(403, "A manager, cashier or dispatcher must confirm courier collection")
    if expected_version is None or not (reason or "").strip():
        raise CodedHTTPException(422, "Confirm courier collection with the current version and a note", "DELIVERY_COLLECTION_CONFIRMATION_REQUIRED")
    request.snapshot = {**(request.snapshot or {}), "courier_collection_pending": {
        "confirmed_by": user.user_id, "confirmed_at": utcnow().isoformat(), "note": reason.strip()}}
    request.version += 1
    audit(db, user, "delivery.courier_collection_pending", "order_payment_request", request.id,
        order.business_id, {"order_id": order.id, "amount": str(request.amount), "note": reason.strip()},
        branch_id=order.branch_id)


def settle_delivery_payment(db, user, order, payload):
    """Caller locks Order; this method never infers a request from a money total."""
    from .agent_checkout import AGENT_SOURCES, initial_payment_pending, payment_request
    from .services import add_payment
    request = payment_request(db, order, payload.payment_request_id)
    active = active_delivery_requests(db, order)
    if (order.channel != "delivery" or request.purpose != "delivery" or len(active) != 1
            or active[0].id != request.id or request.status not in {"pending", "rejected", "under_review"}
            or request.payment_id is not None):
        raise CodedHTTPException(409, "This delivery charge is not outstanding", "PAYMENT_REQUEST_LOCKED")
    if payload.expected_version is None:
        raise HTTPException(422, "expected_version is required")
    if order.delivery_fee_status != "final":
        raise CodedHTTPException(409, "Confirm delivery fee before collecting it", "DELIVERY_FEE_PENDING")
    if order.source in AGENT_SOURCES and initial_payment_pending(db, order):
        raise CodedHTTPException(409, "Approve the initial receipt first", "INITIAL_PAYMENT_PENDING")
    if payload.method not in {"cash", "yape"} or money(payload.amount) != money(request.amount):
        raise CodedHTTPException(422, "Record the exact delivery balance using cash or Yape", "DELIVERY_PAYMENT_AMOUNT_MISMATCH")
    paid = money(db.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(
        Payment.order_id == order.id, Payment.business_id == order.business_id,
        Payment.status == "confirmed")))
    if money(order.total) - paid != money(request.amount):
        raise CodedHTTPException(409, "Review the other outstanding charges before settling delivery", "DELIVERY_PAYMENT_ALLOCATION_AMBIGUOUS")
    payment = add_payment(db, user, order, payload)
    request.payment_id, request.status = payment.id, "paid"
    request.snapshot = {**(request.snapshot or {}), "courier_collection_pending": None,
        "settlement": {"actor_id": user.user_id, "method": payload.method, "amount": str(payment.amount)}}
    request.version += 1
    audit(db, user, "delivery.payment_settled", "order_payment_request", request.id,
        order.business_id, {"order_id": order.id, "payment_id": payment.id, "amount": str(payment.amount)},
        branch_id=order.branch_id)
    return payment


def review_receipt_allocation(db, user, order, evidence, payload):
    """Reassign and approve one stored receipt atomically, with human amounts."""
    from .agent_checkout import AGENT_SOURCES, initial_payment_pending, payment_request
    from .agent_original_payment import original_payment_amount
    from .schemas import PaymentCreate, PaymentAllocationInput
    from .services import add_payment, confirm_order, create_integration_event, send_order_to_kitchen
    if (not payload.approve or order.source not in AGENT_SOURCES or order.payment_method != "yape"
            or order.status in {"cancelled", "closed", "delivered", "dispatched"}
            or not initial_payment_pending(db, order) or evidence.status not in REVIEWABLE
            or not evidence.payment_request_id or not evidence.storage_path or not evidence.image_sha256):
        raise CodedHTTPException(409, "This receipt cannot be reassigned", "PAYMENT_ALLOCATION_LOCKED")
    if payload.expected_version is None or payload.confirmed_amount is None or not (payload.note or "").strip():
        raise CodedHTTPException(422, "Confirm the received amount, current version and review note", "FINANCIAL_REVIEW_REQUIRED")
    request = payment_request(db, order, evidence.payment_request_id)
    requests = active_delivery_requests(db, order)
    all_requests = list(db.scalars(select(OrderPaymentRequest).where(
        OrderPaymentRequest.order_id == order.id, OrderPaymentRequest.business_id == order.business_id)))
    other_receipts = list(db.scalars(select(PaymentEvidence).where(
        PaymentEvidence.order_id == order.id, PaymentEvidence.business_id == order.business_id,
        PaymentEvidence.id != evidence.id,
        PaymentEvidence.status.in_([*REVIEWABLE, "paid"]))))
    paid = db.scalar(select(Payment.id).where(Payment.order_id == order.id,
        Payment.business_id == order.business_id, Payment.status == "confirmed").limit(1))
    if (request.purpose != "delivery" or request.method != "yape" or request.payment_id is not None
            or request.status not in {"pending", "under_review"} or len(requests) != 1
            or requests[0].id != request.id or order.delivery_fee_status != "final"
            or money(request.amount) <= 0 or money(request.amount) != money(order.delivery_fee)
            or paid is not None or other_receipts
            or any(r.purpose == "addition" and r.status not in INACTIVE for r in all_requests)):
        raise CodedHTTPException(409, "The payment allocation is ambiguous; review the order first", "PAYMENT_ALLOCATION_AMBIGUOUS")
    original = original_payment_amount(db, order)
    delivery = money(request.amount) if payload.allocation == "original_and_delivery" else Decimal(0)
    amount = money(payload.confirmed_amount)
    required = money(original + delivery)
    if (payload.allocation == "original" and amount != original) or amount < required:
        raise CodedHTTPException(422, "The verified amount does not cover the selected payment allocation", "PAYMENT_AMOUNT_INSUFFICIENT")
    if amount > required and not (order.whatsapp_chat_id or order.customer_phone):
        raise CodedHTTPException(409, "Identify the order's contact before recording an excess for review", "PAYMENT_ALLOCATION_CONTACT_REQUIRED")
    allocations = [PaymentAllocationInput(label="Productos del pedido original", amount=original)]
    if delivery:
        allocations.append(PaymentAllocationInput(label="Costo de envío", amount=delivery))
    if amount > required:
        allocations.append(PaymentAllocationInput(label="Exceso pendiente de revisión", amount=amount - required))
    previous_request_id = evidence.payment_request_id
    previous_classification = {"payment_request_id": previous_request_id,
        "purpose": request.purpose, "method": request.method, "request_amount": str(request.amount),
        "evidence_status": evidence.status, "expected_amount": (evidence.analysis or {}).get("expected_amount")}
    # Free the request evidence slot without cloning the image or operation.
    evidence.payment_request_id = None
    evidence.analysis = {**(evidence.analysis or {}), "expected_amount": float(required),
        "requires_financial_review": False, "allocation_review": {
            "previous_payment_request_id": previous_request_id, "target": payload.allocation,
            "previous_classification": previous_classification,
            "original_amount": str(original), "delivery_amount": str(delivery),
            "confirmed_amount": str(amount), "actor_id": user.user_id, "note": payload.note.strip()}}
    if order.status in {"draft", "pending_confirmation"}:
        confirm_order(db, user, order)
    payment = add_payment(db, user, order, PaymentCreate(method="yape", amount=amount,
        register_id=payload.register_id, note=payload.note.strip(), external_reference=evidence.operation_number,
        allocations=allocations), allow_reviewed_overpayment=amount > required)
    evidence.status, evidence.reviewed_by, evidence.reviewed_at = "paid", user.user_id, utcnow()
    evidence.analysis = {**evidence.analysis, "allocation_review": {
        **evidence.analysis["allocation_review"], "payment_id": payment.id}}
    request.status = "paid" if delivery else "pending"
    request.payment_id = payment.id if delivery else None
    request.snapshot = {**(request.snapshot or {}), "courier_collection_pending": None,
        "allocation_review": {"evidence_id": evidence.id, "payment_id": payment.id if delivery else None,
            "allocated_amount": str(delivery), "actor_id": user.user_id}}
    request.version += 1
    from .models import KitchenTicket
    existing_kitchen = db.scalar(select(KitchenTicket.id).where(KitchenTicket.order_id == order.id))
    tickets = [] if existing_kitchen else send_order_to_kitchen(db, user, order)
    event = create_integration_event(db, order, "payment.approved", {
        "evidence_id": evidence.id, "payment_id": payment.id, "purpose": "initial", "payment_method": "yape",
        "sent_to_kitchen": bool(tickets), "payment_approved": True,
        "kitchen_release": "released_now" if tickets else "previously_released",
        "ticket_ids": [ticket.id for ticket in tickets], "status": order.status,
        "delivery_payment_included": bool(delivery)}) if order.whatsapp_chat_id or order.customer_phone else None
    if amount > required:
        from .agent_attention import create_attention
        create_attention(db, business_id=order.business_id, branch_id=order.branch_id,
            sender=order.whatsapp_chat_id or order.customer_phone, kind="financial_review",
            summary="El pago verificado supera el importe del pedido; revisar el exceso sin devolución automática.",
            order_id=order.id, source_key=f"receipt-allocation:{evidence.id}",
            details={"payment_id": payment.id, "overpayment_amount": str(amount - required)})
    audit(db, user, "payment_evidence.allocation_reviewed", "payment_evidence", evidence.id,
        order.business_id, evidence.analysis["allocation_review"], branch_id=order.branch_id)
    return tickets, event
