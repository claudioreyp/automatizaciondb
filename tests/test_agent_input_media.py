"""Durable private ingress and scoped recovery; SQLite/TestClient/temp files only."""
from copy import deepcopy
from datetime import timedelta
from io import BytesIO
from pathlib import Path
import base64
import hashlib
import asyncio
from threading import Event, Lock, get_ident
import time

import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from conftest import clean_database, client, tenant, auth_headers
from test_restaurant_agent_boundary import agent_headers, claim, SENDER
from app import agent_input_media as media_api
from app.config import get_settings
from app.database import SessionLocal, engine
from app.models import AgentConversation, IdempotencyRecord, PaymentEvidence, Payment, KitchenTicket, IntegrationCredential, AgentAttentionRequest, Order, utcnow
from app.auth import hash_integration_token
from app.schemas import RESTAURANT_AGENT_SCOPES

BASE = "/api/v1/integrations/conversations"


@pytest.fixture(autouse=True)
def isolated_storage(monkeypatch, tmp_path):
    assert engine.url.database == ":memory:"
    settings = get_settings()
    monkeypatch.setattr(settings, "upload_dir", tmp_path)
    monkeypatch.setattr(settings, "supabase_url", None)
    monkeypatch.setattr(settings, "supabase_service_role_key", None)
    yield tmp_path


def png(color="red", size=(3, 3)):
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


def stage(client, headers, message="image1", *, sender=SENDER, content=None, mime="image/png"):
    return client.post(BASE + "/media", headers={**headers, "Idempotency-Key": "stage-" + message},
        data={"sender": sender, "channel": "whatsapp", "message_id": message},
        files={"file": ("input.png", content if content is not None else png(), mime)})


def claim_media(client, headers, descriptor, message="image1", *, sender=SENDER):
    response = client.post(BASE + "/claim", headers={**headers, "Idempotency-Key": "claim-" + message},
        json={"sender": sender, "message_id": message, "message": {"text": "", "media": descriptor}})
    assert response.status_code == 200, response.text
    return response.json()


def leased_headers(headers, lease):
    return {**headers, "X-Agent-Conversation": lease["conversation_id"], "X-Agent-Lease": lease["lease_token"]}


def disposition(descriptor, message="image1"):
    return {"source_message_id": message, "media_ref": descriptor["media_ref"], "sha256": descriptor["sha256"],
        "content_sha256": descriptor["content_sha256"], "disposition": "consumed"}


def held(descriptor, message="image1"):
    return {"source_turn_id": message, "media_ref": descriptor["media_ref"], "image_sha256": descriptor["sha256"],
        "image_content_sha256": descriptor["content_sha256"], "image_mime": descriptor["mime_type"], "analysis": {"kind": "payment_receipt"}}


def finish(client, headers, lease, *, state=None, media_disposition=None, complete=True, key="finish"):
    body = {"lease_token": lease["lease_token"], "expected_version": lease["version"],
        "state": state or {}, "response": {"actions": [{"type": "text", "text": "Recibido para revisión"}]}, "complete": complete}
    if media_disposition is not None:
        body["media_disposition"] = media_disposition
    return client.post(BASE + f'/{lease["conversation_id"]}/commit', headers={**headers, "Idempotency-Key": key}, json=body)


def read(client, headers, lease, descriptor, source="image1", **override):
    body = {"lease_token": lease["lease_token"], "expected_version": lease["version"], "source_message_id": source,
        "media_ref": descriptor["media_ref"], "sha256": descriptor["sha256"], "content_sha256": descriptor["content_sha256"], **override}
    return client.post(BASE + f'/{lease["conversation_id"]}/media/read', headers=headers, json=body)


def stored(message="image1", sender=SENDER):
    with SessionLocal() as db:
        row = db.scalar(select(AgentConversation).where(AgentConversation.sender == sender))
        record = media_api.media_record(db, row, message)
        return deepcopy(record.response_body)


