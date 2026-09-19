import io
import json
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import select, func

from app.config import get_settings
from app.database import SessionLocal
from app.models import AuditEvent, Branch, IdempotencyRecord, PairedDevice, StaffMember, StaffMemberBranch, StaffMemberRole, utcnow
from app.settings_service import hash_pin

BASE = "/api/v1"
ORIGIN = {"Origin": "http://localhost:5173"}


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "device_auth_secret", "isolated-device-test-secret-not-for-deployment")
    monkeypatch.setattr(settings, "pos_public_base_url", "http://localhost:5173")
    monkeypatch.setattr(settings, "upload_dir", tmp_path)
    monkeypatch.setattr(settings, "supabase_service_role_key", None)


def keyed(headers, key):
    return {**headers, "Idempotency-Key": key}


def make_member(tenant, role="cashier", other=False):
    prefix = "other_" if other else ""
    with SessionLocal.begin() as db:
        member = StaffMember(business_id=tenant[prefix + "business_id"], first_name="Ana", last_name=role, pin_hash=hash_pin("8062"))
        db.add(member)
        db.flush()
        db.add_all([StaffMemberRole(staff_member_id=member.id, business_id=member.business_id, role=role),
                    StaffMemberBranch(staff_member_id=member.id, business_id=member.business_id, branch_id=tenant[prefix + "branch_id"])])
        return member.id


def link(client, tenant, headers, key="link"):
    result = client.post(BASE + "/settings/devices/pairing-links", headers=keyed(headers, key), json={"branch_id": tenant["branch_id"]})
    assert result.status_code == 200, result.text
    assert result.headers["cache-control"] == "no-store"
    return result.json(), parse_qs(urlsplit(result.json()["url"]).fragment)["token"][0]


def activate(client, token, key="activate"):
    return client.post(BASE + "/auth/devices/activate", headers=keyed(ORIGIN, key), json={"token": token, "name": "Tablet caja"})


def csrf_headers(client, key="login"):
    session = client.get(BASE + "/auth/devices/session")
    assert session.status_code == 200, session.text
    return keyed({**ORIGIN, "X-CSRF-Token": session.json()["csrf_token"]}, key)


def login(client, member, key="login", pin="8062"):
    return client.post(BASE + "/auth/devices/login", headers=csrf_headers(client, key), json={"member_id": member, "pin": pin})


def test_pair_activate_login_permissions_and_revocation(client, tenant, auth_headers):
    member = make_member(tenant)
    make_member(tenant, other=True)
    pairing, token = link(client, tenant, auth_headers)
    assert len(token) >= 50
    preview = client.post(BASE + "/auth/devices/preview", headers=ORIGIN, json={"token": token})
    assert preview.json()["branch_name"] == "Main"
    result = activate(client, token)
    assert result.status_code == 200, result.text
    cookie = result.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/api/v1" in cookie
    credential = client.cookies.get("pos_device")
    assert activate(client, token).status_code == 200
    assert client.cookies.get("pos_device") == credential
    assert activate(client, token, "different-activation").status_code == 410
    assert client.get(BASE + "/auth/devices/members").json() == {"items": [{"id": member, "name": "Ana cashier"}]}
    result = login(client, member)
    assert result.status_code == 200, result.text
    session_cookie = client.cookies.get("pos_staff")
    assert login(client, member).status_code == 200
    assert client.cookies.get("pos_staff") == session_cookie
    session = client.get(BASE + "/auth/devices/session").json()
    assert session["user"]["roles"] == ["cashier"]
    assert client.get(BASE + "/orders", params={"branch_id": tenant["branch_id"]}).status_code == 200
    assert client.get(BASE + f"/settings/branches/{tenant['branch_id']}/agent").status_code == 403
    assert client.get(BASE + "/orders", headers={"X-Branch-Id": str(tenant["other_branch_id"])}).status_code == 403
    assert client.post(BASE + "/auth/devices/logout", headers=ORIGIN, json={}).status_code == 403
    assert client.post(BASE + "/auth/devices/logout", headers=csrf_headers(client), json={}).status_code == 200
    assert client.get(BASE + "/auth/devices/session").json()["linked"] is True
    assert client.get(BASE + "/orders").status_code == 401
    assert login(client, member, "second-session").status_code == 200
    device = client.get(BASE + f"/settings/devices/{pairing['device']['id']}", headers=auth_headers).json()
    revoked = client.request("DELETE", BASE + f"/settings/devices/{device['id']}", headers=keyed(auth_headers, "revoke"), json={"expected_version": device["version"]})
    assert revoked.status_code == 200, revoked.text
    assert client.get(BASE + "/orders").status_code == 401
    assert activate(client, token).status_code in {401, 410}
    with SessionLocal() as db:
        records = json.dumps([r.response_body for r in db.scalars(select(IdempotencyRecord))])
        audits = json.dumps([r.payload for r in db.scalars(select(AuditEvent))])
        for value in [token, credential, session_cookie, "8062"]:
            assert value not in records and value not in audits


