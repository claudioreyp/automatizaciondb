"""Agent printing contract against the isolated API; never performs printer I/O."""
from copy import deepcopy

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app import api, pos_printing
from app.database import SessionLocal
from app.models import BranchSettings, KitchenTicket, Order, Payment, PrintJob
from test_agent_domain_revision import no_external_effects, setup_order, receipt, approve, current, addition
from test_agent_transport_profile import agent_headers, profile_claim, draft
from test_pos_printing import enable, jobs, claim, complete, claim_body


def cash_confirm(client, headers, order, key="cash-confirm"):
    return client.post(f"/api/v1/integrations/orders/{order['id']}/cash-confirm",
        headers={**headers, "Idempotency-Key": key})


def new_order(client, tenant, auth, *, method="cash", overrides=None, source="whatsapp_agent"):
    # The shared helper creates a Yape draft without sending it. Set the desired
    # method before any confirmation, then enable the existing branch printer.
    headers, order = setup_order(client, tenant, auth, method="yape")
    with SessionLocal.begin() as db:
        row = db.get(Order, order["id"])
        row.payment_method, row.source = method, source
    enable(tenant, **(overrides or {}))
    return headers, order


def pending(client, order, auth):
    return [job for job in jobs(client, order, auth)["items"] if job["status"] == "pending"]


def mark_sent(client, rows, auth):
    for job in rows:
        body = claim_body()
        claimed = claim(client, job, auth, body)
        assert claimed.status_code == 200 and claimed.json()["dispatch_allowed"], claimed.text
        done = complete(client, job, auth, body, "printed")
        assert done.status_code == 200, done.text


@pytest.mark.parametrize("source", ["whatsapp_agent", "whatsapp", "agent", "n8n"])
def test_cash_agent_initial_prints_receipt_and_kitchen_once(client, tenant, auth_headers, source):
    headers, order = new_order(client, tenant, auth_headers, source=source)
    assert jobs(client, order, auth_headers)["items"] == []
    confirmed = cash_confirm(client, headers, order)
    assert confirmed.status_code == 200, confirmed.text
    rows = pending(client, order, auth_headers)
    assert len(rows) == 2 and {row["job_type"] for row in rows} == {"customer_receipt", "kitchen_ticket"}
    assert all(row["payload"]["order"]["payment_status"] == "pending" for row in rows)
    assert all(row["payload"]["paid_amount"] == 0 for row in rows)
    assert cash_confirm(client, headers, order).json() == confirmed.json()
    assert cash_confirm(client, headers, order, "different-key").status_code == 409
    assert jobs(client, order, auth_headers)["items"] == rows
    with SessionLocal() as db:
        assert db.query(Payment).count() == 0
        assert db.query(PrintJob).count() == 2


def test_restricted_profile_uses_verified_contact_in_same_print_contract(client, tenant, agent_headers, auth_headers):
    enable(tenant)
    lease = profile_claim(client, agent_headers, "verified-contact", contact_display_name="Ana WhatsApp")
    created = draft(client, tenant, agent_headers, lease, "draft")
    assert created.status_code == 201, created.text
    headers = {**agent_headers, "X-Agent-Conversation": lease["conversation_id"], "X-Agent-Lease": lease["lease_token"]}
    confirmed = cash_confirm(client, headers, created.json())
    assert confirmed.status_code == 200, confirmed.text
    rows = pending(client, created.json(), auth_headers)
    assert len(rows) == 2
    assert all(job["payload"]["order"]["customer_name"] == "Ana WhatsApp" for job in rows)
    assert all(job["payload"]["order"]["source"] == "whatsapp_agent" for job in rows)


@pytest.mark.parametrize("method", ["cash", "yape"])
def test_addition_prints_only_new_kitchen_after_own_payment_gate(client, tenant, auth_headers, method):
    headers, order = new_order(client, tenant, auth_headers)
    assert cash_confirm(client, headers, order).status_code == 200
    original = pending(client, order, auth_headers)
    mark_sent(client, original, auth_headers)
    order = current(client, headers, order)
    requested = addition(client, headers, order, method)
    assert requested.status_code == 200, requested.text
    assert addition(client, headers, order, method).json() == requested.json()
    if method == "yape":
        assert pending(client, order, auth_headers) == []
        uploaded = receipt(client, headers, order, "additional", requested.json()["payment_request"]["id"])
        assert pending(client, order, auth_headers) == []
        reviewed = approve(client, auth_headers, tenant, uploaded)
        assert reviewed.status_code == 200, reviewed.text
    rows = pending(client, order, auth_headers)
    assert len(rows) == 1 and rows[0]["job_type"] == "kitchen_ticket"
    assert rows[0]["payload"]["ticket"]["sequence"] == 2
    assert len(rows[0]["payload"]["ticket"]["items"]) == 1
    with SessionLocal() as db:
        assert db.query(PrintJob).count() == 3
        assert db.query(PrintJob).filter_by(job_type="customer_receipt").count() == 1
        assert db.query(Order).count() == 1


