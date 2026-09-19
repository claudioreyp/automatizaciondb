from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select

from app import settings_api
from app.database import SessionLocal
from app.models import (
    AuditEvent, Branch, BranchSettings, Business, CashSession, DeliveryQuote,
    IdempotencyRecord, KitchenTicket, Order, Payment, Product, Promotion, PromotionTarget, utcnow,
)


DESTINATION = {
    "delivery_service": "own", "street": "Avenida Central", "number": "123",
    "cross_streets": "Primera y Segunda", "neighborhood": "Centro",
    "reference": "Puerta azul", "address": "Avenida Central 123, Centro",
    "maps_url": "https://maps.google.com/?q=-12.2,-77.2", "latitude": -12.2, "longitude": -77.2,
}
POLICY = {
    "neighborhoods": [{"name": "Centro", "fee": 8}],
    "origin": {"latitude": -12.1, "longitude": -77.1, "maps_url": ""},
    "outside_band_mode": "quote",
}


@pytest.fixture(autouse=True)
def isolated_http(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("POS quote tests cannot use external services")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)


def url(tenant):
    return f"/api/v1/settings/branches/{tenant['branch_id']}/delivery"


def headers(auth_headers, key):
    return {**auth_headers, "Idempotency-Key": key}


def configure(client, tenant, auth_headers, **values):
    payload = {
        "delivery_mode": "fixed", "fixed_delivery_fee": 8, "delivery_policy": POLICY,
        "minimum_order_amount": 20, "free_delivery_threshold": 100, "expected_version": 1,
        **values,
    }
    response = client.patch(url(tenant), headers=headers(auth_headers, f"settings-{payload['expected_version']}"), json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def quote(client, tenant, auth_headers, key="quote", **values):
    return client.post(url(tenant) + "/quotes", headers=headers(auth_headers, key), json={
        "subtotal": 20, "destination": deepcopy(DESTINATION), "expected_configuration_version": 2, **values,
    })


def order(client, tenant, auth_headers, quote_id, key="order", **values):
    return client.post("/api/v1/orders", headers=headers(auth_headers, key), json={
        "branch_id": tenant["branch_id"], "channel": "delivery", "delivery_address": deepcopy(DESTINATION),
        "delivery_quote_id": quote_id, "items": [{"product_id": tenant["product_id"], "quantity": 1}], **values,
    })


def assert_no_orders():
    with SessionLocal() as db:
        for model in (Order, Payment, KitchenTicket):
            assert db.scalar(select(func.count(model.id))) == 0
        assert db.scalar(select(Business.order_folio_counter).where(Business.slug == "test-restaurant")) == 0


@pytest.mark.parametrize("fee", [0, 7.5, "8.125"])
def test_manual_fee_first_request_is_final_audited_and_idempotent(client, tenant, auth_headers, fee):
    configure(client, tenant, auth_headers, delivery_mode="quote")
    cashier = {**auth_headers, "X-Dev-Role": "cashier", "X-Dev-User": "cashier-test"}
    response = quote(client, tenant, cashier, confirmed_fee=fee)
    assert response.status_code == 201, response.text
    result = response.json()
    expected = float(Decimal(str(fee)).quantize(Decimal("0.01"), rounding="ROUND_HALF_UP"))
    assert result["fee"] == expected
    assert result["requires_quote"] is False
    assert result["fee_status"] == "final"
    assert result["pos_strict"] is True
    assert result["manually_confirmed"] is True
    assert quote(client, tenant, cashier, confirmed_fee=fee).json() == result
    with SessionLocal() as db:
        saved = db.get(DeliveryQuote, result["id"])
        assert saved.input_snapshot["pos_strict"] is True
        assert saved.input_snapshot["context"] == "pos"
        assert saved.input_snapshot["destination"] == DESTINATION
        assert saved.input_snapshot["subtotal_basis"] == "before_discounts"
        assert saved.input_snapshot["manual_confirmation"] == {"fee": expected, "actor_id": "cashier-test"}
        audits = list(db.scalars(select(AuditEvent).where(AuditEvent.action == "settings.delivery.quoted")))
        assert len(audits) == 1
        assert audits[0].actor_id == "cashier-test"
        assert audits[0].actor_display_name
        assert audits[0].branch_id == tenant["branch_id"]
        assert audits[0].payload["input"]["manual_confirmation"]["fee"] == expected
    created = order(client, tenant, cashier, result["id"], delivery_fee=999)
    assert created.status_code == 201, created.text
    assert created.json()["delivery_fee"] == expected
    assert created.json()["total"] == round(20 + expected, 2)
    assert order(client, tenant, cashier, result["id"], delivery_fee=999).json() == created.json()
    paid = client.post(f"/api/v1/orders/{created.json()['id']}/payments", headers=headers(cashier, "payment"),
                       json={"method": "cash", "amount": 20 + expected})
    assert paid.status_code == 201, paid.text
    sent = client.post(f"/api/v1/orders/{created.json()['id']}/confirm-and-send", headers=headers(cashier, "send"),
                       json={"expected_version": paid.json()["order"]["version"]})
    assert sent.status_code == 200, sent.text
    with SessionLocal() as db:
        assert db.scalar(select(func.count(Payment.id))) == 1
        assert db.scalar(select(func.count(KitchenTicket.id))) == 1
        assert db.scalar(select(func.count(AuditEvent.id)).where(AuditEvent.action == "order.created")) == 1


def test_unconfirmed_pos_quote_cannot_create_order_then_new_key_confirms(client, tenant, auth_headers):
    configure(client, tenant, auth_headers, delivery_mode="quote")
    pending = quote(client, tenant, auth_headers)
    assert pending.status_code == 201
    assert pending.json()["fee"] is None
    assert pending.json()["requires_quote"] is True
    rejected = order(client, tenant, auth_headers, pending.json()["id"], delivery_fee=10)
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "DELIVERY_FEE_PENDING"
    assert_no_orders()
    confirmed = quote(client, tenant, auth_headers, key="manual-confirmation", confirmed_fee=10)
    assert confirmed.status_code == 201
    assert confirmed.json()["id"] != pending.json()["id"]
    assert order(client, tenant, auth_headers, confirmed.json()["id"]).status_code == 201


@pytest.mark.parametrize("mode", ["free", "fixed", "neighborhoods", "distance", "bands"])
def test_cannot_override_calculated_fee(client, tenant, auth_headers, monkeypatch, mode):
    configure(client, tenant, auth_headers, delivery_mode=mode, distance_max_km=5,
              bands=[{"minimum_km": 0, "maximum_km": 5, "fee": 8}])
    mock_routes(monkeypatch, 2000)
    response = quote(client, tenant, auth_headers, confirmed_fee=3)
    assert response.status_code == 422, response.text
    with SessionLocal() as db:
        assert db.scalar(select(func.count(DeliveryQuote.id))) == 0


@pytest.mark.parametrize("fee", [-1, "NaN", "Infinity", "-Infinity", "10000000000", True, ""])
def test_invalid_manual_fees_rejected_without_writes(client, tenant, auth_headers, fee):
    configure(client, tenant, auth_headers, delivery_mode="quote")
    assert quote(client, tenant, auth_headers, confirmed_fee=fee).status_code == 422
    with SessionLocal() as db:
        assert db.scalar(select(func.count(DeliveryQuote.id))) == 0
        assert db.scalar(select(func.count(AuditEvent.id))) == 1


def test_manual_fee_requires_strict_configuration_version(client, tenant, auth_headers):
    configure(client, tenant, auth_headers, delivery_mode="quote")
    assert quote(client, tenant, auth_headers, expected_configuration_version=None, confirmed_fee=3).status_code == 422


@pytest.mark.parametrize("expected", [0, -1, True, "2", 2.5])
def test_strict_version_must_be_positive_integer(client, tenant, auth_headers, expected):
    assert quote(client, tenant, auth_headers, expected_configuration_version=expected).status_code == 422


def test_stale_configuration_at_quote_and_application_rolls_back(client, tenant, auth_headers):
    configure(client, tenant, auth_headers)
    stale = quote(client, tenant, auth_headers, expected_configuration_version=1)
    assert stale.status_code == 409
    assert stale.json()["code"] == "DELIVERY_CONFIGURATION_STALE"
    valid = quote(client, tenant, auth_headers)
    assert valid.status_code == 201
    configure(client, tenant, auth_headers, expected_version=2, fixed_delivery_fee=12)
    assert quote(client, tenant, auth_headers).json() == valid.json()
    rejected = order(client, tenant, auth_headers, valid.json()["id"])
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "DELIVERY_CONFIGURATION_STALE"
    assert_no_orders()


@pytest.mark.parametrize("field", list(DESTINATION))
@pytest.mark.parametrize("change", ["replace", "remove"])
def test_every_destination_field_is_bound_exactly(client, tenant, auth_headers, field, change):
    configure(client, tenant, auth_headers)
    result = quote(client, tenant, auth_headers).json()
    destination = deepcopy(DESTINATION)
    if change == "remove":
        destination.pop(field)
    else:
        destination[field] = 0 if field in {"latitude", "longitude"} else "Changed"
    response = order(client, tenant, auth_headers, result["id"], delivery_address=destination)
    assert response.status_code == 409
    assert response.json()["code"] == "DELIVERY_QUOTE_DESTINATION_CHANGED"
    assert_no_orders()


@pytest.mark.parametrize("discount_type", ["manual", "promotion"])
def test_minimum_and_free_shipping_use_subtotal_before_discounts(client, tenant, auth_headers, discount_type):
    configure(client, tenant, auth_headers, minimum_order_amount=100)
    if discount_type == "promotion":
        with SessionLocal.begin() as db:
            promotion = Promotion(business_id=tenant["business_id"], branch_id=tenant["branch_id"],
                                  name="Half off", promotion_type="product_discount", discount_type="percentage",
                                  discount_value=50, target_scope="products", service_channels=["pos_delivery"])
            promotion.targets.append(PromotionTarget(product_id=tenant["product_id"]))
            db.add(promotion)
    result = quote(client, tenant, auth_headers, subtotal=100).json()
    assert result["fee"] == 0
    created = order(client, tenant, auth_headers, result["id"], discount=50 if discount_type == "manual" else 0,
                    items=[{"product_id": tenant["product_id"], "quantity": 5}])
    assert created.status_code == 201, created.text
    assert created.json()["subtotal"] == 100
    assert created.json()["discount"] == 50
    assert created.json()["total"] == 50
    assert created.json()["delivery_fee"] == 0


def test_subtotal_mismatch_cannot_be_hidden_by_discount(client, tenant, auth_headers):
    configure(client, tenant, auth_headers)
    result = quote(client, tenant, auth_headers).json()
    created = order(client, tenant, auth_headers, result["id"], discount=20,
                    items=[{"product_id": tenant["product_id"], "quantity": 2}])
    assert created.status_code == 409
    assert_no_orders()


def test_expired_quote_cannot_create_but_accepted_price_is_not_retroactive(client, tenant, auth_headers):
    configure(client, tenant, auth_headers)
    result = quote(client, tenant, auth_headers).json()
    accepted = order(client, tenant, auth_headers, result["id"])
    assert accepted.status_code == 201
    with SessionLocal.begin() as db:
        db.get(DeliveryQuote, result["id"]).expires_at = utcnow() - timedelta(minutes=1)
    rejected = order(client, tenant, auth_headers, result["id"], key="expired")
    assert rejected.status_code == 409
    configure(client, tenant, auth_headers, fixed_delivery_fee=50, expected_version=2)
    paid = client.post(f"/api/v1/orders/{accepted.json()['id']}/payments", headers=headers(auth_headers, "payment"),
                       json={"method": "cash", "amount": 28})
    assert paid.status_code == 201, paid.text
    assert paid.json()["order"]["delivery_fee"] == 8


def mock_routes(monkeypatch, meters):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: httpx.Response(
        200, json={"routes": [{"distanceMeters": meters}]}, request=httpx.Request("POST", args[0]),
    ))