def test_origin_permissions_and_cancelled_expired_links(client, tenant, auth_headers):
    assert client.post(BASE + "/settings/devices/pairing-links", headers=keyed({**auth_headers, "X-Dev-Role": "cashier"}, "deny"), json={"branch_id": tenant["branch_id"]}).status_code == 403
    assert client.post(BASE + "/settings/devices/pairing-links", headers=keyed(auth_headers, "foreign"), json={"branch_id": tenant["other_branch_id"]}).status_code in {403, 404}
    pairing, token = link(client, tenant, auth_headers)
    assert client.post(BASE + "/auth/devices/activate", headers=keyed({"Origin": "https://attacker.invalid"}, "origin"), json={"token": token, "name": "Tablet"}).status_code == 403
    cancelled = client.request("DELETE", BASE + f"/settings/devices/{pairing['device']['id']}/pairing-link", headers=keyed(auth_headers, "cancel"), json={"expected_version": 1})
    assert cancelled.status_code == 200
    assert activate(client, token).status_code == 410
    pairing, token = link(client, tenant, auth_headers, "new-link")
    with SessionLocal.begin() as db:
        db.get(PairedDevice, pairing["device"]["id"]).pairing_expires_at = utcnow() - timedelta(seconds=1)
    assert activate(client, token).status_code == 410


def test_pin_lock_session_expiry_and_current_permissions(client, tenant, auth_headers):
    member = make_member(tenant, "members_manager")
    pairing, token = link(client, tenant, auth_headers)
    assert activate(client, token).status_code == 200
    for index in range(5):
        assert login(client, member, f"bad-{index}", "0000").status_code == 401
    assert login(client, member, "locked").status_code == 429
    assert login(client, member, "bad-0", "0000").status_code == 401
    with SessionLocal.begin() as db:
        db.get(StaffMember, member).pin_locked_until = utcnow() - timedelta(seconds=1)
        db.get(PairedDevice, pairing["device"]["id"]).pin_locked_until = utcnow() - timedelta(seconds=1)
    assert login(client, member, "unlocked").status_code == 200
    members = client.get(BASE + "/settings/members").json()["items"]
    assert next(row for row in members if row["id"] == member)["is_current_user"]
    # A member manager must never grant their own account an owner role.
    response = client.patch(BASE + f"/settings/members/{member}", headers=csrf_headers(client, "escalate"), json={"expected_version": 1, "roles": ["owner"]})
    assert response.status_code == 403, response.text
    with SessionLocal.begin() as db:
        db.get(StaffMember, member).version += 1
    assert client.get(BASE + "/settings/members").status_code == 401
    assert login(client, member, "changed-permissions").status_code == 200
    with SessionLocal.begin() as db:
        db.get(PairedDevice, pairing["device"]["id"]).staff_session_expires_at = utcnow() - timedelta(seconds=1)
    assert client.get(BASE + "/settings/members").status_code == 401


def png(color="red"):
    stream = io.BytesIO()
    Image.new("RGB", (48, 80), color).save(stream, "PNG")
    return stream.getvalue()


