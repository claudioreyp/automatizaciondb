from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
import os
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import Base, SessionLocal
from app.models import Branch, Business, InventoryItem, KitchenTicket, Order, Product
from app.order_folios import reserve_order_folio
from app.command_revisions import removed_modifiers
from test_command_workflow import create_order, send_order


def revise(client, headers, order, operations, key):
    return client.post(f"/api/v1/orders/{order['id']}/item-revisions", json={
        "expected_version": order["version"], "operations": operations,
    }, headers={**headers, "Idempotency-Key": key})


def edit(item_id, product_id, quantity=1, notes=None):
    return {"type": "edit", "item_id": item_id, "replacement": {"product_id": product_id, "quantity": quantity, "notes": notes}}


def test_successive_edits_cancel_and_add_keep_original_command(client, tenant, auth_headers):
    original = create_order(client, tenant, auth_headers, items=[
        {"product_id": tenant["product_id"], "quantity": 1},
        {"product_id": tenant["product_id"], "quantity": 1},
    ])
    sent = send_order(client, original, auth_headers)
    first = sent["tickets"][0]
    assert original["folio"] == first["order_folio"] == 1
    operation = [edit(original["items"][0]["id"], tenant["product_id"], notes="Sin sal")]
    edited = revise(client, auth_headers, sent["order"], operation, "edit1")
    assert edited.status_code == 201, edited.text
    assert revise(client, auth_headers, sent["order"], operation, "edit1").json() == edited.json()
    latest = edited.json()
    second = revise(client, auth_headers, latest["order"], [edit(latest["replacement_item_ids"][0], tenant["product_id"], 2, "Salsa aparte")], "edit2")
    assert second.status_code == 201, second.text
    latest = second.json()
    cancelled = revise(client, auth_headers, latest["order"], [{"type": "cancel", "item_id": original["items"][1]["id"], "reason": "Por error"}], "cancel-part")
    assert cancelled.status_code == 201, cancelled.text
    result = cancelled.json()
    ticket = result["tickets"][0]
    assert ticket["id"] == first["id"] and ticket["sequence"] == 1
    assert ticket["created_at"] == first["created_at"]
    assert ticket["version"] == 4
    assert len(ticket["items"]) == 2
    assert ticket["items"][0]["notes"] == "Salsa aparte"
    assert ticket["items"][1]["status"] == "cancelled"
    assert result["order"]["total"] == 40
    assert result["created_ticket_ids"] == []
    with SessionLocal() as db:
        stored = db.get(KitchenTicket, first["id"])
        assert [item["item_id"] for item in stored.items_snapshot] == [item["id"] for item in original["items"]]
        assert len(stored.context_snapshot["revisions"]) == 3
        assert all(revision["created_by"] and revision["created_at"] for revision in stored.context_snapshot["revisions"])
        assert db.get(InventoryItem, tenant["inventory_id"]).quantity == Decimal("9")
    stale = client.post(f"/api/v1/kitchen/commands/{first['id']}/complete", json={"expected_status": "queued", "expected_version": 1}, headers={**auth_headers, "Idempotency-Key": "stale-complete"})
    assert stale.status_code == 409 and stale.json()["code"] == "KITCHEN_TICKET_STALE"
    appended = client.post(f"/api/v1/orders/{original['id']}/item-batches", json={"expected_version": result["order"]["version"], "items": [{"product_id": tenant["product_id"], "quantity": 1}]}, headers={**auth_headers, "Idempotency-Key": "same-sku-new-batch"})
    assert appended.status_code == 201, appended.text
    assert appended.json()["tickets"][0]["sequence"] == 2
    assert appended.json()["tickets"][0]["order_folio"] == 1
    assert appended.json()["order"]["total"] == 60


def test_completed_command_keeps_snapshot_and_correction_owns_next_edits(client, tenant, auth_headers):
    sent = send_order(client, create_order(client, tenant, auth_headers), auth_headers)
    original = sent["tickets"][0]
    completed = client.post(f"/api/v1/kitchen/commands/{original['id']}/complete", json={"expected_status": "queued", "expected_version": 1}, headers={**auth_headers, "Idempotency-Key": "complete"})
    assert completed.status_code == 200, completed.text
    response = revise(client, auth_headers, completed.json()["order"], [edit(sent["order"]["items"][0]["id"], tenant["product_id"], notes="Sin sal")], "correction")
    assert response.status_code == 201, response.text
    correction = response.json()["tickets"][0]
    assert correction["sequence"] == 2 and correction["id"] != original["id"]
    assert correction["context"]["source_ticket_ids"] == [original["id"]]
    assert correction["context"]["revisions"][0]["before"]["item_id"] == original["items"][0]["item_id"]
    newer = revise(client, auth_headers, response.json()["order"], [edit(response.json()["replacement_item_ids"][0], tenant["product_id"], notes="Poca sal")], "correction-again")
    assert newer.status_code == 201, newer.text
    assert newer.json()["tickets"][0]["id"] == correction["id"]
    assert newer.json()["updated_ticket_ids"] == [correction["id"]]
    assert newer.json()["tickets"][0]["items"][0]["notes"] == "Poca sal"
    with SessionLocal() as db:
        old = db.get(KitchenTicket, original["id"])
        assert old.status == "ready" and old.version == 2
        assert old.items_snapshot[0].get("notes") is None
        assert db.query(KitchenTicket).filter_by(order_id=sent["order"]["id"]).count() == 2


