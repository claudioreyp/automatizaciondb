"""API acceptance without a model, external transport or physical printer."""
from datetime import datetime, time, timezone
import json
from pathlib import Path
from time import perf_counter

import pytest
from sqlalchemy import event, select

from app.database import SessionLocal, engine
from app.models import (Product, ProductVariant, Modifier, ModifierGroup, ProductModifierGroup,
    AgentConversation, BranchSettings, ServiceSchedule, ScheduleShift, Order, Business, Branch)
from app.integration_package import integration_package
from app.agent_tools import TOOLS, AGENT_ROUTE_ALLOWLIST
from app.schemas import RESTAURANT_AGENT_SCOPES
from test_restaurant_agent_boundary import agent_headers, leased, commit, SENDER


def choices(tenant):
    with SessionLocal.begin() as db:
        product = db.get(Product, tenant["product_id"])
        db.get(Branch, tenant["branch_id"]).accepted_payment_methods = ["cash", "yape"]
        product.name = "Pizza combinada"
        db.add(ProductVariant(product_id=product.id, name="Mediana", price_delta=5))
        flavors = ModifierGroup(business_id=product.business_id, branch_id=product.branch_id,
            name="Sabores", minimum=2, maximum=3, required=True)
        extras = ModifierGroup(business_id=product.business_id, branch_id=product.branch_id,
            name="Extras", minimum=0, maximum=2)
        db.add_all([flavors, extras]); db.flush()
        db.add_all([ProductModifierGroup(product_id=product.id, group_id=g.id) for g in (flavors, extras)])
        options = [Modifier(group_id=flavors.id, name=name, price_delta=0) for name in ("Pepperoni", "Hawaiana", "Vegetariana")]
        db.add_all(options); db.flush()
        return [o.id for o in options]


def test_incomplete_selection_reports_all_requirements_and_executes_no_writes(client, tenant, agent_headers):
    choices(tenant)
    lease, headers = leased(client, agent_headers)
    def prevent_write(conn, cursor, sql, parameters, context, many):
        assert sql.lstrip().split()[0].upper() not in {"INSERT", "UPDATE", "DELETE", "REPLACE"}, sql
    event.listen(engine, "before_cursor_execute", prevent_write)
    try:
        result = client.post("/api/v1/integrations/orders/validate-selection", headers=headers,
            json={"items": [{"product_id": tenant["product_id"]}]})
    finally:
        event.remove(engine, "before_cursor_execute", prevent_write)
    assert result.status_code == 200, result.text
    body = result.json()
    assert not body["ready_for_preview"]
    assert {x["code"] for x in body["missing_fields"]} == {"SERVICE_REQUIRED", "PAYMENT_METHOD_REQUIRED", "VARIANT_REQUIRED", "MODIFIER_MINIMUM_REQUIRED"}
    assert not any("Extras" in x["message"] for x in body["missing_fields"])
    with SessionLocal() as db:
        assert not db.get(AgentConversation, lease["conversation_id"]).state["_processing_message"].get("execution_started")


def test_valid_selection_and_server_preview_share_requirements(client, tenant, agent_headers):
    option_ids = choices(tenant)
    _, headers = leased(client, agent_headers)
    payload = {"channel": "takeaway", "payment_method": "cash", "items": [{"product_id": tenant["product_id"],
        "variant_name": "Mediana", "modifiers": [{"modifier_id": i} for i in option_ids[:2]]}]}
    checked = client.post("/api/v1/integrations/orders/validate-selection", headers=headers, json=payload)
    assert checked.status_code == 200 and checked.json()["ready_for_preview"], checked.text
    preview = client.post("/api/v1/integrations/orders/preview", headers=headers,
        json={**payload, "items": [{**line, "quantity": 1} for line in payload["items"]]})
    assert preview.status_code == 200 and preview.json()["known_total"] == 25
    payload["items"][0]["modifiers"].append({"modifier_id": option_ids[0], "quantity": 2})
    invalid = client.post("/api/v1/integrations/orders/validate-selection", headers=headers, json=payload).json()
    assert {x["code"] for x in invalid["issues"]} == {"MODIFIER_MAXIMUM_EXCEEDED", "MODIFIER_REPEAT_LIMIT"}


@pytest.mark.parametrize("field,value", [("unit_price", 0), ("price", 1), ("payment_status", "paid")])
def test_intent_cannot_override_commercial_facts(client, tenant, agent_headers, field, value):
    _, headers = leased(client, agent_headers)
    body = {"channel": "takeaway", "items": [{"product_id": tenant["product_id"], field: value}]}
    response = client.post("/api/v1/integrations/orders/validate-selection", headers=headers, json=body)
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR" and response.json()["retryable"] is False
    assert response.json()["request_id"] == response.headers["x-request-id"]


