"""Fenced conversation processing and an allowlist for restaurant credentials.

The model never receives a credential, lease, arbitrary HTTP tool or database.
Leases are short transactions, not transactions held during a model call.
"""
from copy import deepcopy
from datetime import timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import secrets
import unicodedata
from types import SimpleNamespace

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, Response
from pydantic import ConfigDict, Field, ValidationError
from sqlalchemy import select, or_, and_, exists

from .auth import IntegrationAuthContext, require_integration_scope
from .database import get_db
from .models import AgentConversation, AgentMessageDelivery, Branch, BranchSettings, Business, IdempotencyRecord, Order, OrderPaymentRequest, utcnow
from .schemas import ApiModel, OrderCreate
from .agent_checkout import assert_sender, ensure_request_upload, fingerprint
from .agent_context import yape_revision
from .agent_blocking_io import private_transaction
from . import agent_message_batching as batching
from .agent_original_policy import annotate_policy_response, confirmed_original_policy_context
from .agent_order_reference import recover_order_reference
from .agent_contracts import NativeQueueReadiness

router = APIRouter(prefix="/api/v1/integrations/conversations", tags=["agent-conversations"])
LEASE_SECONDS = 120
MAX_STATE_BYTES = 131072
from .agent_tools import AGENT_ROUTE_ALLOWLIST, TOOLS_BY_ROUTE


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
    active = deepcopy((row.state or {}).get("_processing_message", {}))
    is_effect = request.method in {"POST", "PATCH", "DELETE"} and not route.endswith("preview") and not TOOLS_BY_ROUTE[(request.method, route)].read_only
    if is_effect and batching.superseded(row):
        raise HTTPException(409, {"code": "INPUT_SUPERSEDED",
            "message": "Newer consumer input must be considered before this operation."})
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
    request.state.agent_contact_display_name = active.get("transport", {}).get("contact_display_name")
    if not request.state.agent_verified_phone and not row.sender.endswith("@lid"):
        request.state.agent_verified_phone = normalize_sender(row.sender)
    if is_effect and active:
        # This marker and the effect share the endpoint transaction and row lock.
        # A successful/uncertain earlier operation must be reconciled, not erased
        # because another input arrived while its HTTP response was in flight.
        active["execution_started"] = True
        row.state = {**row.state, "_processing_message": active}
        db.flush()
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


class DeliveryCheck(ApiModel):
    model_config = ConfigDict(extra="forbid")
    delivery_token: str = Field(min_length=20, max_length=150)
    payment_instruction_unavailable: bool = Field(default=False, strict=True)


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


def transport_identity(sender, supplied_phone=None):
    sender = normalize_sender(sender)
    if supplied_phone is not None:
        supplied_phone = normalize_sender(supplied_phone)
        if supplied_phone.endswith("@lid") or (not sender.endswith("@lid") and supplied_phone != sender):
            raise HTTPException(422, "Verified phone must match the transport sender identity")
    return sender, supplied_phone or (sender if not sender.endswith("@lid") else None)


def _stored_identity_phone(row):
    return (row.state or {}).get("_transport", {}).get("verified_phone") or (
        row.sender if not row.sender.endswith("@lid") else None)


def _identity_state_in_use(db, row):
    state = row.state or {}
    if row.current_message_id or row.pending or state.get("pending") or (
        row.lease_until and as_utc(row.lease_until) > utcnow()
    ):
        return True
    if state.get("order_id"):
        order = db.scalar(select(Order).where(Order.id == state["order_id"],
            Order.business_id == row.business_id, Order.branch_id == row.branch_id))
        return bool(order and order.status not in {"closed", "cancelled", "delivered"})
    destination = state.get("destination")
    has_destination = isinstance(destination, dict) and any(value is not None and value != ""
        for value in destination.values())
    return has_destination or any(state.get(key) for key in ("items", "addition_items", "variant_proposals",
        "reference", "fulfillment", "payment_method", "receipt_submission", "receipt_target_selection",
        "purchase_condition", "price_confirmation", "pending_fulfillment_condition", "pending_product_choice"))


def _select_identity_conversation(db, rows, sender, verified_phone, message_id, provider_ids=()):
    for row in rows:
        transport = (row.state or {}).get("_transport", {})
        aliases = transport.get("identity_aliases", {})
        if (row.sender == sender or isinstance(aliases, dict) and aliases.get(sender) is True):
            known_phone = _stored_identity_phone(row)
            if known_phone and verified_phone and known_phone != verified_phone:
                raise HTTPException(409, "Verified transport identity changed; reconcile it before processing")
    if len(rows) < 2:
        return rows[0] if rows else None
    # Old duplicated conversations remain intact. A replay belongs to its
    # original durable record, never to another row selected for a new turn.
    if message_id:
        recorded = [row for row in rows if db.scalar(select(IdempotencyRecord.id).where(
            IdempotencyRecord.business_id == row.business_id,
            IdempotencyRecord.scope.in_([f"agent-input:{row.id}", f"agent-input-media:{row.branch_id}:{row.id}"]),
            IdempotencyRecord.idempotency_key == message_id).limit(1)) is not None]
        if len(recorded) == 1:
            return recorded[0]
        if len(recorded) > 1:
            raise HTTPException(409, "Multiple durable messages require identity reconciliation")
    if provider_ids:
        requested = set(provider_ids)
        owners = []
        for row in rows:
            known = set(db.scalars(select(IdempotencyRecord.idempotency_key).where(
                IdempotencyRecord.business_id == row.business_id,
                IdempotencyRecord.scope == f"agent-source:{row.id}",
                IdempotencyRecord.idempotency_key.in_(provider_ids))))
            known.update(item.get("id") for item in (row.state or {}).get("recent_messages", [])
                if isinstance(item, dict) and item.get("id") in requested)
            if known:
                owners.append((row, known))
        if len(owners) > 1:
            raise HTTPException(409, "Provider messages span conversations requiring reconciliation")
        if owners:
            owner, known = owners[0]
            if known != requested and any(other.id != owner.id and _identity_state_in_use(db, other) for other in rows):
                raise HTTPException(409, "Partial provider replay spans active conversations")
            return owner
    active = [row for row in rows if _identity_state_in_use(db, row)]
    if len(active) > 1:
        raise HTTPException(409, "Multiple active conversations require identity reconciliation")
    return active[0] if active else min(rows, key=lambda row: (as_utc(row.created_at), row.id))


def lock_or_create_conversation(db, integration, channel, sender, verified_phone=None, message_id=None, provider_ids=()):
    """Existing chats lock Conversation first; creation never reverses that order."""
    restricted = integration.profile == "restaurant_agent"
    identity = AgentConversation.sender == sender
    if restricted:
        identity = or_(identity, AgentConversation.state["_transport"]["identity_aliases"][sender].as_boolean().is_(True))
        if verified_phone:
            identity = or_(identity, AgentConversation.sender == verified_phone,
                AgentConversation.state["_transport"]["verified_phone"].as_string() == verified_phone)
    query = select(AgentConversation).where(AgentConversation.business_id == integration.business_id,
        AgentConversation.branch_id == integration.branch_id, AgentConversation.channel == channel, identity).order_by(AgentConversation.id)
    def choose(lock=True, refresh=False):
        statement = query.with_for_update() if lock else query
        if refresh:
            statement = statement.execution_options(populate_existing=True)
        rows = list(db.scalars(statement))
        return _select_identity_conversation(db, rows, sender, verified_phone, message_id, provider_ids) if restricted else (rows[0] if rows else None)
    row = choose()
    if row is None:
        # Another ingress may create the row after our first SELECT. While holding
        # Branch, observe it without waiting for its Conversation lock: a domain
        # writer may already own that lock and need Branch/FK access. Release the
        # creation mutex before acquiring the shared conversation lock.
        db.scalar(select(Branch.id).where(Branch.id == integration.branch_id,
            Branch.business_id == integration.business_id).with_for_update())
        row = choose(lock=False)
        if row is None:
            initial = {"_transport": {"identity_aliases": {sender: True},
                **({"verified_phone": verified_phone} if verified_phone else {})}} if restricted else {}
            row = AgentConversation(business_id=integration.business_id, branch_id=integration.branch_id,
                channel=channel, sender=sender, state=initial, pending=[], version=1)
            db.add(row)
            db.flush()
        db.commit()
        # The non-locking creation lookup can have populated the identity map.
        # Reload after waiting for a concurrent writer, including when sessions
        # keep objects after commit, so new ingress never restores stale state.
        row = choose(refresh=True)
    if restricted:
        transport = deepcopy((row.state or {}).get("_transport", {}))
        aliases = transport.get("identity_aliases", {})
        aliases = dict(aliases) if isinstance(aliases, dict) else {}
        if sender not in aliases and len(aliases) >= 16:
            raise HTTPException(409, "Transport identity alias limit requires reconciliation")
        aliases[sender] = True
        transport["identity_aliases"] = aliases
        if verified_phone and not transport.get("verified_phone"):
            transport["verified_phone"] = verified_phone
        row.state = {**(row.state or {}), "_transport": transport}
    return row


