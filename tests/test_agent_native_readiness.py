"""Read-only scheduling hints; real claims retain every execution permission."""
from datetime import timedelta
from types import SimpleNamespace
import json

import pytest
from sqlalchemy import and_, event, exists, or_, select

from app.agent_contracts import NativeQueueReadiness
from app.agent_runtime_api import contract_match, delivery_candidates_query, work_candidates_query
from app.auth import hash_integration_token
from app.database import Base, SessionLocal, engine
from app.integration_package import integration_package
from app.models import AgentConversation, AgentMessageDelivery, Branch, IdempotencyRecord, IntegrationCredential, utcnow
from app.schemas import RESTAURANT_AGENT_SCOPES
from test_restaurant_agent_boundary import agent_headers, SENDER

PATH = "/api/v1/integrations/conversations/native/readiness"


def all_rows():
    with SessionLocal() as db:
        return {table.name: [tuple(str(value) for value in row) for row in db.execute(select(table)).all()]
                for table in Base.metadata.sorted_tables}


def read(client, headers, expected):
    before = all_rows()
    queries = []
    def observe(conn, cursor, sql, parameters, context, many):
        assert sql.lstrip().split()[0].upper() not in {"INSERT", "UPDATE", "DELETE", "REPLACE"}, sql
        if "agent_conversations" in sql or "agent_message_deliveries" in sql:
            queries.append(sql)
    event.listen(engine, "before_cursor_execute", observe)
    try:
        response = client.get(PATH, headers=headers)
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert response.status_code == 200, response.text
    assert all_rows() == before
    assert len(queries) == 1 and "FOR UPDATE" not in queries[0].upper()
    body = response.json()
    assert set(body) == {"protocol_version", "runtime_contract", "business_id", "branch_id", "ready", "checked_at"}
    assert NativeQueueReadiness.model_validate(body).ready is expected
    assert response.headers["cache-control"] == "no-store" and response.headers["pragma"] == "no-cache"
    return body


def enqueue(client, headers, *, message_id="native-message", sender=SENDER, native=True):
    route = "/api/v1/integrations/conversations/" + ("native/enqueue" if native else "enqueue")
    response = client.post(route, headers={**headers, "Idempotency-Key": "enqueue-" + message_id},
        json={"sender": sender, "message_id": message_id,
              "message": {"text": "Hola", "messages": [{"id": message_id, "text": "Hola"}]}})
    assert response.status_code == 200, response.text
    return response.json()


def age_pending(conversation_id):
    with SessionLocal.begin() as db:
        row = db.get(AgentConversation, conversation_id)
        row.pending = [{**entry, "accepted_at": (utcnow()-timedelta(seconds=30)).isoformat()} for entry in row.pending]


def credential(business_id, branch_id, name, *, profile="restaurant_agent", scopes=None):
    token = name + ".synthetic-test-token"
    with SessionLocal.begin() as db:
        db.add(IntegrationCredential(business_id=business_id, branch_id=branch_id, name=name, profile=profile,
            token_prefix=name, token_hash=hash_integration_token(token), scopes=scopes or sorted(RESTAURANT_AGENT_SCOPES)))
    return {"Authorization": "Bearer " + token}


def test_empty_queue_is_one_read_only_query_without_private_fields(client, agent_headers):
    body = read(client, agent_headers, False)
    assert body["runtime_contract"] == "native_v1" and body["protocol_version"] == 1
    assert not any(key in json.dumps(body) for key in ("sender", "message_id", "lease", "delivery_token", "actions"))


@pytest.mark.parametrize("profile", ["restaurant_agent", "generic"])
def test_read_never_updates_credential_usage_for_either_profile(client, tenant, agent_headers, profile):
    with SessionLocal.begin() as db:
        row = db.scalar(select(IntegrationCredential).where(IntegrationCredential.token_prefix == "isolated-agent"))
        row.profile = profile
        row.last_used_at = utcnow()-timedelta(days=1)
    read(client, agent_headers, False)


def test_hint_does_not_cache_false_or_claim_quiet_window_input(client, agent_headers):
    read(client, agent_headers, False)
    queued = enqueue(client, agent_headers)
    read(client, agent_headers, True)
    with SessionLocal() as db:
        row = db.get(AgentConversation, queued["conversation_id"])
        assert row.current_message_id is None and row.lease_hash is None and row.lease_until is None
        assert len(row.pending) == 1 and row.pending[0]["batching"] is True


def test_native_hint_ignores_legacy_work_and_unaccepted_processing(client, agent_headers):
    queued = enqueue(client, agent_headers, native=False)
    read(client, agent_headers, False)
    with SessionLocal.begin() as db:
        row = db.get(AgentConversation, queued["conversation_id"])
        row.pending = []
        row.current_message_id = "unaccepted"
        row.state = {"_processing_message": {"_runtime_contract": "native_v1"}}
        for record in db.scalars(select(IdempotencyRecord).where(IdempotencyRecord.scope == "agent-input:"+row.id)):
            record.response_body = {**record.response_body, "status": "completed"}
    read(client, agent_headers, False)


