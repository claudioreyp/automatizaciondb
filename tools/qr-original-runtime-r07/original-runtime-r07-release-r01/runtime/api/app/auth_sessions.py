"""Provider session checks keyed by a durable, locally controlled security epoch."""
from threading import RLock
from time import monotonic
from uuid import UUID

import httpx
from fastapi import HTTPException
from sqlalchemy import select

from .config import get_settings
from .models import AuthSecurityState

_lock = RLock()
_session_locks = [RLock() for _ in range(128)]
_verified: dict[tuple, float] = {}


def provider_headers():
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_service_role_key:
        raise HTTPException(503, "El servicio de acceso no esta configurado.")
    return {"apikey": settings.supabase_service_role_key,
            "Authorization": f"Bearer {settings.supabase_service_role_key}"}


def provider_user(user_id: str):
    settings = get_settings()
    headers = provider_headers()
    try:
        response = httpx.get(f"{settings.supabase_url.rstrip('/')}/auth/v1/admin/users/{user_id}",
                             headers=headers, timeout=10)
    except httpx.HTTPError:
        raise HTTPException(503, "No se pudo comprobar la identidad. Intenta nuevamente.") from None
    if response.status_code == 404:
        raise HTTPException(409, "La identidad no existe en el proyecto configurado.")
    if response.status_code != 200:
        raise HTTPException(503, "No se pudo comprobar la identidad. Intenta nuevamente.")
    try:
        data = response.json()
    except ValueError:
        raise HTTPException(503, "No se pudo comprobar la identidad. Intenta nuevamente.") from None
    if not isinstance(data, dict) or data.get("id") != user_id:
        raise HTTPException(503, "La identidad recibida no coincide.")
    return data


def _state(db, subject):
    row = db.execute(select(AuthSecurityState.version, AuthSecurityState.pending_operation_id,
                            AuthSecurityState.requires_password_reset).where(
                                AuthSecurityState.auth_user_id == subject)).first()
    if row and (row.pending_operation_id or row.requires_password_reset):
        raise HTTPException(401, "Tu acceso requiere renovar la contrasena desde Administracion.")
    return row.version if row else 0


def validate_provider_session(db, token: str, claims: dict, settings=None):
    settings = settings or get_settings()
    subject = claims.get("sub")
    version = _state(db, subject)
    url = getattr(settings, "supabase_url", None)
    # Test-only signing configurations have no remote provider. Production decode
    # rejects a missing issuer configuration before this function can be reached.
    if not url:
        return
    try:
        UUID(claims.get("session_id", ""))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(401, "La sesion no es valida. Inicia sesion nuevamente.") from None
    key = (url, subject, claims["session_id"], version)
    # Bounded positive cache; epoch is read from DB on EVERY authorization.
    with _session_locks[hash(key) % len(_session_locks)]:
        now = monotonic()
        with _lock:
            if _verified.get(key, 0) > now:
                return
        try:
            headers = provider_headers()
            headers["Authorization"] = f"Bearer {token}"
            response = httpx.get(f"{url.rstrip('/')}/auth/v1/user", headers=headers, timeout=10)
        except httpx.HTTPError:
            raise HTTPException(503, "No se pudo comprobar tu sesion temporalmente.") from None
        if response.status_code in (401, 403):
            raise HTTPException(401, "Tu sesion finalizo. Inicia sesion nuevamente.")
        if response.status_code != 200:
            raise HTTPException(503, "No se pudo comprobar tu sesion temporalmente.")
        try:
            identity = response.json()
        except ValueError:
            raise HTTPException(503, "No se pudo comprobar tu sesion temporalmente.") from None
        if not isinstance(identity, dict):
            raise HTTPException(503, "No se pudo comprobar tu sesion temporalmente.")
        if identity.get("id") != subject or _state(db, subject) != version:
            raise HTTPException(401, "Tu sesion cambio. Inicia sesion nuevamente.")
        with _lock:
            for old_key in list(_verified):
                if _verified[old_key] <= now:
                    del _verified[old_key]
            if len(_verified) >= 2048:
                _verified.pop(next(iter(_verified)))
            _verified[key] = monotonic() + 60
