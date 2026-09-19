"""Only Admins can renew owner credentials; never persist the password itself."""
import hashlib
import hmac
import json
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, Field, SecretStr, field_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .auth import AuthContext, require_roles
from .auth_sessions import provider_headers, provider_user
from .config import get_settings
from .database import get_db
from .models import AuthSecurityState, Membership, PasswordResetOperation, utcnow
from .services import audit

router = APIRouter(tags=["superadmin"])
MARKER = "pos_password_reset_operation"


class PasswordResetRequest(BaseModel):
    password: SecretStr
    expected_version: int = Field(ge=0)

    @field_validator("password")
    @classmethod
    def strong_password(cls, value):
        password = value.get_secret_value()
        if not (12 <= len(password) <= 128 and any(c.islower() for c in password)
                and any(c.isupper() for c in password) and any(c.isdigit() for c in password)
                and any(not c.isalnum() and not c.isspace() for c in password)):
            raise ValueError("Usa 12 a 128 caracteres, mayusculas, minusculas, numeros y un simbolo.")
        return value


def reset_capabilities(db, member):
    try:
        UUID(member.auth_user_id)
        valid_id = True
    except (TypeError, ValueError):
        valid_id = False
    superadmin = db.scalar(select(Membership.id).where(
        Membership.auth_user_id == member.auth_user_id, Membership.role == "superadmin"))
    state = db.get(AuthSecurityState, member.auth_user_id)
    businesses = set(db.scalars(select(Membership.business_id).where(
        Membership.auth_user_id == member.auth_user_id, Membership.business_id.is_not(None))))
    return {
        "can_reset_password": bool(valid_id and member.email and member.role == "owner" and not superadmin),
        "password_security_version": state.version if state else 0,
        "password_reset_required": bool(state and state.requires_password_reset),
        "password_reset_operation_id": state.pending_operation_id if state else None,
        "identity_business_count": len(businesses),
    }


def owner(db, business_id, membership_id):
    member = db.scalar(select(Membership).where(
        Membership.id == membership_id, Membership.business_id == business_id).with_for_update())
    if not member:
        raise HTTPException(404, "No se encontro el propietario de este negocio.")
    if not reset_capabilities(db, member)["can_reset_password"]:
        raise HTTPException(403, "Esta accion solo permite renovar propietarios con acceso por correo.")
    return member


def result(operation):
    return {"operation_id": operation.id, "status": operation.status,
            "security_version": operation.security_version, "error_code": operation.error_code,
            "client_pos_url": get_settings().pos_public_base_url or "http://localhost:5173"}


def finish(db, operation, status, actor, error_code=None):
    # A second reconciliation must not create another audit or clear a newer reset.
    changed = db.execute(update(PasswordResetOperation).where(
        PasswordResetOperation.id == operation.id, PasswordResetOperation.status == "pending"
    ).values(status=status, error_code=error_code, updated_at=utcnow())).rowcount
    if changed:
        values = {"pending_operation_id": None, "updated_at": utcnow()}
        if status == "succeeded":
            values["requires_password_reset"] = False
        db.execute(update(AuthSecurityState).where(
            AuthSecurityState.auth_user_id == operation.auth_user_id,
            AuthSecurityState.pending_operation_id == operation.id).values(**values))
        audit(db, actor, f"owner.password_reset.{status}", "membership", operation.membership_id,
              operation.business_id, {"operation_id": operation.id})
        db.commit()
    db.refresh(operation)
    return result(operation)