def input_record(db, row, message_id):
    return db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.business_id == row.business_id,
        IdempotencyRecord.scope == f"agent-input:{row.id}", IdempotencyRecord.idempotency_key == message_id))


def provider_sources(message):
    """Provider IDs survive a gateway restart or a PN/LID-dependent batch ID."""
    messages = message.get("messages")
    if messages is None:
        return []
    if (not isinstance(messages, list) or not 1 <= len(messages) <= 30
        or any(not isinstance(item, dict) or set(item) != {"id", "text"}
            or not isinstance(item["id"], str) or not 1 <= len(item["id"]) <= 160
            or not isinstance(item["text"], str) or len(item["text"]) > 8000 for item in messages)
        or len({item["id"] for item in messages}) != len(messages)
        or message.get("text") != "\n".join(item["text"] for item in messages)):
        raise HTTPException(422, "Provider messages must have unique IDs and preserve the complete input text")
    sources = []
    for index, item in enumerate(messages):
        context = {}
        if index == len(messages) - 1:
            if "unsupported" in message:
                context["unsupported"] = message["unsupported"]
            if message.get("location"):
                context["location"] = message["location"]
            if message.get("media"):
                context["media"] = {key: message["media"].get(key) for key in
                    ("sha256", "content_sha256", "mime_type", "size_bytes")}
        sources.append({"id": item["id"], "text": item["text"], "digest": fingerprint({**item, **context}),
                        "has_context": bool(context)})
    return sources


def deduplicate_provider_sources(db, row, message, sources, native=False):
    if not sources:
        return message, [], None
    records = {record.idempotency_key: record for record in db.scalars(select(IdempotencyRecord).where(
        IdempotencyRecord.business_id == row.business_id, IdempotencyRecord.scope == f"agent-source:{row.id}",
        IdempotencyRecord.idempotency_key.in_([item["id"] for item in sources])))}
    # The previous release retained recent IDs but no source-to-batch mapping.
    # Its exact textual redeliveries are inert; never infer receipt equivalence.
    recent = {item["id"]: item.get("text") for item in (row.state or {}).get("recent_messages", [])
              if isinstance(item, dict) and isinstance(item.get("id"), str)}
    fresh, parents, historical = [], set(), False
    for source in sources:
        record = records.get(source["id"])
        if record:
            if batching.is_native(record.response_body) != native:
                raise HTTPException(409, "Provider source belongs to another processing contract")
            if record.response_body.get("digest") != source["digest"]:
                raise HTTPException(409, "Provider message identifier was reused with different content")
            parents.add(record.response_body["turn_id"])
        elif source["id"] in recent:
            if recent[source["id"]] != source["text"] or source["has_context"]:
                raise HTTPException(409, "Historical provider message requires reconciliation")
            historical = True
        else:
            fresh.append(source)
    if len(fresh) == len(sources):
        return message, fresh, None
    if not fresh:
        unsettled = any((input_record(db, row, parent).response_body.get("status") != "completed")
            for parent in parents if input_record(db, row, parent) is not None)
        active_ids = {item.get("id") for item in (row.state or {}).get("_processing_message", {}).get("message", {}).get("messages", [])
                      if isinstance(item, dict)}
        unsettled = unsettled or historical and bool(active_ids.intersection(source["id"] for source in sources))
        return message, [], {"status": "queued" if unsettled else "completed", "conversation_id": row.id,
            "version": row.version, "pending_count": len(row.pending or []), "duplicate": True}
    if message.get("media") or message.get("location") or "unsupported" in message:
        raise HTTPException(409, "Partially replayed contextual input requires reconciliation")
    filtered = deepcopy(message)
    filtered["messages"] = [{"id": item["id"], "text": item["text"]} for item in fresh]
    filtered["text"] = "\n".join(item["text"] for item in fresh)
    return filtered, fresh, None


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


