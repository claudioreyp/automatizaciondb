"""Complete catalog and POS-derived time rules across businesses, no external services."""
from datetime import date, datetime, time, timezone
from decimal import Decimal
import pytest
from sqlalchemy import select
from app.database import SessionLocal
from app.models import (Business, BranchSettings, Modifier, ModifierGroup, Product, ProductModifierGroup, ProductVariant,
    Promotion, PromotionTarget, ScheduleAssignment, ScheduleShift, ServiceSchedule)
from test_restaurant_agent_boundary import agent_headers, leased


def test_customer_catalog_keeps_over_150_products_30_variants_and_20_groups(client, tenant, agent_headers):
    with SessionLocal.begin() as db:
        for number in range(159):
            db.add(Product(business_id=tenant["business_id"], branch_id=tenant["branch_id"], sku=f"LARGE-{number}",
                name=f"Producto {number}", price=number + 1))
        for number in range(35):
            db.add(ProductVariant(product_id=tenant["product_id"], name=f"Variante {number}", price_delta=number))
        for number in range(24):
            group = ModifierGroup(business_id=tenant["business_id"], branch_id=tenant["branch_id"], name=f"Grupo {number}",
                minimum=2 if number == 0 else 0, maximum=3, required=number == 0, allow_repeats=False, max_per_option=1)
            db.add(group)
            db.flush()
            db.add(ProductModifierGroup(product_id=tenant["product_id"], group_id=group.id))
            for option in range(4):
                db.add(Modifier(group_id=group.id, name=f"Opción {number}-{option}", price_delta=0))
    response = client.get("/api/v1/integrations/context/catalog", headers=agent_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["complete"] is True and body["next_cursor"] is None and body["product_count"] == 160
    assert len(body["products"]) == 160
    product = next(row for row in body["products"] if row["id"] == tenant["product_id"])
    assert len(product["variants"]) == 35 and len(product["modifier_groups"]) == 24
    assert all(len(group["modifiers"]) == 4 for group in product["modifier_groups"])
    mandatory = next(row for row in product["modifier_groups"] if row["required"])
    assert mandatory["minimum"] == 2 and mandatory["maximum"] == 3
    assert all(row["minimum"] == 0 for row in product["modifier_groups"] if not row["required"])
    assert not {"recipe", "inventory", "available_units", "track_stock"}.intersection(product)
    prior = body["catalog_version"]
    with SessionLocal.begin() as db:
        db.get(Product, tenant["product_id"]).price = 999
    assert client.get("/api/v1/integrations/context/catalog", headers=agent_headers).json()["catalog_version"] != prior


@pytest.mark.parametrize("schedule_open", [True, False])
def test_business_timezone_and_promotion_schedule_match_catalog_and_pricing(client, tenant, agent_headers, monkeypatch, schedule_open):
    from app import services, settings_service, agent_catalog
    # Monday in Tokyo, still Sunday in Lima. A global Lima rule would lose the discount.
    instant = datetime(2026, 10, 19, 2, 0, tzinfo=timezone.utc)
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)
    monkeypatch.setattr(services, "datetime", FrozenDateTime)
    monkeypatch.setattr(settings_service, "utcnow", lambda: instant)
    monkeypatch.setattr(agent_catalog, "utcnow", lambda: instant)
    with SessionLocal.begin() as db:
        db.get(Business, tenant["business_id"]).timezone = "Asia/Tokyo"
        promotion = Promotion(business_id=tenant["business_id"], branch_id=tenant["branch_id"], name="Oferta lunes local",
            promotion_type="product_discount", target_scope="products", discount_type="percentage", discount_value=25,
            starts_on=date(2026, 10, 19), ends_on=date(2026, 10, 19), weekdays=[0], service_channels=["digital_takeaway"])
        db.add(promotion)
        db.flush()
        db.add(PromotionTarget(promotion_id=promotion.id, product_id=tenant["product_id"]))
        schedule = ServiceSchedule(business_id=tenant["business_id"], branch_id=tenant["branch_id"], name="Horario promoción", kind="additional")
        db.add(schedule)
        db.flush()
        db.add(ScheduleShift(schedule_id=schedule.id, day_of_week=0,
            starts_at=time(10 if schedule_open else 12), ends_at=time(12 if schedule_open else 14)))
        db.add(ScheduleAssignment(business_id=tenant["business_id"], branch_id=tenant["branch_id"],
            schedule_id=schedule.id, promotion_id=promotion.id))
    catalog = client.get("/api/v1/integrations/context/catalog", headers=agent_headers).json()
    assert catalog["timezone"] == "Asia/Tokyo"
    assert len(catalog["promotions"]) == (1 if schedule_open else 0)
    _, headers = leased(client, agent_headers)
    preview = client.post("/api/v1/integrations/orders/preview", headers=headers,
        json={"channel": "takeaway", "items": [{"product_id": tenant["product_id"], "quantity": 1}]}).json()
    assert preview["known_total"] == (15 if schedule_open else 20)


