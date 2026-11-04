"""Disposable localhost rehearsal using the actual API and synthetic POS rows.

No remote configuration is read. Stop with Ctrl+C; no real printer or transport
is configured. Data remains under the ignored .test-results directory for review.
"""
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ARTIFACT = ROOT / ".test-results" / ("sandbox-" + str(time.time_ns()))
ARTIFACT.mkdir(parents=True)
for name in list(os.environ):
    if any(part in name for part in ("SUPABASE", "OPENAI", "ANTHROPIC", "QZ_TRAY", "DATABASE")):
        os.environ.pop(name)
os.environ.update(ESCALAR_ENV_FILE_DISABLED="true", ENVIRONMENT="development",
    DATABASE_URL="sqlite+pysqlite:///" + (ARTIFACT / "synthetic.db").as_posix(),
    AUTO_CREATE_SCHEMA="false", DEV_AUTH_TOKEN="api-tools-local-only",
    AUTH_ADMIN_SECRET="synthetic-sandbox-secret-never-use-in-production",
    CORS_ORIGINS="http://127.0.0.1:5178,http://127.0.0.1:5179",
    PUBLIC_API_BASE_URL="http://127.0.0.1:8769/api/v1", UPLOAD_DIR=str(ARTIFACT / "uploads"))

from app.database import Base, engine, SessionLocal
from app.models import (Business, Branch, Membership, BranchSettings, ServiceSchedule, ScheduleShift,
    Category, Product, ProductVariant, ModifierGroup, Modifier, ProductModifierGroup,
    CashRegister, DiningArea, AgentAttentionRequest, Order, OrderItem, PaymentEvidence, KitchenTicket, utcnow)
from app.main import app
from datetime import time as clock_time
from fastapi.testclient import TestClient


def seed():
    Base.metadata.create_all(engine)
    from PIL import Image, ImageDraw
    receipt = ARTIFACT / "uploads" / "synthetic.png"
    receipt.parent.mkdir(parents=True)
    picture = Image.new("RGB", (360, 160), "white")
    ImageDraw.Draw(picture).text((25, 50), "COMPROBANTE SINTETICO\nSIN VALOR COMERCIAL", fill="black")
    picture.save(receipt)
    with SessionLocal.begin() as db:
        for number, title in enumerate(("Pizzería Ensayo API", "Alitas Ensayo API"), 1):
            business = Business(name=title, slug=f"sandbox-{number}")
            db.add(business); db.flush()
            branch = Branch(business_id=business.id, slug="principal", name="Sucursal sintética",
                accepted_payment_methods=["cash", "yape"], yape_number="999000001",
                payment_recipient_name="Titular de prueba sin valor")
            db.add(branch); db.flush()
            db.add(Membership(auth_user_id="dev-owner", business_id=business.id, branch_id=branch.id,
                email="owner@example.invalid", role="owner", full_name="Propietario de prueba"))
            db.add(BranchSettings(business_id=business.id, branch_id=branch.id, delivery_mode="quote",
                payment_methods={mode: ["cash", "yape"] for mode in ("counter", "delivery", "takeaway")}))
            db.add_all([CashRegister(business_id=business.id, branch_id=branch.id, name="Caja de ensayo"),
                DiningArea(business_id=business.id, branch_id=branch.id, name="Salón de ensayo")])
            schedule = ServiceSchedule(business_id=business.id, branch_id=branch.id,
                name="Horario de ensayo", kind="primary", active=True)
            category = Category(business_id=business.id, branch_id=branch.id, name="Carta de prueba")
            db.add_all([schedule, category]); db.flush()
            db.add_all([ScheduleShift(schedule_id=schedule.id, day_of_week=day,
                starts_at=clock_time(0), ends_at=clock_time(23, 59)) for day in range(7)])
            product = Product(business_id=business.id, branch_id=branch.id, category_id=category.id,
                sku=f"SYNTHETIC-{number}", name="Pizza combinada" if number == 1 else "Alitas con salsa", price=20,
                service_channels=["pos", "digital_takeaway", "digital_delivery"])
            db.add(product); db.flush()
            group = ModifierGroup(business_id=business.id, branch_id=branch.id,
                name="Sabores" if number == 1 else "Salsa", required=True,
                minimum=2 if number == 1 else 1, maximum=3 if number == 1 else 1)
            db.add(group); db.flush()
            db.add(ProductModifierGroup(product_id=product.id, group_id=group.id))
            db.add_all([Modifier(group_id=group.id, name=name, price_delta=0)
                for name in (("Pepperoni", "Hawaiana", "Vegetariana") if number == 1 else ("BBQ", "Búfalo"))])
            if number == 1:
                db.add(ProductVariant(product_id=product.id, name="Mediana", price_delta=5))
                order = Order(business_id=business.id, branch_id=branch.id, number="ENSAYO-1", folio=1,
                    channel="takeaway", source="whatsapp_agent", status="confirmed", payment_method="yape",
                    payment_status="evidence_received", customer_name="Cliente sintético", total=25, subtotal=25,
                    submitted_at=utcnow(), sent_to_kitchen_at=utcnow())
                db.add(order); db.flush()
                item = OrderItem(order_id=order.id, product_id=product.id, product_name=product.name,
                    variant_name="Mediana", quantity=1, unit_price=25, line_total=25)
                db.add(item); db.flush()
                db.add(KitchenTicket(business_id=business.id, branch_id=branch.id, order_id=order.id,
                    items_snapshot=[{"item_id": item.id, "product_id": product.id, "product_name": product.name,
                        "quantity": 1, "variant_name": "Mediana", "modifiers": []}]))
                db.add(PaymentEvidence(business_id=business.id, order_id=order.id,
                    provider="yape", status="under_review", image_sha256="0" * 64,
                    storage_path=str(receipt)))
                db.add(AgentAttentionRequest(business_id=business.id, branch_id=branch.id,
                    order_id=order.id, kind="fulfillment_changed", status="pending",
                    sender="51999000001", source_key="synthetic-fulfillment-change",
                    summary="Pedido modificado antes del despacho · ensayo sin valor comercial",
                    details={"from_channel": "delivery", "to_channel": "takeaway"}))
    with TestClient(app) as client:
        admin = {"X-Dev-Auth": "api-tools-local-only", "X-Dev-Role": "superadmin", "X-Dev-User": "dev-superadmin"}
        created = client.post("/api/v1/admin/integration-credentials", headers=admin,
            json={"branch_id": 1, "name": "Agente de ensayo", "profile": "restaurant_agent"})
        assert created.status_code == 201, created.status_code
    print("Synthetic API: http://127.0.0.1:8769 | businesses 1,2 | no external integrations", flush=True)


if __name__ == "__main__":
    import uvicorn
    seed()
    uvicorn.run(app, host="127.0.0.1", port=8769, access_log=False)
