import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

import jwt
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db
from .models import Branch, IntegrationCredential, Membership, utcnow


ALL_ROLES = {"superadmin", "owner", "manager", "cashier", "waiter", "kitchen", "dispatcher"}


@dataclass(frozen=True)
class AuthContext:
    user_id: str
    role: str
    business_id: int | None
    branch_id: int | None
    email: str | None = None

    @property
    def is_superadmin(self) -> bool:
        return self.role == "superadmin"


@dataclass(frozen=True)
class IntegrationAuthContext:
    credential_id: int | None
    business_id: int | None
    branch_id: int | None
    scopes: frozenset[str]
    legacy: bool = False


ALL_INTEGRATION_SCOPES = frozenset(
    {
        "menu:read",
        "inventory:read",
        "inventory:write",
        "orders:read",
        "orders:write",
        "payments:write",
        "reservations:write",
        "events:read",
    }
)


def hash_integration_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def decode_access_token(token: str) -> dict:
    settings = get_settings()
    try:
        if settings.supabase_jwt_secret:
            return jwt.decode(
                token,
                settings.supabase_jwt_secret,
                algorithms=["HS256"],
                audience="authenticated",
                options={"verify_iss": False},
            )
        if settings.jwks_url:
            signing_key = jwt.PyJWKClient(settings.jwks_url).get_signing_key_from_jwt(token)
            return jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "ES256"],
                audience="authenticated",
                options={"verify_iss": False},
            )
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid access token") from exc
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Supabase authentication is not configured",
    )


def resolve_membership(
    db: Session,
    user_id: str,
    requested_business_id: int | None = None,
    requested_branch_id: int | None = None,
) -> AuthContext:
    memberships = list(
        db.scalars(
            select(Membership).where(Membership.auth_user_id == user_id, Membership.active.is_(True))
        )
    )
    if not memberships:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User has no active POS membership")

    superadmin = next((membership for membership in memberships if membership.role == "superadmin"), None)
    if superadmin and requested_business_id is None:
        return AuthContext(user_id, "superadmin", None, None, superadmin.email)

    candidates = memberships
    if requested_business_id is not None:
        candidates = [item for item in candidates if item.business_id == requested_business_id]
    if requested_branch_id is not None:
        candidates = [item for item in candidates if item.branch_id in {None, requested_branch_id}]
    selected = next((item for item in candidates if item.branch_id == requested_branch_id), None)
    selected = selected or next((item for item in candidates if item.branch_id is None), None)
    selected = selected or (candidates[0] if candidates else None)
    if not selected:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User cannot access this tenant scope")
    return AuthContext(user_id, selected.role, selected.business_id, requested_branch_id or selected.branch_id, selected.email)


def get_current_user(
    authorization: str | None = Header(default=None),
    x_business_id: int | None = Header(default=None),
    x_branch_id: int | None = Header(default=None),
    x_dev_auth: str | None = Header(default=None),
    x_dev_user: str | None = Header(default=None),
    x_dev_role: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> AuthContext:
    settings = get_settings()
    if settings.dev_auth_token and x_dev_auth == settings.dev_auth_token:
        role = x_dev_role if x_dev_role in ALL_ROLES else "owner"
        return AuthContext(x_dev_user or "dev-user", role, x_business_id, x_branch_id, "dev@impulsa.local")

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bearer token required")
    token = authorization.split(" ", 1)[1].strip()
    claims = decode_access_token(token)
    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has no subject")
    return resolve_membership(db, user_id, x_business_id, x_branch_id)


def get_authenticated_identity(
    authorization: str | None = Header(default=None),
    x_dev_auth: str | None = Header(default=None),
    x_dev_user: str | None = Header(default=None),
    x_dev_email: str | None = Header(default=None),
) -> AuthContext:
    """Validate an identity without requiring an existing POS membership.

    This dependency is intentionally limited to onboarding endpoints. All POS
    routes continue to use ``get_current_user`` and therefore require an active
    tenant membership.
    """
    settings = get_settings()
    if settings.dev_auth_token and x_dev_auth == settings.dev_auth_token:
        return AuthContext(
            x_dev_user or "dev-pending-user",
            "pending",
            None,
            None,
            x_dev_email or "pending@impulsa.local",
        )

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bearer token required")
    token = authorization.split(" ", 1)[1].strip()
    claims = decode_access_token(token)
    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has no subject")
    return AuthContext(user_id, "pending", None, None, claims.get("email"))


def require_roles(*allowed_roles: str):
    def dependency(user: AuthContext = Depends(get_current_user)) -> AuthContext:
        if user.role not in allowed_roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")
        return user

    return dependency


def require_integration_token(x_integration_token: str | None = Header(default=None)) -> None:
    expected = get_settings().integration_service_token
    if not expected:
        raise HTTPException(status_code=503, detail="Integration service token is not configured")
    if not x_integration_token or not secrets.compare_digest(x_integration_token, expected):
        raise HTTPException(status_code=401, detail="Invalid integration token")


def require_integration_scope(required_scope: str):
    if required_scope not in ALL_INTEGRATION_SCOPES:
        raise ValueError(f"Unsupported integration scope: {required_scope}")

    def dependency(
        authorization: str | None = Header(default=None),
        x_integration_token: str | None = Header(default=None),
        db: Session = Depends(get_db),
    ) -> IntegrationAuthContext:
        settings = get_settings()
        if x_integration_token and settings.integration_service_token and secrets.compare_digest(
            x_integration_token, settings.integration_service_token
        ):
            return IntegrationAuthContext(None, None, None, ALL_INTEGRATION_SCOPES, legacy=True)

        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Integration bearer token required")
        token = authorization.split(" ", 1)[1].strip()
        prefix = token.split(".", 1)[0]
        credential = db.scalar(
            select(IntegrationCredential).where(
                IntegrationCredential.token_prefix == prefix,
                IntegrationCredential.active.is_(True),
                IntegrationCredential.revoked_at.is_(None),
            )
        )
        if not credential or not secrets.compare_digest(credential.token_hash, hash_integration_token(token)):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid integration token")
        now = datetime.now(timezone.utc)
        expires_at = credential.expires_at
        if expires_at is not None:
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= now:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Integration token expired")
        if required_scope not in set(credential.scopes or []):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Missing scope: {required_scope}")
        branch = db.scalar(
            select(Branch).where(
                Branch.id == credential.branch_id,
                Branch.business_id == credential.business_id,
                Branch.active.is_(True),
            )
        )
        if not branch:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Integration branch is inactive")
        credential.last_used_at = utcnow()
        db.commit()
        return IntegrationAuthContext(
            credential.id,
            credential.business_id,
            credential.branch_id,
            frozenset(credential.scopes or []),
        )

    return dependency


def ensure_business_scope(user: AuthContext, business_id: int) -> None:
    if not user.is_superadmin and user.business_id != business_id:
        raise HTTPException(status_code=403, detail="Cross-business access denied")


def ensure_branch_scope(user: AuthContext, business_id: int, branch_id: int) -> None:
    ensure_business_scope(user, business_id)
    if user.branch_id is not None and user.branch_id != branch_id:
        raise HTTPException(status_code=403, detail="Cross-branch access denied")
