from copy import deepcopy
from uuid import uuid4

import pytest

from app.database import SessionLocal
from app.models import KitchenTicket, Order, PrintJob, RestaurantTable
from app.settings_service import kitchen_print_snapshot
from test_command_workflow import create_order, send_order
from test_order_folios_revisions import edit, revise


def queue_print(sent, status="pending"):
    with SessionLocal.begin() as db:
        order = db.get(Order, sent["order"]["id"])
        ticket = db.get(KitchenTicket, sent["tickets"][0]["id"])
        job = PrintJob(
            id=str(uuid4()), business_id=order.business_id, branch_id=order.branch_id,
            order_id=order.id, kitchen_ticket_id=ticket.id, job_type="kitchen_ticket",
            payload={"ticket": kitchen_print_snapshot(order, ticket), "copies": 2},
            idempotency_key=str(uuid4()), status=status,
        )
        db.add(job)
        return job.id, deepcopy(job.payload)


def test_revision_refreshes_pending_print_but_preserves_claimed_snapshot(client, tenant, auth_headers):
    sent = send_order(client, create_order(client, tenant, auth_headers), auth_headers)
    pending_id, _ = queue_print(sent)
    claimed_id, claimed_payload = queue_print(sent, "claimed")
    device = client.post(
        "/api/v1/settings/devices", headers={**auth_headers, "Idempotency-Key": "review-device"},
        json={"branch_id": tenant["branch_id"], "name": "Isolated kitchen"},
    ).json()
    paired = client.post(
        "/api/v1/settings/devices/pair", headers={"Idempotency-Key": "review-device-pair"},
        json={"pairing_code": device["pairing_code"]},
    ).json()
    with SessionLocal.begin() as db:
        db.get(PrintJob, pending_id).paired_device_id = device["id"]
    result = revise(client, auth_headers, sent["order"], [
        edit(sent["order"]["items"][0]["id"], tenant["product_id"], 2, "Sin sal"),
    ], "revise-pending-print")
    assert result.status_code == 201, result.text
    with SessionLocal() as db:
        pending = db.get(PrintJob, pending_id)
        assert pending.status == "pending" and pending.payload["copies"] == 2
        assert pending.payload["ticket"]["version"] == 2
        assert pending.payload["ticket"]["items"][0]["quantity"] == 2
        assert pending.payload["ticket"]["items"][0]["notes"] == "Sin sal"
        assert db.get(PrintJob, claimed_id).payload == claimed_payload
        assert db.query(PrintJob).count() == 2
    cancelled = client.post(
        f"/api/v1/orders/{sent['order']['id']}/transition",
        headers=auth_headers,
        json={"status": "cancelled", "expected_version": result.json()["order"]["version"]},
    )
    assert cancelled.status_code == 200, cancelled.text
    with SessionLocal() as db:
        assert db.get(PrintJob, pending_id).status == "cancelled"
        assert db.get(PrintJob, claimed_id).status == "claimed"
        assert db.query(PrintJob).count() == 2
    claim = client.post(
        "/api/v1/settings/devices/print-jobs/claim",
        headers={"X-Device-Token": paired["device_token"], "Idempotency-Key": "claim-after-cancel"},
    )
    assert claim.status_code == 200 and claim.json()["job"] is None


def test_cancellation_also_retires_unclaimed_print_of_completed_ticket(client, tenant, auth_headers):
    sent = send_order(client, create_order(client, tenant, auth_headers), auth_headers)
    pending_id, _ = queue_print(sent)
    other = send_order(client, create_order(client, tenant, auth_headers, key="other-order"), auth_headers, key="other-send")
    other_job_id, _ = queue_print(other)
    completed = client.post(
        f"/api/v1/kitchen/commands/{sent['tickets'][0]['id']}/complete",
        headers={**auth_headers, "Idempotency-Key": "complete-before-cancel"},
        json={"expected_status": "queued", "expected_version": 1},
    )
    assert completed.status_code == 200, completed.text
    cancelled = client.post(
        f"/api/v1/orders/{sent['order']['id']}/transition",
        headers=auth_headers,
        json={"status": "cancelled", "expected_version": completed.json()["order"]["version"]},
    )
    assert cancelled.status_code == 200, cancelled.text
    with SessionLocal() as db:
        assert db.get(PrintJob, pending_id).status == "cancelled"
        assert db.get(PrintJob, other_job_id).status == "pending"
        assert db.get(KitchenTicket, sent["tickets"][0]["id"]).status == "ready"


@pytest.mark.parametrize("released_marker", [True, False])
def test_cancelling_old_order_does_not_release_reused_table(client, tenant, auth_headers, released_marker):
    sent = send_order(client, create_order(client, tenant, auth_headers, dine_in=True), auth_headers)
    started = client.post(
        f"/api/v1/orders/{sent['order']['id']}/table-checkout/start",
        headers={**auth_headers, "Idempotency-Key": "old-checkout"},
        json={"expected_version": sent["order"]["version"]},
    )
    assert started.status_code == 200, started.text
    cash = client.post("/api/v1/cash/sessions/open", headers=auth_headers,
                       json={"register_id": tenant["register_id"], "opening_amount": 100})
    assert cash.status_code == 201, cash.text
    paid = client.post(
        f"/api/v1/orders/{sent['order']['id']}/table-checkout/pay",
        headers={**auth_headers, "Idempotency-Key": "old-pay"},
        json={"expected_version": started.json()["order"]["version"],
              "payments": [{"method": "cash", "amount": 20, "cash_session_id": cash.json()["id"]}]},
    )
    assert paid.status_code == 200, paid.text
    current = create_order(client, tenant, auth_headers, dine_in=True, key="new-occupant")
    with SessionLocal.begin() as db:
        table = db.get(RestaurantTable, tenant["table_id"])
        before_status, before_version = table.status, table.version
        if not released_marker:
            # Historical inconsistency must not release a different active account.
            db.get(Order, sent["order"]["id"]).table_released_at = None
    cancelled = client.post(
        f"/api/v1/orders/{sent['order']['id']}/transition",
        headers=auth_headers,
        json={"status": "cancelled", "expected_version": paid.json()["order"]["version"]},
    )
    assert cancelled.status_code == 200, cancelled.text
    with SessionLocal() as db:
        table = db.get(RestaurantTable, tenant["table_id"])
        assert table.status == before_status == "occupied"
        assert table.version == before_version
        assert db.get(Order, current["id"]).status != "cancelled"
    tables = client.get(f"/api/v1/tables?branch_id={tenant['branch_id']}", headers=auth_headers)
    assert next(table for table in tables.json() if table["id"] == tenant["table_id"])["active_order_id"] == current["id"]
