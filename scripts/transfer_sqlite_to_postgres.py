"""Verified, insert-only transfer to an empty PostgreSQL database. No Auth changes.

Run as python -m scripts.transfer_sqlite_to_postgres --help. Never pass secrets
as command-line arguments; the target connection comes from a private env file.
"""

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from dotenv import dotenv_values
import sqlalchemy as sa
from sqlalchemy.engine import make_url

from app.database import Base
from app import models  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


class TransferError(RuntimeError):
    """An actionable error safe to display without rows, credentials or SQL."""


def migration_config(connection=None):
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def sqlite_engine(path):
    path = Path(path).resolve(strict=True)
    return sa.create_engine(f"sqlite+pysqlite:///{path.as_uri()}?mode=ro&uri=true")


def snapshot(source, directory):
    source = Path(source).resolve(strict=True)
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / ("supabase-source-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".db")
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src:
        with sqlite3.connect(dest) as target:
            src.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise TransferError("SQLite integrity check failed; no transfer allowed.")
            if target.execute("PRAGMA foreign_key_check").fetchone():
                raise TransferError("SQLite has broken references; no transfer allowed.")
    return dest


def validate_schema(connection, *, source=False):
    inspector = sa.inspect(connection)
    tables = set(inspector.get_table_names())
    expected = set(Base.metadata.tables) | {"alembic_version"}
    if tables != expected:
        raise TransferError("Unexpected table set; manual schema reconciliation required.")
    head = ScriptDirectory.from_config(migration_config()).get_current_head()
    if connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalars().all() != [head]:
        raise TransferError("Database revision differs from Alembic head; stop before copying.")
    all_columns = inspector.get_multi_columns(filter_names=list(Base.metadata.tables))
    for table in Base.metadata.sorted_tables:
        columns = {column["name"] for column in all_columns[(None, table.name)]}
        if columns != set(table.columns.keys()):
            raise TransferError(f"Column mismatch in {table.name}; no columns will be discarded.")
    if source:
        if connection.exec_driver_sql("PRAGMA integrity_check").scalar() != "ok":
            raise TransferError("Source integrity check failed.")
        if connection.exec_driver_sql("PRAGMA foreign_key_check").first():
            raise TransferError("Source contains broken references.")


def read_rows(connection, table):
    json_columns = [column for column in table.c if isinstance(column.type, sa.JSON)]
    null_flags = [column.is_(None).label("__sql_null_" + column.name) for column in json_columns]
    statement = sa.select(table, *null_flags).order_by(*table.primary_key.columns)
    rows = []
    for raw in connection.execute(statement).mappings():
        row = {column.name: raw[column.name] for column in table.c}
        for column in json_columns:
            if row[column.name] is None and not raw["__sql_null_" + column.name]:
                row[column.name] = sa.JSON.NULL
        for column in table.c:
            value = row[column.name]
            if isinstance(value, datetime):
                row[column.name] = (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc))
        rows.append(row)
    return rows


def canonical(value):
    if value is sa.JSON.NULL:
        return ["json_null"]
    if value is None:
        return ["sql_null"]
    if isinstance(value, datetime):
        return ["datetime", value.isoformat()]
    if isinstance(value, Decimal):
        return ["decimal", str(value)]
    return ["value", value]


