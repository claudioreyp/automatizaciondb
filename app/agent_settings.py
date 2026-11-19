"""Branch-owned agent presentation. No workflow or agent execution lives here."""
import hashlib
import json
from uuid import uuid4
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .auth import AuthContext, get_current_user, require_integration_scope
from .database import get_db
from .models import Branch
from .services import assert_version, audit, get_idempotent_response, save_idempotent_response
from .settings_api import _key, _validate_branch_media, BRANCH_MEDIA_MAX_BYTES
from .settings_service import scoped_branch, require_settings_permission, actor_display_name, lock_staff_business
from .storage import store_private_file, load_private_file, delete_private_file
from .agent_menu_pdf import MENU_PDF_MAX_BYTES, menu_pdf_filename, validate_menu_pdf

router = APIRouter(tags=["agent settings"])


def menu_images(branch):
    images = [dict(item) for item in (branch.agent_menu_images or [])]
    # Old integrations may replace the primary through the legacy upload route.
    if branch.menu_card_storage_path:
        if images:
            images[0]["path"] = branch.menu_card_storage_path
        else:
            images = [{"id": "legacy", "path": branch.menu_card_storage_path}]
    return images


def set_menu_images(branch, images):
    branch.agent_menu_images = images
    branch.menu_card_storage_path = images[0]["path"] if images else None


def agent_response(branch):
    base = f"/api/v1/settings/branches/{branch.id}/agent"
    return {"branch_id": branch.id, "version": branch.version, "name": branch.agent_name,
            "yape_number": branch.yape_number, "payment_recipient_name": branch.payment_recipient_name,
            "images": [{"id": item["id"], "url": f"{base}/images/{item['id']}?v={branch.version}"} for item in menu_images(branch)],
            "menu_pdf": {"filename": branch.agent_menu_pdf["filename"], "size_bytes": branch.agent_menu_pdf["size_bytes"],
                         "url": f"{base}/menu-pdf?v={branch.version}"} if branch.agent_menu_pdf else None,
            "yape_qr_url": f"{base}/yape-qr?v={branch.version}" if branch.yape_qr_storage_path else None}


class AgentUpdate(BaseModel):
    expected_version: int = Field(ge=1)
    name: str | None = Field(default=None, max_length=80)
    yape_number: str | None = Field(default=None, max_length=40)
    payment_recipient_name: str | None = Field(default=None, max_length=180)
    image_order: list[str] | None = None

    @field_validator("name", "yape_number", "payment_recipient_name")
    @classmethod
    def clean_name(cls, value):
        return value.strip() or None if value else None


class MediaDelete(BaseModel):
    expected_version: int = Field(ge=1)


def writable(db, user, branch_id):
    branch = scoped_branch(db, user, branch_id)
    require_settings_permission(db, user, branch.business_id, "branch")
    lock_staff_business(db, branch.business_id)
    db.refresh(branch)
    return branch


def replay(db, branch, scope, key, fingerprint):
    record = get_idempotent_response(db, scope, key, branch.business_id)
    if record:
        if record.get("fingerprint") != fingerprint:
            raise HTTPException(409, "La clave de reintento pertenece a otra operación")
        return record["result"]


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def commit(db, branch, user, scope, key, signature):
    branch.version += 1
    result = agent_response(branch)
    audit(db, user, scope, "branch", branch.id, branch.business_id,
          {"name": branch.agent_name, "yape_number": branch.yape_number,
           "payment_recipient_name": branch.payment_recipient_name,
           "image_ids": [i["id"] for i in menu_images(branch)], "yape_qr_configured": bool(branch.yape_qr_storage_path),
           "menu_pdf_configured": bool(branch.agent_menu_pdf)},
          branch_id=branch.id, actor_display_name=actor_display_name(db, user))
    save_idempotent_response(db, scope, key, branch.business_id, {"fingerprint": signature, "result": result})
    db.commit()
    return result


async def cleanup_unreferenced(db, path):
    if not path:
        return
    # Legacy uploads can share a stored object; never remove a referenced object.
    for branch in db.scalars(select(Branch)):
        if path in {branch.logo_storage_path, branch.cover_storage_path, branch.yape_qr_storage_path, branch.menu_card_storage_path} or any(i["path"] == path for i in menu_images(branch)) or (branch.agent_menu_pdf or {}).get("path") == path:
            return
    try:
        await delete_private_file(path)
    except (OSError, httpx.HTTPError):
        pass


@router.get("/settings/branches/{branch_id}/agent")
def read_agent(branch_id: int, user: AuthContext = Depends(get_current_user), db: Session = Depends(get_db)):
    branch = scoped_branch(db, user, branch_id)
    require_settings_permission(db, user, branch.business_id, "branch")
    return agent_response(branch)