def test_gallery_legacy_primary_order_limits_and_private_images(client, tenant, auth_headers):
    path = BASE + f"/settings/branches/{tenant['branch_id']}/agent"
    profile = client.get(path, headers=auth_headers).json()
    named = client.patch(path, headers=keyed(auth_headers, "name"), json={"expected_version": profile["version"], "name": "  Ana  "})
    assert named.status_code == 200, named.text
    profile = named.json()
    assert profile["name"] == "Ana"
    assert client.patch(path, headers=keyed(auth_headers, "name"), json={"expected_version": 1, "name": "  Ana  "}).json() == profile
    assert client.patch(path, headers=keyed(auth_headers, "long"), json={"expected_version": profile["version"], "name": "X" * 81}).status_code == 422
    for i in range(10):
        response = client.post(path + "/images", headers=keyed(auth_headers, f"image-{i}"), data={"expected_version": profile["version"]}, files={"file": ("menu.png", png(), "image/png")})
        assert response.status_code == 200, response.text
        profile = response.json()
    assert len(profile["images"]) == 10
    assert client.post(path + "/images", headers=keyed(auth_headers, "eleventh"), data={"expected_version": profile["version"]}, files={"file": ("menu.png", png(), "image/png")}).status_code == 422
    assert client.get(profile["images"][0]["url"]).status_code == 401
    assert client.get(profile["images"][0]["url"], headers=auth_headers).content == png()
    ids = [image["id"] for image in profile["images"]]
    profile = client.patch(path, headers=keyed(auth_headers, "order"), json={"expected_version": profile["version"], "image_order": ids[::-1]}).json()
    legacy = client.post(BASE + f"/branches/{tenant['branch_id']}/menu-card", headers=auth_headers, files={"file": ("new.png", png("blue"), "image/png")})
    assert legacy.status_code == 200, legacy.text
    profile = client.get(path, headers=auth_headers).json()
    assert [i["id"] for i in profile["images"]] == ids[::-1]
    assert client.get(profile["images"][0]["url"], headers=auth_headers).content == png("blue")
    response = client.request("DELETE", path + f"/images/{ids[-1]}", headers=keyed(auth_headers, "delete"), json={"expected_version": profile["version"]})
    assert response.status_code == 200, response.text
    assert response.json()["images"][0]["id"] == ids[-2]
    assert client.get(BASE + f"/branches/{tenant['branch_id']}/menu-card", headers=auth_headers).content == png()
    assert client.get(BASE + f"/settings/branches/{tenant['other_branch_id']}/agent", headers=auth_headers).status_code in {403, 404}


def test_upload_rollback_idempotency_and_uncropped_qr(client, tenant, auth_headers, monkeypatch):
    import app.agent_settings as agent
    path = BASE + f"/settings/branches/{tenant['branch_id']}/agent"
    params = dict(headers=keyed(auth_headers, "qr"), data={"expected_version": 1}, files={"file": ("qr.png", png(), "image/png")})
    original = agent.commit
    def failed(*args):
        raise HTTPException(503, "isolated simulated failure")
    monkeypatch.setattr(agent, "commit", failed)
    assert client.post(path + "/yape-qr", **params).status_code == 503
    assert not list(get_settings().upload_dir.iterdir())
    assert client.get(path, headers=auth_headers).json()["version"] == 1
    monkeypatch.setattr(agent, "commit", original)
    saved = client.post(path + "/yape-qr", **params)
    assert saved.status_code == 200, saved.text
    assert client.post(path + "/yape-qr", **params).json() == saved.json()
    assert len(list(get_settings().upload_dir.iterdir())) == 1
    assert client.get(saved.json()["yape_qr_url"], headers=auth_headers).content == png()
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(AuditEvent)) == 1
    assert client.post(path + "/images", headers=keyed(auth_headers, "stale"), data={"expected_version": 1}, files={"file": ("menu.png", png(), "image/png")}).status_code == 409
    assert client.post(path + "/images", headers=keyed(auth_headers, "invalid"), data={"expected_version": 2}, files={"file": ("menu.png", b"not-image", "image/png")}).status_code == 422


