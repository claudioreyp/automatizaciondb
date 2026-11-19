import asyncio
import io
import importlib
import subprocess
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, get_ident
from urllib.parse import unquote

import pytest
import httpx
from fastapi import HTTPException
from PIL import Image
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import ArrayObject, NameObject
from sqlalchemy import func, inspect, select, text

import app.agent_menu_pdf as pdf_validation
import app.agent_settings as agent
from app.config import get_settings
from app.database import SessionLocal, engine
from app.main import app
from app.models import AgentConversation, AuditEvent, Branch, IdempotencyRecord

BASE = "/api/v1/settings/branches"


@pytest.fixture(autouse=True)
def isolated_uploads(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "upload_dir", tmp_path)
    monkeypatch.setattr(settings, "supabase_service_role_key", None)


def pdf_bytes(*, pages=1, encrypted=False, cyclic=False, title=None):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=300, height=400)
    if title is not None:
        writer.add_metadata({"/Title": title})
    if cyclic:
        pages_ref = writer.root_object.raw_get("/Pages")
        pages_ref.get_object()[NameObject("/Kids")] = ArrayObject([pages_ref])
    if encrypted:
        writer.encrypt("synthetic-only-password")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def path(tenant):
    return f"{BASE}/{tenant['branch_id']}/agent"


def keyed(headers, key):
    return {**headers, "Idempotency-Key": key}


def upload(client, tenant, headers, *, key="pdf", version=1, data=None, filename="carta.pdf", mime="application/pdf"):
    return client.post(path(tenant) + "/menu-pdf", headers=keyed(headers, key),
                       data={"expected_version": version},
                       files={"file": (filename, pdf_bytes() if data is None else data, mime)})


def stored_pdf(tenant):
    with SessionLocal() as db:
        return dict(db.get(Branch, tenant["branch_id"]).agent_menu_pdf or {})


def test_pdf_lifecycle_original_bytes_safe_metadata_and_replays(client, tenant, auth_headers, tmp_path):
    profile = client.get(path(tenant), headers=auth_headers).json()
    assert profile["menu_pdf"] is None
    assert client.get(path(tenant) + "/menu-pdf", headers=auth_headers).status_code == 404
    original = pdf_bytes(title="Carta original sintética")
    saved = upload(client, tenant, auth_headers, data=original, filename="C:\\private\\Carta Perú.pdf")
    assert saved.status_code == 200, saved.text
    profile = saved.json()
    assert profile["version"] == 2
    assert profile["menu_pdf"] == {"filename": "Carta Perú.pdf", "size_bytes": len(original),
                                   "url": path(tenant) + "/menu-pdf?v=2"}
    assert "path" not in profile["menu_pdf"]
    downloaded = client.get(profile["menu_pdf"]["url"], headers=auth_headers)
    assert downloaded.status_code == 200
    assert downloaded.content == original
    assert downloaded.headers["content-type"] == "application/pdf"
    assert downloaded.headers["x-content-type-options"] == "nosniff"
    assert "no-store" in downloaded.headers["cache-control"]
    disposition = downloaded.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="carta.pdf"; filename*=UTF-8\'\'')
    assert unquote(disposition.split("UTF-8''", 1)[1]) == "Carta Perú.pdf"
    replayed = upload(client, tenant, auth_headers, data=original, filename="C:\\private\\Carta Perú.pdf")
    assert replayed.json() == profile
    assert len(list(tmp_path.iterdir())) == 1
    assert upload(client, tenant, auth_headers, filename="otra.pdf").status_code == 409
    assert upload(client, tenant, auth_headers, key="stale", version=1).status_code == 409
    replaced = upload(client, tenant, auth_headers, key="replace", version=2, data=pdf_bytes(pages=2))
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["version"] == 3
    assert len(list(tmp_path.iterdir())) == 1
    deleted = client.delete(path(tenant) + "/menu-pdf?expected_version=3", headers=keyed(auth_headers, "delete"))
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["menu_pdf"] is None
    assert deleted.json()["version"] == 4
    assert not list(tmp_path.iterdir())
    assert client.delete(path(tenant) + "/menu-pdf?expected_version=3", headers=keyed(auth_headers, "delete")).json() == deleted.json()
    assert client.delete(path(tenant) + "/menu-pdf?expected_version=4", headers=keyed(auth_headers, "missing")).status_code == 404
    assert client.get(path(tenant) + "/menu-pdf", headers=auth_headers).status_code == 404
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(AuditEvent)) == 3
        assert db.scalar(select(func.count()).select_from(IdempotencyRecord)) == 3
        assert all("path" not in str(row.payload) for row in db.scalars(select(AuditEvent)))


