"""Fenced conversation processing and an allowlist for restaurant credentials.

The model never receives a credential, lease, arbitrary HTTP tool or database.
Leases are short transactions, not transactions held during a model call.
"""
from copy import deepcopy
from datetime import timedelta, timezone
import hashlib
import json
import re
import secrets
import unicodedata

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import ConfigDict, Field, ValidationError
from sqlalchemy import select, or_, exists

from .auth import IntegrationAuthContext, require_integration_scope
from .database import get_db
from .models import AgentConversation, AgentMessageDelivery, Branch, IdempotencyRecord, Order, utcnow
from .schemas import ApiModel, OrderCreate
from .agent_checkout import assert_sender, fingerprint
from .agent_blocking_io import private_transaction

router = APIRouter(prefix="/api/v1/integrations/conversations", tags=["agent-conversations"])
LEASE_SECONDS = 120
MAX_STATE_BYTES = 131072
AGENT_ROUTE_ALLOWLIST = frozenset({
    ("GET", "/integrations/context"), ("GET", "/integrations/context/catalog"),
    ("GET", "/integrations/context/yape-qr"), ("GET", "/integrations/context/menu-card"),
    ("GET", "/integrations/context/menu-cards/{image_id}"),
    ("POST", "/integrations/orders/preview"), ("POST", "/integrations/orders/draft"),
    ("POST", "/integrations/orders/{order_id}/cash-confirm"),
    ("POST", "/integrations/orders/{order_id}/payment-evidence"),
    ("POST", "/integrations/orders/{order_id}/payment-evidence/reconcile"),
    ("GET", "/integrations/orders/{order_id}/customer-state"),
    ("POST", "/integrations/orders/{order_id}/item-batches"),
    ("POST", "/integrations/orders/{order_id}/fulfillment-preview"),
    ("PATCH", "/integrations/orders/{order_id}/fulfillment"),
    ("PATCH", "/integrations/orders/{order_id}/delivery-payment"),
    ("POST", "/integrations/attention-requests"),
    ("POST", "/integrations/conversations/claim"),
    ("POST", "/integrations/conversations/enqueue"),
    ("POST", "/integrations/conversations/media"),
    ("POST", "/integrations/conversations/{conversation_id}/media/read"),
    ("POST", "/integrations/conversations/work/claim"),
    ("POST", "/integrations/conversations/outbox/claim"),
    ("PATCH", "/integrations/conversations/outbox/{delivery_id}/ack"),
    ("POST", "/integrations/conversations/{conversation_id}/commit"),
    ("POST", "/integrations/conversations/{conversation_id}/renew"),
    ("POST", "/integrations/conversations/{conversation_id}/next"),
    ("POST", "/integrations/conversations/{conversation_id}/model-usage/reserve"),
    ("POST", "/integrations/conversations/{conversation_id}/model-usage/{reservation_id}/settle"),
})


def normalize_sender(value):
    sender = str(value or "").strip().lower().replace("@s.whatsapp.net", "@c.us")
    if re.fullmatch(r"\+?\d{6,20}(?:@c\.us|@lid)?", sender):
        return sender.lstrip("+").removesuffix("@c.us")
    raise HTTPException(422, "A verified individual WhatsApp sender is required")


def contact_display_name(value):
    """Provider display data, never a customer instruction or an identity key."""
    if not isinstance(value, str):
        return None
    value = unicodedata.normalize("NFC", value)
    value = re.sub(r"[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]", "", value)
    value = " ".join(value.split())
    return value if value and len(value) <= 120 else None


