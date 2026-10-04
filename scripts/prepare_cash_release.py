"""Prepare a private read-only cash backup; never applies a migration or deploys.

Run from Apis: python -m scripts.prepare_cash_release
The snapshot preserves SQL NULL separately from JSON null and verifies its own
fingerprints before reporting success. Use a fresh backup at actual activation.
"""
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
import hashlib
import importlib.util
from io import StringIO
import json
import shutil
from unittest.mock import patch
from zoneinfo import ZoneInfo

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa

from scripts.prepare_test_deployment import local_env
from scripts.transfer_sqlite_to_postgres import ROOT, canonical, read_rows

BEFORE = "20261002_0025"
AFTER = "20261004_0026"


def encoded_rows(db, table):
    rows = [[canonical(row[column.name]) for column in table.c] for row in read_rows(db, table)]
    rows.sort(key=lambda row: json.dumps(row, sort_keys=True, ensure_ascii=True))
    return rows


def digest(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def yesterday_review(db, tables, business_id=2, branch_id=2):
    """Only ledger amounts/identities are reported; customer/secret data stays private."""
    lima = ZoneInfo("America/Lima")
    day = datetime.now(lima).date() - timedelta(days=1)
    start = datetime.combine(day, time.min, lima).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    movement, session = tables["cash_movements"], tables["cash_sessions"]
    payment, audit = tables["payments"], tables["audit_events"]
    records = list(db.execute(sa.select(movement).join(session, session.c.id == movement.c.cash_session_id).where(
        session.c.business_id == business_id, session.c.branch_id == branch_id,
        movement.c.created_at >= start, movement.c.created_at < end,
        movement.c.movement_type.in_(["withdrawal", "expense"]),
    )).mappings())
    result = []
    for row in records:
        current = db.execute(sa.select(session).where(session.c.id == row["cash_session_id"])).mappings().one()
        movements = list(db.execute(sa.select(movement).where(movement.c.cash_session_id == current["id"])).mappings())
        cash_collected = db.scalar(sa.select(sa.func.coalesce(sa.func.sum(payment.c.amount), 0)).where(
            payment.c.cash_session_id == current["id"], payment.c.business_id == business_id,
            payment.c.status == "confirmed", payment.c.method == "cash"))
        recomputed = Decimal(str(current["opening_amount"] or 0)) + Decimal(str(cash_collected))
        for item in movements:
            if item["movement_type"] == "refund" and item["payment_method"] not in (None, "cash"):
                continue
            recomputed += Decimal(str(item["amount"])) * (1 if item["movement_type"] == "income" else -1)
        events = list(db.execute(sa.select(audit.c.id, audit.c.action, audit.c.entity_type, audit.c.entity_id).where(
            audit.c.business_id == business_id,
            sa.or_(sa.and_(audit.c.entity_type == "cash_movement", audit.c.entity_id == str(row["id"])),
                   sa.and_(audit.c.entity_type == "cash_session", audit.c.entity_id == str(current["id"]))))).mappings())
        stored = Decimal(str(current["expected_amount"] or 0)) if current["status"] == "closed" else None
        result.append({"movement_id": row["id"], "type": row["movement_type"],
            "amount": str(row["amount"]), "signed_amount": str(-Decimal(str(row["amount"]))),
            "created_at": row["created_at"].astimezone(lima).isoformat(),
            "register_id": current["register_id"], "session_id": current["id"], "status": current["status"],
            "opening": str(current["opening_amount"]), "cash_collected": str(cash_collected),
            "recomputed_cash_expected": str(recomputed), "stored_cash_expected": str(stored) if stored is not None else None,
            "stored_matches_ledger": stored == recomputed if stored is not None else None,
            "audit_events": [dict(event) for event in events]})
    return {"day_lima": day.isoformat(), "business_id": business_id, "branch_id": branch_id,
        "withdrawals": result, "historical_records_changed": False}


def main():
    values = local_env()
    directory = ROOT / "backups" / ("cash-release-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    directory.mkdir()
    engine = sa.create_engine(values["DATABASE_URL"], hide_parameters=True)
    try:
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as db, db.begin():
            db.exec_driver_sql("SET TRANSACTION READ ONLY")
            version = db.scalar(sa.text("SELECT version_num FROM public.alembic_version"))
            if version != BEFORE:
                raise RuntimeError("Unexpected schema version; review before preparing this release")
            inspector = sa.inspect(db)
            reflected, snapshots = {}, {}
            for name in inspector.get_table_names(schema="public"):
                table = sa.Table(name, sa.MetaData(), schema="public", autoload_with=db, resolve_fks=False)
                reflected[name] = table
                rows = encoded_rows(db, table)
                snapshots[name] = {"columns": list(table.c.keys()), "values": rows, "sha256": digest(rows)}
            backup_path = directory / "database.json"
            backup_path.write_text(json.dumps({"version": version, "tables": snapshots}, indent=2), encoding="utf-8")
            saved = json.loads(backup_path.read_text(encoding="utf-8"))
            if any(digest(item["values"]) != item["sha256"] for item in saved["tables"].values()):
                raise RuntimeError("Backup verification failed")
            (directory / "withdrawal-review.json").write_text(json.dumps(yesterday_review(db, reflected), indent=2), encoding="utf-8")
            spec = importlib.util.spec_from_file_location("cash_migration", ROOT / "migrations/versions/20261004_0026_cash_order_refunds.py")
            migration = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(migration)
            output = StringIO()
            context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output})
            with patch.object(migration.sa, "inspect", return_value=inspector), Operations.context(context):
                migration.upgrade()
            sql = f"""-- Prepared only. Execute inside an explicitly authorized transaction.
SET LOCAL search_path TO public;
SET LOCAL lock_timeout = '10s';
DO $$ BEGIN IF (SELECT version_num FROM public.alembic_version) <> '{BEFORE}' THEN
RAISE EXCEPTION 'Unexpected POS schema version'; END IF; END $$;
""" + output.getvalue() + f"\nUPDATE public.alembic_version SET version_num = '{AFTER}' WHERE version_num = '{BEFORE}';\n"
            (directory / "migration.sql").write_text(sql, encoding="utf-8")
        for repo in (ROOT, ROOT.parent / "CLIENTES"):
            for name in (".env", ".env.local", ".env.supabase-target"):
                source = repo / name
                if source.is_file():
                    shutil.copy2(source, directory / (repo.name + name))
        shutil.copy2(ROOT.parent / "AGENTS.md", directory / "AGENTS.md")
        if (ROOT / "uploads").is_dir():
            shutil.make_archive(str(directory / "uploads"), "zip", ROOT / "uploads")
        print(json.dumps({"backup": str(directory), "tables": len(snapshots),
            "rows": sum(len(item["values"]) for item in snapshots.values()),
            "fingerprints_verified": True, "database_changed": False,
            "migration_applied": False, "published": False}))
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Avoid leaking connection parameters or row contents in console/logs.
        print(json.dumps({"stopped": type(error).__name__, "database_changed": False, "automatic_retry": False}))
        raise SystemExit(1)