@pytest.mark.parametrize("dine_in", [False, True])
def test_whole_order_cancel_retires_commands_without_creating_another(client, tenant, auth_headers, dine_in):
    sent = send_order(client, create_order(client, tenant, auth_headers, dine_in=dine_in), auth_headers)
    response = client.post(f"/api/v1/orders/{sent['order']['id']}/transition", json={"status": "cancelled", "expected_version": sent["order"]["version"]}, headers=auth_headers)
    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        tickets = db.query(KitchenTicket).filter_by(order_id=sent["order"]["id"]).all()
        assert len(tickets) == 1 and tickets[0].status == "cancelled"
        assert not tickets[0].context_snapshot.get("revisions")
    active = client.get(f"/api/v1/kitchen/commands?branch_id={tenant['branch_id']}", headers=auth_headers)
    assert active.json()["items"] == []


def test_folios_shared_between_branches_isolated_between_businesses(client, tenant, auth_headers):
    first = create_order(client, tenant, auth_headers)
    with SessionLocal.begin() as db:
        branch = Branch(business_id=tenant["business_id"], slug="second", name="Second")
        db.add(branch); db.flush()
        product = Product(business_id=tenant["business_id"], branch_id=branch.id, sku="T", name="Tea", price=4)
        db.add(product); db.flush()
        branch_id, product_id = branch.id, product.id
    second = create_order(client, {**tenant, "branch_id": branch_id, "product_id": product_id}, {**auth_headers, "X-Branch-Id": str(branch_id)}, key="second-branch")
    assert (first["folio"], second["folio"]) == (1, 2)
    third = create_order(client, tenant, auth_headers, key="third")
    assert third["folio"] == 3 and third["number"] != first["number"]
    denied = client.get(f"/api/v1/orders/{first['id']}/detail", headers={**auth_headers, "X-Business-Id": str(tenant["other_business_id"]), "X-Branch-Id": str(tenant["other_branch_id"])})
    assert denied.status_code == 404
    with SessionLocal.begin() as db:
        assert reserve_order_folio(db, tenant["other_business_id"]) == 1


def test_legacy_item_removal_cannot_bypass_whole_order_cancellation(client, tenant, auth_headers):
    order = create_order(client, tenant, auth_headers)
    response = client.delete(f"/api/v1/orders/{order['id']}/items/{order['items'][0]['id']}?expected_version={order['version']}", headers=auth_headers)
    assert response.status_code == 409 and response.json()["code"] == "ORDER_REQUIRES_CANCELLATION"
    stored = client.get(f"/api/v1/orders/{order['id']}/detail", headers=auth_headers).json()["order"]
    assert stored["total"] == order["total"] and stored["version"] == order["version"]


def test_folio_search_preserves_codes_and_handles_non_integer_input(client, tenant, auth_headers):
    order = create_order(client, tenant, auth_headers)
    for endpoint in ["/api/v1/orders", "/api/v1/orders/workspace"]:
        for query in ["#1", order["number"], "\u00b2", "9" * 40]:
            response = client.get(endpoint, params={"branch_id": tenant["branch_id"], "search": query}, headers=auth_headers)
            assert response.status_code == 200, response.text
            rows = response.json()["items"] if endpoint.endswith("workspace") else response.json()
            assert [row["id"] for row in rows] == ([order["id"]] if query in ["#1", order["number"]] else [])


def test_removed_modifiers_preserve_repetitions_and_groups():
    ranch = {"modifier_id": 1, "name": "Ranch", "group_id": 5, "group_name": "Extras", "price_delta": 7}
    bbq = {"modifier_id": 2, "name": "BBQ", "group_name": "Salsas", "price_delta": 0}
    before = [ranch, deepcopy(ranch), bbq]
    assert removed_modifiers(before, [ranch, bbq]) == [ranch]
    assert before == [ranch, ranch, bbq]


