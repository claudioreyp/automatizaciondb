import io

import httpx
import pytest
from PIL import Image
from sqlalchemy import func, select

from app.config import get_settings
from app.database import SessionLocal
from app.models import Branch, BranchSettings, Business, IntegrationCredential
from app.settings_service import get_or_create_branch_settings
from test_escalar_integrations import create_credential, integration_headers

BASE = "/api/v1"


def keyed(headers, key):
    return {**headers, "Idempotency-Key": key}


def test_profile_payment_fields_compatibility_versions_and_permissions(client, tenant, auth_headers):
    path = f"{BASE}/settings/branches/{tenant['branch_id']}/agent"
    with SessionLocal.begin() as db:
        branch = db.get(Branch, tenant["branch_id"])
        branch.plin_number = "legacy-plin"
        branch.accepted_payment_methods = ["cash"]
    payload = {"expected_version": 1, "yape_number": "  999 888 777  ", "payment_recipient_name": "  Titular prueba  "}
    saved = client.patch(path, headers=keyed(auth_headers, "payment"), json=payload)
    assert saved.status_code == 200, saved.text
    assert saved.json()["yape_number"] == "999 888 777"
    assert saved.json()["payment_recipient_name"] == "Titular prueba"
    assert client.patch(path, headers=keyed(auth_headers, "payment"), json=payload).json() == saved.json()
    assert client.patch(path, headers=keyed(auth_headers, "payment"), json={**payload, "yape_number": "other"}).status_code == 409
    assert client.patch(path, headers=keyed(auth_headers, "stale"), json=payload).status_code == 409
    legacy = client.patch(path, headers=keyed(auth_headers, "name"), json={"expected_version": 2, "name": "Ana"})
    assert legacy.json()["yape_number"] == "999 888 777"
    for field, limit in (("yape_number", 40), ("payment_recipient_name", 180)):
        assert client.patch(path, headers=keyed(auth_headers, field), json={"expected_version": 3, field: "X" * (limit + 1)}).status_code == 422
    assert client.patch(path, headers=keyed({**auth_headers, "X-Dev-Role": "cashier"}, "denied"), json={"expected_version": 3, "yape_number": "x"}).status_code == 403
    cleared = client.patch(path, headers=keyed(auth_headers, "clear"), json={"expected_version": 3, "yape_number": " "})
    assert cleared.json()["yape_number"] is None
    assert cleared.json()["payment_recipient_name"] == "Titular prueba"
    with SessionLocal() as db:
        branch = db.get(Branch, tenant["branch_id"])
        assert branch.plin_number == "legacy-plin"
        assert branch.accepted_payment_methods == ["cash"]


@pytest.mark.parametrize("mode,fee,status", [("fixed", 8, "configured"), ("free", 0, "configured"),
    ("quote", None, "pending_quote"), ("distance", None, "destination_required"),
    ("bands", None, "destination_required"), ("neighborhoods", None, "destination_required")])
def test_context_uses_pos_policy_not_legacy_fee(client, tenant, auth_headers, mode, fee, status):
    credential = create_credential(client, tenant, auth_headers, ["menu:read"])
    with SessionLocal.begin() as db:
        branch = db.get(Branch, tenant["branch_id"])
        branch.delivery_fee = 999
        branch.latitude, branch.longitude = -4.5, -80.5
        branch.address = "Direccion del POS"
        settings = get_or_create_branch_settings(db, branch)
        settings.delivery_mode, settings.fixed_delivery_fee = mode, 8
        settings.pos_delivery = True
        settings.minimum_order_amount, settings.free_delivery_threshold = 10, 100
        settings.delivery_policy = {"origin": {"latitude": -5, "longitude": -81}, "neighborhoods": [], "outside_band_mode": "quote"}
    response = client.get(f"{BASE}/integrations/context", headers=integration_headers(credential["token"]))
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["delivery"]["source"] == "pos_settings"
    assert data["delivery"]["fee"] == fee
    assert data["delivery"]["fee_status"] == status
    assert data["delivery"]["minimum_order_amount"] == 10
    assert data["location"]["latitude"] == -4.5
    assert data["delivery"]["policy"]["origin"]["latitude"] == -5
    admin_path = f"{BASE}/admin/branches/{tenant['branch_id']}/agent-context"
    assert client.get(admin_path, headers=auth_headers).status_code == 403
    admin_headers = {**auth_headers, "X-Dev-Role": "superadmin"}
    admin = client.get(admin_path, headers=admin_headers)
    assert admin.json()["delivery"] == data["delivery"]
    assert admin.headers["cache-control"] == "no-store"
    with SessionLocal.begin() as db:
        db.get(Business, tenant["business_id"]).status = "suspended"
    assert client.get(f"{BASE}/integrations/context", headers=integration_headers(credential["token"])).status_code == 403
    assert client.get(admin_path, headers=admin_headers).status_code == 200


