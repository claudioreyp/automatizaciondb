"""Prepare a verified private backup and guarded SQL; never mutate the database.

The source connection is read-only for the whole snapshot. Configuration, row
values and local media remain in the ignored backups directory. The generated
SQL must be reviewed and applied separately using an authorized DDL connection.
No runtime credentials are changed and no migration is applied by this script.
"""
import argparse
from datetime import date, datetime, time, timezone
import hashlib
import json
from pathlib import Path
import shutil
from uuid import UUID

from dotenv import dotenv_values
import sqlalchemy as sa

from scripts.transfer_sqlite_to_postgres import canonical, read_rows


BEFORE = "20261020_0029"
AFTER = "20261119_0030"
DEFAULT_SOURCE_ROOT = Path(r"C:/codex/apps/Impulsa/APP POS/Apis")


def json_text(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(",", ":"))


def digest(value):
    return hashlib.sha256(json_text(value).encode("utf-8")).hexdigest()


def file_digest(path):
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def encoded_value(value):
    # Keep SQL NULL distinct from a JSON null, including reflected JSON columns.
    if isinstance(value, UUID):
        return ["uuid", str(value)]
    if isinstance(value, (date, time)) and not isinstance(value, datetime):
        return [type(value).__name__, value.isoformat()]
    if isinstance(value, (bytes, memoryview)):
        return ["bytes_hex", bytes(value).hex()]
    return canonical(value)


def encoded_rows(connection, table):
    rows = [[encoded_value(row[column.name]) for column in table.c]
            for row in read_rows(connection, table)]
    rows.sort(key=json_text)
    return rows


def branch_proof_query(json_columns, *, after=False):
    # All names come from reflected metadata, never from request input.
    quote = lambda name: '"' + name.replace('"', '""') + '"'
    flags = ",".join("t." + quote(name) + " IS NULL" for name in json_columns)
    row = "to_jsonb(t)" + (" - 'agent_menu_pdf'" if after else "")
    return ("SELECT count(*),md5(coalesce(string_agg(data::text,E'\\n' ORDER BY data::text),'')) "
            "FROM (SELECT jsonb_build_object('row'," + row + ", 'json_sql_nulls',"
            "jsonb_build_array(" + flags + ")) AS data FROM public.branches t) rows")


def migration_sql(branch_count, branch_md5, json_columns):
    before = branch_proof_query(json_columns)
    after = branch_proof_query(json_columns, after=True)
    return f"""-- Prepared only. Review and apply separately using the authorized DDL role.
-- Only the nullable PDF metadata column and Alembic revision are changed.
BEGIN;
SET LOCAL search_path TO public;
SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '30s';
LOCK TABLE public.branches, public.alembic_version IN ACCESS EXCLUSIVE MODE;
DO $menu_pdf$
DECLARE amount bigint; checksum text; affected bigint;
BEGIN
  IF (SELECT array_agg(version_num ORDER BY version_num) FROM public.alembic_version)
      IS DISTINCT FROM ARRAY['{BEFORE}']::varchar[] THEN
    RAISE EXCEPTION 'Unexpected POS schema revision; no PDF migration applied';
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.columns
      WHERE table_schema='public' AND table_name='branches' AND column_name='agent_menu_pdf') THEN
    RAISE EXCEPTION 'PDF column already exists; inspect before retrying';
  END IF;
  SELECT proof.* INTO amount,checksum FROM ({before}) proof;
  IF amount IS DISTINCT FROM {int(branch_count)} OR checksum IS DISTINCT FROM '{branch_md5}' THEN
    RAISE EXCEPTION 'Branch configuration changed after backup; prepare a fresh backup';
  END IF;
  ALTER TABLE public.branches ADD COLUMN agent_menu_pdf JSON NULL;
  SELECT proof.* INTO amount,checksum FROM ({after}) proof;
  IF amount IS DISTINCT FROM {int(branch_count)} OR checksum IS DISTINCT FROM '{branch_md5}' THEN
    RAISE EXCEPTION 'Existing branch values changed; rolling back PDF migration';
  END IF;
  IF EXISTS (SELECT 1 FROM public.branches WHERE agent_menu_pdf IS NOT NULL) THEN
    RAISE EXCEPTION 'Unexpected PDF metadata after additive migration';
  END IF;
  UPDATE public.alembic_version SET version_num='{AFTER}' WHERE version_num='{BEFORE}';
  GET DIAGNOSTICS affected = ROW_COUNT;
  IF affected <> 1 THEN
    RAISE EXCEPTION 'Unexpected Alembic revision row count';
  END IF;
END $menu_pdf$;
COMMIT;
"""


def snapshot_schema(inspector, name, columns, dialect):
    return {
        "columns": [{"name": column["name"],
                     "type": str(column["type"].compile(dialect=dialect)),
                     "nullable": column["nullable"], "default": column.get("default")}
                    for column in columns],
        "primary_key": inspector.get_pk_constraint(name, schema="public"),
        "foreign_keys": inspector.get_foreign_keys(name, schema="public"),
        "unique_constraints": inspector.get_unique_constraints(name, schema="public"),
        "check_constraints": inspector.get_check_constraints(name, schema="public"),
        "indexes": inspector.get_indexes(name, schema="public"),
    }