@pytest.mark.parametrize("mode", ["bands", "neighborhoods"])
@pytest.mark.parametrize("subtotal", [20, 100])
def test_unknown_coverage_requires_manual_even_over_free_threshold(client, tenant, auth_headers, monkeypatch, mode, subtotal):
    configure(client, tenant, auth_headers, delivery_mode=mode, bands=[{"minimum_km": 0, "maximum_km": 1, "fee": 8}])
    mock_routes(monkeypatch, 2000)
    destination = {**DESTINATION, "neighborhood": "Unknown"}
    pending = quote(client, tenant, auth_headers, subtotal=subtotal, destination=destination)
    assert pending.status_code == 201, pending.text
    assert pending.json()["requires_quote"] is True
    confirmed = quote(client, tenant, auth_headers, key="confirm", subtotal=subtotal, destination=destination, confirmed_fee=9)
    assert confirmed.status_code == 201
    assert confirmed.json()["fee"] == 9


@pytest.mark.parametrize("mode", ["quote", "bands", "neighborhoods"])
def test_manual_cannot_bypass_minimum(client, tenant, auth_headers, mode):
    configure(client, tenant, auth_headers, delivery_mode=mode, free_delivery_threshold=0)
    response = quote(client, tenant, auth_headers, subtotal=19, confirmed_fee=0)
    assert response.status_code == 409
    with SessionLocal() as db:
        assert db.scalar(select(func.count(DeliveryQuote.id))) == 0


