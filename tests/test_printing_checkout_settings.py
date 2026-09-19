import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.encoders import jsonable_encoder
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api import start_table_checkout_endpoint
from app.auth import AuthContext
from app.database import Base, SessionLocal
from app.models import (
    AuditEvent, Branch, BranchSettings, Business, DiningArea, IdempotencyRecord,
    IntegrationEvent, KitchenTicket, Membership, Order, OrderItem, Payment,
    PrintJob, RestaurantTable,
)
from app.schemas import OrderCommand
from app import pos_printing
from test_command_workflow import create_order, send_order
from test_pos_printing import claim, claim_body, complete, enable, jobs, manual_request


def profile_url(tenant):
    return f"/api/v1/settings/branches/{tenant['branch_id']}/printing"


def save_profile(client, tenant, headers, **fields):
    version = client.get(profile_url(tenant), headers=headers).json()["version"]
    return client.patch(profile_url(tenant), json={
        "advanced_printing": True, "expected_version": version, **fields,
    }, headers={**headers, "Idempotency-Key": str(uuid4())})


def checkout(client, order, headers, action="start", key=None):
    current = client.get(f"/api/v1/orders/{order['id']}", headers=headers).json()
    return client.post(f"/api/v1/orders/{order['id']}/table-checkout/{action}",
        json={"expected_version": current["version"]},
        headers={**headers, "Idempotency-Key": key or str(uuid4())})


def close_receipt(client, order, headers):
    response = checkout(client, order, headers)
    assert response.status_code == 200, response.text
    receipts = [job for job in jobs(client, order, headers)["items"] if job["job_type"] == "customer_receipt"]
    return response.json()["order"], receipts[-1]


def test_printing_defaults_are_read_only_and_do_not_enable_legacy_flags(client, tenant, auth_headers):
    enable(tenant)
    with SessionLocal.begin() as db:
        settings = db.scalar(select(BranchSettings).where(BranchSettings.branch_id == tenant["branch_id"]))
        settings.advanced_printing = False
        settings.printer_config = {"printer_name": "Original", "custom_driver": "keep"}
        settings.customer_ticket_template = {"legacy": "keep"}
        before = (settings.version, deepcopy(settings.printer_config), deepcopy(settings.customer_ticket_template))
    result = client.get(profile_url(tenant), headers=auth_headers).json()
    assert result["advanced_printing"] is False
    assert result["printer_config"] == {**before[1], "automatic_printing": True}
    assert "auto_print_kitchen" not in result["printer_config"]
    assert "manual_customer_receipt" not in result["printer_config"]
    assert result["customer_ticket_template"] == {
        "legacy": "keep", "font_size": "normal", "header_enabled": False, "header_text": "",
        "footer_enabled": False, "footer_text": "",
    }
    assert result["kitchen_ticket_template"] == {"font_size": "normal"}
    with SessionLocal() as db:
        settings = db.scalar(select(BranchSettings).where(BranchSettings.branch_id == tenant["branch_id"]))
        assert (settings.version, settings.printer_config, settings.customer_ticket_template) == before


