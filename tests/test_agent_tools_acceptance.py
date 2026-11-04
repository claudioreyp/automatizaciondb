"""Business boundaries for tools, with synthetic data and no external transports."""
from datetime import datetime, time, timedelta, timezone

import pytest
from app.auth import hash_integration_token
from app.database import SessionLocal
from app.models import (AgentConversation, AgentMessageDelivery, Branch, DeliveryAssignment,
    IntegrationCredential, KitchenTicket, Modifier, ModifierGroup, Order, OrderPaymentRequest,
    Payment, PrintJob, Product, ProductModifierGroup, ScheduleShift, ServiceSchedule, utcnow)
from app.schemas import RESTAURANT_AGENT_SCOPES
from test_agent_order_fulfillment import add, registered, state
from test_restaurant_agent_boundary import agent_headers, leased, SENDER


@pytest.mark.parametrize("dispatched", [False, True])
def test_actual_dispatch_controls_additions_even_if_order_status_is_stale(
    client, tenant, auth_headers, dispatched,
):
    headers, order = registered(client, tenant, auth_headers)
    with SessionLocal.begin() as db:
        saved = db.get(Order, order["id"])
        saved.status = "confirmed"
        db.add(DeliveryAssignment(business_id=saved.business_id, branch_id=saved.branch_id,
            order_id=saved.id, status="dispatched" if dispatched else "assigned",
            dispatched_at=utcnow() if dispatched else None))
    before = state(client, headers, order)
    assert before["status"] == "confirmed"
    assert before["dispatch"]["dispatched"] is dispatched
    assert (before["dispatch"]["dispatched_at"] is not None) is dispatched
    assert before["allowed_actions"]["add_items"] is (not dispatched)
    assert before["allowed_actions"]["change_fulfillment"] is (not dispatched)
    if dispatched:
        assert before["allowed_actions"]["reasons"]["add_items"] == "ORDER_ITEMS_LOCKED"
    with SessionLocal() as db:
        counts = {model: db.query(model).count() for model in
                  (Payment, OrderPaymentRequest, KitchenTicket, PrintJob)}
    added = add(client, headers, before)
    if dispatched:
        assert added.status_code == 409, added.text
        assert added.json()["code"] == "ORDER_ITEMS_LOCKED"
        assert state(client, headers, order) == before
        with SessionLocal() as db:
            assert {model: db.query(model).count() for model in counts} == counts
    else:
        assert added.status_code == 200, added.text
        with SessionLocal() as db:
            saved = db.get(Order, order["id"])
            assert len(saved.items) == 2
            assert db.query(KitchenTicket).count() == counts[KitchenTicket] + 1


