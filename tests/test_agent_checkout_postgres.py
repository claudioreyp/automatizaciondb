"""Isolated PostgreSQL rehearsal: never uses the application's schema."""
import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Branch, Business, Order, PaymentEvidence


def test_concurrent_fee_confirmation_replays_one_request_and_blocks_dispatch():
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from fastapi import HTTPException
    from fastapi.encoders import jsonable_encoder
    from sqlalchemy.orm import sessionmaker
    from app.agent_checkout_api import DeliveryFee, set_delivery_fee
    from app.agent_checkout import ensure_dispatch
    from app.auth import AuthContext
    from app.models import OrderPaymentRequest

    url = os.environ.get("POS_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Requires isolated PostgreSQL")
    schema = "test_checkout_race_" + uuid4().hex
    admin = sa.create_engine(url)
    with admin.begin() as db:
        db.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = sa.create_engine(url, execution_options={"schema_translate_map": {None: schema}})
    sessions = sessionmaker(engine)
    try:
        Base.metadata.create_all(engine)
        with sessions.begin() as db:
            business = Business(slug="isolated-race", name="Isolated race")
            db.add(business); db.flush()
            branch = Branch(business_id=business.id, slug="main", name="Main")
            db.add(branch); db.flush()
            order = Order(business_id=business.id, branch_id=branch.id, number="TEST-RACE",
                source="whatsapp_agent", channel="delivery", status="ready", payment_method="cash",
                subtotal=20, total=20, delivery_fee_status="pending_quote")
            db.add(order); db.flush()
            order_id, version = order.id, order.version
            user = AuthContext("isolated-owner", "owner", business.id, branch.id)
        def confirm(_):
            with sessions() as db:
                return asyncio.run(set_delivery_fee(order_id, DeliveryFee(expected_version=version, amount=5),
                    idempotency_key="same-fee-operation", user=user, db=db))
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(confirm, range(3)))
        assert all(jsonable_encoder(result) == jsonable_encoder(results[0]) for result in results)
        with sessions() as db:
            assert db.scalar(sa.select(sa.func.count()).select_from(OrderPaymentRequest)) == 1
            order = db.get(Order, order_id)
            assert order.total == 25
            with pytest.raises(HTTPException) as blocked:
                ensure_dispatch(db, order)
            assert blocked.value.status_code == 409
    finally:
        engine.dispose()
        with admin.begin() as db:
            db.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()


def test_checkout_migration_preserves_every_preexisting_value():
    url = os.environ.get("POS_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Requires isolated PostgreSQL")
    schema = "test_checkout_migration_" + uuid4().hex
    engine = sa.create_engine(url)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
                connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')
                assert connection.scalar(sa.text("SELECT current_schema()")) == schema
                scoped = connection.execution_options(schema_translate_map={None: schema})
                Base.metadata.create_all(scoped)
                with Session(scoped, join_transaction_mode="create_savepoint") as db:
                    business = Business(slug="isolated-migration", name="Preserve original")
                    db.add(business); db.flush()
                    branch = Branch(business_id=business.id, slug="main", name="Original branch")
                    db.add(branch); db.flush()
                    order = Order(business_id=business.id, branch_id=branch.id, number="TEST-OLD", channel="counter", source="whatsapp_agent", total=20)
                    db.add(order); db.flush()
                    db.add(PaymentEvidence(business_id=business.id, order_id=order.id,
                        provider="yape", storage_path="private-original.png", image_sha256="a"*64,
                        operation_number="unchanged", security_code="012", status="under_review"))
                    db.commit()
                connection.exec_driver_sql(f'ALTER TABLE "{schema}".payment_evidence DROP COLUMN payment_request_id CASCADE')
                connection.exec_driver_sql(f'DROP TABLE "{schema}".order_payment_requests')
                connection.exec_driver_sql(f'CREATE UNIQUE INDEX uq_payment_evidence_one_open_per_order ON "{schema}".payment_evidence(order_id) WHERE status IN (\'evidence_received\',\'under_review\')')
                inspector = sa.inspect(connection)
                columns = {table: [c["name"] for c in inspector.get_columns(table, schema=schema)] for table in inspector.get_table_names(schema=schema)}
                def snapshot():
                    return {table: sorted(str(row) for row in connection.exec_driver_sql(
                        'SELECT ' + ','.join('"'+c+'"' for c in names) + f' FROM "{schema}"."{table}"').all())
                        for table, names in columns.items()}
                before = snapshot()
                path = Path(__file__).resolve().parents[1] / "migrations/versions/20260920_0024_agent_checkout.py"
                spec = importlib.util.spec_from_file_location("checkout_migration", path)
                migration = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(migration)
                with Operations.context(MigrationContext.configure(connection)):
                    migration.upgrade()
                assert snapshot() == before
                assert connection.scalar(sa.text(f'SELECT count(*) FROM "{schema}".order_payment_requests')) == 0
                assert connection.scalar(sa.text(f'SELECT payment_request_id FROM "{schema}".payment_evidence')) is None
                assert connection.scalar(sa.text("SELECT relrowsecurity FROM pg_class WHERE oid = CAST(:table AS regclass)"), {"table":schema+".order_payment_requests"})
            finally:
                transaction.rollback()
    finally:
        engine.dispose()
