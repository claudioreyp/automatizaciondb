"""Durable staff attention. A notice is never a payment or an order."""
from copy import deepcopy
import hashlib
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import ConfigDict, Field
from sqlalchemy import select

from .auth import AuthContext, IntegrationAuthContext, ensure_branch_scope, require_integration_scope, require_roles
from .database import get_db
from .models import AgentAttentionRequest, AgentConversation, Branch, Order, utcnow
from .schemas import ApiModel
from .agent_checkout import assert_sender, fingerprint
from .services import audit, get_idempotent_response, save_idempotent_response
from .realtime import hub
from .agent_blocking_io import private_transaction, run_private_transaction

router = APIRouter(prefix="/api/v1", tags=["agent-attention"])
STAFF = ("superadmin", "owner", "manager", "cashier", "dispatcher")


def create_attention(db, *, business_id, branch_id, sender, kind, summary, source_key, order_id=None, details=None):
    # Hash the full key before both lookup and insert; truncation would alias
    # long idempotency keys with different endings.
    source_key = "sha256:" + hashlib.sha256(source_key.encode()).hexdigest()
    existing = db.scalar(select(AgentAttentionRequest).where(
        AgentAttentionRequest.business_id == business_id, AgentAttentionRequest.branch_id == branch_id,
        AgentAttentionRequest.source_key == source_key))
    if existing:
        return existing
    row = AgentAttentionRequest(business_id=business_id, branch_id=branch_id, order_id=order_id,
        sender=sender, kind=kind, summary=summary[:2000], source_key=source_key,
        details=deepcopy(details or {}), status="pending", version=1)
    db.add(row)
    db.flush()
    audit(db, None, "agent.attention_requested", "agent_attention", row.id, business_id,
          {"order_id": order_id, "kind": kind}, branch_id=branch_id)
    return row


def serialize_attention(row):
    result = {key: getattr(row, key) for key in (
        "id", "business_id", "branch_id", "order_id", "sender", "kind", "summary", "details",
        "status", "version", "created_at", "resolved_at", "resolved_by", "resolution_note")}
    result["details"] = {key: deepcopy(value) for key, value in (row.details or {}).items() if not key.startswith("_")}
    return result


class AttentionAttachment(ApiModel):
    model_config = ConfigDict(extra="forbid")
    source_message_id: str = Field(min_length=1, max_length=240)
    media_ref: str = Field(min_length=1, max_length=100)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class AttentionInput(ApiModel):
    model_config = ConfigDict(extra="forbid")
    sender: str = Field(min_length=3, max_length=120)
    order_id: int | None = None
    kind: Literal["catering", "product_change_request", "location_after_dispatch", "complaint", "human_requested", "late_payment_receipt"]
    summary: str = Field(min_length=1, max_length=2000)
    message_ids: list[str] = Field(default_factory=list, max_length=30)
    attachments: list[AttentionAttachment] = Field(default_factory=list, max_length=1)


class AttentionResolution(ApiModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    status: Literal["acknowledged", "resolved"]
    note: str = Field(min_length=1, max_length=2000)


def receipt(db, scope, key, business_id, digest):
    if not key or len(key) > 240:
        raise HTTPException(422, "Idempotency-Key is required (up to 240 characters)")
    old = get_idempotent_response(db, scope, key, business_id)
    if old:
        if old.get("digest") != digest:
            raise HTTPException(409, "Idempotency key already used with other data")
        return old["result"]


@router.post("/integrations/attention-requests", status_code=201)
async def request_attention(payload: AttentionInput, response: Response,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("agent:attention")), db=Depends(get_db)):
    result, branch_id, notify = await run_private_transaction(_request_attention_transaction,
        payload, response, idempotency_key=idempotency_key, integration=integration, db=db)
    if notify:
        await hub.broadcast(branch_id, "human.requested", {"attention_id": result["id"], "order_id": result["order_id"]})
    return result