def test_integration_reads_gallery_without_changing_workflows(client, tenant, auth_headers):
    from test_escalar_integrations import create_credential, integration_headers
    credential = create_credential(client, tenant, auth_headers, ["menu:read"])
    headers = integration_headers(credential["token"])
    path = BASE + f"/settings/branches/{tenant['branch_id']}/agent"
    client.patch(path, headers=keyed(auth_headers, "agent-name"), json={"expected_version": 1, "name": "Ana"})
    response = client.post(path + "/images", headers=keyed(auth_headers, "menu"), data={"expected_version": 2}, files={"file": ("menu.png", png(), "image/png")})
    assert response.status_code == 200
    context = client.get(BASE + "/integrations/context", headers=headers).json()["branch"]
    assert context["agent_name"] == "Ana"
    assert len(context["menu_images"]) == 1
    assert client.get(context["menu_images"][0]["url"], headers=headers).content == png()
    assert client.get(BASE + "/integrations/context/menu-card", headers=headers).content == png()


def test_concurrent_activations_are_single_use(tmp_path, tenant, auth_headers):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.database import Base, engine, get_db
    from app.main import app
    isolated = create_engine(f"sqlite+pysqlite:///{(tmp_path / 'concurrent.db').as_posix()}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(isolated)
    with engine.connect() as source, isolated.begin() as dest:
        for table in Base.metadata.sorted_tables:
            rows = source.execute(select(table)).mappings().all()
            if rows:
                dest.execute(table.insert(), [dict(row) for row in rows])
    def db_session():
        with Session(isolated) as db:
            yield db
    app.dependency_overrides[get_db] = db_session
    try:
        with TestClient(app) as admin:
            pairing, token = link(admin, tenant, auth_headers)
        barrier = Barrier(2)
        def claim(index):
            with TestClient(app) as browser:
                barrier.wait()
                return activate(browser, token, f"claim-{index}").status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(claim, [0, 1])) == [200, 410]
        with Session(isolated) as db:
            assert db.get(PairedDevice, pairing["device"]["id"]).version == 2
    finally:
        app.dependency_overrides.pop(get_db, None)
        isolated.dispose()


def test_activation_rollback_preserves_unused_link(client, tenant, auth_headers, monkeypatch):
    import app.device_api as devices
    pairing, token = link(client, tenant, auth_headers)
    original = devices.save_attempt
    def failed(*args):
        raise HTTPException(503, "isolated transaction failure")
    monkeypatch.setattr(devices, "save_attempt", failed)
    assert activate(client, token).status_code == 503
    assert not client.cookies.get("pos_device")
    with SessionLocal() as db:
        device = db.get(PairedDevice, pairing["device"]["id"])
        assert not device.paired_at and not device.token_hash
        assert device.version == 1
        assert db.scalar(select(func.count()).select_from(AuditEvent)) == 1
    monkeypatch.setattr(devices, "save_attempt", original)
    assert activate(client, token).status_code == 200


def test_realtime_rechecks_origin_scope_and_revocation_before_delivery(client, tenant, auth_headers):
    from starlette.websockets import WebSocketDisconnect
    from app.realtime import hub
    member = make_member(tenant)
    pairing, token = link(client, tenant, auth_headers)
    assert activate(client, token).status_code == 200
    assert login(client, member).status_code == 200
    path = BASE + f"/ws/branches/{tenant['branch_id']}"
    for url, headers in [(path, {"Origin": "https://attacker.invalid"}),
                         (BASE + f"/ws/branches/{tenant['other_branch_id']}", ORIGIN)]:
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect(url, headers=headers):
                pass
        assert denied.value.code == 1008
    with client.websocket_connect(path, headers=ORIGIN) as socket:
        assert socket.receive_json()["event"] == "connected"
        with SessionLocal.begin() as db:
            db.get(PairedDevice, pairing["device"]["id"]).active = False
        client.portal.call(hub.broadcast, tenant["branch_id"], "order.updated", {"private": True})
        with pytest.raises(WebSocketDisconnect) as revoked:
            socket.receive_json()
        assert revoked.value.code == 1008


def test_device_cookie_is_secure_outside_development(monkeypatch):
    from fastapi import Response
    from app.device_auth import set_cookie
    settings = get_settings()
    monkeypatch.setattr(settings, "environment", "production")
    response = Response()
    set_cookie(response, "pos_device", "isolated-cookie", utcnow() + timedelta(days=30))
    assert "Secure" in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]
