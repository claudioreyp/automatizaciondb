"""Recreate identities in the explicitly authorized, new project; no POS writes."""
import argparse
import getpass
import json
import secrets
from uuid import UUID

from dotenv import dotenv_values
import httpx
import sqlalchemy as sa

from app.models import Membership, AuthSecurityState
from scripts.transfer_sqlite_to_postgres import ROOT, sqlite_engine, validate_schema

PROJECT = "vgxbymduddknaxczudxj"
MARKER = "escalar_recovered_identity"


def recover(source, confirmed_project):
    if confirmed_project != PROJECT:
        raise RuntimeError("Unexpected destination")
    env = dotenv_values(ROOT / ".env.supabase-target")
    if env.get("SUPABASE_URL") != f"https://{PROJECT}.supabase.co":
        raise RuntimeError("Target configuration mismatch")
    engine = sqlite_engine(source)
    with engine.connect() as db:
        validate_schema(db, source=True)
        members = list(db.execute(sa.select(Membership.__table__)).mappings())
        pending = set(db.scalars(sa.select(AuthSecurityState.auth_user_id).where(
            AuthSecurityState.requires_password_reset.is_(True))))
    engine.dispose()
    identities = {}
    for member in members:
        try:
            UUID(member["auth_user_id"])
        except ValueError:
            continue
        if not member["email"]:
            raise RuntimeError("Identity without email requires review")
        existing = identities.get(member["auth_user_id"])
        if existing and existing["email"] != member["email"]:
            raise RuntimeError("Inconsistent identity mapping")
        if not existing or member["role"] == "superadmin":
            identities[member["auth_user_id"]] = member
    if sum(item["role"] == "superadmin" for item in identities.values()) != 1:
        raise RuntimeError("Expected exactly one superadministrator")
    if any(item["role"] != "superadmin" and subject not in pending for subject, item in identities.items()):
        raise RuntimeError("Recovered owners must require a password renewal")
    headers = {"apikey": env["SUPABASE_SERVICE_ROLE_KEY"], "Authorization": "Bearer " + env["SUPABASE_SERVICE_ROLE_KEY"]}
    created = 0
    with httpx.Client(base_url=env["SUPABASE_URL"] + "/auth/v1", headers=headers, timeout=25) as client:
        listing = client.get("/admin/users", params={"page": 1, "per_page": 1000})
        listing.raise_for_status()
        users = listing.json()["users"]
        if len(users) >= 1000 or any(user["id"] not in identities for user in users):
            raise RuntimeError("Destination has unrelated identities; stop")
        for subject, member in identities.items():
            found = next((user for user in users if user["id"] == subject), None)
            if found:
                if found.get("email") != member["email"] or found.get("app_metadata", {}).get(MARKER) != subject:
                    raise RuntimeError("Existing identity needs manual reconciliation")
                continue
            password = getpass.getpass("Superadmin password (hidden): ") if member["role"] == "superadmin" else secrets.token_urlsafe(40) + "Aa1!"
            if len(password) < 12:
                raise RuntimeError("Password too short")
            response = client.post("/admin/users", json={"id": subject, "email": member["email"],
                "password": password, "email_confirm": True,
                "app_metadata": {MARKER: subject}, "user_metadata": {"full_name": member["full_name"]}})
            password = ""
            response.raise_for_status()
            if response.json().get("id") != subject:
                raise RuntimeError("Restored UUID does not match")
            created += 1
    print(json.dumps({"identities_created": created, "identities_verified": len(identities),
                      "owner_passwords_distributed": False, "pos_data_modified": False}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--confirm-project", required=True)
    args = parser.parse_args()
    try:
        recover(args.source, args.confirm_project)
    except Exception as error:
        print(json.dumps({"stopped": type(error).__name__, "status": getattr(getattr(error, "response", None), "status_code", None),
                          "automatic_retry": False}))
        raise SystemExit(1)
