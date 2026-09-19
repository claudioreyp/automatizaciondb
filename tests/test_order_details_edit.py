from decimal import Decimal

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import AuditEvent, Branch, BranchSettings, DeliveryAssignment, Modifier, ModifierGroup, Order, Payment, PaymentEvidence, Product, ProductModifierGroup, ProductVariant


def new_order(client, tenant, auth_headers):
    response = client.post("/api/v1/orders", headers={**auth_headers, "Idempotency-Key": "details-create"}, json={
        "branch_id": tenant["branch_id"], "channel": "takeaway", "customer_name": "Pepe", "customer_phone": "+51912345678",
        "items": [{"product_id": tenant["product_id"], "quantity": 1}],
    })
    assert response.status_code == 201, response.text
    return response.json()


def edit(client, auth_headers, order, changes, *, key="details-edit", total=None):
    return client.patch(f"/api/v1/orders/{order['id']}", headers={**auth_headers, "Idempotency-Key": key}, json={
        "expected_version": order["version"], "expected_total": order["total"] if total is None else total, **changes,
    })


def test_edit_after_kitchen_is_idempotent_audited_and_keeps_snapshot(client, tenant, auth_headers):
    order = new_order(client, tenant, auth_headers)
    sent = client.post(f"/api/v1/orders/{order['id']}/confirm-and-send", headers={**auth_headers, "Idempotency-Key": "send"}, json={"expected_version": order["version"]}).json()
    order = sent["order"]
    before = client.get(f"/api/v1/orders/{order['id']}/detail", headers=auth_headers).json()
    response = edit(client, auth_headers, order, {"channel": "counter", "customer_name": "Nuevo nombre"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == order["status"]
    assert response.json()["version"] == order["version"] + 1
    assert edit(client, auth_headers, order, {"channel": "counter", "customer_name": "Nuevo nombre"}).json() == response.json()
    after = client.get(f"/api/v1/orders/{order['id']}/detail", headers=auth_headers).json()
    assert after["tickets"] == before["tickets"]
    with SessionLocal() as db:
        events = list(db.scalars(select(AuditEvent).where(AuditEvent.action == "order.updated")))
        assert len(events) == 1
        assert events[0].branch_id == tenant["branch_id"]
        assert events[0].payload["before"]["customer_name"] == "Pepe"


def test_total_difference_rolls_back_until_confirmed(client, tenant, auth_headers):
    order = new_order(client, tenant, auth_headers)
    changes = {"channel": "delivery", "delivery_address": {"delivery_service": "own", "address": "Calle 1", "reference": "Puerta verde"}, "delivery_fee": 7}
    response = edit(client, auth_headers, order, changes)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ORDER_TOTAL_CHANGED"
    assert response.json()["detail"]["total"] == 27
    with SessionLocal() as db:
        stored = db.get(Order, order["id"])
        assert stored.channel == "takeaway"
        assert stored.version == order["version"]
    response = edit(client, auth_headers, order, changes, total=27)
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 27
    assert response.json()["delivery_fee_status"] == "final"


def test_customer_edits_preserve_historical_discounts_and_unit_prices(client, tenant, auth_headers):
    order = new_order(client, tenant, auth_headers)
    with SessionLocal.begin() as db:
        stored = db.get(Order, order["id"])
        stored.discount = Decimal("5")
        stored.promotion_discount = Decimal("5")
        stored.total = Decimal("15")
        stored.items[0].promotion_discount = Decimal("5")
        stored.items[0].promotion_snapshot = {"name": "Oferta anterior"}
        db.get(Product, tenant["product_id"]).price = Decimal("99")
    response = edit(client, auth_headers, order, {"customer_name": "Nombre corregido"}, total=15)
    assert response.status_code == 200, response.text
    assert response.json()["discount"] == 5
    assert response.json()["items"][0]["unit_price"] == 20
    assert response.json()["items"][0]["promotion_snapshot"] == {"name": "Oferta anterior"}


@pytest.mark.parametrize("lock", ["payment", "evidence", "assignment", "dispatched"])
def test_fulfillment_locks_allow_customer_only_edits(client, tenant, auth_headers, lock):
    order = new_order(client, tenant, auth_headers)
    with SessionLocal.begin() as db:
        if lock == "payment":
            db.add(Payment(business_id=tenant["business_id"], order_id=order["id"], method="cash", amount=5))
        elif lock == "evidence":
            db.add(PaymentEvidence(business_id=tenant["business_id"], order_id=order["id"], provider="yape", storage_path="test", image_sha256="test"))
        elif lock == "assignment":
            db.add(DeliveryAssignment(business_id=tenant["business_id"], branch_id=tenant["branch_id"], order_id=order["id"], status="assigned"))
        else:
            db.get(Order, order["id"]).status = "dispatched"
    assert edit(client, auth_headers, order, {"channel": "counter"}).status_code == 409
    response = edit(client, auth_headers, order, {"customer_name": "Actualizado"}, key="name-only")
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("status", ["closed", "cancelled", "delivered"])
def test_terminal_orders_cannot_be_edited(client, tenant, auth_headers, status):
    order = new_order(client, tenant, auth_headers)
    with SessionLocal.begin() as db:
        db.get(Order, order["id"]).status = status
    assert edit(client, auth_headers, order, {"customer_name": "No"}).status_code == 409


def test_external_services_never_enter_own_delivery(client, tenant, auth_headers):
    order = new_order(client, tenant, auth_headers)
    response = edit(client, auth_headers, order, {"channel": "delivery", "delivery_address": {"delivery_service": "rappi", "service_order_id": "R-12"}})
    assert response.status_code == 200, response.text
    assert response.json()["delivery_fee"] == 0
    assert response.json()["source"] == "pos"
    assert client.get(f"/api/v1/delivery/orders?branch_id={tenant['branch_id']}", headers=auth_headers).json() == []
    assert client.post(f"/api/v1/delivery/orders/{order['id']}/assign", headers=auth_headers, json={}).status_code == 409


def test_edit_validates_scope_role_version_and_compatible_products(client, tenant, auth_headers):
    order = new_order(client, tenant, auth_headers)
    assert client.patch(f"/api/v1/orders/{order['id']}", headers=auth_headers, json={"channel": "counter"}).status_code == 422
    assert edit(client, {**auth_headers, "X-Dev-Role": "kitchen"}, order, {"customer_name": "No"}).status_code == 403
    assert edit(client, {**auth_headers, "X-Business-Id": str(tenant["other_business_id"]), "X-Branch-Id": str(tenant["other_branch_id"])}, order, {"customer_name": "No"}).status_code == 404
    assert edit(client, auth_headers, {**order, "version": 99}, {"customer_name": "No"}).status_code == 409
    with SessionLocal.begin() as db:
        db.get(Product, tenant["product_id"]).service_channels = ["pos_takeaway"]
    assert edit(client, auth_headers, order, {"channel": "counter"}).status_code == 409
    assert edit(client, auth_headers, order, {"items": []}).status_code == 422


def test_modifiers_and_commands_snapshot_group_and_variant(client, tenant, auth_headers):
    with SessionLocal.begin() as db:
        group = ModifierGroup(business_id=tenant["business_id"], branch_id=tenant["branch_id"], name="Elige tus salsas", allow_repeats=True, minimum=0)
        db.add(group)
        db.flush()
        modifier = Modifier(group_id=group.id, name="BBQ", price_delta=2)
        db.add_all([modifier, ProductModifierGroup(product_id=tenant["product_id"], group_id=group.id), ProductVariant(product_id=tenant["product_id"], name="Grande", price_delta=5)])
        db.flush()
        modifier_id = modifier.id
    response = client.post("/api/v1/orders", headers={**auth_headers, "Idempotency-Key": "snapshot"}, json={"branch_id": tenant["branch_id"], "items": [{"product_id": tenant["product_id"], "variant_name": "Grande", "quantity": 1, "modifiers": [{"modifier_id": modifier_id, "name": "BBQ"}] * 2}]})
    assert response.status_code == 201, response.text
    order = response.json()
    assert order["items"][0]["modifiers"][0]["group_name"] == "Elige tus salsas"
    sent = client.post(f"/api/v1/orders/{order['id']}/confirm-and-send", headers={**auth_headers, "Idempotency-Key": "snapshot-send"}, json={"expected_version": order["version"]})
    assert sent.status_code == 200, sent.text
    item = sent.json()["tickets"][0]["items"][0]
    assert item["variant_name"] == "Grande"
    assert len(item["modifiers"]) == 2
    assert item["modifiers"][0]["group_name"] == "Elige tus salsas"


def test_mode_change_recalculates_only_discounts_and_requires_new_total(client, tenant, auth_headers):
    promotion = client.post("/api/v1/catalog/promotions", headers=auth_headers, json={
        "branch_id": tenant["branch_id"], "name": "Para llevar", "promotion_type": "product_discount",
        "discount_type": "percentage", "discount_value": 20, "target_scope": "products",
        "target_ids": [tenant["product_id"]], "service_channels": ["pos_takeaway"], "weekdays": [], "active": True,
    })
    assert promotion.status_code == 201, promotion.text
    order = new_order(client, tenant, auth_headers)
    assert order["total"] == 16
    sent = client.post(f"/api/v1/orders/{order['id']}/confirm-and-send", headers={**auth_headers, "Idempotency-Key": "send"}, json={"expected_version": order["version"]}).json()
    order = sent["order"]
    before = client.get(f"/api/v1/orders/{order['id']}/detail", headers=auth_headers).json()["tickets"]
    with SessionLocal.begin() as db:
        db.get(Product, tenant["product_id"]).price = Decimal("99")
    rejected = edit(client, auth_headers, order, {"channel": "counter"})
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["total"] == 20
    accepted = edit(client, auth_headers, order, {"channel": "counter"}, total=20)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["items"][0]["unit_price"] == 20
    assert accepted.json()["discount"] == 0
    assert client.get(f"/api/v1/orders/{order['id']}/detail", headers=auth_headers).json()["tickets"] == before


def test_disabled_mode_and_same_business_other_branch_cannot_edit(client, tenant, auth_headers):
    order = new_order(client, tenant, auth_headers)
    with SessionLocal.begin() as db:
        sibling = Branch(business_id=tenant["business_id"], slug="sibling", name="Otra sucursal")
        db.add(sibling)
        db.add(BranchSettings(business_id=tenant["business_id"], branch_id=tenant["branch_id"], pos_counter=False))
        db.flush()
        sibling_id = sibling.id
    assert edit(client, {**auth_headers, "X-Branch-Id": str(sibling_id)}, order, {"customer_name": "No"}).status_code == 404
    rejected = edit(client, auth_headers, order, {"channel": "counter"})
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "SERVICE_DISABLED"