def as_utc(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def lease_valid(row, token):
    return bool(token and row.lease_hash and row.lease_until and as_utc(row.lease_until) > utcnow()
        and secrets.compare_digest(row.lease_hash, hashlib.sha256(token.encode()).hexdigest()))


def authorize_agent_request(db, request: Request, credential):
    route = getattr(request.scope.get("route"), "path", "").removeprefix("/api/v1")
    if (request.method, route) not in AGENT_ROUTE_ALLOWLIST:
        raise HTTPException(403, "This operation is not available to restaurant agents")
    if route.startswith("/integrations/context") or route.startswith("/integrations/conversations/"):
        return None, None
    conversation_id = request.headers.get("X-Agent-Conversation")
    token = request.headers.get("X-Agent-Lease")
    row = db.scalar(select(AgentConversation).where(
        AgentConversation.id == conversation_id, AgentConversation.business_id == credential.business_id,
        AgentConversation.branch_id == credential.branch_id).with_for_update())
    if not row or not lease_valid(row, token):
        raise HTTPException(409, "A current conversation lease is required")
    order_id = request.path_params.get("order_id")
    if order_id is not None:
        order = db.scalar(select(Order).where(Order.id == int(order_id),
            Order.business_id == credential.business_id, Order.branch_id == credential.branch_id))
        if not order:
            raise HTTPException(404, "Order not found")
        assert_sender(order, row.sender)
    request.state.agent_sender = row.sender
    request.state.agent_conversation_id = row.id
    request.state.agent_verified_phone = (row.state or {}).get("_transport", {}).get("verified_phone")
    # The current message owns its metadata snapshot. Enqueueing a newer profile
    # must not change the body or authorization of an operation being retried.
    active = (row.state or {}).get("_processing_message", {})
    request.state.agent_contact_display_name = active.get("transport", {}).get("contact_display_name")
    if not request.state.agent_verified_phone and not row.sender.endswith("@lid"):
        request.state.agent_verified_phone = normalize_sender(row.sender)
    return row.sender, row.id


class Claim(ApiModel):
    model_config = ConfigDict(extra="forbid")
    sender: str = Field(min_length=6, max_length=120)
    channel: str = Field(default="whatsapp", pattern="^whatsapp$")
    message_id: str = Field(min_length=1, max_length=240)
    message: dict


class Commit(ApiModel):
    model_config = ConfigDict(extra="forbid")
    lease_token: str = Field(min_length=20, max_length=150)
    expected_version: int = Field(ge=1)
    state: dict
    response: dict | None = None
    complete: bool = True
    media_disposition: dict | None = None


class Renew(ApiModel):
    model_config = ConfigDict(extra="forbid")
    lease_token: str = Field(min_length=20, max_length=150)
    expected_version: int = Field(ge=1)


class DeliveryAck(ApiModel):
    model_config = ConfigDict(extra="forbid")
    delivery_token: str = Field(min_length=20, max_length=150)
    status: str = Field(pattern="^(sent|not_sent|uncertain)$")
    receipt: dict = Field(default_factory=dict)


def require_key(value):
    if not value or len(value) > 240:
        raise HTTPException(422, "Idempotency-Key is required (up to 240 characters)")


def bounded(value, limit=MAX_STATE_BYTES):
    if len(json.dumps(value, ensure_ascii=False, default=str).encode()) > limit:
        raise HTTPException(413, "Conversation input exceeds its size limit")


def scoped_conversation(db, integration, conversation_id):
    row = db.scalar(select(AgentConversation).where(AgentConversation.id == conversation_id,
        AgentConversation.business_id == integration.business_id,
        AgentConversation.branch_id == integration.branch_id).with_for_update())
    if not row:
        raise HTTPException(404, "Conversation not found")
    return row


def lock_or_create_conversation(db, integration, channel, sender):
    """Existing chats lock Conversation first; creation never reverses that order."""
    query = select(AgentConversation).where(AgentConversation.business_id == integration.business_id,
        AgentConversation.branch_id == integration.branch_id, AgentConversation.channel == channel,
        AgentConversation.sender == sender)
    row = db.scalar(query.with_for_update())
    if row is None:
        # Another ingress may create the row after our first SELECT. While holding
        # Branch, observe it without waiting for its Conversation lock: a domain
        # writer may already own that lock and need Branch/FK access. Release the
        # creation mutex before acquiring the shared conversation lock.
        db.scalar(select(Branch.id).where(Branch.id == integration.branch_id,
            Branch.business_id == integration.business_id).with_for_update())
        row = db.scalar(query)
        if row is None:
            row = AgentConversation(business_id=integration.business_id, branch_id=integration.branch_id,
                channel=channel, sender=sender, state={}, pending=[], version=1)
            db.add(row)
            db.flush()
        db.commit()
        # The non-locking creation lookup can have populated the identity map.
        # Reload after waiting for a concurrent writer, including when sessions
        # keep objects after commit, so new ingress never restores stale state.
        row = db.scalar(query.with_for_update().execution_options(populate_existing=True))
    return row


def input_record(db, row, message_id):
    return db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.business_id == row.business_id,
        IdempotencyRecord.scope == f"agent-input:{row.id}", IdempotencyRecord.idempotency_key == message_id))