def confirmed_selection_summary_context(db, row, current_message_id):
    """Prove a retained selection summary was transport-ACKed before this input.

    State identifies one candidate; only the own effective outbox supplies proof.
    No caller timestamp, assistant_context or variant context grants authority.
    The runtime must still compare the quote with its fresh POS preview.
    """
    state = row.state if isinstance(row.state, dict) else {}
    saved = state.get("selection_summary_context")
    keys = {"version", "scope", "conversation_id", "message_id", "order_id", "purpose",
        "local_date", "pricing_fingerprint", "selection_fingerprint", "quote",
        "summary_text", "response_texts"}
    if not isinstance(saved, dict) or set(saved) != keys or type(saved.get("version")) is not int or saved["version"] != 1:
        return None
    scope = {"business_id": row.business_id, "branch_id": row.branch_id}
    if (saved["scope"] != scope or state.get("scope") != scope
            or not isinstance(saved["scope"], dict)
            or any(type(saved["scope"].get(key)) is not int for key in scope)
            or saved["conversation_id"] != row.id):
        return None
    message_id = saved["message_id"]
    order_id = state.get("order_id") or None
    if (not isinstance(message_id, str) or not 0 < len(message_id) <= 240
            or message_id == current_message_id
            or (order_id is not None and (type(order_id) is not int or order_id <= 0))
            or (saved["order_id"] is not None and type(saved["order_id"]) is not int)
            or saved["order_id"] != order_id
            or saved["purpose"] != ("addition" if order_id else "initial")
            or saved["local_date"] != (state.get("draft_local_date") or None)
            or (saved["local_date"] is not None and (not isinstance(saved["local_date"], str)
                or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", saved["local_date"])))):
        return None
    selection = saved["selection_fingerprint"]
    pricing = saved["pricing_fingerprint"]
    if (not isinstance(selection, str) or not 0 < len(selection) <= 65536
            or not isinstance(pricing, str) or not 0 < len(pricing) <= 65536
            or not isinstance(state.get("summary_fingerprint"), str)
            or not 0 < len(state["summary_fingerprint"]) <= 65536):
        return None
    items = state.get("addition_items" if order_id else "items")
    destination = state.get("destination")
    if not isinstance(items, list) or len(items) > 30 or not isinstance(destination, dict):
        return None
    try:
        # Compare decoded structures, not a second cross-language JSON encoder.
        # These are the exact public fields of core.selection/pricingFingerprint.
        selected = {"items": items, "channel": state["fulfillment"], "destination": destination,
            "reference": state["reference"], "payment": state["addition_payment_method" if order_id else "payment_method"]}
        priced_items = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("modifiers"), list):
                return None
            priced = {key: item[key] for key in ("product_id", "variant_id", "quantity") if key in item}
            priced["modifiers"] = [{key: modifier[key] for key in ("modifier_id", "quantity") if key in modifier}
                for modifier in item["modifiers"] if isinstance(modifier, dict)]
            if len(priced["modifiers"]) != len(item["modifiers"]):
                return None
            priced["notes"] = item.get("notes") or ""
            priced_items.append(priced)
        priced = {"order_id": order_id, "items": priced_items, "channel": state["fulfillment"],
            "destination": {key: value for key, value in destination.items() if key != "reference"}
                if order_id is None and state["fulfillment"] == "delivery" else None}
        def no_constant(value):
            raise ValueError("Non-finite fingerprint")
        if (json.loads(selection, parse_constant=no_constant) != selected
                or json.loads(state["summary_fingerprint"], parse_constant=no_constant) != selected
                or json.loads(pricing, parse_constant=no_constant) != priced):
            return None
        quote = saved["quote"]
        allowed_quote = {"id", "kind", "status", "pricing_authority", "purpose", "currency", "fulfillment",
            "subtotal", "discount", "products_total", "known_total", "final_total",
            "delivery_fee", "delivery_fee_status", "items", "promotions"}
        if (not isinstance(quote, dict) or set(quote) - allowed_quote
                or quote.get("id") != "selection_quote" or quote.get("kind") != "selection_quote"
                or quote.get("status") != "verified"
                or quote.get("pricing_authority") not in {"server_preview", "confirmed_pos_order"}
                or quote.get("purpose") != saved["purpose"]
                or quote.get("fulfillment") != state["fulfillment"]
                or not isinstance(quote.get("currency"), str) or not re.fullmatch(r"[A-Z]{3}", quote["currency"])
                or not isinstance(quote.get("items"), list) or len(quote["items"]) > 30
                or not isinstance(quote.get("promotions"), list) or len(quote["promotions"]) > 30
                or len(json.dumps(quote, ensure_ascii=False, allow_nan=False).encode()) > 65536):
            return None
    except (KeyError, TypeError, ValueError, RecursionError):
        return None
    summary, expected_texts = saved["summary_text"], saved["response_texts"]
    if (not isinstance(summary, str) or not summary or len(summary) > 6000
            or not isinstance(expected_texts, list) or not 1 <= len(expected_texts) <= 12
            or any(not isinstance(text, str) or not text or len(text) > 6000 for text in expected_texts)
            or sum(len(text) for text in expected_texts) > 12000
            or not any(summary in text for text in expected_texts)):
        return None
    received = input_record(db, row, current_message_id)
    source = input_record(db, row, message_id)
    received_at = batching.trusted_input_created_at(db, row, current_message_id) if received else None
    if (received_at is None or source is None or not isinstance(source.response_body, dict)
            or source.response_body.get("status") != "completed"):
        return None
    delivery = db.scalar(select(AgentMessageDelivery).where(
        AgentMessageDelivery.business_id == row.business_id, AgentMessageDelivery.branch_id == row.branch_id,
        AgentMessageDelivery.conversation_id == row.id, AgentMessageDelivery.sender == row.sender,
        AgentMessageDelivery.message_id == message_id, AgentMessageDelivery.status == "sent"))
    if (not delivery or not delivery.updated_at or not source.created_at
            or not as_utc(source.created_at) < as_utc(delivery.updated_at) < as_utc(received_at)
            or not isinstance(delivery.receipt, dict)
            or not isinstance(delivery.receipt.get("transport_id"), str) or not delivery.receipt["transport_id"].strip()):
        return None
    response = delivery.response if isinstance(delivery.response, dict) else {}
    actions = response.get("actions")
    if not isinstance(actions, list) or not 1 <= len(actions) <= 12:
        return None
    texts = []
    for action in actions:
        if not isinstance(action, dict):
            return None
        if action.get("type") == "text":
            text = action.get("text")
        elif action.get("type") in {"pos_menu", "private_yape_qr", "private_yape_instructions", "media_binary"}:
            text = action.get("caption")
            if text is None or text == "":
                continue
        else:
            continue
        if not isinstance(text, str) or not text or len(text) > 6000:
            return None
        texts.append(text)
    if texts != expected_texts or sum(len(text) for text in texts) > 12000 or not any(summary in text for text in texts):
        return None
    return {**deepcopy(saved), "status": "sent", "confirmation": "transport_ack", "delivery_id": delivery.id}


def acquire_pending(db, row, native=False, *, allow_order_reference=False):
    # The FIFO head and an expired active turn keep their original worker family.
    active = (row.state or {}).get("_processing_message") if row.current_message_id else None
    head = active if row.current_message_id else (row.pending[0] if row.pending else None)
    if head is not None and batching.is_native(head) != native:
        db.commit()
        return {"status": "idle", "conversation_id": row.id, "version": row.version,
                "pending_count": len(row.pending or [])}
    if row.lease_hash and row.lease_until and as_utc(row.lease_until) > utcnow():
        db.commit()
        return {"status": "queued", "conversation_id": row.id, "version": row.version,
                "pending_count": len(row.pending or [])}
    if row.current_message_id:
        active = (row.state or {}).get("_processing_message")
        if not active:
            raise HTTPException(409, "Expired processing state requires reconciliation")
    elif row.pending:
        wait = batching.wait_seconds(row.pending, utcnow())
        if wait > 0:
            db.commit()
            return {"status": "queued", "conversation_id": row.id, "version": row.version,
                "pending_count": len(row.pending), "retry_after_ms": max(1, round(wait * 1000))}
        if row.pending[0].get("awaiting_media"):
            expire_reserved_media(db, row)
            db.commit()
            return acquire_pending(db, row, native=native, allow_order_reference=allow_order_reference)
        group = batching.prefix(row.pending) if row.pending[0].get("batching") else row.pending[:1]
        active = batching.merge(group)
        active["claim_sequence"] = (row.state or {}).get("_transport", {}).get("input_sequence", 0)
        if len(active["input_ids"]) > 1 or active.get("defer_count", 0):
            # Model usage and commercial intents belong to this exact attempt,
            # not to the first source ID whose earlier model call was charged.
            active["message_id"] = "batch:" + fingerprint([row.id, active["input_ids"], active.get("defer_count", 0)])
            if not input_record(db, row, active["message_id"]):
                sources = [input_record(db, row, ident) for ident in active["input_ids"]]
                db.add(IdempotencyRecord(business_id=row.business_id, scope=f"agent-input:{row.id}",
                    idempotency_key=active["message_id"], created_at=min(source.created_at for source in sources),
                    response_body={"status": "processing", "internal_batch": True,
                        "_runtime_contract": batching.NATIVE_CONTRACT,
                        "input_ids": active["input_ids"], "defer_count": active.get("defer_count", 0)}))
        active["base_state"] = deepcopy({k: v for k, v in (row.state or {}).items()
            if not k.startswith("_processing_") and k != "_transport"})
        row.pending = row.pending[len(group):]
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
    original_policy_context = confirmed_original_policy_context(db, row, active["message_id"])
    summary_context = confirmed_selection_summary_context(db, row, active["message_id"])
    recovered_reference = recover_order_reference(db, row, active) if native and allow_order_reference else None
    return {"status": "acquired", "conversation_id": row.id, "version": row.version,
        "lease_token": token, "lease_until": row.lease_until,
        "state": {k: v for k, v in (row.state or {}).items() if not k.startswith("_processing_") and k not in {"_transport", "_operation_identity"}},
        "message": {k: v for k, v in active["message"].items() if k not in {"verified_phone", "contact_display_name"}},
        "message_id": active["message_id"], "sender": row.sender,
        "verified_phone": (row.state or {}).get("_transport", {}).get("verified_phone"),
        "contact_display_name": active.get("transport", {}).get("contact_display_name"),
        **({"order_reference_recovery": recovered_reference} if recovered_reference else {}),
        **({"confirmed_response_context": confirmed_context} if confirmed_context else {}),
        **({"confirmed_original_policy_context": original_policy_context} if original_policy_context else {}),
        **({"confirmed_selection_summary_context": summary_context} if summary_context else {}),
        "input_ids": active.get("input_ids", [active["message_id"]]),
        "pending_count": len(row.pending or [])}


@router.post("/claim")
@router.post("/enqueue")
def claim(payload: Claim, response: Response, request: Request,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("agent:state")), db=Depends(get_db)):
    return accept_input(payload, response, request, idempotency_key, integration, db)


@router.post("/native/enqueue")
def enqueue_native(payload: Claim, response: Response, request: Request,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("agent:state")), db=Depends(get_db)):
    if integration.profile != "restaurant_agent":
        raise HTTPException(403, "Scoped restaurant credentials are required")
    return accept_input(payload, response, request, idempotency_key, integration, db, native=True)


