from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.auth import AuthContext
from app.database import Base, SessionLocal
from app.models import (
    AuditEvent, Branch, BranchSettings, Business, IdempotencyRecord, IntegrationEvent, KitchenTicket,
    Membership, Order, OrderItem, Payment, PrintJob, Product, StaffMember,
)
from app import pos_printing
from app.pos_printing_api import claim_order_printing, create_order_printing
from app.pos_printing import PosPrintClaim, PosPrintCreate
from test_command_workflow import create_order, send_order


def enable(tenant, **overrides):
    with SessionLocal.begin() as db:
        settings = db.scalar(select(BranchSettings).where(BranchSettings.branch_id == tenant["branch_id"]))
        if not settings:
            settings = BranchSettings(business_id=tenant["business_id"], branch_id=tenant["branch_id"])
            db.add(settings)
        settings.advanced_printing = True
        settings.printer_config = {"printer_name": "POS-80", "paper_width_mm": 80,
                                   "auto_print_kitchen": True, "manual_customer_receipt": False,
                                   **overrides}


def jobs(client, order, headers):
    response = client.get(f"/api/v1/orders/{order['id']}/printing", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def claim_body(**overrides):
    return {"terminal_id": str(uuid4()), "claim_token": str(uuid4()), **overrides}


def claim(client, job, headers, body=None, key=None):
    return client.post(f"/api/v1/orders/{job['order_id']}/printing/{job['id']}/claim",
                       json=body or claim_body(), headers={**headers, "Idempotency-Key": key or str(uuid4())})


def complete(client, job, headers, body, outcome, key=None):
    return client.post(f"/api/v1/orders/{job['order_id']}/printing/{job['id']}/complete",
                       json={"terminal_id": body["terminal_id"], "claim_token": body["claim_token"],
                             "outcome": outcome}, headers={**headers, "Idempotency-Key": key or str(uuid4())})


def ready_jobs(client, tenant, auth_headers):
    enable(tenant)
    order = create_order(client, tenant, auth_headers)
    send_order(client, order, auth_headers)
    return order, jobs(client, order, auth_headers)["items"]


@pytest.mark.parametrize("dine_in", [False, True])
def test_confirmation_creates_expected_jobs_once_with_real_snapshots(client, tenant, auth_headers, dine_in):
    enable(tenant, paper_width_mm=58, copies=3)
    with SessionLocal.begin() as db:
        db.add(Membership(auth_user_id="owner-test", business_id=tenant["business_id"],
                          branch_id=tenant["branch_id"], role="owner", full_name="Maria Cocina"))
    order = create_order(client, tenant, auth_headers, dine_in=dine_in)
    assert jobs(client, order, auth_headers)["items"] == []
    sent = send_order(client, order, auth_headers)
    response = jobs(client, order, auth_headers)
    assert not response["recoverable_error"]
    items = response["items"]
    assert len(items) == (1 if dine_in else 2)
    assert {job["job_type"] for job in items} == ({"kitchen_ticket"} if dine_in else {"customer_receipt", "kitchen_ticket"})
    for job in items:
        assert job["status"] == "pending"
        assert job["attempts"] == 0
        payload = job["payload"]
        assert payload["snapshot_version"] == 1
        assert payload["printer_name"] == "POS-80" and payload["paper_width_mm"] == 58
        assert payload["copies"] == 3
        assert payload["order"]["number"] == order["number"]
        assert payload["order"]["folio"] == order["folio"]
        assert payload["order"]["items"][0]["name"] == "Pizza"
        assert payload["order"]["items"][0]["line_total"] == 20
        assert payload["order"]["subtotal"] == payload["order"]["total"] == 20
        assert payload["order"]["status"] == sent["order"]["status"]
        assert payload["order"]["version"] == sent["order"]["version"]
        assert payload["order"]["payments"] == []
        assert payload["remaining_amount"] == payload["order"]["remaining_amount"] == 20
        assert payload["created_by_name"] == "Maria Cocina"
        assert payload["order"]["table_context"]["table_name"] == ("Mesa 1" if dine_in else None)
        assert payload["business"]["name"] == "Test Restaurant"
        assert payload["branch"]["name"] == "Main"
        if job["job_type"] == "kitchen_ticket":
            assert payload["ticket"]["sequence"] == 1
            assert payload["ticket"]["context"]["created_by_name"] == "Maria Cocina"
            assert payload["ticket"]["context"]["delivery_address"] is None
        else:
            assert payload["ticket"] is None
    assert "_pos_printing" not in sent["tickets"][0]["context"]
    assert send_order(client, order, auth_headers) == sent
    send_order(client, sent["order"], auth_headers, key="different-send-key")
    assert jobs(client, order, auth_headers)["items"] == items


@pytest.mark.parametrize("configured,expected", [
    (1, 1), (2, 2), (5, 5), (10, 5), (0, 1), (-3, 1),
    (None, 1), (True, 1), ("invalid", 1), (2.5, 1),
])
def test_snapshot_uses_bounded_copies_without_failing_confirmation(client, tenant, auth_headers, configured, expected):
    enable(tenant, copies=configured)
    order = create_order(client, tenant, auth_headers)
    send_order(client, order, auth_headers)
    assert all(job["payload"]["copies"] == expected for job in jobs(client, order, auth_headers)["items"])
    enable(tenant, copies=4)
    assert all(job["payload"]["copies"] == expected for job in jobs(client, order, auth_headers)["items"])


@pytest.mark.parametrize("language,expected", [(None, "pixel"), ("pixel", "pixel"), ("escpos", "escpos"), ("legacy", "pixel")])
def test_print_language_is_explicit_and_immutable(client, tenant, auth_headers, language, expected):
    enable(tenant, **({"print_language": language} if language else {}))
    order = create_order(client, tenant, auth_headers)
    send_order(client, order, auth_headers)
    assert {job["payload"]["print_language"] for job in jobs(client, order, auth_headers)["items"]} == {expected}
    enable(tenant, print_language="pixel" if expected == "escpos" else "escpos")
    assert {job["payload"]["print_language"] for job in jobs(client, order, auth_headers)["items"]} == {expected}


def test_settings_accepts_only_supported_print_languages(client, tenant, auth_headers):
    url = f"/api/v1/settings/branches/{tenant['branch_id']}/printing"
    settings = client.get(url, headers=auth_headers).json()
    for language in [None, "raw", {"language": "escpos"}]:
        response = client.patch(url, json={"advanced_printing": True,
            "printer_config": {"printer_name": "POS-80", "print_language": language},
            "expected_version": settings["version"]}, headers={**auth_headers, "Idempotency-Key": str(uuid4())})
        assert response.status_code == 422, response.text
    response = client.patch(url, json={"advanced_printing": True,
        "printer_config": {"printer_name": "POS-80", "print_language": "escpos"},
        "expected_version": settings["version"]}, headers={**auth_headers, "Idempotency-Key": str(uuid4())})
    assert response.status_code == 200, response.text
    assert response.json()["printer_config"]["print_language"] == "escpos"


@pytest.mark.parametrize("other_language", ["pixel", "escpos"])
def test_branch_two_escpos_save_affects_only_new_jobs_and_its_own_profile(
    client, tenant, auth_headers, other_language,
):
    # Reproduce the branch identities only in the isolated tenant fixture.
    assert tenant["branch_id"] == 1 and tenant["other_branch_id"] == 2
    pizza = {"business_id": tenant["other_business_id"], "branch_id": tenant["other_branch_id"]}
    with SessionLocal.begin() as db:
        db.get(Business, tenant["business_id"]).name = "Bazar"
        db.get(Business, pizza["business_id"]).name = "Pizza House"
        product = Product(business_id=pizza["business_id"], branch_id=pizza["branch_id"],
                          sku="PIZZA", name="Pizza", price="20")
        db.add(product)
        db.flush()
        pizza["product_id"] = product.id
    pizza_headers = {**auth_headers, "X-Business-Id": str(pizza["business_id"]),
                     "X-Branch-Id": str(pizza["branch_id"])}

    def save_profile(branch, headers, config):
        url = f"/api/v1/settings/branches/{branch['branch_id']}/printing"
        before = client.get(url, headers=headers)
        assert before.status_code == 200, before.text
        body = {"advanced_printing": True, "printer_config": config,
                "expected_version": before.json()["version"]}
        keyed = {**headers, "Idempotency-Key": str(uuid4())}
        saved = client.patch(url, json=body, headers=keyed)
        assert saved.status_code == 200, saved.text
        assert saved.json()["version"] == body["expected_version"] + 1
        assert saved.json()["printer_config"] == {"automatic_printing": True, **config}
        replay = client.patch(url, json=body, headers=keyed)
        assert replay.status_code == 200 and replay.json() == saved.json()
        fetched = client.get(url, headers=headers)
        assert fetched.status_code == 200 and fetched.json() == saved.json()
        return fetched.json()

    bazar_config = {"printer_name": "Bazar printer", "print_language": other_language,
                    "paper_width_mm": 58, "copies": 3,
                    "auto_print_kitchen": True, "manual_customer_receipt": False}
    pizza_config = {"printer_name": "Generic / Text Only", "print_language": "pixel",
                    "paper_width_mm": 80, "copies": 1,
                    "auto_print_kitchen": True, "manual_customer_receipt": False}
    bazar_profile = save_profile(tenant, auth_headers, bazar_config)
    save_profile(pizza, pizza_headers, pizza_config)

    historical = []
    for branch, headers in [(tenant, auth_headers), (pizza, pizza_headers)]:
        order = create_order(client, branch, headers, key="before-profile-change")
        sent = send_order(client, order, headers, key="before-profile-change")
        manual = manual_request(client, sent["order"], headers)
        assert manual.status_code == 201, manual.text
        previous_jobs = jobs(client, order, headers)["items"]
        assert len(previous_jobs) == 3
        expected_language = other_language if branch is tenant else "pixel"
        assert {job["payload"]["print_language"] for job in previous_jobs} == {expected_language}
        historical.append((sent, headers, previous_jobs))
    with SessionLocal() as db:
        stored_payloads = {job.id: job.payload for job in db.scalars(select(PrintJob))}
        stored_intents = {ticket.id: ticket.context_snapshot for ticket in db.scalars(select(KitchenTicket))}

    pizza_config = {**pizza_config, "print_language": "escpos"}
    save_profile(pizza, pizza_headers, pizza_config)

    for branch, headers, config in [(tenant, auth_headers, bazar_config), (pizza, pizza_headers, pizza_config)]:
        order = create_order(client, branch, headers, key="after-profile-change")
        sent = send_order(client, order, headers, key="after-profile-change")
        automatic = jobs(client, order, headers)["items"]
        assert len(automatic) == 2
        assert {job["job_type"] for job in automatic} == {"customer_receipt", "kitchen_ticket"}
        new_jobs = list(automatic)
        # Explicit reprints of an older order use current settings, not its old jobs.
        old_sent = historical[0 if branch is tenant else 1][0]
        for kind in ["customer_receipt", "kitchen_ticket"]:
            body = {"job_type": kind, "expected_order_version": old_sent["order"]["version"]}
            if kind == "kitchen_ticket":
                ticket = old_sent["tickets"][0]
                body.update(kitchen_ticket_id=ticket["id"], expected_ticket_version=ticket["version"])
            response = manual_request(client, old_sent["order"], headers, body)
            assert response.status_code == 201, response.text
            new_jobs.append(response.json())
        for job in new_jobs:
            assert job["branch_id"] == job["payload"]["branch"]["id"] == branch["branch_id"]
            assert job["payload"]["business"]["id"] == branch["business_id"]
            for field in ["printer_name", "print_language", "paper_width_mm", "copies"]:
                assert job["payload"][field] == config[field]

    bazar_after = client.get(f"/api/v1/settings/branches/{tenant['branch_id']}/printing", headers=auth_headers)
    assert bazar_after.status_code == 200 and bazar_after.json() == bazar_profile
    for sent, headers, previous_jobs in historical:
        current_jobs = {job["id"]: job for job in jobs(client, sent["order"], headers)["items"]}
        for job in previous_jobs:
            assert current_jobs[job["id"]] == job
    with SessionLocal() as db:
        for job_id, payload in stored_payloads.items():
            assert db.get(PrintJob, job_id).payload == payload
        for ticket_id, intent in stored_intents.items():
            assert db.get(KitchenTicket, ticket_id).context_snapshot == intent


@pytest.mark.parametrize("config,expected", [
    ({"auto_print_kitchen": False}, {"customer_receipt"}),
    ({"manual_customer_receipt": True}, {"kitchen_ticket"}),
    ({"auto_print_kitchen": False, "manual_customer_receipt": True}, set()),
    ({"printer_name": ""}, set()),
])
def test_printing_flags(client, tenant, auth_headers, config, expected):
    enable(tenant, **config)
    order = create_order(client, tenant, auth_headers)
    send_order(client, order, auth_headers)
    assert {job["job_type"] for job in jobs(client, order, auth_headers)["items"]} == expected


def test_does_not_backfill_historical_orders_or_integrations(client, tenant, auth_headers):
    order = create_order(client, tenant, auth_headers)
    sent = send_order(client, order, auth_headers)
    enable(tenant)
    send_order(client, sent["order"], auth_headers, key="resend-old")
    assert jobs(client, order, auth_headers)["items"] == []
    integrated = create_order(client, tenant, auth_headers, key="integration")
    with SessionLocal.begin() as db:
        db.get(Order, integrated["id"]).source = "whatsapp_agent"
    send_order(client, integrated, auth_headers, key="integration-send")
    assert jobs(client, integrated, auth_headers)["items"] == []


def test_legacy_confirm_then_send_path_also_enqueues(client, tenant, auth_headers):
    enable(tenant)
    order = create_order(client, tenant, auth_headers)
    confirmed = client.post(f"/api/v1/orders/{order['id']}/confirm", headers=auth_headers)
    assert confirmed.status_code == 200
    assert jobs(client, order, auth_headers)["items"] == []
    response = client.post(f"/api/v1/orders/{order['id']}/send-to-kitchen", headers=auth_headers)
    assert response.status_code == 200
    assert len(jobs(client, order, auth_headers)["items"]) == 2


def test_only_one_dispatch_per_job_and_claim_owner(client, tenant, auth_headers):
    order, items = ready_jobs(client, tenant, auth_headers)
    job = items[0]
    body = claim_body()
    claimed = claim(client, job, auth_headers, body, "first")
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["dispatch_allowed"] is True
    assert claim(client, job, auth_headers, body, "first").json()["dispatch_allowed"] is False
    assert claim(client, job, auth_headers, body).json()["dispatch_allowed"] is False
    assert claim(client, job, auth_headers).status_code == 409
    assert claim(client, job, {**auth_headers, "X-Dev-User": "someone-else"}, body).status_code == 409
    assert complete(client, job, auth_headers, claim_body(), "printed").status_code == 403
    assert complete(client, job, {**auth_headers, "X-Dev-User": "someone-else"}, body, "printed").status_code == 403
    result = complete(client, job, auth_headers, body, "printed", "ack")
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "printed"
    assert complete(client, job, auth_headers, body, "printed", "ack").json() == result.json()
    assert complete(client, job, auth_headers, body, "unknown", "ack").status_code == 409
    assert claim(client, job, auth_headers).status_code == 409
    with SessionLocal() as db:
        rows = list(db.scalars(select(IdempotencyRecord)))
        events = list(db.scalars(select(AuditEvent).where(AuditEvent.entity_type == "print_job")))
        assert len(events) == 2
        assert body["claim_token"] not in json.dumps([row.response_body for row in rows])
        assert body["claim_token"] not in json.dumps([row.payload for row in events])
        assert body["claim_token"] not in json.dumps(db.get(PrintJob, job["id"]).payload)
        assert db.get(Order, order["id"]).status == "sent_to_kitchen"


@pytest.mark.parametrize("outcome,retryable", [("unknown", False), ("not_sent", True)])
def test_uncertain_never_retries_only_explicit_known_not_sent(client, tenant, auth_headers, outcome, retryable):
    _, items = ready_jobs(client, tenant, auth_headers)
    job, body = items[0], claim_body()
    assert claim(client, job, auth_headers, body).status_code == 200
    result = complete(client, job, auth_headers, body, outcome)
    assert result.status_code == 200
    assert result.json()["retryable"] is retryable
    assert claim(client, job, auth_headers).status_code == 409
    retried = claim(client, job, auth_headers, claim_body(retry_not_sent=True))
    assert retried.status_code == (200 if retryable else 409)
    if retryable:
        assert retried.json()["dispatch_allowed"]
        assert retried.json()["job"]["attempts"] == 2


def test_queue_insert_failure_does_not_rollback_order_and_recovers_original_data(client, tenant, auth_headers, monkeypatch):
    enable(tenant)
    original = pos_printing._insert_jobs
    def fail(*args):
        original(*args)
        raise RuntimeError("simulated queue failure after flush")
    monkeypatch.setattr(pos_printing, "_insert_jobs", fail)
    order = create_order(client, tenant, auth_headers)
    sent = send_order(client, order, auth_headers)
    assert sent["order"]["status"] == "sent_to_kitchen"
    failed = jobs(client, order, auth_headers)
    assert failed["items"] == [] and failed["recoverable_error"]
    with SessionLocal.begin() as db:
        assert db.query(PrintJob).count() == 0
        assert db.query(KitchenTicket).count() == 1
        db.get(Product, tenant["product_id"]).name = "New catalog name"
        db.get(Business, tenant["business_id"]).name = "New business name"
        settings = db.scalar(select(BranchSettings).where(BranchSettings.branch_id == tenant["branch_id"]))
        settings.printer_config = {"printer_name": "Other printer", "auto_print_kitchen": False}
    monkeypatch.setattr(pos_printing, "_insert_jobs", original)
    recovered = jobs(client, order, auth_headers)
    assert len(recovered["items"]) == 2 and not recovered["recoverable_error"]
    for job in recovered["items"]:
        assert job["payload"]["order"]["items"][0]["name"] == "Pizza"
        assert job["payload"]["business"]["name"] == "Test Restaurant"
        assert job["payload"]["printer_name"] == "POS-80"


@pytest.mark.parametrize("change", ["cancel", "items", "destination", "ticket"])
def test_stale_jobs_never_claim(client, tenant, auth_headers, change):
    order, items = ready_jobs(client, tenant, auth_headers)
    with SessionLocal.begin() as db:
        row = db.get(Order, order["id"])
        if change == "cancel":
            row.status = "cancelled"
        elif change == "items":
            row.items[0].quantity = Decimal("2")
            ticket = db.scalar(select(KitchenTicket).where(KitchenTicket.order_id == row.id))
            ticket.version += 1
        elif change == "destination":
            row.customer_name = "Changed destination"
        else:
            db.scalar(select(KitchenTicket).where(KitchenTicket.order_id == row.id)).version += 1
    for job in items:
        if change == "ticket" and job["job_type"] == "customer_receipt":
            continue
        response = claim(client, job, auth_headers)
        assert response.status_code == 409, response.text
    assert any(job["status"] == "cancelled" for job in jobs(client, order, auth_headers)["items"])


def test_payment_after_confirmation_does_not_change_or_invalidate_snapshot(client, tenant, auth_headers):
    order, items = ready_jobs(client, tenant, auth_headers)
    with SessionLocal.begin() as db:
        row = db.get(Order, order["id"])
        row.payment_status = "paid"
        row.version += 1
        db.add(Payment(business_id=row.business_id, order_id=row.id,
                       method="cash", amount=20, status="confirmed"))
    assert jobs(client, order, auth_headers)["items"] == items
    assert claim(client, items[0], auth_headers).json()["dispatch_allowed"]


@pytest.mark.parametrize("change", ["cancel", "revision", "payment"])
def test_claimed_jobs_accept_late_success_after_concurrent_order_changes(client, tenant, auth_headers, change):
    order, items = ready_jobs(client, tenant, auth_headers)
    claims = {job["id"]: claim_body() for job in items}
    for job in items:
        assert claim(client, job, auth_headers, claims[job["id"]]).json()["dispatch_allowed"]
    if change == "revision":
        current = client.get(f"/api/v1/orders/{order['id']}", headers=auth_headers).json()
        response = client.post(f"/api/v1/orders/{order['id']}/item-revisions", json={
            "expected_version": current["version"], "operations": [{
                "type": "edit", "item_id": current["items"][0]["id"],
                "replacement": {"product_id": tenant["product_id"], "quantity": 2},
            }],
        }, headers={**auth_headers, "Idempotency-Key": "revision-after-claim"})
        assert response.status_code == 201, response.text
    else:
        with SessionLocal.begin() as db:
            row = db.get(Order, order["id"])
            row.version += 1
            if change == "cancel":
                row.status = "cancelled"
            else:
                row.payment_status = "paid"
                db.add(Payment(business_id=row.business_id, order_id=row.id,
                               method="cash", amount=20, status="confirmed"))
    assert all(job["status"] == "claimed" for job in jobs(client, order, auth_headers)["items"])
    for job in items:
        assert claim(client, job, auth_headers).status_code == 409
        response = complete(client, job, auth_headers, claims[job["id"]], "printed", "late-ack")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "printed"
        assert response.json()["payload"] == job["payload"]
        assert complete(client, job, auth_headers, claims[job["id"]], "printed", "late-ack").json() == response.json()
        assert claim(client, job, auth_headers).status_code == 409


@pytest.mark.parametrize("outcome,expected", [("unknown", "failed"), ("not_sent", "cancelled")])
def test_changed_claim_records_failure_without_allowing_stale_redispatch(client, tenant, auth_headers, outcome, expected):
    order, items = ready_jobs(client, tenant, auth_headers)
    job, body = items[0], claim_body()
    assert claim(client, job, auth_headers, body).json()["dispatch_allowed"]
    with SessionLocal.begin() as db:
        db.get(Order, order["id"]).status = "cancelled"
    response = complete(client, job, auth_headers, body, outcome, "late-failure")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == expected
    assert not response.json()["retryable"]
    assert complete(client, job, auth_headers, body, outcome, "late-failure").json() == response.json()
    assert claim(client, job, auth_headers, claim_body(retry_not_sent=True)).status_code == 409


@pytest.mark.parametrize("role,expected", [("cashier", 200), ("waiter", 200), ("kitchen", 403), ("dispatcher", 403)])
def test_operational_permissions_and_qz_separate_from_configuration(client, tenant, auth_headers, monkeypatch, role, expected):
    order, items = ready_jobs(client, tenant, auth_headers)
    headers = {**auth_headers, "X-Dev-Role": role}
    response = client.get(f"/api/v1/orders/{order['id']}/printing", headers=headers)
    assert response.status_code == expected
    assert claim(client, items[0], headers).status_code == expected
    monkeypatch.delenv("QZ_TRAY_CERTIFICATE", raising=False)
    monkeypatch.delenv("QZ_TRAY_PRIVATE_KEY", raising=False)
    qz = client.get(f"/api/v1/orders/{order['id']}/printing/qz", headers=headers)
    assert qz.status_code == (403 if role == "dispatcher" else 200)
    if qz.status_code == 200:
        assert qz.json() == {"mode": "manual-approval", "certificate": None}
    assert client.get(f"/api/v1/settings/branches/{tenant['branch_id']}/printing", headers=headers).status_code == 403
    assert client.post("/api/v1/settings/printing/qz/sign", json={"payload": "test"}, headers=headers).status_code == 403


def test_scope_and_invalid_claim_bodies(client, tenant, auth_headers):
    order, items = ready_jobs(client, tenant, auth_headers)
    other = {**auth_headers, "X-Business-Id": str(tenant["other_business_id"]),
             "X-Branch-Id": str(tenant["other_branch_id"])}
    assert client.get(f"/api/v1/orders/{order['id']}/printing", headers=other).status_code == 404
    assert claim(client, items[0], other).status_code == 404
    response = client.post(f"/api/v1/orders/{order['id']}/printing/{items[0]['id']}/claim",
                           json=claim_body(), headers=auth_headers)
    assert response.status_code == 422
    assert claim(client, items[0], auth_headers, claim_body(terminal_id="not-uuid")).status_code == 422


def test_real_concurrent_terminals_have_one_winner(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'concurrent-printing.db'}",
                           connect_args={"check_same_thread": False, "timeout": 10})
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            business = Business(slug="parallel", name="Parallel")
            db.add(business); db.flush()
            branch = Branch(business_id=business.id, slug="main", name="Main")
            db.add(branch); db.flush()
            db.add(BranchSettings(business_id=business.id, branch_id=branch.id, advanced_printing=True,
                                  printer_config={"printer_name": "POS-80", "manual_customer_receipt": False,
                                                  "auto_print_kitchen": True}))
            order = Order(business_id=business.id, branch_id=branch.id, number="TEST", folio=1,
                          status="sent_to_kitchen", source="pos", subtotal=20, total=20)
            order.items = [OrderItem(product_name="Pizza", quantity=1, unit_price=20, line_total=20)]
            db.add(order); db.flush()
            ticket = KitchenTicket(business_id=business.id, branch_id=branch.id, order_id=order.id,
                                   items_snapshot=[{"name": "Pizza", "quantity": 1, "line_total": 20}])
            db.add(ticket); db.flush()
            pos_printing.prepare_pos_print_intent(db, order, ticket)
            db.commit()
            job_id = db.scalar(select(PrintJob.id))
            order_id = order.id
            user = AuthContext("cashier", "cashier", business.id, branch.id)
        barrier = Barrier(2)
        def dispatch(_):
            with Session(engine, autoflush=False) as db:
                barrier.wait()
                try:
                    response = claim_order_printing(order_id, job_id, PosPrintClaim(**claim_body()),
                                                    str(uuid4()), user, db)
                    return response["dispatch_allowed"]
                except HTTPException as exc:
                    assert exc.status_code == 409
                    return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(dispatch, range(2))) == [False, True]
        with Session(engine) as db:
            assert db.get(PrintJob, job_id).attempts == 1
        barrier = Barrier(2)
        def manually_request(_):
            with Session(engine, autoflush=False) as db:
                barrier.wait()
                return create_order_printing(order_id, PosPrintCreate(
                    job_type="customer_receipt", expected_order_version=1,
                ), "shared-manual-request", user, db)["id"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert len(set(pool.map(manually_request, range(2)))) == 1
        with Session(engine) as db:
            assert db.query(PrintJob).count() == 3
    finally:
        engine.dispose()


def test_totals_modifiers_discounts_payment_and_pin_author_are_snapshotted(client, tenant, auth_headers):
    enable(tenant)
    order = create_order(client, tenant, auth_headers)
    with SessionLocal.begin() as db:
        staff = StaffMember(business_id=tenant["business_id"], first_name="Rosa", last_name="Cocina")
        db.add(staff); db.flush()
        row = db.get(Order, order["id"])
        row.created_by = f"staff:{staff.id}"
        row.items[0].quantity = Decimal("2")
        row.items[0].modifiers = [{"name": "Queso", "group_name": "Extras", "price_delta": 3}]
        row.items[0].line_total = Decimal("46")
        row.items[0].notes = "Salsa aparte"
        row.subtotal = Decimal("46")
        row.discount = row.manual_discount = Decimal("5")
        row.delivery_fee = Decimal("8")
        row.total = Decimal("49")
        row.payment_status = "partial"
        row.payment_method = "card"
        db.add(Payment(business_id=row.business_id, order_id=row.id,
                       method="card", amount=10, status="confirmed"))
    send_order(client, order, auth_headers)
    for job in jobs(client, order, auth_headers)["items"]:
        payload = job["payload"]
        snapshot = payload["order"]
        assert snapshot["subtotal"] == 46 and snapshot["discount"] == 5
        assert snapshot["delivery_fee"] == 8 and snapshot["total"] == 49
        assert snapshot["items"][0]["line_total"] == 46
        assert snapshot["items"][0]["modifiers"][0]["price_delta"] == 3
        assert snapshot["paid_amount"] == 10 and snapshot["remaining_amount"] == 39
        assert snapshot["payments"][0]["method"] == "card"
        assert payload["created_by_name"] == "Rosa Cocina"
        if payload["ticket"]:
            assert payload["ticket"]["context"]["created_by_name"] == "Rosa Cocina"
            assert payload["ticket"]["items"][0]["notes"] == "Salsa aparte"


def test_revision_cancels_old_documents_without_mutating_typed_payload(client, tenant, auth_headers):
    order, items = ready_jobs(client, tenant, auth_headers)
    current = client.get(f"/api/v1/orders/{order['id']}", headers=auth_headers).json()
    response = client.post(f"/api/v1/orders/{order['id']}/item-revisions", json={
        "expected_version": current["version"], "operations": [{
            "type": "edit", "item_id": current["items"][0]["id"],
            "replacement": {"product_id": tenant["product_id"], "quantity": 2},
        }],
    }, headers={**auth_headers, "Idempotency-Key": "edit-before-print"})
    assert response.status_code == 201, response.text
    updated = jobs(client, order, auth_headers)["items"]
    assert all(job["status"] == "cancelled" for job in updated)
    assert {job["id"]: job["payload"] for job in updated} == {job["id"]: job["payload"] for job in items}


def test_adding_batch_prints_only_new_command_and_does_not_duplicate_receipt(client, tenant, auth_headers):
    order, items = ready_jobs(client, tenant, auth_headers)
    current = client.get(f"/api/v1/orders/{order['id']}", headers=auth_headers).json()
    response = client.post(f"/api/v1/orders/{order['id']}/item-batches", json={
        "expected_version": current["version"], "items": [{"product_id": tenant["product_id"], "quantity": 1}],
    }, headers={**auth_headers, "Idempotency-Key": "batch"})
    assert response.status_code == 201, response.text
    updated = jobs(client, order, auth_headers)["items"]
    assert len(updated) == 3
    assert len([job for job in updated if job["job_type"] == "customer_receipt"]) == 1
    assert all(job["status"] == "pending" for job in updated if job["job_type"] == "kitchen_ticket")
    assert {job["payload"]["ticket"]["sequence"] for job in updated if job["job_type"] == "kitchen_ticket"} == {1, 2}


def test_confirmed_order_rollback_does_not_leave_print_jobs(client, tenant, auth_headers, monkeypatch):
    enable(tenant)
    order = create_order(client, tenant, auth_headers)
    from app import api as api_module
    original = api_module.save_idempotent_response
    def fail(db, scope, *args):
        if scope.startswith("confirm-send-order"):
            raise HTTPException(503, "Simulated transaction failure")
        return original(db, scope, *args)
    monkeypatch.setattr(api_module, "save_idempotent_response", fail)
    result = client.post(f"/api/v1/orders/{order['id']}/confirm-and-send",
                         json={"expected_version": order["version"]},
                         headers={**auth_headers, "Idempotency-Key": "rollback"})
    assert result.status_code == 503
    with SessionLocal() as db:
        assert db.query(PrintJob).count() == 0
        assert db.query(KitchenTicket).count() == 0
        assert db.get(Order, order["id"]).status == "draft"


@pytest.mark.parametrize("role", ["cashier", "waiter", "kitchen"])
@pytest.mark.parametrize("cut", ["0A0A0A1D5601", "0A1D564100"])
def test_qz_operational_signing_only_supports_printing_calls(client, tenant, auth_headers, monkeypatch, role, cut):
    order, _ = ready_jobs(client, tenant, auth_headers)
    from app import settings_api
    signed_payloads = []

    def sign(value):
        signed_payloads.append(value)
        return "test-signature"

    monkeypatch.setattr(settings_api, "qz_sign_payload", sign)
    headers = {**auth_headers, "X-Dev-Role": role}
    url = f"/api/v1/settings/printing/qz/sign?branch_id={tenant['branch_id']}"
    response = client.post(url, json={"payload": json.dumps({"call": "printers.find", "params": {}})}, headers=headers)
    assert response.status_code == 200 and response.json() == {"signature": "test-signature"}
    response = client.post(url, json={"payload": json.dumps({"call": "print", "params": {
        "printer": {"name": "POS-80"}, "data": [{"type": "pixel", "format": "html",
        "flavor": "plain", "data": "<html><body>Thermal document</body></html>"}],
    }})}, headers=headers)
    assert response.status_code == 200 and response.json() == {"signature": "test-signature"}
    raw_data = [
        {"type": "raw", "format": "command", "flavor": "hex", "data": "1B40"},
        {"type": "raw", "format": "html", "flavor": "plain", "data": "<html>Ticket</html>",
         "options": {"language": "ESCPOS", "pageWidth": 80 / 25.4, "pageHeight": 2.5, "dotDensity": "double"}},
        {"type": "raw", "format": "command", "flavor": "hex", "data": cut},
    ]
    payload = json.dumps({"call": "print", "params": {"printer": {"name": "POS-80"}, "data": raw_data}})
    response = client.post(url, json={"payload": payload}, headers=headers)
    assert response.status_code == 200 and response.json() == {"signature": "test-signature"}
    assert signed_payloads[-1] == payload
    accepted_payloads = list(signed_payloads)
    for command in ["1B700019FA", "1B4000", "0A0A0A1D560100", "anything",
                    "0A1D56410000", "0A1D564101", "0A1D5641", "0a1d564100",
                    " 0A1D564100", "0A1D564100\n", "0A1D5641001B700019FA"]:
        raw_data[-1]["data"] = command
        response = client.post(url, json={"payload": json.dumps({"call": "print", "params": {"printer": {"name": "POS-80"}, "data": raw_data}})}, headers=headers)
        assert response.status_code == 403, response.text
    for item in [
        {"type": "raw", "format": "command", "flavor": "plain", "data": cut},
        {"type": "raw", "format": "command", "flavor": "file", "data": "file:///private"},
        {"type": "raw", "format": "html", "flavor": "file", "data": "file:///private",
         "options": {"language": "ESCPOS"}},
        {"type": "pixel", "format": "html", "flavor": "file", "data": "file:///private"},
    ]:
        payload = json.dumps({"call": "print", "params": {"printer": {"name": "POS-80"}, "data": [*raw_data[:2], item]}})
        response = client.post(url, json={"payload": payload}, headers=headers)
        assert response.status_code == 403, response.text
    for payload in ["invalid", json.dumps({"call": "file.read"}), json.dumps({"call": "print", "params": {"printer": {"name": "POS-80"}, "data": ["file:///private"]}})]:
        assert client.post(url, json={"payload": payload}, headers=headers).status_code in {403, 422}
    assert client.post(f"/api/v1/settings/printing/qz/sign?branch_id={tenant['other_branch_id']}",
                       json={"payload": "test"}, headers=headers).status_code == 403
    with SessionLocal.begin() as db:
        sibling = Branch(business_id=tenant["business_id"], slug="unassigned", name="Unassigned")
        db.add(sibling); db.flush()
        sibling_id = sibling.id
    assert client.post(f"/api/v1/settings/printing/qz/sign?branch_id={sibling_id}",
                       json={"payload": "test"}, headers=headers).status_code == 403
    assert signed_payloads == accepted_payloads
    monkeypatch.setenv("QZ_TRAY_CERTIFICATE", "public")
    monkeypatch.delenv("QZ_TRAY_PRIVATE_KEY", raising=False)
    assert client.get(f"/api/v1/orders/{order['id']}/printing/qz", headers=headers).status_code == 503


def test_unowned_legacy_jobs_are_not_returned_or_claimed_by_local_pos(client, tenant, auth_headers):
    order, items = ready_jobs(client, tenant, auth_headers)
    with SessionLocal.begin() as db:
        legacy = PrintJob(business_id=tenant["business_id"], branch_id=tenant["branch_id"],
                          order_id=order["id"], job_type="customer_receipt", payload={"legacy": True},
                          idempotency_key="manual-legacy")
        db.add(legacy); db.flush()
        legacy_id = legacy.id
    assert jobs(client, order, auth_headers)["items"] == items
    assert claim(client, {"id": legacy_id, "order_id": order["id"]}, auth_headers).status_code == 404


def manual_request(client, order, headers, body=None, key=None):
    return client.post(f"/api/v1/orders/{order['id']}/printing", json=body or {
        "job_type": "customer_receipt", "expected_order_version": order["version"],
    }, headers={**headers, "Idempotency-Key": key or str(uuid4())})


@pytest.mark.parametrize("role", ["cashier", "waiter"])
@pytest.mark.parametrize("kind", ["customer_receipt", "kitchen_ticket"])
def test_explicit_manual_print_is_idempotent_and_reprint_is_intentional(client, tenant, auth_headers, role, kind):
    order, automatic = ready_jobs(client, tenant, auth_headers)
    # Manual printing must still work with both automatic toggles disabled.
    enable(tenant, auto_print_kitchen=False, manual_customer_receipt=True, copies=2, print_language="escpos")
    current = client.get(f"/api/v1/orders/{order['id']}", headers=auth_headers).json()
    body = {"job_type": kind, "expected_order_version": current["version"]}
    if kind == "kitchen_ticket":
        ticket = next(job["payload"]["ticket"] for job in automatic if job["job_type"] == "kitchen_ticket")
        body.update(kitchen_ticket_id=ticket["id"], expected_ticket_version=ticket["version"])
    headers = {**auth_headers, "X-Dev-Role": role}
    response = manual_request(client, current, headers, body, "manual-one")
    assert response.status_code == 201, response.text
    job = response.json()
    assert job["status"] == "pending" and job["job_type"] == kind
    assert job["id"] not in {row["id"] for row in automatic}
    assert job["payload"]["print_language"] == "escpos" and job["payload"]["copies"] == 2
    assert job["payload"]["order"]["version"] == current["version"]
    assert manual_request(client, current, headers, body, "manual-one").json() == job
    assert manual_request(client, current, headers, {**body, "expected_order_version": 99}, "manual-one").status_code == 409
    claim_data = claim_body()
    assert claim(client, job, headers, claim_data).json()["dispatch_allowed"]
    assert complete(client, job, headers, claim_data, "printed").json()["status"] == "printed"
    assert manual_request(client, current, headers, body, "manual-one").json()["status"] == "printed"
    reprint = manual_request(client, current, headers, body, "intentional-reprint")
    assert reprint.status_code == 201 and reprint.json()["id"] != job["id"]
    assert len(jobs(client, current, headers)["items"]) == 4
    assert client.get(f"/api/v1/orders/{order['id']}", headers=auth_headers).json() == current
    with SessionLocal() as db:
        events = list(db.scalars(select(AuditEvent).where(AuditEvent.action == "pos.print_job.manually_requested")))
        assert len(events) == 2


def test_manual_print_validates_scope_versions_and_configuration(client, tenant, auth_headers):
    order, automatic = ready_jobs(client, tenant, auth_headers)
    current = client.get(f"/api/v1/orders/{order['id']}", headers=auth_headers).json()
    assert manual_request(client, order, auth_headers).status_code == 409
    ticket = next(job["payload"]["ticket"] for job in automatic if job["job_type"] == "kitchen_ticket")
    body = {"job_type": "kitchen_ticket", "kitchen_ticket_id": ticket["id"],
            "expected_order_version": current["version"], "expected_ticket_version": ticket["version"]}
    assert manual_request(client, current, auth_headers, {**body, "expected_ticket_version": 99}).status_code == 409
    assert manual_request(client, current, auth_headers, {**body, "kitchen_ticket_id": 99999}).status_code == 404
    assert manual_request(client, current, auth_headers, {**body, "expected_ticket_version": None}).status_code == 422
    assert manual_request(client, current, auth_headers, {**body, "job_type": "customer_receipt"}).status_code == 422
    other = {**auth_headers, "X-Business-Id": str(tenant["other_business_id"]), "X-Branch-Id": str(tenant["other_branch_id"])}
    assert manual_request(client, current, other).status_code == 404
    assert manual_request(client, current, {**auth_headers, "X-Dev-Role": "dispatcher"}).status_code == 403
    assert client.post(f"/api/v1/orders/{current['id']}/printing", json=body, headers=auth_headers).status_code == 422
    enable(tenant, printer_name="")
    assert manual_request(client, current, auth_headers).status_code == 409
    enable(tenant)
    with SessionLocal.begin() as db:
        db.scalar(select(BranchSettings).where(BranchSettings.branch_id == tenant["branch_id"])).advanced_printing = False
    assert manual_request(client, current, auth_headers).status_code == 409
    assert len(jobs(client, current, auth_headers)["items"]) == 2


def test_manual_history_snapshot_does_not_edit_closed_order_or_use_new_catalog(client, tenant, auth_headers):
    order, _ = ready_jobs(client, tenant, auth_headers)
    with SessionLocal.begin() as db:
        row = db.get(Order, order["id"])
        row.status = "closed"
        row.payment_status = "paid"
        row.version += 1
        db.add(Payment(business_id=row.business_id, order_id=row.id, method="cash", amount=20, status="confirmed"))
        db.get(Product, tenant["product_id"]).name = "Renamed"
    current = client.get(f"/api/v1/orders/{order['id']}", headers=auth_headers).json()
    headers = {**auth_headers, "X-Dev-Role": "cashier"}
    response = manual_request(client, current, headers, key="historical")
    assert response.status_code == 201, response.text
    job = response.json()
    assert job["payload"]["order"]["items"][0]["name"] == "Pizza"
    assert job["payload"]["paid_amount"] == 20 and job["payload"]["remaining_amount"] == 0
    assert claim(client, job, headers).json()["dispatch_allowed"]
    assert client.get(f"/api/v1/orders/{order['id']}", headers=auth_headers).json() == current


@pytest.mark.parametrize("paid", [0, 7, 22])
def test_cancelled_history_receipt_keeps_real_amounts_and_idempotency_without_kitchen_changes(client, tenant, auth_headers, paid):
    order, automatic = ready_jobs(client, tenant, auth_headers)
    with SessionLocal.begin() as db:
        row = db.get(Order, order["id"])
        row.manual_discount = row.discount = Decimal("3")
        row.delivery_fee = Decimal("5")
        row.total = Decimal("22")
        row.payment_status = "paid" if paid == 22 else "partial" if paid else "pending"
        if paid:
            row.payment_method = "cash"
            db.add(Payment(business_id=row.business_id, order_id=row.id,
                           method="cash", amount=paid, status="confirmed"))
    current = client.get(f"/api/v1/orders/{order['id']}", headers=auth_headers).json()
    before_cancel_job = manual_request(client, current, auth_headers, key="before-cancel").json()
    cancelled = client.post(f"/api/v1/orders/{order['id']}/transition", json={
        "status": "cancelled", "expected_version": current["version"], "reason": "Customer cancelled",
    }, headers=auth_headers)
    assert cancelled.status_code == 200, cancelled.text
    current = client.get(f"/api/v1/orders/{order['id']}", headers=auth_headers).json()

    def domain_state():
        with SessionLocal() as db:
            return {model.__tablename__: [
                {column.name: getattr(row, column.name) for column in model.__table__.columns}
                for row in db.scalars(select(model).order_by(model.id))
            ] for model in (Order, OrderItem, Payment, KitchenTicket, IntegrationEvent)}

    baseline = domain_state()
    headers = {**auth_headers, "X-Dev-Role": "cashier"}
    response = manual_request(client, current, headers, key="cancelled-history")
    assert response.status_code == 201, response.text
    job = response.json()
    snapshot = job["payload"]["order"]
    assert job["status"] == "pending" and job["kitchen_ticket_id"] is None
    assert job["payload"]["ticket"] is None
    assert snapshot["status"] == "cancelled"  # The existing renderer shows PEDIDO CANCELADO.
    assert snapshot["items"][0]["name"] == "Pizza" and snapshot["items"][0]["line_total"] == 20
    assert snapshot["subtotal"] == 20 and snapshot["discount"] == snapshot["manual_discount"] == 3
    assert snapshot["delivery_fee"] == 5 and snapshot["total"] == 22
    assert snapshot["payment_status"] == current["payment_status"]
    assert snapshot["paid_amount"] == job["payload"]["paid_amount"] == paid
    assert snapshot["remaining_amount"] == job["payload"]["remaining_amount"] == 22 - paid
    assert sum(payment["amount"] for payment in snapshot["payments"]) == paid
    assert manual_request(client, current, headers, key="cancelled-history").json() == job
    assert manual_request(client, current, headers, {
        "job_type": "customer_receipt", "expected_order_version": current["version"] + 1,
    }, key="cancelled-history").status_code == 409
    listed = {row["id"]: row for row in jobs(client, current, headers)["items"]}
    assert listed[job["id"]] == job
    for old in [*automatic, before_cancel_job]:
        assert listed[old["id"]]["status"] == "cancelled"
        assert claim(client, old, headers).status_code == 409
    ticket = baseline["kitchen_tickets"][0]
    assert ticket["status"] == "cancelled"
    rejected = manual_request(client, current, headers, {
        "job_type": "kitchen_ticket", "expected_order_version": current["version"],
        "kitchen_ticket_id": ticket["id"], "expected_ticket_version": ticket["version"],
    })
    assert rejected.status_code == 409, rejected.text
    body = claim_body()
    assert claim(client, job, headers, body).json()["dispatch_allowed"]
    assert not claim(client, job, headers, body).json()["dispatch_allowed"]
    completed = complete(client, job, headers, body, "printed", "cancelled-receipt-ack")
    assert completed.status_code == 200 and completed.json()["status"] == "printed"
    assert complete(client, job, headers, body, "printed", "cancelled-receipt-ack").json() == completed.json()
    assert manual_request(client, current, headers, key="cancelled-history").json() == completed.json()
    assert claim(client, job, headers).status_code == 409
    assert len(jobs(client, current, headers)["items"]) == 4
    assert domain_state() == baseline
    with SessionLocal() as db:
        assert db.query(AuditEvent).filter_by(entity_id=job["id"], action="pos.print_job.manually_requested").count() == 1


def test_cancelled_kitchen_ticket_is_rejected_even_when_order_is_active(client, tenant, auth_headers):
    order, automatic = ready_jobs(client, tenant, auth_headers)
    with SessionLocal.begin() as db:
        ticket = db.scalar(select(KitchenTicket).where(KitchenTicket.order_id == order["id"]))
        ticket.status = "cancelled"
        ticket.version += 1
        ticket_id, ticket_version = ticket.id, ticket.version
    current = client.get(f"/api/v1/orders/{order['id']}", headers=auth_headers).json()
    response = manual_request(client, current, auth_headers, {
        "job_type": "kitchen_ticket", "expected_order_version": current["version"],
        "kitchen_ticket_id": ticket_id, "expected_ticket_version": ticket_version,
    })
    assert response.status_code == 409
    assert len(jobs(client, current, auth_headers)["items"]) == len(automatic)


@pytest.mark.parametrize("change", ["amount", "destination", "status"])
def test_cancelled_manual_receipt_still_rejects_later_content_changes(client, tenant, auth_headers, change):
    order, _ = ready_jobs(client, tenant, auth_headers)
    with SessionLocal.begin() as db:
        db.get(Order, order["id"]).status = "cancelled"
    current = client.get(f"/api/v1/orders/{order['id']}", headers=auth_headers).json()
    response = manual_request(client, current, auth_headers)
    assert response.status_code == 201, response.text
    job = response.json()
    with SessionLocal.begin() as db:
        row = db.get(Order, order["id"])
        if change == "amount":
            row.total += Decimal("1")
        elif change == "destination":
            row.customer_name = "Another customer"
        else:
            row.status = "sent_to_kitchen"
    assert claim(client, job, auth_headers).status_code == 409
    listed = next(row for row in jobs(client, current, auth_headers)["items"] if row["id"] == job["id"])
    assert listed["status"] == "cancelled" and listed["payload"] == job["payload"]


def test_manual_transaction_failure_does_not_leave_job(client, tenant, auth_headers, monkeypatch):
    order, _ = ready_jobs(client, tenant, auth_headers)
    current = client.get(f"/api/v1/orders/{order['id']}", headers=auth_headers).json()
    from app import pos_printing_api
    def fail(*args):
        raise HTTPException(503, "Simulated audit failure")
    monkeypatch.setattr(pos_printing_api, "_audit", fail)
    assert manual_request(client, current, auth_headers).status_code == 503
    assert len(jobs(client, current, auth_headers)["items"]) == 2