def confirmed_response_context(db, row, current_message_id):
    """Only public text acknowledged before this input, never client-supplied proof.

    `sent` is the transport's ACK, not a WhatsApp read receipt. A delayed ACK
    cannot retroactively make an earlier consumer message a response to it.
    """
    saved = (row.state or {}).get("assistant_context")
    if not isinstance(saved, dict) or saved.get("conversation_id") != row.id:
        return None
    message_id = saved.get("message_id")
    if not isinstance(message_id, str) or not 0 < len(message_id) <= 160:
        return None
    received = input_record(db, row, current_message_id)
    if not received:
        return None
    delivery = db.scalar(select(AgentMessageDelivery).where(
        AgentMessageDelivery.business_id == row.business_id,
        AgentMessageDelivery.branch_id == row.branch_id,
        AgentMessageDelivery.conversation_id == row.id,
        AgentMessageDelivery.sender == row.sender,
        AgentMessageDelivery.message_id == message_id,
        AgentMessageDelivery.status == "sent"))
    if not delivery or as_utc(delivery.updated_at) > as_utc(received.created_at):
        return None
    texts, size = [], 0
    response = delivery.response if isinstance(delivery.response, dict) else {}
    actions = response.get("actions")
    if not isinstance(actions, list):
        return None
    for action in actions[:12]:
        if not isinstance(action, dict):
            continue
        action_type = action.get("type")
        if action_type == "text":
            text = action.get("text")
        elif action_type in {"pos_menu", "private_yape_qr", "private_yape_instructions", "media_binary"}:
            text = action.get("caption")
        else:
            continue
        if not isinstance(text, str) or not text or len(text) > 6000:
            continue
        size += len(text)
        if size > 12000:
            return None
        texts.append(text)
    return {"status": "sent", "confirmation": "transport_ack", "conversation_id": row.id,
            "message_id": message_id, "texts": texts} if texts else None


def acquire_pending(db, row):
    if row.lease_hash and row.lease_until and as_utc(row.lease_until) > utcnow():
        db.commit()
        return {"status": "queued", "conversation_id": row.id, "version": row.version,
                "pending_count": len(row.pending or [])}
    if row.current_message_id:
        active = (row.state or {}).get("_processing_message")
        if not active:
            raise HTTPException(409, "Expired processing state requires reconciliation")
    elif row.pending:
        active = row.pending[0]
        row.pending = row.pending[1:]
        row.current_message_id = active["message_id"]
        row.state = {**(row.state or {}), "_processing_message": active}
    else:
        return {"status": "idle", "conversation_id": row.id, "version": row.version, "pending_count": 0}
    token = secrets.token_urlsafe(32)
    row.lease_hash = hashlib.sha256(token.encode()).hexdigest()
    row.lease_until = utcnow() + timedelta(seconds=LEASE_SECONDS)
    row.version += 1
    db.commit()
    confirmed_context = confirmed_response_context(db, row, active["message_id"])
    return {"status": "acquired", "conversation_id": row.id, "version": row.version,
        "lease_token": token, "lease_until": row.lease_until,
        "state": {k: v for k, v in (row.state or {}).items() if not k.startswith("_processing_") and k not in {"_transport", "_operation_identity"}},
        "message": {k: v for k, v in active["message"].items() if k not in {"verified_phone", "contact_display_name"}},
        "message_id": active["message_id"], "sender": row.sender,
        "verified_phone": (row.state or {}).get("_transport", {}).get("verified_phone"),
        "contact_display_name": active.get("transport", {}).get("contact_display_name"),
        **({"confirmed_response_context": confirmed_context} if confirmed_context else {}),
        "pending_count": len(row.pending or [])}


