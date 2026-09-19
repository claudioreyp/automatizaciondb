import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache

import jwt
from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db
from .models import Branch, Business, IntegrationCredential, Membership, StaffMember, StaffMemberBranch, StaffMemberRole, utcnow


ALL_ROLES = {"superadmin", "owner", "manager", "cashier", "waiter", "kitchen", "dispatcher"}
# Auth and API clocks can differ slightly when a fresh session is issued.
TOKEN_CLOCK_SKEW_SECONDS = 30


@lru_cache(maxsize=4)
def _jwks_client(url: str):
    # PyJWT refreshes expired key sets and unknown kids. Reuse that bounded cache
    # without caching permissions or skipping signature/session verification.
    return jwt.PyJWKClient(url)


@dataclass(frozen=True)
class AuthContext:
    user_id: str
    role: str
    business_id: int | None
    branch_id: int | None
    email: str | None = None
    staff_member_id: int | None = None
    device_id: int | None = None
    roles: tuple[str, ...] = ()

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


def require_active_scope(db: Session, business_id: int | None, branch_id: int | None = None) -> None:
    business = db.get(Business, business_id) if business_id is not None else None
    if not business or business.status != "active":
        raise HTTPException(403, "Business is inactive")
    if branch_id is not None:
        branch = db.get(Branch, branch_id)
        if not branch or branch.business_id != business_id or not branch.active or branch.archived_at is not None:
            raise HTTPException(403, "Branch is inactive or outside this business")


def decode_access_token(token: str) -> dict:
    settings = get_settings()
    issuer = f"{settings.supabase_url.rstrip('/')}/auth/v1" if getattr(settings, "supabase_url", None) else None
    if not issuer and not getattr(settings, "is_development", True):
        raise HTTPException(503, "Supabase issuer is not configured")
    try:
        if settings.supabase_jwt_secret:
            return jwt.decode(
                token,
                settings.supabase_jwt_secret,
                algorithms=["HS256"],
                audience="authenticated",
                leeway=TOKEN_CLOCK_SKEW_SECONDS,
                issuer=issuer,
                options={"verify_iss": bool(issuer)},
            )
        if settings.jwks_url:
            signing_key = _jwks_client(settings.jwks_url).get_signing_key_from_jwt(token)
            return jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "ES256"],
                audience="authenticated",
                leeway=TOKEN_CLOCK_SKEW_SECONDS,
                issuer=issuer,
                options={"verify_iss": bool(issuer)},
            )
    except jwt.PyJWKClientConnectionError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No se pudo verificar tu sesi\u00f3n temporalmente. Vuelve a intentarlo.",
        ) from exc
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
    if superadmin:
        return AuthContext(user_id, "superadmin", requested_business_id, requested_branch_id, superadmin.email)

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
    branch_id = requested_branch_id or selected.branch_id
    require_active_scope(db, selected.business_id, branch_id)
    member = db.scalar(select(StaffMember).where(
        StaffMember.business_id == selected.business_id, StaffMember.auth_user_id == user_id,
    ))
    roles: tuple[str, ...] = ()
    if member:
        if not member.active or member.archived_at is not None or not member.email_access:
            raise HTTPException(403, "Staff access is inactive")
        roles = tuple(db.scalars(select(StaffMemberRole.role).where(StaffMemberRole.staff_member_id == member.id)))
        if not roles:
            raise HTTPException(403, "Staff has no active roles")
        if branch_id is not None and not db.scalar(select(StaffMemberBranch.branch_id).where(
            StaffMemberBranch.staff_member_id == member.id, StaffMemberBranch.branch_id == branch_id,
        )):
            raise HTTPException(403, "Staff cannot access this branch")
    role = selected.role if not roles or selected.role in roles else roles[0]
    return AuthContext(user_id, role, selected.business_id, branch_id, selected.email,
                       staff_member_id=member.id if member else None, roles=roles)


def get_current_user(
    request: Request,
    authorization: str | None = Header(default=None),
    x_business_id: int | None = Header(default=None),
    x_branch_id: int | None = Header(default=None),
    x_dev_auth: str | None = Header(default=None),
    x_dev_user: str | None = Header(default=None),
    x_dev_role: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> AuthContext:
    settings = get_settings()
    if getattr(settings, "is_development", False) and settings.dev_auth_token and x_dev_auth == settings.dev_auth_token:
        role = x_dev_role if x_dev_role in ALL_ROLES else "owner"
        if role != "superadmin":
            require_active_scope(db, x_business_id, x_branch_id)
        return AuthContext(x_dev_user or "dev-user", role, x_business_id, x_branch_id, "dev@impulsa.local")

    if not authorization or not authorization.lower().startswith("bearer "):
        from .device_auth import session_user
        user = session_user(db, request)
        if x_business_id is not None and x_business_id != user.business_id or x_branch_id is not None and x_branch_id != user.branch_id:
            raise HTTPException(403, "El dispositivo pertenece a otra sucursal")
        return user
    token = authorization.split(" ", 1)[1].strip()
    claims = decode_access_token(token)
    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has no subject")
    from .auth_sessions import validate_provider_session
    validate_provider_session(db, token, claims, settings)
    return resolve_membership(db, user_id, x_business_id, x_branch_id)


def get_authenticated_identity(
    authorization: str | None = Header(default=None),
    x_dev_auth: str | None = Header(default=None),
    x_dev_user: str | None = Header(default=None),
    x_dev_email: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> AuthContext:
    """Validate an identity without requiring an existing POS membership.

    This dependency is intentionally limited to onboarding endpoints. All POS
    routes continue to use ``get_current_user`` and therefore require an active
    tenant membership.
    """
    settings = get_settings()
    if getattr(settings, "is_development", False) and settings.dev_auth_token and x_dev_auth == settings.dev_auth_token:
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
    from .auth_sessions import validate_provider_session
    validate_provider_session(db, token, claims, settings)
    return AuthContext(user_id, "pending", None, None, claims.get("email"))


def require_roles(*allowed_roles: str):
    def dependency(user: AuthContext = Depends(get_current_user)) -> AuthContext:
        if not set(user.roles or (user.role,)).intersection(allowed_roles):
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
        require_active_scope(db, credential.business_id, credential.branch_id)
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
