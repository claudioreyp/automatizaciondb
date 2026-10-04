from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import AuditEvent, CashMovement, CashRegister, CashSession, Order, Payment
from test_cash_cuts import add_payment, confirm_and_send, create_order, preview
from test_cash_refunds import cancellation, cancel, cancel_body, cut
from test_security_audit import add_event, detail, event_id, fields, listing


def test_critical_pagination_excludes_routine_actions_before_counting(client, tenant, auth_headers):
    created_at = datetime(2026, 10, 4, 5, tzinfo=timezone.utc)
    with SessionLocal.begin() as db:
        critical = [AuditEvent(business_id=tenant["business_id"], branch_id=tenant["branch_id"],
            action="order.cancelled", entity_type="order", payload={"paid_amount": 0}, created_at=created_at)
            for _ in range(213)]
        routine = [AuditEvent(business_id=tenant["business_id"], branch_id=tenant["branch_id"],
            action=action, entity_type="order", created_at=created_at)
            for action in ["order.created", "kitchen.ready", "payment.created", "pos.print_job.claimed"] * 30]
        increases = [AuditEvent(business_id=tenant["business_id"], branch_id=tenant["branch_id"],
            action="order.items_revised", entity_type="order", created_at=created_at,
            payload={"snapshot_version": 1, "operations": [{"type": "edit", "before": {"unit_price": 10}, "after": {"unit_price": 20}}]})
            for _ in range(30)]
        db.add_all([*critical, *routine, *increases])
        db.flush()
        ids = [row.id for row in critical][::-1]
    params = {"critical_only": True, "page_size": 12, "from": "2026-10-04", "to": "2026-10-04"}
    first = listing(client, auth_headers, **params)
    assert first["total"] == 213
    assert [row["id"] for row in first["items"]] == ids[:12]
    assert [row["id"] for row in listing(client, auth_headers, **params, page=18)["items"]] == ids[204:]
    assert listing(client, auth_headers, **params, page=100)["items"] == []
    assert listing(client, auth_headers, **params, category="order_cancellation")["total"] == 213
    assert listing(client, auth_headers, **params, action="payment")["total"] == 0
    assert listing(client, auth_headers, page_size=100)["total"] == 363
    assert listing(client, auth_headers, critical_only=False)["total"] == 363
    with SessionLocal() as db:
        assert db.query(AuditEvent).count() == 363


def closed_session(tenant, *, cash_difference=-5, card_difference=5, status="closed"):
    with SessionLocal.begin() as db:
        row = CashSession(business_id=tenant["business_id"], branch_id=tenant["branch_id"],
            register_id=tenant["register_id"], opened_by="operator", closed_by="operator",
            status=status, closed_at=datetime(2026, 10, 4, 5, tzinfo=timezone.utc) if status == "closed" else None,
            actor_display_name="Cajero histórico", declared_amount=0, expected_amount=5,
            difference=cash_difference, card_declared_amount=10, card_expected_amount=5,
            card_difference=card_difference, total_difference=cash_difference + card_difference)
        db.add(row)
        db.flush()
        return row.id


