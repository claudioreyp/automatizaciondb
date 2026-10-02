import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Branch, Business, DiningArea, Order, Reservation, ReservationTable, RestaurantTable, utcnow


def test_additive_table_archive_migration_preserves_rows_constraints_and_references(tmp_path):
    engine = sa.create_engine(f"sqlite+pysqlite:///{tmp_path / 'archive-migration.db'}")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            business = Business(slug="migration-only", name="Migration only")
            db.add(business)
            db.flush()
            branch = Branch(business_id=business.id, slug="main", name="Main")
            db.add(branch)
            db.flush()
            area = DiningArea(business_id=business.id, branch_id=branch.id, name="Salon")
            db.add(area)
            db.flush()
            table = RestaurantTable(business_id=business.id, branch_id=branch.id, area_id=area.id, code="M1", name="Mesa 1", version=7)
            db.add(table)
            db.flush()
            order = Order(business_id=business.id, branch_id=branch.id, table_id=table.id, number="HISTORY", channel="dine_in", status="closed")
            reservation = Reservation(business_id=business.id, branch_id=branch.id, customer_name="Historical", customer_phone="51900000000", party_size=2, start_at=utcnow(), end_at=utcnow(), status="completed")
            db.add_all([order, reservation])
            db.flush()
            db.add(ReservationTable(reservation_id=reservation.id, table_id=table.id))
            db.commit()
        with engine.begin() as db:
            # Bootstrap uses live metadata: reconstruct the actual pre-0025 shape.
            db.exec_driver_sql("DROP INDEX ix_restaurant_tables_archived_at")
            db.exec_driver_sql("ALTER TABLE restaurant_tables DROP COLUMN archived_at")
            names = sa.inspect(db).get_table_names()
            before = {name: [dict(row) for row in db.execute(sa.text(f'SELECT * FROM "{name}"')).mappings()] for name in names}
            constraints = sa.inspect(db).get_unique_constraints("restaurant_tables")
            references = sa.inspect(db).get_foreign_keys("restaurant_tables")
            path = Path(__file__).resolve().parents[1] / "migrations/versions/20261002_0025_table_archives.py"
            spec = importlib.util.spec_from_file_location("table_archive_migration", path)
            migration = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(migration)
            with Operations.context(MigrationContext.configure(db)):
                migration.upgrade()
                migration.upgrade()  # Fresh metadata/bootstrap compatibility.
                migration.downgrade()  # Rollback must keep markers and identities.
            assert sa.inspect(db).get_table_names() == names
            for name in names:
                after = [dict(row) for row in db.execute(sa.text(f'SELECT * FROM "{name}"')).mappings()]
                if name == "restaurant_tables":
                    assert all(row.pop("archived_at") is None for row in after)
                assert after == before[name]
            assert sa.inspect(db).get_unique_constraints("restaurant_tables") == constraints
            assert sa.inspect(db).get_foreign_keys("restaurant_tables") == references
            assert "ix_restaurant_tables_archived_at" in {index["name"] for index in sa.inspect(db).get_indexes("restaurant_tables")}
            assert db.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()
