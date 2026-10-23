"""Durable dispatch budgets, without storing prompts, provider keys or phone text.

Lock order is Conversation -> Business, matching agent domain operations. The
Business row serializes reservations across chats and branches. A reservation
belongs permanently to the UTC day on which it was made; midnight never permits
replaying the same dispatch. Unknown usage stays charged at its reserved amount.
"""
from datetime import timedelta
import hashlib
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import ConfigDict, Field, model_validator
from sqlalchemy import func, or_, select

from .agent_attention import create_attention
from .agent_checkout import fingerprint
from .agent_runtime_api import as_utc, lease_valid, normalize_sender, require_key, scoped_conversation
from .auth import IntegrationAuthContext, require_integration_scope
from .config import get_settings
from .database import get_db
from .models import AgentAttentionRequest, AgentModelUsage, Business, utcnow
from .realtime import hub
from .schemas import ApiModel

router = APIRouter(prefix="/api/v1/integrations/conversations", tags=["agent-model-usage"])
MAX_TOKENS = 10**12


class UsageIdentity(ApiModel):
    model_config = ConfigDict(extra="forbid")
    lease_token: str = Field(min_length=20, max_length=150)
    expected_version: int = Field(ge=1, strict=True)
    message_id: str = Field(min_length=1, max_length=240)
    call_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,100}$")
    request_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class UsageReserve(UsageIdentity):
    model_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,120}$")
    role: Literal["interpreter", "composer", "image", "repair"]
    reserved_tokens: int = Field(ge=1, le=MAX_TOKENS, strict=True)


class UsageSettle(UsageIdentity):
    usage_known: bool = Field(strict=True)
    input_tokens: int | None = Field(default=None, ge=0, le=MAX_TOKENS, strict=True)
    output_tokens: int | None = Field(default=None, ge=0, le=MAX_TOKENS, strict=True)
    cache_creation_input_tokens: int = Field(default=0, ge=0, le=MAX_TOKENS, strict=True)
    cache_read_input_tokens: int = Field(default=0, ge=0, le=MAX_TOKENS, strict=True)

    @model_validator(mode="after")
    def validate_known_usage(self):
        if self.usage_known and (self.input_tokens is None or self.output_tokens is None):
            raise ValueError("Known usage requires input_tokens and output_tokens")
        if not self.usage_known and any((self.input_tokens, self.output_tokens,
                self.cache_creation_input_tokens, self.cache_read_input_tokens)):
            raise ValueError("Unknown usage cannot report positive token counts")
        return self


def usage_limits(business_id):
    settings = get_settings()
    result = {"conversation_tokens": settings.agent_usage_conversation_tokens,
        "sender_tokens": settings.agent_usage_sender_tokens,
        "business_tokens": settings.agent_usage_business_tokens,
        "alert_percent": settings.agent_usage_alert_percent}
    override = settings.agent_usage_business_overrides.get(str(business_id))
    if override:
        result.update(override.model_dump(exclude_none=True))
    return result


def _authorized(db, integration, conversation_id, payload):
    if integration.legacy or not integration.business_id or not integration.branch_id:
        raise HTTPException(403, "Scoped integration credentials are required")
    conversation = scoped_conversation(db, integration, conversation_id)
    if not lease_valid(conversation, payload.lease_token) or conversation.version != payload.expected_version:
        raise HTTPException(409, "Conversation lease or version is stale")
    if conversation.current_message_id != payload.message_id:
        raise HTTPException(409, "Usage must belong to the current processing message")
    # Do not reverse this order: other agent actions already lock Conversation
    # before entering the business domain. Hold both only during this transaction.
    business = db.scalar(select(Business).where(Business.id == integration.business_id).with_for_update())
    if not business:
        raise HTTPException(404, "Business not found")
    # Another conversation may hold the shared business budget lock until this
    # worker's lease expires. Holding our conversation row prevents replacement,
    # but does not extend its clock deadline; recheck after the blocking wait.
    if not lease_valid(conversation, payload.lease_token):
        raise HTTPException(409, "Conversation lease expired while waiting for the usage budget")
    return conversation


def _sender_hash(conversation):
    phone = (conversation.state or {}).get("_transport", {}).get("verified_phone")
    if not phone and not conversation.sender.endswith("@lid"):
        phone = conversation.sender
    if not phone:
        return None
    phone = normalize_sender(phone)
    if phone.endswith("@lid"):
        return None
    return hashlib.sha256(f"{conversation.business_id}:{phone}".encode()).hexdigest()


