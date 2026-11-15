"""Bounded superadmin reporting over the existing meter; never mutates it."""
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timedelta, timezone as datetime_timezone
from threading import BoundedSemaphore, RLock
from time import monotonic
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import String, and_, case, cast, distinct, func, literal, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from .auth import AuthContext, require_roles
from .database import get_db
from .models import AgentModelUsage as Usage, Branch, Business

router = APIRouter(prefix="/api/v1/admin", tags=["superadmin"])
UTC = datetime_timezone.utc
_CACHE = OrderedDict()
_CACHE_LOCK = RLock()
_REPORT_SLOTS = BoundedSemaphore(2)
_CACHE_SECONDS = 30
_CACHE_LIMIT = 128
_MAX_GROUPS = 4000
_MAX_RESULT_BUCKETS = 6000
_TIMEOUT_SECONDS = 5


def _utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _iso(value):
    return _utc(value).isoformat().replace("+00:00", "Z")


def _sqlite_bucket(value, zone, interval):
    when = _utc(datetime.fromisoformat(value)).astimezone(ZoneInfo(zone))
    floor = when.replace(minute=0, second=0, microsecond=0)
    if interval == "day":
        floor = floor.replace(hour=0, fold=0)
    return _iso(floor)


def _bucket_value(value):
    return _utc(datetime.fromisoformat(value)) if isinstance(value, str) else _utc(value)


def _bucket_end(value, zone, interval):
    if interval == "hour":
        return value + timedelta(hours=1)
    local = value.astimezone(zone)
    return (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0, fold=0).astimezone(UTC)


def _metrics():
    known = and_(Usage.status == "settled", Usage.usage_known.is_(True))
    unknown_settled = and_(Usage.status == "settled", Usage.usage_known.is_not(True))

    def count(condition):
        return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)

    def total(column, condition):
        return func.coalesce(func.sum(case((condition, func.coalesce(column, 0)), else_=0)), 0)

    # A length prefix makes the compound identity unambiguous on both dialects.
    turn = cast(func.length(Usage.conversation_id), String) + literal(":") + Usage.conversation_id + Usage.message_id
    components = {
        "known_input_tokens": Usage.input_tokens,
        "known_cache_read_input_tokens": Usage.cache_read_input_tokens,
        "known_cache_creation_input_tokens": Usage.cache_creation_input_tokens,
        "known_output_tokens": Usage.output_tokens,
    }
    values = {
        "attempts": func.count(),
        "settled_known_calls": count(known),
        "settled_unknown_calls": count(unknown_settled),
        "uncertain_calls": count(Usage.status == "uncertain"),
        "reserved_calls": count(Usage.status == "reserved"),
        "denied_calls": count(Usage.status == "denied"),
        "model_turns": func.count(distinct(turn)),
        **{key: total(column, known) for key, column in components.items()},
        "known_total_tokens": total(Usage.input_tokens + Usage.cache_read_input_tokens
            + Usage.cache_creation_input_tokens + Usage.output_tokens, known),
        "uncertain_reserved_tokens": total(Usage.reserved_tokens, Usage.status == "uncertain"),
        "open_reserved_tokens": total(Usage.reserved_tokens, Usage.status == "reserved"),
        "settled_unknown_reserved_tokens": total(Usage.reserved_tokens, unknown_settled),
        "quota_accounted_tokens": func.coalesce(func.sum(Usage.charged_tokens), 0),
    }
    return [expression.label(key) for key, expression in values.items()]


def _metric_values(row):
    return {expression.name: int(row[expression.name] or 0) for expression in _metrics()}


def _pricing_columns():
    input_total = Usage.input_tokens + Usage.cache_read_input_tokens + Usage.cache_creation_input_tokens
    band = case((input_total > 272000, "over_272000"), else_="up_to_272000").label("input_band")
    return [Usage.model_id.label("model_id"), band, func.count().label("known_calls"),
        func.coalesce(func.sum(Usage.input_tokens), 0).label("input_tokens"),
        func.coalesce(func.sum(Usage.cache_read_input_tokens), 0).label("cache_read_input_tokens"),
        func.coalesce(func.sum(Usage.cache_creation_input_tokens), 0).label("cache_creation_input_tokens"),
        func.coalesce(func.sum(Usage.output_tokens), 0).label("output_tokens")]


def _pricing_values(row):
    return {key: row[key] if key in {"model_id", "input_band"} else int(row[key] or 0)
        for key in ("model_id", "input_band", "known_calls", "input_tokens", "cache_read_input_tokens",
            "cache_creation_input_tokens", "output_tokens")}


