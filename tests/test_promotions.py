from datetime import date, timedelta


def create_promotion(client, tenant, auth_headers, **overrides):
    payload = {
        "branch_id": tenant["branch_id"],
        "name": "Promo pizzas",
        "promotion_type": "product_discount",
        "discount_type": "percentage",
        "discount_value": 10,
        "target_scope": "products",
        "target_ids": [tenant["product_id"]],
        "weekdays": [],
        "service_channels": ["pos_counter"],
        "active": True,
        **overrides,
    }
    response = client.post("/api/v1/catalog/promotions", json=payload, headers=auth_headers)
    assert response.status_code == 201, response.text
    return response.json()


def create_counter_order(client, tenant, auth_headers, items, key, discount=0):
    response = client.post(
        "/api/v1/orders",
        json={
            "branch_id": tenant["branch_id"],
            "channel": "counter",
            "source": "pos",
            "discount": discount,
            "items": items,
        },
        headers={**auth_headers, "Idempotency-Key": key},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_best_promotion_is_applied_and_manual_discount_remains_auditable(
    client, tenant, auth_headers
):
    create_promotion(client, tenant, auth_headers, name="Diez por ciento")
    create_promotion(
        client,
        tenant,
        auth_headers,
        name="Cinco soles",
        discount_type="fixed_amount",
        discount_value=5,
    )

    order = create_counter_order(
        client,
        tenant,
        auth_headers,
        [{"product_id": tenant["product_id"], "quantity": 2}],
        "promotion-best-saving",
    )
    assert order["subtotal"] == 40
    assert order["promotion_discount"] == 10
    assert order["manual_discount"] == 0
    assert order["discount"] == 10
    assert order["total"] == 30
    assert order["items"][0]["promotion_discount"] == 10
    assert order["items"][0]["promotion_snapshot"]["name"] == "Cinco soles"

    updated = client.patch(
        f"/api/v1/orders/{order['id']}",
        json={"discount": 15, "expected_version": order["version"]},
        headers=auth_headers,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["manual_discount"] == 15
    assert updated.json()["promotion_discount"] == 10
    assert updated.json()["discount"] == 15
    assert updated.json()["total"] == 25


def test_buy_x_pay_y_does_not_mix_products_or_variants(client, tenant, auth_headers):
    catalog = client.get(
        "/api/v1/catalog",
        params={"branch_id": tenant["branch_id"]},
        headers=auth_headers,
    ).json()
    category_id = catalog["categories"][0]["id"]
    second = client.post(
        "/api/v1/catalog/products",
        json={
            "branch_id": tenant["branch_id"],
            "category_id": category_id,
            "sku": "PIZZA-SECOND",
            "name": "Segunda pizza",
            "price": 20,
        },
        headers=auth_headers,
    )
    assert second.status_code == 201, second.text
    create_promotion(
        client,
        tenant,
        auth_headers,
        name="Dos por uno",
        promotion_type="buy_x_pay_y",
        discount_type=None,
        discount_value=None,
        receive_quantity=2,
        pay_quantity=1,
        target_scope="categories",
        target_ids=[category_id],
    )

    separated = create_counter_order(
        client,
        tenant,
        auth_headers,
        [
            {"product_id": tenant["product_id"], "quantity": 1},
            {"product_id": second.json()["id"], "quantity": 1},
        ],
        "promotion-no-product-mix",
    )
    assert separated["promotion_discount"] == 0

    personal_variant = client.post(
        f"/api/v1/catalog/products/{tenant['product_id']}/variants",
        json={"name": "Personal", "price_delta": 0},
        headers=auth_headers,
    )
    assert personal_variant.status_code == 201, personal_variant.text
    family_variant = client.post(
        f"/api/v1/catalog/products/{tenant['product_id']}/variants",
        json={"name": "Familiar", "price_delta": 10},
        headers=auth_headers,
    )
    assert family_variant.status_code == 201, family_variant.text

    variants = create_counter_order(
        client,
        tenant,
        auth_headers,
        [
            {"product_id": tenant["product_id"], "variant_name": "Personal", "quantity": 1},
            {"product_id": tenant["product_id"], "variant_name": "Familiar", "quantity": 1},
        ],
        "promotion-no-variant-mix",
    )
    assert variants["promotion_discount"] == 0

    eligible = create_counter_order(
        client,
        tenant,
        auth_headers,
        [{"product_id": tenant["product_id"], "variant_name": "Personal", "quantity": 2}],
        "promotion-same-product",
    )
    assert eligible["promotion_discount"] == 20
    assert eligible["total"] == 20


def test_public_and_integration_menus_expose_only_current_digital_promotions(
    client, tenant, auth_headers
):
    digital = create_promotion(
        client,
        tenant,
        auth_headers,
        name="Promo tienda",
        discount_type="fixed_amount",
        discount_value=4,
        service_channels=["digital_takeaway"],
    )
    create_promotion(
        client,
        tenant,
        auth_headers,
        name="Promo futura",
        starts_on=(date.today() + timedelta(days=1)).isoformat(),
        service_channels=["digital_takeaway"],
    )
    create_promotion(client, tenant, auth_headers, name="Solo mostrador")

    public_menu = client.get("/api/v1/public/test-restaurant/menu?branch_slug=main")
    assert public_menu.status_code == 200, public_menu.text
    assert [item["id"] for item in public_menu.json()["promotions"]] == [digital["id"]]

    credential = client.post(
        "/api/v1/admin/integration-credentials",
        json={
            "branch_id": tenant["branch_id"],
            "name": "Agente promociones",
            "scopes": ["menu:read"],
        },
        headers={**auth_headers, "X-Dev-Role": "superadmin"},
    )
    assert credential.status_code == 201, credential.text
    integration_menu = client.get(
        "/api/v1/integrations/context/menu",
        headers={"Authorization": f"Bearer {credential.json()['token']}"},
    )
    assert integration_menu.status_code == 200, integration_menu.text
    assert [item["id"] for item in integration_menu.json()["promotions"]] == [digital["id"]]

    public_order = client.post(
        "/api/v1/public/test-restaurant/orders",
        json={
            "branch_id": tenant["branch_id"],
            "fulfillment": "takeaway",
            "customer_name": "Cliente promo",
            "customer_phone": "999111222",
            "items": [{"product_id": tenant["product_id"], "quantity": 2}],
        },
        headers={"Idempotency-Key": "public-promotion-order"},
    )
    assert public_order.status_code == 201, public_order.text
    assert public_order.json()["promotion_discount"] == 8
    assert public_order.json()["total"] == 32


def test_promotion_lifecycle_and_tenant_isolation(client, tenant, auth_headers):
    promotion = create_promotion(client, tenant, auth_headers)
    other_headers = {
        "X-Dev-Auth": "test-token",
        "X-Dev-Role": "owner",
        "X-Dev-User": "other-owner",
        "X-Business-Id": str(tenant["other_business_id"]),
        "X-Branch-Id": str(tenant["other_branch_id"]),
    }
    denied = client.patch(
        f"/api/v1/catalog/promotions/{promotion['id']}",
        json={"name": "No autorizado", "expected_version": promotion["version"]},
        headers=other_headers,
    )
    assert denied.status_code == 403

    archived = client.post(
        f"/api/v1/catalog/promotions/{promotion['id']}/archive",
        headers=auth_headers,
    )
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived_at"] is not None
    assert archived.json()["active"] is False
    assert client.get(
        "/api/v1/catalog/promotions",
        params={"branch_id": tenant["branch_id"]},
        headers=auth_headers,
    ).json() == []

    restored = client.post(
        f"/api/v1/catalog/promotions/{promotion['id']}/restore",
        headers=auth_headers,
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["archived_at"] is None
    assert restored.json()["active"] is False