def test_two_ready_observers_still_compete_under_original_lease_and_expiry(client, agent_headers):
    queued = enqueue(client, agent_headers)
    age_pending(queued["conversation_id"])
    read(client, agent_headers, True)
    read(client, agent_headers, True)
    first = client.post("/api/v1/integrations/conversations/native/work/claim",
        headers={**agent_headers, "Idempotency-Key": "first-worker"}, json={})
    assert first.status_code == 200 and first.json()["status"] == "acquired", first.text
    second = client.post("/api/v1/integrations/conversations/native/work/claim",
        headers={**agent_headers, "Idempotency-Key": "second-worker"}, json={})
    assert second.status_code == 200 and second.json()["status"] == "idle", second.text
    read(client, agent_headers, False)
    with SessionLocal.begin() as db:
        db.get(AgentConversation, queued["conversation_id"]).lease_until = utcnow()-timedelta(seconds=1)
    read(client, agent_headers, True)


def test_awaiting_media_is_conservative_ready_without_expiry_mutation(client, agent_headers):
    queued = enqueue(client, agent_headers)
    with SessionLocal.begin() as db:
        row = db.get(AgentConversation, queued["conversation_id"])
        row.pending = [{**row.pending[0], "awaiting_media": {"expires_at": (utcnow()+timedelta(seconds=45)).isoformat()}}]
    read(client, agent_headers, True)


@pytest.mark.parametrize("native_first", [False, True])
def test_mixed_runtime_fifo_does_not_skip_the_head(client, agent_headers, native_first):
    first = enqueue(client, agent_headers, message_id="fifo-first", native=native_first)
    enqueue(client, agent_headers, message_id="fifo-second", native=not native_first)
    read(client, agent_headers, native_first)
    with SessionLocal() as db:
        pending = db.get(AgentConversation, first["conversation_id"]).pending
        assert [entry["message_id"] for entry in pending] == ["fifo-first", "fifo-second"]
    if not native_first:
        claim = client.post("/api/v1/integrations/conversations/native/work/claim",
            headers={**agent_headers, "Idempotency-Key": "cannot-skip-legacy"}, json={})
        assert claim.status_code == 200 and claim.json()["status"] == "idle"


def test_two_consumers_share_hint_without_exposing_identity(client, agent_headers):
    first = enqueue(client, agent_headers, message_id="consumer-one", sender="519990090001")
    second = enqueue(client, agent_headers, message_id="consumer-two", sender="519990090002")
    assert first["conversation_id"] != second["conversation_id"]
    body = read(client, agent_headers, True)
    assert "519990090001" not in json.dumps(body) and "519990090002" not in json.dumps(body)


def test_only_claim_expires_media_and_creates_the_failure_notice(client, agent_headers):
    queued = enqueue(client, agent_headers, message_id="expired-media")
    with SessionLocal.begin() as db:
        row = db.get(AgentConversation, queued["conversation_id"])
        row.pending = [{**row.pending[0], "awaiting_media": {"expires_at": (utcnow()-timedelta(seconds=1)).isoformat()}}]
    read(client, agent_headers, True)
    claim = client.post("/api/v1/integrations/conversations/native/work/claim",
        headers={**agent_headers, "Idempotency-Key": "media-expiry-owner"}, json={})
    assert claim.status_code == 200 and claim.json()["status"] == "idle", claim.text
    with SessionLocal() as db:
        row = db.get(AgentConversation, queued["conversation_id"])
        deliveries = db.scalars(select(AgentMessageDelivery).where(AgentMessageDelivery.conversation_id == row.id)).all()
        assert row.pending == [] and len(deliveries) == 1
        assert deliveries[0].response["_media_failure_notice"] is True
    # The next hint sees the newly pending notice; it does not dispatch it.
    read(client, agent_headers, True)


@pytest.mark.parametrize("status,native,expected", [("pending", True, True), ("uncertain", True, False),
    ("sent", True, False), ("suppressed", True, False), ("pending", False, False)])
def test_delivery_hint_never_claims_or_redispatches_transport(client, tenant, agent_headers, status, native, expected):
    with SessionLocal.begin() as db:
        row = AgentConversation(business_id=tenant["business_id"], branch_id=tenant["branch_id"], sender=SENDER)
        db.add(row); db.flush()
        db.add(AgentMessageDelivery(business_id=row.business_id, branch_id=row.branch_id, conversation_id=row.id,
            sender=SENDER, message_id="pending-response", status=status, response={"_runtime_contract": "native_v1" if native else "legacy_v1",
            "actions": [{"type": "text", "text": "Sintético"}]}))
    read(client, agent_headers, expected)


