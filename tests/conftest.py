import os

os.environ["ENVIRONMENT"] = "test"
os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ["AUTO_CREATE_SCHEMA"] = "true"
os.environ["DEV_AUTH_TOKEN"] = "test-token"
os.environ["INTEGRATION_SERVICE_TOKEN"] = "test-integration-token"
os.environ["LEGACY_PUBLIC_READS_ENABLED"] = "true"

import pytest
from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import (
    Branch,
    Business,
    CashRegister,
    Category,
    DiningArea,
    InventoryItem,
    Product,
    RecipeItem,
    RestaurantTable,
)


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def tenant():
    with SessionLocal.begin() as db:
        business = Business(slug="test-restaurant", name="Test Restaurant")
        other_business = Business(slug="other-restaurant", name="Other Restaurant")
        db.add_all([business, other_business])
        db.flush()
        branch = Branch(business_id=business.id, slug="main", name="Main")
        other_branch = Branch(business_id=other_business.id, slug="main", name="Other Main")
        db.add_all([branch, other_branch])
        db.flush()
        area = DiningArea(business_id=business.id, branch_id=branch.id, name="Salon")
        db.add(area)
        db.flush()
        table = RestaurantTable(
            business_id=business.id,
            branch_id=branch.id,
            area_id=area.id,
            code="M1",
            name="Mesa 1",
            capacity=4,
        )
        category = Category(business_id=business.id, branch_id=branch.id, name="Pizzas")
        inventory = InventoryItem(
            business_id=business.id,
            branch_id=branch.id,
            sku="HARINA",
            name="Harina",
            unit="kg",
            quantity="10",
            minimum_stock="2",
        )
        register = CashRegister(business_id=business.id, branch_id=branch.id, name="Caja")
        db.add_all([table, category, inventory, register])
        db.flush()
        product = Product(
            business_id=business.id,
            branch_id=branch.id,
            category_id=category.id,
            sku="PIZZA",
            name="Pizza",
            price="20",
        )
        db.add(product)
        db.flush()
        db.add(RecipeItem(product_id=product.id, inventory_item_id=inventory.id, quantity="0.5"))
        return {
            "business_id": business.id,
            "branch_id": branch.id,
            "other_business_id": other_business.id,
            "other_branch_id": other_branch.id,
            "table_id": table.id,
            "product_id": product.id,
            "inventory_id": inventory.id,
            "register_id": register.id,
        }


@pytest.fixture
def auth_headers(tenant):
    return {
        "X-Dev-Auth": "test-token",
        "X-Dev-Role": "owner",
        "X-Dev-User": "owner-test",
        "X-Business-Id": str(tenant["business_id"]),
        "X-Branch-Id": str(tenant["branch_id"]),
    }