def test_legacy_settings_save_preserves_extensions_and_explicit_reset_works(client, tenant, auth_headers):
    enable(tenant, copies=5, print_language="escpos")
    config = client.get(profile_url(tenant), headers=auth_headers).json()["printer_config"]
    customer = {"font_size": "large", "header_enabled": True, "header_text": "Welcome\nTable service",
                "footer_enabled": True, "footer_text": "Thanks", "legacy_option": 123}
    saved = save_profile(client, tenant, auth_headers,
        printer_config={**config, "automatic_printing": False},
        customer_ticket_template=customer, kitchen_ticket_template={"font_size": "small"})
    assert saved.status_code == 200, saved.text
    body = {"advanced_printing": True, "expected_version": saved.json()["version"],
            "printer_config": {key: value for key, value in config.items() if key != "automatic_printing"},
            "customer_ticket_template": {}, "kitchen_ticket_template": {}}
    headers = {**auth_headers, "Idempotency-Key": "legacy-settings-save"}
    legacy = client.patch(profile_url(tenant), json=body, headers=headers)
    assert legacy.status_code == 200, legacy.text
    result = legacy.json()
    assert result["printer_config"] == {**body["printer_config"], "automatic_printing": False}
    assert result["customer_ticket_template"] == {key: value for key, value in customer.items() if key != "legacy_option"}
    assert result["kitchen_ticket_template"] == {"font_size": "small"}
    assert client.patch(profile_url(tenant), json=body, headers=headers).json() == result
    assert client.get(profile_url(tenant), headers=auth_headers).json() == result
    stale = client.patch(profile_url(tenant), json=body,
                         headers={**headers, "Idempotency-Key": "stale-profile"})
    assert stale.status_code == 409
    omitted = save_profile(client, tenant, auth_headers)
    assert omitted.json()["customer_ticket_template"] == result["customer_ticket_template"]
    assert omitted.json()["kitchen_ticket_template"] == result["kitchen_ticket_template"]
    assert omitted.json()["printer_config"]["automatic_printing"] is False
    cleared = save_profile(client, tenant, auth_headers, printer_config={"automatic_printing": True},
        customer_ticket_template={"font_size": "normal", "header_enabled": False, "header_text": "",
                                  "footer_enabled": False, "footer_text": ""},
        kitchen_ticket_template={"font_size": "normal"})
    assert cleared.status_code == 200
    assert cleared.json()["customer_ticket_template"]["header_text"] == ""
    with SessionLocal() as db:
        assert db.query(AuditEvent).filter(AuditEvent.action == "settings.printing.updated").count() == 4


@pytest.mark.parametrize("field,key,value", [
    ("printer_config", "automatic_printing", value) for value in [None, 0, 1, "false", {}, []]
] + [
    (field, "font_size", value)
    for field in ["customer_ticket_template", "kitchen_ticket_template"]
    for value in [None, "medium", "NORMAL", 12, {}, []]
] + [
    ("customer_ticket_template", key, value)
    for key in ["header_enabled", "footer_enabled"] for value in [None, 0, "true"]
] + [
    ("customer_ticket_template", key, value)
    for key in ["header_text", "footer_text"] for value in [None, 1, "x" * 501, "bad\x00text"]
])
def test_printing_extensions_reject_invalid_values_without_writes(client, tenant, auth_headers, field, key, value):
    enable(tenant)
    before = client.get(profile_url(tenant), headers=auth_headers).json()
    response = save_profile(client, tenant, auth_headers, **{field: {key: value}})
    assert response.status_code == 422, response.text
    assert client.get(profile_url(tenant), headers=auth_headers).json() == before


@pytest.mark.parametrize("size", ["small", "normal", "large"])
def test_template_fonts_are_independent_and_frozen(client, tenant, auth_headers, size):
    enable(tenant, copies=3)
    config = client.get(profile_url(tenant), headers=auth_headers).json()["printer_config"]
    customer = {"font_size": size, "header_enabled": True, "header_text": "x" * 500,
                "footer_enabled": False, "footer_text": "Kept while disabled"}
    saved = save_profile(client, tenant, auth_headers, printer_config=config,
        customer_ticket_template=customer, kitchen_ticket_template={"font_size": "small"})
    assert saved.status_code == 200
    order = create_order(client, tenant, auth_headers)
    current = send_order(client, order, auth_headers)["order"]
    original = jobs(client, order, auth_headers)["items"]
    by_type = {job["job_type"]: job for job in original}
    assert by_type["customer_receipt"]["payload"]["template"] == customer
    assert by_type["kitchen_ticket"]["payload"]["template"] == {"font_size": "small"}
    assert all(job["payload"]["copies"] == 3 for job in original)
    updated = save_profile(client, tenant, auth_headers, printer_config={**config, "automatic_printing": False},
        customer_ticket_template={"font_size": "normal"}, kitchen_ticket_template={"font_size": "large"})
    assert updated.status_code == 200
    assert jobs(client, order, auth_headers)["items"] == original
    manual = manual_request(client, current, auth_headers)
    assert manual.status_code == 201
    assert manual.json()["payload"]["template"]["font_size"] == "normal"
    assert manual.json()["payload"]["template"]["header_text"] == customer["header_text"]


