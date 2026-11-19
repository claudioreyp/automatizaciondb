"""Private PDF delivery is opt-in; existing menu image consumers stay unchanged."""
from io import BytesIO
from urllib.parse import unquote

import httpx
import pytest
from PIL import Image
from pypdf import PdfWriter
from sqlalchemy import func, select

from app import api as pos_api
from app.auth import hash_integration_token
from app.database import SessionLocal
from app.models import AuditEvent, Branch, IdempotencyRecord, IntegrationCredential


CONTEXT = "/api/v1/integrations/context"
MENU = CONTEXT + "/menu-card"
PDF_PATH = "isolated/private-menu.pdf"
IMAGE_PATH = "isolated/original-menu.webp"


@pytest.fixture
def menu_media(monkeypatch, tenant):
    document = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=400)
    writer.write(document)
    image = BytesIO()
    Image.new("RGB", (9, 7), "green").save(image, "WEBP", lossless=True)
    storage = {
        "pdf": document.getvalue(), "image": image.getvalue(), "reads": [], "error": None,
    }
    with SessionLocal.begin() as db:
        branch = db.get(Branch, tenant["branch_id"])
        branch.agent_menu_pdf = {"path": PDF_PATH, "filename": "Carta Perú.pdf", "size_bytes": len(storage["pdf"])}
        branch.menu_card_storage_path = IMAGE_PATH

    async def load_private_file(path):
        storage["reads"].append(path)
        if path == PDF_PATH:
            if storage["error"]:
                raise storage["error"]
            # Saved PDF metadata, not an untrusted storage MIME, determines the download type.
            return storage["pdf"], "application/octet-stream"
        assert path == IMAGE_PATH
        return storage["image"], "image/webp"

    monkeypatch.setattr(pos_api, "load_private_file", load_private_file)
    return storage


@pytest.fixture
def menu_credential(tenant):
    def create(*, scopes=("menu:read",), profile="restaurant_agent"):
        token = "isolated-private-menu.token"
        with SessionLocal.begin() as db:
            db.add(IntegrationCredential(
                business_id=tenant["business_id"], branch_id=tenant["branch_id"],
                name="Isolated PDF menu delivery", profile=profile,
                token_prefix=token.split(".", 1)[0], token_hash=hash_integration_token(token),
                scopes=list(scopes),
            ))
        return {"Authorization": f"Bearer {token}"}
    return create


def test_context_exposes_only_scoped_pdf_metadata_without_loading_document(client, menu_credential, menu_media):
    response = client.get(CONTEXT, headers=menu_credential())
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    branch = response.json()["branch"]
    assert branch["menu_pdf"] == {
        "filename": "Carta Perú.pdf", "size_bytes": len(menu_media["pdf"]),
        "url": MENU + "?prefer_pdf=true",
    }
    assert branch["menu_card_configured"] is True
    assert len(branch["menu_images"]) == 1
    assert PDF_PATH not in response.text and IMAGE_PATH not in response.text
    assert menu_media["reads"] == []


@pytest.mark.parametrize("image_configured", [False, True])
def test_opt_in_returns_original_pdf_before_images_even_for_cloud_format(
    client, tenant, menu_credential, menu_media, image_configured,
):
    if not image_configured:
        with SessionLocal.begin() as db:
            db.get(Branch, tenant["branch_id"]).menu_card_storage_path = None
    response = client.get(MENU, headers=menu_credential(), params={"prefer_pdf": True, "format": "whatsapp_cloud"})
    assert response.status_code == 200, response.text
    assert response.content == menu_media["pdf"]
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="carta.pdf"; filename*=UTF-8\'\'')
    assert unquote(disposition.split("UTF-8''", 1)[1]) == "Carta Perú.pdf"
    assert menu_media["reads"] == [PDF_PATH]


