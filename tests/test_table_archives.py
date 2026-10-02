from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app import api as api_module
from app.database import SessionLocal
from app.models import AuditEvent, DiningArea, IdempotencyRecord, Order, Reservation, ReservationTable, RestaurantTable
from test_command_workflow import create_order, send_order


def zone(tenant):
    with SessionLocal() as db:
        area = db.get(DiningArea, db.get(RestaurantTable, tenant["table_id"]).area_id)
        return {"id": area.id, "expected_version": area.version}


def archive_table(client, tenant, headers, *, key="archive-table", version=1, table_id=None):
    return client.request("DELETE", f"/api/v1/tables/{table_id or tenant['table_id']}",
                          headers={**headers, "Idempotency-Key": key}, json={"expected_version": version})


def archive_zone(client, tenant, headers, *, body=None, key="archive-zone"):
    area = zone(tenant)
    return client.request("DELETE", f"/api/v1/areas/{area['id']}",
                          headers={**headers, "Idempotency-Key": key},
                          json=body or {"expected_version": area["expected_version"]})


def reservation(tenant, *, status="confirmed", start_delta=timedelta(days=1), end_delta=timedelta(days=1, hours=1)):
    now = datetime.now(timezone.utc)
    with SessionLocal.begin() as db:
        row = Reservation(business_id=tenant["business_id"], branch_id=tenant["branch_id"],
                          customer_name="Test reservation", customer_phone="51900000000", party_size=2,
                          start_at=now + start_delta, end_at=now + end_delta, status=status)
        db.add(row)
        db.flush()
        db.add(ReservationTable(reservation_id=row.id, table_id=tenant["table_id"]))
        return row.id


def test_table_archive_replays_once_and_retains_identity(client, tenant, auth_headers, monkeypatch):
    broadcasts = []

    async def capture(branch_id, event, payload):
        broadcasts.append(event)

    monkeypatch.setattr(api_module.hub, "broadcast", capture)
    before = client.get("/api/v1/tables", params={"branch_id": tenant["branch_id"]}, headers=auth_headers).json()[0]
    response = archive_table(client, tenant, auth_headers)
    assert response.status_code == 200, response.text
    assert response.json()["active"] is False
    assert response.json()["archived_at"]
    assert response.json()["code"] == before["code"]
    assert response.json()["name"] == before["name"]
    replay = archive_table(client, tenant, auth_headers)
    assert replay.status_code == 200 and replay.json() == response.json()
    changed = archive_table(client, tenant, auth_headers, version=2)
    assert changed.status_code == 409 and changed.json()["code"] == "ARCHIVE_IDEMPOTENCY_CONFLICT"
    assert archive_table(client, tenant, auth_headers, key="another-operation").status_code == 404
    assert client.get("/api/v1/tables", params={"branch_id": tenant["branch_id"]}, headers=auth_headers).json() == []
    assert broadcasts == ["table.archived"]
    with SessionLocal() as db:
        assert db.get(RestaurantTable, tenant["table_id"]).version == 2
        event = db.scalar(select(AuditEvent).where(AuditEvent.action == "table.archived"))
        assert event.branch_id == tenant["branch_id"]
        assert event.payload["before"]["active"] is True
        assert event.payload["after"]["active"] is False
        assert db.scalar(select(func.count(IdempotencyRecord.id))) == 1
        assert db.scalar(select(func.count(AuditEvent.id)).where(AuditEvent.action == "table.archived")) == 1


