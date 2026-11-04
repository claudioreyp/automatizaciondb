"""Candidate API20: real ASGI handlers and SQLite :memory:, all external I/O denied.

These tests prove transactions sequentially; PostgreSQL lock races and actual
n8n/provider/printing remain unexecuted and require the later isolated campaign.
"""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app import api
from app.database import SessionLocal, engine
from app.models import (AgentAttentionRequest, AgentConversation, Branch, BranchSettings, IntegrationEvent,
    KitchenTicket, Order, OrderPaymentRequest, Payment, PaymentEvidence, PrintJob, Product, ServiceSchedule, utcnow)
from app.agent_original_payment import original_payment_amount, original_payment_policy
from test_agent_domain_revision import (no_external_effects, setup_order, receipt, approve, current,
    addition, preview as fulfillment_preview, change as fulfillment_change, PHONE)
from test_agent_checkout import open_cash
from test_pos_printing import enable
from test_restaurant_agent_boundary import agent_headers, leased, SENDER
from test_escalar_integrations import create_credential, integration_headers


def state(client, headers, order):
    return current(client, headers, order)


def preview(client, headers, order, method, key=None, sender=PHONE):
    return client.post(f"/api/v1/integrations/orders/{order['id']}/payment-method-preview",
        headers={**headers, "Idempotency-Key": key or str(uuid4())},
        json={"sender": sender, "expected_version": order["version"], "method": method})


def change(client, headers, order, method, key=None, prepared=None, sender=PHONE, **extra):
    pre = prepared or preview(client, headers, order, method, sender=sender)
    assert pre.status_code == 200, pre.text
    return client.patch(f"/api/v1/integrations/orders/{order['id']}/payment-method",
        headers={**headers, "Idempotency-Key": key or str(uuid4())},
        json={"sender": sender, "expected_version": order["version"], "method": method,
              "preview_fingerprint": pre.json()["preview_fingerprint"], **extra})


def counts():
    with SessionLocal() as db:
        return tuple(db.query(model).count() for model in
            (Order, Payment, PaymentEvidence, KitchenTicket, PrintJob, AgentAttentionRequest))


def configure_printing(tenant):
    enable(tenant, printer_name="SYNTHETIC-NO-DEVICE")


def test_yape_to_cash_keeps_history_and_idempotently_releases_one_initial_pair(client, tenant, auth_headers):
    headers, order = setup_order(client, tenant, auth_headers, method="yape")
    configure_printing(tenant)
    uploaded = receipt(client, headers, order)
    order = state(client, headers, order)
    with SessionLocal() as db:
        ev = db.get(PaymentEvidence, uploaded["evidence"]["id"])
        before = (ev.image_sha256, ev.storage_path, ev.reviewed_by, ev.reviewed_at)
    prepared = preview(client, headers, order, "cash")
    assert prepared.json()["effects"]["initial_kitchen_release"] is True
    changed = change(client, headers, order, "cash", key="change-cash", prepared=prepared)
    assert changed.status_code == 200, changed.text
    result = changed.json()
    assert result["changed"] and result["command_committed"] and len(result["tickets"]) == 1
    assert result["order"]["id"] == order["id"] and result["order"]["total"] == order["total"]
    assert result["order"]["payment_status"] == "pending" and result["order"]["payment_method"] == "cash"
    assert len(result["order"]["items"]) == 1 and result["superseded_evidence_ids"] == [uploaded["evidence"]["id"]]
    with SessionLocal() as db:
        ev = db.get(PaymentEvidence, uploaded["evidence"]["id"])
        assert ev.status == "superseded" and (ev.image_sha256, ev.storage_path, ev.reviewed_by, ev.reviewed_at) == before
        assert ev.analysis["original_payment_method_change"]["actor"] == "verified_customer_choice"
        assert db.query(Payment).count() == 0
        assert {row.job_type for row in db.query(PrintJob)} == {"customer_receipt", "kitchen_ticket"}
    assert change(client, headers, order, "cash", key="change-cash", prepared=prepared).json() == result
    assert counts()[:5] == (1, 0, 1, 1, 2)
    altered = change(client, headers, order, "yape", key="change-cash", prepared=prepared)
    assert altered.status_code == 409


