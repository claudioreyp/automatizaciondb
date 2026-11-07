"""Synthetic staff reconciliation: physical delivery must never imply collected money."""
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import (AgentAttentionRequest, AuditEvent, DeliveryAssignment, KitchenTicket,
    Order, OrderPaymentRequest, Payment, PaymentAllocation, PaymentEvidence, PrintJob, Product)
from test_agent_checkout import PHONE, setup, upload, review, open_cash


def detail(client, headers, order):
    response = client.get(f"/api/v1/orders/{order['id']}/detail", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def delivery(client, tenant, auth_headers, *, original_paid=True, price=15):
    with SessionLocal.begin() as db:
        db.get(Product, tenant["product_id"]).price = Decimal(str(price))
    headers, order = setup(client, tenant, auth_headers, quote=True)
    open_cash(client, auth_headers, tenant)
    if original_paid:
        first = review(client, auth_headers, tenant, upload(client, headers, order))
        assert first.status_code == 200, first.text
    current = detail(client, auth_headers, order)["order"]
    fee = client.patch(f"/api/v1/orders/{order['id']}/delivery-fee",
        headers={**auth_headers, "Idempotency-Key": "shipping-quote"},
        json={"amount": 6, "method": "yape", "expected_version": current["version"]})
    assert fee.status_code == 200, fee.text
    if original_paid:
        with SessionLocal.begin() as db:
            db.get(Order, order["id"]).status = "ready"
            for ticket in db.scalars(select(KitchenTicket).where(KitchenTicket.order_id == order["id"])):
                ticket.status = "ready"
    return headers, fee.json()["order"], fee.json()["payment_request"]["id"]


def transition(client, headers, order, status, **extra):
    current = detail(client, headers, order)["order"]
    return client.post(f"/api/v1/orders/{order['id']}/transition", headers=headers,
        json={"status": status, "expected_version": current["version"], **extra})


@pytest.mark.parametrize("use_assignment", [False, True])
def test_shipping_yape_does_not_block_dispatch_and_can_be_reviewed_during_delivery(client, tenant, auth_headers, use_assignment):
    headers, order, request_id = delivery(client, tenant, auth_headers)
    if use_assignment:
        with SessionLocal.begin() as db:
            db.add(DeliveryAssignment(business_id=order["business_id"], branch_id=order["branch_id"],
                order_id=order["id"], status="ready", address={}, fee=6, tracking_code="TEST-ONLY"))
    prefix = "/delivery" if use_assignment else ""
    path = f"/api/v1{prefix}/orders/{order['id']}/transition"
    first = client.post(path, headers=auth_headers, json={"status": "dispatched", "expected_version": order["version"]})
    assert first.status_code == 200, first.text
    state = client.get(f"/api/v1/integrations/orders/{order['id']}/customer-state",
        headers=headers, params={"sender": PHONE}).json()
    assert state["allowed_actions"]["submit_delivery_receipt"] is True
    assert state["allowed_actions"]["add_items"] is False
    assert state["allowed_actions"]["change_fulfillment"] is False
    receipt = upload(client, headers, order, request_id)
    state = client.get(f"/api/v1/integrations/orders/{order['id']}/customer-state",
        headers=headers, params={"sender": PHONE}).json()
    assert state["allowed_actions"]["submit_delivery_receipt"] is False
    blocked = client.post(path, headers=auth_headers, json={"status": "delivered",
        "expected_version": receipt["order"]["version"], "delivery_collection": "courier_pending", "reason": "Cobró el repartidor"})
    assert blocked.status_code == 409 and "PAYMENT_EVIDENCE_UNDER_REVIEW" in blocked.text
    approved = review(client, auth_headers, tenant, receipt, key="shipping-approved")
    assert approved.status_code == 200, approved.text
    delivered = client.post(path, headers=auth_headers, json={"status": "delivered", "expected_version": approved.json()["order"]["version"]})
    assert delivered.status_code == 200, delivered.text
    with SessionLocal() as db:
        assert db.query(Payment).count() == 2
        assert db.get(OrderPaymentRequest, request_id).status == "paid"
        assert db.query(KitchenTicket).count() == 1


def test_open_delivery_receipt_allows_dispatch_but_not_delivery(client, tenant, auth_headers):
    headers, order, request_id = delivery(client, tenant, auth_headers)
    upload(client, headers, order, request_id)
    assert transition(client, auth_headers, order, "dispatched").status_code == 200
    rejected = transition(client, auth_headers, order, "delivered", delivery_collection="courier_pending", reason="Cobró el repartidor")
    assert rejected.status_code == 409 and "PAYMENT_EVIDENCE_UNDER_REVIEW" in rejected.text
    with SessionLocal() as db:
        assert db.get(OrderPaymentRequest, request_id).snapshot.get("courier_collection_pending") is None
        assert db.query(Payment).count() == 1


@pytest.mark.parametrize("role", ["owner", "cashier", "dispatcher"])
def test_delivered_courier_confirmation_is_audited_but_never_money(client, tenant, auth_headers, role):
    _, order, request_id = delivery(client, tenant, auth_headers)
    assert transition(client, auth_headers, order, "dispatched").status_code == 200
    current = detail(client, auth_headers, order)["order"]
    request_headers = {**auth_headers, "X-Dev-Role": role}
    response = client.post(f"/api/v1/orders/{order['id']}/transition", headers=request_headers,
        json={"status": "delivered", "expected_version": current["version"], "delivery_collection": "courier_pending", "reason": "Cliente pagó al repartidor"})
    assert response.status_code == 200, response.text
    state = detail(client, auth_headers, order)
    assert state["order"]["status"] == "delivered"
    assert state["payment_summary"]["remaining"] == 6
    assert state["payment_requests"][0]["collection_pending_with_courier"] is True
    with SessionLocal() as db:
        assert db.query(Payment).count() == 1
        assert db.get(OrderPaymentRequest, request_id).status == "pending"
        assert db.query(AuditEvent).filter_by(action="delivery.courier_collection_pending").count() == 1


@pytest.mark.parametrize("change", ["no-confirmation", "no-note", "no-version", "waiter", "stale"])
def test_courier_delivery_requires_explicit_current_authorized_confirmation(client, tenant, auth_headers, change):
    _, order, request_id = delivery(client, tenant, auth_headers)
    assert transition(client, auth_headers, order, "dispatched").status_code == 200
    current = detail(client, auth_headers, order)["order"]
    body = {"status": "delivered", "expected_version": current["version"], "delivery_collection": "courier_pending", "reason": "Cobró el repartidor"}
    h = auth_headers
    if change == "no-confirmation": body.pop("delivery_collection")
    if change == "no-note": body["reason"] = " "
    if change == "no-version": body.pop("expected_version")
    if change == "waiter": h = {**h, "X-Dev-Role": "waiter"}
    if change == "stale": body["expected_version"] -= 1
    response = client.post(f"/api/v1/orders/{order['id']}/transition", headers=h, json=body)
    assert response.status_code in {403, 409, 422}, response.text
    with SessionLocal() as db:
        assert db.get(Order, order["id"]).status == "dispatched"
        assert db.get(OrderPaymentRequest, request_id).snapshot.get("courier_collection_pending") is None
        assert db.query(Payment).count() == 1


@pytest.mark.parametrize("method", ["cash", "yape"])
def test_collection_after_delivered_settles_request_once_and_replays_stale_version(client, tenant, auth_headers, method):
    _, order, request_id = delivery(client, tenant, auth_headers)
    assert transition(client, auth_headers, order, "dispatched").status_code == 200
    assert transition(client, auth_headers, order, "delivered", delivery_collection="courier_pending", reason="Cobró el repartidor").status_code == 200
    current = detail(client, auth_headers, order)["order"]
    path = f"/api/v1/orders/{order['id']}/payments"
    body = {"method": method, "amount": 6, "register_id": tenant["register_id"],
        "payment_request_id": request_id, "expected_version": current["version"], "note": "Ingreso del cobro del repartidor"}
    h = {**auth_headers, "Idempotency-Key": "shipping-cash-lost-response"}
    first = client.post(path, headers=h, json=body)
    assert first.status_code in {200, 201}, first.text
    again = client.post(path, headers=h, json=body)
    assert again.status_code in {200, 201} and again.json() == first.json()
    assert client.post(path, headers=h, json={**body, "amount": 5}).status_code == 409
    state = detail(client, auth_headers, order)
    assert state["payment_summary"]["remaining"] == 0
    assert state["order"]["status"] == "delivered"
    with SessionLocal() as db:
        assert db.query(Payment).count() == 2
        request = db.get(OrderPaymentRequest, request_id)
        assert request.status == "paid" and request.payment_id == first.json()["payment"]["id"]
        assert request.snapshot.get("courier_collection_pending") is None
        assert db.query(KitchenTicket).count() == 1


@pytest.mark.parametrize("target,amount,paid,remaining,requests_paid", [
    ("original", "4.50", "4.50", "6", False),
    ("original_and_delivery", "10.50", "10.50", "0", True),
    ("original_and_delivery", "12", "12", "0", True),
])
def test_reclassify_sole_shipping_receipt_one_transfer_one_payment(client, tenant, auth_headers, target, amount, paid, remaining, requests_paid):
    headers, order, request_id = delivery(client, tenant, auth_headers, original_paid=False, price="4.50")
    receipt = upload(client, headers, order, request_id)
    path = f"/api/v1/payment-evidence/{receipt['evidence']['id']}/review"
    body = {"approve": True, "allocation": target, "confirmed_amount": amount,
        "note": "Verificado en cuenta, corregir asignación", "register_id": tenant["register_id"],
        "expected_version": receipt["order"]["version"]}
    h = {**auth_headers, "Idempotency-Key": "reclassify-lost-response"}
    first = client.post(path, json=body, headers=h)
    assert first.status_code == 200, first.text
    assert first.json()["evidence"]["status"] == "paid"
    assert first.json()["command_committed"] is True
    again = client.post(path, json=body, headers=h)
    assert again.status_code == 200 and again.json() == first.json(), again.text
    assert client.post(path, json={**body, "allocation": "original" if target != "original" else "original_and_delivery"}, headers=h).status_code == 409
    state = detail(client, auth_headers, order)
    assert Decimal(str(state["payment_summary"]["remaining"])) == Decimal(remaining)
    with SessionLocal() as db:
        assert db.query(Payment).count() == 1
        payment = db.scalar(select(Payment))
        assert payment.amount == Decimal(paid)
        assert db.query(PaymentEvidence).count() == 1
        evidence = db.get(PaymentEvidence, receipt["evidence"]["id"])
        assert evidence.storage_path and evidence.image_sha256 and evidence.payment_request_id is None
        assert evidence.analysis["allocation_review"]["previous_classification"]["payment_request_id"] == request_id
        assert evidence.analysis["allocation_review"]["previous_classification"]["purpose"] == "delivery"
        request = db.get(OrderPaymentRequest, request_id)
        assert (request.status == "paid") is requests_paid
        assert (request.payment_id == payment.id) is requests_paid
        assert db.query(KitchenTicket).count() == 1
        rows = list(db.scalars(select(PaymentAllocation).where(PaymentAllocation.payment_id == payment.id)))
        assert sum((row.amount for row in rows), Decimal(0)) == Decimal(paid)
        assert db.query(AgentAttentionRequest).filter_by(kind="financial_review").count() == (1 if amount == "12" else 0)


@pytest.mark.parametrize("target,amount", [("original", "4"), ("original", "5"), ("original_and_delivery", "10")])
def test_insufficient_or_misallocated_receipt_stays_in_review_without_money(client, tenant, auth_headers, target, amount):
    headers, order, request_id = delivery(client, tenant, auth_headers, original_paid=False, price="4.50")
    receipt = upload(client, headers, order, request_id)
    response = client.post(f"/api/v1/payment-evidence/{receipt['evidence']['id']}/review",
        headers={**auth_headers, "Idempotency-Key": "too-small"}, json={"approve": True, "allocation": target,
        "confirmed_amount": amount, "note": "Verificado", "register_id": tenant["register_id"], "expected_version": receipt["order"]["version"]})
    assert response.status_code == 422 and "PAYMENT_AMOUNT_INSUFFICIENT" in response.text
    with SessionLocal() as db:
        assert db.query(Payment).count() == db.query(KitchenTicket).count() == 0
        saved = db.get(PaymentEvidence, receipt["evidence"]["id"])
        assert saved.status == "under_review" and saved.payment_request_id == request_id


@pytest.mark.parametrize("ambiguity", ["approved-payment", "other-original-receipt", "addition", "fee-mismatch"])
def test_reclassification_rejects_ambiguous_financial_history(client, tenant, auth_headers, ambiguity):
    headers, order, request_id = delivery(client, tenant, auth_headers, original_paid=False)
    receipt = upload(client, headers, order, request_id)
    if ambiguity == "other-original-receipt":
        upload(client, headers, order)
    with SessionLocal.begin() as db:
        if ambiguity == "approved-payment":
            db.add(Payment(business_id=order["business_id"], order_id=order["id"], amount=1, method="cash"))
        elif ambiguity == "addition":
            db.add(OrderPaymentRequest(business_id=order["business_id"], branch_id=order["branch_id"],
                order_id=order["id"], purpose="addition", method="yape", amount=2, status="pending", snapshot={}))
        elif ambiguity == "fee-mismatch":
            db.get(OrderPaymentRequest, request_id).amount = 5
    current = detail(client, auth_headers, order)["order"]
    response = client.post(f"/api/v1/payment-evidence/{receipt['evidence']['id']}/review",
        headers={**auth_headers, "Idempotency-Key": "ambiguous"}, json={"approve": True, "allocation": "original_and_delivery",
        "confirmed_amount": 21, "note": "Verificado", "register_id": tenant["register_id"], "expected_version": current["version"]})
    assert response.status_code == 409, response.text
    with SessionLocal() as db:
        assert db.query(Payment).count() == (1 if ambiguity == "approved-payment" else 0)
        assert db.get(PaymentEvidence, receipt["evidence"]["id"]).payment_request_id == request_id


def test_reclassification_rolls_back_all_changes_if_kitchen_release_fails(client, tenant, auth_headers, monkeypatch):
    from fastapi import HTTPException
    import app.services as services
    headers, order, request_id = delivery(client, tenant, auth_headers, original_paid=False)
    receipt = upload(client, headers, order, request_id)
    before = detail(client, auth_headers, order)
    def unavailable(*args, **kwargs):
        raise HTTPException(409, "Synthetic kitchen failure")
    monkeypatch.setattr(services, "send_order_to_kitchen", unavailable)
    response = client.post(f"/api/v1/payment-evidence/{receipt['evidence']['id']}/review",
        headers={**auth_headers, "Idempotency-Key": "rollback-on-kitchen"}, json={"approve": True, "allocation": "original_and_delivery",
        "confirmed_amount": 21, "note": "Verificado", "register_id": tenant["register_id"], "expected_version": receipt["order"]["version"]})
    assert response.status_code == 409
    after = detail(client, auth_headers, order)
    assert before["order"]["version"] == after["order"]["version"]
    with SessionLocal() as db:
        assert db.query(Payment).count() == db.query(KitchenTicket).count() == db.query(PaymentAllocation).count() == 0
        evidence = db.get(PaymentEvidence, receipt["evidence"]["id"])
        assert evidence.status == "under_review" and evidence.payment_request_id == request_id


def test_delivery_collection_cannot_hide_other_debt_or_cross_tenant_scope(client, tenant, auth_headers):
    _, order, request_id = delivery(client, tenant, auth_headers, original_paid=False)
    body = {"method": "cash", "amount": 6, "payment_request_id": request_id,
        "register_id": tenant["register_id"], "expected_version": order["version"]}
    path = f"/api/v1/orders/{order['id']}/payments"
    response = client.post(path, headers={**auth_headers, "Idempotency-Key": "other-debt"}, json=body)
    assert response.status_code == 409 and "INITIAL_PAYMENT_PENDING" in response.text
    other = {**auth_headers, "X-Business-Id": str(tenant["other_business_id"]), "X-Branch-Id": str(tenant["other_branch_id"])}
    assert client.post(path, headers={**other, "Idempotency-Key": "alien"}, json=body).status_code in {403, 404}
    with SessionLocal() as db:
        assert db.query(Payment).count() == 0


def test_open_original_receipt_still_blocks_dispatch(client, tenant, auth_headers):
    headers, order, _ = delivery(client, tenant, auth_headers, original_paid=False)
    upload(client, headers, order)
    with SessionLocal.begin() as db:
        db.get(Order, order["id"]).status = "ready"
    response = transition(client, auth_headers, order, "dispatched")
    assert response.status_code == 409 and "PAYMENT_EVIDENCE_UNDER_REVIEW" in response.text
    with SessionLocal() as db:
        assert db.query(Payment).count() == db.query(KitchenTicket).count() == 0


@pytest.mark.parametrize("missing", ["note", "confirmed_amount", "expected_version", "approve"])
def test_reclassification_requires_explicit_human_review(client, tenant, auth_headers, missing):
    headers, order, request_id = delivery(client, tenant, auth_headers, original_paid=False)
    receipt = upload(client, headers, order, request_id)
    body = {"approve": True, "allocation": "original_and_delivery", "confirmed_amount": 21,
        "note": "Verificado", "register_id": tenant["register_id"], "expected_version": receipt["order"]["version"]}
    if missing == "approve": body["approve"] = False
    else: body.pop(missing)
    response = client.post(f"/api/v1/payment-evidence/{receipt['evidence']['id']}/review",
        headers={**auth_headers, "Idempotency-Key": "human-required"}, json=body)
    assert response.status_code in {409, 422}, response.text
    with SessionLocal() as db:
        assert db.query(Payment).count() == db.query(KitchenTicket).count() == 0
        assert db.get(PaymentEvidence, receipt["evidence"]["id"]).status == "under_review"


def test_excess_missing_contact_has_typed_refusal_not_storage_error(client, tenant, auth_headers):
    headers, order, request_id = delivery(client, tenant, auth_headers, original_paid=False)
    receipt = upload(client, headers, order, request_id)
    with SessionLocal.begin() as db:
        saved = db.get(Order, order["id"])
        saved.whatsapp_chat_id = saved.customer_phone = None
    response = client.post(f"/api/v1/payment-evidence/{receipt['evidence']['id']}/review",
        headers={**auth_headers, "Idempotency-Key": "missing-contact"}, json={"approve": True, "allocation": "original_and_delivery",
        "confirmed_amount": 22, "note": "Verificado", "register_id": tenant["register_id"], "expected_version": receipt["order"]["version"]})
    assert response.status_code == 409 and "PAYMENT_ALLOCATION_CONTACT_REQUIRED" in response.text
    with SessionLocal() as db:
        assert db.query(Payment).count() == 0


def test_existing_kitchen_and_original_receipt_metadata_are_preserved(client, tenant, auth_headers):
    from app.auth import AuthContext
    from app.services import confirm_order, send_order_to_kitchen
    headers, order, request_id = delivery(client, tenant, auth_headers, original_paid=False)
    receipt = upload(client, headers, order, request_id)
    with SessionLocal.begin() as db:
        saved = db.get(Order, order["id"])
        user = AuthContext("test", "owner", saved.business_id, saved.branch_id)
        confirm_order(db, user, saved)
        send_order_to_kitchen(db, user, saved)
        evidence = db.get(PaymentEvidence, receipt["evidence"]["id"])
        evidence.operation_number = "SYNTHETIC-KEEP-OPERATION"
        evidence.security_code = "007"
        evidence.warnings = ["Advertencia conservada"]
        evidence.analysis = {**(evidence.analysis or {}), "custom_warning": "Una revisión humana es necesaria"}
        file_path, image_hash = evidence.storage_path, evidence.image_sha256
    current = detail(client, auth_headers, order)["order"]
    response = client.post(f"/api/v1/payment-evidence/{receipt['evidence']['id']}/review",
        headers={**auth_headers, "Idempotency-Key": "preserve-history"}, json={"approve": True, "allocation": "original_and_delivery",
        "confirmed_amount": 21, "note": "Verificado", "register_id": tenant["register_id"], "expected_version": current["version"]})
    assert response.status_code == 200 and response.json()["command_committed"] is False, response.text
    with SessionLocal() as db:
        assert db.query(KitchenTicket).count() == 1
        evidence = db.get(PaymentEvidence, receipt["evidence"]["id"])
        assert evidence.storage_path == file_path and evidence.image_sha256 == image_hash
        assert evidence.operation_number == "SYNTHETIC-KEEP-OPERATION" and evidence.security_code == "007"
        assert evidence.warnings == ["Advertencia conservada"]
        assert evidence.analysis["custom_warning"] == "Una revisión humana es necesaria"


def test_dispatched_delivery_does_not_accept_addition_receipt_or_commercial_change(client, tenant, auth_headers):
    headers, order, _ = delivery(client, tenant, auth_headers)
    with SessionLocal.begin() as db:
        request = OrderPaymentRequest(business_id=order["business_id"], branch_id=order["branch_id"],
            order_id=order["id"], purpose="addition", method="yape", amount=2, status="rejected", snapshot={})
        db.add(request)
        db.flush()
        addition_id = request.id
    assert transition(client, auth_headers, order, "dispatched").status_code == 200
    response = client.post(f"/api/v1/integrations/orders/{order['id']}/payment-evidence",
        headers={**headers, "Idempotency-Key": "post-dispatch-addition"}, data={"provider": "yape", "sender": PHONE,
        "payment_request_id": addition_id}, files={"file": ("test.png", b"synthetic", "image/png")})
    assert response.status_code == 409, response.text
    assert client.patch(f"/api/v1/orders/{order['id']}/delivery-fee", headers={**auth_headers, "Idempotency-Key": "late-fee"},
        json={"amount": 7, "method": "yape", "expected_version": detail(client, auth_headers, order)["order"]["version"]}).status_code == 409


def test_concurrent_reclassification_replays_same_receipt_with_one_payment(client, tenant, auth_headers):
    from concurrent.futures import ThreadPoolExecutor
    from app.database import engine
    if engine.dialect.name != "postgresql":
        pytest.skip("Concurrent row-lock assertion requires isolated PostgreSQL")
    headers, order, request_id = delivery(client, tenant, auth_headers, original_paid=False)
    receipt = upload(client, headers, order, request_id)
    body = {"approve": True, "allocation": "original_and_delivery", "confirmed_amount": 21,
        "note": "Verificado", "register_id": tenant["register_id"], "expected_version": receipt["order"]["version"]}
    h = {**auth_headers, "Idempotency-Key": "parallel-one-transfer"}
    def submit():
        return client.post(f"/api/v1/payment-evidence/{receipt['evidence']['id']}/review", headers=h, json=body)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: submit(), range(2)))
    assert [r.status_code for r in results] == [200, 200], [r.text for r in results]
    assert results[0].json() == results[1].json()
    with SessionLocal() as db:
        assert db.query(Payment).count() == 1
        assert db.query(KitchenTicket).count() == 1


