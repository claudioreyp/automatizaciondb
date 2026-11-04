"""A quote's instant must survive PostgreSQL session timezone conversion."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event

from app import agent_checkout_api
from app.database import SessionLocal
from app.models import AgentAttentionRequest, DeliveryQuote, Order
from test_agent_domain_revision import DESTINATION, PHONE, setup_order


@pytest.mark.parametrize("representation", ["utc", "minus_five", "naive_utc"])
@pytest.mark.parametrize("seconds_until_expiry", [1, 0, -1])
def test_fulfillment_quote_expiry_preserves_instant_and_exact_boundary(
    client, tenant, auth_headers, monkeypatch, representation, seconds_until_expiry
):
    headers, order = setup_order(client, tenant, auth_headers, fee=6)
    body = {"sender": PHONE, "expected_version": order["version"], "channel": "delivery",
            "delivery_address": DESTINATION}
    path = f"/api/v1/integrations/orders/{order['id']}"
    response = client.post(path + "/fulfillment-preview",
        headers={**headers, "Idempotency-Key": "timezone-preview"}, json=body)
    assert response.status_code == 200, response.text
    quote = response.json()["quote"]
    now = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
    expiry = now + timedelta(seconds=seconds_until_expiry)
    if representation == "minus_five":
        expiry = expiry.astimezone(timezone(timedelta(hours=-5)))
    elif representation == "naive_utc":
        expiry = expiry.replace(tzinfo=None)
    monkeypatch.setattr(agent_checkout_api, "utcnow", lambda: now)

    # SQLite drops timezone information. Intercept the actual ORM load so the
    # same endpoint sees each PostgreSQL-aware or SQLite-naive representation.
    # No business validation is stubbed; the real preview, quote and commit run.
    observed = []

    def represent_loaded_quote(session, instance):
        if isinstance(instance, DeliveryQuote) and instance.id == quote["id"]:
            instance.expires_at = expiry
            observed.append(instance.expires_at)

    event.listen(SessionLocal.class_, "loaded_as_persistent", represent_loaded_quote)
    try:
        committed = client.patch(path + "/fulfillment",
            headers={**headers, "Idempotency-Key": "timezone-commit"},
            json={**body, "quote_id": quote["id"], "expected_delivery_fee": quote["fee"]})
    finally:
        event.remove(SessionLocal.class_, "loaded_as_persistent", represent_loaded_quote)
    assert observed and observed[0] == expiry

    with SessionLocal() as db:
        persisted = db.get(Order, order["id"])
        if seconds_until_expiry > 0:
            assert committed.status_code == 200, committed.text
            assert persisted.channel == "delivery" and persisted.total == 26
            assert committed.json()["order"]["total"] == 26
            assert db.query(AgentAttentionRequest).filter_by(kind="fulfillment_change").count() == 1
        else:
            assert committed.status_code == 409, committed.text
            assert committed.json()["code"] == "DELIVERY_QUOTE_EXPIRED"
            assert persisted.channel == "takeaway" and persisted.total == 20
            assert persisted.version == order["version"]
            assert db.query(AgentAttentionRequest).count() == 0