@router.post("/claim")
@router.post("/enqueue")
def claim(payload: Claim, response: Response, request: Request,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("agent:state")), db=Depends(get_db)):
    require_key(idempotency_key)
    if integration.legacy or not integration.branch_id:
        raise HTTPException(403, "Scoped integration credentials are required")
    sender = normalize_sender(payload.sender)
    supplied_phone = payload.message.get("verified_phone")
    if supplied_phone is not None:
        supplied_phone = normalize_sender(supplied_phone)
        if supplied_phone.endswith("@lid") or (not sender.endswith("@lid") and supplied_phone != sender):
            raise HTTPException(422, "Verified phone must match the transport sender identity")
    verified_phone = supplied_phone or (sender if not sender.endswith("@lid") else None)
    bounded(payload.message, 32768)
    if len(str(payload.message.get("text", ""))) > 8000:
        raise HTTPException(413, "Message is too long")
    row = lock_or_create_conversation(db, integration, payload.channel, sender)
    transport = (row.state or {}).get("_transport", {})
    if transport.get("verified_phone") and verified_phone and transport["verified_phone"] != verified_phone:
        raise HTTPException(409, "Verified transport identity changed; reconcile it before processing")
    # A technical redelivery can carry a newly refreshed profile. It is still the
    # same message, and must retain its original protected identity snapshot.
    digest = fingerprint({"message": {k: v for k, v in payload.message.items() if k != "contact_display_name"},
        "sender": sender, "channel": payload.channel})
    previous = input_record(db, row, payload.message_id)
    if previous and previous.response_body.get("digest") != digest:
        raise HTTPException(409, "Message identifier was reused with different content")
    response.headers["Cache-Control"] = "no-store"
    if previous and previous.response_body.get("status") == "completed":
        return {"status": "completed", "conversation_id": row.id, "version": row.version,
                "response": previous.response_body.get("response"), "duplicate": True}
    if integration.profile == "restaurant_agent":
        from .agent_input_media import validate_ingress_media
        validate_ingress_media(db, row, payload.message_id, payload.message)
    if not previous:
        if len(row.pending or []) >= 30:
            raise HTTPException(429, "Conversation queue is full; retry later", headers={"Retry-After": "30"})
        from .config import get_settings
        limits = get_settings()
        now = utcnow()
        recent = db.scalars(select(IdempotencyRecord.created_at).where(
            IdempotencyRecord.business_id == row.business_id,
            IdempotencyRecord.scope == f"agent-input:{row.id}",
            IdempotencyRecord.created_at >= now - timedelta(hours=1))).all()
        if len(recent) >= limits.agent_messages_per_hour:
            raise HTTPException(429, "Conversation hourly limit reached; retry later", headers={"Retry-After": "3600"})
        if sum(as_utc(value) >= now - timedelta(seconds=60) for value in recent) >= limits.agent_messages_per_minute:
            raise HTTPException(429, "Conversation rate limit reached", headers={"Retry-After": "30"})
        transport = deepcopy(transport)
        if verified_phone and not transport.get("verified_phone"):
            transport["verified_phone"] = verified_phone
        if "contact_display_name" in payload.message:
            transport["contact_display_name"] = contact_display_name(payload.message["contact_display_name"])
        row.state = {**(row.state or {}), "_transport": transport}
        message = {k: deepcopy(v) for k, v in payload.message.items() if k != "contact_display_name"}
        db.add(IdempotencyRecord(business_id=row.business_id, scope=f"agent-input:{row.id}",
            idempotency_key=payload.message_id, response_body={"digest": digest, "status": "accepted"}))
        row.pending = [*(row.pending or []), {"message_id": payload.message_id, "message": message,
            "transport": {"contact_display_name": transport.get("contact_display_name")}}]
    if request.url.path.endswith("/enqueue"):
        db.commit()
        return {"status": "queued", "conversation_id": row.id, "pending_count": len(row.pending or [])}
    return acquire_pending(db, row)


@router.post("/work/claim")
def claim_work(idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("agent:state")), db=Depends(get_db)):
    require_key(idempotency_key)
    # Accepted inputs are durable records, so workers can recover after restarts
    # without holding a list of chat IDs in memory.
    accepted = exists(select(IdempotencyRecord.id).where(
        IdempotencyRecord.business_id == AgentConversation.business_id,
        IdempotencyRecord.scope == "agent-input:" + AgentConversation.id,
        IdempotencyRecord.response_body["status"].as_string() == "accepted"))
    row = db.scalar(select(AgentConversation).where(
        AgentConversation.business_id == integration.business_id,
        AgentConversation.branch_id == integration.branch_id, accepted,
        or_(AgentConversation.lease_until.is_(None), AgentConversation.lease_until <= utcnow()))
        .order_by(AgentConversation.updated_at, AgentConversation.id).with_for_update(skip_locked=True).limit(1))
    return acquire_pending(db, row) if row else {"status": "idle"}


