"""Read current restaurant, catalog and customer order in one fenced request.

This bundles existing serializers; it does not authorize an operation, extend a
lease, cache commercial data, or hold a transaction across a model call.
"""
from dataclasses import replace

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import Field
from sqlalchemy import select

from .auth import IntegrationAuthContext, require_integration_scope
from .database import get_db
from .models import Order
from .agent_runtime_api import Renew, scoped_conversation, lease_valid
from . import agent_message_batching as batching
from .agent_contracts import TurnContextResponse

router = APIRouter(prefix="/api/v1/integrations/conversations", tags=["agent-conversations"])
TURN_CONTEXT_SCOPES = frozenset({"agent:state", "menu:read", "orders:read"})


class TurnContextRead(Renew):
    order_id: int | None = Field(default=None, ge=1, strict=True)
    known_catalog_version: str | None = Field(default=None, pattern="^[a-f0-9]{64}$")


@router.post("/{conversation_id}/turn-context", response_model=TurnContextResponse, response_model_exclude_unset=True)
def turn_context(conversation_id: str, payload: TurnContextRead, response: Response,
    integration: IntegrationAuthContext = Depends(require_integration_scope("agent:state", read_only=True)),
    db=Depends(get_db)):
    if integration.profile != "restaurant_agent" or integration.legacy or not TURN_CONTEXT_SCOPES.issubset(integration.scopes):
        raise HTTPException(403, "A restaurant agent with all turn-context read scopes is required")
    row = scoped_conversation(db, integration, conversation_id)
    if not lease_valid(row, payload.lease_token) or row.version != payload.expected_version:
        raise HTTPException(409, "Conversation lease or version is stale")
    active = (row.state or {}).get("_processing_message", {})
    if not row.current_message_id or active.get("message_id") != row.current_message_id:
        raise HTTPException(409, "A currently processing consumer message is required")
    if batching.superseded(row):
        raise HTTPException(409, {"code": "INPUT_SUPERSEDED", "message": "Newer consumer input must be considered."})

    # The conversation identifies the consumer. No request-supplied sender or
    # business/branch header can select another consumer or tenant.
    bound = replace(integration, sender=row.sender, conversation_id=row.id)
    from .agent_public_context import validate_consumer_context
    saved = validate_consumer_context(db, row, row.state or {})
    order_id = payload.order_id if payload.order_id is not None else saved.order_id if saved else None
    order = None
    if order_id is not None:
        allowed = db.scalar(select(Order.id).where(Order.id == order_id,
            Order.business_id == row.business_id, Order.branch_id == row.branch_id))
        if allowed is None:
            raise HTTPException(404, "Order not found")
        from .agent_checkout_api import customer_state
        order = customer_state(order_id, row.sender, response, integration=bound, db=db)

    from .api import integration_context
    from .agent_checkout_api import catalog
    context = integration_context(response, branch_id=None, integration=bound, db=db)
    menu = catalog(response, integration=bound, db=db)
    from .agent_public_context import public_consumer_context
    public_context = public_consumer_context(db, row, menu, context)
    unchanged = payload.known_catalog_version == menu["catalog_version"]
    response.headers["Cache-Control"] = "private, no-store"
    return {"protocol_version": 1, "business_id": row.business_id, "branch_id": row.branch_id,
        "conversation_id": row.id, "message_id": row.current_message_id, "version": row.version,
        "context": context, "catalog": None if unchanged else menu, "catalog_version": menu["catalog_version"],
        "catalog_unchanged": unchanged, "order": order, "conversation_context": public_context}
