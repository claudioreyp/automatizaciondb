from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Modifier, ModifierGroup, Product, ProductModifierGroup, ProductVariant


def set_product_availability(db: Session, product: Product, available: bool) -> None:
    product.available = available
    for variant in db.scalars(select(ProductVariant).where(ProductVariant.product_id == product.id, ProductVariant.active.is_(True))):
        variant.available = available


def sync_variant_availability(db: Session, product: Product) -> None:
    db.flush()
    variants = list(db.scalars(select(ProductVariant).where(ProductVariant.product_id == product.id, ProductVariant.active.is_(True))))
    if variants:
        product.available = any(variant.available for variant in variants)


def selection_unavailable_reason(variants, groups) -> str | None:
    active = [variant for variant in variants if variant.active]
    if active and not any(variant.available for variant in active):
        return "No hay variantes disponibles."
    for group, options in groups:
        minimum = max(group.minimum, 1 if group.required else 0)
        available = [option for option in options if option.active and option.available]
        capacity = len(available)
        if group.allow_repeats:
            capacity = capacity * group.max_per_option if group.max_per_option is not None else (float("inf") if available else 0)
        if group.maximum is not None:
            capacity = min(capacity, group.maximum)
        if minimum > capacity:
            return f"No hay opciones suficientes en {group.name} para completar este producto."
    return None


def product_selection_unavailable_reason(db: Session, product: Product) -> str | None:
    variants = list(db.scalars(select(ProductVariant).where(ProductVariant.product_id == product.id)))
    groups = list(db.scalars(select(ModifierGroup).join(ProductModifierGroup).where(ProductModifierGroup.product_id == product.id)))
    options = list(db.scalars(select(Modifier).where(Modifier.group_id.in_([group.id for group in groups])))) if groups else []
    return selection_unavailable_reason(variants, [(group, [option for option in options if option.group_id == group.id]) for group in groups])