async def _request_attention_transaction(payload, response, *, idempotency_key, integration, db):
    from .api import integration_branch_from_auth
    from .agent_runtime_api import normalize_sender
    payload.sender = normalize_sender(payload.sender)
    _, branch = integration_branch_from_auth(db, integration)
    if integration.sender is not None and integration.sender != payload.sender:
        raise HTTPException(403, "Sender must match the authenticated conversation")
    if payload.kind == "catering" and payload.order_id is not None:
        raise HTTPException(422, "Catering attention is independent of an order")
    if payload.kind == "late_payment_receipt" and (payload.order_id is not None or len(payload.attachments) != 1):
        raise HTTPException(422, "A late receipt requires one private attachment and no order")
    if payload.attachments and payload.kind != "late_payment_receipt":
        raise HTTPException(422, "Private receipt attachments require the late receipt attention kind")
    if payload.order_id is not None:
        order = db.scalar(select(Order).where(Order.id == payload.order_id,
            Order.business_id == branch.business_id, Order.branch_id == branch.id).with_for_update())
        if not order:
            raise HTTPException(404, "Order not found")
        assert_sender(order, payload.sender)
    if any(len(value) > 240 for value in payload.message_ids):
        raise HTTPException(422, "Invalid message identifier")
    scope = f"agent-attention:{branch.id}"
    fingerprint_body = payload.model_dump()
    if not payload.attachments:
        fingerprint_body.pop("attachments", None)  # Preserve every prior attention digest.
    digest = fingerprint(fingerprint_body)
    old = receipt(db, scope, idempotency_key, branch.business_id, digest)
    if old:
        response.status_code = 200
        return old, branch.id, False
    prepared = await prepare_attention_attachments(db, integration, payload.attachments) if payload.attachments else []
    row = db.scalar(select(AgentAttentionRequest).where(
        AgentAttentionRequest.business_id == branch.business_id, AgentAttentionRequest.branch_id == branch.id,
        AgentAttentionRequest.sender == payload.sender, AgentAttentionRequest.order_id == payload.order_id,
        AgentAttentionRequest.kind == payload.kind, AgentAttentionRequest.status != "resolved").with_for_update())
    if row:
        new_refs = {record.response_body["media_ref"] for record, _ in prepared}
        previous_refs = {item.get("media_ref") for item in (row.details or {}).get("attachments", [])}
        if len(previous_refs | new_refs) > 20:
            raise HTTPException(429, "This attention request has reached its private attachment limit")
        row.details = {**(row.details or {}), "message_ids": list(dict.fromkeys(
            (row.details or {}).get("message_ids", []) + payload.message_ids))[-100:],
            "followups": ((row.details or {}).get("followups", []) + [payload.summary])[-20:]}
        row.version += 1
        row.status = "pending"
    else:
        row = create_attention(db, business_id=branch.business_id, branch_id=branch.id,
            sender=payload.sender, kind=payload.kind, summary=payload.summary, order_id=payload.order_id,
            source_key=f"request:{idempotency_key}", details={"message_ids": payload.message_ids})
    if prepared:
        bind_attention_attachments(row, prepared)
    from fastapi.encoders import jsonable_encoder
    result = jsonable_encoder(serialize_attention(row))
    save_idempotent_response(db, scope, idempotency_key, branch.business_id, {"digest": digest, "result": result})
    db.commit()
    response.headers["Cache-Control"] = "no-store"
    return result, branch.id, True


async def prepare_attention_attachments(db, integration, attachments):
    from .agent_runtime_api import scoped_conversation
    from .agent_input_media import agent_only, state_bindings, media_record, owned_object_path, verify_bytes
    from .storage import load_private_file
    import httpx
    agent_only(integration)
    if not integration.conversation_id or not integration.sender:
        raise HTTPException(409, "A current conversation lease is required")
    conversation = scoped_conversation(db, integration, integration.conversation_id)
    allowed = {(item["source"], item["media_ref"]) for item in state_bindings(conversation.state or {})}
    active = ((conversation.state or {}).get("_processing_message") or {}).get("message", {}).get("media")
    if isinstance(active, dict) and active.get("media_ref"):
        allowed.add((conversation.current_message_id, active["media_ref"]))
    result = []
    for attachment in attachments:
        if (attachment.source_message_id, attachment.media_ref) not in allowed:
            raise HTTPException(403, "Receipt attachment must belong to the current or held input")
        record = media_record(db, conversation, attachment.source_message_id, lock=True)
        descriptor = record.response_body if record else {}
        if descriptor.get("status") not in {"ready", "attached"} or any(
            getattr(attachment, name) != descriptor.get(name) for name in ("media_ref", "sha256", "content_sha256")):
            raise HTTPException(409, "Receipt attachment identity is unavailable or changed")
        try:
            data, _ = await load_private_file(owned_object_path(descriptor["storage_path"], descriptor))
        except (FileNotFoundError, OSError, httpx.HTTPError):
            raise HTTPException(503, "Private receipt storage is unavailable")
        verify_bytes(data, descriptor)
        result.append((record, conversation.id))
    return result


def bind_attention_attachments(row, prepared):
    details = deepcopy(row.details or {})
    visible = list(details.get("attachments", []))
    bindings = list(details.get("_media_bindings", []))
    for record, conversation_id in prepared:
        descriptor = record.response_body
        reference = descriptor["media_ref"]
        if not any(item.get("media_ref") == reference for item in visible):
            visible.append({"media_ref": reference, "mime_type": descriptor["mime_type"], "size_bytes": descriptor["size_bytes"],
                "image_url": f"/api/v1/agent-attention/{row.id}/attachments/{reference}"})
            bindings.append({"media_ref": reference, "conversation_id": conversation_id, "source_message_id": record.idempotency_key})
        record.response_body = {**descriptor, "status": "attached",
            "attention_ids": list(dict.fromkeys([*(descriptor.get("attention_ids") or []), row.id]))}
    row.details = {**details, "attachments": visible, "_media_bindings": bindings}