@pytest.mark.parametrize("status", ["sent_to_kitchen", "preparing", "ready", "dispatched"])
def test_cash_to_yape_real_approval_never_reprints_or_rewinds_current_stage(client, tenant, auth_headers, status):
    headers, order = setup_order(client, tenant, auth_headers, method="yape")
    configure_printing(tenant)
    receipt(client, headers, order, key="accepted-original")
    order = change(client, headers, state(client, headers, order), "cash").json()["order"]
    with SessionLocal.begin() as db:
        db.get(Order, order["id"]).status = status
        original_tickets = [(row.id, deepcopy(row.items_snapshot)) for row in db.query(KitchenTicket)]
        original_prints = [(row.id, row.job_type, deepcopy(row.payload)) for row in db.query(PrintJob)]
    changed = change(client, headers, state(client, headers, order), "yape")
    assert changed.status_code == 200, changed.text
    assert changed.json()["tickets"] == [] and changed.json()["initial_receipt_required"]
    assert changed.json()["order"]["status"] == status
    uploaded = receipt(client, headers, order, key="new-yape-" + status)
    assert uploaded["evidence"]["expected_amount"] == 20
    open_cash(client, auth_headers, tenant)
    review_version = state(client, headers, order)["version"]
    approved = approve(client, auth_headers, tenant, uploaded)
    assert approved.status_code == 200, approved.text
    replayed = client.post(f"/api/v1/payment-evidence/{uploaded['evidence']['id']}/review",
        headers={**auth_headers, "Idempotency-Key": "review-" + str(uploaded["evidence"]["id"])},
        json={"approve": True, "register_id": tenant["register_id"], "expected_version": review_version})
    assert replayed.json() == approved.json()
    with SessionLocal() as db:
        actual = db.get(Order, order["id"])
        assert actual.status == status and actual.payment_status == "paid"
        assert [(row.id, row.items_snapshot) for row in db.query(KitchenTicket)] == original_tickets
        assert [(row.id, row.job_type, row.payload) for row in db.query(PrintJob)] == original_prints
        assert [row.amount for row in db.query(Payment)] == [Decimal("20")]
        events = list(db.scalars(select(IntegrationEvent).where(IntegrationEvent.event_type == "payment.approved")))
        assert len(events) == 1
        assert {key: events[0].payload[key] for key in ("purpose", "sent_to_kitchen", "kitchen_release", "ticket_ids",
            "payment_method", "payment_approved", "original_payment_method_change", "status")} == {
            "purpose": "initial", "sent_to_kitchen": False, "kitchen_release": "previously_released", "ticket_ids": [],
            "payment_method": "yape", "payment_approved": True, "original_payment_method_change": True, "status": status}


def test_old_receipt_and_upload_replay_stay_invalid_after_round_trip(client, tenant, auth_headers):
    headers, order = setup_order(client, tenant, auth_headers, method="yape")
    configure_printing(tenant)
    old = receipt(client, headers, order, key="old")
    changed = change(client, headers, state(client, headers, order), "cash")
    assert changed.status_code == 200, changed.text
    changed = change(client, headers, state(client, headers, order), "yape")
    assert changed.status_code == 200 and changed.json()["initial_receipt_required"]
    open_cash(client, auth_headers, tenant)
    refused = approve(client, auth_headers, tenant, old)
    assert refused.status_code == 409 and refused.json()["code"] == "PAYMENT_EVIDENCE_METHOD_CHANGED"
    endpoint = f"/api/v1/integrations/orders/{order['id']}/payment-evidence"
    payload = {"provider": "yape", "sender": PHONE, "looks_like_payment_receipt": "true", "whatsapp_message_id": "wamid-old"}
    old_file = {"file": ("isolated.png", b"\x89PNG\r\n\x1a\nold", "image/png")}
    replayed = client.post(endpoint, headers={**headers, "Idempotency-Key": "old"}, data=payload, files=old_file)
    assert replayed.status_code == 409 and replayed.json()["code"] == "PAYMENT_EVIDENCE_METHOD_CHANGED"
    duplicate = client.post(endpoint, headers={**headers, "Idempotency-Key": "new-key-old-image"}, data=payload, files=old_file)
    assert duplicate.status_code == 409
    fresh = receipt(client, headers, order, key="fresh")
    assert approve(client, auth_headers, tenant, fresh).status_code == 200
    assert counts()[:5] == (1, 1, 2, 1, 2)


