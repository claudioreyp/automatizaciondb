"""Server delivery quotations and trusted LID-to-phone binding, using isolated HTTP."""
from datetime import timedelta
from decimal import Decimal
import httpx
import pytest
from sqlalchemy import select
from app.database import SessionLocal
from app.models import AgentConversation, BranchSettings, DeliveryBand, DeliveryQuote, Order, utcnow
from test_restaurant_agent_boundary import agent_headers, leased, SENDER

DESTINATION = {"address": "Calle de prueba 123", "reference": "Frente al parque", "neighborhood": "Centro"}
ORIGIN = {"latitude": -12.1, "longitude": -77.1, "maps_url": "https://maps.google.com/?q=-12.1,-77.1"}


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("These tests may not call external network services")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)


def settings(tenant, mode):
    with SessionLocal.begin() as db:
        db.add(BranchSettings(business_id=tenant["business_id"], branch_id=tenant["branch_id"],
            pos_delivery=False, digital_delivery=True, delivery_mode=mode, fixed_delivery_fee=4,
            distance_base_fee=2, distance_fee_per_km=3, distance_max_km=10,
            delivery_policy={"origin": ORIGIN, "neighborhoods": [{"name": "Centro", "fee": 8}], "outside_band_mode": "quote"},
            payment_methods={"takeaway": ["cash", "yape"], "delivery": ["cash", "yape"]}))
        if mode == "bands":
            db.add(DeliveryBand(business_id=tenant["business_id"], branch_id=tenant["branch_id"],
                minimum_km=0, maximum_km=3, fee=7))


def preview(client, headers, tenant, **extra):
    return client.post("/api/v1/integrations/orders/preview", headers={**headers, "Idempotency-Key": "delivery-preview"},
        json={"channel": "delivery", "payment_method": "cash", "items": [{"product_id": tenant["product_id"], "quantity": 1}], **extra})


def draft_body(tenant, quoted, sender=SENDER, phone=SENDER):
    return {"branch_id": tenant["branch_id"], "channel": "delivery", "payment_method": "cash",
        "customer_phone": phone, "whatsapp_chat_id": sender, "delivery_address": DESTINATION,
        "delivery_quote_id": quoted.get("delivery_quote_id"), "quoted_total": quoted["known_total"],
        "delivery_fee": quoted["delivery_fee"] or 0, "items": [{"product_id": tenant["product_id"], "quantity": 1}]}


@pytest.mark.parametrize("mode", ["distance", "bands", "neighborhoods"])
def test_variable_delivery_quotes_staff_destination_before_checkout(client, tenant, agent_headers, monkeypatch, mode):
    settings(tenant, mode)
    from app.settings_service import GoogleRoutesDistanceProvider
    calls = []
    def route(self, origin, destination):
        calls.append(dict(destination))
        return Decimal(2)
    monkeypatch.setattr(GoogleRoutesDistanceProvider, "distance_km", route)
    _, headers = leased(client, agent_headers)
    first = preview(client, headers, tenant, delivery_address=DESTINATION)
    assert first.status_code == 200, first.text
    quoted = first.json()
    # Candidate443 policy: these configured modes use staff quotation only for
    # restaurant_agent. Original automatic-price expectations remain in the
    # preserved pre443 source and initial-local FAIL report.
    assert quoted["delivery_fee"] is None and quoted["final_total"] is None
    assert quoted["known_total"] == 20 and quoted["delivery_fee_status"] == "pending_quote"
    assert preview(client, headers, tenant, delivery_address=DESTINATION).json() == quoted
    assert calls == []
    response = client.post("/api/v1/integrations/orders/draft", headers={**headers, "Idempotency-Key": "draft"},
        json={**draft_body(tenant, quoted), "allow_pending_delivery_quote": True})
    assert response.status_code == 201, response.text
    assert response.json()["delivery_fee"] == 0 and response.json()["delivery_quote_id"] == quoted["delivery_quote_id"]
    assert response.json()["delivery_fee_status"] == "pending_quote"
    with SessionLocal() as db:
        assert db.query(DeliveryQuote).count() == 1 and db.query(Order).count() == 1


def test_missing_destination_remains_staff_quote_and_cannot_register_without_quote(client, tenant, agent_headers):
    settings(tenant, "neighborhoods")
    _, headers = leased(client, agent_headers)
    response = preview(client, headers, tenant)
    assert response.status_code == 200, response.text
    assert response.json()["delivery_fee_status"] == "pending_quote"
    assert response.json()["final_total"] is None and response.json()["delivery_quote_id"] is None
    rejected = client.post("/api/v1/integrations/orders/draft", headers={**headers, "Idempotency-Key": "no-quote"}, json=draft_body(tenant, response.json()))
    assert rejected.status_code == 409 and rejected.json()["code"] == "DELIVERY_QUOTE_REQUIRED"
    with SessionLocal() as db:
        assert db.query(Order).count() == db.query(DeliveryQuote).count() == 0


