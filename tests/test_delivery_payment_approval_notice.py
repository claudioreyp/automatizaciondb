"""Shipping receipt approvals emit a notice without another payment or kitchen release."""
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import Branch, IntegrationEvent, KitchenTicket, OrderPaymentRequest, Payment, PaymentEvidence, PrintJob
from test_agent_checkout import upload, review
from test_delivery_yape_recovery import delivery, transition


@pytest.mark.parametrize("dispatched", [False, True])
def test_shipping_approval_event_replays_stable_without_second_release(client, tenant, auth_headers, dispatched):
    headers, order, request_id = delivery(client, tenant, auth_headers)
    if dispatched:
        assert transition(client, auth_headers, order, "dispatched").status_code == 200
    receipt = upload(client, headers, order, request_id)
    with SessionLocal() as db:
        print_count = db.query(PrintJob).count()
        original_event = db.scalar(select(IntegrationEvent).where(IntegrationEvent.event_type == "payment.approved"))
        original_event_id = original_event.id
    path = f"/api/v1/payment-evidence/{receipt['evidence']['id']}/review"
    body = {"approve": True, "register_id": tenant["register_id"], "expected_version": receipt["order"]["version"]}
    request_headers = {**auth_headers, "Idempotency-Key": "shipping-approval-response-lost"}
    first = client.post(path, json=body, headers=request_headers)
    assert first.status_code == 200, first.text
    repeated = client.post(path, json=body, headers=request_headers)
    assert repeated.status_code == 200 and repeated.json() == first.json(), repeated.text
    event_id = first.json()["notification"]["event_id"]
    assert first.json()["notification"]["queued"] is True
    assert event_id and event_id != original_event_id
    assert first.json()["command_committed"] is False and first.json()["tickets"] == []
    assert client.post(path, json={**body, "approve": False}, headers=request_headers).status_code == 409
    # A different operation key cannot approve the already-paid evidence again.
    again = review(client, auth_headers, tenant, receipt, key="different-shipping-approval")
    assert again.status_code == 200, again.text
    with SessionLocal() as db:
        assert db.query(Payment).count() == 2
        assert db.query(KitchenTicket).count() == 1
        assert db.query(PrintJob).count() == print_count
        assert db.query(IntegrationEvent).filter_by(event_type="payment.approved").count() == 2
        request = db.get(OrderPaymentRequest, request_id)
        event = db.get(IntegrationEvent, event_id)
        assert event.business_id == tenant["business_id"] and event.branch_id == tenant["branch_id"]
        assert event.aggregate_id == str(order["id"])
        assert event.payload["evidence_id"] == receipt["evidence"]["id"]
        assert event.payload["payment_id"] == request.payment_id
        assert event.payload["payment_request_id"] == request_id
        assert event.payload["purpose"] == "delivery"
        assert event.payload["payment_method"] == "yape" and event.payload["payment_approved"] is True
        assert event.payload["sent_to_kitchen"] is False and event.payload["ticket_ids"] == []


@pytest.mark.parametrize("action", ["pending", "reject", "wrong-business", "wrong-branch", "waiter", "stale"])
def test_unapproved_or_unscoped_shipping_receipt_never_emits_approval(client, tenant, auth_headers, action):
    headers, order, request_id = delivery(client, tenant, auth_headers)
    receipt = upload(client, headers, order, request_id)
    if action != "pending":
        body = {"approve": action != "reject", "register_id": tenant["register_id"], "expected_version": receipt["order"]["version"]}
        h = {**auth_headers, "Idempotency-Key": "not-an-approval"}
        if action == "wrong-business":
            h.update({"X-Business-Id": str(tenant["other_business_id"]), "X-Branch-Id": str(tenant["other_branch_id"])})
        elif action == "wrong-branch":
            with SessionLocal.begin() as db:
                branch = Branch(business_id=tenant["business_id"], slug="other-notice", name="Other branch")
                db.add(branch)
                db.flush()
                h["X-Branch-Id"] = str(branch.id)
        elif action == "waiter":
            h["X-Dev-Role"] = "waiter"
        elif action == "stale":
            body["expected_version"] -= 1
        result = client.post(f"/api/v1/payment-evidence/{receipt['evidence']['id']}/review", json=body, headers=h)
        assert result.status_code == (200 if action == "reject" else 403 if action == "waiter" else 409 if action == "stale" else 404), result.text
    with SessionLocal() as db:
        assert db.query(Payment).count() == 1
        assert db.query(IntegrationEvent).filter_by(event_type="payment.approved").count() == 1
        assert db.get(OrderPaymentRequest, request_id).status == ("rejected" if action == "reject" else "under_review")


def test_shipping_notice_failure_rolls_back_payment_and_receipt(client, tenant, auth_headers, monkeypatch):
    from app import agent_checkout
    headers, order, request_id = delivery(client, tenant, auth_headers)
    receipt = upload(client, headers, order, request_id)
    original_create = agent_checkout.create_integration_event

    def fail_shipping_event(db, order, kind, payload):
        event = original_create(db, order, kind, payload)
        if kind == "payment.approved" and payload.get("purpose") == "delivery":
            raise RuntimeError("synthetic shipping event failure")
        return event

    monkeypatch.setattr(agent_checkout, "create_integration_event", fail_shipping_event)
    with pytest.raises(RuntimeError, match="synthetic shipping event failure"):
        review(client, auth_headers, tenant, receipt, key="shipping-transaction-rollback")
    with SessionLocal() as db:
        assert db.query(Payment).count() == 1
        assert db.query(IntegrationEvent).filter_by(event_type="payment.approved").count() == 1
        assert db.get(PaymentEvidence, receipt["evidence"]["id"]).status == "under_review"
        assert db.get(OrderPaymentRequest, request_id).status == "under_review"
    monkeypatch.setattr(agent_checkout, "create_integration_event", original_create)
    recovered = review(client, auth_headers, tenant, receipt, key="shipping-transaction-rollback")
    assert recovered.status_code == 200 and recovered.json()["notification"]["queued"] is True, recovered.text


def test_concurrent_shipping_approval_replays_one_event_and_one_payment(client, tenant, auth_headers):
    from concurrent.futures import ThreadPoolExecutor
    from app.database import engine
    if engine.dialect.name != "postgresql":
        pytest.skip("Concurrent row-lock assertion requires isolated PostgreSQL")
    headers, order, request_id = delivery(client, tenant, auth_headers)
    receipt = upload(client, headers, order, request_id)
    body = {"approve": True, "register_id": tenant["register_id"], "expected_version": receipt["order"]["version"]}
    h = {**auth_headers, "Idempotency-Key": "parallel-shipping-approval"}
    with SessionLocal() as db:
        print_count = db.query(PrintJob).count()

    def submit(_):
        return client.post(f"/api/v1/payment-evidence/{receipt['evidence']['id']}/review", headers=h, json=body)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, range(2)))
    assert [r.status_code for r in results] == [200, 200], [r.text for r in results]
    assert results[0].json() == results[1].json()
    event_id = results[0].json()["notification"]["event_id"]
    assert event_id
    with SessionLocal() as db:
        events = list(db.scalars(select(IntegrationEvent).where(IntegrationEvent.event_type == "payment.approved")))
        shipping_events = [e for e in events if e.payload.get("purpose") == "delivery"]
        assert len(events) == 2 and len(shipping_events) == 1 and shipping_events[0].id == event_id
        assert db.query(Payment).count() == 2
        assert db.query(KitchenTicket).count() == 1
        assert db.query(PrintJob).count() == print_count
