"""PostgreSQL-only checks run solely in the guarded local ASGI test cluster."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.database import engine
from app import admin_agent_usage as reporting
from test_admin_agent_usage import HEADERS, BASE, add_call, report, usage, clear_report_cache
from app.database import SessionLocal
from app.models import AgentConversation

pytestmark = pytest.mark.skipif(engine.dialect.name != "postgresql", reason="Requires isolated local PostgreSQL")


def test_report_uses_read_only_repeatable_transaction_and_local_deadline(client, usage):
    observed = []
    def observe(connection, cursor, statement, parameters, context, executemany):
        if statement == "SET TRANSACTION READ ONLY":
            observed.append("read_only")
            assert connection.get_isolation_level() == "REPEATABLE READ"
        if "set_config('statement_timeout'" in statement:
            observed.append("timeout")
            assert 0 < int(parameters["timeout"]) <= 5000
    event.listen(engine, "before_cursor_execute", observe)
    try:
        response = report(client)
        assert response.status_code == 200, response.text
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert "read_only" in observed and "timeout" in observed
    with engine.connect() as connection:
        assert connection.execute(text("SHOW transaction_read_only")).scalar() == "off"
        assert connection.execute(text("SHOW statement_timeout")).scalar() == "0"


def test_hourly_dst_fallback_does_not_merge_repeated_hours(client, tenant):
    with SessionLocal.begin() as db:
        conv = AgentConversation(business_id=tenant["business_id"], branch_id=tenant["branch_id"], sender="synthetic-dst")
        db.add(conv); db.flush()
        add_call(db, conv, at=datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc))
        add_call(db, conv, at=datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc))
    response = client.get(BASE, headers=HEADERS, params={"from": "2026-11-01T00:00:00-04:00",
        "to": "2026-11-02T00:00:00-05:00", "timezone": "America/New_York", "interval": "hour"})
    assert response.status_code == 200, response.text
    assert [row["bucket_start"] for row in response.json()["series"]] == ["2026-11-01T05:00:00Z", "2026-11-01T06:00:00Z"]


def test_actual_statement_timeout_returns_503_and_does_not_leak_to_pool(client, usage, monkeypatch):
    original = Session.execute
    slept = []
    def delayed(self, statement, *args, **kwargs):
        if "FROM agent_model_usage" in str(statement) and not slept:
            slept.append(True)
            original(self, text("SELECT pg_sleep(6)"))
        return original(self, statement, *args, **kwargs)
    monkeypatch.setattr(Session, "execute", delayed)
    response = report(client)
    assert response.status_code == 503, response.text
    assert "exceeded its time budget" in response.json()["detail"]
    assert "totals" not in response.json()
    assert not reporting._CACHE
    with engine.connect() as connection:
        assert connection.execute(text("SHOW statement_timeout")).scalar() == "0"
        assert connection.execute(text("SHOW transaction_read_only")).scalar() == "off"
    assert report(client).status_code == 200
