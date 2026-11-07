"""A manual POS delivery may await its fee without pretending it is free or paid."""
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import (AuditEvent, BranchSettings, DeliveryAssignment, DeliveryQuote, IntegrationEvent,
                        InventoryItem, KitchenTicket, Order, OrderPaymentRequest, Payment, PaymentEvidence)


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Manual delivery tests cannot contact external services")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)


def configure(tenant, *, mode="quote", enabled=True):
    with SessionLocal.begin() as db:
        db.add(BranchSettings(business_id=tenant["business_id"], branch_id=tenant["branch_id"],
            pos_delivery=enabled, digital_delivery=False, delivery_mode=mode, fixed_delivery_fee=7,
            payment_methods={key: ["cash", "yape"] for key in ("delivery", "takeaway", "counter")}))


def create(client, tenant, headers, *, key="manual-delivery", **values):
    return client.post("/api/v1/orders", headers={**headers, "Idempotency-Key": key}, json={
        "branch_id": tenant["branch_id"], "channel": "delivery", "customer_name": "Ana",
        "customer_phone": "+51912345678", "delivery_fee": 0, "allow_pending_delivery_quote": True,
        "items": [{"product_id": tenant["product_id"], "quantity": 1}], **values})


def created(client, tenant, headers, **values):
    response = create(client, tenant, headers, **values)
    assert response.status_code == 201, response.text
    return response.json()


def send(client, headers, order, *, key="manual-send"):
    response = client.post(f"/api/v1/orders/{order['id']}/confirm-and-send",
        headers={**headers, "Idempotency-Key": key}, json={"expected_version": order["version"]})
    assert response.status_code == 200, response.text
    return response.json()


def edit(client, headers, order, *, fee=None, include_fee=True, total=None, key="manual-fee", **values):
    return client.patch(f"/api/v1/orders/{order['id']}",
        headers={**headers, "Idempotency-Key": key}, json={
            "expected_version": order["version"], "expected_total": order["total"] if total is None else total,
            **({"delivery_fee": fee} if include_fee else {}), **values})


@pytest.mark.parametrize("mode", ["quote", "fixed", "free", "distance", "bands", "neighborhoods"])
def test_manual_pending_is_independent_of_digital_mode_and_needs_no_quote_or_address(client, tenant, auth_headers, mode):
    configure(tenant, mode=mode)
    order = created(client, tenant, auth_headers, source="whatsapp_agent")
    assert order["source"] == "pos" and order["channel"] == "delivery"
    assert order["delivery_address"] is None
    assert order["delivery_fee_status"] == "pending_quote" and order["delivery_fee"] == 0
    assert order["known_total"] == 20 and order["final_total"] is None
    assert order["payment_status"] == "pending" and order["delivery_quote_id"] is None
    with SessionLocal() as db:
        assert db.query(DeliveryQuote).count() == db.query(Payment).count() == db.query(OrderPaymentRequest).count() == 0


@pytest.mark.parametrize("values", [
    {"customer_name": ""}, {"customer_name": "   "}, {"customer_phone": None}, {"customer_phone": " "},
    {"channel": "takeaway"}, {"delivery_address": {"delivery_service": "rappi"}},
    {"delivery_fee": 5}, {"delivery_quote_id": "not-a-pos-quote"},
])
def test_pending_opt_in_rejects_missing_contact_or_incompatible_price_and_service(client, tenant, auth_headers, values):
    response = create(client, tenant, auth_headers, **values)
    assert response.status_code == 422, response.text
    with SessionLocal() as db:
        assert db.query(Order).count() == db.query(DeliveryQuote).count() == db.query(Payment).count() == 0
        assert db.get(InventoryItem, tenant["inventory_id"]).quantity == Decimal("10")


def test_disabled_manual_delivery_stays_disabled(client, tenant, auth_headers):
    configure(tenant, enabled=False)
    response = create(client, tenant, auth_headers)
    assert response.status_code == 409 and response.json()["code"] == "SERVICE_DISABLED"
    with SessionLocal() as db:
        assert db.query(Order).count() == 0