def _totals(db, row):
    base = [AgentModelUsage.business_id == row.business_id,
        AgentModelUsage.window_start == row.window_start]
    def total(*where):
        return int(db.scalar(select(func.coalesce(func.sum(AgentModelUsage.charged_tokens), 0)).where(*base, *where)))
    return {"conversation": total(AgentModelUsage.conversation_id == row.conversation_id),
        "sender": total(AgentModelUsage.sender_hash == row.sender_hash) if row.sender_hash else 0,
        "business": total()}


def _notice(db, row, conversation, scope, event, details):
    # Branch-local visibility, one notice per UTC day/scope/event. Budget limits
    # describe configured protection; they are not proof of consumer abuse.
    identity = row.conversation_id if scope == "conversation" else row.sender_hash if scope == "sender" else str(row.business_id)
    source = f"model-usage:{as_utc(row.window_start).date()}:{scope}:{identity}:{event}"
    digest = "sha256:" + hashlib.sha256(source.encode()).hexdigest()
    existing = db.scalar(select(AgentAttentionRequest).where(
        AgentAttentionRequest.business_id == row.business_id,
        AgentAttentionRequest.branch_id == row.branch_id,
        AgentAttentionRequest.source_key == digest))
    if existing:
        row.attention_recorded = True
        return None
    summaries = {
        "near_limit": "El agente se aproxima al presupuesto diario de uso configurado. Revisar el consumo; este aviso no acredita abuso del consumidor.",
        "budget_exceeded": "El agente alcanzó un presupuesto de uso y detuvo nuevas llamadas al modelo. La atención humana sigue disponible; no se registró un pedido por este aviso.",
        "identity_unavailable": "El canal no aportó un teléfono verificado para aplicar el presupuesto del agente. Revisar la conexión antes de continuar con llamadas al modelo.",
        "actual_over_reservation": "El proveedor informó más tokens que los reservados. Se conservó el consumo real completo; revisar la estimación y el presupuesto.",
    }
    notice = create_attention(db, business_id=row.business_id, branch_id=row.branch_id,
        sender=conversation.sender, kind="human_requested", summary=summaries[event],
        source_key=source, details={"source": "agent_model_usage", "event": event,
            "scope": scope, "window_start": as_utc(row.window_start).isoformat(),
            "window_end": as_utc(row.window_end).isoformat(), **details})
    row.attention_recorded = True
    return notice.id


def _threshold_alerts(db, row, conversation, limits):
    new = []
    for scope, amount in _totals(db, row).items():
        limit = limits[scope + "_tokens"]
        if amount * 100 >= limit * limits["alert_percent"]:
            notice = _notice(db, row, conversation, scope, "near_limit",
                {"charged_tokens": amount, "limit_tokens": limit, "alert_percent": limits["alert_percent"]})
            if notice:
                new.append(notice)
    return new


def _response(row, *, dispatch_allowed=False, reason=None):
    return {"protocol_version": 1, "reservation_id": row.id, "status": row.status,
        "dispatch_allowed": dispatch_allowed, "reason": reason,
        "window_ends_at": as_utc(row.window_end).isoformat().replace("+00:00", "Z"),
        "attention_recorded": row.attention_recorded}


async def _finish(db, response, row, notices, *, dispatch_allowed=False, reason=None):
    result = _response(row, dispatch_allowed=dispatch_allowed, reason=reason)
    db.commit()  # A response can allow dispatch only after durable reservation.
    response.headers["Cache-Control"] = "no-store"
    for notice_id in notices:
        await hub.broadcast(row.branch_id, "human.requested", {"attention_id": notice_id, "order_id": None})
    return result


