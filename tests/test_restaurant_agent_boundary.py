"""Customer boundaries and durable conversations, using only the test database."""
from datetime import timedelta, time
import pytest
from sqlalchemy import select, func

from app.auth import hash_integration_token
from app.database import SessionLocal
from app.models import (AgentConversation, AgentMessageDelivery, AgentAttentionRequest, IntegrationCredential, Order,
                        KitchenTicket, Payment, BranchSettings, ServiceSchedule, ScheduleShift, utcnow)
from app.schemas import RESTAURANT_AGENT_SCOPES

SENDER = "51999900001"


@pytest.fixture
def agent_headers(tenant):
    with SessionLocal.begin() as db:
        # Purchase-boundary fixtures publish explicit all-week service hours.
        # Schedule tests replace this fixture to exercise absence/closure.
        schedule = ServiceSchedule(business_id=tenant["business_id"], branch_id=tenant["branch_id"],
            name="Horario sintético explícito", kind="primary", active=True)
        db.add(schedule)
        db.flush()
        db.add_all([ScheduleShift(schedule_id=schedule.id, day_of_week=day, starts_at=time(0), ends_at=time(0))
                    for day in range(7)])
        db.add(IntegrationCredential(business_id=tenant["business_id"], branch_id=tenant["branch_id"],
            name="Isolated agent", profile="restaurant_agent", token_prefix="isolated-agent",
            token_hash=hash_integration_token("isolated-agent.token"), scopes=sorted(RESTAURANT_AGENT_SCOPES)))
    return {"Authorization": "Bearer isolated-agent.token"}


def claim(client, headers, message_id="m1", sender=SENDER, text="hola"):
    return client.post("/api/v1/integrations/conversations/claim", headers={**headers, "Idempotency-Key": message_id},
        json={"sender": sender, "message_id": message_id, "message": {"text": text}})


def leased(client, headers):
    response = claim(client, headers)
    assert response.status_code == 200, response.text
    lease = response.json()
    return lease, {**headers, "X-Agent-Conversation": lease["conversation_id"], "X-Agent-Lease": lease["lease_token"]}


def commit(client, headers, lease, *, version=None, token=None, key="c1", state=None, complete=True):
    return client.post(f'/api/v1/integrations/conversations/{lease["conversation_id"]}/commit',
        headers={**headers, "Idempotency-Key": key}, json={"lease_token": token or lease["lease_token"],
        "expected_version": version or lease["version"], "state": state or {},
        "response": {"text": "Hola"}, "complete": complete})


def test_duplicate_message_and_commit_are_replayed_without_new_work(client, agent_headers):
    lease, headers = leased(client, agent_headers)
    queued = claim(client, agent_headers)
    assert queued.json()["status"] == "queued"
    done = commit(client, agent_headers, lease)
    assert done.status_code == 200
    assert commit(client, agent_headers, lease).json() == done.json()
    again = claim(client, agent_headers)
    assert again.json()["status"] == "completed" and again.json()["duplicate"] is True
    assert claim(client, agent_headers, text="otro contenido").status_code == 409


def test_queue_preserves_all_messages_and_next_does_not_require_original_body(client, agent_headers):
    first, _ = leased(client, agent_headers)
    assert claim(client, agent_headers, "m2", text="una pizza").json()["status"] == "queued"
    assert claim(client, agent_headers, "m3", text="para recoger").json()["pending_count"] == 2
    assert commit(client, agent_headers, first).status_code == 200
    url = f'/api/v1/integrations/conversations/{first["conversation_id"]}/next'
    second = client.post(url, headers={**agent_headers, "Idempotency-Key": "next2"}).json()
    assert second["message_id"] == "m2"
    assert commit(client, agent_headers, second, key="c2").status_code == 200
    third = client.post(url, headers={**agent_headers, "Idempotency-Key": "next3"}).json()
    assert third["message_id"] == "m3"
    assert commit(client, agent_headers, third, key="c3").status_code == 200
    assert client.post(url, headers={**agent_headers, "Idempotency-Key": "next4"}).json()["status"] == "idle"


def test_worker_recovers_enqueued_input_and_commits_one_durable_response(client, agent_headers):
    payload = {"sender": SENDER, "message_id": "queued-input", "message": {"text": "hi"}}
    enqueue = client.post("/api/v1/integrations/conversations/enqueue",
        headers={**agent_headers, "Idempotency-Key": "enqueue"}, json=payload)
    assert enqueue.status_code == 200 and enqueue.json()["status"] == "queued"
    lease = client.post("/api/v1/integrations/conversations/work/claim",
        headers={**agent_headers, "Idempotency-Key": "worker1"}, json={}).json()
    assert lease["status"] == "acquired" and lease["message_id"] == "queued-input"
    assert client.post("/api/v1/integrations/conversations/work/claim",
        headers={**agent_headers, "Idempotency-Key": "worker2"}, json={}).json()["status"] == "idle"
    body = {"lease_token": lease["lease_token"], "expected_version": lease["version"], "state": {},
        "complete": True, "response": {"actions": [{"type": "text", "text": "Hola, ¿qué te gustaría pedir?"}]}}
    url = f'/api/v1/integrations/conversations/{lease["conversation_id"]}/commit'
    first = client.post(url, headers={**agent_headers, "Idempotency-Key": "complete1"}, json=body)
    assert first.status_code == 200
    assert client.post(url, headers={**agent_headers, "Idempotency-Key": "complete1"}, json=body).json() == first.json()
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(AgentMessageDelivery)) == 1
    assert client.post("/api/v1/integrations/conversations/enqueue",
        headers={**agent_headers, "Idempotency-Key": "retry-enqueue"}, json=payload).json()["status"] == "completed"