@router.post("/outbox/claim")
def claim_delivery(idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("agent:state")), db=Depends(get_db)):
    require_key(idempotency_key)
    scope = "agent-outbox-claim:" + str(integration.branch_id)
    previous = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.business_id == integration.business_id,
        IdempotencyRecord.scope == scope, IdempotencyRecord.idempotency_key == idempotency_key))
    if previous:
        # A lost claim response is uncertain. Never authorize a second dispatch.
        return {"status": "uncertain", "delivery_id": previous.response_body["delivery_id"]}
    row = db.scalar(select(AgentMessageDelivery).where(
        AgentMessageDelivery.business_id == integration.business_id,
        AgentMessageDelivery.branch_id == integration.branch_id, AgentMessageDelivery.status == "pending")
        .order_by(AgentMessageDelivery.created_at, AgentMessageDelivery.id).with_for_update(skip_locked=True).limit(1))
    if not row:
        return {"status": "idle"}
    token = secrets.token_urlsafe(32)
    row.status, row.claimed_at = "uncertain", utcnow()
    row.claim_hash = hashlib.sha256(token.encode()).hexdigest()
    db.add(IdempotencyRecord(business_id=row.business_id, scope=scope, idempotency_key=idempotency_key,
        response_body={"delivery_id": row.id}))
    db.commit()
    return {"status": "claimed", "delivery_id": row.id, "delivery_token": token,
        "conversation_id": row.conversation_id, "message_id": row.message_id,
        "sender": row.sender, "response": row.response}


@router.patch("/outbox/{delivery_id}/ack")
def ack_delivery(delivery_id: str, payload: DeliveryAck,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("agent:state")), db=Depends(get_db)):
    require_key(idempotency_key)
    bounded(payload.receipt, 4096)
    row = db.scalar(select(AgentMessageDelivery).where(AgentMessageDelivery.id == delivery_id,
        AgentMessageDelivery.business_id == integration.business_id,
        AgentMessageDelivery.branch_id == integration.branch_id).with_for_update())
    if not row:
        raise HTTPException(404, "Response not found")
    digest, scope = fingerprint(payload.model_dump()), f"agent-delivery-ack:{row.id}"
    previous = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.business_id == row.business_id,
        IdempotencyRecord.scope == scope, IdempotencyRecord.idempotency_key == idempotency_key))
    if previous:
        if previous.response_body["digest"] != digest:
            raise HTTPException(409, "Idempotency key already used with other data")
        return previous.response_body["result"]
    if not row.claim_hash or not secrets.compare_digest(row.claim_hash, hashlib.sha256(payload.delivery_token.encode()).hexdigest()):
        raise HTTPException(409, "Response claim is stale")
    if row.status == "sent":
        raise HTTPException(409, "Response already sent")
    if payload.status == "sent" and not str(payload.receipt.get("transport_id", "")).strip():
        raise HTTPException(422, "Confirmed transport receipt is required")
    if payload.status == "not_sent" and payload.receipt.get("transport_attempted") is not False:
        raise HTTPException(422, "Only a response never attempted by the transport may be released")
    row.status = "pending" if payload.status == "not_sent" else payload.status
    row.receipt = deepcopy(payload.receipt)
    if payload.status == "not_sent":
        row.claim_hash = None
    result = {"delivery_id": row.id, "status": row.status}
    db.add(IdempotencyRecord(business_id=row.business_id, scope=scope, idempotency_key=idempotency_key,
        response_body={"digest": digest, "result": result}))
    db.commit()
    return result


@router.post("/{conversation_id}/next")
def next_input(conversation_id: str,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("agent:state")), db=Depends(get_db)):
    require_key(idempotency_key)
    return acquire_pending(db, scoped_conversation(db, integration, conversation_id))


@router.post("/{conversation_id}/renew")
def renew(conversation_id: str, payload: Renew,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("agent:state")), db=Depends(get_db)):
    require_key(idempotency_key)
    row = scoped_conversation(db, integration, conversation_id)
    if not lease_valid(row, payload.lease_token) or row.version != payload.expected_version:
        raise HTTPException(409, "Conversation lease or version is stale")
    row.lease_until = utcnow() + timedelta(seconds=LEASE_SECONDS)
    db.commit()
    return {"version": row.version, "lease_until": row.lease_until}


