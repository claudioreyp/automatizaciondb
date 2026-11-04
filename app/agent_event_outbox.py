"""Durable handoff of notices prepared by the trusted n8n notifier.

No conversational policy or text generation belongs here. The API binds the
prepared notice to current own POS rows, and uses the existing native outbox.
"""
from copy import deepcopy
from datetime import timezone
import hashlib
import json
import re
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import ConfigDict, Field, model_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from .auth import IntegrationAuthContext, require_integration_scope
from .database import get_db
from .models import AgentConversation, AgentMessageDelivery, IdempotencyRecord, IntegrationEvent, Order, utcnow
from .schemas import ApiModel
from .agent_checkout import fingerprint
from .agent_delivery_claims import validate_delivery_claim, validate_delivery_ack_receipt
from .errors import CodedHTTPException
from . import agent_message_batching as batching

router = APIRouter(prefix="/api/v1/integrations/events", tags=["pos-event-outbox"])
NOTICE_TYPES = frozenset({"payment.approved", "order.ready", "order.dispatched", "order.addition_committed", "order.delivery_fee_confirmed"})


def recipient(value):
    if not isinstance(value, str):
        return None
    if re.fullmatch(r"\d{8,20}@lid", value):
        return value
    if re.search(r"@(?:g\.us|newsletter|broadcast)", value, re.I):
        return None
    phone = re.sub(r"@(?:c\.us|s\.whatsapp\.net)$", "", value, flags=re.I).removeprefix("+")
    return phone if re.fullmatch(r"\d{8,15}", phone) else None


def event_fingerprint(event):
    # Attempts and acknowledgements are transport metadata, not event identity.
    return fingerprint({"id": event.id, "business_id": event.business_id, "branch_id": event.branch_id,
        "event_type": event.event_type, "aggregate_type": event.aggregate_type, "aggregate_id": event.aggregate_id,
        "customer_phone": event.customer_phone, "whatsapp_chat_id": event.whatsapp_chat_id,
        "payload": event.payload, "created_at": event.created_at.isoformat() if event.created_at else None})


class PreparedNotice(ApiModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    order_id: int = Field(gt=0)
    expected_order_version: int = Field(ge=1)
    event_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    recipient: str = Field(min_length=8, max_length=40)
    message: str = Field(min_length=1, max_length=2000)
    confirmation_key: str | None = Field(default=None, max_length=500)


class RetiredNoticeAttempt(ApiModel):
    """Attestation from the fixed internal transport, never a consumer tool input.

    The transport validates the real v1 records and wins the complete CAS before
    calling this route. The POS validates its own reservation and source here;
    it does not pretend this descriptor alone independently proves n8n storage.
    """
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["native-qr-mailbox-retirement/v1"]
    business_id: int = Field(gt=0)
    branch_id: int = Field(gt=0)
    delivery_id: str = Field(min_length=1, max_length=36)
    message_id: str = Field(min_length=1, max_length=240)
    sender: str = Field(min_length=8, max_length=120)
    source_row_id: int = Field(gt=0)
    source_workflow_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,200}$")
    source_run_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,200}$")
    source_head: int = Field(ge=0)
    source_state: Literal["mailbox_admission_consumed"]
    source_body_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_deadline_at_ms: int = Field(gt=0)
    mailbox_row_id: int = Field(gt=0)
    mailbox_workflow_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,200}$")
    mailbox_code_manifest: str = Field(pattern=r"^[a-f0-9]{64}$")
    mailbox_head_before: Literal[0]
    mailbox_head_after: Literal[1]
    mailbox_state: Literal["retired_no_transport"]
    mailbox_body_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    mailbox_deadline_at_ms: int = Field(gt=0)
    retirement_owner_execution: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,200}$")
    retirement_nonce: str = Field(pattern=r"^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$")

    @model_validator(mode="before")
    @classmethod
    def heads_are_integers(cls, value):
        if isinstance(value, dict) and any(type(value.get(key)) is not int for key in ("mailbox_head_before", "mailbox_head_after")):
            raise ValueError("Retirement heads must be literal integers")
        return value


