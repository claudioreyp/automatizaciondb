"""Menu transport compatibility through isolated ASGI calls and in-memory storage."""
from io import BytesIO

import pytest
from PIL import Image

from app import api as pos_api
from app.auth import hash_integration_token
from app.database import SessionLocal
from app.models import Branch, IntegrationCredential


MENU = "/api/v1/integrations/context/menu-card"
STORAGE_PATH = "isolated/menu-original.webp"


def image_bytes(image_format):
    image = Image.new("RGB", (9, 7))
    image.putdata([(x * 23, y * 31, (x + y) * 13) for y in range(7) for x in range(9)])
    output = BytesIO()
    options = {"lossless": True} if image_format == "WEBP" else {}
    image.save(output, format=image_format, **options)
    return output.getvalue()


@pytest.fixture
def stored_menu(monkeypatch, tenant):
    storage = {"data": image_bytes("WEBP"), "mime": "image/webp", "reads": []}
    with SessionLocal.begin() as db:
        db.get(Branch, tenant["branch_id"]).menu_card_storage_path = STORAGE_PATH

    async def load_private_file(path):
        storage["reads"].append(path)
        assert path == STORAGE_PATH
        return storage["data"], storage["mime"]

    monkeypatch.setattr(pos_api, "load_private_file", load_private_file)
    return storage


@pytest.fixture
def credential(tenant):
    def create(profile="restaurant_agent", scopes=("menu:read",)):
        token = f"isolated-menu-{profile}.token"
        with SessionLocal.begin() as db:
            db.add(IntegrationCredential(
                business_id=tenant["business_id"], branch_id=tenant["branch_id"],
                name="Isolated menu delivery", profile=profile,
                token_prefix=token.split(".", 1)[0], token_hash=hash_integration_token(token),
                scopes=list(scopes),
            ))
        return {"Authorization": f"Bearer {token}"}
    return create


@pytest.mark.parametrize("declared_mime", ["image/webp", "application/octet-stream"])
def test_agent_default_get_converts_webp_losslessly_without_changing_storage(
    client, tenant, credential, stored_menu, declared_mime,
):
    stored_menu["mime"] = declared_mime
    original = stored_menu["data"]
    response = client.get(MENU, headers=credential())
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    with Image.open(BytesIO(original)) as source, Image.open(BytesIO(response.content)) as target:
        assert target.format == "PNG"
        assert target.size == source.size
        assert target.convert("RGBA").tobytes() == source.convert("RGBA").tobytes()
    assert stored_menu["data"] == original
    assert stored_menu["reads"] == [STORAGE_PATH]
    with SessionLocal() as db:
        assert db.get(Branch, tenant["branch_id"]).menu_card_storage_path == STORAGE_PATH


@pytest.mark.parametrize("image_format,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg")])
def test_agent_compatible_images_keep_exact_bytes(client, credential, stored_menu, image_format, mime):
    original = image_bytes(image_format)
    stored_menu.update(data=original, mime=mime)
    response = client.get(MENU, headers=credential())
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == mime
    assert response.content == original


@pytest.mark.parametrize("profile", ["generic", "legacy"])
def test_non_agent_default_keeps_original_webp(client, tenant, credential, stored_menu, profile):
    headers = credential("generic") if profile == "generic" else {
        "X-Integration-Token": "test-integration-token",
    }
    params = {"branch_id": tenant["branch_id"]} if profile == "legacy" else {}
    response = client.get(MENU, headers=headers, params=params)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/webp"
    assert response.content == stored_menu["data"]


def test_generic_explicit_whatsapp_format_still_returns_png(client, credential, stored_menu):
    response = client.get(MENU, headers=credential("generic"), params={"format": "whatsapp_cloud"})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/png"
    with Image.open(BytesIO(response.content)) as image:
        assert image.format == "PNG" and image.size == (9, 7)


def test_pos_menu_download_keeps_original_webp(client, tenant, auth_headers, stored_menu):
    response = client.get(f"/api/v1/branches/{tenant['branch_id']}/menu-card", headers=auth_headers)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/webp"
    assert response.content == stored_menu["data"]


def test_agent_without_menu_scope_cannot_read_storage(client, credential, stored_menu):
    response = client.get(MENU, headers=credential(scopes=("orders:read",)))
    assert response.status_code == 403, response.text
    assert stored_menu["reads"] == []


def test_agent_cannot_read_another_business_menu(client, tenant, credential, stored_menu):
    response = client.get(MENU, headers=credential(), params={"branch_id": tenant["other_branch_id"]})
    assert response.status_code == 403, response.text
    assert stored_menu["reads"] == []


def test_agent_invalid_stored_image_fails_safely_and_preserves_original(client, credential, stored_menu):
    stored_menu["data"] = b"not-an-image"
    response = client.get(MENU, headers=credential())
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "WHATSAPP_IMAGE_NOT_COMPATIBLE"
    assert stored_menu["data"] == b"not-an-image"