def test_yape_initial_waits_for_human_and_replay_never_duplicates(client, tenant, auth_headers):
    headers, order = new_order(client, tenant, auth_headers, method="yape")
    uploaded = receipt(client, headers, order)
    assert jobs(client, order, auth_headers)["items"] == []
    body = {"approve": True, "register_id": tenant["register_id"], "expected_version": uploaded["order"]["version"]}
    path = f"/api/v1/payment-evidence/{uploaded['evidence']['id']}/review"
    request_headers = {**auth_headers, "Idempotency-Key": "approve-one"}
    reviewed = client.post(path, json=body, headers=request_headers)
    assert reviewed.status_code == 200, reviewed.text
    rows = pending(client, order, auth_headers)
    assert len(rows) == 2 and {row["job_type"] for row in rows} == {"customer_receipt", "kitchen_ticket"}
    assert all(row["payload"]["paid_amount"] == 20 for row in rows)
    assert client.post(path, json=body, headers=request_headers).json() == reviewed.json()
    assert approve(client, auth_headers, tenant, uploaded).status_code == 200
    assert jobs(client, order, auth_headers)["items"] == rows
    with SessionLocal() as db:
        assert db.query(Payment).count() == 1 and db.query(KitchenTicket).count() == 1


@pytest.mark.parametrize("method", ["cash", "yape"])
def test_pending_base_holds_all_printing_then_releases_each_eligible_batch(client, tenant, auth_headers, method):
    headers, order = new_order(client, tenant, auth_headers, method="yape")
    base = receipt(client, headers, order)
    order = current(client, headers, order)
    requested = addition(client, headers, order, method).json()
    assert requested["payment_request"]["status"] == "awaiting_base_review"
    extra = receipt(client, headers, order, "extra", requested["payment_request"]["id"]) if method == "yape" else None
    if extra:
        assert approve(client, auth_headers, tenant, extra).status_code == 409
    assert jobs(client, order, auth_headers)["items"] == []
    reviewed = approve(client, auth_headers, tenant, base)
    assert reviewed.status_code == 200, reviewed.text
    rows = pending(client, order, auth_headers)
    assert len(rows) == (3 if method == "cash" else 2)
    assert sum(row["job_type"] == "customer_receipt" for row in rows) == 1
    if extra:
        mark_sent(client, rows, auth_headers)
        assert approve(client, auth_headers, tenant, extra).status_code == 200
        rows = pending(client, order, auth_headers)
        assert len(rows) == 1 and rows[0]["job_type"] == "kitchen_ticket"
    with SessionLocal() as db:
        assert db.query(PrintJob).count() == 3 and db.query(KitchenTicket).count() == 2


@pytest.mark.parametrize("overrides,types", [
    ({"automatic_printing": False}, set()),
    ({"auto_print_kitchen": False}, {"customer_receipt"}),
    ({"manual_customer_receipt": True}, {"kitchen_ticket"}),
    ({"printer_name": ""}, set()),
])
def test_agent_preserves_existing_branch_print_preferences(client, tenant, auth_headers, overrides, types):
    headers, order = new_order(client, tenant, auth_headers, overrides=overrides)
    assert cash_confirm(client, headers, order).status_code == 200
    assert {row["job_type"] for row in pending(client, order, auth_headers)} == types


def test_failed_transaction_leaves_no_agent_printing_work(client, tenant, auth_headers, monkeypatch):
    headers, order = new_order(client, tenant, auth_headers)
    original = api.save_idempotent_response
    def fail(db, scope, *args):
        if scope.startswith("integration-cash-confirm:"):
            raise HTTPException(503, "Simulated transaction failure")
        return original(db, scope, *args)
    monkeypatch.setattr(api, "save_idempotent_response", fail)
    assert cash_confirm(client, headers, order).status_code == 503
    with SessionLocal() as db:
        assert db.query(PrintJob).count() == db.query(KitchenTicket).count() == 0
        assert db.get(Order, order["id"]).status == "draft"
    monkeypatch.setattr(api, "save_idempotent_response", original)
    assert cash_confirm(client, headers, order).status_code == 200
    assert len(pending(client, order, auth_headers)) == 2


def test_agent_queue_recovery_preserves_original_intent_and_no_backfill(client, tenant, auth_headers, monkeypatch):
    headers, order = new_order(client, tenant, auth_headers)
    original = pos_printing._insert_jobs
    def fail(*args):
        original(*args)
        raise RuntimeError("queue insert unavailable")
    monkeypatch.setattr(pos_printing, "_insert_jobs", fail)
    assert cash_confirm(client, headers, order).status_code == 200
    assert jobs(client, order, auth_headers)["recoverable_error"] is True
    with SessionLocal.begin() as db:
        settings = db.scalar(select(BranchSettings))
        settings.printer_config = {"printer_name": "Changed later", "automatic_printing": False}
        assert db.query(PrintJob).count() == 0
    monkeypatch.setattr(pos_printing, "_insert_jobs", original)
    rows = pending(client, order, auth_headers)
    assert len(rows) == 2 and all(row["payload"]["printer_name"] == "POS-80" for row in rows)


