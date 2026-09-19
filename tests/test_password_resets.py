from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app import auth_sessions, password_resets
from app.database import SessionLocal
from app.models import AuthSecurityState, Membership, PasswordResetOperation, AuditEvent

PASSWORD = "New-Owner-Test-482!"


@pytest.fixture
def owner_account(tenant, monkeypatch):
    subject = str(uuid4())
    with SessionLocal.begin() as db:
        member = Membership(auth_user_id=subject, email="owner@example.test", role="owner",
                            business_id=tenant["business_id"])
        db.add(member)
        db.flush()
        member_id = member.id
    identity = {"id": subject, "email": "owner@example.test", "app_metadata": {"keep": "unchanged"}}
    settings = SimpleNamespace(supabase_url="https://auth.example.test", supabase_service_role_key="test-server-key",
                               auth_admin_secret="test-reset-secret-with-at-least-32-bytes", pos_public_base_url="http://localhost:5173")
    monkeypatch.setattr(password_resets, "get_settings", lambda: settings)
    monkeypatch.setattr(auth_sessions, "get_settings", lambda: settings)
    monkeypatch.setattr(password_resets, "provider_user", lambda _: dict(identity))
    calls = []

    def put(url, **kwargs):
        calls.append(kwargs["json"])
        identity["app_metadata"] = kwargs["json"]["app_metadata"]
        return httpx.Response(200, json=identity)

    monkeypatch.setattr(password_resets.httpx, "put", put)
    auth_sessions._verified.clear()
    return SimpleNamespace(id=member_id, subject=subject, identity=identity, calls=calls, settings=settings,
        path=f"/api/v1/admin/businesses/{tenant['business_id']}/memberships/{member_id}/password-reset",
        headers={"X-Dev-Auth": "test-token", "X-Dev-Role": "superadmin", "Idempotency-Key": str(uuid4())})


def reset(client, owner_account, **overrides):
    return client.post(owner_account.path, headers=owner_account.headers,
                       json={"password": PASSWORD, "expected_version": 0, **overrides})


def test_success_and_same_request_do_not_reset_twice(client, owner_account):
    response = reset(client, owner_account)
    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"
    assert response.headers["cache-control"] == "no-store"
    assert reset(client, owner_account).json() == response.json()
    assert len(owner_account.calls) == 1
    assert owner_account.calls[0]["app_metadata"]["keep"] == "unchanged"
    with SessionLocal() as db:
        state = db.get(AuthSecurityState, owner_account.subject)
        assert state.version == 1 and state.pending_operation_id is None
        assert not state.requires_password_reset
        audit_payloads = str([row.payload for row in db.scalars(select(AuditEvent))])
        assert PASSWORD not in audit_payloads
        operation = db.scalars(select(PasswordResetOperation)).one()
        assert PASSWORD not in str(vars(operation))
    assert PASSWORD not in response.text


def test_key_reuse_with_different_password_is_rejected(client, owner_account):
    assert reset(client, owner_account).status_code == 200
    assert reset(client, owner_account, password="Another-Strong-482!").status_code == 409
    assert len(owner_account.calls) == 1


def test_lost_provider_response_reconciles_without_sending_again(client, owner_account, monkeypatch):
    def lost(url, **kwargs):
        owner_account.calls.append(kwargs["json"])
        owner_account.identity["app_metadata"] = kwargs["json"]["app_metadata"]
        raise httpx.ReadTimeout("lost")
    monkeypatch.setattr(password_resets.httpx, "put", lost)
    response = reset(client, owner_account)
    assert response.status_code == 202
    assert reset(client, owner_account).json()["status"] == "pending"
    checked = client.get(owner_account.path + "/lookup", headers=owner_account.headers)
    assert checked.json()["status"] == "succeeded"
    assert len(owner_account.calls) == 1


def test_unknown_result_stays_pending_and_blocks_other_reset(client, owner_account, monkeypatch):
    monkeypatch.setattr(password_resets.httpx, "put", lambda *a, **kw: httpx.Response(503))
    response = reset(client, owner_account)
    assert response.json()["status"] == "pending"
    assert client.get(owner_account.path + "/lookup", headers=owner_account.headers).json()["status"] == "pending"
    owner_account.headers["Idempotency-Key"] = str(uuid4())
    assert reset(client, owner_account, expected_version=1).status_code == 409
    with SessionLocal() as db:
        with pytest.raises(HTTPException) as error:
            auth_sessions.validate_provider_session(db, "token", {"sub": owner_account.subject})
        assert error.value.status_code == 401


def test_confirmed_failure_preserves_recovery_requirement(client, owner_account, monkeypatch):
    with SessionLocal.begin() as db:
        db.add(AuthSecurityState(auth_user_id=owner_account.subject, requires_password_reset=True))
    monkeypatch.setattr(password_resets.httpx, "put", lambda *a, **kw: httpx.Response(422))
    assert reset(client, owner_account).json()["status"] == "failed"
    with SessionLocal() as db:
        state = db.get(AuthSecurityState, owner_account.subject)
        assert state.requires_password_reset and not state.pending_operation_id


