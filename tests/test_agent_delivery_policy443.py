"""Candidate443: current POS shipping, isolated TestClient/SQLite, no provider."""
from copy import deepcopy
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.agent_delivery import POLICY_VERSION
from app.database import SessionLocal
from app.models import (Branch, BranchSettings, DeliveryQuote, IntegrationEvent, KitchenTicket,
                        Order, Payment, Product, PrintJob, Promotion)
from app.settings_service import create_delivery_quote
from test_agent_delivery_lid import DESTINATION, draft_body, preview, settings
from test_escalar_integrations import create_credential, integration_headers
from test_restaurant_agent_boundary import SENDER, agent_headers, leased


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No external service is allowed in shipping443 tests")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr("app.settings_service.GoogleRoutesDistanceProvider.distance_km", forbidden)


def settings_snapshot(db):
    row = db.scalar(select(BranchSettings))
    return deepcopy({column.name: getattr(row, column.name) for column in row.__table__.columns})


def create_agent_order(client, headers, tenant, quoted, *, key="order"):
    response = client.post("/api/v1/integrations/orders/draft",
        headers={**headers, "Idempotency-Key": key},
        json={**draft_body(tenant, quoted), "allow_pending_delivery_quote": quoted["final_total"] is None})
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize("mode", ["quote", "distance", "bands", "neighborhoods", "free", "fixed"])
def test_agent_effective_mode_quote_and_registration_preserve_pos_configuration(client, tenant, agent_headers, mode):
    settings(tenant, mode)
    with SessionLocal.begin() as db:
        db.scalar(select(BranchSettings)).free_delivery_threshold = 1
    with SessionLocal() as db:
        original = settings_snapshot(db)
    _, headers = leased(client, agent_headers)
    context = client.get("/api/v1/integrations/context", headers=headers).json()
    manual = mode in {"quote", "distance", "bands", "neighborhoods"}
    assert context["delivery"]["mode"] == ("quote" if manual else mode)
    assert context["delivery"]["configured_mode"] == mode
    assert context["delivery"]["requires_destination"] is manual
    assert context["delivery"]["address_required_for_order"] is True
    assert context["delivery"]["free_delivery_threshold"] is None
    assert context["delivery"]["policy"] is None and context["delivery"]["bands"] == []
    assert context["delivery"]["distance_fee_per_km"] is None
    result = preview(client, headers, tenant, delivery_address=DESTINATION)
    assert result.status_code == 200, result.text
    quoted = result.json()
    fee = None if manual else 0 if mode == "free" else 4
    assert quoted["delivery_fee"] == fee
    assert quoted["products_total"] == 20
    assert quoted["known_total"] == (24 if mode == "fixed" else 20)
    assert quoted["final_total"] == (None if manual else 20 + fee)
    assert quoted["quote"]["distance_km"] is None
    assert preview(client, headers, tenant, delivery_address=DESTINATION).json() == quoted
    order = create_agent_order(client, headers, tenant, quoted)
    assert order["products_total"] == 20 and order["total"] == quoted["known_total"]
    assert order["delivery_fee_status"] == ("pending_quote" if manual else "final")
    state = client.get(f"/api/v1/integrations/orders/{order['id']}/customer-state",
                       headers=headers, params={"sender": SENDER})
    assert state.status_code == 200 and state.json()["products_total"] == 20
    with SessionLocal() as db:
        assert settings_snapshot(db) == original
        quote = db.get(DeliveryQuote, quoted["delivery_quote_id"])
        assert quote.input_snapshot["configured_mode"] == mode
        assert quote.input_snapshot["agent_policy_version"] == POLICY_VERSION
        assert quote.input_snapshot["sender"] == SENDER and quote.input_snapshot["destination"] == DESTINATION
        assert db.query(DeliveryQuote).count() == db.query(Order).count() == 1
        assert db.query(Payment).count() == db.query(KitchenTicket).count() == db.query(PrintJob).count() == 0


@pytest.mark.parametrize("mode,expected", [("fixed", 0), ("neighborhoods", 0), ("distance", 0), ("bands", 0)])
def test_general_pos_engine_retains_threshold_and_route_behavior(tenant, monkeypatch, mode, expected):
    settings(tenant, mode)
    calls = []
    class Routes:
        def distance_km(self, origin, destination):
            calls.append(deepcopy(destination))
            return Decimal(2)
    with SessionLocal.begin() as db:
        config = db.scalar(select(BranchSettings))
        config.free_delivery_threshold = 1
        db.flush()  # Strict quotes reload the persisted configuration under lock.
        quote = create_delivery_quote(db, db.get(Branch, tenant["branch_id"]), config,
            subtotal=Decimal(20), destination=DESTINATION, expected_configuration_version=config.version,
            distance_provider=Routes())
        assert quote.fee == expected and quote.mode == mode
        assert "agent_policy_version" not in quote.input_snapshot
    assert len(calls) == (1 if mode in {"distance", "bands"} else 0)