@pytest.fixture
def queued_response(tenant):
    with SessionLocal.begin() as db:
        conversation = AgentConversation(business_id=tenant["business_id"], branch_id=tenant["branch_id"], sender=SENDER)
        db.add(conversation)
        db.flush()
        delivery = AgentMessageDelivery(business_id=tenant["business_id"], branch_id=tenant["branch_id"],
            conversation_id=conversation.id, message_id="ready1", sender=SENDER,
            response={"actions": [{"type": "text", "text": "Comprobante pendiente de revisión"}]})
        db.add(delivery)
        db.flush()
        return delivery.id


def test_response_claim_is_at_most_once_and_lost_ack_is_retryable(client, agent_headers, queued_response):
    url = "/api/v1/integrations/conversations/outbox/claim"
    delivery = client.post(url, headers={**agent_headers, "Idempotency-Key": "send1"}, json={}).json()
    assert delivery["status"] == "claimed" and delivery["delivery_id"] == queued_response
    assert client.post(url, headers={**agent_headers, "Idempotency-Key": "send1"}, json={}).json()["status"] == "uncertain"
    assert client.post(url, headers={**agent_headers, "Idempotency-Key": "send2"}, json={}).json()["status"] == "idle"
    ack_url = f'/api/v1/integrations/conversations/outbox/{queued_response}/ack'
    body = {"delivery_token": delivery["delivery_token"], "status": "sent", "receipt": {"transport_id": "synthetic-ack"}}
    ack = client.patch(ack_url, headers={**agent_headers, "Idempotency-Key": "ack1"}, json=body)
    assert ack.status_code == 200 and ack.json()["status"] == "sent"
    assert client.patch(ack_url, headers={**agent_headers, "Idempotency-Key": "ack1"}, json=body).json() == ack.json()
    assert client.patch(ack_url, headers={**agent_headers, "Idempotency-Key": "ack2"}, json={**body, "status": "not_sent", "receipt": {"transport_attempted": False}}).status_code == 409


def test_only_known_unattempted_response_can_be_released(client, agent_headers, queued_response):
    url = "/api/v1/integrations/conversations/outbox/claim"
    delivery = client.post(url, headers={**agent_headers, "Idempotency-Key": "claim1"}, json={}).json()
    ack_url = f'/api/v1/integrations/conversations/outbox/{queued_response}/ack'
    body = {"delivery_token": delivery["delivery_token"], "status": "not_sent", "receipt": {"transport_attempted": True}}
    assert client.patch(ack_url, headers={**agent_headers, "Idempotency-Key": "release1"}, json=body).status_code == 422
    body["receipt"]["transport_attempted"] = False
    assert client.patch(ack_url, headers={**agent_headers, "Idempotency-Key": "release1"}, json=body).json()["status"] == "pending"
    second = client.post(url, headers={**agent_headers, "Idempotency-Key": "claim2"}, json={}).json()
    assert second["delivery_id"] == delivery["delivery_id"] and second["delivery_token"] != delivery["delivery_token"]
    assert client.patch(ack_url, headers={**agent_headers, "Idempotency-Key": "stale-ack"}, json={**body, "status": "sent", "receipt": {"transport_id": "stale"}}).status_code == 409


def test_expired_worker_cannot_write_or_commit_and_recovery_keeps_intention(client, agent_headers):
    lease, headers = leased(client, agent_headers)
    checkpoint = commit(client, agent_headers, lease, state={"intent": {"key": "stable-order-key"}}, complete=False)
    assert checkpoint.status_code == 200
    with SessionLocal.begin() as db:
        db.get(AgentConversation, lease["conversation_id"]).lease_until = utcnow() - timedelta(seconds=1)
    assert commit(client, agent_headers, lease, version=checkpoint.json()["version"], key="stale").status_code == 409
    denied = client.post("/api/v1/integrations/attention-requests", headers={**headers, "Idempotency-Key": "stale-write"},
        json={"sender": SENDER, "kind": "catering", "summary": "evento"})
    assert denied.status_code == 409
    recovered = claim(client, agent_headers, "m2", text="gracias").json()
    assert recovered["message_id"] == "m1"
    assert recovered["state"]["intent"]["key"] == "stable-order-key"
    assert recovered["lease_token"] != lease["lease_token"]