@pytest.mark.parametrize("role", ["owner", "manager", "cashier", "kitchen"])
def test_non_admin_cannot_renew(client, owner_account, auth_headers, role):
    headers = {**auth_headers, "X-Dev-Role": role, "Idempotency-Key": str(uuid4())}
    assert client.post(owner_account.path, headers=headers, json={"password": PASSWORD, "expected_version": 0}).status_code == 403
    assert not owner_account.calls


@pytest.mark.parametrize("role,subject", [("manager", None), ("owner", "dev-owner"), ("superadmin", None)])
def test_only_real_owners_eligible(client, owner_account, role, subject):
    with SessionLocal.begin() as db:
        member = db.get(Membership, owner_account.id)
        member.role = role
        if subject:
            member.auth_user_id = subject
    assert reset(client, owner_account).status_code == 403
    assert not owner_account.calls


def test_weak_password_is_redacted(client, owner_account):
    response = reset(client, owner_account, password="weak-secret")
    assert response.status_code == 422
    assert "weak-secret" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_other_business_and_identity_mismatch_rejected(client, owner_account, tenant):
    foreign = owner_account.path.replace(f"businesses/{tenant['business_id']}", f"businesses/{tenant['other_business_id']}")
    assert client.post(foreign, headers=owner_account.headers, json={"password": PASSWORD, "expected_version": 0}).status_code == 404
    owner_account.identity["email"] = "someone-else@example.test"
    assert reset(client, owner_account).status_code == 409


def test_session_cache_cannot_survive_new_security_epoch(client, owner_account, monkeypatch):
    claims = {"sub": owner_account.subject, "session_id": str(uuid4())}
    calls = []
    def get(*a, **kw):
        calls.append(1)
        return httpx.Response(200, json={"id": owner_account.subject})
    monkeypatch.setattr(auth_sessions.httpx, "get", get)
    with SessionLocal() as db:
        auth_sessions.validate_provider_session(db, "old", claims)
        auth_sessions.validate_provider_session(db, "old", claims)
    assert len(calls) == 1
    assert reset(client, owner_account).json()["status"] == "succeeded"
    monkeypatch.setattr(auth_sessions.httpx, "get", lambda *a, **kw: httpx.Response(403))
    with SessionLocal() as db:
        with pytest.raises(HTTPException) as error:
            auth_sessions.validate_provider_session(db, "old", claims)
        assert error.value.status_code == 401


def test_auth_outage_is_not_a_revocation(owner_account, monkeypatch):
    monkeypatch.setattr(auth_sessions.httpx, "get", lambda *a, **kw: httpx.Response(503))
    with SessionLocal() as db:
        with pytest.raises(HTTPException) as error:
            auth_sessions.validate_provider_session(db, "token", {"sub": owner_account.subject, "session_id": str(uuid4())})
        assert error.value.status_code == 503


def test_malformed_provider_success_is_uncertain_not_a_second_write(client, owner_account, monkeypatch):
    calls = []
    def malformed(*args, **kwargs):
        calls.append(1)
        return httpx.Response(200, content=b"not-json")
    monkeypatch.setattr(password_resets.httpx, "put", malformed)
    assert reset(client, owner_account).status_code == 202
    assert reset(client, owner_account).json()["status"] == "pending"
    assert len(calls) == 1


def test_late_auth_success_cannot_cache_a_previous_epoch(owner_account, monkeypatch):
    claims = {"sub": owner_account.subject, "session_id": str(uuid4())}
    def late(*args, **kwargs):
        with SessionLocal.begin() as db:
            db.add(AuthSecurityState(auth_user_id=owner_account.subject, version=1))
        return httpx.Response(200, json={"id": owner_account.subject})
    monkeypatch.setattr(auth_sessions.httpx, "get", late)
    with SessionLocal() as db:
        with pytest.raises(HTTPException) as error:
            auth_sessions.validate_provider_session(db, "token", claims)
        assert error.value.status_code == 401
    assert not auth_sessions._verified


def test_reset_does_not_reactivate_business_or_modify_memberships(client, owner_account, tenant):
    from app.models import Business
    with SessionLocal.begin() as db:
        db.get(Business, tenant["business_id"]).status = "suspended"
        db.get(Membership, owner_account.id).active = False
    assert reset(client, owner_account).json()["status"] == "succeeded"
    with SessionLocal() as db:
        assert db.get(Business, tenant["business_id"]).status == "suspended"
        assert not db.get(Membership, owner_account.id).active
        assert db.get(Membership, owner_account.id).role == "owner"


def test_owner_with_superadmin_membership_is_excluded(client, owner_account):
    with SessionLocal.begin() as db:
        db.add(Membership(auth_user_id=owner_account.subject, email="owner@example.test", role="superadmin"))
    assert reset(client, owner_account).status_code == 403
    assert not owner_account.calls