@pytest.mark.parametrize("overrides,advanced", [
    ({"automatic_printing": False}, True), ({"manual_customer_receipt": True}, True),
    ({"printer_name": ""}, True), ({}, False),
])
def test_checkout_respects_flags_and_does_not_backfill(client, tenant, auth_headers, overrides, advanced):
    enable(tenant, **overrides)
    with SessionLocal.begin() as db:
        db.scalar(select(BranchSettings).where(BranchSettings.branch_id == tenant["branch_id"])).advanced_printing = advanced
    order = create_order(client, tenant, auth_headers, dine_in=True)
    current = send_order(client, order, auth_headers)["order"]
    before = jobs(client, order, auth_headers)["items"]
    if overrides.get("automatic_printing") is False:
        assert before == []
        assert manual_request(client, current, auth_headers).status_code == 201
    if not advanced or overrides.get("printer_name") == "":
        assert manual_request(client, current, auth_headers).status_code == 409
    count = len(jobs(client, order, auth_headers)["items"])
    assert checkout(client, order, auth_headers).status_code == 200
    assert len(jobs(client, order, auth_headers)["items"]) == count
    enable(tenant)
    assert checkout(client, order, auth_headers).status_code == 200
    assert len(jobs(client, order, auth_headers)["items"]) == count


def test_mesa_commands_then_complete_checkout_account_no_pay_print(client, tenant, auth_headers):
    enable(tenant)
    with SessionLocal.begin() as db:
        db.add(Membership(auth_user_id="owner-test", business_id=tenant["business_id"],
                          branch_id=tenant["branch_id"], role="owner", full_name="Original Author"))
    order = create_order(client, tenant, auth_headers, dine_in=True)
    sent = send_order(client, order, auth_headers)
    first = jobs(client, order, auth_headers)["items"]
    assert len(first) == 1 and first[0]["job_type"] == "kitchen_ticket"
    with SessionLocal.begin() as db:
        table = db.get(RestaurantTable, tenant["table_id"])
        table.name = "MESA1"
        db.get(DiningArea, table.area_id).name = "Comedor principal"
        db.scalar(select(Membership).where(Membership.auth_user_id == "owner-test")).full_name = "Updated Author"
    added = client.post(f"/api/v1/orders/{order['id']}/item-batches", json={
        "expected_version": sent["order"]["version"],
        "items": [{"product_id": tenant["product_id"], "quantity": 2}],
    }, headers={**auth_headers, "Idempotency-Key": "table-addition"})
    assert added.status_code == 201, added.text
    commands = jobs(client, order, auth_headers)["items"]
    assert len(commands) == 2 and all(job["job_type"] == "kitchen_ticket" for job in commands)
    first_ticket = next(job for job in commands if job["id"] == first[0]["id"])["payload"]["ticket"]
    assert first_ticket["created_by_name"] == "Original Author"
    assert first_ticket["created_at"] == first[0]["payload"]["ticket"]["created_at"]
    assert first_ticket["context"]["area_name"] == "Salon"
    addition = next(job for job in commands if job["id"] != first[0]["id"])
    assert len(addition["payload"]["ticket"]["items"]) == 1
    assert addition["payload"]["ticket"]["items"][0]["quantity"] == 2
    assert addition["payload"]["ticket"]["created_by_name"] == "Updated Author"
    current, receipt = close_receipt(client, order, auth_headers)
    assert receipt["kitchen_ticket_id"] is None and receipt["payload"]["ticket"] is None
    assert len(receipt["payload"]["order"]["items"]) == 2
    assert receipt["payload"]["order"]["total"] == 60
    assert receipt["payload"]["remaining_amount"] == 60
    assert receipt["payload"]["order"]["table_context"]["table_name"] == "MESA1"
    assert receipt["payload"]["order"]["table_context"]["area_name"] == "Comedor principal"
    assert receipt["payload"]["order"]["created_at"] == first[0]["payload"]["order"]["created_at"]
    opened = client.post("/api/v1/cash/sessions/open", json={"register_id": tenant["register_id"], "opening_amount": 0}, headers=auth_headers)
    assert opened.status_code == 201, opened.text
    paid = client.post(f"/api/v1/orders/{order['id']}/table-checkout/pay", json={
        "expected_version": current["version"],
        "payments": [{"method": "cash", "amount": 60, "cash_session_id": opened.json()["id"]}],
    }, headers={**auth_headers, "Idempotency-Key": "table-payment"})
    assert paid.status_code == 200, paid.text
    after = jobs(client, order, auth_headers)["items"]
    assert len(after) == 3
    assert next(job for job in after if job["id"] == receipt["id"]) == receipt
    assert claim(client, receipt, auth_headers).json()["dispatch_allowed"] is True
    with SessionLocal.begin() as db:
        table = db.get(RestaurantTable, tenant["table_id"])
        table.name = "Renamed after checkout"
        db.get(DiningArea, table.area_id).name = "Renamed zone"
    manual = manual_request(client, paid.json()["order"], auth_headers)
    assert manual.status_code == 201
    assert manual.json()["payload"]["order"]["table_context"] == receipt["payload"]["order"]["table_context"]
    assert manual.json()["payload"]["paid_amount"] == 60


