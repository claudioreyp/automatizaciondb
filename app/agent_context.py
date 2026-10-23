"""Read-only projection of the configuration maintained in the POS."""
from copy import deepcopy
import hashlib
import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Branch, BranchSettings, DeliveryBand, ServiceSchedule, Business, utcnow
from .settings_service import serialize_delivery_band, serialize_delivery_policy, serialize_schedule, schedule_is_open


def yape_revision(branch: Branch) -> str:
    """Bind public payment instructions to their private QR without exposing its path."""
    value = [branch.business_id, branch.id, branch.yape_number, branch.payment_recipient_name,
             branch.yape_qr_storage_path]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def agent_calendar(timezone_name: str | None) -> dict:
    """Server-owned restaurant day; an invalid zone never fabricates a reset date."""
    now = utcnow()
    result = {"server_time_utc": now.isoformat().replace("+00:00", "Z"),
              "timezone": timezone_name, "local_date": None, "valid_timezone": False}
    try:
        zone = ZoneInfo(timezone_name) if timezone_name else None
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        zone = None
    if zone is not None:
        result.update(local_date=now.astimezone(zone).date().isoformat(), valid_timezone=True)
    return result


def agent_context(db: Session, branch: Branch, *, restaurant_agent: bool = False) -> dict:
    settings = db.scalar(select(BranchSettings).where(
        BranchSettings.business_id == branch.business_id, BranchSettings.branch_id == branch.id,
    ))
    bands = db.scalars(select(DeliveryBand).where(
        DeliveryBand.business_id == branch.business_id, DeliveryBand.branch_id == branch.id,
        DeliveryBand.active.is_(True), DeliveryBand.archived_at.is_(None),
    ).order_by(DeliveryBand.sort_order, DeliveryBand.minimum_km, DeliveryBand.id))
    mode = settings.delivery_mode if settings else "fixed"
    enabled = settings.digital_delivery if settings else branch.delivery_enabled
    fixed_fee = float(settings.fixed_delivery_fee or 0) if settings else float(branch.delivery_fee or 0)
    variable = mode in {"distance", "bands", "neighborhoods"}
    status = ("disabled" if not enabled else "pending_quote" if mode == "quote"
              else "destination_required" if variable else "configured")
    fee = (0.0 if mode == "free" else fixed_fee) if enabled and mode in {"fixed", "free"} else None

    def optional_number(field):
        value = getattr(settings, field, None)
        return float(value) if value is not None else None

    schedules = list(db.scalars(select(ServiceSchedule).where(ServiceSchedule.branch_id == branch.id,
        ServiceSchedule.business_id == branch.business_id, ServiceSchedule.active.is_(True),
        ServiceSchedule.archived_at.is_(None))))
    business = db.get(Business, branch.business_id)
    modes = {"takeaway": bool(settings.digital_takeaway if settings else branch.takeaway_enabled),
             "delivery": bool(enabled), "counter": bool(settings.digital_tables) if settings else False}
    result = {
        "business_id": branch.business_id, "branch_id": branch.id,
        "currency": business.currency, "timezone": business.timezone,
        "calendar": agent_calendar(business.timezone),
        "configuration_version": settings.version if settings else branch.version,
        "model_usage": {"version": 1},
        "capabilities": {"order_modes": [mode for mode, value in modes.items() if value],
            "automated_payment_methods": ["cash", "yape"], "catering": "human_attention",
            "registered_product_changes": False, "registered_cancellation": False,
            "fulfillment_changes_until": "actual_dispatch_or_collection",
            "receipt_requires_human_approval": True},
        "schedules": [{**serialize_schedule(db, schedule), "open_now": schedule_is_open(db, schedule)} for schedule in schedules],
        "open_now": all(schedule_is_open(db, schedule) for schedule in schedules if schedule.kind == "primary"),
        "estimates": {"source": "pos_settings", "delivery_min_minutes": settings.delivery_min_minutes,
            "delivery_max_minutes": settings.delivery_max_minutes, "pickup_minutes": settings.pickup_minutes,
            "is_guarantee": False} if settings else None,
        "agent": {"name": branch.agent_name, "version": branch.version},
        "location": {
            "address": branch.address, "phone": branch.phone, "maps_url": branch.maps_url,
            "latitude": float(branch.latitude) if branch.latitude is not None else None,
            "longitude": float(branch.longitude) if branch.longitude is not None else None,
        },
        "payments": {
            "methods": deepcopy(settings.payment_methods) if settings else {
                channel: list(branch.accepted_payment_methods or []) for channel in ("delivery", "takeaway", "counter")
            },
            # Existing restaurant-agent policy, not availability or an order's state.
            # on_registration means the completed initial cash-confirm operation,
            # not the intermediate draft POST. Retained additions have separate gates.
            "processing": {
                "cash": {
                    "scope": "initial_order", "payment_timing": "on_fulfillment",
                    "kitchen_release": "on_registration", "evidence_required": False,
                    "human_approval_required": False,
                },
                "yape": {
                    "scope": "initial_order", "payment_timing": "before_kitchen_release",
                    "kitchen_release": "after_human_approval", "evidence_required": True,
                    "human_approval_required": True,
                },
            },
            "yape": {
                "revision": yape_revision(branch),
                "number": branch.yape_number, "recipient_name": branch.payment_recipient_name,
                "qr_configured": bool(branch.yape_qr_storage_path),
                "qr_url": "/api/v1/integrations/context/yape-qr" if branch.yape_qr_storage_path else None,
                "qr_authentication": "bearer", "qr_scope": "menu:read",
            },
            "plin_number": branch.plin_number,
        },
        "delivery": {
            "source": "pos_settings" if settings else "legacy_branch",
            "enabled": enabled, "mode": mode, "fee": fee, "fee_status": status,
            "requires_quote": bool(enabled and (mode == "quote" or variable)),
            "requires_destination": bool(enabled and variable),
            "configuration_version": settings.version if settings else branch.version,
            "fixed_delivery_fee": fixed_fee if mode == "fixed" else None,
            "distance_base_fee": optional_number("distance_base_fee") if mode == "distance" else None,
            "distance_fee_per_km": optional_number("distance_fee_per_km") if mode == "distance" else None,
            "distance_max_km": optional_number("distance_max_km") if mode == "distance" else None,
            "minimum_order_amount": optional_number("minimum_order_amount"),
            "free_delivery_threshold": optional_number("free_delivery_threshold"),
            "policy": serialize_delivery_policy(settings) if settings else None,
            "bands": [serialize_delivery_band(band) for band in bands] if mode == "bands" else [],
        },
    }
    if restaurant_agent:
        from .agent_delivery import project_delivery
        result["delivery"] = project_delivery(result["delivery"])
        result["payments"]["methods"] = {channel: [method for method in methods if method in {"cash", "yape"}]
            for channel, methods in result["payments"]["methods"].items()}
        result["payments"].pop("plin_number", None)
        if result["estimates"]:
            # POS labels explicitly measure delivery from customer confirmation.
            # Pickup is its own estimate, never a kitchen-only duration.
            result["estimates"].update(delivery_scope="order_confirmation_to_delivery",
                pickup_scope="pickup_order", preparation_minutes=None, travel_minutes=None)
    return result
