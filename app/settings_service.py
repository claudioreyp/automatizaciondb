from __future__ import annotations

import hashlib
import os
import secrets
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from fastapi import HTTPException
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from .auth import AuthContext, ensure_branch_scope, ensure_business_scope
from .command_revisions import effective_ticket_items
from .errors import CodedHTTPException
from .printing_settings import PRINTING_JSON_DEFAULTS, printing_json
from .qz_signing import qz_certificate, qz_sign_payload
from .schemas import DeliveryOrigin
from .models import (
    AuditEvent,
    Branch,
    BranchSettings,
    Business,
    CashRegister,
    CashSession,
    DeliveryBand,
    DeliveryQuote,
    DiningArea,
    IntegrationCredential,
    KitchenTicket,
    Membership,
    Order,
    PairedDevice,
    PrintJob,
    PrinterDevice,
    Product,
    Promotion,
    Reservation,
    RestaurantTable,
    ScheduleAssignment,
    ScheduleShift,
    ServiceSchedule,
    StaffMember,
    StaffMemberBranch,
    StaffMemberRole,
    utcnow,
)

try:
    from argon2 import PasswordHasher
    from argon2.exceptions import InvalidHashError, VerifyMismatchError
except ImportError:  # pragma: no cover - dependency is required in production
    PasswordHasher = None
    InvalidHashError = VerifyMismatchError = ValueError


TWOPLACES = Decimal("0.01")
SETTINGS_ROLES = {
    "owner",
    "members_manager",
    "manager",
    "menu_manager",
    "cashier",
    "waiter",
    "kitchen",
    "dispatcher",
}
ASSIGNABLE_SETTINGS_ROLES = (
    "owner", "members_manager", "manager", "menu_manager",
    "cashier", "waiter", "kitchen", "dispatcher",
)
PERMISSION_ROLES = {
    "business": {"superadmin", "owner"},
    "branch": {"superadmin", "owner", "manager"},
    "members": {"superadmin", "owner", "members_manager"},
    "operations": {"superadmin", "owner", "manager", "menu_manager"},
    "delivery_quotes": {"superadmin", "owner", "manager", "menu_manager", "cashier", "waiter"},
    "audit": {"superadmin", "owner", "manager"},
    "printing": {"superadmin", "owner", "manager"},
    "printing_runtime": {"superadmin", "owner", "manager", "cashier", "waiter", "kitchen"},
}
DEFAULT_PAYMENT_METHODS = {
    "delivery": ["cash", "card", "transfer", "yape", "plin"],
    "takeaway": ["cash", "card", "transfer", "yape", "plin"],
    "counter": ["cash", "card", "transfer", "yape", "plin"],
}
PIN_LOCK_THRESHOLD = 5
PIN_LOCK_MINUTES = 10
PAIRING_TTL_MINUTES = 10
QUOTE_TTL_MINUTES = 15


def money(value: Decimal | int | float | str | None) -> Decimal:
    return Decimal(str(value or 0)).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def actor_display_name(db: Session, user: AuthContext) -> str:
    member = db.scalar(
        select(StaffMember).where(
            StaffMember.id == user.staff_member_id if user.staff_member_id else StaffMember.auth_user_id == user.user_id,
            StaffMember.business_id == user.business_id,
            StaffMember.active.is_(True),
        )
    )
    if member:
        return f"{member.first_name} {member.last_name}".strip()
    membership = db.scalar(
        select(Membership).where(
            Membership.auth_user_id == user.user_id,
            Membership.active.is_(True),
        )
    )
    return membership.full_name if membership else (user.email or user.user_id)


def effective_roles(db: Session, user: AuthContext, business_id: int) -> set[str]:
    ensure_business_scope(user, business_id)
    if user.is_superadmin:
        return {"superadmin"}
    member = db.scalar(
        select(StaffMember).where(
            StaffMember.business_id == business_id,
            StaffMember.id == user.staff_member_id if user.staff_member_id else StaffMember.auth_user_id == user.user_id,
        ).execution_options(populate_existing=True)
    )
    if member is None:
        return {user.role}
    if not member.active or member.archived_at is not None:
        return set()
    # A stale membership/auth role must not restore access removed in Settings.
    return set(db.scalars(
        select(StaffMemberRole.role).where(
            StaffMemberRole.business_id == business_id,
            StaffMemberRole.staff_member_id == member.id,
        )
    ))


def require_settings_permission(
    db: Session,
    user: AuthContext,
    business_id: int,
    permission: str,
) -> set[str]:
    roles = effective_roles(db, user, business_id)
    if not roles.intersection(PERMISSION_ROLES[permission]):
        raise HTTPException(status_code=403, detail="Insufficient settings permission")
    return roles


def lock_staff_business(db: Session, business_id: int) -> None:
    # All member writes take this lock before reading permissions or owner counts.
    # SQLite ignores FOR UPDATE; a no-op write acquires its transaction write lock.
    if db.get_bind().dialect.name == "sqlite":
        result = db.execute(
            update(Business)
            .where(Business.id == business_id)
            .values(version=Business.version, updated_at=Business.updated_at)
            .execution_options(synchronize_session=False)
        )
        exists = result.rowcount > 0
    else:
        exists = db.scalar(
            select(Business.id).where(Business.id == business_id).with_for_update()
        ) is not None
    if not exists:
        raise HTTPException(status_code=404, detail="Business not found")