def test_overnight_primary_and_product_schedule_exposed_and_enforced(client, tenant, agent_headers, monkeypatch):
    from app import settings_service, agent_catalog
    instant = datetime(2026, 10, 19, 6, 30, tzinfo=timezone.utc)  # Monday 01:30 Lima; Sunday's overnight shift.
    monkeypatch.setattr(settings_service, "utcnow", lambda: instant)
    monkeypatch.setattr(agent_catalog, "utcnow", lambda: instant)
    with SessionLocal.begin() as db:
        primary = db.scalar(select(ServiceSchedule).where(ServiceSchedule.branch_id==tenant["branch_id"],ServiceSchedule.kind=="primary"))
        primary.name="Principal"
        for shift in db.scalars(select(ScheduleShift).where(ScheduleShift.schedule_id==primary.id)):
            db.delete(shift)
        additional = ServiceSchedule(business_id=tenant["business_id"], branch_id=tenant["branch_id"], name="Solo tarde", kind="additional")
        db.add_all([primary, additional])
        db.flush()
        db.add_all([ScheduleShift(schedule_id=primary.id, day_of_week=6, starts_at=time(22), ends_at=time(2)),
            ScheduleShift(schedule_id=additional.id, day_of_week=0, starts_at=time(12), ends_at=time(14))])
        db.add(ScheduleAssignment(business_id=tenant["business_id"], branch_id=tenant["branch_id"],
            schedule_id=additional.id, product_id=tenant["product_id"]))
    catalog = client.get("/api/v1/integrations/context/catalog", headers=agent_headers).json()
    assert catalog["open_now"] is True and catalog["products"][0]["schedule_available"] is False
    _, headers = leased(client, agent_headers)
    response = client.post("/api/v1/integrations/orders/preview", headers=headers,
        json={"channel": "takeaway", "items": [{"product_id": tenant["product_id"], "quantity": 1}]})
    assert response.status_code == 409 and response.json()["code"] == "PRODUCT_SCHEDULE_CLOSED"


def test_promotions_expose_only_enabled_digital_channels(client, tenant, agent_headers):
    with SessionLocal.begin() as db:
        db.add(BranchSettings(business_id=tenant["business_id"], branch_id=tenant["branch_id"],
            digital_takeaway=True, digital_delivery=False, digital_tables=False))
        for name, channels in [("Mostrador", ["pos_counter"]), ("Delivery deshabilitado", ["digital_delivery"]),
                ("Recojo", ["digital_takeaway"]), ("Mixta", ["digital_takeaway", "digital_delivery"])]:
            db.add(Promotion(business_id=tenant["business_id"], branch_id=tenant["branch_id"], name=name,
                promotion_type="product_discount", target_scope="products", discount_type="percentage", discount_value=10,
                service_channels=channels))
    catalog = client.get("/api/v1/integrations/context/catalog", headers=agent_headers).json()
    assert {row["name"] for row in catalog["promotions"]} == {"Recojo", "Mixta"}
    assert next(row for row in catalog["promotions"] if row["name"] == "Mixta")["service_channels"] == ["digital_takeaway", "digital_delivery"]
