from concurrent.futures import ThreadPoolExecutor
from threading import Event, current_thread
from unittest.mock import Mock

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker

from app import settings_api, settings_service
from app.auth import AuthContext
from app.database import Base, SessionLocal
from app.models import (
    AuditEvent, Branch, Business, IdempotencyRecord, StaffMember,
    StaffMemberBranch, StaffMemberRole, utcnow,
)
from app.schemas import ArchiveRequest, StaffMemberUpdate


ROLES = [
    "owner", "members_manager", "manager", "menu_manager",
    "cashier", "waiter", "kitchen", "dispatcher",
]


@pytest.fixture(autouse=True)
def no_external_effects(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("External HTTP and invitations are forbidden in these tests")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(settings_api, "_deliver_staff_invitation", forbidden)


def add_member(db, business_id, user_id, roles, branch_ids, **values):
    member = StaffMember(business_id=business_id, auth_user_id=user_id, first_name=user_id, **values)
    db.add(member)
    db.flush()
    db.add_all([
        StaffMemberRole(staff_member_id=member.id, business_id=business_id, role=role)
        for role in roles
    ] + [
        StaffMemberBranch(staff_member_id=member.id, business_id=business_id, branch_id=branch_id)
        for branch_id in branch_ids
    ])
    db.flush()
    return member.id


@pytest.fixture
def staff(tenant):
    with SessionLocal.begin() as db:
        second = Branch(business_id=tenant["business_id"], slug="second-settings", name="Second")
        db.add(second)
        db.flush()
        business_id, branch_id = tenant["business_id"], tenant["branch_id"]
        return {
            "owner": add_member(db, business_id, "owner-test", ["owner"], [branch_id]),
            "second_owner": add_member(db, business_id, "second-owner", ["owner"], [second.id]),
            "manager": add_member(db, business_id, "members-test", ["members_manager", "cashier"], [branch_id]),
            "cashier": add_member(db, business_id, "cashier-test", ["cashier"], [branch_id]),
            "archived": add_member(db, business_id, "archived-owner", ["owner"], [branch_id], active=False, archived_at=utcnow()),
            "foreign": add_member(db, tenant["other_business_id"], "foreign-owner", ["owner"], [tenant["other_branch_id"]]),
            "second_branch": second.id,
        }


def headers(auth_headers, actor="manager", key=None):
    user_id, role = {
        "manager": ("members-test", "cashier"),
        "owner": ("owner-test", "owner"),
        "superadmin": ("platform-test", "superadmin"),
        "cashier": ("cashier-test", "cashier"),
    }[actor]
    result = {**auth_headers, "X-Dev-User": user_id, "X-Dev-Role": role}
    if key is not None:
        result["Idempotency-Key"] = key
    return result


def mutations_count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def test_capabilities_use_persisted_roles_and_business_wide_owner_count(client, tenant, auth_headers, staff):
    url = f"/api/v1/settings/members?branch_id={tenant['branch_id']}"
    manager_headers = {**headers(auth_headers), "X-Dev-Role": "owner"}
    response = client.get(url, headers=manager_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["capabilities"] == {"assignable_roles": ROLES[1:], "can_manage_admins": False}
    items = {item["id"]: item for item in body["items"]}
    assert body["total"] == 3
    assert items[staff["manager"]]["is_current_user"] is True
    assert items[staff["manager"]]["capabilities"] == {"can_edit": True, "can_archive": False}
    assert items[staff["owner"]]["capabilities"] == {"can_edit": False, "can_archive": False}
    assert items[staff["cashier"]]["capabilities"] == {"can_edit": True, "can_archive": True}
    assert "pin_hash" not in response.text

    admin = client.get(
        f"{url}&business_id={tenant['business_id']}&include_archived=true",
        headers=headers(auth_headers, "superadmin"),
    ).json()
    assert admin["capabilities"] == {"assignable_roles": ROLES, "can_manage_admins": True}
    admin_items = {item["id"]: item for item in admin["items"]}
    # The second owner is in another branch and is deliberately absent from this list.
    assert staff["second_owner"] not in admin_items
    assert admin_items[staff["owner"]]["capabilities"]["can_archive"] is True
    assert admin_items[staff["archived"]]["capabilities"] == {"can_edit": False, "can_archive": False}


def test_legacy_actor_without_staff_keeps_existing_access(client, tenant, auth_headers):
    response = client.get("/api/v1/settings/members", headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == {
        "items": [], "total": 0,
        "capabilities": {"assignable_roles": ROLES, "can_manage_admins": True},
    }
    with SessionLocal() as db:
        capabilities = settings_service.staff_management_capabilities(
            db, AuthContext("legacy-members-manager", "members_manager", tenant["business_id"], None),
            tenant["business_id"],
        )
        assert capabilities == {"assignable_roles": ROLES[1:], "can_manage_admins": False}


@pytest.mark.parametrize("payload", [
    {"roles": ["cashier"]},
    {"roles": ["owner", "manager"]},
    {"first_name": "Changed"},
    {"pin": "1234"},
    {"email": "blocked@example.test", "email_access": True},
])
def test_members_manager_cannot_modify_admin_any_field(client, auth_headers, staff, payload):
    response = client.patch(
        f"/api/v1/settings/members/{staff['owner']}",
        headers=headers(auth_headers, key="blocked-admin"),
        json={"expected_version": 1, **payload},
    )
    assert response.status_code == 403
    with SessionLocal() as db:
        assert db.get(StaffMember, staff["owner"]).version == 1
        assert settings_service.staff_roles(db, staff["owner"]) == ["owner"]
        assert mutations_count(db, AuditEvent) == 0
        assert mutations_count(db, IdempotencyRecord) == 0


@pytest.mark.parametrize("operation", ["create", "grant", "archive"])
def test_members_manager_cannot_create_grant_or_archive_admin(client, tenant, auth_headers, staff, operation):
    request_headers = headers(auth_headers, key=f"blocked-{operation}")
    if operation == "create":
        response = client.post("/api/v1/settings/members", headers=request_headers, json={
            "first_name": "Blocked", "roles": ["owner"], "branch_ids": [tenant["branch_id"]],
            "email": "blocked@example.test", "email_access": True,
        })
    elif operation == "grant":
        response = client.patch(f"/api/v1/settings/members/{staff['cashier']}", headers=request_headers,
                                json={"roles": ["owner"], "expected_version": 1})
    else:
        response = client.request("DELETE", f"/api/v1/settings/members/{staff['owner']}",
                                  headers=request_headers, json={"expected_version": 1})
    assert response.status_code == 403
    with SessionLocal() as db:
        assert mutations_count(db, AuditEvent) == 0
        assert mutations_count(db, IdempotencyRecord) == 0
        assert mutations_count(db, StaffMember) == 6


@pytest.mark.parametrize("expansion", ["owner", "role", "branch"])
def test_members_manager_cannot_expand_own_access(client, tenant, auth_headers, staff, expansion):
    payload = {"expected_version": 1, "first_name": "Must not persist"}
    if expansion == "branch":
        payload["branch_ids"] = [tenant["branch_id"], staff["second_branch"]]
    else:
        payload["roles"] = ["members_manager", "cashier", "owner" if expansion == "owner" else "manager"]
    response = client.patch(f"/api/v1/settings/members/{staff['manager']}",
                            headers=headers(auth_headers, key="self-expand"), json=payload)
    assert response.status_code == 403
    with SessionLocal() as db:
        assert db.get(StaffMember, staff["manager"]).first_name == "members-test"
        assert db.get(StaffMember, staff["manager"]).version == 1
        assert settings_service.staff_branches(db, staff["manager"]) == [tenant["branch_id"]]


def test_self_reduction_and_profile_edit_preserve_version_replay_and_audit(client, tenant, auth_headers, staff):
    with SessionLocal.begin() as db:
        db.add(StaffMemberBranch(staff_member_id=staff["manager"], business_id=tenant["business_id"],
                                 branch_id=staff["second_branch"]))
        business = db.get(Business, tenant["business_id"])
        before_business = (business.version, business.updated_at)
    url = f"/api/v1/settings/members/{staff['manager']}"
    payload = {"first_name": "Updated", "roles": ["members_manager"],
               "branch_ids": [tenant["branch_id"]], "expected_version": 1}
    updated = client.patch(url, headers=headers(auth_headers, key="self-reduce"), json=payload)
    assert updated.status_code == 200
    assert updated.json()["version"] == 2
    replay = client.patch(url, headers=headers(auth_headers, key="self-reduce"), json=payload)
    assert replay.json() == updated.json()
    stale = client.patch(url, headers=headers(auth_headers, key="stale"), json=payload)
    assert stale.status_code == 409
    with SessionLocal() as db:
        assert mutations_count(db, AuditEvent) == 1
        assert mutations_count(db, IdempotencyRecord) == 1
        business = db.get(Business, tenant["business_id"])
        assert (business.version, business.updated_at) == before_business


@pytest.mark.parametrize("actor", ["owner", "superadmin"])
def test_admins_can_grant_edit_demote_and_archive_other_admins(client, tenant, auth_headers, staff, actor):
    cashier_url = f"/api/v1/settings/members/{staff['cashier']}"
    granted = client.patch(cashier_url, headers=headers(auth_headers, actor, "grant"),
                           json={"roles": ["owner"], "expected_version": 1})
    assert granted.status_code == 200
    edited = client.patch(cashier_url, headers=headers(auth_headers, actor, "edit-admin"),
                          json={"first_name": "Administrator", "expected_version": 2})
    assert edited.status_code == 200
    demoted = client.patch(f"/api/v1/settings/members/{staff['second_owner']}",
                           headers=headers(auth_headers, actor, "demote"),
                           json={"roles": ["manager"], "expected_version": 1})
    assert demoted.status_code == 200
    archived = client.request("DELETE", cashier_url, headers=headers(auth_headers, actor, "archive"),
                              json={"expected_version": 3})
    assert archived.status_code == 200
    replay = client.request("DELETE", cashier_url, headers=headers(auth_headers, actor, "archive"),
                            json={"expected_version": 3})
    assert replay.json() == archived.json()
    with SessionLocal() as db:
        assert settings_service.active_owner_count(db, tenant["business_id"]) == 1
        assert mutations_count(db, AuditEvent) == 4


@pytest.mark.parametrize("operation", ["demote", "archive"])
def test_last_admin_protection_ignores_archived_and_foreign_owners(client, tenant, auth_headers, staff, operation):
    with SessionLocal.begin() as db:
        db.get(StaffMember, staff["second_owner"]).archived_at = utcnow()
    listed = client.get(
        f"/api/v1/settings/members?branch_id={tenant['branch_id']}&business_id={tenant['business_id']}",
        headers=headers(auth_headers, "superadmin"),
    ).json()
    owner = next(item for item in listed["items"] if item["id"] == staff["owner"])
    assert owner["capabilities"] == {"can_edit": True, "can_archive": False}
    method = "PATCH" if operation == "demote" else "DELETE"
    payload = {"expected_version": 1}
    if operation == "demote":
        payload.update(roles=["cashier"], first_name="Must roll back")
    response = client.request(method, f"/api/v1/settings/members/{staff['owner']}",
                              headers=headers(auth_headers, "superadmin", operation), json=payload)
    assert response.status_code == 409
    with SessionLocal() as db:
        member = db.get(StaffMember, staff["owner"])
        assert member.first_name == "owner-test" and member.version == 1 and member.active
        assert settings_service.staff_roles(db, member.id) == ["owner"]
        assert mutations_count(db, AuditEvent) == 0
        assert mutations_count(db, IdempotencyRecord) == 0


@pytest.mark.parametrize("actor", ["owner", "manager"])
def test_self_archive_is_blocked(client, auth_headers, staff, actor):
    response = client.request("DELETE", f"/api/v1/settings/members/{staff[actor]}",
                              headers=headers(auth_headers, actor, "self-archive"),
                              json={"expected_version": 1})
    assert response.status_code == 409


@pytest.mark.parametrize("state", ["demoted", "inactive", "archived"])
def test_stale_owner_auth_does_not_restore_removed_staff_permissions(client, auth_headers, staff, state):
    with SessionLocal.begin() as db:
        member = db.get(StaffMember, staff["owner"])
        if state == "demoted":
            db.get(StaffMemberRole, (member.id, "owner")).role = "cashier"
        elif state == "inactive":
            member.active = False
        else:
            member.archived_at = utcnow()
    assert client.get("/api/v1/settings/members", headers=auth_headers).status_code == 403
    response = client.patch(f"/api/v1/settings/members/{staff['second_owner']}",
                            headers=headers(auth_headers, "owner", "stale-auth"),
                            json={"roles": ["cashier"], "expected_version": 1})
    assert response.status_code == 403


def test_cross_tenant_and_branch_isolation_and_required_key(client, tenant, auth_headers, staff):
    assert client.get(f"/api/v1/settings/members?branch_id={staff['second_branch']}",
                      headers=auth_headers).status_code == 403
    assert client.get(f"/api/v1/settings/members?business_id={tenant['other_business_id']}",
                      headers=auth_headers).status_code == 403
    response = client.patch(f"/api/v1/settings/members/{staff['foreign']}",
                            headers=headers(auth_headers, "owner", "cross"),
                            json={"first_name": "Blocked", "expected_version": 1})
    assert response.status_code == 403
    response = client.patch(f"/api/v1/settings/members/{staff['cashier']}",
                            headers=headers(auth_headers, "owner", "cross-branch-assignment"),
                            json={"branch_ids": [tenant["other_branch_id"]], "expected_version": 1})
    assert response.status_code == 422
    assert client.patch(f"/api/v1/settings/members/{staff['cashier']}", headers=auth_headers,
                        json={"first_name": "Blocked", "expected_version": 1}).status_code == 422


def test_members_manager_can_manage_non_admins_with_idempotent_create(client, tenant, auth_headers, staff):
    payload = {"first_name": "New staff", "roles": ["manager", "menu_manager"],
               "branch_ids": [tenant["branch_id"]]}
    request_headers = headers(auth_headers, key="create-non-admin")
    created = client.post("/api/v1/settings/members", headers=request_headers, json=payload)
    assert created.status_code == 201
    assert client.post("/api/v1/settings/members", headers=request_headers, json=payload).json() == created.json()
    url = f"/api/v1/settings/members/{created.json()['id']}"
    updated = client.patch(url, headers=headers(auth_headers, key="edit-non-admin"),
                           json={"roles": ["cashier"], "expected_version": 1})
    assert updated.status_code == 200
    archived = client.request("DELETE", url, headers=headers(auth_headers, key="archive-non-admin"),
                              json={"expected_version": 2})
    assert archived.status_code == 200
    with SessionLocal() as db:
        assert mutations_count(db, AuditEvent) == 3
        assert mutations_count(db, IdempotencyRecord) == 3


def test_postgres_lock_is_a_business_row_for_update():
    db = Mock()
    db.get_bind.return_value.dialect.name = "postgresql"
    db.scalar.return_value = 7
    settings_service.lock_staff_business(db, 7)
    statement = db.scalar.call_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "businesses.id = 7" in sql and sql.endswith("FOR UPDATE")


@pytest.mark.parametrize("operations", [
    ("demote", "demote"), ("archive", "archive"), ("demote", "archive"), ("revoke_actor", "archive"),
])
def test_concurrent_admin_removals_are_serialized_business_wide(tmp_path, monkeypatch, operations):
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / 'concurrent-members.db').as_posix()}",
                           connect_args={"check_same_thread": False, "timeout": 10})
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(engine)
    with sessions.begin() as db:
        business = Business(slug="concurrent", name="Concurrent")
        db.add(business)
        db.flush()
        branches = [Branch(business_id=business.id, slug=f"branch-{i}", name=f"Branch {i}") for i in range(2)]
        db.add_all(branches)
        db.flush()
        ids = [add_member(db, business.id, f"owner-{i}", ["owner"], [branch.id]) for i, branch in enumerate(branches)]
        business_id = business.id
    first_locked, release_first, second_attempted, second_locked = (Event() for _ in range(4))
    original_lock = settings_api.lock_staff_business

    def observed_lock(db, requested_business_id):
        if current_thread().name.endswith("_0"):
            original_lock(db, requested_business_id)
            first_locked.set()
            assert release_first.wait(5)
        else:
            second_attempted.set()
            original_lock(db, requested_business_id)
            second_locked.set()

    monkeypatch.setattr(settings_api, "lock_staff_business", observed_lock)

    def remove(index):
        with sessions() as db:
            user = AuthContext("platform", "superadmin", None, None)
            target_id = ids[index]
            if operations[0] == "revoke_actor":
                target_id = ids[1 - index]
                if index == 1:
                    user = AuthContext("owner-1", "owner", business_id, None)
            try:
                if operations[index] in {"demote", "revoke_actor"}:
                    settings_api.update_staff_member(target_id,
                        StaffMemberUpdate(roles=["cashier"], expected_version=1),
                        idempotency_key=f"concurrent-{index}", user=user, db=db)
                else:
                    settings_api.delete_staff_member(target_id, ArchiveRequest(expected_version=1),
                        idempotency_key=f"concurrent-{index}", user=user, db=db)
                return 200
            except HTTPException as exc:
                db.rollback()
                return exc.status_code

    try:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="settings-owner") as executor:
            first = executor.submit(remove, 0)
            try:
                assert first_locked.wait(5)
                second = executor.submit(remove, 1)
                assert second_attempted.wait(5)
                assert not second_locked.wait(0.1)
            finally:
                release_first.set()
            assert first.result(timeout=5) == 200
            assert second.result(timeout=5) == (403 if operations[0] == "revoke_actor" else 409)
        with sessions() as db:
            assert settings_service.active_owner_count(db, business_id) == 1
            assert mutations_count(db, AuditEvent) == 1
            assert mutations_count(db, IdempotencyRecord) == 1
    finally:
        engine.dispose()
