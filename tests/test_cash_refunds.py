from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database import SessionLocal
from app.models import AuditEvent, CashMovement, CashRegister, CashSession, IdempotencyRecord, IntegrationEvent, InventoryItem, KitchenTicket, Order, Payment, RestaurantTable
from test_cash_cuts import add_movement, add_payment, confirm_and_send, create_order, preview


def cancellation(client, order_id, headers):
    response = client.get(f"/api/v1/orders/{order_id}/cancellation-preview", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def cancel_body(current, register_id=None, refunds=None):
    body = {"reason": "El cliente canceló su pedido", "expected_version": current["order_version"], "refunds": refunds or []}
    if refunds:
        register = next(row for row in current["registers"] if row["id"] == register_id)
        body.update(register_id=register_id, expected_session_id=register["session_id"], expected_cash_version=register["session_version"], refund_confirmed=True)
    return body


def cancel(client, order_id, headers, body, key="refund-cancel"):
    return client.post(f"/api/v1/orders/{order_id}/cancel", json=body, headers={**headers, "Idempotency-Key": key})


def cut(client, register_id, headers, *, cash=0, card=None, retained=0, key="refund-cut", **overrides):
    current = preview(client, register_id, headers)
    body = {"cash_counted": cash, "card_counted": card, "retained_fund": retained, "expected_version": current["version"], "expected_session_id": current["session_id"], **overrides}
    return client.post(f"/api/v1/cash/registers/{register_id}/cuts", json=body, headers={**headers, "Idempotency-Key": key})


def paid_order(client, tenant, headers, *, method="cash", amount=20, quantity=1, channel="counter"):
    order = create_order(client, tenant, headers, key="refund-order", quantity=quantity, channel=channel)
    sent = confirm_and_send(client, order, headers, key="refund-send")["order"]
    add_payment(client, order["id"], headers, key="refund-payment", method=method, amount=amount, register_id=tenant["register_id"])
    return sent


def test_partial_refund_keeps_original_payments_total_and_cancels_kitchen_table(client, tenant, auth_headers):
    order = paid_order(client, tenant, auth_headers, method="yape", amount=7, channel="dine_in")
    before = cancellation(client, order["id"], auth_headers)
    body = cancel_body(before, tenant["register_id"], [{"method": "card", "amount": 7}])
    response = cancel(client, order["id"], auth_headers, body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["financial_summary"] == {"collected": 7, "refunded": 7, "net_collected": 0, "refundable": 0, "status": "refunded"}
    assert result["order"]["total"] == 20
    assert result["order"]["payment_status"] == "partial"
    assert result["refunds"][0]["method"] == "card"
    assert result["refunds"][0]["signed_amount"] == -7
    repeated = cancel(client, order["id"], auth_headers, body)
    assert repeated.json() == result
    with SessionLocal() as db:
        payment = db.scalar(select(Payment).where(Payment.order_id == order["id"]))
        assert payment.amount == Decimal("7.00") and payment.status == "confirmed"
        assert db.get(RestaurantTable, tenant["table_id"]).status == "available"
        assert db.scalar(select(KitchenTicket).where(KitchenTicket.order_id == order["id"])).status == "cancelled"
        assert db.get(InventoryItem, tenant["inventory_id"]).quantity == Decimal("10")
        assert db.query(CashMovement).filter_by(order_id=order["id"]).count() == 1
        assert db.query(AuditEvent).filter_by(action="order.cancelled", entity_id=str(order["id"])).count() == 1
        assert db.query(IntegrationEvent).filter_by(aggregate_id=str(order["id"]), event_type="order.cancelled").count() == 1
    detail = client.get(f"/api/v1/orders/{order['id']}/detail", headers=auth_headers).json()
    assert detail["payment_summary"] == {"paid": 7, "remaining": 13}
    assert detail["financial_summary"]["status"] == "refunded"
    workspace = client.get("/api/v1/orders/workspace", params={"branch_id": tenant["branch_id"], "period": "all"}, headers=auth_headers)
    assert workspace.status_code == 200, workspace.text
    assert workspace.json()["items"][0]["financial_summary"]["status"] == "refunded"


def test_refund_only_current_card_period_requires_count_and_accepts_negative_expected(client, tenant, auth_headers):
    order = paid_order(client, tenant, auth_headers, method="card", amount=234, quantity=12)
    original = cut(client, tenant["register_id"], auth_headers, card=234, ignore_pending_orders=True, key="before-refund")
    assert original.status_code == 201, original.text
    original_value = original.json()
    current = cancellation(client, order["id"], auth_headers)
    body = cancel_body(current, tenant["register_id"], [{"method": "card", "amount": 234}])
    assert cancel(client, order["id"], auth_headers, body).status_code == 200
    current_preview = preview(client, tenant["register_id"], auth_headers)
    assert current_preview["has_cash_activity"] is False
    assert current_preview["has_card_activity"] is True
    assert "card_expected" not in current_preview and "cash_expected" not in current_preview
    missing = cut(client, tenant["register_id"], auth_headers, key="missing-card")
    assert missing.status_code == 422 and missing.json()["code"] == "CASH_CUT_CARD_COUNT_REQUIRED"
    inactive_cash = cut(client, tenant["register_id"], auth_headers, cash=1, card=45, key="inactive-cash")
    assert inactive_cash.status_code == 422 and inactive_cash.json()["code"] == "CASH_CUT_CASH_NOT_APPLICABLE"
    response = cut(client, tenant["register_id"], auth_headers, card=45)
    assert response.status_code == 201, response.text
    groups = {row["key"]: row for row in response.json()["methods"]}
    assert groups["card"]["expected"] == -234
    assert groups["card"]["difference"] == 279
    assert groups["card"]["transactions"][0]["kind"] == "refund"
    assert groups["card"]["transactions"][0]["order_folio"] == order["folio"]
    assert response.json()["reconciliation_status"] == "surplus"
    history = client.get(f"/api/v1/cash/cuts/{original_value['id']}", headers=auth_headers).json()
    assert history["total_expected_amount"] == original_value["total_expected_amount"]
    assert history["methods"] == original_value["methods"]


def test_mixed_refund_methods_differ_from_original_and_register_is_explicit(client, tenant, auth_headers):
    order = paid_order(client, tenant, auth_headers, method="card")
    with SessionLocal.begin() as db:
        target = CashRegister(business_id=tenant["business_id"], branch_id=tenant["branch_id"], name="Caja de reembolsos")
        db.add(target)
        db.flush()
        target_id = target.id
    current = cancellation(client, order["id"], auth_headers)
    assert len(current["registers"]) == 2
    target_preview = next(row for row in current["registers"] if row["id"] == target_id)
    assert target_preview["session_id"] is None and target_preview["session_version"] == 0
    with SessionLocal() as db:
        assert db.query(CashSession).filter_by(register_id=target_id).count() == 0
    response = cancel(client, order["id"], auth_headers, cancel_body(current, target_id, [
        {"method": "cash", "amount": 7}, {"method": "card", "amount": 8}, {"method": "yape", "amount": 5}]))
    assert response.status_code == 200, response.text
    assert {item["register_id"] for item in response.json()["refunds"]} == {target_id}
    result = cut(client, target_id, auth_headers, cash=0, card=0)
    assert result.status_code == 201, result.text
    groups = {item["key"]: item for item in result.json()["methods"]}
    assert [groups[key]["expected"] for key in ("cash", "card", "transfer")] == [-7, -8, -5]
    assert preview(client, tenant["register_id"], auth_headers)["has_card_activity"] is True


@pytest.mark.parametrize("changes,code", [
    ({"refunds": [{"method": "cash", "amount": 19}]}, "ORDER_REFUND_AMOUNT_MISMATCH"),
    ({"refund_confirmed": False}, "ORDER_REFUND_CONFIRMATION_REQUIRED"),
    ({"register_id": None}, "ORDER_REFUND_CASH_PERIOD_REQUIRED"),
    ({"expected_cash_version": None}, "ORDER_REFUND_CASH_PERIOD_REQUIRED"),
    ({"expected_cash_version": 999}, "CASH_CUT_STALE"),
    ({"expected_session_id": None}, "CASH_CUT_STALE"),
])
def test_cancel_rejects_incomplete_or_stale_refund_atomically(client, tenant, auth_headers, changes, code):
    order = paid_order(client, tenant, auth_headers)
    current = cancellation(client, order["id"], auth_headers)
    body = {**cancel_body(current, tenant["register_id"], [{"method": "cash", "amount": 20}]), **changes}
    response = cancel(client, order["id"], auth_headers, body)
    assert response.status_code in {409, 422}, response.text
    assert response.json()["code"] == code
    with SessionLocal() as db:
        assert db.get(Order, order["id"]).status != "cancelled"
        assert db.query(CashMovement).filter_by(order_id=order["id"]).count() == 0
        assert db.scalar(select(KitchenTicket).where(KitchenTicket.order_id == order["id"])).status == "queued"
        assert db.get(InventoryItem, tenant["inventory_id"]).quantity == Decimal("9.5")


def test_cancellation_requires_key_reason_exact_methods_and_cash_period_presence(client, tenant, auth_headers):
    order = paid_order(client, tenant, auth_headers)
    current = cancellation(client, order["id"], auth_headers)
    body = cancel_body(current, tenant["register_id"], [{"method": "cash", "amount": 20}])
    for change in ({"reason": "   "}, {"refunds": [{"method": "cash", "amount": 10}, {"method": "cash", "amount": 10}]}, {"refunds": [{"method": "cash", "amount": 20.001}]}):
        assert cancel(client, order["id"], auth_headers, {**body, **change}).status_code == 422
    without_period = {key: value for key, value in body.items() if key != "expected_session_id"}
    response = cancel(client, order["id"], auth_headers, without_period)
    assert response.status_code == 422 and response.json()["code"] == "ORDER_REFUND_CASH_PERIOD_REQUIRED"
    assert client.post(f"/api/v1/orders/{order['id']}/cancel", json=body, headers=auth_headers).status_code == 422


def test_unpaid_cancel_is_voided_and_never_opens_cash_or_fabricates_refund(client, tenant, auth_headers):
    order = create_order(client, tenant, auth_headers, key="unpaid-cancel")
    sent = confirm_and_send(client, order, auth_headers, key="unpaid-send")["order"]
    waiter = {**auth_headers, "X-Dev-Role": "waiter"}
    current = cancellation(client, order["id"], waiter)
    assert current["can_cancel"] is True and current["can_refund"] is False
    result = cancel(client, order["id"], waiter, cancel_body(current)).json()
    assert result["financial_summary"]["status"] == "voided"
    assert result["order"]["total"] == sent["total"]
    assert result["refunds"] == [] and result["cash_period"] is None
    with SessionLocal() as db:
        assert db.query(CashSession).count() == 0 and db.query(CashMovement).count() == 0


def test_paid_cancellation_permissions_scope_and_legacy_transition_are_preserved(client, tenant, auth_headers):
    order = paid_order(client, tenant, auth_headers)
    current = cancellation(client, order["id"], auth_headers)
    body = cancel_body(current, tenant["register_id"], [{"method": "cash", "amount": 20}])
    waiter = {**auth_headers, "X-Dev-Role": "waiter"}
    assert cancellation(client, order["id"], waiter)["can_cancel"] is False
    assert cancel(client, order["id"], waiter, body).status_code == 403
    other = {**auth_headers, "X-Business-Id": str(tenant["other_business_id"]), "X-Branch-Id": str(tenant["other_branch_id"])}
    assert client.get(f"/api/v1/orders/{order['id']}/cancellation-preview", headers=other).status_code == 404
    assert cancel(client, order["id"], other, body).status_code == 404
    response = client.post(f"/api/v1/orders/{order['id']}/transition", json={"status": "cancelled", "expected_version": current["order_version"]}, headers=auth_headers)
    assert response.status_code == 200, response.text
    detail = client.get(f"/api/v1/orders/{order['id']}/detail", headers=auth_headers).json()
    assert detail["refunds"] == [] and detail["financial_summary"]["status"] == "refund_not_recorded"


def test_body_bound_cancellation_replay_and_storage_unique_guard(client, tenant, auth_headers):
    order = paid_order(client, tenant, auth_headers)
    body = cancel_body(cancellation(client, order["id"], auth_headers), tenant["register_id"], [{"method": "cash", "amount": 20}])
    first = cancel(client, order["id"], auth_headers, body)
    assert first.status_code == 200, first.text
    conflict = cancel(client, order["id"], auth_headers, {**body, "reason": "Otro motivo"})
    assert conflict.status_code == 409 and conflict.json()["code"] == "CASH_IDEMPOTENCY_CONFLICT"
    assert cancel(client, order["id"], auth_headers, body, key="another-cancel").status_code == 409
    with SessionLocal() as db:
        record = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.scope == f"order-cancel-refund:{order['id']}"))
        assert "request_digest" in record.response_body
        movement = db.scalar(select(CashMovement).where(CashMovement.order_id == order["id"]))
        db.add(CashMovement(cash_session_id=movement.cash_session_id, order_id=order["id"], movement_type="refund", payment_method="cash", amount=Decimal("20")))
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()


