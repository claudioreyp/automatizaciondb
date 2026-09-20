from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .auth import AuthContext, ensure_branch_scope
from .cash_service import actor_display_name as cash_actor_display_name, cash_reconciliation, resolve_payment_cash_session
from .catalog_availability import product_selection_unavailable_reason
from .command_revisions import effective_ticket_items, retired_modifier_units
from .order_folios import reserve_order_folio
from .pos_printing import (
    create_checkout_print_job, invalidate_checkout_print_jobs, local_printer_config,
    prepare_pos_print_intent, print_actor_name, table_print_context,
)
from .security_audit import item_audit_snapshot, order_identity_snapshot
from .errors import CodedHTTPException
from .models import (
    AuditEvent,
    Branch,
    BranchSettings,
    Business,
    CashRegister,
    CashSession,
    ComboItem,
    Customer,
    IdempotencyRecord,
    InventoryItem,
    IntegrationEvent,
    KitchenTicket,
    Order,
    OrderItem,
    Payment,
    PaymentAllocation,
    PaymentEvidence,
    Modifier,
    ModifierGroup,
    Product,
    ProductModifierGroup,
    ProductVariant,
    Promotion,
    RecipeItem,
    Reservation,
    ReservationTable,
    RestaurantTable,
    ScheduleAssignment,
    ServiceSchedule,
    StockMovement,
    utcnow,
)
from .schemas import (
    OrderCreate,
    OrderItemRevisionOperation,
    OrderLineInput,
    PaymentCreate,
    ReservationCreate,
)
from .settings_service import (
    apply_delivery_quote_to_order,
    enqueue_kitchen_print_jobs,
    schedule_is_open,
    sync_pending_kitchen_print_jobs,
)


TWOPLACES = Decimal("0.01")
INACTIVE_ORDER_ITEM_STATUSES = {"cancelled", "superseded"}


def money(value: Decimal | int | float | str | None) -> Decimal:
    return Decimal(str(value or 0)).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def decimal_json(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def active_order_items(order: Order) -> list[OrderItem]:
    return [
        item
        for item in order.items
        if item.status not in INACTIVE_ORDER_ITEM_STATUSES
    ]


def audit(
    db: Session,
    user: AuthContext | None,
    action: str,
    entity_type: str,
    entity_id: int | str | None,
    business_id: int | None,
    payload: dict | None = None,
    *,
    branch_id: int | None = None,
    actor_display_name: str | None = None,
) -> None:
    db.add(
        AuditEvent(
            business_id=business_id,
            branch_id=branch_id,
            actor_id=user.user_id if user else "system",
            actor_display_name=actor_display_name or (user.email if user else "Sistema"),
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id is not None else None,
            payload=jsonable_encoder(payload or {}),
        )
    )


def get_idempotent_response(
    db: Session,
    scope: str,
    key: str | None,
    business_id: int | None,
) -> dict | None:
    if not key:
        return None
    record = db.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.scope == scope,
            IdempotencyRecord.idempotency_key == key,
            IdempotencyRecord.business_id == business_id,
        )
    )
    return record.response_body if record else None


def save_idempotent_response(
    db: Session,
    scope: str,
    key: str | None,
    business_id: int | None,
    response: dict,
) -> None:
    if not key:
        return
    db.add(
        IdempotencyRecord(
            business_id=business_id,
            scope=scope,
            idempotency_key=key,
            response_body=jsonable_encoder(response),
            expires_at=utcnow() + timedelta(days=7),
        )
    )


def serialize_order(order: Order) -> dict:
    return {
        "id": order.id,
        "business_id": order.business_id,
        "branch_id": order.branch_id,
        "number": order.number,
        "folio": order.folio,
        "channel": order.channel,
        "source": order.source,
        "status": order.status,
        "payment_status": order.payment_status,
        "payment_method": order.payment_method,
        "table_id": order.table_id,
        "customer_id": order.customer_id,
        "customer_name": order.customer_name,
        "customer_phone": order.customer_phone,
        "delivery_address": order.delivery_address,
        "subtotal": float(order.subtotal or 0),
        "discount": float(order.discount or 0),
        "manual_discount": float(order.manual_discount or 0),
        "promotion_discount": float(order.promotion_discount or 0),
        "applied_promotions": order.applied_promotions or [],
        "delivery_fee": float(order.delivery_fee or 0),
        "delivery_quote_id": order.delivery_quote_id,
        "delivery_fee_status": order.delivery_fee_status,
        "known_total": float(order.total or 0),
        "final_total": None if order.delivery_fee_status == "pending_quote" else float(order.total or 0),
        "total": float(order.total or 0),
        "notes": order.notes,
        "external_reference": order.external_reference,
        "whatsapp_chat_id": order.whatsapp_chat_id,
        "whatsapp_message_id": order.whatsapp_message_id,
        "submitted_at": order.submitted_at,
        "version": order.version,
        "sent_to_kitchen_at": order.sent_to_kitchen_at,
        "checkout_started_at": order.checkout_started_at,
        "table_released_at": order.table_released_at,
        "closed_at": order.closed_at,
        "created_at": order.created_at,
        "updated_at": order.updated_at,
        "items": [
            {
                "id": item.id,
                "product_id": item.product_id,
                "name": item.product_name,
                "variant_name": item.variant_name,
                "quantity": float(item.quantity),
                "unit_price": float(item.unit_price),
                "modifiers": item.modifiers,
                "notes": item.notes,
                "status": item.status,
                "replaces_item_id": item.replaces_item_id,
                "cancellation_reason": item.cancellation_reason,
                "line_total": float(item.line_total),
                "promotion_discount": float(item.promotion_discount or 0),
                "promotion_snapshot": item.promotion_snapshot,
            }
            for item in order.items
        ],
    }


def load_order(db: Session, order_id: int, *, for_update: bool = False) -> Order:
    statement = (
        select(Order)
        .where(Order.id == order_id)
        .options(selectinload(Order.items))
    )
    if for_update:
        statement = statement.with_for_update()
    order = db.scalar(statement)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


def assert_version(current_version: int, expected_version: int | None) -> None:
    if expected_version is not None and current_version != expected_version:
        raise HTTPException(status_code=409, detail="Record was modified by another user")


def promotion_is_active(
    promotion: Promotion,
    *,
    service_channel: str | None = None,
    now: datetime | None = None,
) -> bool:
    if not promotion.active or promotion.archived_at is not None:
        return False
    local_now = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/Lima"))
    current_date = local_now.date()
    if promotion.starts_on and current_date < promotion.starts_on:
        return False
    if promotion.ends_on and current_date > promotion.ends_on:
        return False
    if promotion.weekdays and local_now.weekday() not in promotion.weekdays:
        return False
    if service_channel and promotion.service_channels and service_channel not in promotion.service_channels:
        return False
    return True


DIGITAL_ORDER_SOURCES = {
    "agent",
    "integration",
    "n8n",
    "online",
    "public_store",
    "whatsapp",
    "whatsapp_agent",
}


def service_channel_for_order(channel: str, source: str | None) -> str:
    is_digital = (source or "").strip().lower() in DIGITAL_ORDER_SOURCES
    if channel in {"delivery"}:
        return "digital_delivery" if is_digital else "pos_delivery"
    if channel in {"takeaway", "pickup"}:
        return "digital_takeaway" if is_digital else "pos_takeaway"
    if channel in {"dine_in", "table"}:
        return "digital_tables" if is_digital else "pos_tables"
    if channel in {"counter"}:
        return "digital_tables" if is_digital else "pos_counter"
    if channel in {"online", "whatsapp"}:
        return "digital_takeaway"
    return "pos_counter"


def order_service_channel(order: Order) -> str:
    return service_channel_for_order(order.channel, order.source)


def _promotion_targets_product(promotion: Promotion, product: Product | None) -> bool:
    if product is None:
        return False
    if promotion.target_scope == "products":
        return any(target.product_id == product.id for target in promotion.targets)
    return any(target.category_id == product.category_id for target in promotion.targets)