def staff_management_capabilities(db: Session, user: AuthContext, business_id: int) -> dict:
    roles = require_settings_permission(db, user, business_id, "members")
    can_manage_admins = bool(roles.intersection({"owner", "superadmin"}))
    return {
        "assignable_roles": [
            role for role in ASSIGNABLE_SETTINGS_ROLES if role != "owner" or can_manage_admins
        ],
        "can_manage_admins": can_manage_admins,
    }


def active_owner_count(db: Session, business_id: int) -> int:
    return db.scalar(
        select(func.count(func.distinct(StaffMember.id)))
        .join(StaffMemberRole, StaffMemberRole.staff_member_id == StaffMember.id)
        .where(
            StaffMember.business_id == business_id,
            StaffMemberRole.business_id == business_id,
            StaffMemberRole.role == "owner",
            StaffMember.active.is_(True),
            StaffMember.archived_at.is_(None),
        )
    ) or 0


def staff_member_capabilities(
    member: StaffMember, user: AuthContext, roles: list[str],
    *, can_manage_admins: bool, owner_count: int,
) -> dict:
    is_owner = "owner" in roles
    can_edit = bool(
        member.active and member.archived_at is None and (not is_owner or can_manage_admins)
    )
    return {
        "can_edit": can_edit,
        "can_archive": bool(
            can_edit and member.auth_user_id != user.user_id and member.id != user.staff_member_id
            and (not is_owner or owner_count > 1)
        ),
    }


def require_staff_management_change(
    db: Session, user: AuthContext, business_id: int, *,
    member: StaffMember | None = None,
    roles: list[str] | None = None,
    branch_ids: list[int] | None = None,
) -> None:
    capabilities = staff_management_capabilities(db, user, business_id)
    if capabilities["can_manage_admins"]:
        return
    current_roles = set(staff_roles(db, member.id)) if member is not None else set()
    if "owner" in current_roles or "owner" in (roles or []):
        raise HTTPException(status_code=403, detail="Only administrators can manage administrators")
    if member is not None and (member.auth_user_id == user.user_id or member.id == user.staff_member_id):
        if roles is not None and not set(roles).issubset(current_roles):
            raise HTTPException(status_code=403, detail="You cannot expand your own roles")
        if branch_ids is not None and not set(branch_ids).issubset(staff_branches(db, member.id)):
            raise HTTPException(status_code=403, detail="You cannot expand your own branch access")


def require_another_owner(db: Session, member: StaffMember) -> None:
    if member.active and member.archived_at is None and active_owner_count(db, member.business_id) <= 1:
        raise HTTPException(status_code=409, detail="A business must keep at least one administrator")


def scoped_branch(db: Session, user: AuthContext, branch_id: int, *, include_archived: bool = False) -> Branch:
    statement = select(Branch).where(Branch.id == branch_id)
    if not include_archived:
        statement = statement.where(Branch.active.is_(True), Branch.archived_at.is_(None))
    branch = db.scalar(statement)
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found")
    ensure_branch_scope(user, branch.business_id, branch.id)
    return branch


def get_or_create_branch_settings(db: Session, branch: Branch) -> BranchSettings:
    settings = db.scalar(select(BranchSettings).where(BranchSettings.branch_id == branch.id))
    if settings:
        return settings
    accepted = list(dict.fromkeys(branch.accepted_payment_methods or [])) or list(
        DEFAULT_PAYMENT_METHODS["delivery"]
    )
    settings = BranchSettings(
        business_id=branch.business_id,
        branch_id=branch.id,
        pos_takeaway=branch.takeaway_enabled,
        pos_delivery=branch.delivery_enabled,
        digital_takeaway=branch.takeaway_enabled,
        digital_delivery=branch.delivery_enabled,
        delivery_mode="fixed",
        fixed_delivery_fee=money(branch.delivery_fee),
        payment_methods={
            "delivery": list(accepted),
            "takeaway": list(accepted),
            "counter": list(accepted),
        },
    )
    db.add(settings)
    db.flush()
    return settings


def sync_legacy_branch_fields(branch: Branch, settings: BranchSettings) -> None:
    branch.delivery_enabled = bool(settings.pos_delivery or settings.digital_delivery)
    branch.takeaway_enabled = bool(settings.pos_takeaway or settings.digital_takeaway)
    if settings.delivery_mode == "fixed":
        branch.delivery_fee = money(settings.fixed_delivery_fee)
    elif settings.delivery_mode == "free":
        branch.delivery_fee = money(0)
    merged_methods: list[str] = []
    for methods in (settings.payment_methods or {}).values():
        for method in methods or []:
            if method not in merged_methods:
                merged_methods.append(method)
    branch.accepted_payment_methods = merged_methods


def serialize_business(business: Business) -> dict:
    return {
        "id": business.id,
        "name": business.name,
        "currency": business.currency,
        "timezone": business.timezone,
        "country_code": business.country_code,
        "version": business.version,
    }


def serialize_branch_profile(branch: Branch) -> dict:
    media_base = f"/api/v1/settings/branches/{branch.id}/media"
    return {
        "id": branch.id,
        "business_id": branch.business_id,
        "name": branch.name,
        "address": branch.address,
        "phone": branch.phone,
        "maps_url": branch.maps_url,
        "google_place_id": branch.google_place_id,
        "latitude": float(branch.latitude) if branch.latitude is not None else None,
        "longitude": float(branch.longitude) if branch.longitude is not None else None,
        "logo_configured": bool(branch.logo_storage_path),
        "cover_configured": bool(branch.cover_storage_path),
        "logo_url": f"{media_base}/logo?v={branch.version}" if branch.logo_storage_path else None,
        "cover_url": f"{media_base}/cover?v={branch.version}" if branch.cover_storage_path else None,
        "active": branch.active,
        "archived_at": branch.archived_at,
        "version": branch.version,
    }


