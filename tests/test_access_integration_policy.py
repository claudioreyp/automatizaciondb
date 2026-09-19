from datetime import timedelta
from urllib.parse import urlparse

import pytest
from fastapi import HTTPException

from app import auth
from app.config import get_settings
from app.database import SessionLocal
from app.integration_package import ROUTES
from app.main import app, websocket_user
from app.models import Branch, Business, IntegrationCredential, Membership, ModuleEntitlement, StaffMember, StaffMemberBranch, StaffMemberRole, utcnow

ADMIN = {"X-Dev-Auth": "test-token", "X-Dev-Role": "superadmin"}


def credential(client, branch_id, **values):
    response = client.post("/api/v1/admin/integration-credentials", headers=ADMIN,
                           json={"branch_id": branch_id, "name": "Private API", **values})
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


@pytest.mark.parametrize("role", ["owner", "manager", "cashier", "waiter", "kitchen", "dispatcher"])
def test_only_superadmin_administers_integration_credentials(client, tenant, auth_headers, role):
    item = credential(client, tenant["branch_id"])
    headers = {**auth_headers, "X-Dev-Role": role}
    assert client.get("/api/v1/admin/integration-credentials", params={"branch_id": tenant["branch_id"]}, headers=headers).status_code == 403
    assert client.post("/api/v1/admin/integration-credentials", json={"branch_id": tenant["branch_id"], "name": "Forbidden"}, headers=headers).status_code == 403
    for action in ("rotate", "revoke"):
        assert client.post(f"/api/v1/admin/integration-credentials/{item['id']}/{action}", headers=headers).status_code == 403
    assert client.get(f"/api/v1/admin/branches/{tenant['branch_id']}/integration?credential_id={item['id']}", headers=headers).status_code == 403
    assert client.post("/api/v1/admin/businesses", json={"slug": "forbidden", "name": "Forbidden"}, headers=headers).status_code == 403
    assert client.get("/api/v1/integrations/context", headers={"Authorization": f"Bearer {item['token']}"}).status_code == 200


def test_full_pos_is_effective_without_rewriting_legacy_or_service_settings(client, tenant, auth_headers):
    with SessionLocal.begin() as db:
        db.get(Business, tenant["business_id"]).plan = "legacy-basic"
        db.add(ModuleEntitlement(business_id=tenant["business_id"], module="delivery", enabled=False))
        db.get(Branch, tenant["branch_id"]).delivery_enabled = False
    result = client.get("/api/v1/context", headers=auth_headers).json()
    assert result["business"]["plan"] == "pos"
    assert len(result["business"]["modules"]) == 8
    assert all(result["business"]["modules"].values())
    assert not result["branches"][0]["delivery_enabled"]
    with SessionLocal() as db:
        assert db.get(Business, tenant["business_id"]).plan == "legacy-basic"
        assert not db.query(ModuleEntitlement).one().enabled


@pytest.mark.parametrize("inactive", ["business", "branch"])
def test_suspension_blocks_pos_integrations_and_websockets_not_admins(client, tenant, auth_headers, inactive):
    item = credential(client, tenant["branch_id"])
    with SessionLocal.begin() as db:
        if inactive == "business": db.get(Business, tenant["business_id"]).status = "suspended"
        else: db.get(Branch, tenant["branch_id"]).active = False
    assert client.get("/api/v1/context", headers=auth_headers).status_code == 403
    assert client.get("/api/v1/integrations/context", headers={"Authorization": f"Bearer {item['token']}"}).status_code == 403
    assert client.get("/api/v1/admin/businesses", headers=ADMIN).status_code == 200
    with SessionLocal() as db:
        branch = db.get(Branch, tenant["branch_id"])
        from types import SimpleNamespace
        socket = SimpleNamespace(query_params={"dev_auth": "test-token"})
        with pytest.raises(HTTPException): websocket_user(socket, branch)
    with SessionLocal.begin() as db:
        db.get(Business, tenant["business_id"]).status = "active"
        db.get(Branch, tenant["branch_id"]).active = True
    assert client.get("/api/v1/integrations/context", headers={"Authorization": f"Bearer {item['token']}"}).status_code == 200


def test_production_rejects_dev_credentials_even_when_configured(client, tenant, auth_headers, monkeypatch):
    settings = get_settings().model_copy(update={"environment": "production"})
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    assert not settings.is_development
    assert client.get("/api/v1/context", headers=auth_headers).status_code in (401, 403)
    assert client.get("/api/v1/admin/businesses", headers=ADMIN).status_code in (401, 403)
    with pytest.raises(HTTPException):
        auth.get_authenticated_identity(authorization=None, x_dev_auth="test-token", x_dev_user="x", x_dev_email="x@example.test")