def test_refund_register_cannot_cross_branch(client, tenant, auth_headers):
    order = paid_order(client, tenant, auth_headers)
    with SessionLocal.begin() as db:
        other = CashRegister(business_id=tenant["other_business_id"], branch_id=tenant["other_branch_id"], name="Caja ajena")
        db.add(other)
        db.flush()
        other_id = other.id
    body = cancel_body(cancellation(client, order["id"], auth_headers), tenant["register_id"], [{"method": "cash", "amount": 20}])
    response = cancel(client, order["id"], auth_headers, {**body, "register_id": other_id})
    assert response.status_code == 404


def test_card_sale_refund_net_zero_still_requires_explicit_count(client, tenant, auth_headers):
    order = paid_order(client, tenant, auth_headers, method="card")
    body = cancel_body(cancellation(client, order["id"], auth_headers), tenant["register_id"], [{"method": "card", "amount": 20}])
    assert cancel(client, order["id"], auth_headers, body).status_code == 200
    current = preview(client, tenant["register_id"], auth_headers)
    assert current["has_card_activity"] is True and current["has_cash_activity"] is False
    assert cut(client, tenant["register_id"], auth_headers).status_code == 422
    result = cut(client, tenant["register_id"], auth_headers, card=0)
    assert result.status_code == 201 and result.json()["reconciliation_status"] == "balanced"
    assert len(next(row for row in result.json()["methods"] if row["key"] == "card")["transactions"]) == 2