def test_checkout_without_kitchen_is_atomic_and_idempotent(client, tenant, auth_headers):
    enable(tenant)
    order = create_order(client, tenant, auth_headers, dine_in=True)
    body = {"expected_version": order["version"]}
    headers = {**auth_headers, "Idempotency-Key": "close-once"}
    url = f"/api/v1/orders/{order['id']}/table-checkout/start"
    closed = client.post(url, json=body, headers=headers)
    assert closed.status_code == 200, closed.text
    original = jobs(client, order, auth_headers)["items"]
    assert len(original) == 1
    detail = client.get(f"/api/v1/orders/{order['id']}/detail", headers=auth_headers).json()
    assert detail["table_context"] == original[0]["payload"]["order"]["table_context"]
    assert client.post(url, json=body, headers=headers).json() == closed.json()
    assert checkout(client, order, auth_headers).status_code == 200
    assert jobs(client, order, auth_headers)["items"] == original
    with SessionLocal() as db:
        assert db.query(KitchenTicket).count() == db.query(Payment).count() == db.query(IntegrationEvent).count() == 0
        assert db.query(AuditEvent).filter(AuditEvent.action == "table.checkout_started").count() == 1
        job = db.get(PrintJob, original[0]["id"])
        assert job.idempotency_key == f"pos-local:checkout:{order['id']}:{closed.json()['order']['version']}:customer_receipt"
        assert job.branch_id == tenant["branch_id"] and job.business_id == tenant["business_id"]


def test_checkout_insert_failure_rolls_back_only_checkout_and_same_key_retries(client, tenant, auth_headers):
    enable(tenant)
    order = create_order(client, tenant, auth_headers, dine_in=True)
    current = send_order(client, order, auth_headers)["order"]
    before = jobs(client, order, auth_headers)["items"]
    current = client.get(f"/api/v1/orders/{order['id']}", headers=auth_headers).json()
    def fail_checkout(mapper, connection, target):
        if target.payload.get("_trigger") == "table_checkout_started":
            raise SQLAlchemyError("isolated print insert failure")
    event.listen(PrintJob, "before_insert", fail_checkout)
    try:
        failed = checkout(client, order, auth_headers, key="close-retry")
        assert failed.status_code == 503, failed.text
    finally:
        event.remove(PrintJob, "before_insert", fail_checkout)
    assert client.get(f"/api/v1/orders/{order['id']}", headers=auth_headers).json() == current
    assert jobs(client, order, auth_headers)["items"] == before
    with SessionLocal() as db:
        assert db.query(KitchenTicket).count() == 1
        assert db.query(AuditEvent).filter(AuditEvent.action == "table.checkout_started").count() == 0
        assert db.query(IdempotencyRecord).filter(IdempotencyRecord.scope == f"table-checkout-start:{order['id']}").count() == 0
    assert checkout(client, order, auth_headers, key="close-retry").status_code == 200
    assert len(jobs(client, order, auth_headers)["items"]) == 2