def _read_report(db, params):
    deadline = monotonic() + _TIMEOUT_SECONDS
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        db.execute(text("SET TRANSACTION READ ONLY"))
    elif dialect == "sqlite":
        db.connection().connection.driver_connection.create_function("admin_usage_bucket", 3, _sqlite_bucket)
    else:
        raise HTTPException(503, "Usage reporting is not supported by this database")

    def execute(statement):
        remaining_ms = int((deadline - monotonic()) * 1000)
        if remaining_ms <= 0:
            raise HTTPException(503, "Usage report exceeded its time budget; select a shorter period")
        if dialect == "postgresql":
            db.execute(text("SELECT set_config('statement_timeout', :timeout, true)"), {"timeout": str(remaining_ms)})
        result = db.execute(statement)
        return result

    business_id, branch_id = params["business_id"], params["branch_id"]
    if business_id is not None and execute(select(Business.id).where(Business.id == business_id)).scalar() is None:
        raise HTTPException(404, "Business not found")
    if branch_id is not None:
        branch_business = execute(select(Branch.business_id).where(Branch.id == branch_id)).scalar()
        if branch_business is None:
            raise HTTPException(404, "Branch not found")
        if business_id is not None and branch_business != business_id:
            raise HTTPException(422, "Branch does not belong to this business")
        business_id = branch_business

    start, end = params["from"], params["to"]
    utc_day_start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    utc_day_end = end.replace(hour=0, minute=0, second=0, microsecond=0)
    if utc_day_end < end:
        utc_day_end += timedelta(days=1)
    # Each existing business/window index can serve the bounded UTC window.
    businesses = [business_id] if business_id is not None else list(execute(select(Business.id).limit(_MAX_GROUPS + 1)).scalars())
    if len(businesses) > _MAX_GROUPS:
        raise HTTPException(503, "Too many businesses; select a specific business")
    filters = [Usage.business_id.in_(businesses), Usage.window_start >= utc_day_start,
        Usage.window_start < utc_day_end, Usage.created_at >= start, Usage.created_at < end]
    if branch_id is not None:
        filters.append(Usage.branch_id == branch_id)
    available_models = list(execute(select(distinct(Usage.model_id)).where(*filters)
        .order_by(Usage.model_id).limit(_MAX_GROUPS + 1)).scalars())
    if len(available_models) > _MAX_GROUPS:
        raise HTTPException(503, "Too many model groups; select a narrower report")
    if params["model_id"] is not None:
        filters.append(Usage.model_id == params["model_id"])
    known = [*filters, Usage.status == "settled", Usage.usage_known.is_(True)]
    pricing = _pricing_columns()
    pricing_groups = pricing[:2]
    bucket = (func.date_trunc(params["interval"], Usage.created_at, params["timezone"])
        if dialect == "postgresql" else func.admin_usage_bucket(Usage.created_at, params["timezone"], params["interval"]))
    bucket = bucket.label("bucket")

    totals = _metric_values(execute(select(*_metrics()).select_from(Usage).where(*filters)).mappings().one())
    global_pricing = [_pricing_values(row) for row in execute(select(*pricing).where(*known)
        .group_by(*pricing_groups).order_by(*pricing_groups)).mappings()]
    series_rows = execute(select(bucket, *_metrics()).where(*filters).group_by(bucket).order_by(bucket)).mappings().all()
    series_pricing = execute(select(bucket, *pricing).where(*known).group_by(bucket, *pricing_groups)
        .order_by(bucket, *pricing_groups).limit(_MAX_RESULT_BUCKETS + 1)).mappings().all()
    if len(series_pricing) > _MAX_RESULT_BUCKETS:
        raise HTTPException(503, "Too many pricing groups; select a narrower report")
    series_prices = {}
    for row in series_pricing:
        series_prices.setdefault(_iso(_bucket_value(row["bucket"])), []).append(_pricing_values(row))
    zone = ZoneInfo(params["timezone"])
    series = []
    for row in series_rows:
        value = _bucket_value(row["bucket"])
        key = _iso(value)
        series.append({"bucket_start": key, "bucket_end": _iso(_bucket_end(value, zone, params["interval"])),
            **_metric_values(row), "pricing_buckets": series_prices.get(key, [])})

    group = {"business": Usage.business_id, "branch": Usage.branch_id, "model": Usage.model_id}[params["group_by"]]
    groups = select(group.label("group_key")).where(*filters).group_by(group).subquery()
    total_rows = int(execute(select(func.count()).select_from(groups)).scalar_one())
    if total_rows > _MAX_GROUPS:
        raise HTTPException(503, "Too many report groups; select a narrower report")
    grouped = execute(select(group.label("group_key"), *_metrics()).where(*filters).group_by(group)
        .order_by(group).offset((params["page"] - 1) * params["page_size"]).limit(params["page_size"])).mappings().all()
    keys = [row["group_key"] for row in grouped]
    row_prices = {}
    if keys:
        prices = execute(select(group.label("group_key"), *pricing).where(*known, group.in_(keys))
            .group_by(group, *pricing_groups).order_by(group, *pricing_groups)
            .limit(_MAX_RESULT_BUCKETS + 1)).mappings().all()
        if len(prices) > _MAX_RESULT_BUCKETS:
            raise HTTPException(503, "Too many row pricing groups; select a narrower report")
        for row in prices:
            row_prices.setdefault(row["group_key"], []).append(_pricing_values(row))
    names = {}
    if keys and params["group_by"] == "business":
        names = {row.id: row.name for row in execute(select(Business.id, Business.name).where(Business.id.in_(keys)))}
    elif keys and params["group_by"] == "branch":
        names = {row.id: row for row in execute(select(Branch.id, Branch.name, Branch.business_id,
            Business.name.label("business_name")).join(Business, Business.id == Branch.business_id).where(Branch.id.in_(keys)))}
    rows = []
    for row in grouped:
        key = row["group_key"]
        identity = {"key": str(key)}
        if params["group_by"] == "business":
            identity.update(business_id=key, business_name=names.get(key))
        elif params["group_by"] == "branch":
            named = names.get(key)
            identity.update(branch_id=key, branch_name=named.name if named else None,
                business_id=named.business_id if named else None, business_name=named.business_name if named else None)
        else:
            identity["model_id"] = key
        rows.append({**identity, **_metric_values(row), "pricing_buckets": row_prices.get(key, [])})
    if monotonic() > deadline:
        raise HTTPException(503, "Usage report exceeded its time budget; select a shorter period")
    return {"metadata": {"from": _iso(start), "to": _iso(end), "timezone": params["timezone"],
            "interval": params["interval"], "group_by": params["group_by"], "generated_at": _iso(datetime.now(UTC)),
            "complete": True, "available_models": available_models},
        "totals": totals, "pricing_buckets": global_pricing, "series": series,
        "breakdown": {"page": params["page"], "page_size": params["page_size"], "total_rows": total_rows,
            "total_pages": (total_rows + params["page_size"] - 1) // params["page_size"], "rows": rows}}


@router.get("/agent-usage")
def admin_agent_usage(response: Response,
    from_: datetime = Query(alias="from"), to: datetime = Query(),
    timezone: str = Query(default="America/Lima", min_length=1, max_length=64),
    business_id: int | None = Query(default=None, ge=1), branch_id: int | None = Query(default=None, ge=1),
    model_id: str | None = Query(default=None, min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._:-]+$"),
    interval: Literal["hour", "day"] = "day", group_by: Literal["business", "branch", "model"] = "branch",
    page: int = Query(default=1, ge=1, le=1000000), page_size: int = Query(default=25, ge=1, le=100),
    user: AuthContext = Depends(require_roles("superadmin")), db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    if from_.tzinfo is None or to.tzinfo is None:
        raise HTTPException(422, "from and to must include a UTC offset")
    start, end = _utc(from_), _utc(to)
    if not start < end or end - start > timedelta(days=31) or interval == "hour" and end - start > timedelta(days=7):
        raise HTTPException(422, "Choose a positive range up to 31 days (7 days for hourly reports)")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(422, "Unknown IANA timezone")
    params = {"from": start, "to": end, "timezone": timezone, "business_id": business_id,
        "branch_id": branch_id, "model_id": model_id, "interval": interval, "group_by": group_by,
        "page": page, "page_size": page_size}
    key = (id(db.get_bind()), user.user_id, *params.values())
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and monotonic() < cached[0]:
            _CACHE.move_to_end(key)
            return deepcopy(cached[1])
        _CACHE.pop(key, None)
    if not _REPORT_SLOTS.acquire(blocking=False):
        raise HTTPException(503, "Usage reporting is busy; retry shortly", headers={"Retry-After": "5"})
    try:
        with Session(bind=db.get_bind(), autoflush=False) as report_db:
            try:
                result = _read_report(report_db, params)
            except DBAPIError as exc:
                sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
                if sqlstate == "57014":
                    raise HTTPException(503, "Usage report exceeded its time budget; select a shorter period") from None
                raise HTTPException(503, "Usage report could not be read; retry shortly") from None
            finally:
                report_db.rollback()
    finally:
        _REPORT_SLOTS.release()
    with _CACHE_LOCK:
        _CACHE[key] = (monotonic() + _CACHE_SECONDS, deepcopy(result))
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_LIMIT:
            _CACHE.popitem(last=False)
    return result