@pytest.mark.parametrize("mode", ["bands", "distance"])
def test_manual_cannot_bypass_reject_coverage_or_supply_fake_distance(client, tenant, auth_headers, monkeypatch, mode):
    configure(client, tenant, auth_headers, delivery_mode=mode, delivery_policy={**POLICY, "outside_band_mode": "reject"},
              distance_max_km=1, bands=[{"minimum_km": 0, "maximum_km": 1, "fee": 8}])
    mock_routes(monkeypatch, 2000)
    response = quote(client, tenant, auth_headers, subtotal=100, distance_km=0.1, confirmed_fee=0)
    assert response.status_code == 409
    with SessionLocal() as db:
        assert db.scalar(select(func.count(DeliveryQuote.id))) == 0


@pytest.mark.parametrize("failure", ["network", "status", "empty", "negative", "nan"])
def test_manual_cannot_bypass_google_failure(client, tenant, auth_headers, monkeypatch, failure):
    configure(client, tenant, auth_headers, delivery_mode="bands")
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")

    def routes(*args, **kwargs):
        if failure == "network":
            raise httpx.ConnectError("simulated failure")
        body = {"routes": []} if failure == "empty" else {"routes": [{"distanceMeters": -1 if failure == "negative" else "NaN"}]}
        return httpx.Response(503 if failure == "status" else 200, json=body, request=httpx.Request("POST", args[0]))

    monkeypatch.setattr(httpx, "post", routes)
    result = quote(client, tenant, auth_headers, confirmed_fee=8, distance_km=0)
    assert result.status_code == 502, result.text
    with SessionLocal() as db:
        assert db.scalar(select(func.count(DeliveryQuote.id))) == 0
        assert db.scalar(select(IdempotencyRecord.id).where(IdempotencyRecord.idempotency_key == "quote")) is None