@router.post("/{conversation_id}/commit")
@private_transaction
async def commit(conversation_id: str, payload: Commit,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("agent:state")), db=Depends(get_db)):
    require_key(idempotency_key)
    bounded(payload.state)
    if any(key in {"_transport", "_operation_identity"} or key.startswith("_processing_") for key in payload.state):
        raise HTTPException(422, "Transport and processing state cannot be set by the worker")
    bounded(payload.response, 32768)
    row = scoped_conversation(db, integration, conversation_id)
    commit_body = payload.model_dump()
    if payload.media_disposition is None:
        commit_body.pop("media_disposition", None)  # Preserve existing commit fingerprints.
    digest = fingerprint(commit_body)
    scope = f"agent-commit:{row.id}"
    old = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.business_id == row.business_id,
        IdempotencyRecord.scope == scope, IdempotencyRecord.idempotency_key == idempotency_key))
    if old:
        if old.response_body.get("digest") != digest:
            raise HTTPException(409, "Idempotency key already used with other data")
        return old.response_body["result"]
    if not lease_valid(row, payload.lease_token) or row.version != payload.expected_version:
        raise HTTPException(409, "Conversation lease or version is stale")
    if integration.profile == "restaurant_agent":
        from .agent_input_media import validate_state_media, record_media_disposition
        validate_state_media(db, row, payload.state)
        record_media_disposition(db, row, payload.media_disposition)
    processing = row.state.get("_processing_message")
    transport = deepcopy(row.state.get("_transport"))
    operation_identity = None
    pending = payload.state.get("pending")
    if isinstance(pending, dict) and pending.get("kind") == "draft":
        if pending.get("method") != "POST" or pending.get("path") != "/integrations/orders/draft":
            raise HTTPException(422, "Draft intent must use the scoped order route")
        operation_key = pending.get("idempotency_key")
        require_key(operation_key)
        try:
            body = OrderCreate.model_validate(pending.get("body")).model_dump()
        except ValidationError:
            raise HTTPException(422, "Pending draft body must be a valid structured order")
        body_digest = fingerprint(body)
        previous_identity = row.state.get("_operation_identity")
        if previous_identity and previous_identity.get("idempotency_key") == operation_key:
            if previous_identity["body_digest"] != body_digest or previous_identity["turn_id"] != pending.get("turn_id"):
                raise HTTPException(409, "Pending operation identity or body changed")
            operation_identity = deepcopy(previous_identity)
        else:
            if previous_identity:
                raise HTTPException(409, "Resolve the existing operation before replacing its identity")
            if pending.get("turn_id") != row.current_message_id:
                raise HTTPException(403, "A new operation must belong to the current verified message")
            profile = (processing or {}).get("transport", {}).get("contact_display_name")
            if body.get("customer_name") is not None and body["customer_name"] != profile:
                raise HTTPException(403, "Pending customer name must match the verified message profile")
            operation_identity = {"idempotency_key": operation_key, "turn_id": row.current_message_id,
                "body_digest": body_digest, "contact_display_name": profile}
    row.state = deepcopy(payload.state)
    if transport:
        row.state = {**row.state, "_transport": transport}
    if operation_identity:
        row.state = {**row.state, "_operation_identity": operation_identity}
    if payload.complete:
        record = input_record(db, row, row.current_message_id)
        record.response_body = {**record.response_body, "status": "completed", "response": deepcopy(payload.response)}
        if payload.response and payload.response.get("actions"):
            db.add(AgentMessageDelivery(business_id=row.business_id, branch_id=row.branch_id,
                conversation_id=row.id, message_id=row.current_message_id, sender=row.sender,
                response=deepcopy(payload.response)))
        row.current_message_id, row.lease_hash, row.lease_until = None, None, None
    else:
        row.state = {**row.state, "_processing_message": processing}
    row.version += 1
    result = {"conversation_id": row.id, "version": row.version, "pending_count": len(row.pending or []), "complete": payload.complete}
    db.add(IdempotencyRecord(business_id=row.business_id, scope=scope, idempotency_key=idempotency_key,
        response_body={"digest": digest, "result": result}))
    db.commit()
    if integration.profile == "restaurant_agent" and payload.complete:
        from .agent_input_media import retire_unreferenced_media
        try:
            await retire_unreferenced_media(db, integration, conversation_id)
        except Exception:
            # The response/operation already committed. Retain counted staging
            # and defer bounded cleanup; never turn a completed turn into a retry.
            db.rollback()
            import logging
            logging.getLogger(__name__).warning("Agent input media cleanup deferred")
    return result