def test_image_is_durable_before_enqueue_and_scoped_read_uses_persisted_bytes(client, agent_headers):
    response = stage(client, agent_headers)
    assert response.status_code == 200, response.text
    descriptor = response.json()
    assert set(descriptor) == set(media_api.PUBLIC_FIELDS)
    assert descriptor["sha256"] == hashlib.sha256(base64.b64encode(png())).hexdigest()
    assert descriptor["content_sha256"] == hashlib.sha256(png()).hexdigest()
    assert descriptor["sha256"] != descriptor["content_sha256"]
    assert stored()["status"] == "ready"
    assert Path(stored()["storage_path"]).read_bytes() == png()
    lease = claim_media(client, agent_headers, descriptor)
    result = read(client, agent_headers, lease, descriptor)
    assert result.status_code == 200 and base64.b64decode(result.json()["data"]) == png(), result.text
    assert result.headers["cache-control"] == "no-store"
    assert "storage_path" not in result.json()


@pytest.mark.parametrize("created_by_other_ingress", [False, True])
def test_first_conversation_creation_releases_branch_before_waiting_for_conversation(created_by_other_ingress):
    """Generic-profile SQL order; real PostgreSQL lock interleaving has its own test."""
    from types import SimpleNamespace
    from sqlalchemy.dialects import postgresql
    from app.agent_runtime_api import lock_or_create_conversation
    events = []
    existing = SimpleNamespace(id="other-ingress")
    replies = iter([None, 1, existing if created_by_other_ingress else None, existing])
    class FakeSession:
        def scalar(self, statement):
            events.append(str(statement.compile(dialect=postgresql.dialect())))
            return next(replies)
        def scalars(self, statement):
            value = self.scalar(statement)
            return [] if value is None else [value]
        def add(self, row):
            events.append("ADD")
        def flush(self):
            events.append("FLUSH")
        def commit(self):
            events.append("COMMIT")
    result = lock_or_create_conversation(FakeSession(), SimpleNamespace(business_id=1, branch_id=2, profile="generic"), "whatsapp", SENDER)
    assert result is existing
    selects = [value for value in events if value.startswith("SELECT")]
    assert "agent_conversations" in selects[0] and "FOR UPDATE" in selects[0]
    assert "branches" in selects[1] and "FOR UPDATE" in selects[1]
    assert "agent_conversations" in selects[2] and "FOR UPDATE" not in selects[2]
    assert events.index("COMMIT") < events.index(selects[3], 3)
    assert "agent_conversations" in selects[3] and "FOR UPDATE" in selects[3]


def test_private_transaction_keeps_one_asgi_loop_responsive_while_worker_waits_on_lock():
    from app.agent_blocking_io import private_transaction
    lock, first_entered = Lock(), Event()
    @private_transaction
    async def operation(first):
        assert lock.acquire(timeout=1), "Worker lock timed out"
        try:
            if first:
                first_entered.set()
                await asyncio.sleep(0.15)
            return get_ident()
        finally:
            lock.release()
    async def run():
        original_thread = get_ident()
        first = asyncio.create_task(operation(True))
        while not first_entered.is_set():
            await asyncio.sleep(0.005)
        second = asyncio.create_task(operation(False))
        start = time.monotonic()
        await asyncio.sleep(0.025)
        assert time.monotonic() - start < 0.5, "Synchronous lock starved the caller loop"
        threads = await asyncio.gather(first, second)
        assert all(thread != original_thread for thread in threads)
    asyncio.run(run())


def test_attention_commit_worker_and_broadcast_use_separate_correct_loops(monkeypatch):
    from app import agent_attention
    from fastapi import Response
    observed = {}
    async def transaction(*args, **kwargs):
        observed["transaction_thread"] = get_ident()
        observed["transaction_loop"] = asyncio.get_running_loop()
        return {"id": "synthetic", "order_id": None}, 1, True
    async def broadcast(*args):
        observed["broadcast_thread"] = get_ident()
        observed["broadcast_loop"] = asyncio.get_running_loop()
    monkeypatch.setattr(agent_attention, "_request_attention_transaction", transaction)
    monkeypatch.setattr(agent_attention.hub, "broadcast", broadcast)
    async def run():
        observed["original_thread"] = get_ident()
        observed["original_loop"] = asyncio.get_running_loop()
        await agent_attention.request_attention(None, Response(), idempotency_key="synthetic", integration=None, db=None)
    asyncio.run(run())
    assert observed["transaction_thread"] != observed["original_thread"]
    assert observed["transaction_loop"] is not observed["original_loop"]
    assert observed["broadcast_thread"] == observed["original_thread"]
    assert observed["broadcast_loop"] is observed["original_loop"]


