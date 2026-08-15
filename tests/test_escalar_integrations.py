from app.database import SessionLocal
from app.models import IntegrationCredential, InventoryItem, KitchenTicket, Order, PaymentEvidence, Product, RecipeItem


def create_credential(client, tenant, auth_headers, scopes=None):
    payload = {
        "branch_id": tenant["branch_id"],
        "name": "n8n homologacion",
    }
    if scopes is not None:
        payload["scopes"] = scopes
    response = client.post(
        "/api/v1/admin/integration-credentials",
        json=payload,
        headers=auth_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def integration_headers(token: str, key: str | None = None):
    headers = {"Authorization": f"Bearer {token}"}
    if key:
        headers["Idempotency-Key"] = key
    return headers


def test_branch_credential_is_one_time_scoped_and_revocable(client, tenant, auth_headers):
    credential = create_credential(client, tenant, auth_headers, ["menu:read"])
    assert credential["token"].startswith("esc_live_")

    listed = client.get(
        "/api/v1/admin/integration-credentials",
        params={"branch_id": tenant["branch_id"]},
        headers=auth_headers,
    )
    assert listed.status_code == 200
    assert "token" not in listed.json()[0]
    assert "token_hash" not in listed.json()[0]

    context = client.get(
        "/api/v1/integrations/context",
        headers=integration_headers(credential["token"]),
    )
    assert context.status_code == 200, context.text
    assert context.json()["branch"]["id"] == tenant["branch_id"]
    assert "yape_qr_storage_path" not in context.json()["branch"]

    denied = client.post(
        "/api/v1/integrations/orders/draft",
        json={
            "branch_id": tenant["branch_id"],
            "items": [{"product_id": tenant["product_id"], "quantity": 1}],
        },
        headers=integration_headers(credential["token"], "scope-denied"),
    )
    assert denied.status_code == 403

    revoked = client.post(
        f"/api/v1/admin/integration-credentials/{credential['id']}/revoke",
        headers=auth_headers,
    )
    assert revoked.status_code == 200
    assert revoked.json()["active"] is False
    assert client.get(
        "/api/v1/integrations/context",
        headers=integration_headers(credential["token"]),
    ).status_code == 401


def test_credential_cannot_cross_branch(client, tenant, auth_headers):
    credential = create_credential(client, tenant, auth_headers)
    assert "inventory:write" not in credential["scopes"]
    response = client.post(
        "/api/v1/integrations/orders/draft",
        json={
            "branch_id": tenant["other_branch_id"],
            "items": [{"name": "Intento", "quantity": 1, "unit_price": 1}],
        },
        headers=integration_headers(credential["token"], "cross-branch"),
    )
    assert response.status_code == 403


def test_yape_requires_real_image_and_human_approval_emits_durable_event(
    client, tenant, auth_headers
):
    credential = create_credential(client, tenant, auth_headers)
    token = credential["token"]
    created = client.post(
        "/api/v1/integrations/orders/draft",
        json={
            "branch_id": tenant["branch_id"],
            "channel": "whatsapp",
            "source": "n8n",
            "external_reference": "wa-payment-001",
            "whatsapp_chat_id": "51999999999@c.us",
            "whatsapp_message_id": "wamid-001",
            "customer_name": "Cliente Yape",
            "customer_phone": "+51999999999",
            "payment_method": "yape",
            "items": [{"product_id": tenant["product_id"], "quantity": 1}],
        },
        headers=integration_headers(token, "wa-payment-001"),
    )
    assert created.status_code == 201, created.text
    order_id = created.json()["id"]

    no_image = client.post(
        f"/api/v1/integrations/orders/{order_id}/payment-evidence",
        data={"provider": "yape"},
        headers=integration_headers(token, "evidence-no-image"),
    )
    assert no_image.status_code == 422

    uploaded = client.post(
        f"/api/v1/integrations/orders/{order_id}/payment-evidence",
        data={
            "provider": "yape",
            "amount_detected": "20.00",
            "operation_number": "YP-000001",
            "security_code": "228",
            "whatsapp_message_id": "wamid-proof-001",
        },
        files={"file": ("yape.png", b"\x89PNG\r\n\x1a\nproof", "image/png")},
        headers=integration_headers(token, "evidence-image-001"),
    )
    assert uploaded.status_code == 201, uploaded.text
    body = uploaded.json()
    assert body["requires_human_review"] is True
    assert body["evidence"]["security_code"] == "228"
    assert body["order"]["status"] == "confirmed"
    assert body["order"]["payment_status"] == "evidence_received"
    evidence_id = body["evidence"]["id"]
    with SessionLocal() as db:
        assert db.query(KitchenTicket).filter_by(order_id=order_id).count() == 0
        assert db.get(PaymentEvidence, evidence_id).image_sha256

    approved = client.post(
        f"/api/v1/payment-evidence/{evidence_id}/review",
        json={"approve": True, "note": "Validado por caja"},
        headers=auth_headers,
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["order"]["payment_status"] == "paid"
    assert approved.json()["order"]["status"] == "sent_to_kitchen"
    with SessionLocal() as db:
        assert db.query(KitchenTicket).filter_by(order_id=order_id).count() == 1

    events = client.get(
        "/api/v1/integrations/events",
        headers=integration_headers(token),
    )
    assert events.status_code == 200, events.text
    payment_event = next(item for item in events.json() if item["event_type"] == "payment.approved")
    assert payment_event["whatsapp_chat_id"] == "51999999999@c.us"

    acknowledged = client.post(
        f"/api/v1/integrations/events/{payment_event['id']}/ack",
        headers=integration_headers(token, f"ack-{payment_event['id']}"),
    )
    assert acknowledged.status_code == 200
    pending = client.get(
        "/api/v1/integrations/events",
        headers=integration_headers(token),
    )
    assert all(item["id"] != payment_event["id"] for item in pending.json())


def test_cash_confirm_is_atomic_and_idempotent(client, tenant, auth_headers):
    credential = create_credential(client, tenant, auth_headers)
    token = credential["token"]
    created = client.post(
        "/api/v1/integrations/orders/draft",
        json={
            "branch_id": tenant["branch_id"],
            "channel": "takeaway",
            "customer_phone": "+51911111111",
            "payment_method": "cash",
            "items": [{"product_id": tenant["product_id"], "quantity": 1}],
        },
        headers=integration_headers(token, "cash-order-001"),
    )
    order_id = created.json()["id"]
    headers = integration_headers(token, "cash-confirm-001")
    confirmed = client.post(
        f"/api/v1/integrations/orders/{order_id}/cash-confirm",
        headers=headers,
    )
    assert confirmed.status_code == 200, confirmed.text
    repeated = client.post(
        f"/api/v1/integrations/orders/{order_id}/cash-confirm",
        headers=headers,
    )
    assert repeated.status_code == 200
    assert repeated.json()["order"]["id"] == order_id
    with SessionLocal() as db:
        order = db.get(Order, order_id)
        assert order.status == "sent_to_kitchen"
        assert order.payment_status == "pending"
        assert order.submitted_at is not None
        assert db.query(KitchenTicket).filter_by(order_id=order_id).count() == 1


def test_integration_reservation_checks_tables_and_is_idempotent(client, tenant, auth_headers):
    credential = create_credential(
        client,
        tenant,
        auth_headers,
        ["reservations:write"],
    )
    token = credential["token"]
    start_at = "2035-08-20T19:00:00-05:00"

    availability = client.get(
        "/api/v1/integrations/context/tables",
        params={"start_at": start_at, "party_size": 2, "duration_minutes": 90},
        headers=integration_headers(token),
    )
    assert availability.status_code == 200, availability.text
    assert availability.json()["available"] is True
    assert tenant["table_id"] in [table["id"] for table in availability.json()["tables"]]

    payload = {
        "branch_id": tenant["branch_id"],
        "customer_name": "Reserva WhatsApp",
        "customer_phone": "51922222222",
        "party_size": 2,
        "start_at": start_at,
        "duration_minutes": 90,
        "table_ids": [tenant["table_id"]],
        "source": "whatsapp_agent",
    }
    headers = integration_headers(token, "reservation-wa-001")
    created = client.post(
        "/api/v1/integrations/reservations",
        json=payload,
        headers=headers,
    )
    assert created.status_code == 201, created.text
    repeated = client.post(
        "/api/v1/integrations/reservations",
        json=payload,
        headers=headers,
    )
    assert repeated.status_code == 201
    assert repeated.json()["id"] == created.json()["id"]


def test_integration_human_handoff_creates_a_durable_event(client, tenant, auth_headers):
    credential = create_credential(
        client,
        tenant,
        auth_headers,
        ["orders:read", "orders:write", "events:read"],
    )
    token = credential["token"]
    created = client.post(
        "/api/v1/integrations/orders/draft",
        json={
            "branch_id": tenant["branch_id"],
            "channel": "whatsapp",
            "whatsapp_chat_id": "51933333333@c.us",
            "customer_phone": "51933333333",
            "items": [{"product_id": tenant["product_id"], "quantity": 1}],
        },
        headers=integration_headers(token, "human-order-001"),
    )
    assert created.status_code == 201, created.text

    requested = client.post(
        f"/api/v1/integrations/orders/{created.json()['id']}/request-human",
        params={"reason": "customer_requested_order_change"},
        headers=integration_headers(token, "human-request-001"),
    )
    assert requested.status_code == 200, requested.text
    assert requested.json()["status"] == "queued"

    events = client.get(
        "/api/v1/integrations/events",
        headers=integration_headers(token),
    )
    assert events.status_code == 200, events.text
    event = next(item for item in events.json() if item["event_type"] == "human.requested")
    assert event["aggregate_id"] == str(created.json()["id"])
    assert event["whatsapp_chat_id"] == "51933333333@c.us"


def test_integration_can_replace_draft_items_but_not_confirmed_items(
    client, tenant, auth_headers
):
    credential = create_credential(client, tenant, auth_headers)
    token = credential["token"]
    created = client.post(
        "/api/v1/integrations/orders/draft",
        json={
            "branch_id": tenant["branch_id"],
            "channel": "takeaway",
            "items": [{"product_id": tenant["product_id"], "quantity": 1}],
        },
        headers=integration_headers(token, "replace-items-create"),
    )
    assert created.status_code == 201, created.text
    order = created.json()

    replaced = client.patch(
        f"/api/v1/integrations/orders/{order['id']}",
        json={
            "expected_version": order["version"],
            "items": [{"product_id": tenant["product_id"], "quantity": 2}],
        },
        headers=integration_headers(token, "replace-items-draft"),
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["items"][0]["quantity"] == 2

    confirmed = client.post(
        f"/api/v1/integrations/orders/{order['id']}/cash-confirm",
        headers=integration_headers(token, "replace-items-confirm"),
    )
    assert confirmed.status_code == 200, confirmed.text
    rejected = client.patch(
        f"/api/v1/integrations/orders/{order['id']}",
        json={
            "expected_version": confirmed.json()["order"]["version"],
            "items": [{"product_id": tenant["product_id"], "quantity": 1}],
        },
        headers=integration_headers(token, "replace-items-after-confirm"),
    )
    assert rejected.status_code == 409


def test_integration_cannot_override_catalog_price(client, tenant, auth_headers):
    credential = create_credential(client, tenant, auth_headers)
    with SessionLocal() as db:
        catalog_price = float(db.get(Product, tenant["product_id"]).price)

    created = client.post(
        "/api/v1/integrations/orders/draft",
        json={
            "branch_id": tenant["branch_id"],
            "channel": "whatsapp",
            "items": [
                {
                    "product_id": tenant["product_id"],
                    "quantity": 2,
                    "unit_price": 0.01,
                }
            ],
        },
        headers=integration_headers(credential["token"], "catalog-price-authoritative"),
    )
    assert created.status_code == 201, created.text
    assert created.json()["items"][0]["unit_price"] == catalog_price
    assert created.json()["subtotal"] == catalog_price * 2


def test_catalog_csv_preview_is_read_only_and_commit_creates_recipe_stock(
    client, tenant, auth_headers
):
    csv_content = (
        "sku,name,category,price,description,available,preparation_station,"
        "stock_quantity,stock_unit,minimum_stock,recipe_quantity\n"
        "BEB-INKA-500,Inca Kola 500 ml,Bebidas,6.50,Botella personal,true,bar,"
        "24,unit,4,1\n"
    ).encode()
    files = {"file": ("catalogo.csv", csv_content, "text/csv")}

    preview = client.post(
        "/api/v1/catalog/import-csv",
        params={"branch_id": tenant["branch_id"], "dry_run": "true"},
        files=files,
        headers=auth_headers,
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["valid_rows"] == 1
    assert preview.json()["errors"] == []
    with SessionLocal() as db:
        assert db.query(Product).filter_by(sku="BEB-INKA-500").count() == 0

    committed = client.post(
        "/api/v1/catalog/import-csv",
        params={"branch_id": tenant["branch_id"], "dry_run": "false"},
        files={"file": ("catalogo.csv", csv_content, "text/csv")},
        headers=auth_headers,
    )
    assert committed.status_code == 200, committed.text
    assert committed.json()["created"] == 1
    with SessionLocal() as db:
        product = db.query(Product).filter_by(sku="BEB-INKA-500").one()
        stock = db.query(InventoryItem).filter_by(sku="STOCK-BEB-INKA-500").one()
        recipe = db.query(RecipeItem).filter_by(product_id=product.id).one()
        assert float(stock.quantity) == 24
        assert recipe.inventory_item_id == stock.id
        assert float(recipe.quantity) == 1