def test_legacy_cut_recovers_closed_method_differences_actor_and_scoped_target(client, tenant, auth_headers):
    cut_id = closed_session(tenant)
    audit_id = add_event(tenant, action="cash.cut_created", entity_type="cash_session", entity_id=cut_id,
        payload={"total_difference": 0, "result": "balanced"})
    with SessionLocal.begin() as db:
        db.get(AuditEvent, audit_id).actor_display_name = None
        db.get(CashRegister, tenant["register_id"]).active = False
    page = listing(client, auth_headers, critical_only=True)
    assert page["total"] == 1 and page["items"][0]["categories"] == ["cash_discrepancy"]
    assert page["items"][0]["actor_display_name"] == "Cajero histórico"
    result = detail(client, auth_headers, audit_id)
    assert result["actor_name"] == "Cajero histórico"
    assert result["summary"] == "realizó un corte de caja con diferencias"
    assert result["target"] == {"kind": "cash_cut", "branch_id": tenant["branch_id"],
        "register_id": tenant["register_id"], "cut_id": cut_id, "label": f"Corte #{cut_id}"}
    assert fields(result)["Caja"] is None  # Current register names are not historical evidence.
    assert fields(result["sections"][0])["Diferencia"] == "S/ -5.00"
    assert fields(result["sections"][1])["Diferencia"] == "S/ 5.00"
    assert listing(client, auth_headers, critical_only=True, category="cash_discrepancy")["total"] == 1
    assert client.get(f"/api/v1/settings/audit/{audit_id}", params={"branch_id": tenant["other_branch_id"]}, headers=auth_headers).status_code in {403, 404}
    response = client.get(f"/api/v1/cash/cuts/{cut_id}", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["branch_id"] == tenant["branch_id"]
    assert response.json()["register"]["id"] == tenant["register_id"]


@pytest.mark.parametrize("status,cash_difference,card_difference", [("closed", 0, 0), ("open", -5, 5)])
def test_no_cut_discrepancy_inferred_from_open_period_or_balanced_cut(client, tenant, auth_headers, status, cash_difference, card_difference):
    cut_id = closed_session(tenant, status=status, cash_difference=cash_difference, card_difference=card_difference)
    add_event(tenant, action="cash.cut_created", entity_type="cash_session", entity_id=cut_id,
        payload={"total_difference": -999, "has_discrepancy": True})
    assert listing(client, auth_headers, critical_only=True)["total"] == 0


def test_new_cut_snapshot_is_immutable_with_zero_net_discrepancy_and_one_postcommit_event(client, tenant, auth_headers, monkeypatch):
    import app.api as api
    emitted = []

    async def broadcast(branch_id, action, payload):
        with SessionLocal() as db:
            assert db.get(CashSession, payload["cut_id"]).status == "closed"
            assert db.query(AuditEvent).filter_by(action="cash.cut_created").count() == 1
        emitted.append((branch_id, action, payload))

    order = create_order(client, tenant, auth_headers, key="cut-security-order", quantity=2)
    confirm_and_send(client, order, auth_headers, key="cut-security-send")
    add_payment(client, order["id"], auth_headers, key="cut-cash", method="cash", amount=20, register_id=tenant["register_id"])
    add_payment(client, order["id"], auth_headers, key="cut-card", method="card", amount=20, register_id=tenant["register_id"])
    monkeypatch.setattr(api.hub, "broadcast", broadcast)
    current = preview(client, tenant["register_id"], auth_headers)
    body = {"cash_counted": 0, "card_counted": 40, "retained_fund": 0,
        "expected_version": current["version"], "expected_session_id": current["session_id"]}
    headers = {**auth_headers, "Idempotency-Key": "cut-security"}
    response = client.post(f"/api/v1/cash/registers/{tenant['register_id']}/cuts", json=body, headers=headers)
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["total_difference"] == 0 and result["result"] == "balanced"
    audit_id = event_id("cash.cut_created")
    first = detail(client, auth_headers, audit_id)
    assert listing(client, auth_headers, critical_only=True)["items"][0]["id"] == audit_id
    assert first["summary"] == "realizó un corte de caja con diferencias"
    assert emitted == [(tenant["branch_id"], "cash.cut_created", {"register_id": tenant["register_id"], "cut_id": result["id"]})]
    assert client.post(f"/api/v1/cash/registers/{tenant['register_id']}/cuts", json=body, headers=headers).json() == result
    assert len(emitted) == 1
    with SessionLocal.begin() as db:
        event = db.get(AuditEvent, audit_id)
        assert event.branch_id == tenant["branch_id"]
        assert event.actor_display_name == db.get(CashSession, result["id"]).actor_display_name
        snapshot = deepcopy(event.payload)
        assert snapshot["methods"][0]["difference"] == "-20.00"
        assert snapshot["methods"][1]["difference"] == "20.00"
        db.get(CashSession, result["id"]).difference = 999
        db.get(CashSession, result["id"]).card_difference = 999
        db.get(CashRegister, tenant["register_id"]).name = "Nombre posterior"
    assert detail(client, auth_headers, audit_id) == first
    with SessionLocal() as db:
        assert db.get(AuditEvent, audit_id).payload == snapshot


@pytest.mark.parametrize("original,refund,expected", [("cash", "cash", False), ("cash", "card", True), ("yape", "plin", True)])
def test_refund_method_change_uses_exact_original_methods_and_snapshot(client, tenant, auth_headers, original, refund, expected):
    order = create_order(client, tenant, auth_headers, key="refund-security")
    confirm_and_send(client, order, auth_headers, key="refund-security-send")
    add_payment(client, order["id"], auth_headers, key="refund-security-pay", method=original, amount=20, register_id=tenant["register_id"])
    current = cancellation(client, order["id"], auth_headers)
    response = cancel(client, order["id"], auth_headers, cancel_body(current, tenant["register_id"], [{"method": refund, "amount": 20}]))
    assert response.status_code == 200, response.text
    audit_id = event_id("cash.refund_created")
    with SessionLocal() as db:
        payload = deepcopy(db.get(AuditEvent, audit_id).payload)
        assert payload["original_payment_methods"] == [original]
        assert payload["order"] == {"order_id": order["id"], "number": order["number"], "folio": order["folio"]}
    page = listing(client, auth_headers, critical_only=True)
    assert page["total"] == (2 if expected else 1)  # The cancellation itself is already critical.
    assert listing(client, auth_headers, category="refund_method_change")["total"] == int(expected)
    first = detail(client, auth_headers, audit_id)
    with SessionLocal.begin() as db:
        db.scalar(select(Payment).where(Payment.order_id == order["id"])).method = refund
    assert detail(client, auth_headers, audit_id) == first


def test_legacy_refund_fallback_uses_retained_payment_relationships_and_same_branch(client, tenant, auth_headers):
    order = create_order(client, tenant, auth_headers, key="legacy-refund-security")
    confirm_and_send(client, order, auth_headers, key="legacy-refund-send")
    add_payment(client, order["id"], auth_headers, key="legacy-refund-pay", method="cash", amount=20, register_id=tenant["register_id"])
    response = cancel(client, order["id"], auth_headers, cancel_body(cancellation(client, order["id"], auth_headers),
        tenant["register_id"], [{"method": "card", "amount": 20}]))
    assert response.status_code == 200, response.text
    audit_id = event_id("cash.refund_created")
    with SessionLocal.begin() as db:
        event = db.get(AuditEvent, audit_id)
        event.branch_id = None
        event.payload = {"movement_type": "refund"}
    assert listing(client, auth_headers, critical_only=True, category="refund_method_change")["total"] == 1
    assert fields(detail(client, auth_headers, audit_id))["Métodos del cobro original"] == "Efectivo"
    # A conflicting branch cannot be recovered through payload claims.
    with SessionLocal.begin() as db:
        db.get(Order, order["id"]).branch_id = tenant["other_branch_id"]
    assert listing(client, auth_headers, critical_only=True, category="refund_method_change")["total"] == 0


@pytest.mark.parametrize("legacy", [False, True])
def test_withdrawal_and_expense_event_publish_after_commit_once(client, tenant, auth_headers, monkeypatch, legacy):
    import app.api as api
    emitted = []

    async def broadcast(branch_id, action, payload):
        with SessionLocal() as db:
            assert db.get(CashMovement, payload["movement_id"]) is not None
            assert db.query(AuditEvent).filter_by(action="cash.movement_created").count() == 1
        emitted.append((branch_id, action, payload))

    monkeypatch.setattr(api.hub, "broadcast", broadcast)
    if legacy:
        with SessionLocal.begin() as db:
            session = CashSession(business_id=tenant["business_id"], branch_id=tenant["branch_id"],
                register_id=tenant["register_id"], opened_by="operator")
            db.add(session)
            db.flush()
            session_id = session.id
        path = f"/api/v1/cash/sessions/{session_id}/movements"
        body = {"movement_type": "expense", "amount": 5, "payment_method": "cash", "note": "Insumos"}
    else:
        path = f"/api/v1/cash/registers/{tenant['register_id']}/movements"
        body = {"movement_type": "withdrawal", "amount": 5, "note": "Retiro", "expected_version": 0}
    headers = {**auth_headers, "Idempotency-Key": "security-cash-move"}
    response = client.post(path, json=body, headers=headers)
    assert response.status_code == 201, response.text
    assert client.post(path, json=body, headers=headers).json() == response.json()
    assert emitted == [(tenant["branch_id"], "cash.movement_created", {"register_id": tenant["register_id"], "movement_id": response.json()["id"]})]
    page = listing(client, auth_headers, critical_only=True)
    assert page["total"] == 1 and page["items"][0]["categories"] == ["cash_withdrawal"]
    if legacy:
        assert page["items"][0]["summary"] == "registró un gasto de caja"
