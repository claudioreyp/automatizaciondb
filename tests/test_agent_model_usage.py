"""Budget enforcement through ASGI; test database only, no provider calls."""
from datetime import timedelta
import importlib.util
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.auth import hash_integration_token
from app.config import AgentUsageOverride, Settings, get_settings
from app.database import SessionLocal, engine
from app.models import AgentAttentionRequest, AgentConversation, AgentModelUsage, Branch, IntegrationCredential, KitchenTicket, Order, Payment, utcnow
from app.schemas import RESTAURANT_AGENT_SCOPES
from test_restaurant_agent_boundary import agent_headers, claim

BASE = "/api/v1/integrations/conversations"


def acquire(client, headers, **kwargs):
    response = claim(client, headers, **kwargs)
    assert response.status_code == 200, response.text
    return response.json()


def identity(lease, call_id="interpreter:0", **kwargs):
    return {"lease_token": lease["lease_token"], "expected_version": lease["version"],
        "message_id": lease["message_id"], "call_id": call_id, "request_fingerprint": "a"*64, **kwargs}


def reserve(client, headers, lease, *, key="reserve1", call_id="interpreter:0", tokens=100, **kwargs):
    return client.post(f'{BASE}/{lease["conversation_id"]}/model-usage/reserve',
        headers={**headers, "Idempotency-Key": key}, json={**identity(lease, call_id),
            "model_id": "claude-sonnet-5-5", "role": "interpreter", "reserved_tokens": tokens, **kwargs})


def settle(client, headers, lease, reservation_id, *, key="settle1", call_id="interpreter:0", **kwargs):
    return client.post(f'{BASE}/{lease["conversation_id"]}/model-usage/{reservation_id}/settle',
        headers={**headers, "Idempotency-Key": key}, json={**identity(lease, call_id),
            "usage_known": True, "input_tokens": 50, "output_tokens": 10, **kwargs})


def configure(monkeypatch, **limits):
    settings = get_settings()
    for name, value in limits.items():
        monkeypatch.setattr(settings, "agent_usage_" + name, value)


def credential(tenant, branch_id, prefix):
    with SessionLocal.begin() as db:
        db.add(IntegrationCredential(business_id=tenant["business_id"], branch_id=branch_id,
            name="Isolated usage test", profile="restaurant_agent", token_prefix=prefix,
            token_hash=hash_integration_token(prefix+".token"), scopes=sorted(RESTAURANT_AGENT_SCOPES)))
    return {"Authorization": "Bearer "+prefix+".token"}


def test_reserve_once_settle_exactly_and_no_prompt_or_phone_in_public_response(client, agent_headers):
    lease = acquire(client, agent_headers)
    first = reserve(client, agent_headers, lease)
    assert first.status_code == 200, first.text
    data = first.json()
    assert data["dispatch_allowed"] is True and data["status"] == "reserved"
    assert first.headers["cache-control"] == "no-store"
    assert set(data) == {"protocol_version","reservation_id","status","dispatch_allowed","reason","window_ends_at","attention_recorded"}
    replay = reserve(client, agent_headers, lease).json()
    assert replay["reservation_id"] == data["reservation_id"] and replay["dispatch_allowed"] is False
    assert replay["reason"] == "reservation_replay"
    settled = settle(client, agent_headers, lease, data["reservation_id"], cache_creation_input_tokens=3, cache_read_input_tokens=2)
    assert settled.status_code == 200 and settled.json()["status"] == "settled"
    assert settled.json()["dispatch_allowed"] is False
    assert settle(client, agent_headers, lease, data["reservation_id"], cache_creation_input_tokens=3, cache_read_input_tokens=2).json() == settled.json()
    assert reserve(client, agent_headers, lease).json()["dispatch_allowed"] is False
    with SessionLocal() as db:
        row = db.scalar(select(AgentModelUsage))
        assert row.charged_tokens == 65 and row.reserved_tokens == 100
        assert row.input_tokens == 50 and row.output_tokens == 10
        assert row.sender_hash and "51999900001" not in str(row.__dict__)
        assert db.get(AgentConversation, lease["conversation_id"]).version == lease["version"]
        assert all(db.scalar(select(func.count()).select_from(model)) == 0 for model in [Order, Payment, KitchenTicket])