@pytest.mark.parametrize("resource", ["tables", "areas"])
def test_archive_requires_version_key_and_management(client, tenant, auth_headers, resource):
    identifier = tenant["table_id"] if resource == "tables" else zone(tenant)["id"]
    path = f"/api/v1/{resource}/{identifier}"
    for role in ["cashier", "waiter", "kitchen"]:
        denied = client.request("DELETE", path, headers={**auth_headers, "X-Dev-Role": role, "Idempotency-Key": "denied"}, json={"expected_version": 1})
        assert denied.status_code == 403
    assert client.request("DELETE", path, headers=auth_headers, json={"expected_version": 1}).status_code == 422
    assert client.request("DELETE", path, headers={**auth_headers, "Idempotency-Key": "no-version"}, json={}).status_code == 422
    stale = client.request("DELETE", path, headers={**auth_headers, "Idempotency-Key": "stale"}, json={"expected_version": 99})
    assert stale.status_code == 409
    other = {**auth_headers, "X-Business-Id": str(tenant["other_business_id"]), "X-Branch-Id": str(tenant["other_branch_id"]), "Idempotency-Key": "foreign"}
    assert client.request("DELETE", path, headers=other, json={"expected_version": 1}).status_code == 404
    with SessionLocal() as db:
        assert db.get(RestaurantTable, tenant["table_id"]).archived_at is None
        assert db.get(DiningArea, zone(tenant)["id"]).archived_at is None
        assert db.scalar(select(func.count(IdempotencyRecord.id))) == 0


def test_zone_archive_is_exact_atomic_and_replayable(client, tenant, auth_headers):
    area = zone(tenant)
    with SessionLocal.begin() as db:
        second = RestaurantTable(business_id=tenant["business_id"], branch_id=tenant["branch_id"], area_id=area["id"], code="M2", name="Mesa 2")
        db.add(second)
        db.flush()
        second_id = second.id
    assert archive_zone(client, tenant, auth_headers).json()["code"] == "AREA_HAS_ACTIVE_TABLES"
    body = {"expected_version": 1, "include_tables": True, "tables": [{"id": tenant["table_id"], "expected_version": 1}]}
    changed_set = archive_zone(client, tenant, auth_headers, body=body)
    assert changed_set.status_code == 409 and changed_set.json()["code"] == "AREA_TABLES_CHANGED"
    body["tables"].append({"id": second_id, "expected_version": 99})
    assert archive_zone(client, tenant, auth_headers, body=body).status_code == 409
    with SessionLocal() as db:
        assert all(table.archived_at is None for table in db.scalars(select(RestaurantTable)))
        assert db.get(DiningArea, area["id"]).version == 1
        assert db.scalar(select(func.count(AuditEvent.id))) == 0
    body["tables"][1]["expected_version"] = 1
    archived = archive_zone(client, tenant, auth_headers, body=body)
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived_table_ids"] == [tenant["table_id"], second_id]
    body["tables"].reverse()
    replay = archive_zone(client, tenant, auth_headers, body=body)
    assert replay.status_code == 200 and replay.json() == archived.json()
    assert client.get("/api/v1/areas", params={"branch_id": tenant["branch_id"]}, headers=auth_headers).json() == []
    with SessionLocal() as db:
        assert db.get(DiningArea, area["id"]).archived_at is not None
        assert all(table.archived_at is not None and table.version == 2 for table in db.scalars(select(RestaurantTable)))
        assert db.scalar(select(func.count(AuditEvent.id)).where(AuditEvent.action == "area.archived")) == 1
        assert db.scalar(select(func.count(AuditEvent.id)).where(AuditEvent.action == "table.archived")) == 2


def test_empty_zone_archive_after_archived_table_and_name_reuse(client, tenant, auth_headers):
    area = zone(tenant)
    assert archive_table(client, tenant, auth_headers).status_code == 200
    archived = archive_zone(client, tenant, auth_headers)
    assert archived.status_code == 200 and archived.json()["archived_table_ids"] == []
    assert archive_zone(client, tenant, auth_headers, body={"expected_version": 1}).json() == archived.json()
    new = client.post("/api/v1/areas", headers=auth_headers, json={"branch_id": tenant["branch_id"], "name": "Salon"})
    assert new.status_code == 201 and new.json()["id"] != area["id"]
    assert client.post("/api/v1/areas", headers=auth_headers, json={"branch_id": tenant["branch_id"], "name": " SALON "}).status_code == 409
    renamed = client.patch(f"/api/v1/areas/{new.json()['id']}", headers=auth_headers, json={"expected_version": 1, "name": "Salon"})
    assert renamed.status_code == 200
    new_table = client.post("/api/v1/tables", headers=auth_headers, json={"branch_id": tenant["branch_id"], "area_id": new.json()["id"], "code": "NEW-M1", "name": "Mesa 1"})
    assert new_table.status_code == 201 and new_table.json()["id"] != tenant["table_id"]
    assert client.post("/api/v1/tables", headers=auth_headers, json={"branch_id": tenant["branch_id"], "area_id": new.json()["id"], "code": "M1", "name": "Mesa 1"}).status_code == 409