def test_cashier_reads_and_quotes_but_cannot_modify_any_settings(client, tenant, auth_headers):
    configure(client, tenant, auth_headers)
    cashier = {**auth_headers, "X-Dev-Role": "cashier"}
    response = client.get(url(tenant), headers=cashier)
    assert response.status_code == 200
    assert response.json()["pos_quotes_supported"] is True
    assert quote(client, tenant, cashier).status_code == 201
    denied = client.patch(url(tenant), headers=headers(cashier, "forbidden"), json={"delivery_mode": "free", "expected_version": 2})
    assert denied.status_code == 403
    assert client.get("/api/v1/settings/members", headers=cashier).status_code == 403


def test_quote_is_scoped_to_branch_business_and_pos_context(client, tenant, auth_headers):
    configure(client, tenant, auth_headers)
    result = quote(client, tenant, auth_headers).json()
    other_headers = {**auth_headers, "X-Business-Id": str(tenant["other_business_id"]), "X-Branch-Id": str(tenant["other_branch_id"])}
    with SessionLocal.begin() as db:
        product = Product(business_id=tenant["other_business_id"], branch_id=tenant["other_branch_id"], sku="OTHER", name="Other", price=20)
        same_business_branch = Branch(business_id=tenant["business_id"], slug="second", name="Second")
        db.add_all([product, same_business_branch])
        db.flush()
        product_id, second_id = product.id, same_business_branch.id
        second_product = Product(business_id=tenant["business_id"], branch_id=second_id, sku="SECOND", name="Second", price=20)
        db.add(second_product)
        db.flush()
        second_product_id = second_product.id
    other = order(client, {**tenant, "branch_id": tenant["other_branch_id"]}, other_headers, result["id"],
                  items=[{"product_id": product_id, "quantity": 1}])
    assert other.status_code == 422
    second = order(client, {**tenant, "branch_id": second_id}, {**auth_headers, "X-Branch-Id": str(second_id)}, result["id"],
                   items=[{"product_id": second_product_id, "quantity": 1}])
    assert second.status_code == 422
    integrated = client.post("/api/v1/integrations/orders/draft", headers={"X-Integration-Token": "test-integration-token", "Idempotency-Key": "integration"}, json={
        "branch_id": tenant["branch_id"], "channel": "delivery", "delivery_address": DESTINATION,
        "delivery_quote_id": result["id"], "items": [{"product_id": tenant["product_id"], "quantity": 1}],
    })
    assert integrated.status_code == 409
    assert integrated.json()["code"] == "DELIVERY_QUOTE_CONTEXT_MISMATCH"
    assert_no_orders()