class ReplaceRetiredNotice(ApiModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_event_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    delivery_token: str = Field(min_length=20, max_length=150, repr=False)
    retirement: RetiredNoticeAttempt


class NoticeReplacementResult(ApiModel):
    status: Literal["pending"]
    old_delivery_id: str
    delivery_id: str
    message_id: str
    event_id: str
    event_acknowledged: Literal[False]


def source_scope(branch_id):
    return f"pos-notice-source:{branch_id}"


def insert_once(db, model, values, columns):
    # Use the existing database unique constraint atomically. In particular,
    # never rely on a SELECT followed by a process-local lock or an insert retry.
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise HTTPException(503, "Notice outbox storage is not supported")
    db.execute(insert(model).values(**values).on_conflict_do_nothing(index_elements=columns))


def confirmation_value(event, order):
    # These are operation identities, not conversation or message rules.
    payload = event.payload or {}
    if event.event_type == "order.delivery_fee_confirmed":
        return [order.id, payload.get("quote_id"), payload.get("payment_request_id"), payload.get("delivery_fee"), payload.get("request_amount")]
    if event.event_type == "payment.approved" and payload.get("original_payment_method_change") is True:
        return ["payment.approved", order.id, payload.get("payment_id")]
    return None


def source_rows(db, delivery):
    return list(db.scalars(select(IdempotencyRecord).where(
        IdempotencyRecord.business_id == delivery.business_id,
        IdempotencyRecord.scope == source_scope(delivery.branch_id),
        IdempotencyRecord.response_body["delivery_id"].as_string() == delivery.id).order_by(IdempotencyRecord.id).limit(201)))


def acknowledge_notice_sources(db, delivery):
    if not isinstance(delivery.response, dict) or "_pos_notice" not in delivery.response:
        return
    if delivery.status != "sent" or not str((delivery.receipt or {}).get("transport_id", "")).strip():
        raise HTTPException(409, "Notice has no confirmed transport receipt")
    sources = source_rows(db, delivery)
    if not sources or len(sources) > 200:
        raise HTTPException(409, "Notice source mapping requires review")
    # Existing delivery ACK holds the outbox row lock. No external work is done
    # while these short, scoped source updates are in the transaction.
    for source in sources:
        event = db.scalar(select(IntegrationEvent).where(IntegrationEvent.id == source.idempotency_key,
            IntegrationEvent.business_id == delivery.business_id, IntegrationEvent.branch_id == delivery.branch_id).with_for_update())
        if not event or event_fingerprint(event) != source.response_body.get("event_fingerprint"):
            raise HTTPException(409, "Notice source changed before acknowledgement")
        event.acknowledged_at = event.acknowledged_at or utcnow()
        event.acknowledged_by = event.acknowledged_by or "outbox:" + delivery.id


def notice_is_current(db, delivery):
    notice = (delivery.response or {}).get("_pos_notice")
    if not isinstance(notice, dict) or notice.get("version") != 1:
        return False
    event = db.scalar(select(IntegrationEvent).where(IntegrationEvent.id == notice.get("event_id"),
        IntegrationEvent.business_id == delivery.business_id, IntegrationEvent.branch_id == delivery.branch_id))
    order = db.scalar(select(Order).where(Order.id == notice.get("order_id"),
        Order.business_id == delivery.business_id, Order.branch_id == delivery.branch_id))
    if not event or not order or event_fingerprint(event) != notice.get("event_fingerprint"):
        return False
    current = recipient(order.whatsapp_chat_id or "") or recipient(order.customer_phone or "")
    target = recipient(event.whatsapp_chat_id or "") or recipient(event.customer_phone or "")
    return (current == target == delivery.sender == notice.get("recipient")
        and order.version == notice.get("order_version") and order.status == notice.get("order_status")
        and order.channel == notice.get("order_channel"))


def suppress_stale_notice(db, delivery, *, background_tasks=None):
    if notice_is_current(db, delivery):
        return False
    delivery.status = "suppressed"
    delivery.receipt = {"reason": "pos_notice_source_or_order_changed", "transport_attempted": False}
    request_notice_delivery_review(db, delivery, background_tasks=background_tasks)
    return True


def ensure_conversation(db, business_id, branch_id, sender):
    query = select(AgentConversation).where(AgentConversation.business_id == business_id,
        AgentConversation.branch_id == branch_id, AgentConversation.channel == "whatsapp", AgentConversation.sender == sender)
    row = db.scalar(query)
    if row:
        return row
    insert_once(db, AgentConversation, {"business_id": business_id, "branch_id": branch_id, "channel": "whatsapp",
        "sender": sender, "state": {}, "pending": [], "version": 1}, ["business_id", "branch_id", "channel", "sender"])
    row = db.scalar(query)
    if not row:
        raise HTTPException(409, "Notice conversation contention")
    return row


def current_notice_replacement(db, original):
    """Resolve an audited replacement using the POS's current source pointer.

    A late alias still discovers the original stable message identity. The
    original event mapping advances atomically with every replacement, so this
    resolves the latest child without rewriting or walking historical audits.
    Caller-provided event/delivery IDs never select the replacement target.
    """
    audit = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.business_id == original.business_id,
        IdempotencyRecord.scope == replacement_scope(original.branch_id), IdempotencyRecord.idempotency_key == original.id))
    if audit is None:
        return original
    result = (audit.response_body or {}).get("result", {})
    if (result.get("old_delivery_id") != original.id or result.get("delivery_id") != (original.receipt or {}).get("replacement_delivery_id")
            or original.status != "suppressed" or original.claim_hash is not None):
        raise HTTPException(409, "Notice replacement audit requires review")
    notice = (original.response or {}).get("_pos_notice", {})
    mapping = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.business_id == original.business_id,
        IdempotencyRecord.scope == source_scope(original.branch_id), IdempotencyRecord.idempotency_key == notice.get("event_id")))
    if not mapping or mapping.response_body.get("event_fingerprint") != notice.get("event_fingerprint"):
        raise HTTPException(409, "Current notice source pointer requires review")
    child = db.scalar(select(AgentMessageDelivery).where(AgentMessageDelivery.id == mapping.response_body.get("delivery_id"),
        AgentMessageDelivery.business_id == original.business_id, AgentMessageDelivery.branch_id == original.branch_id,
        AgentMessageDelivery.conversation_id == original.conversation_id).with_for_update().execution_options(populate_existing=True))
    expected = deepcopy(original.response)
    if child is not None:
        expected["turn"] = {**expected["turn"], "id": child.message_id}
    if not child or child.id == original.id or child.sender != original.sender or child.response != expected:
        raise HTTPException(409, "Current notice replacement identity requires review")
    return child


