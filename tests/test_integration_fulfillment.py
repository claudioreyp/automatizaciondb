import pytest

from app.database import SessionLocal
from app.models import Order, Payment, KitchenTicket, PrintJob
from app.schemas import OrderCreate
from test_escalar_integrations import create_credential, integration_headers


@pytest.mark.parametrize("fields,code", [
    ({}, "ORDER_CHANNEL_REQUIRED"),
    ({"channel": "delivery"}, "DELIVERY_DESTINATION_REQUIRED"),
    ({"channel": "delivery", "delivery_address": {"address": {"fake": "address"}, "reference": "Azul"}}, "DELIVERY_DESTINATION_REQUIRED"),
    ({"channel": "delivery", "delivery_address": {"maps_url": "https://[invalid", "reference": "Azul"}}, "DELIVERY_DESTINATION_REQUIRED"),
    ({"channel": "delivery", "delivery_address": {"maps_url": "https://google.com.evil.test/maps/test", "reference": "Azul"}}, "DELIVERY_DESTINATION_REQUIRED"),
    ({"channel": "delivery", "delivery_address": {"latitude": True, "longitude": -80, "reference": "Azul"}}, "DELIVERY_DESTINATION_REQUIRED"),
    ({"channel": "delivery", "delivery_address": {"address": "Calle Prueba 123"}}, "DELIVERY_REFERENCE_REQUIRED"),
])
def test_missing_fulfillment_never_creates_any_operation(client, tenant, auth_headers, fields, code):
    token = create_credential(client, tenant, auth_headers)["token"]
    result = client.post("/api/v1/integrations/orders/draft",
        headers=integration_headers(token, "incomplete-checkout"),
        json={"branch_id": tenant["branch_id"], "items": [{"product_id": tenant["product_id"], "quantity": 1}], **fields})
    assert result.status_code == 422, result.text
    assert result.json()["code"] == code
    with SessionLocal() as db:
        for model in (Order, Payment, KitchenTicket, PrintJob):
            assert db.query(model).count() == 0


@pytest.mark.parametrize("destination", [
    {"address": "Calle Prueba 123", "reference": "Puerta azul"},
    {"latitude": -5.2, "longitude": -80.3, "reference": "Puerta azul"},
    {"maps_url": "https://maps.app.goo.gl/prueba", "reference": "Puerta azul"},
])
def test_delivery_destination_and_idempotent_replay(client, tenant, auth_headers, destination):
    token = create_credential(client, tenant, auth_headers)["token"]
    headers = integration_headers(token, "complete-checkout")
    payload = {"branch_id": tenant["branch_id"], "channel": "delivery", "delivery_address": destination,
        "items": [{"product_id": tenant["product_id"], "quantity": 1}]}
    first = client.post("/api/v1/integrations/orders/draft", headers=headers, json=payload)
    assert first.status_code == 201, first.text
    again = client.post("/api/v1/integrations/orders/draft", headers=headers, json=payload)
    assert again.json() == first.json()
    with SessionLocal() as db:
        assert db.query(Order).count() == 1


def test_manual_pos_retains_default_channel():
    assert OrderCreate(branch_id=1, items=[]).channel == "counter"