def test_pdf_keeps_ten_images_legacy_qr_and_conversation_draft(client, tenant, auth_headers):
    from test_escalar_integrations import create_credential, integration_headers
    image = io.BytesIO()
    Image.new("RGB", (32, 32), "red").save(image, "PNG")
    version = 1
    for index in range(10):
        result = client.post(path(tenant) + "/images", headers=keyed(auth_headers, f"image-{index}"),
                             data={"expected_version": version}, files={"file": ("carta.png", image.getvalue(), "image/png")})
        assert result.status_code == 200, result.text
        version = result.json()["version"]
    qr = client.post(path(tenant) + "/yape-qr", headers=keyed(auth_headers, "qr"),
                     data={"expected_version": version}, files={"file": ("qr.png", image.getvalue(), "image/png")})
    version = qr.json()["version"]
    with SessionLocal.begin() as db:
        branch = db.get(Branch, tenant["branch_id"])
        before = (branch.agent_menu_images, branch.menu_card_storage_path, branch.yape_qr_storage_path)
        draft = AgentConversation(business_id=tenant["business_id"], branch_id=tenant["branch_id"], sender="synthetic-menu-pdf",
                                  state={"items": [{"product_id": tenant["product_id"], "quantity": 2}], "status": "draft"}, pending=[{"kind": "payment_choice"}])
        db.add(draft)
        db.flush()
        draft_id = draft.id
        draft_before = (draft.state, draft.pending, draft.version)
    saved = upload(client, tenant, auth_headers, version=version)
    assert saved.status_code == 200, saved.text
    assert len(saved.json()["images"]) == 10
    ids = [item["id"] for item in saved.json()["images"]]
    ordered = client.patch(path(tenant), headers=keyed(auth_headers, "reorder"),
                           json={"expected_version": saved.json()["version"], "image_order": ids[::-1]})
    assert ordered.status_code == 200
    assert ordered.json()["menu_pdf"]["filename"] == "carta.pdf"
    legacy = client.post(f"/api/v1/branches/{tenant['branch_id']}/menu-card", headers=auth_headers,
                         files={"file": ("legacy.png", image.getvalue(), "image/png")})
    assert legacy.status_code == 200
    assert client.get(path(tenant), headers=auth_headers).json()["menu_pdf"]["filename"] == "carta.pdf"
    credential = create_credential(client, tenant, auth_headers, ["menu:read"])
    headers = integration_headers(credential["token"])
    context = client.get("/api/v1/integrations/context", headers=headers).json()["branch"]
    assert len(context["menu_images"]) == 10
    assert context["menu_pdf"] == {"filename": "carta.pdf", "size_bytes": len(pdf_bytes()),
                                   "url": "/api/v1/integrations/context/menu-card?prefer_pdf=true"}
    assert client.get(context["menu_images"][0]["url"], headers=headers).content == image.getvalue()
    # A settings user session is required; the isolated environment has no JWT provider.
    assert client.get(path(tenant) + "/menu-pdf", headers=headers).status_code in {401, 403, 503}
    with SessionLocal() as db:
        branch = db.get(Branch, tenant["branch_id"])
        assert branch.yape_qr_storage_path == before[2]
        assert len(branch.agent_menu_images) == len(before[0])
        assert [item["id"] for item in branch.agent_menu_images] == ids[::-1]
        draft = db.get(AgentConversation, draft_id)
        assert (draft.state, draft.pending, draft.version) == draft_before


@pytest.mark.parametrize("data,mime,status", [
    (b"", "application/pdf", 422),
    (b"text", "text/plain", 415),
    (b"not a PDF", "application/pdf", 422),
    (b"%PDF-1.7\ninvalid structure\n%%EOF", "application/pdf", 422),
    (b"%PDF-" + b"x" * pdf_validation.MENU_PDF_MAX_BYTES, "application/pdf", 413),
], ids=["empty", "wrong-mime", "spoof", "malformed", "oversized"])
def test_invalid_upload_preserves_existing_pdf(client, tenant, auth_headers, tmp_path, data, mime, status):
    saved = upload(client, tenant, auth_headers)
    old = stored_pdf(tenant)
    rejected = upload(client, tenant, auth_headers, key="invalid", version=2, data=data, mime=mime)
    assert rejected.status_code == status, rejected.text
    assert stored_pdf(tenant) == old
    assert len(list(tmp_path.iterdir())) == 1
    assert client.get(path(tenant), headers=auth_headers).json() == saved.json()