def test_missing_staging_cannot_be_accepted(client, agent_headers):
    result = client.post(BASE + "/enqueue", headers={**agent_headers, "Idempotency-Key": "missing"},
        json={"sender": SENDER, "message_id": "missing", "message": {"media": {"sha256": "a" * 64, "mime_type": "image/png"}}})
    assert result.status_code == 409
    with SessionLocal() as db:
        assert not db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.scope.like("agent-input:%")))


@pytest.mark.parametrize("mutation", ["bytes", "mime"])
def test_same_message_conflict_never_replaces_reserved_object(client, agent_headers, mutation):
    first = stage(client, agent_headers).json()
    response = stage(client, agent_headers, content=png("blue") if mutation == "bytes" else png(),
        mime="image/jpeg" if mutation == "mime" else "image/png")
    assert response.status_code in {409, 422}
    assert stored()["media_ref"] == first["media_ref"]
    assert Path(stored()["storage_path"]).read_bytes() == png()


def test_duplicate_stage_reuses_one_descriptor_and_one_object(client, agent_headers, isolated_storage):
    first = stage(client, agent_headers).json()
    assert stage(client, agent_headers).json() == first
    with SessionLocal() as db:
        assert len(list(db.scalars(select(IdempotencyRecord).where(IdempotencyRecord.scope.like("agent-input-media:%"))))) == 1
    assert len(list(isolated_storage.rglob("*.png"))) == 1


def test_storage_success_lost_ready_commit_reuses_reserved_path_and_quota(client, agent_headers, isolated_storage, monkeypatch):
    original = Session.commit
    def lose_ready_commit(session):
        if any(isinstance(row, IdempotencyRecord) and row.scope.startswith("agent-input-media:")
               and row.response_body.get("status") == "ready" for row in session.dirty):
            raise RuntimeError("synthetic lost ready commit")
        return original(session)
    monkeypatch.setattr(Session, "commit", lose_ready_commit)
    with pytest.raises(RuntimeError, match="synthetic"):
        stage(client, agent_headers)
    first = stored()
    assert first["status"] == "reserved" and Path(first["storage_path"]).exists()
    monkeypatch.setattr(Session, "commit", original)
    retry = stage(client, agent_headers)
    assert retry.status_code == 200 and retry.json()["media_ref"] == first["media_ref"], retry.text
    assert stored()["status"] == "ready"
    assert len(list(isolated_storage.rglob("*.png"))) == 1


def test_storage_failure_keeps_reservation_counted_and_denies_ack(client, agent_headers, monkeypatch):
    async def unavailable(*args):
        raise OSError("synthetic unavailable")
    monkeypatch.setattr(media_api, "store_reserved_file", unavailable)
    monkeypatch.setattr(get_settings(), "agent_media_conversation_files", 1)
    result = stage(client, agent_headers)
    assert result.status_code == 503 and stored()["status"] == "reserved"
    assert stage(client, agent_headers, "image2").status_code == 429
    descriptor = media_api.public_descriptor(stored())
    result = client.post(BASE + "/enqueue", headers={**agent_headers, "Idempotency-Key": "unready"},
        json={"sender": SENDER, "message_id": "image1", "message": {"media": descriptor}})
    assert result.status_code == 409


@pytest.mark.parametrize("content,mime,status", [(b"not an image", "image/png", 422), (png(), "image/jpeg", 422)])
def test_invalid_images_rejected_before_reservation(client, agent_headers, content, mime, status):
    assert stage(client, agent_headers, content=content, mime=mime).status_code == status
    with SessionLocal() as db:
        assert not db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.scope.like("agent-input-media:%")))


def test_pixel_limit_and_size_limit_are_enforced_before_acceptance(client, agent_headers, monkeypatch):
    monkeypatch.setattr(get_settings(), "agent_media_max_pixels", 4)
    assert stage(client, agent_headers).status_code == 413
    monkeypatch.setattr(get_settings(), "agent_media_max_pixels", 100)
    monkeypatch.setattr(get_settings(), "agent_media_max_bytes", 5)
    assert stage(client, agent_headers).status_code == 413


@pytest.mark.parametrize("kind", ["token", "version", "source", "hash"])
def test_read_requires_current_lease_and_exact_current_or_held_reference(client, agent_headers, kind):
    descriptor = stage(client, agent_headers).json()
    lease = claim_media(client, agent_headers, descriptor)
    overrides = {"token": {"lease_token": "wrong-lease-token-1234567890"}, "version": {"expected_version": lease["version"] + 1},
        "source": {"source_message_id": "other"}, "hash": {"content_sha256": "0" * 64}}
    assert read(client, agent_headers, lease, descriptor, **overrides[kind]).status_code in {403, 409}


