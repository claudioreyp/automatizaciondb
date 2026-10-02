from copy import deepcopy
from decimal import Decimal
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import AuditEvent, IntegrationEvent, InventoryItem, KitchenTicket, Order, OrderPaymentRequest, Payment, PaymentEvidence, Product, utcnow
from test_agent_checkout import PHONE, open_cash, review, setup, upload
from test_escalar_integrations import create_credential, integration_headers
from test_command_workflow import create_order, send_order


def registered(client, tenant, auth_headers, method="cash"):
    headers, draft = setup(client, tenant, auth_headers, method=method)
    if method == "cash":
        response = client.post(f"/api/v1/integrations/orders/{draft['id']}/cash-confirm",
            headers={**headers, "Idempotency-Key": "register-cash"})
        assert response.status_code == 200, response.text
        return headers, response.json()["order"]
    receipt = upload(client, headers, draft)
    return headers, receipt["order"]


def state(client, headers, order):
    result = client.get(f"/api/v1/integrations/orders/{order['id']}/customer-state",
        headers=headers, params={"sender": PHONE})
    assert result.status_code == 200, result.text
    return result.json()


def add(client, headers, order, *, method=None, key="addition", quantity=1):
    return client.post(f"/api/v1/integrations/orders/{order['id']}/item-batches",
        headers={**headers, "Idempotency-Key": key}, json={"sender": PHONE,
        "expected_version": order["version"], "expected_amount": 20 * quantity,
        "items": [{"product_id": order["items"][0]["product_id"], "quantity": quantity}],
        **({"payment_method": method} if method else {})})


@pytest.mark.parametrize("method", ["cash", "yape"])
@pytest.mark.parametrize("status", ["confirmed", "sent_to_kitchen", "preparing", "ready", "closed", "delivered", "dispatched"])
def test_agent_can_never_change_registered_order(client, tenant, auth_headers, method, status):
    headers, order = registered(client, tenant, auth_headers, method)
    with SessionLocal.begin() as db:
        db.get(Order, order["id"]).status = status
    before = state(client, headers, order)
    allowed = before["allowed_actions"]
    assert not any(allowed[key] for key in ("cancel_order", "revise_items", "change_fulfillment", "change_to_delivery", "change_to_takeaway"))
    assert allowed["editable_item_ids"] == []
    with SessionLocal() as db:
        counts = {model: db.query(model).count() for model in (AuditEvent, KitchenTicket, Payment, PaymentEvidence, OrderPaymentRequest)}
        stock = db.get(InventoryItem, tenant["inventory_id"]).quantity
    revision = {"sender": PHONE, "expected_version": before["version"], "operations": [{"type": "edit",
        "item_id": before["items"][0]["id"], "replacement": {
            "product_id": tenant["product_id"], "quantity": 1, "notes": "Sin cebolla"}}]}
    requests = [
        client.patch(f"/api/v1/integrations/orders/{order['id']}/fulfillment", headers={**headers, "Idempotency-Key": "fulfillment"}, json={
            "sender": PHONE, "expected_version": before["version"], "channel": "delivery",
            "delivery_address": {"address": "Calle Nueva 123", "reference": "Casa blanca"}}),
        client.post(f"/api/v1/integrations/orders/{order['id']}/item-revisions", headers={**headers, "Idempotency-Key": "revision"}, json=revision),
        client.post(f"/api/v1/integrations/orders/{order['id']}/item-revisions", headers={**headers, "Idempotency-Key": "cancel-line"}, json={
            **revision, "operations": [{"type": "cancel", "item_id": before["items"][0]["id"], "reason": "Cliente solicita cancelar"}]}),
        client.patch(f"/api/v1/integrations/orders/{order['id']}", headers={**headers, "Idempotency-Key": "generic-patch"}, json={
            "expected_version": before["version"], "notes": "Cambiar", "payment_method": "cash"}),
        client.post(f"/api/v1/integrations/orders/{order['id']}/cash-confirm", headers={**headers, "Idempotency-Key": "change-to-cash"}),
        client.post(f"/api/v1/integrations/orders/{order['id']}/confirm", headers={**headers, "Idempotency-Key": "reconfirm"}),
    ]
    for result in requests:
        assert result.status_code == 409, result.text
        assert result.json()["code"] == "AGENT_ORDER_ADDITIONS_ONLY"
    after = state(client, headers, order)
    assert after == before
    with SessionLocal() as db:
        assert {model: db.query(model).count() for model in counts} == counts
        assert db.get(InventoryItem, tenant["inventory_id"]).quantity == stock


