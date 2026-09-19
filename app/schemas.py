from datetime import date, datetime, time
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from .printing_settings import FONT_SIZES


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class BusinessCreate(ApiModel):
    name: str = Field(min_length=2, max_length=180)
    slug: str = Field(min_length=2, max_length=120, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    plan: str = "basic"
    owner_email: str | None = None
    modules: list[str] = Field(default_factory=lambda: ["pos", "tables", "kds", "inventory", "cash"])


class BusinessUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=2, max_length=180)
    status: Literal["active", "suspended"] | None = None
    plan: str | None = None
    phone: str | None = None
    logo_url: str | None = None
    auto_accept_payment_evidence: bool | None = None
    auto_accept_limit: Decimal | None = Field(default=None, ge=0)
    modules: dict[str, bool] | None = None


class BranchCreate(ApiModel):
    name: str = Field(min_length=2, max_length=180)
    slug: str = Field(min_length=2, max_length=120, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    address: str | None = None
    phone: str | None = None
    opening_hours: dict = Field(default_factory=dict)
    accepted_payment_methods: list[str] = Field(default_factory=lambda: ["cash", "card", "yape", "plin"])
    delivery_enabled: bool = True
    takeaway_enabled: bool = True
    delivery_fee: Decimal = Field(default=Decimal("0"), ge=0)
    yape_number: str | None = None
    plin_number: str | None = None
    payment_recipient_name: str | None = None
    maps_url: str | None = None
    yape_qr_storage_path: str | None = None
    agent_context_notes: str | None = Field(default=None, max_length=5000)


class BranchUpdate(ApiModel):
    name: str | None = None
    address: str | None = None
    phone: str | None = None
    opening_hours: dict | None = None
    accepted_payment_methods: list[str] | None = None
    delivery_enabled: bool | None = None
    takeaway_enabled: bool | None = None
    delivery_fee: Decimal | None = Field(default=None, ge=0)
    yape_number: str | None = None
    plin_number: str | None = None
    payment_recipient_name: str | None = None
    maps_url: str | None = None
    yape_qr_storage_path: str | None = None
    agent_context_notes: str | None = Field(default=None, max_length=5000)
    active: bool | None = None


INTEGRATION_SCOPES = {
    "menu:read",
    "inventory:read",
    "inventory:write",
    "orders:read",
    "orders:write",
    "payments:write",
    "reservations:write",
    "events:read",
}

DEFAULT_AGENT_SCOPES = INTEGRATION_SCOPES - {"inventory:write"}


class IntegrationCredentialCreate(ApiModel):
    branch_id: int
    name: str = Field(min_length=2, max_length=120)
    scopes: list[str] = Field(default_factory=lambda: sorted(DEFAULT_AGENT_SCOPES))
    expires_at: datetime | None = None

    @field_validator("scopes")
    @classmethod
    def validate_scopes(cls, value: list[str]) -> list[str]:
        normalized = sorted(set(value))
        invalid = set(normalized) - INTEGRATION_SCOPES
        if invalid:
            raise ValueError(f"Unsupported integration scopes: {', '.join(sorted(invalid))}")
        if not normalized:
            raise ValueError("At least one integration scope is required")
        return normalized


class RestaurantOnboardingCreate(ApiModel):
    business: BusinessCreate
    branch: BranchCreate
    owner_name: str = Field(default="Propietario", min_length=2, max_length=180)
    owner_email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    owner_password: SecretStr = Field(min_length=12, max_length=128)
    credential_name: str = Field(default="Integracion principal", min_length=2, max_length=120)
    integration_scopes: list[str] = Field(default_factory=lambda: sorted(DEFAULT_AGENT_SCOPES))

    @field_validator("owner_password")
    @classmethod
    def validate_owner_password(cls, value: SecretStr) -> SecretStr:
        password = value.get_secret_value()
        checks = (
            any(character.islower() for character in password),
            any(character.isupper() for character in password),
            any(character.isdigit() for character in password),
            any(not character.isalnum() for character in password),
        )
        if not all(checks):
            raise ValueError(
                "La contraseña debe incluir mayúscula, minúscula, número y símbolo"
            )
        return value

    @field_validator("integration_scopes")
    @classmethod
    def validate_integration_scopes(cls, value: list[str]) -> list[str]:
        normalized = sorted(set(value))
        invalid = set(normalized) - INTEGRATION_SCOPES
        if invalid:
            raise ValueError(f"Unsupported integration scopes: {', '.join(sorted(invalid))}")
        if not normalized:
            raise ValueError("At least one integration scope is required")
        return normalized


class InvitationCreate(ApiModel):
    business_id: int
    branch_id: int | None = None
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    role: Literal["owner", "manager", "cashier", "waiter", "kitchen", "dispatcher"]


class InvitationAccept(ApiModel):
    token: str = Field(min_length=20)


class MembershipUpdate(ApiModel):
    role: Literal["owner", "manager", "cashier", "waiter", "kitchen", "dispatcher"] | None = None
    active: bool | None = None


class CategoryCreate(ApiModel):
    branch_id: int
    name: str = Field(min_length=1, max_length=140)
    color: str = "#d85b38"
    sort_order: int = 0


class CategoryUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=140)
    color: str | None = None
    sort_order: int | None = None
    active: bool | None = None


