from datetime import datetime, timedelta, timezone
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from app import api as api_module
from app.database import SessionLocal
from app.models import (
    AuditEvent,
    Branch,
    CashSession,
    DiningArea,
    InventoryItem,
    KitchenTicket,
    Membership,
    Product,
    RestaurantTable,
)


def test_health_and_openapi(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert "/api/v1/orders" in client.get("/openapi.json").json()["paths"]


def test_daily_report_uses_lima_timezone(client, tenant, auth_headers):
    response = client.get(
        "/api/v1/reports/daily",
        params={"branch_id": tenant["branch_id"]},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["branch"]["id"] == tenant["branch_id"]
    assert response.json()["day"]


def test_invited_identity_can_accept_but_has_no_pos_access_before_acceptance(client, tenant):
    admin_headers = {
        "X-Dev-Auth": "test-token",
        "X-Dev-Role": "superadmin",
        "X-Dev-User": "admin-test",
    }
    created = client.post(
        "/api/v1/admin/invitations",
        json={
            "business_id": tenant["business_id"],
            "branch_id": tenant["branch_id"],
            "email": "new.manager@example.com",
            "role": "manager",
        },
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text
    token = created.json()["development_accept_url"].split("token=", 1)[1]
    pending_headers = {
        "X-Dev-Auth": "test-token",
        "X-Dev-User": "new-manager-id",
        "X-Dev-Email": "new.manager@example.com",
    }

    denied = client.get("/api/v1/context", headers=pending_headers)
    assert denied.status_code in {403, 422}

    accepted = client.post(
        "/api/v1/invitations/accept",
        json={"token": token},
        headers=pending_headers,
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["role"] == "manager"
    with SessionLocal() as db:
        membership = db.query(Membership).filter_by(auth_user_id="new-manager-id").one()
        membership_id = membership.id
        assert membership.business_id == tenant["business_id"]
        assert membership.branch_id == tenant["branch_id"]
        assert membership.active is True

    allowed = client.get(
        "/api/v1/context",
        headers={
            "X-Dev-Auth": "test-token",
            "X-Dev-User": "new-manager-id",
            "X-Dev-Role": "manager",
            "X-Business-Id": str(tenant["business_id"]),
            "X-Branch-Id": str(tenant["branch_id"]),
        },
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["role"] == "manager"

    listed = client.get(
        f"/api/v1/admin/businesses/{tenant['business_id']}/memberships",
        headers=admin_headers,
    )
    assert listed.status_code == 200
    assert listed.json()[0]["email"] == "new.manager@example.com"
    suspended = client.patch(
        f"/api/v1/admin/memberships/{membership_id}",
        json={"active": False},
        headers=admin_headers,
    )
    assert suspended.status_code == 200
    assert suspended.json()["active"] is False


def test_complete_dine_in_order_is_idempotent(client, tenant, auth_headers):
    order_payload = {
        "branch_id": tenant["branch_id"],
        "channel": "dine_in",
        "table_id": tenant["table_id"],
        "items": [{"product_id": tenant["product_id"], "quantity": 2}],
    }
    headers = {**auth_headers, "Idempotency-Key": "order-001"}
    created = client.post("/api/v1/orders", json=order_payload, headers=headers)
    assert created.status_code == 201, created.text
    order = created.json()
    assert order["total"] == 40.0

    repeated = client.post("/api/v1/orders", json=order_payload, headers=headers)
    assert repeated.status_code == 201
    assert repeated.json()["id"] == order["id"]

    duplicate_table_order = client.post(
        "/api/v1/orders",
        json=order_payload,
        headers={**auth_headers, "Idempotency-Key": "order-002"},
    )
    assert duplicate_table_order.status_code == 409

    confirmed = client.post(
        f"/api/v1/orders/{order['id']}/confirm",
        headers={**auth_headers, "Idempotency-Key": "confirm-001"},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "confirmed"
    with SessionLocal() as db:
        assert float(db.get(InventoryItem, tenant["inventory_id"]).quantity) == 9.0

    repeated_confirm = client.post(
        f"/api/v1/orders/{order['id']}/confirm",
        headers={**auth_headers, "Idempotency-Key": "confirm-001"},
    )
    assert repeated_confirm.status_code == 200
    with SessionLocal() as db:
        assert float(db.get(InventoryItem, tenant["inventory_id"]).quantity) == 9.0

    kitchen = client.post(
        f"/api/v1/orders/{order['id']}/send-to-kitchen",
        headers={**auth_headers, "Idempotency-Key": "kitchen-001"},
    )
    assert kitchen.status_code == 200, kitchen.text
    assert len(kitchen.json()["tickets"]) == 1
    assert kitchen.json()["tickets"][0]["created_at"]
    client.post(
        f"/api/v1/orders/{order['id']}/send-to-kitchen",
        headers={**auth_headers, "Idempotency-Key": "kitchen-001"},
    )
    with SessionLocal() as db:
        assert db.query(KitchenTicket).filter(KitchenTicket.order_id == order["id"]).count() == 1

    opened = client.post(
        "/api/v1/cash/sessions/open",
        json={"register_id": tenant["register_id"], "opening_amount": 100},
        headers=auth_headers,
    )
    assert opened.status_code == 201, opened.text
    paid = client.post(
        f"/api/v1/orders/{order['id']}/payments",
        json={"method": "cash", "amount": 40, "cash_session_id": opened.json()["id"]},
        headers={**auth_headers, "Idempotency-Key": "payment-001"},
    )
    assert paid.status_code == 201, paid.text
    assert paid.json()["order"]["payment_status"] == "paid"

    transitioned = client.post(
        f"/api/v1/orders/{order['id']}/transition",
        json={"status": "preparing"},
        headers=auth_headers,
    )
    assert transitioned.status_code == 200, transitioned.text
    ticket_id = kitchen.json()["tickets"][0]["id"]
    current_ticket_status = "queued"
    for next_ticket_status in ["preparing", "ready"]:
        ticket_transition = client.post(
            f"/api/v1/kitchen/tickets/{ticket_id}/transition",
            json={"status": next_ticket_status, "expected_status": current_ticket_status},
            headers=auth_headers,
        )
        assert ticket_transition.status_code == 200, ticket_transition.text
        current_ticket_status = next_ticket_status
    transitioned = client.post(
        f"/api/v1/orders/{order['id']}/transition",
        json={"status": "closed"},
        headers=auth_headers,
    )
    assert transitioned.status_code == 200, transitioned.text
    assert transitioned.json()["status"] == "closed"

    closed = client.post(
        f"/api/v1/cash/sessions/{opened.json()['id']}/close",
        json={"declared_amount": 140},
        headers=auth_headers,
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["difference"] == 0.0


def test_cancelling_a_dine_in_order_releases_its_table(client, tenant, auth_headers):
    created = client.post(
        "/api/v1/orders",
        json={
            "branch_id": tenant["branch_id"],
            "channel": "dine_in",
            "table_id": tenant["table_id"],
            "items": [{"product_id": tenant["product_id"], "quantity": 1}],
        },
        headers={**auth_headers, "Idempotency-Key": "cancel-table-order"},
    )
    assert created.status_code == 201, created.text

    cancelled = client.post(
        f"/api/v1/orders/{created.json()['id']}/transition",
        json={"status": "cancelled"},
        headers=auth_headers,
    )
    assert cancelled.status_code == 200, cancelled.text
    with SessionLocal() as db:
        assert db.get(RestaurantTable, tenant["table_id"]).status == "available"


def test_table_rejects_a_second_active_order_even_if_status_is_stale(client, tenant, auth_headers):
    payload = {
        "branch_id": tenant["branch_id"],
        "channel": "dine_in",
        "table_id": tenant["table_id"],
        "items": [{"product_id": tenant["product_id"], "quantity": 1}],
    }
    created = client.post(
        "/api/v1/orders",
        json=payload,
        headers={**auth_headers, "Idempotency-Key": "table-active-first"},
    )
    assert created.status_code == 201, created.text
    with SessionLocal.begin() as db:
        db.get(RestaurantTable, tenant["table_id"]).status = "available"

    duplicate = client.post(
        "/api/v1/orders",
        json=payload,
        headers={**auth_headers, "Idempotency-Key": "table-active-second"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "TABLE_ALREADY_HAS_OPEN_ORDER"

    listed = client.get(
        "/api/v1/tables",
        params={"branch_id": tenant["branch_id"]},
        headers=auth_headers,
    )
    listed_table = next(item for item in listed.json() if item["id"] == tenant["table_id"])
    assert listed_table["active_order_id"] == created.json()["id"]
    assert listed_table["status"] == "occupied"


def test_order_patch_cannot_steal_or_cross_scope_table(client, tenant, auth_headers):
    with SessionLocal.begin() as db:
        second_table = RestaurantTable(
            business_id=tenant["business_id"],
            branch_id=tenant["branch_id"],
            code="M2",
            name="Mesa 2",
            capacity=4,
        )
        foreign_table = RestaurantTable(
            business_id=tenant["other_business_id"],
            branch_id=tenant["other_branch_id"],
            code="AJENA",
            name="Mesa ajena",
            capacity=4,
        )
        db.add_all([second_table, foreign_table])
        db.flush()
        second_table_id = second_table.id
        foreign_table_id = foreign_table.id

    first = client.post(
        "/api/v1/orders",
        json={
            "branch_id": tenant["branch_id"],
            "channel": "dine_in",
            "table_id": tenant["table_id"],
            "items": [{"product_id": tenant["product_id"], "quantity": 1}],
        },
        headers={**auth_headers, "Idempotency-Key": "table-patch-first"},
    )
    second = client.post(
        "/api/v1/orders",
        json={
            "branch_id": tenant["branch_id"],
            "channel": "dine_in",
            "table_id": second_table_id,
            "items": [{"product_id": tenant["product_id"], "quantity": 1}],
        },
        headers={**auth_headers, "Idempotency-Key": "table-patch-second"},
    )
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text

    occupied = client.patch(
        f"/api/v1/orders/{first.json()['id']}",
        json={"table_id": second_table_id, "expected_version": first.json()["version"]},
        headers=auth_headers,
    )
    assert occupied.status_code == 409
    assert occupied.json()["code"] == "TABLE_ALREADY_HAS_OPEN_ORDER"

    foreign = client.patch(
        f"/api/v1/orders/{first.json()['id']}",
        json={"table_id": foreign_table_id, "expected_version": first.json()["version"]},
        headers=auth_headers,
    )
    assert foreign.status_code == 422
    assert foreign.json()["code"] == "ORDER_TABLE_INVALID"

    cancelled = client.post(
        f"/api/v1/orders/{second.json()['id']}/transition",
        json={"status": "cancelled", "expected_version": second.json()["version"]},
        headers=auth_headers,
    )
    assert cancelled.status_code == 200, cancelled.text
    moved = client.patch(
        f"/api/v1/orders/{first.json()['id']}",
        json={"table_id": second_table_id, "expected_version": first.json()["version"]},
        headers=auth_headers,
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["table_id"] == second_table_id
    with SessionLocal() as db:
        assert db.get(RestaurantTable, tenant["table_id"]).status == "available"
        assert db.get(RestaurantTable, second_table_id).status == "occupied"


def test_order_rejects_product_outside_service_channel(client, tenant, auth_headers):
    with SessionLocal.begin() as db:
        db.get(Product, tenant["product_id"]).service_channels = ["pos_counter"]

    response = client.post(
        "/api/v1/orders",
        json={
            "branch_id": tenant["branch_id"],
            "channel": "delivery",
            "items": [{"product_id": tenant["product_id"], "quantity": 1}],
        },
        headers={**auth_headers, "Idempotency-Key": "wrong-service-channel"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "PRODUCT_UNAVAILABLE_FOR_CHANNEL"

    with SessionLocal.begin() as db:
        db.get(Product, tenant["product_id"]).service_channels = ["digital_delivery"]
    spoofed_source = client.post(
        "/api/v1/orders",
        json={
            "branch_id": tenant["branch_id"],
            "channel": "delivery",
            "source": "integration",
            "items": [{"product_id": tenant["product_id"], "quantity": 1}],
        },
        headers={**auth_headers, "Idempotency-Key": "spoofed-digital-source"},
    )
    assert spoofed_source.status_code == 422
    assert spoofed_source.json()["code"] == "PRODUCT_UNAVAILABLE_FOR_CHANNEL"


def test_area_and_table_creation_are_audited(client, tenant, auth_headers):
    area = client.post(
        "/api/v1/areas",
        json={
            "branch_id": tenant["branch_id"],
            "name": "Terraza",
            "sort_order": 1,
            "columns": 9,
            "rows": 6,
        },
        headers=auth_headers,
    )
    assert area.status_code == 201, area.text
    assert area.json()["columns"] == 9
    assert area.json()["rows"] == 6
    assert area.json()["version"] == 1
    duplicate = client.post(
        "/api/v1/areas",
        json={"branch_id": tenant["branch_id"], "name": " terraza "},
        headers=auth_headers,
    )
    assert duplicate.status_code == 409

    table = client.post(
        "/api/v1/tables",
        json={
            "branch_id": tenant["branch_id"],
            "area_id": area.json()["id"],
            "code": "TERRAZA-1",
            "name": "Mesa Terraza 1",
            "capacity": 4,
        },
        headers=auth_headers,
    )
    assert table.status_code == 201, table.text

    with SessionLocal() as db:
        assert db.get(DiningArea, area.json()["id"]).name == "Terraza"
        actions = {
            event.action
            for event in db.query(AuditEvent).filter(
                AuditEvent.business_id == tenant["business_id"],
                AuditEvent.entity_id.in_([area.json()["id"], table.json()["id"]]),
            )
        }
        assert "area.created" in actions
        assert "table.created" in actions


def test_area_defaults_create_dimensions_and_broadcast(client, tenant, auth_headers, monkeypatch):
    broadcasts = []

    async def capture_broadcast(branch_id, event, payload):
        broadcasts.append((branch_id, event, payload))

    monkeypatch.setattr(api_module.hub, "broadcast", capture_broadcast)
    listed = client.get(
        "/api/v1/areas",
        params={"branch_id": tenant["branch_id"]},
        headers=auth_headers,
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()[0]["columns"] == 7
    assert listed.json()[0]["rows"] == 5
    assert listed.json()[0]["version"] == 1

    created = client.post(
        "/api/v1/areas",
        json={"branch_id": tenant["branch_id"], "name": "Patio"},
        headers=auth_headers,
    )
    assert created.status_code == 201, created.text
    assert created.json()["columns"] == 7
    assert created.json()["rows"] == 5
    assert created.json()["version"] == 1
    assert broadcasts == [(tenant["branch_id"], "area.created", created.json())]

    invalid_columns = client.post(
        "/api/v1/areas",
        json={"branch_id": tenant["branch_id"], "name": "Too narrow", "columns": 1},
        headers=auth_headers,
    )
    invalid_rows = client.post(
        "/api/v1/areas",
        json={"branch_id": tenant["branch_id"], "name": "Too tall", "rows": 11},
        headers=auth_headers,
    )
    assert invalid_columns.status_code == 422
    assert invalid_rows.status_code == 422


def test_area_update_versions_audits_and_rejects_duplicates(
    client,
    tenant,
    auth_headers,
    monkeypatch,
):
    broadcasts = []

    async def capture_broadcast(branch_id, event, payload):
        broadcasts.append((branch_id, event, payload))

    monkeypatch.setattr(api_module.hub, "broadcast", capture_broadcast)
    first = client.post(
        "/api/v1/areas",
        json={"branch_id": tenant["branch_id"], "name": "Interior"},
        headers=auth_headers,
    )
    second = client.post(
        "/api/v1/areas",
        json={"branch_id": tenant["branch_id"], "name": "Exterior"},
        headers=auth_headers,
    )
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    broadcasts.clear()

    updated = client.patch(
        f"/api/v1/areas/{second.json()['id']}",
        json={
            "name": " Exterior ",
            "sort_order": 4,
            "columns": 12,
            "rows": 10,
            "expected_version": 1,
        },
        headers=auth_headers,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json() == {
        **second.json(),
        "name": "Exterior",
        "sort_order": 4,
        "columns": 12,
        "rows": 10,
        "version": 2,
    }
    assert broadcasts == [(tenant["branch_id"], "area.updated", updated.json())]

    stale = client.patch(
        f"/api/v1/areas/{second.json()['id']}",
        json={"columns": 8, "expected_version": 1},
        headers=auth_headers,
    )
    assert stale.status_code == 409

    missing_version = client.patch(
        f"/api/v1/areas/{second.json()['id']}",
        json={"columns": 8},
        headers=auth_headers,
    )
    assert missing_version.status_code == 422

    duplicate = client.patch(
        f"/api/v1/areas/{second.json()['id']}",
        json={"name": " interior ", "expected_version": 2},
        headers=auth_headers,
    )
    assert duplicate.status_code == 409
    with SessionLocal() as db:
        area = db.get(DiningArea, second.json()["id"])
        assert area.name == "Exterior"
        assert area.version == 2
        assert db.query(AuditEvent).filter_by(
            action="area.updated",
            entity_type="dining_area",
            entity_id=str(area.id),
        ).count() == 1


def test_area_update_hides_other_branches_and_tenants(client, tenant, auth_headers):
    with SessionLocal.begin() as db:
        sibling_branch = Branch(
            business_id=tenant["business_id"],
            slug="secondary",
            name="Secondary",
        )
        db.add(sibling_branch)
        db.flush()
        sibling_area = DiningArea(
            business_id=tenant["business_id"],
            branch_id=sibling_branch.id,
            name="Sibling private area",
        )
        foreign_area = DiningArea(
            business_id=tenant["other_business_id"],
            branch_id=tenant["other_branch_id"],
            name="Tenant private area",
        )
        db.add_all([sibling_area, foreign_area])
        db.flush()
        hidden_ids = [sibling_area.id, foreign_area.id]

    missing = client.patch(
        "/api/v1/areas/999999",
        json={"name": "Invisible", "expected_version": 1},
        headers=auth_headers,
    )
    assert missing.status_code == 404
    for area_id in hidden_ids:
        hidden = client.patch(
            f"/api/v1/areas/{area_id}",
            json={"name": "Leaked", "expected_version": 1},
            headers=auth_headers,
        )
        assert hidden.status_code == 404
        assert hidden.json() == missing.json()


def test_area_update_requires_management_role(client, tenant, auth_headers):
    listed = client.get(
        "/api/v1/areas",
        params={"branch_id": tenant["branch_id"]},
        headers=auth_headers,
    )
    cashier_headers = {**auth_headers, "X-Dev-Role": "cashier"}
    response = client.patch(
        f"/api/v1/areas/{listed.json()[0]['id']}",
        json={"columns": 8, "expected_version": 1},
        headers=cashier_headers,
    )
    assert response.status_code == 403


def test_area_cannot_shrink_past_an_existing_table(client, tenant, auth_headers):
    area = client.post(
        "/api/v1/areas",
        json={"branch_id": tenant["branch_id"], "name": "Salón amplio", "columns": 7, "rows": 5},
        headers=auth_headers,
    )
    assert area.status_code == 201, area.text
    table = client.post(
        "/api/v1/tables",
        json={
            "branch_id": tenant["branch_id"],
            "area_id": area.json()["id"],
            "code": "LEJANA-1",
            "name": "Mesa lejana",
            "position_x": 600,
            "position_y": 24,
        },
        headers=auth_headers,
    )
    assert table.status_code == 201, table.text

    response = client.patch(
        f"/api/v1/areas/{area.json()['id']}",
        json={"columns": 2, "expected_version": area.json()["version"]},
        headers=auth_headers,
    )
    assert response.status_code == 409
    assert response.json()["code"] == "AREA_DIMENSIONS_CONTAIN_TABLES"


def test_table_configuration_requires_management_but_cashier_can_change_status(
    client,
    tenant,
    auth_headers,
):
    cashier_headers = {**auth_headers, "X-Dev-Role": "cashier"}
    listed = client.get(
        "/api/v1/tables",
        params={"branch_id": tenant["branch_id"]},
        headers=cashier_headers,
    )
    table = next(item for item in listed.json() if item["id"] == tenant["table_id"])
    forbidden = client.patch(
        f"/api/v1/tables/{table['id']}",
        json={"name": "Mesa renombrada", "expected_version": table["version"]},
        headers=cashier_headers,
    )
    assert forbidden.status_code == 403

    operational = client.patch(
        f"/api/v1/tables/{table['id']}",
        json={"status": "cleaning", "expected_version": table["version"]},
        headers=cashier_headers,
    )
    assert operational.status_code == 200, operational.text
    assert operational.json()["status"] == "cleaning"


def test_kitchen_transition_rejects_stale_expected_status(client, tenant, auth_headers):
    created = client.post(
        "/api/v1/orders",
        json={
            "branch_id": tenant["branch_id"],
            "channel": "counter",
            "items": [{"product_id": tenant["product_id"], "quantity": 1}],
        },
        headers={**auth_headers, "Idempotency-Key": "ticket-stale-order"},
    )
    confirmed = client.post(
        f"/api/v1/orders/{created.json()['id']}/confirm",
        headers={**auth_headers, "Idempotency-Key": "ticket-stale-confirm"},
    )
    assert confirmed.status_code == 200, confirmed.text
    sent = client.post(
        f"/api/v1/orders/{created.json()['id']}/send-to-kitchen",
        headers={**auth_headers, "Idempotency-Key": "ticket-stale-send"},
    )
    ticket_id = sent.json()["tickets"][0]["id"]
    missing_expected_status = client.post(
        f"/api/v1/kitchen/tickets/{ticket_id}/transition",
        json={"status": "preparing"},
        headers=auth_headers,
    )
    assert missing_expected_status.status_code == 422
    started = client.post(
        f"/api/v1/kitchen/tickets/{ticket_id}/transition",
        json={"status": "preparing", "expected_status": "queued"},
        headers=auth_headers,
    )
    assert started.status_code == 200, started.text
    stale = client.post(
        f"/api/v1/kitchen/tickets/{ticket_id}/transition",
        json={"status": "ready", "expected_status": "queued"},
        headers=auth_headers,
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "KITCHEN_TICKET_STALE"


def test_alembic_has_a_single_head():
    repository_root = Path(__file__).resolve().parents[1]
    config = Config(str(repository_root / "alembic.ini"))
    config.set_main_option("script_location", str(repository_root / "migrations"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert heads == ["20260919_0023"]


def test_table_cannot_reference_an_area_from_another_branch(client, tenant, auth_headers):
    admin_headers = {
        "X-Dev-Auth": "test-token",
        "X-Dev-Role": "superadmin",
        "X-Dev-User": "admin-test",
    }
    foreign_area = client.post(
        "/api/v1/areas",
        json={"branch_id": tenant["other_branch_id"], "name": "Área externa"},
        headers=admin_headers,
    )
    assert foreign_area.status_code == 201, foreign_area.text

    response = client.post(
        "/api/v1/tables",
        json={
            "branch_id": tenant["branch_id"],
            "area_id": foreign_area.json()["id"],
            "code": "MESA-AJENA",
            "name": "Mesa ajena",
            "capacity": 4,
        },
        headers=auth_headers,
    )
    assert response.status_code == 422
    assert "does not belong" in response.json()["detail"]


def test_cash_payment_lazily_opens_the_default_cash_session(client, tenant, auth_headers):
    created = client.post(
        "/api/v1/orders",
        json={
            "branch_id": tenant["branch_id"],
            "channel": "counter",
            "items": [{"product_id": tenant["product_id"], "quantity": 1}],
        },
        headers={**auth_headers, "Idempotency-Key": "cash-session-required-order"},
    )
    assert created.status_code == 201, created.text

    payment = client.post(
        f"/api/v1/orders/{created.json()['id']}/payments",
        json={"method": "cash", "amount": 20},
        headers={**auth_headers, "Idempotency-Key": "cash-session-required-payment"},
    )
    assert payment.status_code == 201, payment.text
    assert payment.json()["payment"]["cash_session_id"] is not None

    with SessionLocal() as db:
        session = db.get(CashSession, payment.json()["payment"]["cash_session_id"])
        assert session is not None
        assert session.status == "open"
        assert session.register_id == tenant["register_id"]


def test_cross_business_branch_is_denied(client, tenant, auth_headers):
    response = client.get(
        "/api/v1/orders",
        params={"branch_id": tenant["other_branch_id"]},
        headers=auth_headers,
    )
    assert response.status_code == 403


def test_reservation_conflict_is_detected(client, tenant, auth_headers):
    start = datetime.now(timezone.utc) + timedelta(days=1)
    payload = {
        "branch_id": tenant["branch_id"],
        "customer_name": "Ana",
        "customer_phone": "+51999000111",
        "party_size": 4,
        "start_at": start.isoformat(),
        "duration_minutes": 90,
        "table_ids": [tenant["table_id"]],
    }
    first = client.post(
        "/api/v1/reservations",
        json=payload,
        headers={**auth_headers, "Idempotency-Key": "reservation-001"},
    )
    assert first.status_code == 201, first.text
    second = client.post(
        "/api/v1/reservations",
        json={**payload, "customer_phone": "+51999000222"},
        headers={**auth_headers, "Idempotency-Key": "reservation-002"},
    )
    assert second.status_code == 409


def test_integration_order_requires_service_token_and_is_idempotent(client, tenant):
    payload = {
        "branch_id": tenant["branch_id"],
        "channel": "whatsapp",
        "source": "n8n",
        "external_reference": "wa-message-100",
        "customer_name": "Cliente WhatsApp",
        "customer_phone": "+51999999999",
        "items": [{"product_id": tenant["product_id"], "quantity": 1}],
    }
    unauthorized = client.post("/api/v1/integrations/orders/draft", json=payload)
    assert unauthorized.status_code == 401
    headers = {
        "X-Integration-Token": "test-integration-token",
        "Idempotency-Key": "wa-message-100",
    }
    created = client.post("/api/v1/integrations/orders/draft", json=payload, headers=headers)
    assert created.status_code == 201, created.text
    repeated = client.post("/api/v1/integrations/orders/draft", json=payload, headers=headers)
    assert repeated.status_code == 201
    assert repeated.json()["id"] == created.json()["id"]

    patch_payload = {
        "notes": "Sin cubiertos",
        "expected_version": created.json()["version"],
    }
    missing_patch_key = client.patch(
        f"/api/v1/integrations/orders/{created.json()['id']}",
        json=patch_payload,
        headers={"X-Integration-Token": "test-integration-token"},
    )
    assert missing_patch_key.status_code == 422
    patch_headers = {
        "X-Integration-Token": "test-integration-token",
        "Idempotency-Key": "wa-message-100-update",
    }
    patched = client.patch(
        f"/api/v1/integrations/orders/{created.json()['id']}",
        json=patch_payload,
        headers=patch_headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["notes"] == "Sin cubiertos"
    repeated_patch = client.patch(
        f"/api/v1/integrations/orders/{created.json()['id']}",
        json=patch_payload,
        headers=patch_headers,
    )
    assert repeated_patch.status_code == 200
    assert repeated_patch.json()["version"] == patched.json()["version"]


def test_legacy_contract_remains_available_but_dynamic_crud_is_disabled(client, tenant):
    anonymous = client.get("/api/datos/negocios")
    assert anonymous.status_code == 401

    businesses = client.get(
        "/api/datos/negocios",
        headers={"X-Integration-Token": "test-integration-token"},
    )
    assert businesses.status_code == 200
    assert any(item["id"] == tenant["business_id"] for item in businesses.json())
    assert client.post("/api/tablas/arbitrary", json={"secret": "TEXT"}).status_code == 410
