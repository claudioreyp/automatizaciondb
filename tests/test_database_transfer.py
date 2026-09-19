from datetime import datetime, timezone

from alembic import command
import pytest
import sqlalchemy as sa

from scripts.transfer_sqlite_to_postgres import (
    TransferError, assert_empty_destination, destination, fingerprint,
    migration_config, ordered_rows, read_rows, snapshot, sqlite_engine,
    validate_schema,
)


def test_snapshot_is_read_only_and_keeps_every_value(tmp_path):
    source = tmp_path / "source.db"
    engine = sa.create_engine(f"sqlite:///{source.as_posix()}")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE example (id INTEGER PRIMARY KEY, value TEXT)")
        connection.exec_driver_sql("INSERT INTO example VALUES (5, 'original')")
    copied = snapshot(source, tmp_path / "backups")
    backup = sqlite_engine(copied)
    with backup.connect() as connection:
        assert connection.exec_driver_sql("SELECT * FROM example").all() == [(5, "original")]
        with pytest.raises(sa.exc.OperationalError):
            connection.exec_driver_sql("DELETE FROM example")
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM example").scalar() == 1
    backup.dispose()
    engine.dispose()


def test_json_null_sql_null_and_utc_are_preserved():
    engine = sa.create_engine("sqlite:///:memory:")
    table = sa.Table("sample", sa.MetaData(), sa.Column("id", sa.Integer, primary_key=True),
                     sa.Column("data", sa.JSON(none_as_null=True)), sa.Column("date", sa.DateTime(timezone=True)))
    table.create(engine)
    stamp = datetime(2026, 9, 13, 3, 18)
    with engine.begin() as connection:
        connection.execute(table.insert(), [{"id": 1, "data": None, "date": stamp},
                                           {"id": 2, "data": sa.JSON.NULL, "date": stamp}])
        rows = read_rows(connection, table)
        assert rows[0]["data"] is None
        assert rows[1]["data"] is sa.JSON.NULL
        assert rows[0]["date"] == stamp.replace(tzinfo=timezone.utc)
        changed = [dict(row, data=None) for row in rows]
        assert fingerprint(rows, table) != fingerprint(changed, table)
    engine.dispose()


def test_self_references_are_inserted_parent_first():
    table = sa.Table("items", sa.MetaData(), sa.Column("id", sa.Integer, primary_key=True),
                     sa.Column("previous", sa.ForeignKey("items.id")))
    rows = [{"id": 3, "previous": 2}, {"id": 2, "previous": 1}, {"id": 1, "previous": None}]
    assert [row["id"] for row in ordered_rows(table, rows)] == [1, 2, 3]
    assert rows[0]["id"] == 3
    with pytest.raises(TransferError, match="Unresolved"):
        ordered_rows(table, [{"id": 1, "previous": 2}, {"id": 2, "previous": 1}])


def test_migrations_use_supplied_connection_and_unknown_columns_block_transfer():
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        command.upgrade(migration_config(connection), "head")
        validate_schema(connection, source=True)
        connection.exec_driver_sql("ALTER TABLE businesses ADD COLUMN unexpected TEXT")
        with pytest.raises(TransferError, match="Column mismatch"):
            validate_schema(connection, source=True)
    engine.dispose()


@pytest.mark.parametrize("url", [
    "sqlite:///local.db",
    "postgresql://postgres:unused@evil.example/postgres?sslmode=require",
    "postgresql://postgres.wrong:unused@aws-0-us-west-2.pooler.supabase.com:5432/postgres?sslmode=require",
    "postgresql://postgres:unused@db.abcdefghijklmnopqrst.supabase.co:6543/postgres?sslmode=require",
    "postgresql://postgres:unused@db.abcdefghijklmnopqrst.supabase.co/postgres?sslmode=disable",
    "postgresql://postgres:unused@db.abcdefghijklmnopqrst.supabase.co/postgres?sslmode=require&options=unsafe",
])
def test_destination_refuses_other_projects_and_unsafe_connections(tmp_path, url):
    config = tmp_path / "private.env"
    config.write_text(f"DATABASE_URL={url}\nSUPABASE_URL=https://abcdefghijklmnopqrst.supabase.co\n")
    with pytest.raises(TransferError):
        destination(config, "abcdefghijklmnopqrst")


def test_temporary_migration_connection_is_not_runtime_connection(tmp_path):
    config = tmp_path / "private.env"
    config.write_text("DATABASE_URL=postgresql://postgres:YOUR-PASSWORD@db.abcdefghijklmnopqrst.supabase.co/postgres?sslmode=require\n"
                      "MIGRATION_DATABASE_URL=postgresql://cli_login_postgres.abcdefghijklmnopqrst:unused@aws-0-us-west-2.pooler.supabase.com:5432/postgres?sslmode=require\n"
                      "SUPABASE_URL=https://abcdefghijklmnopqrst.supabase.co\n")
    engine = destination(config, "abcdefghijklmnopqrst")
    assert engine.url.username == "cli_login_postgres.abcdefghijklmnopqrst"
    engine.dispose()


def test_copy_refuses_non_postgresql_destination():
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.connect() as connection, pytest.raises(TransferError, match="PostgreSQL"):
        assert_empty_destination(connection)
    engine.dispose()