def apply_order_promotions(db: Session, order: Order) -> Decimal:
    for item in order.items:
        item.promotion_discount = Decimal("0")
        item.promotion_snapshot = None
    order.applied_promotions = []
    active_items = active_order_items(order)
    if not active_items:
        order.promotion_discount = Decimal("0")
        return Decimal("0")

    promotions = list(
        db.scalars(
            select(Promotion)
            .where(
                Promotion.business_id == order.business_id,
                Promotion.branch_id == order.branch_id,
                Promotion.active.is_(True),
                Promotion.archived_at.is_(None),
            )
            .options(selectinload(Promotion.targets))
        )
    )
    channel = order_service_channel(order)
    promotions = [
        promotion
        for promotion in promotions
        if promotion_is_active(promotion, service_channel=channel)
    ]
    if not promotions:
        order.promotion_discount = Decimal("0")
        return Decimal("0")

    product_ids = {item.product_id for item in active_items if item.product_id is not None}
    products = {
        product.id: product
        for product in db.scalars(select(Product).where(Product.id.in_(product_ids)))
    } if product_ids else {}
    best_by_item: dict[int, tuple[Decimal, Promotion]] = {}

    for promotion in promotions:
        eligible = [
            item
            for item in active_items
            if _promotion_targets_product(promotion, products.get(item.product_id))
        ]
        if not eligible:
            continue
        if promotion.promotion_type == "product_discount":
            for item in eligible:
                gross = money(item.line_total)
                if promotion.discount_type == "percentage":
                    candidate = money(gross * money(promotion.discount_value) / Decimal("100"))
                else:
                    candidate = money(money(promotion.discount_value) * Decimal(str(item.quantity)))
                candidate = min(candidate, gross)
                current = best_by_item.get(id(item))
                if candidate > 0 and (current is None or candidate > current[0]):
                    best_by_item[id(item)] = (candidate, promotion)
            continue

        receive = int(promotion.receive_quantity or 0)
        pay = int(promotion.pay_quantity or 0)
        if receive <= pay or pay < 1:
            continue
        grouped: dict[tuple[int | None, str | None, Decimal], list[OrderItem]] = {}
        for item in eligible:
            key = (item.product_id, item.variant_name, money(item.unit_price))
            grouped.setdefault(key, []).append(item)
        for (_, _, unit_price), grouped_items in grouped.items():
            total_quantity = sum((Decimal(str(item.quantity)) for item in grouped_items), Decimal("0"))
            free_quantity = Decimal(int(total_quantity // receive) * (receive - pay))
            for item in grouped_items:
                if free_quantity <= 0:
                    break
                discounted_quantity = min(Decimal(str(item.quantity)), free_quantity)
                candidate = money(discounted_quantity * unit_price)
                free_quantity -= discounted_quantity
                current = best_by_item.get(id(item))
                if candidate > 0 and (current is None or candidate > current[0]):
                    best_by_item[id(item)] = (candidate, promotion)

    applied: dict[int, dict] = {}
    total_discount = Decimal("0")
    for item in active_items:
        result = best_by_item.get(id(item))
        if not result:
            continue
        discount, promotion = result
        snapshot = {
            "id": promotion.id,
            "name": promotion.name,
            "promotion_type": promotion.promotion_type,
            "discount": float(discount),
        }
        item.promotion_discount = discount
        item.promotion_snapshot = snapshot
        total_discount += discount
        if promotion.id not in applied:
            applied[promotion.id] = {
                "id": promotion.id,
                "name": promotion.name,
                "promotion_type": promotion.promotion_type,
                "discount": 0.0,
            }
        applied[promotion.id]["discount"] = float(
            money(Decimal(str(applied[promotion.id]["discount"])) + discount)
        )
    order.applied_promotions = list(applied.values())
    order.promotion_discount = money(total_discount)
    return order.promotion_discount


def recalculate_order(db: Session, order: Order) -> None:
    subtotal = sum(
        (money(item.line_total) for item in active_order_items(order)),
        Decimal("0"),
    )
    order.subtotal = money(subtotal)
    promotion_discount = apply_order_promotions(db, order)
    manual_discount = min(money(order.manual_discount), order.subtotal)
    order.manual_discount = manual_discount
    order.discount = max(manual_discount, promotion_discount)
    order.total = max(
        money(order.subtotal - money(order.discount) + money(order.delivery_fee)),
        Decimal("0"),
    )


def build_order_item(
    db: Session,
    business_id: int,
    branch_id: int,
    line: OrderLineInput,
    order_channel: str | None = None,
    order_source: str | None = None,
    *,
    allow_catalog_name_lookup: bool = False,
) -> OrderItem:
    product = None
    if line.product_id is not None:
        product = db.scalar(
            select(Product).where(
                Product.id == line.product_id,
                Product.business_id == business_id,
                Product.branch_id == branch_id,
                Product.available.is_(True),
            )
        )
        if not product:
            raise HTTPException(status_code=422, detail=f"Product {line.product_id} is unavailable")
    elif allow_catalog_name_lookup and line.name:
        products = list(
            db.scalars(
                select(Product).where(
                    Product.business_id == business_id,
                    Product.branch_id == branch_id,
                    Product.available.is_(True),
                    func.lower(func.trim(Product.name)) == line.name.strip().lower(),
                )
            )
        )
        if len(products) == 1:
            product = products[0]

    if product is None and (order_source or "").strip().lower() in DIGITAL_ORDER_SOURCES:
        raise CodedHTTPException(
            422,
            "Integration order items must reference an available catalog product",
            "CATALOG_PRODUCT_REQUIRED",
        )

    if product is not None:
        unavailable_reason = product_selection_unavailable_reason(db, product)
        if unavailable_reason:
            raise CodedHTTPException(422, unavailable_reason, "PRODUCT_OPTIONS_UNAVAILABLE")
        required_channel = (
            service_channel_for_order(order_channel, order_source)
            if order_channel
            else None
        )
        if required_channel and required_channel not in (product.service_channels or []):
            raise CodedHTTPException(
                422,
                f"Product {product.name} is unavailable for this order channel",
                "PRODUCT_UNAVAILABLE_FOR_CHANNEL",
            )
    if product is None and (not line.name or line.unit_price is None):
        raise HTTPException(status_code=422, detail="Ad-hoc order items require name and unit_price")

    variant_name = line.variant_name
    variant_delta = Decimal("0")
    active_variants = (
        list(
            db.scalars(
                select(ProductVariant).where(
                    ProductVariant.product_id == product.id,
                    ProductVariant.active.is_(True),
                )
            )
        )
        if product
        else []
    )
    if product and active_variants and not variant_name:
        raise HTTPException(status_code=422, detail=f"Choose a variant for {product.name}")
    if product and variant_name:
        variant = db.scalar(
            select(ProductVariant).where(
                ProductVariant.product_id == product.id,
                func.lower(ProductVariant.name) == variant_name.strip().lower(),
                ProductVariant.active.is_(True),
                ProductVariant.available.is_(True),
            )
        )
        if not variant:
            raise HTTPException(status_code=422, detail=f"Variant {variant_name} is unavailable")
        variant_name = variant.name
        variant_delta = money(variant.price_delta)

    # Catalog products always use server-side prices. Integration clients may
    # identify a modifier, but cannot supply or override its price.
    base_price = money(product.price + variant_delta) if product else money(line.unit_price)
    sanitized_modifiers: list[dict] = []
    modifier_total = Decimal("0")
    linked_groups = (
        list(
            db.scalars(
                select(ModifierGroup)
                .join(ProductModifierGroup, ProductModifierGroup.group_id == ModifierGroup.id)
                .where(ProductModifierGroup.product_id == product.id)
            )
        )
        if product
        else []
    )
    linked_groups_by_id = {group.id: group for group in linked_groups}
    selections_by_group: dict[int, int] = {}
    selections_by_modifier: dict[int, int] = {}
    for selection in (selected for selected in line.modifiers for _ in range(selected.quantity)):
        if not product:
            sanitized_modifiers.append(selection.model_dump(mode="json"))
            modifier_total += money(selection.price_delta)
            continue

        statement = (
            select(Modifier)
            .join(ProductModifierGroup, ProductModifierGroup.group_id == Modifier.group_id)
            .where(
                ProductModifierGroup.product_id == product.id,
                Modifier.active.is_(True),
                Modifier.available.is_(True),
            )
        )
        if selection.modifier_id is not None:
            statement = statement.where(Modifier.id == selection.modifier_id)
        else:
            statement = statement.where(func.lower(Modifier.name) == selection.name.strip().lower())
        matches = list(db.scalars(statement.limit(2)))
        if len(matches) != 1:
            raise HTTPException(
                status_code=422,
                detail=f"Modifier {selection.name or selection.modifier_id} is unavailable for {product.name}",
            )
        modifier = matches[0]
        modifier_group = linked_groups_by_id.get(modifier.group_id)
        modifier_count = selections_by_modifier.get(modifier.id, 0) + 1
        if modifier_count > 1 and not (modifier_group and modifier_group.allow_repeats):
            raise HTTPException(
                status_code=422,
                detail=f"Modifier {modifier.name} was selected more than once",
            )
        if (
            modifier_group
            and modifier_group.allow_repeats
            and modifier_group.max_per_option is not None
            and modifier_count > modifier_group.max_per_option
        ):
            raise HTTPException(
                status_code=422,
                detail=f"Select at most {modifier_group.max_per_option} of {modifier.name}",
            )
        selections_by_modifier[modifier.id] = modifier_count
        selections_by_group[modifier.group_id] = selections_by_group.get(modifier.group_id, 0) + 1
        modifier_total += money(modifier.price_delta)
        sanitized_modifiers.append(
            {
                "modifier_id": modifier.id,
                "name": modifier.name,
                "price_delta": float(money(modifier.price_delta)),
                "group_id": modifier.group_id,
                "group_name": modifier_group.name if modifier_group else "Personalizaciones",
            }
        )
    for group in linked_groups:
        selected_count = selections_by_group.get(group.id, 0)
        minimum = max(group.minimum, 1 if group.required else 0)
        if selected_count < minimum:
            raise HTTPException(
                status_code=422,
                detail=f"Select at least {minimum} option(s) from {group.name} for {product.name}",
            )
        if group.maximum is not None and selected_count > group.maximum:
            raise HTTPException(
                status_code=422,
                detail=f"Select at most {group.maximum} option(s) from {group.name} for {product.name}",
            )
    unit_price = money(base_price + modifier_total)
    quantity = Decimal(str(line.quantity))
    return OrderItem(
        product_id=product.id if product else None,
        product_name=product.name if product else str(line.name),
        variant_name=variant_name,
        quantity=quantity,
        unit_price=unit_price,
        modifiers=sanitized_modifiers,
        notes=line.notes,
        line_total=money(unit_price * quantity),
    )


def create_order(
    db: Session,
    user: AuthContext,
    payload: OrderCreate,
    *,
    allow_catalog_name_lookup: bool = False,
) -> Order:
    branch = db.scalar(select(Branch).where(Branch.id == payload.branch_id, Branch.active.is_(True)))
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found")
    ensure_branch_scope(user, branch.business_id, branch.id)

    branch_settings = db.scalar(
        select(BranchSettings).where(BranchSettings.branch_id == branch.id)
    )
    if branch_settings:
        digital_source = payload.source in {
            "public_store",
            "agent",
            "n8n",
            "integration",
            "whatsapp",
            "whatsapp_agent",
        }
        service_key = {
            "dine_in": "digital_tables" if digital_source else "pos_tables",
            "counter": "pos_counter",
            "takeaway": "digital_takeaway" if digital_source else "pos_takeaway",
            "delivery": "digital_delivery" if digital_source else "pos_delivery",
            "online": "digital_takeaway",
            "whatsapp": "digital_takeaway",
        }.get(payload.channel)
        if service_key and not getattr(branch_settings, service_key):
            raise CodedHTTPException(409, "Service is disabled for this branch", "SERVICE_DISABLED")
        fulfillment = (
            "delivery"
            if payload.channel == "delivery"
            else "counter"
            if payload.channel in {"counter", "dine_in"}
            else "takeaway"
        )
        allowed_methods = (branch_settings.payment_methods or {}).get(fulfillment, [])
        if payload.payment_method and allowed_methods and payload.payment_method not in allowed_methods:
            raise CodedHTTPException(
                409,
                "Payment method is disabled for this service",
                "PAYMENT_METHOD_DISABLED",
            )
        primary_schedule = db.scalar(
            select(ServiceSchedule).where(
                ServiceSchedule.branch_id == branch.id,
                ServiceSchedule.kind == "primary",
                ServiceSchedule.active.is_(True),
                ServiceSchedule.archived_at.is_(None),
            )
        )
        if primary_schedule and not schedule_is_open(db, primary_schedule):
            raise CodedHTTPException(409, "Branch is closed at this time", "BRANCH_CLOSED")

    if payload.channel == "dine_in" and not payload.table_id:
        raise CodedHTTPException(422, "Dine-in orders require a table", "TABLE_REQUIRED_FOR_DINE_IN")

    table = None
    if payload.table_id:
        table = db.scalar(
            select(RestaurantTable).where(
                RestaurantTable.id == payload.table_id,
                RestaurantTable.branch_id == payload.branch_id,
            ).with_for_update()
        )
        if not table:
            raise HTTPException(status_code=422, detail="Table does not belong to branch")
        active_order_id = db.scalar(
            select(Order.id).where(
                Order.table_id == table.id,
                Order.branch_id == branch.id,
                Order.status.not_in(["closed", "cancelled", "delivered"]),
                Order.table_released_at.is_(None),
            ).limit(1)
        )
        if active_order_id is not None:
            raise CodedHTTPException(
                409,
                "Table already has an open order",
                "TABLE_ALREADY_HAS_OPEN_ORDER",
            )
        if table.status not in {"available", "reserved"}:
            raise HTTPException(status_code=409, detail="Table is not available")

    number = f"{datetime.now().strftime('%y%m%d')}-{uuid4().hex[:6].upper()}"
    order = Order(
        business_id=branch.business_id,
        branch_id=branch.id,
        number=number,
        channel=payload.channel,
        source=payload.source,
        table_id=payload.table_id,
        customer_id=payload.customer_id,
        customer_name=payload.customer_name,
        customer_phone=payload.customer_phone,
        payment_method=payload.payment_method,
        whatsapp_chat_id=payload.whatsapp_chat_id,
        whatsapp_message_id=payload.whatsapp_message_id,
        delivery_address=payload.delivery_address,
        delivery_fee=money(payload.delivery_fee),
        discount=money(payload.discount),
        manual_discount=money(payload.discount),
        notes=payload.notes,
        external_reference=payload.external_reference,
        created_by=user.user_id,
    )
    for line in payload.items:
        order.items.append(
            build_order_item(
                db,
                branch.business_id,
                branch.id,
                line,
                payload.channel,
                payload.source,
                allow_catalog_name_lookup=allow_catalog_name_lookup,
            )
        )
    recalculate_order(db, order)
    product_ids = {item.product_id for item in order.items if item.product_id is not None}
    if product_ids:
        assigned_schedules = list(
            db.scalars(
                select(ServiceSchedule)
                .join(ScheduleAssignment, ScheduleAssignment.schedule_id == ServiceSchedule.id)
                .where(
                    ServiceSchedule.branch_id == branch.id,
                    ServiceSchedule.kind == "additional",
                    ServiceSchedule.active.is_(True),
                    ServiceSchedule.archived_at.is_(None),
                    ScheduleAssignment.product_id.in_(product_ids),
                )
                .distinct()
            )
        )
        if any(not schedule_is_open(db, schedule) for schedule in assigned_schedules):
            raise CodedHTTPException(
                409,
                "One or more products are outside their configured schedule",
                "PRODUCT_SCHEDULE_CLOSED",
            )
    if payload.delivery_quote_id:
        if payload.channel != "delivery":
            raise HTTPException(status_code=422, detail="Delivery quotes can only be used for delivery orders")
        apply_delivery_quote_to_order(db, order, payload.delivery_quote_id)
        recalculate_order(db, order)
    if payload.allow_pending_delivery_quote:
        from .agent_checkout import AGENT_SOURCES
        if (order.source not in AGENT_SOURCES or order.channel != "delivery" or not branch_settings
                or branch_settings.delivery_mode != "quote" or not branch_settings.pos_delivery):
            raise HTTPException(422, "Pending delivery is only allowed for WhatsApp orders with quotation pricing")
        order.delivery_fee_status = "pending_quote"
        order.delivery_fee = Decimal("0")
        recalculate_order(db, order)
    if payload.quoted_total is not None and money(payload.quoted_total) != money(order.total):
        raise CodedHTTPException(409, "The price changed; confirm the new total before payment", "ORDER_QUOTE_CHANGED")
    if order.source == "pos":
        ensure_final_delivery_fee(order)
    order.folio = reserve_order_folio(db, branch.business_id)
    db.add(order)
    db.flush()
    if table:
        table.status = "occupied"
        table.version += 1
    audit(db, user, "order.created", "order", order.id, order.business_id, {"channel": order.channel})
    return order


def product_capacity(db: Session, product: Product) -> dict:
    components = list(
        db.execute(
            select(RecipeItem, InventoryItem)
            .join(InventoryItem, InventoryItem.id == RecipeItem.inventory_item_id)
            .where(
                RecipeItem.product_id == product.id,
                InventoryItem.branch_id == product.branch_id,
            )
        )
    )
    if not components:
        return {
            "available": bool(product.available),
            "available_units": None,
            "stock_known": not product.track_stock,
        }
    capacities = [
        int(Decimal(str(inventory.quantity)) // Decimal(str(recipe.quantity)))
        for recipe, inventory in components
        if Decimal(str(recipe.quantity)) > 0
    ]
    available_units = min(capacities) if capacities else 0
    return {
        "available": bool(product.available and available_units > 0),
        "available_units": available_units,
        "stock_known": True,
    }


def create_integration_event(
    db: Session,
    order: Order,
    event_type: str,
    payload: dict | None = None,
) -> IntegrationEvent:
    event = IntegrationEvent(
        business_id=order.business_id,
        branch_id=order.branch_id,
        event_type=event_type,
        aggregate_type="order",
        aggregate_id=str(order.id),
        customer_phone=order.customer_phone,
        whatsapp_chat_id=order.whatsapp_chat_id,
        payload=jsonable_encoder(
            {
                "order_id": order.id,
                "order_number": order.number,
                "channel": order.channel,
                "customer_phone": order.customer_phone,
                "whatsapp_chat_id": order.whatsapp_chat_id,
                **(payload or {}),
            }
        ),
    )
    db.add(event)
    db.flush()
    return event


def add_order_item(db: Session, user: AuthContext, order: Order, line: OrderLineInput) -> Order:
    if order.status not in {"draft", "pending_confirmation", "confirmed"}:
        raise HTTPException(status_code=409, detail="Order can no longer be edited")
    ensure_order_products_mutable(db, order, "adding products")
    order.items.append(
        build_order_item(
            db,
            order.business_id,
            order.branch_id,
            line,
            order.channel,
            order.source,
        )
    )
    recalculate_order(db, order)
    order.version += 1
    db.flush()
    audit(db, user, "order.item_added", "order", order.id, order.business_id)
    return order


def _inventory_requirements_for_items(
    db: Session,
    items: list[OrderItem],
) -> dict[int, Decimal]:
    requirements: dict[int, Decimal] = {}
    for item in items:
        if not item.product_id:
            continue
        product_multipliers = [(item.product_id, Decimal("1"))]
        product = db.get(Product, item.product_id)
        if product and product.product_type == "combo":
            product_multipliers.extend(
                (component.component_product_id, Decimal(str(component.quantity)))
                for component in db.scalars(
                    select(ComboItem).where(ComboItem.product_id == product.id)
                )
            )
        for product_id, product_multiplier in product_multipliers:
            components = db.scalars(
                select(RecipeItem).where(RecipeItem.product_id == product_id)
            ).all()
            for component in components:
                required = (
                    Decimal(str(component.quantity))
                    * Decimal(str(item.quantity))
                    * product_multiplier
                )
                requirements[component.inventory_item_id] = requirements.get(
                    component.inventory_item_id, Decimal("0")
                ) + required
    return requirements


def commit_order_items_stock(
    db: Session,
    user: AuthContext,
    order: Order,
    items: list[OrderItem],
) -> None:
    requirements = _inventory_requirements_for_items(db, items)

    inventory_items: dict[int, InventoryItem] = {}
    for inventory_id, required in requirements.items():
        inventory = db.scalar(
            select(InventoryItem)
            .where(
                InventoryItem.id == inventory_id,
                InventoryItem.branch_id == order.branch_id,
            )
            .with_for_update()
        )
        if not inventory:
            raise HTTPException(status_code=422, detail=f"Recipe inventory item {inventory_id} not found")
        if Decimal(str(inventory.quantity)) < required:
            raise HTTPException(status_code=409, detail=f"Insufficient stock for {inventory.name}")
        inventory_items[inventory_id] = inventory

    for inventory_id, required in requirements.items():
        inventory = inventory_items[inventory_id]
        inventory.quantity = Decimal(str(inventory.quantity)) - required
        inventory.version += 1
        db.add(
            StockMovement(
                business_id=order.business_id,
                branch_id=order.branch_id,
                inventory_item_id=inventory.id,
                movement_type="sale_consumption",
                quantity_delta=-required,
                balance_after=inventory.quantity,
                reference_type="order",
                reference_id=str(order.id),
                created_by=user.user_id,
            )
        )


def restore_order_items_stock(
    db: Session,
    user: AuthContext,
    order: Order,
    items: list[OrderItem],
) -> None:
    requirements = _inventory_requirements_for_items(db, items)
    for inventory_id, restored in requirements.items():
        inventory = db.scalar(
            select(InventoryItem)
            .where(
                InventoryItem.id == inventory_id,
                InventoryItem.branch_id == order.branch_id,
            )
            .with_for_update()
        )
        if not inventory:
            raise HTTPException(
                status_code=422,
                detail=f"Recipe inventory item {inventory_id} not found",
            )
        inventory.quantity = Decimal(str(inventory.quantity)) + restored
        inventory.version += 1
        db.add(
            StockMovement(
                business_id=order.business_id,
                branch_id=order.branch_id,
                inventory_item_id=inventory.id,
                movement_type="item_revision_reversal",
                quantity_delta=restored,
                balance_after=inventory.quantity,
                reference_type="order",
                reference_id=str(order.id),
                created_by=user.user_id,
            )
        )


def commit_order_stock(db: Session, user: AuthContext, order: Order) -> None:
    commit_order_items_stock(db, user, order, active_order_items(order))


def ensure_final_delivery_fee(order: Order, *, allow_agent_pending: bool = False) -> None:
    if allow_agent_pending:
        from .agent_checkout import pending_delivery_allowed
        if pending_delivery_allowed(order):
            return
    if order.delivery_fee_status == "pending_quote" or order.delivery_fee is None:
        raise CodedHTTPException(
            409,
            "Delivery cost must be confirmed before the order can continue",
            "DELIVERY_FEE_PENDING",
        )


def confirm_order(db: Session, user: AuthContext, order: Order) -> Order:
    ensure_final_delivery_fee(order, allow_agent_pending=True)
    if order.status == "confirmed":
        return order
    if order.status not in {"draft", "pending_confirmation"}:
        raise HTTPException(status_code=409, detail="Only draft orders can be confirmed")
    if not active_order_items(order):
        raise HTTPException(status_code=422, detail="Order must contain at least one item")
    commit_order_stock(db, user, order)
    order.status = "confirmed"
    order.version += 1
    audit(db, user, "order.confirmed", "order", order.id, order.business_id)
    return order


def _ticket_items_snapshot(db: Session, items: list[OrderItem]) -> list[dict]:
    snapshot: list[dict] = []
    for item in items:
        combo_components: list[dict] = []
        if item.product_id:
            product = db.get(Product, item.product_id)
            if product and product.product_type == "combo":
                components = list(
                    db.scalars(
                        select(ComboItem)
                        .where(ComboItem.product_id == product.id)
                        .order_by(ComboItem.sort_order, ComboItem.id)
                    )
                )
                component_products = {
                    component.id: component
                    for component in db.scalars(
                        select(Product).where(
                            Product.id.in_([item.component_product_id for item in components])
                        )
                    )
                } if components else {}
                combo_components = [
                    {
                        "product_id": component.component_product_id,
                        "name": component_products[component.component_product_id].name,
                        "quantity": float(component.quantity),
                    }
                    for component in components
                    if component.component_product_id in component_products
                ]
        snapshot.append(
            {
                "item_id": item.id,
                "name": item.product_name,
                "variant_name": item.variant_name,
                "quantity": float(item.quantity),
                "modifiers": item.modifiers,
                "line_total": float(item.line_total),
                "promotion_discount": float(item.promotion_discount or 0),
                "notes": item.notes,
                "status": item.status,
                "replaces_item_id": item.replaces_item_id,
                "cancellation_reason": item.cancellation_reason,
                "combo_components": combo_components,
            }
        )
    return snapshot


def _ticket_context_snapshot(db: Session, order: Order) -> dict:
    return {
        "order_number": order.number,
        "order_folio": order.folio,
        "channel": order.channel,
        "source": order.source,
        "customer_name": order.customer_name,
        "customer_phone": order.customer_phone,
        "delivery_address": deepcopy(order.delivery_address),
        "notes": order.notes,
        **table_print_context(db, order),
        "created_by": order.created_by,
    }


def create_kitchen_tickets(
    db: Session,
    order: Order,
    items: list[OrderItem],
    *,
    kind: str = "standard",
    items_snapshot: list[dict] | None = None,
    context: dict | None = None,
) -> list[KitchenTicket]:
    if not items and items_snapshot is None:
        return []

    # Every caller also locks the order, but acquiring it here keeps sequence
    # allocation safe if this service is reused by another endpoint later.
    db.scalar(select(Order.id).where(Order.id == order.id).with_for_update())
    existing_sequences = list(
        db.scalars(
            select(KitchenTicket.sequence)
            .where(KitchenTicket.order_id == order.id)
            .with_for_update()
        )
    )
    ticket_context = _ticket_context_snapshot(db, order)
    if context:
        ticket_context.update(context)
    ticket_context["created_by_name"] = print_actor_name(db, order, ticket_context.get("created_by"))
    ticket = KitchenTicket(
        business_id=order.business_id,
        branch_id=order.branch_id,
        order_id=order.id,
        station="kitchen",
        kind=kind,
        sequence=max(existing_sequences, default=0) + 1,
        items_snapshot=(
            items_snapshot
            if items_snapshot is not None
            else _ticket_items_snapshot(db, items)
        ),
        context_snapshot=ticket_context,
    )
    db.add(ticket)
    db.flush()
    settings = db.scalar(select(BranchSettings).where(BranchSettings.branch_id == order.branch_id))
    if order.source != "pos" or local_printer_config(settings) is None:
        enqueue_kitchen_print_jobs(db, order, [ticket])
    return [ticket]


OPEN_PAYMENT_EVIDENCE_STATUSES = {"evidence_received", "under_review"}


def ensure_no_open_payment_evidence(
    db: Session,
    order: Order,
    action: str,
    *, initial_only: bool = False,
) -> None:
    evidence_under_review = db.scalar(
        select(PaymentEvidence.id).where(
            PaymentEvidence.order_id == order.id,
            PaymentEvidence.status.in_(OPEN_PAYMENT_EVIDENCE_STATUSES),
            *([PaymentEvidence.payment_request_id.is_(None)] if initial_only else []),
        ).limit(1)
    )
    if evidence_under_review is not None:
        raise CodedHTTPException(
            409,
            f"Resolve the payment evidence review before {action}",
            "PAYMENT_EVIDENCE_UNDER_REVIEW",
        )


def ensure_order_products_mutable(
    db: Session,
    order: Order,
    action: str,
) -> None:
    if order.status in {"cancelled", "closed"} or order.table_released_at is not None:
        raise CodedHTTPException(
            409,
            "Order products are locked",
            "ORDER_ITEMS_LOCKED",
        )
    if order.checkout_started_at is not None:
        raise CodedHTTPException(
            409,
            "Reopen the table before changing its products",
            "TABLE_CHECKOUT_STARTED",
        )
    has_payment = db.scalar(
        select(Payment.id).where(
            Payment.order_id == order.id,
            Payment.status == "confirmed",
        ).limit(1)
    )
    if has_payment is not None:
        raise CodedHTTPException(
            409,
            "Order products cannot change after a payment",
            "ORDER_HAS_PAYMENTS",
        )
    ensure_no_open_payment_evidence(db, order, action)


def send_order_to_kitchen(db: Session, user: AuthContext, order: Order) -> list[KitchenTicket]:
    ensure_final_delivery_fee(order, allow_agent_pending=True)
    db.flush()
    ensure_no_open_payment_evidence(db, order, "sending the order to kitchen", initial_only=True)
    existing = list(
        db.scalars(
            select(KitchenTicket)
            .where(KitchenTicket.order_id == order.id)
            .order_by(KitchenTicket.station, KitchenTicket.sequence)
        )
    )
    if order.status in {"sent_to_kitchen", "preparing"}:
        return existing
    if order.status != "confirmed":
        raise HTTPException(status_code=409, detail="Confirm the order before sending it to kitchen")
    if existing:
        raise CodedHTTPException(
            409,
            "Kitchen tickets were already created for this order",
            "KITCHEN_TICKETS_ALREADY_CREATED",
        )

    tickets = create_kitchen_tickets(db, order, active_order_items(order))
    order.status = "sent_to_kitchen"
    order.sent_to_kitchen_at = utcnow()
    order.version += 1
    audit(db, user, "order.sent_to_kitchen", "order", order.id, order.business_id)
    db.flush()
    for ticket in tickets:
        prepare_pos_print_intent(db, order, ticket)
    return tickets


def sync_order_payment_status(db: Session, order: Order) -> Decimal:
    paid_total = money(
        db.scalar(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.order_id == order.id,
                Payment.status == "confirmed",
            )
        )
    )
    if paid_total >= money(order.total) and money(order.total) > 0 and order.delivery_fee_status != "pending_quote":
        order.payment_status = "paid"
    elif paid_total > 0:
        order.payment_status = "partial"
    elif order.payment_status not in {"evidence_received", "under_review", "rejected"}:
        order.payment_status = "pending"
    return paid_total


def append_order_item_batch(
    db: Session,
    user: AuthContext,
    order: Order,
    lines: list[OrderLineInput],
) -> tuple[list[OrderItem], list[KitchenTicket]]:
    if order.status not in {
        "draft",
        "pending_confirmation",
        "confirmed",
        "sent_to_kitchen",
        "preparing",
        "ready",
    }:
        raise CodedHTTPException(
            409,
            "Order can no longer receive products",
            "ORDER_ITEMS_LOCKED",
        )
    ensure_order_products_mutable(db, order, "adding products")

    previous_status = order.status
    new_items = [
        build_order_item(
            db,
            order.business_id,
            order.branch_id,
            line,
            order.channel,
            order.source,
        )
        for line in lines
    ]
    order.items.extend(new_items)
    db.flush()
    recalculate_order(db, order)

    tickets: list[KitchenTicket] = []
    if previous_status in {"confirmed", "sent_to_kitchen", "preparing", "ready"}:
        commit_order_items_stock(db, user, order, new_items)
        existing_ticket_count = db.scalar(
            select(func.count(KitchenTicket.id)).where(KitchenTicket.order_id == order.id)
        ) or 0
        if previous_status == "confirmed" and existing_ticket_count == 0:
            tickets = create_kitchen_tickets(db, order, active_order_items(order), context={"created_by": user.user_id})
            order.status = "sent_to_kitchen"
            order.sent_to_kitchen_at = order.sent_to_kitchen_at or utcnow()
        else:
            tickets = create_kitchen_tickets(db, order, new_items, kind="addition", context={"created_by": user.user_id})
            order.status = "sent_to_kitchen"
            order.sent_to_kitchen_at = order.sent_to_kitchen_at or utcnow()

    sync_order_payment_status(db, order)
    order.version += 1
    audit(
        db,
        user,
        "order.item_batch_added",
        "order",
        order.id,
        order.business_id,
        {
            "item_ids": [item.id for item in new_items],
            "ticket_ids": [ticket.id for ticket in tickets],
        },
    )
    db.flush()
    for ticket in tickets:
        prepare_pos_print_intent(db, order, ticket)
    return new_items, tickets


def apply_order_item_revisions(
    db: Session,
    user: AuthContext,
    order: Order,
    operations: list[OrderItemRevisionOperation],
) -> tuple[list[OrderItem], list[OrderItem], list[KitchenTicket]]:
    if order.status not in {"sent_to_kitchen", "preparing", "ready"}:
        raise CodedHTTPException(
            409,
            "Only products already sent to kitchen can be revised",
            "ORDER_ITEMS_NOT_SENT",
        )
    ensure_order_products_mutable(db, order, "revising products")

    operation_ids = [operation.item_id for operation in operations]
    if len(operation_ids) != len(set(operation_ids)):
        raise CodedHTTPException(
            422,
            "Each product can be revised only once per request",
            "DUPLICATE_ITEM_REVISION",
        )
    candidates = {
        item.id: item
        for item in active_order_items(order)
        if item.id in operation_ids
    }
    if len(candidates) != len(operation_ids):
        raise CodedHTTPException(
            404,
            "One or more active order products were not found",
            "ORDER_ITEM_NOT_FOUND",
        )

    cancelled_ids = {op.item_id for op in operations if op.type == "cancel"}
    if not any(item.id not in cancelled_ids for item in active_order_items(order)):
        raise CodedHTTPException(
            409,
            'Para cancelar todos los productos, usa "Cancelar pedido" en el men\u00fa de tres puntos.',
            "ORDER_REQUIRES_CANCELLATION",
        )
    existing_tickets = list(db.scalars(
        select(KitchenTicket).where(
            KitchenTicket.order_id == order.id,
            KitchenTicket.business_id == order.business_id,
            KitchenTicket.branch_id == order.branch_id,
        ).order_by(KitchenTicket.sequence).with_for_update()
    ))
    owners: dict[int, tuple[KitchenTicket, dict]] = {}
    for ticket in existing_tickets:
        for snapshot in effective_ticket_items(ticket):
            owners[snapshot["item_id"]] = (ticket, snapshot)
    if any(item_id not in owners for item_id in operation_ids):
        raise CodedHTTPException(409, "No se encontr\u00f3 la comanda original del producto.", "ORDER_ITEM_COMMAND_MISSING")
    modified_at = utcnow().isoformat()
    audit_before = {item_id: item_audit_snapshot(item) for item_id, item in candidates.items()}

    original_items: list[OrderItem] = []
    replacement_items: list[OrderItem] = []
    edit_pairs: list[tuple[OrderItem, OrderItem]] = []
    cancelled_items: list[OrderItem] = []
    for operation in operations:
        original = candidates[operation.item_id]
        original_items.append(original)
        if operation.type == "edit":
            replacement = build_order_item(
                db,
                order.business_id,
                order.branch_id,
                operation.replacement,
                order.channel,
                order.source,
            )
            original.status = "superseded"
            replacement.replaces_item_id = original.id
            order.items.append(replacement)
            replacement_items.append(replacement)
            edit_pairs.append((original, replacement))
        else:
            original.status = "cancelled"
            original.cancellation_reason = (operation.reason or "").strip()
            cancelled_items.append(original)

    restore_order_items_stock(db, user, order, original_items)
    db.flush()
    if replacement_items:
        commit_order_items_stock(db, user, order, replacement_items)
    recalculate_order(db, order)

    command_items: list[dict] = []
    for original, replacement in edit_pairs:
        item_snapshot = _ticket_items_snapshot(db, [replacement])[0]
        if original.product_id == replacement.product_id:
            item_snapshot["combo_components"] = deepcopy(owners[original.id][1].get("combo_components", []))
        item_snapshot.update(
            {
                "action": "modified",
                "previous_item_id": original.id,
                "previous_name": original.product_name,
                "previous_quantity": float(original.quantity),
                "previous_variant_name": original.variant_name,
                "removed_modifiers": retired_modifier_units(
                    owners[original.id][1], item_snapshot,
                    keep_history=owners[original.id][0].status in {"queued", "preparing"},
                ),
                "modified_at": modified_at,
            }
        )
        command_items.append(item_snapshot)
    for item in cancelled_items:
        item_snapshot = deepcopy(owners[item.id][1])
        item_snapshot.update(
            {
                "action": "cancelled",
                "previous_item_id": item.id,
                "status": "cancelled",
                "cancellation_reason": item.cancellation_reason,
                "modified_at": modified_at,
                "line_total": float(item.line_total),
                "promotion_discount": float(item.promotion_discount or 0),
            }
        )
        command_items.append(item_snapshot)

    if edit_pairs and cancelled_items:
        kind = "revision"
    elif cancelled_items:
        kind = "cancellation"
    else:
        kind = "modification"
    updated: dict[int, KitchenTicket] = {}
    new_command_items = []
    source_ticket_ids = set()
    new_revisions = []
    for after in command_items:
        previous_id = after.get("previous_item_id", after["item_id"])
        ticket, before = owners[previous_id]
        if ticket.status in {"queued", "preparing"}:
            context = deepcopy(ticket.context_snapshot or {})
            context.setdefault("revisions", []).append({
                "before": deepcopy(before), "after": deepcopy(after),
                "created_at": modified_at, "created_by": user.user_id,
            })
            context["modified_at"] = modified_at
            ticket.context_snapshot = context
            updated[ticket.id] = ticket
        else:
            new_command_items.append(after)
            source_ticket_ids.add(ticket.id)
            new_revisions.append({"before": deepcopy(before), "after": deepcopy(after), "created_at": modified_at, "created_by": user.user_id})
    for ticket in updated.values():
        ticket.version += 1
    sync_pending_kitchen_print_jobs(db, order, list(updated.values()))
    new_tickets = create_kitchen_tickets(
        db, order, [], kind=kind, items_snapshot=new_command_items,
        context={"modified_at": modified_at, "source_ticket_ids": sorted(source_ticket_ids), "revisions": new_revisions, "created_by": user.user_id},
    ) if new_command_items else []
    tickets = [*updated.values(), *new_tickets]
    if new_tickets:
        order.status = "sent_to_kitchen"
    sync_order_payment_status(db, order)
    order.version += 1
    audit(
        db,
        user,
        "order.items_revised",
        "order",
        order.id,
        order.business_id,
        {
            "snapshot_version": 1,
            "order": order_identity_snapshot(order),
            "operations": [
                {
                    "type": operation.type,
                    "item_id": operation.item_id,
                    "replacement_item_id": next(
                        (
                            replacement.id
                            for original, replacement in edit_pairs
                            if original.id == operation.item_id
                        ),
                        None,
                    ),
                    "reason": operation.reason,
                    "before": audit_before[operation.item_id],
                    "after": next(
                        (item_audit_snapshot(replacement) for original, replacement in edit_pairs
                         if original.id == operation.item_id),
                        None,
                    ),
                }
                for operation in operations
            ],
            "ticket_ids": [ticket.id for ticket in tickets],
            "updated_ticket_ids": list(updated),
            "created_ticket_ids": [ticket.id for ticket in new_tickets],
        },
        branch_id=order.branch_id,
        actor_display_name=cash_actor_display_name(
            db, user, business_id=order.business_id, branch_id=order.branch_id,
        ),
    )
    db.flush()
    for ticket in new_tickets:
        prepare_pos_print_intent(db, order, ticket)
    return replacement_items, cancelled_items, tickets


ORDER_TRANSITIONS = {
    "sent_to_kitchen": {"preparing", "cancelled"},
    "preparing": {"ready", "cancelled"},
    "ready": {"dispatched", "delivered", "closed", "cancelled"},
    "dispatched": {"delivered", "cancelled"},
    "delivered": {"closed"},
    "confirmed": {"cancelled"},
    "draft": {"cancelled"},
    "pending_confirmation": {"cancelled"},
}


def reverse_order_stock(db: Session, user: AuthContext, order: Order) -> None:
    movements = list(
        db.scalars(
            select(StockMovement).where(
                StockMovement.reference_type == "order",
                StockMovement.reference_id == str(order.id),
                StockMovement.movement_type.in_(
                    ["sale_consumption", "item_revision_reversal"]
                ),
            )
        )
    )
    already_reversed = db.scalar(
        select(func.count(StockMovement.id)).where(
            StockMovement.reference_type == "order_reversal",
            StockMovement.reference_id == str(order.id),
        )
    )
    if already_reversed:
        return
    net_by_inventory: dict[int, Decimal] = {}
    for movement in movements:
        net_by_inventory[movement.inventory_item_id] = (
            net_by_inventory.get(movement.inventory_item_id, Decimal("0"))
            + Decimal(str(movement.quantity_delta))
        )
    for inventory_id, net_delta in net_by_inventory.items():
        if net_delta >= 0:
            continue
        inventory = db.scalar(
            select(InventoryItem).where(InventoryItem.id == inventory_id).with_for_update()
        )
        if not inventory:
            continue
        restored = abs(net_delta)
        inventory.quantity = Decimal(str(inventory.quantity)) + restored
        inventory.version += 1
        db.add(
            StockMovement(
                business_id=order.business_id,
                branch_id=order.branch_id,
                inventory_item_id=inventory.id,
                movement_type="cancellation_reversal",
                quantity_delta=restored,
                balance_after=inventory.quantity,
                reference_type="order_reversal",
                reference_id=str(order.id),
                created_by=user.user_id,
            )
        )


def cancel_active_kitchen_tickets(
    db: Session,
    user: AuthContext,
    order: Order,
) -> list[KitchenTicket]:
    tickets = list(
        db.scalars(
            select(KitchenTicket)
            .where(
                KitchenTicket.order_id == order.id,
                KitchenTicket.business_id == order.business_id,
                KitchenTicket.branch_id == order.branch_id,
                KitchenTicket.status.in_(["queued", "preparing"]),
            )
            .with_for_update()
        )
    )
    for ticket in tickets:
        previous_status = ticket.status
        ticket.status = "cancelled"
        ticket.version += 1
        audit(
            db,
            user,
            "kitchen.cancelled",
            "kitchen_ticket",
            ticket.id,
            ticket.business_id,
            {"previous_status": previous_status, "order_id": order.id},
        )
    sync_pending_kitchen_print_jobs(db, order, tickets, cancel=True)
    return tickets


def release_current_order_table(db: Session, order: Order, status: str) -> None:
    if not order.table_id or order.table_released_at is not None:
        return
    table = db.scalar(
        select(RestaurantTable).where(
            RestaurantTable.id == order.table_id,
            RestaurantTable.business_id == order.business_id,
            RestaurantTable.branch_id == order.branch_id,
        ).with_for_update().execution_options(populate_existing=True)
    )
    if not table:
        return
    other_occupant = db.scalar(
        select(Order.id).where(
            Order.table_id == table.id,
            Order.business_id == order.business_id,
            Order.branch_id == order.branch_id,
            Order.id != order.id,
            Order.table_released_at.is_(None),
            Order.status.notin_(["cancelled", "closed"]),
        ).limit(1)
    )
    if other_occupant is None:
        table.status = status
        table.version += 1
    order.table_released_at = utcnow()


def transition_order(
    db: Session,
    user: AuthContext,
    order: Order,
    next_status: str,
    *,
    reason: str | None = None,
) -> Order:
    if next_status == order.status:
        if next_status == "cancelled":
            cancel_active_kitchen_tickets(db, user, order)
        return order
    if next_status in {"dispatched", "delivered", "closed"}:
        from .agent_checkout import ensure_dispatch
        ensure_dispatch(db, order)
    ensure_no_open_payment_evidence(db, order, "changing the order status")
    if next_status not in ORDER_TRANSITIONS.get(order.status, set()):
        raise HTTPException(status_code=409, detail=f"Cannot transition {order.status} to {next_status}")
    if next_status == "closed" and order.payment_status != "paid":
        raise HTTPException(status_code=409, detail="Order must be fully paid before closing")
    if next_status == "ready":
        pending_ticket = db.scalar(
            select(KitchenTicket.id).where(
                KitchenTicket.order_id == order.id,
                KitchenTicket.status.in_(["queued", "preparing"]),
            ).limit(1)
        )
        if pending_ticket is not None:
            raise CodedHTTPException(
                409,
                "All kitchen tickets must be ready before the order can be marked ready",
                "KITCHEN_TICKETS_PENDING",
            )
    cancellation_snapshot = None
    if next_status == "cancelled":
        cancellation_snapshot = {
            "snapshot_version": 1,
            "order": order_identity_snapshot(order),
            "reason": (reason or "").strip() or None,
            "items": [item_audit_snapshot(item) for item in active_order_items(order)],
            "total": str(order.total),
            "paid_amount": str(db.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.business_id == order.business_id, Payment.order_id == order.id,
                Payment.status == "confirmed",
            ))),
        }
        reverse_order_stock(db, user, order)
        cancel_active_kitchen_tickets(db, user, order)
        release_current_order_table(db, order, "available")
    order.status = next_status
    order.version += 1
    if next_status == "closed":
        order.closed_at = utcnow()
        release_current_order_table(db, order, "cleaning")
    audit(
        db, user, f"order.{next_status}", "order", order.id, order.business_id,
        cancellation_snapshot,
        branch_id=order.branch_id,
        actor_display_name=cash_actor_display_name(
            db, user, business_id=order.business_id, branch_id=order.branch_id,
        ) if next_status == "cancelled" else None,
    )
    return order


