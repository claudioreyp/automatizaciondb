"""Public promotion eligibility agrees with actual scoped preview and persistence."""
from datetime import date, timedelta

import pytest

from app.database import SessionLocal
from app.models import Product, ProductVariant, Promotion, PromotionTarget, BranchSettings
from test_restaurant_agent_boundary import agent_headers, leased, SENDER


def add_promotion(db, tenant, name, **values):
    target = values.pop("target")
    promotion = Promotion(business_id=tenant["business_id"], branch_id=tenant["branch_id"],
        name=name, service_channels=["digital_takeaway"], **values)
    db.add(promotion); db.flush()
    db.add(PromotionTarget(promotion_id=promotion.id,
        **({"product_id": target} if promotion.target_scope == "products" else {"category_id": target})))
    return promotion.id


@pytest.mark.parametrize("shape,expected", [("same_variant", 20), ("different_variants", 50), ("different_products", 40)])
def test_two_for_one_public_rules_match_preview_and_persisted_order(client, tenant, agent_headers, shape, expected):
    with SessionLocal.begin() as db:
        product = db.get(Product, tenant["product_id"])
        second = Product(business_id=tenant["business_id"], branch_id=tenant["branch_id"], category_id=product.category_id,
            sku="PROMO-SECOND", name="Otro producto", price=20)
        db.add(second); db.flush(); second_id = second.id
        db.add_all([ProductVariant(product_id=product.id, name="Regular", price_delta=0),
            ProductVariant(product_id=product.id, name="Grande", price_delta=10)])
        promo_id = add_promotion(db, tenant, "Dos por uno", promotion_type="buy_x_pay_y", target_scope="categories",
            target=product.category_id, receive_quantity=2, pay_quantity=1)
    catalog = client.get("/api/v1/integrations/context/catalog", headers=agent_headers).json()
    promo = next(p for p in catalog["promotions"] if p["id"] == promo_id)
    assert set(promo["eligible_product_ids"]) == {tenant["product_id"], second_id}
    assert promo["eligible_order_modes"] == ["takeaway"]
    assert all(p["order_modes"] == ["takeaway"] for p in promo["eligible_products"])
    assert promo["application_rules"]["group_by"] == ["product_id", "variant_name", "unit_price"]
    assert promo["application_rules"]["whole_sets_only"] is True
    assert promo["application_rules"]["stacking"] == "best_discount_per_order_line"
    items = [{"product_id": tenant["product_id"], "variant_name": "Regular", "quantity": 2}]
    if shape == "different_variants":
        items = [{"product_id": tenant["product_id"], "variant_name": name, "quantity": 1} for name in ("Regular", "Grande")]
    if shape == "different_products":
        items = [{"product_id": tenant["product_id"], "variant_name": "Regular", "quantity": 1}, {"product_id": second_id, "quantity": 1}]
    _, headers = leased(client, agent_headers)
    preview = client.post("/api/v1/integrations/orders/preview", headers={**headers, "Idempotency-Key": "preview-promo"},
        json={"channel": "takeaway", "payment_method": "cash", "items": items})
    assert preview.status_code == 200, preview.text
    assert preview.json()["final_total"] == expected
    created = client.post("/api/v1/integrations/orders/draft", headers={**headers, "Idempotency-Key": "draft-promo"},
        json={"branch_id": tenant["branch_id"], "channel": "takeaway", "payment_method": "cash",
            "whatsapp_chat_id": SENDER, "customer_phone": SENDER, "quoted_total": expected, "items": items})
    assert created.status_code == 201, created.text
    assert created.json()["total"] == expected
    assert created.json()["promotion_discount"] == (20 if shape == "same_variant" else 0)
    assert created.json()["manual_discount"] == 0


def test_category_discount_channel_availability_and_best_price_are_public(client, tenant, agent_headers):
    with SessionLocal.begin() as db:
        product = db.get(Product, tenant["product_id"])
        db.add(BranchSettings(business_id=tenant["business_id"], branch_id=tenant["branch_id"],
            digital_takeaway=True, digital_delivery=True, digital_tables=False,
            payment_methods={"takeaway": ["cash"], "delivery": ["cash"]}))
        product.service_channels = ["digital_takeaway", "digital_delivery"]
        add_promotion(db, tenant, "Categoría 25%", promotion_type="product_discount", target_scope="categories",
            target=product.category_id, discount_type="percentage", discount_value=25)
        add_promotion(db, tenant, "Producto 2 soles", promotion_type="product_discount", target_scope="products",
            target=product.id, discount_type="fixed_amount", discount_value=2)
        for name, changes in [("Vencida", {"ends_on": date.today()-timedelta(days=3)}),
                ("Futura", {"starts_on": date.today()+timedelta(days=3)}), ("Inactiva", {"active": False})]:
            add_promotion(db, tenant, name, promotion_type="product_discount", target_scope="products",
                target=product.id, discount_type="percentage", discount_value=99, **changes)
        pos_id = add_promotion(db, tenant, "Sólo POS", promotion_type="product_discount", target_scope="products",
            target=product.id, discount_type="percentage", discount_value=99)
        db.get(Promotion, pos_id).service_channels = ["pos_counter"]
    catalog = client.get("/api/v1/integrations/context/catalog", headers=agent_headers).json()
    assert {p["name"] for p in catalog["promotions"]} == {"Categoría 25%", "Producto 2 soles"}
    assert all(p["eligible_order_modes"] == ["takeaway"] for p in catalog["promotions"])
    assert {p["application_rules"]["discount_basis"] for p in catalog["promotions"]} == {"quantity", "line_total"}
    _, headers = leased(client, agent_headers)
    for mode, products_total in [("takeaway", 15), ("delivery", 20)]:
        response = client.post("/api/v1/integrations/orders/preview", headers={**headers, "Idempotency-Key": "price-"+mode},
            json={"channel": mode, "payment_method": "cash", "items": [{"product_id": tenant["product_id"], "quantity": 1}],
                **({"delivery_address": {"address": "Calle sintética 1", "reference": "Ensayo"}} if mode == "delivery" else {})})
        assert response.status_code == 200, response.text
        assert response.json()["known_total"] == products_total + (response.json()["delivery_fee"] or 0)