@router.post("/{conversation_id}/model-usage/reserve")
async def reserve_usage(conversation_id: str, payload: UsageReserve, response: Response,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("agent:state")), db=Depends(get_db)):
    require_key(idempotency_key)
    conversation = _authorized(db, integration, conversation_id, payload)
    digest = fingerprint(payload.model_dump(exclude={"lease_token", "expected_version"}))
    previous = list(db.scalars(select(AgentModelUsage).where(
        AgentModelUsage.conversation_id == conversation.id,
        or_(AgentModelUsage.reserve_key == idempotency_key,
            (AgentModelUsage.message_id == payload.message_id) & (AgentModelUsage.call_id == payload.call_id)))))
    if previous:
        if len(previous) != 1 or previous[0].reserve_fingerprint != digest:
            raise HTTPException(409, "Usage reservation identity was reused with different data")
        old = previous[0]
        return await _finish(db, response, old, [], reason=old.denial_reason if old.status == "denied" else "reservation_replay")
    now = utcnow()
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    row = AgentModelUsage(business_id=conversation.business_id, branch_id=conversation.branch_id,
        conversation_id=conversation.id, message_id=payload.message_id, call_id=payload.call_id,
        sender_hash=_sender_hash(conversation), request_fingerprint=payload.request_fingerprint,
        reserve_fingerprint=digest, reserve_key=idempotency_key, model_id=payload.model_id, role=payload.role,
        window_start=day, window_end=day+timedelta(days=1), reserved_tokens=payload.reserved_tokens,
        charged_tokens=0, status="reserved", created_at=now, updated_at=now,
        attention_recorded=False)
    limits = usage_limits(row.business_id)
    totals = _totals(db, row)
    exceeded = [scope for scope, amount in totals.items() if amount + row.reserved_tokens > limits[scope + "_tokens"]]
    row.denial_reason = "identity_unavailable" if not row.sender_hash else "budget_exceeded" if exceeded else None
    row.status = "denied" if row.denial_reason else "reserved"
    row.charged_tokens = 0 if row.denial_reason else row.reserved_tokens
    db.add(row)
    db.flush()
    notices = []
    if row.denial_reason:
        for scope in exceeded if row.sender_hash else ["conversation"]:
            notice = _notice(db, row, conversation, scope, row.denial_reason,
                {"attempted_tokens": row.reserved_tokens})
            if notice:
                notices.append(notice)
    else:
        notices.extend(_threshold_alerts(db, row, conversation, limits))
    return await _finish(db, response, row, notices, dispatch_allowed=row.status == "reserved", reason=row.denial_reason)


@router.post("/{conversation_id}/model-usage/{reservation_id}/settle")
async def settle_usage(conversation_id: str, reservation_id: str, payload: UsageSettle, response: Response,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("agent:state")), db=Depends(get_db)):
    require_key(idempotency_key)
    conversation = _authorized(db, integration, conversation_id, payload)
    row = db.scalar(select(AgentModelUsage).where(AgentModelUsage.id == reservation_id,
        AgentModelUsage.conversation_id == conversation.id,
        AgentModelUsage.business_id == conversation.business_id,
        AgentModelUsage.branch_id == conversation.branch_id))
    if not row:
        raise HTTPException(404, "Usage reservation not found")
    if (row.message_id, row.call_id, row.request_fingerprint) != (payload.message_id, payload.call_id, payload.request_fingerprint):
        raise HTTPException(409, "Usage settlement must identify the reserved call")
    digest = fingerprint(payload.model_dump(exclude={"lease_token", "expected_version"}))
    other_key = db.scalar(select(AgentModelUsage.id).where(AgentModelUsage.conversation_id == conversation.id,
        AgentModelUsage.settle_key == idempotency_key, AgentModelUsage.id != row.id))
    if other_key:
        raise HTTPException(409, "Settlement idempotency key already identifies another call")
    if row.settlement_fingerprint:
        if row.settlement_fingerprint != digest:
            raise HTTPException(409, "Usage was already settled with different data")
        return await _finish(db, response, row, [], reason="usage_uncertain" if row.status == "uncertain" else None)
    if row.status != "reserved":
        raise HTTPException(409, "Only a dispatched reservation may be settled")
    row.usage_known = payload.usage_known
    row.input_tokens = payload.input_tokens or 0
    row.output_tokens = payload.output_tokens or 0
    row.cache_creation_input_tokens = payload.cache_creation_input_tokens
    row.cache_read_input_tokens = payload.cache_read_input_tokens
    row.status = "settled" if payload.usage_known else "uncertain"
    if payload.usage_known:
        row.charged_tokens = row.input_tokens + row.output_tokens + row.cache_creation_input_tokens + row.cache_read_input_tokens
    row.settlement_fingerprint = digest
    row.settle_key = idempotency_key
    row.settled_at = utcnow()
    db.flush()
    notices = []
    if row.charged_tokens > row.reserved_tokens:
        notice = _notice(db, row, conversation, "conversation", "actual_over_reservation",
            {"reserved_tokens": row.reserved_tokens, "actual_tokens": row.charged_tokens})
        if notice:
            notices.append(notice)
    notices.extend(_threshold_alerts(db, row, conversation, usage_limits(row.business_id)))
    return await _finish(db, response, row, notices, reason="usage_uncertain" if row.status == "uncertain" else None)