def add_payment(db: Session, user: AuthContext, order: Order, payload: PaymentCreate) -> Payment:
    if order.status in {"cancelled", "closed"}:
        raise HTTPException(status_code=409, detail="Cannot add a payment to this order")
    ensure_final_delivery_fee(order, allow_agent_pending=True)
    paid_total = money(
        db.scalar(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.order_id == order.id,
                Payment.status == "confirmed",
            )
        )
    )
    if paid_total + money(payload.amount) > money(order.total):
        raise HTTPException(status_code=422, detail="Payment exceeds outstanding order amount")
    cash_session = resolve_payment_cash_session(
        db,
        user,
        order,
        cash_session_id=payload.cash_session_id,
        register_id=payload.register_id,
    )

    payment = Payment(
        business_id=order.business_id,
        order_id=order.id,
        cash_session_id=cash_session.id,
        method=payload.method,
        amount=money(payload.amount),
        external_reference=payload.external_reference,
        note=payload.note,
        created_by=user.user_id,
    )
    db.add(payment)
    cash_session.version += 1
    db.flush()
    for allocation in payload.allocations:
        db.add(
            PaymentAllocation(
                payment_id=payment.id,
                order_item_id=allocation.order_item_id,
                label=allocation.label,
                amount=money(allocation.amount),
            )
        )
    paid_total += money(payload.amount)
    order.payment_status = "paid" if paid_total >= money(order.total) and order.delivery_fee_status != "pending_quote" else "partial"
    order.version += 1
    register_name = db.scalar(select(CashRegister.name).where(
        CashRegister.id == cash_session.register_id,
        CashRegister.business_id == order.business_id,
        CashRegister.branch_id == order.branch_id,
    ))
    audit(
        db, user, "payment.created", "payment", payment.id, order.business_id,
        {
            "order_id": order.id,
            "cash_session_id": cash_session.id,
            "cash_register_id": cash_session.register_id,
            "cash_register_name": register_name,
        },
        branch_id=order.branch_id,
    )
    return payment