def test_deferred_order_prepares_once_but_cannot_collect_or_dispatch_before_fee(client, tenant, auth_headers):
    order = created(client, tenant, auth_headers)
    sent = send(client, auth_headers, order)
    assert send(client, auth_headers, order) == sent
    current = sent["order"]
    assert current["status"] == "sent_to_kitchen" and current["payment_status"] == "pending"
    payment = client.post(f"/api/v1/orders/{order['id']}/payments", headers={**auth_headers, "Idempotency-Key": "early-payment"},
        json={"expected_version": current["version"], "method": "cash", "amount": 20, "register_id": tenant["register_id"]})
    assert payment.status_code == 409 and payment.json()["code"] == "DELIVERY_FEE_PENDING"
    dispatch = client.post(f"/api/v1/orders/{order['id']}/transition", headers=auth_headers,
        json={"expected_version": current["version"], "status": "dispatched"})
    assert dispatch.status_code == 409 and dispatch.json()["code"] == "DELIVERY_FEE_PENDING"
    with SessionLocal() as db:
        assert db.query(KitchenTicket).count() == 1 and db.query(Payment).count() == 0
        assert db.get(InventoryItem, tenant["inventory_id"]).quantity == Decimal("9.5")


@pytest.mark.parametrize("fee", [0, 5])
def test_fee_confirmation_is_audited_idempotent_and_then_allows_normal_single_payment(client, tenant, auth_headers, fee):
    order = send(client, auth_headers, created(client, tenant, auth_headers))["order"]
    response = edit(client, auth_headers, order, fee=fee, total=20 + fee)
    assert response.status_code == 200, response.text
    final = response.json()
    assert final["delivery_fee_status"] == "final" and final["final_total"] == 20 + fee
    assert final["version"] == order["version"] + 1
    assert edit(client, auth_headers, order, fee=fee, total=20 + fee).json() == final
    opened = client.post("/api/v1/cash/sessions/open", headers=auth_headers,
        json={"register_id": tenant["register_id"], "opening_amount": 0})
    assert opened.status_code in {200, 201}, opened.text
    payment_body = {"expected_version": final["version"], "method": "cash", "amount": 20 + fee, "register_id": tenant["register_id"]}
    payment_headers = {**auth_headers, "Idempotency-Key": "normal-manual-payment"}
    paid = client.post(f"/api/v1/orders/{order['id']}/payments", headers=payment_headers, json=payment_body)
    assert paid.status_code == 201, paid.text
    assert paid.json()["order"]["payment_status"] == "paid"
    replay = client.post(f"/api/v1/orders/{order['id']}/payments", headers=payment_headers, json=payment_body)
    assert replay.status_code == 201 and replay.json() == paid.json()
    with SessionLocal() as db:
        assert db.query(Payment).count() == 1 and db.query(KitchenTicket).count() == 1
        assert db.query(OrderPaymentRequest).count() == 0
        assert db.query(IntegrationEvent).filter_by(event_type="order.delivery_fee_confirmed").count() == 0
        audits = list(db.scalars(select(AuditEvent).where(AuditEvent.action == "order.updated")))
        assert len(audits) == 1
        assert audits[0].payload["before"]["delivery_fee_status"] == "pending_quote"
        assert audits[0].payload["after"]["delivery_fee_status"] == "final"


def test_editing_contact_or_destination_does_not_turn_pending_into_free_shipping(client, tenant, auth_headers):
    order = created(client, tenant, auth_headers)
    response = edit(client, auth_headers, order, include_fee=False, customer_name="Ana María")
    assert response.status_code == 200, response.text
    order = response.json()
    assert order["delivery_fee_status"] == "pending_quote"
    response = edit(client, auth_headers, order, include_fee=False, key="address-only", delivery_address={"address": "Calle 1"})
    assert response.status_code == 200, response.text
    assert response.json()["delivery_fee_status"] == "pending_quote" and response.json()["final_total"] is None
    assert response.json()["delivery_address"] == {"address": "Calle 1"}


def test_wrong_total_and_stale_version_do_not_confirm_fee(client, tenant, auth_headers):
    order = created(client, tenant, auth_headers)
    wrong_total = edit(client, auth_headers, order, fee=5)
    assert wrong_total.status_code == 409, wrong_total.text
    stale = edit(client, auth_headers, {**order, "version": 999}, fee=5, total=25, key="stale")
    assert stale.status_code == 409, stale.text
    with SessionLocal() as db:
        current = db.get(Order, order["id"])
        assert current.delivery_fee_status == "pending_quote" and current.version == order["version"]
        assert current.total == Decimal("20") and db.query(Payment).count() == 0