class CategoryOrderUpdate(ApiModel):
    branch_id: int
    category_ids: list[int] = Field(min_length=1)


class ProductOrderUpdate(ApiModel):
    branch_id: int
    category_id: int | None = None
    product_ids: list[int] = Field(min_length=1)


ProductServiceChannel = Literal[
    "pos_tables",
    "pos_counter",
    "pos_takeaway",
    "pos_delivery",
    "digital_tables",
    "digital_takeaway",
    "digital_delivery",
]

DEFAULT_PRODUCT_SERVICE_CHANNELS = [
    "pos_tables",
    "pos_counter",
    "pos_takeaway",
    "pos_delivery",
    "digital_tables",
    "digital_takeaway",
    "digital_delivery",
]


class ProductCreate(ApiModel):
    branch_id: int
    category_id: int | None = None
    sku: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=180)
    description: str | None = None
    price: Decimal = Field(gt=0)
    image_url: str | None = None
    service_channels: list[ProductServiceChannel] = Field(
        default_factory=lambda: list(DEFAULT_PRODUCT_SERVICE_CHANNELS)
    )
    product_type: Literal["standard", "combo"] = "standard"
    available: bool = True
    track_stock: bool = False
    preparation_station: str = "kitchen"
    sort_order: int = 0


class ProductUpdate(ApiModel):
    category_id: int | None = None
    sku: str | None = None
    name: str | None = None
    description: str | None = None
    price: Decimal | None = Field(default=None, gt=0)
    image_url: str | None = None
    service_channels: list[ProductServiceChannel] | None = None
    product_type: Literal["standard", "combo"] | None = None
    available: bool | None = None
    track_stock: bool | None = None
    preparation_station: str | None = None
    sort_order: int | None = None


class PromotionCreate(ApiModel):
    branch_id: int
    name: str = Field(min_length=2, max_length=180)
    promotion_type: Literal["product_discount", "buy_x_pay_y"]
    discount_type: Literal["percentage", "fixed_amount"] | None = None
    discount_value: Decimal | None = Field(default=None, gt=0)
    receive_quantity: int | None = Field(default=None, ge=1, le=100)
    pay_quantity: int | None = Field(default=None, ge=1, le=100)
    target_scope: Literal["products", "categories"]
    target_ids: list[int] = Field(min_length=1)
    starts_on: date | None = None
    ends_on: date | None = None
    weekdays: list[int] = Field(default_factory=list)
    service_channels: list[ProductServiceChannel] = Field(
        default_factory=lambda: list(DEFAULT_PRODUCT_SERVICE_CHANNELS)
    )
    active: bool = True
    sort_order: int = Field(default=0, ge=0, le=10000)

    @field_validator("target_ids")
    @classmethod
    def unique_target_ids(cls, value: list[int]) -> list[int]:
        if any(item <= 0 for item in value):
            raise ValueError("Los productos o categorías seleccionados no son válidos")
        if len(set(value)) != len(value):
            raise ValueError("No repitas productos o categorías en la promoción")
        return value

    @field_validator("weekdays")
    @classmethod
    def valid_weekdays(cls, value: list[int]) -> list[int]:
        if any(day < 0 or day > 6 for day in value):
            raise ValueError("Los días de la semana deben estar entre 0 y 6")
        if len(set(value)) != len(value):
            raise ValueError("No repitas días de la semana")
        return sorted(value)

    @field_validator("service_channels")
    @classmethod
    def unique_service_channels(
        cls,
        value: list[ProductServiceChannel],
    ) -> list[ProductServiceChannel]:
        if not value:
            raise ValueError("Selecciona al menos un canal de venta")
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def validate_promotion_rules(self) -> "PromotionCreate":
        if self.starts_on and self.ends_on and self.ends_on < self.starts_on:
            raise ValueError("La fecha final no puede ser anterior a la inicial")
        if self.promotion_type == "product_discount":
            if self.discount_type is None or self.discount_value is None:
                raise ValueError("Completa el tipo y el valor del descuento")
            if self.discount_type == "percentage" and self.discount_value > 100:
                raise ValueError("El porcentaje no puede superar 100")
            self.receive_quantity = None
            self.pay_quantity = None
        else:
            if self.receive_quantity is None or self.pay_quantity is None:
                raise ValueError("Completa las cantidades a recibir y pagar")
            if self.receive_quantity <= self.pay_quantity:
                raise ValueError("La cantidad a recibir debe ser mayor que la cantidad a pagar")
            self.discount_type = None
            self.discount_value = None
        return self


class PromotionUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=2, max_length=180)
    promotion_type: Literal["product_discount", "buy_x_pay_y"] | None = None
    discount_type: Literal["percentage", "fixed_amount"] | None = None
    discount_value: Decimal | None = Field(default=None, gt=0)
    receive_quantity: int | None = Field(default=None, ge=1, le=100)
    pay_quantity: int | None = Field(default=None, ge=1, le=100)
    target_scope: Literal["products", "categories"] | None = None
    target_ids: list[int] | None = None
    starts_on: date | None = None
    ends_on: date | None = None
    weekdays: list[int] | None = None
    service_channels: list[ProductServiceChannel] | None = None
    active: bool | None = None
    sort_order: int | None = Field(default=None, ge=0, le=10000)
    expected_version: int | None = None


class ProductAvailabilityUpdate(ApiModel):
    available: bool


class ProductVariantCreate(ApiModel):
    name: str = Field(min_length=1, max_length=120)
    price_delta: Decimal = Decimal("0")
    active: bool = True


class ProductVariantUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    price_delta: Decimal | None = None
    active: bool | None = None


class ModifierOptionInput(ApiModel):
    id: int | None = None
    name: str = Field(min_length=1, max_length=140)
    price_delta: Decimal = Field(default=Decimal("0"), ge=0)
    active: bool = True
    sort_order: int = Field(default=0, ge=0, le=10000)


class ModifierGroupCreate(ApiModel):
    branch_id: int
    name: str = Field(min_length=1, max_length=140)
    internal_label: str | None = Field(default=None, max_length=140)
    minimum: int = Field(default=0, ge=0)
    maximum: int | None = Field(default=1, ge=1)
    required: bool = False
    allow_repeats: bool = False
    max_per_option: int | None = Field(default=None, ge=1)
    sort_order: int = Field(default=0, ge=0, le=10000)
    modifiers: list[ModifierOptionInput] | None = None


class ModifierGroupUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=140)
    internal_label: str | None = Field(default=None, max_length=140)
    minimum: int | None = Field(default=None, ge=0)
    maximum: int | None = Field(default=None, ge=1)
    required: bool | None = None
    allow_repeats: bool | None = None
    max_per_option: int | None = Field(default=None, ge=1)
    sort_order: int | None = Field(default=None, ge=0, le=10000)
    modifiers: list[ModifierOptionInput] | None = None


class ModifierGroupOrderUpdate(ApiModel):
    branch_id: int
    group_ids: list[int] = Field(min_length=1)


class ModifierCreate(ApiModel):
    name: str = Field(min_length=1, max_length=140)
    price_delta: Decimal = Decimal("0")
    active: bool = True
    sort_order: int = Field(default=0, ge=0, le=10000)


class ModifierUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=140)
    price_delta: Decimal | None = None
    active: bool | None = None
    sort_order: int | None = Field(default=None, ge=0, le=10000)


class ProductModifierGroupsReplace(ApiModel):
    group_ids: list[int] = Field(default_factory=list)


class IngredientCreate(ApiModel):
    branch_id: int
    sku: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=180)
    unit: str = Field(default="unit", min_length=1, max_length=30)


class IngredientUpdate(ApiModel):
    sku: str | None = Field(default=None, min_length=1, max_length=80)
    name: str | None = Field(default=None, min_length=1, max_length=180)
    unit: str | None = Field(default=None, min_length=1, max_length=30)
    active: bool | None = None


