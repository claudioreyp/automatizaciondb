from datetime import timedelta
from copy import deepcopy
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select

from app.database import SessionLocal
from app.models import AuditEvent, BranchSettings, InventoryItem, KitchenTicket, Order, OrderPaymentRequest, Payment, PaymentEvidence, PrintJob, Product, utcnow
from app.pos_printing import build_print_payload, content_digest
from test_escalar_integrations import create_credential, integration_headers


PHONE = "51999999999"
DESTINATION = {"address": "Calle Prueba 123", "reference": "Puerta azul", "maps_url": "https://maps.app.goo.gl/abc123"}


def agent_order(client, tenant, auth_headers, *, method="yape", mode="fixed", fee=5, items=None):
    with SessionLocal.begin() as db:
        db.add(BranchSettings(business_id=tenant["business_id"], branch_id=tenant["branch_id"],
            delivery_mode=mode, fixed_delivery_fee=fee,
            payment_methods={key: ["cash", "yape"] for key in ("delivery", "takeaway", "counter")}))
    token = create_credential(client, tenant, auth_headers)["token"]
    headers = integration_headers(token)
    created = client.post("/api/v1/integrations/orders/draft", headers={**headers, "Idempotency-Key": "initial"}, json={
        "branch_id": tenant["branch_id"], "source": "whatsapp_agent", "channel": "takeaway",
        "payment_method": method, "customer_phone": PHONE, "whatsapp_chat_id": PHONE + "@c.us",
        "notes": "RECOJO EN LOCAL | Tocar timbre",
        "items": items or [{"product_id": tenant["product_id"], "quantity": 1}],
    })
    assert created.status_code == 201, created.text
    return headers, created.json()


def change(client, headers, order, *, key="delivery-change", **overrides):
    body = {"sender": PHONE, "expected_version": order["version"], "channel": "delivery",
            "delivery_address": DESTINATION, **overrides}
    return client.patch(f"/api/v1/integrations/orders/{order['id']}/fulfillment",
        headers={**headers, "Idempotency-Key": key}, json=body)


def upload_receipt(client, headers, order):
    result = client.post(f"/api/v1/integrations/orders/{order['id']}/payment-evidence",
        headers={**headers, "Idempotency-Key": "original-receipt"},
        data={"provider": "yape", "looks_like_payment_receipt": "true", "sender": PHONE,
              "whatsapp_message_id": "wamid-original-receipt"},
        files={"file": ("proof.png", b"\x89PNG\r\n\x1a\noriginal-proof", "image/png")})
    assert result.status_code == 201, result.text
    return result.json()