def accept_input(payload, response, request, idempotency_key, integration, db, reservation=None, *, native=False):
    require_key(idempotency_key)
    if integration.legacy or not integration.branch_id:
        raise HTTPException(403, "Scoped integration credentials are required")
    from .agent_unsupported_input import validate_unsupported_input
    validate_unsupported_input(payload.message, payload.message_id)
    sender, verified_phone = transport_identity(payload.sender, payload.message.get("verified_phone"))
    bounded(payload.message, 32768)
    if len(str(payload.message.get("text", ""))) > 8000:
        raise HTTPException(413, "Message is too long")
    sources = provider_sources(payload.message) if integration.profile == "restaurant_agent" else []
    row = lock_or_create_conversation(db, integration, payload.channel, sender, verified_phone, payload.message_id,
        tuple(source["id"] for source in sources))
    transport = (row.state or {}).get("_transport", {})
    if transport.get("verified_phone") and verified_phone and transport["verified_phone"] != verified_phone:
        raise HTTPException(409, "Verified transport identity changed; reconcile it before processing")
    # A technical redelivery can carry a newly refreshed profile. It is still the
    # same message, and must retain its original protected identity snapshot.
    digest = fingerprint({"message": {k: v for k, v in payload.message.items() if k != "contact_display_name"},
        "sender": sender, "channel": payload.channel})
    identity_digest = fingerprint({"message": {k: v for k, v in payload.message.items()
        if k not in {"contact_display_name", "verified_phone"}}, "conversation_id": row.id, "channel": payload.channel})
    previous = input_record(db, row, payload.message_id)
    if previous and batching.is_native(previous.response_body) != native:
        raise HTTPException(409, "Input belongs to another processing contract")
    if previous and (previous.response_body.get("identity_digest") != identity_digest if previous.response_body.get("identity_digest")
        else previous.response_body.get("digest") != digest):
        raise HTTPException(409, "Message identifier was reused with different content")
    response.headers["Cache-Control"] = "no-store"
    if previous and previous.response_body.get("status") == "completed":
        return {"status": "completed", "conversation_id": row.id, "version": row.version,
                "response": previous.response_body.get("response"), "duplicate": True}
    fresh_sources = sources
    if not previous:
        payload.message, fresh_sources, duplicate = deduplicate_provider_sources(db, row, payload.message, sources, native=native)
        if duplicate:
            db.commit()
            return duplicate
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
            IdempotencyRecord.response_body["internal_batch"].as_boolean().is_not(True),
            IdempotencyRecord.created_at >= now - timedelta(hours=1))).all()
        if len(recent) >= limits.agent_messages_per_hour:
            raise HTTPException(429, "Conversation hourly limit reached; retry later", headers={"Retry-After": "3600"})
        if sum(as_utc(value) >= now - timedelta(seconds=60) for value in recent) >= limits.agent_messages_per_minute:
            raise HTTPException(429, "Conversation rate limit reached", headers={"Retry-After": "30"})
        transport = deepcopy(transport)
        transport["input_sequence"] = int(transport.get("input_sequence", 0)) + 1
        if verified_phone and not transport.get("verified_phone"):
            transport["verified_phone"] = verified_phone
        if "contact_display_name" in payload.message:
            transport["contact_display_name"] = contact_display_name(payload.message["contact_display_name"])
        row.state = {**(row.state or {}), "_transport": transport}
        message = {k: deepcopy(v) for k, v in payload.message.items() if k != "contact_display_name"}
        db.add(IdempotencyRecord(business_id=row.business_id, scope=f"agent-input:{row.id}",
            idempotency_key=payload.message_id, response_body={"digest": digest, "identity_digest": identity_digest, "status": "accepted",
                "_runtime_contract": batching.NATIVE_CONTRACT if native else batching.LEGACY_CONTRACT}))
        for source in fresh_sources:
            db.add(IdempotencyRecord(business_id=row.business_id, scope=f"agent-source:{row.id}",
                idempotency_key=source["id"], response_body={"digest": source["digest"], "turn_id": payload.message_id,
                    "_runtime_contract": batching.NATIVE_CONTRACT if native else batching.LEGACY_CONTRACT}))
        row.pending = [*(row.pending or []), {"message_id": payload.message_id, "message": message,
            "accepted_at": now.isoformat(), "input_sequence": transport["input_sequence"],
            "batching": native,
            "_runtime_contract": batching.NATIVE_CONTRACT if native else batching.LEGACY_CONTRACT,
            **({"awaiting_media": {**reservation, "expires_at": (now + timedelta(seconds=batching.MEDIA_WAIT_SECONDS)).isoformat()}} if reservation else {}),
            "transport": {"contact_display_name": transport.get("contact_display_name")}}]
        if reservation:
            # SessionLocal has autoflush=False: the input record added above
            # must exist before its private reservation metadata is queried.
            db.flush()
            record = input_record(db, row, payload.message_id)
            record.response_body = {**record.response_body, "media_reservation": reservation,
                "reserved_source_digest": fingerprint({k: v for k, v in message.items() if k not in {"contact_display_name", "verified_phone"}})}
    if reservation is not None or request.url.path.endswith("/enqueue"):
        db.commit()
        return {"status": "queued", "conversation_id": row.id, "pending_count": len(row.pending or [])}
    return acquire_pending(db, row, native=native, allow_order_reference="orders:read" in integration.scopes)


def media_reservation(value, *, native_source_message_id=None):
    if (not isinstance(value, dict) or set(value) != {"id", "mime_type"}
            or not isinstance(value["id"], str)
            or not (re.fullmatch(r"\d{5,80}", value["id"])
                or (value["id"] == native_source_message_id
                    and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value["id"])))
            or value["mime_type"] not in {"image/png", "image/jpeg", "image/webp"}):
        raise HTTPException(422, "A bounded image identity and MIME are required")
    return deepcopy(value)


def expire_reserved_media(db, row):
    entry = row.pending[0]
    notice = {"actions": [{"type": "text", "text": "No pude cargar la imagen que enviaste. No la usé como comprobante ni realicé una operación con ella. Puedes reenviarla para que la revise."}],
        "turn": {"id": entry["message_id"], "outcome": "none"},
        "_media_failure_notice": True, "_input_sequence": entry.get("input_sequence", 0),
        "_runtime_contract": batching.NATIVE_CONTRACT}
    record = input_record(db, row, entry["message_id"])
    record.response_body = {**record.response_body, "status": "completed", "media_failed": True,
        "response": {k: v for k, v in notice.items() if not k.startswith("_")}}
    db.add(AgentMessageDelivery(business_id=row.business_id, branch_id=row.branch_id,
        conversation_id=row.id, message_id=entry["message_id"], sender=row.sender, response=notice))
    history = list((row.state or {}).get("recent_messages", []))
    known = {m.get("id") for m in history}
    history.extend(deepcopy(m) for m in entry["message"].get("messages", []) if m.get("id") not in known)
    row.state = {**(row.state or {}), "recent_messages": history[-30:]}
    row.pending = row.pending[1:]