@router.post("/{event_id}/outbox")
def queue_prepared_notice(event_id: str, payload: PreparedNotice,
    integration: IntegrationAuthContext = Depends(require_integration_scope("events:deliver")), db=Depends(get_db)):
    if integration.profile != "generic" or not integration.credential_id or not integration.business_id or not integration.branch_id:
        raise HTTPException(403, "A dedicated internal notice credential is required")
    event = db.scalar(select(IntegrationEvent).where(IntegrationEvent.id == event_id,
        IntegrationEvent.business_id == integration.business_id, IntegrationEvent.branch_id == integration.branch_id))
    if not event or event.event_type not in NOTICE_TYPES or event.aggregate_type != "order":
        raise HTTPException(404, "Notice source not found")
    if (str(payload.order_id) != event.aggregate_id or (event.payload or {}).get("order_id", payload.order_id) != payload.order_id
            or payload.event_fingerprint != event_fingerprint(event)):
        raise HTTPException(409, "Notice source identity changed")
    order_query = select(Order).where(Order.id == payload.order_id, Order.business_id == integration.business_id,
        Order.branch_id == integration.branch_id)
    order = db.scalar(order_query)
    target = recipient(event.whatsapp_chat_id or "") or recipient(event.customer_phone or "")
    if not order or not target or target != payload.recipient or target != (recipient(order.whatsapp_chat_id or "") or recipient(order.customer_phone or "")):
        raise HTTPException(409, "Notice recipient or order changed")
    if order.version != payload.expected_order_version:
        raise HTTPException(409, "Notice order version changed")
    identity = confirmation_value(event, order)
    try:
        provided = json.loads(payload.confirmation_key) if payload.confirmation_key is not None else None
    except (TypeError, ValueError):
        raise HTTPException(422, "Invalid notice confirmation identity")
    if provided != identity:
        raise HTTPException(409, "Notice confirmation identity mismatch")
    message_id = "pos-notice:" + fingerprint([integration.branch_id, order.id, target, identity if identity is not None else ["event", event.id]])
    conversation = ensure_conversation(db, integration.business_id, integration.branch_id, target)
    conversation = db.scalar(select(AgentConversation).where(AgentConversation.id == conversation.id,
        AgentConversation.business_id == integration.business_id, AgentConversation.branch_id == integration.branch_id)
        .with_for_update().execution_options(populate_existing=True))
    # Create/find the conversation before locking an order, matching the existing
    # conversation-to-order lock order; delivery/source locks follow below.
    order = db.scalar(order_query.with_for_update(read=True).execution_options(populate_existing=True))
    if not order or order.version != payload.expected_order_version or target != (recipient(order.whatsapp_chat_id or "") or recipient(order.customer_phone or "")):
        raise HTTPException(409, "Notice order changed before durable handoff")
    if disposition_record(db, event.business_id, event.branch_id, event.id):
        raise HTTPException(409, "Notice source is held for review or was skipped")
    query = select(AgentMessageDelivery).where(AgentMessageDelivery.conversation_id == conversation.id,
        AgentMessageDelivery.message_id == message_id, AgentMessageDelivery.business_id == integration.business_id,
        AgentMessageDelivery.branch_id == integration.branch_id)
    # Serialize source mapping with the existing delivery ACK row lock.
    # An ACK cannot miss an alias committed while it reads the source set.
    query = query.with_for_update().execution_options(populate_existing=True)
    row = db.scalar(query)
    request_fingerprint = fingerprint({"sender": target, "text": payload.message, "order_id": order.id,
        "order_version": order.version, "confirmation": identity})
    if row is None:
        if event.acknowledged_at is not None:
            raise HTTPException(409, "Notice source was already acknowledged")
        transport = (conversation.state or {}).get("_transport", {})
        sequence = transport.get("input_sequence", 0) if isinstance(transport, dict) else 0
        response = {"actions": [{"type": "text", "text": payload.message}], "turn": {"id": message_id, "outcome": "none"},
            "_runtime_contract": batching.NATIVE_CONTRACT, "_input_sequence": sequence,
            "_pos_notice": {"version": 1, "event_id": event.id, "event_fingerprint": payload.event_fingerprint,
                "order_id": order.id, "order_version": order.version, "order_status": order.status,
                "order_channel": order.channel, "recipient": target, "request_fingerprint": request_fingerprint}}
        insert_once(db, AgentMessageDelivery, {"business_id": integration.business_id, "branch_id": integration.branch_id,
            "conversation_id": conversation.id, "message_id": message_id, "sender": target, "response": response}, ["conversation_id", "message_id"])
        row = db.scalar(query)
    if row is None or (row.response or {}).get("_pos_notice", {}).get("request_fingerprint") != request_fingerprint:
        raise HTTPException(409, "Notice outbox identity already has different data")
    row = current_notice_replacement(db, row)
    mapping_query = select(IdempotencyRecord).where(IdempotencyRecord.business_id == integration.business_id,
        IdempotencyRecord.scope == source_scope(integration.branch_id), IdempotencyRecord.idempotency_key == event.id)
    mapping = db.scalar(mapping_query)
    expected = {"delivery_id": row.id, "event_fingerprint": payload.event_fingerprint}
    if not mapping:
        insert_once(db, IdempotencyRecord, {"business_id": integration.business_id, "scope": source_scope(integration.branch_id),
            "idempotency_key": event.id, "response_body": deepcopy(expected)}, ["business_id", "scope", "idempotency_key"])
        mapping = db.scalar(mapping_query)
    if not mapping or mapping.response_body != expected:
        raise HTTPException(409, "Notice source mapping conflict")
    if row.status == "sent":
        # A later alias for an already confirmed operation earns only its ACK.
        acknowledge_notice_sources(db, row)
    db.commit()
    return {"status": row.status, "delivery_id": row.id, "message_id": row.message_id,
        "event_id": event.id, "event_acknowledged": event.acknowledged_at is not None}


