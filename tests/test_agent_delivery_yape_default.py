"""Human shipping quotes preserve an agent customer's original Yape choice."""
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import (AuditEvent, BranchSettings, IntegrationEvent,
                        KitchenTicket, Order, OrderPaymentRequest, Payment, PrintJob)
from test_agent_checkout import setup


def confirm(client, headers, order, **extra):
    return client.patch(f"/api/v1/orders/{order['id']}/delivery-fee",
        headers={**headers, "Idempotency-Key": "shipping-only"},
        json={"expected_version": order["version"], "amount": 5, **extra})


@pytest.mark.parametrize("source", ["whatsapp_agent", "agent", "n8n", "whatsapp"])
def test_yape_default_for_all_agent_origins_is_one_shipping_request_without_money(client, tenant, auth_headers, source):
    _, order = setup(client, tenant, auth_headers, quote=True)
    with SessionLocal.begin() as db:
        db.get(Order, order["id"]).source = source
    first = confirm(client, auth_headers, order)
    assert first.status_code == 200, first.text
    result = first.json()
    request = result["payment_request"]
    assert request["method"] == "yape" and request["amount"] == 5 and request["status"] == "pending"
    assert result["order"]["payment_method"] == "yape"
    assert result["order"]["products_total"] == 20 and result["order"]["total"] == 25
    # Recover a lost response using its original receipt even after configuration changes.
    with SessionLocal.begin() as db:
        db.scalar(select(BranchSettings)).payment_methods = {"delivery": ["cash"]}
    replay = confirm(client, auth_headers, order)
    assert replay.status_code == 200 and replay.json() == result
    with SessionLocal() as db:
        assert db.query(OrderPaymentRequest).count() == 1
        assert db.query(IntegrationEvent).filter_by(event_type="order.delivery_fee_confirmed").count() == 1
        audit = db.scalar(select(AuditEvent).where(AuditEvent.action == "order.delivery_fee_confirmed"))
        assert audit.payload["method"] == "yape"
        assert db.query(Payment).count() == db.query(KitchenTicket).count() == db.query(PrintJob).count() == 0


@pytest.mark.parametrize("original,explicit,enabled,expected", [
    ("yape", "cash", ["cash", "yape"], "cash"),
    ("cash", None, ["cash", "yape"], "unselected"),
    ("cash", "yape", ["cash", "yape"], "yape"),
    ("yape", None, ["cash"], "unselected"),
    ("yape", None, [], "unselected"),
])
def test_explicit_choice_cash_original_and_disabled_yape_keep_their_behavior(client, tenant, auth_headers, original, explicit, enabled, expected):
    _, order = setup(client, tenant, auth_headers, quote=True, method=original)
    with SessionLocal.begin() as db:
        db.scalar(select(BranchSettings)).payment_methods = {"delivery": enabled}
    response = confirm(client, auth_headers, order, **({"method": explicit} if explicit else {}))
    assert response.status_code == 200, response.text
    assert response.json()["payment_request"]["method"] == expected
    assert response.json()["order"]["payment_method"] == original


def test_missing_payment_configuration_does_not_invent_yape_availability(client, tenant, auth_headers):
    _, order = setup(client, tenant, auth_headers, quote=True)
    with SessionLocal.begin() as db:
        db.delete(db.scalar(select(BranchSettings)))
    response = confirm(client, auth_headers, order)
    assert response.status_code == 200, response.text
    assert response.json()["payment_request"]["method"] == "unselected"


def test_free_shipping_is_waived_not_a_payment(client, tenant, auth_headers):
    _, order = setup(client, tenant, auth_headers, quote=True)
    response = confirm(client, auth_headers, order, amount=0)
    assert response.status_code == 200, response.text
    request = response.json()["payment_request"]
    assert request["method"] == "yape" and request["amount"] == 0 and request["status"] == "waived"
    with SessionLocal() as db:
        assert db.query(Payment).count() == 0


def test_requoted_agent_shipping_uses_same_default_without_recharging_products(client, tenant, auth_headers):
    from test_agent_checkout import open_cash
    from test_agent_domain_revision import setup_order, receipt, approve, current, preview, change, DESTINATION
    headers, order = setup_order(client, tenant, auth_headers, method="yape", channel="delivery", fee=0)
    original = receipt(client, headers, order)
    open_cash(client, auth_headers, tenant)
    assert approve(client, auth_headers, tenant, original).status_code == 200
    with SessionLocal.begin() as db:
        config = db.scalar(select(BranchSettings))
        config.delivery_mode = "quote"
        config.version += 1
    order = current(client, headers, order)
    proposal = preview(client, headers, order, destination={**DESTINATION, "address": "Otra Calle 456"})
    changed = change(client, headers, order, proposal)
    assert changed.status_code == 200, changed.text
    response = confirm(client, auth_headers, changed.json()["order"])
    assert response.status_code == 200, response.text
    assert response.json()["payment_request"]["method"] == "yape"
    assert response.json()["payment_request"]["amount"] == 5
    with SessionLocal() as db:
        assert db.query(Payment).count() == 1
        assert db.scalar(select(Payment)).amount == Decimal("20")


@pytest.mark.parametrize("blocked", ["manual", "foreign-business", "foreign-branch", "waiter", "stale-version", "explicit-disabled"])
def test_default_does_not_bypass_origin_scope_permissions_version_or_method_controls(client, tenant, auth_headers, blocked):
    _, order = setup(client, tenant, auth_headers, quote=True)
    headers = auth_headers
    extra = {}
    if blocked == "manual":
        with SessionLocal.begin() as db:
            db.get(Order, order["id"]).source = "pos"
    elif blocked == "foreign-business":
        headers = {**headers, "X-Business-Id": str(tenant["other_business_id"]), "X-Branch-Id": str(tenant["other_branch_id"])}
    elif blocked == "foreign-branch":
        headers = {**headers, "X-Branch-Id": str(tenant["other_branch_id"])}
    elif blocked == "waiter":
        headers = {**headers, "X-Dev-Role": "waiter"}
    elif blocked == "stale-version":
        with SessionLocal.begin() as db:
            db.get(Order, order["id"]).version += 1
    else:
        with SessionLocal.begin() as db:
            db.scalar(select(BranchSettings)).payment_methods = {"delivery": ["cash"]}
        extra["method"] = "yape"
    response = confirm(client, headers, order, **extra)
    assert response.status_code in {403, 404, 409}, response.text
    with SessionLocal() as db:
        assert db.get(Order, order["id"]).delivery_fee_status == "pending_quote"
        assert db.query(OrderPaymentRequest).count() == db.query(IntegrationEvent).count() == db.query(Payment).count() == 0