@router.post("/media-reservations")
def reserve_media(payload: Claim, response: Response, request: Request,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("agent:state")), db=Depends(get_db)):
    require_key(idempotency_key)
    if integration.profile != "restaurant_agent":
        raise HTTPException(403, "Scoped restaurant credentials are required")
    message = deepcopy(payload.message)
    reservation = media_reservation(message.pop("media_reservation", None), native_source_message_id=payload.message_id)
    if (message.get("media") or message.get("location") or not isinstance(message.get("messages"), list)
            or len(message["messages"]) != 1 or not isinstance(message["messages"][0], dict)
            or message["messages"][0].get("id") != payload.message_id):
        raise HTTPException(422, "Reserve exactly one image source before its download")
    original = Claim(sender=payload.sender, channel=payload.channel, message_id=payload.message_id, message=message)
    sender, phone = transport_identity(payload.sender, message.get("verified_phone"))
    row = lock_or_create_conversation(db, integration, payload.channel, sender, phone, payload.message_id)
    record = input_record(db, row, payload.message_id)
    source_digest = fingerprint({k: v for k, v in message.items() if k not in {"contact_display_name", "verified_phone"}})
    if record:
        if not batching.is_native(record.response_body):
            raise HTTPException(409, "Image input belongs to another processing contract")
        if (record.response_body.get("media_reservation") != reservation
                or record.response_body.get("reserved_source_digest") != source_digest):
            raise HTTPException(409, "Image reservation identity or caption changed")
        entry = next((item for item in row.pending or [] if item["message_id"] == payload.message_id), None)
        needs = bool(entry and entry.get("awaiting_media") and batching.wait_seconds([entry], utcnow()) > 0)
        db.commit()
        return {"reserved": True, "duplicate": True, "needs_media": needs,
            "media_failed": record.response_body.get("media_failed", False),
            "completed_media_sha256": record.response_body.get("completed_media_sha256")}
    result = accept_input(original, response, request, idempotency_key, integration, db, reservation, native=True)
    row = scoped_conversation(db, integration, result["conversation_id"])
    record = input_record(db, row, payload.message_id)
    record.response_body = {**record.response_body, "reserved_source_digest": source_digest}
    if record.response_body.get("media_reservation") != reservation:
        raise HTTPException(409, "Image reservation identity changed")
    entry = next((item for item in row.pending or [] if item["message_id"] == payload.message_id), None)
    if entry and entry.get("awaiting_media") and batching.wait_seconds([entry], utcnow()) <= 0:
        # Timeout is always consumed in FIFO order by the worker, never by a
        # late download overtaking older messages.
        needs_media = False
    else:
        needs_media = bool(entry and entry.get("awaiting_media"))
    db.commit()
    return {"reserved": True, "duplicate": result.get("duplicate", False) or not result.get("pending_count"),
        "needs_media": needs_media, "media_failed": record.response_body.get("media_failed", False)}


@router.post("/media-reservations/inspect")
def inspect_reserved_media(payload: Claim, response: Response,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("agent:state", read_only=True)),
    db=Depends(get_db)):
    """Private read-only revalidation of an existing original native reservation."""
    require_key(idempotency_key)  # Validated, never inserted as idempotency state.
    if integration.profile != "restaurant_agent" or integration.legacy or not integration.branch_id:
        raise HTTPException(403, "Scoped restaurant credentials are required")
    message = deepcopy(payload.message)
    bounded(message, 32768)
    reservation = media_reservation(message.pop("media_reservation", None), native_source_message_id=payload.message_id)
    if (message.get("media") or message.get("location") or not isinstance(message.get("messages"), list)
            or len(message["messages"]) != 1 or not isinstance(message["messages"][0], dict)
            or message["messages"][0].get("id") != payload.message_id):
        raise HTTPException(422, "Inspect exactly one original image source")
    provider_sources(message)
    sender, phone = transport_identity(payload.sender, message.get("verified_phone"))
    # An already stored alias can identify this conversation. A supplied phone
    # alone never discovers one, and this route cannot register a new alias.
    identity = or_(AgentConversation.sender == sender,
        AgentConversation.state["_transport"]["identity_aliases"][sender].as_boolean().is_(True))
    rows = db.execute(select(AgentConversation, IdempotencyRecord).join(IdempotencyRecord, and_(
        IdempotencyRecord.business_id == AgentConversation.business_id,
        IdempotencyRecord.scope == "agent-input:" + AgentConversation.id,
        IdempotencyRecord.idempotency_key == payload.message_id)).where(
        AgentConversation.business_id == integration.business_id,
        AgentConversation.branch_id == integration.branch_id,
        AgentConversation.channel == payload.channel, identity).limit(2)).all()
    if len(rows) != 1:
        raise HTTPException(404 if not rows else 409, "Existing native image reservation is unavailable")
    row, record = rows[0]
    known_phone = _stored_identity_phone(row)
    if known_phone and phone and known_phone != phone:
        raise HTTPException(409, "Existing native image reservation identity changed")
    stored = record.response_body
    if (not isinstance(stored, dict) or not batching.is_native(stored)
            or stored.get("status") not in {"accepted", "completed"}
            or not isinstance(stored.get("media_reservation"), dict)):
        raise HTTPException(404, "Existing native image reservation is unavailable")
    source_fingerprint = fingerprint({k: v for k, v in message.items()
        if k not in {"contact_display_name", "verified_phone"}})
    if (stored["media_reservation"] != reservation
            or stored.get("reserved_source_digest") != source_fingerprint):
        raise HTTPException(409, "Existing native image reservation identity changed")
    pending = [entry for entry in row.pending or []
        if isinstance(entry, dict) and entry.get("message_id") == payload.message_id]
    if len(pending) > 1 or (pending and not batching.is_native(pending[0])):
        raise HTTPException(409, "Existing native image reservation is inconsistent")
    if pending and (not isinstance(pending[0].get("message"), dict)
            or fingerprint({k: v for k, v in pending[0]["message"].items()
                if k not in {"media", "contact_display_name", "verified_phone"}}) != source_fingerprint):
        raise HTTPException(409, "Existing native image reservation is inconsistent")
    failed = stored.get("media_failed", False)
    if type(failed) is not bool:
        raise HTTPException(409, "Existing native image reservation is inconsistent")
    awaiting = pending[0].get("awaiting_media") if pending else None
    expires_at, needs_media = None, False
    if awaiting is not None:
        if (not isinstance(awaiting, dict) or set(awaiting) != {"id", "mime_type", "expires_at"}
                or {k: awaiting[k] for k in ("id", "mime_type")} != reservation
                or not isinstance(awaiting["expires_at"], str)):
            raise HTTPException(409, "Existing native image reservation is inconsistent")
        expires_at = awaiting["expires_at"]
        deadline = batching.timestamp(expires_at)
        if deadline is None:
            raise HTTPException(409, "Existing native image reservation is inconsistent")
        # Inspection never consumes a timeout, creates its notice or touches FIFO.
        needs_media = stored["status"] == "accepted" and not failed and deadline > utcnow()
    response.headers["Cache-Control"] = "no-store"
    return {"reserved": True, "needs_media": needs_media, "media_failed": failed,
        "expires_at": expires_at, "media_reservation": deepcopy(stored["media_reservation"]),
        "source_fingerprint": stored["reserved_source_digest"]}


@router.post("/media-reservations/complete")
def complete_reserved_media(payload: Claim,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("agent:state")), db=Depends(get_db)):
    require_key(idempotency_key)
    if integration.profile != "restaurant_agent":
        raise HTTPException(403, "Scoped restaurant credentials are required")
    sender, phone = transport_identity(payload.sender, payload.message.get("verified_phone"))
    row = lock_or_create_conversation(db, integration, payload.channel, sender, phone, payload.message_id)
    record = input_record(db, row, payload.message_id)
    if not record or not record.response_body.get("media_reservation") or not batching.is_native(record.response_body):
        raise HTTPException(409, "Image must reserve its durable native input position first")
    message = deepcopy(payload.message)
    reservation = media_reservation(message.pop("media_reservation", None), native_source_message_id=payload.message_id)
    if record.response_body["media_reservation"] != reservation:
        raise HTTPException(409, "Image reservation identity changed")
    source_digest = fingerprint({k: v for k, v in message.items() if k not in {"media", "contact_display_name", "verified_phone"}})
    if record.response_body.get("reserved_source_digest") != source_digest:
        raise HTTPException(409, "Image caption or source changed")
    if record.response_body.get("status") == "completed":
        if record.response_body.get("completed_media_sha256") and message.get("media", {}).get("sha256") != record.response_body["completed_media_sha256"]:
            raise HTTPException(409, "Completed image bytes changed")
        return {"status": "completed", "duplicate": True, "media_failed": record.response_body.get("media_failed", False)}
    if not isinstance(message.get("media"), dict):
        raise HTTPException(422, "Completed input requires its durable image descriptor")
    entry = next((item for item in row.pending or [] if item["message_id"] == payload.message_id), None)
    if not entry:
        raise HTTPException(409, "Image input is no longer pending")
    if not entry.get("awaiting_media"):
        if fingerprint(entry["message"]) != fingerprint(message):
            raise HTTPException(409, "Completed image bytes or caption changed")
        return {"status": "queued", "duplicate": True}
    if batching.wait_seconds([entry], utcnow()) <= 0:
        return {"status": "completed", "duplicate": True, "media_failed": True}
    if (message.get("messages") != entry["message"].get("messages")
            or message.get("text") != entry["message"].get("text")
            or message.get("location") or message.get("media", {}).get("mime_type") != reservation["mime_type"]):
        raise HTTPException(409, "Image source, caption or MIME changed")
    bounded(message, 32768)
    from .agent_input_media import validate_ingress_media
    validate_ingress_media(db, row, payload.message_id, message)
    sources = provider_sources(message)
    for source in sources:
        proof = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.business_id == row.business_id,
            IdempotencyRecord.scope == f"agent-source:{row.id}", IdempotencyRecord.idempotency_key == source["id"]))
        proof.response_body = {**proof.response_body, "digest": source["digest"]}
    updated = {**entry, "message": message}
    updated.pop("awaiting_media")
    row.pending = [updated if item["message_id"] == payload.message_id else item for item in row.pending]
    record.response_body = {**record.response_body,
        "completed_media_sha256": message["media"]["sha256"],
        "digest": fingerprint({"message": {k: v for k, v in message.items() if k != "contact_display_name"}, "sender": sender, "channel": payload.channel}),
        "identity_digest": fingerprint({"message": {k: v for k, v in message.items() if k not in {"contact_display_name", "verified_phone"}}, "conversation_id": row.id, "channel": payload.channel})}
    db.commit()
    return {"status": "queued", "duplicate": False}