@router.patch("/settings/branches/{branch_id}/agent")
def update_agent(branch_id: int, payload: AgentUpdate, idempotency_key: str | None = Header(None), user: AuthContext = Depends(get_current_user), db: Session = Depends(get_db)):
    branch = writable(db, user, branch_id)
    key, scope = _key(idempotency_key), f"settings.branch.{branch_id}.agent.updated"
    signature = fingerprint(payload.model_dump(exclude_unset=True))
    previous = replay(db, branch, scope, key, signature)
    if previous:
        return previous
    assert_version(branch.version, payload.expected_version)
    if "name" in payload.model_fields_set:
        branch.agent_name = payload.name
    for field in ("yape_number", "payment_recipient_name"):
        if field in payload.model_fields_set:
            setattr(branch, field, getattr(payload, field))
    if payload.image_order is not None:
        images = {i["id"]: i for i in menu_images(branch)}
        if len(payload.image_order) != len(images) or set(payload.image_order) != set(images):
            raise HTTPException(422, "El orden debe incluir cada imagen exactamente una vez")
        set_menu_images(branch, [images[i] for i in payload.image_order])
    return commit(db, branch, user, scope, key, signature)


def _commit_menu_pdf_upload(db, user, branch_id, expected_version, scope, key, signature, path, filename, size_bytes):
    # Keep the entire locked transaction on one worker. Legacy image uploads can
    # yield while holding this lock; waiting here must never block their event loop.
    try:
        branch = writable(db, user, branch_id)
        previous = replay(db, branch, scope, key, signature)
        if previous:
            db.rollback()
            return previous, None, False
        assert_version(branch.version, expected_version)
        old_path = (branch.agent_menu_pdf or {}).get("path")
        branch.agent_menu_pdf = {"path": path, "filename": filename, "size_bytes": size_bytes}
        return commit(db, branch, user, scope, key, signature), old_path, True
    except Exception:
        db.rollback()
        raise


def _commit_menu_pdf_delete(db, user, branch_id, expected_version, scope, key, signature):
    try:
        branch = writable(db, user, branch_id)
        previous = replay(db, branch, scope, key, signature)
        if previous:
            db.rollback()
            return previous, None
        assert_version(branch.version, expected_version)
        if not branch.agent_menu_pdf:
            raise HTTPException(404, "La carta PDF no está configurada")
        path = branch.agent_menu_pdf["path"]
        branch.agent_menu_pdf = None
        return commit(db, branch, user, scope, key, signature), path
    except Exception:
        db.rollback()
        raise


@router.post("/settings/branches/{branch_id}/agent/menu-pdf")
async def upload_agent_menu_pdf(branch_id: int, expected_version: int = Form(..., ge=1), file: UploadFile = File(...),
                                idempotency_key: str | None = Header(None),
                                user: AuthContext = Depends(get_current_user), db: Session = Depends(get_db)):
    branch = scoped_branch(db, user, branch_id)
    require_settings_permission(db, user, branch.business_id, "branch")
    data = await file.read(MENU_PDF_MAX_BYTES + 1)
    filename = menu_pdf_filename(file.filename)
    await run_in_threadpool(validate_menu_pdf, data, file.content_type)
    key, scope = _key(idempotency_key), f"settings.branch.{branch_id}.agent.menu_pdf.uploaded"
    signature = fingerprint([hashlib.sha256(data).hexdigest(), filename, expected_version])
    previous = replay(db, branch, scope, key, signature)
    if previous:
        return previous
    assert_version(branch.version, expected_version)
    path = await store_private_file(data, filename, "application/pdf", "agent-menu-pdf")
    try:
        result, old_path, used = await run_in_threadpool(
            _commit_menu_pdf_upload, db, user, branch_id, expected_version, scope, key, signature,
            path, filename, len(data),
        )
    except Exception:
        await cleanup_unreferenced(db, path)
        raise
    await cleanup_unreferenced(db, old_path if used else path)
    return result


@router.delete("/settings/branches/{branch_id}/agent/menu-pdf")
async def delete_agent_menu_pdf(branch_id: int, expected_version: int = Query(..., ge=1),
                                idempotency_key: str | None = Header(None),
                                user: AuthContext = Depends(get_current_user), db: Session = Depends(get_db)):
    key, scope = _key(idempotency_key), f"settings.branch.{branch_id}.agent.menu_pdf.deleted"
    signature = fingerprint([expected_version])
    result, path = await run_in_threadpool(
        _commit_menu_pdf_delete, db, user, branch_id, expected_version, scope, key, signature,
    )
    await cleanup_unreferenced(db, path)
    return result


@router.get("/settings/branches/{branch_id}/agent/menu-pdf")
async def read_agent_menu_pdf(branch_id: int, user: AuthContext = Depends(get_current_user), db: Session = Depends(get_db)):
    branch = scoped_branch(db, user, branch_id)
    require_settings_permission(db, user, branch.business_id, "branch")
    if not branch.agent_menu_pdf:
        raise HTTPException(404, "La carta PDF no está configurada")
    try:
        data, _ = await load_private_file(branch.agent_menu_pdf["path"])
    except (FileNotFoundError, httpx.HTTPError) as exc:
        raise HTTPException(404, "No se encontró la carta PDF") from exc
    filename = quote(menu_pdf_filename(branch.agent_menu_pdf["filename"]), safe="")
    return Response(data, media_type="application/pdf", headers={
        "Content-Disposition": f"attachment; filename=\"carta.pdf\"; filename*=UTF-8''{filename}",
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
    })