@pytest.mark.parametrize("kind", ["payment", "paid_evidence"])
def test_actual_approved_rows_block_change_even_when_order_status_is_pending(client, tenant, auth_headers, kind):
    headers, order = setup_order(client, tenant, auth_headers, method="yape")
    uploaded = receipt(client, headers, order)
    with SessionLocal.begin() as db:
        if kind == "payment":
            db.add(Payment(business_id=tenant["business_id"], order_id=order["id"], method="cash", amount=Decimal("0.01")))
        else:
            db.get(PaymentEvidence, uploaded["evidence"]["id"]).status = "paid"
        db.get(Order, order["id"]).payment_status = "pending"
    order = state(client, headers, order)
    before = counts()
    prepared = preview(client, headers, order, "cash")
    assert not prepared.json()["allowed"] and prepared.json()["block_reason"] == "ORIGINAL_PAYMENT_ALREADY_APPROVED"
    for key in ["review-one", "review-two"]:
        response = change(client, headers, order, "cash", key=key, prepared=prepared)
        assert response.status_code == 200 and response.json()["requires_human_review"] and not response.json()["changed"]
        assert response.json()["order"]["payment_method"] == "yape"
    assert counts() == (*before[:5], 1)
    with SessionLocal() as db:
        assert db.get(Order, order["id"]).payment_status == "pending"


@pytest.mark.parametrize("status", ["cancelled", "closed", "delivered"])
def test_terminal_closed_order_requires_staff_and_does_not_request_payment(client, tenant, auth_headers, status):
    headers, order = setup_order(client, tenant, auth_headers, method="yape")
    with SessionLocal.begin() as db:
        db.get(Order, order["id"]).status = status
    order = state(client, headers, order)
    assert not order["payment_method_change"]["initial_receipt_required"]
    result = change(client, headers, order, "cash")
    assert result.status_code == 409 and result.json()["code"] == "ORDER_PAYMENT_CLOSED"
    with SessionLocal() as db:
        row = db.get(Order, order["id"])
        assert row.status == status and row.payment_method == "yape"
    assert counts() == (1, 0, 0, 0, 0, 0)


def test_preview_fingerprint_detects_payment_approval_without_a_version_bump(client, tenant, auth_headers):
    headers, order = setup_order(client, tenant, auth_headers, method="yape")
    receipt(client, headers, order)
    order = state(client, headers, order)
    prepared = preview(client, headers, order, "cash")
    with SessionLocal.begin() as db:
        db.add(Payment(business_id=tenant["business_id"], order_id=order["id"], method="cash", amount=1))
    response = change(client, headers, order, "cash", prepared=prepared)
    assert response.status_code == 409 and response.json()["code"] == "ORIGINAL_PAYMENT_PREVIEW_STALE"
    assert counts() == (1, 1, 1, 0, 0, 0)


def test_cash_change_releases_retained_cash_additions_only_once(client, tenant, auth_headers):
    headers, order = setup_order(client, tenant, auth_headers, method="yape")
    configure_printing(tenant)
    receipt(client, headers, order)
    first = addition(client, headers, state(client, headers, order), "cash", key="held-cash")
    assert first.status_code == 200, first.text
    assert first.json()["payment_request"]["status"] == "awaiting_base_review"
    second = addition(client, headers, state(client, headers, order), "yape", key="held-yape")
    assert second.status_code == 200
    assert counts()[:5] == (1, 0, 1, 0, 0)
    order = state(client, headers, order)
    prepared = preview(client, headers, order, "cash")
    assert prepared.json()["initial_payment_amount"] == 20
    response = change(client, headers, order, "cash", key="release-held", prepared=prepared)
    assert response.status_code == 200, response.text
    assert len(response.json()["tickets"]) == 2 and len(response.json()["appended_item_ids"]) == 1
    assert response.json()["order"]["total"] == 40
    assert change(client, headers, order, "cash", key="release-held", prepared=prepared).json() == response.json()
    with SessionLocal() as db:
        assert db.get(OrderPaymentRequest, first.json()["payment_request"]["id"]).status == "committed"
        assert db.get(OrderPaymentRequest, second.json()["payment_request"]["id"]).status == "pending"
        assert [row.job_type for row in db.query(PrintJob)].count("customer_receipt") == 1
        assert db.query(PrintJob).count() == 3 and db.query(Payment).count() == 0
        assert original_payment_amount(db, db.get(Order, order["id"])) == Decimal("20")