def test_second_restaurant_requires_sauce_and_rejects_sold_out_or_foreign_choices(
    client, tenant, agent_headers,
):
    with SessionLocal.begin() as db:
        db.get(Branch, tenant["branch_id"]).accepted_payment_methods = ["cash", "yape"]
        branch = db.get(Branch, tenant["other_branch_id"])
        branch.accepted_payment_methods = ["cash", "yape"]
        schedule = ServiceSchedule(business_id=branch.business_id, branch_id=branch.id,
            name="Wings hours", kind="primary", active=True)
        db.add(schedule); db.flush()
        db.add_all([ScheduleShift(schedule_id=schedule.id, day_of_week=day,
            starts_at=time(0), ends_at=time(0)) for day in range(7)])
        wings = Product(business_id=branch.business_id, branch_id=branch.id,
            sku="WINGS-SYNTHETIC", name="Alitas", price=18)
        sauce = ModifierGroup(business_id=branch.business_id, branch_id=branch.id,
            name="Salsa obligatoria", minimum=0, maximum=1, required=True)
        db.add_all([wings, sauce]); db.flush()
        db.add(ProductModifierGroup(product_id=wings.id, group_id=sauce.id))
        available = Modifier(group_id=sauce.id, name="BBQ", price_delta=0)
        sold_out = Modifier(group_id=sauce.id, name="Buffalo", price_delta=0, available=False)
        db.add_all([available, sold_out])
        db.add(IntegrationCredential(business_id=branch.business_id, branch_id=branch.id,
            name="Isolated wings agent", profile="restaurant_agent", token_prefix="isolated-wings",
            token_hash=hash_integration_token("isolated-wings.token"), scopes=sorted(RESTAURANT_AGENT_SCOPES)))
        db.flush()
        wings_id, available_id, sold_out_id = wings.id, available.id, sold_out.id
    wings_auth = {"Authorization": "Bearer isolated-wings.token"}
    _, headers = leased(client, wings_auth)
    path = "/api/v1/integrations/orders/validate-selection"
    body = {"channel": "takeaway", "payment_method": "cash", "items": [{"product_id": wings_id}]}
    missing = client.post(path, headers=headers, json=body)
    assert missing.status_code == 200, missing.text
    assert {item["code"] for item in missing.json()["missing_fields"]} == {"MODIFIER_MINIMUM_REQUIRED"}
    assert missing.json()["missing_fields"][0]["minimum"] == 1
    options = {choice["id"]: choice["available"] for choice in missing.json()["missing_fields"][0]["options"]}
    assert options == {available_id: True, sold_out_id: False}
    body["items"][0]["modifiers"] = [{"modifier_id": sold_out_id}]
    unavailable = client.post(path, headers=headers, json=body).json()
    assert not unavailable["ready_for_preview"]
    assert {item["code"] for item in unavailable["issues"]} == {"MODIFIER_UNAVAILABLE"}
    body["items"][0]["modifiers"] = [{"modifier_id": available_id}]
    valid = client.post(path, headers=headers, json=body)
    assert valid.status_code == 200 and valid.json()["ready_for_preview"], valid.text
    _, pizza_headers = leased(client, agent_headers)
    foreign = client.post(path, headers=pizza_headers, json=body).json()
    assert {item["code"] for item in foreign["issues"]} == {"PRODUCT_UNAVAILABLE"}
    assert "Salsa obligatoria" not in str(foreign)
    with SessionLocal() as db:
        assert db.query(Order).count() == 0
        assert db.query(Payment).count() == 0
        assert db.query(KitchenTicket).count() == 0


def test_native_input_batches_equal_legitimate_messages_but_not_provider_redelivery(
    client, agent_headers, monkeypatch,
):
    from app import agent_runtime_api as runtime
    clock = [datetime(2026, 11, 3, 15, tzinfo=timezone.utc)]
    monkeypatch.setattr(runtime, "utcnow", lambda: clock[0])

    def enqueue(identifier, text="una pizza", key=None):
        result = client.post("/api/v1/integrations/conversations/native/enqueue",
            headers={**agent_headers, "Idempotency-Key": key or identifier},
            json={"sender": SENDER, "message_id": identifier, "message": {"text": text}})
        assert result.status_code == 200, result.text
        return result.json()

    first = enqueue("provider-1")
    duplicate = enqueue("provider-1", key="redelivery-1")
    assert duplicate["pending_count"] == 1
    second = enqueue("provider-2")
    assert second["pending_count"] == 2
    clock[0] += timedelta(seconds=1)
    claimed = client.post("/api/v1/integrations/conversations/native/work/claim",
        headers={**agent_headers, "Idempotency-Key": "worker-1"}, json={})
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()
    assert lease["status"] == "acquired"
    assert lease["input_ids"] == ["provider-1", "provider-2"]
    assert lease["message"]["text"] == "una pizza\nuna pizza"
    assert [source["id"] for source in lease["message"]["messages"]] == lease["input_ids"]
    assert enqueue("provider-3", text="mejor dos pizzas")["pending_count"] == 1
    freshness = client.post(f'/api/v1/integrations/conversations/{lease["conversation_id"]}/freshness',
        headers={**agent_headers, "Idempotency-Key": "freshness-1"},
        json={"lease_token": lease["lease_token"], "expected_version": lease["version"]})
    assert freshness.status_code == 200 and freshness.json()["status"] == "superseded", freshness.text
    commit = client.post(f'/api/v1/integrations/conversations/{lease["conversation_id"]}/commit',
        headers={**agent_headers, "Idempotency-Key": "stale-completion"},
        json={"lease_token": lease["lease_token"], "expected_version": lease["version"],
            "state": {}, "complete": True, "response": {"actions": [{"type": "text", "text": "Una pizza"}]}})
    assert commit.status_code == 409, commit.text
    assert commit.json()["code"] == "INPUT_SUPERSEDED"
    with SessionLocal() as db:
        conversation = db.get(AgentConversation, first["conversation_id"])
        assert [entry["message_id"] for entry in conversation.pending] == ["provider-3"]
        assert db.query(AgentMessageDelivery).count() == 0
        assert db.query(Order).count() == 0