def test_yape_pending_receipt_can_change_to_delivery_with_separate_fixed_fee(client, tenant, auth_headers):
    headers, order = agent_order(client, tenant, auth_headers)
    receipt = upload_receipt(client, headers, order)
    original = receipt["order"]
    response = change(client, headers, original)
    assert response.status_code == 200, response.text
    changed = response.json()
    assert changed["order"]["channel"] == "delivery"
    assert changed["order"]["delivery_address"]["reference"] == "Puerta azul"
    assert changed["order"]["notes"] == "Tocar timbre"
    assert changed["order"]["total"] == 25
    assert changed["order"]["delivery_fee_status"] == "final"
    assert changed["payment_request"]["purpose"] == "delivery"
    assert changed["payment_request"]["method"] == "unselected"
    assert changed["payment_request"]["amount"] == 5
    assert changed["recent_modification"]["source"] == "agent"
    assert change(client, headers, original).json() == changed
    assert change(client, headers, original, key="stale-key").status_code == 409
    assert change(client, headers, changed["order"], key="wrong-sender", sender="51888888888").status_code == 404
    with SessionLocal() as db:
        stored_receipt = db.get(PaymentEvidence, receipt["evidence"]["id"])
        assert stored_receipt.status == "under_review"
        assert stored_receipt.analysis["expected_amount"] == 20
        assert db.query(Payment).count() == 0
        assert db.query(OrderPaymentRequest).count() == 1
        assert db.query(AuditEvent).filter_by(action="agent.fulfillment_updated").count() == 1
    detail = client.get(f"/api/v1/orders/{order['id']}/detail", headers=auth_headers).json()
    assert detail["order"]["recent_modification"]["source"] == "agent"
    workspace = client.get("/api/v1/orders/workspace", headers=auth_headers,
        params={"branch_id": tenant["branch_id"], "period": "all"}).json()
    assert workspace["items"][0]["recent_modification"]["source"] == "agent"
    correction = change(client, headers, changed["order"], key="correct-address",
        delivery_address={**DESTINATION, "reference": "Portón negro"})
    assert correction.status_code == 200, correction.text
    assert correction.json()["payment_request"]["id"] == changed["payment_request"]["id"]
    state = client.get(f"/api/v1/integrations/orders/{order['id']}/customer-state",
        headers=headers, params={"sender": PHONE}).json()
    assert state["allowed_actions"]["change_to_delivery"] is True
    assert state["allowed_actions"]["change_to_takeaway"] is False
    unchanged = change(client, headers, correction.json()["order"], key="no-op",
        delivery_address={**DESTINATION, "reference": "Portón negro"})
    assert unchanged.status_code == 200 and unchanged.json()["changed"] is False
    with SessionLocal() as db:
        assert db.query(OrderPaymentRequest).count() == 1


def test_yape_paid_receipt_keeps_original_payment_when_delivery_fee_is_added(client, tenant, auth_headers):
    headers, order = agent_order(client, tenant, auth_headers)
    receipt = upload_receipt(client, headers, order)
    opened = client.post("/api/v1/cash/sessions/open", headers=auth_headers,
        json={"register_id": tenant["register_id"], "opening_amount": 0})
    assert opened.status_code in (200, 201), opened.text
    approved = client.post(f"/api/v1/payment-evidence/{receipt['evidence']['id']}/review",
        headers={**auth_headers, "Idempotency-Key": "approve-original"},
        json={"approve": True, "register_id": tenant["register_id"], "expected_version": receipt["order"]["version"]})
    assert approved.status_code == 200, approved.text
    changed = change(client, headers, approved.json()["order"])
    assert changed.status_code == 200, changed.text
    assert changed.json()["order"]["payment_status"] == "partial"
    assert changed.json()["payment_request"]["method"] == "unselected"
    with SessionLocal() as db:
        assert [payment.amount for payment in db.scalars(select(Payment).where(Payment.order_id == order["id"]))] == [Decimal("20.00")]
        assert db.get(PaymentEvidence, receipt["evidence"]["id"]).status == "paid"


def test_quote_change_persists_delivery_without_claiming_a_final_total(client, tenant, auth_headers):
    headers, order = agent_order(client, tenant, auth_headers, mode="quote")
    receipt = upload_receipt(client, headers, order)
    changed = change(client, headers, receipt["order"])
    assert changed.status_code == 200, changed.text
    assert changed.json()["order"]["channel"] == "delivery"
    assert changed.json()["order"]["delivery_fee_status"] == "pending_quote"
    assert changed.json()["order"]["final_total"] is None
    assert changed.json()["payment_request"] is None
    assert changed.json()["order"]["total"] == 20
    corrected = change(client, headers, changed.json()["order"], key="correct-quote-address",
        delivery_address={**DESTINATION, "reference": "Segundo piso"})
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["order"]["delivery_fee_status"] == "pending_quote"
    fee = client.patch(f"/api/v1/orders/{order['id']}/delivery-fee",
        headers={**auth_headers, "Idempotency-Key": "quoted-fee"},
        json={"expected_version": corrected.json()["order"]["version"], "amount": 5})
    assert fee.status_code == 200, fee.text
    blocked = change(client, headers, fee.json()["order"], key="quoted-address-after-fee",
        delivery_address={**DESTINATION, "reference": "Otro punto"})
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "ORDER_FULFILLMENT_LOCKED"


