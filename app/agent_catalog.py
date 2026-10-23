"""Complete public catalog metadata. No inventory quantities or recipes."""
from datetime import date
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .agent_checkout import fingerprint
from .models import Business, BranchSettings, ScheduleAssignment, ServiceSchedule, utcnow
from .settings_service import schedule_is_open


def promotion_application_rules(promotion):
    """Describe the existing server calculator; this does not calculate a price."""
    rules = {"automatic": True, "pricing_authority": "server_preview",
        "stacking": "best_discount_per_order_line", "delivery_fee_discounted": False}
    if promotion["promotion_type"] == "buy_x_pay_y":
        rules.update(group_by=["product_id", "variant_name", "unit_price"],
            whole_sets_only=True, mix_products=False, mix_variants=False,
            discount_basis="unit_price_of_free_units")
    else:
        rules.update(discount_basis="line_total" if promotion.get("discount_type") == "percentage" else "quantity",
            discount_capped_at_line_total=True)
    return rules


def public_promotion(promotion, products, allowed_channels):
    channel_modes = {"digital_takeaway": "takeaway", "digital_delivery": "delivery", "digital_tables": "counter"}
    promotion_channels = set(promotion.get("service_channels") or channel_modes)
    targets = set(promotion.get("target_ids") or [])
    eligible = []
    for product in products:
        target = product["id"] if promotion.get("target_scope") == "products" else product.get("category_id")
        if target not in targets or not product.get("available"):
            continue
        channels = allowed_channels & promotion_channels & set(product.get("service_channels") or channel_modes)
        modes = [mode for channel, mode in channel_modes.items() if channel in channels]
        if modes:
            eligible.append({"product_id": product["id"], "order_modes": modes})
    return {**promotion, "eligible_product_ids": [p["product_id"] for p in eligible],
        "eligible_order_modes": [mode for mode in channel_modes.values() if any(mode in p["order_modes"] for p in eligible)],
        "eligible_products": eligible, "application_rules": promotion_application_rules(promotion)}


def complete_catalog(db, branch, payload):
    business = db.get(Business, branch.business_id)
    settings = db.scalar(select(BranchSettings).where(BranchSettings.branch_id == branch.id,
        BranchSettings.business_id == branch.business_id))
    allowed_channels = {name for name, enabled in {
        "digital_takeaway": settings.digital_takeaway if settings else branch.takeaway_enabled,
        "digital_delivery": settings.digital_delivery if settings else branch.delivery_enabled,
        "digital_tables": settings.digital_tables if settings else False,
    }.items() if enabled}
    schedules = list(db.scalars(select(ServiceSchedule).where(ServiceSchedule.branch_id == branch.id,
        ServiceSchedule.business_id == branch.business_id, ServiceSchedule.active.is_(True),
        ServiceSchedule.archived_at.is_(None))))
    open_schedules = {s.id: schedule_is_open(db, s) for s in schedules}
    primary_open = all(open_schedules[s.id] for s in schedules if s.kind == "primary")
    assignments = list(db.scalars(select(ScheduleAssignment).where(
        ScheduleAssignment.schedule_id.in_(list(open_schedules))))) if schedules else []
    for product in payload["products"]:
        schedule_open = primary_open and all(open_schedules[a.schedule_id] for a in assignments if a.product_id == product["id"])
        product["available"] = product["available"] and schedule_open
        product["schedule_available"] = schedule_open
    local = utcnow().astimezone(ZoneInfo(business.timezone))
    def active(promotion):
        starts = promotion.get("starts_on")
        ends = promotion.get("ends_on")
        starts = date.fromisoformat(starts) if isinstance(starts, str) else starts
        ends = date.fromisoformat(ends) if isinstance(ends, str) else ends
        return bool(promotion.get("active") and not promotion.get("archived_at")
            and (not promotion.get("service_channels") or allowed_channels.intersection(promotion["service_channels"]))
            and (starts is None or starts <= local.date()) and (ends is None or ends >= local.date())
            and (not promotion.get("weekdays") or local.weekday() in promotion["weekdays"])
            and all(open_schedules[a.schedule_id] for a in assignments if a.promotion_id == promotion["id"]))
    payload["promotions"] = [public_promotion(p, payload["products"], allowed_channels)
        for p in payload["promotions"] if active(p)]
    payload.update(currency=business.currency, timezone=business.timezone,
        complete=True, next_cursor=None, product_count=len(payload["products"]),
        schema_version=1, open_now=primary_open)
    payload["catalog_version"] = fingerprint(payload)
    return payload
