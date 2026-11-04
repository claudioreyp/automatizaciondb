"""Verified POS service hours for consumer agents; legacy POS defaults stay separate."""
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from datetime import datetime, timedelta

from sqlalchemy import select

from .errors import CodedHTTPException
from .models import Business, ServiceSchedule, ScheduleShift
from . import settings_service


def service_availability(db, branch):
    now = settings_service.utcnow()
    business = db.get(Business, branch.business_id)
    zone_name = business.timezone if business else None
    result = {"status": "unverified", "source": "pos_active_primary", "reason": None,
              "business_id": branch.business_id, "branch_id": branch.id,
              "checked_at": now.isoformat(), "timezone": zone_name, "local_time": None,
              "schedule_versions": [], "next_opening": None}
    try:
        if not zone_name:
            raise ValueError("missing timezone")
        zone = ZoneInfo(zone_name)
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        return {**result, "reason": "invalid_timezone"}
    result["local_time"] = now.astimezone(zone).isoformat()
    primary = list(db.scalars(select(ServiceSchedule).where(
        ServiceSchedule.business_id == branch.business_id, ServiceSchedule.branch_id == branch.id,
        ServiceSchedule.kind == "primary", ServiceSchedule.archived_at.is_(None))))
    result["schedule_versions"] = [{"id": s.id, "version": s.version, "active": s.active} for s in primary]
    active = [s for s in primary if s.active]
    if not active:
        return {**result, "reason": "inactive_schedule" if primary else "missing_schedule"}
    if len(active) != 1:
        return {**result, "reason": "ambiguous_schedule"}
    schedule = active[0]
    shifts = list(db.scalars(select(ScheduleShift).where(ScheduleShift.schedule_id == schedule.id)))
    if not shifts:
        return {**result, "reason": "empty_schedule"}
    if any(not isinstance(s.day_of_week, int) or not 0 <= s.day_of_week <= 6
           or s.starts_at is None or s.ends_at is None for s in shifts):
        return {**result, "reason": "invalid_schedule"}
    is_open = settings_service.schedule_is_open(db, schedule, at=now)
    local = now.astimezone(zone)
    candidates = [datetime.combine(local.date() + timedelta(days=offset), shift.starts_at, tzinfo=zone)
                  for offset in range(8) for shift in shifts
                  if (local.date() + timedelta(days=offset)).weekday() == shift.day_of_week]
    future = [start for start in candidates if start > local]
    return {**result, "status": "open" if is_open else "closed",
            "next_opening": min(future).isoformat() if not is_open and future else None}


def require_agent_service(db, branch):
    availability = service_availability(db, branch)
    if availability["status"] == "unverified":
        raise CodedHTTPException(409, "Service hours could not be verified", "SERVICE_UNVERIFIED")
    if availability["status"] != "open":
        raise CodedHTTPException(409, "Restaurant is currently closed", "SERVICE_CLOSED")
    return availability
