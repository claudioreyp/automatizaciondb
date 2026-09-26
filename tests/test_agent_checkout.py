from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import (BranchSettings, IntegrationEvent, KitchenTicket, Order, Payment, PaymentEvidence, PrintJob, Product,
                        Modifier, ModifierGroup, ProductModifierGroup)
from test_escalar_integrations import create_credential, integration_headers


PHONE = "51999999999"


def setup(client, tenant, auth_headers, quote=False, method="yape"):
    with SessionLocal.begin() as db:
        db.add(BranchSettings(business_id=tenant["business_id"], branch_id=tenant["branch_id"],
            delivery_mode="quote" if quote else "fixed", fixed_delivery_fee=0,
            payment_methods={k: ["cash", "yape"] for k in ("delivery", "takeaway", "counter")}))
    token = create_credential(client, tenant, auth_headers)["token"]
    headers = integration_headers(token)
    body = {"branch_id": tenant["branch_id"], "source": "whatsapp_agent",
        "channel": "delivery" if quote else "counter", "payment_method": method,
        "whatsapp_chat_id": PHONE + "@c.us", "customer_phone": PHONE,
        "allow_pending_delivery_quote": quote,
        "delivery_address": {"address": "Calle Prueba 123", "reference": "Puerta azul"} if quote else None,
        "items": [{"product_id": tenant["product_id"], "quantity": 1}]}
    result = client.post("/api/v1/integrations/orders/draft", json=body,
        headers={**headers, "Idempotency-Key": "initial"})
    assert result.status_code == 201, result.text
    return headers, result.json()


def upload(client, headers, order, request_id=None, key=None):
    key = key or str(uuid4())
    response = client.post(f"/api/v1/integrations/orders/{order['id']}/payment-evidence",
        headers={**headers, "Idempotency-Key": key},
        data={"provider": "yape", "looks_like_payment_receipt": "true", "sender": PHONE,
              "whatsapp_message_id": f"wamid-{key}",
              **({"payment_request_id": request_id} if request_id else {})},
        files={"file": ("test.png", b"\x89PNG\r\n\x1a\n" + key.encode(), "image/png")})
    assert response.status_code == 201, response.text
    return response.json()


def review(client, auth_headers, tenant, receipt, approve=True, key=None):
    detail = client.get(f"/api/v1/orders/{receipt['order']['id']}/detail", headers=auth_headers).json()
    return client.post(f"/api/v1/payment-evidence/{receipt['evidence']['id']}/review",
        headers={**auth_headers, "Idempotency-Key": key or str(uuid4())},
        json={"approve": approve, "register_id": tenant["register_id"], "expected_version": detail["order"]["version"]})


def open_cash(client, auth_headers, tenant):
    response = client.post("/api/v1/cash/sessions/open", headers=auth_headers,
        json={"register_id": tenant["register_id"], "opening_amount": 0})
    assert response.status_code in (200, 201), response.text


def test_preview_has_no_order_or_stock_side_effects(client, tenant, auth_headers):
    token = create_credential(client, tenant, auth_headers)["token"]
    response = client.post("/api/v1/integrations/orders/preview", headers=integration_headers(token),
        json={"channel": "counter", "items": [{"product_id": tenant["product_id"], "quantity": 2}]})
    assert response.status_code == 200, response.text
    assert response.json()["known_total"] == 40
    assert response.headers["cache-control"] == "no-store"
    with SessionLocal() as db:
        assert db.query(Order).count() == 0
    with SessionLocal.begin() as db:
        db.get(Product, tenant["product_id"]).available = False
    catalog = client.get("/api/v1/integrations/context/catalog", headers=integration_headers(token)).json()
    assert not catalog["products"][0]["available"]
    assert "recipe" not in catalog["products"][0]
    assert "ingredients" not in catalog