def test_concurrent_courier_collection_records_money_and_settles_only_once(client, tenant, auth_headers):
    from concurrent.futures import ThreadPoolExecutor
    from app.database import engine
    if engine.dialect.name != "postgresql":
        pytest.skip("Concurrent row-lock assertion requires isolated PostgreSQL")
    _, order, request_id = delivery(client, tenant, auth_headers)
    assert transition(client, auth_headers, order, "dispatched").status_code == 200
    assert transition(client, auth_headers, order, "delivered", delivery_collection="courier_pending", reason="Cobró el repartidor").status_code == 200
    current = detail(client, auth_headers, order)["order"]
    body = {"method": "cash", "amount": 6, "register_id": tenant["register_id"],
        "payment_request_id": request_id, "expected_version": current["version"]}
    h = {**auth_headers, "Idempotency-Key": "parallel-shipping"}
    def submit():
        return client.post(f"/api/v1/orders/{order['id']}/payments", headers=h, json=body)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: submit(), range(2)))
    assert [r.status_code for r in results] == [201, 201], [r.text for r in results]
    assert results[0].json() == results[1].json()
    with SessionLocal() as db:
        assert db.query(Payment).count() == 2
        assert db.get(OrderPaymentRequest, request_id).status == "paid"


def test_financial_actions_reject_other_branch_and_do_not_write(client, tenant, auth_headers):
    from app.models import Branch
    headers, order, request_id = delivery(client, tenant, auth_headers, original_paid=False)
    receipt = upload(client, headers, order, request_id)
    with SessionLocal.begin() as db:
        branch = Branch(business_id=tenant["business_id"], slug="isolated-other", name="Other branch")
        db.add(branch)
        db.flush()
        branch_id = branch.id
    h = {**auth_headers, "X-Branch-Id": str(branch_id), "Idempotency-Key": "wrong-branch"}
    response = client.post(f"/api/v1/payment-evidence/{receipt['evidence']['id']}/review", headers=h,
        json={"approve": True, "allocation": "original_and_delivery", "confirmed_amount": 21,
            "note": "Verificado", "register_id": tenant["register_id"], "expected_version": receipt["order"]["version"]})
    assert response.status_code in {403, 404}, response.text
    with SessionLocal() as db:
        assert db.query(Payment).count() == 0


