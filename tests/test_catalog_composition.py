from app.api import serialize_catalog
from app.database import SessionLocal
from app.models import AuditEvent, Branch, ModifierGroup


def test_catalog_composition_and_order_validation(client, tenant, auth_headers):
    variant = client.post(
        f"/api/v1/catalog/products/{tenant['product_id']}/variants",
        json={"name": "Familiar", "price_delta": 5},
        headers=auth_headers,
    )
    assert variant.status_code == 201, variant.text

    group = client.post(
        "/api/v1/catalog/modifier-groups",
        json={
            "branch_id": tenant["branch_id"],
            "name": "Tipo de masa",
            "minimum": 1,
            "maximum": 1,
            "required": True,
        },
        headers=auth_headers,
    )
    assert group.status_code == 201, group.text
    modifier = client.post(
        f"/api/v1/catalog/modifier-groups/{group.json()['id']}/modifiers",
        json={"name": "Masa artesanal", "price_delta": 2},
        headers=auth_headers,
    )
    assert modifier.status_code == 201, modifier.text
    linked = client.put(
        f"/api/v1/catalog/products/{tenant['product_id']}/modifier-groups",
        json={"group_ids": [group.json()["id"]]},
        headers=auth_headers,
    )
    assert linked.status_code == 200, linked.text

    ingredient = client.post(
        "/api/v1/catalog/ingredients",
        json={
            "branch_id": tenant["branch_id"],
            "sku": "MOZZARELLA",
            "name": "Queso mozzarella",
            "unit": "g",
        },
        headers=auth_headers,
    )
    assert ingredient.status_code == 201, ingredient.text
    recipe = client.put(
        f"/api/v1/catalog/products/{tenant['product_id']}/recipe",
        json={"components": [{"inventory_item_id": ingredient.json()["id"], "quantity": 180}]},
        headers=auth_headers,
    )
    assert recipe.status_code == 200, recipe.text

    combo = client.post(
        "/api/v1/catalog/products",
        json={
            "branch_id": tenant["branch_id"],
            "sku": "COMBO-PIZZA",
            "name": "Combo Pizza",
            "price": 30,
            "product_type": "combo",
        },
        headers=auth_headers,
    )
    assert combo.status_code == 201, combo.text
    combo_configured = client.put(
        f"/api/v1/catalog/products/{combo.json()['id']}/combo",
        json={"components": [{"product_id": tenant["product_id"], "quantity": 1}]},
        headers=auth_headers,
    )
    assert combo_configured.status_code == 200, combo_configured.text

    catalog = client.get(
        f"/api/v1/catalog?branch_id={tenant['branch_id']}",
        headers=auth_headers,
    )
    assert catalog.status_code == 200, catalog.text
    pizza = next(item for item in catalog.json()["products"] if item["id"] == tenant["product_id"])
    assert pizza["variants"][0]["name"] == "Familiar"
    assert pizza["modifier_groups"][0]["modifiers"][0]["name"] == "Masa artesanal"
    assert pizza["recipe"][0]["name"] == "Queso mozzarella"
    configured_combo = next(item for item in catalog.json()["products"] if item["id"] == combo.json()["id"])
    assert configured_combo["product_type"] == "combo"
    assert configured_combo["combo_components"][0]["product_id"] == tenant["product_id"]

    missing_variant = client.post(
        "/api/v1/orders",
        json={
            "branch_id": tenant["branch_id"],
            "channel": "counter",
            "items": [{"product_id": tenant["product_id"], "quantity": 1}],
        },
        headers={**auth_headers, "Idempotency-Key": "catalog-missing-variant"},
    )
    assert missing_variant.status_code == 422

    missing_required_extra = client.post(
        "/api/v1/orders",
        json={
            "branch_id": tenant["branch_id"],
            "channel": "counter",
            "items": [
                {
                    "product_id": tenant["product_id"],
                    "variant_name": "Familiar",
                    "quantity": 1,
                }
            ],
        },
        headers={**auth_headers, "Idempotency-Key": "catalog-missing-modifier"},
    )
    assert missing_required_extra.status_code == 422

    valid_order = client.post(
        "/api/v1/orders",
        json={
            "branch_id": tenant["branch_id"],
            "channel": "counter",
            "items": [
                {
                    "product_id": tenant["product_id"],
                    "variant_name": "Familiar",
                    "quantity": 1,
                    "modifiers": [
                        {
                            "modifier_id": modifier.json()["id"],
                            "name": "Masa artesanal",
                        }
                    ],
                }
            ],
        },
        headers={**auth_headers, "Idempotency-Key": "catalog-valid-order"},
    )
    assert valid_order.status_code == 201, valid_order.text
    assert valid_order.json()["total"] == 27.0