def test_membership_required_and_cross_branch_and_suspension_checked(tenant):
    with SessionLocal.begin() as db:
        with pytest.raises(HTTPException): auth.resolve_membership(db, "unknown")
        db.add(Membership(auth_user_id="owner", business_id=tenant["business_id"], role="owner", active=True))
        db.flush()
        assert auth.resolve_membership(db, "owner").business_id == tenant["business_id"]
        with pytest.raises(HTTPException): auth.resolve_membership(db, "owner", tenant["business_id"], tenant["other_branch_id"])
        db.get(Business, tenant["business_id"]).status = "suspended"
        with pytest.raises(HTTPException): auth.resolve_membership(db, "owner")


def test_packages_are_scoped_match_real_routes_and_never_recover_secrets(client, tenant):
    item = credential(client, tenant["branch_id"])
    other = credential(client, tenant["other_branch_id"], scopes=["menu:read"])
    url = f"/api/v1/admin/branches/{tenant['branch_id']}/integration"
    result = client.get(url, params={"credential_id": item["id"]}, headers=ADMIN)
    package = result.json()["integration"]
    assert "inventory:write" not in package["scopes"]
    assert item["token"] not in result.text and "token_hash" not in result.text
    assert result.headers["cache-control"] == "no-store"
    assert client.get(url, params={"credential_id": other["id"]}, headers=ADMIN).status_code == 404
    routes = {(path, method.upper()) for path, operations in app.openapi()["paths"].items() for method in operations}
    for _, method, path, _ in ROUTES:
        assert (f"/api/v1/integrations{path}", method) in routes
    for endpoint in package["endpoints"].values():
        assert (urlparse(endpoint["url"]).path, endpoint["method"]) in routes
        assert endpoint["scope"] in package["scopes"]
    assert client.get("/api/v1/integrations/context", params={"branch_id": tenant["other_branch_id"]}, headers={"Authorization": f"Bearer {item['token']}"}).status_code == 403
    with SessionLocal.begin() as db:
        db.get(IntegrationCredential, item["id"]).expires_at = utcnow() - timedelta(seconds=1)
    assert client.get("/api/v1/integrations/context", headers={"Authorization": f"Bearer {item['token']}"}).status_code == 401


def test_reconciliation_does_not_return_secrets(client, tenant, auth_headers):
    assert client.get("/api/v1/admin/onboarding/restaurants/status?slug=test-restaurant", headers=auth_headers).status_code == 403
    result = client.get("/api/v1/admin/onboarding/restaurants/status?slug=test-restaurant", headers=ADMIN)
    assert result.json()["status"] == "requires_review"
    assert "token" not in result.text and "password" not in result.text
    assert client.get("/api/v1/admin/onboarding/restaurants/status?slug=absent", headers=ADMIN).json()["status"] == "not_found"


def test_email_staff_uses_current_roles_and_allowed_branches(tenant):
    with SessionLocal.begin() as db:
        business_id, branch_id = tenant["business_id"], tenant["branch_id"]
        other = Branch(business_id=business_id, slug="second", name="Second")
        member = StaffMember(business_id=business_id, auth_user_id="employee", first_name="Employee", email_access=True)
        db.add_all([other, member, Membership(auth_user_id="employee", business_id=business_id, role="manager", active=True)])
        db.flush()
        role = StaffMemberRole(staff_member_id=member.id, business_id=business_id, role="cashier")
        db.add_all([role, StaffMemberBranch(staff_member_id=member.id, business_id=business_id, branch_id=branch_id)])
        db.flush()
        current = auth.resolve_membership(db, "employee", business_id, branch_id)
        assert current.role == "cashier" and current.roles == ("cashier",)
        assert current.staff_member_id == member.id
        with pytest.raises(HTTPException): auth.resolve_membership(db, "employee", business_id, other.id)
        member.email_access = False
        with pytest.raises(HTTPException): auth.resolve_membership(db, "employee", business_id, branch_id)
        member.email_access = True
        member.active = False
        with pytest.raises(HTTPException): auth.resolve_membership(db, "employee", business_id, branch_id)
        member.active = True
        db.delete(role)
        db.flush()
        with pytest.raises(HTTPException): auth.resolve_membership(db, "employee", business_id, branch_id)


def test_open_jwt_socket_is_closed_when_membership_is_revoked(client, tenant, monkeypatch):
    import app.main as main
    from starlette.websockets import WebSocketDisconnect
    monkeypatch.setattr(main, "decode_access_token", lambda _: {"sub": "socket-owner"})
    with SessionLocal.begin() as db:
        db.add(Membership(auth_user_id="socket-owner", business_id=tenant["business_id"], role="owner", active=True))
    with client.websocket_connect(f"/api/v1/ws/branches/{tenant['branch_id']}?access_token=test") as socket:
        assert socket.receive_json()["event"] == "connected"
        with SessionLocal.begin() as db:
            db.query(Membership).filter_by(auth_user_id="socket-owner").one().active = False
        socket.send_text("ping")
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == 1008