def serialize_branch_settings(settings: BranchSettings) -> dict:
    return {
        "id": settings.id,
        "business_id": settings.business_id,
        "branch_id": settings.branch_id,
        "services": {
            key: getattr(settings, key)
            for key in (
                "pos_tables",
                "pos_counter",
                "pos_takeaway",
                "pos_delivery",
                "digital_tables",
                "digital_takeaway",
                "digital_delivery",
            )
        },
        "delivery": {
            "delivery_mode": settings.delivery_mode,
            "delivery_policy": serialize_delivery_policy(settings),
            "delivery_policy_supported": True,
            "pos_quotes_supported": True,
            "fixed_delivery_fee": float(settings.fixed_delivery_fee or 0),
            "distance_base_fee": float(settings.distance_base_fee or 0),
            "distance_fee_per_km": float(settings.distance_fee_per_km or 0),
            "distance_max_km": float(settings.distance_max_km) if settings.distance_max_km is not None else None,
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
        },
        "payment_methods": settings.payment_methods or DEFAULT_PAYMENT_METHODS,
        "times": {
            "delivery_min_minutes": settings.delivery_min_minutes,
            "delivery_max_minutes": settings.delivery_max_minutes,
            "pickup_minutes": settings.pickup_minutes,
        },
        "printing": {
            "advanced_printing": settings.advanced_printing,
            **{field: printing_json(field, getattr(settings, field)) for field in PRINTING_JSON_DEFAULTS},
        },
        "version": settings.version,
    }


def serialize_delivery_policy(settings: BranchSettings) -> dict:
    return deepcopy(settings.delivery_policy) if settings.delivery_policy is not None else {
        "neighborhoods": [], "origin": None, "outside_band_mode": "reject",
    }


def serialize_branch_origin(branch: Branch) -> dict | None:
    if branch.latitude is None or branch.longitude is None:
        return None
    return {
        "latitude": float(branch.latitude),
        "longitude": float(branch.longitude),
        "maps_url": branch.maps_url,
    }


def serialize_delivery_band(band: DeliveryBand) -> dict:
    return {
        "id": band.id,
        "branch_id": band.branch_id,
        "minimum_km": float(band.minimum_km),
        "maximum_km": float(band.maximum_km),
        "fee": float(band.fee),
        "sort_order": band.sort_order,
        "active": band.active,
        "version": band.version,
    }


def ensure_delivery_bands_do_not_overlap(
    db: Session,
    branch_id: int,
    minimum_km: Decimal,
    maximum_km: Decimal,
    *,
    exclude_id: int | None = None,
) -> None:
    statement = select(DeliveryBand.id).where(
        DeliveryBand.branch_id == branch_id,
        DeliveryBand.active.is_(True),
        DeliveryBand.archived_at.is_(None),
        DeliveryBand.minimum_km < maximum_km,
        DeliveryBand.maximum_km > minimum_km,
    )
    if exclude_id is not None:
        statement = statement.where(DeliveryBand.id != exclude_id)
    if db.scalar(statement.limit(1)) is not None:
        raise HTTPException(status_code=409, detail="Delivery distance bands cannot overlap")


class DistanceProvider(Protocol):
    def distance_km(self, branch: Branch | DeliveryOrigin, destination: dict) -> Decimal: ...


class GoogleRoutesDistanceProvider:
    endpoint = "https://routes.googleapis.com/directions/v2:computeRoutes"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.getenv("GOOGLE_MAPS_API_KEY")

    def distance_km(self, branch: Branch | DeliveryOrigin, destination: dict) -> Decimal:
        if not self.api_key:
            raise HTTPException(status_code=503, detail="Google Maps credentials are not configured")
        if branch.latitude is None or branch.longitude is None:
            raise HTTPException(status_code=409, detail="Branch coordinates are required for distance delivery")
        latitude = destination.get("latitude")
        longitude = destination.get("longitude")
        if latitude is None or longitude is None:
            raise HTTPException(status_code=422, detail="Destination coordinates are required")
        try:
            response = httpx.post(
                self.endpoint,
                headers={
                    "X-Goog-Api-Key": self.api_key,
                    "X-Goog-FieldMask": "routes.distanceMeters",
                    "Content-Type": "application/json",
                },
                json={
                    "origin": {"location": {"latLng": {"latitude": float(branch.latitude), "longitude": float(branch.longitude)}}},
                    "destination": {"location": {"latLng": {"latitude": float(latitude), "longitude": float(longitude)}}},
                    "travelMode": "DRIVE",
                    "routingPreference": "TRAFFIC_AWARE",
                },
                timeout=10,
            )
            response.raise_for_status()
            meters = Decimal(str(response.json()["routes"][0]["distanceMeters"]))
            if not meters.is_finite() or meters < 0:
                raise ValueError("Invalid route distance")
            return (meters / Decimal("1000")).quantize(Decimal("0.01"))
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError, ArithmeticError) as exc:
            raise HTTPException(status_code=502, detail="Google Routes could not calculate this delivery") from exc