@pytest.mark.parametrize("tamper", ["destination", "configuration", "expiry", "subtotal", "sender", "context"])
def test_checkout_quote_cannot_be_reused_with_changed_inputs(client, tenant, agent_headers, tamper):
    settings(tenant, "neighborhoods")
    _, headers = leased(client, agent_headers)
    quoted = preview(client, headers, tenant, delivery_address=DESTINATION).json()
    body = draft_body(tenant, quoted)
    if tamper == "destination":
        body["delivery_address"] = {**DESTINATION, "address": "Otra calle 456"}
    elif tamper == "subtotal":
        body["items"][0]["quantity"] = 2
    elif tamper == "sender":
        second = client.post("/api/v1/integrations/conversations/claim", headers={**agent_headers, "Idempotency-Key": "another-consumer"},
            json={"sender": "51999900002", "message_id": "another", "message": {"text": "hola"}}).json()
        headers = {**agent_headers, "X-Agent-Conversation": second["conversation_id"], "X-Agent-Lease": second["lease_token"]}
        body.update(customer_phone="51999900002", whatsapp_chat_id="51999900002")
    else:
        with SessionLocal.begin() as db:
            if tamper == "configuration":
                db.scalar(select(BranchSettings)).version += 1
            elif tamper == "context":
                quote = db.get(DeliveryQuote, quoted["delivery_quote_id"])
                quote.input_snapshot = {**quote.input_snapshot, "context": "legacy", "pos_strict": False}
            else:
                db.get(DeliveryQuote, quoted["delivery_quote_id"]).expires_at = utcnow() - timedelta(seconds=1)
    result = client.post("/api/v1/integrations/orders/draft", headers={**headers, "Idempotency-Key": "invalid-draft"}, json=body)
    assert result.status_code in {404, 409}, result.text
    with SessionLocal() as db:
        assert db.query(Order).count() == 0


def test_unknown_destination_is_pending_staff_quote_even_above_free_threshold(client, tenant, agent_headers):
    settings(tenant, "neighborhoods")
    with SessionLocal.begin() as db:
        db.scalar(select(BranchSettings)).free_delivery_threshold = 1
    _, headers = leased(client, agent_headers)
    destination = {**DESTINATION, "neighborhood": "Fuera de la lista"}
    quoted = preview(client, headers, tenant, delivery_address=destination).json()
    assert quoted["delivery_fee_status"] == "pending_quote" and quoted["delivery_fee"] is None
    body = {**draft_body(tenant, quoted), "delivery_address": destination, "allow_pending_delivery_quote": True}
    created = client.post("/api/v1/integrations/orders/draft", headers={**headers, "Idempotency-Key": "pending"}, json=body)
    assert created.status_code == 201, created.text
    assert created.json()["delivery_fee_status"] == "pending_quote" and created.json()["final_total"] is None


@pytest.mark.parametrize("verified", [True, False])
def test_lid_uses_transport_verified_phone_and_never_model_state(client, tenant, agent_headers, verified):
    lid = "123456789012345@lid"
    message = {"text": "una pizza para recoger", **({"verified_phone": "+" + SENDER} if verified else {})}
    lease = client.post("/api/v1/integrations/conversations/claim", headers={**agent_headers, "Idempotency-Key": "lid"},
        json={"sender": lid, "message_id": "lid1", "message": message}).json()
    assert "verified_phone" not in lease["message"] and "_transport" not in lease["state"]
    assert lease["verified_phone"] == (SENDER if verified else None)
    headers = {**agent_headers, "X-Agent-Conversation": lease["conversation_id"], "X-Agent-Lease": lease["lease_token"]}
    body = {"branch_id": tenant["branch_id"], "channel": "takeaway", "payment_method": "cash", "quoted_total": 20,
        "whatsapp_chat_id": lid, "customer_phone": SENDER, "items": [{"product_id": tenant["product_id"], "quantity": 1}]}
    created = client.post("/api/v1/integrations/orders/draft", headers={**headers, "Idempotency-Key": "lid-order"}, json=body)
    assert created.status_code == (201 if verified else 409), created.text
    commit = {"lease_token": lease["lease_token"], "expected_version": lease["version"], "state": {"_transport": {"verified_phone": "51999900999"}}}
    denied = client.post(f"/api/v1/integrations/conversations/{lease['conversation_id']}/commit",
        headers={**agent_headers, "Idempotency-Key": "tamper"}, json=commit)
    assert denied.status_code == 422
    changed = client.post("/api/v1/integrations/conversations/claim", headers={**agent_headers, "Idempotency-Key": "lid2"},
        json={"sender": lid, "message_id": "lid2", "message": {"text": "hola", "verified_phone": "51999900999"}})
    assert changed.status_code == (409 if verified else 200), changed.text
    with SessionLocal() as db:
        assert db.query(Order).count() == (1 if verified else 0)