def test_initial_yape_receipt_allows_only_price_neutral_item_changes(client, tenant, auth_headers):
    headers, order = agent_order(client, tenant, auth_headers)
    receipt = upload_receipt(client, headers, order)
    state = client.get(f"/api/v1/integrations/orders/{order['id']}/customer-state",
        headers=headers, params={"sender": PHONE}).json()
    assert state["allowed_actions"]["editable_item_ids"] == [order["items"][0]["id"]]
    path = f"/api/v1/integrations/orders/{order['id']}/item-revisions"
    expensive = client.post(path, headers={**headers, "Idempotency-Key": "more-quantity"}, json={
        "sender": PHONE, "expected_version": receipt["order"]["version"],
        "operations": [{"type": "edit", "item_id": order["items"][0]["id"],
            "replacement": {"product_id": tenant["product_id"], "quantity": 2}}]})
    assert expensive.status_code == 409 and expensive.json()["code"] == "PAYMENT_EVIDENCE_AMOUNT_LOCKED"
    neutral = client.post(path, headers={**headers, "Idempotency-Key": "note-change"}, json={
        "sender": PHONE, "expected_version": receipt["order"]["version"],
        "operations": [{"type": "edit", "item_id": order["items"][0]["id"],
            "replacement": {"product_id": tenant["product_id"], "quantity": 1, "notes": "Sin cebolla"}}]})
    assert neutral.status_code == 200, neutral.text
    assert neutral.json()["order"]["total"] == 20
    assert neutral.json()["order"]["items"][-1]["notes"] == "Sin cebolla"
    with SessionLocal() as db:
        assert db.get(PaymentEvidence, receipt["evidence"]["id"]).analysis["expected_amount"] == 20
        assert db.query(KitchenTicket).count() == 0


def test_paid_yape_revisions_before_kitchen_completion_require_equal_total(client, tenant, auth_headers):
    headers, order = agent_order(client, tenant, auth_headers)
    receipt = upload_receipt(client, headers, order)
    assert client.post("/api/v1/cash/sessions/open", headers=auth_headers,
        json={"register_id": tenant["register_id"], "opening_amount": 0}).status_code in (200, 201)
    approved = client.post(f"/api/v1/payment-evidence/{receipt['evidence']['id']}/review",
        headers={**auth_headers, "Idempotency-Key": "paid-revision-approval"},
        json={"approve": True, "register_id": tenant["register_id"], "expected_version": receipt["order"]["version"]})
    assert approved.status_code == 200, approved.text
    current = approved.json()["order"]
    path = f"/api/v1/integrations/orders/{order['id']}/item-revisions"
    raised = client.post(path, headers={**headers, "Idempotency-Key": "paid-more-quantity"}, json={
        "sender": PHONE, "expected_version": current["version"],
        "operations": [{"type": "edit", "item_id": order["items"][0]["id"],
            "replacement": {"product_id": tenant["product_id"], "quantity": 2}}]})
    assert raised.status_code == 409 and raised.json()["code"] == "PAID_ORDER_PRICE_CHANGE_REQUIRES_STAFF"
    neutral = client.post(path, headers={**headers, "Idempotency-Key": "paid-note"}, json={
        "sender": PHONE, "expected_version": current["version"],
        "operations": [{"type": "edit", "item_id": order["items"][0]["id"],
            "replacement": {"product_id": tenant["product_id"], "quantity": 1, "notes": "Bien cocido"}}]})
    assert neutral.status_code == 200, neutral.text
    assert neutral.json()["order"]["total"] == 20