def test_cross_chat_reference_is_denied(client, agent_headers):
    descriptor = stage(client, agent_headers).json()
    lease = claim(client, agent_headers, "other-chat", sender="51988800002").json()
    assert read(client, agent_headers, lease, descriptor).status_code == 403


def test_other_tenant_and_generic_profile_cannot_read_or_stage_agent_media(client, agent_headers, tenant):
    descriptor = stage(client, agent_headers).json()
    lease = claim_media(client, agent_headers, descriptor)
    with SessionLocal.begin() as db:
        for profile, business, branch, token in [("restaurant_agent", tenant["other_business_id"], tenant["other_branch_id"], "other-agent"),
                ("generic", tenant["business_id"], tenant["branch_id"], "generic-agent")]:
            db.add(IntegrationCredential(business_id=business, branch_id=branch, profile=profile, name=token,
                token_prefix=token, token_hash=hash_integration_token(token), scopes=sorted(RESTAURANT_AGENT_SCOPES)))
    assert read(client, {"Authorization": "Bearer other-agent"}, lease, descriptor).status_code == 404
    generic = {"Authorization": "Bearer generic-agent"}
    assert stage(client, generic, "generic-image").status_code == 403
    assert read(client, generic, lease, descriptor).status_code == 403


def test_held_image_survives_completed_turn_then_retires_after_resolved(client, agent_headers):
    descriptor = stage(client, agent_headers).json()
    lease = claim_media(client, agent_headers, descriptor)
    result = finish(client, agent_headers, lease, state={"receipt_submission": held(descriptor)}, media_disposition=disposition(descriptor))
    assert result.status_code == 200 and stored()["status"] == "ready", result.text
    next_lease = claim(client, agent_headers, "target-answer").json()
    assert read(client, agent_headers, next_lease, descriptor).status_code == 200
    assert finish(client, agent_headers, next_lease, key="resolved").status_code == 200
    assert stored()["status"] == "released" and not Path(stored()["storage_path"]).exists()


@pytest.mark.parametrize("location", ["pending", "quote", "binding"])
def test_all_durable_operation_references_prevent_retirement(client, agent_headers, location):
    descriptor = stage(client, agent_headers).json()
    lease = claim_media(client, agent_headers, descriptor)
    body = {"whatsapp_message_id": "image1", "image_media_ref": descriptor["media_ref"], "image_sha256": descriptor["sha256"],
        "image_content_sha256": descriptor["content_sha256"], "image_mime": descriptor["mime_type"]}
    operation = {"kind": "evidence", "body": body, "turn_id": "image1"}
    state = {"pending": operation} if location == "pending" else {"pending_fulfillment_condition": {"quote_response": {"operation": operation}}}
    if location == "binding":
        state = {"pending": {"kind": "addition", "receipt_binding": held(descriptor)}}
    response = finish(client, agent_headers, lease, state=state, media_disposition=disposition(descriptor))
    assert response.status_code == 200 and stored()["status"] == "ready", response.text


def test_completed_without_consumption_never_loses_current_image(client, agent_headers):
    descriptor = stage(client, agent_headers).json()
    lease = claim_media(client, agent_headers, descriptor)
    assert finish(client, agent_headers, lease).status_code == 200
    assert stored()["status"] == "ready" and Path(stored()["storage_path"]).exists()


def test_current_new_image_remains_readable_after_old_recovery_checkpoint(client, agent_headers):
    old = stage(client, agent_headers).json()
    first = claim_media(client, agent_headers, old)
    assert finish(client, agent_headers, first, state={"receipt_submission": held(old)}, media_disposition=disposition(old)).status_code == 200
    new = stage(client, agent_headers, "new-image", content=png("blue")).json()
    current = claim_media(client, agent_headers, new, "new-image")
    # Executor confirms old recovery but deliberately keeps current input incomplete.
    checkpoint = finish(client, agent_headers, current, complete=False, key="old-recovered")
    assert checkpoint.status_code == 200
    current["version"] = checkpoint.json()["version"]
    assert stored()["status"] == "ready" and stored("new-image")["status"] == "ready"
    assert read(client, agent_headers, current, new, "new-image").status_code == 200
    assert finish(client, agent_headers, current, media_disposition=disposition(new, "new-image"), key="new-consumed").status_code == 200
    assert stored()["status"] == "released" and stored("new-image")["status"] == "released"


