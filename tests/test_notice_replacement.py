"""Real ASGI/SQL replacement and ordinary native claim/ACK, synthetic only."""
from copy import deepcopy
from datetime import timedelta
import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import (AgentMessageDelivery, IdempotencyRecord, IntegrationEvent,
    KitchenTicket, Order, Payment, PrintJob, utcnow)
from test_agent_checkout import setup, PHONE
from test_escalar_integrations import create_credential, integration_headers

BASE = "/api/v1/integrations"


def seed_reserved_notice(client, tenant, auth_headers, *, aliases=False):
    producer, order = setup(client, tenant, auth_headers, method="cash", quote=aliases)
    result = client.post(f"{BASE}/orders/{order['id']}/cash-confirm",
        headers={**producer, "Idempotency-Key": "cash-confirm"})
    assert result.status_code == 200, result.text
    with SessionLocal() as db:
        ticket = db.scalar(select(KitchenTicket).where(KitchenTicket.order_id == order["id"]))
        ticket = {"id": ticket.id, "status": ticket.status, "version": ticket.version}
    for state in ("preparing", "ready"):
        result = client.post(f"/api/v1/kitchen/tickets/{ticket['id']}/transition", headers=auth_headers,
            json={"status": state, "expected_status": ticket["status"], "expected_version": ticket["version"]})
        assert result.status_code == 200, result.text
        ticket = result.json()
    notifier = integration_headers(create_credential(client, tenant, auth_headers,
        ["menu:read", "orders:read", "events:read", "events:deliver"])["token"])
    worker = integration_headers(create_credential(client, tenant, auth_headers, ["agent:state"])["token"])
    if aliases:
        current = client.get(f"{BASE}/orders/{order['id']}/status", headers=notifier).json()
        quote = client.patch(f"/api/v1/orders/{order['id']}/delivery-fee", headers={**auth_headers, "Idempotency-Key": "human-fee"},
            json={"expected_version": current["version"], "amount": 5, "method": "cash"})
        assert quote.status_code == 200, quote.text
        with SessionLocal.begin() as db:
            original = db.scalar(select(IntegrationEvent).where(IntegrationEvent.event_type == "order.delivery_fee_confirmed"))
            db.add(IntegrationEvent(business_id=original.business_id, branch_id=original.branch_id, event_type=original.event_type,
                aggregate_type=original.aggregate_type, aggregate_id=original.aggregate_id, customer_phone=original.customer_phone,
                whatsapp_chat_id=original.whatsapp_chat_id, payload=deepcopy(original.payload)))
    events = client.get(BASE + "/events", headers=notifier, params={"event_types": "order.delivery_fee_confirmed" if aliases else "order.ready", "pending_only": "true"})
    assert events.status_code == 200, events.text
    event = events.json()[0]
    current = client.get(f"{BASE}/orders/{order['id']}/status", headers=notifier).json()
    prepared = {"order_id": order["id"], "expected_order_version": current["version"],
        "event_fingerprint": event["notice_fingerprint"], "recipient": PHONE,
        "message": "Tu pedido sintético está listo para recoger.", "confirmation_key": None}
    if aliases:
        p = event["payload"]
        prepared["confirmation_key"] = json.dumps([order["id"], p.get("quote_id"), p.get("payment_request_id"), p.get("delivery_fee"), p.get("request_amount")], separators=(",", ":"))
    queued = client.post(f"{BASE}/events/{event['id']}/outbox", headers=notifier, json=prepared)
    assert queued.status_code == 200, queued.text
    if aliases:
        for alias in events.json()[1:]:
            alias_result = client.post(f"{BASE}/events/{alias['id']}/outbox", headers=notifier,
                json={**prepared, "event_fingerprint": alias["notice_fingerprint"]})
            assert alias_result.status_code == 200, alias_result.text
            assert alias_result.json()["delivery_id"] == queued.json()["delivery_id"]
    claim = client.post(BASE + "/conversations/native/outbox/claim", json={},
        headers={**worker, "Idempotency-Key": "original-claim"})
    assert claim.status_code == 200 and claim.json()["status"] == "claimed", claim.text
    claim = claim.json()
    expired = int((utcnow() - timedelta(minutes=1)).timestamp() * 1000)
    body = {"expected_event_fingerprint": event["notice_fingerprint"], "delivery_token": claim["delivery_token"],
        "retirement": {"schema_version": "native-qr-mailbox-retirement/v1",
            "business_id": tenant["business_id"], "branch_id": tenant["branch_id"],
            "delivery_id": claim["delivery_id"], "message_id": claim["message_id"], "sender": PHONE,
            "source_row_id": 44, "source_workflow_id": "synthetic-main", "source_run_id": "original-source",
            "source_head": 12, "source_state": "mailbox_admission_consumed", "source_body_hash": "a" * 64,
            "source_deadline_at_ms": expired, "mailbox_row_id": 9, "mailbox_workflow_id": "synthetic-mailbox",
            "mailbox_code_manifest": "b" * 64, "mailbox_head_before": 0, "mailbox_head_after": 1,
            "mailbox_state": "retired_no_transport", "mailbox_body_hash": "c" * 64,
            "mailbox_deadline_at_ms": expired, "retirement_owner_execution": "retirement-execution",
            "retirement_nonce": str(uuid4())}}
    return {"notifier": notifier, "worker": worker, "event": event, "claim": claim, "body": body,
        "order_id": order["id"], "prepared": prepared, "source_events": events.json()}