@pytest.mark.parametrize("change", [{"request_fingerprint":"b"*64}, {"reserved_tokens":101}, {"role":"composer"}, {"model_id":"another-model"}, {"call_id":"different"}])
def test_reservation_reused_key_or_call_with_different_payload_fails(client, agent_headers, change):
    lease = acquire(client, agent_headers)
    assert reserve(client, agent_headers, lease).status_code == 200
    assert reserve(client, agent_headers, lease, **change).status_code == 409
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(AgentModelUsage)) == 1


def test_changed_settlement_rejected_and_unknown_keeps_full_reserved_charge(client, agent_headers):
    lease = acquire(client, agent_headers)
    row = reserve(client, agent_headers, lease).json()
    unknown = settle(client, agent_headers, lease, row["reservation_id"], usage_known=False, input_tokens=None, output_tokens=None)
    assert unknown.status_code == 200 and unknown.json()["status"] == "uncertain"
    assert settle(client, agent_headers, lease, row["reservation_id"], usage_known=False, input_tokens=None, output_tokens=None).json() == unknown.json()
    assert settle(client, agent_headers, lease, row["reservation_id"]).status_code == 409
    assert reserve(client, agent_headers, lease).json()["dispatch_allowed"] is False
    with SessionLocal() as db:
        saved = db.get(AgentModelUsage, row["reservation_id"])
        assert saved.charged_tokens == 100 and saved.usage_known is False


def test_known_usage_over_reservation_is_never_truncated_and_alerts_staff(client, agent_headers):
    lease = acquire(client, agent_headers)
    row = reserve(client, agent_headers, lease).json()
    response = settle(client, agent_headers, lease, row["reservation_id"], input_tokens=105, output_tokens=10)
    assert response.status_code == 200 and response.json()["attention_recorded"]
    with SessionLocal() as db:
        assert db.get(AgentModelUsage, row["reservation_id"]).charged_tokens == 115
        notice = db.scalar(select(AgentAttentionRequest))
        assert notice.order_id is None and notice.details["event"] == "actual_over_reservation"
    assert settle(client, agent_headers, lease, row["reservation_id"], input_tokens=106).status_code == 409


def test_threshold_and_denial_are_durable_deduplicated_and_never_orders(client, agent_headers, monkeypatch):
    configure(monkeypatch, conversation_tokens=10, sender_tokens=1000, business_tokens=1000)
    lease = acquire(client, agent_headers)
    assert reserve(client, agent_headers, lease, tokens=8).json()["attention_recorded"] is True
    for index in [1, 1, 2]:
        response = reserve(client, agent_headers, lease, key=f"deny{index}", call_id=f"composer:{index}", tokens=3)
        assert response.status_code == 200
        assert response.json()["status"] == "denied" and response.json()["dispatch_allowed"] is False
        assert response.json()["reason"] == "budget_exceeded"
    with SessionLocal() as db:
        assert db.scalar(select(func.sum(AgentModelUsage.charged_tokens))) == 8
        notices = db.scalars(select(AgentAttentionRequest)).all()
        assert len(notices) == 2 and {n.details["event"] for n in notices} == {"near_limit", "budget_exceeded"}
        assert all(n.order_id is None for n in notices)
        assert db.scalar(select(func.count()).select_from(Order)) == 0


def test_actual_usage_frees_only_known_difference_and_unknown_does_not(client, agent_headers, monkeypatch):
    configure(monkeypatch, conversation_tokens=100, sender_tokens=1000, business_tokens=1000)
    lease = acquire(client, agent_headers)
    first = reserve(client, agent_headers, lease, tokens=80).json()
    assert settle(client, agent_headers, lease, first["reservation_id"], input_tokens=20, output_tokens=0).status_code == 200
    second = reserve(client, agent_headers, lease, key="r2", call_id="composer:0", tokens=80).json()
    assert second["dispatch_allowed"] is True
    assert settle(client, agent_headers, lease, second["reservation_id"], key="s2", call_id="composer:0", usage_known=False, input_tokens=None, output_tokens=None).status_code == 200
    assert reserve(client, agent_headers, lease, key="r3", call_id="repair:0", tokens=1).json()["status"] == "denied"