def test_generic_context_and_preview_keep_existing_neighborhood_price(client, tenant, auth_headers):
    settings(tenant, "neighborhoods")
    headers = integration_headers(create_credential(client, tenant, auth_headers)["token"])
    context = client.get("/api/v1/integrations/context", headers=headers).json()
    assert context["delivery"]["mode"] == "neighborhoods"
    assert "address_required_for_order" not in context["delivery"]
    assert context["delivery"]["policy"]["neighborhoods"][0]["fee"] == 8
    response = preview(client, headers, tenant, delivery_address=DESTINATION)
    assert response.status_code == 200 and response.json()["delivery_fee"] == 8


@pytest.mark.parametrize("mode", ["quote", "distance", "bands", "neighborhoods", "free", "fixed"])
def test_agent_fulfillment_uses_same_policy_and_versioned_quotes(client, tenant, agent_headers, mode):
    settings(tenant, mode)
    with SessionLocal.begin() as db:
        db.scalar(select(BranchSettings)).free_delivery_threshold = 1
    _, headers = leased(client, agent_headers)
    body = {"branch_id": tenant["branch_id"], "channel": "takeaway", "payment_method": "cash",
        "customer_phone": SENDER, "whatsapp_chat_id": SENDER, "quoted_total": 20,
        "items": [{"product_id": tenant["product_id"], "quantity": 1}]}
    created = client.post("/api/v1/integrations/orders/draft", headers={**headers, "Idempotency-Key": "initial"}, json=body)
    assert created.status_code == 201, created.text
    order = created.json()
    path = f"/api/v1/integrations/orders/{order['id']}"
    proposal = {"sender": SENDER, "expected_version": order["version"], "channel": "delivery", "delivery_address": DESTINATION}
    result = client.post(path + "/fulfillment-preview", headers={**headers, "Idempotency-Key": "change-preview"}, json=proposal)
    assert result.status_code == 200, result.text
    quoted = result.json()
    fee = None if mode in {"quote", "distance", "bands", "neighborhoods"} else 0 if mode == "free" else 4
    assert quoted["quote"]["fee"] == fee and quoted["products_total"] == 20
    committed = client.patch(path + "/fulfillment", headers={**headers, "Idempotency-Key": "change"},
        json={**proposal, "quote_id": quoted["quote"]["id"], "expected_delivery_fee": fee})
    assert committed.status_code == 200, committed.text
    changed = committed.json()["order"]
    assert changed["id"] == order["id"] and changed["folio"] == order["folio"]
    assert changed["total"] == 20 + (fee or 0)
    assert changed["delivery_fee_status"] == ("pending_quote" if fee is None else "final")


@pytest.mark.parametrize("tamper", ["policy", "configuration", "sender", "destination"])
def test_checkout_rejects_old_policy_or_modified_quote(client, tenant, agent_headers, tamper):
    settings(tenant, "distance")
    _, headers = leased(client, agent_headers)
    quoted = preview(client, headers, tenant, delivery_address=DESTINATION).json()
    with SessionLocal.begin() as db:
        quote = db.get(DeliveryQuote, quoted["delivery_quote_id"])
        if tamper == "configuration":
            db.scalar(select(BranchSettings)).version += 1
        else:
            key, value = {"policy": ("agent_policy_version", "previous"), "sender": ("sender", "51999900002"),
                "destination": ("destination", {"address": "Otra calle"})}[tamper]
            quote.input_snapshot = {**quote.input_snapshot, key: value}
    result = client.post("/api/v1/integrations/orders/draft", headers={**headers, "Idempotency-Key": "reject"},
        json={**draft_body(tenant, quoted), "allow_pending_delivery_quote": True})
    assert result.status_code == 409, result.text
    with SessionLocal() as db:
        assert db.query(Order).count() == 0