def assert_command_version(ticket: KitchenTicket, expected_version: int | None) -> None:
    if expected_version is not None and ticket.version != expected_version:
        raise CodedHTTPException(
            409,
            "La comanda cambi\u00f3. Revisa su contenido actualizado antes de continuar.",
            "KITCHEN_TICKET_STALE",
        )


def complete_kitchen_command(
    db: Session,
    user: AuthContext,
    order: Order,
    ticket: KitchenTicket,
    expected_status: str,
    expected_version: int | None = None,
) -> KitchenTicket:
    assert_command_version(ticket, expected_version)
    if order.status == "cancelled":
        raise CodedHTTPException(409, "Order is cancelled", "ORDER_CANCELLED")
    if ticket.status != expected_status:
        raise CodedHTTPException(
            409,
            "Kitchen command changed on another terminal",
            "KITCHEN_TICKET_STALE",
        )
    if ticket.status not in {"queued", "preparing"}:
        raise CodedHTTPException(
            409,
            "Only an active kitchen command can be completed",
            "KITCHEN_COMMAND_NOT_ACTIVE",
        )
    now = utcnow()
    ticket.started_at = ticket.started_at or now
    ticket.ready_at = now
    ticket.status = "ready"
    ticket.version += 1
    remaining = db.scalar(
        select(KitchenTicket.id).where(
            KitchenTicket.order_id == order.id,
            KitchenTicket.id != ticket.id,
            KitchenTicket.status.in_(["queued", "preparing"]),
        ).limit(1)
    )
    if remaining is None:
        if order.table_released_at is not None:
            order.status = "closed"
            order.closed_at = order.closed_at or now
        else:
            order.status = "ready"
    elif order.table_released_at is None:
        order.status = "preparing"
    order.version += 1
    audit(
        db,
        user,
        "kitchen.ready",
        "kitchen_ticket",
        ticket.id,
        ticket.business_id,
        {"order_id": order.id},
    )
    return ticket