def test_foreign_business_and_same_business_other_branch_do_not_wake_worker(client, tenant, agent_headers):
    other = credential(tenant["other_business_id"], tenant["other_branch_id"], "foreign-agent")
    enqueue(client, other, message_id="foreign")
    with SessionLocal.begin() as db:
        branch = Branch(business_id=tenant["business_id"], slug="second", name="Second synthetic")
        db.add(branch); db.flush(); branch_id = branch.id
    other_branch = credential(tenant["business_id"], branch_id, "other-branch-agent")
    enqueue(client, other_branch, message_id="other-branch")
    own = read(client, agent_headers, False)
    assert own["business_id"] == tenant["business_id"] and own["branch_id"] == tenant["branch_id"]
    read(client, other, True)
    read(client, other_branch, True)
    attempted = client.get(PATH, headers=agent_headers, params={"business_id": tenant["other_business_id"], "branch_id": branch_id})
    assert attempted.status_code == 200 and attempted.json()["ready"] is False
    assert attempted.json()["business_id"] == own["business_id"] and attempted.json()["branch_id"] == own["branch_id"]


@pytest.mark.parametrize("fault,status", [("scope", 403), ("revoked", 401), ("inactive", 401), ("expired", 401), ("branch_inactive", 403)])
def test_readiness_preserves_authentication_and_scope_requirements(client, tenant, agent_headers, fault, status):
    with SessionLocal.begin() as db:
        row = db.scalar(select(IntegrationCredential).where(IntegrationCredential.token_prefix == "isolated-agent"))
        if fault == "scope": row.scopes = []
        elif fault == "revoked": row.revoked_at = utcnow()
        elif fault == "inactive": row.active = False
        elif fault == "expired": row.expires_at = utcnow()-timedelta(seconds=1)
        else: db.get(Branch, tenant["branch_id"]).active = False
    response = client.get(PATH, headers=agent_headers)
    assert response.status_code == status and response.json()["code"] in {"FORBIDDEN", "UNAUTHENTICATED"}


def test_unscoped_legacy_service_token_is_rejected_stably(client):
    response = client.get(PATH, headers={"X-Integration-Token": "test-integration-token"})
    assert response.status_code == 403 and response.json()["code"] == "FORBIDDEN"
    assert client.get(PATH).status_code == 401


@pytest.mark.parametrize("native", [True, False])
def test_shared_query_filters_are_exactly_the_previous_claim_predicates(tenant, native):
    scope = SimpleNamespace(business_id=tenant["business_id"], branch_id=tenant["branch_id"])
    at = utcnow()
    accepted = exists(select(IdempotencyRecord.id).where(IdempotencyRecord.business_id == AgentConversation.business_id,
        IdempotencyRecord.scope == "agent-input:"+AgentConversation.id, IdempotencyRecord.response_body["status"].as_string() == "accepted"))
    original_work = select(AgentConversation).where(AgentConversation.business_id == scope.business_id,
        AgentConversation.branch_id == scope.branch_id, accepted,
        or_(and_(AgentConversation.current_message_id.is_not(None), contract_match(AgentConversation.state["_processing_message"]["_runtime_contract"].as_string(), native)),
            and_(AgentConversation.current_message_id.is_(None), contract_match(AgentConversation.pending[0]["_runtime_contract"].as_string(), native))),
        or_(AgentConversation.lease_until.is_(None), AgentConversation.lease_until <= at))
    original_delivery = select(AgentMessageDelivery).where(AgentMessageDelivery.business_id == scope.business_id,
        AgentMessageDelivery.branch_id == scope.branch_id, AgentMessageDelivery.status == "pending",
        contract_match(AgentMessageDelivery.response["_runtime_contract"].as_string(), native))
    for actual, previous in [(work_candidates_query(scope, native=native, evaluated_at=at), original_work),
                             (delivery_candidates_query(scope, native=native), original_delivery)]:
        assert str(actual.compile()) == str(previous.compile())
        assert actual.compile().params == previous.compile().params


def test_openapi_package_and_health_publish_111_typed_read_only_contract(client, tenant):
    document = client.get("/openapi.json").json()
    operation = document["paths"][PATH]["get"]
    assert document["info"]["version"] == "1.1.1"
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("/NativeQueueReadiness")
    schema = document["components"]["schemas"]["NativeQueueReadiness"]
    assert set(schema["required"]) >= {"business_id", "branch_id", "ready", "checked_at"}
    assert schema["properties"]["checked_at"]["format"] == "date-time"
    assert operation["x-read-only"] is True and operation["x-idempotency"] == "none"
    assert operation["x-conversation-auth"] == "none" and operation["security"] == [{"IntegrationBearer": []}]
    package = integration_package("https://example.test/api/v1", tenant["business_id"], tenant["branch_id"], ["agent:state"], "restaurant_agent")
    assert package["api_contract_version"] == "1.1.1" and "NativeQueueReadiness" in package["components"]["schemas"]
    assert package["endpoints"]["conversations_native_readiness"]["read_only"] is True
    health = client.get("/api/v1/health").json()
    assert health["api_contract_version"] == "1.1.1" and "native_queue_readiness" in health["capabilities"]