def prepare(source_root=DEFAULT_SOURCE_ROOT, env_file=None, backup_root=None):
    source_root = Path(source_root).resolve(strict=True)
    env_file = Path(env_file or source_root / ".env.local").resolve(strict=True)
    backup_root = Path(backup_root or source_root / "backups").resolve()
    if not backup_root.is_relative_to(source_root / "backups"):
        raise RuntimeError("Backups must remain inside the source repository's private backups directory")
    values = dotenv_values(env_file)
    url = sa.engine.make_url(values["DATABASE_URL"])
    if url.get_backend_name() != "postgresql":
        raise RuntimeError("A PostgreSQL source is required")
    engine = sa.create_engine(url, hide_parameters=True,
                              connect_args={"connect_timeout": 20})
    directory = backup_root / ("menu-pdf-release-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    directory.mkdir(parents=True, exist_ok=False)
    try:
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as db, db.begin():
            db.exec_driver_sql("SET TRANSACTION READ ONLY")
            db.exec_driver_sql("SET LOCAL statement_timeout = '30s'")
            if db.exec_driver_sql("SHOW transaction_read_only").scalar_one() != "on":
                raise RuntimeError("Snapshot connection must be read-only")
            versions = db.exec_driver_sql("SELECT version_num FROM public.alembic_version").scalars().all()
            if versions != [BEFORE]:
                raise RuntimeError("Unexpected schema revision; review before preparing the release")
            inspector = sa.inspect(db)
            names = sorted(inspector.get_table_names(schema="public"))
            if not {"branches", "alembic_version"}.issubset(names):
                raise RuntimeError("Expected POS tables are missing")
            branch_columns = inspector.get_columns("branches", schema="public")
            if any(column["name"] == "agent_menu_pdf" for column in branch_columns):
                raise RuntimeError("PDF column is already present; inspect before retrying")
            tables = {}
            for name in names:
                table = sa.Table(name, sa.MetaData(), schema="public", autoload_with=db, resolve_fks=False)
                rows = encoded_rows(db, table)
                columns = inspector.get_columns(name, schema="public")
                tables[name] = {
                    "columns": list(table.c.keys()), "rows": len(rows),
                    "values": rows, "sha256": digest(rows),
                    "schema": snapshot_schema(inspector, name, columns, db.dialect),
                }
            json_columns = [column["name"] for column in branch_columns
                            if isinstance(column["type"], sa.JSON)]
            branch_count, branch_md5 = db.exec_driver_sql(branch_proof_query(json_columns)).one()
            backup = {"version": BEFORE, "schema": "public", "tables": tables}
            backup_path = directory / "database.json"
            backup_path.write_text(json_text(backup) + "\n", encoding="utf-8")
            saved = json.loads(backup_path.read_text(encoding="utf-8"))
            if saved["version"] != BEFORE or any(
                digest(item["values"]) != item["sha256"] or len(item["values"]) != item["rows"]
                for item in saved["tables"].values()
            ):
                raise RuntimeError("Private backup verification failed")
            (directory / "migration.sql").write_text(
                migration_sql(branch_count, branch_md5, json_columns), encoding="utf-8")
        config = directory / "config"
        config.mkdir()
        copied = set()
        for source in [env_file, *(source_root / name for name in (
                ".env", ".env.local", ".env.supabase-target", ".env.production"))]:
            if source.is_file() and source.resolve() not in copied:
                destination = config / source.name
                if destination.exists():
                    destination = config / ("selected-" + source.name)
                shutil.copy2(source, destination)
                copied.add(source.resolve())
        if (source_root / "uploads").is_dir():
            shutil.make_archive(str(directory / "uploads"), "zip", source_root / "uploads")
        files = {path.relative_to(directory).as_posix(): {
                    "size_bytes": path.stat().st_size, "sha256": file_digest(path)}
                 for path in directory.rglob("*") if path.is_file()}
        manifest = {
            "prepared_at": datetime.now(timezone.utc).isoformat(),
            "before": BEFORE, "after": AFTER,
            "tables": len(tables), "rows": sum(table["rows"] for table in tables.values()),
            "fingerprints_verified": True, "database_changed": False,
            "migration_applied": False, "published": False,
            "remote_private_objects": "Not copied; the additive migration does not alter or delete existing media",
            "files": files,
        }
        manifest_path = directory / "manifest.json"
        manifest_path.write_text(json_text(manifest) + "\n", encoding="utf-8")
        # No rows, configuration values, connection parameters or customer data.
        return {"backup": str(directory), "before": BEFORE, "after": AFTER,
                "tables": manifest["tables"], "rows": manifest["rows"],
                "fingerprints_verified": True, "manifest_sha256": file_digest(manifest_path),
                "database_changed": False, "migration_applied": False, "published": False}
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--backup-root", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(prepare(args.source_root, args.env_file, args.backup_root)))
    except Exception as error:
        # Database exceptions may include credentials or rows; print only type.
        print(json.dumps({"stopped": type(error).__name__, "database_changed": False,
                          "migration_applied": False, "published": False, "automatic_retry": False}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