@pytest.mark.parametrize("state,outcome", [("pending", None), ("not_sent", None), ("claimed", "printed"), ("claimed", "not_sent"), ("claimed", "unknown")])
def test_reopening_invalidates_only_unsent_checkout_preserving_late_ack(client, tenant, auth_headers, state, outcome):
    enable(tenant)
    order = create_order(client, tenant, auth_headers, dine_in=True)
    _, original = close_receipt(client, order, auth_headers)
    body = claim_body()
    if state != "pending":
        assert claim(client, original, auth_headers, body).json()["dispatch_allowed"]
    if state == "not_sent":
        assert complete(client, original, auth_headers, body, "not_sent").json()["retryable"]
    assert checkout(client, order, auth_headers, "reopen").status_code == 200
    _, newer = close_receipt(client, order, auth_headers)
    assert newer["id"] != original["id"]
    if outcome:
        result = complete(client, original, auth_headers, body, outcome, "late-ack")
        assert result.status_code == 200, result.text
        expected = {"printed": "printed", "not_sent": "cancelled", "unknown": "failed"}[outcome]
        assert result.json()["status"] == expected
        assert complete(client, original, auth_headers, body, outcome, "late-ack").json() == result.json()
    listed = {job["id"]: job for job in jobs(client, order, auth_headers)["items"]}
    assert listed[original["id"]]["payload"] == original["payload"]
    assert not listed[original["id"]]["retryable"]
    assert claim(client, original, auth_headers, claim_body(retry_not_sent=True)).status_code == 409
    assert claim(client, newer, auth_headers).json()["dispatch_allowed"]


@pytest.mark.parametrize("completed", [False, True])
def test_revision_hook_ids_and_only_new_commands_get_recoverable_intents(client, tenant, auth_headers, monkeypatch, completed):
    enable(tenant)
    order = create_order(client, tenant, auth_headers, dine_in=True)
    sent = send_order(client, order, auth_headers)
    ticket = sent["tickets"][0]
    if completed:
        response = client.post(f"/api/v1/kitchen/commands/{ticket['id']}/complete", json={
            "expected_status": ticket["status"], "expected_version": ticket["version"],
        }, headers={**auth_headers, "Idempotency-Key": "complete-for-revision"})
        assert response.status_code == 200, response.text
    current = client.get(f"/api/v1/orders/{order['id']}", headers=auth_headers).json()
    original_insert = pos_printing._insert_jobs
    def unavailable(*args):
        raise SQLAlchemyError("isolated recovery test")
    monkeypatch.setattr(pos_printing, "_insert_jobs", unavailable)
    body = {"expected_version": current["version"], "operations": [{
        "type": "edit", "item_id": current["items"][0]["id"], "reason": "Two portions",
        "replacement": {"product_id": tenant["product_id"], "quantity": 2},
    }]}
    headers = {**auth_headers, "Idempotency-Key": "revision-printing"}
    response = client.post(f"/api/v1/orders/{order['id']}/item-revisions", json=body, headers=headers)
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["order"]["branch_id"] == tenant["branch_id"]
    assert all(row["branch_id"] == tenant["branch_id"] for row in result["tickets"])
    assert len(result["created_ticket_ids"]) == (1 if completed else 0)
    assert len(result["updated_ticket_ids"]) == (0 if completed else 1)
    if completed:
        with SessionLocal() as db:
            new_ticket = db.get(KitchenTicket, result["created_ticket_ids"][0])
            intent = deepcopy(new_ticket.context_snapshot["_pos_printing"])
            assert [row["job_type"] for row in intent["jobs"]] == ["kitchen_ticket"]
            assert db.query(PrintJob).count() == 1
        assert jobs(client, order, auth_headers)["recoverable_error"] is True
    monkeypatch.setattr(pos_printing, "_insert_jobs", original_insert)
    recovered = jobs(client, order, auth_headers)
    assert not recovered["recoverable_error"]
    assert len(recovered["items"]) == (2 if completed else 1)
    assert all(row["job_type"] == "kitchen_ticket" for row in recovered["items"])
    if completed:
        job = next(row for row in recovered["items"] if row["kitchen_ticket_id"] == result["created_ticket_ids"][0])
        assert job["payload"] == intent["jobs"][0]["payload"]
    replay = client.post(f"/api/v1/orders/{order['id']}/item-revisions", json=body, headers=headers)
    assert replay.status_code == 201
    assert replay.json()["created_ticket_ids"] == result["created_ticket_ids"]
    assert len(jobs(client, order, auth_headers)["items"]) == len(recovered["items"])