@pytest.mark.parametrize("kind,expected", [("income", 10), ("withdrawal", -10), ("expense", -10)])
def test_manual_movements_without_sales_enable_cash_count(client, tenant, auth_headers, kind, expected):
    if kind == "expense":
        with SessionLocal.begin() as db:
            session = CashSession(business_id=tenant["business_id"], branch_id=tenant["branch_id"], register_id=tenant["register_id"], opened_by="owner-test", opening_amount=0)
            db.add(session)
            db.flush()
            db.add(CashMovement(cash_session_id=session.id, movement_type="expense", amount=Decimal("10"), payment_method="cash"))
    else:
        add_movement(client, tenant["register_id"], auth_headers, key=kind, movement_type=kind, amount=10)
    current = preview(client, tenant["register_id"], auth_headers)
    assert current["has_cash_activity"] and not current["has_card_activity"]
    result = cut(client, tenant["register_id"], auth_headers, cash=0)
    assert result.status_code == 201, result.text
    cash = next(row for row in result.json()["methods"] if row["key"] == "cash")
    assert cash["expected"] == expected and cash["transactions"][0]["signed_amount"] == expected


def test_opening_fund_enables_cash_count_with_other_activity_but_empty_cut_stays_blocked(client, tenant, auth_headers):
    with SessionLocal.begin() as db:
        session = CashSession(business_id=tenant["business_id"], branch_id=tenant["branch_id"], register_id=tenant["register_id"], opened_by="owner-test", opening_amount=Decimal("10"))
        db.add(session)
    current = preview(client, tenant["register_id"], auth_headers)
    assert current["has_cash_activity"] is True
    empty = cut(client, tenant["register_id"], auth_headers, cash=10)
    assert empty.status_code == 409 and empty.json()["code"] == "CASH_CUT_NO_ACTIVITY"
    paid_order(client, tenant, auth_headers, method="yape")
    result = cut(client, tenant["register_id"], auth_headers, cash=10)
    assert result.status_code == 201, result.text