def test_read_context_does_not_create_settings_and_disabled_fee_is_not_free(client, tenant, auth_headers):
    credential = create_credential(client, tenant, auth_headers, ["menu:read"])
    headers = integration_headers(credential["token"])
    assert client.get(f"{BASE}/integrations/context", headers=headers).status_code == 200
    with SessionLocal.begin() as db:
        assert db.scalar(select(func.count()).select_from(BranchSettings)) == 0
        settings = get_or_create_branch_settings(db, db.get(Branch, tenant["branch_id"]))
        settings.pos_delivery = False
    delivery = client.get(f"{BASE}/integrations/context", headers=headers).json()["delivery"]
    assert delivery["fee"] is None and delivery["fee_status"] == "disabled"


def test_pos_qr_download_is_identical_private_and_scoped(client, tenant, auth_headers, monkeypatch, tmp_path):
    config = get_settings()
    monkeypatch.setattr(config, "upload_dir", tmp_path)
    monkeypatch.setattr(config, "supabase_service_role_key", None)
    credential = create_credential(client, tenant, auth_headers, ["menu:read"])
    headers = integration_headers(credential["token"])
    qr_path = f"{BASE}/integrations/context/yape-qr"
    assert client.get(qr_path, headers=headers).status_code == 404
    image = io.BytesIO()
    Image.new("RGB", (80, 100), "white").save(image, "PNG")
    content = image.getvalue()
    uploaded = client.post(f"{BASE}/settings/branches/{tenant['branch_id']}/agent/yape-qr", headers=keyed(auth_headers, "qr"), data={"expected_version": 1}, files={"file": ("qr.png", content, "image/png")})
    assert uploaded.status_code == 200, uploaded.text
    context = client.get(f"{BASE}/integrations/context", headers=headers).json()
    assert context["payments"]["yape"]["qr_url"] == qr_path
    qr = client.get(qr_path, headers=headers)
    assert qr.content == content
    assert qr.headers["cache-control"] == "private, no-store"
    assert qr.headers["content-type"] == "image/png"
    assert "storage_path" not in str(context)
    assert client.get(qr_path).status_code == 401
    assert client.get(qr_path, headers=headers, params={"branch_id": tenant["other_branch_id"]}).status_code == 403
    with SessionLocal.begin() as db:
        second = Branch(business_id=tenant["business_id"], slug="second", name="Second")
        db.add(second); db.flush(); second_id = second.id
    assert client.get(qr_path, headers=headers, params={"branch_id": second_id}).status_code == 403
    other_headers = {**auth_headers, "X-Branch-Id": str(tenant["other_branch_id"]), "X-Business-Id": str(tenant["other_business_id"])}
    other = create_credential(client, {"branch_id": tenant["other_branch_id"]}, other_headers, ["menu:read"])
    assert client.get(qr_path, headers=integration_headers(other["token"])).status_code == 404
    denied = create_credential(client, tenant, auth_headers, ["orders:read"])
    assert client.get(qr_path, headers=integration_headers(denied["token"])).status_code == 403
    with SessionLocal.begin() as db:
        db.get(IntegrationCredential, credential["id"]).active = False
    assert client.get(qr_path, headers=headers).status_code == 401


def test_qr_storage_outage_is_not_missing_configuration(client, tenant, auth_headers, monkeypatch):
    credential = create_credential(client, tenant, auth_headers, ["menu:read"])
    with SessionLocal.begin() as db:
        db.get(Branch, tenant["branch_id"]).yape_qr_storage_path = "supabase://impulsa-private/test.png"
    async def unavailable(_):
        raise httpx.ConnectError("isolated outage")
    monkeypatch.setattr("app.api.load_private_file", unavailable)
    assert client.get(f"{BASE}/integrations/context/yape-qr", headers=integration_headers(credential["token"])).status_code == 503