@pytest.mark.parametrize("mode", ["neighborhoods", "bands", "distance", "quote"])
def test_nonflat_policy_changes_preserve_digital_legacy_fee(client, tenant, auth_headers, mode):
    with SessionLocal.begin() as db:
        db.get(Branch, tenant["branch_id"]).delivery_fee = Decimal("6.50")
    configure(client, tenant, auth_headers, delivery_mode=mode, distance_max_km=5)
    with SessionLocal() as db:
        assert db.get(Branch, tenant["branch_id"]).delivery_fee == Decimal("6.50")
    old_quote = quote(client, tenant, auth_headers, expected_configuration_version=None, distance_km=1)
    assert old_quote.status_code == 201
    assert old_quote.json()["pos_strict"] is False


def test_legacy_final_quote_keeps_destination_and_configuration_compatibility(client, tenant, auth_headers):
    configure(client, tenant, auth_headers)
    legacy = quote(client, tenant, auth_headers, expected_configuration_version=None).json()
    configure(client, tenant, auth_headers, expected_version=2, fixed_delivery_fee=20)
    response = order(client, tenant, auth_headers, legacy["id"], delivery_address={"address": "Legacy destination"})
    assert response.status_code == 201
    assert response.json()["delivery_fee"] == 8


@pytest.mark.parametrize("endpoint", ["payments", "confirm", "confirm-and-send", "send-to-kitchen"])
def test_pending_legacy_quote_cannot_be_charged_confirmed_or_sent(client, tenant, auth_headers, endpoint):
    configure(client, tenant, auth_headers, delivery_mode="quote")
    legacy = quote(client, tenant, auth_headers, expected_configuration_version=None).json()
    # Pending drafts remain supported for old integrations, never for a new POS order.
    assert order(client, tenant, auth_headers, legacy["id"]).status_code == 409
    created = client.post("/api/v1/integrations/orders/draft", headers={"X-Integration-Token": "test-integration-token", "Idempotency-Key": "pending"}, json={
        "branch_id": tenant["branch_id"], "channel": "delivery", "delivery_quote_id": legacy["id"],
        "items": [{"product_id": tenant["product_id"], "quantity": 1}],
    })
    assert created.status_code == 201, created.text
    body = {"method": "cash", "amount": 20} if endpoint == "payments" else {"expected_version": created.json()["version"]}
    response = client.post(f"/api/v1/orders/{created.json()['id']}/{endpoint}", headers=headers(auth_headers, "attempt"), json=body)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "DELIVERY_FEE_PENDING"
    with SessionLocal() as db:
        for model in (Payment, KitchenTicket, CashSession):
            assert db.scalar(select(func.count(model.id))) == 0
        assert db.get(Order, created.json()["id"]).status == "draft"
        assert db.scalar(select(IdempotencyRecord.id).where(IdempotencyRecord.idempotency_key == "attempt")) is None


def test_failed_manual_quote_transaction_preserves_no_quote_or_audit(client, tenant, auth_headers, monkeypatch):
    configure(client, tenant, auth_headers, delivery_mode="quote")

    def fail(*args, **kwargs):
        raise RuntimeError("simulated commit failure")

    monkeypatch.setattr(settings_api, "save_idempotent_response", fail)
    with pytest.raises(RuntimeError, match="simulated"):
        quote(client, tenant, auth_headers, confirmed_fee=7)
    with SessionLocal() as db:
        assert db.scalar(select(func.count(DeliveryQuote.id))) == 0
        assert db.scalar(select(func.count(AuditEvent.id))) == 1
        assert db.scalar(select(IdempotencyRecord.id).where(IdempotencyRecord.idempotency_key == "quote")) is None


