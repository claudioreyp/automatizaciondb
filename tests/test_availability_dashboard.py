from datetime import date, datetime, timezone
from decimal import Decimal
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.database import SessionLocal
from app.dashboard_report import dashboard_report, line_net_amounts, source_label
from app.models import AuditEvent, Modifier, ModifierGroup, Order, OrderItem, Payment, ProductModifierGroup, ProductVariant


def seed_options(tenant):
    with SessionLocal.begin() as db:
        variants = [ProductVariant(product_id=tenant["product_id"], name=name, active=active, price_delta=0) for name, active in [("Chico", True), ("Grande", True), ("Archivada", False)]]
        group = ModifierGroup(business_id=tenant["business_id"], branch_id=tenant["branch_id"], name="Salsas", minimum=1, maximum=2, allow_repeats=True, max_per_option=2)
        db.add_all([group, *variants])
        db.flush()
        options = [Modifier(group_id=group.id, name="BBQ", price_delta=2), Modifier(group_id=group.id, name="Ranch", price_delta=3)]
        db.add_all([*options, ProductModifierGroup(product_id=tenant["product_id"], group_id=group.id)])
        db.flush()
        return [item.id for item in variants], [item.id for item in options]


def test_variant_switches_bulk_archive_and_scope(client, tenant, auth_headers):
    variants, _ = seed_options(tenant)
    headers = {**auth_headers, "X-Dev-Role": "kitchen"}
    path = f"/api/v1/catalog/variants/{variants[0]}/availability"
    response = client.patch(path, headers=headers, json={"available": False})
    assert response.status_code == 200
    assert response.json()["product_available"] is True
    assert client.patch(f"/api/v1/catalog/products/{tenant['product_id']}", headers=auth_headers, json={"name": "Pizza nueva", "available": True}).status_code == 200
    with SessionLocal() as db:
        assert db.get(ProductVariant, variants[0]).available is False
    assert client.patch(f"/api/v1/catalog/variants/{variants[2]}/availability", headers=headers, json={"available": True}).status_code == 404
    assert client.patch(path, headers={**headers, "X-Business-Id": str(tenant["other_business_id"]), "X-Branch-Id": str(tenant["other_branch_id"])}, json={"available": True}).status_code == 403
    assert client.patch(f"/api/v1/catalog/variants/{variants[0]}", headers=headers, json={"name": "Hack"}).status_code == 403
    bulk = f"/api/v1/catalog/products/{tenant['product_id']}/availability"
    assert client.patch(bulk, headers=headers, json={"available": False}).status_code == 200
    response = client.patch(path, headers=headers, json={"available": True})
    assert response.json()["product_available"] is True
    with SessionLocal() as db:
        assert db.get(ProductVariant, variants[1]).available is False
        assert db.get(ProductVariant, variants[2]).active is False
        assert db.scalar(sa.select(AuditEvent).where(AuditEvent.action == "variant.availability_changed"))