def test_catalog_groups_cannot_cross_branches(client, tenant, auth_headers):
    other_headers = {
        **auth_headers,
        "X-Dev-Role": "superadmin",
        "X-Dev-User": "catalog-superadmin",
        "X-Business-Id": str(tenant["other_business_id"]),
        "X-Branch-Id": str(tenant["other_branch_id"]),
    }
    group = client.post(
        "/api/v1/catalog/modifier-groups",
        json={"branch_id": tenant["other_branch_id"], "name": "Ajeno"},
        headers=other_headers,
    )
    assert group.status_code == 201, group.text

    linked = client.put(
        f"/api/v1/catalog/products/{tenant['product_id']}/modifier-groups",
        json={"group_ids": [group.json()["id"]]},
        headers=auth_headers,
    )
    assert linked.status_code == 422

    with SessionLocal.begin() as db:
        legacy_group = ModifierGroup(
            business_id=tenant["business_id"],
            branch_id=None,
            name="Personalización heredada sin sucursal",
        )
        db.add(legacy_group)
        db.flush()
        legacy_group_id = legacy_group.id

    catalog = client.get(
        f"/api/v1/catalog?branch_id={tenant['branch_id']}",
        headers=auth_headers,
    )
    assert catalog.status_code == 200, catalog.text
    assert legacy_group_id not in {item["id"] for item in catalog.json()["modifier_groups"]}

    linked_legacy = client.put(
        f"/api/v1/catalog/products/{tenant['product_id']}/modifier-groups",
        json={"group_ids": [legacy_group_id]},
        headers=auth_headers,
    )
    assert linked_legacy.status_code == 422


def test_categories_can_be_created_and_reordered_atomically(client, tenant, auth_headers):
    catalog = client.get(
        f"/api/v1/catalog?branch_id={tenant['branch_id']}",
        headers=auth_headers,
    )
    assert catalog.status_code == 200, catalog.text
    pizzas_id = catalog.json()["categories"][0]["id"]

    bebidas = client.post(
        "/api/v1/catalog/categories",
        json={"branch_id": tenant["branch_id"], "name": "Bebidas", "sort_order": 1},
        headers=auth_headers,
    )
    postres = client.post(
        "/api/v1/catalog/categories",
        json={"branch_id": tenant["branch_id"], "name": "Postres", "sort_order": 2},
        headers=auth_headers,
    )
    assert bebidas.status_code == 201, bebidas.text
    assert postres.status_code == 201, postres.text

    reordered = client.put(
        "/api/v1/catalog/categories/order",
        json={
            "branch_id": tenant["branch_id"],
            "category_ids": [postres.json()["id"], pizzas_id, bebidas.json()["id"]],
        },
        headers=auth_headers,
    )
    assert reordered.status_code == 200, reordered.text

    refreshed = client.get(
        f"/api/v1/catalog?branch_id={tenant['branch_id']}",
        headers=auth_headers,
    )
    assert [category["name"] for category in refreshed.json()["categories"]] == [
        "Postres",
        "Pizzas",
        "Bebidas",
    ]

    duplicate = client.post(
        "/api/v1/catalog/categories",
        json={"branch_id": tenant["branch_id"], "name": " bebidas "},
        headers=auth_headers,
    )
    assert duplicate.status_code == 409