def reopen_kitchen_command(
    db: Session,
    user: AuthContext,
    order: Order,
    ticket: KitchenTicket,
    expected_status: str,
    expected_version: int | None = None,
) -> KitchenTicket:
    assert_command_version(ticket, expected_version)
    if order.status == "cancelled":
        raise CodedHTTPException(409, "Order is cancelled", "ORDER_CANCELLED")
    if ticket.status != expected_status:
        raise CodedHTTPException(
            409,
            "Kitchen command changed on another terminal",
            "KITCHEN_TICKET_STALE",
        )
    if ticket.status not in {"ready", "served"}:
        raise CodedHTTPException(
            409,
            "Only a completed kitchen command can be reopened",
            "KITCHEN_COMMAND_NOT_COMPLETED",
        )
    ticket.status = "preparing"
    ticket.version += 1
    ticket.ready_at = None
    ticket.started_at = ticket.started_at or utcnow()
    if order.table_released_at is None and order.status != "closed":
        order.status = "preparing"
    order.version += 1
    audit(
        db,
        user,
        "kitchen.reopened",
        "kitchen_ticket",
        ticket.id,
        ticket.business_id,
        {"order_id": order.id},
    )
    return ticket


def _ensure_table_order(order: Order) -> None:
    if order.channel != "dine_in" or order.table_id is None:
        raise CodedHTTPException(
            409,
            "This order is not assigned to a table",
            "ORDER_NOT_TABLE_SERVICE",
        )
    if order.status == "cancelled":
        raise CodedHTTPException(409, "Order is cancelled", "ORDER_CANCELLED")