@pytest.mark.parametrize("method", [None, "cash", "yape"])
def test_staff_fee_confirmation_durable_event_idempotency_and_no_payment(client, tenant, auth_headers, agent_headers, monkeypatch, method):
    settings(tenant, "distance")
    _, headers = leased(client, agent_headers)
    quoted = preview(client, headers, tenant, delivery_address=DESTINATION).json()
    order = create_agent_order(client, headers, tenant, quoted)
    body = {"expected_version": order["version"], "amount": 5, "method": method}
    path = f"/api/v1/orders/{order['id']}/delivery-fee"
    key = {**auth_headers, "Idempotency-Key": "staff-fee"}
    result = client.patch(path, headers=key, json=body)
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["order"]["total"] == 25 and data["order"]["products_total"] == 20
    assert data["payment_request"]["method"] == (method or "unselected")
    assert client.patch(path, headers=key, json=body).json() == data
    assert client.patch(path, headers=key, json={**body, "amount": 6}).status_code == 409
    assert client.patch(path, headers={**auth_headers, "Idempotency-Key": "new-key-old-version"}, json=body).status_code == 409
    generic = integration_headers(create_credential(client, tenant, auth_headers)["token"])
    state = client.get(f"/api/v1/integrations/orders/{order['id']}/status", headers=generic).json()
    notification = state["delivery_fee_notification"]
    assert notification["quote_id"] == quoted["delivery_quote_id"] and notification["cash_available"] is True
    assert notification["payment_request"]["payable_amount"] == 5
    with SessionLocal() as db:
        events = list(db.scalars(select(IntegrationEvent).where(IntegrationEvent.event_type == "order.delivery_fee_confirmed")))
        assert len(events) == 1 and events[0].id == data["event_id"]
        payload = events[0].payload
        assert payload["payment_request_id"] == data["payment_request"]["id"]
        assert payload["quote_id"] == quoted["delivery_quote_id"] and payload["delivery_fee"] == payload["request_amount"] == 5
        assert payload["currency"] == "PEN" and payload["request_version"] == data["payment_request"]["version"]
        assert db.query(Payment).count() == db.query(KitchenTicket).count() == db.query(PrintJob).count() == 0
    with SessionLocal.begin() as db:
        db.add(Payment(business_id=tenant["business_id"], order_id=order["id"],
            method="cash", amount=25, status="confirmed"))
    after = client.get(f"/api/v1/integrations/orders/{order['id']}/status", headers=generic).json()["delivery_fee_notification"]
    assert after["payment_request"]["status"] == "pending"
    assert after["remaining_amount"] == after["payment_request"]["payable_amount"] == 0
    # Unit boundary: an already recorded staff refund is a read-only projection,
    # not a new refund or an alteration to the historical payment.
    monkeypatch.setattr("app.cash_service.order_refunds", lambda db, order: [{"amount": 3}])
    after_refund = client.get(f"/api/v1/integrations/orders/{order['id']}/status", headers=generic).json()["delivery_fee_notification"]
    assert after_refund["remaining_amount"] == after_refund["payment_request"]["payable_amount"] == 3


def test_fresh_catalog_and_checkout_do_not_reuse_previous_availability(client, tenant, agent_headers):
    _, headers = leased(client, agent_headers)
    path = "/api/v1/integrations/context/catalog"
    first = client.get(path, headers=headers)
    assert first.headers["cache-control"] == "no-store" and first.json()["products"][0]["available"] is True
    with SessionLocal.begin() as db:
        db.get(Product, tenant["product_id"]).available = False
    second = client.get(path, headers=headers)
    assert second.json()["products"][0]["available"] is False
    rejected = client.post("/api/v1/integrations/orders/preview", headers=headers,
        json={"channel": "takeaway", "items": [{"product_id": tenant["product_id"], "quantity": 1}]})
    assert rejected.status_code == 422 and rejected.json()["detail"] == f"Product {tenant['product_id']} is unavailable"
    with SessionLocal() as db:
        assert db.query(Order).count() == 0


def test_agent_estimates_and_payment_details_are_pos_scoped_without_defaults(client, tenant, agent_headers):
    _, headers = leased(client, agent_headers)
    assert client.get("/api/v1/integrations/context", headers=headers).json()["estimates"] is None
    settings(tenant, "fixed")
    with SessionLocal.begin() as db:
        config = db.scalar(select(BranchSettings))
        config.delivery_min_minutes, config.delivery_max_minutes, config.pickup_minutes = 38, 57, 19
        config.payment_methods = {"delivery": ["cash", "yape", "plin", "card"], "takeaway": ["cash", "yape"]}
        branch = db.get(Branch, tenant["branch_id"])
        branch.yape_number, branch.payment_recipient_name = "999111222", "Titular sintético"
    data = client.get("/api/v1/integrations/context", headers=headers).json()
    assert data["estimates"]["delivery_min_minutes"] == 38 and data["estimates"]["pickup_minutes"] == 19
    assert data["estimates"]["delivery_scope"] == "order_confirmation_to_delivery"
    assert data["estimates"]["preparation_minutes"] is None and data["estimates"]["travel_minutes"] is None
    assert data["payments"]["methods"]["delivery"] == ["cash", "yape"]
    assert data["payments"]["yape"]["number"] == "999111222"
    assert data["payments"]["yape"]["recipient_name"] == "Titular sintético"
    assert data["payments"]["yape"]["qr_configured"] is False and data["payments"]["yape"]["qr_url"] is None