def test_original_yape_excludes_committed_cash_and_held_yape_additions(client, tenant, auth_headers):
    headers, order = setup_order(client, tenant, auth_headers, method="cash")
    added = addition(client, headers, state(client, headers, order), "cash", key="cash-added")
    assert added.status_code == 200
    held = addition(client, headers, state(client, headers, order), "yape", key="yape-held")
    assert held.status_code == 200
    order = state(client, headers, order)
    assert order["total"] == 40 and order["initial_payment_amount"] == 20
    changed = change(client, headers, order, "yape")
    assert changed.status_code == 200 and changed.json()["initial_payment_amount"] == 20
    uploaded = receipt(client, headers, order, key="only-original")
    assert uploaded["evidence"]["expected_amount"] == 20
    open_cash(client, auth_headers, tenant)
    result = approve(client, auth_headers, tenant, uploaded)
    assert result.status_code == 200, result.text
    with SessionLocal() as db:
        actual = db.get(Order, order["id"])
        assert actual.total == 40 and actual.payment_status == "partial"
        assert [row.amount for row in db.query(Payment)] == [Decimal("20")]
        assert db.get(OrderPaymentRequest, held.json()["payment_request"]["id"]).status == "pending"
        assert db.query(KitchenTicket).count() == 2


@pytest.mark.parametrize("direction", ["delivery_to_pickup", "pickup_to_delivery"])
def test_original_amount_uses_current_fulfillment_and_separate_delivery_debt(client, tenant, auth_headers, direction):
    with SessionLocal.begin() as db:
        db.get(Product, tenant["product_id"]).price = Decimal("3")
    headers, order = setup_order(client, tenant, auth_headers, method="cash",
        channel="delivery" if direction == "delivery_to_pickup" else "takeaway", fee=5)
    assert order["total"] == (8 if direction == "delivery_to_pickup" else 3)
    body = fulfillment_preview(client, headers, state(client, headers, order),
        channel="takeaway" if direction == "delivery_to_pickup" else "delivery")
    fulfilled = fulfillment_change(client, headers, order, body)
    assert fulfilled.status_code == 200, fulfilled.text
    order = state(client, headers, order)
    expected_total = 3 if direction == "delivery_to_pickup" else 8
    assert order["total"] == expected_total and order["initial_payment_amount"] == 3
    change_result = change(client, headers, order, "yape")
    assert change_result.status_code == 200 and change_result.json()["initial_payment_amount"] == 3
    uploaded = receipt(client, headers, order, key=direction)
    assert uploaded["evidence"]["expected_amount"] == 3
    with SessionLocal() as db:
        requests = list(db.query(OrderPaymentRequest))
        assert len(requests) == (0 if direction == "delivery_to_pickup" else 1)
        if requests:
            assert requests[0].purpose == "delivery" and requests[0].amount == 5 and requests[0].status == "pending"


