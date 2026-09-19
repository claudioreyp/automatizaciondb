import httpx
import pytest
from sqlalchemy import func, select

from app import settings_api
from app.database import SessionLocal
from app.models import AuditEvent, Branch, IdempotencyRecord


@pytest.fixture(autouse=True)
def no_external_services(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Media deletion tests must not call external services")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)


@pytest.fixture
def media(tenant):
    with SessionLocal.begin() as db:
        branch = db.get(Branch, tenant["branch_id"])
        branch.logo_storage_path = "test-only/logo.png"
        branch.cover_storage_path = "test-only/cover.png"


def delete(client, tenant, headers, kind="logo", version=1):
    return client.request("DELETE", f"/api/v1/settings/branches/{tenant['branch_id']}/media/{kind}",
                          headers=headers, json={"expected_version": version})


@pytest.mark.parametrize("kind", ["logo", "cover"])
def test_delete_media_commits_before_cleanup_and_replays_once(client, tenant, auth_headers, media, monkeypatch, kind):
    deleted = []

    async def cleanup(path):
        with SessionLocal() as db:
            branch = db.get(Branch, tenant["branch_id"])
            assert getattr(branch, f"{kind}_storage_path") is None
            assert branch.version == 2
            assert db.scalar(select(func.count(AuditEvent.id))) == 1
            assert db.scalar(select(func.count(IdempotencyRecord.id))) == 1
        deleted.append(path)

    monkeypatch.setattr(settings_api, "delete_private_file", cleanup)
    headers = {**auth_headers, "Idempotency-Key": "delete"}
    response = delete(client, tenant, headers, kind)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result[f"{kind}_configured"] is False
    assert result[f"{kind}_url"] is None
    assert result["active"] is True
    assert result["archived_at"] is None
    assert result["version"] == 2
    assert "storage_path" not in response.text
    assert delete(client, tenant, headers, kind).json() == result
    assert deleted == [f"test-only/{kind}.png"]
    assert client.get(f"/api/v1/settings/branches/{tenant['branch_id']}/media/{kind}").status_code == 404
    with SessionLocal() as db:
        branch = db.get(Branch, tenant["branch_id"])
        other_kind = "cover" if kind == "logo" else "logo"
        assert getattr(branch, f"{other_kind}_storage_path") == f"test-only/{other_kind}.png"
        audit = db.scalar(select(AuditEvent))
        assert audit.action == f"settings.branch.{kind}.deleted"
        assert audit.business_id == tenant["business_id"]
        assert audit.branch_id == tenant["branch_id"]
        assert audit.actor_id == auth_headers["X-Dev-User"]
        assert audit.payload["before"][f"{kind}_configured"] is True
        assert audit.payload["after"][f"{kind}_configured"] is False


@pytest.mark.parametrize("failure", [FileNotFoundError, PermissionError, httpx.ConnectError])
def test_cleanup_failure_does_not_undo_committed_media_deletion(client, tenant, auth_headers, media, monkeypatch, failure):
    async def cleanup(path):
        raise failure("simulated")

    monkeypatch.setattr(settings_api, "delete_private_file", cleanup)
    response = delete(client, tenant, {**auth_headers, "Idempotency-Key": "cleanup-fails"})
    assert response.status_code == 200
    assert response.json()["logo_configured"] is False
    with SessionLocal() as db:
        assert db.get(Branch, tenant["branch_id"]).logo_storage_path is None
        assert db.scalar(select(func.count(AuditEvent.id))) == 1


def test_failed_media_commit_never_cleans_old_file(client, tenant, auth_headers, media, monkeypatch):
    async def cleanup(path):
        raise AssertionError("Cleanup cannot run before commit")

    def fail(*args, **kwargs):
        raise RuntimeError("simulated transaction failure")

    monkeypatch.setattr(settings_api, "delete_private_file", cleanup)
    monkeypatch.setattr(settings_api, "save_idempotent_response", fail)
    with pytest.raises(RuntimeError, match="simulated"):
        delete(client, tenant, {**auth_headers, "Idempotency-Key": "commit-fails"})
    with SessionLocal() as db:
        branch = db.get(Branch, tenant["branch_id"])
        assert branch.logo_storage_path == "test-only/logo.png"
        assert branch.version == 1
        assert db.scalar(select(func.count(AuditEvent.id))) == 0
        assert db.scalar(select(func.count(IdempotencyRecord.id))) == 0


def test_shared_storage_path_is_not_deleted(client, tenant, auth_headers, media, monkeypatch):
    with SessionLocal.begin() as db:
        db.get(Branch, tenant["other_branch_id"]).logo_storage_path = "test-only/logo.png"

    async def cleanup(path):
        raise AssertionError("A referenced file must not be deleted")

    monkeypatch.setattr(settings_api, "delete_private_file", cleanup)
    assert delete(client, tenant, {**auth_headers, "Idempotency-Key": "shared"}).status_code == 200


def test_media_delete_permissions_versions_and_kinds(client, tenant, auth_headers, media, monkeypatch):
    async def cleanup(path):
        raise AssertionError("Failed requests must not touch storage")

    monkeypatch.setattr(settings_api, "delete_private_file", cleanup)
    headers = {**auth_headers, "Idempotency-Key": "validation"}
    assert delete(client, tenant, auth_headers).status_code == 422
    assert delete(client, tenant, headers, kind="yape-qr").status_code == 422
    assert delete(client, tenant, headers, version=0).status_code == 422
    assert delete(client, tenant, headers, version=2).status_code == 409
    assert delete(client, tenant, {**headers, "X-Dev-Role": "cashier"}).status_code == 403
    assert delete(client, {**tenant, "branch_id": tenant["other_branch_id"]}, headers).status_code == 403
    with SessionLocal.begin() as db:
        branch = Branch(business_id=tenant["business_id"], slug="second", name="Second")
        db.add(branch)
        db.flush()
        second_id = branch.id
    assert delete(client, {**tenant, "branch_id": second_id}, headers).status_code == 403
    with SessionLocal() as db:
        assert db.scalar(select(func.count(AuditEvent.id))) == 0
        assert db.scalar(select(func.count(IdempotencyRecord.id))) == 0


def test_deleting_absent_image_is_versioned_without_storage_access(client, tenant, auth_headers, monkeypatch):
    async def cleanup(path):
        raise AssertionError("No image to delete")

    monkeypatch.setattr(settings_api, "delete_private_file", cleanup)
    response = delete(client, tenant, {**auth_headers, "Idempotency-Key": "absent"})
    assert response.status_code == 200
    assert response.json()["logo_configured"] is False
    assert response.json()["version"] == 2