def replacement_scope(branch_id):
    return f"pos-notice-replacement:{branch_id}"


def retry_error(detail, code="notice_replacement_conflict", status=409):
    raise CodedHTTPException(status, detail, code)


def replacement_fingerprint(event_id, delivery_id, payload):
    # Store only this digest. Neither a raw delivery token nor a copy of the
    # source checkpoint is persisted in the replacement audit.
    return fingerprint({"event_id": event_id, "delivery_id": delivery_id,
        "event_fingerprint": payload.expected_event_fingerprint,
        "claim_sha256": hashlib.sha256(payload.delivery_token.encode()).hexdigest(),
        "retirement": payload.retirement.model_dump()})


def replacement_replay(db, business_id, branch_id, delivery_id, digest):
    row = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.business_id == business_id,
        IdempotencyRecord.scope == replacement_scope(branch_id), IdempotencyRecord.idempotency_key == delivery_id))
    if row is None:
        return None
    if row.response_body.get("request_fingerprint") != digest:
        retry_error("Notice replacement identity already has different retirement data")
    return deepcopy(row.response_body["result"])


def replace_retired_notice(db, integration, event_id, delivery_id, payload):
    """One SQL transaction, including token release, replacement and alias audit.

    The caller is a dedicated internal events:deliver transport. It must already
    have retired the actual never-claimed v1 queue with a complete CAS/readback.
    Restaurant agents cannot use the attestation boundary, even with a token.
    """
    if integration.profile != "generic" or not integration.credential_id or not integration.business_id or not integration.branch_id:
        retry_error("A dedicated internal notice credential is required", "notice_replacement_internal_only", 403)
    digest = replacement_fingerprint(event_id, delivery_id, payload)
    replay = replacement_replay(db, integration.business_id, integration.branch_id, delivery_id, digest)
    if replay is not None:
        return replay
    query = select(AgentMessageDelivery).where(AgentMessageDelivery.id == delivery_id,
        AgentMessageDelivery.business_id == integration.business_id, AgentMessageDelivery.branch_id == integration.branch_id)
    discovered = db.scalar(query)
    notice = (discovered.response or {}).get("_pos_notice") if discovered is not None else None
    if not isinstance(notice, dict) or notice.get("version") != 1:
        retry_error("Own POS notice delivery not found", "notice_replacement_not_found", 404)
    # Keep the existing conversation -> order -> delivery -> event lock order.
    conversation = db.scalar(select(AgentConversation).where(AgentConversation.id == discovered.conversation_id,
        AgentConversation.business_id == integration.business_id, AgentConversation.branch_id == integration.branch_id).with_for_update())
    order = db.scalar(select(Order).where(Order.id == notice.get("order_id"), Order.business_id == integration.business_id,
        Order.branch_id == integration.branch_id).with_for_update(read=True))
    row = db.scalar(query.with_for_update().execution_options(populate_existing=True))
    replay = replacement_replay(db, integration.business_id, integration.branch_id, delivery_id, digest)
    if replay is not None:
        return replay
    if not conversation or not order or not row:
        retry_error("Notice context changed before replacement")
    validate_delivery_claim(row, payload.delivery_token)
    # POS uncertain means a reserved response, not a transport UNKNOWN. The
    # latter is rejected by the fixed retirement adapter before this route.
    if row.status != "uncertain" or row.receipt not in (None, {}):
        retry_error("Only an unreconciled POS reservation with no receipt can be replaced")
    proof = payload.retirement
    if (proof.business_id != integration.business_id or proof.branch_id != integration.branch_id
            or proof.delivery_id != row.id or proof.message_id != row.message_id or proof.sender != row.sender
            or proof.mailbox_head_before != 0 or proof.mailbox_head_after != 1):
        retry_error("Retirement does not match the own reserved notice", "notice_replacement_retirement_mismatch")
    now_ms = int(utcnow().timestamp() * 1000)
    if now_ms < proof.source_deadline_at_ms or now_ms < proof.mailbox_deadline_at_ms:
        retry_error("Original source and mailbox windows must both have expired", "notice_replacement_not_expired")
    sources = source_rows(db, row)
    if not sources or len(sources) > 200:
        retry_error("Notice source mappings require review")
    # Lock every alias and revalidate the unchanged business source, rather than
    # creating another event or accepting a caller-generated replacement text.
    snapshots = []
    selected_event = None
    for mapping in sources:
        event = db.scalar(select(IntegrationEvent).where(IntegrationEvent.id == mapping.idempotency_key,
            IntegrationEvent.business_id == row.business_id, IntegrationEvent.branch_id == row.branch_id).with_for_update())
        created = event.created_at if event is not None else None
        if created is not None and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_ms = now_ms - int(created.timestamp() * 1000) if created is not None else -1
        if (not event or event.acknowledged_at is not None or event.event_type not in NOTICE_TYPES
                or event.aggregate_type != "order" or event.aggregate_id != str(order.id)
                or event_fingerprint(event) != mapping.response_body.get("event_fingerprint")
                or not 0 <= age_ms <= 86400000 or disposition_record(db, row.business_id, row.branch_id, event.id)):
            retry_error("Notice source is no longer eligible for replacement", "notice_replacement_source_changed")
        snapshots.append({"idempotency_key": mapping.idempotency_key, "scope": mapping.scope,
            "response_body": deepcopy(mapping.response_body)})
        if event.id == event_id:
            selected_event = event
    if (selected_event is None or event_fingerprint(selected_event) != payload.expected_event_fingerprint
            or not notice_is_current(db, row)):
        retry_error("Current event, order or recipient changed", "notice_replacement_source_changed")
    original_response = deepcopy(row.response)
    original_claim_hash = row.claim_hash
    # This conditional reservation transition is also a database fence where
    # SELECT FOR UPDATE is unavailable. A loser cannot insert another child.
    changed = db.execute(update(AgentMessageDelivery).where(AgentMessageDelivery.id == row.id,
        AgentMessageDelivery.business_id == row.business_id, AgentMessageDelivery.branch_id == row.branch_id,
        AgentMessageDelivery.status == "uncertain", AgentMessageDelivery.claim_hash == original_claim_hash)
        .values(status="suppressed", claim_hash=None).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        retry_error("Notice reservation changed before atomic replacement")
    new_id = str(uuid4())
    message_id = "pos-notice-retry:" + fingerprint([row.business_id, row.branch_id, row.id])
    response = deepcopy(original_response)
    response["turn"] = {**response["turn"], "id": message_id}
    new_row = AgentMessageDelivery(id=new_id, business_id=row.business_id, branch_id=row.branch_id,
        conversation_id=row.conversation_id, message_id=message_id, sender=row.sender, response=response, status="pending")
    db.add(new_row)
    db.flush()
    receipt = {"transport_attempted": False, "reason": "expired_before_transport",
        "replacement_delivery_id": new_id, "retirement_body_hash": proof.mailbox_body_hash}
    validate_delivery_ack_receipt("not_sent", receipt)
    row.status, row.claim_hash, row.receipt = "suppressed", None, receipt
    for mapping in sources:
        mapping.response_body = {**mapping.response_body, "delivery_id": new_id}
    result = {"status": "pending", "old_delivery_id": row.id, "delivery_id": new_id, "message_id": message_id,
        "event_id": event_id, "event_acknowledged": False}
    db.add(IdempotencyRecord(business_id=row.business_id, scope=replacement_scope(row.branch_id), idempotency_key=row.id,
        response_body={"request_fingerprint": digest, "result": deepcopy(result), "audit": {
            "issuer_credential_id": integration.credential_id, "kind": "never_transport_replacement",
            "retirement": proof.model_dump(), "old_status": "uncertain", "new_old_status": "suppressed",
            "old_message_id": row.message_id, "old_response_fingerprint": fingerprint(original_response),
            "old_source_mappings": snapshots, "receipt": deepcopy(receipt)}}))
    db.commit()
    return result


@router.post("/{event_id}/outbox/{delivery_id}/replace-never-transported", response_model=NoticeReplacementResult)
def replace_expired_notice(event_id: str, delivery_id: str, payload: ReplaceRetiredNotice,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("events:deliver")), db=Depends(get_db)):
    if idempotency_key != "notice-replacement:" + delivery_id:
        retry_error("Use the stable replacement key for this delivery", "notice_replacement_key_required", 422)
    try:
        return replace_retired_notice(db, integration, event_id, delivery_id, payload)
    except (IntegrityError, OperationalError):
        db.rollback()
        retry_error("Notice replacement contention; reconcile the same operation key before retrying")


