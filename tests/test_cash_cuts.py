from decimal import Decimal

from app.database import SessionLocal
from app.models import (
    AuditEvent,
    CashMovement,
    CashRegister,
    CashSession,
    Membership,
    Order,
    Payment,
    RestaurantTable,
)


def create_order(
    client,
    tenant,
    auth_headers,
    *,
    key: str,
    quantity: int = 1,
    channel: str = "counter",
):
    payload = {
        "branch_id": tenant["branch_id"],
        "channel": channel,
        "source": "pos",
        "items": [{"product_id": tenant["product_id"], "quantity": quantity}],
    }
    if channel == "dine_in":
        payload["table_id"] = tenant["table_id"]
    response = client.post(
        "/api/v1/orders",
        json=payload,
        headers={**auth_headers, "Idempotency-Key": key},
    )
    assert response.status_code == 201, response.text
    return response.json()


def confirm_and_send(client, order, auth_headers, *, key: str):
    response = client.post(
        f"/api/v1/orders/{order['id']}/confirm-and-send",
        json={"expected_version": order["version"]},
        headers={**auth_headers, "Idempotency-Key": key},
    )
    assert response.status_code == 200, response.text
    return response.json()


def add_payment(
    client,
    order_id: int,
    auth_headers,
    *,
    key: str,
    method: str,
    amount: float,
    register_id: int | None = None,
):
    payload = {"method": method, "amount": amount}
    if register_id is not None:
        payload["register_id"] = register_id
    response = client.post(
        f"/api/v1/orders/{order_id}/payments",
        json=payload,
        headers={**auth_headers, "Idempotency-Key": key},
    )
    assert response.status_code == 201, response.text
    return response.json()


def add_movement(
    client,
    register_id: int,
    auth_headers,
    *,
    key: str,
    movement_type: str,
    amount: float,
):
    current_preview = preview(client, register_id, auth_headers)
    response = client.post(
        f"/api/v1/cash/registers/{register_id}/movements",
        json={
            "movement_type": movement_type,
            "amount": amount,
            "note": f"Movimiento {movement_type}",
            "expected_version": current_preview["version"],
        },
        headers={**auth_headers, "Idempotency-Key": key},
    )
    assert response.status_code == 201, response.text
    return response.json()


