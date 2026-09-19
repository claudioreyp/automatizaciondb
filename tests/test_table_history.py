from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, select

from app import api as api_module
from app.database import SessionLocal, engine
from app.models import (
    AuditEvent,
    Branch,
    CashMovement,
    CashRegister,
    CashSession,
    IntegrationEvent,
    InventoryItem,
    KitchenTicket,
    Order,
    Payment,
    PaymentAllocation,
    PaymentEvidence,
    RestaurantTable,
    StockMovement,
)
from test_command_workflow import create_order, send_order


def seed_order(db, tenant, **values):
    fields = {
        "business_id": tenant["business_id"],
        "branch_id": tenant["branch_id"],
        "number": uuid4().hex,
        "channel": "dine_in",
        "table_id": tenant["table_id"],
        "status": "closed",
        "total": Decimal("20"),
    }
    order = Order(**{**fields, **values})
    db.add(order)
    db.flush()
    return order


def workspace(client, tenant, headers, **params):
    response = client.get(
        "/api/v1/orders/workspace",
        params={"branch_id": tenant["branch_id"], **params},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def detail(client, order_id, headers):
    response = client.get(f"/api/v1/orders/{order_id}/detail", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_all_history_paginates_past_200_with_date_id_ties_and_global_search(
    client, tenant, auth_headers,
):
    base = datetime(2024, 1, 1, 12, tzinfo=timezone.utc)
    with SessionLocal.begin() as db:
        orders = [
            seed_order(
                db, tenant, number=f"HIST-{index:04}", folio=9000 + index,
                customer_name="Historical guest" if index == 0 else "Regular guest",
                customer_phone="+51987654321" if index == 0 else None,
                created_at=base + timedelta(days=index % 7),
            )
            for index in range(237)
        ]
        for index in (0, 150, 236):
            db.add(PaymentEvidence(
                business_id=tenant["business_id"], order_id=orders[index].id,
                provider="yape", storage_path="private/test.webp",
                image_sha256=f"{index:064x}", status="under_review",
            ))
        expected = [row.id for row in sorted(orders, key=lambda row: (row.created_at, row.id), reverse=True)]
        oldest_id = orders[0].id

    received = []
    for page in (1, 2, 3):
        payload = workspace(
            client, tenant, auth_headers, period="all", view="table_history",
            day="2030-01-01", page=page, page_size=100,
        )
        assert payload["period"] == "all"
        assert payload["view"] == "table_history"
        assert payload["day"] == "2030-01-01"
        assert payload["total"] == 237
        assert payload["review_count"] == 3
        assert len(payload["items"]) == (37 if page == 3 else 100)
        received.extend(row["id"] for row in payload["items"])
    assert received == expected
    assert len(set(received)) == 237
    assert received[-1] == oldest_id

    for query in ("  historical GUEST  ", "987654321", "HIST-0000", "9000"):
        found = workspace(client, tenant, auth_headers, period="all", view="table_history", search=query)
        assert found["total"] == 1
        assert [row["id"] for row in found["items"]] == [oldest_id]
        assert found["items"][0]["requires_review"] is True
        assert found["items"][0]["paid_amount"] == 0
        assert found["review_count"] == 3
    empty = workspace(client, tenant, auth_headers, period="all", view="table_history", page=4, page_size=100)
    assert empty["items"] == []
    assert empty["total"] == 237
    missing = workspace(client, tenant, auth_headers, period="all", search="not present")
    assert missing["items"] == []
    assert missing["total"] == 0
    assert missing["review_count"] == 3


def test_daily_default_preserves_lima_boundaries_and_review_scope(client, tenant, auth_headers):
    today = datetime.now(ZoneInfo("America/Lima")).date()
    start = datetime.combine(today, time.min, tzinfo=ZoneInfo("America/Lima")).astimezone(timezone.utc)
    with SessionLocal.begin() as db:
        yesterday = seed_order(db, tenant, status="pending_confirmation", created_at=start - timedelta(microseconds=1))
        first = seed_order(db, tenant, status="pending_confirmation", created_at=start)
        last = seed_order(db, tenant, created_at=start + timedelta(days=1, microseconds=-1))
        tomorrow = seed_order(db, tenant, status="pending_confirmation", created_at=start + timedelta(days=1))
        db.add(PaymentEvidence(
            business_id=tenant["business_id"], order_id=last.id, provider="yape",
            storage_path="private/day.webp", image_sha256="a" * 64, status="under_review",
        ))
    default = workspace(client, tenant, auth_headers)
    explicit = workspace(client, tenant, auth_headers, period="day", view="orders", day=today.isoformat())
    assert default == explicit
    assert default["day"] == today.isoformat()
    assert default["period"] == "day"
    assert default["view"] == "orders"
    assert [row["id"] for row in default["items"]] == [last.id, first.id]
    assert default["review_count"] == default["total"] == 2
    historical_day = workspace(client, tenant, auth_headers, day=(today - timedelta(days=1)).isoformat())
    assert [row["id"] for row in historical_day["items"]] == [yesterday.id]
    assert historical_day["review_count"] == 1
    all_orders = workspace(client, tenant, auth_headers, period="all")
    assert [row["id"] for row in all_orders["items"]] == [tomorrow.id, last.id, first.id, yesterday.id]
    assert all_orders["review_count"] == all_orders["total"] == 4
    history = workspace(client, tenant, auth_headers, view="table_history")
    assert [row["id"] for row in history["items"]] == [last.id]
    assert history["review_count"] == 1


@pytest.mark.parametrize("params", [{"period": "week"}, {"view": "history"}, {"page_size": 101}, {"page": 0}])
def test_workspace_rejects_invalid_contract_values(client, tenant, auth_headers, params):
    response = client.get("/api/v1/orders/workspace", params={"branch_id": tenant["branch_id"], **params}, headers=auth_headers)
    assert response.status_code == 422


def test_table_history_excludes_open_and_non_table_orders(client, tenant, auth_headers):
    with SessionLocal.begin() as db:
        included = [seed_order(db, tenant, status=status) for status in ("closed", "cancelled", "delivered")]
        included.append(seed_order(db, tenant, status="preparing", table_released_at=datetime.now(timezone.utc)))
        for status in ("draft", "pending_confirmation", "confirmed", "sent_to_kitchen", "preparing", "ready"):
            seed_order(db, tenant, status=status)
        seed_order(db, tenant, status="ready", checkout_started_at=datetime.now(timezone.utc))
        seed_order(db, tenant, status="ready", payment_status="paid")
        for channel in ("counter", "pickup", "delivery", "dine_in"):
            seed_order(db, tenant, channel=channel, table_id=None, status="cancelled")
        seed_order(db, tenant, channel="counter", table_released_at=datetime.now(timezone.utc))
    result = workspace(client, tenant, auth_headers, period="all", view="table_history")
    assert result["total"] == 4
    assert result["review_count"] == 0
    assert {row["id"] for row in result["items"]} == {row.id for row in included}
    assert workspace(client, tenant, auth_headers, period="all")["total"] == 17


def test_workspace_and_details_isolate_business_and_branch(client, tenant, auth_headers):
    with SessionLocal.begin() as db:
        sibling = Branch(business_id=tenant["business_id"], slug="second", name="Second")
        db.add(sibling)
        db.flush()
        own = seed_order(db, tenant, customer_name="Shared search")
        others = [
            seed_order(db, tenant, branch_id=sibling.id, customer_name="Shared search", status="pending_confirmation", table_released_at=datetime.now(timezone.utc)),
            seed_order(db, tenant, business_id=tenant["other_business_id"], branch_id=tenant["other_branch_id"], customer_name="Shared search"),
            seed_order(db, tenant, business_id=tenant["other_business_id"], customer_name="Shared search"),
        ]
        db.add(PaymentEvidence(
            business_id=tenant["other_business_id"], order_id=own.id, provider="yape",
            storage_path="private/foreign.webp", image_sha256="b" * 64, status="under_review",
        ))
        db.add(Payment(business_id=tenant["other_business_id"], order_id=own.id, method="cash", amount=999))
    for period in ("day", "all"):
        for view in ("orders", "table_history"):
            result = workspace(client, tenant, auth_headers, period=period, view=view, search="Shared")
            assert result["total"] == 1
            assert result["review_count"] == 0
            assert result["items"][0]["id"] == own.id
            assert result["items"][0]["paid_amount"] == 0
            assert result["items"][0]["requires_review"] is False
    for branch_id in (sibling.id, tenant["other_branch_id"]):
        response = client.get("/api/v1/orders/workspace", params={"branch_id": branch_id, "period": "all", "view": "table_history"}, headers=auth_headers)
        assert response.status_code == 404
        assert response.json()["code"] == "BRANCH_NOT_FOUND"
    for order in others:
        response = client.get(f"/api/v1/orders/{order.id}/detail", headers=auth_headers)
        assert response.status_code == 404
        assert response.json()["code"] == "ORDER_NOT_FOUND"
    own_detail = detail(client, own.id, auth_headers)
    assert own_detail["payments"] == own_detail["payment_evidence"] == []
    assert own_detail["payment_summary"] == {"paid": 0, "remaining": 20}


def test_paid_released_table_is_history_while_kitchen_is_pending(client, tenant, auth_headers):
    order = create_order(client, tenant, auth_headers, dine_in=True)
    sent = send_order(client, order, auth_headers)
    started = client.post(
        f"/api/v1/orders/{order['id']}/table-checkout/start",
        json={"expected_version": sent["order"]["version"]},
        headers={**auth_headers, "Idempotency-Key": "start"},
    )
    assert started.status_code == 200, started.text
    paid = client.post(
        f"/api/v1/orders/{order['id']}/table-checkout/pay",
        json={"expected_version": started.json()["order"]["version"], "payments": [{"method": "cash", "amount": 20, "register_id": tenant["register_id"]}]},
        headers={**auth_headers, "Idempotency-Key": "pay"},
    )
    assert paid.status_code == 200, paid.text
    assert paid.json()["order"]["status"] == "sent_to_kitchen"
    assert paid.json()["order"]["table_released_at"] is not None
    new_open = create_order(client, tenant, auth_headers, dine_in=True, key="next-occupant")
    result = workspace(client, tenant, auth_headers, period="all", view="table_history")
    assert [row["id"] for row in result["items"]] == [order["id"]]
    assert result["items"][0]["paid_amount"] == 20
    with SessionLocal() as db:
        assert db.get(KitchenTicket, sent["tickets"][0]["id"]).status == "queued"
        assert db.get(Order, new_open["id"]).table_released_at is None
        assert db.get(RestaurantTable, tenant["table_id"]).status == "occupied"


def test_detail_uses_ticket_snapshot_not_live_table_name_and_handles_unknown(client, tenant, auth_headers):
    order = create_order(client, tenant, auth_headers, dine_in=True)
    assert detail(client, order["id"], auth_headers)["table_context"] == {"table_id": tenant["table_id"], "table_name": None}
    sent = send_order(client, order, auth_headers)
    with SessionLocal.begin() as db:
        table = db.get(RestaurantTable, tenant["table_id"])
        table.name = "Renamed live table"
        table.archived_at = datetime.now(timezone.utc)
    payload = detail(client, order["id"], auth_headers)
    assert payload["table_context"].items() >= {"table_id": tenant["table_id"], "table_name": "Mesa 1", "area_name": "Salon"}.items()
    assert payload["cancellation_reason"] is None
    with SessionLocal.begin() as db:
        ticket = db.get(KitchenTicket, sent["tickets"][0]["id"])
        ticket.context_snapshot = {}
    assert detail(client, order["id"], auth_headers)["table_context"] == {"table_id": tenant["table_id"], "table_name": None}
    counter = create_order(client, tenant, auth_headers, key="counter")
    assert detail(client, counter["id"], auth_headers)["table_context"] == {"table_id": None, "table_name": None}


@pytest.mark.parametrize("status", ["closed", "cancelled", "sent_to_kitchen"])
def test_released_history_survives_table_archive_and_delete_with_snapshot_name(
    client, tenant, auth_headers, status,
):
    order = create_order(client, tenant, auth_headers, dine_in=True)
    sent = send_order(client, order, auth_headers)
    with SessionLocal.begin() as db:
        stored = db.get(Order, order["id"])
        stored.status = status
        stored.payment_status = "paid"
        stored.table_released_at = datetime.now(timezone.utc)
        db.add(Payment(
            business_id=tenant["business_id"], order_id=stored.id,
            method="cash", amount=20,
        ))
        table = db.get(RestaurantTable, tenant["table_id"])
        table.name = "Renamed live table"
        table.archived_at = datetime.now(timezone.utc)

    def assert_preserved(table_id):
        for period in ("day", "all"):
            history = workspace(client, tenant, auth_headers, period=period, view="table_history")
            assert history["total"] == 1
            assert history["items"][0]["id"] == order["id"]
            assert history["items"][0]["status"] == status
            assert history["items"][0]["paid_amount"] == 20
        payload = detail(client, order["id"], auth_headers)
        assert payload["order"]["table_id"] == table_id
        assert payload["order"]["table_released_at"] is not None
        assert payload["table_context"].items() >= {"table_id": tenant["table_id"], "table_name": "Mesa 1", "area_name": "Salon"}.items()
        assert payload["tickets"][0]["id"] == sent["tickets"][0]["id"]
        assert payload["tickets"][0]["table_name"] == "Mesa 1"
        assert payload["payment_summary"] == {"paid": 20, "remaining": 0}

    assert_preserved(tenant["table_id"])
    # Exercise the actual FK action without changing the shared SQLite fixture.
    with engine.connect() as connection:
        foreign_keys = connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
        try:
            connection.exec_driver_sql("PRAGMA foreign_keys = ON")
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            connection.execute(delete(RestaurantTable).where(RestaurantTable.id == tenant["table_id"]))
            connection.commit()
        finally:
            connection.rollback()
            connection.exec_driver_sql("PRAGMA foreign_keys = ON" if foreign_keys else "PRAGMA foreign_keys = OFF")
    with SessionLocal() as db:
        assert db.get(RestaurantTable, tenant["table_id"]) is None
        assert db.get(Order, order["id"]).table_id is None
    assert_preserved(None)


def financial_snapshot(db, order_id, session_id):
    payments = [dict(row) for row in db.execute(select(Payment.__table__).where(Payment.order_id == order_id)).mappings()]
    return {
        "payments": payments,
        "allocations": [dict(row) for row in db.execute(select(PaymentAllocation.__table__).where(PaymentAllocation.payment_id.in_([row["id"] for row in payments]))).mappings()],
        "session": dict(db.execute(select(CashSession.__table__).where(CashSession.id == session_id)).mappings().one()),
        "movements": [dict(row) for row in db.execute(select(CashMovement.__table__).where(CashMovement.cash_session_id == session_id)).mappings()],
    }


@pytest.mark.parametrize("amount", [7, 20])
def test_cancel_reason_keeps_money_and_same_status_version_event_contract(client, tenant, auth_headers, amount):
    order = create_order(client, tenant, auth_headers, dine_in=True)
    sent = send_order(client, order, auth_headers)
    paid = client.post(
        f"/api/v1/orders/{order['id']}/payments",
        json={"method": "cash", "amount": amount, "register_id": tenant["register_id"], "allocations": [{"order_item_id": order["items"][0]["id"], "amount": amount}]},
        headers={**auth_headers, "Idempotency-Key": "payment"},
    )
    assert paid.status_code == 201, paid.text
    session_id = paid.json()["payment"]["cash_session_id"]
    with SessionLocal() as db:
        finances = financial_snapshot(db, order["id"], session_id)
        payment_audit = db.query(AuditEvent).filter_by(
            action="payment.created", entity_type="payment",
            entity_id=str(paid.json()["payment"]["id"]),
            business_id=tenant["business_id"], branch_id=tenant["branch_id"],
        ).one()
        register_name_snapshot = payment_audit.payload["cash_register_name"]
        assert register_name_snapshot == "Caja"
    before = paid.json()["order"]
    cancelled = client.post(
        f"/api/v1/orders/{order['id']}/transition",
        json={"status": "cancelled", "expected_version": before["version"], "reason": "  Cliente cambio de planes \n "},
        headers=auth_headers,
    )
    assert cancelled.status_code == 200, cancelled.text
    current = cancelled.json()
    assert current["version"] == before["version"] + 1
    for key in ("total", "subtotal", "payment_status", "payment_method", "items", "folio", "number"):
        assert current[key] == before[key]
    current = detail(client, order["id"], auth_headers)["order"]
    for payload in (
        {"status": "cancelled", "reason": "Do not overwrite"},
        {"status": "cancelled", "expected_version": current["version"], "reason": "Also ignored"},
    ):
        replay = client.post(f"/api/v1/orders/{order['id']}/transition", json=payload, headers=auth_headers)
        assert replay.status_code == 200, replay.text
        assert replay.json() == current
    stale = client.post(
        f"/api/v1/orders/{order['id']}/transition",
        json={"status": "cancelled", "expected_version": before["version"], "reason": "Stale"}, headers=auth_headers,
    )
    assert stale.status_code == 409
    payload = detail(client, order["id"], auth_headers)
    assert payload["cancellation_reason"] == "Cliente cambio de planes"
    assert payload["payment_summary"] == {"paid": amount, "remaining": 20 - amount}
    assert payload["payments"][0]["cash_register_name"] == register_name_snapshot
    assert payload["payments"][0]["status"] == "confirmed"
    assert workspace(client, tenant, auth_headers, period="all", view="table_history")["items"][0]["paid_amount"] == amount
    with SessionLocal() as db:
        assert financial_snapshot(db, order["id"], session_id) == finances
        audits = db.query(AuditEvent).filter_by(action="order.cancelled", entity_type="order", entity_id=str(order["id"])).all()
        assert len(audits) == 1
        assert audits[0].branch_id == tenant["branch_id"]
        assert audits[0].payload["reason"] == "Cliente cambio de planes"
        assert audits[0].payload["snapshot_version"] == 1
        assert Decimal(audits[0].payload["paid_amount"]) == amount
        assert audits[0].payload["order"]["folio"] == order["folio"]
        events = db.query(IntegrationEvent).filter_by(event_type="order.cancelled", aggregate_id=str(order["id"])).all()
        assert len(events) == 1
        assert events[0].business_id == tenant["business_id"]
        assert events[0].branch_id == tenant["branch_id"]
        assert events[0].payload["status"] == "cancelled"
        assert "reason" not in events[0].payload
        assert db.get(InventoryItem, tenant["inventory_id"]).quantity == Decimal("10")
        assert db.get(KitchenTicket, sent["tickets"][0]["id"]).status == "cancelled"
        assert db.get(RestaurantTable, tenant["table_id"]).status == "available"
        assert db.query(StockMovement).filter_by(reference_type="order_reversal", reference_id=str(order["id"])).count() == 1


@pytest.mark.parametrize("reason_fields, expected", [({}, None), ({"reason": None}, None), ({"reason": " \n "}, None), ({"reason": " " + "x" * 1000 + " "}, "x" * 1000)])
def test_optional_cancellation_reason_is_backwards_compatible(client, tenant, auth_headers, reason_fields, expected):
    order = create_order(client, tenant, auth_headers, dine_in=True)
    response = client.post(f"/api/v1/orders/{order['id']}/transition", json={"status": "cancelled", **reason_fields}, headers=auth_headers)
    assert response.status_code == 200, response.text
    assert detail(client, order["id"], auth_headers)["cancellation_reason"] == expected


@pytest.mark.parametrize("reason", ["x" * 1001, 42, {"text": "reason"}])
def test_invalid_reason_does_not_cancel(client, tenant, auth_headers, reason):
    order = create_order(client, tenant, auth_headers)
    before = detail(client, order["id"], auth_headers)["order"]
    response = client.post(f"/api/v1/orders/{order['id']}/transition", json={"status": "cancelled", "reason": reason}, headers=auth_headers)
    assert response.status_code == 422
    assert detail(client, order["id"], auth_headers)["order"] == before
    with SessionLocal() as db:
        assert db.query(AuditEvent).filter_by(action="order.cancelled").count() == 0
        assert db.query(IntegrationEvent).filter_by(event_type="order.cancelled").count() == 0


def test_cancellation_reason_and_event_roll_back_with_the_operation(client, tenant, auth_headers, monkeypatch):
    order = create_order(client, tenant, auth_headers, dine_in=True)
    sent = send_order(client, order, auth_headers)
    before = detail(client, order["id"], auth_headers)["order"]
    original_create = api_module.create_integration_event

    def fail_after_event(*args, **kwargs):
        original_create(*args, **kwargs)
        raise RuntimeError("Simulated event failure")

    monkeypatch.setattr(api_module, "create_integration_event", fail_after_event)
    with pytest.raises(RuntimeError, match="Simulated event failure"):
        client.post(
            f"/api/v1/orders/{order['id']}/transition",
            json={"status": "cancelled", "expected_version": sent["order"]["version"], "reason": "Must roll back"}, headers=auth_headers,
        )
    current = detail(client, order["id"], auth_headers)
    assert current["order"] == before
    assert current["cancellation_reason"] is None
    with SessionLocal() as db:
        assert db.get(InventoryItem, tenant["inventory_id"]).quantity == Decimal("9.5")
        assert db.get(KitchenTicket, sent["tickets"][0]["id"]).status == "queued"
        assert db.get(RestaurantTable, tenant["table_id"]).status == "occupied"
        assert db.query(AuditEvent).filter_by(action="order.cancelled").count() == 0
        assert db.query(IntegrationEvent).filter_by(event_type="order.cancelled").count() == 0


def test_detail_scopes_audit_and_ticket_context_and_breaks_snapshot_ties(client, tenant, auth_headers):
    stamp = datetime(2024, 1, 1, tzinfo=timezone.utc)
    with SessionLocal.begin() as db:
        sibling = Branch(business_id=tenant["business_id"], slug="second", name="Second")
        db.add(sibling)
        db.flush()
        order = seed_order(db, tenant, status="cancelled")
        for sequence, (business_id, branch_id, table_id, name) in enumerate((
            (tenant["business_id"], tenant["branch_id"], tenant["table_id"], "Older snapshot"),
            (tenant["business_id"], tenant["branch_id"], tenant["table_id"], "Latest snapshot"),
            (tenant["business_id"], tenant["branch_id"], 999, "Unrelated table"),
            (tenant["business_id"], sibling.id, tenant["table_id"], "Foreign branch"),
            (tenant["other_business_id"], tenant["other_branch_id"], tenant["table_id"], "Foreign business"),
        ), start=1):
            db.add(KitchenTicket(
                order_id=order.id, business_id=business_id, branch_id=branch_id,
                sequence=sequence, fired_at=stamp,
                context_snapshot={"table_id": table_id, "table_name": name},
            ))
        for business_id, branch_id, entity_type, entity_id, action, reason in (
            (tenant["business_id"], None, "order", str(order.id), "order.cancelled", "Older reason"),
            (tenant["business_id"], None, "order", str(order.id), "order.cancelled", " Legacy reason "),
            (tenant["business_id"], sibling.id, "order", str(order.id), "order.cancelled", "Wrong branch"),
            (tenant["other_business_id"], None, "order", str(order.id), "order.cancelled", "Wrong business"),
            (tenant["business_id"], tenant["branch_id"], "payment", str(order.id), "order.cancelled", "Wrong type"),
            (tenant["business_id"], tenant["branch_id"], "order", "99999", "order.cancelled", "Wrong order"),
            (tenant["business_id"], tenant["branch_id"], "order", str(order.id), "order.updated", "Wrong action"),
        ):
            db.add(AuditEvent(
                business_id=business_id, branch_id=branch_id, entity_type=entity_type,
                entity_id=entity_id, action=action, payload={"reason": reason}, created_at=stamp,
            ))
            db.flush()
    payload = detail(client, order.id, auth_headers)
    assert payload["table_context"] == {"table_id": tenant["table_id"], "table_name": "Latest snapshot"}
    assert len(payload["tickets"]) == 3
    assert payload["cancellation_reason"] == "Legacy reason"
    with SessionLocal.begin() as db:
        db.add(AuditEvent(
            business_id=tenant["business_id"], branch_id=tenant["branch_id"],
            entity_type="order", entity_id=str(order.id), action="order.cancelled", payload={},
        ))
    assert detail(client, order.id, auth_headers)["cancellation_reason"] is None


def test_detail_payments_without_audit_have_no_register_name_and_totals_only_confirmed(client, tenant, auth_headers):
    with SessionLocal.begin() as db:
        sibling = Branch(business_id=tenant["business_id"], slug="second", name="Second")
        db.add(sibling)
        db.flush()
        other_register = CashRegister(business_id=tenant["other_business_id"], branch_id=tenant["other_branch_id"], name="Foreign business register")
        sibling_register = CashRegister(business_id=tenant["business_id"], branch_id=sibling.id, name="Foreign branch register")
        db.add_all([other_register, sibling_register])
        db.flush()
        order = seed_order(db, tenant, status="cancelled")
        sessions = []
        for business_id, branch_id, register_id in (
            (tenant["business_id"], tenant["branch_id"], tenant["register_id"]),
            (tenant["other_business_id"], tenant["other_branch_id"], other_register.id),
            (tenant["business_id"], sibling.id, sibling_register.id),
            (tenant["business_id"], tenant["branch_id"], other_register.id),
            (tenant["business_id"], tenant["branch_id"], sibling_register.id),
            (tenant["other_business_id"], tenant["branch_id"], tenant["register_id"]),
        ):
            session = CashSession(business_id=business_id, branch_id=branch_id, register_id=register_id, status="closed", opened_by="test")
            db.add(session)
            db.flush()
            sessions.append(session)
        register = db.get(CashRegister, tenant["register_id"])
        register.active = False
        register.archived_at = datetime.now(timezone.utc)
        for session in sessions:
            db.add(Payment(business_id=tenant["business_id"], order_id=order.id, cash_session_id=session.id, method="cash", amount=1))
        db.add(Payment(business_id=tenant["business_id"], order_id=order.id, method="cash", amount=2))
        for status in ("pending", "cancelled", "refunded"):
            db.add(Payment(business_id=tenant["business_id"], order_id=order.id, method="cash", status=status, amount=100))
        db.add(Payment(business_id=tenant["other_business_id"], order_id=order.id, cash_session_id=sessions[0].id, method="cash", amount=999))
    payload = detail(client, order.id, auth_headers)
    assert len(payload["payments"]) == 10
    for payment in payload["payments"]:
        assert payment["cash_register_name"] is None
    assert payload["payment_summary"] == {"paid": 8, "remaining": 12}
    result = workspace(client, tenant, auth_headers, period="all", view="table_history")
    assert result["items"][0]["paid_amount"] == 8


def test_real_payment_register_snapshot_survives_rename_and_ignores_unscoped_audits(
    client, tenant, auth_headers,
):
    order = create_order(client, tenant, auth_headers)
    paid = client.post(
        f"/api/v1/orders/{order['id']}/payments",
        json={"method": "cash", "amount": 20, "register_id": tenant["register_id"]},
        headers={**auth_headers, "Idempotency-Key": "snapshot-payment"},
    )
    assert paid.status_code == 201, paid.text
    payment = paid.json()["payment"]
    with SessionLocal.begin() as db:
        snapshot = db.query(AuditEvent).filter_by(
            business_id=tenant["business_id"], branch_id=tenant["branch_id"],
            action="payment.created", entity_type="payment", entity_id=str(payment["id"]),
        ).one()
        snapshot_id = snapshot.id
        expected_snapshot = {
            "order_id": order["id"],
            "cash_session_id": payment["cash_session_id"],
            "cash_register_id": tenant["register_id"],
            "cash_register_name": "Caja",
        }
        assert {key: snapshot.payload[key] for key in expected_snapshot} == expected_snapshot
        db.get(CashRegister, tenant["register_id"]).name = "Renamed live register"
        sibling = Branch(business_id=tenant["business_id"], slug="snapshot-sibling", name="Sibling")
        db.add(sibling)
        db.flush()
        base_audit = {
            "business_id": tenant["business_id"],
            "branch_id": tenant["branch_id"],
            "action": "payment.created",
            "entity_type": "payment",
            "entity_id": str(payment["id"]),
            "created_at": snapshot.created_at + timedelta(days=1),
        }
        foreign_payload = {**expected_snapshot, "cash_register_name": "Unrelated audit name"}
        for overrides, payload in (
            ({"business_id": tenant["other_business_id"]}, foreign_payload),
            ({"branch_id": sibling.id}, foreign_payload),
            ({"entity_type": "order"}, foreign_payload),
            ({"entity_id": str(payment["id"] + 1000)}, foreign_payload),
            ({"action": "payment.updated"}, foreign_payload),
            ({}, {**foreign_payload, "order_id": order["id"] + 1000}),
            ({}, {key: value for key, value in foreign_payload.items() if key != "order_id"}),
        ):
            db.add(AuditEvent(**{**base_audit, **overrides, "payload": payload}))

    payload = detail(client, order["id"], auth_headers)
    assert len(payload["payments"]) == 1
    assert payload["payments"][0]["id"] == payment["id"]
    assert payload["payments"][0]["cash_register_name"] == "Caja"
    assert payload["payment_summary"] == {"paid": 20, "remaining": 0}
    with SessionLocal.begin() as db:
        db.get(Payment, payment["id"]).cash_session_id = None
    without_session = detail(client, order["id"], auth_headers)
    assert without_session["payments"][0]["cash_session_id"] is None
    assert without_session["payments"][0]["cash_register_name"] == "Caja"
    with SessionLocal.begin() as db:
        db.execute(delete(AuditEvent).where(AuditEvent.id == snapshot_id))
    without_snapshot = detail(client, order["id"], auth_headers)
    assert without_snapshot["payments"][0]["cash_register_name"] is None
    assert without_snapshot["payment_summary"] == payload["payment_summary"]
    with SessionLocal.begin() as db:
        db.add(AuditEvent(
            business_id=tenant["business_id"], branch_id=None,
            action="payment.created", entity_type="payment", entity_id=str(payment["id"]),
            payload={**expected_snapshot, "cash_register_name": "Legacy saved name"},
        ))
    assert detail(client, order["id"], auth_headers)["payments"][0]["cash_register_name"] == "Legacy saved name"
    with SessionLocal() as db:
        assert db.get(CashRegister, tenant["register_id"]).name == "Renamed live register"


def test_reason_does_not_change_non_cancellation_transitions(client, tenant, auth_headers):
    order = create_order(client, tenant, auth_headers)
    sent = send_order(client, order, auth_headers)
    response = client.post(
        f"/api/v1/orders/{order['id']}/transition",
        json={"status": "preparing", "expected_version": sent["order"]["version"], "reason": "Not a cancellation reason"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "preparing"
    assert detail(client, order["id"], auth_headers)["cancellation_reason"] is None
    with SessionLocal() as db:
        event = db.query(AuditEvent).filter_by(action="order.preparing").one()
        assert event.payload == {}
        assert db.query(AuditEvent).filter_by(action="order.cancelled").count() == 0
