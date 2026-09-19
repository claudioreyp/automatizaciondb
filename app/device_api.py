import hmac
import json
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import AuthContext, get_current_user
from .config import get_settings
from .database import get_db
from .device_auth import (DEVICE_COOKIE, SESSION_COOKIE, COOKIE_PATH, access_hash, aware, check_origin, check_csrf, clear_session, csrf_token,
                          device_for_token, digest, eligible_member, seal, secret, session_user, set_cookie, unseal)
from .models import Branch, Business, PairedDevice, StaffMember, StaffMemberBranch, utcnow
from .services import assert_version, audit, get_idempotent_response, save_idempotent_response
from .settings_api import _key, _device_response
from .settings_service import actor_display_name, lock_staff_business, require_settings_permission, scoped_branch, verify_staff_pin

router = APIRouter(tags=["device access"])


class PairingCreate(BaseModel):
    branch_id: int


class PairingVersion(BaseModel):
    expected_version: int = Field(ge=1)


class PairingToken(BaseModel):
    token: SecretStr = Field(min_length=30, max_length=200)


class Activate(PairingToken):
    name: str = Field(min_length=2, max_length=180)


class PinLogin(BaseModel):
    member_id: int
    pin: SecretStr = Field(min_length=4, max_length=4)


def signed_fingerprint(value):
    import hashlib
    return hmac.new(secret(), json.dumps(value, sort_keys=True).encode(), hashlib.sha256).hexdigest()


def saved_attempt(db, scope, key, business_id, signature):
    record = get_idempotent_response(db, scope, key, business_id)
    if record:
        if record["signature"] != signature:
            raise HTTPException(409, "La clave de reintento pertenece a otra operación")
        return unseal(record["sealed"])


def save_attempt(db, scope, key, business_id, signature, result):
    save_idempotent_response(db, scope, key, business_id, {"signature": signature, "sealed": seal(result)})


def link_result(device, token):
    base = (get_settings().pos_public_base_url or "").rstrip("/")
    if not base or not urlsplit(base).hostname or urlsplit(base).scheme not in {"http", "https"} or urlsplit(base).username or urlsplit(base).query or urlsplit(base).fragment:
        raise HTTPException(503, "Configura POS_PUBLIC_BASE_URL con la dirección accesible del POS")
    if not get_settings().is_development and urlsplit(base).scheme != "https":
        raise HTTPException(503, "La vinculación requiere HTTPS")
    return {"device": _device_response(device), "url": f"{base}/activar-dispositivo#token={token}",
            "expires_at": aware(device.pairing_expires_at).isoformat()}


def device_audit(db, user, device, action):
    audit(db, user, action, "paired_device", device.id, device.business_id, {"name": device.name},
          branch_id=device.branch_id, actor_display_name=actor_display_name(db, user) if user else "Dispositivo")


