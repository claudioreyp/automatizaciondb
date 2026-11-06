"""Atomic, opt-in issuance for the one-copy restaurant automation bundle."""
import json

import pytest
from sqlalchemy import select

import app.api as api_module
from app.database import SessionLocal
from app.models import AuditEvent, Branch, Business, IntegrationCredential, Membership


ADMIN = {"X-Dev-Auth": "test-token", "X-Dev-Role": "superadmin"}
PATH = "/api/v1/admin/onboarding/restaurants"
AGENT_SCOPES = {"menu:read", "orders:read", "orders:write", "payments:write", "agent:state", "agent:attention"}
NOTICE_SCOPES = {"menu:read", "orders:read", "events:read", "events:deliver"}


def payload(**overrides):
    return {
        "business": {"name": "New restaurant", "slug": "new-restaurant"},
        "branch": {"name": "Main", "slug": "main"},
        "owner_email": "owner@example.test", "owner_password": "Synthetic-Password42!",
        "integration_profile": "restaurant_agent", **overrides,
    }


@pytest.fixture
def owner_calls(monkeypatch):
    calls = {"created": [], "deleted": []}

    async def create_owner(*args):
        calls["created"].append(args)
        return "synthetic-owner"

    async def delete_owner(user_id, *_args):
        calls["deleted"].append(user_id)

    monkeypatch.setattr(api_module, "create_supabase_owner_account", create_owner)
    monkeypatch.setattr(api_module, "delete_supabase_auth_user", delete_owner)
    return calls


@pytest.mark.parametrize("notifications", [False, True])
def test_opt_in_pair_is_scoped_and_secret_once(client, owner_calls, notifications):
    body = payload(**({"create_pos_notifications": True} if notifications else {}))
    response = client.post(PATH, headers=ADMIN, json=body)
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    result = response.json()
    agent = result["credential"]
    assert agent["profile"] == "restaurant_agent"
    assert set(agent["scopes"]) == AGENT_SCOPES
    assert ("pos_notifications" in result) == notifications
    packages = [(agent, result["integration"])]
    if notifications:
        notice = result["pos_notifications"]["credential"]
        assert notice["token"] != agent["token"]
        assert notice["id"] != agent["id"] and notice["profile"] == "generic"
        assert set(notice["scopes"]) == NOTICE_SCOPES
        packages.append((notice, result["pos_notifications"]["integration"]))
        endpoints = result["pos_notifications"]["integration"]["endpoints"]
        assert {"events", "queue_event_notice", "resolve_event_notice"}.issubset(endpoints)
        assert "create_order_draft" not in endpoints and "payment_evidence" not in endpoints
        notice_headers = {"Authorization": "Bearer " + notice["token"]}
        assert client.get("/api/v1/integrations/events", headers=notice_headers).status_code == 200
        assert client.post("/api/v1/integrations/orders/draft", headers=notice_headers, json={}).status_code == 403
        assert client.get("/api/v1/integrations/events", headers={"Authorization": "Bearer " + agent["token"]}).status_code == 403

    for credential, package in packages:
        assert credential["business_id"] == package["business_id"] == result["business"]["id"]
        assert credential["branch_id"] == package["branch_id"] == result["branch"]["id"]
        assert package["profile"] == credential["profile"]
        assert set(package["scopes"]) == set(credential["scopes"])
        context = client.get("/api/v1/integrations/context", headers={"Authorization": "Bearer " + credential["token"]})
        assert context.status_code == 200
        assert context.json()["branch"]["id"] == result["branch"]["id"]

    listed = client.get("/api/v1/admin/integration-credentials", headers=ADMIN, params={"branch_id": result["branch"]["id"]})
    status = client.get(PATH + "/status?slug=new-restaurant", headers=ADMIN)
    assert status.json()["status"] == "created"
    with SessionLocal() as db:
        rows = list(db.scalars(select(IntegrationCredential)))
        assert len(rows) == 1 + int(notifications)
        events = list(db.scalars(select(AuditEvent).where(AuditEvent.action == "integration_credential.created")))
        assert len(events) == len(rows)
        recorded = json.dumps([event.payload for event in events])
        for credential, _ in packages:
            assert credential["token"] not in recorded + listed.text + status.text
            row = db.get(IntegrationCredential, credential["id"])
            assert credential["token"] != row.token_hash
    assert len(owner_calls["created"]) == 1 and owner_calls["deleted"] == []


def test_notification_pair_requires_restaurant_agent_profile(client, owner_calls):
    response = client.post(PATH, headers=ADMIN, json=payload(integration_profile="generic", create_pos_notifications=True))
    assert response.status_code == 422
    assert owner_calls["created"] == []
    with SessionLocal() as db:
        assert db.query(Business).count() == 0 and db.query(IntegrationCredential).count() == 0


@pytest.mark.parametrize("failure", ["second_token", "notification_package", "notification_audit"])
def test_pair_rolls_back_in_full_on_failure(client, monkeypatch, owner_calls, failure):
    original_issue = api_module.issue_integration_token
    original_package = api_module.integration_package
    original_audit = api_module.audit
    issued = []

    def issue():
        if failure == "second_token" and issued:
            raise RuntimeError("Synthetic second token failure")
        value = original_issue()
        issued.append(value)
        return value

    def package(base, business, branch, scopes, profile):
        if failure == "notification_package" and "events:deliver" in scopes:
            raise RuntimeError("Synthetic notification contract failure")
        return original_package(base, business, branch, scopes, profile)

    def audit(db, user, action, entity_type, entity_id, business_id, event_payload=None):
        if failure == "notification_audit" and action == "integration_credential.created" and "events:deliver" in (event_payload or {}).get("scopes", []):
            raise RuntimeError("Synthetic notification audit failure")
        return original_audit(db, user, action, entity_type, entity_id, business_id, event_payload)

    monkeypatch.setattr(api_module, "issue_integration_token", issue)
    monkeypatch.setattr(api_module, "integration_package", package)
    monkeypatch.setattr(api_module, "audit", audit)
    response = client.post(PATH, headers=ADMIN, json=payload(create_pos_notifications=True))
    assert response.status_code == 500
    for token, _, _ in issued:
        assert token not in response.text
    with SessionLocal() as db:
        for model in (Business, Branch, Membership, IntegrationCredential, AuditEvent):
            assert db.query(model).count() == 0
    assert owner_calls["deleted"] == ([] if failure == "second_token" else ["synthetic-owner"])


def test_duplicate_pair_onboarding_does_not_issue_again(client, owner_calls):
    body = payload(create_pos_notifications=True)
    first = client.post(PATH, headers=ADMIN, json=body)
    assert first.status_code == 201
    duplicate = client.post(PATH, headers=ADMIN, json=body)
    assert duplicate.status_code == 409
    assert len(owner_calls["created"]) == 1
    with SessionLocal() as db:
        assert db.query(Business).count() == 1
        assert db.query(IntegrationCredential).count() == 2
    for credential in (first.json()["credential"], first.json()["pos_notifications"]["credential"]):
        assert credential["token"] not in duplicate.text
