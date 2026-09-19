from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Time,
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
    order_folio_counter: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="active", index=True)
    plan: Mapped[str] = mapped_column(String(60), default="basic")
    currency: Mapped[str] = mapped_column(String(3), default="PEN")
    timezone: Mapped[str] = mapped_column(String(80), default="America/Lima")
    country_code: Mapped[str] = mapped_column(
        String(2), default="PE", server_default=text("'PE'"), nullable=False
    )
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1"), nullable=False
    )
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
    google_place_id: Mapped[str | None] = mapped_column(String(255))
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 7))
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 7))
    logo_storage_path: Mapped[str | None] = mapped_column(Text)
    cover_storage_path: Mapped[str | None] = mapped_column(Text)
    whatsapp_number: Mapped[str | None] = mapped_column(String(40))
    whatsapp_status: Mapped[str] = mapped_column(
        String(30), default="unknown", server_default=text("'unknown'"), nullable=False
    )
    yape_qr_storage_path: Mapped[str | None] = mapped_column(Text)
    menu_card_storage_path: Mapped[str | None] = mapped_column(Text)
    agent_name: Mapped[str | None] = mapped_column(String(80))
    agent_menu_images: Mapped[list | None] = mapped_column(JSON(none_as_null=True))
    agent_context_notes: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1"), nullable=False
    )

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