def create_delivery_quote(
    db: Session,
    branch: Branch,
    settings: BranchSettings,
    *,
    subtotal: Decimal,
    destination: dict,
    distance_provider: DistanceProvider | None = None,
    supplied_distance_km: Decimal | None = None,
    expected_configuration_version: int | None = None,
    confirmed_fee: Decimal | None = None,
    confirmed_by: AuthContext | None = None,
) -> DeliveryQuote:
    pos_strict = expected_configuration_version is not None
    if pos_strict:
        settings = db.scalar(select(BranchSettings).where(
            BranchSettings.branch_id == branch.id,
            BranchSettings.business_id == branch.business_id,
        ).with_for_update().execution_options(populate_existing=True))
        if settings is None or settings.version != expected_configuration_version:
            raise CodedHTTPException(409, "Delivery configuration changed; request a new quote", "DELIVERY_CONFIGURATION_STALE")
    if confirmed_fee is not None and (not pos_strict or confirmed_by is None):
        raise HTTPException(status_code=422, detail="Manual confirmation requires a POS quote and an authenticated actor")
    subtotal = money(subtotal)
    minimum = money(settings.minimum_order_amount) if settings.minimum_order_amount is not None else None
    if minimum is not None and subtotal < minimum:
        raise HTTPException(status_code=409, detail="Order subtotal is below the delivery minimum")

    distance: Decimal | None = None
    mode = settings.delivery_mode
    policy = serialize_delivery_policy(settings)
    if pos_strict and mode in {"bands", "distance"} and not policy.get("origin"):
        branch = db.scalar(select(Branch).where(
            Branch.id == branch.id,
            Branch.business_id == branch.business_id,
        ).with_for_update().execution_options(populate_existing=True))
    origin = DeliveryOrigin.model_validate(policy["origin"]) if policy.get("origin") else branch
    if mode in {"bands", "distance"}:
        if not pos_strict and supplied_distance_km is not None and os.getenv("ENVIRONMENT", "development").lower() in {
            "development",
            "dev",
            "test",
        }:
            distance = Decimal(str(supplied_distance_km)).quantize(Decimal("0.01"))
        else:
            distance = (distance_provider or GoogleRoutesDistanceProvider()).distance_km(origin, destination)

    band = None
    neighborhood = None
    coverage_requires_quote = False
    if mode == "distance":
        if settings.distance_max_km is not None and distance is not None and distance > settings.distance_max_km:
            raise HTTPException(status_code=409, detail="Destination is outside the delivery range")
    elif mode == "bands":
        band = db.scalar(
            select(DeliveryBand)
            .where(
                DeliveryBand.branch_id == branch.id,
                DeliveryBand.active.is_(True),
                DeliveryBand.archived_at.is_(None),
                DeliveryBand.minimum_km <= distance,
                DeliveryBand.maximum_km >= distance,
            )
            .order_by(DeliveryBand.sort_order, DeliveryBand.id)
        )
        if not band:
            if policy.get("outside_band_mode", "reject") == "quote":
                coverage_requires_quote = True
            else:
                raise HTTPException(status_code=409, detail="Destination is outside the configured delivery bands")
    elif mode == "neighborhoods":
        name = destination.get("neighborhood")
        if isinstance(name, str):
            neighborhood = next((
                item for item in policy.get("neighborhoods", [])
                if item["name"].strip().casefold() == name.strip().casefold()
            ), None)
        coverage_requires_quote = neighborhood is None

    # Free shipping changes the price, never the geographical coverage.
    if coverage_requires_quote:
        fee: Decimal | None = None
    elif settings.free_delivery_threshold is not None and subtotal >= money(settings.free_delivery_threshold):
        fee = Decimal("0")
    elif mode == "free":
        fee = Decimal("0")
    elif mode == "fixed":
        fee = money(settings.fixed_delivery_fee)
    elif mode == "quote":
        fee = None
    elif mode == "distance":
        fee = money(settings.distance_base_fee + settings.distance_fee_per_km * (distance or 0))
    elif mode == "bands":
        fee = money(band.fee)
    elif mode == "neighborhoods":
        fee = money(neighborhood["fee"])
    else:
        raise HTTPException(status_code=422, detail="Unsupported delivery mode")

    manual_confirmation = None
    if confirmed_fee is not None:
        if fee is not None:
            raise HTTPException(status_code=422, detail="Only a delivery requiring a quote can be manually confirmed")
        if not confirmed_fee.is_finite() or not Decimal("0") <= confirmed_fee <= Decimal("9999999999.99"):
            raise HTTPException(status_code=422, detail="Confirmed delivery fee must be finite and nonnegative")
        fee = money(confirmed_fee)
        manual_confirmation = {"fee": float(fee), "actor_id": confirmed_by.user_id}

    quote = DeliveryQuote(
        id=str(uuid4()),
        business_id=branch.business_id,
        branch_id=branch.id,
        mode=mode,
        subtotal=subtotal,
        distance_km=distance,
        fee=fee,
        minimum_order_amount=minimum,
        configuration_version=settings.version,
        input_snapshot={
            "destination": deepcopy(destination),
            "pos_strict": pos_strict,
            "context": "pos" if pos_strict else "legacy",
            "subtotal_basis": "before_discounts",
            "manual_confirmation": manual_confirmation,
            "delivery_policy": policy,
            "origin": (
                policy.get("origin") or serialize_branch_origin(branch)
                if mode in {"bands", "distance"} else None
            ),
        },
        expires_at=utcnow() + timedelta(minutes=QUOTE_TTL_MINUTES),
    )
    db.add(quote)
    db.flush()
    return quote


