"""Checkout with a transport-authenticated WhatsApp user and no exposed phone.

All requests use the isolated ASGI database. No WhatsApp, payment provider or
physical printer is contacted.
"""
import httpx
import pytest
from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import BranchSettings, IntegrationEvent, KitchenTicket, Order, Payment
from test_restaurant_agent_boundary import agent_headers, commit, SENDER


LID = "123456789012345@lid"
OTHER_LID = "123456789012346@lid"


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("LID checkout tests may not contact external services")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)


def acquire(client, headers, *, sender=LID, key="lid-input", name="Ana", phone=None):
    message = {"text": "Una pizza para recoger", "contact_display_name": name}
    if phone is not None:
        message["verified_phone"] = phone
    response = client.post("/api/v1/integrations/conversations/claim",
        headers={**headers, "Idempotency-Key": key},
        json={"sender": sender, "message_id": key, "message": message})
    assert response.status_code == 200, response.text
    lease = response.json()
    assert lease["status"] == "acquired", response.text
    return lease, {**headers, "X-Agent-Conversation": lease["conversation_id"],
        "X-Agent-Lease": lease["lease_token"]}


def body(tenant, **values):
    return {"branch_id": tenant["branch_id"], "channel": "takeaway", "payment_method": "cash",
        "quoted_total": 20, "whatsapp_chat_id": LID, "customer_phone": None,
        "items": [{"product_id": tenant["product_id"], "quantity": 1}], **values}


def draft(client, headers, payload, key="lid-order"):
    return client.post("/api/v1/integrations/orders/draft", json=payload,
        headers={**headers, "Idempotency-Key": key})


@pytest.mark.parametrize("name", ["Ana", None])
@pytest.mark.parametrize("method", ["cash", "yape"])
def test_lid_checkout_uses_channel_profile_and_null_phone(client, tenant, agent_headers, name, method):
    lease, headers = acquire(client, agent_headers, name=name)
    assert lease["verified_phone"] is None and lease["contact_display_name"] == name
    payload = body(tenant, payment_method=method, customer_name=name)
    created = draft(client, headers, payload)
    assert created.status_code == 201, created.text
    order = created.json()
    assert order["customer_phone"] is None and order["whatsapp_chat_id"] == LID
    assert order["customer_name"] == name and order["payment_method"] == method
    assert order["status"] == "draft" and order["payment_status"] == "pending"
    assert draft(client, headers, payload).json() == order
    state = client.get(f'/api/v1/integrations/orders/{order["id"]}/customer-state',
        params={"sender": LID}, headers=headers)
    assert state.status_code == 200, state.text
    assert state.json()["id"] == order["id"] and state.json()["customer_phone"] is None
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Order)) == 1
        assert db.scalar(select(func.count()).select_from(Payment)) == 0
        assert db.scalar(select(func.count()).select_from(KitchenTicket)) == 0
    if method == "cash":
        path = f'/api/v1/integrations/orders/{order["id"]}/cash-confirm'
        confirmation = client.post(path, headers={**headers, "Idempotency-Key": "lid-cash"})
        assert confirmation.status_code == 200, confirmation.text
        assert confirmation.json()["order"]["customer_phone"] is None
        assert len(confirmation.json()["tickets"]) == 1
        assert client.post(path, headers={**headers, "Idempotency-Key": "lid-cash"}).json() == confirmation.json()
        with SessionLocal() as db:
            assert db.scalar(select(func.count()).select_from(KitchenTicket)) == 1
            assert db.scalar(select(func.count()).select_from(Payment)) == 0
            event = db.scalar(select(IntegrationEvent).where(IntegrationEvent.event_type == "order.cash_confirmed"))
            assert event.whatsapp_chat_id == LID and event.customer_phone is None


@pytest.mark.parametrize("phone", [SENDER, LID, LID.removesuffix("@lid"), ""])
def test_unverified_lid_cannot_supply_or_disguise_a_phone(client, tenant, agent_headers, phone):
    _, headers = acquire(client, agent_headers)
    denied = draft(client, headers, body(tenant, customer_phone=phone))
    assert denied.status_code == 409, denied.text
    assert denied.json()["code"] == "TRANSPORT_PHONE_REQUIRED"
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Order)) == 0


@pytest.mark.parametrize("change", [
    {"whatsapp_chat_id": OTHER_LID}, {"customer_name": "Nombre inventado"},
])
def test_lid_order_identity_and_display_name_remain_server_owned(client, tenant, agent_headers, change):
    _, headers = acquire(client, agent_headers)
    denied = draft(client, headers, body(tenant, **change))
    assert denied.status_code == 403, denied.text
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Order)) == 0


