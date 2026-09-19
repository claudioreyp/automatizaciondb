"""Opt-in HTTPS/CORS/WebSocket/PIN test restricted to a deployment-test tenant."""
import argparse
import asyncio
import json
import secrets
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx
import websockets


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    state = json.loads(args.receipt.read_text(encoding="utf-8"))
    tenant = state["tenants"][0]
    assert args.apply and state["complete"] and tenant["slug"].startswith("prueba-despliegue-")
    assert not state.get("transport_started"), "Do not replay a live write; inspect the receipt"
    state["transport_started"] = True

    def save():
        args.receipt.write_text(json.dumps(state, indent=2), encoding="utf-8")

    save()
    base = "https://api.escalarai.tech/api/v1"
    origin = "https://pos.escalarai.tech"
    client = httpx.Client(timeout=75)
    owner = {"Authorization": "Bearer " + tenant["access_token"],
        "X-Business-Id": str(tenant["business_id"]), "X-Branch-Id": str(tenant["branch_id"])}

    def check(name, condition):
        assert condition, name
        print("PASS " + name, flush=True)
        state["checks"].append(name)
        save()

    def call(method, path, headers=None, expected=200, **kwargs):
        result = client.request(method, base + path,
            headers={**(headers if headers is not None else owner), "Idempotency-Key": str(uuid4())}, **kwargs)
        check(f"{method} {path} HTTP {expected}", result.status_code == expected)
        return result

    for site in (origin, "https://admin.escalarai.tech"):
        response = call("OPTIONS", "/context", {"Origin": site,
            "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization,x-branch-id,x-business-id"})
        check("exact CORS " + site, response.headers.get("access-control-allow-origin") == site
            and response.headers.get("access-control-allow-credentials") == "true")
    call("OPTIONS", "/context", {"Origin": "https://untrusted.example", "Access-Control-Request-Method": "GET"}, expected=400)
    call("GET", "/context", {"X-Dev-Auth": "not-a-production-credential"}, expected=401)

    async def socket_check():
        url = base.replace("https:", "wss:") + f"/ws/branches/{tenant['branch_id']}?access_token=" + quote(tenant["access_token"])
        async with websockets.connect(url, origin=origin, open_timeout=30) as socket:
            message = json.loads(await asyncio.wait_for(socket.recv(), 30))
            check("direct authenticated WSS connected", message["event"] == "connected"
                and message["payload"]["branch_id"] == tenant["branch_id"])
    asyncio.run(socket_check())

    pin = f"{secrets.randbelow(10000):04d}"
    member = call("POST", "/settings/members", expected=201, json={
        "first_name": "PRUEBA PIN DESPLIEGUE", "roles": ["cashier"],
        "branch_ids": [tenant["branch_id"]], "pin": pin, "email_access": False}).json()
    state["transport_member_id"] = member["id"]
    save()
    pairing = call("POST", "/settings/devices/pairing-links", json={"branch_id": tenant["branch_id"]}).json()
    state["transport_device_id"] = pairing["device"]["id"]
    save()
    check("pairing uses public HTTPS POS", pairing["url"].startswith(origin + "/activar-dispositivo#token="))
    public_headers = {"Origin": origin}
    response = call("POST", "/auth/devices/activate", public_headers, json={
        "token": urlsplit(pairing["url"]).fragment.removeprefix("token="), "name": "PRUEBA HTTPS PIN"})
    cookie = response.headers.get("set-cookie", "").lower()
    check("device cookie Secure HttpOnly SameSite=Lax", all(x in cookie for x in ("secure", "httponly", "samesite=lax")))
    session = call("GET", "/auth/devices/session", public_headers).json()
    check("paired session survives next request", session["linked"])
    response = call("POST", "/auth/devices/login", {**public_headers, "X-CSRF-Token": session["csrf_token"]},
        json={"member_id": member["id"], "pin": pin})
    cookie = response.headers.get("set-cookie", "").lower()
    check("staff cookie Secure HttpOnly SameSite=Lax", all(x in cookie for x in ("secure", "httponly", "samesite=lax")))
    staff = call("GET", "/context", public_headers).json()
    check("PIN role remains cashier", staff["role"] == "cashier")
    call("GET", "/admin/businesses", public_headers, expected=403)
    call("POST", "/auth/devices/logout", {**public_headers, "X-CSRF-Token": session["csrf_token"]})
    call("GET", "/context", public_headers, expected=401)
    device = call("GET", f"/settings/devices/{state['transport_device_id']}").json()
    call("DELETE", f"/settings/devices/{device['id']}", json={"expected_version": device["version"]})
    call("DELETE", f"/settings/members/{member['id']}", json={"expected_version": member["version"]})
    state["transport_complete"] = True
    save()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"STOP {type(error).__name__}; inspect private receipt without replaying writes")
        raise SystemExit(1) from None