def test_paid_neutral_revision_preserves_other_line_prices_and_historical_promotions(client, tenant, auth_headers):
    with SessionLocal.begin() as db:
        first = db.get(Product, tenant["product_id"])
        second = Product(business_id=tenant["business_id"], branch_id=tenant["branch_id"],
            category_id=first.category_id, sku="SECOND", name="Otra pizza", price=20)
        db.add(second)
        db.flush()
        second_id = second.id
    promotion = client.post("/api/v1/catalog/promotions", headers=auth_headers, json={
        "branch_id": tenant["branch_id"], "name": "Oferta original", "promotion_type": "product_discount",
        "discount_type": "fixed_amount", "discount_value": 5, "target_scope": "products",
        "target_ids": [tenant["product_id"]], "service_channels": ["digital_takeaway"], "active": True,
    })
    assert promotion.status_code == 201, promotion.text
    headers, order = agent_order(client, tenant, auth_headers, method="cash", fee=0, items=[
        {"product_id": tenant["product_id"], "quantity": 1}, {"product_id": second_id, "quantity": 1}])
    assert order["total"] == 35
    confirmed = client.post(f"/api/v1/integrations/orders/{order['id']}/cash-confirm",
        headers={**headers, "Idempotency-Key": "confirm-paid-promo"})
    assert confirmed.status_code == 200, confirmed.text
    assert client.post("/api/v1/cash/sessions/open", headers=auth_headers,
        json={"register_id": tenant["register_id"], "opening_amount": 0}).status_code in (200, 201)
    paid = client.post(f"/api/v1/orders/{order['id']}/payments", headers=auth_headers,
        json={"method": "cash", "amount": 35, "register_id": tenant["register_id"]})
    assert paid.status_code in (200, 201), paid.text
    state = client.get(f"/api/v1/integrations/orders/{order['id']}/customer-state",
        headers=headers, params={"sender": PHONE}).json()
    original_promotion = deepcopy(order["items"][0]["promotion_snapshot"])
    with SessionLocal() as db:
        original_stock = db.get(InventoryItem, tenant["inventory_id"]).quantity
        ticket = db.scalar(select(KitchenTicket).where(KitchenTicket.order_id == order["id"]))
        original_ticket = deepcopy(ticket.items_snapshot)
        original_ticket_version = ticket.version
    moved = client.patch(f"/api/v1/catalog/promotions/{promotion.json()['id']}", headers=auth_headers,
        json={"expected_version": promotion.json()["version"], "target_ids": [second_id]})
    assert moved.status_code == 200, moved.text
    path = f"/api/v1/integrations/orders/{order['id']}/item-revisions"
    body = {"sender": PHONE, "expected_version": state["version"], "operations": [{"type": "edit",
        "item_id": order["items"][0]["id"], "replacement": {
            "product_id": tenant["product_id"], "quantity": 1, "notes": "Bien cocido"}}]}
    rejected = client.post(path, headers={**headers, "Idempotency-Key": "paid-promo-redistributed"}, json=body)
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "PAID_ORDER_PRICE_CHANGE_REQUIRES_STAFF"
    with SessionLocal() as db:
        stored = db.get(Order, order["id"])
        assert stored.total == Decimal("35") and stored.version == state["version"]
        assert len(stored.items) == 2
        assert stored.items[0].promotion_snapshot == original_promotion
        assert stored.items[1].promotion_discount == 0 and stored.items[1].promotion_snapshot is None
        assert db.get(InventoryItem, tenant["inventory_id"]).quantity == original_stock
        ticket = db.scalar(select(KitchenTicket).where(KitchenTicket.order_id == order["id"]))
        assert ticket.items_snapshot == original_ticket and ticket.version == original_ticket_version
        assert not db.scalar(select(AuditEvent.id).where(AuditEvent.action == "agent.items_revised"))
    restored = client.patch(f"/api/v1/catalog/promotions/{promotion.json()['id']}", headers=auth_headers,
        json={"expected_version": moved.json()["version"], "target_ids": [tenant["product_id"]]})
    assert restored.status_code == 200, restored.text
    revised = client.post(path, headers={**headers, "Idempotency-Key": "paid-promo-neutral"}, json=body)
    assert revised.status_code == 200, revised.text
    historical = next(item for item in revised.json()["order"]["items"] if item["id"] == order["items"][0]["id"])
    assert historical["status"] == "superseded"
    assert historical["promotion_discount"] == 5 and historical["promotion_snapshot"] == original_promotion
    assert revised.json()["order"]["total"] == 35


