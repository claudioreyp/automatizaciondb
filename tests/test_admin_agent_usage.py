"""Synthetic reporting only: no provider, printer, commercial write or live DB."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import DBAPIError

from app import admin_agent_usage as reporting
from app.database import SessionLocal, engine
from app.models import AgentConversation, AgentModelUsage, Branch

BASE = "/api/v1/admin/agent-usage"
HEADERS = {"X-Dev-Auth": "test-token", "X-Dev-Role": "superadmin", "X-Dev-User": "usage-admin"}
PARAMS = {"from": "2026-11-15T00:00:00-05:00", "to": "2026-11-16T00:00:00-05:00"}
START = datetime(2026, 11, 15, 5, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clear_report_cache():
    with reporting._CACHE_LOCK:
        reporting._CACHE.clear()
    yield
    with reporting._CACHE_LOCK:
        reporting._CACHE.clear()


def add_call(db, conversation, *, status="settled", known=True, input=100, cached=20, creation=0, output=10,
    reserved=1000, model="gpt-6.1-terra", message=None, at=None):
    ident = uuid4().hex
    when = at or START + timedelta(hours=1)
    row = AgentModelUsage(id=str(uuid4()), business_id=conversation.business_id, branch_id=conversation.branch_id,
        conversation_id=conversation.id, message_id=message or ident, call_id=ident,
        sender_hash="hidden-sender", request_fingerprint="a" * 64, reserve_fingerprint="b" * 64,
        reserve_key=ident, model_id=model, role="composer", window_start=when.replace(hour=0, minute=0, second=0, microsecond=0),
        window_end=when.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1),
        reserved_tokens=reserved, charged_tokens=(input + cached + creation + output) if status == "settled" and known else 0 if status == "denied" else reserved,
        status=status, usage_known=known, input_tokens=input, output_tokens=output,
        cache_read_input_tokens=cached, cache_creation_input_tokens=creation, created_at=when, updated_at=when)
    db.add(row)
    return row


@pytest.fixture
def usage(tenant):
    with SessionLocal.begin() as db:
        branch2 = Branch(business_id=tenant["business_id"], name="Second branch", slug="second")
        db.add(branch2); db.flush()
        first = AgentConversation(business_id=tenant["business_id"], branch_id=tenant["branch_id"], sender="hidden-phone")
        second = AgentConversation(business_id=tenant["business_id"], branch_id=branch2.id, sender="hidden-other-phone")
        other = AgentConversation(business_id=tenant["other_business_id"], branch_id=tenant["other_branch_id"], sender="hidden-lid@lid")
        db.add_all([first, second, other]); db.flush()
        add_call(db, first, message="shared-turn")
        add_call(db, first, model="gpt-6-luna", input=5, cached=0, output=2, message="shared-turn")
        add_call(db, first, input=200000, cached=72000, output=30)
        add_call(db, first, input=200001, cached=72000, output=31)
        add_call(db, first, status="uncertain", known=False, input=0, cached=0, output=0, reserved=1000)
        add_call(db, first, status="reserved", known=None, input=0, cached=0, output=0, reserved=2000)
        add_call(db, first, status="denied", known=None, input=0, cached=0, output=0, model="blocked-model")
        add_call(db, first, status="settled", known=None, input=0, cached=0, output=0, reserved=123)
        add_call(db, second, input=300, cached=0, output=3)
        add_call(db, other, input=9000, cached=0, output=9)
        add_call(db, first, at=START-timedelta(microseconds=1), input=77777)
        add_call(db, first, at=START+timedelta(days=1), input=88888)
        return {**tenant, "second_branch_id": branch2.id}


def report(client, **params):
    return client.get(BASE, params={**PARAMS, **params}, headers=HEADERS)


def test_exact_metrics_unknowns_and_no_double_counted_turns(client, usage):
    response = report(client, business_id=usage["business_id"])
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json(); totals = body["totals"]
    assert totals["attempts"] == 9
    assert totals["settled_known_calls"] == 5
    assert totals["model_turns"] == 8
    assert {key: totals[key] for key in ["uncertain_calls", "reserved_calls", "denied_calls", "settled_unknown_calls"]} == {
        "uncertain_calls": 1, "reserved_calls": 1, "denied_calls": 1, "settled_unknown_calls": 1}
    assert totals["known_input_tokens"] == 400406
    assert totals["known_cache_read_input_tokens"] == 144020
    assert totals["known_output_tokens"] == 76
    assert totals["known_total_tokens"] == 544502
    assert totals["uncertain_reserved_tokens"] == 1000
    assert totals["open_reserved_tokens"] == 2000
    assert totals["settled_unknown_reserved_tokens"] == 123
    assert totals["quota_accounted_tokens"] == 547625
    for pii in ["hidden-phone", "hidden-lid", "hidden-sender", "conversation_id", "message_id", "reserve_key", "sender_hash"]:
        assert pii not in response.text


def test_bands_are_classified_per_call_including_cache_and_exact_boundary(client, usage):
    body = report(client, business_id=usage["business_id"], branch_id=usage["branch_id"]).json()
    groups = {(r["model_id"], r["input_band"]): r for r in body["pricing_buckets"]}
    assert groups["gpt-6.1-terra", "up_to_272000"]["known_calls"] == 2
    assert groups["gpt-6.1-terra", "over_272000"]["known_calls"] == 1
    assert groups["gpt-6.1-terra", "over_272000"]["input_tokens"] == 200001
    assert groups["gpt-6.1-terra", "over_272000"]["cache_read_input_tokens"] == 72000
    assert sum(r["known_calls"] for r in groups.values()) == 4
    assert all("cost" not in key and "rate" not in key for row in groups.values() for key in row)


def test_filters_and_available_models_ignore_only_model_filter(client, usage):
    response = report(client, business_id=usage["business_id"], branch_id=usage["branch_id"], model_id="gpt-6-luna")
    body = response.json()
    assert body["totals"]["attempts"] == body["totals"]["model_turns"] == 1
    assert body["metadata"]["available_models"] == ["blocked-model", "gpt-6-luna", "gpt-6.1-terra"]
    assert len(body["breakdown"]["rows"]) == 1
    second = report(client, branch_id=usage["second_branch_id"]).json()
    assert second["totals"]["known_total_tokens"] == 303
    assert second["metadata"]["available_models"] == ["gpt-6.1-terra"]


def test_pagination_never_changes_global_or_series_pricing(client, usage):
    first = report(client, page_size=1).json()
    second = report(client, page_size=1, page=2).json()
    assert first["totals"] == second["totals"]
    assert first["series"] == second["series"]
    assert first["pricing_buckets"] == second["pricing_buckets"]
    assert first["breakdown"]["total_rows"] == 3
    assert first["breakdown"]["total_pages"] == 3
    assert first["breakdown"]["rows"][0]["key"] != second["breakdown"]["rows"][0]["key"]
    assert all(r["pricing_buckets"] for r in [*first["breakdown"]["rows"], *second["breakdown"]["rows"]])


@pytest.mark.parametrize("group_by,key", [("business", "business_name"), ("branch", "branch_name"), ("model", "model_id")])
def test_grouping_and_names(client, usage, group_by, key):
    body = report(client, group_by=group_by).json()
    assert body["metadata"]["group_by"] == group_by
    assert all(row.get(key) for row in body["breakdown"]["rows"])
    assert sum(r["attempts"] for r in body["breakdown"]["rows"]) == body["totals"]["attempts"]
    if group_by == "model":
        assert sum(r["model_turns"] for r in body["breakdown"]["rows"]) > body["totals"]["model_turns"]


def test_lima_boundaries_and_timezone_change(client, usage):
    body = report(client, business_id=usage["business_id"], timezone="America/Lima").json()
    assert body["series"][0]["bucket_start"] == "2026-11-15T05:00:00Z"
    assert body["series"][0]["bucket_end"] == "2026-11-16T05:00:00Z"
    utc = report(client, business_id=usage["business_id"], timezone="UTC").json()
    assert utc["totals"] == body["totals"]
    assert utc["series"][0]["bucket_start"] == "2026-11-15T00:00:00Z"


def test_hourly_buckets_and_settlement_date_not_used(client, usage):
    body = report(client, business_id=usage["business_id"], interval="hour").json()
    assert body["series"][0]["bucket_start"] == "2026-11-15T06:00:00Z"
    assert body["series"][0]["bucket_end"] == "2026-11-15T07:00:00Z"
    assert body["series"][0]["settled_known_calls"] == 5


@pytest.mark.parametrize("params", [
    {"from": "2026-11-15T00:00:00"}, {"to": "2026-11-15T00:00:00-05:00"},
    {"to": "2026-12-17T00:00:00-05:00"}, {"to": "2026-11-23T00:00:00-05:00", "interval": "hour"},
    {"timezone": "No/Such_Zone"}, {"timezone": "../etc/passwd"}, {"page_size": 101}, {"page": 0},
    {"interval": "minute"}, {"group_by": "sender"}, {"model_id": "x' OR 1=1"},
])
def test_invalid_inputs(client, params):
    assert report(client, **params).status_code == 422


def test_missing_or_mismatched_scope_ids(client, usage):
    assert report(client, business_id=999999).status_code == 404
    assert report(client, branch_id=999999).status_code == 404
    assert report(client, business_id=usage["business_id"], branch_id=usage["other_branch_id"]).status_code == 422


def test_no_usage_is_complete_empty_not_an_error(client, tenant):
    body = report(client).json()
    assert body["metadata"]["complete"] is True
    assert body["totals"]["attempts"] == 0
    assert body["series"] == body["pricing_buckets"] == body["breakdown"]["rows"] == []


def test_auth_still_checked_for_cached_reports(client, usage):
    assert report(client).status_code == 200
    assert client.get(BASE, params=PARAMS).status_code == 401
    owner = {**HEADERS, "X-Dev-Role": "owner"}
    assert client.get(BASE, params=PARAMS, headers=owner).status_code == 403


def test_queries_do_not_write_meter_or_any_table(client, usage):
    writes = []
    def observe(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP")):
            writes.append(statement)
    with SessionLocal() as db:
        before = [(r.id, r.status, r.charged_tokens, r.updated_at) for r in db.scalars(select(AgentModelUsage).order_by(AgentModelUsage.id))]
    event.listen(engine, "before_cursor_execute", observe)
    try:
        assert report(client).status_code == 200
        assert report(client, group_by="model").status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    with SessionLocal() as db:
        after = [(r.id, r.status, r.charged_tokens, r.updated_at) for r in db.scalars(select(AgentModelUsage).order_by(AgentModelUsage.id))]
    assert before == after
    assert writes == []


def test_timeout_never_returns_partial_data(client, tenant, monkeypatch):
    values = iter([0, 6])
    monkeypatch.setattr(reporting, "monotonic", lambda: next(values, 6))
    response = report(client)
    assert response.status_code == 503
    assert "totals" not in response.json()
    assert not reporting._CACHE


def test_busy_reports_are_bounded(client, tenant):
    reporting._REPORT_SLOTS.acquire(); reporting._REPORT_SLOTS.acquire()
    try:
        response = report(client)
        assert response.status_code == 503
        assert response.headers["retry-after"] == "5"
    finally:
        reporting._REPORT_SLOTS.release(); reporting._REPORT_SLOTS.release()


def test_cache_is_private_bounded_and_does_not_retry_queries(client, usage, monkeypatch):
    monkeypatch.setattr(reporting, "_CACHE_LIMIT", 2)
    calls = []
    original = reporting._read_report
    def read(*args):
        calls.append(1)
        return original(*args)
    monkeypatch.setattr(reporting, "_read_report", read)
    assert report(client).status_code == report(client).status_code == 200
    assert len(calls) == 1
    assert report(client, group_by="model").status_code == 200
    assert report(client, group_by="business").status_code == 200
    assert len(reporting._CACHE) == 2


def test_group_cap_returns_error_instead_of_truncated_totals(client, usage, monkeypatch):
    monkeypatch.setattr(reporting, "_MAX_GROUPS", 1)
    assert report(client).status_code == 503


def test_cache_expires_and_remains_separate_by_authenticated_user(client, usage, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(reporting, "monotonic", lambda: clock[0])
    first = report(client).json()
    with SessionLocal.begin() as db:
        conversation = db.scalar(select(AgentConversation).where(AgentConversation.branch_id == usage["branch_id"]))
        add_call(db, conversation, input=9, cached=0, output=1)
    assert report(client).json()["totals"] == first["totals"]
    other = client.get(BASE, params=PARAMS, headers={**HEADERS, "X-Dev-User": "other-superadmin"})
    assert other.json()["totals"]["attempts"] == first["totals"]["attempts"] + 1
    clock[0] += 31
    assert report(client).json()["totals"]["attempts"] == first["totals"]["attempts"] + 1


def test_cache_creation_and_zero_usage_are_preserved_without_prices(client, tenant):
    with SessionLocal.begin() as db:
        conversation = AgentConversation(business_id=tenant["business_id"], branch_id=tenant["branch_id"], sender="synthetic-zero")
        db.add(conversation); db.flush()
        add_call(db, conversation, input=0, cached=0, output=0)
        add_call(db, conversation, input=271999, cached=0, creation=2, output=0)
    body = report(client).json()
    assert body["totals"]["settled_known_calls"] == 2
    assert body["totals"]["known_cache_creation_input_tokens"] == 2
    assert {r["input_band"]: r["known_calls"] for r in body["pricing_buckets"]} == {"up_to_272000": 1, "over_272000": 1}


@pytest.mark.parametrize("attribute", ["sqlstate", "pgcode"])
def test_timeout_code_is_recognized_for_both_postgres_drivers(client, tenant, monkeypatch, attribute):
    error = Exception("Synthetic cancelled query")
    setattr(error, attribute, "57014")
    def cancelled(*args):
        raise DBAPIError("SELECT", {}, error)
    monkeypatch.setattr(reporting, "_read_report", cancelled)
    response = report(client)
    assert response.status_code == 503
    assert "exceeded its time budget" in response.json()["detail"]
    assert not reporting._CACHE