def test_allocation_preserves_historical_discounts_cents_and_never_uses_new_catalog_prices(client, tenant, auth_headers):
    headers, order = setup_order(client, tenant, auth_headers, method="cash")
    added = addition(client, headers, state(client, headers, order), "cash")
    assert added.status_code == 200
    with SessionLocal.begin() as db:
        actual = db.get(Order, order["id"])
        base, extra = sorted(actual.items, key=lambda row: row.id)
        base.line_total, base.promotion_discount = Decimal("20.01"), Decimal("1.11")
        extra.line_total, extra.promotion_discount = Decimal("20.02"), Decimal("2.22")
        actual.subtotal, actual.promotion_discount = Decimal("40.03"), Decimal("3.33")
        actual.manual_discount, actual.discount, actual.total = Decimal("4.44"), Decimal("6.66"), Decimal("33.37")
        db.get(Product, tenant["product_id"]).price = 999
        before = (actual.subtotal, actual.discount, actual.manual_discount, actual.promotion_discount,
            base.line_total, base.promotion_discount, extra.line_total, extra.promotion_discount)
        assert original_payment_amount(db, actual) == Decimal("15.57")
    result = change(client, headers, state(client, headers, order), "yape")
    assert result.status_code == 200 and result.json()["initial_payment_amount"] == 15.57
    with SessionLocal() as db:
        actual = db.get(Order, order["id"])
        base, extra = sorted(actual.items, key=lambda row: row.id)
        assert (actual.subtotal, actual.discount, actual.manual_discount, actual.promotion_discount,
            base.line_total, base.promotion_discount, extra.line_total, extra.promotion_discount) == before


def test_malformed_committed_addition_allocation_requires_review_instead_of_guessing(client, tenant, auth_headers):
    headers, order = setup_order(client, tenant, auth_headers, method="cash")
    with SessionLocal.begin() as db:
        db.add(OrderPaymentRequest(business_id=tenant["business_id"], branch_id=tenant["branch_id"], order_id=order["id"],
            purpose="addition", method="cash", amount=5, status="committed", snapshot={"pricing": {"amount": "5"}}))
    prepared = preview(client, headers, state(client, headers, order), "yape")
    assert prepared.json()["initial_payment_amount"] is None and not prepared.json()["allowed"]
    result = change(client, headers, state(client, headers, order), "yape", prepared=prepared)
    assert result.json()["requires_human_review"] and result.json()["block_reason"] == "ORIGINAL_PAYMENT_ALLOCATION_UNVERIFIABLE"
    assert counts()[:2] == (1, 0)


def test_version_fingerprint_disabled_method_and_untrusted_price_are_rejected(client, tenant, auth_headers):
    headers, order = setup_order(client, tenant, auth_headers, method="cash")
    prepared = preview(client, headers, order, "yape")
    with SessionLocal.begin() as db:
        settings = db.scalar(select(BranchSettings))
        settings.payment_methods = {"takeaway": ["cash"]}
        settings.version += 1
    stale = change(client, headers, order, "yape", prepared=prepared)
    assert stale.status_code == 409
    prepared = preview(client, headers, order, "yape")
    assert prepared.json()["block_reason"] == "PAYMENT_METHOD_DISABLED"
    forbidden = change(client, headers, order, "yape", prepared=prepared, total=1)
    assert forbidden.status_code == 422
    refused = change(client, headers, order, "yape", prepared=prepared)
    assert refused.json()["requires_human_review"] and refused.json()["order"]["payment_method"] == "cash"
    wrong_sender = preview(client, headers, order, "yape", sender="51900000000")
    assert wrong_sender.status_code == 404  # Do not disclose another contact's order.