def test_new_table_zone_snapshot_is_scoped_and_cannot_leak_another_branch(client, tenant, auth_headers):
    enable(tenant)
    with SessionLocal.begin() as db:
        area = DiningArea(business_id=tenant["other_business_id"], branch_id=tenant["other_branch_id"], name="Private zone")
        db.add(area)
        db.flush()
        db.get(RestaurantTable, tenant["table_id"]).area_id = area.id
    order = create_order(client, tenant, auth_headers, dine_in=True)
    send_order(client, order, auth_headers)
    _, receipt = close_receipt(client, order, auth_headers)
    for job in jobs(client, order, auth_headers)["items"]:
        context = job["payload"]["order"]["table_context"]
        assert context["table_name"] == "Mesa 1"
        assert context["area_id"] is None and context["area_name"] is None
    other_headers = {**auth_headers, "X-Business-Id": str(tenant["other_business_id"]), "X-Branch-Id": str(tenant["other_branch_id"])}
    assert claim(client, receipt, other_headers).status_code == 404


def test_historical_kitchen_context_never_guesses_current_area_or_actor(client, tenant, auth_headers):
    enable(tenant)
    order = create_order(client, tenant, auth_headers, dine_in=True)
    sent = send_order(client, order, auth_headers)
    ticket = sent["tickets"][0]
    with SessionLocal.begin() as db:
        row = db.get(KitchenTicket, ticket["id"])
        row.context_snapshot = {"table_id": tenant["table_id"], "table_name": "Recorded table", "created_by": "owner-test"}
        db.add(Membership(auth_user_id="owner-test", business_id=tenant["business_id"],
                          branch_id=tenant["branch_id"], role="owner", full_name="Current name is not evidence"))
    response = manual_request(client, sent["order"], auth_headers, {
        "job_type": "kitchen_ticket", "expected_order_version": sent["order"]["version"],
        "kitchen_ticket_id": ticket["id"], "expected_ticket_version": ticket["version"],
    })
    assert response.status_code == 201, response.text
    payload = response.json()["payload"]
    assert payload["table_name"] == "Recorded table"
    assert payload["order"]["table_context"]["area_name"] is None
    assert payload["created_by_name"] is None
    assert datetime.fromisoformat(payload["ticket"]["created_at"]) == datetime.fromisoformat(ticket["created_at"])


