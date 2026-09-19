"""Switch local apps only after verified data and restored Auth identities."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import shutil

from dotenv import dotenv_values, set_key
import httpx
import sqlalchemy as sa

from scripts.transfer_sqlite_to_postgres import ROOT, sqlite_engine, verify

PROJECT = "vgxbymduddknaxczudxj"


def activate(source, project):
    if project != PROJECT:
        raise RuntimeError("Unexpected target")
    target_file = ROOT / ".env.supabase-target"
    env = dotenv_values(target_file)
    if env.get("SUPABASE_URL") != f"https://{PROJECT}.supabase.co" or "escalar_pos_api." not in env["DATABASE_URL"]:
        raise RuntimeError("Runtime configuration not verified")
    runtime = sa.create_engine(env["DATABASE_URL"], hide_parameters=True)
    original = sqlite_engine(source)
    with original.connect() as src, runtime.connect() as dst:
        verify(src, dst)
        ids = dst.execute(sa.text("SELECT auth_user_id FROM memberships WHERE role IN ('owner', 'superadmin')")).scalars().all()
    headers = {"apikey": env["SUPABASE_SERVICE_ROLE_KEY"], "Authorization": "Bearer " + env["SUPABASE_SERVICE_ROLE_KEY"]}
    with httpx.Client(base_url=env["SUPABASE_URL"], headers=headers, timeout=20) as client:
        response = client.get("/auth/v1/admin/users")
        response.raise_for_status()
        restored = {row["id"] for row in response.json()["users"]}
        from uuid import UUID
        for subject in ids:
            try:
                UUID(subject)
            except ValueError:
                continue
            if subject not in restored:
                raise RuntimeError("Auth identity not restored")
    original.dispose()
    runtime.dispose()
    backup = ROOT / "backups" / ("activation-config-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    backup.mkdir()
    for directory in (ROOT, ROOT.parent / "Admins", ROOT.parent / "CLIENTES"):
        for name in (".env", ".env.local"):
            path = directory / name
            if path.exists():
                shutil.copy2(path, backup / (directory.name + name))
    secret = env.get("AUTH_ADMIN_SECRET") or secrets.token_urlsafe(48)
    set_key(target_file, "AUTH_ADMIN_SECRET", secret)
    api_values = {
        "DATABASE_URL": env["DATABASE_URL"], "AUTO_CREATE_SCHEMA": "false",
        "SUPABASE_URL": env["SUPABASE_URL"], "SUPABASE_SERVICE_ROLE_KEY": env["SUPABASE_SERVICE_ROLE_KEY"],
        "SUPABASE_JWT_SECRET": "", "SUPABASE_JWKS_URL": env["SUPABASE_URL"] + "/auth/v1/.well-known/jwks.json",
        "AUTH_ADMIN_SECRET": secret, "DEV_AUTH_TOKEN": "",
        "POS_PUBLIC_BASE_URL": "http://127.0.0.1:5173", "PUBLIC_API_BASE_URL": "http://127.0.0.1:8000/api/v1",
        "INVITE_REDIRECT_URL": "http://127.0.0.1:5173/invitacion",
        "CORS_ORIGINS": "http://127.0.0.1:5173,http://127.0.0.1:5174,http://localhost:5173,http://localhost:5174",
    }
    for name, value in api_values.items():
        set_key(ROOT / ".env.local", name, value)
    for directory in (ROOT.parent / "Admins", ROOT.parent / "CLIENTES"):
        for name, value in {
            "VITE_SUPABASE_URL": env["SUPABASE_URL"],
            "VITE_SUPABASE_ANON_KEY": env["SUPABASE_PUBLISHABLE_KEY"],
            "VITE_SUPABASE_PUBLISHABLE_KEY": env["SUPABASE_PUBLISHABLE_KEY"],
            "VITE_API_BASE_URL": "http://127.0.0.1:8000/api/v1", "VITE_DEV_AUTH_TOKEN": "",
            "VITE_CLIENT_POS_URL": "http://127.0.0.1:5173",
        }.items():
            set_key(directory / ".env.local", name, value)
    print(json.dumps({"local_apps_configured": True, "development_auth_disabled": True,
                      "original_sqlite_untouched": True, "private_config_backup": str(backup)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--confirm-project", required=True)
    args = parser.parse_args()
    try:
        activate(args.source, args.confirm_project)
    except Exception as error:
        print(json.dumps({"stopped": type(error).__name__, "automatic_retry": False}))
        raise SystemExit(1)