def test_opposing_differences_are_mixed_not_balanced_and_filters_match(client, tenant, auth_headers):
    order = paid_order(client, tenant, auth_headers, amount=10)
    add_payment(client, order["id"], auth_headers, key="mixed-card", method="card", amount=10, register_id=tenant["register_id"])
    result = cut(client, tenant["register_id"], auth_headers, cash=5, card=15).json()
    assert result["result"] == "balanced" and result["total_difference"] == 0
    assert result["reconciliation_status"] == "mixed" and result["has_discrepancy"] is True
    for params, total in [({"reconciliation_status": "mixed"}, 1), ({"reconciliation_status": "balanced"}, 0), ({"has_discrepancy": True}, 1), ({"has_discrepancy": False}, 0)]:
        response = client.get("/api/v1/cash/cuts", params={"branch_id": tenant["branch_id"], **params}, headers=auth_headers)
        assert response.status_code == 200, response.text
        assert response.json()["total"] == total


def test_current_period_aggregates_are_not_page_totals_and_keep_withdrawal_sign(client, tenant, auth_headers):
    income = add_movement(client, tenant["register_id"], auth_headers, key="income", movement_type="income", amount=30)
    withdrawal = add_movement(client, tenant["register_id"], auth_headers, key="withdrawal", movement_type="withdrawal", amount=15)
    assert withdrawal["amount"] == 15 and withdrawal["signed_amount"] == -15
    response = client.get(f"/api/v1/cash/registers/{tenant['register_id']}/movements", params={"current_period": True, "page_size": 1}, headers=auth_headers).json()
    assert response["total"] == 2 and len(response["items"]) == 1
    assert response["summary"] == {"income_amount": 30, "withdrawal_amount": 15, "expense_amount": 0, "refund_amount": 0, "signed_amount": 15}
    assert response["session_id"] == income["session_id"]
    assert cut(client, tenant["register_id"], auth_headers, cash=15, retained=5).status_code == 201
    current = client.get(f"/api/v1/cash/registers/{tenant['register_id']}/movements", params={"current_period": True}, headers=auth_headers).json()
    assert current["total"] == 0 and current["summary"]["signed_amount"] == 0
    assert current["session_id"] != response["session_id"]
    archived = client.get(f"/api/v1/cash/registers/{tenant['register_id']}/movements", params={"session_id": response["session_id"]}, headers=auth_headers).json()
    assert archived["total"] == 2