def test_settlement_failure_after_payment_creation_rolls_back_request_and_money(client, tenant, auth_headers, monkeypatch):
    from fastapi import HTTPException
    import app.agent_delivery_payments as helper
    _, order, request_id = delivery(client, tenant, auth_headers)
    assert transition(client, auth_headers, order, "dispatched").status_code == 200
    assert transition(client, auth_headers, order, "delivered", delivery_collection="courier_pending", reason="Cobró el repartidor").status_code == 200
    current = detail(client, auth_headers, order)["order"]
    original_audit = helper.audit
    def fail_settlement_audit(db, user, action, *args, **kwargs):
        if action == "delivery.payment_settled":
            raise HTTPException(409, "Synthetic ledger failure")
        return original_audit(db, user, action, *args, **kwargs)
    monkeypatch.setattr(helper, "audit", fail_settlement_audit)
    response = client.post(f"/api/v1/orders/{order['id']}/payments",
        headers={**auth_headers, "Idempotency-Key": "settlement-rollback"}, json={"method": "cash", "amount": 6,
            "register_id": tenant["register_id"], "payment_request_id": request_id, "expected_version": current["version"]})
    assert response.status_code == 409
    with SessionLocal() as db:
        assert db.query(Payment).count() == 1
        request = db.get(OrderPaymentRequest, request_id)
        assert request.status == "pending" and request.payment_id is None
        assert request.snapshot.get("courier_collection_pending")