def test_other_integration_source_does_not_gain_pos_printing(client, tenant, auth_headers):
    headers, order = new_order(client, tenant, auth_headers, source="integration")
    assert cash_confirm(client, headers, order).status_code == 200
    assert jobs(client, order, auth_headers)["items"] == []


def test_pending_discovery_is_scoped_readonly_and_paginated(client, tenant, auth_headers):
    headers, first = new_order(client, tenant, auth_headers)
    created = []
    for index in range(3):
        if index == 0:
            order = first
        else:
            response = client.post("/api/v1/integrations/orders/draft", headers={**headers, "Idempotency-Key": f"draft-{index}"},
                json={"branch_id": tenant["branch_id"], "source": "whatsapp_agent", "channel": "takeaway",
                    "payment_method": "cash", "customer_phone": "51912345678",
                    "items": [{"product_id": tenant["product_id"], "quantity": 1}]})
            assert response.status_code == 201, response.text
            order = response.json()
        assert cash_confirm(client, headers, order, f"cash-{index}").status_code == 200
        created.append(order)
    path = f"/api/v1/orders/printing/pending?branch_id={tenant['branch_id']}&limit=2"
    before = [jobs(client, order, auth_headers) for order in created]
    page = client.get(path, headers=auth_headers)
    assert page.status_code == 200, page.text
    assert page.headers["cache-control"] == "no-store"
    assert [row["order_id"] for row in page.json()["items"]] == [row["id"] for row in created[:2]]
    after = page.json()["next_after_order_id"]
    last = client.get(path + f"&after_order_id={after}", headers=auth_headers).json()
    assert [row["order_id"] for row in last["items"]] == [created[2]["id"]]
    assert last["next_after_order_id"] is None
    assert [jobs(client, order, auth_headers) for order in created] == before
    assert client.get(f"/api/v1/orders/printing/pending?branch_id={tenant['other_branch_id']}", headers=auth_headers).status_code == 403
    for role in ["kitchen", "dispatcher"]:
        assert client.get(path, headers={**auth_headers, "X-Dev-Role": role}).status_code == 403
    mark_sent(client, pending(client, created[0], auth_headers), auth_headers)
    assert created[0]["id"] not in [row["order_id"] for row in client.get(path, headers=auth_headers).json()["items"]]


def test_pending_discovery_cannot_be_used_by_agent_or_make_historical_work(client, tenant, auth_headers, agent_headers):
    headers, order = setup_order(client, tenant, auth_headers, method="cash")
    enable(tenant)
    path = f"/api/v1/orders/printing/pending?branch_id={tenant['branch_id']}"
    # This staff endpoint never accepts integration auth; isolated Auth is not configured.
    denied = client.get(path, headers=agent_headers)
    assert denied.status_code in {401, 403, 503} and "items" not in denied.json()
    assert client.get(path, headers=auth_headers).json()["items"] == []
    assert jobs(client, order, auth_headers)["items"] == []


def test_pending_discovery_retains_queue_failure_but_never_dispatches(client, tenant, auth_headers, monkeypatch):
    headers, order = new_order(client, tenant, auth_headers)
    original = pos_printing._insert_jobs
    monkeypatch.setattr(pos_printing, "_insert_jobs", lambda *args: (_ for _ in ()).throw(RuntimeError("offline queue")))
    assert cash_confirm(client, headers, order).status_code == 200
    path = f"/api/v1/orders/printing/pending?branch_id={tenant['branch_id']}"
    response = client.get(path, headers=auth_headers)
    assert response.status_code == 200 and response.json()["items"][0]["order_id"] == order["id"]
    with SessionLocal() as db:
        assert db.query(PrintJob).count() == 0
    monkeypatch.setattr(pos_printing, "_insert_jobs", original)
    rows = pending(client, order, auth_headers)
    assert len(rows) == 2
    mark_sent(client, rows, auth_headers)
    assert client.get(path, headers=auth_headers).json()["items"] == []


def test_agent_addition_updates_only_never_claimed_initial_receipt(client, tenant, auth_headers):
    headers, order = new_order(client, tenant, auth_headers)
    assert cash_confirm(client, headers, order).status_code == 200
    initial = pending(client, order, auth_headers)
    receipt_job = next(row for row in initial if row["job_type"] == "customer_receipt")
    requested = addition(client, headers, current(client, headers, order), "cash")
    assert requested.status_code == 200, requested.text
    rows = pending(client, order, auth_headers)
    updated = next(row for row in rows if row["job_type"] == "customer_receipt")
    assert updated["id"] == receipt_job["id"]
    assert updated["payload"]["order"]["total"] == current(client, headers, order)["total"]
    assert len(rows) == 3
    body = claim_body()
    assert claim(client, updated, auth_headers, body).json()["dispatch_allowed"]
    with SessionLocal() as db:
        claimed_payload = deepcopy(db.get(PrintJob, updated["id"]).payload)
    assert addition(client, headers, current(client, headers, order), "cash", key="second-addition").status_code == 200
    with SessionLocal() as db:
        assert db.get(PrintJob, updated["id"]).payload == claimed_payload
        assert db.query(PrintJob).filter_by(job_type="customer_receipt").count() == 1