def test_receipt_shape_is_not_payment_approval_and_retry_cannot_duplicate(client, tenant, auth_headers):
    headers, order = setup(client, tenant, auth_headers)
    data = {"provider": "yape", "looks_like_payment_receipt": "true", "sender": PHONE,
        "amount_detected": "100", "operation_number": "TEST-RECEIPT-NO-PAYMENT",
        "security_code": "085", "recipient": "OTRO TITULAR DE PRUEBA",
        "whatsapp_message_id": "isolated-receipt-message"}
    files = {"file": ("PRUEBA-SIN-VALOR.png", b"\x89PNG\r\n\x1a\nreceipt-test-only", "image/png")}
    endpoint = f"/api/v1/integrations/orders/{order['id']}/payment-evidence"
    request_headers = {**headers, "Idempotency-Key": "same-receipt-attempt"}
    first = client.post(endpoint, headers=request_headers, data=data, files=files)
    assert first.status_code == 201, first.text
    result = first.json()
    assert result["requires_human_review"] is True
    assert result["evidence"]["status"] == "under_review"
    assert result["evidence"]["security_code"] == "085"
    assert result["evidence"]["amount_detected"] == 100
    assert result["order"]["sent_to_kitchen_at"] is None
    assert result["order"]["total"] == 20
    assert result["order"]["payment_status"] == "evidence_received"
    repeated = client.post(endpoint, headers=request_headers, data=data, files=files)
    assert repeated.status_code == 201
    assert repeated.json() == result
    with SessionLocal() as db:
        assert db.query(Order).count() == 1
        assert db.query(PaymentEvidence).count() == 1
        assert db.query(Payment).count() == 0
        assert db.query(KitchenTicket).count() == 0
        assert db.query(PrintJob).count() == 0
        assert db.query(IntegrationEvent).count() == 0


def test_initial_and_extra_receipts_coexist_and_review_in_order(client, tenant, auth_headers):
    headers, order = setup(client, tenant, auth_headers)
    initial = upload(client, headers, order)
    body = {"sender": PHONE, "expected_version": initial["order"]["version"],
            "expected_amount": 20, "items": [{"product_id": tenant["product_id"], "quantity": 1}]}
    added = client.post(f"/api/v1/integrations/orders/{order['id']}/item-batches", json=body,
        headers={**headers, "Idempotency-Key": "extra"})
    assert added.status_code == 200, added.text
    repeated = client.post(f"/api/v1/integrations/orders/{order['id']}/item-batches", json=body,
        headers={**headers, "Idempotency-Key": "extra"})
    assert repeated.json() == added.json()
    request_id = added.json()["payment_request"]["id"]
    extra = upload(client, headers, order, request_id)
    assert review(client, auth_headers, tenant, extra).status_code == 409
    open_cash(client, auth_headers, tenant)
    first = review(client, auth_headers, tenant, initial)
    assert first.status_code == 200, first.text
    second = review(client, auth_headers, tenant, extra)
    assert second.status_code == 200, second.text
    assert second.json()["order"]["total"] == 40
    with SessionLocal() as db:
        assert db.query(Payment).count() == 2
        assert db.query(PaymentEvidence).filter_by(status="paid").count() == 2
        assert db.query(IntegrationEvent).filter_by(event_type="payment.approved").count() == 2
    again = review(client, auth_headers, tenant, extra)
    assert again.status_code == 200, again.text
    with SessionLocal() as db:
        assert db.query(Payment).count() == 2


def test_reject_extra_preserves_original_and_allows_new_receipt(client, tenant, auth_headers):
    headers, order = setup(client, tenant, auth_headers)
    initial = upload(client, headers, order)
    open_cash(client, auth_headers, tenant)
    paid = review(client, auth_headers, tenant, initial)
    assert paid.status_code == 200, paid.text
    added = client.post(f"/api/v1/integrations/orders/{order['id']}/item-batches",
        headers={**headers, "Idempotency-Key": "extra"}, json={"sender": PHONE,
        "expected_version": paid.json()["order"]["version"], "expected_amount": 20,
        "items": [{"product_id": tenant["product_id"], "quantity": 1}]})
    assert added.status_code == 200, added.text
    extra = upload(client, headers, order, added.json()["payment_request"]["id"])
    rejected = review(client, auth_headers, tenant, extra, False)
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["order"]["status"] == "sent_to_kitchen"
    assert rejected.json()["order"]["total"] == 20
    replacement = upload(client, headers, order, added.json()["payment_request"]["id"])
    assert replacement["requires_human_review"]


def test_initial_review_replays_lost_response_without_second_payment(client, tenant, auth_headers):
    headers, order = setup(client, tenant, auth_headers)
    receipt = upload(client, headers, order)
    open_cash(client, auth_headers, tenant)
    path = f"/api/v1/payment-evidence/{receipt['evidence']['id']}/review"
    body = {"approve": True, "register_id": tenant["register_id"],
            "expected_version": receipt["order"]["version"]}
    request_headers = {**auth_headers, "Idempotency-Key": "review-response-lost"}
    first = client.post(path, json=body, headers=request_headers)
    assert first.status_code == 200, first.text
    repeated = client.post(path, json=body, headers=request_headers)
    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == first.json()
    assert client.post(path, json={**body, "approve": False}, headers=request_headers).status_code == 409
    with SessionLocal() as db:
        assert db.query(Payment).count() == 1
        assert db.query(IntegrationEvent).filter_by(event_type="payment.approved").count() == 1