class InventoryItemCreate(ApiModel):
    branch_id: int
    sku: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=180)
    unit: str = "unit"
    quantity: Decimal = Decimal("0")
    minimum_stock: Decimal = Decimal("0")
    unit_cost: Decimal = Decimal("0")


class StockAdjustment(ApiModel):
    quantity_delta: Decimal
    movement_type: Literal["purchase", "adjustment", "waste", "transfer", "return"] = "adjustment"
    note: str | None = None
    expected_version: int | None = None


class RecipeComponent(ApiModel):
    inventory_item_id: int
    quantity: Decimal = Field(gt=0)


class RecipeReplace(ApiModel):
    components: list[RecipeComponent]


class ComboComponent(ApiModel):
    product_id: int
    quantity: Decimal = Field(default=Decimal("1"), gt=0)
    sort_order: int = 0


class ComboReplace(ApiModel):
    components: list[ComboComponent]


class AreaCreate(ApiModel):
    branch_id: int
    name: str = Field(min_length=1, max_length=120)
    sort_order: int = 0
    columns: int = Field(default=7, ge=2, le=12)
    rows: int = Field(default=5, ge=2, le=10)


class AreaUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    sort_order: int | None = None
    columns: int | None = Field(default=None, ge=2, le=12)
    rows: int | None = Field(default=None, ge=2, le=10)
    expected_version: int = Field(ge=1)


class TableCreate(ApiModel):
    branch_id: int
    area_id: int | None = None
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=100)
    capacity: int = Field(default=4, ge=1, le=50)
    position_x: int = Field(default=0, ge=0)
    position_y: int = Field(default=0, ge=0)
    width: int = 120
    height: int = 92
    shape: Literal["round", "square", "rectangle"] = "round"


class TableUpdate(ApiModel):
    area_id: int | None = None
    name: str | None = None
    capacity: int | None = Field(default=None, ge=1, le=50)
    position_x: int | None = Field(default=None, ge=0)
    position_y: int | None = Field(default=None, ge=0)
    width: int | None = None
    height: int | None = None
    shape: Literal["round", "square", "rectangle"] | None = None
    status: Literal["available", "reserved", "occupied", "cleaning"] | None = None
    expected_version: int | None = None


class ModifierSelection(ApiModel):
    modifier_id: int | None = None
    name: str
    price_delta: Decimal = Decimal("0")


class OrderLineInput(ApiModel):
    product_id: int | None = None
    name: str | None = None
    variant_name: str | None = None
    quantity: Decimal = Field(gt=0)
    unit_price: Decimal | None = Field(default=None, ge=0)
    modifiers: list[ModifierSelection] = Field(default_factory=list)
    notes: str | None = None


class OrderCreate(ApiModel):
    branch_id: int
    channel: Literal["dine_in", "counter", "takeaway", "delivery", "online", "whatsapp"] = "counter"
    source: str = "pos"
    table_id: int | None = None
    customer_id: int | None = None
    customer_name: str | None = None
    customer_phone: str | None = None
    payment_method: Literal["cash", "yape", "plin", "card", "transfer", "online"] | None = None
    whatsapp_chat_id: str | None = None
    whatsapp_message_id: str | None = None
    delivery_address: dict | None = None
    delivery_quote_id: str | None = None
    delivery_fee: Decimal = Field(default=Decimal("0"), ge=0)
    discount: Decimal = Field(default=Decimal("0"), ge=0)
    notes: str | None = None
    external_reference: str | None = None
    items: list[OrderLineInput] = Field(default_factory=list)


class OrderPatch(ApiModel):
    channel: Literal["counter", "takeaway", "delivery"] | None = None
    expected_total: Decimal | None = Field(default=None, ge=0)
    table_id: int | None = None
    customer_name: str | None = None
    customer_phone: str | None = None
    payment_method: Literal["cash", "yape", "plin", "card", "transfer", "online"] | None = None
    whatsapp_chat_id: str | None = None
    whatsapp_message_id: str | None = None
    delivery_address: dict | None = None
    delivery_quote_id: str | None = None
    delivery_fee: Decimal | None = Field(default=None, ge=0)
    discount: Decimal | None = Field(default=None, ge=0)
    notes: str | None = None
    items: list[OrderLineInput] | None = None
    expected_version: int | None = None


class AddOrderItem(ApiModel):
    item: OrderLineInput
    expected_version: int | None = None