def test_completed_kitchen_only_allows_new_products_even_after_reopening(client, tenant, auth_headers):
    headers, order = agent_order(client, tenant, auth_headers, method="cash", fee=0)
    confirmed = client.post(f"/api/v1/integrations/orders/{order['id']}/cash-confirm",
        headers={**headers, "Idempotency-Key": "confirm"})
    assert confirmed.status_code == 200, confirmed.text
    with SessionLocal() as db:
        ticket = db.scalar(select(KitchenTicket).where(KitchenTicket.order_id == order["id"]))
        ticket_id, ticket_version = ticket.id, ticket.version
    completed = client.post(f"/api/v1/kitchen/commands/{ticket_id}/complete",
        headers={**auth_headers, "Idempotency-Key": "complete"},
        json={"expected_status": "queued", "expected_version": ticket_version})
    assert completed.status_code == 200, completed.text
    current = completed.json()["order"]
    assert change(client, headers, current).json()["code"] == "KITCHEN_ALREADY_COMPLETED"
    state = client.get(f"/api/v1/integrations/orders/{order['id']}/customer-state",
        headers=headers, params={"sender": PHONE}).json()
    assert state["kitchen_completed"] is True
    assert state["allowed_actions"]["change_fulfillment"] is False
    assert state["allowed_actions"]["fulfillment_change_block_reason"] == "kitchen_completed"
    assert state["allowed_actions"]["editable_item_ids"] == []
    reopened = client.post(f"/api/v1/kitchen/commands/{ticket_id}/reopen",
        headers={**auth_headers, "Idempotency-Key": "reopen"},
        json={"expected_status": "ready", "expected_version": completed.json()["command"]["version"]})
    assert reopened.status_code == 200, reopened.text
    state = client.get(f"/api/v1/integrations/orders/{order['id']}/customer-state",
        headers=headers, params={"sender": PHONE}).json()
    assert state["kitchen_completed"] is True
    assert change(client, headers, reopened.json()["order"], key="after-reopen").json()["code"] == "KITCHEN_ALREADY_COMPLETED"
    revised = client.post(f"/api/v1/integrations/orders/{order['id']}/item-revisions",
        headers={**headers, "Idempotency-Key": "revise"}, json={"sender": PHONE,
        "expected_version": reopened.json()["order"]["version"], "operations": [{"type": "edit",
        "item_id": order["items"][0]["id"], "replacement": {"product_id": tenant["product_id"], "quantity": 2}}]})
    assert revised.status_code == 409 and revised.json()["code"] == "KITCHEN_ALREADY_COMPLETED"
    assert client.post("/api/v1/cash/sessions/open", headers=auth_headers,
        json={"register_id": tenant["register_id"], "opening_amount": 0}).status_code in (200, 201)
    paid = client.post(f"/api/v1/orders/{order['id']}/payments", headers=auth_headers,
        json={"method": "cash", "amount": 20, "register_id": tenant["register_id"]})
    assert paid.status_code in (200, 201), paid.text
    latest = client.get(f"/api/v1/integrations/orders/{order['id']}/customer-state",
        headers=headers, params={"sender": PHONE}).json()
    assert latest["allowed_actions"]["add_items"] is True
    added = client.post(f"/api/v1/integrations/orders/{order['id']}/item-batches",
        headers={**headers, "Idempotency-Key": "new-products"}, json={"sender": PHONE,
        "expected_version": latest["version"], "expected_amount": 20,
        "items": [{"product_id": tenant["product_id"], "quantity": 1}]})
    assert added.status_code == 200, added.text
    assert added.json()["order"]["payment_status"] == "partial"
    with SessionLocal() as db:
        assert db.query(KitchenTicket).filter_by(order_id=order["id"]).count() == 2