@pytest.mark.parametrize("method", ["cash", "yape"])
def test_pending_delivery_can_prepare_but_cannot_dispatch(client, tenant, auth_headers, method):
    headers, order = setup(client, tenant, auth_headers, quote=True, method=method)
    if method == "cash":
        prepared = client.post(f"/api/v1/integrations/orders/{order['id']}/cash-confirm",
            headers={**headers, "Idempotency-Key": "confirm"})
    else:
        receipt = upload(client, headers, order)
        open_cash(client, auth_headers, tenant)
        prepared = review(client, auth_headers, tenant, receipt)
    assert prepared.status_code == 200, prepared.text
    assert prepared.json()["order"]["final_total"] is None
    assert prepared.json()["order"]["payment_status"] != "paid"
    with SessionLocal.begin() as db:
        db.get(Order, order["id"]).status = "ready"
    blocked = client.post(f"/api/v1/orders/{order['id']}/transition", headers=auth_headers,
                          json={"status": "dispatched"})
    assert blocked.status_code == 409, blocked.text
    with SessionLocal() as db:
        version = db.get(Order, order["id"]).version
    fee = client.patch(f"/api/v1/orders/{order['id']}/delivery-fee",
        headers={**auth_headers, "Idempotency-Key": "fee"},
        json={"expected_version": version, "amount": 5, "method": "cash"})
    assert fee.status_code == 200, fee.text
    assert fee.json()["order"]["total"] == 25
    assert fee.json()["order"]["delivery_fee_status"] == "final"


def test_addition_rejects_other_customer_and_key_reuse(client, tenant, auth_headers):
    headers, order = setup(client, tenant, auth_headers)
    body = {"sender": "51888888888", "expected_version": order["version"], "expected_amount": 20,
            "items": [{"product_id": tenant["product_id"], "quantity": 1}]}
    path = f"/api/v1/integrations/orders/{order['id']}/item-batches"
    assert client.post(path, json=body, headers={**headers, "Idempotency-Key": "same"}).status_code == 404
    body["sender"] = PHONE
    assert client.post(path, json=body, headers={**headers, "Idempotency-Key": "same"}).status_code == 200
    body["expected_amount"] = 40
    assert client.post(path, json=body, headers={**headers, "Idempotency-Key": "same"}).status_code == 409


def test_prepared_line_is_protected_and_customer_state_reports_exact_balance(client, tenant, auth_headers):
    headers, order = setup(client, tenant, auth_headers, method="cash")
    confirmed = client.post(f"/api/v1/integrations/orders/{order['id']}/cash-confirm",
                            headers={**headers, "Idempotency-Key": "confirm"})
    assert confirmed.status_code == 200, confirmed.text
    with SessionLocal.begin() as db:
        db.scalar(select(KitchenTicket).where(KitchenTicket.order_id == order["id"])).status = "ready"
    state = client.get(f"/api/v1/integrations/orders/{order['id']}/customer-state", params={"sender": PHONE}, headers=headers)
    assert state.status_code == 200, state.text
    assert state.json()["allowed_actions"]["editable_item_ids"] == []
    assert state.json()["remaining_amount"] == 20
    response = client.post(f"/api/v1/integrations/orders/{order['id']}/item-revisions",
        headers={**headers, "Idempotency-Key": "prepared-change"}, json={"sender": PHONE,
        "expected_version": state.json()["version"], "operations": [{"type": "edit", "item_id": order["items"][0]["id"],
        "replacement": {"product_id": tenant["product_id"], "quantity": 2}}]})
    assert response.status_code == 409, response.text
    open_cash(client, auth_headers, tenant)
    paid = client.post(f"/api/v1/orders/{order['id']}/payments", headers=auth_headers,
        json={"method": "cash", "amount": 20, "register_id": tenant["register_id"]})
    assert paid.status_code in (200, 201), paid.text
    state = client.get(f"/api/v1/integrations/orders/{order['id']}/customer-state", params={"sender": PHONE}, headers=headers).json()
    assert state["paid_amount"] == 20 and state["remaining_amount"] == 0
    assert not state["allowed_actions"]["add_items"]


