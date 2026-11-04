"""Public, additive v1 contracts. Existing extra POS fields remain compatible."""
from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field
from .agent_selection_schemas import PartialSelection


class PublicModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class Choice(PublicModel):
    id: int
    name: str
    available: bool


class VariantRequirement(PublicModel):
    required: bool
    field: Literal["variant_name"]
    options: list[Choice]


class GroupRequirement(PublicModel):
    group_id: int
    name: str
    required: bool
    minimum: int
    maximum: int | None
    allow_repeats: bool
    max_per_option: int | None
    options: list[Choice]


class SelectionRequirements(PublicModel):
    variant: VariantRequirement
    modifier_groups: list[GroupRequirement]
    optional_extras_require_selection: Literal[False]
    pricing_authority: Literal["server_preview"]


class ProductAvailability(PublicModel):
    manual: bool
    stock: bool
    options: bool
    schedule: bool | None
    channels: dict[str, bool]
    reasons: list[str]


class CatalogVariant(Choice):
    price_delta: float
    active: bool


class CatalogModifier(Choice):
    price_delta: float
    active: bool


class CatalogGroup(PublicModel):
    id: int
    name: str
    minimum: int
    maximum: int | None
    required: bool
    allow_repeats: bool
    max_per_option: int | None
    modifiers: list[CatalogModifier]


class ComboComponent(PublicModel):
    product_id: int
    name: str
    quantity: float


class CatalogProduct(PublicModel):
    id: int
    category_id: int | None
    name: str
    description: str | None
    price: float
    service_channels: list[str]
    product_type: str
    available: bool
    schedule_available: bool
    variants: list[CatalogVariant]
    modifier_groups: list[CatalogGroup]
    combo_components: list[ComboComponent]
    selection_requirements: SelectionRequirements
    availability: ProductAvailability


class Category(PublicModel):
    id: int
    name: str


class Promotion(PublicModel):
    id: int
    name: str
    promotion_type: str
    eligible_product_ids: list[int]
    eligible_order_modes: list[str]
    application_rules: dict[str, Any]


class ServiceAvailability(PublicModel):
    status: Literal["open", "closed", "unverified"]
    reason: str | None
    timezone: str | None
    checked_at: str
    local_time: str | None
    next_opening: str | None = None


class CatalogResponse(PublicModel):
    business_id: int
    branch_id: int
    currency: str
    timezone: str
    schema_version: int
    catalog_version: str
    complete: bool
    next_cursor: str | None
    product_count: int
    open_now: bool | None
    service_availability: ServiceAvailability | None = None
    categories: list[Category]
    products: list[CatalogProduct]
    promotions: list[Promotion]


class ScheduleShift(PublicModel):
    day_of_week: int
    starts_at: str
    ends_at: str
    display: str | None = None


class Schedule(PublicModel):
    id: int
    name: str
    kind: str
    active: bool
    shifts: list[ScheduleShift]
    open_now: bool | None
    version: int


class YapeInstructions(PublicModel):
    revision: str
    number: str | None
    recipient_name: str | None
    qr_configured: bool
    qr_url: str | None
    instructions_ready: bool | None = None
    missing_fields: list[str] = Field(default_factory=list)


class PaymentContext(PublicModel):
    methods: dict[str, list[str]]
    yape: YapeInstructions


class DeliveryContext(PublicModel):
    enabled: bool
    mode: str
    fee: float | None
    fee_status: str
    requires_quote: bool
    configuration_version: int


class TimeEstimates(PublicModel):
    source: str
    delivery_min_minutes: int | None
    delivery_max_minutes: int | None
    pickup_minutes: int | None
    is_guarantee: bool
    preparation_minutes: int | None = None
    travel_minutes: int | None = None


class RestaurantContext(PublicModel):
    business_id: int
    branch_id: int
    currency: str
    timezone: str
    configuration_version: int
    open_now: bool | None
    schedules: list[Schedule]
    payments: PaymentContext
    delivery: DeliveryContext
    estimates: TimeEstimates | None
    service_availability: ServiceAvailability | None = None


class AllowedActions(PublicModel):
    add_items: bool
    cancel_order: bool
    change_original_payment: bool
    revise_items: bool
    change_fulfillment: bool
    change_to_delivery: bool
    change_to_takeaway: bool
    choose_delivery_payment: bool
    editable_item_ids: list[int]
    fulfillment_change_block_reason: str | None
    reasons: dict[str, str | None] = Field(default_factory=dict)


class OrderLine(PublicModel):
    id: int
    product_id: int | None
    name: str
    variant_name: str | None
    quantity: float
    unit_price: float
    line_total: float
    status: str


class PaymentRequest(PublicModel):
    id: str
    order_id: int
    purpose: str
    method: str
    amount: float
    status: str


class KitchenTicket(PublicModel):
    id: int
    status: str
    items: list[dict[str, Any]]


class DispatchState(PublicModel):
    assignment_id: int | None
    status: str | None
    courier_assigned: bool = Field(description="Assignment alone never means dispatched.")
    dispatched: bool = Field(description="Actual dispatch from order state or delivery assignment timestamps/state.")
    dispatched_at: datetime | None
    delivered_at: datetime | None


class EvidenceSummary(PublicModel):
    id: int
    payment_request_id: str | None
    status: str = Field(description="Receipt review state; under_review does not mean payment approved.")
    expected_amount: float | None
    amount_detected: float | None
    operation_number: str | None
    security_code: str | None
    recipient: str | None
    rejection_reason: str | None
    created_at: datetime
    reviewed_at: datetime | None


