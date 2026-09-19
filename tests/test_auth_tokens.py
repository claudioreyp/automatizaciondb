from datetime import datetime, timezone
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException

from app import auth
from app.database import SessionLocal
from app.models import Membership


@pytest.fixture(autouse=True)
def clear_key_client_cache():
    auth._jwks_client.cache_clear()
    yield
    auth._jwks_client.cache_clear()


@pytest.fixture(params=["HS256", "ES256"])
def signed_token(request, monkeypatch):
    algorithm = request.param
    key = "test-only-signing-secret-with-32-bytes"
    if algorithm == "ES256":
        key = ec.generate_private_key(ec.SECP256R1())
        client = SimpleNamespace(
            get_signing_key_from_jwt=lambda token: SimpleNamespace(key=key.public_key())
        )
        monkeypatch.setattr(auth.jwt, "PyJWKClient", lambda url: client)
    settings = SimpleNamespace(
        dev_auth_token=None,
        supabase_jwt_secret=key if algorithm == "HS256" else None,
        jwks_url="https://auth.example.test/jwks" if algorithm == "ES256" else None,
    )
    monkeypatch.setattr(auth, "get_settings", lambda: settings)

    def create(**overrides):
        now = int(datetime.now(timezone.utc).timestamp())
        claims = {"sub": "test-user", "aud": "authenticated", "iat": now, "exp": now + 3600}
        claims.update(overrides)
        return jwt.encode(claims, key, algorithm=algorithm)

    return create


def test_fresh_session_accepts_small_clock_difference(signed_token):
    now = int(datetime.now(timezone.utc).timestamp())
    token = signed_token(iat=now + 6, nbf=now + 6)
    assert auth.decode_access_token(token)["sub"] == "test-user"


@pytest.mark.parametrize("claim,offset", [("iat", 120), ("nbf", 120), ("exp", -120)])
def test_time_validation_still_rejects_invalid_sessions(signed_token, claim, offset):
    now = int(datetime.now(timezone.utc).timestamp())
    with pytest.raises(HTTPException) as error:
        auth.decode_access_token(signed_token(**{claim: now + offset}))
    assert error.value.status_code == 401


def test_wrong_audience_is_rejected(signed_token):
    with pytest.raises(HTTPException) as error:
        auth.decode_access_token(signed_token(aud="another-service"))
    assert error.value.status_code == 401


@pytest.mark.parametrize("issuer", [None, "https://foreign.example.test/auth/v1"])
def test_missing_or_foreign_issuer_is_rejected(signed_token, issuer):
    auth.get_settings().supabase_url = "https://auth.example.test"
    with pytest.raises(HTTPException) as error:
        auth.decode_access_token(signed_token(**({"iss": issuer} if issuer else {})))
    assert error.value.status_code == 401


def test_matching_issuer_is_accepted(signed_token):
    auth.get_settings().supabase_url = "https://auth.example.test"
    assert auth.decode_access_token(signed_token(iss="https://auth.example.test/auth/v1"))["sub"] == "test-user"


def test_reuses_key_client_but_checks_every_signature(monkeypatch):
    calls = []
    signer = object()
    monkeypatch.setattr(auth.jwt, "PyJWKClient", lambda url: calls.append(url) or signer)
    assert auth._jwks_client("https://auth.example.test/jwks") is signer
    assert auth._jwks_client("https://auth.example.test/jwks") is signer
    assert calls == ["https://auth.example.test/jwks"]


def test_modified_signature_is_rejected(signed_token):
    header, payload, signature = signed_token().split(".")
    changed = ("A" if signature[0] != "A" else "B") + signature[1:]
    with pytest.raises(HTTPException) as error:
        auth.decode_access_token(f"{header}.{payload}.{changed}")
    assert error.value.status_code == 401


def test_fresh_login_loads_only_assigned_restaurant(client, tenant, signed_token):
    with SessionLocal.begin() as db:
        db.add(Membership(
            auth_user_id="test-user", role="owner", active=True,
            business_id=tenant["business_id"], branch_id=tenant["branch_id"],
        ))
    now = int(datetime.now(timezone.utc).timestamp())
    headers = {"Authorization": f"Bearer {signed_token(iat=now + 6)}"}
    response = client.get("/api/v1/context", headers=headers)
    assert response.status_code == 200
    assert response.json()["business"]["id"] == tenant["business_id"]
    assert [branch["id"] for branch in response.json()["branches"]] == [tenant["branch_id"]]
    denied = client.get("/api/v1/context", headers={
        **headers, "X-Business-Id": str(tenant["other_business_id"]),
    })
    assert denied.status_code == 403


def test_key_server_outage_is_not_reported_as_invalid_credentials(monkeypatch):
    monkeypatch.setattr(auth, "get_settings", lambda: SimpleNamespace(
        supabase_jwt_secret=None, jwks_url="https://auth.example.test/jwks",
    ))

    def unavailable(token):
        raise jwt.PyJWKClientConnectionError("test connection failure")

    monkeypatch.setattr(auth.jwt, "PyJWKClient", lambda url: SimpleNamespace(
        get_signing_key_from_jwt=unavailable,
    ))
    with pytest.raises(HTTPException) as error:
        auth.decode_access_token("test-token")
    assert error.value.status_code == 503