@router.get("/native/readiness", response_model=NativeQueueReadiness,
    summary="Observe native queue readiness without claiming work",
    description=("A current branch-scoped scheduling hint. False means no native work or pending response "
        "was visible at checked_at; a later input can arrive immediately. True includes quiet-window "
        "candidates. This read never claims a lease or delivery and grants no execution authority."))
def native_queue_readiness(response: Response,
    integration: IntegrationAuthContext = Depends(require_integration_scope("agent:state", read_only=True)), db=Depends(get_db)):
    if integration.legacy or not integration.business_id or not integration.branch_id:
        raise HTTPException(403, "Scoped integration credentials are required")
    # A current database observation, not a claim or an execution permission.
    # Quiet-window candidates deliberately stay ready; the existing claim owns
    # batching, media expiry, ordering, leases and every subsequent decision.
    checked_at = utcnow()
    deliveries = delivery_candidates_query(integration, native=True).with_only_columns(AgentMessageDelivery.id)
    work = work_candidates_query(integration, native=True, evaluated_at=checked_at).with_only_columns(AgentConversation.id)
    ready = bool(db.scalar(select(or_(exists(deliveries), exists(work)))))
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return {"protocol_version": 1, "runtime_contract": "native_v1",
        "business_id": integration.business_id, "branch_id": integration.branch_id,
        "ready": ready, "checked_at": checked_at}


@router.post("/work/claim")
def claim_work(idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("agent:state")), db=Depends(get_db)):
    return claim_work_for_contract(idempotency_key, integration, db, native=False)


@router.post("/native/work/claim")
def claim_native_work(idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("agent:state")), db=Depends(get_db)):
    return claim_work_for_contract(idempotency_key, integration, db, native=True)


def contract_match(value, native):
    # Missing markers are historical 449 work. Only the server writes native_v1.
    return value == batching.NATIVE_CONTRACT if native else or_(value.is_(None), value == batching.LEGACY_CONTRACT)


def work_candidates_query(integration, *, native, evaluated_at):
    # Accepted inputs are durable records, so workers can recover after restarts
    # without holding a list of chat IDs in memory.
    accepted = exists(select(IdempotencyRecord.id).where(
        IdempotencyRecord.business_id == AgentConversation.business_id,
        IdempotencyRecord.scope == "agent-input:" + AgentConversation.id,
        IdempotencyRecord.response_body["status"].as_string() == "accepted"))
    return select(AgentConversation).where(
        AgentConversation.business_id == integration.business_id,
        AgentConversation.branch_id == integration.branch_id, accepted,
        or_(and_(AgentConversation.current_message_id.is_not(None), contract_match(
                AgentConversation.state["_processing_message"]["_runtime_contract"].as_string(), native)),
            and_(AgentConversation.current_message_id.is_(None), contract_match(
                AgentConversation.pending[0]["_runtime_contract"].as_string(), native))),
        or_(AgentConversation.lease_until.is_(None), AgentConversation.lease_until <= evaluated_at))


def claim_work_for_contract(idempotency_key, integration, db, *, native):
    require_key(idempotency_key)
    rows = db.scalars(work_candidates_query(integration, native=native, evaluated_at=utcnow())
        .order_by(AgentConversation.updated_at, AgentConversation.id).with_for_update(skip_locked=True).limit(30)).all()
    # A quiet-window wait in one chat must not stall a ready, unrelated chat.
    for row in rows:
        if row.current_message_id or batching.wait_seconds(row.pending or [], utcnow()) <= 0:
            return acquire_pending(db, row, native=native, allow_order_reference="orders:read" in integration.scopes)
    if rows:
        wait = min(batching.wait_seconds(row.pending or [], utcnow()) for row in rows)
        db.commit()
        return {"status": "queued", "retry_after_ms": max(1, round(wait * 1000))}
    db.commit()
    return {"status": "idle"}


@router.post("/outbox/claim")
def claim_delivery(background_tasks: BackgroundTasks, idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("agent:state")), db=Depends(get_db)):
    return claim_delivery_for_contract(idempotency_key, integration, db, native=False, background_tasks=background_tasks)


@router.post("/native/outbox/claim")
def claim_native_delivery(background_tasks: BackgroundTasks, idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("agent:state")), db=Depends(get_db)):
    return claim_delivery_for_contract(idempotency_key, integration, db, native=True, background_tasks=background_tasks)


def claim_delivery_for_contract(idempotency_key, integration, db, *, native, background_tasks=None):
    require_key(idempotency_key)
    scope = ("agent-native-outbox-claim:" if native else "agent-outbox-claim:") + str(integration.branch_id)
    previous = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.business_id == integration.business_id,
        IdempotencyRecord.scope == scope, IdempotencyRecord.idempotency_key == idempotency_key))
    if previous:
        # A lost claim response is uncertain. Never authorize a second dispatch.
        return {"status": "uncertain", "delivery_id": previous.response_body["delivery_id"]}
    row = db.scalar(delivery_candidates_query(integration, native=native)
        .order_by(AgentMessageDelivery.created_at, AgentMessageDelivery.id).with_for_update(skip_locked=True).limit(1))
    if not row:
        return {"status": "idle"}
    if suppress_stale_delivery(db, row, background_tasks=background_tasks):
        db.commit()
        return {"status": "suppressed", "delivery_id": row.id}
    token = secrets.token_urlsafe(32)
    row.status, row.claimed_at = "uncertain", utcnow()
    row.claim_hash = hashlib.sha256(token.encode()).hexdigest()
    db.add(IdempotencyRecord(business_id=row.business_id, scope=scope, idempotency_key=idempotency_key,
        response_body={"delivery_id": row.id}))
    db.commit()
    return {"status": "claimed", "delivery_id": row.id, "delivery_token": token,
        "conversation_id": row.conversation_id, "message_id": row.message_id,
        "sender": row.sender, "response": row.response}


def delivery_candidates_query(integration, *, native):
    return select(AgentMessageDelivery).where(
        AgentMessageDelivery.business_id == integration.business_id,
        AgentMessageDelivery.branch_id == integration.branch_id, AgentMessageDelivery.status == "pending",
        contract_match(AgentMessageDelivery.response["_runtime_contract"].as_string(), native))


