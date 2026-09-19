"""Protected metadata/fulfillment edits without rewriting kitchen history."""

from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .errors import CodedHTTPException
from .models import Branch, BranchSettings, DeliveryAssignment, Order, Payment, PaymentEvidence, Product
from .services import active_order_items, money, recalculate_order, service_channel_for_order


DELIVERY_SERVICES = {"own", "uber_eats", "rappi", "didi_food"}
TERMINAL_STATUSES = {"closed", "cancelled", "delivered"}


def delivery_service(order: Order) -> str:
    return (order.delivery_address or {}).get("delivery_service") or "own"


def order_edit_policy(db: Session, order: Order) -> dict:
    paid = db.scalar(select(Payment.id).where(
        Payment.order_id == order.id,
        Payment.status.not_in(["rejected", "cancelled"]), Payment.amount > 0,
    ).limit(1))
    evidence = db.scalar(select(PaymentEvidence.id).where(
        PaymentEvidence.order_id == order.id,
        PaymentEvidence.status.in_(["evidence_received", "under_review"]),
    ).limit(1))
    assignment = db.scalar(select(DeliveryAssignment.id).where(
        DeliveryAssignment.order_id == order.id,
        DeliveryAssignment.status != "cancelled",
    ).limit(1))
    locked = bool(paid or evidence or assignment or order.status == "dispatched"
                  or order.payment_status in {"paid", "partial"})
    return {
        "can_edit": order.status not in TERMINAL_STATUSES and order.table_id is None,
        "fulfillment_locked": locked,
        "reason": "La modalidad, dirección y tarifa no se pueden cambiar con pagos, comprobantes o reparto en curso." if locked else None,
    }


def edit_order_details(db: Session, order: Order, changes: dict, expected_total: Decimal) -> dict:
    allowed = {"channel", "customer_name", "customer_phone", "delivery_address", "delivery_fee", "notes"}
    if set(changes) - allowed:
        raise CodedHTTPException(422, "Los productos se editan desde las comandas del pedido.", "ORDER_EDIT_FIELDS")
    policy = order_edit_policy(db, order)
    if not policy["can_edit"]:
        raise CodedHTTPException(409, "Este pedido ya no se puede editar desde este formulario.", "ORDER_NOT_EDITABLE")
    previous = {key: getattr(order, key) for key in changes}
    changed = {key: value for key, value in changes.items() if value != previous[key]}
    fulfillment_changed = bool(set(changed) & {"channel", "delivery_address", "delivery_fee"})
    if fulfillment_changed and policy["fulfillment_locked"]:
        raise CodedHTTPException(409, policy["reason"], "ORDER_FULFILLMENT_LOCKED")

    channel = changes.get("channel", order.channel)
    if channel is None:
        raise CodedHTTPException(422, "Selecciona el tipo de pedido.", "ORDER_CHANNEL_REQUIRED")
    channel_changed = channel != order.channel
    address = changes.get("delivery_address", order.delivery_address) or {}
    service = address.get("delivery_service") or "own"
    if service not in DELIVERY_SERVICES:
        raise CodedHTTPException(422, "Selecciona un servicio de entrega válido.", "DELIVERY_SERVICE_INVALID")
    if fulfillment_changed:
        if channel == "delivery" and service == "own":
            name = changes.get("customer_name", order.customer_name)
            phone = changes.get("customer_phone", order.customer_phone)
            if not name or not phone or not str(address.get("address", "")).strip() or not str(address.get("reference", "")).strip():
                raise CodedHTTPException(422, "Completa nombre, teléfono, dirección y referencia para el domicilio propio.", "DELIVERY_DETAILS_REQUIRED")
        if channel != "delivery" or service != "own":
            previous.setdefault("delivery_fee", order.delivery_fee)
            changes["delivery_fee"] = Decimal("0")
        if "delivery_fee" in changes and changes["delivery_fee"] is None:
            raise CodedHTTPException(422, "Indica un costo de envío válido.", "DELIVERY_FEE_REQUIRED")

    if channel_changed:
        branch = db.get(Branch, order.branch_id)
        settings = db.scalar(select(BranchSettings).where(BranchSettings.branch_id == order.branch_id))
        service_key = service_channel_for_order(channel, order.source)
        enabled = getattr(settings, service_key, True) if settings else (
            branch.delivery_enabled if channel == "delivery" else branch.takeaway_enabled if channel == "takeaway" else True
        )
        if not branch.active or not enabled:
            raise CodedHTTPException(409, "Esta modalidad está deshabilitada en la sucursal.", "SERVICE_DISABLED")
        for item in active_order_items(order):
            if item.product_id is None:
                continue
            product = db.get(Product, item.product_id)
            if not product or product.branch_id != order.branch_id or not product.available or service_key not in (product.service_channels or []):
                raise CodedHTTPException(409, f"{item.product_name} no está disponible para esta modalidad. Revisa sus productos desde la comanda.", "PRODUCT_UNAVAILABLE_FOR_CHANNEL")

    previous.update(total=order.total, discount=order.discount)
    for key, value in changes.items():
        setattr(order, key, value)
    if channel_changed:
        history = [(item, item.promotion_discount, item.promotion_snapshot) for item in order.items if item.status in {"cancelled", "superseded"}]
        recalculate_order(db, order)
        for item, discount, snapshot in history:
            item.promotion_discount, item.promotion_snapshot = discount, snapshot
    else:
        # Customer-only edits must not re-evaluate expired or modified promotions.
        order.total = max(money(order.subtotal - order.discount + order.delivery_fee), Decimal("0"))
    if fulfillment_changed:
        order.delivery_quote_id = None
        order.delivery_fee_status = "final"
    if money(expected_total) != money(order.total):
        raise HTTPException(status_code=409, detail={
            "code": "ORDER_TOTAL_CHANGED",
            "message": "El total cambió. Revisa el importe y confirma nuevamente para guardar.",
            "total": float(order.total), "version": order.version,
        })
    return {"before": previous, "after": {**changes, "total": order.total, "discount": order.discount}}
