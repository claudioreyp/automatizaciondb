"""Read-only recovery of an explicit order reference for an empty native chat."""
import re

from fastapi import HTTPException
from sqlalchemy import func, select

from .agent_checkout import assert_sender
from .models import Order

REFERENCE = re.compile(
    r"\b(?:pedido|orden)\s+(?:n(?:[úu]mero|[°º.]?)\s*|#\s*)?"
    r"([A-Z0-9][A-Z0-9-]{1,39})(?![A-Z0-9-])", re.IGNORECASE | re.ASCII
)


def explicit_references(message):
    messages = message.get("messages") or []
    found = []
    for source in messages:
        if not isinstance(source, dict) or not isinstance(source.get("id"), str):
            continue
        text = source.get("text")
        if not isinstance(text, str):
            continue
        for match in REFERENCE.finditer(text):
            number = match.group(1).upper()
            if any(character.isdigit() for character in number):
                found.append((number, source["id"]))
    return found


def recover_order_reference(db, conversation, active):
    state = conversation.state or {}
    public = {key: value for key, value in state.items()
              if not key.startswith("_processing_")
              and key not in {"_transport", "_operation_identity"}}
    if public:
        return None
    references = explicit_references(active.get("message") or {})
    if not references:
        return None
    # Several distinct references require clarification, even if one exists.
    unique = {reference for reference, _ in references}
    first, source_id = references[0]
    result = {"version": 1, "status": "unresolved",
              "business_id": conversation.business_id,
              "branch_id": conversation.branch_id,
              "source_message_id": source_id, "reference": first,
              "order_id": None, "order_number": None}
    if len(unique) != 1:
        return result
    verified = state.get("_transport", {}).get("verified_phone")
    if not verified:
        return result
    candidates = db.scalars(select(Order).where(
        Order.business_id == conversation.business_id,
        Order.branch_id == conversation.branch_id,
        func.upper(Order.number) == first).limit(2)).all()
    if len(candidates) != 1:
        return result
    order = candidates[0]
    try:
        assert_sender(order, conversation.sender)
        assert_sender(order, verified)
    except HTTPException:
        return result
    return {**result, "status": "matched", "order_id": order.id,
            "order_number": order.number}
