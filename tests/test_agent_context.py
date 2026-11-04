import io

import httpx
import pytest
from PIL import Image
from sqlalchemy import func, select

from app.config import get_settings
from app.database import SessionLocal
from app.models import (Branch, BranchSettings, Business, IntegrationCredential,
                        KitchenTicket, Order, Payment, PaymentEvidence, PrintJob)
from app.settings_service import get_or_create_branch_settings
from test_escalar_integrations import create_credential, integration_headers
from test_restaurant_agent_boundary import agent_headers

BASE = "/api/v1"
INITIAL_PROCESSING = {
    "cash": {"scope": "initial_order", "payment_timing": "on_fulfillment",
             "kitchen_release": "on_registration", "evidence_required": False,
             "human_approval_required": False},
    "yape": {"scope": "initial_order", "payment_timing": "before_kitchen_release",
             "kitchen_release": "after_human_approval", "evidence_required": True,
             "human_approval_required": True},
}


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
        settings.digital_delivery = False
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


@pytest.mark.parametrize("profile", ["generic", "restaurant_agent"])
def test_payment_processing_is_additive_read_only_policy_with_legacy_methods(
    client, tenant, auth_headers, agent_headers, profile,
):
    headers = agent_headers if profile == "restaurant_agent" else integration_headers(
        create_credential(client, tenant, auth_headers, ["menu:read"])["token"])
    with SessionLocal.begin() as db:
        branch = db.get(Branch, tenant["branch_id"])
        branch.accepted_payment_methods = ["cash", "transfer"]
        branch.yape_number, branch.payment_recipient_name = "999000111", "Titular sintético"
        branch.plin_number = "legacy-plin"
        version = branch.version
    response = client.get(f"{BASE}/integrations/context", headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    data = response.json()
    assert data["payments"] == {
        "methods": {channel: (["cash"] if profile == "restaurant_agent" else ["cash", "transfer"])
            for channel in ("delivery", "takeaway", "counter")},
        "processing": INITIAL_PROCESSING,
        "yape": {"revision": data["payments"]["yape"]["revision"],
                 "number": "999000111", "recipient_name": "Titular sintético", "qr_configured": False,
                 "qr_url": None, "qr_authentication": "bearer", "qr_scope": "menu:read",
                 **({"missing_fields": [], "instructions_ready": True} if profile == "restaurant_agent" else {})},
        **({"plin_number": "legacy-plin"} if profile == "generic" else {}),
    }
    assert data["configuration_version"] == version
    with SessionLocal() as db:
        assert db.get(Branch, tenant["branch_id"]).version == version
        for model in (BranchSettings, Order, Payment, PaymentEvidence, KitchenTicket, PrintJob):
            assert db.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize("methods", [
    {"delivery": ["cash"], "takeaway": ["cash"], "counter": []},
    {"delivery": ["yape"], "takeaway": [], "counter": ["cash"]},
    {"delivery": ["cash", "yape"], "takeaway": ["yape"], "counter": ["transfer"]},
    {"delivery": [], "takeaway": [], "counter": []},
])
def test_processing_policy_never_enables_or_replaces_configured_payment_methods(
    client, tenant, auth_headers, methods,
):
    token = create_credential(client, tenant, auth_headers, ["menu:read"])["token"]
    with SessionLocal.begin() as db:
        branch = db.get(Branch, tenant["branch_id"])
        branch.accepted_payment_methods = ["plin"]  # Superseded legacy configuration.
        settings = get_or_create_branch_settings(db, branch)
        settings.payment_methods = methods
        settings.digital_tables = False
        version = settings.version
    headers = integration_headers(token)
    response = client.get(f"{BASE}/integrations/context", headers=headers)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["payments"]["methods"] == methods
    assert data["payments"]["processing"] == INITIAL_PROCESSING
    assert "counter" not in data["capabilities"]["order_modes"]
    assert set(data["payments"]["processing"]) == {"cash", "yape"}
    assert data["configuration_version"] == version
    data["payments"]["processing"]["yape"]["human_approval_required"] = False
    data["payments"]["methods"]["takeaway"].append("plin")
    fresh = client.get(f"{BASE}/integrations/context", headers=headers).json()
    assert fresh["payments"]["processing"] == INITIAL_PROCESSING
    assert fresh["payments"]["methods"] == methods
    with SessionLocal() as db:
        settings = db.scalar(select(BranchSettings).where(BranchSettings.branch_id == tenant["branch_id"]))
        assert settings.payment_methods == methods and settings.version == version


@pytest.mark.parametrize("method", ["cash", "yape"])
def test_published_initial_processing_matches_registration_and_human_review(
    client, tenant, auth_headers, monkeypatch, tmp_path, method,
):
    from test_agent_checkout import open_cash, review, setup, upload
    config = get_settings()
    monkeypatch.setattr(config, "upload_dir", tmp_path)
    monkeypatch.setattr(config, "supabase_service_role_key", None)
    headers, order = setup(client, tenant, auth_headers, method=method)
    policy = client.get(f"{BASE}/integrations/context", headers=headers).json()["payments"]["processing"][method]
    assert policy == INITIAL_PROCESSING[method]
    assert order["sent_to_kitchen_at"] is None  # The intermediate draft is not completed registration.
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Payment)) == 0
        assert db.scalar(select(func.count()).select_from(KitchenTicket)) == 0
    if method == "cash":
        result = client.post(f"{BASE}/integrations/orders/{order['id']}/cash-confirm",
            headers=keyed(headers, "processing-initial-cash"))
        assert result.status_code == 200, result.text
        assert result.json()["order"]["payment_status"] == "pending"
    else:
        receipt = upload(client, headers, order)
        assert receipt["requires_human_review"] is True
        assert receipt["evidence"]["status"] == "under_review"
        assert receipt["order"]["sent_to_kitchen_at"] is None
        with SessionLocal() as db:
            assert db.scalar(select(func.count()).select_from(Payment)) == 0
            assert db.scalar(select(func.count()).select_from(KitchenTicket)) == 0
            assert db.scalar(select(func.count()).select_from(PrintJob)) == 0
        open_cash(client, auth_headers, tenant)
        result = review(client, auth_headers, tenant, receipt)
        assert result.status_code == 200, result.text
        assert result.json()["evidence"]["status"] == "paid"
    assert result.json()["order"]["sent_to_kitchen_at"] is not None
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(KitchenTicket)) == 1
        assert db.scalar(select(func.count()).select_from(Payment)) == (1 if method == "yape" else 0)