def serialize_delivery_quote(quote: DeliveryQuote) -> dict:
    return {
        "id": quote.id,
        "branch_id": quote.branch_id,
        "mode": quote.mode,
        "subtotal": float(quote.subtotal),
        "distance_km": float(quote.distance_km) if quote.distance_km is not None else None,
        "fee": float(quote.fee) if quote.fee is not None else None,
        "fee_status": "pending_quote" if quote.fee is None else "final",
        "requires_quote": quote.fee is None,
        "pos_strict": bool((quote.input_snapshot or {}).get("pos_strict")),
        "manually_confirmed": bool((quote.input_snapshot or {}).get("manual_confirmation")),
        "minimum_order_amount": (
            float(quote.minimum_order_amount) if quote.minimum_order_amount is not None else None
        ),
        "configuration_version": quote.configuration_version,
        "expires_at": quote.expires_at,
    }


def apply_delivery_quote_to_order(db: Session, order: Order, quote_id: str) -> None:
    quote = db.scalar(
        select(DeliveryQuote).where(
            DeliveryQuote.id == quote_id,
            DeliveryQuote.business_id == order.business_id,
            DeliveryQuote.branch_id == order.branch_id,
        )
    )
    if not quote:
        raise HTTPException(status_code=422, detail="Delivery quote is not valid for this branch")
    if (_aware(quote.expires_at) or utcnow()) <= utcnow():
        raise HTTPException(status_code=409, detail="Delivery quote has expired")
    if money(quote.subtotal) != money(order.subtotal):
        raise HTTPException(status_code=409, detail="Order subtotal changed after the delivery quote")
    snapshot = quote.input_snapshot or {}
    if snapshot.get("pos_strict"):
        if order.source != "pos" or order.channel != "delivery" or snapshot.get("context") != "pos":
            raise CodedHTTPException(409, "This delivery quote is only valid for a POS delivery order", "DELIVERY_QUOTE_CONTEXT_MISMATCH")
        if snapshot.get("destination") != (order.delivery_address or {}):
            raise CodedHTTPException(409, "Delivery destination changed; request a new quote", "DELIVERY_QUOTE_DESTINATION_CHANGED")
        settings = db.scalar(select(BranchSettings).where(
            BranchSettings.business_id == order.business_id,
            BranchSettings.branch_id == order.branch_id,
        ).with_for_update().execution_options(populate_existing=True))
        if settings is None or settings.version != quote.configuration_version:
            raise CodedHTTPException(409, "Delivery configuration changed; request a new quote", "DELIVERY_CONFIGURATION_STALE")
        if quote.mode in {"bands", "distance"}:
            current_origin = serialize_delivery_policy(settings).get("origin")
            if current_origin is None:
                # Profile coordinates have their own version, independent of settings.
                branch = db.scalar(select(Branch).where(
                    Branch.id == order.branch_id,
                    Branch.business_id == order.business_id,
                ).with_for_update().execution_options(populate_existing=True))
                current_origin = serialize_branch_origin(branch) if branch else None
            if current_origin is None or snapshot.get("origin") != current_origin:
                raise CodedHTTPException(409, "Delivery origin changed; request a new quote", "DELIVERY_QUOTE_ORIGIN_CHANGED")
        if quote.fee is None:
            raise CodedHTTPException(409, "Confirm the delivery fee before creating the order", "DELIVERY_FEE_PENDING")
    order.delivery_quote_id = quote.id
    order.delivery_fee = money(quote.fee)
    order.delivery_fee_status = "pending_quote" if quote.fee is None else "final"


def _password_hasher():
    if PasswordHasher is None:
        raise RuntimeError("argon2-cffi is required for staff PIN hashing")
    return PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2, hash_len=32, salt_len=16)


def hash_pin(pin: str) -> str:
    return _password_hasher().hash(pin)


def verify_pin_hash(pin_hash: str | None, pin: str) -> bool:
    if not pin_hash:
        return False
    try:
        return bool(_password_hasher().verify(pin_hash, pin))
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def staff_roles(db: Session, staff_member_id: int) -> list[str]:
    return list(
        db.scalars(
            select(StaffMemberRole.role)
            .where(StaffMemberRole.staff_member_id == staff_member_id)
            .order_by(StaffMemberRole.role)
        )
    )


def staff_branches(db: Session, staff_member_id: int) -> list[int]:
    return list(
        db.scalars(
            select(StaffMemberBranch.branch_id)
            .where(StaffMemberBranch.staff_member_id == staff_member_id)
            .order_by(StaffMemberBranch.branch_id)
        )
    )


def serialize_staff_member(db: Session, member: StaffMember) -> dict:
    return {
        "id": member.id,
        "business_id": member.business_id,
        "first_name": member.first_name,
        "last_name": member.last_name,
        "email": member.email,
        "email_access": member.email_access,
        "pin_configured": bool(member.pin_hash),
        "roles": staff_roles(db, member.id),
        "branch_ids": staff_branches(db, member.id),
        "active": member.active,
        "archived_at": member.archived_at,
        "version": member.version,
    }