def test_turn_context_preserves_validated_intent_and_conditionally_omits_catalog(client, tenant, agent_headers):
    choices(tenant)
    lease, headers = leased(client, agent_headers)
    saved = commit(client, agent_headers, lease, complete=False, state={"consumer_context": {
        "schema_version": 1, "intent": "purchase", "selection": {"items": [{"product_id": tenant["product_id"]}]}}})
    assert saved.status_code == 200, saved.text
    payload = {"lease_token": lease["lease_token"], "expected_version": saved.json()["version"]}
    path = f'/api/v1/integrations/conversations/{lease["conversation_id"]}/turn-context'
    first = client.post(path, headers=agent_headers, json=payload)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["conversation_context"]["commercial_authority"] == "pos"
    assert len(body["conversation_context"]["selection_validation"]["missing_fields"]) == 4
    assert not {"_processing_message", "_transport", "lease_token"}.intersection(body["conversation_context"])
    second = client.post(path, headers=agent_headers, json={**payload, "known_catalog_version": body["catalog_version"]})
    assert second.status_code == 200 and second.json()["catalog"] is None and second.json()["catalog_unchanged"]
    with SessionLocal.begin() as db:
        db.get(Product, tenant["product_id"]).available = False
    changed = client.post(path, headers=agent_headers, json={**payload, "known_catalog_version": body["catalog_version"]})
    assert changed.status_code == 200 and not changed.json()["catalog_unchanged"]
    assert "MANUALLY_UNAVAILABLE" in changed.json()["catalog"]["products"][0]["availability"]["reasons"]


def test_foreign_order_reference_cannot_be_saved(client, tenant, agent_headers):
    lease, _ = leased(client, agent_headers)
    with SessionLocal.begin() as db:
        order = Order(business_id=tenant["other_business_id"], branch_id=tenant["other_branch_id"], number=1,
                      source="whatsapp_agent", whatsapp_chat_id=SENDER)
        db.add(order); db.flush(); foreign_id = order.id
    response = commit(client, agent_headers, lease, complete=False,
                      state={"consumer_context": {"order_id": foreign_id}})
    assert response.status_code == 404


def test_closed_schedule_reports_next_opening_in_restaurant_zone(client, tenant, agent_headers, monkeypatch):
    from app import settings_service
    monkeypatch.setattr(settings_service, "utcnow", lambda: datetime(2026, 11, 2, 15, tzinfo=timezone.utc))
    with SessionLocal.begin() as db:
        schedule = db.scalar(select(ServiceSchedule).where(ServiceSchedule.branch_id == tenant["branch_id"]))
        for shift in db.scalars(select(ScheduleShift).where(ScheduleShift.schedule_id == schedule.id)):
            db.delete(shift)
        db.add(ScheduleShift(schedule_id=schedule.id, day_of_week=0, starts_at=time(22), ends_at=time(2)))
    context = client.get("/api/v1/integrations/context", headers=agent_headers)
    assert context.status_code == 200, context.text
    service = context.json()["service_availability"]
    assert service["status"] == "closed" and service["next_opening"] == "2026-11-02T22:00:00-05:00"


def test_registry_openapi_and_generated_package_are_consistent(client, tenant, agent_headers):
    document = client.get("/openapi.json").json()
    package = integration_package("https://api.example.test/api/v1", tenant["business_id"], tenant["branch_id"],
        sorted(RESTAURANT_AGENT_SCOPES), "restaurant_agent")
    assert package["package_version"] == 2
    expected = {t.name for t in TOOLS if "restaurant_agent" in t.profiles and set(t.required_scopes) <= RESTAURANT_AGENT_SCOPES}
    assert set(package["endpoints"]) == expected
    for name, tool in package["endpoints"].items():
        path = tool["url"].replace("https://api.example.test/api/v1", "")
        assert (tool["method"], path) in AGENT_ROUTE_ALLOWLIST
        operation = document["paths"]["/api/v1" + path][tool["method"].lower()]
        assert operation["x-tool-name"] == name
        success = [response for status, response in operation["responses"].items() if status.startswith("2")]
        assert success and all(item.get("schema") for response in success for item in response["content"].values())
        assert operation["responses"]["409"]["content"]["application/json"]["schema"]["$ref"].endswith("AgentError")
    reduced = integration_package("https://api.example.test/api/v1", 1, 1, ["agent:state"], "restaurant_agent")
    assert "turn_context" not in reduced["endpoints"]
    assert "inventory" not in package["endpoints"] and "adjust_inventory" not in package["endpoints"]


def test_catalog_50_and_500_products_use_a_constant_number_of_queries(client, tenant, agent_headers):
    measurements = []
    for size in (50, 500):
        with SessionLocal.begin() as db:
            existing = len(list(db.scalars(select(Product).where(Product.branch_id == tenant["branch_id"]))))
            db.add_all([Product(business_id=tenant["business_id"], branch_id=tenant["branch_id"],
                name=f"Producto {i}", sku=f"SYNTH-{i}", price=3) for i in range(existing, size)])
        queries = []
        def counted(conn, cursor, sql, parameters, context, many):
            if sql.lstrip().upper().startswith("SELECT"):
                queries.append(sql)
        event.listen(engine, "before_cursor_execute", counted)
        started = perf_counter()
        try:
            response = client.get("/api/v1/integrations/context/catalog", headers=agent_headers)
        finally:
            event.remove(engine, "before_cursor_execute", counted)
        assert response.status_code == 200, response.text
        assert response.json()["product_count"] == size
        measurements.append({"products": size, "selects": len(queries), "bytes": len(response.content),
                             "elapsed_ms": round((perf_counter() - started) * 1000, 2)})
    assert measurements[1]["selects"] == measurements[0]["selects"], measurements
    assert measurements[1]["selects"] < 35
    output = Path(".test-results"); output.mkdir(exist_ok=True)
    (output / "catalog-benchmark.json").write_text(json.dumps(measurements, indent=2), encoding="utf-8")