def test_queued_image_is_not_retired_when_previous_input_finishes(client, agent_headers):
    current = claim(client, agent_headers, "earlier").json()
    descriptor = stage(client, agent_headers).json()
    queued = claim_media(client, agent_headers, descriptor)
    assert queued["status"] == "queued"
    assert finish(client, agent_headers, current).status_code == 200
    assert stored()["status"] == "ready"
    next_result = client.post(BASE + f'/{current["conversation_id"]}/next', headers={**agent_headers, "Idempotency-Key": "next"}).json()
    assert next_result["message_id"] == "image1"
    assert read(client, agent_headers, next_result, descriptor).status_code == 200


def test_consumed_image_retires_and_duplicate_never_revives_it(client, agent_headers):
    descriptor = stage(client, agent_headers).json()
    lease = claim_media(client, agent_headers, descriptor)
    result = finish(client, agent_headers, lease, media_disposition=disposition(descriptor))
    assert result.status_code == 200 and stored()["status"] == "released", result.text
    assert stage(client, agent_headers).json() == descriptor
    duplicate = claim_media(client, agent_headers, descriptor)
    assert duplicate["duplicate"] is True
    assert not Path(stored()["storage_path"]).exists()


def test_late_stage_retry_cannot_rewrite_a_retiring_object(client, agent_headers, monkeypatch):
    descriptor = stage(client, agent_headers).json()
    lease = claim_media(client, agent_headers, descriptor)
    original = media_api.delete_private_file
    async def delayed_delete(path):
        # This is the durable boundary before deletion, not a timing assertion.
        assert stored()["status"] == "retiring"
        raise OSError("synthetic delayed delete")
    monkeypatch.setattr(media_api, "delete_private_file", delayed_delete)
    assert finish(client, agent_headers, lease, media_disposition=disposition(descriptor)).status_code == 200
    async def forbidden_write(*args):
        raise AssertionError("retiring image must not be rewritten")
    monkeypatch.setattr(media_api, "store_reserved_file", forbidden_write)
    assert stage(client, agent_headers).json() == descriptor
    assert stored()["status"] == "retiring"
    monkeypatch.setattr(media_api, "delete_private_file", original)


def test_invalid_disposition_does_not_release_other_source(client, agent_headers):
    descriptor = stage(client, agent_headers).json()
    lease = claim_media(client, agent_headers, descriptor)
    bad = {**disposition(descriptor), "source_message_id": "someone-else"}
    assert finish(client, agent_headers, lease, media_disposition=bad).status_code == 409
    assert stored()["status"] == "ready"


def test_retirement_failure_remains_counted_and_next_commit_retries_bounded(client, agent_headers, monkeypatch):
    descriptor = stage(client, agent_headers).json()
    lease = claim_media(client, agent_headers, descriptor)
    remove = media_api.delete_private_file
    async def fail_delete(*args):
        raise OSError("synthetic deletion failure")
    monkeypatch.setattr(media_api, "delete_private_file", fail_delete)
    monkeypatch.setattr(get_settings(), "agent_media_conversation_files", 1)
    assert finish(client, agent_headers, lease, media_disposition=disposition(descriptor)).status_code == 200
    assert stored()["status"] == "retiring"
    assert stage(client, agent_headers, "image2").status_code == 429
    monkeypatch.setattr(media_api, "delete_private_file", remove)
    later = claim(client, agent_headers, "cleanup-next").json()
    assert finish(client, agent_headers, later, key="cleanup-next").status_code == 200
    assert stored()["status"] == "released"
    assert stage(client, agent_headers, "image2").status_code == 200


def test_stale_worker_or_new_turn_cannot_resurrect_retired_reference(client, agent_headers):
    descriptor = stage(client, agent_headers).json()
    lease = claim_media(client, agent_headers, descriptor)
    assert finish(client, agent_headers, lease, media_disposition=disposition(descriptor)).status_code == 200
    assert finish(client, agent_headers, lease, state={"receipt_submission": held(descriptor)}, key="stale").status_code == 409
    current = claim(client, agent_headers, "new-current").json()
    assert finish(client, agent_headers, current, state={"receipt_submission": held(descriptor)}, key="resurrect").status_code == 409
    assert stored()["status"] == "released"