@pytest.mark.parametrize("blocked", ["foreign-business", "foreign-branch", "kitchen"])
def test_fee_confirmation_preserves_scope_and_roles(client, tenant, auth_headers, blocked):
    order = created(client, tenant, auth_headers)
    headers = {**auth_headers}
    if blocked == "foreign-business":
        headers.update({"X-Business-Id": str(tenant["other_business_id"]), "X-Branch-Id": str(tenant["other_branch_id"])})
    elif blocked == "foreign-branch":
        headers["X-Branch-Id"] = str(tenant["other_branch_id"])
    else:
        headers["X-Dev-Role"] = "kitchen"
    response = edit(client, headers, order, fee=5, total=25)
    assert response.status_code in {403, 404}, response.text
    with SessionLocal() as db:
        assert db.get(Order, order["id"]).delivery_fee_status == "pending_quote"


def test_explicit_known_manual_fee_without_quote_preserves_existing_creation_path(client, tenant, auth_headers):
    order = created(client, tenant, auth_headers, allow_pending_delivery_quote=False, delivery_fee=5)
    assert order["delivery_fee_status"] == "final" and order["total"] == 25
    assert order["delivery_quote_id"] is None and order["delivery_address"] is None
    response = edit(client, auth_headers, order, fee=7, total=27)
    assert response.status_code == 200, response.text
    assert response.json()["delivery_fee"] == 7 and response.json()["total"] == 27


def test_absent_pending_flag_remains_a_final_zero_fee(client, tenant, auth_headers):
    response = client.post("/api/v1/orders", headers={**auth_headers, "Idempotency-Key": "legacy-manual"},
        json={"branch_id": tenant["branch_id"], "channel": "delivery", "delivery_fee": 0,
              "items": [{"product_id": tenant["product_id"], "quantity": 1}]})
    assert response.status_code == 201, response.text
    assert response.json()["delivery_fee_status"] == "final" and response.json()["final_total"] == 20


@pytest.mark.parametrize("lock", ["payment", "evidence", "assignment", "dispatched"])
def test_free_shipping_confirmation_still_obeys_fulfillment_locks(client, tenant, auth_headers, lock):
    order = created(client, tenant, auth_headers)
    with SessionLocal.begin() as db:
        if lock == "payment":
            db.add(Payment(business_id=tenant["business_id"], order_id=order["id"], method="cash", amount=1))
        elif lock == "evidence":
            db.add(PaymentEvidence(business_id=tenant["business_id"], order_id=order["id"], provider="yape",
                                   storage_path="synthetic", image_sha256="synthetic"))
        elif lock == "assignment":
            db.add(DeliveryAssignment(business_id=tenant["business_id"], branch_id=tenant["branch_id"],
                                      order_id=order["id"], status="assigned"))
        else:
            db.get(Order, order["id"]).status = "dispatched"
    response = edit(client, auth_headers, order, fee=0)
    assert response.status_code == 409 and response.json()["code"] == "ORDER_FULFILLMENT_LOCKED"
    with SessionLocal() as db:
        assert db.get(Order, order["id"]).delivery_fee_status == "pending_quote"


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_fee_confirmation_has_one_audited_result(client, tenant, auth_headers, same_key):
    from concurrent.futures import ThreadPoolExecutor
    from app.database import engine
    if engine.dialect.name != "postgresql":
        pytest.skip("Concurrent row-lock assertion requires isolated PostgreSQL")
    order = send(client, auth_headers, created(client, tenant, auth_headers))["order"]
    def submit(index):
        return edit(client, auth_headers, order, fee=5, total=25,
                    key="concurrent-fee" if same_key else f"concurrent-fee-{index}")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, range(2)))
    if same_key:
        assert [r.status_code for r in results] == [200, 200], [r.text for r in results]
        assert results[0].json() == results[1].json()
    else:
        assert sorted(r.status_code for r in results) == [200, 409], [r.text for r in results]
    with SessionLocal() as db:
        saved = db.get(Order, order["id"])
        assert saved.delivery_fee_status == "final" and saved.total == 25 and saved.version == order["version"] + 1
        assert db.query(AuditEvent).filter_by(action="order.updated").count() == 1
        assert db.query(KitchenTicket).count() == 1 and db.query(OrderPaymentRequest).count() == db.query(Payment).count() == 0