def test_recent_agent_notice_expires_and_does_not_label_closed_history(client, tenant, auth_headers):
    headers, order = agent_order(client, tenant, auth_headers, method="cash")
    changed = change(client, headers, order)
    assert changed.status_code == 200, changed.text
    with SessionLocal.begin() as db:
        event = db.scalar(select(AuditEvent).where(AuditEvent.action == "agent.fulfillment_updated"))
        event.created_at = utcnow() - timedelta(days=3)
        event.payload = {"at": (utcnow() - timedelta(days=3)).isoformat()}
    detail = client.get(f"/api/v1/orders/{order['id']}/detail", headers=auth_headers).json()
    assert detail["order"]["recent_modification"] is None
    with SessionLocal.begin() as db:
        event = db.scalar(select(AuditEvent).where(AuditEvent.action == "agent.fulfillment_updated"))
        event.created_at = utcnow()
        event.payload = {"at": utcnow().isoformat()}
        db.get(Order, order["id"]).status = "delivered"
    detail = client.get(f"/api/v1/orders/{order['id']}/detail", headers=auth_headers).json()
    assert detail["order"]["recent_modification"] is None


def test_paid_cash_addition_keeps_original_agreed_discount(client, tenant, auth_headers):
    headers, order = agent_order(client, tenant, auth_headers, method="cash", fee=0)
    confirmed = client.post(f"/api/v1/integrations/orders/{order['id']}/cash-confirm",
        headers={**headers, "Idempotency-Key": "confirm-discount"})
    assert confirmed.status_code == 200, confirmed.text
    with SessionLocal.begin() as db:
        stored = db.get(Order, order["id"])
        stored.discount = Decimal("5")
        stored.promotion_discount = Decimal("5")
        stored.total = Decimal("15")
        stored.items[0].promotion_discount = Decimal("5")
        stored.items[0].promotion_snapshot = {"name": "Promoción anterior"}
    assert client.post("/api/v1/cash/sessions/open", headers=auth_headers,
        json={"register_id": tenant["register_id"], "opening_amount": 0}).status_code in (200, 201)
    paid = client.post(f"/api/v1/orders/{order['id']}/payments", headers=auth_headers,
        json={"method": "cash", "amount": 15, "register_id": tenant["register_id"]})
    assert paid.status_code in (200, 201), paid.text
    state = client.get(f"/api/v1/integrations/orders/{order['id']}/customer-state",
        headers=headers, params={"sender": PHONE}).json()
    added = client.post(f"/api/v1/integrations/orders/{order['id']}/item-batches",
        headers={**headers, "Idempotency-Key": "cash-add-discount"}, json={"sender": PHONE,
        "expected_version": state["version"], "expected_amount": 20,
        "items": [{"product_id": tenant["product_id"], "quantity": 1}]})
    assert added.status_code == 200, added.text
    assert added.json()["order"]["total"] == 35
    assert added.json()["order"]["discount"] == 5
    assert added.json()["order"]["items"][0]["promotion_snapshot"] == {"name": "Promoción anterior"}


