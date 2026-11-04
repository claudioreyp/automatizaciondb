"""Fixed, append-only notification fixture for the private memory API sandbox.

Inert on import. Never starts a listener, resets a database, sends transport,
prints, or calls a model. Only attach explicitly to create_sandbox()'s engine.
"""
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
import re
import secrets
from threading import Lock

SCOPES = frozenset({"menu:read", "orders:read", "events:read", "events:deliver"})
PREFIX = "smoke-notice-2"
CASE = re.compile(r"SMOKE90-NOTICE-[A-Z0-9-]{1,70}\Z")
SENDER = re.compile(r"519990[0-9]{5,9}\Z")
ALLOWED_SENDERS = frozenset({"519990900001"})


def _json(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if k in {"lease_token", "access_token", "header_value", "claim_hash", "lease_hash"} else _json(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_json(v) for v in value]
    return value


def attach_notifier_fixture(sandbox, assert_private_memory_engine):
    """Add one 2/2 notifier credential and fixed operation to a NEW sandbox.

    Caller must use this only while constructing its private synthetic process.
    It intentionally rejects an already-used sandbox or a duplicate attachment.
    The returned token belongs only in the private binding file, never output.
    """
    from sqlalchemy import select
    from app.auth import AuthContext, IntegrationAuthContext, get_current_user, hash_integration_token
    from app.api import api
    from app.database import SessionLocal
    from app.main import app
    from app.models import (IntegrationCredential, IntegrationEvent, IdempotencyRecord,
        Order, KitchenTicket, AgentMessageDelivery)

    assert_private_memory_engine(sandbox["engine"])
    assert SessionLocal.kw.get("bind") is sandbox["engine"] and sandbox["client"].app is app, "NOTICE_PRIVATE_APP_ENGINE_BINDING"
    assert sandbox["state"] == {"case_id": "unstarted", "reset_count": 1}, "NOTICE_ATTACH_FRESH_SANDBOX"
    assert "notifier_fixture" not in sandbox, "NOTICE_ATTACH_ONCE"
    token = PREFIX + "." + secrets.token_urlsafe(40)
    operation_lock, attempts = Lock(), set()

    def add_credential():
        assert_private_memory_engine(sandbox["engine"])
        with SessionLocal.begin() as db:
            assert db.scalar(select(IntegrationCredential).where(IntegrationCredential.token_prefix == PREFIX)) is None
            db.add(IntegrationCredential(business_id=2, branch_id=2,
                name="Notifier sintético cuatro permisos", profile="generic", token_prefix=PREFIX,
                token_hash=hash_integration_token(token), scopes=sorted(SCOPES)))

    add_credential()
    seed_before = sandbox["seed"]

    def seed(case_id):
        assert operation_lock.acquire(blocking=False), "NOTICE_FIXTURE_BUSY"
        try:
            result = seed_before(case_id)
            add_credential()
            return result
        finally:
            operation_lock.release()

    # Only calls from the sandbox's existing explicit start-case path can reset.
    sandbox["seed"] = seed

    def exact_dependency(path):
        # Read the canonical router: current FastAPI lazily includes routers,
        # while older versions copied APIRoutes into app.routes.
        routes = [r for r in api.routes if getattr(r, "path", None) == path and "POST" in getattr(r, "methods", ())]
        assert len(routes) == 1, "NOTICE_FIXTURE_ROUTE_CHANGED"
        matches = [d.call for d in routes[0].dependant.dependencies if d.name == "integration"]
        assert len(matches) == 1, "NOTICE_FIXTURE_AUTH_SEAM_CHANGED"
        return matches[0]

    producer_dependencies = [exact_dependency("/api/v1/integrations/orders/draft"),
        exact_dependency("/api/v1/integrations/orders/{order_id}/cash-confirm")]

    @contextmanager
    def fixture_staff():
        # A synchronous, fixed handler only. No caller-supplied role, scope,
        # endpoint, body, ID or credential can select these overrides.
        absent = object()
        replacements = {get_current_user: lambda: AuthContext("notice-fixture-r02", "owner", 2, 2),
            **{dep: lambda: IntegrationAuthContext(None, 2, 2, frozenset({"orders:write"}), profile="generic") for dep in producer_dependencies}}
        previous = {key: app.dependency_overrides.get(key, absent) for key in replacements}
        try:
            app.dependency_overrides.update(replacements)
            yield
        finally:
            for key, old in previous.items():
                if old is absent:
                    app.dependency_overrides.pop(key, None)
                else:
                    app.dependency_overrides[key] = old

    def post(path, body=None, key=None, expected=200):
        response = sandbox["client"].post(path, json=body,
            headers={"Idempotency-Key": key} if key else {})
        if response.status_code != expected:
            # No request credentials/body or API response gets copied to logs.
            raise AssertionError("NOTICE_FIXTURE_API_" + str(response.status_code))
        return response.json()

    def ready(body):
        assert isinstance(body, dict) and set(body) == {"sender"}, "NOTICE_FIXTURE_BODY"
        sender = body["sender"]
        assert isinstance(sender, str) and SENDER.fullmatch(sender) and sender in ALLOWED_SENDERS, "NOTICE_FIXTURE_SENDER"
        assert CASE.fullmatch(sandbox["state"].get("case_id") or ""), "NOTICE_FIXTURE_DEDICATED_CASE"
        assert_private_memory_engine(sandbox["engine"])
        assert operation_lock.acquire(blocking=False), "NOTICE_FIXTURE_BUSY"
        try:
            identity = (sandbox["state"]["case_id"], sender)
            assert identity not in attempts, "NOTICE_FIXTURE_NO_RETRY"
            with SessionLocal() as db:
                assert not list(db.scalars(select(Order).where(Order.business_id == 2, Order.whatsapp_chat_id == sender))), "NOTICE_FIXTURE_SENDER_USED"
            attempts.add(identity)  # A partial failure is retained; no replay.
            prefix = sandbox["state"]["case_id"] + ":" + sender
            with fixture_staff():
                draft = post("/api/v1/integrations/orders/draft", {"branch_id": 2,
                    "source": "whatsapp_agent", "channel": "takeaway", "payment_method": "cash",
                    "whatsapp_chat_id": sender, "customer_phone": sender,
                    "customer_name": "Consumidor sintético NO ENVIAR",
                    "items": [{"product_id": 102, "quantity": 1}]}, key=prefix + ":draft", expected=201)
                order_id = draft["id"]
                confirmed = post(f"/api/v1/integrations/orders/{order_id}/cash-confirm", key=prefix + ":cash")
                tickets = confirmed["tickets"]
                assert len(tickets) == 1, "NOTICE_FIXTURE_ONE_TICKET"
                ticket = tickets[0]
                for status in ("preparing", "ready"):
                    ticket = post(f"/api/v1/kitchen/tickets/{ticket['id']}/transition", {
                        "status": status, "expected_status": ticket["status"], "expected_version": ticket["version"]})
            current = snapshot(sender)
            events = [e for e in current["events"] if e["event_type"] == "order.ready" and e["aggregate_id"] == str(order_id)]
            assert len(events) == 1 and events[0]["acknowledged_at"] is None, "NOTICE_FIXTURE_READY_EVENT"
            return {"synthetic_only": True, "business_id": 2, "branch_id": 2,
                "sender": sender, "order_id": order_id, "ticket_id": ticket["id"],
                "event_id": events[0]["id"], "status": "ready", "physical_printing": False,
                "real_whatsapp": False, "no_transport_invoked": True}
        finally:
            operation_lock.release()

    def snapshot(sender):
        assert isinstance(sender, str) and SENDER.fullmatch(sender) and sender in ALLOWED_SENDERS, "NOTICE_FIXTURE_SENDER"
        assert_private_memory_engine(sandbox["engine"])
        result = sandbox["snapshot"](2, sender)
        with SessionLocal() as db:
            events = list(db.scalars(select(IntegrationEvent).where(IntegrationEvent.business_id == 2,
                IntegrationEvent.branch_id == 2, IntegrationEvent.whatsapp_chat_id == sender)))
            ids = [e.id for e in events]
            mappings = list(db.scalars(select(IdempotencyRecord).where(IdempotencyRecord.business_id == 2,
                IdempotencyRecord.scope == "pos-notice-source:2", IdempotencyRecord.idempotency_key.in_(ids)))) if ids else []
            result["events"] = [{c.name: getattr(e, c.name) for c in e.__table__.columns} for e in events]
            result["notice_source_mappings"] = [{"event_id": row.idempotency_key,
                "delivery_id": row.response_body.get("delivery_id"),
                "event_fingerprint": row.response_body.get("event_fingerprint")} for row in mappings]
        return _json(result)

    fixture = {"ready": ready, "snapshot": snapshot, "token": token,
        "credential_scope": {"business_id": 2, "branch_id": 2, "profile": "generic", "scopes": sorted(SCOPES)}}
    sandbox["notifier_fixture"] = fixture
    return fixture