@pytest.mark.parametrize("options", [{"pages": 0}, {"encrypted": True}, {"cyclic": True}])
def test_parser_rejects_no_pages_encryption_and_cyclic_page_tree(client, tenant, auth_headers, tmp_path, options):
    rejected = upload(client, tenant, auth_headers, data=pdf_bytes(**options))
    assert rejected.status_code == 422, rejected.text
    assert stored_pdf(tenant) == {}
    assert not list(tmp_path.iterdir())


def test_upload_and_delete_rollback_keep_prior_pdf(client, tenant, auth_headers, monkeypatch, tmp_path):
    saved = upload(client, tenant, auth_headers)
    old = stored_pdf(tenant)
    old_content = client.get(saved.json()["menu_pdf"]["url"], headers=auth_headers).content
    original_commit = agent.commit
    def fail_commit(*args):
        raise HTTPException(503, "Synthetic commit failure")
    monkeypatch.setattr(agent, "commit", fail_commit)
    assert upload(client, tenant, auth_headers, key="replace", version=2, data=pdf_bytes(pages=2)).status_code == 503
    assert stored_pdf(tenant) == old
    assert len(list(tmp_path.iterdir())) == 1
    assert client.delete(path(tenant) + "/menu-pdf?expected_version=2", headers=keyed(auth_headers, "delete")).status_code == 503
    assert stored_pdf(tenant) == old
    assert client.get(saved.json()["menu_pdf"]["url"], headers=auth_headers).content == old_content
    monkeypatch.setattr(agent, "commit", original_commit)
    retried = upload(client, tenant, auth_headers, key="replace", version=2, data=pdf_bytes(pages=2))
    assert retried.status_code == 200
    assert retried.json()["version"] == 3
    assert len(list(tmp_path.iterdir())) == 1


def test_isolation_permissions_keys_and_query_version(client, tenant, auth_headers):
    assert upload(client, tenant, {}).status_code == 401
    cashier = {**auth_headers, "X-Dev-Role": "cashier"}
    assert upload(client, tenant, cashier).status_code == 403
    assert client.get(path(tenant), headers=cashier).status_code == 403
    other = {**tenant, "branch_id": tenant["other_branch_id"]}
    assert upload(client, other, auth_headers).status_code in {403, 404}
    assert client.get(path(other) + "/menu-pdf", headers=auth_headers).status_code in {403, 404}
    assert client.delete(path(other) + "/menu-pdf?expected_version=1", headers=keyed(auth_headers, "delete-other")).status_code in {403, 404}
    assert client.post(path(tenant) + "/menu-pdf", headers=auth_headers, data={"expected_version": 1},
                       files={"file": ("carta.pdf", pdf_bytes(), "application/pdf")}).status_code == 422
    assert upload(client, tenant, auth_headers, version=0).status_code == 422
    assert client.delete(path(tenant) + "/menu-pdf", headers=keyed(auth_headers, "missing-version")).status_code == 422
    assert client.delete(path(tenant) + "/menu-pdf?expected_version=0", headers=keyed(auth_headers, "bad-version")).status_code == 422


def test_shared_stored_object_not_deleted_until_unreferenced(client, tenant, auth_headers, tmp_path):
    assert upload(client, tenant, auth_headers).status_code == 200
    old = stored_pdf(tenant)
    with SessionLocal.begin() as db:
        db.get(Branch, tenant["other_branch_id"]).agent_menu_pdf = old
    assert upload(client, tenant, auth_headers, key="replace", version=2, data=pdf_bytes(pages=2)).status_code == 200
    assert len(list(tmp_path.iterdir())) == 2


def test_parser_timeout_is_definitive_and_does_not_persist(client, tenant, auth_headers, monkeypatch, tmp_path):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
    monkeypatch.setattr(pdf_validation.subprocess, "run", timeout)
    rejected = upload(client, tenant, auth_headers)
    assert rejected.status_code == 422
    assert stored_pdf(tenant) == {}
    assert not list(tmp_path.iterdir())


