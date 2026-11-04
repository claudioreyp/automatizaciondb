"""Revocable browser/device credentials, separate from Supabase and integrations."""
import base64
import hashlib
import hmac
import json

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select

from .config import get_settings
from .models import Business, Branch, PairedDevice, StaffMember, StaffMemberBranch, StaffMemberRole, utcnow

DEVICE_COOKIE = "pos_device"
SESSION_COOKIE = "pos_staff"
COOKIE_PATH = "/api/v1"


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def secret():
    value = get_settings().device_auth_secret or ""
    if len(value) < 32:
        raise HTTPException(503, "Configura DEVICE_AUTH_SECRET antes de vincular dispositivos")
    return value.encode()


def seal(value):
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret()).digest())).encrypt(json.dumps(jsonable_encoder(value)).encode()).decode()


def unseal(value):
    try:
        return json.loads(Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret()).digest())).decrypt(value.encode()))
    except InvalidToken:
        raise HTTPException(409, "El intento anterior ya no se puede recuperar. Inicia una nueva operación") from None


def csrf_token(device_token):
    return hmac.new(secret(), ("csrf:" + device_token).encode(), hashlib.sha256).hexdigest()


def aware(value):
    from datetime import timezone
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def check_origin(request):
    origin = request.headers.get("origin", "")
    if origin not in get_settings().allowed_origins:
        raise HTTPException(403, "Origen no autorizado para acceso por dispositivo")


def check_csrf(request, token):
    check_origin(request)
    if not token or not hmac.compare_digest(request.headers.get("x-csrf-token", ""), csrf_token(token)):
        raise HTTPException(403, "Vuelve a abrir el acceso por PIN para renovar la sesión")


def device_for_token(db, token):
    if not token:
        raise HTTPException(401, "Vincula este dispositivo para acceder con PIN")
    device = db.scalar(select(PairedDevice).where(PairedDevice.token_hash == digest(token), PairedDevice.active.is_(True), PairedDevice.archived_at.is_(None)))
    if not device or not device.paired_at or not device.credential_expires_at or aware(device.credential_expires_at) <= utcnow():
        raise HTTPException(401, "La vinculación venció o fue revocada")
    branch = db.get(Branch, device.branch_id)
    business = db.get(Business, device.business_id)
    if not branch or not branch.active or not business or business.status != "active" or branch.business_id != device.business_id:
        raise HTTPException(401, "La sucursal ya no está disponible")
    return device


def eligible_member(db, device, member_id):
    member = db.scalar(select(StaffMember).join(StaffMemberBranch, StaffMemberBranch.staff_member_id == StaffMember.id).where(
        StaffMember.id == member_id, StaffMember.business_id == device.business_id, StaffMember.active.is_(True), StaffMember.archived_at.is_(None),
        StaffMemberBranch.branch_id == device.branch_id, StaffMemberBranch.business_id == device.business_id))
    if not member or not member.pin_hash:
        raise HTTPException(401, "Miembro o PIN no disponible")
    roles = sorted(db.scalars(select(StaffMemberRole.role).where(StaffMemberRole.staff_member_id == member.id, StaffMemberRole.business_id == device.business_id)))
    if not roles or "superadmin" in roles:
        raise HTTPException(403, "Este miembro no tiene acceso operativo")
    return member, roles


def access_hash(member, roles):
    return digest(json.dumps([member.id, member.pin_hash, roles, member.version]))


def session_user(db, request):
    from .auth import AuthContext
    token = request.cookies.get(DEVICE_COOKIE)
    device = device_for_token(db, token)
    if request.scope["type"] == "websocket":
        check_origin(request)
    elif request.method not in {"GET", "HEAD", "OPTIONS"}:
        check_csrf(request, token)
    session = request.cookies.get(SESSION_COOKIE, "")
    if not session or not device.staff_session_hash or not hmac.compare_digest(digest(session), device.staff_session_hash) or not device.staff_session_expires_at or aware(device.staff_session_expires_at) <= utcnow():
        raise HTTPException(401, "Ingresa tu PIN para continuar")
    member, roles = eligible_member(db, device, device.session_staff_id)
    if device.session_access_hash != access_hash(member, roles):
        raise HTTPException(401, "Los permisos cambiaron. Ingresa nuevamente tu PIN")
    priorities = ["owner", "manager", "members_manager", "menu_manager", "cashier", "waiter", "kitchen", "dispatcher"]
    role = next(role for role in priorities if role in roles)
    return AuthContext(member.auth_user_id or f"staff:{member.id}", role, device.business_id, device.branch_id, member.email,
                       staff_member_id=member.id, device_id=device.id, roles=tuple(roles))


def set_cookie(response, name, value, expires_at):
    response.set_cookie(name, value, max_age=max(0, int((aware(expires_at) - utcnow()).total_seconds())),
                        httponly=True, secure=not get_settings().is_development, samesite="lax", path=COOKIE_PATH)
    response.headers["Cache-Control"] = "no-store"


def clear_session(device):
    device.staff_session_hash = None
    device.staff_session_expires_at = None
    device.session_staff_id = None
    device.session_access_hash = None