@router.post("/settings/devices/pairing-links")
def create_link(payload: PairingCreate, response: Response, idempotency_key: str | None = Header(None), user: AuthContext = Depends(get_current_user), db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    branch = scoped_branch(db, user, payload.branch_id)
    require_settings_permission(db, user, branch.business_id, "members")
    lock_staff_business(db, branch.business_id)
    key, scope = _key(idempotency_key), f"device.link.{branch.id}.{user.user_id}"
    signature = signed_fingerprint(payload.model_dump())
    previous = saved_attempt(db, scope, key, branch.business_id, signature)
    if previous:
        device = db.get(PairedDevice, previous["id"])
        if not device or not device.active or device.paired_at or device.pairing_code_hash != digest(previous["token"]) or aware(device.pairing_expires_at) <= utcnow():
            raise HTTPException(410, "El enlace ya no está disponible. Genera uno nuevo")
        return link_result(device, previous["token"])
    token = secrets.token_urlsafe(40)
    device = PairedDevice(business_id=branch.business_id, branch_id=branch.id, name="Dispositivo pendiente",
                          pairing_code_hash=digest(token), pairing_expires_at=utcnow() + timedelta(minutes=10))
    db.add(device)
    db.flush()
    result = link_result(device, token)
    save_attempt(db, scope, key, branch.business_id, signature, {"id": device.id, "token": token})
    device_audit(db, user, device, "settings.device.link.created")
    db.commit()
    return result


@router.delete("/settings/devices/{device_id}/pairing-link")
def cancel_link(device_id: int, payload: PairingVersion, idempotency_key: str | None = Header(None), user: AuthContext = Depends(get_current_user), db: Session = Depends(get_db)):
    device = db.get(PairedDevice, device_id)
    if not device:
        raise HTTPException(404, "Dispositivo no encontrado")
    branch = scoped_branch(db, user, device.branch_id)
    require_settings_permission(db, user, branch.business_id, "members")
    lock_staff_business(db, branch.business_id)
    db.refresh(device)
    key, scope = _key(idempotency_key), f"device.link.{device.id}.cancel"
    signature = signed_fingerprint(payload.model_dump())
    previous = saved_attempt(db, scope, key, branch.business_id, signature)
    if previous:
        return previous
    if device.paired_at:
        raise HTTPException(409, "El dispositivo ya fue activado; puedes desvincularlo desde la lista")
    assert_version(device.version, payload.expected_version)
    device.pairing_code_hash = None
    device.pairing_expires_at = None
    device.active = False
    device.archived_at = utcnow()
    device.version += 1
    result = _device_response(device)
    save_attempt(db, scope, key, branch.business_id, signature, result)
    device_audit(db, user, device, "settings.device.link.cancelled")
    db.commit()
    return result


def pending_device(db, token):
    device = db.scalar(select(PairedDevice).where(PairedDevice.pairing_code_hash == digest(token), PairedDevice.active.is_(True), PairedDevice.archived_at.is_(None)))
    if not device or device.paired_at or not device.pairing_expires_at or aware(device.pairing_expires_at) <= utcnow():
        raise HTTPException(410, "El enlace venció, fue cancelado o ya se utilizó")
    branch = db.get(Branch, device.branch_id)
    business = db.get(Business, device.business_id)
    if not branch or not branch.active or not business or business.status != "active":
        raise HTTPException(410, "La sucursal ya no está disponible")
    return device, branch


@router.post("/auth/devices/preview")
def preview_link(payload: PairingToken, request: Request, response: Response, db: Session = Depends(get_db)):
    check_origin(request)
    device, branch = pending_device(db, payload.token.get_secret_value())
    response.headers["Cache-Control"] = "no-store"
    return {"branch_name": branch.name, "expires_at": aware(device.pairing_expires_at).isoformat()}


@router.post("/auth/devices/activate")
def activate(payload: Activate, request: Request, response: Response, idempotency_key: str | None = Header(None), db: Session = Depends(get_db)):
    check_origin(request)
    token, key = payload.token.get_secret_value(), _key(idempotency_key)
    scope = f"device.activate.{digest(token)}"
    signature = signed_fingerprint([token, payload.name.strip()])
    # The pending row remains discoverable for idempotent recovery, but is never reusable.
    record = db.scalar(select(PairedDevice).where(PairedDevice.pairing_code_hash == digest(token)))
    if not record:
        raise HTTPException(410, "El enlace ya no está disponible")
    lock_staff_business(db, record.business_id)
    db.refresh(record)
    previous = saved_attempt(db, scope, key, record.business_id, signature)
    if previous:
        device_for_token(db, previous["credential"])
        if aware(record.pairing_expires_at) <= utcnow():
            raise HTTPException(410, "El plazo de recuperación del enlace venció")
        set_cookie(response, DEVICE_COOKIE, previous["credential"], record.credential_expires_at)
        return {"activated": True, "branch_name": db.get(Branch, record.branch_id).name}
    device, branch = pending_device(db, token)
    if len(payload.name.strip()) < 2:
        raise HTTPException(422, "Escribe un nombre para el dispositivo")
    existing_token = request.cookies.get(DEVICE_COOKIE)
    if existing_token:
        try:
            existing = device_for_token(db, existing_token)
        except HTTPException:
            existing = None
        if existing and existing.id != device.id:
            raise HTTPException(409, "Este navegador ya está vinculado; desvincúlalo antes de cambiar de sucursal")
    credential = secrets.token_urlsafe(40)
    device.name = payload.name.strip()
    device.token_hash = digest(credential)
    device.paired_at = utcnow()
    device.credential_expires_at = utcnow() + timedelta(days=30)
    device.version += 1
    save_attempt(db, scope, key, device.business_id, signature, {"credential": credential})
    device_audit(db, None, device, "settings.device.activated")
    db.commit()
    set_cookie(response, DEVICE_COOKIE, credential, device.credential_expires_at)
    response.delete_cookie(SESSION_COOKIE, path=COOKIE_PATH)
    return {"activated": True, "branch_name": branch.name}


@router.get("/auth/devices/session")
def read_session(request: Request, response: Response, db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    token = request.cookies.get(DEVICE_COOKIE)
    if not token:
        return {"linked": False, "user": None}
    try:
        device = device_for_token(db, token)
    except HTTPException:
        return {"linked": False, "user": None}
    result = {"linked": True, "branch_id": device.branch_id, "business_id": device.business_id,
              "branch_name": db.get(Branch, device.branch_id).name, "csrf_token": csrf_token(token), "user": None}
    try:
        user = session_user(db, request)
        member = db.get(StaffMember, user.staff_member_id)
        result["user"] = {"id": user.user_id, "name": f"{member.first_name} {member.last_name}".strip(), "roles": user.roles}
    except HTTPException:
        pass
    return result


@router.get("/auth/devices/members")
def members(request: Request, response: Response, db: Session = Depends(get_db)):
    device = device_for_token(db, request.cookies.get(DEVICE_COOKIE))
    response.headers["Cache-Control"] = "no-store"
    rows = db.scalars(select(StaffMember).join(StaffMemberBranch, StaffMemberBranch.staff_member_id == StaffMember.id).where(
        StaffMember.business_id == device.business_id, StaffMemberBranch.business_id == device.business_id,
        StaffMemberBranch.branch_id == device.branch_id, StaffMember.active.is_(True), StaffMember.archived_at.is_(None), StaffMember.pin_hash.is_not(None)).order_by(StaffMember.first_name, StaffMember.id))
    result = []
    for member in rows:
        try:
            eligible_member(db, device, member.id)
            result.append({"id": member.id, "name": f"{member.first_name} {member.last_name}".strip()})
        except HTTPException:
            continue
    return {"items": result}


@router.post("/auth/devices/login")
def login(payload: PinLogin, request: Request, response: Response, idempotency_key: str | None = Header(None), db: Session = Depends(get_db)):
    token = request.cookies.get(DEVICE_COOKIE)
    check_csrf(request, token)
    device = device_for_token(db, token)
    lock_staff_business(db, device.business_id)
    db.refresh(device)
    device_for_token(db, token)
    key, scope = _key(idempotency_key), f"device.login.{device.id}"
    pin = payload.pin.get_secret_value()
    if not pin.isascii() or not pin.isdigit():
        raise HTTPException(422, "El PIN debe tener cuatro dígitos")
    signature = signed_fingerprint([payload.member_id, pin, token])
    previous = saved_attempt(db, scope, key, device.business_id, signature)
    if previous:
        if previous.get("failed"):
            raise HTTPException(401, "PIN incorrecto")
        member, roles = eligible_member(db, device, payload.member_id)
        if device.staff_session_hash != digest(previous["session"]) or device.session_access_hash != access_hash(member, roles) or aware(device.staff_session_expires_at) <= utcnow():
            raise HTTPException(409, "Esta sesión ya no está vigente. Ingresa nuevamente tu PIN")
        set_cookie(response, SESSION_COOKIE, previous["session"], device.staff_session_expires_at)
        return {"authenticated": True}
    if device.pin_locked_until and aware(device.pin_locked_until) > utcnow():
        raise HTTPException(429, "Demasiados intentos. Espera diez minutos")
    member, roles = eligible_member(db, device, payload.member_id)
    if not verify_staff_pin(db, member, pin):
        device.failed_pin_attempts += 1
        if device.failed_pin_attempts >= 5:
            device.pin_locked_until = utcnow() + timedelta(minutes=10)
            device.failed_pin_attempts = 0
        save_attempt(db, scope, key, device.business_id, signature, {"failed": True})
        db.commit()
        raise HTTPException(401, "PIN incorrecto")
    session = secrets.token_urlsafe(40)
    device.staff_session_hash = digest(session)
    device.staff_session_expires_at = utcnow() + timedelta(hours=8)
    device.session_staff_id = member.id
    device.session_access_hash = access_hash(member, roles)
    device.failed_pin_attempts = 0
    device.pin_locked_until = None
    device.last_used_at = utcnow()
    save_attempt(db, scope, key, device.business_id, signature, {"session": session})
    audit(db, AuthContext(member.auth_user_id or f"staff:{member.id}", roles[0], device.business_id, device.branch_id),
          "settings.device.pin_login", "paired_device", device.id, device.business_id, {"staff_member_id": member.id},
          branch_id=device.branch_id, actor_display_name=f"{member.first_name} {member.last_name}".strip())
    db.commit()
    set_cookie(response, SESSION_COOKIE, session, device.staff_session_expires_at)
    return {"authenticated": True}


@router.post("/auth/devices/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    token = request.cookies.get(DEVICE_COOKIE)
    check_csrf(request, token)
    device = device_for_token(db, token)
    lock_staff_business(db, device.business_id)
    db.refresh(device)
    clear_session(device)
    db.commit()
    response.delete_cookie(SESSION_COOKIE, path=COOKIE_PATH)
    response.headers["Cache-Control"] = "no-store"
    return {"signed_out": True}