@pytest.mark.parametrize("declared_source", ["pos", "integration", "manual"])
def test_source_cannot_bypass_integration_policy(client, tenant, auth_headers, declared_source):
    token = create_credential(client, tenant, auth_headers)["token"]
    headers = integration_headers(token)
    created = client.post("/api/v1/integrations/orders/draft", headers={**headers, "Idempotency-Key": "source-create"}, json={
        "branch_id": tenant["branch_id"], "source": declared_source, "channel": "takeaway",
        "payment_method": "cash", "items": [{"product_id": tenant["product_id"], "quantity": 1}]})
    assert created.status_code == 201, created.text
    draft = created.json()
    assert draft["source"] == "integration"
    patched = client.patch(f"/api/v1/integrations/orders/{draft['id']}", headers={**headers, "Idempotency-Key": "before-registration"}, json={
        "expected_version": draft["version"], "notes": "Nota del borrador"})
    assert patched.status_code == 200, patched.text
    registered_order = client.post(f"/api/v1/integrations/orders/{draft['id']}/cash-confirm",
        headers={**headers, "Idempotency-Key": "source-confirm"})
    assert registered_order.status_code == 200, registered_order.text
    blocked = client.patch(f"/api/v1/integrations/orders/{draft['id']}", headers={**headers, "Idempotency-Key": "after-registration"}, json={
        "expected_version": registered_order.json()["order"]["version"], "notes": "Otra nota", "payment_method": "yape"})
    assert blocked.status_code == 409 and blocked.json()["code"] == "AGENT_ORDER_ADDITIONS_ONLY"
    assert client.post(f"/api/v1/integrations/orders/{draft['id']}/cash-confirm",
        headers={**headers, "Idempotency-Key": "source-confirm"}).json() == registered_order.json()


@pytest.mark.parametrize("method", ["cash", "yape"])
def test_additions_require_registered_original(client, tenant, auth_headers, method):
    headers, order = setup(client, tenant, auth_headers, method=method)
    assert state(client, headers, order)["allowed_actions"]["add_items"] is False
    blocked = add(client, headers, order)
    assert blocked.status_code == 409 and blocked.json()["code"] == "INITIAL_ORDER_PENDING"
    with SessionLocal() as db:
        assert len(db.get(Order, order["id"]).items) == 1
        assert db.query(OrderPaymentRequest).count() == 0
        assert db.query(KitchenTicket).count() == 0


def test_yape_draft_cannot_use_generic_confirmation_to_skip_review(client, tenant, auth_headers):
    headers, order = setup(client, tenant, auth_headers)
    result = client.post(f"/api/v1/integrations/orders/{order['id']}/confirm",
        headers={**headers, "Idempotency-Key": "skip-review"})
    assert result.status_code == 409 and result.json()["code"] == "PAYMENT_APPROVAL_REQUIRED"
    with SessionLocal() as db:
        assert db.get(Order, order["id"]).status == "draft"
        assert db.query(KitchenTicket).count() == 0
        assert db.query(Payment).count() == 0


@pytest.mark.parametrize("reason", ["cancel_order", "customer_requested_change", "modificar productos", "cambiar a delivery", "anular pedido", "fulfillment_revision"])
def test_human_request_cannot_turn_into_pending_edit(client, tenant, auth_headers, reason):
    from app.models import IntegrationEvent
    headers, order = registered(client, tenant, auth_headers)
    before = state(client, headers, order)
    result = client.post(f"/api/v1/integrations/orders/{order['id']}/request-human",
        headers={**headers, "Idempotency-Key": "forbidden-human"}, params={"reason": reason})
    assert result.status_code == 409 and result.json()["code"] == "AGENT_ORDER_ADDITIONS_ONLY"
    assert state(client, headers, order) == before
    with SessionLocal() as db:
        assert db.query(IntegrationEvent).filter_by(event_type="human.requested").count() == 0
    help_result = client.post(f"/api/v1/integrations/orders/{order['id']}/request-human",
        headers={**headers, "Idempotency-Key": "assistance"}, params={"reason": "customer_requested_human"})
    assert help_result.status_code == 200, help_result.text


