"""Private durable agent input images; no payment authorization or public files."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from uuid import uuid4
import base64
import hashlib
import os
import tempfile
import warnings

import httpx
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Response, UploadFile
from PIL import Image, UnidentifiedImageError
from pydantic import ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .auth import IntegrationAuthContext, require_integration_scope
from .config import get_settings
from .database import get_db
from .models import IdempotencyRecord, Order, PaymentEvidence
from .schemas import ApiModel
from .agent_checkout import assert_sender, fingerprint
from .storage import delete_private_file, load_private_file
from .agent_blocking_io import private_transaction

router = APIRouter(prefix="/integrations", tags=["agent-private-input-media"])
CATEGORY = "agent-input-media/v1"
PUBLIC_FIELDS = ("media_ref", "sha256", "content_sha256", "mime_type", "size_bytes")
SHA_PATTERN = r"^[0-9a-f]{64}$"


def fail(status, code):
    raise HTTPException(status, {"code": code})


def agent_only(integration):
    if integration.profile != "restaurant_agent" or integration.legacy or not integration.branch_id:
        fail(403, "AGENT_MEDIA_SCOPED_PROFILE_REQUIRED")


def media_scope(row):
    return f"agent-input-media:{row.branch_id}:{row.id}"


def media_record(db, row, source_message_id, *, lock=False):
    query = select(IdempotencyRecord).where(IdempotencyRecord.business_id == row.business_id,
        IdempotencyRecord.scope == media_scope(row), IdempotencyRecord.idempotency_key == source_message_id)
    return db.scalar(query.with_for_update() if lock else query)


def lock_media_quota(db, row):
    """Technical mutex, never the commercial Branch row. Conversation first."""
    query = select(IdempotencyRecord).where(IdempotencyRecord.business_id == row.business_id,
        IdempotencyRecord.scope == f"agent-media-quota:{row.branch_id}", IdempotencyRecord.idempotency_key == "reservation-lock")
    record = db.scalar(query.with_for_update())
    if record is None:
        try:
            with db.begin_nested():
                record = IdempotencyRecord(business_id=row.business_id, scope=f"agent-media-quota:{row.branch_id}",
                    idempotency_key="reservation-lock", response_body={"version": 1})
                db.add(record)
                db.flush()
        except IntegrityError:
            record = db.scalar(query.with_for_update())
            if record is None:
                raise
    return record


def public_descriptor(descriptor):
    return {field: descriptor[field] for field in PUBLIC_FIELDS}


def hashes(data):
    encoded = base64.b64encode(data)
    return hashlib.sha256(encoded).hexdigest(), hashlib.sha256(data).hexdigest()


def validate_image(data, declared):
    limits = get_settings()
    if not data or len(data) > limits.agent_media_max_bytes:
        fail(413, "AGENT_MEDIA_SIZE_LIMIT")
    mime = (declared or "").split(";", 1)[0].lower().strip()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as picture:
                actual = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}.get(picture.format)
                if not actual or mime != actual:
                    fail(422, "AGENT_MEDIA_MIME_MISMATCH")
                if picture.width <= 0 or picture.height <= 0 or picture.width * picture.height > limits.agent_media_max_pixels:
                    fail(413, "AGENT_MEDIA_PIXEL_LIMIT")
                if getattr(picture, "n_frames", 1) != 1:
                    fail(422, "AGENT_MEDIA_ANIMATION_UNSUPPORTED")
                picture.verify()
            with Image.open(BytesIO(data)) as picture:
                picture.load()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        fail(422, "AGENT_MEDIA_INVALID_IMAGE")
    return mime


def object_path(row, reference, mime):
    suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[mime]
    relative = f"{CATEGORY}/b{row.business_id}/s{row.branch_id}/{reference}{suffix}"
    settings = get_settings()
    if settings.supabase_url and settings.supabase_service_role_key:
        return "supabase://impulsa-private/" + relative
    return str((settings.upload_dir.resolve() / relative).resolve())


def owned_object_path(path, descriptor):
    expected = object_path(type("Scope", (), descriptor), descriptor["media_ref"], descriptor["mime_type"])
    if path != expected:
        fail(409, "AGENT_MEDIA_STORAGE_IDENTITY_CONFLICT")
    return path


def verify_bytes(data, descriptor):
    legacy, content = hashes(data)
    if len(data) != descriptor["size_bytes"] or legacy != descriptor["sha256"] or content != descriptor["content_sha256"]:
        fail(409, "AGENT_MEDIA_STORAGE_INTEGRITY_CONFLICT")


async def store_reserved_file(data, descriptor):
    """Stable reserved object only. Caller holds descriptor row lock through IO."""
    target = owned_object_path(descriptor["storage_path"], descriptor)
    settings = get_settings()
    if target.startswith("supabase://impulsa-private/"):
        relative = target.removeprefix("supabase://impulsa-private/")
        url = f"{settings.supabase_url.rstrip('/')}/storage/v1/object/impulsa-private/{relative}"
        headers = {"Authorization": f"Bearer {settings.supabase_service_role_key}",
            "apikey": settings.supabase_service_role_key, "Content-Type": descriptor["mime_type"], "x-upsert": "false"}
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, headers=headers, content=data)
        if response.status_code not in {200, 201}:
            # A timeout/lost ready commit may leave this exact object already stored.
            # Read the stable private key and prove identity; never overwrite a conflict.
            try:
                present, _ = await load_private_file(target)
            except (FileNotFoundError, OSError, httpx.HTTPError):
                fail(503, "AGENT_MEDIA_STORAGE_UNCONFIRMED")
            verify_bytes(present, descriptor)
        return
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        verify_bytes(path.read_bytes(), descriptor)
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".staging-", suffix=".tmp", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def descriptor_matches(descriptor, supplied):
    return isinstance(supplied, dict) and all(supplied.get(k) == descriptor.get(k) for k in PUBLIC_FIELDS)


def validate_ingress_media(db, row, message_id, message):
    """Called only for new/in-flight input, after completed redelivery detection."""
    media = message.get("media")
    if not media:
        return
    record = media_record(db, row, message_id)
    if not record or record.response_body.get("status") not in {"ready", "attached"} or not descriptor_matches(record.response_body, media):
        fail(409, "AGENT_MEDIA_NOT_DURABLE")
    # A server descriptor is the only allowed shape, never a path or inline bytes.
    if set(media) != set(PUBLIC_FIELDS):
        fail(422, "AGENT_MEDIA_DESCRIPTOR_REQUIRED")


def state_bindings(state):
    """Known durable locations only; return source and descriptor identity."""
    found = []
    def add(value, body=False):
        if not isinstance(value, dict):
            return
        source = value.get("whatsapp_message_id") if body else value.get("source_turn_id")
        reference = value.get("image_media_ref") if body else value.get("media_ref")
        if source and reference:
            found.append({"source": str(source), "media_ref": reference, "sha256": value.get("image_sha256"),
                "content_sha256": value.get("image_content_sha256"), "mime_type": value.get("image_mime")})
    add(state.get("receipt_submission"))
    condition = state.get("pending_fulfillment_condition")
    quote = condition.get("quote_response") if isinstance(condition, dict) else None
    operations = [state.get("pending"), quote.get("operation") if isinstance(quote, dict) else None]
    for operation in operations:
        if isinstance(operation, dict):
            add(operation.get("receipt_binding"))
            if operation.get("kind") == "evidence":
                add(operation.get("body"), True)
            if operation.get("kind") == "attention":
                attachments = (operation.get("body") or {}).get("attachments", [])
                if isinstance(attachments, list):
                    for attachment in attachments:
                        if isinstance(attachment, dict) and attachment.get("source_message_id") and attachment.get("media_ref"):
                            found.append({"source": attachment["source_message_id"], "media_ref": attachment["media_ref"],
                                "sha256": attachment.get("sha256"), "content_sha256": attachment.get("content_sha256"), "attention": True})
    return found


def validate_state_media(db, row, proposed):
    allowed = {(b["source"], b["media_ref"]) for b in state_bindings(row.state or {})}
    active = ((row.state or {}).get("_processing_message") or {}).get("message", {}).get("media")
    if isinstance(active, dict) and active.get("media_ref"):
        allowed.add((row.current_message_id, active["media_ref"]))
    for binding in state_bindings(proposed):
        if (binding["source"], binding["media_ref"]) not in allowed:
            fail(409, "AGENT_MEDIA_REFERENCE_NOT_CURRENT_OR_HELD")
        record = media_record(db, row, binding["source"])
        if not record or record.response_body.get("status") not in {"ready", "attached"}:
            fail(409, "AGENT_MEDIA_REFERENCE_RETIRED")
        identity_fields = ("media_ref", "sha256", "content_sha256") if binding.get("attention") else ("media_ref", "sha256", "content_sha256", "mime_type")
        if any(binding[k] != record.response_body.get(k) for k in identity_fields):
            fail(409, "AGENT_MEDIA_REFERENCE_CONFLICT")


def referenced_sources(row):
    sources = {str(row.current_message_id)} if row.current_message_id else set()
    for queued in row.pending or []:
        if not isinstance(queued, dict) or not queued.get("message_id"):
            return None  # Ambiguous queue: preserve everything.
        sources.add(str(queued["message_id"]))
    state = row.state or {}
    receipt = state.get("receipt_submission")
    if receipt is not None:
        if not isinstance(receipt, dict) or not receipt.get("source_turn_id"):
            return None
        sources.add(str(receipt["source_turn_id"]))
    condition = state.get("pending_fulfillment_condition")
    if condition is not None and not isinstance(condition, dict):
        return None
    quote = (condition or {}).get("quote_response")
    if quote is not None and not isinstance(quote, dict):
        return None
    for operation in [state.get("pending"), (quote or {}).get("operation")]:
        if operation is not None and not isinstance(operation, dict):
            return None
        if not operation:
            continue
        if operation.get("kind") == "evidence":
            source = (operation.get("body") or {}).get("whatsapp_message_id") or operation.get("turn_id")
            if not source:
                return None
            sources.add(str(source))
        if operation.get("kind") == "attention":
            attachments = (operation.get("body") or {}).get("attachments", [])
            if not isinstance(attachments, list):
                return None
            for attachment in attachments:
                if not isinstance(attachment, dict) or not attachment.get("source_message_id"):
                    return None
                sources.add(str(attachment["source_message_id"]))
        binding = operation.get("receipt_binding")
        if binding is not None:
            if not isinstance(binding, dict) or not binding.get("source_turn_id"):
                return None
            sources.add(str(binding["source_turn_id"]))
    return sources


async def retire_unreferenced_media(db, integration, conversation_id):
    """Bounded, fail-preserving cleanup after a verified commit. No evidence deletion."""
    from .agent_runtime_api import scoped_conversation, input_record
    row = scoped_conversation(db, integration, conversation_id)
    references = referenced_sources(row)
    if references is None:
        db.commit()
        return
    records = list(db.scalars(select(IdempotencyRecord).where(IdempotencyRecord.business_id == row.business_id,
        IdempotencyRecord.scope == media_scope(row)).order_by(IdempotencyRecord.id).with_for_update()))
    retiring = []
    for record in records:
        if len(retiring) >= get_settings().agent_media_cleanup_batch:
            break
        descriptor = record.response_body
        if (descriptor.get("status") not in {"ready", "retiring"} or descriptor.get("attention_ids")
            or record.idempotency_key in references or not descriptor.get("disposition")):
            continue
        accepted = input_record(db, row, record.idempotency_key)
        if not accepted or accepted.response_body.get("status") != "completed":
            continue
        owned_object_path(descriptor["storage_path"], descriptor)
        record.response_body = {**descriptor, "status": "retiring"}
        retiring.append((record.id, deepcopy(record.response_body)))
    db.commit()  # Retiring still consumes quota and cannot be read or rewritten.
    for record_id, descriptor in retiring:
        try:
            await delete_private_file(owned_object_path(descriptor["storage_path"], descriptor))
        except (FileNotFoundError, OSError, httpx.HTTPError):
            continue  # Keep counted; a later bounded invocation can retry.
        record = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.id == record_id).with_for_update())
        if record and record.response_body.get("status") == "retiring" and record.response_body.get("media_ref") == descriptor["media_ref"]:
            record.response_body = {**record.response_body, "status": "released"}
        db.commit()


def record_media_disposition(db, row, value):
    if value is None:
        return
    if not isinstance(value, dict) or set(value) != {"source_message_id", "media_ref", "sha256", "content_sha256", "disposition"}:
        fail(422, "AGENT_MEDIA_DISPOSITION_INVALID")
    active = ((row.state or {}).get("_processing_message") or {}).get("message", {}).get("media")
    if (value.get("source_message_id") != row.current_message_id or value.get("disposition") not in {"consumed", "discarded"}
        or not isinstance(active, dict) or any(value.get(k) != active.get(k) for k in ("media_ref", "sha256", "content_sha256"))):
        fail(409, "AGENT_MEDIA_DISPOSITION_NOT_CURRENT")
    record = media_record(db, row, row.current_message_id, lock=True)
    if not record or record.response_body.get("status") not in {"ready", "attached"}:
        fail(409, "AGENT_MEDIA_DISPOSITION_UNAVAILABLE")
    record.response_body = {**record.response_body, "disposition": value["disposition"]}


@router.post("/conversations/media")
@private_transaction
async def stage_media(response: Response, file: UploadFile = File(...), sender: str = Form(...),
    channel: str = Form(default="whatsapp"), message_id: str = Form(...),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("agent:state")), db=Depends(get_db)):
    from .agent_runtime_api import normalize_sender, require_key
    agent_only(integration)
    require_key(idempotency_key)
    require_key(message_id)
    if channel != "whatsapp":
        fail(422, "AGENT_MEDIA_CHANNEL_INVALID")
    sender = normalize_sender(sender)
    limits = get_settings()
    data = await file.read(limits.agent_media_max_bytes + 1)
    mime = validate_image(data, file.content_type)
    legacy, content = hashes(data)
    from .agent_runtime_api import lock_or_create_conversation
    row = lock_or_create_conversation(db, integration, channel, sender)
    lock_media_quota(db, row)
    record = media_record(db, row, message_id, lock=True)
    if record:
        existing = record.response_body
        if any(existing.get(k) != v for k, v in {"sha256": legacy, "content_sha256": content, "mime_type": mime, "size_bytes": len(data)}.items()):
            fail(409, "AGENT_MEDIA_MESSAGE_CONFLICT")
        if existing.get("status") in {"ready", "attached", "retiring", "released"}:
            result = public_descriptor(existing)
            db.commit()
            response.headers["Cache-Control"] = "no-store"
            return result
    else:
        reservations = list(db.scalars(select(IdempotencyRecord).where(IdempotencyRecord.business_id == row.business_id,
            IdempotencyRecord.scope.like(f"agent-input-media:{row.branch_id}:%"))))
        counted = [r for r in reservations if r.response_body.get("status") not in {"released", "attached"}]
        own = [r for r in counted if r.scope == media_scope(row)]
        if (sum(int(r.response_body.get("size_bytes", 0)) for r in counted) + len(data) > limits.agent_media_branch_bytes
            or len(counted) + 1 > limits.agent_media_branch_files
            or sum(int(r.response_body.get("size_bytes", 0)) for r in own) + len(data) > limits.agent_media_conversation_bytes
            or len(own) + 1 > limits.agent_media_conversation_files):
            fail(429, "AGENT_MEDIA_CAPACITY_REACHED")
        reference = str(uuid4())
        descriptor = {"media_ref": reference, "sha256": legacy, "content_sha256": content, "mime_type": mime,
            "size_bytes": len(data), "status": "reserved", "business_id": row.business_id,
            "branch_id": row.branch_id, "conversation_id": row.id, "sender": row.sender, "channel": row.channel,
            "source_message_id": message_id, "storage_path": object_path(row, reference, mime)}
        record = IdempotencyRecord(business_id=row.business_id, scope=media_scope(row),
            idempotency_key=message_id, response_code=200, response_body=descriptor)
        db.add(record)
        db.flush()
    record_id = record.id
    db.commit()  # Reservation/quota must survive storage success plus later DB failure.
    record = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.id == record_id).with_for_update())
    descriptor = deepcopy(record.response_body)
    if descriptor["status"] == "reserved":
        try:
            await store_reserved_file(data, descriptor)
        except (OSError, httpx.HTTPError):
            db.rollback()
            fail(503, "AGENT_MEDIA_STORAGE_UNCONFIRMED")
        record.response_body = {**descriptor, "status": "ready"}
    result = public_descriptor(record.response_body)
    db.commit()
    response.headers["Cache-Control"] = "no-store"
    return result


class ReadMedia(ApiModel):
    model_config = ConfigDict(extra="forbid")
    lease_token: str = Field(min_length=20, max_length=150)
    expected_version: int = Field(ge=1)
    source_message_id: str = Field(min_length=1, max_length=240)
    media_ref: str = Field(min_length=1, max_length=100)
    sha256: str = Field(pattern=SHA_PATTERN)
    content_sha256: str = Field(pattern=SHA_PATTERN)


@router.post("/conversations/{conversation_id}/media/read")
@private_transaction
async def read_media(conversation_id: str, payload: ReadMedia, response: Response,
    integration: IntegrationAuthContext = Depends(require_integration_scope("agent:state")), db=Depends(get_db)):
    from .agent_runtime_api import scoped_conversation, lease_valid
    agent_only(integration)
    row = scoped_conversation(db, integration, conversation_id)
    if not lease_valid(row, payload.lease_token) or row.version != payload.expected_version:
        fail(409, "AGENT_MEDIA_STALE_LEASE")
    held = {(b["source"], b["media_ref"]) for b in state_bindings(row.state or {})}
    active = ((row.state or {}).get("_processing_message") or {}).get("message", {}).get("media")
    if isinstance(active, dict) and active.get("media_ref"):
        held.add((row.current_message_id, active["media_ref"]))
    if (payload.source_message_id, payload.media_ref) not in held:
        fail(403, "AGENT_MEDIA_NOT_CURRENT_OR_HELD")
    record = media_record(db, row, payload.source_message_id)
    if not record or record.response_body.get("status") not in {"ready", "attached"}:
        fail(409, "AGENT_MEDIA_NOT_AVAILABLE")
    descriptor = record.response_body
    if any(getattr(payload, field) != descriptor.get(field) for field in ("media_ref", "sha256", "content_sha256")):
        fail(409, "AGENT_MEDIA_REFERENCE_CONFLICT")
    try:
        data, _ = await load_private_file(owned_object_path(descriptor["storage_path"], descriptor))
    except (FileNotFoundError, OSError, httpx.HTTPError):
        fail(503, "AGENT_MEDIA_STORAGE_UNAVAILABLE")
    verify_bytes(data, descriptor)
    response.headers["Cache-Control"] = "no-store"
    return {**public_descriptor(descriptor), "data": base64.b64encode(data).decode("ascii")}


class ReconcileEvidence(ApiModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=240)


def evidence_fingerprint(body, content_hash):
    try:
        amount = Decimal(str(body["amount_detected"])) if body.get("amount_detected") is not None else None
    except (InvalidOperation, ValueError):
        fail(409, "AGENT_EVIDENCE_PENDING_INVALID")
    return fingerprint({"request": body.get("payment_request_id"), "sha256": content_hash,
        "provider": body.get("provider", "yape"), "amount": amount, "operation": body.get("operation_number"),
        "code": body.get("security_code"), "occurred_at": body.get("occurred_at"), "recipient": body.get("recipient"),
        "message": body.get("whatsapp_message_id"), "sender": body.get("sender"),
        "looks_like_receipt": str(body.get("looks_like_payment_receipt")).lower() == "true",
        "warnings": body.get("analysis_warnings")})


@router.post("/orders/{order_id}/payment-evidence/reconcile")
@private_transaction
async def reconcile_evidence(order_id: int, payload: ReconcileEvidence, response: Response,
    integration: IntegrationAuthContext = Depends(require_integration_scope("payments:write")), db=Depends(get_db)):
    from .agent_runtime_api import scoped_conversation
    agent_only(integration)
    # Generic agent authorization already fenced the lease and own order for this route.
    if not integration.conversation_id or not integration.sender:
        fail(409, "AGENT_EVIDENCE_LEASE_REQUIRED")
    row = scoped_conversation(db, integration, integration.conversation_id)
    order = db.scalar(select(Order).where(Order.id == order_id, Order.business_id == row.business_id,
        Order.branch_id == row.branch_id))
    if not order:
        fail(404, "AGENT_EVIDENCE_ORDER_NOT_FOUND")
    assert_sender(order, row.sender)
    pending = (row.state or {}).get("pending")
    if (not isinstance(pending, dict) or pending.get("kind") != "evidence" or pending.get("method") != "POST"
        or pending.get("path") != f"/integrations/orders/{order_id}/payment-evidence"
        or pending.get("idempotency_key") != payload.idempotency_key or not isinstance(pending.get("body"), dict)):
        fail(409, "AGENT_EVIDENCE_PENDING_MISMATCH")
    body = pending["body"]
    if not body.get("whatsapp_message_id"):
        fail(409, "AGENT_EVIDENCE_SOURCE_REQUIRED")
    assert_sender(order, body.get("sender"))
    saved = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.business_id == row.business_id,
        IdempotencyRecord.scope == f"integration-evidence-upload:{order_id}",
        IdempotencyRecord.idempotency_key == payload.idempotency_key))
    response.headers["Cache-Control"] = "no-store"
    if not saved:
        return {"status": "not_recorded"}
    result = saved.response_body
    content_hash = body.get("image_content_sha256")
    if content_hash:
        staged = media_record(db, row, str(body["whatsapp_message_id"]))
        expected = {"media_ref": body.get("image_media_ref"), "content_sha256": content_hash,
            "sha256": body.get("image_sha256"), "mime_type": body.get("image_mime")}
        if not staged or any(staged.response_body.get(k) != v for k, v in expected.items()):
            fail(409, "AGENT_EVIDENCE_IMAGE_IDENTITY_CONFLICT")
    else:
        # Historical pending intents have only the base64-domain hash. The server
        # can prove the exact stored receipt; it must never equate the two hashes.
        evidence_id = (result.get("evidence") or {}).get("id")
        evidence = db.scalar(select(PaymentEvidence).where(PaymentEvidence.id == evidence_id,
            PaymentEvidence.business_id == row.business_id, PaymentEvidence.order_id == order_id))
        if not evidence or evidence.whatsapp_message_id != body["whatsapp_message_id"]:
            fail(409, "AGENT_EVIDENCE_LEGACY_UNRESOLVED")
        try:
            data, mime = await load_private_file(evidence.storage_path)
        except (FileNotFoundError, OSError, httpx.HTTPError):
            fail(409, "AGENT_EVIDENCE_LEGACY_UNRESOLVED")
        transport_hash, content_hash = hashes(data)
        if transport_hash != body.get("image_sha256") or (mime.split(";", 1)[0] != body.get("image_mime")):
            fail(409, "AGENT_EVIDENCE_IMAGE_IDENTITY_CONFLICT")
    if not result.get("request_fingerprint") or result["request_fingerprint"] != evidence_fingerprint(body, content_hash):
        fail(409, "AGENT_EVIDENCE_FINGERPRINT_CONFLICT")
    return {"status": "committed", "result": deepcopy(result)}