def _confirmed_payment_total(db: Session, order: Order) -> Decimal:
    return money(
        db.scalar(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.order_id == order.id,
                Payment.status == "confirmed",
            )
        )
    )


def start_table_checkout(db: Session, user: AuthContext, order: Order) -> Order:
    _ensure_table_order(order)
    if order.table_released_at is not None:
        raise CodedHTTPException(409, "Table is already released", "TABLE_ALREADY_RELEASED")
    if not active_order_items(order):
        raise CodedHTTPException(
            409,
            "This table has no products",
            "TABLE_HAS_NO_PRODUCTS",
        )
    if _confirmed_payment_total(db, order) > 0:
        raise CodedHTTPException(
            409,
            "A table with payments cannot start checkout again",
            "ORDER_HAS_PAYMENTS",
        )
    ensure_no_open_payment_evidence(db, order, "starting table checkout")
    if order.checkout_started_at is None:
        order.checkout_started_at = utcnow()
        order.version += 1
        create_checkout_print_job(db, order)
        audit(db, user, "table.checkout_started", "order", order.id, order.business_id)
    return order


def reopen_table_checkout(db: Session, user: AuthContext, order: Order) -> Order:
    _ensure_table_order(order)
    if order.table_released_at is not None:
        raise CodedHTTPException(409, "Table is already released", "TABLE_ALREADY_RELEASED")
    if _confirmed_payment_total(db, order) > 0:
        raise CodedHTTPException(
            409,
            "A table cannot be reopened after a payment",
            "ORDER_HAS_PAYMENTS",
        )
    ensure_no_open_payment_evidence(db, order, "reopening table checkout")
    if order.checkout_started_at is not None:
        order.checkout_started_at = None
        order.version += 1
        invalidate_checkout_print_jobs(db, order)
        audit(db, user, "table.checkout_reopened", "order", order.id, order.business_id)
    return order