@router.post("/settings/branches/{branch_id}/agent/images")
@router.post("/settings/branches/{branch_id}/agent/images/{image_id}")
@router.post("/settings/branches/{branch_id}/agent/yape-qr")
async def upload_agent_image(request: Request, branch_id: int, expected_version: int = Form(...), file: UploadFile = File(...), image_id: str | None = None,
                             idempotency_key: str | None = Header(None),
                             user: AuthContext = Depends(get_current_user), db: Session = Depends(get_db)):
    kind = "yape" if request.url.path.endswith("/yape-qr") else "menu"
    branch = writable(db, user, branch_id)
    data = await file.read(BRANCH_MEDIA_MAX_BYTES + 1)
    content_type = _validate_branch_media(data, file.content_type)
    key, scope = _key(idempotency_key), f"settings.branch.{branch_id}.agent.{kind}.uploaded"
    signature = fingerprint([hashlib.sha256(data).hexdigest(), image_id, expected_version, kind])
    previous = replay(db, branch, scope, key, signature)
    if previous:
        return previous
    assert_version(branch.version, expected_version)
    images = menu_images(branch)
    current = next((i for i in images if i["id"] == image_id), None)
    if image_id and not current:
        raise HTTPException(404, "Imagen no encontrada")
    if kind == "menu" and not current and len(images) >= 10:
        raise HTTPException(422, "Puedes guardar hasta 10 imágenes")
    old_path = branch.yape_qr_storage_path if kind == "yape" else current["path"] if current else None
    path = await store_private_file(data, file.filename or "image.png", content_type, "agent-media")
    try:
        if kind == "yape":
            branch.yape_qr_storage_path = path
        else:
            if current:
                current["path"] = path
            else:
                images.append({"id": str(uuid4()), "path": path})
            set_menu_images(branch, images)
        result = commit(db, branch, user, scope, key, signature)
    except Exception:
        db.rollback()
        await cleanup_unreferenced(db, path)
        raise
    await cleanup_unreferenced(db, old_path)
    return result


@router.delete("/settings/branches/{branch_id}/agent/images/{image_id}")
@router.delete("/settings/branches/{branch_id}/agent/yape-qr")
async def delete_agent_image(branch_id: int, payload: MediaDelete, image_id: str = "yape-qr", idempotency_key: str | None = Header(None),
                             user: AuthContext = Depends(get_current_user), db: Session = Depends(get_db)):
    branch = writable(db, user, branch_id)
    key, scope = _key(idempotency_key), f"settings.branch.{branch_id}.agent.image.deleted"
    signature = fingerprint([image_id, payload.expected_version])
    previous = replay(db, branch, scope, key, signature)
    if previous:
        return previous
    assert_version(branch.version, payload.expected_version)
    if image_id == "yape-qr":
        path = branch.yape_qr_storage_path
        branch.yape_qr_storage_path = None
    else:
        images = menu_images(branch)
        current = next((i for i in images if i["id"] == image_id), None)
        if not current:
            raise HTTPException(404, "Imagen no encontrada")
        path = current["path"]
        set_menu_images(branch, [i for i in images if i["id"] != image_id])
    result = commit(db, branch, user, scope, key, signature)
    await cleanup_unreferenced(db, path)
    return result


async def image_response(path, transport_format=None):
    if not path:
        raise HTTPException(404, "Imagen no encontrada")
    try:
        data, media_type = await load_private_file(path)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Imagen no encontrada") from exc
    if transport_format == "whatsapp_cloud":
        from .agent_media import whatsapp_cloud_image
        data, media_type = whatsapp_cloud_image(data, media_type)
    return Response(data, media_type=media_type, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/settings/branches/{branch_id}/agent/images/{image_id}")
@router.get("/settings/branches/{branch_id}/agent/yape-qr")
async def read_agent_image(branch_id: int, image_id: str = "yape-qr", user: AuthContext = Depends(get_current_user), db: Session = Depends(get_db)):
    branch = scoped_branch(db, user, branch_id)
    require_settings_permission(db, user, branch.business_id, "branch")
    path = branch.yape_qr_storage_path if image_id == "yape-qr" else next((i["path"] for i in menu_images(branch) if i["id"] == image_id), None)
    return await image_response(path)


@router.get("/integrations/context/menu-cards/{image_id}")
async def integration_menu_image(image_id: str, format: str | None = Query(default=None, pattern="^whatsapp_cloud$"),
        auth=Depends(require_integration_scope("menu:read")), db: Session = Depends(get_db)):
    if auth.legacy or not auth.branch_id:
        raise HTTPException(403, "Se requiere una credencial de sucursal")
    branch = db.scalar(select(Branch).where(Branch.id == auth.branch_id, Branch.business_id == auth.business_id, Branch.active.is_(True)))
    if not branch:
        raise HTTPException(404, "Sucursal no encontrada")
    return await image_response(next((i["path"] for i in menu_images(branch) if i["id"] == image_id), None), format)