@pytest.mark.parametrize("prepared", [False, True])
def test_cash_addition_is_one_batch_with_one_attributed_command(client, tenant, auth_headers, prepared):
    headers, order = registered(client, tenant, auth_headers)
    with SessionLocal.begin() as db:
        original = db.scalar(select(KitchenTicket).where(KitchenTicket.order_id == order["id"]))
        original_id = original.id
        original_items = deepcopy(original.items_snapshot)
        if prepared:
            original.status = "ready"
            db.get(Order, order["id"]).status = "ready"
    current = state(client, headers, order)
    response = add(client, headers, current)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["command_committed"] is True and result["sent_to_kitchen"] is True
    assert len(result["appended_item_ids"]) == len(result["tickets"]) == 1
    ticket = result["tickets"][0]
    assert ticket["kind"] == "addition" and ticket["sequence"] == 2
    assert ticket["created_by_name"] == "Agente de WhatsApp"
    assert [x["item_id"] for x in ticket["items"]] == result["appended_item_ids"]
    assert result["order"]["total"] == 40 and result["order"]["payment_status"] == "pending"
    assert result["order"]["folio"] == order["folio"]
    assert result["order"]["recent_agent_addition"]["item_count"] == 1
    assert add(client, headers, current).json() == result
    assert add(client, headers, current, key="stale-another-key").status_code == 409
    detail = client.get(f"/api/v1/orders/{order['id']}/detail", headers=auth_headers).json()
    workspace = client.get("/api/v1/orders/workspace", params={"branch_id": tenant["branch_id"], "period": "all"}, headers=auth_headers).json()
    assert detail["order"]["recent_agent_addition"] == workspace["items"][0]["recent_agent_addition"]
    assert workspace["items"][0]["recent_agent_addition"]["source"] == "agent"
    with SessionLocal() as db:
        assert db.get(KitchenTicket, original_id).items_snapshot == original_items
        assert db.query(KitchenTicket).count() == 2
        assert db.query(AuditEvent).filter_by(action="agent.items_added").count() == 1
        assert db.query(AuditEvent).filter_by(action="order.item_batch_added").count() == 1
        assert db.query(Payment).count() == 0
        assert len(db.get(Order, order["id"]).items) == 2


def test_cash_addition_preserves_old_agreed_price_even_when_catalog_changes(client, tenant, auth_headers):
    headers, order = registered(client, tenant, auth_headers)
    with SessionLocal.begin() as db:
        db.get(Product, tenant["product_id"]).price = Decimal(30)
    original = deepcopy(order["items"][0])
    path = f"/api/v1/integrations/orders/{order['id']}/item-batches"
    body = {"sender": PHONE, "expected_version": order["version"], "expected_amount": 30,
            "items": [{"product_id": tenant["product_id"], "quantity": 1}]}
    response = client.post(path, headers={**headers, "Idempotency-Key": "new-price-add"}, json=body)
    assert response.status_code == 200, response.text
    result = response.json()["order"]
    assert result["total"] == 50
    assert result["items"][0] == original
    assert result["items"][1]["line_total"] == 30