def update_profile(client, tenant, auth_headers, key, **values):
    profile_url = f"/api/v1/settings/branches/{tenant['branch_id']}/profile"
    current = client.get(profile_url, headers=auth_headers)
    assert current.status_code == 200
    response = client.patch(profile_url, headers=headers(auth_headers, key), json={
        "expected_version": current.json()["version"], **values,
    })
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("mode", ["distance", "bands"])
@pytest.mark.parametrize("change", [
    {"latitude": -12.4, "longitude": -77.1},
    {"latitude": -12.1, "longitude": -77.4},
    {"maps_url": "https://maps.google.com/?q=-12.4,-77.4"},
])
def test_profile_origin_change_invalidates_strict_route_quote(client, tenant, auth_headers, monkeypatch, mode, change):
    update_profile(client, tenant, auth_headers, "initial-origin", latitude=-12.1, longitude=-77.1)
    configure(client, tenant, auth_headers, delivery_mode=mode, delivery_policy={**POLICY, "origin": None},
              distance_max_km=5, distance_base_fee=2, distance_fee_per_km=3,
              bands=[{"minimum_km": 0, "maximum_km": 5, "fee": 8}])
    mock_routes(monkeypatch, 2000)
    stale = quote(client, tenant, auth_headers)
    assert stale.status_code == 201, stale.text
    with SessionLocal() as db:
        original_snapshot = deepcopy(db.get(DeliveryQuote, stale.json()["id"]).input_snapshot)
    updated = update_profile(client, tenant, auth_headers, "change-origin", **change)
    current_configuration = client.get(url(tenant), headers=auth_headers).json()
    assert current_configuration["version"] == stale.json()["configuration_version"] + 1
    stale_version = quote(client, tenant, auth_headers, key="stale-version")
    assert stale_version.status_code == 409
    assert stale_version.json()["code"] == "DELIVERY_CONFIGURATION_STALE"
    assert quote(client, tenant, auth_headers).json() == stale.json()
    blocked = order(client, tenant, auth_headers, stale.json()["id"])
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "DELIVERY_CONFIGURATION_STALE"
    assert_no_orders()
    with SessionLocal() as db:
        assert db.get(DeliveryQuote, stale.json()["id"]).input_snapshot == original_snapshot
        assert db.scalar(select(IdempotencyRecord.id).where(IdempotencyRecord.idempotency_key == "order")) is None
    routes_origins = []

    def routes(*args, **kwargs):
        routes_origins.append(kwargs["json"]["origin"]["location"]["latLng"])
        return httpx.Response(200, json={"routes": [{"distanceMeters": 3000}]}, request=httpx.Request("POST", args[0]))

    monkeypatch.setattr(httpx, "post", routes)
    refreshed = quote(client, tenant, auth_headers, key="new-origin-quote",
                      expected_configuration_version=current_configuration["version"])
    assert refreshed.status_code == 201
    assert routes_origins == [{"latitude": updated["latitude"], "longitude": updated["longitude"]}]
    assert order(client, tenant, auth_headers, refreshed.json()["id"]).status_code == 201


@pytest.mark.parametrize("mode", ["bands", "distance"])
def test_policy_origin_quote_survives_unrelated_branch_coordinate_change(client, tenant, auth_headers, monkeypatch, mode):
    configure(client, tenant, auth_headers, delivery_mode=mode, distance_max_km=5,
              bands=[{"minimum_km": 0, "maximum_km": 5, "fee": 8}])
    mock_routes(monkeypatch, 2000)
    original = quote(client, tenant, auth_headers)
    assert original.status_code == 201
    update_profile(client, tenant, auth_headers, "unrelated-branch-origin", latitude=0, longitude=0)
    assert client.get(url(tenant), headers=auth_headers).json()["version"] == original.json()["configuration_version"]
    assert order(client, tenant, auth_headers, original.json()["id"]).status_code == 201


def test_branch_name_change_does_not_invalidate_fallback_origin(client, tenant, auth_headers, monkeypatch):
    update_profile(client, tenant, auth_headers, "initial-origin", latitude=0, longitude=0)
    configure(client, tenant, auth_headers, delivery_mode="distance", distance_max_km=5,
              delivery_policy={**POLICY, "origin": None})
    mock_routes(monkeypatch, 2000)
    original = quote(client, tenant, auth_headers)
    assert original.status_code == 201
    update_profile(client, tenant, auth_headers, "rename", name="Renamed branch", phone="1234567")
    assert order(client, tenant, auth_headers, original.json()["id"]).status_code == 201