def test_category_delete_cannot_leave_the_restaurant_without_visible_products(
    client,
    tenant,
    auth_headers,
):
    catalog = client.get(
        f"/api/v1/catalog?branch_id={tenant['branch_id']}",
        headers=auth_headers,
    )
    category_id = catalog.json()["categories"][0]["id"]

    blocked = client.patch(
        f"/api/v1/catalog/categories/{category_id}",
        json={"active": False},
        headers=auth_headers,
    )
    assert blocked.status_code == 409, blocked.text
    assert "últimos productos" in blocked.json()["detail"]

    empty_category = client.post(
        "/api/v1/catalog/categories",
        json={"branch_id": tenant["branch_id"], "name": "Vacía"},
        headers=auth_headers,
    )
    assert empty_category.status_code == 201, empty_category.text
    deleted_empty = client.patch(
        f"/api/v1/catalog/categories/{empty_category.json()['id']}",
        json={"active": False},
        headers=auth_headers,
    )
    assert deleted_empty.status_code == 200, deleted_empty.text

    second_category = client.post(
        "/api/v1/catalog/categories",
        json={"branch_id": tenant["branch_id"], "name": "Bebidas"},
        headers=auth_headers,
    )
    assert second_category.status_code == 201, second_category.text
    second_product = client.post(
        "/api/v1/catalog/products",
        json={
            "branch_id": tenant["branch_id"],
            "category_id": second_category.json()["id"],
            "sku": "BEBIDA-DELETE-GUARD",
            "name": "Bebida disponible",
            "price": 5,
        },
        headers=auth_headers,
    )
    assert second_product.status_code == 201, second_product.text

    deleted_with_fallback = client.patch(
        f"/api/v1/catalog/categories/{category_id}",
        json={"active": False},
        headers=auth_headers,
    )
    assert deleted_with_fallback.status_code == 200, deleted_with_fallback.text

    with SessionLocal() as db:
        branch = db.get(Branch, tenant["branch_id"])
        available_catalog = serialize_catalog(db, branch, available_only=True)
    available_product_names = {
        product["name"] for product in available_catalog["products"]
    }
    assert "Pizza" not in available_product_names
    assert "Bebida disponible" in available_product_names


def test_products_and_modifier_groups_can_be_reordered_atomically(client, tenant, auth_headers):
    catalog = client.get(
        f"/api/v1/catalog?branch_id={tenant['branch_id']}",
        headers=auth_headers,
    )
    assert catalog.status_code == 200, catalog.text
    category_id = catalog.json()["categories"][0]["id"]

    second_product = client.post(
        "/api/v1/catalog/products",
        json={
            "branch_id": tenant["branch_id"],
            "category_id": category_id,
            "sku": "PIZZA-SECOND",
            "name": "Segunda pizza",
            "price": 24,
            "sort_order": 1,
        },
        headers=auth_headers,
    )
    assert second_product.status_code == 201, second_product.text
    product_order = [second_product.json()["id"], tenant["product_id"]]
    reordered_products = client.put(
        "/api/v1/catalog/products/order",
        json={
            "branch_id": tenant["branch_id"],
            "category_id": category_id,
            "product_ids": product_order,
        },
        headers=auth_headers,
    )
    assert reordered_products.status_code == 200, reordered_products.text

    first_group = client.post(
        "/api/v1/catalog/modifier-groups",
        json={
            "branch_id": tenant["branch_id"],
            "name": "Primera personalización",
            "modifiers": [{"name": "Opción uno", "price_delta": 0}],
        },
        headers=auth_headers,
    )
    second_group = client.post(
        "/api/v1/catalog/modifier-groups",
        json={
            "branch_id": tenant["branch_id"],
            "name": "Segunda personalización",
            "sort_order": 1,
            "modifiers": [{"name": "Opción dos", "price_delta": 1}],
        },
        headers=auth_headers,
    )
    assert first_group.status_code == 201, first_group.text
    assert second_group.status_code == 201, second_group.text
    group_order = [second_group.json()["id"], first_group.json()["id"]]
    reordered_groups = client.put(
        "/api/v1/catalog/modifier-groups/order",
        json={"branch_id": tenant["branch_id"], "group_ids": group_order},
        headers=auth_headers,
    )
    assert reordered_groups.status_code == 200, reordered_groups.text

    refreshed = client.get(
        f"/api/v1/catalog?branch_id={tenant['branch_id']}",
        headers=auth_headers,
    )
    assert refreshed.status_code == 200, refreshed.text
    category_products = [
        product["id"]
        for product in refreshed.json()["products"]
        if product["category_id"] == category_id
    ]
    assert category_products == product_order
    assert [group["id"] for group in refreshed.json()["modifier_groups"]] == group_order

    incomplete = client.put(
        "/api/v1/catalog/products/order",
        json={
            "branch_id": tenant["branch_id"],
            "category_id": category_id,
            "product_ids": [tenant["product_id"]],
        },
        headers=auth_headers,
    )
    assert incomplete.status_code == 422


