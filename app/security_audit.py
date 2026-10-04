"""Historical security projections. Never resolve product names from the catalog."""

from __future__ import annotations

from datetime import timezone
from decimal import Decimal, InvalidOperation
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import String, and_, cast, func, or_, select
from sqlalchemy.orm import Session

from .models import AuditEvent, CashMovement, CashRegister, CashSession, KitchenTicket, Order, OrderItem, Payment


AuditCategory = Literal[
    "order_cancellation", "item_cancellation", "amount_reduction", "cash_withdrawal",
    "cash_discrepancy", "refund_method_change",
]
CATEGORY_ACTIONS = {
    "order_cancellation": "order.cancelled",
    "item_cancellation": "order.items_revised",
    "amount_reduction": "order.items_revised",
    "cash_withdrawal": "cash.movement_created",
    "cash_discrepancy": "cash.cut_created",
    "refund_method_change": "cash.refund_created",
}
CRITICAL_ACTIONS = frozenset(CATEGORY_ACTIONS.values())
LIMA = ZoneInfo("America/Lima")
ACTION_SUMMARIES = {
    "order.created": "cre\u00f3 un pedido",
    "order.confirmed": "confirm\u00f3 un pedido",
    "order.sent_to_kitchen": "envi\u00f3 un pedido a cocina",
    "order.preparing": "inici\u00f3 la preparaci\u00f3n de un pedido",
    "order.ready": "marc\u00f3 un pedido como listo",
    "order.dispatched": "despach\u00f3 un pedido",
    "order.delivered": "entreg\u00f3 un pedido",
    "order.closed": "cerr\u00f3 un pedido",
    "order.updated": "actualiz\u00f3 un pedido",
    "order.item_added": "agreg\u00f3 un producto a un pedido",
    "order.item_removed": "retir\u00f3 un producto de un pedido",
    "order.item_batch_added": "agreg\u00f3 una comanda a un pedido",
    "payment.created": "registr\u00f3 un pago",
    "payment.approved": "aprob\u00f3 un pago",
    "payment.rejected": "rechaz\u00f3 un pago",
    "payment_evidence.reviewed": "revis\u00f3 un comprobante de pago",
    "cash.opened": "abri\u00f3 una caja",
    "cash.closed": "cerr\u00f3 una caja",
    "cash.cut_created": "realiz\u00f3 un corte de caja",
    "cash.refund_created": "registr\u00f3 un reembolso",
    "kitchen.ready": "complet\u00f3 una comanda",
    "kitchen.reopened": "reabri\u00f3 una comanda",
    "kitchen.cancelled": "retir\u00f3 una comanda cancelada",
    "table.checkout_started": "inici\u00f3 el cobro de una mesa",
    "table.checkout_reopened": "reabri\u00f3 la cuenta de una mesa",
    "table.paid_and_released": "cobr\u00f3 y liber\u00f3 una mesa",
    "inventory.adjusted": "ajust\u00f3 el inventario",
    "catalog.availability_updated": "actualiz\u00f3 la disponibilidad del men\u00fa",
}
RESOURCE_LABELS = {
    "area": "una zona", "table": "una mesa", "branch": "una sucursal",
    "business": "el negocio", "product": "un producto", "category": "una categor\u00eda",
    "cash_register": "una caja", "promotion": "una promoci\u00f3n",
    "reservation": "una reserva", "inventory": "un art\u00edculo del inventario",
    "settings.business": "la configuraci\u00f3n del negocio",
    "settings.branch": "una sucursal", "settings.branch.profile": "los datos de una sucursal",
    "settings.member": "un miembro del equipo", "settings.device": "un dispositivo",
    "settings.printer": "una impresora", "settings.printing": "la configuraci\u00f3n de impresi\u00f3n",
    "settings.schedule": "un horario", "settings.schedule.assignments": "la asignaci\u00f3n de horarios",
    "settings.services": "las opciones de servicio", "settings.times": "los tiempos de servicio",
    "settings.delivery": "los costos de env\u00edo", "settings.payment_methods": "los m\u00e9todos de pago",
}
PAYMENT_METHOD_LABELS = {
    "cash": "Efectivo", "card": "Tarjeta", "yape": "Yape", "plin": "Plin",
    "transfer": "Transferencia bancaria", "online": "Pago en l\u00ednea",
}


