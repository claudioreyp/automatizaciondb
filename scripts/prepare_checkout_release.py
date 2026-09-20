"""Read-only backup and PostgreSQL SQL generation from the tested Alembic revision."""
from datetime import datetime, timezone
import hashlib
import importlib.util
from io import StringIO
import json
from pathlib import Path
import shutil
import sys
from unittest.mock import patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa

from scripts.prepare_test_deployment import local_env
from scripts.transfer_sqlite_to_postgres import ROOT, canonical


def main():
    values = local_env()
    if len(sys.argv) == 3 and sys.argv[1] == "--verify":
        return verify(Path(sys.argv[2]), values)
    directory = ROOT / "backups" / ("checkout-release-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    directory.mkdir()
    for name in (".env", ".env.local", ".env.supabase-target"):
        source = ROOT / name
        if source.exists():
            shutil.copy2(source, directory / name)
    shutil.make_archive(str(directory / "uploads"), "zip", ROOT / "uploads")
    engine = sa.create_engine(values["DATABASE_URL"], hide_parameters=True)
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as db, db.begin():
        db.exec_driver_sql("SET TRANSACTION READ ONLY")
        version = db.scalar(sa.text("SELECT version_num FROM public.alembic_version"))
        if version != "20260919_0023":
            raise RuntimeError("Unexpected version; reconcile before preparing another release")
        inspector = sa.inspect(db)
        tables = {}
        for name in inspector.get_table_names(schema="public"):
            table = sa.Table(name, sa.MetaData(), schema="public", autoload_with=db, resolve_fks=False)
            rows = [[canonical(value) for value in row] for row in db.execute(sa.select(table))]
            rows.sort(key=lambda row: json.dumps(row, sort_keys=True))
            tables[name] = {"columns": list(table.columns.keys()), "values": rows,
                "sha256": hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()}
        (directory / "database.json").write_text(json.dumps({"version":version, "tables":tables}, indent=2), encoding="utf-8")
        spec = importlib.util.spec_from_file_location("checkout_migration", ROOT / "migrations/versions/20260920_0024_agent_checkout.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        output = StringIO()
        context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql":True,"output_buffer":output})
        with patch.object(migration.sa, "inspect", return_value=inspector), Operations.context(context):
            migration.upgrade()
        sql = """SET LOCAL lock_timeout = '10s';
DO $$ BEGIN IF (SELECT version_num FROM public.alembic_version) <> '20260919_0023' THEN
RAISE EXCEPTION 'Unexpected POS schema version'; END IF; END $$;
""" + output.getvalue() + """
GRANT SELECT, INSERT, UPDATE, DELETE ON public.order_payment_requests TO escalar_pos_api;
CREATE POLICY pos_runtime_access ON public.order_payment_requests FOR ALL TO escalar_pos_api USING (true) WITH CHECK (true);
UPDATE public.alembic_version SET version_num = '20260920_0024' WHERE version_num = '20260919_0023';
"""
        (directory / "migration.sql").write_text(sql, encoding="utf-8")
    engine.dispose()
    print(json.dumps({"backup":str(directory),"tables":len(tables),"rows":sum(len(t["values"]) for t in tables.values()),"sql_generated_from_alembic":True,"database_changed":False}))


def verify(directory, values):
    before = json.loads((directory / "database.json").read_text(encoding="utf-8"))
    engine = sa.create_engine(values["DATABASE_URL"], hide_parameters=True)
    differences = []
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as db, db.begin():
        db.exec_driver_sql("SET TRANSACTION READ ONLY")
        for name, saved in before["tables"].items():
            if name == "alembic_version":
                continue
            table = sa.Table(name, sa.MetaData(), schema="public", autoload_with=db, resolve_fks=False)
            rows = [[canonical(v) for v in row] for row in db.execute(sa.select(*(table.c[c] for c in saved["columns"])))]
            rows.sort(key=lambda row: json.dumps(row, sort_keys=True))
            if hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest() != saved["sha256"]:
                differences.append(name)
        version = db.scalar(sa.text("SELECT version_num FROM public.alembic_version"))
        requests = db.scalar(sa.text("SELECT count(*) FROM public.order_payment_requests"))
    engine.dispose()
    print(json.dumps({"version":version,"preexisting_tables_compared":len(before["tables"])-1,
        "different_tables":differences,"payment_requests":requests,"database_changed":False}))
    if differences:
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"stopped":type(error).__name__,"database_changed":False,"automatic_retry":False}))
        raise SystemExit(1)