def replacement_path(seed):
    return f"{BASE}/events/{seed['event']['id']}/outbox/{seed['claim']['delivery_id']}/replace-never-transported"


def replace(client, seed, body=None, headers=None):
    return client.post(replacement_path(seed), json=body if body is not None else seed["body"], headers={
        **(headers if headers is not None else seed["notifier"]),
        "Idempotency-Key": "notice-replacement:" + seed["claim"]["delivery_id"]})


def commercial_snapshot():
    with SessionLocal() as db:
        return {model.__name__: [{column.name: getattr(row, column.name) for column in model.__table__.columns}
            for row in db.scalars(select(model).order_by(model.id))]
            for model in (Order, Payment, KitchenTicket, PrintJob)}


def test_replacement_uses_real_claim_freshness_and_ack_with_only_new_identity(client, tenant, auth_headers):
    seed = seed_reserved_notice(client, tenant, auth_headers)
    before = commercial_snapshot()
    with SessionLocal() as db:
        old_response = deepcopy(db.get(AgentMessageDelivery, seed["claim"]["delivery_id"]).response)
    response = replace(client, seed)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["delivery_id"] != result["old_delivery_id"] == seed["claim"]["delivery_id"]
    assert result["event_acknowledged"] is False
    assert replace(client, seed).json() == result
    with SessionLocal() as db:
        old, new = db.get(AgentMessageDelivery, result["old_delivery_id"]), db.get(AgentMessageDelivery, result["delivery_id"])
        assert old.status == "suppressed" and old.claim_hash is None
        assert old.response == old_response and old.message_id == seed["claim"]["message_id"]
        assert old.receipt["transport_attempted"] is False
        expected = deepcopy(old_response)
        expected["turn"]["id"] = result["message_id"]
        assert new.response == expected and new.message_id == result["message_id"] and new.status == "pending"
        assert seed["claim"]["delivery_id"] not in json.dumps(new.response)
        assert seed["claim"]["message_id"] not in json.dumps(new.response)
        mapping = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.scope == f"pos-notice-source:{tenant['branch_id']}"))
        assert mapping.response_body["delivery_id"] == new.id
        audit = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.scope == f"pos-notice-replacement:{tenant['branch_id']}"))
        assert audit.response_body["audit"]["old_source_mappings"][0]["response_body"]["delivery_id"] == old.id
        assert seed["claim"]["delivery_token"] not in json.dumps(audit.response_body)
        assert db.get(IntegrationEvent, seed["event"]["id"]).acknowledged_at is None
    old_ack = client.patch(f"{BASE}/conversations/outbox/{result['old_delivery_id']}/ack",
        headers={**seed["worker"], "Idempotency-Key": "old-ack-forbidden"}, json={
            "delivery_token": seed["claim"]["delivery_token"], "status": "sent", "receipt": {"transport_id": "MUST-NOT-ACK"}})
    assert old_ack.status_code == 409, old_ack.text
    claim = client.post(BASE + "/conversations/native/outbox/claim", json={},
        headers={**seed["worker"], "Idempotency-Key": "replacement-claim"})
    assert claim.status_code == 200, claim.text
    claimed = claim.json()
    assert claimed["delivery_id"] == result["delivery_id"] and claimed["message_id"] == result["message_id"]
    assert claimed["response"]["turn"]["id"] == claimed["message_id"]
    assert claimed["sender"] == PHONE and claimed["conversation_id"] == seed["claim"]["conversation_id"]
    fresh = client.post(f"{BASE}/conversations/outbox/{result['delivery_id']}/freshness",
        headers={**seed["worker"], "Idempotency-Key": "replacement-fresh"}, json={"delivery_token": claimed["delivery_token"]})
    assert fresh.status_code == 200 and fresh.json()["status"] == "current", fresh.text
    assert fresh.json()["response"] == claimed["response"]
    ack_body = {"delivery_token": claimed["delivery_token"], "status": "sent", "receipt": {"transport_id": "SYNTHETIC-ONLY-NEW"}}
    ack_headers = {**seed["worker"], "Idempotency-Key": "replacement-ack"}
    ack = client.patch(f"{BASE}/conversations/outbox/{claimed['delivery_id']}/ack", headers=ack_headers, json=ack_body)
    assert ack.status_code == 200 and ack.json() == {"delivery_id": claimed["delivery_id"], "status": "sent"}, ack.text
    assert client.patch(f"{BASE}/conversations/outbox/{claimed['delivery_id']}/ack", headers=ack_headers, json=ack_body).json() == ack.json()
    with SessionLocal() as db:
        event = db.get(IntegrationEvent, seed["event"]["id"])
        assert event.acknowledged_by == "outbox:" + claimed["delivery_id"] and event.acknowledged_at is not None
        assert db.query(AgentMessageDelivery).count() == 2
    assert commercial_snapshot() == before


