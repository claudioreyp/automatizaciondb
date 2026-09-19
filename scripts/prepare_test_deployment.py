"""Prepare private release backups and move referenced media, never print secrets.

Run from Apis. Configuration stays in backups/; this does not deploy services.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import mimetypes
from pathlib import Path
import shutil
import secrets

from dotenv import dotenv_values, set_key
import httpx
import sqlalchemy as sa

from scripts.transfer_sqlite_to_postgres import (
    ROOT, Base, canonical, fingerprint, read_rows, validate_schema,
)

PROJECT = "vgxbymduddknaxczudxj"
API_ORIGIN = "https://api.escalarai.tech"
POS_ORIGIN = "https://pos.escalarai.tech"
ADMIN_ORIGIN = "https://admin.escalarai.tech"


def local_env():
    values = {**dotenv_values(ROOT / ".env"), **dotenv_values(ROOT / ".env.local")}
    if values.get("SUPABASE_URL") != f"https://{PROJECT}.supabase.co":
        raise RuntimeError("Unexpected Auth project")
    url = sa.engine.make_url(values["DATABASE_URL"])
    if PROJECT not in (url.username or "") or not (url.username or "").startswith("escalar_pos_api."):
        raise RuntimeError("Expected the restored project's restricted runtime role")
    return values


def encode_env(values):
    return "".join(f"{key}={json.dumps(str(value), ensure_ascii=True)}\n" for key, value in values.items() if value)


def prepare():
    values = local_env()
    directory = ROOT / "backups" / ("deployment-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    directory.mkdir()
    for repo in (ROOT, ROOT.parent / "Admins", ROOT.parent / "CLIENTES"):
        for name in (".env", ".env.local", ".env.supabase-target"):
            source = repo / name
            if source.is_file():
                shutil.copy2(source, directory / (repo.name + name))
    shutil.copy2(ROOT.parent / "AGENTS.md", directory / "AGENTS.md")
    shutil.make_archive(str(directory / "uploads"), "zip", ROOT / "uploads")
    engine = sa.create_engine(values["DATABASE_URL"], hide_parameters=True)
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as db, db.begin():
        db.exec_driver_sql("SET TRANSACTION READ ONLY")
        validate_schema(db)
        tables = {}
        for table in Base.metadata.sorted_tables:
            rows = read_rows(db, table)
            tables[table.name] = {
                "columns": list(table.columns.keys()), "rows": len(rows), "sha256": fingerprint(rows, table),
                "values": [[canonical(row[c.name]) for c in table.c] for row in rows],
            }
        backup = {"alembic": db.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one(), "tables": tables}
        (directory / "database.json").write_text(json.dumps(backup, ensure_ascii=True, indent=2), encoding="utf-8")
    engine.dispose()
    if not values.get("DEVICE_AUTH_SECRET"):
        if tables["paired_devices"]["rows"]:
            raise RuntimeError("Existing paired devices require their original secret")
        values["DEVICE_AUTH_SECRET"] = secrets.token_urlsafe(48)
        set_key(ROOT / ".env.local", "DEVICE_AUTH_SECRET", values["DEVICE_AUTH_SECRET"])
    allowed = (
        "DATABASE_URL", "SUPABASE_URL", "SUPABASE_JWKS_URL", "SUPABASE_SERVICE_ROLE_KEY",
        "AUTH_ADMIN_SECRET", "DEVICE_AUTH_SECRET", "INTEGRATION_SERVICE_TOKEN",
        "GOOGLE_MAPS_API_KEY", "OPENAI_API_KEY", "PAYMENT_VISION_MODEL",
    )
    runtime = {key: values[key] for key in allowed if values.get(key)}
    for key in ("QZ_TRAY_CERTIFICATE", "QZ_TRAY_PRIVATE_KEY"):
        runtime[key] = values.get(key) or Path(values[key + "_FILE"]).read_text(encoding="utf-8-sig")
    for required in ("DATABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "AUTH_ADMIN_SECRET", "DEVICE_AUTH_SECRET"):
        if not runtime.get(required):
            raise RuntimeError("Missing required private runtime configuration")
    runtime.update({
        "ENVIRONMENT": "production", "AUTO_CREATE_SCHEMA": "false", "DEV_AUTH_TOKEN": "",
        "LEGACY_PUBLIC_READS_ENABLED": "false", "PUBLIC_API_BASE_URL": API_ORIGIN + "/api/v1",
        "POS_PUBLIC_BASE_URL": POS_ORIGIN, "INVITE_REDIRECT_URL": POS_ORIGIN + "/invitacion",
        "CORS_ORIGINS": ",".join((POS_ORIGIN, ADMIN_ORIGIN)), "PYTHON_VERSION": "3.12.4",
    })
    (directory / "render.env").write_text(encode_env(runtime), encoding="utf-8")
    for name in ("Admins", "CLIENTES"):
        front = dotenv_values(ROOT.parent / name / ".env.local")
        frontend = {key: front[key] for key in (
            "VITE_SUPABASE_URL", "VITE_SUPABASE_ANON_KEY", "VITE_SUPABASE_PUBLISHABLE_KEY", "VITE_GOOGLE_MAPS_BROWSER_KEY",
        ) if front.get(key)}
        frontend.update({"VITE_API_BASE_URL": API_ORIGIN + "/api/v1", "VITE_CLIENT_POS_URL": POS_ORIGIN})
        (directory / (name + ".env.deploy")).write_text(encode_env(frontend), encoding="utf-8")
    print(json.dumps({"backup": str(directory), "tables": len(tables), "rows": sum(t["rows"] for t in tables.values()),
                      "runtime_keys": sorted(runtime), "secret_values_printed": False}))


def rewrite(value, replace):
    if isinstance(value, str):
        return replace(value)
    if isinstance(value, list):
        return [rewrite(child, replace) for child in value]
    if isinstance(value, dict):
        return {key: rewrite(child, replace) for key, child in value.items()}
    return value


def local_path(value):
    if not (value.startswith(("uploads/", "uploads\\")) or Path(value).is_absolute()):
        return None
    path = Path(value)
    path = (path if path.is_absolute() else ROOT / path).resolve()
    if (ROOT / "uploads").resolve() not in path.parents or not path.is_file():
        raise RuntimeError("Referenced local media is missing or outside uploads")
    return path


def media(apply):
    values = local_env()
    engine = sa.create_engine(values["DATABASE_URL"], hide_parameters=True)
    references = {}
    updates = []

    def locate(value):
        path = local_path(value)
        if path is not None:
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            key = f"recovered/{digest}{path.suffix.lower()}"
            references[value] = (path, key, digest)
            return "supabase://impulsa-private/" + key
        return value

    with engine.connect() as db:
        for table in Base.metadata.sorted_tables:
            columns = [c for c in table.c if isinstance(c.type, (sa.String, sa.JSON))]
            for row in db.execute(sa.select(table)).mappings():
                changed = {}
                for col in columns:
                    before = row[col.name]
                    after = rewrite(before, locate)
                    if before != after:
                        changed[col.name] = (before, after)
                if changed:
                    updates.append((table, {pk.name: row[pk.name] for pk in table.primary_key}, changed))
    if apply:
        headers = {"apikey": values["SUPABASE_SERVICE_ROLE_KEY"], "Authorization": "Bearer " + values["SUPABASE_SERVICE_ROLE_KEY"]}
        with httpx.Client(base_url=values["SUPABASE_URL"], headers=headers, timeout=45) as client:
            for path, key, digest in references.values():
                endpoint = "/storage/v1/object/impulsa-private/" + key
                response = client.get(endpoint)
                if response.status_code == 400 or response.status_code == 404:
                    uploaded = client.post(endpoint, content=path.read_bytes(), headers={
                        "Content-Type": mimetypes.guess_type(path.name)[0] or "application/octet-stream", "x-upsert": "false",
                    })
                    if uploaded.status_code not in (200, 201, 409):
                        raise RuntimeError("Storage upload was not confirmed; references unchanged")
                    response = client.get(endpoint)
                if response.status_code != 200 or hashlib.sha256(response.content).hexdigest() != digest:
                    raise RuntimeError("Storage hash verification failed; references unchanged")
        with engine.begin() as db:
            for table, key, changed in updates:
                match = sa.and_(*(table.c[k] == v for k, v in key.items()))
                current = db.execute(sa.select(table).where(match).with_for_update()).mappings().one()
                if any(current[name] != before for name, (before, _) in changed.items()):
                    raise RuntimeError("Media changed concurrently; all reference changes rolled back")
                db.execute(table.update().where(match).values(**{name: after for name, (_, after) in changed.items()}))
    engine.dispose()
    print(json.dumps({"local_references": len(references), "affected_rows": len(updates), "applied": apply,
                      "original_files_deleted": False, "uploads_verified_before_reference_changes": apply}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "media"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        prepare() if args.mode == "prepare" else media(args.apply)
    except Exception as error:
        print(json.dumps({"stopped": type(error).__name__, "reason": str(error) if isinstance(error, RuntimeError) else "Check private configuration and connectivity", "automatic_retry": False}))
        raise SystemExit(1)
