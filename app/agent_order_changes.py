"""Kitchen completion guard and durable staff notices for agent order edits."""

from datetime import datetime, timedelta, timezone
from copy import deepcopy

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from .models import AuditEvent, KitchenTicket, Order, utcnow
from .services import audit
from .command_revisions import effective_ticket_context


AGENT_CHANGE_SUMMARIES = {
    "agent.fulfillment_updated": "El agente cambió la modalidad o dirección de entrega.",
    "agent.items_revised": "El agente modificó productos del pedido.",
    "agent.items_added": "El agente agregó productos al pedido.",
}
RECENT_CHANGE_WINDOW = timedelta(hours=48)
FINAL_ORDER_STATUSES = {"cancelled", "closed", "delivered", "dispatched"}


def kitchen_was_completed(db: Session, order: Order) -> bool:
    """A reopened ticket still counts as having been completed once."""
    tickets = list(db.scalars(select(KitchenTicket).where(
        KitchenTicket.order_id == order.id,
        KitchenTicket.business_id == order.business_id,
        KitchenTicket.branch_id == order.branch_id,
    )))
    if any(ticket.status in {"ready", "served"} or ticket.ready_at is not None for ticket in tickets):
        return True
    ids = [str(ticket.id) for ticket in tickets]
    return bool(ids and db.scalar(select(AuditEvent.id).where(
        AuditEvent.business_id == order.business_id,
        AuditEvent.entity_type == "kitchen_ticket",
        AuditEvent.entity_id.in_(ids),
        AuditEvent.action == "kitchen.ready",
    ).limit(1)))


def record_agent_order_change(db: Session, user, order: Order, action: str, details: dict | None = None) -> dict:
    if action not in AGENT_CHANGE_SUMMARIES:
        raise ValueError("Unknown agent order change")
    marker = {"at": utcnow().isoformat(), "source": "agent", "summary": AGENT_CHANGE_SUMMARIES[action]}
    audit(db, user, action, "order", order.id, order.business_id,
          {"at": marker["at"], **(details or {})}, branch_id=order.branch_id)
    order._recent_modification = marker
    if action == "agent.items_added":
        item_ids = (details or {}).get("item_ids", [])
        order._recent_agent_addition = {**marker, "item_count": len(set(item_ids))} if item_ids else None
    return marker


def update_active_ticket_fulfillment(db: Session, user, order: Order) -> list[KitchenTicket]:
    tickets = list(db.scalars(select(KitchenTicket).where(
        KitchenTicket.order_id == order.id, KitchenTicket.business_id == order.business_id,
        KitchenTicket.branch_id == order.branch_id, KitchenTicket.status.in_(["queued", "preparing"]),
    ).with_for_update()))
    at = utcnow().isoformat()
    after = {"channel": order.channel, "delivery_address": deepcopy(order.delivery_address), "notes": order.notes}
    for ticket in tickets:
        effective = effective_ticket_context(ticket)
        before = {key: deepcopy(effective.get(key)) for key in after}
        context = deepcopy(ticket.context_snapshot or {})
        context.setdefault("fulfillment_revisions", []).append({
            "before": before, "after": deepcopy(after), "created_at": at, "created_by": user.user_id,
        })
        context["fulfillment_current"] = deepcopy(after)
        ticket.context_snapshot = context
        ticket.version += 1
    return tickets


def attach_recent_agent_order_changes(db: Session, orders: list[Order]) -> None:
    for order in orders:
        order._recent_modification = None
        order._recent_agent_addition = None
    scoped_orders = {order.id: order for order in orders}
    open_ids = [order.id for order in orders if order.status not in FINAL_ORDER_STATUSES]
    if not scoped_orders:
        return
    events = db.scalars(select(AuditEvent).where(
        AuditEvent.entity_type == "order",
        AuditEvent.business_id.in_({order.business_id for order in orders}),
        AuditEvent.branch_id.in_({order.branch_id for order in orders}),
        AuditEvent.entity_id.in_([str(order_id) for order_id in scoped_orders]),
        AuditEvent.action.in_(AGENT_CHANGE_SUMMARIES),
        or_(AuditEvent.action == "agent.items_added", and_(
            AuditEvent.entity_id.in_([str(order_id) for order_id in open_ids]),
            AuditEvent.created_at >= utcnow() - RECENT_CHANGE_WINDOW)),
    ).order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()))
    for event in events:
        order = scoped_orders.get(int(event.entity_id))
        if order is None or order.business_id != event.business_id or order.branch_id != event.branch_id:
            continue
        at = (event.payload or {}).get("at")
        try:
            moment = datetime.fromisoformat(at) if isinstance(at, str) else event.created_at
        except ValueError:
            moment = event.created_at
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        marker = {
            "at": moment.isoformat(), "source": "agent", "summary": AGENT_CHANGE_SUMMARIES[event.action],
        }
        if (order.id in open_ids and utcnow() - moment <= RECENT_CHANGE_WINDOW
                and getattr(order, "_recent_modification", None) is None):
            order._recent_modification = marker
        if event.action == "agent.items_added" and getattr(order, "_recent_agent_addition", None) is None:
            item_ids = (event.payload or {}).get("item_ids", [])
            actual_ids = {item.id for item in order.items}
            if isinstance(item_ids, list):
                recorded_ids = {item_id for item_id in item_ids
                                if isinstance(item_id, int) and not isinstance(item_id, bool) and item_id in actual_ids}
                if recorded_ids:
                    order._recent_agent_addition = {**marker, "item_count": len(recorded_ids)}
