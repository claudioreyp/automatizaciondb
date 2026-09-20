from app.database import SessionLocal
from app.models import (
    Branch,
    Category,
    IntegrationCredential,
    InventoryItem,
    KitchenTicket,
    Order,
    PaymentEvidence,
    Product,
    RecipeItem,
    RestaurantTable,
)


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
        headers={**auth_headers, "X-Dev-Role": "superadmin"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def integration_headers(token: str, key: str | None = None):
    headers = {"Authorization": f"Bearer {token}"}
    if key:
        headers["Idempotency-Key"] = key
    return headers


def test_public_yape_qr_serves_active_business_image(client, tenant, auth_headers):
    image = b"\x89PNG\r\n\x1a\nqr-test"
    uploaded = client.post(
        f"/api/v1/branches/{tenant['branch_id']}/yape-qr",
        files={"file": ("yape.png", image, "image/png")},
        headers=auth_headers,
    )
    assert uploaded.status_code == 200, uploaded.text

    response = client.get("/api/v1/public/test-restaurant/main/yape-qr")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/png"
    assert response.content == image


def test_branch_credential_is_one_time_scoped_and_revocable(client, tenant, auth_headers):
    credential = create_credential(client, tenant, auth_headers, ["menu:read"])
    assert credential["token"].startswith("esc_live_")

    listed = client.get(
        "/api/v1/admin/integration-credentials",
        params={"branch_id": tenant["branch_id"]},
        headers={**auth_headers, "X-Dev-Role": "superadmin"},
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
        headers={**auth_headers, "X-Dev-Role": "superadmin"},
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


def test_integration_menu_and_orders_use_digital_service_channels(
    client,
    tenant,
    auth_headers,
):
    credential = create_credential(
        client,
        tenant,
        auth_headers,
        ["menu:read", "orders:write"],
    )
    headers = integration_headers(credential["token"])
    with SessionLocal.begin() as db:
        db.get(Product, tenant["product_id"]).service_channels = ["digital_delivery"]

    menu = client.get("/api/v1/integrations/context/menu", headers=headers)
    assert menu.status_code == 200, menu.text
    assert [product["id"] for product in menu.json()["products"]] == [tenant["product_id"]]

    created = client.post(
        "/api/v1/integrations/orders/draft",
        json={
            "branch_id": tenant["branch_id"],
            "channel": "delivery",
            "source": "pos",
            "items": [{"product_id": tenant["product_id"], "quantity": 1}],
        },
        headers=integration_headers(credential["token"], "digital-delivery-order"),
    )
    assert created.status_code == 201, created.text
    assert created.json()["source"] == "integration"

    with SessionLocal.begin() as db:
        db.get(Product, tenant["product_id"]).service_channels = ["pos_delivery"]

    filtered_menu = client.get("/api/v1/integrations/context/menu", headers=headers)
    assert filtered_menu.status_code == 200, filtered_menu.text
    assert filtered_menu.json()["products"] == []
    blocked = client.post(
        "/api/v1/integrations/orders/draft",
        json={
            "branch_id": tenant["branch_id"],
            "channel": "delivery",
            "items": [{"product_id": tenant["product_id"], "quantity": 1}],
        },
        headers=integration_headers(credential["token"], "pos-only-delivery-order"),
    )
    assert blocked.status_code == 422
    assert blocked.json()["code"] == "PRODUCT_UNAVAILABLE_FOR_CHANNEL"


def test_integrations_reject_ad_hoc_order_lines(client, tenant, auth_headers):
    credential = create_credential(client, tenant, auth_headers)
    token = credential["token"]
    arbitrary_line = {"name": "Producto inventado", "quantity": 1, "unit_price": 0.01}

    modern = client.post(
        "/api/v1/integrations/orders/draft",
        json={
            "branch_id": tenant["branch_id"],
            "channel": "whatsapp",
            "items": [arbitrary_line],
        },
        headers=integration_headers(token, "reject-modern-ad-hoc"),
    )
    assert modern.status_code == 422, modern.text
    assert modern.json()["code"] == "CATALOG_PRODUCT_REQUIRED"

    pos_draft = client.post(
        "/api/v1/orders",
        json={
            "branch_id": tenant["branch_id"],
            "channel": "counter",
            "items": [{"product_id": tenant["product_id"], "quantity": 1}],
        },
        headers={**auth_headers, "Idempotency-Key": "pos-draft-for-integration"},
    )
    assert pos_draft.status_code == 201, pos_draft.text
    patched_pos_draft = client.patch(
        f"/api/v1/integrations/orders/{pos_draft.json()['id']}",
        json={
            "expected_version": pos_draft.json()["version"],
            "items": [arbitrary_line],
        },
        headers=integration_headers(token, "reject-pos-draft-ad-hoc"),
    )
    assert patched_pos_draft.status_code == 422, patched_pos_draft.text
    assert patched_pos_draft.json()["code"] == "CATALOG_PRODUCT_REQUIRED"

    legacy = client.post(
        "/api/datos/pedidos_draft",
        json={
            "message_id": "reject-legacy-ad-hoc",
            "items_json": [arbitrary_line],
            "source": "pos",
        },
        headers=integration_headers(token),
    )
    assert legacy.status_code == 422, legacy.text
    assert legacy.json()["code"] == "CATALOG_PRODUCT_REQUIRED"


def test_legacy_draft_mapping_and_replay_are_scoped_to_credential_branch(
    client,
    tenant,
    auth_headers,
):
    first_credential = create_credential(client, tenant, auth_headers)
    with SessionLocal.begin() as db:
        product_name = db.get(Product, tenant["product_id"]).name
        second_branch = Branch(
            business_id=tenant["business_id"],
            slug="second",
            name="Second",
        )
        db.add(second_branch)
        db.flush()
        second_branch_id = second_branch.id
        second_category = Category(
            business_id=tenant["business_id"],
            branch_id=second_branch_id,
            name="Legacy",
        )
        db.add(second_category)
        db.flush()
        second_product = Product(
            business_id=tenant["business_id"],
            branch_id=second_branch_id,
            category_id=second_category.id,
            sku="LEGACY-SECOND",
            name=product_name,
            price=12,
        )
        db.add(second_product)
        db.flush()
        second_product_id = second_product.id

    superadmin_headers = {
        **auth_headers,
        "X-Dev-Role": "superadmin",
        "X-Dev-User": "platform-superadmin",
        "X-Branch-Id": str(second_branch_id),
    }
    second_credential = client.post(
        "/api/v1/admin/integration-credentials",
        json={"branch_id": second_branch_id, "name": "n8n second branch"},
        headers=superadmin_headers,
    )
    assert second_credential.status_code == 201, second_credential.text

    payload = {
        "negocio_id": tenant["other_business_id"],
        "message_id": "same-message-across-branches",
        "customer_name": "Legacy customer",
        "items_json": [{"name": product_name, "quantity": 1, "unit_price": 0.01}],
    }
    first = client.post(
        "/api/datos/pedidos_draft",
        json=payload,
        headers=integration_headers(first_credential["token"]),
    )
    assert first.status_code == 200, first.text
    first_order = first.json()["dato_guardado"]
    assert first_order["business_id"] == tenant["business_id"]
    assert first_order["branch_id"] == tenant["branch_id"]

    second = client.post(
        "/api/datos/pedidos_draft",
        json=payload,
        headers=integration_headers(second_credential.json()["token"]),
    )
    assert second.status_code == 409, second.text
    assert second.json() == {
        "detail": "Message reference is already in use",
        "code": "LEGACY_MESSAGE_ALREADY_USED",
    }

    second_payload = {
        **payload,
        "message_id": "second-branch-message",
        "items_json": [{"product_id": second_product_id, "quantity": 1, "unit_price": 0.01}],
    }
    second_created = client.post(
        "/api/datos/pedidos_draft",
        json=second_payload,
        headers=integration_headers(second_credential.json()["token"]),
    )
    assert second_created.status_code == 200, second_created.text
    second_order = second_created.json()["dato_guardado"]
    assert second_order["business_id"] == tenant["business_id"]
    assert second_order["branch_id"] == second_branch_id
    assert second_order["id"] != first_order["id"]

    replay = client.post(
        "/api/datos/pedidos_draft",
        json=second_payload,
        headers=integration_headers(second_credential.json()["token"]),
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["dato_guardado"]["id"] == second_order["id"]


def test_integration_cannot_reassign_a_table_after_order_is_final(
    client,
    tenant,
    auth_headers,
):
    credential = create_credential(client, tenant, auth_headers)
    with SessionLocal.begin() as db:
        second_table = RestaurantTable(
            business_id=tenant["business_id"],
            branch_id=tenant["branch_id"],
            code="M-FINAL",
            name="Mesa final",
            capacity=4,
        )
        db.add(second_table)
        db.flush()
        second_table_id = second_table.id

    created = client.post(
        "/api/v1/integrations/orders/draft",
        json={
            "branch_id": tenant["branch_id"],
            "channel": "dine_in",
            "table_id": tenant["table_id"],
            "items": [{"product_id": tenant["product_id"], "quantity": 1}],
        },
        headers=integration_headers(credential["token"], "final-table-create"),
    )
    assert created.status_code == 201, created.text
    with SessionLocal.begin() as db:
        db.get(Order, created.json()["id"]).status = "closed"
        db.get(RestaurantTable, tenant["table_id"]).status = "cleaning"

    rejected = client.patch(
        f"/api/v1/integrations/orders/{created.json()['id']}",
        json={
            "table_id": second_table_id,
            "expected_version": created.json()["version"],
        },
        headers=integration_headers(credential["token"], "final-table-patch"),
    )
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["code"] == "ORDER_TABLE_ASSIGNMENT_LOCKED"
    with SessionLocal() as db:
        order = db.get(Order, created.json()["id"])
        assert order.table_id == tenant["table_id"]
        assert db.get(RestaurantTable, tenant["table_id"]).status == "cleaning"
        assert db.get(RestaurantTable, second_table_id).status == "available"


def test_payment_evidence_ids_are_hidden_across_tenants(
    client,
    tenant,
    auth_headers,
):
    created = client.post(
        "/api/v1/orders",
        json={
            "branch_id": tenant["branch_id"],
            "channel": "counter",
            "items": [{"product_id": tenant["product_id"], "quantity": 1}],
        },
        headers={**auth_headers, "Idempotency-Key": "hidden-evidence-order"},
    )
    assert created.status_code == 201, created.text
    with SessionLocal.begin() as db:
        evidence = PaymentEvidence(
            business_id=tenant["business_id"],
            order_id=created.json()["id"],
            provider="yape",
            storage_path="private/hidden.webp",
            image_sha256="d" * 64,
            status="under_review",
        )
        db.add(evidence)
        db.flush()
        evidence_id = evidence.id

    foreign_headers = {
        **auth_headers,
        "X-Business-Id": str(tenant["other_business_id"]),
        "X-Branch-Id": str(tenant["other_branch_id"]),
        "X-Dev-User": "foreign-owner",
    }
    foreign_image = client.get(
        f"/api/v1/payment-evidence/{evidence_id}/image",
        headers=foreign_headers,
    )
    missing_image = client.get(
        "/api/v1/payment-evidence/999999/image",
        headers={**auth_headers, "X-Dev-Role": "superadmin"},
    )
    assert foreign_image.status_code == 404
    assert foreign_image.json() == missing_image.json() == {
        "detail": "Payment evidence not found",
        "code": "PAYMENT_EVIDENCE_NOT_FOUND",
    }

    foreign_review = client.post(
        f"/api/v1/payment-evidence/{evidence_id}/review",
        json={"approve": False},
        headers=foreign_headers,
    )
    missing_review = client.post(
        "/api/v1/payment-evidence/999999/review",
        json={"approve": False},
        headers=auth_headers,
    )
    assert foreign_review.status_code == 404
    assert foreign_review.json() == missing_review.json() == {
        "detail": "Payment evidence not found",
        "code": "PAYMENT_EVIDENCE_NOT_FOUND",
    }


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

    duplicate_evidence = client.post(
        f"/api/v1/integrations/orders/{order_id}/payment-evidence",
        data={"provider": "yape", "operation_number": "YP-000002"},
        files={"file": ("second.png", b"not-an-image", "image/png")},
        headers=integration_headers(token, "evidence-image-002"),
    )
    assert duplicate_evidence.status_code == 409, duplicate_evidence.text
    assert duplicate_evidence.json()["code"] == "PAYMENT_EVIDENCE_UNDER_REVIEW"

    approved = client.post(
        f"/api/v1/payment-evidence/{evidence_id}/review",
        json={"approve": True, "note": "Validado por caja"},
        headers=auth_headers,
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["order"]["payment_status"] == "paid"
    assert approved.json()["order"]["status"] == "sent_to_kitchen"
    assert approved.json()["notification"]["queued"] is True
    assert approved.json()["notification"]["recipient_available"] is True
    with SessionLocal() as db:
        assert db.query(KitchenTicket).filter_by(order_id=order_id).count() == 1

    with SessionLocal.begin() as db:
        stale = PaymentEvidence(
            business_id=tenant["business_id"],
            order_id=order_id,
            provider="yape",
            storage_path="private/stale-evidence.webp",
            image_sha256="c" * 64,
            operation_number="YP-STALE-001",
            status="under_review",
        )
        db.add(stale)
        db.flush()
        stale_evidence_id = stale.id

    stale_rejection = client.post(
        f"/api/v1/payment-evidence/{stale_evidence_id}/review",
        json={"approve": False, "note": "Revisión tardía"},
        headers=auth_headers,
    )
    assert stale_rejection.status_code == 200, stale_rejection.text
    assert stale_rejection.json()["evidence"]["status"] == "superseded"
    assert stale_rejection.json()["order"]["payment_status"] == "paid"
    assert stale_rejection.json()["order"]["status"] == "sent_to_kitchen"

    events = client.get(
        "/api/v1/integrations/events",
        headers=integration_headers(token),
    )
    assert events.status_code == 200, events.text
    payment_event = next(item for item in events.json() if item["event_type"] == "payment.approved")
    assert payment_event["whatsapp_chat_id"] == "51999999999@c.us"
    assert payment_event["payload"]["channel"] == "whatsapp"
    assert payment_event["payload"]["message"] == (
        "¡Pago confirmado! Tu pedido fue aprobado y ya está en preparación. "
        "Te avisaremos cuando esté listo para que puedas venir al local. 🍕"
    )
    assert approved.json()["notification"]["event_id"] == payment_event["id"]

    repeated_approval = client.post(
        f"/api/v1/payment-evidence/{evidence_id}/review",
        json={"approve": True, "note": "Reintento del mismo clic"},
        headers=auth_headers,
    )
    assert repeated_approval.status_code == 200, repeated_approval.text
    assert repeated_approval.json()["notification"]["event_id"] == payment_event["id"]
    repeated_events = client.get(
        "/api/v1/integrations/events",
        headers=integration_headers(token),
    )
    assert repeated_events.status_code == 200
    assert len(
        [
            item
            for item in repeated_events.json()
            if item["event_type"] == "payment.approved"
            and item["aggregate_id"] == str(order_id)
        ]
    ) == 1

    future_events = client.get(
        "/api/v1/integrations/events",
        params={"created_after": "2999-01-01T00:00:00Z"},
        headers=integration_headers(token),
    )
    assert future_events.status_code == 200
    assert future_events.json() == []

    filtered_events = client.get(
        "/api/v1/integrations/events",
        params={"event_types": ["payment.approved", "payment.rejected"]},
        headers=integration_headers(token),
    )
    assert filtered_events.status_code == 200
    assert any(item["id"] == payment_event["id"] for item in filtered_events.json())
    assert all(item["event_type"] in {"payment.approved", "payment.rejected"} for item in filtered_events.json())

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

    no_recipient_order = client.post(
        "/api/v1/integrations/orders/draft",
        json={
            "branch_id": tenant["branch_id"],
            "channel": "counter",
            "source": "n8n",
            "external_reference": "payment-without-whatsapp-recipient",
            "payment_method": "yape",
            "items": [{"product_id": tenant["product_id"], "quantity": 1}],
        },
        headers=integration_headers(token, "payment-without-whatsapp-recipient"),
    )
    assert no_recipient_order.status_code == 201, no_recipient_order.text
    no_recipient_order_id = no_recipient_order.json()["id"]
    no_recipient_evidence = client.post(
        f"/api/v1/integrations/orders/{no_recipient_order_id}/payment-evidence",
        data={"provider": "yape", "operation_number": "YP-NO-RECIPIENT"},
        files={"file": ("yape.png", b"\x89PNG\r\n\x1a\nproof-2", "image/png")},
        headers=integration_headers(token, "evidence-without-whatsapp-recipient"),
    )
    assert no_recipient_evidence.status_code == 201, no_recipient_evidence.text
    no_recipient_approval = client.post(
        f"/api/v1/payment-evidence/{no_recipient_evidence.json()['evidence']['id']}/review",
        json={"approve": True},
        headers=auth_headers,
    )
    assert no_recipient_approval.status_code == 200, no_recipient_approval.text
    assert no_recipient_approval.json()["notification"] == {
        "event_id": None,
        "event_type": None,
        "recipient_available": False,
        "queued": False,
        "acknowledged": False,
    }
    events_without_recipient = client.get(
        "/api/v1/integrations/events",
        headers=integration_headers(token),
    )
    assert all(
        item["aggregate_id"] != str(no_recipient_order_id)
        for item in events_without_recipient.json()
    )


def test_non_receipt_image_is_not_confirmed_and_leaves_an_operational_note(
    client, tenant, auth_headers
):
    credential = create_credential(client, tenant, auth_headers)
    token = credential["token"]
    created = client.post(
        "/api/v1/integrations/orders/draft",
        json={
            "branch_id": tenant["branch_id"],
            "channel": "delivery",
            "source": "whatsapp_agent",
            "external_reference": "wa-invalid-image-001",
            "whatsapp_chat_id": "51988888888@c.us",
            "whatsapp_message_id": "wamid-invalid-order",
            "customer_name": "Cliente imagen",
            "customer_phone": "+51988888888",
            "payment_method": "yape",
            "delivery_address": {
                "address": "Calle Lima 123",
                "reference": "Frente al parque",
            },
            "items": [{"product_id": tenant["product_id"], "quantity": 1}],
        },
        headers=integration_headers(token, "wa-invalid-image-001"),
    )
    assert created.status_code == 201, created.text
    order_id = created.json()["id"]

    uploaded = client.post(
        f"/api/v1/integrations/orders/{order_id}/payment-evidence",
        data={
            "provider": "yape",
            "looks_like_payment_receipt": "false",
            "analysis_warnings": '["La imagen contiene un personaje animado."]',
            "whatsapp_message_id": "wamid-invalid-image",
        },
        files={"file": ("pokemon.png", b"\x89PNG\r\n\x1a\nnot-a-receipt", "image/png")},
        headers=integration_headers(token, "invalid-image-001"),
    )
    assert uploaded.status_code == 201, uploaded.text
    body = uploaded.json()
    assert body["receipt_detected"] is False
    assert body["requires_human_review"] is False
    assert body["evidence"]["status"] == "not_a_receipt"
    assert body["evidence"]["amount_detected"] is None
    assert body["evidence"]["operation_number"] is None
    assert body["evidence"]["security_code"] is None
    assert body["order"]["status"] == "pending_confirmation"
    assert body["order"]["payment_status"] == "invalid_evidence"
    assert "no parece ser un comprobante" in body["order"]["notes"]
    with SessionLocal() as db:
        assert db.query(KitchenTicket).filter_by(order_id=order_id).count() == 0


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