def test_public_menu_respects_category_order_and_archived_categories(client, tenant, auth_headers):
    catalog = client.get(
        f"/api/v1/catalog?branch_id={tenant['branch_id']}",
        headers=auth_headers,
    )
    assert catalog.status_code == 200, catalog.text
    pizzas_id = catalog.json()["categories"][0]["id"]
    moved_pizzas = client.patch(
        f"/api/v1/catalog/categories/{pizzas_id}",
        json={"sort_order": 2},
        headers=auth_headers,
    )
    assert moved_pizzas.status_code == 200, moved_pizzas.text

    postres = client.post(
        "/api/v1/catalog/categories",
        json={"branch_id": tenant["branch_id"], "name": "Postres", "sort_order": 0},
        headers=auth_headers,
    )
    bebidas = client.post(
        "/api/v1/catalog/categories",
        json={"branch_id": tenant["branch_id"], "name": "Bebidas", "sort_order": 1},
        headers=auth_headers,
    )
    assert postres.status_code == 201, postres.text
    assert bebidas.status_code == 201, bebidas.text

    dessert = client.post(
        "/api/v1/catalog/products",
        json={
            "branch_id": tenant["branch_id"],
            "category_id": postres.json()["id"],
            "sku": "POSTRE-PUBLIC",
            "name": "Postre público",
            "price": 12,
        },
        headers=auth_headers,
    )
    hidden_drink = client.post(
        "/api/v1/catalog/products",
        json={
            "branch_id": tenant["branch_id"],
            "category_id": bebidas.json()["id"],
            "sku": "BEBIDA-HIDDEN",
            "name": "Bebida archivada",
            "price": 6,
        },
        headers=auth_headers,
    )
    assert dessert.status_code == 201, dessert.text
    assert hidden_drink.status_code == 201, hidden_drink.text

    archived = client.patch(
        f"/api/v1/catalog/categories/{bebidas.json()['id']}",
        json={"active": False},
        headers=auth_headers,
    )
    assert archived.status_code == 200, archived.text

    public_menu = client.get("/api/v1/public/test-restaurant/menu?branch_slug=main")
    assert public_menu.status_code == 200, public_menu.text
    assert [category["name"] for category in public_menu.json()["categories"]] == [
        "Postres",
        "Pizzas",
    ]
    public_product_names = [product["name"] for product in public_menu.json()["products"]]
    assert "Postre público" in public_product_names
    assert "Bebida archivada" not in public_product_names


def test_modifier_group_editor_saves_group_and_options_together(client, tenant, auth_headers):
    created = client.post(
        "/api/v1/catalog/modifier-groups",
        json={
            "branch_id": tenant["branch_id"],
            "name": "Salsas",
            "internal_label": "Salsas delivery",
            "minimum": 1,
            "maximum": 3,
            "required": True,
            "allow_repeats": True,
            "sort_order": 2,
            "modifiers": [
                {"name": "BBQ", "price_delta": 0, "sort_order": 0},
                {"name": "Ajo", "price_delta": 1.5, "sort_order": 1},
            ],
        },
        headers=auth_headers,
    )
    assert created.status_code == 201, created.text
    group = created.json()
    assert group["internal_label"] == "Salsas delivery"
    assert group["allow_repeats"] is True
    assert [modifier["name"] for modifier in group["modifiers"]] == ["BBQ", "Ajo"]

    ajo = group["modifiers"][1]
    updated = client.patch(
        f"/api/v1/catalog/modifier-groups/{group['id']}",
        json={
            "name": "Salsas para pizza",
            "internal_label": "Cocina: salsas",
            "minimum": 0,
            "maximum": 2,
            "required": False,
            "allow_repeats": False,
            "modifiers": [
                {
                    "id": ajo["id"],
                    "name": "Alioli",
                    "price_delta": 2,
                    "sort_order": 0,
                },
                {"name": "Tártara", "price_delta": 1, "sort_order": 1},
            ],
        },
        headers=auth_headers,
    )
    assert updated.status_code == 200, updated.text
    active_options = [option for option in updated.json()["modifiers"] if option["active"]]
    assert [option["name"] for option in active_options] == ["Alioli", "Tártara"]
    assert next(option for option in updated.json()["modifiers"] if option["name"] == "BBQ")["active"] is False

    invalid = client.patch(
        f"/api/v1/catalog/modifier-groups/{group['id']}",
        json={
            "modifiers": [
                {"name": "Duplicada", "price_delta": 0},
                {"name": " duplicada ", "price_delta": 1},
            ]
        },
        headers=auth_headers,
    )
    assert invalid.status_code == 422

    refreshed = client.get(
        f"/api/v1/catalog?branch_id={tenant['branch_id']}",
        headers=auth_headers,
    )
    saved_group = next(item for item in refreshed.json()["modifier_groups"] if item["id"] == group["id"])
    assert saved_group["name"] == "Salsas para pizza"
    assert saved_group["internal_label"] == "Cocina: salsas"
    assert [option["name"] for option in saved_group["modifiers"] if option["active"]] == [
        "Alioli",
        "Tártara",
    ]


