from copy import deepcopy
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError

from app import api as api_module
from app.database import SessionLocal
from app.models import AuditEvent, CashSession, KitchenTicket, Order, Payment, PaymentEvidence, PrintJob, RestaurantTable
from test_command_workflow import create_order, send_order
from test_pos_printing import enable, jobs


def current_order(client, order, headers):
    response = client.get(f"/api/v1/orders/{order['id']}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def start(client, order, headers, key=None):
    body = {"expected_version": order["version"]}
    request_headers = {**headers, "Idempotency-Key": key or str(uuid4())}
    return client.post(f"/api/v1/orders/{order['id']}/table-checkout/start", json=body, headers=request_headers)


def pay(client, order, headers, payments, key=None):
    return client.post(f"/api/v1/orders/{order['id']}/table-checkout/pay", json={
        "expected_version": order["version"], "payments": payments,
    }, headers={**headers, "Idempotency-Key": key or str(uuid4())})


def record_prior_payment(client, order, headers, amount):
    response = client.post(f"/api/v1/orders/{order['id']}/payments", json={
        "expected_version": order["version"], "method": "cash", "amount": amount,
    }, headers={**headers, "Idempotency-Key": "prior-panel-payment"})
    assert response.status_code == 201, response.text
    return response.json()


def create_sent_table(client, tenant, headers):
    order = create_order(client, tenant, headers, dine_in=True)
    return send_order(client, order, headers)


@pytest.mark.parametrize("kitchen_complete", [False, True])
def test_fully_paid_table_can_release_without_another_payment(client, tenant, auth_headers, monkeypatch, kitchen_complete):
    enable(tenant)
    sent = create_sent_table(client, tenant, auth_headers)
    original_ticket = sent["tickets"][0]
    if kitchen_complete:
        completed = client.post(f"/api/v1/kitchen/commands/{original_ticket['id']}/complete", json={
            "expected_status": "queued", "expected_version": original_ticket["version"],
        }, headers={**auth_headers, "Idempotency-Key": "ready-before-release"})
        assert completed.status_code == 200, completed.text
    prior = record_prior_payment(client, current_order(client, sent["order"], auth_headers), auth_headers, 20)
    with SessionLocal() as db:
        saved_payment = deepcopy(db.get(Payment, prior["payment"]["id"]).__dict__)
        session_version = db.get(CashSession, prior["payment"]["cash_session_id"]).version
    begun = start(client, prior["order"], auth_headers, "paid-table-start")
    assert begun.status_code == 200, begun.text
    receipt = next(job for job in jobs(client, sent["order"], auth_headers)["items"] if job["job_type"] == "customer_receipt")
    assert receipt["payload"]["paid_amount"] == 20
    assert receipt["payload"]["remaining_amount"] == 0
    assert start(client, prior["order"], auth_headers, "paid-table-start").json() == begun.json()
    assert start(client, begun.json()["order"], auth_headers).status_code == 200
    notifications = []

    async def capture(branch_id, event_type, payload):
        notifications.append(event_type)

    monkeypatch.setattr(api_module.hub, "broadcast", capture)
    released = pay(client, begun.json()["order"], auth_headers, [], "paid-table-release")
    assert released.status_code == 200, released.text
    assert released.json()["payments"] == []
    assert released.json()["order"]["table_released_at"] is not None
    assert released.json()["order"]["payment_status"] == "paid"
    assert released.json()["order"]["status"] == ("closed" if kitchen_complete else "sent_to_kitchen")
    assert released.json()["table"]["status"] == "available"
    assert notifications == ["order.updated", "table.updated"]
    assert pay(client, begun.json()["order"], auth_headers, [], "paid-table-release").json() == released.json()
    assert notifications == ["order.updated", "table.updated"]
    assert len(jobs(client, sent["order"], auth_headers)["items"]) == 2
    with SessionLocal() as db:
        assert db.query(Payment).count() == 1
        stored_payment = db.get(Payment, prior["payment"]["id"])
        assert {key: value for key, value in stored_payment.__dict__.items() if not key.startswith("_")} == {
            key: value for key, value in saved_payment.items() if not key.startswith("_")
        }
        assert db.get(CashSession, prior["payment"]["cash_session_id"]).version == session_version
        assert db.query(KitchenTicket).count() == 1
        assert db.get(KitchenTicket, original_ticket["id"]).status == ("ready" if kitchen_complete else "queued")
        assert db.query(AuditEvent).filter_by(action="payment.created").count() == 1
        assert db.query(AuditEvent).filter_by(action="table.checkout_started").count() == 1
        assert db.query(AuditEvent).filter_by(action="table.paid_and_released").count() == 1
    if not kitchen_complete:
        completed = client.post(f"/api/v1/kitchen/commands/{original_ticket['id']}/complete", json={
            "expected_status": "queued", "expected_version": original_ticket["version"],
        }, headers={**auth_headers, "Idempotency-Key": "ready-after-release"})
        assert completed.status_code == 200, completed.text
        assert completed.json()["order"]["status"] == "closed"


def test_partial_panel_payment_then_mixed_checkout_collects_only_balance(client, tenant, auth_headers):
    enable(tenant)
    sent = create_sent_table(client, tenant, auth_headers)
    prior = record_prior_payment(client, sent["order"], auth_headers, 6)
    begun = start(client, prior["order"], auth_headers)
    assert begun.status_code == 200, begun.text
    receipt = next(job for job in jobs(client, sent["order"], auth_headers)["items"] if job["job_type"] == "customer_receipt")
    assert receipt["payload"]["paid_amount"] == 6
    assert receipt["payload"]["remaining_amount"] == 14
    response = pay(client, begun.json()["order"], auth_headers, [
        {"method": "cash", "amount": 4}, {"method": "card", "amount": 10},
    ], "mixed-balance")
    assert response.status_code == 200, response.text
    assert [item["amount"] for item in response.json()["payments"]] == [4, 10]
    assert response.json()["order"]["table_released_at"] is not None
    assert pay(client, begun.json()["order"], auth_headers, [
        {"method": "cash", "amount": 4}, {"method": "card", "amount": 10},
    ], "mixed-balance").json() == response.json()
    assert len(jobs(client, sent["order"], auth_headers)["items"]) == 2
    with SessionLocal() as db:
        payments = db.query(Payment).filter_by(order_id=sent["order"]["id"]).all()
        assert len(payments) == 3
        assert sum(item.amount for item in payments) == Decimal("20")
        assert db.get(Payment, prior["payment"]["id"]).amount == Decimal("6")
        assert db.query(KitchenTicket).count() == 1


@pytest.mark.parametrize("prior_amount", [0, 6])
def test_empty_payment_cannot_release_an_outstanding_table(client, tenant, auth_headers, prior_amount):
    sent = create_sent_table(client, tenant, auth_headers)
    current = sent["order"]
    if prior_amount:
        current = record_prior_payment(client, current, auth_headers, prior_amount)["order"]
    begun = start(client, current, auth_headers)
    assert begun.status_code == 200, begun.text
    response = pay(client, begun.json()["order"], auth_headers, [])
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "TABLE_PAYMENT_TOTAL_MISMATCH"
    with SessionLocal() as db:
        assert db.get(Order, current["id"]).table_released_at is None
        assert db.get(RestaurantTable, tenant["table_id"]).status == "occupied"
        assert db.query(Payment).count() == (1 if prior_amount else 0)


def test_failed_mixed_balance_rolls_back_all_new_payments(client, tenant, auth_headers):
    sent = create_sent_table(client, tenant, auth_headers)
    prior = record_prior_payment(client, sent["order"], auth_headers, 6)
    begun = start(client, prior["order"], auth_headers)
    assert begun.status_code == 200, begun.text
    response = pay(client, begun.json()["order"], auth_headers, [
        {"method": "card", "amount": 10}, {"method": "cash", "amount": 4, "cash_session_id": 999999},
    ])
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "CASH_SESSION_INVALID"
    with SessionLocal() as db:
        assert db.query(Payment).count() == 1
        order = db.get(Order, sent["order"]["id"])
        assert order.version == begun.json()["order"]["version"]
        assert order.payment_status == "partial" and order.table_released_at is None


def test_checkout_print_failure_preserves_prior_payment_and_retries_same_close(client, tenant, auth_headers):
    enable(tenant)
    sent = create_sent_table(client, tenant, auth_headers)
    prior = record_prior_payment(client, sent["order"], auth_headers, 20)

    def fail_checkout(mapper, connection, target):
        if target.payload.get("_trigger") == "table_checkout_started":
            raise SQLAlchemyError("isolated checkout queue failure")

    event.listen(PrintJob, "before_insert", fail_checkout)
    try:
        failed = start(client, prior["order"], auth_headers, "retry-paid-close")
        assert failed.status_code == 503, failed.text
    finally:
        event.remove(PrintJob, "before_insert", fail_checkout)
    with SessionLocal() as db:
        order = db.get(Order, sent["order"]["id"])
        assert order.checkout_started_at is None and order.table_released_at is None
        assert order.version == prior["order"]["version"]
        assert db.query(Payment).count() == 1
        assert db.get(Payment, prior["payment"]["id"]).amount == Decimal("20")
        assert db.query(PrintJob).count() == 1
    retried = start(client, prior["order"], auth_headers, "retry-paid-close")
    assert retried.status_code == 200, retried.text
    assert pay(client, retried.json()["order"], auth_headers, []).status_code == 200
    assert len(jobs(client, sent["order"], auth_headers)["items"]) == 2


def test_release_old_paid_account_keeps_another_occupant(client, tenant, auth_headers):
    sent = create_sent_table(client, tenant, auth_headers)
    prior = record_prior_payment(client, sent["order"], auth_headers, 20)
    begun = start(client, prior["order"], auth_headers)
    assert begun.status_code == 200, begun.text
    with SessionLocal.begin() as db:
        other = Order(business_id=tenant["business_id"], branch_id=tenant["branch_id"],
            number="legacy-current-occupant", channel="dine_in", table_id=tenant["table_id"], status="confirmed")
        db.add(other)
        db.flush()
        other_id = other.id
        table_version = db.get(RestaurantTable, tenant["table_id"]).version
    response = pay(client, begun.json()["order"], auth_headers, [])
    assert response.status_code == 200, response.text
    assert response.json()["table"]["status"] == "occupied"
    with SessionLocal() as db:
        assert db.get(Order, sent["order"]["id"]).table_released_at is not None
        assert db.get(Order, other_id).table_released_at is None
        assert db.get(RestaurantTable, tenant["table_id"]).version == table_version
        assert db.query(Payment).count() == 1


def test_release_paid_account_ignores_delivered_history_occupant(client, tenant, auth_headers):
    sent = create_sent_table(client, tenant, auth_headers)
    prior = record_prior_payment(client, sent["order"], auth_headers, 20)
    begun = start(client, prior["order"], auth_headers)
    assert begun.status_code == 200, begun.text
    with SessionLocal.begin() as db:
        history = Order(business_id=tenant["business_id"], branch_id=tenant["branch_id"],
            number="legacy-delivered-history", channel="dine_in", table_id=tenant["table_id"], status="delivered")
        db.add(history)
        db.flush()
        history_id = history.id
        table_version = db.get(RestaurantTable, tenant["table_id"]).version
    response = pay(client, begun.json()["order"], auth_headers, [])
    assert response.status_code == 200, response.text
    assert response.json()["table"]["status"] == "available"
    assert response.json()["order"]["table_released_at"] is not None
    with SessionLocal() as db:
        assert db.get(RestaurantTable, tenant["table_id"]).version == table_version + 1
        assert db.get(Order, history_id).status == "delivered"
        assert db.get(Order, history_id).table_released_at is None
        assert db.query(Payment).count() == 1


@pytest.mark.parametrize("stage", ["start", "pay"])
def test_paid_recovery_still_blocks_open_payment_evidence(client, tenant, auth_headers, stage):
    sent = create_sent_table(client, tenant, auth_headers)
    prior = record_prior_payment(client, sent["order"], auth_headers, 20)
    current = prior["order"]
    if stage == "pay":
        response = start(client, current, auth_headers)
        assert response.status_code == 200, response.text
        current = response.json()["order"]
    with SessionLocal.begin() as db:
        db.add(PaymentEvidence(business_id=tenant["business_id"], order_id=sent["order"]["id"],
            provider="yape", storage_path="private/isolated.webp", image_sha256="f" * 64, status="under_review"))
    response = start(client, current, auth_headers) if stage == "start" else pay(client, current, auth_headers, [])
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "PAYMENT_EVIDENCE_UNDER_REVIEW"
    with SessionLocal() as db:
        assert db.get(Order, sent["order"]["id"]).table_released_at is None
        assert db.query(Payment).count() == 1