def suppress_stale_delivery(db, delivery, *, background_tasks=None):
    if isinstance(delivery.response, dict) and "_pos_notice" in delivery.response:
        from .agent_event_outbox import suppress_stale_notice
        return suppress_stale_notice(db, delivery, background_tasks=background_tasks)
    if not batching.is_native(delivery.response):
        return False
    conversation = db.scalar(select(AgentConversation).where(
        AgentConversation.id == delivery.conversation_id,
        AgentConversation.business_id == delivery.business_id,
        AgentConversation.branch_id == delivery.branch_id).with_for_update())
    if not conversation:
        raise HTTPException(409, "Response conversation is unavailable")
    sequence = delivery.response.get("_input_sequence")
    latest = (conversation.state or {}).get("_transport", {}).get("input_sequence", 0)
    # Historical outputs lack this server-owned sequence; never infer freshness
    # from their text, an approximate timestamp, or a caller supplied marker.
    if not isinstance(sequence, int) or latest <= sequence:
        return False
    if batching.stale_output(delivery.response, sequence, latest) and not any(delivery.response.get(k) for k in ("_batch_limit_notice", "_media_failure_notice")):
        delivery.status = "suppressed"
        delivery.receipt = {"reason": "superseded", "transport_attempted": False}
        return True
    receipts = delivery.response.get("_receipt_actions")
    if isinstance(receipts, list) and receipts:
        effective = {**delivery.response, "actions": deepcopy(receipts)}
        notice = effective.pop("_original_order_notice", None)
        if isinstance(notice, dict):
            shadow = SimpleNamespace(business_id=delivery.business_id, branch_id=delivery.branch_id,
                sender=delivery.sender, current_message_id=delivery.message_id)
            policy_state = {"order_id": notice.get("order_id"), "original_order_explanation_candidate": {
                "kind": notice.get("kind"), "order_id": notice.get("order_id"), "message_id": delivery.message_id}}
            effective = annotate_policy_response(db, shadow, policy_state, effective)
        delivery.response = effective
    return False


PAYMENT_INSTRUCTION_NOTICE = ("No pude comprobar las instrucciones de pago vigentes. "
    "No adjunté el QR ni los datos anteriores para evitar un pago equivocado. "
    "No repitas un pago; consulta su estado con el encargado.")
PAYABLE_INSTRUCTION_STATUSES = frozenset({"pending", "rejected", "awaiting_base_review"})


def registered_payment_actions(response):
    return [action for key in ("actions", "_receipt_actions")
        for action in (response.get(key) if isinstance(response.get(key), list) else [])
        if isinstance(action, dict) and "registered_payment_ref" in action]


def checked_payment_reference(db, delivery, action):
    """Read current own POS rows using the authenticated outbox claim, never a lease bypass."""
    ref = action.get("registered_payment_ref")
    if (not isinstance(ref, dict) or type(ref.get("version")) is not int or ref["version"] != 1
            or type(ref.get("order_id")) is not int or ref["order_id"] <= 0
            or not isinstance(ref.get("request_id"), str) or not 1 <= len(ref["request_id"]) <= 36
            or action.get("type") not in {"private_yape_qr", "private_yape_instructions"}):
        return None
    if (ref.get("business_id"), ref.get("branch_id"), ref.get("sender")) != (delivery.business_id, delivery.branch_id, delivery.sender):
        return None
    if ref.get("purpose") not in {"addition", "delivery"} or ref.get("method") != "yape" or ref.get("status") not in PAYABLE_INSTRUCTION_STATUSES:
        return None
    order = db.scalar(select(Order).where(Order.id == ref.get("order_id"),
        Order.business_id == delivery.business_id, Order.branch_id == delivery.branch_id))
    if not order:
        return None
    try:
        assert_sender(order, delivery.sender)
    except HTTPException:
        return None
    request = db.scalar(select(OrderPaymentRequest).where(OrderPaymentRequest.id == ref.get("request_id"),
        OrderPaymentRequest.order_id == order.id, OrderPaymentRequest.business_id == delivery.business_id,
        OrderPaymentRequest.branch_id == delivery.branch_id))
    source = ref.get("source_message_id")
    if not isinstance(source, str) or not source:
        return None
    source_record = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.business_id == delivery.business_id,
        IdempotencyRecord.scope == f"agent-input:{delivery.conversation_id}", IdempotencyRecord.idempotency_key == source))
    stage = "addition" if ref["purpose"] == "addition" else "delivery_payment"
    expected_key = "restaurant-v1:" + hashlib.sha256(json.dumps([delivery.sender, source, stage],
        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    if not source_record or ref.get("source_operation_key") != expected_key:
        return None
    operation_scope = ("agent-addition:" if ref["purpose"] == "addition" else "agent-delivery-method:") + str(order.id)
    operation = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.business_id == delivery.business_id,
        IdempotencyRecord.scope == operation_scope, IdempotencyRecord.idempotency_key == expected_key))
    result = (operation.response_body or {}).get("result", {}) if operation else {}
    original_request = result.get("payment_request") or {}
    if (not request or str(original_request.get("id")) != str(request.id)
            or result.get("order", {}).get("id") != order.id or original_request.get("purpose") != ref["purpose"]):
        return None
    return order, request, ref


def validate_payment_instructions(db, delivery, *, unavailable=False):
    """Only technical outbox data changes; orders, requests, evidence and money are read-only."""
    response = deepcopy(delivery.response)
    actions = registered_payment_actions(response)
    if not actions:
        if response.get("_registered_payment_withheld"):
            return {"version": 1, "delivery_id": delivery.id, "status": "withheld", "references": []}
        return None
    valid, receipts = not unavailable and not response.get("_registered_payment_withheld"), []
    for action in actions:
        checked = checked_payment_reference(db, delivery, action)
        if not checked:
            valid = False
            continue
        order, request, ref = checked
        try:
            ensure_request_upload(db, order, request)
        except HTTPException:
            valid = False
        label = "adicional" if request.purpose == "addition" else "del envío"
        receipt = f"La solicitud de pago {label} del pedido #{order.folio or order.number} quedó registrada. Esto no confirma el pago."
        if receipt not in receipts:
            receipts.append(receipt)
        branch = db.scalar(select(Branch).where(Branch.id == delivery.branch_id, Branch.business_id == delivery.business_id))
        business = db.scalar(select(Business).where(Business.id == delivery.business_id))
        settings = db.scalar(select(BranchSettings).where(BranchSettings.branch_id == delivery.branch_id))
        methods = (settings.payment_methods or {}).get(order.channel, []) if settings else (branch.accepted_payment_methods or []) if branch else []
        payment = action.get("payment") if isinstance(action.get("payment"), dict) else {}
        try:
            amount = Decimal(str(ref.get("amount")))
            payment_amount = Decimal(str(payment.get("amount")))
            amount_matches = amount.is_finite() and amount >= 0 and amount == request.amount == payment_amount
        except (InvalidOperation, TypeError, ValueError):
            amount_matches = False
        if (not branch or not business or "yape" not in methods
                or not isinstance(ref.get("currency"), str) or ref["currency"] != payment.get("currency") or ref["currency"] != business.currency
                or request.method != ref["method"] or request.purpose != ref["purpose"]
                or request.status != ref["status"] or request.status not in PAYABLE_INSTRUCTION_STATUSES
                or not amount_matches or payment.get("purpose") != request.purpose
                or not branch.yape_number or not branch.payment_recipient_name
                or payment.get("number") != branch.yape_number or payment.get("recipient_name") != branch.payment_recipient_name
                or action.get("yape_revision") != yape_revision(branch)
                or (action.get("type") == "private_yape_qr" and not branch.yape_qr_storage_path)):
            valid = False
    if not valid:
        # Do not retain arbitrary prose/captions that could expose an old payee.
        # Rebuild the operation acknowledgement only from verified domain rows.
        text = "\n".join([*receipts, PAYMENT_INSTRUCTION_NOTICE])
        safe = [{"type": "text", "text": text}]
        response.update(actions=safe, _receipt_actions=deepcopy(safe), _registered_payment_withheld=True)
        response.pop("_original_order_notice", None)  # Its explanation was removed, so it cannot earn an ACK.
        delivery.response = response
    return {"version": 1, "delivery_id": delivery.id, "status": "validated" if valid else "withheld",
        "references": [deepcopy(action["registered_payment_ref"]) for action in response.get("actions", [])
            if isinstance(action, dict) and "registered_payment_ref" in action] if valid else []}


