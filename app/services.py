from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .auth import AuthContext
from .models import (
    AuditEvent,
    Branch,
    CashMovement,
    CashSession,
    Customer,
    IdempotencyRecord,
    InventoryItem,
    IntegrationEvent,
    KitchenTicket,
    Order,
    OrderItem,
    Payment,
    PaymentAllocation,
    Modifier,
    Product,
    ProductModifierGroup,
    ProductVariant,
    RecipeItem,
    Reservation,
    ReservationTable,
    RestaurantTable,
    StockMovement,
    utcnow,
)
from .schemas import OrderCreate, OrderLineInput, PaymentCreate, ReservationCreate


TWOPLACES = Decimal("0.01")


def money(value: Decimal | int | float | str | None) -> Decimal:
    return Decimal(str(value or 0)).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def decimal_json(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def audit(
    db: Session,
    user: AuthContext | None,
    action: str,
    entity_type: str,
    entity_id: int | str | None,
    business_id: int | None,
    payload: dict | None = None,
) -> None:
    db.add(
        AuditEvent(
            business_id=business_id,
            actor_id=user.user_id if user else "system",
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id is not None else None,
            payload=payload or {},
        )
    )


def get_idempotent_response(db: Session, scope: str, key: str | None) -> dict | None:
    if not key:
        return None
    record = db.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.scope == scope,
            IdempotencyRecord.idempotency_key == key,
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
        "delivery_fee": float(order.delivery_fee or 0),
        "total": float(order.total or 0),
        "notes": order.notes,
        "external_reference": order.external_reference,
        "whatsapp_chat_id": order.whatsapp_chat_id,
        "whatsapp_message_id": order.whatsapp_message_id,
        "submitted_at": order.submitted_at,
        "version": order.version,
        "sent_to_kitchen_at": order.sent_to_kitchen_at,
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
                "line_total": float(item.line_total),
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


def recalculate_order(order: Order) -> None:
    subtotal = sum((money(item.line_total) for item in order.items), Decimal("0"))
    order.subtotal = money(subtotal)
    order.total = max(money(order.subtotal - money(order.discount) + money(order.delivery_fee)), Decimal("0"))


def build_order_item(db: Session, business_id: int, branch_id: int, line: OrderLineInput) -> OrderItem:
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
    if product is None and (not line.name or line.unit_price is None):
        raise HTTPException(status_code=422, detail="Ad-hoc order items require name and unit_price")

    variant_name = line.variant_name
    variant_delta = Decimal("0")
    if product and variant_name:
        variant = db.scalar(
            select(ProductVariant).where(
                ProductVariant.product_id == product.id,
                func.lower(ProductVariant.name) == variant_name.strip().lower(),
                ProductVariant.active.is_(True),
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
    for selection in line.modifiers:
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
        modifier_total += money(modifier.price_delta)
        sanitized_modifiers.append(
            {
                "modifier_id": modifier.id,
                "name": modifier.name,
                "price_delta": float(money(modifier.price_delta)),
            }
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


def create_order(db: Session, user: AuthContext, payload: OrderCreate) -> Order:
    branch = db.scalar(select(Branch).where(Branch.id == payload.branch_id, Branch.active.is_(True)))
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found")
    if not user.is_superadmin and user.business_id != branch.business_id:
        raise HTTPException(status_code=403, detail="Cross-business access denied")

    if payload.table_id:
        table = db.scalar(
            select(RestaurantTable).where(
                RestaurantTable.id == payload.table_id,
                RestaurantTable.branch_id == payload.branch_id,
            )
        )
        if not table:
            raise HTTPException(status_code=422, detail="Table does not belong to branch")
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
        notes=payload.notes,
        external_reference=payload.external_reference,
        created_by=user.user_id,
    )
    for line in payload.items:
        order.items.append(build_order_item(db, branch.business_id, branch.id, line))
    recalculate_order(order)
    db.add(order)
    db.flush()
    if payload.table_id:
        table = db.get(RestaurantTable, payload.table_id)
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
    order.items.append(build_order_item(db, order.business_id, order.branch_id, line))
    recalculate_order(order)
    order.version += 1
    db.flush()
    audit(db, user, "order.item_added", "order", order.id, order.business_id)
    return order


def commit_order_stock(db: Session, user: AuthContext, order: Order) -> None:
    requirements: dict[int, Decimal] = {}
    for item in order.items:
        if not item.product_id:
            continue
        components = db.scalars(select(RecipeItem).where(RecipeItem.product_id == item.product_id)).all()
        for component in components:
            requirements[component.inventory_item_id] = requirements.get(
                component.inventory_item_id, Decimal("0")
            ) + Decimal(str(component.quantity)) * Decimal(str(item.quantity))

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


def confirm_order(db: Session, user: AuthContext, order: Order) -> Order:
    if order.status == "confirmed":
        return order
    if order.status not in {"draft", "pending_confirmation"}:
        raise HTTPException(status_code=409, detail="Only draft orders can be confirmed")
    if not order.items:
        raise HTTPException(status_code=422, detail="Order must contain at least one item")
    commit_order_stock(db, user, order)
    order.status = "confirmed"
    order.version += 1
    audit(db, user, "order.confirmed", "order", order.id, order.business_id)
    return order


def send_order_to_kitchen(db: Session, user: AuthContext, order: Order) -> list[KitchenTicket]:
    if order.status == "sent_to_kitchen":
        return list(db.scalars(select(KitchenTicket).where(KitchenTicket.order_id == order.id)))
    if order.status != "confirmed":
        raise HTTPException(status_code=409, detail="Confirm the order before sending it to kitchen")

    grouped: dict[str, list[dict]] = {}
    for item in order.items:
        station = "kitchen"
        if item.product_id:
            product = db.get(Product, item.product_id)
            station = product.preparation_station if product else "kitchen"
        grouped.setdefault(station, []).append(
            {
                "item_id": item.id,
                "name": item.product_name,
                "quantity": float(item.quantity),
                "modifiers": item.modifiers,
                "notes": item.notes,
            }
        )

    tickets: list[KitchenTicket] = []
    for station, items in grouped.items():
        existing = db.scalar(
            select(KitchenTicket).where(KitchenTicket.order_id == order.id, KitchenTicket.station == station)
        )
        if existing:
            tickets.append(existing)
            continue
        ticket = KitchenTicket(
            business_id=order.business_id,
            branch_id=order.branch_id,
            order_id=order.id,
            station=station,
            items_snapshot=items,
        )
        db.add(ticket)
        tickets.append(ticket)
    order.status = "sent_to_kitchen"
    order.sent_to_kitchen_at = utcnow()
    order.version += 1
    audit(db, user, "order.sent_to_kitchen", "order", order.id, order.business_id)
    db.flush()
    return tickets


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
                StockMovement.movement_type == "sale_consumption",
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
    for movement in movements:
        inventory = db.scalar(
            select(InventoryItem).where(InventoryItem.id == movement.inventory_item_id).with_for_update()
        )
        if not inventory:
            continue
        restored = abs(Decimal(str(movement.quantity_delta)))
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


def transition_order(db: Session, user: AuthContext, order: Order, next_status: str) -> Order:
    if next_status == order.status:
        return order
    if next_status not in ORDER_TRANSITIONS.get(order.status, set()):
        raise HTTPException(status_code=409, detail=f"Cannot transition {order.status} to {next_status}")
    if next_status == "closed" and order.payment_status != "paid":
        raise HTTPException(status_code=409, detail="Order must be fully paid before closing")
    if next_status == "cancelled":
        reverse_order_stock(db, user, order)
    order.status = next_status
    order.version += 1
    if next_status == "closed":
        order.closed_at = utcnow()
        if order.table_id:
            table = db.get(RestaurantTable, order.table_id)
            if table:
                table.status = "cleaning"
                table.version += 1
    audit(db, user, f"order.{next_status}", "order", order.id, order.business_id)
    return order


def add_payment(db: Session, user: AuthContext, order: Order, payload: PaymentCreate) -> Payment:
    if order.status in {"cancelled", "closed"}:
        raise HTTPException(status_code=409, detail="Cannot add a payment to this order")
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
    if payload.cash_session_id:
        cash_session = db.get(CashSession, payload.cash_session_id)
        if not cash_session or cash_session.status != "open" or cash_session.branch_id != order.branch_id:
            raise HTTPException(status_code=422, detail="Cash session is not open for this branch")

    payment = Payment(
        business_id=order.business_id,
        order_id=order.id,
        cash_session_id=payload.cash_session_id,
        method=payload.method,
        amount=money(payload.amount),
        external_reference=payload.external_reference,
        note=payload.note,
        created_by=user.user_id,
    )
    db.add(payment)
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
    if payload.cash_session_id:
        db.add(
            CashMovement(
                cash_session_id=payload.cash_session_id,
                movement_type="sale",
                payment_method=payload.method,
                amount=money(payload.amount),
                reference_type="order",
                reference_id=str(order.id),
                created_by=user.user_id,
            )
        )
    paid_total += money(payload.amount)
    order.payment_status = "paid" if paid_total >= money(order.total) else "partial"
    order.version += 1
    audit(db, user, "payment.created", "payment", payment.id, order.business_id, {"order_id": order.id})
    return payment


def split_amounts(total: Decimal, parts: int) -> list[Decimal]:
    total = money(total)
    base = (total / parts).quantize(TWOPLACES, rounding=ROUND_HALF_UP)
    values = [base for _ in range(parts)]
    difference = total - sum(values, Decimal("0"))
    values[0] = money(values[0] + difference)
    return values


def cash_session_expected(db: Session, session: CashSession) -> Decimal:
    movements = list(db.scalars(select(CashMovement).where(CashMovement.cash_session_id == session.id)))
    expected = money(session.opening_amount)
    for movement in movements:
        if movement.payment_method != "cash":
            continue
        if movement.movement_type in {"sale", "income"}:
            expected += money(movement.amount)
        elif movement.movement_type in {"withdrawal", "expense", "refund"}:
            expected -= money(movement.amount)
    return money(expected)


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