@pytest.mark.parametrize("mutation", ["not_expired", "wrong_token", "wrong_sender", "wrong_scope", "wrong_fingerprint", "sent", "transport_unknown", "order_changed", "acked", "source_changed"])
def test_rejects_unsafe_or_stale_replacement_without_side_effects(client, tenant, auth_headers, mutation):
    seed = seed_reserved_notice(client, tenant, auth_headers)
    body = deepcopy(seed["body"])
    if mutation == "not_expired":
        body["retirement"]["source_deadline_at_ms"] = int((utcnow() + timedelta(minutes=1)).timestamp() * 1000)
    elif mutation == "wrong_token":
        body["delivery_token"] = "invalid-synthetic-token-value"
    elif mutation == "wrong_sender":
        body["retirement"]["sender"] = "519990123456"
    elif mutation == "wrong_scope":
        body["retirement"]["branch_id"] = tenant["other_branch_id"]
    elif mutation == "wrong_fingerprint":
        body["expected_event_fingerprint"] = "d" * 64
    else:
        with SessionLocal.begin() as db:
            row = db.get(AgentMessageDelivery, seed["claim"]["delivery_id"])
            if mutation == "sent":
                row.status = "sent"
            elif mutation == "transport_unknown":
                row.receipt = {"reason": "transport_outcome_unknown"}
            elif mutation == "order_changed":
                db.get(Order, seed["order_id"]).version += 1
            elif mutation == "acked":
                db.get(IntegrationEvent, seed["event"]["id"]).acknowledged_at = utcnow()
            elif mutation == "source_changed":
                db.get(IntegrationEvent, seed["event"]["id"]).payload = {"tampered": True}
    before = commercial_snapshot()
    response = replace(client, seed, body)
    assert response.status_code == 409, response.text
    assert commercial_snapshot() == before
    with SessionLocal() as db:
        assert db.query(AgentMessageDelivery).count() == 1
        assert db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.scope == f"pos-notice-replacement:{tenant['branch_id']}")) is None


def test_permissions_and_validation_do_not_expose_token_or_proof_input(client, tenant, auth_headers):
    seed = seed_reserved_notice(client, tenant, auth_headers)
    denied = replace(client, seed, headers=seed["worker"])
    assert denied.status_code == 403, denied.text
    body = deepcopy(seed["body"])
    body["retirement"]["mailbox_head_before"] = True
    body["retirement"]["unexpected_secret"] = "SYNTHETIC-PROOF-SECRET"
    response = replace(client, seed, body)
    assert response.status_code == 422, response.text
    assert seed["claim"]["delivery_token"] not in response.text
    assert "SYNTHETIC-PROOF-SECRET" not in response.text
    assert '"input"' not in response.text and '"ctx"' not in response.text