def preview(client, register_id: int, auth_headers):
    response = client.get(
        f"/api/v1/cash/registers/{register_id}/cut-preview",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def create_cut(
    client,
    register_id: int,
    auth_headers,
    *,
    key: str,
    version: int,
    cash_counted: float,
    card_counted: float | None = None,
    retained_fund: float = 0,
    denominations: dict[str, int] | None = None,
    ignore_pending_orders: bool = False,
):
    payload = {
        "cash_counted": cash_counted,
        "card_counted": card_counted,
        "retained_fund": retained_fund,
        "denominations": denominations,
        "ignore_pending_orders": ignore_pending_orders,
        "expected_version": version,
    }
    return client.post(
        f"/api/v1/cash/registers/{register_id}/cuts",
        json=payload,
        headers={**auth_headers, "Idempotency-Key": key},
    )


def test_blind_cut_reconciles_mixed_methods_and_rolls_over_idempotently(
    client,
    tenant,
    auth_headers,
):
    order = create_order(
        client,
        tenant,
        auth_headers,
        key="cash-mixed-order",
        quantity=3,
    )
    sent = confirm_and_send(client, order, auth_headers, key="cash-mixed-send")
    add_payment(
        client,
        sent["order"]["id"],
        auth_headers,
        key="cash-mixed-cash",
        method="cash",
        amount=20,
    )
    add_payment(
        client,
        sent["order"]["id"],
        auth_headers,
        key="cash-mixed-card",
        method="card",
        amount=15,
    )
    add_payment(
        client,
        sent["order"]["id"],
        auth_headers,
        key="cash-mixed-yape",
        method="yape",
        amount=25,
    )
    add_movement(
        client,
        tenant["register_id"],
        auth_headers,
        key="cash-mixed-income",
        movement_type="income",
        amount=10,
    )
    add_movement(
        client,
        tenant["register_id"],
        auth_headers,
        key="cash-mixed-withdrawal",
        movement_type="withdrawal",
        amount=5,
    )

    cut_preview = preview(client, tenant["register_id"], auth_headers)
    assert "cash_expected" not in cut_preview
    assert "card_expected" not in cut_preview
    assert cut_preview["has_card_activity"] is True
    assert cut_preview["transfer_expected_amount"] == 25.0
    assert cut_preview["pending_orders"] == []

    cut = create_cut(
        client,
        tenant["register_id"],
        auth_headers,
        key="cash-mixed-cut",
        version=cut_preview["version"],
        cash_counted=25,
        card_counted=15,
        retained_fund=5,
        denominations={"5": 1, "20": 1},
    )
    assert cut.status_code == 201, cut.text
    payload = cut.json()
    assert payload["result"] == "balanced"
    assert payload["total_expected_amount"] == 65.0
    assert payload["total_difference"] == 0.0
    assert payload["cash_withdrawn_amount"] == 20.0
    assert payload["next_period"]["opening_amount"] == 5.0
    groups = {group["key"]: group for group in payload["methods"]}
    assert groups["cash"]["expected"] == 25.0
    assert groups["card"]["expected"] == 15.0
    assert groups["transfer"]["expected"] == 25.0
    assert {row["method"] for row in groups["transfer"]["transactions"]} == {"yape"}

    repeated = create_cut(
        client,
        tenant["register_id"],
        auth_headers,
        key="cash-mixed-cut",
        version=cut_preview["version"],
        cash_counted=25,
        card_counted=15,
        retained_fund=5,
        denominations={"5": 1, "20": 1},
    )
    assert repeated.status_code == 201, repeated.text
    assert repeated.json()["id"] == payload["id"]

    with SessionLocal() as db:
        assert (
            db.query(CashSession)
            .filter(CashSession.register_id == tenant["register_id"], CashSession.status == "closed")
            .count()
            == 1
        )
        open_session = db.query(CashSession).filter_by(
            register_id=tenant["register_id"],
            status="open",
        ).one()
        assert open_session.previous_session_id == payload["id"]
        assert open_session.opening_amount == Decimal("5.00")
        assert db.query(CashMovement).filter_by(movement_type="sale").count() == 0

    history = client.get(
        "/api/v1/cash/cuts",
        params={"branch_id": tenant["branch_id"], "page": 1, "page_size": 10},
        headers=auth_headers,
    )
    assert history.status_code == 200, history.text
    assert history.json()["total"] == 1
    assert history.json()["items"][0]["id"] == payload["id"]

    empty_preview = preview(client, tenant["register_id"], auth_headers)
    no_activity = create_cut(
        client,
        tenant["register_id"],
        auth_headers,
        key="cash-no-activity",
        version=empty_preview["version"],
        cash_counted=5,
        retained_fund=5,
    )
    assert no_activity.status_code == 409
    assert no_activity.json()["code"] == "CASH_CUT_NO_ACTIVITY"


def test_pending_partial_order_requires_audited_override_and_full_name(
    client,
    tenant,
    auth_headers,
):
    with SessionLocal.begin() as db:
        db.add(
            Membership(
                auth_user_id="owner-test",
                email="owner@example.com",
                full_name="Claudio Rey",
                business_id=tenant["business_id"],
                branch_id=tenant["branch_id"],
                role="owner",
            )
        )
    order = create_order(
        client,
        tenant,
        auth_headers,
        key="cash-pending-order",
    )
    sent = confirm_and_send(client, order, auth_headers, key="cash-pending-send")
    add_payment(
        client,
        sent["order"]["id"],
        auth_headers,
        key="cash-pending-partial",
        method="cash",
        amount=10,
    )

    cut_preview = preview(client, tenant["register_id"], auth_headers)
    assert cut_preview["pending_order_count"] == 1
    assert cut_preview["pending_orders"][0]["balance"] == 10.0
    blocked = create_cut(
        client,
        tenant["register_id"],
        auth_headers,
        key="cash-pending-blocked",
        version=cut_preview["version"],
        cash_counted=10,
    )
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "CASH_CUT_PENDING_ORDERS"

    overridden = create_cut(
        client,
        tenant["register_id"],
        auth_headers,
        key="cash-pending-override",
        version=cut_preview["version"],
        cash_counted=10,
        ignore_pending_orders=True,
    )
    assert overridden.status_code == 201, overridden.text
    payload = overridden.json()
    assert payload["created_by"] == "Claudio Rey"
    assert payload["pending_orders_ignored"] is True
    assert payload["pending_orders"][0]["id"] == order["id"]

    with SessionLocal() as db:
        session = db.get(CashSession, payload["id"])
        assert session.pending_orders_override_by == "owner-test"
        assert session.pending_orders_override_at is not None
        assert session.actor_display_name == "Claudio Rey"
        event = db.query(AuditEvent).filter_by(
            action="cash.cut_created",
            entity_id=str(payload["id"]),
        ).one()
        assert event.payload["pending_orders_ignored"] is True


def test_explicit_register_attribution_and_historical_unattributed_payments_are_safe(
    client,
    tenant,
    auth_headers,
):
    second = client.post(
        "/api/v1/cash/registers",
        json={"branch_id": tenant["branch_id"], "name": "Caja secundaria"},
        headers=auth_headers,
    )
    assert second.status_code == 201, second.text
    order = create_order(
        client,
        tenant,
        auth_headers,
        key="cash-explicit-register-order",
    )
    sent = confirm_and_send(client, order, auth_headers, key="cash-explicit-register-send")
    paid = add_payment(
        client,
        sent["order"]["id"],
        auth_headers,
        key="cash-explicit-register-payment",
        method="card",
        amount=20,
        register_id=second.json()["id"],
    )
    with SessionLocal.begin() as db:
        payment = db.get(Payment, paid["payment"]["id"])
        session = db.get(CashSession, payment.cash_session_id)
        assert session.register_id == second.json()["id"]
        historical_order = db.get(Order, sent["order"]["id"])
        db.add(
            Payment(
                business_id=tenant["business_id"],
                order_id=historical_order.id,
                cash_session_id=None,
                method="cash",
                status="confirmed",
                amount=Decimal("0.01"),
                created_by="legacy-import",
            )
        )

    cut_preview = preview(client, second.json()["id"], auth_headers)
    cut = create_cut(
        client,
        second.json()["id"],
        auth_headers,
        key="cash-explicit-register-cut",
        version=cut_preview["version"],
        cash_counted=0,
        card_counted=20,
    )
    assert cut.status_code == 201, cut.text
    assert cut.json()["total_expected_amount"] == 20.0


def test_payment_without_register_rejects_multiple_open_periods(
    client,
    tenant,
    auth_headers,
):
    second = client.post(
        "/api/v1/cash/registers",
        json={"branch_id": tenant["branch_id"], "name": "Caja secundaria"},
        headers=auth_headers,
    )
    assert second.status_code == 201, second.text
    add_movement(
        client,
        tenant["register_id"],
        auth_headers,
        key="cash-ambiguous-primary-open",
        movement_type="income",
        amount=1,
    )
    add_movement(
        client,
        second.json()["id"],
        auth_headers,
        key="cash-ambiguous-secondary-open",
        movement_type="income",
        amount=1,
    )

    order = create_order(
        client,
        tenant,
        auth_headers,
        key="cash-ambiguous-register-order",
    )
    sent = confirm_and_send(
        client,
        order,
        auth_headers,
        key="cash-ambiguous-register-send",
    )
    response = client.post(
        f"/api/v1/orders/{sent['order']['id']}/payments",
        json={"method": "cash", "amount": 20},
        headers={**auth_headers, "Idempotency-Key": "cash-ambiguous-register-payment"},
    )

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "CASH_REGISTER_AMBIGUOUS"

    explicit = client.post(
        f"/api/v1/orders/{sent['order']['id']}/payments",
        json={"method": "cash", "amount": 20, "register_id": second.json()["id"]},
        headers={**auth_headers, "Idempotency-Key": "cash-explicit-register-after-ambiguity"},
    )
    assert explicit.status_code == 201, explicit.text


def test_cash_movement_rejects_a_stale_period_version(
    client,
    tenant,
    auth_headers,
):
    first = client.post(
        f"/api/v1/cash/registers/{tenant['register_id']}/movements",
        json={
            "movement_type": "income",
            "amount": 5,
            "note": "Fondo inicial",
            "expected_version": 0,
        },
        headers={**auth_headers, "Idempotency-Key": "cash-versioned-movement-first"},
    )
    assert first.status_code == 201, first.text

    stale = client.post(
        f"/api/v1/cash/registers/{tenant['register_id']}/movements",
        json={
            "movement_type": "withdrawal",
            "amount": 1,
            "note": "Versión desactualizada",
            "expected_version": 0,
        },
        headers={**auth_headers, "Idempotency-Key": "cash-versioned-movement-stale"},
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["code"] == "CASH_CUT_STALE"


def test_payment_reassigns_default_when_the_previous_default_is_inactive(
    client,
    tenant,
    auth_headers,
):
    second = client.post(
        "/api/v1/cash/registers",
        json={"branch_id": tenant["branch_id"], "name": "Caja disponible"},
        headers=auth_headers,
    )
    assert second.status_code == 201, second.text
    with SessionLocal.begin() as db:
        previous_default = db.get(CashRegister, tenant["register_id"])
        previous_default.active = False

    order = create_order(
        client,
        tenant,
        auth_headers,
        key="cash-default-reassignment-order",
    )
    sent = confirm_and_send(
        client,
        order,
        auth_headers,
        key="cash-default-reassignment-send",
    )
    payment = add_payment(
        client,
        sent["order"]["id"],
        auth_headers,
        key="cash-default-reassignment-payment",
        method="cash",
        amount=20,
    )

    with SessionLocal() as db:
        stored_payment = db.get(Payment, payment["payment"]["id"])
        session = db.get(CashSession, stored_payment.cash_session_id)
        previous_default = db.get(CashRegister, tenant["register_id"])
        current_default = db.get(CashRegister, second.json()["id"])
        assert session.register_id == current_default.id
        assert previous_default.is_default is False
        assert current_default.is_default is True


def test_cash_cut_is_tenant_isolated(client, tenant, auth_headers):
    add_movement(
        client,
        tenant["register_id"],
        auth_headers,
        key="cash-isolation-income",
        movement_type="income",
        amount=5,
    )
    cut_preview = preview(client, tenant["register_id"], auth_headers)
    cut = create_cut(
        client,
        tenant["register_id"],
        auth_headers,
        key="cash-isolation-cut",
        version=cut_preview["version"],
        cash_counted=5,
    )
    assert cut.status_code == 201, cut.text

    with SessionLocal.begin() as db:
        other_register = CashRegister(
            business_id=tenant["other_business_id"],
            branch_id=tenant["other_branch_id"],
            name="Caja Principal",
            active=True,
            is_default=True,
        )
        db.add(other_register)
        db.flush()
        other_register_id = other_register.id
    other_headers = {
        **auth_headers,
        "X-Business-Id": str(tenant["other_business_id"]),
        "X-Branch-Id": str(tenant["other_branch_id"]),
    }
    hidden_cut = client.get(
        f"/api/v1/cash/cuts/{cut.json()['id']}",
        headers=other_headers,
    )
    assert hidden_cut.status_code == 404
    hidden_preview = client.get(
        f"/api/v1/cash/registers/{tenant['register_id']}/cut-preview",
        headers=other_headers,
    )
    assert hidden_preview.status_code == 404
    own_preview = client.get(
        f"/api/v1/cash/registers/{other_register_id}/cut-preview",
        headers=other_headers,
    )
    assert own_preview.status_code == 200, own_preview.text


def test_paid_table_remains_in_kitchen_and_is_included_in_cash_cut(
    client,
    tenant,
    auth_headers,
):
    order = create_order(
        client,
        tenant,
        auth_headers,
        key="cash-table-order",
        channel="dine_in",
    )
    sent = confirm_and_send(client, order, auth_headers, key="cash-table-send")
    started = client.post(
        f"/api/v1/orders/{order['id']}/table-checkout/start",
        json={"expected_version": sent["order"]["version"]},
        headers={**auth_headers, "Idempotency-Key": "cash-table-start"},
    )
    assert started.status_code == 200, started.text
    paid = client.post(
        f"/api/v1/orders/{order['id']}/table-checkout/pay",
        json={
            "expected_version": started.json()["order"]["version"],
            "payments": [{"method": "cash", "amount": 20}],
        },
        headers={**auth_headers, "Idempotency-Key": "cash-table-pay"},
    )
    assert paid.status_code == 200, paid.text
    assert paid.json()["order"]["payment_status"] == "paid"
    assert paid.json()["order"]["status"] == "sent_to_kitchen"
    assert paid.json()["table"]["status"] == "available"

    cut_preview = preview(client, tenant["register_id"], auth_headers)
    assert cut_preview["pending_orders"] == []
    cut = create_cut(
        client,
        tenant["register_id"],
        auth_headers,
        key="cash-table-cut",
        version=cut_preview["version"],
        cash_counted=20,
    )
    assert cut.status_code == 201, cut.text
    cash_transactions = next(
        group["transactions"]
        for group in cut.json()["methods"]
        if group["key"] == "cash"
    )
    assert cash_transactions[0]["order_id"] == order["id"]
    with SessionLocal() as db:
        assert db.get(Order, order["id"]).status == "sent_to_kitchen"
        assert db.get(RestaurantTable, tenant["table_id"]).status == "available"