def replace_staff_access(
    db: Session,
    member: StaffMember,
    *,
    roles: list[str],
    branch_ids: list[int],
) -> None:
    normalized_roles = sorted(set(roles))
    if not normalized_roles or set(normalized_roles) - SETTINGS_ROLES:
        raise HTTPException(status_code=422, detail="Invalid staff roles")
    normalized_branches = sorted(set(branch_ids))
    if not normalized_branches:
        raise HTTPException(status_code=422, detail="At least one branch is required")
    branches = list(
        db.scalars(
            select(Branch).where(
                Branch.id.in_(normalized_branches),
                Branch.business_id == member.business_id,
                Branch.active.is_(True),
            )
        )
    )
    if len(branches) != len(normalized_branches):
        raise HTTPException(status_code=422, detail="One or more branches are outside this business")
    if "owner" not in normalized_roles and "owner" in staff_roles(db, member.id):
        require_another_owner(db, member)
    db.execute(delete(StaffMemberRole).where(StaffMemberRole.staff_member_id == member.id))
    db.execute(delete(StaffMemberBranch).where(StaffMemberBranch.staff_member_id == member.id))
    db.add_all(
        [StaffMemberRole(staff_member_id=member.id, business_id=member.business_id, role=role) for role in normalized_roles]
        + [
            StaffMemberBranch(
                staff_member_id=member.id,
                business_id=member.business_id,
                branch_id=branch_id,
            )
            for branch_id in normalized_branches
        ]
    )


def verify_staff_pin(db: Session, member: StaffMember, pin: str) -> bool:
    now = utcnow()
    locked_until = _aware(member.pin_locked_until)
    if locked_until and locked_until > now:
        raise HTTPException(status_code=429, detail="PIN is temporarily locked")
    if verify_pin_hash(member.pin_hash, pin):
        member.failed_pin_attempts = 0
        member.pin_locked_until = None
        return True
    member.failed_pin_attempts += 1
    if member.failed_pin_attempts >= PIN_LOCK_THRESHOLD:
        member.failed_pin_attempts = 0
        member.pin_locked_until = now + timedelta(minutes=PIN_LOCK_MINUTES)
    return False


def issue_pairing_code(device: PairedDevice) -> str:
    code = f"{secrets.randbelow(1_000_000):06d}"
    device.pairing_code_hash = hashlib.sha256(code.encode()).hexdigest()
    device.pairing_expires_at = utcnow() + timedelta(minutes=PAIRING_TTL_MINUTES)
    return code


def complete_device_pairing(db: Session, pairing_code: str) -> tuple[PairedDevice, str]:
    if len(pairing_code) != 6 or not pairing_code.isdigit():
        raise HTTPException(401, "Invalid legacy pairing code")
    code_hash = hashlib.sha256(pairing_code.encode()).hexdigest()
    device = db.scalar(
        select(PairedDevice)
        .where(
            PairedDevice.pairing_code_hash == code_hash,
            PairedDevice.active.is_(True),
        )
        .with_for_update()
    )
    if not device or device.paired_at or not device.pairing_expires_at or (_aware(device.pairing_expires_at) or utcnow()) <= utcnow():
        raise HTTPException(status_code=401, detail="Pairing code is invalid or expired")
    raw_token = secrets.token_urlsafe(40)
    device.token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    device.pairing_code_hash = None
    device.pairing_expires_at = None
    device.paired_at = utcnow()
    device.version += 1
    db.flush()
    return device, raw_token


def device_from_token(db: Session, token: str) -> PairedDevice:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    device = db.scalar(
        select(PairedDevice).where(
            PairedDevice.token_hash == token_hash,
            PairedDevice.active.is_(True),
            PairedDevice.archived_at.is_(None),
        )
    )
    if not device:
        raise HTTPException(status_code=401, detail="Invalid paired device token")
    device.last_used_at = utcnow()
    return device


def validate_schedule_shifts(shifts: list) -> None:
    intervals: list[tuple[int, int]] = []
    week_minutes = 7 * 24 * 60
    for shift in shifts:
        start = shift.day_of_week * 1440 + shift.starts_at.hour * 60 + shift.starts_at.minute
        end = shift.day_of_week * 1440 + shift.ends_at.hour * 60 + shift.ends_at.minute
        if end <= start:
            end += 1440
        intervals.append((start, end))
        if end > week_minutes:
            intervals.append((0, end - week_minutes))
    intervals.sort()
    for (_, previous_end), (current_start, _) in zip(intervals, intervals[1:]):
        if current_start < previous_end:
            raise HTTPException(status_code=409, detail="Schedule shifts cannot overlap")


def replace_schedule_shifts(db: Session, schedule: ServiceSchedule, shifts: list) -> None:
    validate_schedule_shifts(shifts)
    db.execute(delete(ScheduleShift).where(ScheduleShift.schedule_id == schedule.id))
    db.add_all(
        [
            ScheduleShift(
                schedule_id=schedule.id,
                day_of_week=shift.day_of_week,
                starts_at=shift.starts_at,
                ends_at=shift.ends_at,
                sort_order=shift.sort_order,
            )
            for shift in shifts
        ]
    )


def serialize_schedule(db: Session, schedule: ServiceSchedule) -> dict:
    shifts = list(
        db.scalars(
            select(ScheduleShift)
            .where(ScheduleShift.schedule_id == schedule.id)
            .order_by(ScheduleShift.day_of_week, ScheduleShift.sort_order, ScheduleShift.starts_at)
        )
    )
    assignments = list(
        db.scalars(select(ScheduleAssignment).where(ScheduleAssignment.schedule_id == schedule.id))
    )
    return {
        "id": schedule.id,
        "branch_id": schedule.branch_id,
        "name": schedule.name,
        "kind": schedule.kind,
        "active": schedule.active,
        "shifts": [
            {
                "id": shift.id,
                "day_of_week": shift.day_of_week,
                "starts_at": shift.starts_at.isoformat(),
                "ends_at": shift.ends_at.isoformat(),
                "sort_order": shift.sort_order,
            }
            for shift in shifts
        ],
        "product_ids": [item.product_id for item in assignments if item.product_id is not None],
        "promotion_ids": [item.promotion_id for item in assignments if item.promotion_id is not None],
        "archived_at": schedule.archived_at,
        "version": schedule.version,
    }