def test_sender_budget_crosses_branches_and_lid_chats_using_authenticated_phone(client, agent_headers, tenant, monkeypatch):
    configure(monkeypatch, conversation_tokens=1000, sender_tokens=10, business_tokens=1000)
    first = acquire(client, agent_headers)
    assert reserve(client, agent_headers, first, tokens=6).json()["dispatch_allowed"]
    with SessionLocal.begin() as db:
        branch = Branch(business_id=tenant["business_id"], slug="usage-second", name="Second")
        db.add(branch); db.flush(); branch_id = branch.id
    other_headers = credential(tenant, branch_id, "usage-second")
    response = client.post(BASE+"/claim", headers={**other_headers,"Idempotency-Key":"other-chat"},
        json={"sender":"123456789@lid","message_id":"other-chat","message":{"text":"hola","verified_phone":"51999900001"}})
    assert response.status_code == 200, response.text
    second = reserve(client, other_headers, response.json(), tokens=5).json()
    assert second["status"] == "denied" and second["dispatch_allowed"] is False
    with SessionLocal() as db:
        records = db.scalars(select(AgentModelUsage)).all()
        assert len({r.sender_hash for r in records}) == 1


def test_business_budget_accumulates_different_senders_and_conversations(client, agent_headers, monkeypatch):
    configure(monkeypatch, conversation_tokens=1000, sender_tokens=1000, business_tokens=10)
    first = acquire(client, agent_headers)
    second = acquire(client, agent_headers, sender="51999900002", message_id="m2")
    assert reserve(client, agent_headers, first, tokens=6).json()["dispatch_allowed"]
    assert reserve(client, agent_headers, second, tokens=5).json()["status"] == "denied"


def test_missing_authenticated_lid_phone_fails_closed_without_guessing(client, agent_headers):
    lease = acquire(client, agent_headers, sender="123456789@lid", text="mi telefono 51999900001")
    response = reserve(client, agent_headers, lease)
    assert response.status_code == 200 and response.json()["reason"] == "identity_unavailable"
    assert response.json()["dispatch_allowed"] is False and response.json()["attention_recorded"]
    with SessionLocal() as db:
        assert db.scalar(select(AgentModelUsage)).sender_hash is None


def test_utc_rollover_never_rebills_prior_reservation_or_grants_replay(client, agent_headers, monkeypatch):
    configure(monkeypatch, conversation_tokens=100, sender_tokens=1000, business_tokens=1000)
    lease = acquire(client, agent_headers)
    original = utcnow().replace(hour=23,minute=59,second=59,microsecond=0)
    monkeypatch.setattr("app.agent_usage.utcnow", lambda: original)
    first = reserve(client, agent_headers, lease, tokens=100).json()
    monkeypatch.setattr("app.agent_usage.utcnow", lambda: original+timedelta(seconds=2))
    assert reserve(client, agent_headers, lease, tokens=100).json()["dispatch_allowed"] is False
    second = reserve(client, agent_headers, lease, key="r2", call_id="composer:0", tokens=100).json()
    assert second["dispatch_allowed"] is True and second["window_ends_at"] != first["window_ends_at"]
    assert settle(client, agent_headers, lease, first["reservation_id"], usage_known=False, input_tokens=None, output_tokens=None).status_code == 200
    with SessionLocal() as db:
        a,b = db.get(AgentModelUsage, first["reservation_id"]),db.get(AgentModelUsage, second["reservation_id"])
        assert a.charged_tokens == b.charged_tokens == 100 and a.window_start != b.window_start


@pytest.mark.parametrize("change", [{"lease_token":"x"*32},{"expected_version":999},{"message_id":"another-input"}])
def test_stale_lease_version_or_other_message_cannot_reserve_or_settle(client, agent_headers, change):
    lease = acquire(client, agent_headers)
    first = reserve(client, agent_headers, lease).json()
    assert reserve(client, agent_headers, lease, key="r2", call_id="composer:0", **change).status_code == 409
    assert settle(client, agent_headers, lease, first["reservation_id"], **change).status_code == 409


def test_expired_worker_and_other_tenant_cannot_touch_ledger(client, agent_headers, tenant):
    lease = acquire(client, agent_headers)
    first = reserve(client, agent_headers, lease).json()
    other = credential({"business_id":tenant["other_business_id"]}, tenant["other_branch_id"], "other-usage")
    assert reserve(client, other, lease).status_code == 404
    assert settle(client, other, lease, first["reservation_id"]).status_code == 404
    with SessionLocal.begin() as db:
        db.get(AgentConversation, lease["conversation_id"]).lease_until = utcnow()-timedelta(seconds=1)
    assert settle(client, agent_headers, lease, first["reservation_id"]).status_code == 409
    assert reserve(client, agent_headers, lease).status_code == 409


