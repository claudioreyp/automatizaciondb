"""Read-only projection of the configuration maintained in the POS."""
from copy import deepcopy

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Branch, BranchSettings, DeliveryBand
from .settings_service import serialize_delivery_band, serialize_delivery_policy


def agent_context(db: Session, branch: Branch) -> dict:
    settings = db.scalar(select(BranchSettings).where(
        BranchSettings.business_id == branch.business_id, BranchSettings.branch_id == branch.id,
    ))
    bands = db.scalars(select(DeliveryBand).where(
        DeliveryBand.business_id == branch.business_id, DeliveryBand.branch_id == branch.id,
        DeliveryBand.active.is_(True), DeliveryBand.archived_at.is_(None),
    ).order_by(DeliveryBand.sort_order, DeliveryBand.minimum_km, DeliveryBand.id))
    mode = settings.delivery_mode if settings else "fixed"
    enabled = settings.pos_delivery if settings else branch.delivery_enabled
    fixed_fee = float(settings.fixed_delivery_fee or 0) if settings else float(branch.delivery_fee or 0)
    variable = mode in {"distance", "bands", "neighborhoods"}
    status = ("disabled" if not enabled else "pending_quote" if mode == "quote"
              else "destination_required" if variable else "configured")
    fee = (0.0 if mode == "free" else fixed_fee) if enabled and mode in {"fixed", "free"} else None

    def optional_number(field):
        value = getattr(settings, field, None)
        return float(value) if value is not None else None

    return {
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
            "yape": {
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