def test_limited_profile_requires_same_tenant_sender_and_current_lease(client, tenant, auth_headers, agent_headers):
    # Construct the order through the actual limited credential and profile.
    with SessionLocal.begin() as db:
        db.add(BranchSettings(business_id=tenant["business_id"], branch_id=tenant["branch_id"],
            payment_methods={key: ["cash", "yape"] for key in ("takeaway", "counter", "delivery")}))
    lease, headers = leased(client, agent_headers)
    response = client.post("/api/v1/integrations/orders/draft", headers={**headers, "Idempotency-Key": "profile-draft"},
        json={"branch_id": tenant["branch_id"], "channel": "takeaway", "payment_method": "yape", "quoted_total": 20,
            "customer_phone": SENDER, "whatsapp_chat_id": SENDER, "items": [{"product_id": tenant["product_id"], "quantity": 1}]})
    assert response.status_code == 201, response.text
    order = response.json()
    captured = client.post(f"/api/v1/integrations/orders/{order['id']}/payment-evidence",
        headers={**headers, "Idempotency-Key": "profile-receipt"},
        data={"provider": "yape", "sender": SENDER, "looks_like_payment_receipt": "true", "whatsapp_message_id": "profile-image"},
        files={"file": ("receipt.png", b"\x89PNG\r\n\x1a\nprofile-receipt", "image/png")})
    assert captured.status_code == 201, captured.text
    order = captured.json()["order"]
    allowed = preview(client, headers, order, "cash", sender=SENDER)
    assert allowed.status_code == 200, allowed.text
    wrong = preview(client, headers, order, "cash", sender="51900000000")
    assert wrong.status_code == 404
    missing = preview(client, agent_headers, order, "cash", sender=SENDER)
    assert missing.status_code == 409
    with SessionLocal.begin() as db:
        conversation = db.get(AgentConversation, lease["conversation_id"])
        conversation.lease_until = utcnow() - timedelta(seconds=1)
    expired = change(client, headers, order, "cash", prepared=allowed, sender=SENDER)
    assert expired.status_code == 409
    assert counts() == (1, 0, 1, 0, 0, 0)


def test_same_approved_method_is_noop_with_actual_paid_state_and_no_attention(client, tenant, auth_headers):
    headers, order = setup_order(client, tenant, auth_headers, method="cash")
    with SessionLocal.begin() as db:
        db.add(Payment(business_id=tenant["business_id"], order_id=order["id"], method="cash", amount=20))
        db.get(Order, order["id"]).payment_status = "paid"
    order = state(client, headers, order)
    before = counts()
    result = change(client, headers, order, "cash")
    assert result.status_code == 200 and not result.json()["changed"] and not result.json()["requires_human_review"]
    assert result.json()["order"]["payment_status"] == "paid" and not result.json()["initial_receipt_required"]
    assert counts() == before


@pytest.mark.parametrize("other", ["business", "branch"])
def test_tenant_and_branch_credentials_cannot_inspect_or_change_original_method(client, tenant, auth_headers, other):
    headers, order = setup_order(client, tenant, auth_headers, method="cash")
    if other == "business":
        other_tenant = {**tenant, "business_id": tenant["other_business_id"], "branch_id": tenant["other_branch_id"]}
    else:
        with SessionLocal.begin() as db:
            branch = Branch(business_id=tenant["business_id"], slug="second", name="Other branch")
            db.add(branch)
            db.flush()
            other_tenant = {**tenant, "branch_id": branch.id}
    other_auth = {**auth_headers, "X-Business-Id": str(other_tenant["business_id"]), "X-Branch-Id": str(other_tenant["branch_id"])}
    wrong = integration_headers(create_credential(client, other_tenant, other_auth)["token"])
    refused = preview(client, wrong, order, "yape")
    assert refused.status_code == 403  # Existing credential tenant/branch boundary.
    prepared = preview(client, headers, order, "yape")
    refused = change(client, wrong, order, "yape", prepared=prepared)
    assert refused.status_code == 403
    assert counts()[:2] == (1, 0)


def test_approved_additional_yape_payment_also_blocks_original_method_change(client, tenant, auth_headers):
    headers, order = setup_order(client, tenant, auth_headers, method="cash")
    held = addition(client, headers, state(client, headers, order), "yape")
    assert held.status_code == 200
    uploaded = receipt(client, headers, order, key="addition-proof", request_id=held.json()["payment_request"]["id"])
    open_cash(client, auth_headers, tenant)
    assert approve(client, auth_headers, tenant, uploaded).status_code == 200
    order = state(client, headers, order)
    before = counts()
    result = change(client, headers, order, "yape")
    assert result.status_code == 200 and result.json()["requires_human_review"]
    assert result.json()["order"]["payment_method"] == "cash"
    assert result.json()["block_reason"] == "ORIGINAL_PAYMENT_ALREADY_APPROVED"
    assert counts() == (*before[:5], before[5] + 1)