@router.post("/outbox/{delivery_id}/freshness")
def delivery_freshness(delivery_id: str, payload: DeliveryCheck, background_tasks: BackgroundTasks,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("agent:state")), db=Depends(get_db)):
    require_key(idempotency_key)
    row = db.scalar(select(AgentMessageDelivery).where(AgentMessageDelivery.id == delivery_id,
        AgentMessageDelivery.business_id == integration.business_id,
        AgentMessageDelivery.branch_id == integration.branch_id).with_for_update())
    if not row or not row.claim_hash or not secrets.compare_digest(
            row.claim_hash, hashlib.sha256(payload.delivery_token.encode()).hexdigest()):
        raise HTTPException(409, "Response claim is stale")
    if row.status == "suppressed":
        return {"status": "superseded"}
    if row.status != "uncertain":
        raise HTTPException(409, "Response is not awaiting its first transport attempt")
    suppressed = suppress_stale_delivery(db, row, background_tasks=background_tasks)
    validation = None if suppressed else validate_payment_instructions(db, row, unavailable=payload.payment_instruction_unavailable)
    db.commit()
    return {"status": "superseded"} if suppressed else {"status": "current", "response": row.response,
        **({"payment_instruction_validation": validation} if validation else {})}


@router.patch("/outbox/{delivery_id}/ack")
def ack_delivery(delivery_id: str, payload: DeliveryAck, background_tasks: BackgroundTasks,
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
    if payload.status == "sent" and isinstance(row.response, dict) and "_pos_notice" in row.response:
        from .agent_event_outbox import acknowledge_notice_sources
        acknowledge_notice_sources(db, row)
    if payload.status == "uncertain" and isinstance(row.response, dict) and "_pos_notice" in row.response:
        from .agent_event_outbox import request_notice_delivery_review
        request_notice_delivery_review(db, row, background_tasks=background_tasks)
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
    return acquire_pending(db, scoped_conversation(db, integration, conversation_id), native=False)


@router.post("/native/{conversation_id}/next")
def next_native_input(conversation_id: str,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("agent:state")), db=Depends(get_db)):
    require_key(idempotency_key)
    return acquire_pending(db, scoped_conversation(db, integration, conversation_id), native=True,
        allow_order_reference="orders:read" in integration.scopes)


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


@router.post("/{conversation_id}/freshness")
def input_freshness(conversation_id: str, payload: Renew,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("agent:state")), db=Depends(get_db)):
    require_key(idempotency_key)
    row = scoped_conversation(db, integration, conversation_id)
    if not lease_valid(row, payload.lease_token) or row.version != payload.expected_version:
        raise HTTPException(409, "Conversation lease or version is stale")
    result = {"status": "superseded" if batching.superseded(row) else "current",
        "version": row.version, "pending_count": len(row.pending or [])}
    db.commit()
    return result


@router.post("/{conversation_id}/defer")
def defer_input(conversation_id: str, payload: Renew,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("agent:state")), db=Depends(get_db)):
    require_key(idempotency_key)
    row = scoped_conversation(db, integration, conversation_id)
    scope, digest = "agent-defer:" + row.id, fingerprint(payload.model_dump())
    old = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.business_id == row.business_id,
        IdempotencyRecord.scope == scope, IdempotencyRecord.idempotency_key == idempotency_key))
    if old:
        if old.response_body.get("digest") != digest:
            raise HTTPException(409, "Idempotency key already used with other data")
        return old.response_body["result"]
    if not lease_valid(row, payload.lease_token) or row.version != payload.expected_version:
        raise HTTPException(409, "Conversation lease or version is stale")
    active = deepcopy((row.state or {}).get("_processing_message", {}))
    if not batching.superseded(row) or "base_state" not in active:
        raise HTTPException(409, "Only an unexecuted, superseded input can be deferred")
    transport = deepcopy((row.state or {}).get("_transport", {}))
    row.state = {**active.pop("base_state"), "_transport": transport}
    attempt = input_record(db, row, active["message_id"])
    if attempt and attempt.response_body.get("internal_batch"):
        attempt.response_body = {**attempt.response_body, "status": "superseded"}
    active["defer_count"] = active.get("defer_count", 0) + 1
    exhausted = active["defer_count"] > batching.MAX_DEFER
    if exhausted:
        # A continuously changing stream cannot cause unbounded model dispatch.
        # Retain the exact unexecuted texts as context for the next accepted
        # message, without inventing a draft selection or a purchase consent.
        history = list(row.state.get("recent_messages", []))
        known = {m.get("id") for m in history}
        history.extend(deepcopy(m) for m in active["message"].get("messages", []) if m.get("id") not in known)
        row.state = {**row.state, "recent_messages": history[-30:]}
        for input_id in active.get("input_ids", [active["message_id"]]):
            record = input_record(db, row, input_id)
            record.response_body = {**record.response_body, "status": "completed", "response": {"actions": [],
                "turn": {"id": active["message_id"], "outcome": "none"}}}
        previous_notice = batching.timestamp(transport.get("batch_limit_notice_at"))
        if previous_notice is None or (utcnow() - previous_notice).total_seconds() >= 60:
            notice = {"actions": [{"type": "text", "text": "Recibí varios cambios seguidos. No ejecuté una operación con la elección anterior. Revisaré tu último mensaje para continuar."}],
                "turn": {"id": active["message_id"], "outcome": "none"}, "_batch_limit_notice": True,
                "_input_sequence": transport.get("input_sequence", 0), "_runtime_contract": batching.NATIVE_CONTRACT}
            db.add(AgentMessageDelivery(business_id=row.business_id, branch_id=row.branch_id,
                conversation_id=row.id, message_id=active["message_id"], sender=row.sender, response=notice))
            transport["batch_limit_notice_at"] = utcnow().isoformat()
            row.state = {**row.state, "_transport": transport}
    else:
        row.pending = [active, *(row.pending or [])]
    row.current_message_id, row.lease_hash, row.lease_until = None, None, None
    row.version += 1
    result = {"status": "deferred", "version": row.version,
        "pending_count": len(row.pending or []), "defer_exhausted": exhausted}
    db.add(IdempotencyRecord(business_id=row.business_id, scope=scope, idempotency_key=idempotency_key,
        response_body={"digest": digest, "result": result}))
    db.commit()
    return result


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
    from .agent_public_context import validate_consumer_context
    validate_consumer_context(db, row, payload.state)
    if batching.superseded(row):
        raise HTTPException(409, {"code": "INPUT_SUPERSEDED",
            "message": "Newer consumer input must be considered before this operation."})
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
        public_response = {k: deepcopy(v) for k, v in (payload.response or {}).items() if not k.startswith("_")}
        completed_ids = set((processing or {}).get("input_ids", [row.current_message_id])) | {row.current_message_id}
        for input_id in completed_ids:
            record = input_record(db, row, input_id)
            record.response_body = {**record.response_body, "status": "completed", "response": public_response}
        if payload.response and payload.response.get("actions"):
            effective = {k: deepcopy(v) for k, v in payload.response.items()
                if k not in {"_runtime_contract", "_input_sequence", "_batch_limit_notice", "_media_failure_notice"}}
            if (batching.is_native(processing) and (transport or {}).get("input_sequence", 0) > (processing or {}).get("input_sequence", 0)
                    and effective.get("turn", {}).get("outcome") in {"committed", "uncertain"}
                    and isinstance(effective.get("_receipt_actions"), list) and effective["_receipt_actions"]):
                effective["actions"] = deepcopy(effective["_receipt_actions"])
            delivery_response = annotate_policy_response(db, row, row.state, effective)
            if batching.is_native(processing):
                delivery_response = {**delivery_response, "_runtime_contract": batching.NATIVE_CONTRACT,
                    "_input_sequence": (processing or {}).get("input_sequence", 0)}
            db.add(AgentMessageDelivery(business_id=row.business_id, branch_id=row.branch_id,
                conversation_id=row.id, message_id=row.current_message_id, sender=row.sender,
                response=deepcopy(delivery_response)))
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