def test_unavailable_options_reject_new_orders_preserve_history(client, tenant, auth_headers):
    variants, options = seed_options(tenant)
    payload = {"branch_id": tenant["branch_id"], "channel": "takeaway", "items": [{"product_id": tenant["product_id"], "variant_name": "Chico", "quantity": 1, "modifiers": [{"modifier_id": options[0], "name": "BBQ"}]}]}
    saved = client.post("/api/v1/orders", headers={**auth_headers, "Idempotency-Key": "available-order"}, json=payload)
    assert saved.status_code == 201, saved.text
    original = saved.json()["items"]
    for option in options:
        assert client.patch(f"/api/v1/catalog/modifiers/{option}/availability", headers=auth_headers, json={"available": False}).status_code == 200
    from app.api import serialize_catalog
    from app.models import Branch
    with SessionLocal() as db:
        public = serialize_catalog(db, db.get(Branch, tenant["branch_id"]), available_only=True)
        assert all(product["id"] != tenant["product_id"] for product in public["products"])
        assert not public["modifier_groups"][0]["modifiers"]
    foreign = {**auth_headers, "X-Business-Id": str(tenant["other_business_id"]), "X-Branch-Id": str(tenant["other_branch_id"])}
    assert client.patch(f"/api/v1/catalog/modifiers/{options[0]}/availability", headers=foreign, json={"available": True}).status_code == 403
    failed = client.post("/api/v1/orders", headers={**auth_headers, "Idempotency-Key": "unavailable-order"}, json=payload)
    assert failed.status_code == 422
    assert "Salsas" in failed.text
    catalog = client.get(f"/api/v1/catalog?branch_id={tenant['branch_id']}", headers=auth_headers).json()
    assert catalog["products"][0]["unavailable_reason"]
    assert catalog["modifier_groups"][0]["modifiers"][0]["active"] is True
    menu = client.get("/api/v1/public/test-restaurant/menu").json()
    assert not menu["products"]
    historical = client.get(f"/api/v1/orders/{saved.json()['id']}", headers=auth_headers).json()
    assert historical["items"] == original
    # Restore one repeatable option: two units can still satisfy a minimum of two.
    with SessionLocal.begin() as db:
        group = db.get(ModifierGroup, db.get(Modifier, options[0]).group_id)
        group.minimum = 2
    client.patch(f"/api/v1/catalog/modifiers/{options[0]}/availability", headers=auth_headers, json={"available": True})
    payload["items"][0]["modifiers"] *= 2
    assert client.post("/api/v1/orders", headers={**auth_headers, "Idempotency-Key": "restored-order"}, json=payload).status_code == 201
    client.patch(f"/api/v1/catalog/variants/{variants[0]}/availability", headers=auth_headers, json={"available": False})
    menu = client.get("/api/v1/public/test-restaurant/menu").json()
    assert [variant["name"] for variant in menu["products"][0]["variants"]] == ["Grande"]
    assert client.post("/api/v1/orders", headers={**auth_headers, "Idempotency-Key": "bad-variant"}, json=payload).status_code == 422


def test_availability_migration_preserves_archives_and_parent_flags(tmp_path):
    spec = importlib.util.spec_from_file_location("availability_migration", Path(__file__).parents[1] / "migrations/versions/20260908_0019_option_availability.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'availability.db'}")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE products (id INTEGER PRIMARY KEY, available BOOLEAN NOT NULL)"))
        connection.execute(sa.text("CREATE TABLE product_variants (id INTEGER PRIMARY KEY, product_id INTEGER, active BOOLEAN NOT NULL)"))
        connection.execute(sa.text("CREATE TABLE modifiers (id INTEGER PRIMARY KEY, active BOOLEAN NOT NULL)"))
        connection.execute(sa.text("INSERT INTO products VALUES (1, 0), (2, 1)"))
        connection.execute(sa.text("INSERT INTO product_variants VALUES (1, 1, 1), (2, 2, 0)"))
        connection.execute(sa.text("INSERT INTO modifiers VALUES (1, 0), (2, 1)"))
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
            module.upgrade()
        assert connection.execute(sa.text("SELECT active, available FROM product_variants ORDER BY id")).all() == [(1, 0), (0, 1)]
        assert connection.execute(sa.text("SELECT active, available FROM modifiers ORDER BY id")).all() == [(0, 1), (1, 1)]
    engine.dispose()