def test_fixed_quote_does_not_depend_on_branch_coordinates(client, tenant, auth_headers):
    configure(client, tenant, auth_headers)
    original = quote(client, tenant, auth_headers)
    assert original.status_code == 201
    update_profile(client, tenant, auth_headers, "branch-origin", latitude=0, longitude=0)
    assert order(client, tenant, auth_headers, original.json()["id"]).status_code == 201


def test_manual_outside_band_confirmation_does_not_bypass_origin_change(client, tenant, auth_headers, monkeypatch):
    update_profile(client, tenant, auth_headers, "initial-origin", latitude=-12.1, longitude=-77.1)
    configure(client, tenant, auth_headers, delivery_mode="bands", delivery_policy={**POLICY, "origin": None},
              bands=[{"minimum_km": 0, "maximum_km": 1, "fee": 8}])
    mock_routes(monkeypatch, 2000)
    original = quote(client, tenant, auth_headers, confirmed_fee=7)
    assert original.status_code == 201
    update_profile(client, tenant, auth_headers, "moved-origin", latitude=0, longitude=0)
    blocked = order(client, tenant, auth_headers, original.json()["id"])
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "DELIVERY_CONFIGURATION_STALE"
    assert_no_orders()


def test_legacy_route_quote_keeps_old_origin_compatibility(client, tenant, auth_headers):
    configure(client, tenant, auth_headers, delivery_mode="distance", distance_max_km=5,
              delivery_policy={**POLICY, "origin": None})
    original = quote(client, tenant, auth_headers, expected_configuration_version=None, distance_km=2)
    assert original.status_code == 201
    update_profile(client, tenant, auth_headers, "branch-origin", latitude=0, longitude=0)
    assert order(client, tenant, auth_headers, original.json()["id"]).status_code == 201


def test_origin_version_bump_is_scoped_idempotent_and_audited_once(client, tenant, auth_headers):
    update_profile(client, tenant, auth_headers, "initial-origin", latitude=0, longitude=0)
    configure(client, tenant, auth_headers, delivery_mode="distance", distance_max_km=5,
              delivery_policy={**POLICY, "origin": None})
    with SessionLocal.begin() as db:
        sibling = Branch(business_id=tenant["business_id"], slug="sibling", name="Sibling")
        db.add(sibling)
        db.flush()
        sibling_id = sibling.id
        db.add_all([
            BranchSettings(business_id=tenant["business_id"], branch_id=sibling_id, version=10),
            BranchSettings(business_id=tenant["other_business_id"], branch_id=tenant["other_branch_id"], version=20),
        ])
        before_count = db.scalar(select(func.count(AuditEvent.id)))
        before_branch = db.get(Branch, tenant["branch_id"])
        branch_version, legacy_fee = before_branch.version, before_branch.delivery_fee
    profile_url = f"/api/v1/settings/branches/{tenant['branch_id']}/profile"
    payload = {"expected_version": branch_version, "latitude": -12.1, "longitude": -77.1}
    request_headers = headers(auth_headers, "move-origin")
    changed = client.patch(profile_url, headers=request_headers, json=payload)
    assert changed.status_code == 200
    assert client.patch(profile_url, headers=request_headers, json=payload).json() == changed.json()
    cashier = {**auth_headers, "X-Dev-Role": "cashier"}
    current = client.get(url(tenant), headers=cashier)
    assert current.status_code == 200
    assert current.json()["version"] == 3
    with SessionLocal() as db:
        versions = dict(db.execute(select(BranchSettings.branch_id, BranchSettings.version)).all())
        assert versions == {tenant["branch_id"]: 3, sibling_id: 10, tenant["other_branch_id"]: 20}
        assert db.get(Branch, tenant["branch_id"]).delivery_fee == legacy_fee
        assert db.scalar(select(func.count(AuditEvent.id))) == before_count + 1
        event = db.scalar(select(AuditEvent).order_by(AuditEvent.id.desc()))
        assert event.action == "settings.branch.profile.updated"
        assert event.branch_id == tenant["branch_id"]
        assert event.actor_id == auth_headers["X-Dev-User"]
        assert event.payload["delivery_configuration"] == {
            "before_version": 2, "after_version": 3,
            "before_origin": {"latitude": 0, "longitude": 0, "maps_url": None},
            "after_origin": {"latitude": -12.1, "longitude": -77.1, "maps_url": None},
        }