class OrderItemBatch(ApiModel):
    items: list[OrderLineInput] = Field(min_length=1, max_length=100)
    expected_version: int | None = None


class OrderItemRevisionOperation(ApiModel):
    type: Literal["edit", "cancel"]
    item_id: int
    replacement: OrderLineInput | None = None
    reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_operation(self):
        if self.type == "edit" and self.replacement is None:
            raise ValueError("An edited item requires its replacement")
        if self.type == "cancel" and not (self.reason or "").strip():
            raise ValueError("A cancellation reason is required")
        return self


class OrderItemRevision(ApiModel):
    operations: list[OrderItemRevisionOperation] = Field(min_length=1, max_length=100)
    expected_version: int | None = None


class OrderCommand(ApiModel):
    expected_version: int | None = None


class OrderTransition(ApiModel):
    status: Literal["preparing", "ready", "dispatched", "delivered", "closed", "cancelled"]
    expected_version: int | None = None
    reason: str | None = Field(default=None, max_length=1000)

    @field_validator("reason", mode="before")
    @classmethod
    def trim_reason(cls, value):
        return (value.strip() or None) if isinstance(value, str) else value


class PaymentAllocationInput(ApiModel):
    order_item_id: int | None = None
    label: str | None = None
    amount: Decimal = Field(gt=0)


class PaymentCreate(ApiModel):
    method: Literal["cash", "card", "yape", "plin", "transfer", "online"]
    amount: Decimal = Field(gt=0)
    cash_session_id: int | None = None
    register_id: int | None = None
    external_reference: str | None = None
    note: str | None = None
    allocations: list[PaymentAllocationInput] = Field(default_factory=list)
    expected_version: int | None = None


class TableCheckoutPayment(ApiModel):
    payments: list[PaymentCreate] = Field(min_length=1, max_length=20)
    expected_version: int | None = None


class SplitPreview(ApiModel):
    parts: int = Field(ge=2, le=50)


class TicketTransition(ApiModel):
    status: Literal["preparing", "ready", "served", "cancelled"]
    expected_status: Literal["queued", "preparing", "ready", "served", "cancelled"]
    expected_version: int | None = Field(default=None, ge=1)


class KitchenCommandAction(ApiModel):
    expected_status: Literal["queued", "preparing", "ready", "served", "cancelled"]
    expected_version: int | None = Field(default=None, ge=1)


class RegisterCreate(ApiModel):
    branch_id: int
    name: str = Field(min_length=1, max_length=120)
    is_default: bool = False


class CashSessionOpen(ApiModel):
    register_id: int
    opening_amount: Decimal = Field(default=Decimal("0"), ge=0)


class CashMovementCreate(ApiModel):
    movement_type: Literal["income", "withdrawal", "expense"]
    amount: Decimal = Field(gt=0)
    payment_method: str = "cash"
    note: str | None = None


class CashSessionClose(ApiModel):
    declared_amount: Decimal = Field(ge=0)
    notes: str | None = None


class CashCutCreate(ApiModel):
    cash_counted: Decimal = Field(ge=0)
    card_counted: Decimal | None = Field(default=None, ge=0)
    retained_fund: Decimal = Field(default=Decimal("0"), ge=0)
    denominations: dict[str, int] | None = None
    ignore_pending_orders: bool = False
    note: str | None = Field(default=None, max_length=2000)
    expected_version: int = Field(ge=0)

    @field_validator("denominations", mode="before")
    @classmethod
    def validate_denominations(cls, value):
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("Denominations must be an object")
        for count in value.values():
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("Denomination counts must be nonnegative integers")
        return value


class CashRegisterMovementCreate(ApiModel):
    movement_type: Literal["income", "withdrawal"]
    amount: Decimal = Field(gt=0)
    note: str = Field(min_length=1, max_length=1000)
    expected_version: int = Field(ge=0)


class ReservationCreate(ApiModel):
    branch_id: int
    customer_name: str
    customer_phone: str
    party_size: int = Field(ge=1, le=100)
    start_at: datetime
    duration_minutes: int = Field(default=90, ge=15, le=480)
    table_ids: list[int] = Field(default_factory=list)
    source: str = "manual"
    notes: str | None = None