def test_modifier_group_editor_rejects_blank_and_impossible_configurations(
    client,
    tenant,
    auth_headers,
):
    blank_group = client.post(
        "/api/v1/catalog/modifier-groups",
        json={"branch_id": tenant["branch_id"], "name": "   "},
        headers=auth_headers,
    )
    assert blank_group.status_code == 422

    blank_option = client.post(
        "/api/v1/catalog/modifier-groups",
        json={
            "branch_id": tenant["branch_id"],
            "name": "Cremas",
            "modifiers": [{"name": "   ", "price_delta": 0}],
        },
        headers=auth_headers,
    )
    assert blank_option.status_code == 422

    impossible_minimum = client.post(
        "/api/v1/catalog/modifier-groups",
        json={
            "branch_id": tenant["branch_id"],
            "name": "Tamaños",
            "minimum": 3,
            "maximum": 3,
            "allow_repeats": False,
            "modifiers": [
                {"name": "Mediana", "price_delta": 0},
                {"name": "Familiar", "price_delta": 12},
            ],
        },
        headers=auth_headers,
    )
    assert impossible_minimum.status_code == 422

    repeated = client.post(
        "/api/v1/catalog/modifier-groups",
        json={
            "branch_id": tenant["branch_id"],
            "name": "Porciones extra",
            "minimum": 0,
            "maximum": None,
            "allow_repeats": True,
            "max_per_option": None,
            "modifiers": [{"name": "Queso", "price_delta": 2}],
        },
        headers=auth_headers,
    )
    assert repeated.status_code == 201, repeated.text
    assert repeated.json()["maximum"] is None
    assert repeated.json()["max_per_option"] is None

    impossible_repeat_limit = client.post(
        "/api/v1/catalog/modifier-groups",
        json={
            "branch_id": tenant["branch_id"],
            "name": "Salsas repetibles",
            "minimum": 3,
            "maximum": None,
            "allow_repeats": True,
            "max_per_option": 2,
            "modifiers": [{"name": "Ají", "price_delta": 1}],
        },
        headers=auth_headers,
    )
    assert impossible_repeat_limit.status_code == 422


def test_orders_charge_allowed_repetitions_and_reject_invalid_quantities(
    client,
    tenant,
    auth_headers,
):
    group = client.post(
        "/api/v1/catalog/modifier-groups",
        json={
            "branch_id": tenant["branch_id"],
            "name": "Salsas repetibles",
            "minimum": 1,
            "maximum": 4,
            "required": True,
            "allow_repeats": True,
            "max_per_option": 3,
            "modifiers": [
                {"name": "BBQ", "price_delta": 2},
                {"name": "Ajo", "price_delta": 1},
            ],
        },
        headers=auth_headers,
    )
    assert group.status_code == 201, group.text
    bbq = next(item for item in group.json()["modifiers"] if item["name"] == "BBQ")
    ajo = next(item for item in group.json()["modifiers"] if item["name"] == "Ajo")
    linked = client.put(
        f"/api/v1/catalog/products/{tenant['product_id']}/modifier-groups",
        json={"group_ids": [group.json()["id"]]},
        headers=auth_headers,
    )
    assert linked.status_code == 200, linked.text
    modifier_names = {bbq["id"]: bbq["name"], ajo["id"]: ajo["name"]}

    def order_with(modifier_ids, key):
        return client.post(
            "/api/v1/orders",
            json={
                "branch_id": tenant["branch_id"],
                "channel": "counter",
                "items": [{
                    "product_id": tenant["product_id"],
                    "quantity": 1,
                    "modifiers": [
                        {
                            "modifier_id": modifier_id,
                            "name": modifier_names[modifier_id],
                        }
                        for modifier_id in modifier_ids
                    ],
                }],
            },
            headers={**auth_headers, "Idempotency-Key": key},
        )

    repeated = order_with([bbq["id"], bbq["id"], bbq["id"]], "repeat-valid")
    assert repeated.status_code == 201, repeated.text
    assert repeated.json()["total"] == 26.0
    assert [item["modifier_id"] for item in repeated.json()["items"][0]["modifiers"]] == [
        bbq["id"],
        bbq["id"],
        bbq["id"],
    ]

    over_per_option = order_with([bbq["id"]] * 4, "repeat-over-option")
    assert over_per_option.status_code == 422

    over_total = order_with([bbq["id"]] * 3 + [ajo["id"]] * 2, "repeat-over-total")
    assert over_total.status_code == 422

    disabled = client.patch(
        f"/api/v1/catalog/modifier-groups/{group.json()['id']}",
        json={"allow_repeats": False},
        headers=auth_headers,
    )
    assert disabled.status_code == 200, disabled.text
    duplicate = order_with([bbq["id"], bbq["id"]], "repeat-disabled")
    assert duplicate.status_code == 422


