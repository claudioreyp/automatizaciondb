"""Versioned consumer intent, never a source of prices, payment or order facts."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from fastapi import HTTPException
from sqlalchemy import select

from .agent_selection_schemas import PartialSelection
from .models import Order
from .agent_checkout import assert_sender


class ConsumerContextInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    intent: str | None = Field(default=None, max_length=500)
    selection: PartialSelection | None = None
    order_id: int | None = Field(default=None, ge=1, strict=True)


def validate_consumer_context(db, row, state):
    value = state.get("consumer_context")
    if value is None:
        return None
    try:
        parsed = ConsumerContextInput.model_validate(value)
    except ValidationError:
        raise HTTPException(422, {"code": "INVALID_CONSUMER_CONTEXT",
            "message": "Consumer context must contain only versioned intent, selection and order reference."})
    if parsed.order_id is not None:
        order = db.scalar(select(Order).where(Order.id == parsed.order_id,
            Order.business_id == row.business_id, Order.branch_id == row.branch_id))
        if order is None:
            raise HTTPException(404, "Order not found")
        assert_sender(order, row.sender)
    return parsed


def public_consumer_context(db, row, menu, context):
    from .agent_selection import validate_selection
    value = validate_consumer_context(db, row, row.state or {})
    return {"schema_version": 1, "intent": value.intent if value else None,
        "selection": value.selection.model_dump(mode="json") if value and value.selection else None,
        "order_id": value.order_id if value else None, "commercial_authority": "pos",
        "selection_validation": validate_selection(value.selection, menu,
            payment_methods=context["payments"]["methods"]) if value and value.selection else None}
