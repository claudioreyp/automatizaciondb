from __future__ import annotations

import hashlib
import io
import json
import os
import secrets
from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated
from urllib.parse import quote
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Response, UploadFile
from PIL import Image, UnidentifiedImageError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import AuthContext, ensure_business_scope, get_current_user
from .config import get_settings
from .qz_signing import qz_connection_settings
from .qz_activation import activation_bundle
from .database import get_db
from .printing_settings import PRINTING_JSON_DEFAULTS, preserve_printing_extensions, printing_json
from .models import (
    AuditEvent,
    Branch,
    BranchSettings,
    Business,
    DeliveryBand,
    IdempotencyRecord,
    Invitation,
    KitchenTicket,
    Order,
    PairedDevice,
    PrintJob,
    PrinterDevice,
    ServiceSchedule,
    StaffMember,
    StaffMemberBranch,
    StaffMemberRole,
    utcnow,
)
from .schemas import (
    ArchiveRequest,
    DeliveryQuoteCreate,
    PairedDeviceCreate,
    PairedDeviceUpdate,
    PairDeviceComplete,
    PrintJobComplete,
    PrintJobCreate,
    PrinterDeviceCreate,
    PrinterDeviceUpdate,
    QZSignRequest,
    ScheduleAssignmentsReplace,
    ServiceScheduleCreate,
    ServiceScheduleUpdate,
    SettingsBranchProfileUpdate,
    SettingsBusinessUpdate,
    SettingsDeliveryUpdate,
    SettingsPaymentMethodsUpdate,
    SettingsPrintingUpdate,
    SettingsServicesUpdate,
    SettingsTimesUpdate,
    StaffMemberCreate,
    StaffMemberUpdate,
    StaffPinVerify,
)
from .services import (
    assert_version,
    audit,
    get_idempotent_response,
    save_idempotent_response,
)
from .security_audit import (
    AuditCategory, CATEGORY_ACTIONS, LIMA, audit_branch_filter, audit_list_entry, project_audit,
)
from .settings_service import (
    active_owner_count,
    actor_display_name,
    archive_branch,
    archive_staff_member,
    complete_device_pairing,
    create_delivery_quote,
    device_from_token,
    get_or_create_branch_settings,
    get_or_create_primary_schedule,
    hash_pin,
    issue_pairing_code,
    lock_staff_business,
    qz_certificate,
    qz_sign_payload,
    replace_schedule_assignments,
    replace_schedule_shifts,
    replace_staff_access,
    require_settings_permission,
    require_staff_management_change,
    scoped_branch,
    serialize_branch_origin,
    serialize_branch_profile,
    serialize_business,
    serialize_delivery_band,
    serialize_delivery_policy,
    serialize_delivery_quote,
    serialize_print_job,
    serialize_printer,
    serialize_schedule,
    serialize_staff_member,
    staff_management_capabilities,
    staff_member_capabilities,
    sync_legacy_branch_fields,
    verify_staff_pin,
)
from .storage import delete_private_file, load_private_file, store_private_file


router = APIRouter(prefix="/settings", tags=["settings"])
IdempotencyHeader = Annotated[str | None, Header(alias="Idempotency-Key")]
BRANCH_MEDIA_MAX_BYTES = 8 * 1024 * 1024
BRANCH_MEDIA_TYPES = {"image/jpeg", "image/png", "image/webp"}


def _business_id(user: AuthContext, requested: int | None) -> int:
    if user.is_superadmin:
        if requested is None:
            raise HTTPException(status_code=422, detail="business_id is required for superadmin")
        return requested
    if user.business_id is None:
        raise HTTPException(status_code=403, detail="Business scope is required")
    if requested is not None:
        ensure_business_scope(user, requested)
    return user.business_id


def _key(value: str | None) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise HTTPException(status_code=422, detail="Idempotency-Key header is required")
    if len(normalized) > 240:
        raise HTTPException(status_code=422, detail="Idempotency-Key is too long")
    return normalized


def _actor(db: Session, user: AuthContext) -> str:
    return actor_display_name(db, user)


def _save(
    db: Session,
    *,
    scope: str,
    key: str,
    business_id: int,
    response: dict,
) -> dict:
    save_idempotent_response(db, scope, key, business_id, response)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        existing = get_idempotent_response(db, scope, key, business_id)
        if existing is not None:
            return existing
        raise HTTPException(status_code=409, detail="Settings change conflicts with existing data") from exc
    return response


def _validate_branch_media(data: bytes, content_type: str | None) -> str:
    if content_type not in BRANCH_MEDIA_TYPES:
        raise HTTPException(status_code=415, detail="Usa una imagen JPEG, PNG o WebP")
    if not data:
        raise HTTPException(status_code=422, detail="La imagen está vacía")
    if len(data) > BRANCH_MEDIA_MAX_BYTES:
        raise HTTPException(status_code=413, detail="La imagen no puede superar 8 MB")
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="El archivo no contiene una imagen válida") from exc
    return content_type


def _branch_settings(db: Session, user: AuthContext, branch_id: int, permission: str):
    branch = scoped_branch(db, user, branch_id)
    require_settings_permission(db, user, branch.business_id, permission)
    settings = get_or_create_branch_settings(db, branch)
    return branch, settings


def _services_response(settings: BranchSettings) -> dict:
    return {
        "branch_id": settings.branch_id,
        "pos_tables": settings.pos_tables,
        "pos_counter": settings.pos_counter,
        "pos_takeaway": settings.pos_takeaway,
        "pos_delivery": settings.pos_delivery,
        "digital_tables": settings.digital_tables,
        "digital_takeaway": settings.digital_takeaway,
        "digital_delivery": settings.digital_delivery,
        "version": settings.version,
    }


def _delivery_response(db: Session, settings: BranchSettings) -> dict:
    branch = db.get(Branch, settings.branch_id)
    policy = serialize_delivery_policy(settings)
    branch_origin = serialize_branch_origin(branch) if branch else None
    bands = list(
        db.scalars(
            select(DeliveryBand)
            .where(
                DeliveryBand.branch_id == settings.branch_id,
                DeliveryBand.active.is_(True),
                DeliveryBand.archived_at.is_(None),
            )
            .order_by(DeliveryBand.sort_order, DeliveryBand.minimum_km, DeliveryBand.id)
        )
    )
    return {
        "branch_id": settings.branch_id,
        "delivery_mode": settings.delivery_mode,
        "delivery_policy": policy,
        "delivery_policy_supported": True,
        "pos_quotes_supported": True,
        "branch_origin": branch_origin,
        "fixed_delivery_fee": float(settings.fixed_delivery_fee or 0),
        "distance_base_fee": float(settings.distance_base_fee or 0),
        "distance_fee_per_km": float(settings.distance_fee_per_km or 0),
        "distance_max_km": (
            float(settings.distance_max_km) if settings.distance_max_km is not None else None
        ),
        "free_delivery_threshold": (
            float(settings.free_delivery_threshold)
            if settings.free_delivery_threshold is not None
            else None
        ),
        "minimum_order_amount": (
            float(settings.minimum_order_amount)
            if settings.minimum_order_amount is not None
            else None
        ),
        "bands": [serialize_delivery_band(item) for item in bands],
        "google_routes_configured": bool(
            os.getenv("GOOGLE_MAPS_API_KEY")
            and (policy.get("origin") or branch_origin)
        ),
        "version": settings.version,
    }


def _payment_methods_response(settings: BranchSettings) -> dict:
    return {
        "branch_id": settings.branch_id,
        "payment_methods": settings.payment_methods or {},
        "version": settings.version,
    }


def _times_response(settings: BranchSettings) -> dict:
    return {
        "branch_id": settings.branch_id,
        "delivery_min_minutes": settings.delivery_min_minutes,
        "delivery_max_minutes": settings.delivery_max_minutes,
        "pickup_minutes": settings.pickup_minutes,
        "version": settings.version,
    }


def _printing_response(settings: BranchSettings) -> dict:
    return {
        "branch_id": settings.branch_id,
        "advanced_printing": settings.advanced_printing,
        **{field: printing_json(field, getattr(settings, field)) for field in PRINTING_JSON_DEFAULTS},
        "version": settings.version,
    }