def test_all_real_coalesced_event_aliases_move_and_ack_only_new_delivery(client, tenant, auth_headers):
    seed = seed_reserved_notice(client, tenant, auth_headers, aliases=True)
    before = commercial_snapshot()
    assert len(seed["source_events"]) == 2
    replaced = replace(client, seed)
    assert replaced.status_code == 200, replaced.text
    result = replaced.json()
    with SessionLocal() as db:
        audit = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.scope == f"pos-notice-replacement:{tenant['branch_id']}"))
        saved = audit.response_body["audit"]["old_source_mappings"]
        assert len(saved) == 2 and all(m["response_body"]["delivery_id"] == seed["claim"]["delivery_id"] for m in saved)
        mappings = db.scalars(select(IdempotencyRecord).where(IdempotencyRecord.scope == f"pos-notice-source:{tenant['branch_id']}"))
        assert all(m.response_body["delivery_id"] == result["delivery_id"] for m in mappings)
    claimed = client.post(BASE + "/conversations/native/outbox/claim", json={},
        headers={**seed["worker"], "Idempotency-Key": "alias-replacement-claim"}).json()
    assert claimed["delivery_id"] == result["delivery_id"]
    ack = client.patch(f"{BASE}/conversations/outbox/{claimed['delivery_id']}/ack",
        headers={**seed["worker"], "Idempotency-Key": "alias-new-ack"}, json={"delivery_token": claimed["delivery_token"],
            "status": "sent", "receipt": {"transport_id": "SYNTHETIC-ALIASES"}})
    assert ack.status_code == 200, ack.text
    with SessionLocal() as db:
        for source in seed["source_events"]:
            assert db.get(IntegrationEvent, source["id"]).acknowledged_by == "outbox:" + result["delivery_id"]
    assert commercial_snapshot() == before