def without_queued_notices(statement, branch):
    from sqlalchemy import exists
    # The durable source mapping is inserted atomically with its outbox entry.
    # Uncertain or suppressed sends remain held, not re-enqueued on each poll.
    statement = statement.where(~exists(select(IdempotencyRecord.id).where(
        IdempotencyRecord.business_id == branch.business_id,
        IdempotencyRecord.scope == disposition_scope(branch.id),
        IdempotencyRecord.idempotency_key == IntegrationEvent.id)))
    return statement.where(~exists(select(IdempotencyRecord.id).where(
        IdempotencyRecord.business_id == branch.business_id,
        IdempotencyRecord.scope == source_scope(branch.id),
        IdempotencyRecord.idempotency_key == IntegrationEvent.id)))


# A technical hold is distinct from both a transport ACK and a new outbox item.
# The trusted n8n policy selects skip/review; the API binds that choice to current
# own source/order rows and makes it durable. No conversational rules run here.
REVIEW_REASONS = {
    "direct_recipient_required": "no tiene un WhatsApp directo válido",
    "kitchen_release_or_original_approval_required": "la aprobación no acredita el envío de la comanda a cocina",
    "committed_cash_addition_required": "los productos agregados no acreditan una comanda confirmada",
    "projection_requires_review": "los datos actuales no permiten preparar un aviso seguro",
    "unsupported_or_historical_event": "el evento no corresponde a un aviso vigente",
}