def test_fulfillment_updates_active_kitchen_and_only_unclaimed_print_work(client, tenant, auth_headers):
    headers, order = agent_order(client, tenant, auth_headers, method="cash", fee=0)
    confirmed = client.post(f"/api/v1/integrations/orders/{order['id']}/cash-confirm",
        headers={**headers, "Idempotency-Key": "confirm-kitchen-overlay"})
    assert confirmed.status_code == 200, confirmed.text
    pending_id, claimed_id, retained_id, legacy_id = [str(uuid4()) for _ in range(4)]
    with SessionLocal.begin() as db:
        stored = db.get(Order, order["id"])
        ticket = db.scalar(select(KitchenTicket).where(KitchenTicket.order_id == order["id"]))
        ticket_id, original_version = ticket.id, ticket.version
        original_items = deepcopy(ticket.items_snapshot)
        payload = build_print_payload(db, stored, ticket,
            {"printer_name": "Test Printer", "paper_width_mm": 80, "copies": 1}, "kitchen_ticket", {})
        guard = {"order": content_digest(payload["order"], kitchen=True), "ticket_version": ticket.version}
        claimed_payload = {**deepcopy(payload), "_transport": "pos-local-v1", "_guard": deepcopy(guard)}
        db.add_all([
            PrintJob(id=pending_id, business_id=tenant["business_id"], branch_id=tenant["branch_id"],
                order_id=order["id"], kitchen_ticket_id=ticket.id, job_type="kitchen_ticket", status="pending",
                idempotency_key="pending-fulfillment-print", payload={**deepcopy(payload), "_transport": "pos-local-v1", "_guard": deepcopy(guard)}),
            PrintJob(id=claimed_id, business_id=tenant["business_id"], branch_id=tenant["branch_id"],
                order_id=order["id"], kitchen_ticket_id=ticket.id, job_type="kitchen_ticket", status="claimed",
                idempotency_key="claimed-fulfillment-print", payload=deepcopy(claimed_payload)),
            PrintJob(id=legacy_id, business_id=tenant["business_id"], branch_id=tenant["branch_id"],
                order_id=order["id"], kitchen_ticket_id=ticket.id, job_type="kitchen_ticket", status="pending",
                idempotency_key="legacy-fulfillment-print", payload={"ticket": {"context": {"channel": "takeaway"}}}),
        ])
        context = deepcopy(ticket.context_snapshot)
        context["_pos_printing"] = {"transport": "pos-local-v1", "jobs": [
            {"id": pending_id, "key": "pending-fulfillment-print", "job_type": "kitchen_ticket", "payload": deepcopy(payload), "guard": deepcopy(guard)},
            {"id": claimed_id, "key": "claimed-fulfillment-print", "job_type": "kitchen_ticket", "payload": deepcopy(payload), "guard": deepcopy(guard)},
            {"id": retained_id, "key": "retained-fulfillment-print", "job_type": "kitchen_ticket", "payload": deepcopy(payload), "guard": deepcopy(guard)},
        ]}
        ticket.context_snapshot = context
    changed = change(client, headers, confirmed.json()["order"])
    assert changed.status_code == 200, changed.text
    detail = client.get(f"/api/v1/orders/{order['id']}/detail", headers=auth_headers).json()
    assert detail["tickets"][0]["channel"] == "delivery"
    assert detail["tickets"][0]["context"]["channel"] == "delivery"
    assert detail["tickets"][0]["version"] == original_version + 1
    with SessionLocal() as db:
        ticket = db.get(KitchenTicket, ticket_id)
        assert ticket.context_snapshot["channel"] == "takeaway"
        assert ticket.context_snapshot["fulfillment_current"]["channel"] == "delivery"
        assert ticket.items_snapshot == original_items
        pending = db.get(PrintJob, pending_id)
        assert pending.status == "pending"
        assert pending.payload["ticket"]["context"]["channel"] == "delivery"
        assert pending.payload["_guard"]["ticket_version"] == ticket.version
        assert db.get(PrintJob, claimed_id).payload == claimed_payload
        assert db.get(PrintJob, claimed_id).status == "claimed"
        assert db.get(PrintJob, legacy_id).payload["ticket"]["context"]["channel"] == "delivery"
        definitions = {definition["id"]: definition for definition in ticket.context_snapshot["_pos_printing"]["jobs"]}
        assert definitions[retained_id]["payload"]["ticket"]["context"]["channel"] == "delivery"
        assert definitions[retained_id]["guard"]["ticket_version"] == ticket.version
        assert definitions[claimed_id]["payload"] == payload
        assert db.query(PrintJob).count() == 3
    stale_complete = client.post(f"/api/v1/kitchen/commands/{ticket_id}/complete",
        headers={**auth_headers, "Idempotency-Key": "stale-complete"},
        json={"expected_status": "queued", "expected_version": original_version})
    assert stale_complete.status_code == 409 and stale_complete.json()["code"] == "KITCHEN_TICKET_STALE"