def _known_summary(action: str) -> str:
    if action in ACTION_SUMMARIES:
        return ACTION_SUMMARIES[action]
    resource, _, verb = action.rpartition(".")
    translated = {"created": "cre\u00f3", "updated": "actualiz\u00f3", "archived": "archiv\u00f3", "restored": "restaur\u00f3"}.get(verb)
    if translated and resource in RESOURCE_LABELS:
        return f"{translated} {RESOURCE_LABELS[resource]}"
    return "realiz\u00f3 una acci\u00f3n registrada"


def order_identity_snapshot(order: Order) -> dict:
    return {"order_id": order.id, "number": order.number, "folio": order.folio}


def item_audit_snapshot(item: OrderItem) -> dict:
    return {
        "item_id": item.id,
        "product_name": item.product_name,
        "variant_name": item.variant_name,
        "quantity": str(item.quantity),
        "unit_price": str(item.unit_price),
        "line_total": str(item.line_total),
        "promotion_discount": str(item.promotion_discount or 0),
        "net_total": str(item.line_total - (item.promotion_discount or Decimal(0))),
    }


def cash_audit_snapshot(movement: CashMovement, session: CashSession, register: CashRegister) -> dict:
    return {
        "snapshot_version": 1,
        "movement_id": movement.id,
        "register_id": register.id,
        "register_name": register.name,
        "session_id": session.id,
        "movement_type": movement.movement_type,
        "amount": float(movement.amount),
        "signed_amount": float(movement.amount if movement.movement_type == "income" else -movement.amount),
        "payment_method": movement.payment_method,
        "order_id": movement.order_id,
        "note": movement.note,
    }


def cash_cut_audit_snapshot(session: CashSession, register: CashRegister) -> dict:
    """Freeze closed amounts; later security reads never recalculate a cut."""
    return {
        "snapshot_version": 1, "cut_id": session.id, "register_id": register.id,
        "register_name": register.name, "opening_amount": str(session.opening_amount),
        "retained_fund_amount": str(session.retained_fund_amount),
        "cash_withdrawn_amount": str(session.cash_withdrawn_amount),
        "methods": [
            {"key": key, "counted": str(counted) if counted is not None else None,
             "expected": str(expected), "difference": str(difference) if difference is not None else None}
            for key, counted, expected, difference in (
                ("cash", session.declared_amount, session.expected_amount, session.difference),
                ("card", session.card_declared_amount, session.card_expected_amount, session.card_difference),
                ("transfer", None, session.transfer_expected_amount, None),
            )
        ],
    }


def audit_branch_expression():
    # Compare canonical strings, never cast arbitrary legacy IDs to integers.
    order_branch = select(Order.branch_id).where(
        AuditEvent.entity_type == "order",
        cast(Order.id, String) == AuditEvent.entity_id,
        Order.business_id == AuditEvent.business_id,
    ).correlate(AuditEvent).scalar_subquery()
    session_branch = select(CashSession.branch_id).where(
        AuditEvent.entity_type == "cash_session",
        cast(CashSession.id, String) == AuditEvent.entity_id,
        CashSession.business_id == AuditEvent.business_id,
    ).correlate(AuditEvent).scalar_subquery()
    movement_branch = select(CashSession.branch_id).join(
        CashMovement, CashMovement.cash_session_id == CashSession.id,
    ).where(
        AuditEvent.entity_type == "cash_movement",
        cast(CashMovement.id, String) == AuditEvent.entity_id,
        CashSession.business_id == AuditEvent.business_id,
    ).correlate(AuditEvent).scalar_subquery()
    return func.coalesce(order_branch, session_branch, movement_branch)