@pytest.mark.parametrize("method,path", [
    ("GET", "/integrations/context/menu"), ("GET", "/integrations/context/inventory"),
    ("GET", "/integrations/orders/1/status"), ("PATCH", "/integrations/orders/1"),
    ("POST", "/integrations/orders/1/confirm"), ("POST", "/integrations/orders/1/request-human"),
    ("POST", "/integrations/reservations"), ("GET", "/integrations/events")])
def test_agent_profile_cannot_use_legacy_escape_routes(client, agent_headers, method, path):
    response = client.request(method, "/api/v1" + path, headers=agent_headers, json={})
    assert response.status_code == 403, response.text


def test_catering_is_visible_durable_attention_and_never_a_sale(client, tenant, agent_headers, auth_headers):
    _, headers = leased(client, agent_headers)
    payload = {"sender": SENDER, "kind": "catering", "summary": "Solicita comida para un evento", "message_ids": ["m1"]}
    first = client.post("/api/v1/integrations/attention-requests", headers={**headers, "Idempotency-Key": "attention1"}, json=payload)
    assert first.status_code == 201, first.text
    assert client.post("/api/v1/integrations/attention-requests", headers={**headers, "Idempotency-Key": "attention1"}, json=payload).json() == first.json()
    visible = client.get(f'/api/v1/agent-attention?branch_id={tenant["branch_id"]}', headers=auth_headers).json()
    assert visible[0]["order_id"] is None
    with SessionLocal() as db:
        for model in (Order, KitchenTicket, Payment):
            assert db.scalar(select(func.count()).select_from(model)) == 0
        assert db.scalar(select(func.count()).select_from(AgentAttentionRequest)) == 1
    rejected = client.post(f'/api/v1/agent-attention/{first.json()["id"]}/resolve', headers=headers,
        json={"expected_version": 1, "status": "resolved", "note": "approved"})
    # This fixture deliberately has no Auth provider; 503 also fails closed.
    assert rejected.status_code in {401, 403, 503}
    with SessionLocal() as db:
        assert db.get(AgentAttentionRequest, first.json()["id"]).status == "pending"
    resolved = client.post(f'/api/v1/agent-attention/{first.json()["id"]}/resolve',
        headers={**auth_headers, "Idempotency-Key": "resolve1"},
        json={"expected_version": 1, "status": "resolved", "note": "Se contactó al consumidor"})
    assert resolved.status_code == 200 and resolved.json()["resolved_by"] == "owner-test"


def test_sender_and_tenant_cannot_be_switched(client, tenant, agent_headers, auth_headers):
    lease, headers = leased(client, agent_headers)
    response = client.post("/api/v1/integrations/attention-requests", headers={**headers, "Idempotency-Key": "spoof"},
        json={"sender": "51999900002", "kind": "catering", "summary": "evento"})
    assert response.status_code == 403
    assert client.get(f'/api/v1/agent-attention?branch_id={tenant["other_branch_id"]}', headers=auth_headers).status_code == 403
    assert claim(client, agent_headers, "group", sender="12345@g.us").status_code == 422


def test_profile_provisioning_and_discovery_are_narrow(client, tenant, auth_headers):
    admin = {**auth_headers, "X-Dev-Role": "superadmin"}
    created = client.post("/api/v1/admin/integration-credentials", headers=admin,
        json={"branch_id": tenant["branch_id"], "name": "Agente nuevo", "profile": "restaurant_agent"})
    assert created.status_code == 201, created.text
    assert set(created.json()["scopes"]) == RESTAURANT_AGENT_SCOPES
    package = client.get(f'/api/v1/admin/branches/{tenant["branch_id"]}/integration?credential_id={created.json()["id"]}', headers=admin).json()
    endpoints = package["integration"]["endpoints"]
    assert "conversation_claim" in endpoints and "customer_catalog" in endpoints
    assert not {"inventory", "menu", "update_order", "events", "order_status"}.intersection(endpoints)
    denied = client.post("/api/v1/admin/integration-credentials", headers=admin,
        json={"branch_id": tenant["branch_id"], "name": "Unsafe agent", "profile": "restaurant_agent", "scopes": ["inventory:write"]})
    assert denied.status_code == 422


def test_context_uses_digital_services_and_pos_times(client, tenant, agent_headers):
    with SessionLocal.begin() as db:
        db.add(BranchSettings(business_id=tenant["business_id"], branch_id=tenant["branch_id"],
            pos_delivery=True, digital_delivery=False, digital_takeaway=True, digital_tables=False,
            delivery_min_minutes=31, delivery_max_minutes=47, pickup_minutes=18))
    body = client.get("/api/v1/integrations/context", headers=agent_headers).json()
    assert body["delivery"]["enabled"] is False
    assert body["capabilities"]["order_modes"] == ["takeaway"]
    assert body["estimates"]["pickup_minutes"] == 18
    catalog = client.get("/api/v1/integrations/context/catalog", headers=agent_headers).json()
    assert catalog["complete"] is True and catalog["product_count"] == 1 and catalog["catalog_version"]
    assert "recipes" not in str(catalog) and "available_units" not in str(catalog)
