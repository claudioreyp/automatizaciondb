"""Cash races and migration preservation in a disposable PostgreSQL schema.

Requires POS_TEST_POSTGRES_URL. Never uses the configured operational database.
Every ORM operation is qualified with schema_translate_map.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from datetime import datetime
from decimal import Decimal
import os
from pathlib import Path
from threading import Barrier, Event
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.auth import AuthContext
from app.database import Base
from app.models import AuditEvent, Branch, Business, CashMovement, CashRegister, CashSession, IdempotencyRecord, Order, Payment


@pytest.fixture
def cash_pg():
    url = os.environ.get("POS_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Requires explicitly configured isolated PostgreSQL")
    schema = "test_cash_" + uuid4().hex
    admin = sa.create_engine(url, hide_parameters=True)
    with admin.begin() as db:
        db.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = sa.create_engine(url, hide_parameters=True, execution_options={"schema_translate_map": {None: schema}},
        connect_args={"options": "-c lock_timeout=8000 -c statement_timeout=15000"})
    sessions = sessionmaker(engine)
    try:
        Base.metadata.create_all(engine)
        with sessions.begin() as db:
            business = Business(slug="cash-race", name="Cash race")
            db.add(business); db.flush()
            branch = Branch(business_id=business.id, slug="main", name="Main")
            db.add(branch); db.flush()
            register = CashRegister(business_id=business.id, branch_id=branch.id, name="Principal", is_default=True)
            db.add(register); db.flush()
            period = CashSession(business_id=business.id, branch_id=branch.id, register_id=register.id,
                opening_amount=0, opened_by="test-owner", version=1)
            db.add(period); db.flush()
            order = Order(business_id=business.id, branch_id=branch.id, number="TEST-CASH", source="pos",
                channel="counter", status="confirmed", payment_status="partial", subtotal=100, total=100, version=1)
            db.add(order); db.flush()
            db.add(Payment(business_id=business.id, order_id=order.id, cash_session_id=period.id,
                method="cash", amount=20, status="confirmed", created_by="test-owner"))
            ids = {"order": order.id, "register": register.id, "period": period.id, "schema": schema}
            user = AuthContext("test-owner", "owner", business.id, branch.id)
        yield sessions, user, ids
    finally:
        engine.dispose()
        with admin.begin() as db:
            db.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()


def cancellation(ids, **changes):
    from app.schemas import OrderCancel
    return OrderCancel(**({"reason": "Cliente solicita devolución", "expected_version": 1,
        "register_id": ids["register"], "expected_session_id": ids["period"], "expected_cash_version": 1,
        "refund_confirmed": True, "refunds": [{"method": "card", "amount": 20}]} | changes))


def test_concurrent_cancel_replays_exactly_one_refund(cash_pg):
    from app.api import cancel_order_and_refund
    sessions, user, ids = cash_pg
    barrier = Barrier(4)
    def cancel(_):
        with sessions() as db:
            barrier.wait(timeout=5)
            return asyncio.run(cancel_order_and_refund(ids["order"], cancellation(ids),
                idempotency_key="same-refund", user=user, db=db))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(cancel, range(4)))
    assert all(jsonable_encoder(item) == jsonable_encoder(results[0]) for item in results)
    with sessions() as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(CashMovement)) == 1
        assert db.scalar(sa.select(sa.func.count()).select_from(Payment)) == 1
        assert db.scalar(sa.select(sa.func.count()).select_from(AuditEvent).where(AuditEvent.action == "order.cancelled")) == 1
        assert db.scalar(sa.select(sa.func.count()).select_from(IdempotencyRecord)) == 1
        assert db.get(Order, ids["order"]).total == 100
        assert db.get(Payment, 1).status == "confirmed"
        assert db.get(CashSession, ids["period"]).version == 2


def test_payment_racing_cancel_cannot_over_refund_or_charge_cancelled(cash_pg):
    from app.api import cancel_order_and_refund, create_payment_endpoint
    from app.schemas import PaymentCreate
    sessions, user, ids = cash_pg
    barrier = Barrier(2)
    def operation(kind):
        with sessions() as db:
            barrier.wait(timeout=5)
            try:
                if kind == "cancel":
                    result = asyncio.run(cancel_order_and_refund(ids["order"], cancellation(ids),
                        idempotency_key="cancel-race", user=user, db=db))
                else:
                    result = asyncio.run(create_payment_endpoint(ids["order"],
                        PaymentCreate(method="cash", amount=80, register_id=ids["register"], expected_version=1),
                        idempotency_key="payment-race", user=user, db=db))
                return kind, result
            except HTTPException as error:
                return kind, error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = dict(pool.map(operation, ["cancel", "payment"]))
    assert sum(isinstance(item, dict) for item in results.values()) == 1
    with sessions() as db:
        order = db.get(Order, ids["order"])
        total_paid = db.scalar(sa.select(sa.func.sum(Payment.amount)).where(Payment.status == "confirmed"))
        total_refunded = db.scalar(sa.select(sa.func.coalesce(sa.func.sum(CashMovement.amount), 0)))
        if order.status == "cancelled":
            assert total_paid == total_refunded == 20
        else:
            assert total_paid == 100 and total_refunded == 0


def test_implicit_payment_waiting_for_cut_uses_new_period_without_deadlock(cash_pg):
    from app.api import create_cash_cut, create_payment_endpoint
    from app.schemas import CashCutCreate, PaymentCreate
    sessions, user, ids = cash_pg
    candidate_read = Event()
    worker_connection = {"thread": None}
    from threading import get_ident
    def observe(conn, cursor, statement, parameters, context, executemany):
        if get_ident() == worker_connection["thread"] and "cash_registers" in statement and "FOR UPDATE" in statement:
            candidate_read.set()
    sa.event.listen(sessions.kw["bind"], "before_cursor_execute", observe)
    try:
        with sessions() as cut_db, ThreadPoolExecutor(max_workers=1) as pool:
            cut_db.scalar(sa.select(CashRegister).where(CashRegister.id == ids["register"]).with_for_update())
            def pay():
                worker_connection["thread"] = get_ident()
                with sessions() as db:
                    return asyncio.run(create_payment_endpoint(ids["order"], PaymentCreate(method="cash", amount=80),
                        idempotency_key="implicit-payment", user=user, db=db))
            future = pool.submit(pay)
            assert candidate_read.wait(timeout=5), "Payment did not reach register lock"
            cut = create_cash_cut(ids["register"], CashCutCreate(cash_counted=20, retained_fund=0,
                expected_version=1, expected_session_id=ids["period"], ignore_pending_orders=True),
                idempotency_key="racing-cut", user=user, db=cut_db)
            payment = future.result(timeout=12)
            assert payment["payment"]["cash_session_id"] == cut["next_period"]["id"]
            assert payment["payment"]["cash_session_id"] != ids["period"]
        with sessions() as db:
            assert db.get(CashSession, ids["period"]).expected_amount == 20
            assert db.scalar(sa.select(sa.func.count()).select_from(Payment)) == 2
    finally:
        sa.event.remove(sessions.kw["bind"], "before_cursor_execute", observe)


def test_refund_after_period_changes_rolls_back_whole_cancellation(cash_pg):
    from app.api import cancel_order_and_refund, create_cash_cut
    from app.schemas import CashCutCreate
    sessions, user, ids = cash_pg
    with sessions() as db:
        create_cash_cut(ids["register"], CashCutCreate(cash_counted=20, expected_session_id=ids["period"],
            expected_version=1, ignore_pending_orders=True), idempotency_key="advance-period", user=user, db=db)
    with sessions() as db, pytest.raises(HTTPException) as blocked:
        asyncio.run(cancel_order_and_refund(ids["order"], cancellation(ids), idempotency_key="old-refund", user=user, db=db))
    assert blocked.value.status_code == 409
    with sessions() as db:
        assert db.get(Order, ids["order"]).status == "confirmed"
        assert db.scalar(sa.select(sa.func.count()).select_from(CashMovement)) == 0
        assert db.scalar(sa.select(sa.func.count()).select_from(AuditEvent).where(AuditEvent.action == "order.cancelled")) == 0
        assert db.get(CashSession, ids["period"]).expected_amount == 20


@pytest.mark.parametrize("kind", ["cut", "movement"])
def test_concurrent_cash_retry_replays_one_operation(cash_pg, kind):
    from app.api import create_cash_cut, create_register_movement
    from app.schemas import CashCutCreate, CashRegisterMovementCreate
    sessions, user, ids = cash_pg
    barrier = Barrier(3)
    def operation(_):
        with sessions() as db:
            barrier.wait(timeout=5)
            if kind == "cut":
                return create_cash_cut(ids["register"], CashCutCreate(cash_counted=20,
                    expected_session_id=ids["period"], expected_version=1, ignore_pending_orders=True),
                    idempotency_key="same-cut", user=user, db=db)
            return create_register_movement(ids["register"], CashRegisterMovementCreate(movement_type="withdrawal",
                amount=3, note="Retiro confirmado", expected_session_id=ids["period"], expected_version=1),
                idempotency_key="same-movement", user=user, db=db)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(operation, range(3)))
    assert all(jsonable_encoder(item) == jsonable_encoder(results[0]) for item in results)
    with sessions() as db:
        if kind == "cut":
            assert db.scalar(sa.select(sa.func.count()).select_from(CashSession).where(CashSession.status == "closed")) == 1
            assert db.scalar(sa.select(sa.func.count()).select_from(CashSession).where(CashSession.status == "open")) == 1
        else:
            assert db.scalar(sa.select(sa.func.count()).select_from(CashMovement)) == 1
            assert db.get(CashSession, ids["period"]).version == 2


def test_cash_migration_preserves_every_original_column_and_row(cash_pg):
    sessions, _, ids = cash_pg
    engine = sessions.kw["bind"]
    with engine.connect() as connection, connection.begin():
        schema = ids["schema"]
        connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')
        assert connection.scalar(sa.text("SELECT current_schema()")) == schema
        connection.exec_driver_sql(f'ALTER TABLE "{schema}".cash_movements DROP COLUMN order_id CASCADE')
        connection.exec_driver_sql(f"INSERT INTO \"{schema}\".cash_movements (cash_session_id,movement_type,payment_method,amount,note,created_at) VALUES ({ids['period']},'withdrawal','cash',15.25,'Original withdrawal',CURRENT_TIMESTAMP)")
        inspector = sa.inspect(connection)
        columns = {name: [column["name"] for column in inspector.get_columns(name, schema=schema)]
            for name in inspector.get_table_names(schema=schema)}
        def snapshot():
            return {name: sorted(str(row) for row in connection.exec_driver_sql(
                'SELECT ' + ','.join('"' + column + '"' for column in selected) + f' FROM "{schema}"."{name}"'))
                for name, selected in columns.items()}
        before = snapshot()
        path = Path(__file__).resolve().parents[1] / "migrations/versions/20261004_0026_cash_order_refunds.py"
        spec = importlib.util.spec_from_file_location("cash_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        assert snapshot() == before
        assert connection.scalar(sa.text(f'SELECT order_id FROM "{schema}".cash_movements')) is None
        assert "uq_cash_movements_order_refund_method" in {index["name"] for index in sa.inspect(connection).get_indexes("cash_movements", schema=schema)}
        # Repeating the additive revision remains harmless to all existing rows.
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        assert snapshot() == before


def test_private_backup_restores_then_migrates_without_value_changes():
    """Optional full-data rehearsal; private backup never enters the test output."""
    from scripts.prepare_cash_release import digest, encoded_rows
    url, backup_path = os.environ.get("POS_TEST_POSTGRES_URL"), os.environ.get("POS_TEST_CASH_BACKUP")
    if not url or not backup_path:
        pytest.skip("Requires isolated PostgreSQL and explicitly selected private backup")
    saved = json.loads(Path(backup_path).read_text(encoding="utf-8"))
    assert saved["version"] == "20261002_0025"
    schema = "test_cash_restore_" + uuid4().hex
    admin = sa.create_engine(url, hide_parameters=True)
    with admin.begin() as db:
        db.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = sa.create_engine(url, hide_parameters=True, execution_options={"schema_translate_map": {None: schema}})
    def decode(item):
        kind, *tail = item
        if kind == "sql_null": return None
        if kind == "json_null": return sa.JSON.NULL
        if kind == "datetime": return datetime.fromisoformat(tail[0])
        if kind == "decimal": return Decimal(tail[0])
        assert kind == "value"
        return tail[0]
    try:
        with engine.connect() as db, db.begin():
            db.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')
            assert db.scalar(sa.text("SELECT current_schema()")) == schema
            Base.metadata.create_all(db)
            db.exec_driver_sql(f'ALTER TABLE "{schema}".cash_movements DROP COLUMN order_id CASCADE')
            for table in Base.metadata.sorted_tables:
                item = saved["tables"][table.name]
                expected_columns = set(table.c.keys()) - ({"order_id"} if table.name == "cash_movements" else set())
                assert set(item["columns"]) == expected_columns, table.name
                rows = [dict(zip(item["columns"], map(decode, row))) for row in item["values"]]
                if rows:
                    # Dependencies and self-references keep their exact IDs.
                    pk = list(table.primary_key.columns)
                    rows.sort(key=lambda row: tuple(row[column.name] for column in pk))
                    bindings = {column.name: sa.bindparam(column.name, type_=sa.JSON(none_as_null=True))
                        for column in table.c if isinstance(column.type, sa.JSON)}
                    statement = table.insert().values(bindings) if bindings else table.insert()
                    db.execute(statement, rows)
            db.exec_driver_sql(f'CREATE TABLE "{schema}".alembic_version (version_num varchar(32) PRIMARY KEY)')
            db.exec_driver_sql(f"INSERT INTO \"{schema}\".alembic_version VALUES ('20261002_0025')")
            def compare_originals():
                for name, item in saved["tables"].items():
                    table = sa.Table(name, sa.MetaData(), schema=schema, autoload_with=db, resolve_fks=False)
                    # Only original columns participate in the preservation check.
                    original = sa.Table(name, sa.MetaData(), *(table.c[column]._copy() for column in item["columns"]), schema=schema)
                    assert digest(encoded_rows(db, original)) == item["sha256"], name
            compare_originals()
            spec = importlib.util.spec_from_file_location("cash_migration", Path(__file__).resolve().parents[1] / "migrations/versions/20261004_0026_cash_order_refunds.py")
            migration = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(migration)
            with Operations.context(MigrationContext.configure(db)):
                migration.upgrade()
            compare_originals()
            assert db.scalar(sa.text(f'SELECT count(*) FROM "{schema}".cash_movements WHERE order_id IS NOT NULL')) == 0
    finally:
        engine.dispose()
        with admin.begin() as db:
            db.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()