@router.get("/agent-attention/{attention_id}/attachments/{media_ref}")
@private_transaction
async def get_attention_attachment(attention_id: str, media_ref: str,
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager", "cashier")), db=Depends(get_db)):
    from .agent_input_media import media_record, owned_object_path, verify_bytes
    from .storage import load_private_file
    import httpx
    attention = db.get(AgentAttentionRequest, attention_id)
    if not attention:
        raise HTTPException(404, "Attention request not found")
    ensure_branch_scope(user, attention.business_id, attention.branch_id)
    binding = next((entry for entry in (attention.details or {}).get("_media_bindings", []) if entry.get("media_ref") == media_ref), None)
    if not binding:
        raise HTTPException(404, "Private attachment not found")
    conversation = db.scalar(select(AgentConversation).where(AgentConversation.id == binding["conversation_id"],
        AgentConversation.business_id == attention.business_id, AgentConversation.branch_id == attention.branch_id,
        AgentConversation.sender == attention.sender))
    record = media_record(db, conversation, binding["source_message_id"]) if conversation else None
    descriptor = record.response_body if record else {}
    if (descriptor.get("status") != "attached" or descriptor.get("media_ref") != media_ref
        or attention.id not in descriptor.get("attention_ids", [])):
        raise HTTPException(409, "Private attachment identity changed")
    try:
        data, _ = await load_private_file(owned_object_path(descriptor["storage_path"], descriptor))
    except (FileNotFoundError, OSError, httpx.HTTPError):
        raise HTTPException(503, "Private receipt storage is unavailable")
    verify_bytes(data, descriptor)
    return Response(content=data, media_type=descriptor["mime_type"], headers={"Cache-Control": "no-store"})


@router.get("/agent-attention")
def list_attention(branch_id: int, response: Response, status: Literal["open", "all"] = "open",
    order_id: int | None = None,
    user: AuthContext = Depends(require_roles(*STAFF)), db=Depends(get_db)):
    branch = db.get(Branch, branch_id)
    if not branch:
        raise HTTPException(404, "Branch not found")
    ensure_branch_scope(user, branch.business_id, branch_id)
    stmt = select(AgentAttentionRequest).where(AgentAttentionRequest.business_id == branch.business_id,
        AgentAttentionRequest.branch_id == branch_id)
    if status == "open":
        stmt = stmt.where(AgentAttentionRequest.status != "resolved")
    if order_id is not None:
        stmt = stmt.where(AgentAttentionRequest.order_id == order_id)
    response.headers["Cache-Control"] = "no-store"
    return [serialize_attention(row) for row in db.scalars(stmt.order_by(AgentAttentionRequest.created_at.desc(), AgentAttentionRequest.id))]


@router.post("/agent-attention/{attention_id}/resolve")
async def resolve_attention(attention_id: str, payload: AttentionResolution,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    user: AuthContext = Depends(require_roles(*STAFF)), db=Depends(get_db)):
    row = db.scalar(select(AgentAttentionRequest).where(AgentAttentionRequest.id == attention_id).with_for_update())
    if not row:
        raise HTTPException(404, "Attention request not found")
    ensure_branch_scope(user, row.business_id, row.branch_id)
    if user.role == "dispatcher" and row.kind not in {"fulfillment_change", "location_after_dispatch"}:
        raise HTTPException(403, "This request requires an owner, manager or cashier")
    scope = f"attention-resolve:{row.id}"
    digest = fingerprint(payload.model_dump())
    old = receipt(db, scope, idempotency_key, row.business_id, digest)
    if old:
        return old
    if row.version != payload.expected_version:
        raise HTTPException(409, "Attention request changed; reload before resolving")
    row.status, row.resolution_note = payload.status, payload.note
    row.resolved_by, row.resolved_at = user.user_id, utcnow()
    row.version += 1
    audit(db, user, "agent.attention_reviewed", "agent_attention", row.id, row.business_id,
          {"status": row.status, "note": payload.note, "financial_operation": False}, branch_id=row.branch_id)
    from fastapi.encoders import jsonable_encoder
    result = jsonable_encoder(serialize_attention(row))
    save_idempotent_response(db, scope, idempotency_key, row.business_id, {"digest": digest, "result": result})
    db.commit()
    await hub.broadcast(row.branch_id, "human.requested", {"attention_id": row.id, "order_id": row.order_id})
    return result