def test_branch_quota_counts_other_chat_reservations_before_acceptance(client, agent_headers, monkeypatch):
    monkeypatch.setattr(get_settings(), "agent_media_branch_files", 1)
    assert stage(client, agent_headers).status_code == 200
    assert stage(client, agent_headers, "second-chat", sender="51988800002").status_code == 429


def payment_setup(client, agent_headers, tenant, *, legacy=False):
    descriptor = stage(client, agent_headers).json()
    lease = claim_media(client, agent_headers, descriptor)
    headers = leased_headers(agent_headers, lease)
    order_response = client.post("/api/v1/integrations/orders/draft", headers={**headers, "Idempotency-Key": "order"},
        json={"branch_id": tenant["branch_id"], "channel": "takeaway", "payment_method": "yape", "quoted_total": 20,
            "whatsapp_chat_id": SENDER, "customer_phone": SENDER, "items": [{"product_id": tenant["product_id"], "quantity": 1}]})
    assert order_response.status_code == 201, order_response.text
    order_id = order_response.json()["id"]
    fields = {"provider": "yape", "sender": SENDER, "looks_like_payment_receipt": "true", "whatsapp_message_id": "image1", "amount_detected": "20", "analysis_warnings": "[]"}
    body = {**fields, "image_sha256": descriptor["sha256"], "image_mime": descriptor["mime_type"]}
    if not legacy:
        body.update({"image_content_sha256": descriptor["content_sha256"], "image_media_ref": descriptor["media_ref"]})
    pending = {"kind": "evidence", "method": "POST", "path": f"/integrations/orders/{order_id}/payment-evidence",
        "body": body, "idempotency_key": "evidence-original", "turn_id": "image1"}
    checkpoint = finish(client, agent_headers, lease, state={"order_id": order_id, "pending": pending}, complete=False, key="pending")
    assert checkpoint.status_code == 200, checkpoint.text
    lease["version"] = checkpoint.json()["version"]
    return descriptor, lease, headers, order_id, fields, pending


def reconcile(client, headers, order_id, key="evidence-original"):
    return client.post(f"/api/v1/integrations/orders/{order_id}/payment-evidence/reconcile", headers=headers,
        json={"idempotency_key": key})


@pytest.mark.parametrize("legacy", [False, True])
def test_exact_saved_evidence_reconciles_without_runtime_cache_or_new_upload(client, tenant, agent_headers, legacy):
    descriptor, lease, headers, order_id, fields, pending = payment_setup(client, agent_headers, tenant, legacy=legacy)
    uploaded = client.post(f"/api/v1/integrations/orders/{order_id}/payment-evidence", headers={**headers, "Idempotency-Key": "evidence-original"},
        data=fields, files={"file": ("evidence.png", png(), "image/png")})
    assert uploaded.status_code == 201, uploaded.text
    if not legacy:
        Path(stored()["storage_path"]).unlink()  # Simulate missing staging; confirmed evidence is separate and intact.
    result = reconcile(client, headers, order_id)
    assert result.status_code == 200 and result.json() == {"status": "committed", "result": uploaded.json()}, result.text
    with SessionLocal() as db:
        assert db.query(PaymentEvidence).count() == 1 and db.query(Payment).count() == 0 and db.query(KitchenTicket).count() == 0


def test_unrecorded_evidence_returns_absent_without_claiming_payment(client, tenant, agent_headers):
    descriptor, lease, headers, order_id, fields, pending = payment_setup(client, agent_headers, tenant)
    result = reconcile(client, headers, order_id)
    assert result.status_code == 200 and result.json() == {"status": "not_recorded"}
    assert read(client, agent_headers, lease, descriptor).status_code == 200