def pay_table_checkout(
    db: Session,
    user: AuthContext,
    order: Order,
    payment_payloads: list[PaymentCreate],
) -> list[Payment]:
    _ensure_table_order(order)
    if order.table_released_at is not None:
        raise CodedHTTPException(409, "Table is already released", "TABLE_ALREADY_RELEASED")
    if order.checkout_started_at is None:
        raise CodedHTTPException(
            409,
            "Close the table before collecting payment",
            "TABLE_CHECKOUT_NOT_STARTED",
        )
    ensure_no_open_payment_evidence(db, order, "collecting table payment")
    already_paid = _confirmed_payment_total(db, order)
    remaining = max(money(order.total) - already_paid, Decimal("0"))
    requested_total = money(
        sum((money(payload.amount) for payload in payment_payloads), Decimal("0"))
    )
    if requested_total != remaining:
        raise CodedHTTPException(
            422,
            "Payment methods must cover the exact outstanding table total",
            "TABLE_PAYMENT_TOTAL_MISMATCH",
        )

    payments = [add_payment(db, user, order, payload) for payload in payment_payloads]
    db.flush()
    if _confirmed_payment_total(db, order) < money(order.total):
        raise CodedHTTPException(
            409,
            "The table payment did not settle the full balance",
            "TABLE_PAYMENT_INCOMPLETE",
        )

    now = utcnow()
    order.payment_status = "paid"
    order.table_released_at = now
    table = db.scalar(
        select(RestaurantTable)
        .where(
            RestaurantTable.id == order.table_id,
            RestaurantTable.business_id == order.business_id,
            RestaurantTable.branch_id == order.branch_id,
        )
        .with_for_update()
    )
    if table:
        table.status = "available"
        table.version += 1
    active_ticket = db.scalar(
        select(KitchenTicket.id).where(
            KitchenTicket.order_id == order.id,
            KitchenTicket.status.in_(["queued", "preparing"]),
        ).limit(1)
    )
    if active_ticket is None:
        order.status = "closed"
        order.closed_at = order.closed_at or now
    order.version += 1
    audit(
        db,
        user,
        "table.paid_and_released",
        "order",
        order.id,
        order.business_id,
        {
            "payment_ids": [payment.id for payment in payments],
            "table_id": order.table_id,
        },
    )
    return payments