def test_filename_removes_paths_controls_and_header_characters():
    assert pdf_validation.menu_pdf_filename('../../Carta\r\n" Perú.PDF') == "Carta_ Perú.pdf"
    assert pdf_validation.menu_pdf_filename("..\\..\\carta.pdf") == "carta.pdf"
    assert pdf_validation.menu_pdf_filename("\x00.pdf") == "carta.pdf"
    assert len(pdf_validation.menu_pdf_filename("a" * 400 + ".pdf")) == 164
    assert len(pdf_validation.menu_pdf_filename("菜" * 160 + ".pdf").encode("utf-8")) <= 184


def test_additive_migration_preserves_all_existing_branch_data(tenant):
    migration = importlib.import_module("migrations.versions.20261119_0030_agent_menu_pdf")
    assert migration.down_revision == "20261020_0029"
    with SessionLocal.begin() as db:
        branch = db.get(Branch, tenant["branch_id"])
        branch.agent_menu_images = [{"id": "old-a", "path": "old-a.png"}, {"id": "old-b", "path": "old-b.webp"}]
        branch.menu_card_storage_path = "old-a.png"
        branch.yape_qr_storage_path = "existing-qr.png"
        branch.agent_context_notes = "Existing notes must survive"
        branch.agent_name = "Existing assistant"
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE branches DROP COLUMN agent_menu_pdf"))
        before = [dict(row) for row in connection.execute(text("SELECT * FROM branches ORDER BY id")).mappings()]
        tables_before = sorted(inspect(connection).get_table_names())
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.upgrade()
        column = next(item for item in inspect(connection).get_columns("branches") if item["name"] == "agent_menu_pdf")
        assert column["nullable"]
        assert sorted(inspect(connection).get_table_names()) == tables_before
        rows = [dict(row) for row in connection.execute(text("SELECT * FROM branches ORDER BY id")).mappings()]
        assert all(row.pop("agent_menu_pdf") is None for row in rows)
        assert rows == before
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        assert [dict(row) for row in connection.execute(text("SELECT * FROM branches ORDER BY id")).mappings()] == before


@pytest.mark.skipif(engine.dialect.name != "postgresql", reason="Concurrent locking requires isolated PostgreSQL")
def test_concurrent_pdf_uploads_one_version_one_commit(tenant, auth_headers, tmp_path):
    barrier = Barrier(2)
    content = pdf_bytes()
    def concurrent_upload(index):
        with TestClient(app) as worker:
            barrier.wait(timeout=10)
            return upload(worker, tenant, auth_headers, key=f"concurrent-{index}", data=content)
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(concurrent_upload, range(2)))
    assert sorted(response.status_code for response in replies) == [200, 409]
    successful = next((index, reply.json()) for index, reply in enumerate(replies) if reply.status_code == 200)
    with TestClient(app) as worker:
        assert upload(worker, tenant, auth_headers, key=f"concurrent-{successful[0]}", data=content).json() == successful[1]
    assert len(list(tmp_path.iterdir())) == 1
    with SessionLocal() as db:
        assert db.get(Branch, tenant["branch_id"]).version == 2
        assert db.scalar(select(func.count()).select_from(AuditEvent)) == 1
        assert db.scalar(select(func.count()).select_from(IdempotencyRecord)) == 1