def test_period_guard_rejects_same_version_on_different_period_and_explicit_null(client, tenant, auth_headers):
    initial = preview(client, tenant["register_id"], auth_headers)
    add_movement(client, tenant["register_id"], auth_headers, key="open", movement_type="income", amount=10)
    response = client.post(f"/api/v1/cash/registers/{tenant['register_id']}/movements", json={"movement_type": "income", "amount": 1, "note": "stale-null", "expected_version": 2, "expected_session_id": initial["session_id"]}, headers={**auth_headers, "Idempotency-Key": "null-stale"})
    assert response.status_code == 409
    old = preview(client, tenant["register_id"], auth_headers)
    assert cut(client, tenant["register_id"], auth_headers, cash=10).status_code == 201
    current = preview(client, tenant["register_id"], auth_headers)
    response = client.post(f"/api/v1/cash/registers/{tenant['register_id']}/movements", json={"movement_type": "income", "amount": 1, "note": "stale-id", "expected_version": current["version"], "expected_session_id": old["session_id"]}, headers={**auth_headers, "Idempotency-Key": "period-stale"})
    assert response.status_code == 409 and response.json()["code"] == "CASH_CUT_STALE"
    stale_cut = cut(client, tenant["register_id"], auth_headers, expected_session_id=old["session_id"], key="period-stale-cut")
    assert stale_cut.status_code == 409


def test_cash_idempotency_binds_new_body_and_replays_old_raw_record(client, tenant, auth_headers):
    current = preview(client, tenant["register_id"], auth_headers)
    body = {"movement_type": "income", "amount": 10, "note": "Aporte", "expected_version": current["version"], "expected_session_id": current["session_id"]}
    url = f"/api/v1/cash/registers/{tenant['register_id']}/movements"
    headers = {**auth_headers, "Idempotency-Key": "body-bound"}
    first = client.post(url, json=body, headers=headers)
    assert first.status_code == 201, first.text
    assert client.post(url, json=body, headers=headers).json() == first.json()
    conflict = client.post(url, json={**body, "amount": 11}, headers=headers)
    assert conflict.status_code == 409 and conflict.json()["code"] == "CASH_IDEMPOTENCY_CONFLICT"
    with SessionLocal.begin() as db:
        db.add(IdempotencyRecord(business_id=tenant["business_id"], scope=f"cash-register-movement:{tenant['register_id']}", idempotency_key="legacy", response_body={"id": 999, "amount": 3}))
    legacy = client.post(url, json=body, headers={**auth_headers, "Idempotency-Key": "legacy"})
    assert legacy.status_code == 201 and legacy.json() == {"id": 999, "amount": 3}
    with SessionLocal() as db:
        assert db.query(CashMovement).count() == 1


def test_historical_unknown_card_count_not_invented_and_synthetic_refunds_not_linked(client, tenant, auth_headers):
    with SessionLocal.begin() as db:
        session = CashSession(business_id=tenant["business_id"], branch_id=tenant["branch_id"], register_id=tenant["register_id"], opened_by="old", status="closed", opening_amount=0, expected_amount=10, declared_amount=10, difference=0, card_expected_amount=20, card_declared_amount=None, card_difference=None, result="balanced")
        db.add(session)
        db.flush()
        session_id = session.id
    result = client.get(f"/api/v1/cash/cuts/{session_id}", headers=auth_headers).json()
    card = next(item for item in result["methods"] if item["key"] == "card")
    assert card["counted"] is None and card["difference"] is None
    assert result["has_discrepancy"] is False
