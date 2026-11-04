"""Read-only analytics from persisted order and payment amounts."""

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .models import Order, Payment

LIMA = ZoneInfo("America/Lima")
VALID_STATUSES = {"confirmed", "sent_to_kitchen", "preparing", "ready", "dispatched", "delivered", "closed"}
ZERO = Decimal("0")


def amount(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def local_time(value: datetime) -> datetime:
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(LIMA)


def source_label(source: str) -> str:
    if source in {"agent", "integration", "n8n", "whatsapp", "whatsapp_agent"}:
        return "WhatsApp"
    if source in {"online", "public_store", "public_portal"}:
        return "Menú digital"
    return "Punto de venta" if source in {"pos", "staff", "counter"} else "Otros"


def service_label(channel: str) -> str:
    if channel in {"takeaway", "pickup"}:
        return "Para llevar / Para recoger"
    if channel in {"dine_in", "table", "counter"}:
        return "En el local"
    return "Domicilio" if channel == "delivery" else "Otros"


def line_net_amounts(order: Order, items: list) -> list[Decimal]:
    gross = [max(ZERO, amount(item.line_total)) for item in items]
    total_discount = min(sum(gross, ZERO), max(ZERO, amount(order.discount)))
    discounts = [min(value, max(ZERO, amount(item.promotion_discount))) for item, value in zip(items, gross)]
    if sum(discounts, ZERO) > total_discount:
        discounts = [ZERO for _ in items]
    remaining = total_discount - sum(discounts, ZERO)
    weights = [value - discount for value, discount in zip(gross, discounts)]
    weight_total = sum(weights, ZERO)
    # Allocate remaining cents deterministically; the last weighted line closes the sum.
    for index, weight in enumerate(weights):
        share = min(weight, amount(remaining * weight / weight_total)) if weight_total else ZERO
        discounts[index] += share
        remaining -= share
        weight_total -= weight
    return [value - discount for value, discount in zip(gross, discounts)]


def dashboard_report(db: Session, business_id: int, branch_id: int, date_from: date, date_to: date, *, now: datetime | None = None) -> dict:
    current = local_time(now or datetime.now(timezone.utc))
    if date_from > date_to or date_to > current.date():
        raise HTTPException(422, "Selecciona un rango válido sin fechas futuras.")
    if (date_to - date_from).days >= 366:
        raise HTTPException(422, "Selecciona un rango de hasta 366 días.")
    start = datetime.combine(date_from, time.min, LIMA).astimezone(timezone.utc)
    end = datetime.combine(date_to + timedelta(days=1), time.min, LIMA).astimezone(timezone.utc)
    orders = list(db.scalars(select(Order).where(
        Order.business_id == business_id, Order.branch_id == branch_id,
        Order.created_at >= start, Order.created_at < end, Order.status.in_(VALID_STATUSES),
    ).options(selectinload(Order.items)).order_by(Order.created_at, Order.id)))
    payments = list(db.scalars(select(Payment).join(Order).where(
        Order.business_id == business_id, Order.branch_id == branch_id,
        Payment.status == "confirmed", Payment.received_at >= start, Payment.received_at < end,
    )))
    hourly = date_from == date_to
    count = (current.hour + 1 if date_to == current.date() else 24) if hourly else (date_to - date_from).days + 1
    series = [{"key": f"{hour:02}:00" if hourly else (date_from + timedelta(days=hour)).isoformat(),
               "sales": ZERO, "orders": 0, "shipping": ZERO} for hour in range(count)]
    channels = defaultdict(lambda: {"sales": ZERO, "orders": 0})
    services = defaultdict(lambda: ZERO)
    products = {}
    sales = shipping = ZERO
    daily = defaultdict(lambda: ZERO)
    for order in orders:
        created = local_time(order.created_at)
        net = max(ZERO, amount(order.subtotal) - amount(order.discount))
        fee = amount(order.delivery_fee)
        sales += net
        shipping += fee
        daily[created.date()] += net
        index = created.hour if hourly else (created.date() - date_from).days
        if index < len(series):
            series[index]["sales"] += net
            series[index]["shipping"] += fee
            series[index]["orders"] += 1
        channel = channels[source_label(order.source)]
        channel["sales"] += net
        channel["orders"] += 1
        services[service_label(order.channel)] += net
        items = [item for item in order.items if item.status not in {"cancelled", "superseded"}]
        for item, revenue in zip(items, line_net_amounts(order, items)):
            key = f"id:{item.product_id}" if item.product_id is not None else f"name:{item.product_name}"
            entry = products.setdefault(key, {"name": item.product_name, "quantity": ZERO, "sales": ZERO})
            entry["quantity"] += Decimal(str(item.quantity))
            entry["sales"] += revenue
    by_payment = defaultdict(lambda: ZERO)
    for payment in payments:
        by_payment[payment.method] += amount(payment.amount)
    weekdays = []
    if not hourly:
        for weekday, label in enumerate(["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]):
            days = [date_from + timedelta(days=offset) for offset in range(count) if (date_from + timedelta(days=offset)).weekday() == weekday]
            weekdays.append({"name": label, "sales": amount(sum((daily[day] for day in days), ZERO) / len(days)) if days else None})
    ranked = sorted(products.values(), key=lambda item: (-item["quantity"], -item["sales"], item["name"]))
    return {
        "branch_id": branch_id, "date_from": date_from, "date_to": date_to, "timezone": "America/Lima",
        "sales": sales, "orders": len(orders), "shipping": shipping,
        "average_ticket": amount(sales / len(orders)) if orders else ZERO,
        "granularity": "hour" if hourly else "day", "series": series,
        "channels": [{"name": name, **value} for name, value in channels.items()],
        "services": [{"name": name, "sales": value} for name, value in services.items()],
        "payment_methods": [{"name": name, "amount": value} for name, value in sorted(by_payment.items(), key=lambda item: -item[1])],
        "weekdays": weekdays, "top_products": ranked[:10],
        "bottom_products": sorted(products.values(), key=lambda item: (item["quantity"], item["sales"], item["name"]))[:10],
    }