def test_shipping_yape_is_independent_and_blocks_all_dispatch_until_review(client, tenant, auth_headers):
    headers, order = setup(client, tenant, auth_headers, quote=True)
    receipt = upload(client, headers, order)
    open_cash(client, auth_headers, tenant)
    first = review(client, auth_headers, tenant, receipt)
    assert first.status_code == 200, first.text
    with SessionLocal.begin() as db:
        db.get(Order, order["id"]).status = "ready"
    version = first.json()["order"]["version"]
    body = {"expected_version": version, "amount": 5}
    fee = client.patch(f"/api/v1/orders/{order['id']}/delivery-fee", json=body,
                       headers={**auth_headers, "Idempotency-Key": "fee"})
    assert fee.status_code == 200, fee.text
    again = client.patch(f"/api/v1/orders/{order['id']}/delivery-fee", json=body,
                         headers={**auth_headers, "Idempotency-Key": "fee"})
    assert again.json() == fee.json()
    path = f"/api/v1/orders/{order['id']}/transition"
    assert client.post(path, headers=auth_headers, json={"status": "dispatched"}).status_code == 409
    chosen = client.patch(f"/api/v1/integrations/orders/{order['id']}/delivery-payment",
        headers={**headers, "Idempotency-Key": "method"}, json={"sender": PHONE, "method": "yape", "expected_version": fee.json()["order"]["version"]})
    assert chosen.status_code == 200, chosen.text
    assert client.post(path, headers=auth_headers, json={"status": "dispatched"}).status_code == 409
    extra = upload(client, headers, order, chosen.json()["payment_request"]["id"])
    accepted = review(client, auth_headers, tenant, extra)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["order"]["total"] == 25
    dispatched = client.post(path, headers=auth_headers, json={"status": "dispatched"})
    assert dispatched.status_code == 200, dispatched.text
    with SessionLocal() as db:
        assert db.query(Payment).count() == 2
        assert db.query(KitchenTicket).count() == 1
        assert db.query(IntegrationEvent).filter_by(event_type="payment.approved").count() == 1


def test_ready_notice_waits_for_addition_and_deduplicates_reopening(client, tenant, auth_headers):
    from app.agent_checkout import ready_event
    from app.models import OrderPaymentRequest
    headers, order = setup(client, tenant, auth_headers, method="cash")
    assert client.post(f"/api/v1/integrations/orders/{order['id']}/cash-confirm",
        headers={**headers, "Idempotency-Key": "confirm"}).status_code == 200
    with SessionLocal.begin() as db:
        saved = db.get(Order, order["id"])
        ticket = db.scalar(select(KitchenTicket).where(KitchenTicket.order_id == saved.id))
        ticket.status = "ready"
        request = OrderPaymentRequest(business_id=saved.business_id, branch_id=saved.branch_id,
            order_id=saved.id, purpose="addition", method="yape", amount=20, status="under_review", snapshot={})
        db.add(request)
        assert ready_event(db, saved) is None
        request.status = "rejected"
        assert ready_event(db, saved) is not None
        ticket.status = "preparing"
        assert ready_event(db, saved) is None
        ticket.status = "ready"
        assert ready_event(db, saved) is None
        legacy = db.scalar(select(IntegrationEvent).where(IntegrationEvent.event_type == "order.ready"))
        legacy.payload = {"status": "ready"}
        assert ready_event(db, saved) is None
    with SessionLocal() as db:
        assert db.query(IntegrationEvent).filter_by(event_type="order.ready").count() == 1


def test_preview_enforces_modifier_quantity_and_group_limits(client, tenant, auth_headers):
    with SessionLocal.begin() as db:
        group = ModifierGroup(business_id=tenant["business_id"], branch_id=tenant["branch_id"], name="Una salsa", minimum=1, maximum=1, required=True)
        db.add(group)
        db.flush()
        option = Modifier(group_id=group.id, name="BBQ", price_delta=2)
        db.add(option)
        db.add(ProductModifierGroup(product_id=tenant["product_id"], group_id=group.id))
        db.flush()
        option_id = option.id
    token = create_credential(client, tenant, auth_headers)["token"]
    headers = integration_headers(token)
    body = {"channel": "counter", "items": [{"product_id": tenant["product_id"], "quantity": 1, "modifiers": []}]}
    path = "/api/v1/integrations/orders/preview"
    assert client.post(path, headers=headers, json=body).status_code in (409, 422)
    body["items"][0]["modifiers"] = [{"modifier_id": option_id, "quantity": 1}]
    valid = client.post(path, headers=headers, json=body)
    assert valid.status_code == 200, valid.text
    assert valid.json()["known_total"] == 22
    body["items"][0]["modifiers"][0]["quantity"] = 2
    assert client.post(path, headers=headers, json=body).status_code in (409, 422)


def test_initial_evidence_rejects_wrong_sender_even_without_extra_request(client, tenant, auth_headers):
    headers, order = setup(client, tenant, auth_headers)
    response = client.post(f"/api/v1/integrations/orders/{order['id']}/payment-evidence",
        headers={**headers, "Idempotency-Key": "wrong-sender"},
        data={"sender": "51888888888", "provider": "yape"},
        files={"file": ("test.png", b"test", "image/png")})
    assert response.status_code == 404