class NoticeDisposition(ApiModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    order_id: int = Field(gt=0)
    expected_order_version: int = Field(ge=1)
    event_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    kind: Literal["skip", "review"]
    reason: Literal["superseded_or_not_current", "direct_recipient_required",
        "kitchen_release_or_original_approval_required", "committed_cash_addition_required",
        "projection_requires_review", "unsupported_or_historical_event"]

    @model_validator(mode="after")
    def reason_matches_kind(self):
        if (self.kind == "skip") != (self.reason == "superseded_or_not_current"):
            raise ValueError("Disposition kind does not match its reason")
        return self


def disposition_scope(branch_id):
    return f"pos-notice-disposition:{branch_id}"


def disposition_record(db, business_id, branch_id, event_id):
    return db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.business_id == business_id,
        IdempotencyRecord.scope == disposition_scope(branch_id), IdempotencyRecord.idempotency_key == event_id))


@router.post("/{event_id}/disposition")
async def record_notice_disposition(event_id: str, payload: NoticeDisposition,
    integration: IntegrationAuthContext = Depends(require_integration_scope("events:deliver")), db=Depends(get_db)):
    from .agent_blocking_io import run_private_transaction
    result, attention_id, order_id = await run_private_transaction(
        _record_notice_disposition_transaction, event_id, payload, integration=integration, db=db)
    if attention_id:
        from .realtime import hub
        await hub.broadcast(integration.branch_id, "human.requested", {"attention_id": attention_id, "order_id": order_id})
    return result