def audit_branch_filter(branch_id: int):
    return or_(
        AuditEvent.branch_id == branch_id,
        and_(AuditEvent.branch_id.is_(None), audit_branch_expression() == branch_id),
    )


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _text(value) -> str | None:
    return (value.strip() or None) if isinstance(value, str) else None


def _number(value) -> Decimal | None:
    if value is None or isinstance(value, (bool, dict, list)):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def _numeric(value) -> str | None:
    number = _number(value)
    if number is None:
        return None
    formatted = format(number, "f")
    return formatted.rstrip("0").rstrip(".") if "." in formatted else formatted


def _amount(value) -> str | None:
    number = _number(value)
    return f"S/ {number:.2f}" if number is not None else None


def _id(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(str(value))
        return result if result > 0 and str(result) == str(value) else None
    except (ValueError, TypeError):
        return None


def _field(label: str, value: str | None) -> dict:
    return {"label": label, "value": value}


def _reduced(before: dict, after: dict) -> bool:
    for key in ("net_total", "unit_price", "line_total"):
        old, new = _number(before.get(key)), _number(after.get(key))
        if old is not None and new is not None and new < old:
            return True
    return False


def _ticket_item(raw: dict) -> dict:
    result = {**raw, "product_name": raw.get("name")}
    gross = _number(raw.get("line_total"))
    discount = _number(raw.get("promotion_discount"))
    if gross is not None and discount is not None:
        result["net_total"] = str(gross - discount)
    return result


def _legacy_ticket_revisions(db: Session, order: Order) -> list[dict]:
    contexts = db.scalars(select(KitchenTicket.context_snapshot).where(
        KitchenTicket.order_id == order.id, KitchenTicket.business_id == order.business_id,
        KitchenTicket.branch_id == order.branch_id,
    ))
    revisions = []
    for context in contexts:
        raw_revisions = _dict(context).get("revisions")
        if isinstance(raw_revisions, list):
            revisions.extend(item for item in raw_revisions if isinstance(item, dict))
    return revisions


def _order(db: Session, event: AuditEvent) -> Order | None:
    if event.entity_type != "order" or _id(event.entity_id) is None:
        return None
    filters = [Order.id == _id(event.entity_id), Order.business_id == event.business_id]
    if event.branch_id is not None:
        filters.append(Order.branch_id == event.branch_id)
    return db.scalar(select(Order).where(*filters))


def _cash(db: Session, event: AuditEvent) -> tuple[CashSession | None, CashMovement | None]:
    entity_id = _id(event.entity_id)
    if entity_id is None or event.entity_type not in {"cash_session", "cash_movement"}:
        return None, None
    filters = [CashSession.business_id == event.business_id]
    if event.branch_id is not None:
        filters.append(CashSession.branch_id == event.branch_id)
    if event.entity_type == "cash_session":
        session = db.scalar(select(CashSession).where(CashSession.id == entity_id, *filters))
        # Old session events did not record which of their movements was created.
        return session, None
    row = db.execute(select(CashSession, CashMovement).join(
        CashMovement, CashMovement.cash_session_id == CashSession.id,
    ).where(CashMovement.id == entity_id, *filters)).first()
    return (row[0], row[1]) if row else (None, None)


def _closed_cut(payload: dict, session: CashSession | None) -> dict:
    if payload.get("snapshot_version"):
        return payload
    if session is None or session.status != "closed":
        return {}
    # Only persisted closing columns prove past amounts. Do not sum current
    # payments or use total_difference, which may hide opposite differences.
    return {**payload, "cut_id": session.id, "opening_amount": str(session.opening_amount),
        "retained_fund_amount": str(session.retained_fund_amount),
        "cash_withdrawn_amount": str(session.cash_withdrawn_amount),
        "methods": [
            {"key": key, "counted": str(counted) if counted is not None else None,
             "expected": str(expected), "difference": str(difference) if difference is not None else None}
            for key, counted, expected, difference in (
                ("cash", session.declared_amount, session.expected_amount, session.difference),
                ("card", session.card_declared_amount, session.card_expected_amount, session.card_difference),
                ("transfer", None, session.transfer_expected_amount, None),
            )
        ],
    }


def _refund_original_methods(db: Session, payload: dict, movement: CashMovement | None,
                             session: CashSession | None) -> list[str]:
    saved = payload.get("original_payment_methods")
    if isinstance(saved, list):
        return sorted({method for method in saved if isinstance(method, str) and method in PAYMENT_METHOD_LABELS})
    if movement is None or session is None or movement.order_id is None:
        return []
    # Old refunds retained their payment rows. Their order relationship, not
    # arbitrary payload IDs, establishes scope and the historical payment set.
    return sorted(set(db.scalars(select(Payment.method).join(Order, Order.id == Payment.order_id).where(
        Payment.order_id == movement.order_id, Payment.status == "confirmed",
        Payment.business_id == session.business_id, Order.business_id == session.business_id,
        Order.branch_id == session.branch_id, Payment.received_at <= movement.created_at,
    ))))


def _cash_register(db: Session, event: AuditEvent, session: CashSession | None) -> CashRegister | None:
    if session is None:
        return None
    return db.scalar(select(CashRegister).where(
        CashRegister.id == session.register_id, CashRegister.business_id == event.business_id,
        CashRegister.branch_id == session.branch_id,
    ))


def _operations(db: Session, payload: dict, order: Order | None) -> list[dict]:
    operations = payload.get("operations")
    if not isinstance(operations, list):
        return []
    legacy_revisions = _legacy_ticket_revisions(db, order) if not payload.get("snapshot_version") and order is not None else []
    result = []
    for raw in operations:
        if not isinstance(raw, dict) or raw.get("type") not in {"cancel", "edit"}:
            continue
        op = {**raw, "before": _dict(raw.get("before")), "after": _dict(raw.get("after"))}
        if not payload.get("snapshot_version") and order is not None:
            original = db.scalar(select(OrderItem).where(
                OrderItem.id == _id(raw.get("item_id")), OrderItem.order_id == order.id,
            ))
            if original is not None:
                op["before"] = item_audit_snapshot(original)
                # Quantities/prices belong to this immutable item version, but
                # promotions may have changed while that version was active.
                op["before"].pop("net_total")
                op["before"].pop("promotion_discount")
                replacement = db.scalar(select(OrderItem).where(
                    OrderItem.id == _id(raw.get("replacement_item_id")),
                    OrderItem.order_id == order.id, OrderItem.replaces_item_id == original.id,
                )) if raw["type"] == "edit" else None
                if replacement is not None:
                    op["after"] = item_audit_snapshot(replacement)
                    op["after"].pop("net_total")
                    op["after"].pop("promotion_discount")
                matches = []
                for revision in legacy_revisions:
                    before, after = _dict(revision.get("before")), _dict(revision.get("after"))
                    expected_after = raw.get("replacement_item_id") if raw["type"] == "edit" else raw.get("item_id")
                    if before.get("item_id") != original.id or after.get("item_id") != expected_after:
                        continue
                    if raw["type"] == "cancel" and after.get("action") != "cancelled":
                        continue
                    pair = (_ticket_item(before), _ticket_item(after))
                    if pair not in matches:
                        matches.append(pair)
                if len(matches) == 1:
                    # A rounded line total cannot recover an exact unit price
                    # for fractional quantities; retain the saved item price.
                    matches[0][0].setdefault("unit_price", op["before"].get("unit_price"))
                    matches[0][1].setdefault("unit_price", op["after"].get("unit_price"))
                    op["before"], op["after"] = matches[0]
        result.append(op)
    return result


def project_audit(db: Session, event: AuditEvent) -> dict:
    payload = _dict(event.payload)
    order = _order(db, event)
    session, movement = _cash(db, event)
    branch_id = event.branch_id
    if branch_id is None:
        branch_id = order.branch_id if order is not None else (session.branch_id if session else None)
    categories, sections, fields = [], [], []
    target = None
    actor = _text(event.actor_display_name) or (
        _text(session.actor_display_name) if event.action == "cash.cut_created" and session and session.status == "closed" else None
    ) or "Usuario no registrado"
    summary = _known_summary(event.action)
    identity = _dict(payload.get("order"))
    if not payload.get("snapshot_version") and order is not None:
        identity = order_identity_snapshot(order)
    if event.entity_type == "order":
        number, folio = _text(identity.get("number")), _id(identity.get("folio"))
        fields = [_field("ID de pedido", f"#{number}" if number else None), _field("Folio de pedido", f"#{folio}" if folio else None)]
        if order is not None:
            label = f"Pedido #{number}" if number else "Pedido"
            label += f" (#{folio})" if folio else " (sin folio)"
            target = {"kind": "order", "branch_id": order.branch_id, "label": label, "order_id": order.id}
    if event.action == "order.cancelled":
        categories.append("order_cancellation")
        paid = _number(payload.get("paid_amount"))
        summary = "cancel\u00f3 un pedido no pagado" if paid == 0 else "cancel\u00f3 un pedido"
        fields += [_field("Motivo de cancelaci\u00f3n", _text(payload.get("reason"))), _field("Total", _amount(payload.get("total"))), _field("Monto cobrado", _amount(payload.get("paid_amount")))]
        for item in payload.get("items", []) if isinstance(payload.get("items"), list) else []:
            item = _dict(item)
            sections.append({"title": "Producto cancelado", "fields": [
                _field("Producto", _text(item.get("product_name"))),
                _field("Tama\u00f1o", _text(item.get("variant_name"))),
                _field("Cantidad cancelada", _numeric(item.get("quantity"))),
                _field("Importe", _amount(item.get("net_total"))),
            ]})
    elif event.action == "order.items_revised":
        operations = _operations(db, payload, order)
        if any(op["type"] == "cancel" for op in operations):
            categories.append("item_cancellation")
        if any(op["type"] == "edit" and _reduced(op["before"], op["after"]) for op in operations):
            categories.append("amount_reduction")
        if len(categories) == 2:
            summary = "cancel\u00f3 productos y redujo el importe de otros productos de un pedido"
        elif "item_cancellation" in categories:
            summary = "cancel\u00f3 productos de un pedido"
        elif "amount_reduction" in categories:
            summary = "edit\u00f3 productos reduciendo su importe"
        else:
            summary = "edit\u00f3 productos de un pedido"
        for op in operations:
            before, after = op["before"], op["after"]
            rows = [_field("Producto", _text(before.get("product_name"))), _field("Tama\u00f1o", _text(before.get("variant_name")))]
            if op["type"] == "cancel":
                rows += [_field("Motivo de cancelaci\u00f3n", _text(op.get("reason"))), _field("Cantidad cancelada", _numeric(before.get("quantity"))), _field("Importe cancelado", _amount(before.get("net_total")))]
            else:
                rows += [_field("Producto posterior", _text(after.get("product_name"))), _field("Tama\u00f1o posterior", _text(after.get("variant_name"))), _field("Cantidad anterior", _numeric(before.get("quantity"))), _field("Cantidad posterior", _numeric(after.get("quantity"))), _field("Precio anterior", _amount(before.get("unit_price"))), _field("Precio posterior", _amount(after.get("unit_price"))), _field("Importe anterior", _amount(before.get("net_total"))), _field("Importe posterior", _amount(after.get("net_total")))]
            sections.append({"title": "Producto cancelado" if op["type"] == "cancel" else "Producto modificado", "fields": rows})
    elif event.action == "cash.cut_created":
        cut = _closed_cut(payload, session)
        methods = [_dict(method) for method in cut.get("methods", [])] if isinstance(cut.get("methods"), list) else []
        if any(_text(method.get("key")) in {"cash", "card"} and _number(method.get("counted")) is not None
               and _number(method.get("difference")) not in {None, Decimal(0)} for method in methods):
            categories.append("cash_discrepancy")
            summary = "realiz\u00f3 un corte de caja con diferencias"
        cut_id = session.id if session and session.status == "closed" else _id(cut.get("cut_id"))
        fields = [
            _field("ID del corte", f"#{cut_id}" if cut_id else None),
            _field("Caja", _text(cut.get("register_name"))),
            _field("Fondo anterior", _amount(cut.get("opening_amount"))),
            _field("Fondo conservado", _amount(cut.get("retained_fund_amount"))),
            _field("Efectivo retirado al cierre", _amount(cut.get("cash_withdrawn_amount"))),
        ]
        for method in methods:
            label = {"cash": "Efectivo", "card": "Tarjeta", "transfer": "Transferencias"}.get(_text(method.get("key")))
            if label:
                sections.append({"title": label, "fields": [
                    _field("Contado", _amount(method.get("counted"))),
                    _field("Monto esperado", _amount(method.get("expected"))),
                    _field("Diferencia", _amount(method.get("difference"))),
                ]})
        if session and session.status == "closed" and _cash_register(db, event, session):
            target = {"kind": "cash_cut", "branch_id": session.branch_id, "register_id": session.register_id,
                      "cut_id": session.id, "label": f"Corte #{session.id}"}
    elif event.action in {"cash.movement_created", "cash.refund_created"}:
        cash = payload
        if not payload.get("snapshot_version") and movement is not None:
            cash = {"movement_type": movement.movement_type, "amount": str(movement.amount),
                    "payment_method": movement.payment_method, "note": movement.note, **payload}
        if cash.get("movement_type") in {"withdrawal", "expense"}:
            categories.append("cash_withdrawal")
            summary = "registr\u00f3 un gasto de caja" if cash.get("movement_type") == "expense" else "realiz\u00f3 un retiro de efectivo"
        elif cash.get("movement_type") == "refund":
            summary = "registr\u00f3 un reembolso"
        else:
            summary = "registr\u00f3 un movimiento de caja"
        fields = [
            _field("ID de movimiento", f"#{movement.id}" if movement else (f"#{_id(cash['movement_id'])}" if _id(cash.get("movement_id")) else None)),
            _field("ID de la caja", f"#{session.register_id}" if session else (f"#{_id(cash['register_id'])}" if _id(cash.get("register_id")) else None)),
            _field("Caja", _text(cash.get("register_name"))),
            _field("Monto", _amount(cash.get("amount"))),
            _field("M\u00e9todo", _text(cash.get("payment_method"))),
            _field("Nota", _text(cash.get("note"))),
        ]
        if cash.get("movement_type") == "refund":
            original_methods = _refund_original_methods(db, payload, movement, session)
            refund_method = _text(cash.get("payment_method"))
            if original_methods and refund_method in PAYMENT_METHOD_LABELS and refund_method not in original_methods:
                categories.append("refund_method_change")
                summary = "registr\u00f3 un reembolso con un m\u00e9todo distinto del cobro"
                fields.append(_field("M\u00e9todos del cobro original", ", ".join(PAYMENT_METHOD_LABELS.get(method, method) for method in original_methods)))
        if movement is not None and session is not None:
            register = _cash_register(db, event, session)
            if register:
                target = {"kind": "cash_movement", "branch_id": session.branch_id, "label": f"Movimiento #{movement.id}", "register_id": register.id, "movement_id": movement.id}
    else:
        fields.append(_field("Acci\u00f3n", summary))
    occurred_at = event.created_at
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)
    return {
        "id": event.id, "branch_id": branch_id, "actor_name": actor,
        "occurred_at": occurred_at.astimezone(LIMA), "summary": summary,
        "fields": fields, "sections": sections, "target": target,
        "categories": categories,
    }


def audit_list_entry(event: AuditEvent, projection: dict) -> dict:
    return {
        "id": event.id, "business_id": event.business_id, "branch_id": projection["branch_id"],
        "actor_id": event.actor_id, "actor_display_name": projection["actor_name"] if event.action == "cash.cut_created" else event.actor_display_name,
        "action": event.action, "entity_type": event.entity_type, "entity_id": event.entity_id,
        "payload": event.payload, "created_at": event.created_at,
        "summary": projection["summary"], "categories": projection["categories"],
    }