def test_product_discount_survives_ignored_shipping_threshold(client, tenant, agent_headers):
    from test_agent_promotion_contract import add_promotion
    settings(tenant, "fixed")
    with SessionLocal.begin() as db:
        config = db.scalar(select(BranchSettings))
        config.free_delivery_threshold = 1
        promo_id = add_promotion(db, tenant, "Productos 25%", promotion_type="product_discount",
            target_scope="products", target=tenant["product_id"], discount_type="percentage", discount_value=25)
        db.get(Promotion, promo_id).service_channels = ["digital_delivery"]
    _, headers = leased(client, agent_headers)
    response = preview(client, headers, tenant, delivery_address=DESTINATION)
    assert response.status_code == 200, response.text
    quoted = response.json()
    assert quoted["subtotal"] == "20.00" and quoted["discount"] == "5.00"
    assert quoted["products_total"] == 15 and quoted["delivery_fee"] == 4 and quoted["known_total"] == 19
    order = create_agent_order(client, headers, tenant, quoted)
    assert order["products_total"] == 15 and order["delivery_fee"] == 4 and order["total"] == 19


@pytest.mark.parametrize("mode", ["fixed", "quote", "distance", "bands", "neighborhoods"])
def test_minimum_delivery_purchase_is_not_a_free_shipping_promotion(client, tenant, agent_headers, mode):
    settings(tenant, mode)
    with SessionLocal.begin() as db:
        config = db.scalar(select(BranchSettings))
        config.minimum_order_amount, config.free_delivery_threshold = 25, 1
    _, headers = leased(client, agent_headers)
    rejected = preview(client, headers, tenant, delivery_address=DESTINATION)
    assert rejected.status_code == 409 and rejected.json()["code"] == "DELIVERY_MINIMUM_REQUIRED"
    with SessionLocal() as db:
        assert db.query(DeliveryQuote).count() == db.query(Order).count() == 0


def test_fee_event_failure_rolls_back_confirmation_before_retry(client, tenant, auth_headers, agent_headers, monkeypatch):
    from app import services
    settings(tenant, "quote")
    _, headers = leased(client, agent_headers)
    quoted = preview(client, headers, tenant, delivery_address=DESTINATION).json()
    order = create_agent_order(client, headers, tenant, quoted)
    body = {"expected_version": order["version"], "amount": 5, "method": None}
    path = f"/api/v1/orders/{order['id']}/delivery-fee"
    key = {**auth_headers, "Idempotency-Key": "recover-confirmation"}
    original = services.create_integration_event
    def broken(*args, **kwargs):
        raise RuntimeError("isolated event insertion failure")
    monkeypatch.setattr(services, "create_integration_event", broken)
    with pytest.raises(RuntimeError, match="isolated event insertion failure"):
        client.patch(path, headers=key, json=body)
    with SessionLocal() as db:
        current = db.get(Order, order["id"])
        assert current.version == order["version"] and current.delivery_fee_status == "pending_quote"
        assert current.delivery_fee == 0 and db.query(IntegrationEvent).count() == 0
    monkeypatch.setattr(services, "create_integration_event", original)
    recovered = client.patch(path, headers=key, json=body)
    assert recovered.status_code == 200, recovered.text
    with SessionLocal() as db:
        assert db.query(IntegrationEvent).count() == 1 and db.get(Order, order["id"]).delivery_fee == 5


@pytest.mark.parametrize("method", [None, "cash", "yape"])
def test_zero_staff_fee_preserves_method_without_a_payment(client, tenant, auth_headers, agent_headers, method):
    settings(tenant, "neighborhoods")
    _, headers = leased(client, agent_headers)
    quoted = preview(client, headers, tenant, delivery_address=DESTINATION).json()
    order = create_agent_order(client, headers, tenant, quoted)
    result = client.patch(f"/api/v1/orders/{order['id']}/delivery-fee",
        headers={**auth_headers, "Idempotency-Key": "zero-fee"},
        json={"expected_version": order["version"], "amount": 0, "method": method})
    assert result.status_code == 200, result.text
    request = result.json()["payment_request"]
    assert request["status"] == "waived" and request["method"] == (method or "unselected")
    assert result.json()["order"]["total"] == 20 and result.json()["order"]["delivery_fee_status"] == "final"
    with SessionLocal() as db:
        assert db.query(Payment).count() == 0
        event = db.scalar(select(IntegrationEvent))
        assert event.payload["delivery_fee"] == event.payload["request_amount"] == 0


