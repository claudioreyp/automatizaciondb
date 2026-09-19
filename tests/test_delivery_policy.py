from copy import deepcopy
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select

from app import settings_api
from app.database import SessionLocal
from app.models import AuditEvent, Branch, BranchSettings, DeliveryQuote, IdempotencyRecord
from app.settings_service import serialize_branch_settings


ORIGIN = {"latitude": -12.1, "longitude": -77.1, "maps_url": "https://maps.google.com/?q=-12.1,-77.1"}
POLICY = {
    "neighborhoods": [{"name": "Centro", "fee": 7.5}, {"name": "Norte", "fee": 0}],
    "origin": ORIGIN,
    "outside_band_mode": "quote",
}
DEFAULT_POLICY = {"neighborhoods": [], "origin": None, "outside_band_mode": "reject"}


@pytest.fixture(autouse=True)
def isolated_services(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Delivery policy tests cannot call external services")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)


def url(tenant):
    return f"/api/v1/settings/branches/{tenant['branch_id']}/delivery"


def configure(client, tenant, auth_headers, **values):
    payload = {
        "delivery_mode": "neighborhoods", "delivery_policy": deepcopy(POLICY),
        "minimum_order_amount": 20, "free_delivery_threshold": 100, "expected_version": 1,
        **values,
    }
    response = client.patch(url(tenant), json=payload, headers={
        **auth_headers, "Idempotency-Key": f"config-{payload['expected_version']}",
    })
    assert response.status_code == 200, response.text
    return response.json()


def quote(client, tenant, auth_headers, *, key="quote", **values):
    return client.post(url(tenant) + "/quotes", json={"subtotal": 50, "destination": {}, **values},
                       headers={**auth_headers, "Idempotency-Key": key})


def test_roundtrip_preserves_omitted_policy_and_explicit_null_resets(client, tenant, auth_headers):
    initial = client.get(url(tenant), headers=auth_headers).json()
    assert initial["delivery_mode"] == "fixed"
    assert initial["delivery_policy"] == DEFAULT_POLICY
    assert initial["branch_origin"] is None
    assert initial["delivery_policy_supported"] is True
    policy = deepcopy(POLICY)
    policy["neighborhoods"][0]["name"] = "  Centro  "
    result = configure(client, tenant, auth_headers, delivery_policy=policy)
    assert result["delivery_policy"] == POLICY
    assert client.get(url(tenant), headers=auth_headers).json() == result
    with SessionLocal() as db:
        settings = db.scalar(select(BranchSettings).where(BranchSettings.branch_id == tenant["branch_id"]))
        assert settings.delivery_policy == POLICY
        assert serialize_branch_settings(settings)["delivery"]["delivery_policy"] == POLICY
        audit = db.scalar(select(AuditEvent).where(AuditEvent.action == "settings.delivery.updated"))
        assert audit.branch_id == tenant["branch_id"]
        assert audit.actor_id == auth_headers["X-Dev-User"]
        assert audit.payload["before"]["delivery_policy"] == DEFAULT_POLICY
        assert audit.payload["after"]["delivery_policy"] == POLICY
    omitted = client.patch(url(tenant), json={"delivery_mode": "fixed", "expected_version": 2},
                           headers={**auth_headers, "Idempotency-Key": "omitted"})
    assert omitted.status_code == 200
    assert omitted.json()["delivery_policy"] == POLICY
    cleared = configure(client, tenant, auth_headers, delivery_policy=None, expected_version=3)
    assert cleared["delivery_policy"] == DEFAULT_POLICY
    with SessionLocal() as db:
        assert db.scalar(select(BranchSettings.delivery_policy)) is None


@pytest.mark.parametrize("name,subtotal,fee", [
    ("Centro", 20, 7.5), ("  cEnTrO ", 99.99, 7.5), ("CENTRO", 100, 0),
    ("Centro", 101, 0), ("Norte", 50, 0), ("Unknown", 50, None),
    ("Unknown", 100, None), ("Unknown", 101, None), (None, 150, None), (25, 150, None),
])
def test_neighborhood_coverage_and_thresholds(client, tenant, auth_headers, name, subtotal, fee):
    configure(client, tenant, auth_headers)
    response = quote(client, tenant, auth_headers, subtotal=subtotal, destination={"neighborhood": name})
    assert response.status_code == 201, response.text
    assert response.json()["fee"] == fee
    assert response.json()["requires_quote"] is (fee is None)
    assert response.json()["fee_status"] == ("pending_quote" if fee is None else "final")


@pytest.mark.parametrize("subtotal", [19, 19.99])
def test_neighborhood_minimum_precedes_free_threshold(client, tenant, auth_headers, subtotal):
    configure(client, tenant, auth_headers, free_delivery_threshold=0)
    response = quote(client, tenant, auth_headers, subtotal=subtotal, destination={"neighborhood": "Centro"})
    assert response.status_code == 409
    with SessionLocal() as db:
        assert db.scalar(select(func.count(DeliveryQuote.id))) == 0
        assert db.scalar(select(IdempotencyRecord.id).where(IdempotencyRecord.idempotency_key == "quote")) is None


@pytest.mark.parametrize("distance,fee", [(0, 4), (2, 4), (3, None), (5, 8), (6, None)])
@pytest.mark.parametrize("subtotal", [50, 100, 150])
def test_bands_outside_quote_never_becomes_free(client, tenant, auth_headers, distance, fee, subtotal):
    configure(client, tenant, auth_headers, delivery_mode="bands", bands=[
        {"minimum_km": 0, "maximum_km": 2, "fee": 4, "sort_order": 0},
        {"minimum_km": 4, "maximum_km": 5, "fee": 8, "sort_order": 1},
    ])
    response = quote(client, tenant, auth_headers, subtotal=subtotal, distance_km=distance)
    assert response.status_code == 201
    expected = 0 if subtotal >= 100 and fee is not None else fee
    assert response.json()["fee"] == expected
    assert response.json()["requires_quote"] is (fee is None)


def test_distance_mode_stays_linear_and_rejects_outside_even_with_policy(client, tenant, auth_headers):
    configure(client, tenant, auth_headers, delivery_mode="distance", distance_base_fee=2,
              distance_fee_per_km=3, distance_max_km=5)
    response = quote(client, tenant, auth_headers, distance_km=2)
    assert response.json()["fee"] == 8
    assert quote(client, tenant, auth_headers, key="outside", distance_km=6, subtotal=150).status_code == 409


@pytest.mark.parametrize("mode,subtotal,fee", [
    ("free", 50, 0), ("fixed", 50, 7), ("fixed", 100, 0),
    ("quote", 50, None), ("quote", 100, 0),
])
def test_existing_modes_unchanged_with_policy(client, tenant, auth_headers, mode, subtotal, fee):
    configure(client, tenant, auth_headers, delivery_mode=mode, fixed_delivery_fee=7)
    response = quote(client, tenant, auth_headers, subtotal=subtotal)
    assert response.status_code == 201
    assert response.json()["fee"] == fee


@pytest.mark.parametrize("policy", [
    {"neighborhoods": [{"name": " ", "fee": 1}]},
    {"neighborhoods": [{"name": "a" * 121, "fee": 1}]},
    {"neighborhoods": [{"name": "Centro", "fee": -1}]},
    {"neighborhoods": [{"name": "Centro", "fee": "NaN"}]},
    {"neighborhoods": [{"name": "Centro", "fee": "Infinity"}]},
    {"neighborhoods": [{"name": "Centro", "fee": 10000000000}]},
    {"neighborhoods": [{"name": " Centro ", "fee": 1}, {"name": "CENTRO", "fee": 2}]},
    {"neighborhoods": [{"name": "Stra\u00dfe", "fee": 1}, {"name": "STRASSE", "fee": 2}]},
    {"neighborhoods": [{"name": str(i), "fee": 1} for i in range(201)]},
    {"neighborhoods": [{"name": "Centro"}]},
    {"neighborhoods": None}, {"neighborhoods": [{"name": "Centro", "fee": 1, "admin": True}]},
    {"origin": {}}, {"origin": {"latitude": 0}}, {"origin": {"longitude": 0}},
    {"origin": {"latitude": 91, "longitude": 0}},
    {"origin": {"latitude": -91, "longitude": 0}},
    {"origin": {"latitude": 0, "longitude": 181}},
    {"origin": {"latitude": 0, "longitude": -181}},
    {"origin": {"latitude": "NaN", "longitude": 0}},
    {"origin": {"latitude": 0, "longitude": "Infinity"}},
    {"origin": {"latitude": None, "longitude": None}},
    {"origin": {**ORIGIN, "maps_url": "a" * 2049}},
    {"origin": {**ORIGIN, "api_key": "not-allowed"}},
    {"outside_band_mode": "free"}, {"outside_band_mode": None}, {"unknown": True},
])
def test_invalid_policy_is_rejected_without_writes(client, tenant, auth_headers, policy):
    response = client.patch(url(tenant), json={
        "delivery_mode": "neighborhoods", "delivery_policy": policy, "expected_version": 1,
    }, headers={**auth_headers, "Idempotency-Key": "invalid"})
    assert response.status_code == 422, response.text
    with SessionLocal() as db:
        assert db.scalar(select(func.count(BranchSettings.id))) == 0
        assert db.scalar(select(func.count(AuditEvent.id))) == 0
        assert db.scalar(select(func.count(IdempotencyRecord.id))) == 0


@pytest.mark.parametrize("latitude,longitude", [(-90, -180), (90, 180), (0, 0)])
def test_origin_coordinate_boundaries(client, tenant, auth_headers, latitude, longitude):
    result = configure(client, tenant, auth_headers, delivery_policy={
        "origin": {"latitude": latitude, "longitude": longitude},
    })
    assert result["delivery_policy"]["origin"] == {
        "latitude": latitude, "longitude": longitude, "maps_url": "",
    }


@pytest.mark.parametrize("override", [True, False])
def test_routes_origin_override_and_branch_fallback_are_separate(client, tenant, auth_headers, monkeypatch, override):
    branch_origin = {"latitude": -12.2, "longitude": -77.2, "maps_url": "https://maps.google.com/?q=-12.2,-77.2"}
    with SessionLocal.begin() as db:
        branch = db.get(Branch, tenant["branch_id"])
        for field, value in branch_origin.items():
            setattr(branch, field, value)
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-routes-key")
    policy = {**POLICY, "origin": ORIGIN if override else None}
    config = configure(client, tenant, auth_headers, delivery_mode="distance", delivery_policy=policy,
                       distance_base_fee=2, distance_fee_per_km=3, distance_max_km=5)
    assert config["branch_origin"] == branch_origin
    assert config["delivery_policy"]["origin"] == (ORIGIN if override else None)
    assert config["google_routes_configured"] is True
    captured = []

    def routes(*args, **kwargs):
        captured.append(kwargs["json"])
        return httpx.Response(200, json={"routes": [{"distanceMeters": 2000}]},
                              request=httpx.Request("POST", args[0]))

    monkeypatch.setattr(httpx, "post", routes)
    response = quote(client, tenant, auth_headers, destination={"latitude": -12.3, "longitude": -77.3})
    assert response.status_code == 201, response.text
    assert response.json()["fee"] == 8
    effective = ORIGIN if override else branch_origin
    assert captured[0]["origin"]["location"]["latLng"] == {key: effective[key] for key in ("latitude", "longitude")}
    with SessionLocal() as db:
        branch = db.get(Branch, tenant["branch_id"])
        assert float(branch.latitude) == branch_origin["latitude"]
        assert float(branch.longitude) == branch_origin["longitude"]
        assert branch.maps_url == branch_origin["maps_url"]
        assert branch.version == config["version"]
        saved_quote = db.get(DeliveryQuote, response.json()["id"])
        assert saved_quote.input_snapshot["origin"] == effective


def test_settings_and_quote_retries_have_one_audit_each_and_stale_write_fails(client, tenant, auth_headers):
    initial = configure(client, tenant, auth_headers)
    assert configure(client, tenant, auth_headers) == initial
    stale = client.patch(url(tenant), json={"delivery_mode": "free", "expected_version": 1},
                         headers={**auth_headers, "Idempotency-Key": "stale"})
    assert stale.status_code == 409
    created = quote(client, tenant, auth_headers, destination={"neighborhood": "Centro"})
    assert quote(client, tenant, auth_headers, destination={"neighborhood": "Centro"}).json() == created.json()
    assert client.get(url(tenant), headers=auth_headers).json() == initial
    with SessionLocal() as db:
        assert db.scalar(select(func.count(DeliveryQuote.id))) == 1
        assert db.scalar(select(func.count(AuditEvent.id))) == 2
        assert db.scalar(select(func.count(IdempotencyRecord.id))) == 2


def test_failed_commit_rolls_back_policy_quote_and_audit(client, tenant, auth_headers, monkeypatch):
    initial = configure(client, tenant, auth_headers)

    def fail(*args, **kwargs):
        raise RuntimeError("simulated commit failure")

    monkeypatch.setattr(settings_api, "save_idempotent_response", fail)
    with pytest.raises(RuntimeError, match="simulated"):
        configure(client, tenant, auth_headers, delivery_mode="free", expected_version=2, delivery_policy=None)
    with pytest.raises(RuntimeError, match="simulated"):
        quote(client, tenant, auth_headers)
    assert client.get(url(tenant), headers=auth_headers).json() == initial
    with SessionLocal() as db:
        assert db.scalar(select(func.count(DeliveryQuote.id))) == 0
        assert db.scalar(select(func.count(AuditEvent.id))) == 1


@pytest.mark.parametrize("role", ["cashier", "waiter", "kitchen", "dispatcher"])
def test_delivery_policy_permissions_unchanged(client, tenant, auth_headers, role):
    headers = {**auth_headers, "X-Dev-Role": role, "Idempotency-Key": "forbidden"}
    assert client.patch(url(tenant), headers=headers, json={
        "delivery_mode": "neighborhoods", "delivery_policy": POLICY, "expected_version": 1,
    }).status_code == 403
    expected = 201 if role in {"cashier", "waiter"} else 403
    assert client.post(url(tenant) + "/quotes", headers=headers, json={"subtotal": 50}).status_code == expected


def test_cross_tenant_and_branch_scopes_and_missing_idempotency(client, tenant, auth_headers):
    payload = {"delivery_mode": "neighborhoods", "delivery_policy": POLICY, "expected_version": 1}
    assert client.patch(url(tenant), headers=auth_headers, json=payload).status_code == 422
    other = {**tenant, "branch_id": tenant["other_branch_id"]}
    assert client.get(url(other), headers=auth_headers).status_code == 403
    assert client.patch(url(other), headers={**auth_headers, "Idempotency-Key": "cross"}, json=payload).status_code == 403
    with SessionLocal.begin() as db:
        branch = Branch(business_id=tenant["business_id"], slug="second", name="Second")
        db.add(branch)
        db.flush()
        second = {**tenant, "branch_id": branch.id}
    assert client.get(url(second), headers=auth_headers).status_code == 403
    assert quote(client, second, auth_headers).status_code == 403