@pytest.mark.parametrize("mismatch", ["key", "amount", "image", "source", "target"])
def test_saved_receipt_requires_original_pending_key_body_and_image(client, tenant, agent_headers, mismatch):
    descriptor, lease, headers, order_id, fields, pending = payment_setup(client, agent_headers, tenant)
    uploaded = client.post(f"/api/v1/integrations/orders/{order_id}/payment-evidence", headers={**headers, "Idempotency-Key": "evidence-original"},
        data=fields, files={"file": ("evidence.png", png(), "image/png")})
    assert uploaded.status_code == 201, uploaded.text
    if mismatch != "key":
        key, value = {"amount": ("amount_detected", "19"), "image": ("image_content_sha256", "0" * 64),
            "source": ("whatsapp_message_id", "another"), "target": ("payment_request_id", "999")}[mismatch]
        with SessionLocal.begin() as db:
            row = db.get(AgentConversation, lease["conversation_id"])
            state = deepcopy(row.state)
            state["pending"]["body"][key] = value
            row.state = state
    result = reconcile(client, headers, order_id, "wrong-key" if mismatch == "key" else "evidence-original")
    assert result.status_code == 409, result.text


def test_reconcile_does_not_accept_stale_lease(client, tenant, agent_headers):
    descriptor, lease, headers, order_id, fields, pending = payment_setup(client, agent_headers, tenant)
    with SessionLocal.begin() as db:
        db.get(AgentConversation, lease["conversation_id"]).lease_until = utcnow() - timedelta(seconds=1)
    assert reconcile(client, headers, order_id).status_code == 409


def late_attention(client, headers, descriptor, *, message="image1", key="late", **overrides):
    body = {"sender": SENDER, "order_id": None, "kind": "late_payment_receipt", "summary": "Comprobante solicitado antes del cierre, recibido después; revisar sin pedido.",
        "message_ids": [message], "attachments": [{"source_message_id": message, "media_ref": descriptor["media_ref"],
            "sha256": descriptor["sha256"], "content_sha256": descriptor["content_sha256"]}], **overrides}
    return client.post("/api/v1/integrations/attention-requests", headers={**headers, "Idempotency-Key": key}, json=body)


def test_late_receipt_creates_only_reviewable_attention_and_survives_resolution(client, tenant, agent_headers, auth_headers):
    descriptor = stage(client, agent_headers).json()
    lease = claim_media(client, agent_headers, descriptor)
    headers = leased_headers(agent_headers, lease)
    first = late_attention(client, headers, descriptor)
    assert first.status_code == 201, first.text
    attention = first.json()
    assert attention["order_id"] is None and attention["kind"] == "late_payment_receipt"
    assert "_media_bindings" not in attention["details"]
    assert "storage_path" not in first.text and "base64" not in first.text and "supabase://" not in first.text
    attachment = attention["details"]["attachments"][0]
    assert set(attachment) == {"media_ref", "mime_type", "size_bytes", "image_url"}
    replay = late_attention(client, headers, descriptor)
    assert replay.status_code == 200 and replay.json() == attention
    assert finish(client, agent_headers, lease, media_disposition=disposition(descriptor)).status_code == 200
    assert stored()["status"] == "attached" and Path(stored()["storage_path"]).exists()
    image = client.get(attachment["image_url"], headers=auth_headers)
    assert image.status_code == 200 and image.content == png() and image.headers["cache-control"] == "no-store"
    resolved = client.post(f'/api/v1/agent-attention/{attention["id"]}/resolve', headers={**auth_headers, "Idempotency-Key": "review"},
        json={"expected_version": attention["version"], "status": "resolved", "note": "Revisado por encargado sin registrar una venta."})
    assert resolved.status_code == 200, resolved.text
    assert client.get(attachment["image_url"], headers=auth_headers).content == png()
    assert stored()["status"] == "attached"
    with SessionLocal() as db:
        assert db.query(AgentAttentionRequest).count() == 1
        assert all(db.query(model).count() == 0 for model in [Order, PaymentEvidence, Payment, KitchenTicket])


def test_second_late_receipt_preserves_first_attachment_and_exact_key_conflict(client, tenant, agent_headers, auth_headers):
    first_descriptor = stage(client, agent_headers).json()
    first_lease = claim_media(client, agent_headers, first_descriptor)
    first = late_attention(client, leased_headers(agent_headers, first_lease), first_descriptor).json()
    assert finish(client, agent_headers, first_lease, media_disposition=disposition(first_descriptor)).status_code == 200
    second_descriptor = stage(client, agent_headers, "image2", content=png("blue")).json()
    second_lease = claim_media(client, agent_headers, second_descriptor, "image2")
    headers = leased_headers(agent_headers, second_lease)
    assert late_attention(client, headers, second_descriptor, message="image2", key="late").status_code == 409
    second = late_attention(client, headers, second_descriptor, message="image2", key="late2")
    assert second.status_code == 201 and second.json()["id"] == first["id"], second.text
    attachments = second.json()["details"]["attachments"]
    assert len(attachments) == 2
    assert client.get(attachments[0]["image_url"], headers=auth_headers).content == png()
    assert client.get(attachments[1]["image_url"], headers=auth_headers).content == png("blue")


