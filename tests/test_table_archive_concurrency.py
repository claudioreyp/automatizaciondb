import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
from threading import Barrier
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.orm import sessionmaker

from app.api import delete_table
from app.auth import AuthContext
from app.database import Base
from app.models import AuditEvent, Branch, Business, DiningArea, Order, Reservation, RestaurantTable, utcnow
from app.schemas import ArchiveRequest, OrderCreate, ReservationCreate
from app.services import create_order, create_reservation


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
@pytest.mark.parametrize("operation", ["order", "reservation", "archive"])
def test_table_archive_serializes_with_opening_and_reservations(tmp_path, dialect, operation):
    schema = None
    admin = None
    if dialect == "postgresql":
        url = os.environ.get("POS_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("POS_TEST_POSTGRES_URL is required for an isolated PostgreSQL schema")
        admin = sa.create_engine(url)
        schema = "test_table_archive_" + uuid4().hex
        with admin.begin() as db:
            db.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        engine = sa.create_engine(url, execution_options={"schema_translate_map": {None: schema}})
    else:
        engine = sa.create_engine(f"sqlite+pysqlite:///{tmp_path / 'archive-race.db'}", connect_args={"check_same_thread": False, "timeout": 15})
    sessions = sessionmaker(engine, autoflush=False, expire_on_commit=False)
    try:
        Base.metadata.create_all(engine)
        with sessions.begin() as db:
            if schema:
                # Verify schema_translate_map before any test insertion.
                assert db.scalar(sa.select(sa.func.count()).select_from(Business)) == 0
                assert sa.inspect(engine).has_table("restaurant_tables", schema=schema)
            business = Business(slug="isolated-archive", name="Isolated archive")
            db.add(business)
            db.flush()
            branch = Branch(business_id=business.id, slug="main", name="Main")
            db.add(branch)
            db.flush()
            area = DiningArea(business_id=business.id, branch_id=branch.id, name="Salon")
            db.add(area)
            db.flush()
            table = RestaurantTable(business_id=business.id, branch_id=branch.id, area_id=area.id, code="M1", name="Mesa 1", capacity=4)
            db.add(table)
            db.flush()
            table_id, branch_id = table.id, branch.id
            user = AuthContext("isolated-owner", "owner", business.id, branch.id)
        barrier = Barrier(2)

        def archive():
            with sessions() as db:
                barrier.wait(timeout=5)
                try:
                    asyncio.run(delete_table(table_id, ArchiveRequest(expected_version=1), "one-archive", user, db))
                    return "archived"
                except HTTPException as error:
                    assert error.status_code == 409
                    return "blocked"

        def competing():
            if operation == "archive":
                return archive()
            with sessions() as db:
                barrier.wait(timeout=5)
                try:
                    if operation == "order":
                        create_order(db, user, OrderCreate(branch_id=branch_id, channel="dine_in", table_id=table_id, items=[]))
                    else:
                        create_reservation(db, user, ReservationCreate(branch_id=branch_id, customer_name="Isolated", customer_phone="51900000000", party_size=2, start_at=utcnow(), table_ids=[table_id]))
                    db.commit()
                    return "created"
                except HTTPException as error:
                    assert error.status_code == 409
                    return "blocked"

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(archive), pool.submit(competing)
            results = [first.result(timeout=20), second.result(timeout=20)]
        with sessions() as db:
            archived = db.get(RestaurantTable, table_id).archived_at is not None
            active = db.scalar(sa.select(sa.func.count()).select_from(Order if operation == "order" else Reservation))
            if operation == "archive":
                assert results == ["archived", "archived"]  # Second caller replays the ledger.
            else:
                assert sorted(results) in [["archived", "blocked"], ["blocked", "created"]]
                assert (active, archived) in [(0, True), (1, False)]
            assert db.scalar(sa.select(sa.func.count(AuditEvent.id)).where(AuditEvent.action == "table.archived")) == int(archived)
    finally:
        engine.dispose()
        if schema:
            with admin.begin() as db:
                db.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
            admin.dispose()
