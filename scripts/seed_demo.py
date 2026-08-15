"""Seed a complete local demo tenant without storing any real credentials."""

from decimal import Decimal

from sqlalchemy import select

from app.database import Base, SessionLocal, engine
from app.models import (
    Branch,
    Business,
    CashRegister,
    Category,
    DiningArea,
    InventoryItem,
    Membership,
    ModuleEntitlement,
    Product,
    RecipeItem,
    RestaurantTable,
)


def seed() -> None:
    Base.metadata.create_all(bind=engine)
    with SessionLocal.begin() as db:
        business = db.scalar(select(Business).where(Business.slug == "bazar-pizzas"))
        if not business:
            business = Business(slug="bazar-pizzas", name="Bazar pizzas", plan="superpro", phone="+51 999 000 000")
            db.add(business)
            db.flush()
        branch = db.scalar(select(Branch).where(Branch.business_id == business.id, Branch.slug == "principal"))
        if not branch:
            branch = Branch(
                business_id=business.id,
                slug="principal",
                name="Sucursal principal",
                address="Lima, Peru",
                opening_hours={"monday": ["12:00", "23:00"], "sunday": ["12:00", "22:00"]},
                accepted_payment_methods=["cash", "card", "yape", "plin"],
                yape_number="999000000",
                plin_number="999000000",
            )
            db.add(branch)
            db.flush()
        if not db.scalar(select(Membership).where(Membership.auth_user_id == "dev-owner")):
            db.add(
                Membership(
                    auth_user_id="dev-owner",
                    email="owner@impulsa.local",
                    full_name="Propietario demo",
                    business_id=business.id,
                    branch_id=None,
                    role="owner",
                )
            )
        for module in ["pos", "tables", "kds", "inventory", "cash", "delivery", "reservations", "whatsapp"]:
            if not db.scalar(
                select(ModuleEntitlement).where(
                    ModuleEntitlement.business_id == business.id,
                    ModuleEntitlement.module == module,
                )
            ):
                db.add(ModuleEntitlement(business_id=business.id, module=module, enabled=True))
        area = db.scalar(select(DiningArea).where(DiningArea.branch_id == branch.id, DiningArea.name == "Salon"))
        if not area:
            area = DiningArea(business_id=business.id, branch_id=branch.id, name="Salon")
            db.add(area)
            db.flush()
            for number in range(1, 9):
                db.add(
                    RestaurantTable(
                        business_id=business.id,
                        branch_id=branch.id,
                        area_id=area.id,
                        code=f"M{number}",
                        name=f"Mesa {number}",
                        capacity=4 if number < 7 else 6,
                        position_x=((number - 1) % 4) * 150,
                        position_y=((number - 1) // 4) * 120,
                    )
                )
        category = db.scalar(select(Category).where(Category.branch_id == branch.id, Category.name == "Pizzas"))
        if not category:
            category = Category(business_id=business.id, branch_id=branch.id, name="Pizzas", color="#d95d39")
            db.add(category)
            db.flush()
        products = [
            ("PIZ-PEP", "Pizza pepperoni", "Mozzarella, salsa y pepperoni", "35.00"),
            ("PIZ-AME", "Pizza americana", "Jamon, mozzarella y salsa", "32.00"),
            ("BEB-INK", "Inca Kola 1L", "Botella helada", "10.00"),
        ]
        created_products = []
        for sku, name, description, price in products:
            product = db.scalar(select(Product).where(Product.branch_id == branch.id, Product.sku == sku))
            if not product:
                product = Product(
                    business_id=business.id,
                    branch_id=branch.id,
                    category_id=category.id,
                    sku=sku,
                    name=name,
                    description=description,
                    price=Decimal(price),
                    preparation_station="bar" if sku.startswith("BEB") else "kitchen",
                )
                db.add(product)
                db.flush()
            created_products.append(product)
        flour = db.scalar(select(InventoryItem).where(InventoryItem.branch_id == branch.id, InventoryItem.sku == "INS-HAR"))
        if not flour:
            flour = InventoryItem(
                business_id=business.id,
                branch_id=branch.id,
                sku="INS-HAR",
                name="Harina",
                unit="kg",
                quantity=Decimal("25"),
                minimum_stock=Decimal("5"),
            )
            db.add(flour)
            db.flush()
        for product in created_products[:2]:
            if not db.scalar(select(RecipeItem).where(RecipeItem.product_id == product.id, RecipeItem.inventory_item_id == flour.id)):
                db.add(RecipeItem(product_id=product.id, inventory_item_id=flour.id, quantity=Decimal("0.25")))
        if not db.scalar(select(CashRegister).where(CashRegister.branch_id == branch.id, CashRegister.name == "Caja principal")):
            db.add(CashRegister(business_id=business.id, branch_id=branch.id, name="Caja principal"))
    print("Demo tenant ready: bazar-pizzas")


if __name__ == "__main__":
    seed()
