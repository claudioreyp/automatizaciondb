import importlib.util
from pathlib import Path

import sqlalchemy as sa


def load_cash_cut_migration():
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "20260831_0017_cash_cuts.py"
    )
    spec = importlib.util.spec_from_file_location("cash_cut_migration_0017", migration_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_historical_payments_are_backfilled_only_for_one_matching_period():
    migration = load_cash_cut_migration()
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE orders (id INTEGER PRIMARY KEY, business_id INTEGER, branch_id INTEGER)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE cash_sessions ("
            "id INTEGER PRIMARY KEY, business_id INTEGER, branch_id INTEGER, "
            "opened_at DATETIME, closed_at DATETIME)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE payments ("
            "id INTEGER PRIMARY KEY, order_id INTEGER, cash_session_id INTEGER, "
            "status VARCHAR(30), received_at DATETIME)"
        )
        connection.exec_driver_sql(
            "INSERT INTO orders (id, business_id, branch_id) VALUES "
            "(1, 1, 10), (2, 1, 20)"
        )
        connection.exec_driver_sql(
            "INSERT INTO cash_sessions "
            "(id, business_id, branch_id, opened_at, closed_at) VALUES "
            "(101, 1, 10, '2026-08-31 08:00:00', '2026-08-31 16:00:00'), "
            "(201, 1, 20, '2026-08-31 08:00:00', '2026-08-31 16:00:00'), "
            "(202, 1, 20, '2026-08-31 12:00:00', '2026-08-31 20:00:00')"
        )
        connection.exec_driver_sql(
            "INSERT INTO payments "
            "(id, order_id, cash_session_id, status, received_at) VALUES "
            "(1, 1, NULL, 'confirmed', '2026-08-31 10:00:00'), "
            "(2, 2, NULL, 'confirmed', '2026-08-31 13:00:00'), "
            "(3, 1, NULL, 'pending', '2026-08-31 10:00:00'), "
            "(4, 1, NULL, 'confirmed', '2026-09-01 10:00:00')"
        )

        migration._backfill_unambiguous_payments(connection)

        rows = dict(
            connection.execute(
                sa.text("SELECT id, cash_session_id FROM payments ORDER BY id")
            ).all()
        )
        assert rows == {1: 101, 2: None, 3: None, 4: None}