def split_amounts(total: Decimal, parts: int) -> list[Decimal]:
    total = money(total)
    base = (total / parts).quantize(TWOPLACES, rounding=ROUND_HALF_UP)
    values = [base for _ in range(parts)]
    difference = total - sum(values, Decimal("0"))
    values[0] = money(values[0] + difference)
    return values


def cash_session_expected(db: Session, session: CashSession) -> Decimal:
    return money(cash_reconciliation(db, session)["cash_expected"])


def create_reservation(db: Session, user: AuthContext, payload: ReservationCreate) -> Reservation:
    branch = db.get(Branch, payload.branch_id)
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found")
    if not user.is_superadmin and user.business_id != branch.business_id:
        raise HTTPException(status_code=403, detail="Cross-business access denied")
    end_at = payload.start_at + timedelta(minutes=payload.duration_minutes)

    if payload.table_ids:
        tables = list(
            db.scalars(
                select(RestaurantTable).where(
                    RestaurantTable.id.in_(payload.table_ids),
                    RestaurantTable.branch_id == branch.id,
                )
            )
        )
        if len(tables) != len(set(payload.table_ids)):
            raise HTTPException(status_code=422, detail="One or more tables are invalid")
        conflicts = db.scalar(
            select(func.count(ReservationTable.table_id))
            .join(Reservation, Reservation.id == ReservationTable.reservation_id)
            .where(
                ReservationTable.table_id.in_(payload.table_ids),
                Reservation.status.in_(["confirmed", "seated"]),
                Reservation.start_at < end_at,
                Reservation.end_at > payload.start_at,
            )
        )
        if conflicts:
            raise HTTPException(status_code=409, detail="One or more tables are already reserved")
        if sum(table.capacity for table in tables) < payload.party_size:
            raise HTTPException(status_code=422, detail="Selected tables do not have enough capacity")

    customer = db.scalar(
        select(Customer).where(
            Customer.business_id == branch.business_id,
            Customer.phone == payload.customer_phone,
        )
    )
    if not customer:
        customer = Customer(
            business_id=branch.business_id,
            name=payload.customer_name,
            phone=payload.customer_phone,
        )
        db.add(customer)
        db.flush()
    else:
        customer.name = payload.customer_name

    reservation = Reservation(
        business_id=branch.business_id,
        branch_id=branch.id,
        customer_id=customer.id,
        customer_name=payload.customer_name,
        customer_phone=payload.customer_phone,
        party_size=payload.party_size,
        start_at=payload.start_at,
        end_at=end_at,
        source=payload.source,
        notes=payload.notes,
    )
    db.add(reservation)
    db.flush()
    for table_id in payload.table_ids:
        db.add(ReservationTable(reservation_id=reservation.id, table_id=table_id))
    audit(db, user, "reservation.created", "reservation", reservation.id, branch.business_id)
    return reservation


def available_tables(
    db: Session,
    branch_id: int,
    start_at: datetime,
    duration_minutes: int,
    party_size: int,
) -> list[RestaurantTable]:
    end_at = start_at + timedelta(minutes=duration_minutes)
    busy_table_ids = select(ReservationTable.table_id).join(
        Reservation, Reservation.id == ReservationTable.reservation_id
    ).where(
        Reservation.branch_id == branch_id,
        Reservation.status.in_(["confirmed", "seated"]),
        Reservation.start_at < end_at,
        Reservation.end_at > start_at,
    )
    return list(
        db.scalars(
            select(RestaurantTable)
            .where(
                RestaurantTable.branch_id == branch_id,
                RestaurantTable.capacity >= party_size,
                RestaurantTable.id.not_in(busy_table_ids),
            )
            .order_by(RestaurantTable.capacity.asc())
        )
    )


def parse_legacy_items(raw) -> list[OrderLineInput]:
    value = raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = []
    if isinstance(value, dict):
        value = value.get("items", [])
    if not isinstance(value, list):
        return []
    parsed: list[OrderLineInput] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        parsed.append(
            OrderLineInput(
                product_id=item.get("product_id"),
                name=item.get("name") or item.get("nombre") or "Producto",
                quantity=item.get("qty") or item.get("quantity") or item.get("cantidad") or 1,
                unit_price=item.get("unit_price") or item.get("price") or item.get("precio") or 0,
                notes=item.get("notes") or item.get("nota"),
            )
        )
    return parsed
