"""Idempotently import the old negocios/restaurantes_perfiles data.

The legacy tables are intentionally left untouched. Plain-text passwords and
private payment keys are never copied into the normalized POS schema.
"""

import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation

from sqlalchemy import inspect, select, text

from app.database import Base, SessionLocal, engine
from app.models import Branch, Business, Category, ModuleEntitlement, Product


MODULES = ["pos", "tables", "kds", "inventory", "cash", "delivery", "reservations", "whatsapp"]


def slugify(value: str, fallback: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    return slug or fallback


def decimal_value(value) -> Decimal:
    try:
        return Decimal(str(value or "0")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return Decimal("0.00")


def json_value(value, default):
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def legacy_rows(table_name: str) -> list[dict]:
    if table_name not in inspect(engine).get_table_names():
        return []
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(text(f'SELECT * FROM "{table_name}"')).mappings()]


def migrate() -> dict[str, int]:
    Base.metadata.create_all(bind=engine)
    businesses = legacy_rows("negocios")
    profiles = {row.get("negocio_id"): row for row in legacy_rows("restaurantes_perfiles")}
    totals = {"businesses": 0, "branches": 0, "products": 0}

    with SessionLocal.begin() as db:
        for legacy in businesses:
            legacy_id = legacy.get("id")
            name = str(legacy.get("nombre") or f"Negocio {legacy_id}").strip()
            slug = slugify(name, f"negocio-{legacy_id}")
            business = db.scalar(select(Business).where(Business.slug == slug))
            profile = profiles.get(legacy_id) or {}
            if not business:
                status_value = str(legacy.get("estatus") or legacy.get("status") or "active").lower()
                business = Business(
                    slug=slug,
                    name=name,
                    status="active" if status_value in {"activo", "active", "true", "1"} else "suspended",
                    plan=str(legacy.get("plan") or "basic"),
                    logo_url=profile.get("logo_url"),
                    phone=profile.get("telefono"),
                )
                db.add(business)
                db.flush()
                totals["businesses"] += 1
            for module in MODULES:
                if not db.scalar(
                    select(ModuleEntitlement).where(
                        ModuleEntitlement.business_id == business.id,
                        ModuleEntitlement.module == module,
                    )
                ):
                    db.add(ModuleEntitlement(business_id=business.id, module=module, enabled=True))

            branch_payloads = json_value(profile.get("sucursales_json"), [])
            if not branch_payloads:
                branch_payloads = [{"nombre": "Principal", "direccion": "", "horario": "", "pagos": "", "menu": []}]
            for branch_index, source_branch in enumerate(branch_payloads, start=1):
                branch_name = str(source_branch.get("nombre") or f"Sucursal {branch_index}").strip()
                branch_slug = slugify(branch_name, f"sucursal-{branch_index}")
                branch = db.scalar(
                    select(Branch).where(Branch.business_id == business.id, Branch.slug == branch_slug)
                )
                if not branch:
                    payments = [item.strip() for item in str(source_branch.get("pagos") or "").split(",") if item.strip()]
                    branch = Branch(
                        business_id=business.id,
                        slug=branch_slug,
                        name=branch_name,
                        address=source_branch.get("direccion"),
                        phone=profile.get("telefono"),
                        opening_hours={"legacy_text": source_branch.get("horario") or ""},
                        accepted_payment_methods=payments,
                    )
                    db.add(branch)
                    db.flush()
                    totals["branches"] += 1
                category = db.scalar(
                    select(Category).where(Category.branch_id == branch.id, Category.name == "Carta migrada")
                )
                if not category:
                    category = Category(
                        business_id=business.id,
                        branch_id=branch.id,
                        name="Carta migrada",
                        sort_order=0,
                    )
                    db.add(category)
                    db.flush()
                for product_index, source_product in enumerate(source_branch.get("menu") or [], start=1):
                    sku = f"LEG-{legacy_id}-{branch_index}-{product_index}"
                    product = db.scalar(select(Product).where(Product.branch_id == branch.id, Product.sku == sku))
                    if product:
                        continue
                    db.add(
                        Product(
                            business_id=business.id,
                            branch_id=branch.id,
                            category_id=category.id,
                            sku=sku,
                            name=str(source_product.get("nombre") or f"Producto {product_index}"),
                            description=source_product.get("desc"),
                            price=decimal_value(source_product.get("precio")),
                            image_url=source_product.get("img"),
                            available=True,
                        )
                    )
                    totals["products"] += 1
    return totals


if __name__ == "__main__":
    print(json.dumps(migrate(), ensure_ascii=True))
