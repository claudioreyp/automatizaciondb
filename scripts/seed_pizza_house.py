"""Idempotently load Pizza House's real menu and initial operating data."""

from __future__ import annotations

import argparse
import os
import secrets
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select, text

from app.auth import hash_integration_token
from app.database import SessionLocal
from app.models import (
    Branch,
    Business,
    CashRegister,
    Category,
    DiningArea,
    IntegrationCredential,
    InventoryItem,
    Membership,
    ModuleEntitlement,
    Product,
    ProductVariant,
    RecipeItem,
    RestaurantTable,
)


BUSINESS_ID = 2
BRANCH_ID = 2
BUSINESS_SLUG = "pizza-house"
OWNER_USER_ID = "b8c2d20a-8cb6-4210-a09f-ffa33d1e47f6"
OWNER_EMAIL = "roalrepa12@gmail.com"
SUPERADMIN_USER_ID = "7acbc847-a4e5-4869-a8e5-b13d6843a93c"
MODULES = ["pos", "tables", "kds", "inventory", "cash", "delivery", "reservations", "whatsapp"]
SCOPES = [
    "events:read",
    "inventory:read",
    "inventory:write",
    "menu:read",
    "orders:read",
    "orders:write",
    "payments:write",
    "reservations:write",
]

PIZZAS = [
    ("PIZ-AMERICANA", "Americana", "Salsa de tomate, queso mozzarella y jamon", "15.00", "27.00", "48.00"),
    ("PIZ-HAWAIANA", "Hawaiana", "Salsa de tomate, queso mozzarella, jamon y pina", "15.00", "30.00", "50.00"),
    ("PIZ-PEPERONI", "Peperoni", "Salsa de tomate, queso mozzarella y peperoni", "15.00", "27.00", "48.00"),
    (
        "PIZ-VEGETARIANA",
        "Vegetariana",
        "Salsa de tomate, queso mozzarella, champinones, aceitunas verdes y negras",
        "15.00",
        "27.00",
        "50.00",
    ),
    ("PIZ-DIABLA", "Diabla", "Salsa de tomate, queso mozzarella y chorizo espanol", "15.00", "30.00", "50.00"),
    (
        "PIZ-CONTINENTAL",
        "Continental",
        "Salsa de tomate, queso mozzarella, jamon, champinones y pimiento",
        "15.00",
        "30.00",
        "50.00",
    ),
    ("PIZ-BOLONESA", "Bolonesa", "Salsa de tomate, queso mozzarella y carne molida", "15.00", "30.00", "50.00"),
]

DRINKS = [
    ("BEB-INCA", "Inca Kola", "3.50", "7.00"),
    ("BEB-COCA", "Coca-Cola", "3.50", "7.00"),
]


def issue_token() -> tuple[str, str, str]:
    token_id = secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12]
    prefix = f"esc_live_{token_id}"
    token = f"{prefix}.{secrets.token_urlsafe(32)}"
    return token, prefix, hash_integration_token(token)


def get_or_create(session, model, defaults: dict | None = None, **filters):
    instance = session.scalar(select(model).filter_by(**filters))
    if instance is not None:
        return instance, False
    instance = model(**filters, **(defaults or {}))
    session.add(instance)
    session.flush()
    return instance, True


