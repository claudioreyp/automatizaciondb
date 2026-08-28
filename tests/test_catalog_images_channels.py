from io import BytesIO

from PIL import Image

from app.database import SessionLocal
from app.models import Category, Product


def image_bytes(image_format: str = "PNG") -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (96, 96), color=(38, 165, 111)).save(buffer, format=image_format)
    return buffer.getvalue()


def test_product_channels_and_private_image_lifecycle(client, tenant, auth_headers):
    product_id = tenant["product_id"]
    updated = client.patch(
        f"/api/v1/catalog/products/{product_id}",
        json={"service_channels": ["pos_counter", "digital_takeaway"]},
        headers=auth_headers,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["service_channels"] == ["pos_counter", "digital_takeaway"]

    uploaded = client.post(
        f"/api/v1/catalog/products/{product_id}/image",
        files={"file": ("pizza.png", image_bytes(), "image/png")},
        headers=auth_headers,
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["image_url"].startswith(
        f"/api/v1/public/catalog/products/{product_id}/image"
    )

    public_image = client.get(uploaded.json()["image_url"])
    assert public_image.status_code == 200
    assert public_image.headers["content-type"] == "image/png"
    assert public_image.content == image_bytes()

    local_only = client.patch(
        f"/api/v1/catalog/products/{product_id}",
        json={"service_channels": ["pos_counter"]},
        headers=auth_headers,
    )
    assert local_only.status_code == 200, local_only.text
    assert client.get(uploaded.json()["image_url"]).status_code == 404

    paused = client.patch(
        f"/api/v1/catalog/products/{product_id}",
        json={"service_channels": ["pos_counter", "digital_takeaway"], "available": False},
        headers=auth_headers,
    )
    assert paused.status_code == 200, paused.text
    assert client.get(uploaded.json()["image_url"]).status_code == 404

    restored = client.patch(
        f"/api/v1/catalog/products/{product_id}",
        json={"available": True},
        headers=auth_headers,
    )
    assert restored.status_code == 200, restored.text
    assert client.get(uploaded.json()["image_url"]).status_code == 200

    with SessionLocal.begin() as db:
        product = db.get(Product, product_id)
        category = db.get(Category, product.category_id)
        category_id = category.id
        category.active = False
    assert client.get(uploaded.json()["image_url"]).status_code == 404
    with SessionLocal.begin() as db:
        db.get(Category, category_id).active = True

    removed = client.delete(
        f"/api/v1/catalog/products/{product_id}/image",
        headers=auth_headers,
    )
    assert removed.status_code == 204, removed.text
    assert client.get(uploaded.json()["image_url"]).status_code == 404


def test_product_image_rejects_fake_and_cross_tenant_uploads(client, tenant, auth_headers):
    product_id = tenant["product_id"]
    fake = client.post(
        f"/api/v1/catalog/products/{product_id}/image",
        files={"file": ("fake.png", b"not an image", "image/png")},
        headers=auth_headers,
    )
    assert fake.status_code == 422

    other_headers = {
        **auth_headers,
        "X-Dev-Role": "owner",
        "X-Dev-User": "other-owner",
        "X-Business-Id": str(tenant["other_business_id"]),
        "X-Branch-Id": str(tenant["other_branch_id"]),
    }
    hidden = client.post(
        f"/api/v1/catalog/products/{product_id}/image",
        files={"file": ("pizza.png", image_bytes(), "image/png")},
        headers=other_headers,
    )
    assert hidden.status_code == 404
    assert hidden.json()["code"] == "CATALOG_RESOURCE_NOT_FOUND"

    hidden_delete = client.delete(
        f"/api/v1/catalog/products/{product_id}/image",
        headers=other_headers,
    )
    assert hidden_delete.status_code == 404
    assert hidden_delete.json()["code"] == "CATALOG_RESOURCE_NOT_FOUND"
