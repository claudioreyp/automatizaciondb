"""Original-product policy grounded in POS and confirmed transport delivery.

Conversation history and worker/model booleans are never proof that a customer
received an explanation. A committed response is still pending until its ACK.
"""
from copy import deepcopy
from datetime import timezone
import hashlib

from sqlalchemy import select

from .models import AgentMessageDelivery, IdempotencyRecord, KitchenTicket, Order

REVIEW_TITLE = 'REQUIERE REVISIÓN HUMANA: EL CLIENTE INSISTE EN MODIFICAR SU PEDIDO. REVISAR LA CONVERSACIÓN DE WHATSAPP'
NOTICE_KIND = 'original_items_after_kitchen'


def kitchen_started(db, order):
    return bool(order.sent_to_kitchen_at is not None or db.scalar(select(KitchenTicket.id).where(
        KitchenTicket.order_id == order.id, KitchenTicket.business_id == order.business_id,
        KitchenTicket.branch_id == order.branch_id).limit(1)) is not None)


def before_kitchen_mutable(db, order):
    return bool(order.status in {'draft', 'pending_confirmation', 'confirmed'}
                and not order.table_released_at and not order.checkout_started_at
                and not kitchen_started(db, order))


def restriction_text(order):
    folio = f' #{order.folio}' if order.folio else ''
    return f'Tu pedido{folio} ya fue enviado a cocina. Sus productos originales no pueden modificarse ni cancelarse por este chat.'


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _own_order(db, row, order_id):
    order = db.scalar(select(Order).where(Order.id == order_id, Order.business_id == row.business_id,
        Order.branch_id == row.branch_id))
    if order is None:
        return None
    from .agent_checkout import assert_sender
    try:
        assert_sender(order, row.sender)
    except Exception:
        return None
    return order


def annotate_policy_response(db, row, state, response):
    """Consume a candidate, stamp only the private outbox copy; never 'explained'."""
    candidate = state.pop('original_order_explanation_candidate', None)
    result = deepcopy(response)
    if not isinstance(result, dict):
        return result
    result.pop('_original_order_notice', None)
    if (not isinstance(candidate, dict) or set(candidate) != {'kind', 'order_id', 'message_id'}
            or candidate['kind'] != NOTICE_KIND or candidate['message_id'] != row.current_message_id
            or str(candidate['order_id']) != str(state.get('order_id'))):
        return result
    order = _own_order(db, row, candidate['order_id'])
    if order is None or not kitchen_started(db, order):
        return result
    text = restriction_text(order)
    if not any(isinstance(action, dict) and action.get('type') == 'text'
               and isinstance(action.get('text'), str) and text in action['text']
               for action in result.get('actions', [])):
        return result
    result['_original_order_notice'] = {'kind': NOTICE_KIND, 'order_id': order.id,
        'message_id': row.current_message_id, 'text_sha256': hashlib.sha256(text.encode()).hexdigest()}
    return result


def confirmed_original_policy_context(db, row, current_message_id):
    """An explanation must have been ACKed before the *receipt* of this input."""
    order_id = (row.state or {}).get('order_id')
    order = _own_order(db, row, order_id) if order_id else None
    if order is None or not kitchen_started(db, order):
        return None
    from .agent_message_batching import trusted_input_created_at
    received_at = trusted_input_created_at(db, row, current_message_id)
    if received_at is None:
        return None
    expected = hashlib.sha256(restriction_text(order).encode()).hexdigest()
    deliveries = db.scalars(select(AgentMessageDelivery).where(
        AgentMessageDelivery.business_id == row.business_id, AgentMessageDelivery.branch_id == row.branch_id,
        AgentMessageDelivery.conversation_id == row.id, AgentMessageDelivery.sender == row.sender,
        AgentMessageDelivery.status == 'sent').order_by(AgentMessageDelivery.updated_at.desc()))
    for delivery in deliveries:
        notice = (delivery.response or {}).get('_original_order_notice')
        if (isinstance(notice, dict) and notice.get('kind') == NOTICE_KIND
                and notice.get('order_id') == order.id and notice.get('message_id') == delivery.message_id
                and notice.get('text_sha256') == expected and delivery.message_id != current_message_id
                and _utc(delivery.updated_at) < _utc(received_at)):
            return {'status': 'sent', 'confirmation': 'transport_ack', 'kind': NOTICE_KIND,
                    'conversation_id': row.id, 'order_id': order.id, 'message_id': delivery.message_id,
                    'delivery_id': delivery.id, 'text_sha256': expected}
    return None