@pytest.mark.skipif(engine.dialect.name != "postgresql", reason="Shared event-loop locking requires isolated PostgreSQL")
@pytest.mark.parametrize("same_key", [False, True], ids=["stale-version", "exact-replay"])
def test_shared_event_loop_pdf_uploads_release_locks_before_cleanup(tenant, auth_headers, tmp_path, monkeypatch, same_key):
    original_store = agent.store_private_file
    original_delete = agent.delete_private_file
    original_writable = agent.writable
    storage_paths, deleted_paths = [], []
    content = pdf_bytes()
    monkeypatch.setattr(agent, "validate_menu_pdf", lambda *args: None)

    async def exercise():
        event_loop_thread = get_ident()
        storage_entered = 0
        both_storing = asyncio.Event()

        async def yielding_store(data, filename, mime, category):
            nonlocal storage_entered
            storage_entered += 1
            if storage_entered == 2:
                both_storing.set()
            await asyncio.wait_for(both_storing.wait(), timeout=5)
            stored = await original_store(data, filename, mime, category)
            storage_paths.append(stored)
            return stored

        def checked_writable(*args):
            assert get_ident() != event_loop_thread, "Lock waits must run outside the ASGI event loop"
            assert storage_entered == 2, "Both private stores must start before business locking"
            return original_writable(*args)

        async def checked_delete(storage_path):
            deleted_paths.append(storage_path)
            await original_delete(storage_path)

        monkeypatch.setattr(agent, "store_private_file", yielding_store)
        monkeypatch.setattr(agent, "writable", checked_writable)
        monkeypatch.setattr(agent, "delete_private_file", checked_delete)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as worker:
            async def send(index):
                key = "same-loop-shared" if same_key else f"same-loop-{index}"
                return await worker.post(path(tenant) + "/menu-pdf", headers=keyed(auth_headers, key),
                                         data={"expected_version": 1}, files={"file": ("carta.pdf", content, "application/pdf")})
            replies = await asyncio.wait_for(asyncio.gather(send(0), send(1)), timeout=10)
            assert sorted(reply.status_code for reply in replies) == ([200, 200] if same_key else [200, 409])
            winning_index = next(index for index, reply in enumerate(replies) if reply.status_code == 200)
            replayed = await send(winning_index)
            assert replayed.json() == replies[winning_index].json()
            assert storage_entered == 2, "An ordinary exact retry must return before storing another blob"
            if same_key:
                assert replies[0].json() == replies[1].json()

    asyncio.run(exercise())
    assert len(storage_paths) == 2
    assert len(deleted_paths) == 1
    assert deleted_paths[0] in storage_paths
    assert deleted_paths[0] != stored_pdf(tenant)["path"]
    assert len(list(tmp_path.iterdir())) == 1
    with SessionLocal() as db:
        assert db.get(Branch, tenant["branch_id"]).version == 2
        assert db.scalar(select(func.count()).select_from(AuditEvent)) == 1
        assert db.scalar(select(func.count()).select_from(IdempotencyRecord)) == 1


@pytest.mark.skipif(engine.dialect.name != "postgresql", reason="Legacy lock overlap requires isolated PostgreSQL")
@pytest.mark.parametrize("action", ["upload", "delete"])
def test_pdf_can_wait_for_legacy_image_on_same_event_loop(client, tenant, auth_headers, monkeypatch, action):
    if action == "delete":
        assert upload(client, tenant, auth_headers).status_code == 200
    original_store = agent.store_private_file
    original_writable = agent.writable
    image = io.BytesIO()
    Image.new("RGB", (32, 32), "red").save(image, "PNG")
    monkeypatch.setattr(agent, "validate_menu_pdf", lambda *args: None)

    async def exercise():
        event_loop_thread = get_ident()
        legacy_storing = asyncio.Event()
        pdf_waiting = asyncio.Event()
        version = 2 if action == "delete" else 1

        async def yielding_store(data, filename, mime, category):
            if category == "agent-media":
                legacy_storing.set()
                await asyncio.wait_for(pdf_waiting.wait(), timeout=5)
            return await original_store(data, filename, mime, category)

        def checked_writable(*args):
            if get_ident() != event_loop_thread:
                event_loop.call_soon_threadsafe(pdf_waiting.set)
            else:
                assert not legacy_storing.is_set(), "The PDF lock wait must run in a worker"
            return original_writable(*args)

        event_loop = asyncio.get_running_loop()
        monkeypatch.setattr(agent, "store_private_file", yielding_store)
        monkeypatch.setattr(agent, "writable", checked_writable)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as worker:
            legacy = asyncio.create_task(worker.post(path(tenant) + "/images", headers=keyed(auth_headers, "legacy-overlap"),
                                        data={"expected_version": version}, files={"file": ("menu.png", image.getvalue(), "image/png")}))
            await asyncio.wait_for(legacy_storing.wait(), timeout=5)
            if action == "delete":
                document = worker.delete(path(tenant) + f"/menu-pdf?expected_version={version}", headers=keyed(auth_headers, "pdf-overlap"))
            else:
                document = worker.post(path(tenant) + "/menu-pdf", headers=keyed(auth_headers, "pdf-overlap"),
                                       data={"expected_version": version}, files={"file": ("carta.pdf", pdf_bytes(), "application/pdf")})
            legacy_reply, pdf_reply = await asyncio.wait_for(asyncio.gather(legacy, document), timeout=10)
            assert legacy_reply.status_code == 200, legacy_reply.text
            assert pdf_reply.status_code == 409, pdf_reply.text

    asyncio.run(exercise())
    with SessionLocal() as db:
        branch = db.get(Branch, tenant["branch_id"])
        assert len(branch.agent_menu_images) == 1
        assert bool(branch.agent_menu_pdf) == (action == "delete")