def schedule_is_open(db: Session, schedule: ServiceSchedule, at: datetime | None = None) -> bool:
    if not schedule.active or schedule.archived_at is not None:
        return False
    local = (at or utcnow()).astimezone(ZoneInfo("America/Lima"))
    shifts = list(db.scalars(select(ScheduleShift).where(ScheduleShift.schedule_id == schedule.id)))
    if not shifts:
        return schedule.kind == "primary"
    minute = local.hour * 60 + local.minute
    weekday = local.weekday()
    for shift in shifts:
        start = shift.starts_at.hour * 60 + shift.starts_at.minute
        end = shift.ends_at.hour * 60 + shift.ends_at.minute
        if end > start and shift.day_of_week == weekday and start <= minute < end:
            return True
        if end <= start:
            if shift.day_of_week == weekday and minute >= start:
                return True
            if (shift.day_of_week + 1) % 7 == weekday and minute < end:
                return True
    return False


def replace_schedule_assignments(
    db: Session,
    schedule: ServiceSchedule,
    product_ids: list[int],
    promotion_ids: list[int],
) -> None:
    products = list(
        db.scalars(
            select(Product).where(
                Product.id.in_(set(product_ids)),
                Product.business_id == schedule.business_id,
                Product.branch_id == schedule.branch_id,
            )
        )
    )
    promotions = list(
        db.scalars(
            select(Promotion).where(
                Promotion.id.in_(set(promotion_ids)),
                Promotion.business_id == schedule.business_id,
                Promotion.branch_id == schedule.branch_id,
            )
        )
    )
    if len(products) != len(set(product_ids)) or len(promotions) != len(set(promotion_ids)):
        raise HTTPException(status_code=422, detail="Schedule assignments must belong to the same branch")
    db.execute(delete(ScheduleAssignment).where(ScheduleAssignment.schedule_id == schedule.id))
    db.add_all(
        [
            ScheduleAssignment(
                business_id=schedule.business_id,
                branch_id=schedule.branch_id,
                schedule_id=schedule.id,
                product_id=product_id,
            )
            for product_id in sorted(set(product_ids))
        ]
        + [
            ScheduleAssignment(
                business_id=schedule.business_id,
                branch_id=schedule.branch_id,
                schedule_id=schedule.id,
                promotion_id=promotion_id,
            )
            for promotion_id in sorted(set(promotion_ids))
        ]
    )


def archive_branch(db: Session, branch: Branch) -> None:
    active_branches = db.scalar(
        select(func.count(Branch.id)).where(
            Branch.business_id == branch.business_id,
            Branch.active.is_(True),
            Branch.archived_at.is_(None),
        )
    ) or 0
    if active_branches <= 1:
        raise HTTPException(status_code=409, detail="A business must keep at least one active branch")
    blockers = {
        "open_orders": db.scalar(
            select(func.count(Order.id)).where(
                Order.branch_id == branch.id,
                Order.status.not_in(["closed", "cancelled", "delivered"]),
            )
        ) or 0,
        "open_cash_sessions": db.scalar(
            select(func.count(CashSession.id)).where(
                CashSession.branch_id == branch.id,
                CashSession.status == "open",
            )
        ) or 0,
        "future_reservations": db.scalar(
            select(func.count(Reservation.id)).where(
                Reservation.branch_id == branch.id,
                Reservation.start_at >= utcnow(),
                Reservation.status.not_in(["cancelled", "completed"]),
            )
        ) or 0,
        "active_integrations": db.scalar(
            select(func.count(IntegrationCredential.id)).where(
                IntegrationCredential.branch_id == branch.id,
                IntegrationCredential.active.is_(True),
            )
        ) or 0,
    }
    if any(blockers.values()):
        raise HTTPException(status_code=409, detail={"message": "Branch has active operations", "blockers": blockers})
    branch.active = False
    branch.archived_at = utcnow()
    branch.version += 1


def archive_area(db: Session, area: DiningArea) -> None:
    table_count = db.scalar(
        select(func.count(RestaurantTable.id)).where(RestaurantTable.area_id == area.id)
    ) or 0
    if table_count:
        raise HTTPException(status_code=409, detail="Move or archive every table before archiving this area")
    area.archived_at = utcnow()
    area.version += 1


def archive_register(db: Session, register: CashRegister) -> None:
    if db.scalar(
        select(CashSession.id).where(
            CashSession.register_id == register.id,
            CashSession.status == "open",
        ).limit(1)
    ):
        raise HTTPException(status_code=409, detail="Close the cash period before archiving this register")
    active_count = db.scalar(
        select(func.count(CashRegister.id)).where(
            CashRegister.branch_id == register.branch_id,
            CashRegister.active.is_(True),
            CashRegister.archived_at.is_(None),
        )
    ) or 0
    if active_count <= 1:
        raise HTTPException(status_code=409, detail="A branch must keep at least one active cash register")
    if register.is_default:
        raise HTTPException(status_code=409, detail="Select another default cash register before archiving this one")
    register.active = False
    register.archived_at = utcnow()
    register.version += 1