async def _record_notice_disposition_transaction(event_id, payload, *, integration, db):
    if integration.profile != "generic" or not integration.credential_id or not integration.business_id or not integration.branch_id:
        raise HTTPException(403, "A dedicated internal notice credential is required")
    event_query = select(IntegrationEvent).where(IntegrationEvent.id == event_id,
        IntegrationEvent.business_id == integration.business_id, IntegrationEvent.branch_id == integration.branch_id)
    event = db.scalar(event_query)
    if not event or event.event_type not in NOTICE_TYPES or event.aggregate_type != "order":
        raise HTTPException(404, "Notice source not found")
    if (str(payload.order_id) != event.aggregate_id
            or (event.payload or {}).get("order_id", payload.order_id) != payload.order_id):
        raise HTTPException(409, "Notice source identity changed")
    # Match the existing queue order: order before source. An ACK holds delivery
    # then source and never an order; this introduces no opposite lock order.
    order = db.scalar(select(Order).where(Order.id == payload.order_id,
        Order.business_id == integration.business_id, Order.branch_id == integration.branch_id)
        .with_for_update().execution_options(populate_existing=True))
    event = db.scalar(event_query.with_for_update().execution_options(populate_existing=True))
    if not order or order.version != payload.expected_order_version or not event or event_fingerprint(event) != payload.event_fingerprint:
        raise HTTPException(409, "Notice source or order changed before disposition")
    if event.acknowledged_at is not None:
        raise HTTPException(409, "An acknowledged source cannot be reclassified")
    digest = fingerprint(payload.model_dump())
    old = disposition_record(db, integration.business_id, integration.branch_id, event_id)
    if old:
        if old.response_body.get("digest") != digest:
            raise HTTPException(409, "Notice source already has a different disposition")
        return deepcopy(old.response_body["result"]), None, order.id
    mapped = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.business_id == integration.business_id,
        IdempotencyRecord.scope == source_scope(integration.branch_id), IdempotencyRecord.idempotency_key == event_id))
    if mapped or event.acknowledged_at is not None:
        raise HTTPException(409, "A queued or acknowledged source cannot be reclassified")
    attention_id = None
    if payload.kind == "review":
        from .agent_attention import create_attention
        reference = f" #{order.folio}" if order.folio else ""
        row = create_attention(db, business_id=integration.business_id, branch_id=integration.branch_id,
            order_id=order.id, sender=recipient(order.whatsapp_chat_id or "") or recipient(order.customer_phone or "") or "",
            kind="notification_review", summary=f"El aviso del pedido{reference} requiere revisión: {REVIEW_REASONS[payload.reason]}. No se envió un mensaje.",
            source_key="pos-notice-review:" + event.id + ":" + payload.event_fingerprint,
            details={"_source_event_id": event.id, "_reason": payload.reason})
        attention_id = row.id
    result = {"status": "held_for_review" if payload.kind == "review" else "skipped",
        "event_id": event.id, "event_acknowledged": False, "attention_id": attention_id}
    insert_once(db, IdempotencyRecord, {"business_id": integration.business_id,
        "scope": disposition_scope(integration.branch_id), "idempotency_key": event.id,
        "response_body": {"digest": digest, "result": result}}, ["business_id", "scope", "idempotency_key"])
    stored = disposition_record(db, integration.business_id, integration.branch_id, event.id)
    if not stored or stored.response_body != {"digest": digest, "result": result}:
        raise HTTPException(409, "Notice disposition contention")
    db.commit()
    return result, attention_id, order.id