@pytest.mark.parametrize("values", [
    {"latitude": -12.1, "longitude": -77.1},
    {"maps_url": "https://maps.google.com/?q=-12.1,-77.1"},
    {"name": "New name", "phone": "123456"},
])
def test_unchanged_route_origin_keeps_delivery_version(client, tenant, auth_headers, values):
    update_profile(client, tenant, auth_headers, "initial-origin", latitude=-12.1, longitude=-77.1,
                   maps_url="https://maps.google.com/?q=-12.1,-77.1")
    configure(client, tenant, auth_headers, delivery_mode="distance", distance_max_km=5,
              delivery_policy={**POLICY, "origin": None})
    update_profile(client, tenant, auth_headers, "update-profile", **values)
    assert client.get(url(tenant), headers=auth_headers).json()["version"] == 2


def test_profile_and_delivery_version_rollback_together(client, tenant, auth_headers, monkeypatch):
    update_profile(client, tenant, auth_headers, "initial-origin", latitude=0, longitude=0)
    configure(client, tenant, auth_headers, delivery_mode="distance", distance_max_km=5,
              delivery_policy={**POLICY, "origin": None})
    profile_url = f"/api/v1/settings/branches/{tenant['branch_id']}/profile"
    before = client.get(profile_url, headers=auth_headers).json()
    with SessionLocal() as db:
        audit_count = db.scalar(select(func.count(AuditEvent.id)))

    def fail(*args, **kwargs):
        raise RuntimeError("simulated commit failure")

    monkeypatch.setattr(settings_api, "save_idempotent_response", fail)
    with pytest.raises(RuntimeError, match="simulated"):
        client.patch(profile_url, headers=headers(auth_headers, "failed-origin-change"), json={
            "latitude": -12.1, "longitude": -77.1, "expected_version": before["version"],
        })
    assert client.get(profile_url, headers=auth_headers).json() == before
    assert client.get(url(tenant), headers=auth_headers).json()["version"] == 2
    with SessionLocal() as db:
        assert db.scalar(select(func.count(AuditEvent.id))) == audit_count
        assert db.scalar(select(IdempotencyRecord.id).where(
            IdempotencyRecord.idempotency_key == "failed-origin-change")) is None


def test_first_default_origin_increments_existing_route_configuration(client, tenant, auth_headers):
    configure(client, tenant, auth_headers, delivery_mode="distance", distance_max_km=5,
              delivery_policy={**POLICY, "origin": None})
    update_profile(client, tenant, auth_headers, "first-origin", latitude=0, longitude=0)
    config = client.get(url(tenant), headers=auth_headers).json()
    assert config["version"] == 3
    assert config["branch_origin"] == {"latitude": 0, "longitude": 0, "maps_url": None}


def test_origin_snapshot_still_rejects_legacy_coordinate_change_without_version(client, tenant, auth_headers, monkeypatch):
    update_profile(client, tenant, auth_headers, "initial-origin", latitude=0, longitude=0)
    configure(client, tenant, auth_headers, delivery_mode="distance", distance_max_km=5,
              delivery_policy={**POLICY, "origin": None})
    mock_routes(monkeypatch, 2000)
    original = quote(client, tenant, auth_headers)
    assert original.status_code == 201
    # Simulate an older writer in the isolated fixture; retain the snapshot safety check.
    with SessionLocal.begin() as db:
        branch = db.get(Branch, tenant["branch_id"])
        branch.latitude, branch.longitude = Decimal("-12.1"), Decimal("-77.1")
    assert client.get(url(tenant), headers=auth_headers).json()["version"] == 2
    blocked = order(client, tenant, auth_headers, original.json()["id"])
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "DELIVERY_QUOTE_ORIGIN_CHANGED"
    assert_no_orders()


@pytest.mark.parametrize("mode", ["free", "fixed", "neighborhoods", "quote"])
def test_nonroute_modes_do_not_bump_delivery_version_on_profile_location_change(client, tenant, auth_headers, mode):
    configure(client, tenant, auth_headers, delivery_mode=mode, delivery_policy={**POLICY, "origin": None})
    update_profile(client, tenant, auth_headers, "change-origin", latitude=0, longitude=0)
    assert client.get(url(tenant), headers=auth_headers).json()["version"] == 2
