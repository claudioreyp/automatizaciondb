"""Manual delivery entry by staff is independent of the online ordering hours."""
from datetime import datetime, time, timezone
from decimal import Decimal

import pytest

from app.database import SessionLocal
from app.models import (BranchSettings, InventoryItem, KitchenTicket, Order, Payment,
                        ScheduleAssignment, ScheduleShift, ServiceSchedule)
from test_escalar_integrations import create_credential, integration_headers
from test_pos_manual_delivery import configure, create, no_external_calls, send


@pytest.fixture
def closed_hours(tenant, monkeypatch):
    from app import settings_service
    monkeypatch.setattr(settings_service, "utcnow", lambda: datetime(2026, 11, 2, 20, tzinfo=timezone.utc))
    configure(tenant)
    with SessionLocal.begin() as db:
        schedule = ServiceSchedule(business_id=tenant["business_id"], branch_id=tenant["branch_id"],
                                   name="Principal de prueba", kind="primary", active=True)
        db.add(schedule)
        db.flush()
        db.add(ScheduleShift(schedule_id=schedule.id, day_of_week=0, starts_at=time(12), ends_at=time(14)))


@pytest.mark.parametrize("fee,pending", [(0, True), (0, False), (5, False)])
def test_manual_delivery_outside_primary_hours_saves_and_prepares_once(
    client, tenant, auth_headers, closed_hours, fee, pending,
):
    values = {"delivery_fee": fee, "allow_pending_delivery_quote": pending}
    first = create(client, tenant, auth_headers, **values)
    assert first.status_code == 201, first.text
    order = first.json()
    assert order["source"] == "pos" and order["delivery_address"] is None
    assert order["delivery_fee_status"] == ("pending_quote" if pending else "final")
    assert order["total"] == 20 + fee and order["payment_status"] == "pending"
    replay = create(client, tenant, auth_headers, **values)
    assert replay.status_code == 201 and replay.json() == order
    prepared = send(client, auth_headers, order)
    assert send(client, auth_headers, order) == prepared
    with SessionLocal() as db:
        assert db.query(Order).count() == db.query(KitchenTicket).count() == 1
        assert db.query(Payment).count() == 0
        assert db.get(InventoryItem, tenant["inventory_id"]).quantity == Decimal("9.5")


@pytest.mark.parametrize("channel", ["takeaway", "counter", "dine_in"])
def test_other_pos_channels_keep_primary_schedule_control(client, tenant, auth_headers, closed_hours, channel):
    result = create(client, tenant, auth_headers, channel=channel, allow_pending_delivery_quote=False,
                    table_id=tenant["table_id"] if channel == "dine_in" else None)
    assert result.status_code == 409 and result.json()["code"] == "BRANCH_CLOSED", result.text
    with SessionLocal() as db:
        assert db.query(Order).count() == db.query(KitchenTicket).count() == db.query(Payment).count() == 0


def test_integration_cannot_impersonate_manual_pos_to_bypass_closed_hours(client, tenant, auth_headers, closed_hours):
    with SessionLocal.begin() as db:
        db.query(BranchSettings).filter_by(branch_id=tenant["branch_id"]).one().digital_delivery = True
    credential = create_credential(client, tenant, auth_headers, ["orders:write", "orders:read"])
    result = client.post("/api/v1/integrations/orders/draft", headers=integration_headers(credential["token"], "closed-integration"),
        json={"branch_id": tenant["branch_id"], "channel": "delivery", "source": "pos",
              "customer_name": "Ana", "customer_phone": "+51912345678", "payment_method": "cash",
              "delivery_fee": 5, "delivery_address": {"address": "Calle de prueba 123", "reference": "Puerta azul"},
              "items": [{"product_id": tenant["product_id"], "quantity": 1}]})
    assert result.status_code == 409 and result.json()["code"] == "BRANCH_CLOSED", result.text
    with SessionLocal() as db:
        assert db.query(Order).count() == 0


def test_manual_delivery_still_requires_enabled_service(client, tenant, auth_headers, closed_hours):
    with SessionLocal.begin() as db:
        db.query(BranchSettings).filter_by(branch_id=tenant["branch_id"]).one().pos_delivery = False
    result = create(client, tenant, auth_headers)
    assert result.status_code == 409 and result.json()["code"] == "SERVICE_DISABLED", result.text


def test_manual_delivery_keeps_product_specific_hours(client, tenant, auth_headers, closed_hours):
    with SessionLocal.begin() as db:
        schedule = ServiceSchedule(business_id=tenant["business_id"], branch_id=tenant["branch_id"],
                                   name="Producto de prueba", kind="additional", active=True)
        db.add(schedule)
        db.flush()
        db.add(ScheduleShift(schedule_id=schedule.id, day_of_week=0, starts_at=time(12), ends_at=time(14)))
        db.add(ScheduleAssignment(business_id=tenant["business_id"], branch_id=tenant["branch_id"],
                                 schedule_id=schedule.id, product_id=tenant["product_id"]))
    result = create(client, tenant, auth_headers)
    assert result.status_code == 409 and result.json()["code"] == "PRODUCT_SCHEDULE_CLOSED", result.text
    with SessionLocal() as db:
        assert db.query(Order).count() == db.query(KitchenTicket).count() == db.query(Payment).count() == 0