def request_notice_delivery_review(db, delivery, *, background_tasks=None):
    """Staff visibility only; never authorize another send or acknowledge a source.

    The caller holds the delivery row lock. A transient claim is not a failure:
    callers invoke this only for an explicit uncertain ACK or suppression.
    """
    from .agent_attention import create_attention
    from .models import AgentAttentionRequest
    notice = (delivery.response or {}).get("_pos_notice")
    if (delivery.status not in {"uncertain", "suppressed"} or not isinstance(notice, dict)
            or notice.get("version") != 1 or type(notice.get("order_id")) is not int):
        return None
    event = db.scalar(select(IntegrationEvent).where(IntegrationEvent.id == notice.get("event_id"),
        IntegrationEvent.business_id == delivery.business_id, IntegrationEvent.branch_id == delivery.branch_id))
    mapping = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.business_id == delivery.business_id,
        IdempotencyRecord.scope == source_scope(delivery.branch_id),
        IdempotencyRecord.idempotency_key == notice.get("event_id")))
    if (not event or event.event_type not in NOTICE_TYPES or event.aggregate_type != "order"
            or event.aggregate_id != str(notice["order_id"]) or not mapping
            or mapping.response_body != {"delivery_id": delivery.id, "event_fingerprint": notice.get("event_fingerprint")}):
        return None
    # Read current contact details without introducing delivery-to-order locks.
    # A changed source/version is precisely why a suppressed notice needs review.
    order = db.scalar(select(Order).where(Order.id == notice["order_id"],
        Order.business_id == delivery.business_id, Order.branch_id == delivery.branch_id))
    if not order:
        return None
    key = "pos-notice-delivery-review:" + delivery.id
    digest = "sha256:" + hashlib.sha256(key.encode()).hexdigest()
    existing = db.scalar(select(AgentAttentionRequest).where(
        AgentAttentionRequest.business_id == delivery.business_id, AgentAttentionRequest.branch_id == delivery.branch_id,
        AgentAttentionRequest.source_key == digest))
    if existing:
        return existing.id  # A repeated report must not reopen a resolved issue.
    reason = ("no se pudo confirmar su entrega por WhatsApp. Comprueba la conversación antes de volver a contactar al cliente."
        if delivery.status == "uncertain" else
        "no se envió porque cambió la información del pedido o del destinatario. Revisa el pedido antes de contactar al cliente.")
    row = create_attention(db, business_id=delivery.business_id, branch_id=delivery.branch_id,
        sender=recipient(order.whatsapp_chat_id or "") or recipient(order.customer_phone or "") or "",
        kind="notification_review", order_id=order.id, source_key=key,
        summary=f"El aviso del pedido #{order.folio or order.id} requiere revisión: {reason}",
        details={"_delivery_id": delivery.id, "_source_event_id": event.id, "_delivery_status": delivery.status})
    if background_tasks is not None:
        from .realtime import hub
        # FastAPI runs this only after the successful response, hence after the
        # caller's commit. No session or mutable ORM row is passed to the task.
        background_tasks.add_task(hub.broadcast, delivery.branch_id, "human.requested",
            {"attention_id": row.id, "order_id": order.id})
    return row.id