def archive_staff_member(db: Session, member: StaffMember, current_user_id: str) -> None:
    if member.auth_user_id and member.auth_user_id == current_user_id:
        raise HTTPException(status_code=409, detail="You cannot archive your own staff profile")
    roles = set(staff_roles(db, member.id))
    if "owner" in roles:
        require_another_owner(db, member)
    member.active = False
    member.archived_at = utcnow()
    member.pin_hash = None
    member.version += 1


def get_or_create_primary_schedule(db: Session, branch: Branch) -> ServiceSchedule:
    schedule = db.scalar(
        select(ServiceSchedule).where(
            ServiceSchedule.branch_id == branch.id,
            ServiceSchedule.kind == "primary",
            ServiceSchedule.archived_at.is_(None),
        )
    )
    if schedule:
        return schedule
    schedule = ServiceSchedule(
        business_id=branch.business_id,
        branch_id=branch.id,
        name="Horario principal",
        kind="primary",
    )
    db.add(schedule)
    db.flush()
    return schedule


def kitchen_print_snapshot(order: Order, ticket: KitchenTicket) -> dict:
    return {
        "id": ticket.id,
        "sequence": ticket.sequence,
        "station": ticket.station,
        "kind": ticket.kind,
        "items": effective_ticket_items(ticket),
        "version": ticket.version,
        "order_folio": order.folio,
        "order_number": order.number,
        "context": deepcopy({key: value for key, value in (ticket.context_snapshot or {}).items()
                             if key != "_pos_printing"}),
    }


def sync_pending_kitchen_print_jobs(
    db: Session, order: Order, tickets: list[KitchenTicket], *, cancel: bool = False,
) -> None:
    statement = select(PrintJob).where(
        PrintJob.business_id == order.business_id,
        PrintJob.branch_id == order.branch_id,
        PrintJob.order_id == order.id,
        PrintJob.kitchen_ticket_id.is_not(None),
        PrintJob.status == "pending",
    )
    by_id = {ticket.id: ticket for ticket in tickets}
    if not cancel:
        if not by_id:
            return
        statement = statement.where(PrintJob.kitchen_ticket_id.in_(by_id))
    # A claimed job may already be printing; only unclaimed jobs can be changed.
    jobs = db.scalars(statement.with_for_update().execution_options(populate_existing=True))
    for job in jobs:
        if (job.payload or {}).get("_transport") == "pos-local-v1":
            # Local automatic documents are immutable. A revision invalidates the
            # pending version rather than silently replacing its claimed content.
            job.status = "cancelled"
            job.error_message = "La comanda cambio; no se enviara esta impresion."
            continue
        if cancel:
            job.status = "cancelled"
        else:
            job.payload = {
                **(job.payload or {}),
                "ticket": kitchen_print_snapshot(order, by_id[job.kitchen_ticket_id]),
            }


def enqueue_kitchen_print_jobs(
    db: Session,
    order: Order,
    tickets: list[KitchenTicket],
) -> list[PrintJob]:
    if not tickets:
        return []
    settings = db.scalar(
        select(BranchSettings).where(BranchSettings.branch_id == order.branch_id)
    )
    printer_config = settings.printer_config if settings and settings.printer_config else {}
    if (not settings or not settings.advanced_printing or not printer_config.get("auto_print_kitchen")
            or printer_config.get("automatic_printing", True) is not True):
        return []
    printers = list(
        db.scalars(
            select(PrinterDevice).where(
                PrinterDevice.business_id == order.business_id,
                PrinterDevice.branch_id == order.branch_id,
                PrinterDevice.purpose.in_(["kitchen", "bar"]),
                PrinterDevice.paired_device_id.is_not(None),
                PrinterDevice.active.is_(True),
                PrinterDevice.archived_at.is_(None),
            )
        )
    )
    jobs: list[PrintJob] = []
    for ticket in tickets:
        for printer in printers:
            key = f"kitchen-ticket:{ticket.id}:printer:{printer.id}"
            existing = db.scalar(
                select(PrintJob).where(
                    PrintJob.business_id == order.business_id,
                    PrintJob.idempotency_key == key,
                )
            )
            if existing:
                jobs.append(existing)
                continue
            job = PrintJob(
                id=str(uuid4()),
                business_id=order.business_id,
                branch_id=order.branch_id,
                paired_device_id=printer.paired_device_id,
                printer_id=printer.id,
                order_id=order.id,
                kitchen_ticket_id=ticket.id,
                job_type="kitchen_ticket",
                payload={
                    "ticket": kitchen_print_snapshot(order, ticket),
                    "copies": printer.copies,
                    "paper_width_mm": printer.paper_width_mm,
                },
                idempotency_key=key,
            )
            db.add(job)
            jobs.append(job)
    if jobs:
        db.flush()
    return jobs


def serialize_printer(printer: PrinterDevice) -> dict:
    return {
        "id": printer.id,
        "branch_id": printer.branch_id,
        "paired_device_id": printer.paired_device_id,
        "name": printer.name,
        "system_name": printer.system_name,
        "purpose": printer.purpose,
        "paper_width_mm": printer.paper_width_mm,
        "copies": printer.copies,
        "active": printer.active,
        "version": printer.version,
    }


def serialize_print_job(job: PrintJob) -> dict:
    return {
        "id": job.id,
        "branch_id": job.branch_id,
        "paired_device_id": job.paired_device_id,
        "printer_id": job.printer_id,
        "order_id": job.order_id,
        "kitchen_ticket_id": job.kitchen_ticket_id,
        "job_type": job.job_type,
        "payload": job.payload,
        "status": job.status,
        "attempts": job.attempts,
        "error_message": job.error_message,
        "created_at": job.created_at,
    }