class ReservationUpdate(ApiModel):
    start_at: datetime | None = None
    duration_minutes: int | None = Field(default=None, ge=15, le=480)
    party_size: int | None = Field(default=None, ge=1, le=100)
    table_ids: list[int] | None = None
    status: Literal["confirmed", "seated", "completed", "cancelled", "no_show"] | None = None
    notes: str | None = None
    expected_version: int | None = None


class CourierCreate(ApiModel):
    branch_id: int
    name: str
    phone: str


class DeliveryAssign(ApiModel):
    courier_id: int | None = None
    estimated_at: datetime | None = None


class DeliveryTransition(ApiModel):
    status: Literal["preparing", "ready", "assigned", "dispatched", "delivered", "cancelled"]


class EvidenceMetadata(ApiModel):
    provider: Literal["yape", "plin"]
    amount_detected: Decimal | None = None
    operation_number: str | None = None
    security_code: str | None = Field(default=None, pattern=r"^\d{3}$")
    whatsapp_message_id: str | None = None
    occurred_at: datetime | None = None
    recipient: str | None = None
    confidence: Decimal | None = Field(default=None, ge=0, le=1)


class EvidenceReview(ApiModel):
    approve: bool
    note: str | None = None
    register_id: int | None = None


class IntegrationEvidenceCreate(EvidenceMetadata):
    order_id: int
    storage_path: str


class PublicReservationCreate(ApiModel):
    branch_id: int
    customer_name: str
    customer_phone: str
    party_size: int = Field(ge=1, le=30)
    start_at: datetime
    notes: str | None = None


class PublicOrderCreate(ApiModel):
    branch_id: int
    fulfillment: Literal["takeaway", "delivery"] = "takeaway"
    customer_name: str
    customer_phone: str
    delivery_address: dict | None = None
    delivery_quote_id: str | None = None
    notes: str | None = None
    items: list[OrderLineInput] = Field(min_length=1)


class LegacyDraftCreate(ApiModel):
    tenant_id: str = "impulsa"
    negocio_id: int | None = None
    negocio_nombre: str | None = None
    customer_phone: str | None = None
    customer_name: str | None = None
    message_id: str | None = None
    intent: str = "create_order"
    status: str = "draft"
    branch_name: str | None = None
    items_json: str | list | dict = Field(default_factory=list)
    notes: str | None = None
    agent_reply: str | None = None
    payment_status: str = "pending"
    source: str = "whatsapp_agent"

    @field_validator("items_json")
    @classmethod
    def keep_items_payload(cls, value):
        return value


SettingsRole = Literal[
    "owner",
    "members_manager",
    "manager",
    "menu_manager",
    "cashier",
    "waiter",
    "kitchen",
    "dispatcher",
]


class SettingsBusinessUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=2, max_length=180)
    currency: Literal["PEN"] | None = None
    timezone: Literal["America/Lima"] | None = None
    country_code: Literal["PE"] | None = None
    expected_version: int = Field(ge=1)


class SettingsBranchProfileUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=2, max_length=180)
    address: str | None = Field(default=None, max_length=1000)
    phone: str | None = Field(default=None, max_length=40)
    maps_url: str | None = Field(default=None, max_length=2000)
    google_place_id: str | None = Field(default=None, max_length=255)
    latitude: Decimal | None = Field(default=None, ge=-90, le=90)
    longitude: Decimal | None = Field(default=None, ge=-180, le=180)
    expected_version: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_coordinates(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("Latitude and longitude must be provided together")
        return self


class SettingsServicesUpdate(ApiModel):
    pos_tables: bool
    pos_counter: bool
    pos_takeaway: bool
    pos_delivery: bool
    digital_tables: bool
    digital_takeaway: bool
    digital_delivery: bool
    expected_version: int = Field(ge=1)


class DeliveryNeighborhood(ApiModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=120)
    fee: float = Field(ge=0, le=9999999999.99, allow_inf_nan=False)


class DeliveryOrigin(ApiModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    maps_url: str = Field(default="", max_length=2048)


class DeliveryPolicy(ApiModel):
    model_config = ConfigDict(extra="forbid")

    neighborhoods: list[DeliveryNeighborhood] = Field(default_factory=list, max_length=200)
    origin: DeliveryOrigin | None = None
    outside_band_mode: Literal["reject", "quote"] = "reject"

    @model_validator(mode="after")
    def validate_neighborhood_names(self):
        names = [item.name.casefold() for item in self.neighborhoods]
        if len(set(names)) != len(names):
            raise ValueError("Neighborhood names must be unique ignoring case")
        return self


class SettingsDeliveryUpdate(ApiModel):
    delivery_mode: Literal["free", "fixed", "bands", "distance", "quote", "neighborhoods"]
    delivery_policy: DeliveryPolicy | None = None
    fixed_delivery_fee: Decimal = Field(default=Decimal("0"), ge=0)
    distance_base_fee: Decimal = Field(default=Decimal("0"), ge=0)
    distance_fee_per_km: Decimal = Field(default=Decimal("0"), ge=0)
    distance_max_km: Decimal | None = Field(default=None, gt=0)
    free_delivery_threshold: Decimal | None = Field(default=None, ge=0)
    minimum_order_amount: Decimal | None = Field(default=None, ge=0)
    bands: list["DeliveryBandCreate"] | None = None
    expected_version: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_distance_configuration(self):
        if self.delivery_mode == "distance" and self.distance_max_km is None:
            raise ValueError("Distance delivery requires a maximum distance")
        return self


class DeliveryBandCreate(ApiModel):
    minimum_km: Decimal = Field(ge=0)
    maximum_km: Decimal = Field(gt=0)
    fee: Decimal = Field(ge=0)
    sort_order: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_range(self):
        if self.maximum_km <= self.minimum_km:
            raise ValueError("maximum_km must be greater than minimum_km")
        return self


class DeliveryBandUpdate(DeliveryBandCreate):
    expected_version: int = Field(ge=1)


class DeliveryQuoteCreate(ApiModel):
    subtotal: Decimal = Field(ge=0)
    destination: dict = Field(default_factory=dict)
    distance_km: Decimal | None = Field(default=None, ge=0)
    expected_configuration_version: int | None = Field(default=None, ge=1, strict=True)
    confirmed_fee: Decimal | None = Field(default=None, ge=0, le=Decimal("9999999999.99"), allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_manual_confirmation_context(self):
        if self.confirmed_fee is not None and self.expected_configuration_version is None:
            raise ValueError("Manual confirmation requires expected_configuration_version")
        return self


class SettingsPaymentMethodsUpdate(ApiModel):
    payment_methods: dict[Literal["delivery", "takeaway", "counter"], list[Literal[
        "cash", "card", "transfer", "yape", "plin", "online"
    ]]]
    expected_version: int = Field(ge=1)


class SettingsTimesUpdate(ApiModel):
    delivery_min_minutes: int = Field(ge=0, le=1440)
    delivery_max_minutes: int = Field(ge=0, le=1440)
    pickup_minutes: int = Field(ge=0, le=1440)
    expected_version: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_delivery_window(self):
        if self.delivery_max_minutes < self.delivery_min_minutes:
            raise ValueError("Maximum delivery time cannot be below the minimum")
        return self


class StaffMemberCreate(ApiModel):
    first_name: str = Field(min_length=1, max_length=120)
    last_name: str = Field(default="", max_length=120)
    email: str | None = Field(default=None, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    email_access: bool = False
    pin: SecretStr | None = None
    roles: list[SettingsRole] = Field(min_length=1)
    branch_ids: list[int] = Field(min_length=1)

    @field_validator("pin")
    @classmethod
    def validate_pin(cls, value: SecretStr | None):
        if value is not None:
            raw = value.get_secret_value()
            if len(raw) != 4 or not raw.isdigit():
                raise ValueError("PIN must contain exactly four digits")
        return value

    @model_validator(mode="after")
    def validate_email_access(self):
        if self.email_access and not self.email:
            raise ValueError("Email access requires an email address")
        return self


class StaffMemberUpdate(ApiModel):
    first_name: str | None = Field(default=None, min_length=1, max_length=120)
    last_name: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    email_access: bool | None = None
    pin: SecretStr | None = None
    roles: list[SettingsRole] | None = None
    branch_ids: list[int] | None = None
    expected_version: int = Field(ge=1)

    @field_validator("pin")
    @classmethod
    def validate_pin(cls, value: SecretStr | None):
        if value is not None:
            raw = value.get_secret_value()
            if len(raw) != 4 or not raw.isdigit():
                raise ValueError("PIN must contain exactly four digits")
        return value


class StaffPinVerify(ApiModel):
    staff_member_id: int
    pin: SecretStr

    @field_validator("pin")
    @classmethod
    def validate_pin(cls, value: SecretStr):
        raw = value.get_secret_value()
        if len(raw) != 4 or not raw.isdigit():
            raise ValueError("PIN must contain exactly four digits")
        return value


class PairedDeviceCreate(ApiModel):
    branch_id: int
    name: str = Field(min_length=2, max_length=180)


class PairedDeviceUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=2, max_length=180)
    active: bool | None = None
    expected_version: int = Field(ge=1)


class PairDeviceComplete(ApiModel):
    pairing_code: SecretStr


class ScheduleShiftInput(ApiModel):
    day_of_week: int = Field(ge=0, le=6)
    starts_at: time
    ends_at: time
    sort_order: int = Field(default=0, ge=0)


class ServiceScheduleCreate(ApiModel):
    name: str = Field(min_length=1, max_length=180)
    kind: Literal["primary", "additional"] = "additional"
    shifts: list[ScheduleShiftInput] = Field(default_factory=list)


class ServiceScheduleUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=180)
    active: bool | None = None
    shifts: list[ScheduleShiftInput] | None = None
    expected_version: int = Field(ge=1)


class ScheduleAssignmentsReplace(ApiModel):
    product_ids: list[int] = Field(default_factory=list)
    promotion_ids: list[int] = Field(default_factory=list)
    expected_version: int = Field(ge=1)


class SettingsPrintingUpdate(ApiModel):
    advanced_printing: bool
    printer_config: dict = Field(default_factory=dict)
    customer_ticket_template: dict = Field(default_factory=dict)
    kitchen_ticket_template: dict = Field(default_factory=dict)
    expected_version: int = Field(ge=1)

    @field_validator("printer_config")
    @classmethod
    def validate_print_language(cls, config: dict) -> dict:
        if "print_language" in config and config["print_language"] not in ("pixel", "escpos"):
            raise ValueError("print_language must be pixel or escpos")
        if "automatic_printing" in config and type(config["automatic_printing"]) is not bool:
            raise ValueError("automatic_printing must be a boolean")
        return config

    @field_validator("customer_ticket_template", "kitchen_ticket_template")
    @classmethod
    def validate_font_size(cls, template: dict) -> dict:
        if "font_size" in template and template["font_size"] not in FONT_SIZES:
            raise ValueError("font_size must be small, normal or large")
        return template

    @field_validator("customer_ticket_template")
    @classmethod
    def validate_receipt_text(cls, template: dict) -> dict:
        for key in ("header_enabled", "footer_enabled"):
            if key in template and type(template[key]) is not bool:
                raise ValueError(f"{key} must be a boolean")
        for key in ("header_text", "footer_text"):
            if key not in template:
                continue
            value = template[key]
            if not isinstance(value, str) or len(value) > 500:
                raise ValueError(f"{key} must be plain text of at most 500 characters")
            if any((ord(char) < 32 and char not in "\n\r\t") or ord(char) == 127 for char in value):
                raise ValueError(f"{key} contains unsupported control characters")
        return template


class PrinterDeviceCreate(ApiModel):
    name: str = Field(min_length=1, max_length=180)
    system_name: str = Field(min_length=1, max_length=255)
    paired_device_id: int | None = None
    purpose: Literal["kitchen", "bar", "customer"] = "kitchen"
    paper_width_mm: Literal[58, 80] = 80
    copies: int = Field(default=1, ge=1, le=10)


class PrinterDeviceUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=180)
    paired_device_id: int | None = None
    purpose: Literal["kitchen", "bar", "customer"] | None = None
    paper_width_mm: Literal[58, 80] | None = None
    copies: int | None = Field(default=None, ge=1, le=10)
    active: bool | None = None
    expected_version: int = Field(ge=1)


class PrintJobCreate(ApiModel):
    printer_id: int | None = None
    paired_device_id: int | None = None
    order_id: int | None = None
    kitchen_ticket_id: int | None = None
    job_type: Literal["customer_receipt", "kitchen_ticket"]
    payload: dict = Field(default_factory=dict)


class PrintJobComplete(ApiModel):
    status: Literal["printed", "failed"]
    error_message: str | None = Field(default=None, max_length=2000)


class QZSignRequest(ApiModel):
    payload: str = Field(min_length=1, max_length=200000)
    request: str | None = Field(default=None, min_length=1, max_length=200000)


class ArchiveRequest(ApiModel):
    expected_version: int = Field(ge=1)


class RegisterSettingsUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    is_default: bool | None = None
    expected_version: int = Field(ge=1)