@pytest.mark.parametrize("original_method", ["cash", "yape"])
def test_yape_addition_has_its_own_review_and_cannot_change_original_payment(client, tenant, auth_headers, original_method):
    headers, order = registered(client, tenant, auth_headers, original_method)
    open_cash(client, auth_headers, tenant)
    if original_method == "yape":
        with SessionLocal() as db:
            evidence_id = db.scalar(select(PaymentEvidence.id).where(PaymentEvidence.order_id == order["id"]))
        initial = {"order": order, "evidence": {"id": evidence_id}}
        approved = review(client, auth_headers, tenant, initial)
        assert approved.status_code == 200, approved.text
        order = approved.json()["order"]
    before = state(client, headers, order)
    response = add(client, headers, before, method="yape")
    assert response.status_code == 200, response.text
    requested = response.json()
    assert requested["command_committed"] is False and requested["sent_to_kitchen"] is False
    assert requested["tickets"] == [] and requested["appended_item_ids"] == []
    assert requested["order"]["items"] == before["items"]
    assert requested["order"]["total"] == before["total"]
    assert requested["order"]["recent_agent_addition"] is None
    receipt = upload(client, headers, before, requested["payment_request"]["id"], key="addition-receipt")
    review_path = f"/api/v1/payment-evidence/{receipt['evidence']['id']}/review"
    review_body = {"approve": True, "register_id": tenant["register_id"], "expected_version": receipt["order"]["version"]}
    review_headers = {**auth_headers, "Idempotency-Key": "addition-review"}
    after = client.post(review_path, headers=review_headers, json=review_body)
    assert after.status_code == 200, after.text
    result = after.json()
    assert result["command_committed"] is True
    assert result["order"]["payment_method"] == original_method
    assert result["order"]["total"] == 40
    assert result["tickets"][0]["created_by_name"] == "Agente de WhatsApp"
    assert result["order"]["recent_agent_addition"]["item_count"] == 1
    assert client.post(review_path, headers=review_headers, json=review_body).json() == result
    with SessionLocal() as db:
        assert db.query(KitchenTicket).count() == 2
        assert db.query(AuditEvent).filter_by(action="agent.items_added").count() == 1
        assert db.query(Payment).count() == (2 if original_method == "yape" else 1)
        assert db.query(OrderPaymentRequest).count() == 1
        events = list(db.scalars(select(IntegrationEvent).where(IntegrationEvent.event_type == "payment.approved")))
        additions = [event for event in events if event.payload.get("purpose") == "addition"]
        assert len(additions) == 1
        assert additions[0].payload["appended_item_ids"] == result["appended_item_ids"]
        assert additions[0].payload["ticket_ids"] == [ticket["id"] for ticket in result["tickets"]]
        assert additions[0].payload["addition_amount"] == 20


def test_cash_addition_does_not_overwrite_paid_yape_original(client, tenant, auth_headers):
    headers, order = registered(client, tenant, auth_headers, "yape")
    open_cash(client, auth_headers, tenant)
    with SessionLocal() as db:
        evidence_id = db.scalar(select(PaymentEvidence.id).where(PaymentEvidence.order_id == order["id"]))
    approved = review(client, auth_headers, tenant, {"order": order, "evidence": {"id": evidence_id}})
    assert approved.status_code == 200, approved.text
    current = state(client, headers, approved.json()["order"])
    response = add(client, headers, current, method="cash")
    assert response.status_code == 200, response.text
    assert response.json()["order"]["payment_method"] == "yape"
    assert response.json()["order"]["payment_status"] == "partial"
    assert response.json()["command_committed"] is True
    with SessionLocal() as db:
        assert [p.amount for p in db.scalars(select(Payment))] == [Decimal(20)]
        assert db.get(PaymentEvidence, evidence_id).status == "paid"
        assert db.query(KitchenTicket).count() == 2


def test_rejected_yape_addition_never_changes_products_or_command(client, tenant, auth_headers):
    headers, order = registered(client, tenant, auth_headers, "cash")
    requested = add(client, headers, order, method="yape")
    assert requested.status_code == 200, requested.text
    receipt = upload(client, headers, order, requested.json()["payment_request"]["id"], key="rejected-proof")
    rejected = review(client, auth_headers, tenant, receipt, approve=False, key="reject-addition")
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["order"]["items"] == order["items"]
    assert rejected.json()["order"]["total"] == order["total"]
    assert rejected.json()["command_committed"] is False
    assert rejected.json()["order"]["recent_agent_addition"] is None
    with SessionLocal() as db:
        assert db.query(KitchenTicket).count() == 1
        assert db.query(Payment).count() == 0
        assert db.query(AuditEvent).filter_by(action="agent.items_added").count() == 0
        assert not any(event.payload.get("purpose") == "addition" for event in db.scalars(select(IntegrationEvent)))


def test_integration_access_cannot_edit_actual_pos_order(client, tenant, auth_headers):
    order = create_order(client, tenant, auth_headers)
    sent = send_order(client, order, auth_headers)["order"]
    assert sent["source"] == "pos"
    token = create_credential(client, tenant, auth_headers)["token"]
    headers = integration_headers(token)
    response = client.patch(f"/api/v1/integrations/orders/{order['id']}", headers={**headers, "Idempotency-Key": "agent-pos-edit"}, json={
        "expected_version": sent["version"], "notes": "Cambiar pedido manual"})
    assert response.status_code == 409 and response.json()["code"] == "AGENT_ORDER_ADDITIONS_ONLY"
    assert client.post(f"/api/v1/integrations/orders/{order['id']}/cash-confirm",
        headers={**headers, "Idempotency-Key": "agent-pos-cash"}).status_code == 409
    with SessionLocal() as db:
        assert db.get(Order, order["id"]).version == sent["version"]
        assert db.get(Order, order["id"]).notes == sent["notes"]