@pytest.mark.parametrize("params", [{}, {"prefer_pdf": False}])
def test_default_still_delivers_primary_image_when_pdf_exists(client, menu_credential, menu_media, params):
    response = client.get(MENU, headers=menu_credential(), params=params)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/png"
    with Image.open(BytesIO(response.content)) as image:
        assert image.size == (9, 7)
    assert menu_media["reads"] == [IMAGE_PATH]


@pytest.mark.parametrize("image_configured", [False, True])
def test_missing_pdf_metadata_falls_back_to_existing_image_behavior(
    client, tenant, menu_credential, menu_media, image_configured,
):
    with SessionLocal.begin() as db:
        branch = db.get(Branch, tenant["branch_id"])
        branch.agent_menu_pdf = None
        if not image_configured:
            branch.menu_card_storage_path = None
    headers = menu_credential()
    assert client.get(CONTEXT, headers=headers).json()["branch"]["menu_pdf"] is None
    response = client.get(MENU, headers=headers, params={"prefer_pdf": True})
    assert response.status_code == (200 if image_configured else 404), response.text
    if image_configured:
        assert response.headers["content-type"] == "image/png"
    assert menu_media["reads"] == ([IMAGE_PATH] if image_configured else [])


@pytest.mark.parametrize("error", [FileNotFoundError("synthetic missing PDF"), httpx.HTTPError("synthetic storage error")])
def test_configured_pdf_storage_failure_is_explicit_without_sending_wrong_image(
    client, menu_credential, menu_media, error,
):
    menu_media["error"] = error
    response = client.get(MENU, headers=menu_credential(), params={"prefer_pdf": True})
    assert response.status_code == 404
    assert "PDF" in response.json()["detail"]
    assert menu_media["reads"] == [PDF_PATH]


@pytest.mark.parametrize("target", [CONTEXT, MENU + "?prefer_pdf=true"])
def test_pdf_metadata_and_download_keep_authentication_and_menu_scope(client, menu_credential, menu_media, target):
    assert client.get(target).status_code == 401
    assert client.get(target, headers=menu_credential(scopes=("orders:read",))).status_code == 403
    assert menu_media["reads"] == []


@pytest.mark.parametrize("target", [CONTEXT, MENU + "?prefer_pdf=true"])
@pytest.mark.parametrize("same_business", [False, True])
def test_pdf_access_cannot_cross_restaurant_or_sibling_branch(
    client, tenant, menu_credential, menu_media, target, same_business,
):
    with SessionLocal.begin() as db:
        if same_business:
            other = Branch(business_id=tenant["business_id"], name="Sibling", slug="sibling")
            db.add(other)
            db.flush()
            other_id = other.id
        else:
            other_id = tenant["other_branch_id"]
    response = client.get(target, headers=menu_credential(), params={"branch_id": other_id})
    assert response.status_code == 403, response.text
    assert menu_media["reads"] == []


def test_pdf_read_keeps_domain_records_and_private_file_unchanged(client, tenant, menu_credential, menu_media):
    with SessionLocal() as db:
        branch = db.get(Branch, tenant["branch_id"])
        before = (branch.version, branch.agent_menu_pdf, branch.menu_card_storage_path, branch.agent_menu_images)
        audit_count = db.scalar(select(func.count()).select_from(AuditEvent))
        receipt_count = db.scalar(select(func.count()).select_from(IdempotencyRecord))
    original = menu_media["pdf"]
    headers = menu_credential()
    metadata = client.get(CONTEXT, headers=headers).json()["branch"]["menu_pdf"]
    assert client.get(metadata["url"], headers=headers).content == original
    with SessionLocal() as db:
        branch = db.get(Branch, tenant["branch_id"])
        assert (branch.version, branch.agent_menu_pdf, branch.menu_card_storage_path, branch.agent_menu_images) == before
        assert db.scalar(select(func.count()).select_from(AuditEvent)) == audit_count
        assert db.scalar(select(func.count()).select_from(IdempotencyRecord)) == receipt_count
    assert menu_media["pdf"] == original