class AuthSecurityState(Base, TimestampMixin):
    __tablename__ = "auth_security_states"

    auth_user_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pending_operation_id: Mapped[str | None] = mapped_column(String(36))
    requires_password_reset: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class PasswordResetOperation(Base, TimestampMixin):
    __tablename__ = "password_reset_operations"
    __table_args__ = (UniqueConstraint("auth_user_id", "key_hash", name="uq_password_reset_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    auth_user_id: Mapped[str] = mapped_column(String(120), index=True)
    membership_id: Mapped[int] = mapped_column(ForeignKey("memberships.id"))
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    actor_id: Mapped[str] = mapped_column(String(120))
    key_hash: Mapped[str] = mapped_column(String(64))
    request_digest: Mapped[str] = mapped_column(String(64))
    security_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    error_code: Mapped[str | None] = mapped_column(String(60))


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
    columns: Mapped[int] = mapped_column(
        Integer,
        default=7,
        server_default=text("7"),
        nullable=False,
    )
    rows: Mapped[int] = mapped_column(
        Integer,
        default=5,
        server_default=text("5"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default=text("1"),
        nullable=False,
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


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
    image_storage_path: Mapped[str | None] = mapped_column(Text)
    service_channels: Mapped[list[str]] = mapped_column(
        JSON,
        default=lambda: [
            "pos_tables",
            "pos_counter",
            "pos_takeaway",
            "pos_delivery",
            "digital_tables",
            "digital_takeaway",
            "digital_delivery",
        ],
        nullable=False,
    )
    product_type: Mapped[str] = mapped_column(String(20), default="standard")
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
    available: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"), nullable=False)


class Promotion(Base, TimestampMixin):
    __tablename__ = "promotions"
    __table_args__ = (
        CheckConstraint(
            "promotion_type IN ('product_discount', 'buy_x_pay_y')",
            name="ck_promotions_type",
        ),
        CheckConstraint(
            "target_scope IN ('products', 'categories')",
            name="ck_promotions_target_scope",
        ),
        CheckConstraint(
            "discount_type IS NULL OR discount_type IN ('percentage', 'fixed_amount')",
            name="ck_promotions_discount_type",
        ),
        Index("ix_promotions_branch_active", "branch_id", "active", "archived_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(180))
    promotion_type: Mapped[str] = mapped_column(String(30), index=True)
    discount_type: Mapped[str | None] = mapped_column(String(30))
    discount_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    receive_quantity: Mapped[int | None] = mapped_column(Integer)
    pay_quantity: Mapped[int | None] = mapped_column(Integer)
    target_scope: Mapped[str] = mapped_column(String(30))
    starts_on: Mapped[date | None] = mapped_column(Date)
    ends_on: Mapped[date | None] = mapped_column(Date)
    weekdays: Mapped[list[int]] = mapped_column(JSON, default=list, nullable=False)
    service_channels: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)

    targets: Mapped[list[PromotionTarget]] = relationship(
        back_populates="promotion",
        cascade="all, delete-orphan",
    )


class PromotionTarget(Base):
    __tablename__ = "promotion_targets"
    __table_args__ = (
        CheckConstraint(
            "(product_id IS NOT NULL AND category_id IS NULL) OR "
            "(product_id IS NULL AND category_id IS NOT NULL)",
            name="ck_promotion_targets_single_target",
        ),
        UniqueConstraint("promotion_id", "product_id", name="uq_promotion_target_product"),
        UniqueConstraint("promotion_id", "category_id", name="uq_promotion_target_category"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    promotion_id: Mapped[int] = mapped_column(ForeignKey("promotions.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id", ondelete="CASCADE"),
        index=True,
    )

    promotion: Mapped[Promotion] = relationship(back_populates="targets")


class ModifierGroup(Base, TimestampMixin):
    __tablename__ = "modifier_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(140))
    internal_label: Mapped[str | None] = mapped_column(String(140), nullable=True)
    minimum: Mapped[int] = mapped_column(Integer, default=0)
    maximum: Mapped[int | None] = mapped_column(Integer, nullable=True)
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_repeats: Mapped[bool] = mapped_column(Boolean, default=False)
    max_per_option: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class Modifier(Base, TimestampMixin):
    __tablename__ = "modifiers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("modifier_groups.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(140))
    price_delta: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    available: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


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


class ComboItem(Base, TimestampMixin):
    __tablename__ = "combo_items"
    __table_args__ = (UniqueConstraint("product_id", "component_product_id", name="uq_combo_component"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    component_product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=Decimal("1"))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


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
        UniqueConstraint("business_id", "folio", name="uq_order_business_folio"),
        UniqueConstraint("business_id", "external_reference", name="uq_order_external_reference"),
        Index("ix_orders_branch_status_created", "branch_id", "status", "created_at"),
        Index("ix_orders_branch_created_id", "branch_id", "created_at", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    number: Mapped[str] = mapped_column(String(40))
    folio: Mapped[int | None] = mapped_column(Integer)
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
    manual_discount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    promotion_discount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    applied_promotions: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    delivery_fee: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    delivery_quote_id: Mapped[str | None] = mapped_column(
        ForeignKey("delivery_quotes.id", ondelete="SET NULL"), index=True
    )
    delivery_fee_status: Mapped[str] = mapped_column(
        String(30), default="final", server_default=text("'final'"), nullable=False
    )
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(120))
    whatsapp_chat_id: Mapped[str | None] = mapped_column(String(120), index=True)
    whatsapp_message_id: Mapped[str | None] = mapped_column(String(180), index=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    sent_to_kitchen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checkout_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    table_released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
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
    replaces_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("order_items.id", ondelete="SET NULL"),
        index=True,
    )
    cancellation_reason: Mapped[str | None] = mapped_column(Text)
    line_total: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    promotion_discount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    promotion_snapshot: Mapped[dict | None] = mapped_column(JSON)

    order: Mapped[Order] = relationship(back_populates="items")


class KitchenTicket(Base, TimestampMixin):
    __tablename__ = "kitchen_tickets"
    __table_args__ = (
        UniqueConstraint(
            "order_id",
            "station",
            "sequence",
            name="uq_ticket_order_station_sequence",
        ),
        UniqueConstraint("order_id", "sequence", name="uq_ticket_order_sequence"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    station: Mapped[str] = mapped_column(String(80), default="kitchen")
    kind: Mapped[str] = mapped_column(String(30), default="standard", index=True)
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    sequence: Mapped[int] = mapped_column(Integer, default=1)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"), nullable=False)
    items_snapshot: Mapped[list] = mapped_column(JSON, default=list)
    context_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    fired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    print_count: Mapped[int] = mapped_column(Integer, default=0)


class CashRegister(Base, TimestampMixin):
    __tablename__ = "cash_registers"
    __table_args__ = (
        Index(
            "uq_cash_registers_one_default_per_branch",
            "branch_id",
            unique=True,
            sqlite_where=text("is_default = 1"),
            postgresql_where=text("is_default IS TRUE"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1"), nullable=False
    )


class CashSession(Base, TimestampMixin):
    __tablename__ = "cash_sessions"
    __table_args__ = (
        Index(
            "uq_cash_sessions_one_open_per_register",
            "register_id",
            unique=True,
            sqlite_where=text("status = 'open'"),
            postgresql_where=text("status = 'open'"),
        ),
        Index(
            "ix_cash_sessions_branch_status_closed",
            "branch_id",
            "status",
            "closed_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    register_id: Mapped[int] = mapped_column(ForeignKey("cash_registers.id", ondelete="CASCADE"), index=True)
    previous_session_id: Mapped[int | None] = mapped_column(
        ForeignKey("cash_sessions.id", ondelete="SET NULL"),
        index=True,
    )
    status: Mapped[str] = mapped_column(String(30), default="open", index=True)
    opening_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    expected_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    declared_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    difference: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    card_expected_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    card_declared_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    card_difference: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    transfer_expected_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    total_expected_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    total_difference: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    retained_fund_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    cash_withdrawn_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    result: Mapped[str | None] = mapped_column(String(30), index=True)
    denominations: Mapped[dict | None] = mapped_column(JSON)
    pending_orders_snapshot: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    pending_orders_ignored: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    pending_orders_override_by: Mapped[str | None] = mapped_column(String(120))
    pending_orders_override_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actor_display_name: Mapped[str | None] = mapped_column(String(180))
    opened_by: Mapped[str] = mapped_column(String(120))
    closed_by: Mapped[str | None] = mapped_column(String(120))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    close_notes: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


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
        Index(
            "uq_payment_evidence_one_open_per_order",
            "order_id",
            unique=True,
            postgresql_where=text("status IN ('evidence_received', 'under_review')"),
            sqlite_where=text("status IN ('evidence_received', 'under_review')"),
        ),
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
    branch_id: Mapped[int | None] = mapped_column(
        ForeignKey("branches.id", ondelete="SET NULL"), index=True
    )
    actor_id: Mapped[str | None] = mapped_column(String(120), index=True)
    actor_display_name: Mapped[str | None] = mapped_column(String(180))
    action: Mapped[str] = mapped_column(String(100), index=True)
    entity_type: Mapped[str] = mapped_column(String(80), index=True)
    entity_id: Mapped[str | None] = mapped_column(String(120))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint(
            "business_id",
            "scope",
            "idempotency_key",
            name="uq_idempotency_business_scope_key",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
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


class BranchSettings(Base, TimestampMixin):
    __tablename__ = "branch_settings"
    __table_args__ = (UniqueConstraint("branch_id", name="uq_branch_settings_branch"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    pos_tables: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    pos_counter: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    pos_takeaway: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    pos_delivery: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    digital_tables: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    digital_takeaway: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    digital_delivery: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    delivery_mode: Mapped[str] = mapped_column(String(30), default="fixed", index=True)
    delivery_policy: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    fixed_delivery_fee: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    distance_base_fee: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    distance_fee_per_km: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    distance_max_km: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    free_delivery_threshold: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    minimum_order_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    payment_methods: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    delivery_min_minutes: Mapped[int] = mapped_column(Integer, default=25, nullable=False)
    delivery_max_minutes: Mapped[int] = mapped_column(Integer, default=45, nullable=False)
    pickup_minutes: Mapped[int] = mapped_column(Integer, default=15, nullable=False)
    advanced_printing: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    printer_config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    customer_ticket_template: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    kitchen_ticket_template: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class DeliveryBand(Base, TimestampMixin):
    __tablename__ = "delivery_bands"
    __table_args__ = (
        UniqueConstraint("branch_id", "sort_order", name="uq_delivery_band_branch_order"),
        CheckConstraint("minimum_km >= 0", name="ck_delivery_band_minimum_nonnegative"),
        CheckConstraint("maximum_km > minimum_km", name="ck_delivery_band_valid_range"),
        CheckConstraint("fee >= 0", name="ck_delivery_band_fee_nonnegative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    minimum_km: Mapped[Decimal] = mapped_column(Numeric(8, 2))
    maximum_km: Mapped[Decimal] = mapped_column(Numeric(8, 2))
    fee: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class DeliveryQuote(Base):
    __tablename__ = "delivery_quotes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    mode: Mapped[str] = mapped_column(String(30), index=True)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    distance_km: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    fee: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    minimum_order_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    configuration_version: Mapped[int] = mapped_column(Integer)
    input_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class StaffMember(Base, TimestampMixin):
    __tablename__ = "staff_members"
    __table_args__ = (
        UniqueConstraint("business_id", "auth_user_id", name="uq_staff_business_auth_user"),
        UniqueConstraint("business_id", "email", name="uq_staff_business_email"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    auth_user_id: Mapped[str | None] = mapped_column(String(120), index=True)
    email: Mapped[str | None] = mapped_column(String(240), index=True)
    first_name: Mapped[str] = mapped_column(String(120))
    last_name: Mapped[str] = mapped_column(String(120), default="")
    pin_hash: Mapped[str | None] = mapped_column(Text)
    email_access: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    failed_pin_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pin_locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class StaffMemberRole(Base):
    __tablename__ = "staff_member_roles"

    staff_member_id: Mapped[int] = mapped_column(
        ForeignKey("staff_members.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(40), primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)


class StaffMemberBranch(Base):
    __tablename__ = "staff_member_branches"

    staff_member_id: Mapped[int] = mapped_column(
        ForeignKey("staff_members.id", ondelete="CASCADE"), primary_key=True
    )
    branch_id: Mapped[int] = mapped_column(
        ForeignKey("branches.id", ondelete="CASCADE"), primary_key=True
    )
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)


class PairedDevice(Base, TimestampMixin):
    __tablename__ = "paired_devices"
    __table_args__ = (UniqueConstraint("token_hash", name="uq_paired_device_token_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(180))
    token_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    pairing_code_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    pairing_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    credential_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    staff_session_hash: Mapped[str | None] = mapped_column(String(64))
    staff_session_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    session_staff_id: Mapped[int | None] = mapped_column(Integer)
    session_access_hash: Mapped[str | None] = mapped_column(String(64))
    failed_pin_attempts: Mapped[int] = mapped_column(Integer, default=0)
    pin_locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ServiceSchedule(Base, TimestampMixin):
    __tablename__ = "service_schedules"
    __table_args__ = (
        UniqueConstraint("branch_id", "name", name="uq_service_schedule_branch_name"),
        Index(
            "uq_service_schedule_primary_branch",
            "branch_id",
            unique=True,
            sqlite_where=text("kind = 'primary' AND archived_at IS NULL"),
            postgresql_where=text("kind = 'primary' AND archived_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(180))
    kind: Mapped[str] = mapped_column(String(30), default="additional", index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class ScheduleShift(Base, TimestampMixin):
    __tablename__ = "schedule_shifts"
    __table_args__ = (
        UniqueConstraint(
            "schedule_id", "day_of_week", "starts_at", "ends_at", name="uq_schedule_shift_window"
        ),
        CheckConstraint("day_of_week >= 0 AND day_of_week <= 6", name="ck_schedule_shift_weekday"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    schedule_id: Mapped[int] = mapped_column(
        ForeignKey("service_schedules.id", ondelete="CASCADE"), index=True
    )
    day_of_week: Mapped[int] = mapped_column(Integer, index=True)
    starts_at: Mapped[time] = mapped_column(Time())
    ends_at: Mapped[time] = mapped_column(Time())
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class ScheduleAssignment(Base):
    __tablename__ = "schedule_assignments"
    __table_args__ = (
        CheckConstraint(
            "(product_id IS NOT NULL AND promotion_id IS NULL) OR "
            "(product_id IS NULL AND promotion_id IS NOT NULL)",
            name="ck_schedule_assignment_single_target",
        ),
        UniqueConstraint("schedule_id", "product_id", name="uq_schedule_assignment_product"),
        UniqueConstraint("schedule_id", "promotion_id", name="uq_schedule_assignment_promotion"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    schedule_id: Mapped[int] = mapped_column(
        ForeignKey("service_schedules.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    promotion_id: Mapped[int | None] = mapped_column(
        ForeignKey("promotions.id", ondelete="CASCADE"), index=True
    )


class PrinterDevice(Base, TimestampMixin):
    __tablename__ = "printer_devices"
    __table_args__ = (UniqueConstraint("branch_id", "system_name", name="uq_printer_branch_system_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    paired_device_id: Mapped[int | None] = mapped_column(
        ForeignKey("paired_devices.id", ondelete="SET NULL"), index=True
    )
    name: Mapped[str] = mapped_column(String(180))
    system_name: Mapped[str] = mapped_column(String(255))
    purpose: Mapped[str] = mapped_column(String(40), default="kitchen", index=True)
    paper_width_mm: Mapped[int] = mapped_column(Integer, default=80)
    copies: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class PrintJob(Base):
    __tablename__ = "print_jobs"
    __table_args__ = (
        UniqueConstraint("business_id", "idempotency_key", name="uq_print_job_business_key"),
        Index("ix_print_jobs_device_status_created", "paired_device_id", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    paired_device_id: Mapped[int | None] = mapped_column(
        ForeignKey("paired_devices.id", ondelete="SET NULL"), index=True
    )
    printer_id: Mapped[int | None] = mapped_column(
        ForeignKey("printer_devices.id", ondelete="SET NULL"), index=True
    )
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), index=True)
    kitchen_ticket_id: Mapped[int | None] = mapped_column(
        ForeignKey("kitchen_tickets.id", ondelete="SET NULL"), index=True
    )
    job_type: Mapped[str] = mapped_column(String(40), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(240))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