@router.get("/business")
def get_business_settings(
    business_id: int | None = None,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    resolved = _business_id(user, business_id)
    require_settings_permission(db, user, resolved, "business")
    business = db.get(Business, resolved)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")
    return serialize_business(business)


@router.patch("/business")
def update_business_settings(
    payload: SettingsBusinessUpdate,
    idempotency_key: IdempotencyHeader = None,
    business_id: int | None = None,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    resolved = _business_id(user, business_id)
    require_settings_permission(db, user, resolved, "business")
    key = _key(idempotency_key)
    scope = "settings.business.update"
    if existing := get_idempotent_response(db, scope, key, resolved):
        return existing
    business = db.scalar(select(Business).where(Business.id == resolved).with_for_update())
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")
    assert_version(business.version, payload.expected_version)
    values = payload.model_dump(exclude={"expected_version"}, exclude_none=True)
    for field, value in values.items():
        setattr(business, field, value)
    business.version += 1
    response = serialize_business(business)
    audit(
        db,
        user,
        "settings.business.updated",
        "business",
        business.id,
        business.id,
        values,
        actor_display_name=_actor(db, user),
    )
    return _save(db, scope=scope, key=key, business_id=resolved, response=response)


@router.get("/branches/{branch_id}/profile")
def get_branch_profile(
    branch_id: int,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch = scoped_branch(db, user, branch_id)
    require_settings_permission(db, user, branch.business_id, "branch")
    return serialize_branch_profile(branch)


@router.patch("/branches/{branch_id}/profile")
def update_branch_profile(
    branch_id: int,
    payload: SettingsBranchProfileUpdate,
    idempotency_key: IdempotencyHeader = None,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch = scoped_branch(db, user, branch_id)
    require_settings_permission(db, user, branch.business_id, "branch")
    key = _key(idempotency_key)
    scope = f"settings.branch.{branch.id}.profile.update"
    if existing := get_idempotent_response(db, scope, key, branch.business_id):
        return existing
    # Match the settings -> branch lock order used by strict route quotes.
    settings = db.scalar(select(BranchSettings).where(
        BranchSettings.branch_id == branch.id,
        BranchSettings.business_id == branch.business_id,
    ).with_for_update().execution_options(populate_existing=True))
    branch = db.scalar(select(Branch).where(Branch.id == branch.id).with_for_update()
                       .execution_options(populate_existing=True))
    assert_version(branch.version, payload.expected_version)
    previous_origin = serialize_branch_origin(branch)
    values = payload.model_dump(exclude={"expected_version"}, exclude_none=True)
    for field, value in values.items():
        setattr(branch, field, value)
    current_origin = serialize_branch_origin(branch)
    audit_values = dict(values)
    if (
        settings is not None
        and settings.delivery_mode in {"bands", "distance"}
        and serialize_delivery_policy(settings).get("origin") is None
        and previous_origin != current_origin
    ):
        audit_values["delivery_configuration"] = {
            "before_version": settings.version,
            "after_version": settings.version + 1,
            "before_origin": previous_origin,
            "after_origin": current_origin,
        }
        settings.version += 1
    branch.version += 1
    response = serialize_branch_profile(branch)
    audit(
        db,
        user,
        "settings.branch.profile.updated",
        "branch",
        branch.id,
        branch.business_id,
        audit_values,
        branch_id=branch.id,
        actor_display_name=_actor(db, user),
    )
    return _save(db, scope=scope, key=key, business_id=branch.business_id, response=response)


@router.post("/branches/{branch_id}/profile")
async def upload_branch_media(
    branch_id: int,
    media_kind: Annotated[str, Form()],
    expected_version: Annotated[int, Form()],
    file: UploadFile = File(...),
    idempotency_key: IdempotencyHeader = None,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if media_kind not in {"logo", "cover"}:
        raise HTTPException(status_code=422, detail="media_kind must be logo or cover")
    branch = scoped_branch(db, user, branch_id)
    require_settings_permission(db, user, branch.business_id, "branch")
    key = _key(idempotency_key)
    scope = f"settings.branch.{branch.id}.{media_kind}.upload"
    if existing := get_idempotent_response(db, scope, key, branch.business_id):
        return existing
    branch = db.scalar(select(Branch).where(Branch.id == branch.id).with_for_update())
    assert_version(branch.version, expected_version)
    data = await file.read(BRANCH_MEDIA_MAX_BYTES + 1)
    content_type = _validate_branch_media(data, file.content_type)
    new_path = await store_private_file(
        data,
        file.filename or f"branch-{branch.id}-{media_kind}.png",
        content_type,
    )
    field = "logo_storage_path" if media_kind == "logo" else "cover_storage_path"
    old_path = getattr(branch, field)
    setattr(branch, field, new_path)
    branch.version += 1
    response = serialize_branch_profile(branch)
    audit(
        db,
        user,
        f"settings.branch.{media_kind}.updated",
        "branch",
        branch.id,
        branch.business_id,
        {"media_kind": media_kind},
        branch_id=branch.id,
        actor_display_name=_actor(db, user),
    )
    try:
        response = _save(
            db,
            scope=scope,
            key=key,
            business_id=branch.business_id,
            response=response,
        )
    except Exception:
        await delete_private_file(new_path)
        raise
    if old_path and old_path != new_path:
        try:
            await delete_private_file(old_path)
        except (FileNotFoundError, httpx.HTTPError):
            pass
    return response


@router.delete("/branches/{branch_id}/media/{media_kind}")
async def delete_branch_media(
    branch_id: int,
    media_kind: str,
    payload: ArchiveRequest,
    idempotency_key: IdempotencyHeader = None,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if media_kind not in {"logo", "cover"}:
        raise HTTPException(status_code=422, detail="media_kind must be logo or cover")
    branch = scoped_branch(db, user, branch_id)
    require_settings_permission(db, user, branch.business_id, "branch")
    key = _key(idempotency_key)
    scope = f"settings.branch.{branch.id}.{media_kind}.delete"
    if existing := get_idempotent_response(db, scope, key, branch.business_id):
        return existing
    branch = db.scalar(select(Branch).where(Branch.id == branch.id).with_for_update()
                       .execution_options(populate_existing=True))
    assert_version(branch.version, payload.expected_version)
    field = "logo_storage_path" if media_kind == "logo" else "cover_storage_path"
    old_path = getattr(branch, field)
    before = serialize_branch_profile(branch)
    setattr(branch, field, None)
    branch.version += 1
    response = serialize_branch_profile(branch)
    audit(
        db, user, f"settings.branch.{media_kind}.deleted", "branch", branch.id,
        branch.business_id, {"media_kind": media_kind, "before": before, "after": response},
        branch_id=branch.id, actor_display_name=_actor(db, user),
    )
    response = _save(db, scope=scope, key=key, business_id=branch.business_id, response=response)
    # A failed transaction must never remove an image still referenced by the branch.
    if old_path and not db.scalar(select(Branch.id).where(
        (Branch.logo_storage_path == old_path) | (Branch.cover_storage_path == old_path)
    ).limit(1)):
        try:
            await delete_private_file(old_path)
        except (OSError, httpx.HTTPError):
            pass
    return response


@router.get("/branches/{branch_id}/media/{media_kind}")
async def get_branch_media(
    branch_id: int,
    media_kind: str,
    db: Session = Depends(get_db),
):
    if media_kind not in {"logo", "cover"}:
        raise HTTPException(status_code=404, detail="Media not found")
    branch = db.scalar(
        select(Branch).where(
            Branch.id == branch_id,
            Branch.active.is_(True),
            Branch.archived_at.is_(None),
        )
    )
    storage_path = (
        branch.logo_storage_path if branch and media_kind == "logo" else
        branch.cover_storage_path if branch else None
    )
    if not storage_path:
        raise HTTPException(status_code=404, detail="Media not found")
    try:
        data, content_type = await load_private_file(storage_path)
    except (FileNotFoundError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=404, detail="Media not found") from exc
    return Response(
        content=data,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.delete("/branches/{branch_id}/profile")
def archive_branch_profile(
    branch_id: int,
    payload: ArchiveRequest,
    idempotency_key: IdempotencyHeader = None,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch = scoped_branch(db, user, branch_id)
    require_settings_permission(db, user, branch.business_id, "branch")
    key = _key(idempotency_key)
    scope = f"settings.branch.{branch.id}.archive"
    if existing := get_idempotent_response(db, scope, key, branch.business_id):
        return existing
    branch = db.scalar(select(Branch).where(Branch.id == branch.id).with_for_update())
    assert_version(branch.version, payload.expected_version)
    archive_branch(db, branch)
    response = serialize_branch_profile(branch)
    audit(
        db,
        user,
        "settings.branch.archived",
        "branch",
        branch.id,
        branch.business_id,
        branch_id=branch.id,
        actor_display_name=_actor(db, user),
    )
    return _save(db, scope=scope, key=key, business_id=branch.business_id, response=response)


@router.get("/places/autocomplete")
async def autocomplete_places(
    input: str = Query(min_length=3, max_length=240),
    session_token: str = Query(min_length=8, max_length=120),
    branch_id: int = Query(gt=0),
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch = scoped_branch(db, user, branch_id)
    require_settings_permission(db, user, branch.business_id, "branch")
    api_key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not api_key:
        raise HTTPException(status_code=503, detail="Google Maps is not configured")
    headers = {
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "suggestions.placePrediction.placeId,suggestions.placePrediction.text.text",
    }
    body = {
        "input": input,
        "includedRegionCodes": ["pe"],
        "languageCode": "es",
        "regionCode": "PE",
        "sessionToken": session_token,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            google_response = await client.post(
                "https://places.googleapis.com/v1/places:autocomplete",
                headers=headers,
                json=body,
            )
            google_response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Google Places is temporarily unavailable") from exc
    suggestions = google_response.json().get("suggestions", [])
    return {
        "items": [
            {
                "place_id": prediction.get("placeId"),
                "text": (prediction.get("text") or {}).get("text"),
            }
            for item in suggestions
            if (prediction := item.get("placePrediction")) and prediction.get("placeId")
        ],
        "configured": True,
    }


@router.get("/places/{place_id}")
async def get_place_details(
    place_id: str,
    session_token: str = Query(min_length=8, max_length=120),
    branch_id: int = Query(gt=0),
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch = scoped_branch(db, user, branch_id)
    require_settings_permission(db, user, branch.business_id, "branch")
    api_key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not api_key:
        raise HTTPException(status_code=503, detail="Google Maps is not configured")
    headers = {
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "id,formattedAddress,location,googleMapsUri",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            google_response = await client.get(
                f"https://places.googleapis.com/v1/places/{quote(place_id, safe='')}",
                headers=headers,
                params={"languageCode": "es", "regionCode": "PE", "sessionToken": session_token},
            )
            google_response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Google Places is temporarily unavailable") from exc
    place = google_response.json()
    location = place.get("location") or {}
    return {
        "place_id": place.get("id") or place_id,
        "address": place.get("formattedAddress") or "",
        "latitude": location.get("latitude"),
        "longitude": location.get("longitude"),
        "maps_url": place.get("googleMapsUri") or "",
    }


@router.get("/branches/{branch_id}/services")
def get_services_settings(
    branch_id: int,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _, settings = _branch_settings(db, user, branch_id, "operations")
    return _services_response(settings)


@router.patch("/branches/{branch_id}/services")
def update_services_settings(
    branch_id: int,
    payload: SettingsServicesUpdate,
    idempotency_key: IdempotencyHeader = None,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch, settings = _branch_settings(db, user, branch_id, "operations")
    key = _key(idempotency_key)
    scope = f"settings.branch.{branch.id}.services.update"
    if existing := get_idempotent_response(db, scope, key, branch.business_id):
        return existing
    settings = db.scalar(
        select(BranchSettings).where(BranchSettings.id == settings.id).with_for_update()
    )
    assert_version(settings.version, payload.expected_version)
    values = payload.model_dump(exclude={"expected_version"})
    for field, value in values.items():
        setattr(settings, field, value)
    settings.version += 1
    sync_legacy_branch_fields(branch, settings)
    branch.version += 1
    response = _services_response(settings)
    audit(
        db,
        user,
        "settings.services.updated",
        "branch_settings",
        settings.id,
        branch.business_id,
        values,
        branch_id=branch.id,
        actor_display_name=_actor(db, user),
    )
    return _save(db, scope=scope, key=key, business_id=branch.business_id, response=response)


@router.get("/branches/{branch_id}/delivery")
def get_delivery_settings(
    branch_id: int,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _, settings = _branch_settings(db, user, branch_id, "delivery_quotes")
    return _delivery_response(db, settings)


@router.patch("/branches/{branch_id}/delivery")
def update_delivery_settings(
    branch_id: int,
    payload: SettingsDeliveryUpdate,
    idempotency_key: IdempotencyHeader = None,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch, settings = _branch_settings(db, user, branch_id, "operations")
    key = _key(idempotency_key)
    scope = f"settings.branch.{branch.id}.delivery.update"
    if existing := get_idempotent_response(db, scope, key, branch.business_id):
        return existing
    settings = db.scalar(
        select(BranchSettings).where(BranchSettings.id == settings.id).with_for_update()
        .execution_options(populate_existing=True)
    )
    assert_version(settings.version, payload.expected_version)
    before = _delivery_response(db, settings)
    values = payload.model_dump(exclude={"expected_version", "bands", "delivery_policy"})
    if "delivery_policy" in payload.model_fields_set:
        values["delivery_policy"] = (
            payload.delivery_policy.model_dump(mode="json") if payload.delivery_policy is not None else None
        )
    for field, value in values.items():
        setattr(settings, field, value)
    if payload.bands is not None:
        ordered = sorted(payload.bands, key=lambda item: (item.minimum_km, item.maximum_km))
        for previous, current in zip(ordered, ordered[1:]):
            if current.minimum_km < previous.maximum_km:
                raise HTTPException(status_code=409, detail="Delivery distance bands cannot overlap")
        if len({item.sort_order for item in ordered}) != len(ordered):
            raise HTTPException(status_code=422, detail="Delivery band sort_order values must be unique")
        existing_bands = list(
            db.scalars(
                select(DeliveryBand)
                .where(DeliveryBand.branch_id == branch.id)
                .with_for_update()
            )
        )
        by_sort_order = {item.sort_order: item for item in existing_bands}
        requested_orders = {item.sort_order for item in ordered}
        for item in existing_bands:
            if item.sort_order not in requested_orders and item.archived_at is None:
                item.active = False
                item.archived_at = utcnow()
                item.version += 1
        for item in ordered:
            band = by_sort_order.get(item.sort_order)
            if band is None:
                band = DeliveryBand(
                    business_id=branch.business_id,
                    branch_id=branch.id,
                    sort_order=item.sort_order,
                )
                db.add(band)
            else:
                band.version += 1
            band.minimum_km = item.minimum_km
            band.maximum_km = item.maximum_km
            band.fee = item.fee
            band.active = True
            band.archived_at = None
    settings.version += 1
    sync_legacy_branch_fields(branch, settings)
    branch.version += 1
    db.flush()
    response = _delivery_response(db, settings)
    audit(
        db,
        user,
        "settings.delivery.updated",
        "branch_settings",
        settings.id,
        branch.business_id,
        {**values, "bands": response["bands"], "before": before, "after": response},
        branch_id=branch.id,
        actor_display_name=_actor(db, user),
    )
    return _save(db, scope=scope, key=key, business_id=branch.business_id, response=response)


@router.post("/branches/{branch_id}/delivery/quotes", status_code=201)
def quote_delivery(
    branch_id: int,
    payload: DeliveryQuoteCreate,
    idempotency_key: IdempotencyHeader = None,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch, settings = _branch_settings(db, user, branch_id, "delivery_quotes")
    key = _key(idempotency_key)
    scope = f"settings.branch.{branch.id}.delivery.quote"
    if existing := get_idempotent_response(db, scope, key, branch.business_id):
        return existing
    quote = create_delivery_quote(
        db,
        branch,
        settings,
        subtotal=payload.subtotal,
        destination=payload.destination,
        supplied_distance_km=payload.distance_km,
        expected_configuration_version=payload.expected_configuration_version,
        confirmed_fee=payload.confirmed_fee,
        confirmed_by=user,
    )
    response = serialize_delivery_quote(quote)
    audit(
        db, user, "settings.delivery.quoted", "delivery_quote", quote.id, branch.business_id,
        {"after": response, "input": quote.input_snapshot},
        branch_id=branch.id, actor_display_name=_actor(db, user),
    )
    return _save(db, scope=scope, key=key, business_id=branch.business_id, response=response)


@router.get("/branches/{branch_id}/payment-methods")
def get_payment_methods_settings(
    branch_id: int,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _, settings = _branch_settings(db, user, branch_id, "operations")
    return _payment_methods_response(settings)


@router.patch("/branches/{branch_id}/payment-methods")
def update_payment_methods_settings(
    branch_id: int,
    payload: SettingsPaymentMethodsUpdate,
    idempotency_key: IdempotencyHeader = None,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch, settings = _branch_settings(db, user, branch_id, "operations")
    key = _key(idempotency_key)
    scope = f"settings.branch.{branch.id}.payment_methods.update"
    if existing := get_idempotent_response(db, scope, key, branch.business_id):
        return existing
    settings = db.scalar(
        select(BranchSettings).where(BranchSettings.id == settings.id).with_for_update()
    )
    assert_version(settings.version, payload.expected_version)
    payment_methods = {key: list(value) for key, value in payload.payment_methods.items()}
    payment_methods.setdefault("counter", list(payment_methods.get("takeaway", [])))
    settings.payment_methods = payment_methods
    settings.version += 1
    sync_legacy_branch_fields(branch, settings)
    branch.version += 1
    response = _payment_methods_response(settings)
    audit(
        db,
        user,
        "settings.payment_methods.updated",
        "branch_settings",
        settings.id,
        branch.business_id,
        {"payment_methods": payment_methods},
        branch_id=branch.id,
        actor_display_name=_actor(db, user),
    )
    return _save(db, scope=scope, key=key, business_id=branch.business_id, response=response)


@router.get("/branches/{branch_id}/times")
def get_times_settings(
    branch_id: int,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _, settings = _branch_settings(db, user, branch_id, "operations")
    return _times_response(settings)


@router.patch("/branches/{branch_id}/times")
def update_times_settings(
    branch_id: int,
    payload: SettingsTimesUpdate,
    idempotency_key: IdempotencyHeader = None,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch, settings = _branch_settings(db, user, branch_id, "operations")
    key = _key(idempotency_key)
    scope = f"settings.branch.{branch.id}.times.update"
    if existing := get_idempotent_response(db, scope, key, branch.business_id):
        return existing
    settings = db.scalar(
        select(BranchSettings).where(BranchSettings.id == settings.id).with_for_update()
    )
    assert_version(settings.version, payload.expected_version)
    values = payload.model_dump(exclude={"expected_version"})
    for field, value in values.items():
        setattr(settings, field, value)
    settings.version += 1
    response = _times_response(settings)
    audit(
        db,
        user,
        "settings.times.updated",
        "branch_settings",
        settings.id,
        branch.business_id,
        values,
        branch_id=branch.id,
        actor_display_name=_actor(db, user),
    )
    return _save(db, scope=scope, key=key, business_id=branch.business_id, response=response)


@router.get("/branches/{branch_id}/printing")
def get_printing_settings(
    branch_id: int,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _, settings = _branch_settings(db, user, branch_id, "printing")
    return _printing_response(settings)


@router.patch("/branches/{branch_id}/printing")
def update_printing_settings(
    branch_id: int,
    payload: SettingsPrintingUpdate,
    idempotency_key: IdempotencyHeader = None,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch, settings = _branch_settings(db, user, branch_id, "printing")
    key = _key(idempotency_key)
    scope = f"settings.branch.{branch.id}.printing.update"
    if existing := get_idempotent_response(db, scope, key, branch.business_id):
        return existing
    settings = db.scalar(
        select(BranchSettings).where(BranchSettings.id == settings.id).with_for_update()
    )
    assert_version(settings.version, payload.expected_version)
    values = payload.model_dump(exclude={"expected_version"})
    for field in PRINTING_JSON_DEFAULTS:
        values[field] = preserve_printing_extensions(field, values[field], getattr(settings, field))
    for field, value in values.items():
        setattr(settings, field, value)
    settings.version += 1
    response = _printing_response(settings)
    audit(
        db,
        user,
        "settings.printing.updated",
        "branch_settings",
        settings.id,
        branch.business_id,
        {"advanced_printing": settings.advanced_printing},
        branch_id=branch.id,
        actor_display_name=_actor(db, user),
    )
    return _save(db, scope=scope, key=key, business_id=branch.business_id, response=response)


@router.get("/branches/{branch_id}/whatsapp")
def get_whatsapp_settings(
    branch_id: int,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch = scoped_branch(db, user, branch_id)
    require_settings_permission(db, user, branch.business_id, "branch")
    return {
        "branch_id": branch.id,
        "number": branch.whatsapp_number,
        "status": branch.whatsapp_status,
        "read_only": True,
        "version": branch.version,
    }


@router.get("/branches/{branch_id}/printing/qz")
def get_qz_connection_settings(
    branch_id: int,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch = scoped_branch(db, user, branch_id)
    require_settings_permission(db, user, branch.business_id, "printing_runtime")
    return qz_connection_settings()


@router.get("/branches/{branch_id}/printing/qz/activation")
def download_qz_activation(
    branch_id: int,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch = scoped_branch(db, user, branch_id)
    require_settings_permission(db, user, branch.business_id, "printing_runtime")
    return Response(content=activation_bundle(), media_type="application/zip", headers={
        "Content-Disposition": 'attachment; filename="Escalar-AI-POS-activar-impresion.zip"',
        "Cache-Control": "no-store",
    })


@router.get("/printing/qz/certificate")
def get_qz_certificate(
    business_id: int | None = None,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    resolved = _business_id(user, business_id)
    require_settings_permission(db, user, resolved, "printing")
    return {"certificate": qz_certificate()}


@router.post("/printing/qz/sign")
def sign_qz_payload(
    payload: QZSignRequest,
    business_id: int | None = None,
    branch_id: int | None = None,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    resolved = _business_id(user, business_id)
    if payload.request is not None:
        # QZ 2.2.6 signs SHA-256(JSON). Bind authorization to that exact JSON,
        # including whitespace and timestamp, without changing the signed bytes.
        try:
            digest = hashlib.sha256(payload.request.encode("utf-8")).hexdigest()
            matches = secrets.compare_digest(digest, payload.payload)
        except (UnicodeError, TypeError):
            matches = False
        if not matches:
            raise HTTPException(status_code=422, detail="QZ request does not match its SHA-256 payload")
    if branch_id is not None:
        branch = scoped_branch(db, user, branch_id)
        if branch.business_id != resolved:
            raise HTTPException(status_code=403, detail="Branch is outside business scope")
        roles = require_settings_permission(db, user, resolved, "printing_runtime")
        if not roles.intersection({"superadmin", "owner", "manager"}):
            # Operational signing is not authority to access files, USB or sockets.
            try:
                request = json.loads(payload.request if payload.request is not None else payload.payload)
            except (ValueError, TypeError):
                raise HTTPException(status_code=422, detail="Invalid QZ printing request")
            call = request.get("call") if isinstance(request, dict) else None
            if not isinstance(call, str) or call not in {"printers.find", "printers.getDefault", "printers.detail", "print"}:
                raise HTTPException(status_code=403, detail="QZ operation is not allowed")
            if call == "print":
                params = request.get("params") or {}
                printer = params.get("printer") if isinstance(params, dict) else None
                if (not isinstance(printer, dict) or set(printer) != {"name"}
                        or not isinstance(printer.get("name"), str) or not printer["name"].strip()):
                    # QZ gives host/file precedence even when a name is present.
                    raise HTTPException(status_code=403, detail="Only named installed printers are allowed")
                data = params.get("data") if isinstance(params, dict) else None
                if not isinstance(data, list) or not data or not all(
                    isinstance(item, dict) and (
                        (item.get("format") == "html" and item.get("flavor") == "plain"
                         and isinstance(item.get("data"), str) and (
                             item.get("type") == "pixel" or (
                                 item.get("type") == "raw" and isinstance(item.get("options"), dict)
                                 and item["options"].get("language") == "ESCPOS"
                             )
                         )) or (
                             item.get("type") == "raw" and item.get("format") == "command"
                             and item.get("flavor") == "hex"
                             and item.get("data") in ("1B40", "0A0A0A1D5601", "0A1D564100")
                         )
                    ) for item in data
                ):
                    raise HTTPException(status_code=403, detail="Only thermal HTML and exact ESC/POS initialization/feed/cut are allowed")
    else:
        require_settings_permission(db, user, resolved, "printing")
    return {"signature": qz_sign_payload(payload.payload)}


def _staff_member(
    db: Session, user: AuthContext, member_id: int, *, for_update: bool = False,
) -> StaffMember:
    member = db.scalar(select(StaffMember).where(StaffMember.id == member_id))
    if not member:
        raise HTTPException(status_code=404, detail="Staff member not found")
    ensure_business_scope(user, member.business_id)
    if for_update:
        lock_staff_business(db, member.business_id)
        member = db.scalar(
            select(StaffMember).where(StaffMember.id == member_id)
            .with_for_update().execution_options(populate_existing=True)
        )
    require_settings_permission(db, user, member.business_id, "members")
    return member


def _device_response(device: PairedDevice) -> dict:
    return {
        "id": device.id,
        "business_id": device.business_id,
        "branch_id": device.branch_id,
        "name": device.name,
        "paired": bool(device.paired_at and device.token_hash),
        "paired_at": device.paired_at,
        "last_used_at": device.last_used_at,
        "active": device.active,
        "archived_at": device.archived_at,
        "version": device.version,
    }


async def _deliver_staff_invitation(invitation: Invitation, raw_token: str) -> dict:
    settings = get_settings()
    redirect_url = f"{settings.invite_redirect_url}?token={raw_token}"
    delivery_status = "not_configured"
    if settings.supabase_url and settings.supabase_service_role_key:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    f"{settings.supabase_url.rstrip('/')}/auth/v1/invite",
                    headers={
                        "apikey": settings.supabase_service_role_key,
                        "Authorization": f"Bearer {settings.supabase_service_role_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "email": invitation.email,
                        "redirect_to": redirect_url,
                        "data": {
                            "impulsa_invitation_id": invitation.id,
                            "business_id": invitation.business_id,
                        },
                    },
                )
                response.raise_for_status()
                delivery_status = "sent"
        except httpx.HTTPError:
            delivery_status = "failed"
    result = {"id": invitation.id, "status": invitation.status, "delivery_status": delivery_status}
    if settings.is_development:
        result["development_accept_url"] = redirect_url
    return result


def _create_staff_invitation(
    db: Session,
    user: AuthContext,
    member: StaffMember,
    role: str,
    branch_ids: list[int],
) -> tuple[Invitation, str]:
    for pending in db.scalars(
        select(Invitation).where(
            Invitation.business_id == member.business_id,
            Invitation.email == member.email,
            Invitation.status == "pending",
        )
    ):
        pending.status = "superseded"
    raw_token = secrets.token_urlsafe(36)
    invitation = Invitation(
        business_id=member.business_id,
        branch_id=branch_ids[0] if len(branch_ids) == 1 else None,
        email=member.email,
        role=role,
        token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
        expires_at=utcnow() + timedelta(days=7),
        created_by=user.user_id,
    )
    db.add(invitation)
    db.flush()
    return invitation, raw_token


@router.get("/members")
def list_staff_members(
    business_id: int | None = None,
    branch_id: int | None = None,
    include_archived: bool = False,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    resolved = _business_id(user, business_id)
    capabilities = staff_management_capabilities(db, user, resolved)
    statement = select(StaffMember).where(StaffMember.business_id == resolved)
    if branch_id is not None:
        branch = scoped_branch(db, user, branch_id)
        if branch.business_id != resolved:
            raise HTTPException(status_code=404, detail="Branch not found")
        statement = statement.join(
            StaffMemberBranch,
            StaffMemberBranch.staff_member_id == StaffMember.id,
        ).where(StaffMemberBranch.branch_id == branch_id)
    if not include_archived:
        statement = statement.where(StaffMember.active.is_(True), StaffMember.archived_at.is_(None))
    members = list(db.scalars(statement.order_by(StaffMember.first_name, StaffMember.id)))
    owner_count = active_owner_count(db, resolved)
    items = []
    for item in members:
        serialized = serialize_staff_member(db, item)
        serialized["is_current_user"] = item.id == user.staff_member_id or bool(item.auth_user_id and item.auth_user_id == user.user_id)
        serialized["capabilities"] = staff_member_capabilities(
            item, user, serialized["roles"],
            can_manage_admins=capabilities["can_manage_admins"], owner_count=owner_count,
        )
        items.append(serialized)
    return {"items": items, "total": len(items), "capabilities": capabilities}


@router.post("/members", status_code=201)
async def create_staff_member(
    payload: StaffMemberCreate,
    idempotency_key: IdempotencyHeader = None,
    business_id: int | None = None,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    resolved = _business_id(user, business_id)
    key = _key(idempotency_key)
    lock_staff_business(db, resolved)
    require_staff_management_change(db, user, resolved, roles=payload.roles)
    scope = "settings.members.create"
    if existing := get_idempotent_response(db, scope, key, resolved):
        return existing
    normalized_email = payload.email.strip().lower() if payload.email else None
    if normalized_email and db.scalar(
        select(StaffMember.id).where(
            StaffMember.business_id == resolved,
            StaffMember.email == normalized_email,
        )
    ):
        raise HTTPException(status_code=409, detail="A staff member already uses this email")
    member = StaffMember(
        business_id=resolved,
        email=normalized_email,
        first_name=payload.first_name.strip(),
        last_name=payload.last_name.strip(),
        pin_hash=hash_pin(payload.pin.get_secret_value()) if payload.pin else None,
        email_access=payload.email_access,
    )
    db.add(member)
    db.flush()
    replace_staff_access(db, member, roles=payload.roles, branch_ids=payload.branch_ids)
    invitation_result = None
    invitation_data = None
    if payload.email_access and member.email:
        invitation, raw_token = _create_staff_invitation(
            db, user, member, payload.roles[0], payload.branch_ids
        )
        invitation_data = (invitation, raw_token)
    db.flush()
    response = serialize_staff_member(db, member)
    audit(
        db,
        user,
        "settings.member.created",
        "staff_member",
        member.id,
        resolved,
        {"roles": payload.roles, "branch_ids": payload.branch_ids},
        actor_display_name=_actor(db, user),
    )
    response = _save(db, scope=scope, key=key, business_id=resolved, response=response)
    if invitation_data:
        invitation_result = await _deliver_staff_invitation(*invitation_data)
        response = {**response, "invitation": invitation_result}
    return response


@router.get("/members/{member_id}")
def get_staff_member(
    member_id: int,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return serialize_staff_member(db, _staff_member(db, user, member_id))


@router.patch("/members/{member_id}")
def update_staff_member(
    member_id: int,
    payload: StaffMemberUpdate,
    idempotency_key: IdempotencyHeader = None,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    member = _staff_member(db, user, member_id, for_update=True)
    require_staff_management_change(
        db, user, member.business_id, member=member,
        roles=payload.roles, branch_ids=payload.branch_ids,
    )
    key = _key(idempotency_key)
    scope = f"settings.member.{member.id}.update"
    if existing := get_idempotent_response(db, scope, key, member.business_id):
        return existing
    if not member.active or member.archived_at is not None:
        raise HTTPException(status_code=409, detail="Staff member is archived")
    assert_version(member.version, payload.expected_version)
    values = payload.model_dump(
        exclude={"expected_version", "pin", "roles", "branch_ids"}, exclude_unset=True
    )
    if "email" in values and values["email"]:
        values["email"] = values["email"].strip().lower()
        duplicate = db.scalar(
            select(StaffMember.id).where(
                StaffMember.business_id == member.business_id,
                StaffMember.email == values["email"],
                StaffMember.id != member.id,
            )
        )
        if duplicate:
            raise HTTPException(status_code=409, detail="A staff member already uses this email")
    for field, value in values.items():
        setattr(member, field, value)
    if payload.pin is not None:
        member.pin_hash = hash_pin(payload.pin.get_secret_value())
    current_roles = [item.role for item in db.scalars(
        select(StaffMemberRole).where(StaffMemberRole.staff_member_id == member.id)
    )]
    current_branches = [item.branch_id for item in db.scalars(
        select(StaffMemberBranch).where(StaffMemberBranch.staff_member_id == member.id)
    )]
    if payload.roles is not None or payload.branch_ids is not None:
        replace_staff_access(
            db,
            member,
            roles=payload.roles if payload.roles is not None else current_roles,
            branch_ids=payload.branch_ids if payload.branch_ids is not None else current_branches,
        )
    if member.email_access and not member.email:
        raise HTTPException(status_code=422, detail="Email access requires an email address")
    member.version += 1
    db.flush()
    response = serialize_staff_member(db, member)
    audit(
        db,
        user,
        "settings.member.updated",
        "staff_member",
        member.id,
        member.business_id,
        {"roles_changed": payload.roles is not None, "branches_changed": payload.branch_ids is not None},
        actor_display_name=_actor(db, user),
    )
    return _save(db, scope=scope, key=key, business_id=member.business_id, response=response)


@router.delete("/members/{member_id}")
def delete_staff_member(
    member_id: int,
    payload: ArchiveRequest,
    idempotency_key: IdempotencyHeader = None,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    member = _staff_member(db, user, member_id, for_update=True)
    require_staff_management_change(db, user, member.business_id, member=member)
    key = _key(idempotency_key)
    scope = f"settings.member.{member.id}.archive"
    if existing := get_idempotent_response(db, scope, key, member.business_id):
        return existing
    if not member.active or member.archived_at is not None:
        raise HTTPException(status_code=409, detail="Staff member is archived")
    assert_version(member.version, payload.expected_version)
    archive_staff_member(db, member, user.user_id)
    response = serialize_staff_member(db, member)
    audit(
        db,
        user,
        "settings.member.archived",
        "staff_member",
        member.id,
        member.business_id,
        actor_display_name=_actor(db, user),
    )
    return _save(db, scope=scope, key=key, business_id=member.business_id, response=response)


@router.get("/devices")
def list_devices(
    branch_id: int,
    include_archived: bool = False,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch = scoped_branch(db, user, branch_id)
    require_settings_permission(db, user, branch.business_id, "members")
    statement = select(PairedDevice).where(PairedDevice.branch_id == branch.id)
    if not include_archived:
        statement = statement.where(PairedDevice.active.is_(True), PairedDevice.archived_at.is_(None))
    devices = list(db.scalars(statement.order_by(PairedDevice.name, PairedDevice.id)))
    return {"items": [_device_response(item) for item in devices], "total": len(devices)}


@router.post("/devices", status_code=201)
def create_device(
    payload: PairedDeviceCreate,
    idempotency_key: IdempotencyHeader = None,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch = scoped_branch(db, user, payload.branch_id)
    require_settings_permission(db, user, branch.business_id, "members")
    key = _key(idempotency_key)
    scope = f"settings.branch.{branch.id}.devices.create"
    if existing := get_idempotent_response(db, scope, key, branch.business_id):
        return existing
    device = PairedDevice(
        business_id=branch.business_id,
        branch_id=branch.id,
        name=payload.name.strip(),
    )
    db.add(device)
    db.flush()
    pairing_code = issue_pairing_code(device)
    response = {**_device_response(device), "pairing_code": pairing_code}
    audit(
        db,
        user,
        "settings.device.created",
        "paired_device",
        device.id,
        branch.business_id,
        branch_id=branch.id,
        actor_display_name=_actor(db, user),
    )
    return _save(db, scope=scope, key=key, business_id=branch.business_id, response=response)


@router.post("/devices/pair")
def pair_device(
    payload: PairDeviceComplete,
    idempotency_key: IdempotencyHeader = None,
    db: Session = Depends(get_db),
):
    key = _key(idempotency_key)
    code_hash = hashlib.sha256(payload.pairing_code.get_secret_value().encode()).hexdigest()
    scope = f"settings.devices.pair.{code_hash}"
    replay = db.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.scope == scope,
            IdempotencyRecord.idempotency_key == key,
        )
    )
    if replay:
        device_from_token(db, replay.response_body["device_token"])
        return replay.response_body
    pending_device = db.scalar(
        select(PairedDevice).where(
            PairedDevice.pairing_code_hash == code_hash,
            PairedDevice.active.is_(True),
        )
    )
    if not pending_device:
        raise HTTPException(status_code=401, detail="Pairing code is invalid or expired")
    device, raw_token = complete_device_pairing(db, payload.pairing_code.get_secret_value())
    response = {**_device_response(device), "device_token": raw_token}
    return _save(db, scope=scope, key=key, business_id=device.business_id, response=response)


@router.get("/devices/{device_id}")
def get_device(
    device_id: int,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    device = db.get(PairedDevice, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Paired device not found")
    branch = scoped_branch(db, user, device.branch_id)
    require_settings_permission(db, user, branch.business_id, "members")
    return _device_response(device)


@router.patch("/devices/{device_id}")
def update_device(
    device_id: int,
    payload: PairedDeviceUpdate,
    idempotency_key: IdempotencyHeader = None,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    device = db.get(PairedDevice, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Paired device not found")
    branch = scoped_branch(db, user, device.branch_id)
    require_settings_permission(db, user, branch.business_id, "members")
    key = _key(idempotency_key)
    scope = f"settings.device.{device.id}.update"
    if existing := get_idempotent_response(db, scope, key, branch.business_id):
        return existing
    device = db.scalar(select(PairedDevice).where(PairedDevice.id == device.id).with_for_update())
    assert_version(device.version, payload.expected_version)
    for field, value in payload.model_dump(exclude={"expected_version"}, exclude_none=True).items():
        setattr(device, field, value)
    if payload.active is False:
        from .device_auth import clear_session
        clear_session(device)
        device.token_hash = None
        device.pairing_code_hash = None
        device.credential_expires_at = None
    device.version += 1
    response = _device_response(device)
    audit(
        db,
        user,
        "settings.device.updated",
        "paired_device",
        device.id,
        branch.business_id,
        branch_id=branch.id,
        actor_display_name=_actor(db, user),
    )
    return _save(db, scope=scope, key=key, business_id=branch.business_id, response=response)


@router.delete("/devices/{device_id}")
def delete_device(
    device_id: int,
    payload: ArchiveRequest,
    idempotency_key: IdempotencyHeader = None,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    device = db.get(PairedDevice, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Paired device not found")
    branch = scoped_branch(db, user, device.branch_id)
    require_settings_permission(db, user, branch.business_id, "members")
    key = _key(idempotency_key)
    scope = f"settings.device.{device.id}.archive"
    if existing := get_idempotent_response(db, scope, key, branch.business_id):
        return existing
    device = db.scalar(select(PairedDevice).where(PairedDevice.id == device.id).with_for_update())
    assert_version(device.version, payload.expected_version)
    device.active = False
    device.archived_at = utcnow()
    device.token_hash = None
    device.pairing_code_hash = None
    from .device_auth import clear_session
    clear_session(device)
    device.credential_expires_at = None
    device.version += 1
    response = _device_response(device)
    audit(
        db,
        user,
        "settings.device.archived",
        "paired_device",
        device.id,
        branch.business_id,
        branch_id=branch.id,
        actor_display_name=_actor(db, user),
    )
    return _save(db, scope=scope, key=key, business_id=branch.business_id, response=response)


@router.post("/devices/pin/verify")
def verify_device_pin(
    payload: StaffPinVerify,
    x_device_token: Annotated[str, Header(alias="X-Device-Token")],
    idempotency_key: IdempotencyHeader = None,
    db: Session = Depends(get_db),
):
    device = device_from_token(db, x_device_token)
    key = _key(idempotency_key)
    scope = f"settings.device.{device.id}.pin.verify"
    if existing := get_idempotent_response(db, scope, key, device.business_id):
        return existing
    member = db.scalar(
        select(StaffMember)
        .join(StaffMemberBranch, StaffMemberBranch.staff_member_id == StaffMember.id)
        .where(
            StaffMember.id == payload.staff_member_id,
            StaffMember.business_id == device.business_id,
            StaffMemberBranch.branch_id == device.branch_id,
            StaffMember.active.is_(True),
            StaffMember.archived_at.is_(None),
        )
        .with_for_update()
    )
    if not member or not verify_staff_pin(db, member, payload.pin.get_secret_value()):
        db.commit()
        raise HTTPException(status_code=401, detail="Invalid staff PIN")
    response = {"verified": True, "member": serialize_staff_member(db, member)}
    return _save(db, scope=scope, key=key, business_id=device.business_id, response=response)


def _schedule(
    db: Session,
    user: AuthContext,
    branch_id: int,
    schedule_id: int,
    *,
    for_update: bool = False,
) -> tuple[Branch, ServiceSchedule]:
    branch = scoped_branch(db, user, branch_id)
    require_settings_permission(db, user, branch.business_id, "operations")
    statement = select(ServiceSchedule).where(
        ServiceSchedule.id == schedule_id,
        ServiceSchedule.branch_id == branch.id,
        ServiceSchedule.business_id == branch.business_id,
    )
    if for_update:
        statement = statement.with_for_update()
    schedule = db.scalar(statement)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return branch, schedule


@router.get("/branches/{branch_id}/schedules")
def list_schedules(
    branch_id: int,
    include_archived: bool = False,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch = scoped_branch(db, user, branch_id)
    require_settings_permission(db, user, branch.business_id, "operations")
    statement = select(ServiceSchedule).where(ServiceSchedule.branch_id == branch.id)
    if not include_archived:
        statement = statement.where(ServiceSchedule.archived_at.is_(None))
    schedules = list(
        db.scalars(statement.order_by(ServiceSchedule.kind.desc(), ServiceSchedule.name))
    )
    return {"items": [serialize_schedule(db, item) for item in schedules], "total": len(schedules)}


@router.post("/branches/{branch_id}/schedules", status_code=201)
def create_schedule(
    branch_id: int,
    payload: ServiceScheduleCreate,
    idempotency_key: IdempotencyHeader = None,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch = scoped_branch(db, user, branch_id)
    require_settings_permission(db, user, branch.business_id, "operations")
    key = _key(idempotency_key)
    scope = f"settings.branch.{branch.id}.schedules.create"
    if existing := get_idempotent_response(db, scope, key, branch.business_id):
        return existing
    if payload.kind == "primary" and db.scalar(
        select(ServiceSchedule.id).where(
            ServiceSchedule.branch_id == branch.id,
            ServiceSchedule.kind == "primary",
            ServiceSchedule.archived_at.is_(None),
        )
    ):
        raise HTTPException(status_code=409, detail="This branch already has a primary schedule")
    schedule = ServiceSchedule(
        business_id=branch.business_id,
        branch_id=branch.id,
        name=payload.name.strip(),
        kind=payload.kind,
    )
    db.add(schedule)
    db.flush()
    replace_schedule_shifts(db, schedule, payload.shifts)
    db.flush()
    response = serialize_schedule(db, schedule)
    audit(
        db,
        user,
        "settings.schedule.created",
        "service_schedule",
        schedule.id,
        branch.business_id,
        {"kind": schedule.kind},
        branch_id=branch.id,
        actor_display_name=_actor(db, user),
    )
    return _save(db, scope=scope, key=key, business_id=branch.business_id, response=response)


@router.get("/branches/{branch_id}/schedules/{schedule_id}")
def get_schedule(
    branch_id: int,
    schedule_id: int,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _, schedule = _schedule(db, user, branch_id, schedule_id)
    return serialize_schedule(db, schedule)


@router.patch("/branches/{branch_id}/schedules/{schedule_id}")
def update_schedule(
    branch_id: int,
    schedule_id: int,
    payload: ServiceScheduleUpdate,
    idempotency_key: IdempotencyHeader = None,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch, schedule = _schedule(db, user, branch_id, schedule_id, for_update=True)
    key = _key(idempotency_key)
    scope = f"settings.schedule.{schedule.id}.update"
    if existing := get_idempotent_response(db, scope, key, branch.business_id):
        return existing
    assert_version(schedule.version, payload.expected_version)
    values = payload.model_dump(exclude={"expected_version", "shifts"}, exclude_none=True)
    for field, value in values.items():
        setattr(schedule, field, value)
    if payload.shifts is not None:
        replace_schedule_shifts(db, schedule, payload.shifts)
    schedule.version += 1
    db.flush()
    response = serialize_schedule(db, schedule)
    audit(
        db,
        user,
        "settings.schedule.updated",
        "service_schedule",
        schedule.id,
        branch.business_id,
        {"shifts_changed": payload.shifts is not None, **values},
        branch_id=branch.id,
        actor_display_name=_actor(db, user),
    )
    return _save(db, scope=scope, key=key, business_id=branch.business_id, response=response)


@router.put("/branches/{branch_id}/schedules/{schedule_id}/assignments")
def replace_assignments(
    branch_id: int,
    schedule_id: int,
    payload: ScheduleAssignmentsReplace,
    idempotency_key: IdempotencyHeader = None,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch, schedule = _schedule(db, user, branch_id, schedule_id, for_update=True)
    key = _key(idempotency_key)
    scope = f"settings.schedule.{schedule.id}.assignments.replace"
    if existing := get_idempotent_response(db, scope, key, branch.business_id):
        return existing
    assert_version(schedule.version, payload.expected_version)
    if schedule.kind == "primary" and (payload.product_ids or payload.promotion_ids):
        raise HTTPException(status_code=409, detail="The primary schedule applies to the whole branch")
    replace_schedule_assignments(db, schedule, payload.product_ids, payload.promotion_ids)
    schedule.version += 1
    db.flush()
    response = serialize_schedule(db, schedule)
    audit(
        db,
        user,
        "settings.schedule.assignments.updated",
        "service_schedule",
        schedule.id,
        branch.business_id,
        {"product_ids": payload.product_ids, "promotion_ids": payload.promotion_ids},
        branch_id=branch.id,
        actor_display_name=_actor(db, user),
    )
    return _save(db, scope=scope, key=key, business_id=branch.business_id, response=response)


@router.delete("/branches/{branch_id}/schedules/{schedule_id}")
def delete_schedule(
    branch_id: int,
    schedule_id: int,
    payload: ArchiveRequest,
    idempotency_key: IdempotencyHeader = None,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch, schedule = _schedule(db, user, branch_id, schedule_id, for_update=True)
    key = _key(idempotency_key)
    scope = f"settings.schedule.{schedule.id}.archive"
    if existing := get_idempotent_response(db, scope, key, branch.business_id):
        return existing
    assert_version(schedule.version, payload.expected_version)
    if schedule.kind == "primary":
        raise HTTPException(status_code=409, detail="The primary schedule cannot be archived")
    schedule.active = False
    schedule.archived_at = utcnow()
    schedule.version += 1
    response = serialize_schedule(db, schedule)
    audit(
        db,
        user,
        "settings.schedule.archived",
        "service_schedule",
        schedule.id,
        branch.business_id,
        branch_id=branch.id,
        actor_display_name=_actor(db, user),
    )
    return _save(db, scope=scope, key=key, business_id=branch.business_id, response=response)


def _printer(
    db: Session,
    user: AuthContext,
    branch_id: int,
    printer_id: int,
    *,
    for_update: bool = False,
) -> tuple[Branch, PrinterDevice]:
    branch = scoped_branch(db, user, branch_id)
    require_settings_permission(db, user, branch.business_id, "printing")
    statement = select(PrinterDevice).where(
        PrinterDevice.id == printer_id,
        PrinterDevice.branch_id == branch.id,
        PrinterDevice.business_id == branch.business_id,
    )
    if for_update:
        statement = statement.with_for_update()
    printer = db.scalar(statement)
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    return branch, printer


def _validate_printer_device(db: Session, branch: Branch, paired_device_id: int | None) -> None:
    if paired_device_id is None:
        return
    device = db.scalar(
        select(PairedDevice).where(
            PairedDevice.id == paired_device_id,
            PairedDevice.business_id == branch.business_id,
            PairedDevice.branch_id == branch.id,
            PairedDevice.active.is_(True),
            PairedDevice.archived_at.is_(None),
        )
    )
    if not device:
        raise HTTPException(status_code=422, detail="Paired device must belong to this branch")


@router.get("/branches/{branch_id}/printers")
def list_printers(
    branch_id: int,
    include_archived: bool = False,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch = scoped_branch(db, user, branch_id)
    require_settings_permission(db, user, branch.business_id, "printing")
    statement = select(PrinterDevice).where(PrinterDevice.branch_id == branch.id)
    if not include_archived:
        statement = statement.where(PrinterDevice.active.is_(True), PrinterDevice.archived_at.is_(None))
    printers = list(db.scalars(statement.order_by(PrinterDevice.name, PrinterDevice.id)))
    return {"items": [serialize_printer(item) for item in printers], "total": len(printers)}


@router.post("/branches/{branch_id}/printers", status_code=201)
def create_printer(
    branch_id: int,
    payload: PrinterDeviceCreate,
    idempotency_key: IdempotencyHeader = None,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch = scoped_branch(db, user, branch_id)
    require_settings_permission(db, user, branch.business_id, "printing")
    _validate_printer_device(db, branch, payload.paired_device_id)
    key = _key(idempotency_key)
    scope = f"settings.branch.{branch.id}.printers.create"
    if existing := get_idempotent_response(db, scope, key, branch.business_id):
        return existing
    printer = PrinterDevice(
        business_id=branch.business_id,
        branch_id=branch.id,
        **payload.model_dump(),
    )
    db.add(printer)
    db.flush()
    response = serialize_printer(printer)
    audit(
        db,
        user,
        "settings.printer.created",
        "printer_device",
        printer.id,
        branch.business_id,
        {"purpose": printer.purpose},
        branch_id=branch.id,
        actor_display_name=_actor(db, user),
    )
    return _save(db, scope=scope, key=key, business_id=branch.business_id, response=response)


@router.get("/branches/{branch_id}/printers/{printer_id}")
def get_printer(
    branch_id: int,
    printer_id: int,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _, printer = _printer(db, user, branch_id, printer_id)
    return serialize_printer(printer)


@router.patch("/branches/{branch_id}/printers/{printer_id}")
def update_printer(
    branch_id: int,
    printer_id: int,
    payload: PrinterDeviceUpdate,
    idempotency_key: IdempotencyHeader = None,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch, printer = _printer(db, user, branch_id, printer_id, for_update=True)
    key = _key(idempotency_key)
    scope = f"settings.printer.{printer.id}.update"
    if existing := get_idempotent_response(db, scope, key, branch.business_id):
        return existing
    assert_version(printer.version, payload.expected_version)
    values = payload.model_dump(exclude={"expected_version"}, exclude_none=True)
    if "paired_device_id" in values:
        _validate_printer_device(db, branch, values["paired_device_id"])
    for field, value in values.items():
        setattr(printer, field, value)
    printer.version += 1
    response = serialize_printer(printer)
    audit(
        db,
        user,
        "settings.printer.updated",
        "printer_device",
        printer.id,
        branch.business_id,
        values,
        branch_id=branch.id,
        actor_display_name=_actor(db, user),
    )
    return _save(db, scope=scope, key=key, business_id=branch.business_id, response=response)


@router.delete("/branches/{branch_id}/printers/{printer_id}")
def delete_printer(
    branch_id: int,
    printer_id: int,
    payload: ArchiveRequest,
    idempotency_key: IdempotencyHeader = None,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch, printer = _printer(db, user, branch_id, printer_id, for_update=True)
    key = _key(idempotency_key)
    scope = f"settings.printer.{printer.id}.archive"
    if existing := get_idempotent_response(db, scope, key, branch.business_id):
        return existing
    assert_version(printer.version, payload.expected_version)
    if db.scalar(
        select(PrintJob.id).where(
            PrintJob.printer_id == printer.id,
            PrintJob.status.in_(["pending", "claimed"]),
        ).limit(1)
    ):
        raise HTTPException(status_code=409, detail="Printer has pending print jobs")
    printer.active = False
    printer.archived_at = utcnow()
    printer.version += 1
    response = serialize_printer(printer)
    audit(
        db,
        user,
        "settings.printer.archived",
        "printer_device",
        printer.id,
        branch.business_id,
        branch_id=branch.id,
        actor_display_name=_actor(db, user),
    )
    return _save(db, scope=scope, key=key, business_id=branch.business_id, response=response)


@router.get("/branches/{branch_id}/print-jobs")
def list_print_jobs(
    branch_id: int,
    status: str | None = None,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch = scoped_branch(db, user, branch_id)
    require_settings_permission(db, user, branch.business_id, "printing")
    statement = select(PrintJob).where(PrintJob.branch_id == branch.id)
    if status:
        statement = statement.where(PrintJob.status == status)
    jobs = list(db.scalars(statement.order_by(PrintJob.created_at.desc()).limit(200)))
    return {"items": [serialize_print_job(item) for item in jobs], "total": len(jobs)}


@router.post("/branches/{branch_id}/print-jobs", status_code=201)
def create_print_job(
    branch_id: int,
    payload: PrintJobCreate,
    idempotency_key: IdempotencyHeader = None,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch = scoped_branch(db, user, branch_id)
    require_settings_permission(db, user, branch.business_id, "printing")
    key = _key(idempotency_key)
    scope = f"settings.branch.{branch.id}.print_jobs.create"
    if existing := get_idempotent_response(db, scope, key, branch.business_id):
        return existing
    printer = None
    if payload.printer_id is not None:
        _, printer = _printer(db, user, branch.id, payload.printer_id)
    paired_device_id = payload.paired_device_id or (printer.paired_device_id if printer else None)
    _validate_printer_device(db, branch, paired_device_id)
    if payload.order_id is not None and not db.scalar(
        select(Order.id).where(
            Order.id == payload.order_id,
            Order.business_id == branch.business_id,
            Order.branch_id == branch.id,
        )
    ):
        raise HTTPException(status_code=422, detail="Order must belong to this branch")
    if payload.kitchen_ticket_id is not None and not db.scalar(
        select(KitchenTicket.id).where(
            KitchenTicket.id == payload.kitchen_ticket_id,
            KitchenTicket.business_id == branch.business_id,
            KitchenTicket.branch_id == branch.id,
        )
    ):
        raise HTTPException(status_code=422, detail="Kitchen ticket must belong to this branch")
    job = PrintJob(
        id=str(uuid4()),
        business_id=branch.business_id,
        branch_id=branch.id,
        paired_device_id=paired_device_id,
        printer_id=payload.printer_id,
        order_id=payload.order_id,
        kitchen_ticket_id=payload.kitchen_ticket_id,
        job_type=payload.job_type,
        payload=payload.payload,
        idempotency_key=key,
    )
    db.add(job)
    db.flush()
    response = serialize_print_job(job)
    audit(
        db,
        user,
        "settings.print_job.created",
        "print_job",
        job.id,
        branch.business_id,
        {"job_type": job.job_type},
        branch_id=branch.id,
        actor_display_name=_actor(db, user),
    )
    return _save(db, scope=scope, key=key, business_id=branch.business_id, response=response)


@router.post("/devices/print-jobs/claim")
def claim_print_job(
    x_device_token: Annotated[str, Header(alias="X-Device-Token")],
    idempotency_key: IdempotencyHeader = None,
    db: Session = Depends(get_db),
):
    device = device_from_token(db, x_device_token)
    key = _key(idempotency_key)
    scope = f"settings.device.{device.id}.print_jobs.claim"
    if existing := get_idempotent_response(db, scope, key, device.business_id):
        return existing
    job = db.scalar(
        select(PrintJob)
        .where(
            PrintJob.business_id == device.business_id,
            PrintJob.branch_id == device.branch_id,
            PrintJob.paired_device_id == device.id,
            PrintJob.status == "pending",
        )
        .order_by(PrintJob.created_at, PrintJob.id)
        .with_for_update()
    )
    if not job:
        db.commit()
        return {"job": None}
    job.status = "claimed"
    job.claimed_at = utcnow()
    job.attempts += 1
    response = {"job": serialize_print_job(job)}
    return _save(db, scope=scope, key=key, business_id=device.business_id, response=response)


@router.patch("/devices/print-jobs/{job_id}")
def complete_print_job(
    job_id: str,
    payload: PrintJobComplete,
    x_device_token: Annotated[str, Header(alias="X-Device-Token")],
    idempotency_key: IdempotencyHeader = None,
    db: Session = Depends(get_db),
):
    device = device_from_token(db, x_device_token)
    key = _key(idempotency_key)
    scope = f"settings.print_job.{job_id}.complete"
    if existing := get_idempotent_response(db, scope, key, device.business_id):
        return existing
    job = db.scalar(
        select(PrintJob).where(
            PrintJob.id == job_id,
            PrintJob.business_id == device.business_id,
            PrintJob.branch_id == device.branch_id,
            PrintJob.paired_device_id == device.id,
        ).with_for_update()
    )
    if not job:
        raise HTTPException(status_code=404, detail="Print job not found")
    if job.status not in {"pending", "claimed"}:
        raise HTTPException(status_code=409, detail="Print job is already complete")
    job.status = payload.status
    job.error_message = payload.error_message
    if payload.status == "printed":
        job.completed_at = utcnow()
    else:
        job.failed_at = utcnow()
    response = serialize_print_job(job)
    return _save(db, scope=scope, key=key, business_id=device.business_id, response=response)


@router.get("/audit")
def get_settings_audit(
    business_id: int | None = None,
    branch_id: int | None = None,
    action: str | None = None,
    category: AuditCategory | None = None,
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    filters = _audit_scope(db, user, business_id, branch_id)
    if from_date and to_date and from_date > to_date:
        raise HTTPException(status_code=422, detail="from must not be after to")
    if action:
        filters.append(AuditEvent.action.ilike(f"%{action.strip()}%"))
    if from_date:
        filters.append(
            AuditEvent.created_at >= datetime.combine(from_date, time.min, tzinfo=LIMA).astimezone(timezone.utc)
        )
    if to_date:
        filters.append(
            AuditEvent.created_at < datetime.combine(to_date + timedelta(days=1), time.min, tzinfo=LIMA).astimezone(timezone.utc)
        )
    if category:
        filters.append(AuditEvent.action == CATEGORY_ACTIONS[category])
        # Legacy JSON has no category flags. Stream the scoped candidates so
        # semantic counts are complete without loading an unbounded result list.
        candidates = db.scalars(select(AuditEvent).where(*filters).order_by(
            AuditEvent.created_at.desc(), AuditEvent.id.desc(),
        ).execution_options(yield_per=100))
        items, total = [], 0
        offset = (page - 1) * page_size
        for item in candidates:
            projection = project_audit(db, item)
            if category not in projection["categories"]:
                continue
            if offset <= total < offset + page_size:
                items.append(audit_list_entry(item, projection))
            total += 1
        return {"items": items, "page": page, "page_size": page_size, "total": total}
    total = db.scalar(select(func.count(AuditEvent.id)).where(*filters)) or 0
    events = list(
        db.scalars(
            select(AuditEvent)
            .where(*filters)
            .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return {
        "items": [audit_list_entry(item, project_audit(db, item)) for item in events],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


def _audit_scope(db: Session, user: AuthContext, business_id: int | None, branch_id: int | None):
    resolved = _business_id(user, business_id)
    require_settings_permission(db, user, resolved, "audit")
    filters = [AuditEvent.business_id == resolved]
    if branch_id is not None:
        branch = scoped_branch(db, user, branch_id, include_archived=True)
        if branch.business_id != resolved:
            raise HTTPException(status_code=404, detail="Branch not found")
    effective_branch = user.branch_id if user.branch_id is not None else branch_id
    if effective_branch is not None:
        filters.append(audit_branch_filter(effective_branch))
    return filters


@router.get("/audit/{audit_id}")
def get_settings_audit_detail(
    audit_id: int,
    business_id: int | None = None,
    branch_id: int | None = None,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    filters = _audit_scope(db, user, business_id, branch_id)
    event = db.scalar(select(AuditEvent).where(AuditEvent.id == audit_id, *filters))
    if event is None:
        raise HTTPException(status_code=404, detail="Audit event not found")
    projection = project_audit(db, event)
    projection.pop("categories")
    return projection
