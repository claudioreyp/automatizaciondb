"""Pure bounds/order policy for the durable QR and Meta input queue.

Timestamps and sequence numbers are assigned by the API, never the transport.
Media and locations remain separate entries; only adjacent plain texts merge.
"""
from copy import deepcopy
from datetime import datetime, timezone

NATIVE_CONTRACT = "native_v1"
LEGACY_CONTRACT = "legacy_v1"


def is_native(entry):
    return isinstance(entry, dict) and entry.get("_runtime_contract") == NATIVE_CONTRACT


SILENCE_SECONDS = 0.7
MAX_WAIT_SECONDS = 5.0
MAX_SOURCES = 30
MAX_TEXT = 8000
MAX_DEFER = 2
MEDIA_WAIT_SECONDS = 45


def timestamp(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    return value.replace(tzinfo=timezone.utc) if isinstance(value, datetime) and value.tzinfo is None else value


def plain_text(entry):
    message = entry.get("message", {})
    return not entry.get("awaiting_media") and not message.get("media") and not message.get("location") and all(
        source.get("type", "text") == "text" for source in message.get("messages", []))


def prefix(pending):
    if not pending:
        return []
    group, sources, length = [], 0, 0
    first_at = timestamp(pending[0].get("accepted_at"))
    for entry in pending:
        if group and is_native(entry) != is_native(pending[0]):
            break
        entry_at = timestamp(entry.get("accepted_at"))
        if group and first_at and entry_at and (entry_at - first_at).total_seconds() >= MAX_WAIT_SECONDS:
            break
        if group and (not plain_text(pending[0]) or not plain_text(entry)):
            break
        count = len(entry.get("message", {}).get("messages", [])) or 1
        size = len(entry.get("message", {}).get("text", "")) + bool(group)
        if group and (sources + count > MAX_SOURCES or length + size > MAX_TEXT):
            break
        group.append(entry)
        sources += count
        length += size
        if not plain_text(entry):
            break
    return group


def wait_seconds(pending, now):
    if pending and pending[0].get("awaiting_media"):
        deadline = timestamp(pending[0]["awaiting_media"].get("expires_at"))
        return max(0.0, (deadline - now).total_seconds()) if deadline else 0.0
    group = prefix(pending)
    if not group or not plain_text(group[0]) or not group[0].get("batching"):
        return 0.0
    # A barrier or a bound closes this prefix immediately. Legacy entries that
    # predate this policy are already accepted work and must not be postponed.
    if len(group) < len(pending):
        return 0.0
    first, last = timestamp(group[0].get("accepted_at")), timestamp(group[-1].get("accepted_at"))
    if first is None or last is None:
        return 0.0
    if now < first:
        return 0.0  # A wall-clock rollback must not extend a durable wait.
    return max(0.0, min((last - now).total_seconds() + SILENCE_SECONDS,
                        (first - now).total_seconds() + MAX_WAIT_SECONDS))


def merge(group):
    active = deepcopy(group[0])
    active["input_ids"] = [value for entry in group for value in entry.get("input_ids", [entry["message_id"]])]
    active["input_sequence"] = max(entry.get("input_sequence", 0) for entry in group)
    active["defer_count"] = max(entry.get("defer_count", 0) for entry in group)
    if len(group) > 1:
        active["message"]["messages"] = [deepcopy(source) for entry in group
            for source in entry["message"].get("messages", [{"id": entry["message_id"], "text": entry["message"].get("text", "")}])]
        active["message"]["text"] = "\n".join(source["text"] for source in active["message"]["messages"])
        # Last provider profile belongs to this group, not a later queued turn.
        active["transport"] = deepcopy(group[-1].get("transport", {}))
    return active


def superseded(row):
    active = (row.state or {}).get("_processing_message", {})
    return bool(is_native(active) and row.pending and not active.get("execution_started") and any(
        entry.get("input_sequence", 0) > active.get("claim_sequence", active.get("input_sequence", 0))
        for entry in row.pending))


def stale_output(response, sequence, latest):
    return bool(response and response.get("turn", {}).get("outcome") == "none" and latest > sequence)


def trusted_input_created_at(db, conversation, message_id):
    """Earliest original receipt, never the time an internal attempt was made.

    The internal_batch marker lives in server-created idempotency metadata;
    consumer message fields and message-ID prefixes cannot set this marker.
    Missing or inconsistent provenance yields no proof.
    """
    from sqlalchemy import select
    from .models import IdempotencyRecord
    def record(ident):
        return db.scalar(select(IdempotencyRecord).where(
            IdempotencyRecord.business_id == conversation.business_id,
            IdempotencyRecord.scope == f"agent-input:{conversation.id}",
            IdempotencyRecord.idempotency_key == ident))
    current = record(message_id)
    if current is None:
        return None
    if current.response_body.get("internal_batch") is not True:
        return current.created_at
    ids = current.response_body.get("input_ids")
    if (not isinstance(ids, list) or not 1 <= len(ids) <= MAX_SOURCES
            or any(not isinstance(ident, str) or not ident or len(ident) > 240 for ident in ids)
            or len(set(ids)) != len(ids)):
        return None
    sources = [record(ident) for ident in ids]
    if any(source is None or source.response_body.get("internal_batch") is True for source in sources):
        return None
    return min(source.created_at for source in sources)
