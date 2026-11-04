"""Versioned selection input shared by tools and conversation intent."""
from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

class PartialModifier(BaseModel):
    model_config = ConfigDict(extra="forbid")
    modifier_id: int | None = Field(default=None, ge=1)
    name: str | None = Field(default=None, max_length=200)
    quantity: int = Field(default=1, ge=1, le=100)


class PartialLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: int | None = Field(default=None, ge=1)
    quantity: Decimal = Field(default=Decimal(1), gt=0, le=10000)
    variant_name: str | None = Field(default=None, max_length=200)
    modifiers: list[PartialModifier] = Field(default_factory=list, max_length=100)
    notes: str | None = Field(default=None, max_length=2000)


class PartialDeliveryDestination(BaseModel):
    model_config = ConfigDict(extra="forbid")
    address: str | None = Field(default=None, max_length=2000)
    full_address: str | None = Field(default=None, max_length=2000)
    reference: str | None = Field(default=None, max_length=2000)
    maps_url: str | None = Field(default=None, max_length=2000)
    map_url: str | None = Field(default=None, max_length=2000)
    latitude: float | None = Field(default=None, ge=-90, le=90, strict=True)
    longitude: float | None = Field(default=None, ge=-180, le=180, strict=True)
    delivery_service: Literal["own"] = "own"
    neighborhood: str | None = Field(default=None, max_length=2000)
    street: str | None = Field(default=None, max_length=2000)
    number: str | None = Field(default=None, max_length=2000)
    cross_streets: str | None = Field(default=None, max_length=2000)


class PartialSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel: Literal["takeaway", "delivery", "counter"] | None = None
    payment_method: Literal["cash", "yape"] | None = None
    delivery_address: PartialDeliveryDestination | None = None
    items: list[PartialLine] = Field(default_factory=list, max_length=100)