@pytest.mark.parametrize("status", ["ready", "dispatched"])
def test_rejecting_later_original_receipt_preserves_previously_released_order(client, tenant, auth_headers, status):
    headers, order = setup_order(client, tenant, auth_headers, method="yape")
    configure_printing(tenant)
    receipt(client, headers, order, key="original-accepted")
    assert change(client, headers, state(client, headers, order), "cash").status_code == 200
    with SessionLocal.begin() as db:
        db.get(Order, order["id"]).status = status
        tickets = [(row.id, deepcopy(row.items_snapshot)) for row in db.query(KitchenTicket)]
        prints = [(row.id, deepcopy(row.payload)) for row in db.query(PrintJob)]
    assert change(client, headers, state(client, headers, order), "yape").status_code == 200
    uploaded = receipt(client, headers, order, key="reject-" + status)
    version = state(client, headers, order)["version"]
    response = client.post(f"/api/v1/payment-evidence/{uploaded['evidence']['id']}/review",
        headers={**auth_headers, "Idempotency-Key": "negative-review"},
        json={"approve": False, "expected_version": version, "note": "No coincide el comprobante"})
    assert response.status_code == 200, response.text
    assert response.json()["order"]["status"] == status and response.json()["order"]["payment_status"] == "rejected"
    assert response.json()["evidence"]["status"] == "rejected"
    with SessionLocal() as db:
        assert db.query(Payment).count() == 0
        assert [(row.id, row.items_snapshot) for row in db.query(KitchenTicket)] == tickets
        assert [(row.id, row.payload) for row in db.query(PrintJob)] == prints
    replacement = receipt(client, headers, order, key="replacement-" + status)
    assert replacement["evidence"]["status"] == "under_review" and replacement["evidence"]["expected_amount"] == 20
    switched = change(client, headers, state(client, headers, order), "cash")
    assert switched.status_code == 200 and switched.json()["order"]["status"] == status and switched.json()["tickets"] == []
    assert counts()[:5] == (1, 0, 3, 1, 2)


def test_staff_approval_of_receipt_already_accepted_before_delivery_keeps_delivery_final(client, tenant, auth_headers):
    headers, order = setup_order(client, tenant, auth_headers, method="yape")
    configure_printing(tenant)
    receipt(client, headers, order, key="original-accepted")
    assert change(client, headers, state(client, headers, order), "cash").status_code == 200
    assert change(client, headers, state(client, headers, order), "yape").status_code == 200
    uploaded = receipt(client, headers, order, key="accepted-before-delivery")
    with SessionLocal.begin() as db:
        db.get(Order, order["id"]).status = "delivered"
        tickets = [(row.id, deepcopy(row.items_snapshot)) for row in db.query(KitchenTicket)]
        prints = [(row.id, deepcopy(row.payload)) for row in db.query(PrintJob)]
    before = state(client, headers, order)
    assert not before["payment_method_change"]["allowed"] and not before["payment_method_change"]["initial_receipt_required"]
    open_cash(client, auth_headers, tenant)
    response = approve(client, auth_headers, tenant, uploaded)
    assert response.status_code == 200 and response.json()["order"]["status"] == "delivered"
    with SessionLocal() as db:
        assert [row.amount for row in db.query(Payment)] == [Decimal("20")]
        assert [(row.id, row.items_snapshot) for row in db.query(KitchenTicket)] == tickets
        assert [(row.id, row.payload) for row in db.query(PrintJob)] == prints


@pytest.mark.parametrize("status", ["draft", "pending_confirmation"])
def test_original_method_route_cannot_accept_an_unregistered_draft_after_closing(client, tenant, auth_headers, status):
    headers, order = setup_order(client, tenant, auth_headers, method="yape")
    with SessionLocal.begin() as db:
        db.get(Order, order["id"]).status = status
        db.add(ServiceSchedule(business_id=tenant["business_id"], branch_id=tenant["branch_id"], kind="primary", name="Cerrado sin turnos", active=True))
    order = state(client, headers, order)
    assert order["payment_method_change"]["block_reason"] == "ORDER_NOT_REGISTERED"
    assert not order["payment_method_change"]["initial_receipt_required"]
    before = counts()
    response = preview(client, headers, order, "cash")
    assert response.status_code == 409 and response.json()["code"] == "ORDER_NOT_REGISTERED"
    response = client.patch(f"/api/v1/integrations/orders/{order['id']}/payment-method",
        headers={**headers, "Idempotency-Key": "no-draft-bypass"}, json={"sender": PHONE, "expected_version": order["version"],
            "method": "cash", "preview_fingerprint": "0" * 64})
    assert response.status_code == 409 and response.json()["code"] == "ORDER_NOT_REGISTERED"
    assert counts() == before


