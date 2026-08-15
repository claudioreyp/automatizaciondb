from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class Business(Base, TimestampMixin):
    __tablename__ = "businesses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(180), index=True)
    status: Mapped[str] = mapped_column(String(30), default="active", index=True)
    plan: Mapped[str] = mapped_column(String(60), default="basic")
    currency: Mapped[str] = mapped_column(String(3), default="PEN")
    timezone: Mapped[str] = mapped_column(String(80), default="America/Lima")
    logo_url: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(String(40))
    auto_accept_payment_evidence: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_accept_limit: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))

    branches: Mapped[list[Branch]] = relationship(back_populates="business", cascade="all, delete-orphan")


class Branch(Base, TimestampMixin):
    __tablename__ = "branches"
    __table_args__ = (UniqueConstraint("business_id", "slug", name="uq_branch_business_slug"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    slug: Mapped[str] = mapped_column(String(120))
    name: Mapped[str] = mapped_column(String(180))
    address: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(String(40))
    opening_hours: Mapped[dict] = mapped_column(JSON, default=dict)
    accepted_payment_methods: Mapped[list] = mapped_column(JSON, default=list)
    delivery_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    takeaway_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    delivery_fee: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    yape_number: Mapped[str | None] = mapped_column(String(40))
    plin_number: Mapped[str | None] = mapped_column(String(40))
    payment_recipient_name: Mapped[str | None] = mapped_column(String(180))
    maps_url: Mapped[str | None] = mapped_column(Text)
    yape_qr_storage_path: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    business: Mapped[Business] = relationship(back_populates="branches")


class Membership(Base, TimestampMixin):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("auth_user_id", "business_id", "branch_id", name="uq_membership_scope"),
        Index(
            "uq_memberships_single_active_superadmin",
            "role",
            unique=True,
            sqlite_where=text(
                "role = 'superadmin' AND business_id IS NULL AND branch_id IS NULL AND active = 1"
            ),
            postgresql_where=text(
                "role = 'superadmin' AND business_id IS NULL AND branch_id IS NULL AND active IS TRUE"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    auth_user_id: Mapped[str] = mapped_column(String(120), index=True)
    email: Mapped[str | None] = mapped_column(String(240), index=True)
    full_name: Mapped[str] = mapped_column(String(180), default="Usuario")
    business_id: Mapped[int | None] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(30), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ModuleEntitlement(Base, TimestampMixin):
    __tablename__ = "module_entitlements"
    __table_args__ = (UniqueConstraint("business_id", "module", name="uq_business_module"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    module: Mapped[str] = mapped_column(String(60))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Invitation(Base, TimestampMixin):
    __tablename__ = "invitations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(240), index=True)
    role: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_by: Mapped[str] = mapped_column(String(120))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DiningArea(Base, TimestampMixin):
    __tablename__ = "dining_areas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class RestaurantTable(Base, TimestampMixin):
    __tablename__ = "restaurant_tables"
    __table_args__ = (UniqueConstraint("branch_id", "code", name="uq_table_branch_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    area_id: Mapped[int | None] = mapped_column(ForeignKey("dining_areas.id", ondelete="SET NULL"), index=True)
    code: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(100))
    capacity: Mapped[int] = mapped_column(Integer, default=4)
    position_x: Mapped[int] = mapped_column(Integer, default=0)
    position_y: Mapped[int] = mapped_column(Integer, default=0)
    width: Mapped[int] = mapped_column(Integer, default=120)
    height: Mapped[int] = mapped_column(Integer, default=92)
    shape: Mapped[str] = mapped_column(String(20), default="round")
    status: Mapped[str] = mapped_column(String(30), default="available", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class Customer(Base, TimestampMixin):
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("business_id", "phone", name="uq_customer_business_phone"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(180))
    phone: Mapped[str] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(240))
    addresses: Mapped[list] = mapped_column(JSON, default=list)
    notes: Mapped[str | None] = mapped_column(Text)


class Category(Base, TimestampMixin):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(140))
    color: Mapped[str] = mapped_column(String(20), default="#d85b38")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Product(Base, TimestampMixin):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("branch_id", "sku", name="uq_product_branch_sku"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"), index=True)
    sku: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(180), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    image_url: Mapped[str | None] = mapped_column(Text)
    available: Mapped[bool] = mapped_column(Boolean, default=True)
    track_stock: Mapped[bool] = mapped_column(Boolean, default=False)
    preparation_station: Mapped[str] = mapped_column(String(80), default="kitchen")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class ProductVariant(Base, TimestampMixin):
    __tablename__ = "product_variants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    price_delta: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ModifierGroup(Base, TimestampMixin):
    __tablename__ = "modifier_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(140))
    minimum: Mapped[int] = mapped_column(Integer, default=0)
    maximum: Mapped[int] = mapped_column(Integer, default=1)
    required: Mapped[bool] = mapped_column(Boolean, default=False)


class Modifier(Base, TimestampMixin):
    __tablename__ = "modifiers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("modifier_groups.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(140))
    price_delta: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ProductModifierGroup(Base):
    __tablename__ = "product_modifier_groups"

    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("modifier_groups.id", ondelete="CASCADE"), primary_key=True)


class InventoryItem(Base, TimestampMixin):
    __tablename__ = "inventory_items"
    __table_args__ = (UniqueConstraint("branch_id", "sku", name="uq_inventory_branch_sku"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    sku: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(180), index=True)
    unit: Mapped[str] = mapped_column(String(30), default="unit")
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    minimum_stock: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class RecipeItem(Base, TimestampMixin):
    __tablename__ = "recipe_items"
    __table_args__ = (UniqueConstraint("product_id", "inventory_item_id", name="uq_recipe_component"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    inventory_item_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_items.id", ondelete="CASCADE"), index=True
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3))


class StockMovement(Base):
    __tablename__ = "stock_movements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    inventory_item_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_items.id", ondelete="CASCADE"), index=True
    )
    movement_type: Mapped[str] = mapped_column(String(40), index=True)
    quantity_delta: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    balance_after: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    reference_type: Mapped[str | None] = mapped_column(String(60))
    reference_id: Mapped[str | None] = mapped_column(String(120))
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Order(Base, TimestampMixin):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("branch_id", "number", name="uq_order_branch_number"),
        UniqueConstraint("business_id", "external_reference", name="uq_order_external_reference"),
        Index("ix_orders_branch_status_created", "branch_id", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    number: Mapped[str] = mapped_column(String(40))
    channel: Mapped[str] = mapped_column(String(30), default="counter", index=True)
    source: Mapped[str] = mapped_column(String(40), default="pos")
    status: Mapped[str] = mapped_column(String(40), default="draft", index=True)
    payment_status: Mapped[str] = mapped_column(String(40), default="pending", index=True)
    payment_method: Mapped[str | None] = mapped_column(String(30), index=True)
    table_id: Mapped[int | None] = mapped_column(ForeignKey("restaurant_tables.id", ondelete="SET NULL"), index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), index=True)
    external_reference: Mapped[str | None] = mapped_column(String(180))
    customer_name: Mapped[str | None] = mapped_column(String(180))
    customer_phone: Mapped[str | None] = mapped_column(String(40))
    delivery_address: Mapped[dict | None] = mapped_column(JSON)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    discount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    delivery_fee: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(120))
    whatsapp_chat_id: Mapped[str | None] = mapped_column(String(120), index=True)
    whatsapp_message_id: Mapped[str | None] = mapped_column(String(180), index=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    sent_to_kitchen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1)

    items: Mapped[list[OrderItem]] = relationship(back_populates="order", cascade="all, delete-orphan")


class OrderItem(Base, TimestampMixin):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"), index=True)
    product_name: Mapped[str] = mapped_column(String(180))
    variant_name: Mapped[str | None] = mapped_column(String(120))
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    modifiers: Mapped[list] = mapped_column(JSON, default=list)
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    line_total: Mapped[Decimal] = mapped_column(Numeric(12, 2))

    order: Mapped[Order] = relationship(back_populates="items")


class KitchenTicket(Base, TimestampMixin):
    __tablename__ = "kitchen_tickets"
    __table_args__ = (UniqueConstraint("order_id", "station", name="uq_ticket_order_station"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    station: Mapped[str] = mapped_column(String(80), default="kitchen")
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    sequence: Mapped[int] = mapped_column(Integer, default=1)
    items_snapshot: Mapped[list] = mapped_column(JSON, default=list)
    fired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    print_count: Mapped[int] = mapped_column(Integer, default=0)


class CashRegister(Base, TimestampMixin):
    __tablename__ = "cash_registers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class CashSession(Base, TimestampMixin):
    __tablename__ = "cash_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    register_id: Mapped[int] = mapped_column(ForeignKey("cash_registers.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="open", index=True)
    opening_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    expected_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    declared_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    difference: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    opened_by: Mapped[str] = mapped_column(String(120))
    closed_by: Mapped[str | None] = mapped_column(String(120))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    close_notes: Mapped[str | None] = mapped_column(Text)


class CashMovement(Base):
    __tablename__ = "cash_movements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cash_session_id: Mapped[int] = mapped_column(ForeignKey("cash_sessions.id", ondelete="CASCADE"), index=True)
    movement_type: Mapped[str] = mapped_column(String(30), index=True)
    payment_method: Mapped[str | None] = mapped_column(String(30))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    reference_type: Mapped[str | None] = mapped_column(String(60))
    reference_id: Mapped[str | None] = mapped_column(String(120))
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Payment(Base, TimestampMixin):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    cash_session_id: Mapped[int | None] = mapped_column(ForeignKey("cash_sessions.id", ondelete="SET NULL"), index=True)
    method: Mapped[str] = mapped_column(String(30), index=True)
    status: Mapped[str] = mapped_column(String(30), default="confirmed", index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    external_reference: Mapped[str | None] = mapped_column(String(180))
    note: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by: Mapped[str | None] = mapped_column(String(120))


class PaymentAllocation(Base):
    __tablename__ = "payment_allocations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id", ondelete="CASCADE"), index=True)
    order_item_id: Mapped[int | None] = mapped_column(ForeignKey("order_items.id", ondelete="SET NULL"), index=True)
    label: Mapped[str | None] = mapped_column(String(120))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))


class Reservation(Base, TimestampMixin):
    __tablename__ = "reservations"
    __table_args__ = (Index("ix_reservations_branch_time", "branch_id", "start_at", "end_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), index=True)
    customer_name: Mapped[str] = mapped_column(String(180))
    customer_phone: Mapped[str] = mapped_column(String(40))
    party_size: Mapped[int] = mapped_column(Integer)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(30), default="confirmed", index=True)
    source: Mapped[str] = mapped_column(String(30), default="manual")
    notes: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)


class ReservationTable(Base):
    __tablename__ = "reservation_tables"

    reservation_id: Mapped[int] = mapped_column(ForeignKey("reservations.id", ondelete="CASCADE"), primary_key=True)
    table_id: Mapped[int] = mapped_column(ForeignKey("restaurant_tables.id", ondelete="CASCADE"), primary_key=True)


class Courier(Base, TimestampMixin):
    __tablename__ = "couriers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(180))
    phone: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(30), default="available", index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class DeliveryAssignment(Base, TimestampMixin):
    __tablename__ = "delivery_assignments"
    __table_args__ = (UniqueConstraint("order_id", name="uq_delivery_order"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    courier_id: Mapped[int | None] = mapped_column(ForeignKey("couriers.id", ondelete="SET NULL"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="preparing", index=True)
    address: Mapped[dict] = mapped_column(JSON, default=dict)
    fee: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    tracking_code: Mapped[str | None] = mapped_column(String(80), unique=True)
    estimated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PaymentEvidence(Base, TimestampMixin):
    __tablename__ = "payment_evidence"
    __table_args__ = (
        UniqueConstraint("business_id", "provider", "operation_number", name="uq_payment_operation"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(30), index=True)
    storage_path: Mapped[str] = mapped_column(Text)
    amount_detected: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    operation_number: Mapped[str | None] = mapped_column(String(120))
    security_code: Mapped[str | None] = mapped_column(String(3), index=True)
    image_sha256: Mapped[str] = mapped_column(String(64), index=True)
    whatsapp_message_id: Mapped[str | None] = mapped_column(String(180), index=True)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recipient: Mapped[str | None] = mapped_column(String(180))
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    status: Mapped[str] = mapped_column(String(40), default="evidence_received", index=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[str | None] = mapped_column(String(120))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    analysis: Mapped[dict] = mapped_column(JSON, default=dict)
    warnings: Mapped[list] = mapped_column(JSON, default=list)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int | None] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    actor_id: Mapped[str | None] = mapped_column(String(120), index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    entity_type: Mapped[str] = mapped_column(String(80), index=True)
    entity_id: Mapped[str | None] = mapped_column(String(120))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("scope", "idempotency_key", name="uq_idempotency_scope_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int | None] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    scope: Mapped[str] = mapped_column(String(120))
    idempotency_key: Mapped[str] = mapped_column(String(240))
    response_code: Mapped[int] = mapped_column(Integer, default=200)
    response_body: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IntegrationCredential(Base, TimestampMixin):
    __tablename__ = "integration_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    token_prefix: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(String(120))


class IntegrationEvent(Base):
    __tablename__ = "integration_events"
    __table_args__ = (
        Index("ix_integration_events_branch_pending", "branch_id", "acknowledged_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    aggregate_type: Mapped[str] = mapped_column(String(60))
    aggregate_id: Mapped[str] = mapped_column(String(120), index=True)
    customer_phone: Mapped[str | None] = mapped_column(String(40), index=True)
    whatsapp_chat_id: Mapped[str | None] = mapped_column(String(120), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    acknowledged_by: Mapped[str | None] = mapped_column(String(120))
    delivery_attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
