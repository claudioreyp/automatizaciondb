"""Typed metadata only; never file contents, a payment or a human request."""
import re
from copy import deepcopy
from fastapi import HTTPException

KINDS = {"audioMessage", "videoMessage", "documentMessage", "imageMessage", "stickerMessage"}
SUPPORTED_IMAGES = {"image/png", "image/jpeg", "image/webp"}


def validate_unsupported_input(message, message_id):
    if "unsupported" not in message:
        return None
    value = message["unsupported"]
    fields = {"source_message_id", "kind"}
    if isinstance(value, dict) and "mime_type" in value:
        fields.add("mime_type")
    valid = (
        isinstance(value, dict) and set(value) == fields
        and isinstance(message_id, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", message_id)
        and "__UNBOUND" not in message_id
        and value.get("source_message_id") == message_id
        and isinstance(value.get("kind"), str) and value["kind"] in KINDS
        and ("mime_type" not in value or isinstance(value["mime_type"], str)
             and len(value["mime_type"]) <= 100 and not re.search(r"[\x00-\x1f\x7f]", value["mime_type"]))
        and (value["kind"] not in {"imageMessage", "stickerMessage"} or value.get("mime_type") not in SUPPORTED_IMAGES)
        and set(message) <= {"text", "messages", "unsupported", "verified_phone", "contact_display_name"}
        and message.get("text") == ""
        and message.get("messages") == [{"id": message_id, "text": ""}]
    )
    if not valid:
        raise HTTPException(422, "Unsupported input must contain only one original source ID and typed file metadata")
    return deepcopy(value)