@pytest.mark.parametrize("change", ["number", "recipient", "qr", "removed"])
def test_qr_revision_rejects_changed_payment_instructions_before_storage(client, tenant, agent_headers, monkeypatch, change):
    with SessionLocal.begin() as db:
        branch = db.get(Branch, tenant["branch_id"])
        branch.yape_number, branch.payment_recipient_name, branch.yape_qr_storage_path = "999111222", "Titular A", "private/test-a.png"
    context = client.get("/api/v1/integrations/context", headers=agent_headers).json()
    revision = context["payments"]["yape"]["revision"]
    assert len(revision) == 64 and set(revision) <= set("0123456789abcdef")
    assert "private/test-a.png" not in str(context)
    loads = []
    async def storage(path):
        loads.append(path)
        return b"synthetic-qr", "image/png"
    monkeypatch.setattr("app.api.load_private_file", storage)
    path = "/api/v1/integrations/context/yape-qr"
    matching = client.get(path, headers=agent_headers, params={"expected_revision": revision})
    assert matching.status_code == 200 and loads == ["private/test-a.png"]
    with SessionLocal.begin() as db:
        branch = db.get(Branch, tenant["branch_id"])
        field, value = {"number": ("yape_number", "999222333"), "recipient": ("payment_recipient_name", "Titular B"),
            "qr": ("yape_qr_storage_path", "private/test-b.png"), "removed": ("yape_qr_storage_path", None)}[change]
        setattr(branch, field, value)
    stale = client.get(path, headers=agent_headers, params={"expected_revision": revision})
    assert stale.status_code == 409 and stale.json()["code"] == "YAPE_CONFIGURATION_STALE"
    assert loads == ["private/test-a.png"]
    current = client.get("/api/v1/integrations/context", headers=agent_headers).json()["payments"]["yape"]
    assert current["revision"] != revision
    # Explicit compatibility: callers without a revision keep their existing
    # behavior; the consumer runtime must supply the matching context revision.
    assert client.get(path, headers=agent_headers).status_code == (404 if change == "removed" else 200)


def test_yape_revision_depends_on_tenant_and_payment_fields_only(client, tenant, agent_headers):
    from app.agent_context import yape_revision
    first = client.get("/api/v1/integrations/context", headers=agent_headers).json()["payments"]["yape"]["revision"]
    with SessionLocal.begin() as db:
        branch = db.get(Branch, tenant["branch_id"])
        branch.agent_name = "Otro nombre público"
        branch.version += 1
        other = db.get(Branch, tenant["other_branch_id"])
        assert yape_revision(other) != yape_revision(branch)
    second = client.get("/api/v1/integrations/context", headers=agent_headers).json()["payments"]["yape"]["revision"]
    assert first == second


@pytest.mark.parametrize("mode", ["free", "fixed"])
def test_known_fee_without_destination_does_not_authorize_delivery_order(client, tenant, agent_headers, mode):
    settings(tenant, mode)
    _, headers = leased(client, agent_headers)
    response = preview(client, headers, tenant)
    assert response.status_code == 200, response.text
    quoted = response.json()
    assert quoted["delivery_fee"] == (0 if mode == "free" else 4)
    assert quoted["final_total"] == (20 if mode == "free" else 24)
    assert quoted["delivery_fee_status"] == "final" and quoted["delivery_quote_id"] is None
    rejected = client.post("/api/v1/integrations/orders/draft",
        headers={**headers, "Idempotency-Key": "missing-delivery-address"},
        json={**draft_body(tenant, quoted), "delivery_address": None})
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["code"] == "DELIVERY_DESTINATION_REQUIRED"
    with SessionLocal() as db:
        assert db.query(Order).count() == db.query(Payment).count() == 0
        assert db.query(KitchenTicket).count() == db.query(PrintJob).count() == 0


@pytest.mark.parametrize("mode", ["quote", "distance", "bands", "neighborhoods", "free", "fixed"])
def test_disabled_projection_requires_neither_quote_destination_nor_delivery_address(mode):
    from app.agent_delivery import project_delivery
    published = {"mode": mode, "enabled": False, "fee": 0 if mode == "free" else 4,
                 "bands": [{"fee": 99}], "free_delivery_threshold": 1}
    original = deepcopy(published)
    result = project_delivery(published)
    assert result["fee_status"] == "disabled"
    assert result["requires_destination"] is False
    assert result["address_required_for_order"] is False
    assert result["requires_quote"] is False
    assert published == original