@pytest.mark.parametrize("status,start,end,blocked", [
    ("confirmed", -1, 1, True), ("confirmed", 24, 25, True), ("seated", -1, 1, True),
    ("confirmed", -25, -24, False), ("completed", 24, 25, False),
    ("cancelled", 24, 25, False), ("no_show", 24, 25, False),
])
def test_reservations_guard_both_archive_routes(client, tenant, auth_headers, status, start, end, blocked):
    reservation_id = reservation(tenant, status=status, start_delta=timedelta(hours=start), end_delta=timedelta(hours=end))
    body = {"expected_version": 1, "include_tables": True, "tables": [{"id": tenant["table_id"], "expected_version": 1}]}
    response = archive_zone(client, tenant, auth_headers, body=body)
    if blocked:
        assert response.status_code == 409 and response.json()["code"] == "TABLE_HAS_ACTIVE_RESERVATION"
        response = archive_table(client, tenant, auth_headers)
        assert response.status_code == 409 and response.json()["code"] == "TABLE_HAS_ACTIVE_RESERVATION"
    else:
        assert response.status_code == 200, response.text
    with SessionLocal() as db:
        assert db.get(ReservationTable, (reservation_id, tenant["table_id"])) is not None
        assert (db.get(RestaurantTable, tenant["table_id"]).archived_at is None) == blocked
        assert (db.get(DiningArea, zone(tenant)["id"]).archived_at is None) == blocked


def test_open_account_blocks_whole_zone_even_if_table_status_is_stale(client, tenant, auth_headers):
    order = create_order(client, tenant, auth_headers, dine_in=True)
    with SessionLocal.begin() as db:
        table = db.get(RestaurantTable, tenant["table_id"])
        table.status = "available"
        version = table.version
    body = {"expected_version": 1, "include_tables": True, "tables": [{"id": tenant["table_id"], "expected_version": version}]}
    blocked = archive_zone(client, tenant, auth_headers, body=body)
    assert blocked.status_code == 409 and blocked.json()["code"] == "TABLE_HAS_ACTIVE_ORDER"
    assert archive_table(client, tenant, auth_headers, version=version).json()["code"] == "TABLE_HAS_ACTIVE_ORDER"
    with SessionLocal() as db:
        assert db.get(Order, order["id"]).table_id == tenant["table_id"]
        assert db.get(RestaurantTable, tenant["table_id"]).archived_at is None
        assert db.get(DiningArea, zone(tenant)["id"]).archived_at is None


def test_released_account_preserves_history_and_pending_kitchen(client, tenant, auth_headers):
    order = create_order(client, tenant, auth_headers, dine_in=True)
    sent = send_order(client, order, auth_headers)
    reservation_id = reservation(tenant, status="completed")
    with SessionLocal.begin() as db:
        stored = db.get(Order, order["id"])
        stored.table_released_at = datetime.now(timezone.utc)
        version = db.get(RestaurantTable, tenant["table_id"]).version
    response = archive_zone(client, tenant, auth_headers, body={"expected_version": 1, "include_tables": True, "tables": [{"id": tenant["table_id"], "expected_version": version}]})
    assert response.status_code == 200, response.text
    detail = client.get(f"/api/v1/orders/{order['id']}/detail", headers=auth_headers)
    assert detail.status_code == 200
    assert detail.json()["order"]["table_id"] == tenant["table_id"]
    assert detail.json()["table_context"]["table_name"] == "Mesa 1"
    assert detail.json()["tickets"][0]["id"] == sent["tickets"][0]["id"]
    assert detail.json()["tickets"][0]["status"] == "queued"
    history = client.get("/api/v1/orders/workspace", headers=auth_headers, params={"branch_id": tenant["branch_id"], "period": "all", "view": "table_history"})
    assert history.status_code == 200 and history.json()["items"][0]["id"] == order["id"]
    with SessionLocal() as db:
        assert db.get(ReservationTable, (reservation_id, tenant["table_id"])) is not None