def test_modifier_group_delete_is_scoped_audited_and_preserves_order_snapshots(
    client,
    tenant,
    auth_headers,
):
    group = client.post(
        "/api/v1/catalog/modifier-groups",
        json={
            "branch_id": tenant["branch_id"],
            "name": "Salsas para borrar",
            "internal_label": "máximo 1",
            "minimum": 0,
            "maximum": 1,
            "required": False,
            "modifiers": [{"name": "BBQ", "price_delta": 2}],
        },
        headers=auth_headers,
    )
    assert group.status_code == 201, group.text
    modifier = group.json()["modifiers"][0]
    linked = client.put(
        f"/api/v1/catalog/products/{tenant['product_id']}/modifier-groups",
        json={"group_ids": [group.json()["id"]]},
        headers=auth_headers,
    )
    assert linked.status_code == 200, linked.text

    order = client.post(
        "/api/v1/orders",
        json={
            "branch_id": tenant["branch_id"],
            "channel": "counter",
            "items": [{
                "product_id": tenant["product_id"],
                "quantity": 1,
                "modifiers": [{"modifier_id": modifier["id"], "name": modifier["name"]}],
            }],
        },
        headers={**auth_headers, "Idempotency-Key": "delete-group-snapshot"},
    )
    assert order.status_code == 201, order.text

    foreign_headers = {
        **auth_headers,
        "X-Dev-User": "other-owner",
        "X-Business-Id": str(tenant["other_business_id"]),
        "X-Branch-Id": str(tenant["other_branch_id"]),
    }
    forbidden = client.delete(
        f"/api/v1/catalog/modifier-groups/{group.json()['id']}",
        headers=foreign_headers,
    )
    assert forbidden.status_code == 403

    deleted = client.delete(
        f"/api/v1/catalog/modifier-groups/{group.json()['id']}",
        headers=auth_headers,
    )
    assert deleted.status_code == 204, deleted.text

    catalog = client.get(
        f"/api/v1/catalog?branch_id={tenant['branch_id']}",
        headers=auth_headers,
    )
    assert catalog.status_code == 200, catalog.text
    assert group.json()["id"] not in {item["id"] for item in catalog.json()["modifier_groups"]}
    product = next(item for item in catalog.json()["products"] if item["id"] == tenant["product_id"])
    assert product["modifier_groups"] == []

    historical_order = client.get(
        f"/api/v1/orders/{order.json()['id']}",
        headers=auth_headers,
    )
    assert historical_order.status_code == 200, historical_order.text
    assert historical_order.json()["items"][0]["modifiers"] == order.json()["items"][0]["modifiers"]

    with SessionLocal() as db:
        event = db.query(AuditEvent).filter_by(
            action="modifier_group.deleted",
            entity_id=str(group.json()["id"]),
        ).one()
        assert event.business_id == tenant["business_id"]
        assert event.branch_id == tenant["branch_id"]
        assert event.payload["detached_product_count"] == 1
        assert event.payload["detached_products"] == [{
            "id": tenant["product_id"],
            "name": "Pizza",
        }]
        assert event.payload["deleted_modifier_ids"] == [modifier["id"]]
