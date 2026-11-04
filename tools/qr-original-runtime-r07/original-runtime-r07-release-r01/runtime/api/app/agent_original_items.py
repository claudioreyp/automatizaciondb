"""Versioned original-item changes before the first kitchen command. No printing."""
from copy import deepcopy
from decimal import Decimal

from sqlalchemy import select

from .agent_checkout import fingerprint
from .agent_original_policy import before_kitchen_mutable
from .auth import AuthContext
from .errors import CodedHTTPException
from .models import Payment, PaymentEvidence, OrderPaymentRequest
from .services import (INACTIVE_ORDER_ITEM_STATUSES, active_order_items, audit, build_order_item,
    commit_order_items_stock, ensure_order_products_mutable, money, recalculate_order,
    restore_order_items_stock, serialize_order, sync_order_payment_status, transition_order)


def assert_before_kitchen(db, order):
    if not before_kitchen_mutable(db, order):
        raise CodedHTTPException(409, 'Original products are locked after the first kitchen command', 'ORDER_ITEMS_ALREADY_SENT')


def _apply(db, order, operations, *, verify_agent_service=False):
    assert_before_kitchen(db, order)
    ensure_order_products_mutable(db, order, 'revising original products',
        allow_open_initial_evidence=True, allow_confirmed_payment=True)
    ids = [operation.item_id for operation in operations]
    if len(set(ids)) != len(ids):
        raise CodedHTTPException(422, 'Each original product can be revised once', 'DUPLICATE_ITEM_REVISION')
    candidates = {item.id: item for item in active_order_items(order) if item.id in ids}
    if len(candidates) != len(ids):
        raise CodedHTTPException(404, 'One or more active original products were not found', 'ORDER_ITEM_NOT_FOUND')
    removed = {operation.item_id for operation in operations if operation.type == 'cancel'}
    if all(item.id in removed for item in active_order_items(order)):
        raise CodedHTTPException(409, 'Use the explicit order cancellation operation', 'ORDER_REQUIRES_CANCELLATION')
    user = AuthContext('integration', 'owner', order.business_id, order.branch_id)
    originals, replacements = list(candidates.values()), []
    historical = {item.id: (item.promotion_discount, deepcopy(item.promotion_snapshot)) for item in order.items}
    reserved = order.status == 'confirmed'
    if reserved:
        restore_order_items_stock(db, user, order, originals)
        db.flush()
    replacement_lines = [operation.replacement for operation in operations if operation.type == 'edit']
    if replacement_lines:
        from .agent_checkout import preview_items
        from .models import Branch
        preview_items(db, db.get(Branch, order.branch_id), order.channel, replacement_lines,
            order.payment_method, verify_agent_service=verify_agent_service, check_schedules=verify_agent_service)
    for operation in operations:
        original = candidates[operation.item_id]
        if operation.type == 'edit':
            replacement = build_order_item(db, order.business_id, order.branch_id,
                operation.replacement, order.channel, order.source)
            original.status = 'superseded'
            replacement.replaces_item_id = original.id
            order.items.append(replacement)
            replacements.append(replacement)
        else:
            original.status = 'cancelled'
            original.cancellation_reason = (operation.reason or '').strip()
    db.flush()
    if replacements and reserved:
        commit_order_items_stock(db, user, order, replacements)
    recalculate_order(db, order)
    for item in order.items:
        if item.status in INACTIVE_ORDER_ITEM_STATUSES and item.id in historical:
            item.promotion_discount, item.promotion_snapshot = historical[item.id]
    order.version += 1
    audit(db, user, 'order.items_revised', 'order', order.id, order.business_id,
        {'item_ids': ids, 'replacement_item_ids': [item.id for item in replacements]}, branch_id=order.branch_id)


def _original_snapshot(order):
    return [(item.id, item.product_id, str(Decimal(str(item.quantity)).normalize()),
             str(money(item.line_total)), item.status) for item in active_order_items(order)]


def _priced_snapshot(order):
    return [{'product_id': item.product_id, 'name': item.product_name, 'variant_name': item.variant_name,
             'quantity': str(Decimal(str(item.quantity)).normalize()), 'unit_price': str(money(item.unit_price)),
             'line_total': str(money(item.line_total)), 'notes': item.notes, 'modifiers': deepcopy(item.modifiers)}
            for item in active_order_items(order)]


def _quote_fingerprint(order, operations, version, original):
    return fingerprint({'order_id': order.id, 'business_id': order.business_id, 'branch_id': order.branch_id,
        'version': version, 'original': original, 'operations': [op.model_dump(mode='json') for op in operations],
        'lines': _priced_snapshot(order), 'total': str(money(order.total)), 'delivery_fee': str(money(order.delivery_fee))})