@pytest.mark.parametrize("recording", ["recorded", "null", "empty", "legacy_intent", "unrecorded"])
def test_manual_kitchen_keeps_recorded_destination_context_not_current_order(client, tenant, auth_headers, recording):
    enable(tenant)
    order = create_order(client, tenant, auth_headers)
    original = {"channel": "takeaway", "source": "pos", "customer_name": "Original customer",
                "customer_phone": "000000001", "delivery_address": {"address": "Original destination"},
                "notes": "Original preparation note"}
    with SessionLocal.begin() as db:
        row = db.get(Order, order["id"])
        for key, value in original.items():
            setattr(row, key, value)
    sent = send_order(client, order, auth_headers)
    ticket = sent["tickets"][0]
    expected = {**original, "order_number": order["number"], "order_folio": order["folio"]}
    current_destination = {"channel": "counter", "source": "whatsapp_agent", "customer_name": "Current customer",
                           "customer_phone": "000000002", "delivery_address": {"address": "Current destination"},
                           "notes": "Current preparation note"}
    with SessionLocal.begin() as db:
        stored_ticket = db.get(KitchenTicket, ticket["id"])
        context = deepcopy(stored_ticket.context_snapshot)
        if recording == "recorded":
            assert {key: context[key] for key in expected} == expected
        elif recording in {"null", "empty"}:
            expected = {key: None if recording == "null" else "" for key in expected}
            if recording == "empty":
                expected.update(delivery_address={}, order_folio=None)
            context.update(expected)
        else:
            for key in expected:
                context.pop(key, None)
            if recording == "unrecorded":
                context.pop("_pos_printing", None)
                expected = {key: None for key in expected}
        stored_ticket.context_snapshot = context
        stored_before = deepcopy(context)
        row = db.get(Order, order["id"])
        for key, value in current_destination.items():
            setattr(row, key, value)
        row.version += 1
    current = client.get(f"/api/v1/orders/{order['id']}", headers=auth_headers).json()
    body = {"job_type": "kitchen_ticket", "expected_order_version": current["version"],
            "kitchen_ticket_id": ticket["id"], "expected_ticket_version": ticket["version"]}
    response = manual_request(client, current, auth_headers, body, key="frozen-kitchen-context")
    assert response.status_code == 201, response.text
    job = response.json()
    actual = job["payload"]["ticket"]["context"]
    assert {key: actual[key] for key in expected} == expected
    assert {key: job["payload"]["order"][key] for key in current_destination} == current_destination
    assert datetime.fromisoformat(job["payload"]["ticket"]["created_at"]) == datetime.fromisoformat(ticket["created_at"])
    assert job["payload"]["ticket"]["items"] == ticket["items"]
    assert manual_request(client, current, auth_headers, body, key="frozen-kitchen-context").json() == job
    assert claim(client, job, auth_headers).json()["dispatch_allowed"] is True
    receipt = manual_request(client, current, auth_headers)
    assert receipt.status_code == 201
    assert {key: receipt.json()["payload"]["order"][key] for key in current_destination} == current_destination
    with SessionLocal() as db:
        assert db.get(KitchenTicket, ticket["id"]).context_snapshot == stored_before


def test_concurrent_checkout_uses_one_document_and_replay(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'checkout-concurrent.db'}",
                           connect_args={"check_same_thread": False, "timeout": 10})
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            business = Business(slug="checkout", name="Checkout")
            db.add(business)
            db.flush()
            branch = Branch(business_id=business.id, slug="main", name="Main")
            db.add(branch)
            db.flush()
            table = RestaurantTable(business_id=business.id, branch_id=branch.id, code="M1", name="Mesa 1")
            db.add(table)
            db.flush()
            db.add(BranchSettings(business_id=business.id, branch_id=branch.id, advanced_printing=True,
                printer_config={"printer_name": "POS", "manual_customer_receipt": False}))
            order = Order(business_id=business.id, branch_id=branch.id, number="TEST", folio=1,
                          channel="dine_in", table_id=table.id, source="pos", subtotal=20, total=20)
            order.items = [OrderItem(product_name="Pizza", quantity=1, unit_price=20, line_total=20)]
            db.add(order)
            db.commit()
            order_id, version = order.id, order.version
            user = AuthContext("owner", "owner", business.id, branch.id)
        barrier = Barrier(2)
        def start(_):
            with Session(engine, autoflush=False, expire_on_commit=False) as db:
                barrier.wait()
                return asyncio.run(start_table_checkout_endpoint(order_id, OrderCommand(expected_version=version), "same-close", user, db))
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(start, range(2)))
        assert jsonable_encoder(responses[0]) == jsonable_encoder(responses[1])
        with Session(engine) as db:
            assert db.query(PrintJob).count() == 1
            assert db.query(IdempotencyRecord).count() == 1
            assert db.query(AuditEvent).filter(AuditEvent.action == "table.checkout_started").count() == 1
    finally:
        engine.dispose()