@pytest.mark.parametrize("status", ["dispatched", "delivered", "closed"])
def test_addition_attribution_survives_closed_history_and_recent_window(client, tenant, auth_headers, status):
    headers, order = registered(client, tenant, auth_headers)
    response = add(client, headers, order)
    assert response.status_code == 200, response.text
    with SessionLocal.begin() as db:
        event = db.scalar(select(AuditEvent).where(AuditEvent.action == "agent.items_added"))
        older = utcnow() - timedelta(days=5)
        event.created_at = older
        event.payload = {**event.payload, "at": older.isoformat()}
        db.get(Order, order["id"]).status = status
    detail = client.get(f"/api/v1/orders/{order['id']}/detail", headers=auth_headers).json()
    workspace = client.get("/api/v1/orders/workspace", params={"branch_id": tenant["branch_id"], "period": "all"}, headers=auth_headers).json()
    assert detail["order"]["recent_modification"] is None
    assert workspace["items"][0]["recent_modification"] is None
    marker = detail["order"]["recent_agent_addition"]
    assert marker["source"] == "agent" and marker["item_count"] == 1
    assert workspace["items"][0]["recent_agent_addition"] == marker


def test_old_addition_reply_replays_after_optional_method_is_added(client, tenant, auth_headers):
    from app.agent_checkout import fingerprint
    from app.agent_checkout_api import Addition
    from app.models import IdempotencyRecord
    headers, order = registered(client, tenant, auth_headers)
    body = {"sender": PHONE, "expected_version": order["version"], "expected_amount": 20,
            "items": [{"product_id": tenant["product_id"], "quantity": 1}]}
    path = f"/api/v1/integrations/orders/{order['id']}/item-batches"
    request_headers = {**headers, "Idempotency-Key": "pre-deployment-addition"}
    first = client.post(path, headers=request_headers, json=body)
    assert first.status_code == 200, first.text
    old_content = Addition.model_validate(body).model_dump(mode="json")
    old_content.pop("payment_method")
    legacy_result = {"order": first.json()["order"], "sent_to_kitchen": True}
    with SessionLocal.begin() as db:
        record = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.scope == f"agent-addition:{order['id']}"))
        record.response_body = {"request_digest": fingerprint(old_content), "result": legacy_result}
    replayed = client.post(path, headers=request_headers, json=body)
    assert replayed.status_code == 200 and replayed.json() == legacy_result
    with SessionLocal() as db:
        assert len(db.get(Order, order["id"]).items) == 2
        assert db.query(KitchenTicket).count() == 2
        assert db.query(AuditEvent).filter_by(action="agent.items_added").count() == 1


def test_manual_pos_can_still_edit_cancel_and_add(client, tenant, auth_headers):
    original = create_order(client, tenant, auth_headers, items=[
        {"product_id": tenant["product_id"], "quantity": 1}, {"product_id": tenant["product_id"], "quantity": 1}])
    sent = send_order(client, original, auth_headers)
    revised = client.post(f"/api/v1/orders/{original['id']}/item-revisions", headers={**auth_headers, "Idempotency-Key": "manual-edit"}, json={
        "expected_version": sent["order"]["version"], "operations": [{"type": "edit", "item_id": original["items"][0]["id"],
        "replacement": {"product_id": tenant["product_id"], "quantity": 1, "notes": "Sin sal"}}]})
    assert revised.status_code == 201, revised.text
    added = client.post(f"/api/v1/orders/{original['id']}/item-batches", headers={**auth_headers, "Idempotency-Key": "manual-add"}, json={
        "expected_version": revised.json()["order"]["version"], "items": [{"product_id": tenant["product_id"], "quantity": 1}]})
    assert added.status_code == 201, added.text
    assert added.json()["tickets"][0]["context"].get("agent_addition") is None
    assert added.json()["order"]["recent_agent_addition"] is None
    cancelled = client.post(f"/api/v1/orders/{original['id']}/transition", headers=auth_headers, json={"status": "cancelled", "reason": "Error de caja"})
    assert cancelled.status_code == 200, cancelled.text
