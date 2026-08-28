from app import api as api_module
from app.database import SessionLocal
from app.models import Category, Product


def test_category_soft_delete_has_stable_codes_reuses_names_and_broadcasts(
    client,
    tenant,
    auth_headers,
    monkeypatch,
):
    broadcasts = []

    async def capture_broadcast(branch_id, event, payload):
        broadcasts.append((branch_id, event, payload))

    monkeypatch.setattr(api_module.hub, "broadcast", capture_broadcast)
    created = client.post(
        "/api/v1/catalog/categories",
        json={"branch_id": tenant["branch_id"], "name": "Temporal"},
        headers=auth_headers,
    )
    assert created.status_code == 201, created.text

    deleted = client.patch(
        f"/api/v1/catalog/categories/{created.json()['id']}",
        json={"active": False},
        headers=auth_headers,
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["active"] is False
    assert broadcasts == [
        (
            tenant["branch_id"],
            "catalog.category_updated",
            deleted.json(),
        )
    ]

    reused = client.post(
        "/api/v1/catalog/categories",
        json={"branch_id": tenant["branch_id"], "name": " temporal "},
        headers=auth_headers,
    )
    assert reused.status_code == 201, reused.text

def test_catalog_soft_delete_is_tenant_safe_and_last_products_has_code(
    client,
    tenant,
    auth_headers,
):
    with SessionLocal.begin() as db:
        foreign_category = Category(
            business_id=tenant["other_business_id"],
            branch_id=tenant["other_branch_id"],
            name="Privada",
        )
        db.add(foreign_category)
        db.flush()
        foreign_product = Product(
            business_id=tenant["other_business_id"],
            branch_id=tenant["other_branch_id"],
            category_id=foreign_category.id,
            sku="FOREIGN-PRIVATE",
            name="Producto privado",
            price=10,
        )
        db.add(foreign_product)
        db.flush()
        foreign_category_id = foreign_category.id
        foreign_product_id = foreign_product.id

    foreign_category_response = client.patch(
        f"/api/v1/catalog/categories/{foreign_category_id}",
        json={"active": False},
        headers=auth_headers,
    )
    assert foreign_category_response.status_code == 404
    assert foreign_category_response.json() == {
        "detail": "No encontramos esa categoría",
        "code": "CATALOG_RESOURCE_NOT_FOUND",
    }

    foreign_product_response = client.patch(
        f"/api/v1/catalog/products/{foreign_product_id}/availability",
        json={"available": False},
        headers=auth_headers,
    )
    assert foreign_product_response.status_code == 404
    assert foreign_product_response.json()["code"] == "CATALOG_RESOURCE_NOT_FOUND"

    own_category_id = client.get(
        f"/api/v1/catalog?branch_id={tenant['branch_id']}",
        headers=auth_headers,
    ).json()["categories"][0]["id"]
    blocked = client.patch(
        f"/api/v1/catalog/categories/{own_category_id}",
        json={"active": False},
        headers=auth_headers,
    )
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "CATEGORY_LAST_VISIBLE_PRODUCTS"
    assert "últimos productos" in blocked.json()["detail"]
