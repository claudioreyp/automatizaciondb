import json

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import AuditEvent, Branch, IdempotencyRecord, IntegrationCredential

ADMIN = {"X-Dev-Auth": "test-token", "X-Dev-Role": "superadmin"}
PATH = "/api/v1/admin/integration-credentials"


def issue(client, tenant, key="issuance-1", **overrides):
    return client.post(PATH, headers={**ADMIN, "Idempotency-Key": key}, json={
        "branch_id": tenant["branch_id"], "name": "Additional credential", **overrides,
    })


def test_additional_tokens_coexist_and_replay_never_recovers_secret(client, tenant):
    first = issue(client, tenant)
    second = issue(client, tenant, "issuance-2")
    assert first.status_code == second.status_code == 201
    for response in (first, second):
        assert response.headers["cache-control"] == "no-store"
        assert "inventory:write" not in response.json()["scopes"]
        assert client.get("/api/v1/integrations/context", headers={
            "Authorization": f"Bearer {response.json()['token']}",
        }).status_code == 200
    replay = issue(client, tenant)
    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["secret_available"] is False
    assert "token" not in replay.json()
    with SessionLocal() as db:
        assert len(list(db.scalars(select(IntegrationCredential)))) == 2
        events = list(db.scalars(select(AuditEvent).where(AuditEvent.action == "integration_credential.created")))
        assert len(events) == 2
        receipts = list(db.scalars(select(IdempotencyRecord)))
        persisted = json.dumps([item.response_body for item in receipts] + [item.payload for item in events])
        for response in (first, second):
            assert response.json()["token"] not in persisted


def test_changed_content_conflicts_and_status_is_scoped(client, tenant):
    original = issue(client, tenant).json()
    assert issue(client, tenant, name="Different").status_code == 409
    with SessionLocal.begin() as db:
        branch = Branch(business_id=tenant["business_id"], slug="second", name="Second")
        db.add(branch)
        db.flush()
        branch_id = branch.id
    assert issue(client, tenant, branch_id=branch_id).status_code == 409
    url = f"{PATH}/operations/issuance-1"
    response = client.get(url, params={"branch_id": tenant["branch_id"]}, headers=ADMIN)
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["status"] == "created"
    assert response.json()["credential"]["id"] == original["id"]
    assert original["token"] not in response.text
    assert "token_hash" not in response.text
    for other in (branch_id, tenant["other_branch_id"]):
        assert client.get(url, params={"branch_id": other}, headers=ADMIN).json()["status"] == "not_found"


@pytest.mark.parametrize("role", ["owner", "manager", "cashier", "kitchen"])
def test_issuance_status_requires_superadmin(client, tenant, role):
    assert client.get(f"{PATH}/operations/issuance-1", params={"branch_id": tenant["branch_id"]},
                      headers={**ADMIN, "X-Dev-Role": role}).status_code == 403


def test_actor_namespace_and_revocation_do_not_reissue(client, tenant):
    original = issue(client, tenant).json()
    client.post(f"{PATH}/{original['id']}/revoke", headers=ADMIN)
    replay = issue(client, tenant).json()
    assert replay["active"] is False and "token" not in replay
    status = client.get(f"{PATH}/operations/issuance-1", params={"branch_id": tenant["branch_id"]},
                        headers={**ADMIN, "X-Dev-User": "another-superadmin"})
    assert status.json()["status"] == "not_found"


def test_legacy_creation_and_key_validation(client, tenant):
    response = client.post(PATH, headers=ADMIN, json={"branch_id": tenant["branch_id"], "name": "Legacy"})
    assert response.status_code == 201 and response.json()["token"]
    assert issue(client, tenant, "x" * 129).status_code == 422