@pytest.mark.parametrize("mutate", ["source", "hash", "reference", "order", "kind", "two"])
def test_late_receipt_rejects_foreign_or_invalid_attachment(client, agent_headers, mutate):
    descriptor = stage(client, agent_headers).json()
    lease = claim_media(client, agent_headers, descriptor)
    headers = leased_headers(agent_headers, lease)
    attachment = {"source_message_id": "image1", "media_ref": descriptor["media_ref"], "sha256": descriptor["sha256"], "content_sha256": descriptor["content_sha256"]}
    overrides = {}
    if mutate in {"source", "hash", "reference"}:
        key, value = {"source": ("source_message_id", "another"), "hash": ("content_sha256", "0" * 64), "reference": ("media_ref", "foreign")}[mutate]
        overrides["attachments"] = [{**attachment, key: value}]
    elif mutate == "order":
        overrides["order_id"] = 999
    elif mutate == "kind":
        overrides["kind"] = "catering"
    else:
        overrides["attachments"] = [attachment, attachment]
    result = late_attention(client, headers, descriptor, **overrides)
    assert result.status_code in {403, 409, 422}, result.text
    assert stored()["status"] == "ready"
    with SessionLocal() as db:
        assert db.query(AgentAttentionRequest).count() == 0


def test_attention_attachment_is_staff_only_and_same_tenant(client, tenant, agent_headers, auth_headers):
    descriptor = stage(client, agent_headers).json()
    lease = claim_media(client, agent_headers, descriptor)
    result = late_attention(client, leased_headers(agent_headers, lease), descriptor)
    url = result.json()["details"]["attachments"][0]["image_url"]
    denied = client.get(url, headers=agent_headers)
    # The staff-auth dependency may fail closed as unavailable when this isolated
    # environment has no identity provider. It must never return private bytes.
    assert denied.status_code in {401, 403, 503} and denied.content != png()
    assert client.get(url, headers={**auth_headers, "X-Dev-Role": "dispatcher"}).status_code == 403
    other = {**auth_headers, "X-Business-Id": str(tenant["other_business_id"]), "X-Branch-Id": str(tenant["other_branch_id"])}
    assert client.get(url, headers=other).status_code == 403


def test_attention_ownership_frees_transient_quota_without_deleting_receipt(client, agent_headers, monkeypatch):
    monkeypatch.setattr(get_settings(), "agent_media_conversation_files", 1)
    descriptor = stage(client, agent_headers).json()
    lease = claim_media(client, agent_headers, descriptor)
    result = late_attention(client, leased_headers(agent_headers, lease), descriptor)
    assert result.status_code == 201
    assert stage(client, agent_headers, "image2", content=png("blue")).status_code == 200
    assert stored()["status"] == "attached" and Path(stored()["storage_path"]).read_bytes() == png()


def test_pending_attention_preserves_consumed_image_before_notice_exists(client, agent_headers):
    descriptor = stage(client, agent_headers).json()
    lease = claim_media(client, agent_headers, descriptor)
    attachment = {"source_message_id": "image1", "media_ref": descriptor["media_ref"], "sha256": descriptor["sha256"], "content_sha256": descriptor["content_sha256"]}
    pending = {"kind": "attention", "method": "POST", "path": "/integrations/attention-requests", "idempotency_key": "unconfirmed-notice",
        "turn_id": "image1", "body": {"sender": SENDER, "kind": "late_payment_receipt", "order_id": None,
            "summary": "Comprobante pendiente de revisión", "message_ids": ["image1"], "attachments": [attachment]}}
    result = finish(client, agent_headers, lease, state={"pending": pending}, media_disposition=disposition(descriptor))
    assert result.status_code == 200 and stored()["status"] == "ready", result.text
    current = claim(client, agent_headers, "check-notice").json()
    assert read(client, agent_headers, current, descriptor).status_code == 200
    result = late_attention(client, leased_headers(agent_headers, current), descriptor)
    assert result.status_code == 201 and stored()["status"] == "attached", result.text