@router.post("/admin/businesses/{business_id}/memberships/{membership_id}/password-reset")
def reset_password(business_id: int, membership_id: int, payload: PasswordResetRequest,
                   response: Response, idempotency_key: str = Header(min_length=8, max_length=200),
                   user: AuthContext = Depends(require_roles("superadmin")), db=Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    member = owner(db, business_id, membership_id)
    settings = get_settings()
    secret = settings.auth_admin_secret
    if not secret or len(secret) < 32:
        raise HTTPException(503, "La renovacion segura no esta configurada en el servidor.")
    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()
    body = json.dumps([business_id, membership_id, payload.expected_version, payload.password.get_secret_value()])
    digest = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    previous = db.scalar(select(PasswordResetOperation).where(
        PasswordResetOperation.auth_user_id == member.auth_user_id, PasswordResetOperation.key_hash == key_hash))
    if previous:
        if not hmac.compare_digest(previous.request_digest, digest):
            raise HTTPException(409, "Esta solicitud ya se uso con otros datos.")
        return result(previous)
    identity = provider_user(member.auth_user_id)
    if (identity.get("email") or "").casefold() != member.email.casefold():
        raise HTTPException(409, "La identidad no coincide con el propietario. Revisa su acceso.")
    state = db.get(AuthSecurityState, member.auth_user_id)
    if not state:
        state = AuthSecurityState(auth_user_id=member.auth_user_id, version=0)
        db.add(state)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "El acceso cambio. Actualiza antes de continuar.") from None
    operation_id = str(uuid4())
    claimed = db.execute(update(AuthSecurityState).where(
        AuthSecurityState.auth_user_id == member.auth_user_id,
        AuthSecurityState.version == payload.expected_version,
        AuthSecurityState.pending_operation_id.is_(None),
    ).values(version=payload.expected_version + 1, pending_operation_id=operation_id, updated_at=utcnow())).rowcount
    if not claimed:
        db.rollback()
        raise HTTPException(409, "Hay una renovacion pendiente o el acceso cambio. Actualiza antes de continuar.")
    operation = PasswordResetOperation(id=operation_id, auth_user_id=member.auth_user_id,
        membership_id=member.id, business_id=business_id, actor_id=user.user_id,
        key_hash=key_hash, request_digest=digest, security_version=payload.expected_version + 1)
    db.add(operation)
    audit(db, user, "owner.password_reset.requested", "membership", member.id, business_id,
          {"operation_id": operation_id})
    db.commit()  # Durable lock BEFORE contacting a service whose response may be lost.
    metadata = {**(identity.get("app_metadata") or {}), MARKER: operation_id}
    try:
        upstream = httpx.put(f"{settings.supabase_url.rstrip('/')}/auth/v1/admin/users/{operation.auth_user_id}",
            headers=provider_headers(), json={"password": payload.password.get_secret_value(), "app_metadata": metadata},
            timeout=15)
    except httpx.HTTPError:
        response.status_code = 202
        return result(operation)
    if upstream.status_code == 200:
        try:
            data = upstream.json()
        except ValueError:
            data = None
        if isinstance(data, dict) and data.get("id") == operation.auth_user_id and isinstance(data.get("app_metadata"), dict) and data["app_metadata"].get(MARKER) == operation.id:
            return finish(db, operation, "succeeded", user)
    if upstream.status_code in (400, 401, 403, 404, 422, 429):
        return finish(db, operation, "failed", user, "PROVIDER_REJECTED")
    response.status_code = 202
    return result(operation)


@router.get("/admin/businesses/{business_id}/memberships/{membership_id}/password-reset/lookup")
def reset_lookup(business_id: int, membership_id: int, response: Response,
                 idempotency_key: str = Header(min_length=8, max_length=200),
                 user: AuthContext = Depends(require_roles("superadmin")), db=Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    operation = db.scalar(select(PasswordResetOperation).where(
        PasswordResetOperation.membership_id == membership_id,
        PasswordResetOperation.business_id == business_id,
        PasswordResetOperation.key_hash == hashlib.sha256(idempotency_key.encode()).hexdigest()))
    if not operation:
        # Absence does not prove a lost POST cannot still finish. Never resend here.
        return {"status": "unconfirmed", "operation_id": None}
    return reset_status(business_id, membership_id, operation.id, response, user, db)


@router.get("/admin/businesses/{business_id}/memberships/{membership_id}/password-reset/{operation_id}")
def reset_status(business_id: int, membership_id: int, operation_id: str, response: Response,
                 user: AuthContext = Depends(require_roles("superadmin")), db=Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    operation = db.get(PasswordResetOperation, operation_id)
    if not operation or operation.business_id != business_id or operation.membership_id != membership_id:
        raise HTTPException(404, "No se encontro esta renovacion.")
    if operation.status == "pending":
        identity = provider_user(operation.auth_user_id)
        if identity.get("app_metadata", {}).get(MARKER) == operation.id:
            return finish(db, operation, "succeeded", user)
    return result(operation)
