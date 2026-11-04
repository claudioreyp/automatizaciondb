"""Isolated domain regressions for the 2026-10-18 agent contract.

No WhatsApp, storage, routes, payment provider or production database is used.
"""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app import api
from app.database import SessionLocal
from app.models import (AgentAttentionRequest, AuditEvent, BranchSettings, Courier, DeliveryAssignment, DeliveryQuote,
    KitchenTicket, Order, OrderPaymentRequest, Payment, PaymentEvidence, Product, utcnow)
from test_escalar_integrations import create_credential, integration_headers

PHONE = "51912345678"
DESTINATION = {"address": "Calle Uno 123", "reference": "Al lado del parque", "delivery_service": "own"}


@pytest.fixture(autouse=True)
def no_external_effects(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Domain tests must not call external services")
    async def image_analysis(*args, **kwargs):
        return {"available": True, "looks_like_payment_receipt": True}
    async def storage(*args, **kwargs):
        return "isolated/not-an-upload.png"
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(api, "analyze_payment_image", image_analysis)
    monkeypatch.setattr(api, "store_private_file", storage)


def setup_order(client, tenant, auth, method="cash", channel="takeaway", fee=0):
    with SessionLocal.begin() as db:
        db.add(BranchSettings(business_id=tenant["business_id"], branch_id=tenant["branch_id"],
            delivery_mode="fixed", fixed_delivery_fee=fee,
            payment_methods={key: ["cash", "yape"] for key in ("delivery", "takeaway", "counter")}))
    headers = integration_headers(create_credential(client, tenant, auth)["token"])
    response = client.post("/api/v1/integrations/orders/draft", headers={**headers, "Idempotency-Key": "draft"},
        json={"branch_id": tenant["branch_id"], "source": "whatsapp_agent", "channel": channel,
            "payment_method": method, "delivery_fee": fee if channel == "delivery" else 0, "customer_phone": PHONE,
            "whatsapp_chat_id": PHONE + "@c.us", "delivery_address": DESTINATION if channel == "delivery" else None,
            "items": [{"product_id": tenant["product_id"], "quantity": 1}]})
    assert response.status_code == 201, response.text
    order = response.json()
    if method == "cash":
        response = client.post(f"/api/v1/integrations/orders/{order['id']}/cash-confirm",
            headers={**headers, "Idempotency-Key": "cash-confirm"})
        assert response.status_code == 200, response.text
        order = response.json()["order"]
    return headers, order


def current(client, headers, order):
    response = client.get(f"/api/v1/integrations/orders/{order['id']}/customer-state", headers=headers,
        params={"sender": PHONE})
    assert response.status_code == 200, response.text
    return response.json()


def receipt(client, headers, order, key="base", request_id=None, amount=None):
    response = client.post(f"/api/v1/integrations/orders/{order['id']}/payment-evidence",
        headers={**headers, "Idempotency-Key": key},
        data={"provider": "yape", "sender": PHONE, "looks_like_payment_receipt": "true",
            "whatsapp_message_id": "wamid-" + key,
            **({"payment_request_id": request_id} if request_id else {}),
            **({"amount_detected": amount} if amount is not None else {})},
        files={"file": ("isolated.png", b"\x89PNG\r\n\x1a\n" + key.encode(), "image/png")})
    assert response.status_code == 201, response.text
    return response.json()


def approve(client, auth, tenant, item, **extra):
    detail = client.get(f"/api/v1/orders/{item['order']['id']}/detail", headers=auth).json()
    return client.post(f"/api/v1/payment-evidence/{item['evidence']['id']}/review",
        headers={**auth, "Idempotency-Key": "review-" + str(item["evidence"]["id"])},
        json={"approve": True, "register_id": tenant["register_id"],
            "expected_version": detail["order"]["version"], **extra})


def preview(client, headers, order, channel="delivery", destination=None, key="quote"):
    body = {"sender": PHONE, "expected_version": order["version"], "channel": channel,
        "delivery_address": (destination or DESTINATION) if channel == "delivery" else None}
    response = client.post(f"/api/v1/integrations/orders/{order['id']}/fulfillment-preview",
        headers={**headers, "Idempotency-Key": key}, json=body)
    assert response.status_code == 200, response.text
    return {**body, "quote_id": response.json()["quote"]["id"],
        "expected_delivery_fee": response.json()["quote"]["fee"]}


def change(client, headers, order, body, key="change"):
    return client.patch(f"/api/v1/integrations/orders/{order['id']}/fulfillment",
        headers={**headers, "Idempotency-Key": key}, json=body)


def addition(client, headers, order, method="cash", key="addition"):
    return client.post(f"/api/v1/integrations/orders/{order['id']}/item-batches",
        headers={**headers, "Idempotency-Key": key}, json={"sender": PHONE,
            "expected_version": order["version"], "payment_method": method, "expected_amount": 20,
            "items": [{"product_id": order["items"][0]["product_id"], "quantity": 1}]})


def test_fulfillment_quote_is_bound_idempotent_and_preserves_original_command(client, tenant, auth_headers):
    headers, order = setup_order(client, tenant, auth_headers, fee=6)
    with SessionLocal() as db:
        original = db.scalar(select(KitchenTicket).where(KitchenTicket.order_id == order["id"]))
        original_id, original_items = original.id, deepcopy(original.items_snapshot)
    body = preview(client, headers, order)
    changed = change(client, headers, order, body)
    assert changed.status_code == 200, changed.text
    result = changed.json()
    assert result["order"]["id"] == order["id"]
    assert result["order"]["channel"] == "delivery"
    assert result["command_committed"] is False
    assert change(client, headers, order, body).json() == result
    wrong = change(client, headers, order, {**body, "delivery_address": {**DESTINATION, "address": "Otra 999"}})
    assert wrong.status_code == 409
    with SessionLocal() as db:
        assert db.query(KitchenTicket).count() == 1
        assert db.get(KitchenTicket, original_id).items_snapshot == original_items
        assert db.query(AgentAttentionRequest).filter_by(kind="fulfillment_change").count() == 1
        assert db.query(Payment).count() == 0


@pytest.mark.parametrize("drift", ["version", "destination", "config", "expired", "sender", "fee"])
def test_fulfillment_rejects_stale_or_foreign_quote_without_order_mutation(client, tenant, auth_headers, drift):
    headers, order = setup_order(client, tenant, auth_headers)
    body = preview(client, headers, order)
    with SessionLocal.begin() as db:
        if drift == "version":
            db.get(Order, order["id"]).version += 1
        elif drift == "config":
            db.scalar(select(BranchSettings)).version += 1
        elif drift == "expired":
            db.get(DeliveryQuote, body["quote_id"]).expires_at = utcnow() - timedelta(seconds=1)
    if drift == "destination":
        body["delivery_address"] = {**DESTINATION, "address": "Otra calle 321"}
    elif drift == "sender":
        body["sender"] = "51888888888"
    elif drift == "fee":
        body["expected_delivery_fee"] = 100
    response = change(client, headers, order, body)
    assert response.status_code in {404, 409}, response.text
    with SessionLocal() as db:
        assert db.get(Order, order["id"]).channel == "takeaway"
        assert db.query(AgentAttentionRequest).count() == 0


def test_assigned_delivery_allows_destination_and_pickup_without_cancelling_order(client, tenant, auth_headers):
    headers, order = setup_order(client, tenant, auth_headers, channel="delivery", fee=5)
    with SessionLocal.begin() as db:
        courier = Courier(business_id=tenant["business_id"], branch_id=tenant["branch_id"], name="Test courier", phone="51911111111")
        db.add(courier)
        db.flush()
        courier_id = courier.id
    assigned = client.post(f"/api/v1/delivery/orders/{order['id']}/assign", headers=auth_headers, json={"courier_id": courier_id})
    assert assigned.status_code == 200, assigned.text
    order = current(client, headers, order)
    body = preview(client, headers, order, destination={**DESTINATION, "address": "Calle Nueva 456"})
    changed = change(client, headers, order, body)
    assert changed.status_code == 200, changed.text
    with SessionLocal() as db:
        assignment = db.scalar(select(DeliveryAssignment))
        assert assignment.status == "assigned" and assignment.address["address"] == "Calle Nueva 456"
    order = changed.json()["order"]
    pickup = change(client, headers, order, preview(client, headers, order, "takeaway", key="pickup-quote"), "pickup")
    assert pickup.status_code == 200, pickup.text
    with SessionLocal() as db:
        assert db.get(Order, order["id"]).status != "cancelled"
        assert db.scalar(select(DeliveryAssignment)).status == "cancelled"
        assert db.get(Courier, courier_id).status == "available"
        assert db.query(KitchenTicket).count() == 1
    assert client.post(f"/api/v1/delivery/orders/{order['id']}/assign", headers=auth_headers,
        json={"courier_id": courier_id}).status_code == 422


@pytest.mark.parametrize("status", ["dispatched", "delivered", "closed", "cancelled"])
def test_completed_order_cannot_change_or_receive_new_receipt(client, tenant, auth_headers, status):
    headers, order = setup_order(client, tenant, auth_headers, method="yape")
    with SessionLocal.begin() as db:
        db.get(Order, order["id"]).status = status
    response = client.post(f"/api/v1/integrations/orders/{order['id']}/fulfillment-preview",
        headers={**headers, "Idempotency-Key": "terminal-quote"},
        json={"sender": PHONE, "expected_version": order["version"], "channel": "delivery", "delivery_address": DESTINATION})
    assert response.status_code == 409
    response = client.post(f"/api/v1/integrations/orders/{order['id']}/payment-evidence",
        headers={**headers, "Idempotency-Key": "terminal-receipt"},
        data={"sender": PHONE, "looks_like_payment_receipt": "true", "whatsapp_message_id": "wamid-terminal"},
        files={"file": ("x.png", b"\x89PNG\r\n\x1a\nterminal", "image/png")})
    assert response.status_code == 409, response.text
    with SessionLocal() as db:
        assert db.query(PaymentEvidence).count() == 0


def test_lower_delivery_fee_preserves_confirmed_payment_and_records_excess(client, tenant, auth_headers):
    headers, order = setup_order(client, tenant, auth_headers, method="yape", channel="delivery", fee=10)
    base = receipt(client, headers, order, amount=30)
    paid = approve(client, auth_headers, tenant, base)
    assert paid.status_code == 200, paid.text
    order = current(client, headers, order)
    body = preview(client, headers, order, "takeaway")
    changed = change(client, headers, order, body)
    assert changed.status_code == 200, changed.text
    assert changed.json()["financial_summary"]["overpayment_amount"] == 10
    with SessionLocal() as db:
        assert db.scalar(select(Payment)).amount == Decimal("30")
        assert db.get(Order, order["id"]).total == Decimal("20")
        assert db.get(Order, order["id"]).payment_status == "paid"
        assert db.query(AgentAttentionRequest).filter_by(kind="financial_review").count() == 1
        assert db.query(KitchenTicket).count() == 1


def test_changed_receipt_amount_requires_explicit_human_amount_and_preserves_evidence(client, tenant, auth_headers):
    headers, order = setup_order(client, tenant, auth_headers, method="yape", channel="delivery", fee=10)
    base = receipt(client, headers, order, amount=30)
    order = current(client, headers, order)
    response = change(client, headers, order, preview(client, headers, order, "takeaway"))
    assert response.status_code == 200, response.text
    blocked = approve(client, auth_headers, tenant, base)
    assert blocked.status_code == 422, blocked.text
    approved = approve(client, auth_headers, tenant, base, note="Verificado ingreso de 30; revisar exceso de 10", confirmed_amount=30)
    assert approved.status_code == 200, approved.text
    with SessionLocal() as db:
        evidence = db.get(PaymentEvidence, base["evidence"]["id"])
        assert evidence.analysis["expected_amount"] == 30
        assert evidence.storage_path == "isolated/not-an-upload.png"
        assert db.scalar(select(Payment)).amount == Decimal("30")


@pytest.mark.parametrize("method", ["cash", "yape"])
def test_pending_base_preserves_addition_and_never_sends_it_early(client, tenant, auth_headers, method):
    headers, order = setup_order(client, tenant, auth_headers, method="yape")
    base = receipt(client, headers, order)
    order = current(client, headers, order)
    requested = addition(client, headers, order, method)
    assert requested.status_code == 200, requested.text
    request = requested.json()["payment_request"]
    assert request["status"] == "awaiting_base_review"
    assert requested.json()["command_committed"] is False
    assert addition(client, headers, order, method).json() == requested.json()
    with SessionLocal() as db:
        assert len(db.get(Order, order["id"]).items) == 1
        assert db.query(KitchenTicket).count() == db.query(Payment).count() == 0
    additional_receipt = receipt(client, headers, order, "additional", request["id"]) if method == "yape" else None
    if additional_receipt:
        assert approve(client, auth_headers, tenant, additional_receipt).status_code == 409
    approved = approve(client, auth_headers, tenant, base)
    assert approved.status_code == 200, approved.text
    with SessionLocal() as db:
        assert db.query(KitchenTicket).count() == (2 if method == "cash" else 1)
        assert db.query(Payment).count() == 1
        assert db.get(OrderPaymentRequest, request["id"]).status == ("committed" if method == "cash" else "under_review")
    if additional_receipt:
        approved_addition = approve(client, auth_headers, tenant, additional_receipt)
        assert approved_addition.status_code == 200, approved_addition.text
    with SessionLocal() as db:
        tickets = list(db.scalars(select(KitchenTicket).order_by(KitchenTicket.sequence)))
        assert len(tickets) == 2
        assert len(tickets[0].items_snapshot) == len(tickets[1].items_snapshot) == 1
        assert db.query(Order).count() == 1


def test_cash_addition_revalidation_failure_does_not_undo_base_approval(client, tenant, auth_headers):
    headers, order = setup_order(client, tenant, auth_headers, method="yape")
    base = receipt(client, headers, order)
    order = current(client, headers, order)
    requested = addition(client, headers, order).json()["payment_request"]
    with SessionLocal.begin() as db:
        db.get(Product, tenant["product_id"]).price = Decimal("25")
    approved = approve(client, auth_headers, tenant, base)
    assert approved.status_code == 200, approved.text
    with SessionLocal() as db:
        assert db.get(PaymentEvidence, base["evidence"]["id"]).status == "paid"
        assert db.get(OrderPaymentRequest, requested["id"]).status == "requires_review"
        assert len(db.get(Order, order["id"]).items) == 1
        assert db.query(KitchenTicket).count() == db.query(Payment).count() == 1
        assert db.query(AgentAttentionRequest).filter_by(kind="financial_review").count() == 1


def test_pending_requotation_retains_paid_fee_and_only_reviews_the_difference(client, tenant, auth_headers):
    headers, order = setup_order(client, tenant, auth_headers, method="yape", channel="delivery", fee=10)
    base = receipt(client, headers, order, amount=30)
    assert approve(client, auth_headers, tenant, base).status_code == 200
    with SessionLocal.begin() as db:
        settings = db.scalar(select(BranchSettings))
        settings.delivery_mode = "quote"
        settings.version += 1
    order = current(client, headers, order)
    body = preview(client, headers, order, destination={**DESTINATION, "address": "Calle Distinta 456"})
    assert body["expected_delivery_fee"] is None
    changed = change(client, headers, order, body)
    assert changed.status_code == 200, changed.text
    pending = changed.json()["order"]
    assert pending["delivery_fee_status"] == "pending_quote"
    result = client.patch(f"/api/v1/orders/{order['id']}/delivery-fee",
        headers={**auth_headers, "Idempotency-Key": "staff-requote"},
        json={"expected_version": pending["version"], "amount": 8})
    assert result.status_code == 200, result.text
    assert result.json()["payment_request"]["amount"] == 0
    detail = client.get(f"/api/v1/orders/{order['id']}/detail", headers=auth_headers).json()
    assert detail["financial_summary"]["overpayment_amount"] == 2
    assert detail["order"]["total"] == 28
    with SessionLocal() as db:
        assert db.scalar(select(Payment)).amount == Decimal("30")
        assert db.query(AgentAttentionRequest).filter_by(kind="financial_review").count() == 1


def test_delivery_repeated_changes_do_not_recharge_preserved_payment(client, tenant, auth_headers):
    headers, order = setup_order(client, tenant, auth_headers, method="yape", channel="delivery", fee=10)
    base = receipt(client, headers, order)
    assert approve(client, auth_headers, tenant, base).status_code == 200
    order = current(client, headers, order)
    pickup = change(client, headers, order, preview(client, headers, order, "takeaway"))
    assert pickup.status_code == 200, pickup.text
    with SessionLocal.begin() as db:
        settings = db.scalar(select(BranchSettings))
        settings.fixed_delivery_fee = Decimal("5")
        settings.version += 1
    order = current(client, headers, order)
    delivery = change(client, headers, order, preview(client, headers, order, key="back-to-delivery"), "back-to-delivery")
    assert delivery.status_code == 200, delivery.text
    assert delivery.json()["payment_request"] is None
    assert delivery.json()["financial_summary"]["overpayment_amount"] == 5
    with SessionLocal() as db:
        assert db.query(Payment).count() == 1
        assert db.scalar(select(Payment)).amount == Decimal("30")
        assert db.query(OrderPaymentRequest).count() == 0


def test_increased_delivery_requires_separate_payment_choice_and_keeps_paid_original(client, tenant, auth_headers):
    headers, order = setup_order(client, tenant, auth_headers, method="yape", channel="delivery", fee=5)
    base = receipt(client, headers, order)
    assert approve(client, auth_headers, tenant, base).status_code == 200
    with SessionLocal.begin() as db:
        settings = db.scalar(select(BranchSettings))
        settings.fixed_delivery_fee = Decimal("9")
        settings.version += 1
    order = current(client, headers, order)
    response = change(client, headers, order, preview(client, headers, order, destination={**DESTINATION, "address": "Otra Calle 456"}))
    assert response.status_code == 200, response.text
    assert response.json()["payment_request"]["amount"] == 4
    assert response.json()["payment_request"]["method"] == "unselected"
    assert response.json()["order"]["payment_method"] == "yape"
    with SessionLocal() as db:
        assert db.scalar(select(Payment)).amount == Decimal("25")
        assert db.query(KitchenTicket).count() == 1


def test_assignment_and_dispatch_with_stale_version_cannot_undo_fulfillment(client, tenant, auth_headers):
    headers, order = setup_order(client, tenant, auth_headers, channel="delivery", fee=5)
    old_version = order["version"]
    changed = change(client, headers, order, preview(client, headers, order, destination={**DESTINATION, "address": "Nueva Calle 789"}))
    assert changed.status_code == 200, changed.text
    assigned = client.post(f"/api/v1/delivery/orders/{order['id']}/assign", headers=auth_headers,
        json={"expected_version": old_version})
    assert assigned.status_code == 409, assigned.text
    with SessionLocal.begin() as db:
        db.add(DeliveryAssignment(business_id=tenant["business_id"], branch_id=tenant["branch_id"],
            order_id=order["id"], status="ready", address=DESTINATION, fee=5))
    dispatched = client.post(f"/api/v1/delivery/orders/{order['id']}/transition", headers=auth_headers,
        json={"status": "dispatched", "expected_version": old_version})
    assert dispatched.status_code == 409, dispatched.text
    with SessionLocal() as db:
        assert db.scalar(select(DeliveryAssignment)).dispatched_at is None
        assert db.get(Order, order["id"]).status != "dispatched"


def test_rejected_initial_payment_keeps_cash_addition_pending_without_command(client, tenant, auth_headers):
    headers, order = setup_order(client, tenant, auth_headers, method="yape")
    base = receipt(client, headers, order)
    order = current(client, headers, order)
    requested = addition(client, headers, order).json()["payment_request"]
    rejected = approve(client, auth_headers, tenant, base, approve=False, note="No se recibió el pago")
    assert rejected.status_code == 200, rejected.text
    with SessionLocal() as db:
        assert db.get(OrderPaymentRequest, requested["id"]).status == "awaiting_base_review"
        assert db.query(Payment).count() == db.query(KitchenTicket).count() == 0
        assert len(db.get(Order, order["id"]).items) == 1


@pytest.mark.parametrize("method", ["cash", "yape"])
def test_staff_declines_only_pending_addition_preserving_base_and_receipt(client, tenant, auth_headers, method):
    headers, order = setup_order(client, tenant, auth_headers, method="yape")
    base = receipt(client, headers, order)
    requested = addition(client, headers, current(client, headers, order), method).json()["payment_request"]
    evidence = receipt(client, headers, order, "additional", requested["id"]) if method == "yape" else None
    order = current(client, headers, order)
    url = f"/api/v1/orders/{order['id']}/addition-requests/{requested['id']}/resolve"
    body = {"action": "decline", "expected_version": order["version"], "note": "Cliente prefiere conservar solo el pedido original"}
    request_headers = {**auth_headers, "Idempotency-Key": "decline-addition"}
    assert client.post(url, headers={**request_headers, "X-Dev-Role": "dispatcher"}, json=body).status_code == 403
    assert client.post(url, headers={**request_headers, "X-Business-Id": str(tenant["other_business_id"]),
        "X-Branch-Id": str(tenant["other_branch_id"])}, json=body).status_code in {403, 404}
    assert client.post(url, headers=request_headers, json={**body, "expected_version": order["version"] - 1}).status_code == 409
    response = client.post(url, headers=request_headers, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["payment_request"]["status"] == "declined"
    assert response.json()["payment_request"]["resolution_note"] == body["note"]
    assert client.post(url, headers=request_headers, json=body).json() == response.json()
    with SessionLocal() as db:
        assert db.query(Payment).count() == db.query(KitchenTicket).count() == 0
        assert len(db.get(Order, order["id"]).items) == 1
        assert db.get(PaymentEvidence, base["evidence"]["id"]).status == "under_review"
        assert db.query(AuditEvent).filter_by(action="payment_request.declined").count() == 1
        if evidence:
            saved = db.get(PaymentEvidence, evidence["evidence"]["id"])
            assert saved.status == "rejected" and saved.storage_path == "isolated/not-an-upload.png"
    assert approve(client, auth_headers, tenant, base).status_code == 200
    with SessionLocal() as db:
        assert db.query(Payment).count() == db.query(KitchenTicket).count() == 1
        assert len(db.get(Order, order["id"]).items) == 1
        from app.agent_checkout import ensure_dispatch
        ensure_dispatch(db, db.get(Order, order["id"]))


def test_decline_cannot_remove_committed_cash_addition(client, tenant, auth_headers):
    headers, order = setup_order(client, tenant, auth_headers, method="yape")
    base = receipt(client, headers, order)
    requested = addition(client, headers, current(client, headers, order)).json()["payment_request"]
    assert approve(client, auth_headers, tenant, base).status_code == 200
    order = current(client, headers, order)
    response = client.post(f"/api/v1/orders/{order['id']}/addition-requests/{requested['id']}/resolve",
        headers={**auth_headers, "Idempotency-Key": "decline-committed"},
        json={"action": "decline", "expected_version": order["version"], "note": "No debe retirar productos enviados"})
    assert response.status_code == 409, response.text
    with SessionLocal() as db:
        assert db.get(OrderPaymentRequest, requested["id"]).status == "committed"
        assert len(db.get(Order, order["id"]).items) == 2
        assert db.query(KitchenTicket).count() == 2


def test_pending_delivery_uses_digital_service_setting_not_manual_pos(client, tenant, auth_headers):
    headers, _ = setup_order(client, tenant, auth_headers)
    with SessionLocal.begin() as db:
        settings = db.scalar(select(BranchSettings))
        settings.pos_delivery = False
        settings.digital_delivery = True
        settings.delivery_mode = "quote"
    body = {"branch_id": tenant["branch_id"], "source": "whatsapp_agent", "channel": "delivery",
        "payment_method": "cash", "allow_pending_delivery_quote": True, "customer_phone": PHONE,
        "whatsapp_chat_id": PHONE + "@c.us", "delivery_address": DESTINATION,
        "items": [{"product_id": tenant["product_id"], "quantity": 1}]}
    response = client.post("/api/v1/integrations/orders/draft", headers={**headers, "Idempotency-Key": "digital-quote"}, json=body)
    assert response.status_code == 201, response.text
    assert response.json()["delivery_fee_status"] == "pending_quote"
    with SessionLocal.begin() as db:
        settings = db.scalar(select(BranchSettings))
        settings.pos_delivery = True
        settings.digital_delivery = False
    response = client.post("/api/v1/integrations/orders/draft", headers={**headers, "Idempotency-Key": "disabled-digital-quote"}, json=body)
    assert response.status_code == 409, response.text