def seed(*, reset_stock: bool, rotate_credential: bool, token_output: Path | None) -> dict:
    if not os.getenv("DATABASE_URL"):
        raise RuntimeError("DATABASE_URL is required")

    with SessionLocal() as session:
        business = session.scalar(select(Business).where(Business.slug == BUSINESS_SLUG))
        if business is None:
            if session.get(Business, BUSINESS_ID) is not None:
                raise RuntimeError(f"Business ID {BUSINESS_ID} already belongs to another restaurant")
            business = Business(
                id=BUSINESS_ID,
                slug=BUSINESS_SLUG,
                name="Pizza House",
                status="active",
                plan="pro",
                currency="PEN",
                timezone="America/Lima",
                phone="924369164",
            )
            session.add(business)
            session.flush()
        else:
            business.name = "Pizza House"
            business.status = "active"
            business.phone = "924369164"

        branch = session.scalar(select(Branch).where(Branch.business_id == business.id, Branch.slug == "principal"))
        if branch is None:
            if session.get(Branch, BRANCH_ID) is not None:
                raise RuntimeError(f"Branch ID {BRANCH_ID} already belongs to another restaurant")
            branch = Branch(id=BRANCH_ID, business_id=business.id, slug="principal", name="Sucursal principal")
            session.add(branch)
            session.flush()
        branch.name = "Sucursal principal"
        branch.address = "Jr Lima 836, Tambogrande"
        branch.phone = "924369164"
        branch.accepted_payment_methods = ["cash", "yape"]
        branch.delivery_enabled = True
        branch.takeaway_enabled = True
        branch.yape_number = "944197385"
        branch.payment_recipient_name = "Claudio David Rey Palacios"
        branch.active = True

        for module in MODULES:
            entitlement, _ = get_or_create(
                session,
                ModuleEntitlement,
                business_id=business.id,
                module=module,
                defaults={"enabled": True},
            )
            entitlement.enabled = True

        area, _ = get_or_create(
            session,
            DiningArea,
            business_id=business.id,
            branch_id=branch.id,
            name="Salon principal",
            defaults={"sort_order": 0},
        )
        get_or_create(
            session,
            CashRegister,
            business_id=business.id,
            branch_id=branch.id,
            name="Caja principal",
            defaults={"active": True},
        )
        for number in range(1, 7):
            table, _ = get_or_create(
                session,
                RestaurantTable,
                branch_id=branch.id,
                code=f"M{number}",
                defaults={
                    "business_id": business.id,
                    "area_id": area.id,
                    "name": f"Mesa {number}",
                    "capacity": 4,
                    "position_x": ((number - 1) % 3) * 150,
                    "position_y": ((number - 1) // 3) * 120,
                    "status": "available",
                },
            )
            table.area_id = area.id

        pizza_category, _ = get_or_create(
            session,
            Category,
            business_id=business.id,
            branch_id=branch.id,
            name="Pizzas",
            defaults={"color": "#e96b2c", "sort_order": 1, "active": True},
        )
        food_category, _ = get_or_create(
            session,
            Category,
            business_id=business.id,
            branch_id=branch.id,
            name="Pastas",
            defaults={"color": "#d29b45", "sort_order": 2, "active": True},
        )
        drink_category, _ = get_or_create(
            session,
            Category,
            business_id=business.id,
            branch_id=branch.id,
            name="Bebidas",
            defaults={"color": "#2589a6", "sort_order": 3, "active": True},
        )

        stock, stock_created = get_or_create(
            session,
            InventoryItem,
            branch_id=branch.id,
            sku="STOCK-PIZZAS-TOTAL",
            defaults={
                "business_id": business.id,
                "name": "Pizzas disponibles",
                "unit": "unit",
                "quantity": Decimal("30"),
                "minimum_stock": Decimal("5"),
                "active": True,
            },
        )
        if stock_created or reset_stock:
            stock.quantity = Decimal("30")
            stock.version = max(1, int(stock.version or 1))

        for order, (sku, name, description, personal, medium, family) in enumerate(PIZZAS, start=1):
            product, _ = get_or_create(
                session,
                Product,
                branch_id=branch.id,
                sku=sku,
                defaults={
                    "business_id": business.id,
                    "category_id": pizza_category.id,
                    "name": name,
                    "description": description,
                    "price": Decimal(personal),
                    "available": True,
                    "track_stock": True,
                    "preparation_station": "kitchen",
                    "sort_order": order,
                },
            )
            product.business_id = business.id
            product.category_id = pizza_category.id
            product.name = name
            product.description = description
            product.price = Decimal(personal)
            product.available = True
            product.track_stock = True
            product.preparation_station = "kitchen"
            product.sort_order = order

            desired_variants = {
                "Personal": Decimal("0"),
                "Mediana": Decimal(medium) - Decimal(personal),
                "Familiar": Decimal(family) - Decimal(personal),
            }
            existing_variants = {
                variant.name: variant
                for variant in session.scalars(select(ProductVariant).where(ProductVariant.product_id == product.id))
            }
            for variant_name, delta in desired_variants.items():
                variant = existing_variants.get(variant_name)
                if variant is None:
                    variant = ProductVariant(product_id=product.id, name=variant_name)
                    session.add(variant)
                variant.price_delta = delta
                variant.active = True

            recipe, _ = get_or_create(
                session,
                RecipeItem,
                product_id=product.id,
                inventory_item_id=stock.id,
                defaults={"quantity": Decimal("1")},
            )
            recipe.quantity = Decimal("1")

        lasagna, _ = get_or_create(
            session,
            Product,
            branch_id=branch.id,
            sku="PAS-LASANA",
            defaults={
                "business_id": business.id,
                "category_id": food_category.id,
                "name": "Lasana",
                "description": "Lasana de la casa",
                "price": Decimal("20"),
                "available": True,
                "track_stock": False,
                "preparation_station": "kitchen",
                "sort_order": 1,
            },
        )
        lasagna.business_id = business.id
        lasagna.category_id = food_category.id
        lasagna.name = "Lasana"
        lasagna.price = Decimal("20")
        lasagna.available = True
        lasagna.track_stock = False

        for order, (sku, name, personal, liter) in enumerate(DRINKS, start=1):
            drink, _ = get_or_create(
                session,
                Product,
                branch_id=branch.id,
                sku=sku,
                defaults={
                    "business_id": business.id,
                    "category_id": drink_category.id,
                    "name": name,
                    "description": f"{name} personal o de 1 litro",
                    "price": Decimal(personal),
                    "available": True,
                    "track_stock": False,
                    "preparation_station": "bar",
                    "sort_order": order,
                },
            )
            drink.business_id = business.id
            drink.category_id = drink_category.id
            drink.name = name
            drink.description = f"{name} personal o de 1 litro"
            drink.price = Decimal(personal)
            drink.available = True
            drink.track_stock = False
            drink.preparation_station = "bar"
            drink.sort_order = order

            desired_variants = {
                "Personal": Decimal("0"),
                "1 litro": Decimal(liter) - Decimal(personal),
            }
            existing_variants = {
                variant.name: variant
                for variant in session.scalars(select(ProductVariant).where(ProductVariant.product_id == drink.id))
            }
            for variant_name, delta in desired_variants.items():
                variant = existing_variants.get(variant_name)
                if variant is None:
                    variant = ProductVariant(product_id=drink.id, name=variant_name)
                    session.add(variant)
                variant.price_delta = delta
                variant.active = True

        membership, _ = get_or_create(
            session,
            Membership,
            auth_user_id=OWNER_USER_ID,
            business_id=business.id,
            branch_id=None,
            defaults={
                "email": OWNER_EMAIL,
                "full_name": "AlonsoRey",
                "role": "owner",
                "active": True,
            },
        )
        membership.email = OWNER_EMAIL
        membership.full_name = "AlonsoRey"
        membership.role = "owner"
        membership.active = True

        credential = session.scalar(
            select(IntegrationCredential).where(
                IntegrationCredential.branch_id == branch.id,
                IntegrationCredential.name == "Agente n8n Pizza House",
                IntegrationCredential.active.is_(True),
            )
        )
        raw_token = None
        if credential is None:
            raw_token, prefix, token_hash = issue_token()
            credential = IntegrationCredential(
                business_id=business.id,
                branch_id=branch.id,
                name="Agente n8n Pizza House",
                token_prefix=prefix,
                token_hash=token_hash,
                scopes=SCOPES,
                active=True,
                created_by=SUPERADMIN_USER_ID,
            )
            session.add(credential)
        elif rotate_credential:
            raw_token, prefix, token_hash = issue_token()
            credential.token_prefix = prefix
            credential.token_hash = token_hash
            credential.scopes = SCOPES
            credential.active = True
            credential.revoked_at = None
        credential.scopes = SCOPES

        if token_output is not None:
            if raw_token is None:
                raise RuntimeError("Credential already exists; use --rotate-credential to issue a new token")
            token_output.write_text(raw_token, encoding="utf-8")

        session.flush()
        session.execute(text("select setval(pg_get_serial_sequence('businesses','id'), greatest((select max(id) from businesses), 1))"))
        session.execute(text("select setval(pg_get_serial_sequence('branches','id'), greatest((select max(id) from branches), 1))"))
        session.commit()
        return {
            "business_id": business.id,
            "branch_id": branch.id,
            "products": len(PIZZAS) + len(DRINKS) + 1,
            "pizza_stock": str(stock.quantity),
            "credential_prefix": credential.token_prefix,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset-stock", action="store_true")
    parser.add_argument("--rotate-credential", action="store_true")
    parser.add_argument("--token-output", type=Path)
    args = parser.parse_args()
    result = seed(
        reset_stock=args.reset_stock,
        rotate_credential=args.rotate_credential,
        token_output=args.token_output,
    )
    print(
        "Pizza House ready: "
        f"business={result['business_id']} branch={result['branch_id']} "
        f"products={result['products']} pizza_stock={result['pizza_stock']} "
        f"credential={result['credential_prefix']}"
    )


if __name__ == "__main__":
    main()