def test_distinct_lids_with_same_display_name_cannot_read_each_others_order(client, tenant, agent_headers):
    first, first_headers = acquire(client, agent_headers, name="Ana")
    created = draft(client, first_headers, body(tenant))
    assert created.status_code == 201, created.text
    second, second_headers = acquire(client, agent_headers, sender=OTHER_LID, key="other-lid", name="Ana")
    assert second["conversation_id"] != first["conversation_id"]
    order_id = created.json()["id"]
    for sender in (LID, OTHER_LID):
        denied = client.get(f"/api/v1/integrations/orders/{order_id}/customer-state",
            params={"sender": sender}, headers=second_headers)
        assert denied.status_code == 404, denied.text
    denied = draft(client, first_headers, body(tenant, branch_id=tenant["other_branch_id"]), key="other-business")
    assert denied.status_code == 403, denied.text
    created_second = draft(client, second_headers, body(tenant, whatsapp_chat_id=OTHER_LID), key="second-lid-order")
    assert created_second.status_code == 201, created_second.text
    assert created_second.json()["id"] != order_id


@pytest.mark.parametrize("sender", [LID, SENDER])
def test_known_phone_still_requires_exact_transport_phone(client, tenant, agent_headers, sender):
    _, headers = acquire(client, agent_headers, sender=sender, phone=SENDER if sender == LID else None)
    missing = draft(client, headers, body(tenant, whatsapp_chat_id=sender))
    assert missing.status_code == 403, missing.text
    forged = draft(client, headers, body(tenant, whatsapp_chat_id=sender, customer_phone="51999900999"), key="forged")
    assert forged.status_code == 403, forged.text
    accepted = draft(client, headers, body(tenant, whatsapp_chat_id=sender, customer_phone=SENDER), key="verified")
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["customer_phone"] == SENDER


def test_same_operation_replays_after_transport_later_resolves_phone(client, tenant, agent_headers):
    first, headers = acquire(client, agent_headers)
    payload = body(tenant)
    created = draft(client, headers, payload)
    assert created.status_code == 201, created.text
    assert commit(client, agent_headers, first, state={"order_id": created.json()["id"]}).status_code == 200
    second, current_headers = acquire(client, agent_headers, key="phone-resolved", phone=SENDER, name="Nuevo nombre")
    assert second["conversation_id"] == first["conversation_id"]
    replay = draft(client, current_headers, payload)
    assert replay.status_code in {200, 201} and replay.json() == created.json()
    assert draft(client, current_headers, body(tenant, customer_phone=SENDER)).status_code == 409
    assert draft(client, current_headers, payload, key="new-without-current-phone").status_code == 403
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Order)) == 1
        stored = db.get(Order, created.json()["id"])
        assert stored.customer_phone is None and stored.customer_name == "Ana"


def test_lid_delivery_keeps_manual_quote_and_stable_recipient(client, tenant, agent_headers):
    with SessionLocal.begin() as db:
        db.add(BranchSettings(business_id=tenant["business_id"], branch_id=tenant["branch_id"],
            digital_delivery=True, delivery_mode="neighborhoods",
            delivery_policy={"neighborhoods": [{"name": "Centro", "fee": 8}]},
            payment_methods={"takeaway": ["cash", "yape"], "delivery": ["cash", "yape"]}))
    _, headers = acquire(client, agent_headers)
    destination = {"address": "Calle de prueba 123", "reference": "En una esquina", "neighborhood": "Centro"}
    quote = client.post("/api/v1/integrations/orders/preview",
        headers={**headers, "Idempotency-Key": "lid-quote"},
        json={"channel": "delivery", "payment_method": "cash", "delivery_address": destination,
            "items": [{"product_id": tenant["product_id"], "quantity": 1}]})
    assert quote.status_code == 200, quote.text
    quoted = quote.json()
    assert quoted["delivery_fee_status"] == "pending_quote" and quoted["final_total"] is None
    created = draft(client, headers, body(tenant, channel="delivery", delivery_address=destination,
        delivery_quote_id=quoted["delivery_quote_id"], quoted_total=quoted["known_total"],
        delivery_fee=0, allow_pending_delivery_quote=True))
    assert created.status_code == 201, created.text
    assert created.json()["customer_phone"] is None and created.json()["whatsapp_chat_id"] == LID
    assert created.json()["delivery_fee_status"] == "pending_quote"
    assert created.json()["delivery_address"]["reference"] == "En una esquina"
