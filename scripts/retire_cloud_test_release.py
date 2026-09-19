"""Suspend only tenants created by verify_cloud_test_release; retain their audit."""
import argparse
import json
import sys
from pathlib import Path

import httpx
from dotenv import dotenv_values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    state = json.loads(args.receipt.read_text(encoding="utf-8"))
    assert args.apply and state.get("complete") and state.get("transport_complete")
    config = dotenv_values(Path(__file__).resolve().parents[1] / ".env.supabase-target")
    assert config["SUPABASE_URL"] == "https://vgxbymduddknaxczudxj.supabase.co"
    login = json.load(sys.stdin)
    base = "https://api.escalarai.tech/api/v1"
    with httpx.Client(timeout=75) as client:
        response = client.post(config["SUPABASE_URL"] + "/auth/v1/token?grant_type=password",
            headers={"apikey": config["SUPABASE_PUBLISHABLE_KEY"]}, json=login)
        assert response.status_code == 200
        headers = {"Authorization": "Bearer " + response.json()["access_token"]}
        businesses = client.get(base + "/admin/businesses", headers=headers).json()
        for tenant in state["tenants"]:
            business = next(item for item in businesses if item["id"] == tenant["business_id"])
            assert business["slug"] == tenant["slug"] and tenant["slug"].startswith("prueba-despliegue-")
            assert business["name"].startswith("PRUEBA DESPLIEGUE ")
            response = client.post(base + f"/admin/integration-credentials/{tenant['credential_id']}/revoke", headers=headers)
            assert response.status_code == 200
            response = client.patch(base + f"/admin/businesses/{tenant['business_id']}", headers=headers, json={"status": "suspended"})
            assert response.status_code == 200
            response = client.get(base + "/integrations/context", headers={"Authorization": "Bearer " + tenant["token"]})
            assert response.status_code == 401
            for field in ("password", "new_password", "access_token", "token", "reset_key"):
                tenant.pop(field, None)
            tenant["status"] = "test_suspended_credential_revoked"
            args.receipt.write_text(json.dumps(state, indent=2), encoding="utf-8")
            print(f"PASS test business {tenant['business_id']} suspended, token revoked; audit retained")
        state["retired"] = True
        args.receipt.write_text(json.dumps(state, indent=2), encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"STOP {type(error).__name__}; reconcile before retrying")
        raise SystemExit(1) from None