def test_dashboard_persisted_net_amounts_channels_and_lima_bounds(client, tenant, auth_headers):
    with SessionLocal.begin() as db:
        for number, status, created, source in [
            ("1", "preparing", "2026-09-08T05:00:00", "pos"),
            ("2", "closed", "2026-09-09T04:59:59", "whatsapp"),
            ("3", "cancelled", "2026-09-08T12:00:00", "pos"),
            ("4", "draft", "2026-09-08T12:00:00", "pos"),
            ("5", "pending_confirmation", "2026-09-08T12:00:00", "pos"),
            ("6", "confirmed", "2026-09-09T05:00:00", "public_store"),
        ]:
            order = Order(business_id=tenant["business_id"], branch_id=tenant["branch_id"], number=number, status=status, source=source, channel="delivery", subtotal=40, discount=10, delivery_fee=5, total=35, created_at=datetime.fromisoformat(created))
            order.items = [OrderItem(product_id=tenant["product_id"], product_name="Pizza histórica", quantity=2, unit_price=20, line_total=40, promotion_discount=6), OrderItem(product_name="Reemplazado", quantity=1, unit_price=100, line_total=100, status="superseded")]
            db.add(order)
            db.flush()
            if number == "1":
                db.add(Payment(business_id=tenant["business_id"], order_id=order.id, method="cash", amount=15, status="confirmed", received_at=datetime(2026, 9, 8, 13), created_by="test"))
    with SessionLocal() as db:
        report = dashboard_report(db, tenant["business_id"], tenant["branch_id"], date(2026, 9, 8), date(2026, 9, 8), now=datetime(2026, 9, 10, tzinfo=timezone.utc))
    assert report["orders"] == 2
    assert report["sales"] == 60 and report["shipping"] == 10 and report["average_ticket"] == 30
    assert report["series"][0]["sales"] == 30 and report["series"][23]["sales"] == 30
    assert report["top_products"] == [{"name": "Pizza histórica", "quantity": Decimal(4), "sales": Decimal(60)}]
    assert report["payment_methods"] == [{"name": "cash", "amount": Decimal(15)}]
    assert {item["name"] for item in report["channels"]} == {"Punto de venta", "WhatsApp"}
    path = f"/api/v1/reports/dashboard?branch_id={tenant['branch_id']}&date_from=2026-09-01&date_to=2026-09-07"
    response = client.get(path, headers=auth_headers)
    assert response.status_code == 200
    assert all(item["sales"] == 0 for item in response.json()["weekdays"])
    assert client.get(path, headers={**auth_headers, "X-Dev-Role": "kitchen"}).status_code == 403
    assert client.get(path, headers={**auth_headers, "X-Business-Id": str(tenant["other_business_id"]), "X-Branch-Id": str(tenant["other_branch_id"])}).status_code == 403


def test_report_prorates_remaining_discount_without_losing_cents():
    order = Order(discount=Decimal("1.01"))
    items = [OrderItem(line_total=1, promotion_discount=0) for _ in range(3)]
    values = line_net_amounts(order, items)
    assert sum(values) == Decimal("1.99")
    assert all(value >= 0 for value in values)


def test_dashboard_payment_dates_empty_days_and_tenant_isolation(client, tenant, auth_headers):
    with SessionLocal.begin() as db:
        for business_id, branch_id, created, total in [
            (tenant["business_id"], tenant["branch_id"], datetime(2026, 9, 1, 14), 40),
            (tenant["business_id"], tenant["branch_id"], datetime(2026, 8, 31, 14), 20),
            (tenant["other_business_id"], tenant["other_branch_id"], datetime(2026, 9, 1, 14), 999),
        ]:
            order = Order(business_id=business_id, branch_id=branch_id, number=str(total), status="confirmed", source="public_portal", channel="takeaway", created_at=created, subtotal=total, total=total)
            db.add(order); db.flush()
            db.add(Payment(business_id=business_id, order_id=order.id, method="card", amount=total, status="confirmed", received_at=datetime(2026, 9, 2, 14), created_by="test"))
            db.add(Payment(business_id=business_id, order_id=order.id, method="cash", amount=500, status="pending", received_at=datetime(2026, 9, 2, 14), created_by="test"))
    path = f"/api/v1/reports/dashboard?branch_id={tenant['branch_id']}&date_from=2026-09-01&date_to=2026-09-07"
    result = client.get(path, headers=auth_headers).json()
    assert result["sales"] == 40 and result["orders"] == 1
    assert result["payment_methods"] == [{"name": "card", "amount": 60}]
    assert result["channels"] == [{"name": "Menú digital", "sales": 40, "orders": 1}]
    assert sum(point["orders"] for point in result["series"]) == 1
    assert len([point for point in result["series"] if point["sales"] == 0]) == 6
    assert next(day for day in result["weekdays"] if day["name"] == "Martes")["sales"] == 40
    assert source_label("whatsapp_agent") == "WhatsApp"


@pytest.mark.parametrize("start,end", [("2026-09-02", "2026-09-01"), ("2025-01-01", "2026-09-01"), ("2099-01-01", "2099-01-01")])
def test_dashboard_rejects_invalid_ranges(client, tenant, auth_headers, start, end):
    assert client.get(f"/api/v1/reports/dashboard?branch_id={tenant['branch_id']}&date_from={start}&date_to={end}", headers=auth_headers).status_code == 422