def test_contract_exposes_internal_route_only_to_generic_notifier(client):
    from app.agent_tools import TOOLS
    from test_agent_tool_metadata import package
    tool = next(t for t in TOOLS if t.name == "replace_never_transported_event_notice")
    assert tool.profiles == ("generic",) and tool.scope == "events:deliver"
    assert tool.name in package("generic") and tool.name not in package("restaurant_agent")
    operation = client.get("/openapi.json").json()["paths"]["/api/v1" + tool.path]["post"]
    assert operation["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("/ReplaceRetiredNotice")
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("/NoticeReplacementResult")


def add_late_alias(client, seed):
    with SessionLocal.begin() as db:
        original = db.get(IntegrationEvent, seed["event"]["id"])
        alias = IntegrationEvent(business_id=original.business_id, branch_id=original.branch_id, event_type=original.event_type,
            aggregate_type=original.aggregate_type, aggregate_id=original.aggregate_id, customer_phone=original.customer_phone,
            whatsapp_chat_id=original.whatsapp_chat_id, payload=deepcopy(original.payload))
        db.add(alias)
        db.flush()
        alias_id = alias.id
    events = client.get(BASE + "/events", headers=seed["notifier"], params={"event_types": "order.delivery_fee_confirmed", "pending_only": "true"})
    assert events.status_code == 200, events.text
    event = next(row for row in events.json() if row["id"] == alias_id)
    return event, {**seed["prepared"], "event_fingerprint": event["notice_fingerprint"]}


@pytest.mark.parametrize("already_sent", [False, True])
def test_late_alias_follows_current_child_including_after_its_ack(client, tenant, auth_headers, already_sent):
    seed = seed_reserved_notice(client, tenant, auth_headers, aliases=True)
    result = replace(client, seed)
    assert result.status_code == 200, result.text
    child = result.json()
    if already_sent:
        claimed = client.post(BASE + "/conversations/native/outbox/claim", json={},
            headers={**seed["worker"], "Idempotency-Key": "before-late-alias-claim"}).json()
        ack = client.patch(f"{BASE}/conversations/outbox/{claimed['delivery_id']}/ack",
            headers={**seed["worker"], "Idempotency-Key": "before-late-alias-ack"}, json={"delivery_token": claimed["delivery_token"],
                "status": "sent", "receipt": {"transport_id": "SYNTHETIC-SENT-BEFORE-ALIAS"}})
        assert ack.status_code == 200, ack.text
    late, prepared = add_late_alias(client, seed)
    before = commercial_snapshot()
    queued = client.post(f"{BASE}/events/{late['id']}/outbox", headers=seed["notifier"], json=prepared)
    assert queued.status_code == 200, queued.text
    assert queued.json() == {"status": "sent" if already_sent else "pending", "delivery_id": child["delivery_id"],
        "message_id": child["message_id"], "event_id": late["id"], "event_acknowledged": already_sent}
    # Original replay likewise resolves the latest actual message identity.
    replay = client.post(f"{BASE}/events/{seed['event']['id']}/outbox", headers=seed["notifier"], json=seed["prepared"])
    assert replay.status_code == 200 and replay.json()["delivery_id"] == child["delivery_id"], replay.text
    assert replay.json()["message_id"] == child["message_id"]
    with SessionLocal() as db:
        mappings = list(db.scalars(select(IdempotencyRecord).where(IdempotencyRecord.scope == f"pos-notice-source:{tenant['branch_id']}")))
        assert len(mappings) == 3 and all(m.response_body["delivery_id"] == child["delivery_id"] for m in mappings)
        assert db.query(AgentMessageDelivery).count() == 2
        event = db.get(IntegrationEvent, late["id"])
        assert (event.acknowledged_by == "outbox:" + child["delivery_id"]) if already_sent else event.acknowledged_at is None
    assert commercial_snapshot() == before


def test_alias_waiting_for_replacement_transaction_follows_committed_child(client, tenant, auth_headers):
    """PostgreSQL lock held at the replacement UPDATE; alias must wait for it."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from sqlalchemy import event as sql_event
    from app.database import engine
    from app.auth import IntegrationAuthContext
    from app.agent_event_outbox import ReplaceRetiredNotice, PreparedNotice, replace_retired_notice, queue_prepared_notice
    if engine.dialect.name != "postgresql":
        pytest.skip("Real row-lock ordering is covered in isolated PostgreSQL")
    seed = seed_reserved_notice(client, tenant, auth_headers, aliases=True)
    alias, prepared = add_late_alias(client, seed)
    replacement_holds_lock, alias_issued_lock, allow_replacement = Event(), Event(), Event()
    def observe(conn, cursor, statement, parameters, context, many):
        if (statement.lstrip().upper().startswith("UPDATE AGENT_MESSAGE_DELIVERIES SET STATUS=")
                and "AGENT_MESSAGE_DELIVERIES.CLAIM_HASH =" in statement.upper()):
            replacement_holds_lock.set()
            assert allow_replacement.wait(8)
        if statement.lstrip().upper().startswith("SELECT AGENT_CONVERSATIONS.") and "FOR UPDATE" in statement.upper() and replacement_holds_lock.is_set():
            alias_issued_lock.set()
    sql_event.listen(engine, "before_cursor_execute", observe)
    integration = IntegrationAuthContext(1, tenant["business_id"], tenant["branch_id"], frozenset({"events:deliver"}))
    def do_replacement():
        with SessionLocal() as db:
            return replace_retired_notice(db, integration, seed["event"]["id"], seed["claim"]["delivery_id"], ReplaceRetiredNotice.model_validate(seed["body"]))
    def do_alias():
        with SessionLocal() as db:
            return queue_prepared_notice(alias["id"], PreparedNotice.model_validate(prepared), integration, db)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            replacing = pool.submit(do_replacement)
            assert replacement_holds_lock.wait(8)
            queuing = pool.submit(do_alias)
            assert alias_issued_lock.wait(8)
            from concurrent.futures import TimeoutError as FutureTimeout
            with pytest.raises(FutureTimeout):
                queuing.result(timeout=0.2)  # Issued SQL is blocked by the real row lock.
            allow_replacement.set()
            result, queued = replacing.result(timeout=10), queuing.result(timeout=10)
        assert queued["delivery_id"] == result["delivery_id"] and queued["message_id"] == result["message_id"]
        with SessionLocal() as db:
            mappings = list(db.scalars(select(IdempotencyRecord).where(IdempotencyRecord.scope == f"pos-notice-source:{tenant['branch_id']}")))
            assert len(mappings) == 3 and all(m.response_body["delivery_id"] == result["delivery_id"] for m in mappings)
            assert db.query(AgentMessageDelivery).count() == 2
    finally:
        allow_replacement.set()
        sql_event.remove(engine, "before_cursor_execute", observe)


def test_committed_response_loss_replays_same_child_without_second_release(client, tenant, auth_headers, monkeypatch):
    from app import agent_event_outbox as module
    from fastapi.testclient import TestClient
    from app.main import app
    seed = seed_reserved_notice(client, tenant, auth_headers)
    original = module.replace_retired_notice
    committed = []
    def lose_once(*args, **kwargs):
        result = original(*args, **kwargs)
        if not committed:
            committed.append(deepcopy(result))
            raise RuntimeError("SYNTHETIC_RESPONSE_LOSS_AFTER_COMMIT")
        return result
    monkeypatch.setattr(module, "replace_retired_notice", lose_once)
    with TestClient(app, raise_server_exceptions=False) as failing:
        lost = replace(failing, seed)
    assert lost.status_code == 500
    recovered = replace(client, seed)
    assert recovered.status_code == 200 and recovered.json() == committed[0], recovered.text
    changed = deepcopy(seed["body"])
    changed["retirement"]["retirement_nonce"] = str(uuid4())
    assert replace(client, seed, changed).status_code == 409
    with SessionLocal() as db:
        assert db.query(AgentMessageDelivery).count() == 2
        assert db.query(IdempotencyRecord).filter(IdempotencyRecord.scope == f"pos-notice-replacement:{tenant['branch_id']}").count() == 1


def test_two_real_sql_transactions_create_one_child_and_reconcile_same_key(client, tenant, auth_headers, tmp_path):
    """Real SQL transactions/unique constraint, not a mocked commit or row count.

    SQLite contenders both reach the conditional UPDATE after reading the old
    reservation. PostgreSQL uses its real conversation/delivery row locks.
    """
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    import sqlite3
    from fastapi import HTTPException
    from sqlalchemy import create_engine, event as sql_event
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import sessionmaker
    from app.database import engine
    from app.auth import IntegrationAuthContext
    from app.agent_event_outbox import ReplaceRetiredNotice, replace_retired_notice

    seed = seed_reserved_notice(client, tenant, auth_headers)
    owned_engine = engine.dialect.name == "sqlite"
    if owned_engine:
        copied_db = tmp_path / "synthetic-notice-race.sqlite"
        raw = engine.raw_connection()
        try:
            with sqlite3.connect(copied_db) as target:
                raw.driver_connection.backup(target)
        finally:
            raw.close()
        race_engine = create_engine("sqlite+pysqlite:///" + str(copied_db), connect_args={"timeout": 5, "check_same_thread": False})
    else:
        assert engine.dialect.name == "postgresql"
        race_engine = engine
    sessions = sessionmaker(race_engine)
    before_update, start = Barrier(2, timeout=8), Barrier(2, timeout=8)
    synchronized = []
    def synchronize_update(conn, cursor, statement, parameters, context, executemany):
        if (statement.lstrip().upper().startswith("UPDATE AGENT_MESSAGE_DELIVERIES SET STATUS=")
                and "AGENT_MESSAGE_DELIVERIES.CLAIM_HASH =" in statement.upper()):
            synchronized.append(id(conn))
            before_update.wait()
    if owned_engine:
        sql_event.listen(race_engine, "before_cursor_execute", synchronize_update)
    integration = IntegrationAuthContext(1, tenant["business_id"], tenant["branch_id"], frozenset({"events:deliver"}))
    payload = ReplaceRetiredNotice.model_validate(seed["body"])
    def contender(_):
        with sessions() as db:
            start.wait()
            try:
                return {"result": replace_retired_notice(db, integration, seed["event"]["id"], seed["claim"]["delivery_id"], payload)}
            except (HTTPException, OperationalError) as error:
                db.rollback()
                return {"blocked": type(error).__name__, "status": getattr(error, "status_code", None)}
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(contender, range(2)))
        if owned_engine:
            sql_event.remove(race_engine, "before_cursor_execute", synchronize_update)
            assert len(synchronized) == 2 and len(set(synchronized)) == 2
        winners = [outcome["result"] for outcome in outcomes if "result" in outcome]
        assert winners and all(result == winners[0] for result in winners), outcomes
        with sessions() as db:
            assert db.query(AgentMessageDelivery).count() == 2
            assert db.query(IdempotencyRecord).filter(IdempotencyRecord.scope == f"pos-notice-replacement:{tenant['branch_id']}").count() == 1
            old = db.get(AgentMessageDelivery, seed["claim"]["delivery_id"])
            assert old.status == "suppressed" and old.claim_hash is None
            replay = replace_retired_notice(db, integration, seed["event"]["id"], old.id, payload)
            assert replay == winners[0]
    finally:
        if owned_engine:
            race_engine.dispose()
