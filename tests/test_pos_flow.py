from datetime import datetime, timedelta, timezone

from app.database import SessionLocal
from app.models import InventoryItem, KitchenTicket, Membership


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
            "X-Business-Id": str(tenant["business_id"]),
            "X-Branch-Id": str(tenant["branch_id"]),
        },
    )
    assert allowed.status_code == 200, allowed.text

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

    for next_status in ["preparing", "ready", "closed"]:
        transitioned = client.post(
            f"/api/v1/orders/{order['id']}/transition",
            json={"status": next_status},
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
    businesses = client.get("/api/datos/negocios")
    assert businesses.status_code == 200
    assert any(item["id"] == tenant["business_id"] for item in businesses.json())
    assert client.post("/api/tablas/arbitrary", json={"secret": "TEXT"}).status_code == 410