def test_retired_extras_survive_successive_edits_without_recounting(client, tenant, auth_headers):
    group_response = client.post("/api/v1/catalog/modifier-groups", json={
        "branch_id": tenant["branch_id"], "name": "Extras", "minimum": 0,
        "maximum": 4, "allow_repeats": True, "max_per_option": 4,
    }, headers=auth_headers)
    assert group_response.status_code == 201, group_response.text
    group = group_response.json()
    option = client.post(f"/api/v1/catalog/modifier-groups/{group['id']}/modifiers", json={"name": "Ranch", "price_delta": 7}, headers=auth_headers).json()
    client.put(f"/api/v1/catalog/products/{tenant['product_id']}/modifier-groups", json={"group_ids": [group["id"]]}, headers=auth_headers)
    sent = send_order(client, create_order(client, tenant, auth_headers, items=[{
        "product_id": tenant["product_id"], "quantity": 2, "modifiers": [{"modifier_id": option["id"], "name": "Ranch"}] * 2,
    }]), auth_headers)
    item_id = sent["order"]["items"][0]["id"]
    order = sent["order"]
    for step, (quantity, notes) in enumerate([(2, "Salsa aparte"), (3, "Poca sal")]):
        operation = edit(item_id, tenant["product_id"], quantity, notes)
        operation["replacement"]["modifiers"] = [{"modifier_id": option["id"], "name": "Ranch"}]
        response = revise(client, auth_headers, order, [operation], f"retire-{step}")
        assert response.status_code == 201, response.text
        result = response.json()
        order, item_id = result["order"], result["replacement_item_ids"][0]
        ticket = result["tickets"][0]
        assert ticket["id"] == sent["tickets"][0]["id"]
        assert sum(option["removed_quantity"] for option in ticket["items"][0]["removed_modifiers"]) == 2
        assert ticket["items"][0]["removed_modifiers"][0]["group_name"] == "Extras"
        assert order["total"] == quantity * 27


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_atomic_folios_with_concurrent_transactions_and_rollback(tmp_path, dialect):
    schema = None
    if dialect == "postgresql":
        url = os.environ.get("POS_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("POS_TEST_POSTGRES_URL is not configured for an isolated PostgreSQL test schema")
        admin = sa.create_engine(url)
        schema = f"test_folios_{uuid4().hex}"
        with admin.begin() as connection:
            connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        # Poolers may ignore startup search_path. Qualify every ORM/DDL table
        # explicitly so this test can never write to the application's schema.
        engine = sa.create_engine(url, execution_options={"schema_translate_map": {None: schema}})
    else:
        engine = sa.create_engine(f"sqlite+pysqlite:///{tmp_path / 'concurrent.db'}", connect_args={"timeout": 30})
    try:
        Base.metadata.create_all(engine)
        if schema:
            with engine.connect() as connection:
                assert set(sa.inspect(connection).get_table_names(schema=schema)) == set(Base.metadata.tables)
        with Session(engine) as db, db.begin():
            business = Business(slug="concurrent", name="Concurrent")
            db.add(business); db.flush()
            branches = [Branch(business_id=business.id, slug=f"b{i}", name=f"Branch {i}") for i in range(2)]
            db.add_all(branches); db.flush()
            business_id, branch_ids = business.id, [branch.id for branch in branches]
        def create(index):
            with Session(engine) as db, db.begin():
                folio = reserve_order_folio(db, business_id)
                db.add(Order(business_id=business_id, branch_id=branch_ids[index % 2], number=f"TEST-{index}", folio=folio, channel="counter", source="pos"))
                return folio
        with ThreadPoolExecutor(max_workers=6) as pool:
            folios = list(pool.map(create, range(18)))
        assert sorted(folios) == list(range(1, 19))
        with Session(engine) as db:
            assert reserve_order_folio(db, business_id) == 19
            db.rollback()
        assert create(19) == 19
    finally:
        engine.dispose()
        if schema:
            with admin.begin() as connection:
                connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin.dispose()


def test_folio_migration_backfills_chronologically_and_preserves_children(tmp_path, monkeypatch):
    url = f"sqlite+pysqlite:///{tmp_path / 'legacy-folios.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    engine = sa.create_engine(url)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE businesses (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql("CREATE TABLE orders (id INTEGER PRIMARY KEY, business_id INTEGER REFERENCES businesses(id), branch_id INTEGER, created_at DATETIME, number TEXT)")
            connection.exec_driver_sql("CREATE TABLE kitchen_tickets (id INTEGER PRIMARY KEY, order_id INTEGER REFERENCES orders(id), items_snapshot JSON)")
            connection.exec_driver_sql("INSERT INTO businesses VALUES (1), (2)")
            connection.exec_driver_sql("INSERT INTO orders VALUES (9,1,11,'2026-01-01','OLD-A'), (2,1,12,'2026-01-02','OLD-B'), (3,2,13,'2025-01-01','OTHER'), (1,1,12,'2026-01-02','OLD-C')")
            connection.exec_driver_sql('INSERT INTO kitchen_tickets VALUES (1,9,\'[ {"name":"Original"} ]\')')
        command.stamp(config, "20260908_0019")
        command.upgrade(config, "20260910_0020")
        with engine.connect() as connection:
            assert connection.execute(sa.text("SELECT id, folio, number FROM orders ORDER BY business_id, folio")).all() == [(9,1,"OLD-A"), (1,2,"OLD-C"), (2,3,"OLD-B"), (3,1,"OTHER")]
            assert connection.execute(sa.text("SELECT order_folio_counter FROM businesses ORDER BY id")).scalars().all() == [3,1]
            assert connection.scalar(sa.text("SELECT version FROM kitchen_tickets")) == 1
            assert "Original" in connection.scalar(sa.text("SELECT items_snapshot FROM kitchen_tickets"))
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
            connection.exec_driver_sql("INSERT INTO orders (id,business_id,folio) VALUES (99,1,1)")
    finally:
        engine.dispose()
        get_settings.cache_clear()