def fingerprint(rows, table):
    encoded = json.dumps([[canonical(row[column.name]) for column in table.c] for row in rows],
                         sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def inventory(connection):
    return {table.name: {"rows": len(rows), "sha256": fingerprint(rows, table)}
            for table in Base.metadata.sorted_tables
            for rows in [read_rows(connection, table)]}


def ordered_rows(table, rows):
    self_refs = [fk for fk in table.foreign_keys if fk.column.table is table]
    if not self_refs:
        return rows
    remaining = list(rows)
    ordered = []
    seen = {fk.column.name: set() for fk in self_refs}
    while remaining:
        ready = [row for row in remaining if all(
            row[fk.parent.name] is None or row[fk.parent.name] in seen[fk.column.name]
            or row[fk.parent.name] == row[fk.column.name] for fk in self_refs
        )]
        if not ready:
            raise TransferError(f"Unresolved self references in {table.name}.")
        for row in ready:
            remaining.remove(row)
            ordered.append(row)
            for name in seen:
                seen[name].add(row[name])
    return ordered


def assert_empty_destination(connection):
    if connection.dialect.name != "postgresql":
        raise TransferError("Destination must be PostgreSQL.")
    if sa.inspect(connection).get_table_names(schema="public"):
        raise TransferError("Destination public schema is not empty; refusing to overwrite or merge.")
    if connection.scalar(sa.text("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('v','m','S','f','p')")):
        raise TransferError("Destination contains existing public objects; manual review required.")


def secure_tables(connection):
    # FastAPI owns tenant authorization; browser roles have no direct table access.
    roles = list(connection.execute(sa.text("SELECT rolname FROM pg_roles WHERE rolname IN ('anon','authenticated')")).scalars())
    statements = []
    for name in sorted(set(Base.metadata.tables) | {"alembic_version"}):
        quoted = connection.dialect.identifier_preparer.quote(name)
        statements.append(f"ALTER TABLE public.{quoted} ENABLE ROW LEVEL SECURITY")
        statements.append(f"REVOKE ALL ON TABLE public.{quoted} FROM PUBLIC")
        for role in roles:
            statements.append(f"REVOKE ALL ON TABLE public.{quoted} FROM {role}")
    sequences = connection.execute(sa.text("SELECT sequencename FROM pg_sequences WHERE schemaname='public'")).scalars()
    for name in sequences:
        quoted = connection.dialect.identifier_preparer.quote(name)
        statements.append(f"REVOKE ALL ON SEQUENCE public.{quoted} FROM PUBLIC")
        for role in roles:
            statements.append(f"REVOKE ALL ON SEQUENCE public.{quoted} FROM {role}")
    connection.exec_driver_sql(";\n".join(statements))


def verify_security(connection):
    insecure = connection.scalar(sa.text("""
        SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind='r' AND (
            NOT c.relrowsecurity OR EXISTS (
                SELECT 1 FROM pg_roles r WHERE r.rolname IN ('anon','authenticated')
                AND has_table_privilege(r.oid,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE')
            )
        )
    """))
    if insecure:
        raise TransferError("Destination tables are not protected from direct browser access.")
    if connection.scalar(sa.text("""
        SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind='S' AND EXISTS (
            SELECT 1 FROM pg_roles r WHERE r.rolname IN ('anon','authenticated')
            AND has_sequence_privilege(r.oid,c.oid,'USAGE,SELECT,UPDATE')
        )
    """)):
        raise TransferError("Destination sequences allow direct browser access.")


def align_sequences(connection, source):
    old_sequences = {}
    if "sqlite_sequence" in source.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").scalars():
        old_sequences = dict(source.exec_driver_sql("SELECT name,seq FROM sqlite_sequence").all())
    for table in Base.metadata.sorted_tables:
        if len(table.primary_key.columns) != 1:
            continue
        column = next(iter(table.primary_key.columns))
        if not isinstance(column.type, sa.Integer):
            continue
        sequence = connection.scalar(sa.text("SELECT pg_get_serial_sequence(:table, :column)"),
                                     {"table": "public." + table.name, "column": column.name})
        if sequence:
            highest = max(connection.scalar(sa.select(sa.func.max(column))) or 0, old_sequences.get(table.name, 0))
            connection.execute(sa.text("SELECT setval(CAST(:sequence AS regclass), :value, :used)"),
                               {"sequence": sequence, "value": max(1, highest), "used": highest > 0})


def verify(source, target):
    validate_schema(source, source=True)
    validate_schema(target)
    expected = inventory(source)
    actual = inventory(target)
    mismatched = [name for name in expected if expected[name] != actual[name]]
    if mismatched:
        raise TransferError("Content differs in: " + ", ".join(mismatched))
    verify_security(target)
    return actual


def transfer(source_engine, target_engine, *, commit=True):
    with source_engine.connect() as source:
        validate_schema(source, source=True)
        with target_engine.begin() as target:
            if target.scalar(sa.text("SELECT current_user")) == "cli_login_postgres":
                # Temporary CLI credentials must not become the permanent table owner.
                target.exec_driver_sql("SET LOCAL ROLE postgres")
            target.exec_driver_sql("SET LOCAL lock_timeout = '10s'")
            target.exec_driver_sql("SET LOCAL statement_timeout = '120s'")
            target.exec_driver_sql("SET LOCAL TIME ZONE 'UTC'")
            if not target.scalar(sa.text("SELECT pg_try_advisory_xact_lock(1936748403)")):
                raise TransferError("Another transfer is running; do not retry concurrently.")
            assert_empty_destination(target)
            command.upgrade(migration_config(target), "head")
            secure_tables(target)
            for table in Base.metadata.sorted_tables:
                rows = ordered_rows(table, read_rows(source, table))
                if rows:
                    # Preserve SQL NULL and JSON null as distinct values.
                    bindings = {column.name: sa.bindparam(column.name, type_=sa.JSON(none_as_null=True))
                                for column in table.c if isinstance(column.type, sa.JSON)}
                    statement = table.insert().values(bindings) if bindings else table.insert()
                    target.execute(statement, rows)
            align_sequences(target, source)
            result = verify(source, target)
            if not commit:
                target.rollback()
            # Return only counts and digests. Never serialize row data or secrets.
            return result


def destination(env_file, project):
    if not re.fullmatch(r"[a-z]{20}", project):
        raise TransferError("Expected a Supabase project reference of 20 letters.")
    settings = dotenv_values(env_file)
    raw = settings.get("MIGRATION_DATABASE_URL") or settings.get("DATABASE_URL")
    if not raw:
        raise TransferError("Complete DATABASE_URL in the private target env file.")
    try:
        url = make_url(raw)
    except Exception:
        raise TransferError("DATABASE_URL is not a valid PostgreSQL URL.") from None
    if url.get_backend_name() not in {"postgres", "postgresql"}:
        raise TransferError("DATABASE_URL must be a PostgreSQL URL.")
    roles = {"postgres", "cli_login_postgres"}
    direct = url.host == f"db.{project}.supabase.co" and url.username in roles
    pooler = bool(re.fullmatch(r"aws-[0-9]+-[a-z0-9-]+\.pooler\.supabase\.com", url.host or "")) and url.username in {f"{role}.{project}" for role in roles}
    if not (direct or pooler) or url.database != "postgres" or url.port not in {None, 5432}:
        raise TransferError("Target must be this project's direct or session-pooler connection on port 5432.")
    if settings.get("SUPABASE_URL") != f"https://{project}.supabase.co":
        raise TransferError("SUPABASE_URL and the confirmed project do not match.")
    if not url.password or url.password in {"YOUR-PASSWORD", "[YOUR-PASSWORD]"}:
        raise TransferError("The private target file still needs the database password.")
    if url.query.get("sslmode") not in {"require", "verify-ca", "verify-full"}:
        raise TransferError("TLS is required: set sslmode=require or stronger.")
    if set(url.query) - {"sslmode"}:
        raise TransferError("Unexpected connection options; use only sslmode in the URL.")
    return sa.create_engine(url.set(drivername="postgresql+psycopg2"), pool_pre_ping=True,
                            connect_args={"connect_timeout": 15}, hide_parameters=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "check", "rehearse", "apply", "verify"])
    parser.add_argument("--source", type=Path, required=True, help="SQLite file; apply/verify must use a frozen backup")
    parser.add_argument("--backup-dir", type=Path, default=ROOT / "backups")
    parser.add_argument("--target-env", type=Path, default=ROOT / ".env.supabase-target")
    parser.add_argument("--confirm-project", help="Exact project ref; no project is selected implicitly")
    parser.add_argument("--confirm-writes-stopped", action="store_true", help="API writers stopped before preparing this snapshot")
    args = parser.parse_args()
    source = None
    target = None
    try:
        if args.action == "prepare":
            path = snapshot(args.source, args.backup_dir)
            source = sqlite_engine(path)
            with source.connect() as connection:
                validate_schema(connection, source=True)
                report = {"source": str(path), "tables": inventory(connection), "auth_passwords_included": False}
            path.with_suffix(".manifest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps({"snapshot": str(path), "tables": len(report["tables"]), "rows": sum(t["rows"] for t in report["tables"].values())}))
            return
        if not args.confirm_project:
            raise TransferError("Pass --confirm-project with the explicitly authorized destination.")
        if args.action == "apply" and not args.confirm_writes_stopped:
            raise TransferError("Stop API writers, create a fresh snapshot, then confirm with --confirm-writes-stopped.")
        source = sqlite_engine(args.source)
        target = destination(args.target_env, args.confirm_project)
        if args.action in {"rehearse", "apply"}:
            report = transfer(source, target, commit=args.action == "apply")
            if args.action == "rehearse":
                with target.connect() as connection:
                    assert_empty_destination(connection)
            print(json.dumps({"status": "copied_and_verified" if args.action == "apply" else "rehearsed_and_rolled_back", "tables": len(report), "rows": sum(t["rows"] for t in report.values()), "auth_migrated": False, "applications_switched": False}))
        else:
            with source.connect() as src, target.connect() as dest:
                if args.action == "verify":
                    verify(src, dest)
                else:
                    validate_schema(src, source=True)
                    assert_empty_destination(dest)
            print(json.dumps({"status": args.action + "_ok", "writes": False}))
    except TransferError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception as error:
        # Driver exception strings can contain passwords, SQL values and PII.
        print(f"Transfer stopped ({type(error).__name__}). No automatic retry. Inspect connection/schema safely before continuing.", file=sys.stderr)
        return 1
    finally:
        if source is not None:
            source.dispose()
        if target is not None:
            target.dispose()


if __name__ == "__main__":
    sys.exit(main())
