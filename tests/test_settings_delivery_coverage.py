import httpx
import pytest
from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import DeliveryQuote, IdempotencyRecord


@pytest.fixture(autouse=True)
def no_external_http(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Delivery coverage tests must not call external services")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)


def configure(client, tenant, auth_headers, mode, bands=None):
    response = client.patch(
        f"/api/v1/settings/branches/{tenant['branch_id']}/delivery",
        headers={**auth_headers, "Idempotency-Key": "coverage-settings"},
        json={
            "delivery_mode": mode, "distance_max_km": 5,
            "distance_base_fee": 2, "distance_fee_per_km": 3,
            "fixed_delivery_fee": 7, "free_delivery_threshold": 100,
            "minimum_order_amount": 20, "expected_version": 1,
            "bands": bands if bands is not None else [
                {"minimum_km": 0, "maximum_km": 2, "fee": 4, "sort_order": 0},
                {"minimum_km": 4, "maximum_km": 5, "fee": 8, "sort_order": 1},
            ],
        },
    )
    assert response.status_code == 200


@pytest.mark.parametrize("mode,distance,bands", [
    ("distance", 6, None),
    ("bands", 6, None),
    ("bands", 3, None),
    ("bands", 1, []),
])
@pytest.mark.parametrize("subtotal", [100, 150])
def test_free_threshold_never_bypasses_coverage(client, tenant, auth_headers, mode, distance, bands, subtotal):
    configure(client, tenant, auth_headers, mode, bands)
    response = client.post(
        f"/api/v1/settings/branches/{tenant['branch_id']}/delivery/quotes",
        headers={**auth_headers, "Idempotency-Key": "outside-coverage"},
        json={"subtotal": subtotal, "distance_km": distance, "destination": {}},
    )
    assert response.status_code == 409
    with SessionLocal() as db:
        assert db.scalar(select(func.count(DeliveryQuote.id))) == 0
        assert db.scalar(select(IdempotencyRecord.id).where(
            IdempotencyRecord.idempotency_key == "outside-coverage")) is None


@pytest.mark.parametrize("mode,distance,subtotal,fee", [
    ("distance", 5, 100, 0), ("bands", 5, 100, 0),
    ("distance", 2, 50, 8), ("bands", 2, 50, 4),
    ("fixed", 100, 50, 7), ("fixed", 100, 100, 0),
    ("free", 100, 50, 0), ("quote", 100, 50, None),
])
def test_covered_boundaries_and_existing_modes_keep_prices_and_replay(client, tenant, auth_headers, mode, distance, subtotal, fee):
    configure(client, tenant, auth_headers, mode)
    url = f"/api/v1/settings/branches/{tenant['branch_id']}/delivery/quotes"
    request_headers = {**auth_headers, "Idempotency-Key": "covered-quote"}
    payload = {"subtotal": subtotal, "distance_km": distance, "destination": {}}
    response = client.post(url, headers=request_headers, json=payload)
    assert response.status_code == 201
    assert response.json()["fee"] == fee
    assert response.json()["fee_status"] == ("pending_quote" if fee is None else "final")
    assert client.post(url, headers=request_headers, json=payload).json() == response.json()
    with SessionLocal() as db:
        assert db.scalar(select(func.count(DeliveryQuote.id))) == 1


def test_minimum_order_still_applies_before_free_shipping(client, tenant, auth_headers):
    configure(client, tenant, auth_headers, "free")
    response = client.post(
        f"/api/v1/settings/branches/{tenant['branch_id']}/delivery/quotes",
        headers={**auth_headers, "Idempotency-Key": "below-minimum"},
        json={"subtotal": 10, "destination": {}},
    )
    assert response.status_code == 409