def test_archived_resources_cannot_be_modified_opened_or_reserved(client, tenant, auth_headers):
    area = zone(tenant)
    assert archive_zone(client, tenant, auth_headers, body={"expected_version": 1, "include_tables": True, "tables": [{"id": tenant["table_id"], "expected_version": 1}]}).status_code == 200
    assert client.patch(f"/api/v1/tables/{tenant['table_id']}", headers=auth_headers, json={"name": "Changed", "expected_version": 2}).status_code == 404
    assert client.patch(f"/api/v1/areas/{area['id']}", headers=auth_headers, json={"name": "Changed", "expected_version": 2}).status_code == 404
    assert client.post("/api/v1/tables", headers=auth_headers, json={"branch_id": tenant["branch_id"], "area_id": area["id"], "code": "M2", "name": "Mesa 2"}).status_code == 422
    order = client.post("/api/v1/orders", headers={**auth_headers, "Idempotency-Key": "archived-order"}, json={"branch_id": tenant["branch_id"], "channel": "dine_in", "table_id": tenant["table_id"], "items": []})
    assert order.status_code == 409 and order.json()["code"] == "TABLE_ARCHIVED"
    start = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    reserved = client.post("/api/v1/reservations", headers=auth_headers, json={"branch_id": tenant["branch_id"], "customer_name": "Test", "customer_phone": "51900000000", "party_size": 2, "start_at": start, "table_ids": [tenant["table_id"]]})
    assert reserved.status_code == 409 and reserved.json()["code"] == "TABLE_ARCHIVED"
    available = client.get("/api/v1/reservations/availability", headers=auth_headers, params={"branch_id": tenant["branch_id"], "start_at": start, "duration_minutes": 60, "party_size": 2})
    assert available.status_code == 200 and available.json()["tables"] == []


def test_archived_table_cannot_receive_transfer_or_reactivated_reservation(client, tenant, auth_headers):
    reservation_id = reservation(tenant, status="cancelled")
    assert archive_table(client, tenant, auth_headers).status_code == 200
    with SessionLocal.begin() as db:
        table = RestaurantTable(business_id=tenant["business_id"], branch_id=tenant["branch_id"], code="M2", name="Mesa 2")
        db.add(table)
        db.flush()
        table_id = table.id
    created = client.post("/api/v1/orders", headers={**auth_headers, "Idempotency-Key": "transfer-source"}, json={"branch_id": tenant["branch_id"], "channel": "dine_in", "table_id": table_id, "items": []})
    assert created.status_code == 201, created.text
    transfer = client.patch(f"/api/v1/orders/{created.json()['id']}", headers=auth_headers, json={"table_id": tenant["table_id"], "expected_version": created.json()["version"]})
    assert transfer.status_code == 409 and transfer.json()["code"] == "TABLE_ARCHIVED"
    reactivated = client.patch(f"/api/v1/reservations/{reservation_id}", headers=auth_headers, json={"status": "confirmed", "expected_version": 1})
    assert reactivated.status_code == 409 and reactivated.json()["code"] == "TABLE_ARCHIVED"
    with SessionLocal() as db:
        assert db.get(Order, created.json()["id"]).table_id == table_id
        assert db.get(Reservation, reservation_id).status == "cancelled"


def test_zone_confirmation_rejects_duplicate_ids_and_flag_omission(client, tenant, auth_headers):
    table = {"id": tenant["table_id"], "expected_version": 1}
    for body in [{"expected_version": 1, "tables": [table]}, {"expected_version": 1, "include_tables": True, "tables": [table, table]}]:
        assert archive_zone(client, tenant, auth_headers, body=body).status_code == 422