def test_accepted_original_yape_can_change_after_closing_without_accepting_new_products(client, tenant, auth_headers):
    headers, order = setup_order(client, tenant, auth_headers, method="yape")
    receipt(client, headers, order)
    with SessionLocal.begin() as db:
        db.add(ServiceSchedule(business_id=tenant["business_id"], branch_id=tenant["branch_id"], kind="primary", name="Cerrado sin turnos", active=True))
    order = state(client, headers, order)
    result = change(client, headers, order, "cash")
    assert result.status_code == 200 and result.json()["changed"]
    assert len(result.json()["order"]["items"]) == 1 and len(result.json()["tickets"]) == 1
    assert counts()[:4] == (1, 0, 1, 1)


@pytest.mark.parametrize("previous_receipt", [False, True])
def test_delivered_rejects_new_upload_but_preserves_exact_accepted_upload_replay(client, tenant, auth_headers, previous_receipt):
    headers, order = setup_order(client, tenant, auth_headers, method="cash")
    assert change(client, headers, state(client, headers, order), "yape").status_code == 200
    earlier = receipt(client, headers, order, key="before-delivery") if previous_receipt else None
    with SessionLocal.begin() as db:
        db.get(Order, order["id"]).status = "delivered"
    before = counts()
    if previous_receipt:
        assert receipt(client, headers, order, key="before-delivery") == earlier
        assert counts() == before
    endpoint = f"/api/v1/integrations/orders/{order['id']}/payment-evidence"
    response = client.post(endpoint, headers={**headers, "Idempotency-Key": "after-delivery"},
        data={"provider": "yape", "sender": PHONE, "looks_like_payment_receipt": "true", "whatsapp_message_id": "wamid-after-delivery"},
        files={"file": ("new.png", b"\x89PNG\r\n\x1a\nafter-delivery", "image/png")})
    assert response.status_code == 409 and response.json()["code"] == "ORDER_ITEMS_LOCKED"
    assert counts() == before


def test_dispatched_without_real_kitchen_never_requests_initial_receipt(client, tenant, auth_headers):
    headers, order = setup_order(client, tenant, auth_headers, method="yape")
    with SessionLocal.begin() as db:
        db.get(Order, order["id"]).status = "dispatched"
    current = state(client, headers, order)
    assert current["payment_method_change"]["block_reason"] == "KITCHEN_HISTORY_UNVERIFIABLE"
    assert not current["payment_method_change"]["initial_receipt_required"]


def test_dispatched_without_kitchen_rejects_new_initial_receipt_before_storage(client, tenant, auth_headers, monkeypatch):
    headers, order = setup_order(client, tenant, auth_headers, method="yape")
    with SessionLocal.begin() as db:
        db.get(Order, order["id"]).status = "dispatched"
    async def forbidden(*args, **kwargs):
        raise AssertionError("Inconsistent dispatched order must not analyze or store a new receipt")
    monkeypatch.setattr(api, "analyze_payment_image", forbidden)
    monkeypatch.setattr(api, "store_private_file", forbidden)
    before = counts()
    response = client.post(f"/api/v1/integrations/orders/{order['id']}/payment-evidence",
        headers={**headers, "Idempotency-Key": "inconsistent-dispatch"},
        data={"provider": "yape", "sender": PHONE, "looks_like_payment_receipt": "true", "whatsapp_message_id": "inconsistent-dispatch"},
        files={"file": ("new.png", b"\x89PNG\r\n\x1a\ninconsistent", "image/png")})
    assert response.status_code == 409 and response.json()["code"] == "KITCHEN_HISTORY_UNVERIFIABLE"
    assert counts() == before


def test_offline_environment_is_memory_only():
    assert str(engine.url) == "sqlite+pysqlite:///:memory:"
