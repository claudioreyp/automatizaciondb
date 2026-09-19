"""Opt-in live smoke check. Only creates clearly labelled deployment-test tenants.

Credentials arrive through stdin; private test receipts stay under ignored backups.
No order, payment, print job or historical event is dispatched by this script.
"""
import argparse
import json
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import dotenv_values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--base", default="https://api.escalarai.tech/api/v1")
    args = parser.parse_args()
    if not args.apply or args.base not in {
        "https://api.escalarai.tech/api/v1",
        "https://escalar-ai-pos-api.onrender.com/api/v1",
    }:
        raise SystemExit("Explicit --apply and an approved test deployment are required")
    root = Path(__file__).resolve().parents[1]
    config = dotenv_values(root / ".env.supabase-target")
    assert config["SUPABASE_URL"] == "https://vgxbymduddknaxczudxj.supabase.co"
    login = json.load(sys.stdin)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    receipt = root / "backups" / f"cloud-smoke-{stamp}.json"
    state = {"base": args.base, "tenants": [], "checks": []}
    client = httpx.Client(timeout=75)

    def save():
        receipt.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def check(name, condition):
        if not condition:
            save()
            raise RuntimeError(f"Check failed: {name}; inspect private receipt, do not repeat writes blindly")
        state["checks"].append(name)
        save()
        print(f"PASS {name}", flush=True)

    def auth(email, password, expected=200):
        response = client.post(config["SUPABASE_URL"] + "/auth/v1/token?grant_type=password",
            headers={"apikey": config["SUPABASE_PUBLISHABLE_KEY"]},
            json={"email": email, "password": password})
        check(f"password login HTTP {expected}", response.status_code == expected)
        return response.json()

    admin = auth(login["email"], login["password"])["access_token"]
    login.clear()
    admin_headers = {"Authorization": f"Bearer {admin}"}

    def request(method, path, headers=None, expected=200, **kwargs):
        response = client.request(method, args.base + path, headers=headers or admin_headers, **kwargs)
        check(f"{method} {path} HTTP {expected}", response.status_code == expected)
        return response

    request("GET", "/admin/businesses")
    for suffix in ("a", "b"):
        slug = f"prueba-despliegue-{stamp.lower()}-{suffix}"
        tenant = {"slug": slug, "email": f"{slug}@example.test",
            "password": "Aa1!" + secrets.token_urlsafe(24), "status": "not_submitted"}
        state["tenants"].append(tenant)
        save()
        tenant["status"] = "submitted_result_unconfirmed"
        save()
        response = request("POST", "/admin/onboarding/restaurants", expected=201, json={
            "business": {"name": f"PRUEBA DESPLIEGUE {stamp} {suffix.upper()}", "slug": slug},
            "branch": {"name": "Sucursal de prueba", "slug": "prueba"},
            "owner_name": "Propietario de prueba", "owner_email": tenant["email"],
            "owner_password": tenant["password"], "credential_name": "Prueba de aislamiento",
        })
        data = response.json()
        tenant.update(business_id=data["business"]["id"], branch_id=data["branch"]["id"],
            credential_id=data["credential"]["id"], token=data["credential"]["token"], status="created")
        save()
        check("onboarding is no-store", response.headers.get("cache-control") == "no-store")
        check("default token cannot change inventory", "inventory:write" not in data["credential"]["scopes"])
        check("public integration base", data["integration"]["api_base_url"] == "https://api.escalarai.tech/api/v1")
        tenant["access_token"] = auth(tenant["email"], tenant["password"])["access_token"]
        headers = {"Authorization": "Bearer " + tenant["access_token"],
            "X-Business-Id": str(tenant["business_id"]), "X-Branch-Id": str(tenant["branch_id"])}
        request("GET", "/context", headers)
        request("GET", "/admin/businesses", headers, expected=403)
        token_headers = {"Authorization": "Bearer " + tenant["token"]}
        request("GET", "/integrations/context", token_headers)
        spec = client.get(args.base.removesuffix("/api/v1") + "/openapi.json").json()["paths"]
        check("copyable routes exist", all(
            endpoint["method"].lower() in spec.get(endpoint["url"].replace("https://api.escalarai.tech", ""), {})
            for endpoint in data["integration"]["endpoints"].values()))

    first, other = state["tenants"]
    scoped = {"Authorization": "Bearer " + first["access_token"],
        "X-Business-Id": str(other["business_id"]), "X-Branch-Id": str(other["branch_id"])}
    request("GET", "/context", scoped, expected=403)
    token_headers = {"Authorization": "Bearer " + first["token"]}
    request("GET", f"/integrations/context?branch_id={other['branch_id']}", token_headers, expected=403)

    business_path = f"/admin/businesses/{first['business_id']}"
    request("PATCH", business_path, json={"status": "suspended"})
    request("GET", "/integrations/context", token_headers, expected=403)
    request("PATCH", business_path, json={"status": "active"})
    request("GET", "/integrations/context", token_headers)
    members = request("GET", business_path + "/memberships").json()
    member = next(item for item in members if item["email"] == first["email"])
    reset_path = business_path + f"/memberships/{member['id']}/password-reset"
    new_password = "Bb2!" + secrets.token_urlsafe(24)
    reset_headers = {**admin_headers, "Idempotency-Key": str(uuid4())}
    first["new_password"] = new_password
    first["reset_key"] = reset_headers["Idempotency-Key"]
    save()
    reset_body = {"password": new_password, "expected_version": member["password_security_version"]}
    reset = request("POST", reset_path, reset_headers, json=reset_body).json()
    first["reset_operation"] = reset
    save()
    check("password reset succeeded", reset["status"] == "succeeded")
    replay = request("POST", reset_path, reset_headers, json=reset_body).json()
    check("reset replay is same operation", replay["operation_id"] == reset["operation_id"])
    old_headers = {"Authorization": "Bearer " + first["access_token"],
        "X-Business-Id": str(first["business_id"]), "X-Branch-Id": str(first["branch_id"])}
    request("GET", "/context", old_headers, expected=401)
    auth(first["email"], first["password"], expected=400)
    first["password"] = new_password
    first["access_token"] = auth(first["email"], new_password)["access_token"]
    request("GET", "/context", {**old_headers, "Authorization": "Bearer " + first["access_token"]})
    request("GET", "/integrations/context", token_headers)
    state["complete"] = True
    save()
    print(f"Private receipt: {receipt.name}; test accounts remain for browser checks", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"STOP {type(error).__name__}: live check incomplete. No automatic retry.", file=sys.stderr)
        raise SystemExit(1) from None
