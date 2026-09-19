from app.database import SessionLocal
from app.models import AuditEvent, Product


def create_credential(client, tenant, auth_headers, scopes):
    response = client.post(
        "/api/v1/admin/integration-credentials",
        json={
            "branch_id": tenant["branch_id"],
            "name": "Disponibilidad POS",
            "scopes": scopes,
        },
        headers={**auth_headers, "X-Dev-Role": "superadmin"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def integration_headers(token: str, key: str | None = None):
    headers = {"Authorization": f"Bearer {token}"}
    if key:
        headers["Idempotency-Key"] = key
    return headers


def test_branch_menu_card_and_agent_context_are_private_and_scoped(
    client, tenant, auth_headers
):
    updated = client.patch(
        f"/api/v1/branches/{tenant['branch_id']}",
        json={
            "agent_context_notes": (
                "Atender con tono cálido. No prometer stock ni descuentos no configurados."
            )
        },
        headers=auth_headers,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["agent_context_notes"].startswith("Atender con tono")

    image = b"\x89PNG\r\n\x1a\nprinted-menu"
    uploaded = client.post(
        f"/api/v1/branches/{tenant['branch_id']}/menu-card",
        files={"file": ("carta.png", image, "image/png")},
        headers=auth_headers,
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["menu_card_configured"] is True
    assert "menu_card_storage_path" not in uploaded.json()

    preview = client.get(
        f"/api/v1/branches/{tenant['branch_id']}/menu-card",
        headers=auth_headers,
    )
    assert preview.status_code == 200, preview.text
    assert preview.headers["content-type"] == "image/png"
    assert preview.content == image

    credential = create_credential(client, tenant, auth_headers, ["menu:read"])
    context = client.get(
        "/api/v1/integrations/context",
        headers=integration_headers(credential["token"]),
    )
    assert context.status_code == 200, context.text
    assert context.json()["branch"]["menu_card_configured"] is True
    assert context.json()["branch"]["agent_context_notes"].startswith("Atender con tono")

    integration_preview = client.get(
        "/api/v1/integrations/context/menu-card",
        headers=integration_headers(credential["token"]),
    )
    assert integration_preview.status_code == 200, integration_preview.text
    assert integration_preview.content == image


def test_product_availability_is_audited_and_immediately_changes_agent_menu(
    client, tenant, auth_headers
):
    disabled = client.patch(
        f"/api/v1/catalog/products/{tenant['product_id']}/availability",
        json={"available": False},
        headers=auth_headers,
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["available"] is False

    credential = create_credential(
        client,
        tenant,
        auth_headers,
        ["menu:read", "inventory:write"],
    )
    menu = client.get(
        "/api/v1/integrations/context/menu",
        headers=integration_headers(credential["token"]),
    )
    assert menu.status_code == 200, menu.text
    assert tenant["product_id"] not in [item["id"] for item in menu.json()["products"]]

    missing_key = client.patch(
        f"/api/v1/integrations/context/menu/{tenant['product_id']}/availability",
        json={"available": True},
        headers=integration_headers(credential["token"]),
    )
    assert missing_key.status_code == 422

    enabled = client.patch(
        f"/api/v1/integrations/context/menu/{tenant['product_id']}/availability",
        json={"available": True},
        headers=integration_headers(credential["token"], "availability-pizza-1"),
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["available"] is True

    repeated = client.patch(
        f"/api/v1/integrations/context/menu/{tenant['product_id']}/availability",
        json={"available": False},
        headers=integration_headers(credential["token"], "availability-pizza-1"),
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["available"] is True

    with SessionLocal() as db:
        assert db.get(Product, tenant["product_id"]).available is True
        actions = [
            event.action
            for event in db.query(AuditEvent)
            .filter(AuditEvent.entity_type == "product")
            .all()
        ]
        assert "product.availability_changed" in actions
        assert "integration.product_availability_changed" in actions