def preview_revisions(db, order, operations, *, verify_agent_service=False):
    """Use the POS pricing/stock validators in a rollback-only savepoint."""
    assert_before_kitchen(db, order)
    version = order.version
    original = _original_snapshot(order)
    savepoint = db.begin_nested()
    try:
        _apply(db, order, operations, verify_agent_service=verify_agent_service)
        db.flush()
        lines = _priced_snapshot(order)
        total = str(money(order.total))
        quote = _quote_fingerprint(order, operations, version, original)
        result = {'allowed': True, 'order_id': order.id, 'order_version': version,
                  'quote_fingerprint': quote, 'expected_total': float(total), 'items': lines}
    finally:
        savepoint.rollback()
        db.expire_all()
    return result


def _financial_review(db, order, *, old_total, operation_key, cancelled=False):
    """Never change confirmed money or receipt files; retire stale pending evidence."""
    changed_amount = money(order.total) != money(old_total)
    evidence_ids = []
    if changed_amount or cancelled:
        for evidence in db.scalars(select(PaymentEvidence).where(PaymentEvidence.order_id == order.id,
                PaymentEvidence.status.in_(['under_review', 'evidence_received'])).with_for_update()):
            evidence_ids.append(evidence.id)
            evidence.status = 'superseded'
            evidence.rejection_reason = 'Original order changed before kitchen; original receipt retained for staff review'
            evidence.analysis = {**(evidence.analysis or {}), 'requires_financial_review': True,
                'original_order_change': {'previous_total': str(money(old_total)), 'current_total': str(money(order.total)),
                                          'cancelled': cancelled, 'order_version': order.version}}
    paid = sum((payment.amount for payment in db.scalars(select(Payment).where(
        Payment.order_id == order.id, Payment.status == 'confirmed'))), Decimal(0))
    needs_review = bool(evidence_ids or (paid > 0 and (changed_amount or cancelled)))
    if cancelled:
        for request in db.scalars(select(OrderPaymentRequest).where(OrderPaymentRequest.order_id == order.id,
                OrderPaymentRequest.status.notin_(['paid', 'superseded', 'waived', 'cancelled', 'declined']))):
            request.status = 'superseded'
            request.snapshot = {**(request.snapshot or {}), 'cancelled_before_kitchen': True}
            request.version += 1
    if needs_review:
        from .agent_attention import create_attention
        create_attention(db, business_id=order.business_id, branch_id=order.branch_id,
            sender=order.whatsapp_chat_id or order.customer_phone, kind='financial_review', order_id=order.id,
            summary='REVISIÓN FINANCIERA: pedido corregido o cancelado antes de cocina. Pagos y comprobantes anteriores conservados; no se emitió devolución.',
            source_key='original-items:' + operation_key,
            details={'previous_total': str(money(old_total)), 'current_total': str(money(order.total)),
                     'paid_amount': str(money(paid)), 'cancelled': cancelled, 'evidence_ids': evidence_ids})
    sync_order_payment_status(db, order)
    return {'requires_payment_review': needs_review, 'superseded_evidence_ids': evidence_ids,
            'approved_payments_preserved': True, 'refund_created': False}


def commit_revisions(db, order, operations, *, quote_fingerprint, expected_total, operation_key, verify_agent_service=False):
    quote = preview_revisions(db, order, operations, verify_agent_service=verify_agent_service)
    if quote['quote_fingerprint'] != quote_fingerprint or money(quote['expected_total']) != money(expected_total):
        raise CodedHTTPException(409, 'Review the current original-product price', 'ORIGINAL_ITEMS_QUOTE_CHANGED')
    old_total, version, original = order.total, order.version, _original_snapshot(order)
    savepoint = db.begin_nested()
    try:
        _apply(db, order, operations, verify_agent_service=verify_agent_service)
        db.flush()
        assert_before_kitchen(db, order)
        if _quote_fingerprint(order, operations, version, original) != quote_fingerprint:
            raise CodedHTTPException(409, 'Original-product pricing changed during commit', 'ORIGINAL_ITEMS_QUOTE_CHANGED')
        payment = _financial_review(db, order, old_total=old_total, operation_key=operation_key)
        db.flush()
        result = {'order': serialize_order(order), 'changed': True, 'cancelled': False, **payment,
                  'sent_to_kitchen': False, 'tickets': [], 'command_committed': False}
        savepoint.commit()
        return result
    except Exception:
        savepoint.rollback()
        db.expire_all()
        raise


def cancel_before_kitchen(db, order, *, reason, operation_key):
    assert_before_kitchen(db, order)
    old_total = order.total
    # Retire pending evidence first so the generic POS transition can enforce its
    # normal stock/audit invariants. The receipt bytes and detected money survive.
    savepoint = db.begin_nested()
    try:
        payment = _financial_review(db, order, old_total=old_total, operation_key=operation_key, cancelled=True)
        assert_before_kitchen(db, order)
        user = AuthContext('integration', 'owner', order.business_id, order.branch_id)
        transition_order(db, user, order, 'cancelled', reason=reason)
        db.flush()
        result = {'order': serialize_order(order), 'changed': True, 'cancelled': True, **payment,
                  'sent_to_kitchen': False, 'tickets': [], 'command_committed': False}
        savepoint.commit()
        return result
    except Exception:
        savepoint.rollback()
        db.expire_all()
        raise