def test_unknown_fields_injecting_identity_or_budget_are_rejected(client, agent_headers):
    lease = acquire(client, agent_headers)
    for field in ["sender", "business_id", "budget", "prompt", "provider_key"]:
        assert reserve(client, agent_headers, lease, **{field:"untrusted"}).status_code == 422
    row = reserve(client, agent_headers, lease).json()
    assert settle(client, agent_headers, lease, row["reservation_id"], usage_known=False, input_tokens=5).status_code == 422
    assert settle(client, agent_headers, lease, row["reservation_id"], input_tokens=None).status_code == 422


def test_scopes_and_context_publish_protocol_only(client, agent_headers, tenant):
    lease = acquire(client, agent_headers)
    context = client.get('/api/v1/integrations/context',headers=agent_headers).json()
    assert context["model_usage"] == {"version":1}
    assert "agent_usage_conversation_tokens" not in str(context)
    with SessionLocal.begin() as db:
        row = db.scalar(select(IntegrationCredential))
        row.scopes = [s for s in row.scopes if s != "agent:state"]
    assert reserve(client, agent_headers, lease).status_code == 403


def test_admin_connection_package_describes_usage_without_secrets_or_elevating_generic(client, tenant, auth_headers):
    admin = {**auth_headers, "X-Dev-Role": "superadmin"}
    usage_paths = {
        "model_usage_reserve": "/conversations/{conversation_id}/model-usage/reserve",
        "model_usage_settle": "/conversations/{conversation_id}/model-usage/{reservation_id}/settle",
    }
    for profile in ["restaurant_agent", "generic"]:
        created = client.post("/api/v1/admin/integration-credentials", headers=admin,
            json={"branch_id":tenant["branch_id"], "name":"Usage discovery test", "profile":profile})
        assert created.status_code == 201, created.text
        original = created.json()
        response = client.get(f'/api/v1/admin/branches/{tenant["branch_id"]}/integration',
            params={"credential_id":original["id"]}, headers=admin)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert original["token"] not in response.text and "token_hash" not in response.text
        package = response.json()["integration"]
        assert set(package["scopes"]) == set(original["scopes"])
        if profile == "restaurant_agent":
            assert set(package["scopes"]) == RESTAURANT_AGENT_SCOPES
            for name, path in usage_paths.items():
                tool = package["endpoints"][name]
                assert {key: tool[key] for key in ("method", "scope", "url")} == {
                    "method":"POST", "scope":"agent:state",
                    "url":package["api_base_url"]+"/integrations"+path}
                assert tool["request_body"] and tool["responses"]
                assert tool["required_scopes"] == ["agent:state"]
        else:
            assert "agent:state" not in package["scopes"]
            assert not set(usage_paths).intersection(package["endpoints"])
        with SessionLocal() as db:
            assert set(db.get(IntegrationCredential, original["id"]).scopes) == set(original["scopes"])


def test_server_overrides_are_scoped_and_invalid_configuration_cannot_disable_guard(monkeypatch):
    from app.agent_usage import usage_limits
    configure(monkeypatch, business_overrides={"7":AgentUsageOverride(business_tokens=123,alert_percent=75)})
    assert usage_limits(7)["business_tokens"] == 123 and usage_limits(7)["alert_percent"] == 75
    assert usage_limits(8)["business_tokens"] == 50_000_000
    for value in [0,-1,True,1.5]:
        with pytest.raises(ValidationError):
            Settings(_env_file=None,agent_usage_business_tokens=value)
    for overrides in [{"0":{}},{"07":{}},{"x":{}},{"7":{"business_tokens":0}},{"7":{"enabled":False}},{"7":{"sender_tokens":"10"}}]:
        with pytest.raises(ValidationError):
            Settings(_env_file=None,agent_usage_business_overrides=overrides)


def test_additive_migration_and_code_rollback_preserve_existing_records(tenant):
    path = Path(__file__).resolve().parents[1]/'migrations/versions/20261020_0029_agent_model_usage.py'
    spec = importlib.util.spec_from_file_location('isolated_usage_migration',path)
    migration = importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
    assert engine.url.database == ':memory:'
    with engine.begin() as conn:
        before = conn.exec_driver_sql('SELECT id, name FROM businesses ORDER BY id').all()
        AgentModelUsage.__table__.drop(conn)
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade();migration.upgrade();migration.downgrade()
        assert conn.exec_driver_sql('SELECT id, name FROM businesses ORDER BY id').all() == before
        assert conn.exec_driver_sql('SELECT count(*) FROM agent_model_usage').scalar() == 0
