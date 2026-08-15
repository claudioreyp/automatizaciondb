import asyncio
from types import SimpleNamespace

import httpx
from fastapi import HTTPException

import app.api as api_module
from app.database import SessionLocal
from app.models import (
    Branch,
    Business,
    CashRegister,
    DiningArea,
    IntegrationCredential,
    ModuleEntitlement,
    Membership,
)


ADMIN_HEADERS = {
    "X-Dev-Auth": "test-token",
    "X-Dev-Role": "superadmin",
    "X-Dev-User": "admin-onboarding",
}


def onboarding_payload(slug: str = "pizza-house") -> dict:
    return {
        "business": {
            "name": "Pizza House",
            "slug": slug,
            "plan": "pro",
            "modules": [
                "pos",
                "tables",
                "kds",
                "inventory",
                "cash",
                "delivery",
                "reservations",
                "whatsapp",
            ],
        },
        "branch": {
            "name": "Sucursal principal",
            "slug": "principal",
            "address": "Av. Principal 123",
            "phone": "+51 999 111 222",
        },
        "owner_name": "Ana Paredes",
        "owner_email": "owner@pizzahouse.pe",
        "owner_password": "PizzaHouse2026!",
        "credential_name": "Agente n8n Pizza House",
    }


def test_onboarding_creates_complete_restaurant_and_one_time_n8n_package(client, monkeypatch):
    async def create_owner(email, password, full_name, business_id):
        assert email == "owner@pizzahouse.pe"
        assert password == "PizzaHouse2026!"
        assert full_name == "Ana Paredes"
        assert business_id == 1
        return "supabase-owner-1"

    monkeypatch.setattr("app.api.create_supabase_owner_account", create_owner)
    response = client.post(
        "/api/v1/admin/onboarding/restaurants",
        json=onboarding_payload(),
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 201, response.text
    result = response.json()

    assert result["business"]["slug"] == "pizza-house"
    assert result["branch"]["slug"] == "principal"
    assert result["owner_access"]["email"] == "owner@pizzahouse.pe"
    assert result["owner_access"]["full_name"] == "Ana Paredes"
    assert result["owner_access"]["user_id"] == "supabase-owner-1"
    assert result["owner_access"]["status"] == "active"
    assert result["credential"]["token"].startswith("esc_live_")
    assert "inventory:write" in result["credential"]["scopes"]
    assert result["n8n"]["business_id"] == result["business"]["id"]
    assert result["n8n"]["branch_id"] == result["branch"]["id"]
    assert result["n8n"]["endpoints"]["menu"]["url"].endswith(
        "/api/v1/integrations/context/menu"
    )
    assert result["n8n"]["endpoints"]["adjust_inventory"]["method"] == "POST"
    assert "owner_password" not in result
    assert "PizzaHouse2026!" not in response.text

    token = result["credential"]["token"]
    context = client.get(
        "/api/v1/integrations/context",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert context.status_code == 200, context.text
    assert context.json()["branch"]["id"] == result["branch"]["id"]

    listed = client.get(
        "/api/v1/admin/integration-credentials",
        params={"branch_id": result["branch"]["id"]},
        headers=ADMIN_HEADERS,
    )
    assert listed.status_code == 200
    assert "token" not in listed.json()[0]
    assert "token_hash" not in listed.json()[0]

    with SessionLocal() as db:
        assert db.query(Business).count() == 1
        assert db.query(Branch).count() == 1
        assert db.query(DiningArea).count() == 1
        assert db.query(CashRegister).count() == 1
        assert db.query(Membership).count() == 1
        assert db.query(IntegrationCredential).count() == 1
        assert db.query(ModuleEntitlement).count() == 8
        credential = db.query(IntegrationCredential).one()
        assert credential.token_hash != token
        assert token not in credential.token_hash
        membership = db.query(Membership).one()
        assert membership.auth_user_id == "supabase-owner-1"
        assert membership.email == "owner@pizzahouse.pe"
        assert membership.role == "owner"
        assert membership.business_id == result["business"]["id"]
        assert membership.branch_id is None


def test_onboarding_duplicate_slug_does_not_create_partial_second_tenant(client, monkeypatch):
    created_users = []

    async def create_owner(*_args):
        created_users.append("supabase-owner-1")
        return "supabase-owner-1"

    monkeypatch.setattr("app.api.create_supabase_owner_account", create_owner)
    first = client.post(
        "/api/v1/admin/onboarding/restaurants",
        json=onboarding_payload(),
        headers=ADMIN_HEADERS,
    )
    assert first.status_code == 201, first.text

    duplicate = client.post(
        "/api/v1/admin/onboarding/restaurants",
        json=onboarding_payload(),
        headers=ADMIN_HEADERS,
    )
    assert duplicate.status_code == 409

    with SessionLocal() as db:
        assert db.query(Business).count() == 1
        assert db.query(Branch).count() == 1
        assert db.query(Membership).count() == 1
        assert db.query(IntegrationCredential).count() == 1
    assert created_users == ["supabase-owner-1"]


def test_onboarding_rolls_back_when_supabase_cannot_create_owner(client, monkeypatch):
    async def fail_owner(*_args):
        raise HTTPException(status_code=502, detail="Supabase unavailable")

    monkeypatch.setattr("app.api.create_supabase_owner_account", fail_owner)
    response = client.post(
        "/api/v1/admin/onboarding/restaurants",
        json=onboarding_payload("rollback-pizza"),
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 502

    with SessionLocal() as db:
        assert db.query(Business).count() == 0
        assert db.query(Branch).count() == 0
        assert db.query(Membership).count() == 0
        assert db.query(IntegrationCredential).count() == 0
        assert db.query(ModuleEntitlement).count() == 0


def test_onboarding_deletes_external_user_when_local_transaction_fails(client, monkeypatch):
    deleted_users = []

    async def create_owner(*_args):
        return "orphan-candidate"

    async def delete_owner(user_id):
        deleted_users.append(user_id)

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("forced local failure")

    monkeypatch.setattr(api_module, "create_supabase_owner_account", create_owner)
    monkeypatch.setattr(api_module, "delete_supabase_auth_user", delete_owner)
    monkeypatch.setattr(api_module, "audit", fail_audit)

    response = client.post(
        "/api/v1/admin/onboarding/restaurants",
        json=onboarding_payload("compensated-pizza"),
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 500
    assert deleted_users == ["orphan-candidate"]

    with SessionLocal() as db:
        assert db.query(Business).count() == 0
        assert db.query(Membership).count() == 0


def test_onboarding_rejects_weak_owner_password_before_writing(client):
    payload = onboarding_payload("weak-password")
    payload["owner_password"] = "demasiado-simple"
    response = client.post(
        "/api/v1/admin/onboarding/restaurants",
        json=payload,
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 422
    assert "demasiado-simple" not in response.text
    assert "[REDACTED]" in response.text

    with SessionLocal() as db:
        assert db.query(Business).count() == 0


def test_supabase_owner_helper_uses_admin_api_without_returning_password(monkeypatch):
    captured = {}

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, headers, json):
            captured.update({"url": url, "headers": headers, "json": json})
            return httpx.Response(
                200,
                json={"id": "created-auth-user"},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(
        api_module,
        "get_settings",
        lambda: SimpleNamespace(
            supabase_url="https://project.supabase.co",
            supabase_service_role_key="service-role-secret",
        ),
    )
    monkeypatch.setattr(api_module.httpx, "AsyncClient", FakeAsyncClient)

    user_id = asyncio.run(
        api_module.create_supabase_owner_account(
            "owner@example.com",
            "DirectAccess2026!",
            "Owner Name",
            42,
        )
    )

    assert user_id == "created-auth-user"
    assert captured["url"] == "https://project.supabase.co/auth/v1/admin/users"
    assert captured["json"]["email_confirm"] is True
    assert captured["json"]["password"] == "DirectAccess2026!"
    assert captured["json"]["app_metadata"]["business_id"] == 42
    assert captured["headers"]["Authorization"] == "Bearer service-role-secret"