class CustomerOrderState(PublicModel):
    id: int
    business_id: int
    branch_id: int
    version: int
    status: str = Field(description="Order lifecycle, separate from payment, receipt, kitchen and actual dispatch.")
    payment_method: str | None
    payment_status: str
    total: float
    paid_amount: float
    remaining_amount: float
    kitchen_completed: bool
    kitchen_started: bool
    dispatch: DispatchState
    items: list[OrderLine]
    allowed_actions: AllowedActions
    payment_requests: list[PaymentRequest]
    tickets: list[KitchenTicket]
    payment_evidence: list[EvidenceSummary]


class SelectionIssue(PublicModel):
    path: str
    code: str
    message: str
    options: list[Choice] = Field(default_factory=list)
    minimum: int | None = None
    maximum: int | None = None


class SelectionValidation(PublicModel):
    schema_version: Literal[1]
    business_id: int
    branch_id: int
    catalog_version: str
    valid: bool
    ready_for_preview: bool
    missing_fields: list[SelectionIssue]
    issues: list[SelectionIssue]
    pricing_authority: Literal["server_preview"]


class ConversationPublicContext(PublicModel):
    schema_version: Literal[1]
    intent: str | None
    selection: PartialSelection | None
    order_id: int | None
    commercial_authority: Literal["pos"]
    selection_validation: SelectionValidation | None = None


class TurnContextResponse(PublicModel):
    protocol_version: Literal[1]
    business_id: int
    branch_id: int
    conversation_id: str
    message_id: str
    version: int
    context: RestaurantContext
    catalog: CatalogResponse | None
    catalog_version: str
    catalog_unchanged: bool
    order: CustomerOrderState | None
    conversation_context: ConversationPublicContext


class AgentError(PublicModel):
    detail: str | list | dict
    code: str
    request_id: str
    retryable: bool
    issues: list[SelectionIssue] = Field(default_factory=list)


class PricedLine(PublicModel):
    product_id: int | None
    name: str
    variant_name: str | None
    quantity: str
    unit_price: str
    line_total: str


class OrderPreview(PublicModel):
    business_id: int
    branch_id: int
    subtotal: str
    discount: str
    amount: str
    items: list[PricedLine]
    products_total: float
    known_total: float
    final_total: float | None
    delivery_fee: float | None
    delivery_fee_status: str
    delivery_quote_id: str | None


class OrderSnapshot(PublicModel):
    id: int
    business_id: int
    branch_id: int
    version: int
    status: str
    payment_status: str
    payment_method: str | None
    total: float
    items: list[OrderLine]


class OrderToolResult(PublicModel):
    """Compatible operation result; order writes return either order or root ID/version."""
    id: int | None = None
    order_id: int | None = None
    version: int | None = None
    status: str | None = None
    order: OrderSnapshot | None = None
    payment_request: PaymentRequest | None = None
    tickets: list[KitchenTicket] | None = None
    sent_to_kitchen: bool | None = None
    command_committed: bool | None = None
    appended_item_ids: list[int] | None = None


class ConversationToolResult(PublicModel):
    """Transport state is separate from commercial success and delivery confirmation."""
    status: str | None = None
    conversation_id: str | None = None
    message_id: str | None = None
    version: int | None = None
    lease_token: str | None = Field(default=None, description="Private runtime token; never pass to the language model.")
    delivery_id: str | None = None
    delivery_token: str | None = Field(default=None, description="Private transport token for freshness and ACK; never pass to the language model.")
    pending_count: int | None = None
    duplicate: bool | None = None
    response: dict[str, Any] | None = None


class AttentionToolResult(PublicModel):
    id: int | str | None = None
    status: str | None = None
    order_id: int | None = None


class IntegrationEventResult(PublicModel):
    id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    customer_phone: str | None
    whatsapp_chat_id: str | None
    payload: dict[str, Any]
    delivery_attempts: int
    notice_fingerprint: str
    created_at: str


class EventAcknowledgement(PublicModel):
    id: str
    acknowledged_at: str


class PaymentMethodPreview(PublicModel):
    order_id: int
    order_version: int
    from_method: str | None
    to_method: str
    preview_fingerprint: str
    allowed: bool
    effects: dict[str, bool]


class InventoryItemResult(PublicModel):
    id: int
    business_id: int
    branch_id: int
    sku: str
    name: str
    unit: str
    quantity: float
    minimum_stock: float
    unit_cost: float
    low_stock: bool
    active: bool
    version: int


class ProductCapacity(PublicModel):
    product_id: int
    name: str
    available_units: int | None
    available: bool


class InventoryContext(PublicModel):
    branch_id: int
    inventory: list[InventoryItemResult]
    product_capacity: list[ProductCapacity]


class TableResult(PublicModel):
    id: int
    business_id: int
    branch_id: int
    area_id: int
    code: str
    name: str
    capacity: int
    status: str
    version: int


class TableAvailability(PublicModel):
    available: bool
    tables: list[TableResult]


class BranchIdentity(PublicModel):
    id: int
    business_id: int
    name: str
    slug: str


class LegacyCatalogProduct(PublicModel):
    id: int
    name: str
    price: float
    available: bool
    service_channels: list[str]
    variants: list[CatalogVariant]
    modifier_groups: list[CatalogGroup]
    combo_components: list[ComboComponent]


class LegacyMenu(PublicModel):
    business_id: int
    branch: BranchIdentity
    categories: list[Category]
    products: list[LegacyCatalogProduct]
    modifier_groups: list[CatalogGroup]
    promotions: list[Promotion]
