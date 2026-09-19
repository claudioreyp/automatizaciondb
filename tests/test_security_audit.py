from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import (
    AuditEvent, Branch, CashMovement, CashRegister, CashSession, IdempotencyRecord,
    InventoryItem, KitchenTicket, Order, OrderItem, Product,
)
from test_command_workflow import create_order, send_order
from test_order_folios_revisions import revise
from test_cash_cuts import add_movement


def listing(client, headers, **params):
    response = client.get("/api/v1/settings/audit", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def detail(client, headers, event_id, **params):
    response = client.get(f"/api/v1/settings/audit/{event_id}", params=params, headers=headers)
    assert response.status_code == 200, response.text
    result = response.json()
    assert set(result) == {"id", "branch_id", "actor_name", "occurred_at", "summary", "fields", "sections", "target"}
    assert "payload" not in result
    assert result["actor_name"] not in result["summary"]
    for field in result["fields"] + [field for section in result["sections"] for field in section["fields"]]:
        assert set(field) == {"label", "value"}
        assert field["value"] is None or isinstance(field["value"], str)
    return result


def fields(result):
    return {row["label"]: row["value"] for row in result["fields"]}


def event_id(action, order_id=None):
    with SessionLocal() as db:
        query = select(AuditEvent).where(AuditEvent.action == action)
        if order_id is not None:
            query = query.where(AuditEvent.entity_id == str(order_id))
        return db.scalar(query.order_by(AuditEvent.id.desc())).id


def cancel(client, headers, order, reason="No quiso esperar"):
    response = client.post(f"/api/v1/orders/{order['id']}/transition", json={
        "status": "cancelled", "reason": reason, "expected_version": order["version"],
    }, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def edit(item_id, product_id, price):
    return {"type": "edit", "item_id": item_id, "replacement": {
        "name": f"Plato de prueba {product_id}", "quantity": 1, "unit_price": price,
    }}


def add_event(tenant, *, action="order.cancelled", entity_type="order", entity_id=None,
              payload=None, branch_id=None, business_id=None, created_at=None):
    with SessionLocal.begin() as db:
        event = AuditEvent(
            business_id=business_id or tenant["business_id"], branch_id=branch_id,
            action=action, entity_type=entity_type, entity_id=str(entity_id) if entity_id is not None else None,
            actor_id="test-user", actor_display_name="Operador de prueba", payload=payload or {},
            created_at=created_at or datetime(2026, 9, 12, 15, tzinfo=timezone.utc),
        )
        db.add(event)
        db.flush()
        return event.id


@pytest.mark.parametrize("paid", [0, 7, 20])
def test_order_cancellation_snapshot_identity_money_and_replay(client, tenant, auth_headers, paid):
    order = send_order(client, create_order(client, tenant, auth_headers), auth_headers)["order"]
    if paid:
        response = client.post(f"/api/v1/orders/{order['id']}/payments", json={
            "method": "cash", "amount": paid, "register_id": tenant["register_id"],
        }, headers={**auth_headers, "Idempotency-Key": "paid"})
        assert response.status_code == 201, response.text
        order = response.json()["order"]
    cancelled = cancel(client, auth_headers, order)
    audit_id = event_id("order.cancelled", order["id"])
    first = detail(client, auth_headers, audit_id, branch_id=tenant["branch_id"])
    assert fields(first)["ID de pedido"] == f"#{order['number']}"
    assert fields(first)["Folio de pedido"] == f"#{order['folio']}"
    assert fields(first)["Monto cobrado"] == f"S/ {paid:.2f}"
    assert fields(first)["Total"] == "S/ 20.00"
    assert fields(first)["Motivo de cancelaci\u00f3n"] == "No quiso esperar"
    assert first["target"] == {"kind": "order", "branch_id": tenant["branch_id"],
        "label": f"Pedido #{order['number']} (#{order['folio']})", "order_id": order["id"]}
    assert ("no pagado" in first["summary"]) == (paid == 0)
    assert first["occurred_at"].endswith("-05:00")
    with SessionLocal.begin() as db:
        db.get(Product, tenant["product_id"]).name = "Nombre actual diferente"
        db.get(OrderItem, order["items"][0]["id"]).product_name = "Cambio posterior aislado"
        stored = db.get(AuditEvent, audit_id)
        assert stored.branch_id == tenant["branch_id"]
        snapshot = deepcopy(stored.payload)
    assert detail(client, auth_headers, audit_id) == first
    cancel(client, auth_headers, cancelled, "No debe reemplazar")
    assert listing(client, auth_headers, category="order_cancellation")["total"] == 1
    with SessionLocal() as db:
        assert db.get(AuditEvent, audit_id).payload == snapshot


def test_revision_is_one_event_with_many_products_and_immutable_successive_reductions(client, tenant, auth_headers):
    order = create_order(client, tenant, auth_headers, items=[
        {"product_id": tenant["product_id"], "quantity": 1},
        {"product_id": tenant["product_id"], "quantity": 2},
        {"product_id": tenant["product_id"], "quantity": 1},
    ])
    sent = send_order(client, order, auth_headers)["order"]
    operations = [edit(order["items"][0]["id"], tenant["product_id"], 15),
        {"type": "cancel", "item_id": order["items"][1]["id"], "reason": "Por error"}]
    response = revise(client, auth_headers, sent, operations, "revision-1")
    assert response.status_code == 201, response.text
    assert revise(client, auth_headers, sent, operations, "revision-1").json() == response.json()
    audit_id = event_id("order.items_revised", order["id"])
    first = detail(client, auth_headers, audit_id)
    assert len(first["sections"]) == 2
    assert fields(first["sections"][0])["Importe anterior"] == "S/ 20.00"
    assert fields(first["sections"][0])["Importe posterior"] == "S/ 15.00"
    assert fields(first["sections"][1])["Cantidad cancelada"] == "2"
    assert fields(first["sections"][1])["Motivo de cancelaci\u00f3n"] == "Por error"
    for category in ["item_cancellation", "amount_reduction"]:
        page = listing(client, auth_headers, category=category)
        assert page["total"] == 1
        assert page["items"][0]["id"] == audit_id
        assert page["items"][0]["categories"] == ["item_cancellation", "amount_reduction"]
    replacement_id = response.json()["replacement_item_ids"][0]
    newer = revise(client, auth_headers, response.json()["order"], [edit(replacement_id, tenant["product_id"], 12)], "revision-2")
    assert newer.status_code == 201, newer.text
    second = detail(client, auth_headers, event_id("order.items_revised", order["id"]))
    assert fields(second["sections"][0])["Importe anterior"] == "S/ 15.00"
    assert fields(second["sections"][0])["Importe posterior"] == "S/ 12.00"
    assert detail(client, auth_headers, audit_id) == first
    assert listing(client, auth_headers, category="amount_reduction")["total"] == 2
    assert listing(client, auth_headers, category="item_cancellation")["total"] == 1
    with SessionLocal() as db:
        assert db.query(AuditEvent).filter_by(action="order.items_revised").count() == 2
        assert db.get(AuditEvent, audit_id).branch_id == tenant["branch_id"]


@pytest.mark.parametrize("legacy", [False, True])
def test_cash_snapshot_both_endpoints_idempotent_and_archived_exact_lookup(client, tenant, auth_headers, legacy):
    if legacy:
        with SessionLocal.begin() as db:
            session = CashSession(business_id=tenant["business_id"], branch_id=tenant["branch_id"],
                register_id=tenant["register_id"], opened_by="test")
            db.add(session)
            db.flush()
            session_id = session.id
        path = f"/api/v1/cash/sessions/{session_id}/movements"
        payload = {"movement_type": "withdrawal", "amount": 100, "note": "Insumos", "payment_method": "cash"}
    else:
        path = f"/api/v1/cash/registers/{tenant['register_id']}/movements"
        payload = {"movement_type": "withdrawal", "amount": 100, "note": "Insumos", "expected_version": 0}
    headers = {**auth_headers, "Idempotency-Key": "movement"}
    response = client.post(path, json=payload, headers=headers)
    assert response.status_code == 201, response.text
    assert client.post(path, json=payload, headers=headers).json() == response.json()
    movement_id = response.json()["id"]
    audit_id = event_id("cash.movement_created")
    first = detail(client, auth_headers, audit_id)
    assert first["summary"].endswith("realiz\u00f3 un retiro de efectivo")
    assert fields(first)["Caja"] == "Caja"
    assert fields(first)["Nota"] == "Insumos"
    assert fields(first)["Monto"] == "S/ 100.00"
    assert first["target"] == {"kind": "cash_movement", "branch_id": tenant["branch_id"],
        "register_id": tenant["register_id"], "movement_id": movement_id, "label": f"Movimiento #{movement_id}"}
    with SessionLocal.begin() as db:
        movement = db.get(CashMovement, movement_id)
        session = db.get(CashSession, movement.cash_session_id)
        session.status = "closed"
        session.closed_at = datetime(2026, 9, 12, tzinfo=timezone.utc)
        register = db.get(CashRegister, tenant["register_id"])
        register.name = "Renombrada"
        register.active = False
        register.archived_at = session.closed_at
        assert db.query(CashMovement).count() == 1
        assert db.query(AuditEvent).filter_by(action="cash.movement_created").count() == 1
        assert db.get(AuditEvent, audit_id).branch_id == tenant["branch_id"]
    assert detail(client, auth_headers, audit_id) == first
    found = client.get(f"/api/v1/cash/registers/{tenant['register_id']}/movements", params={"movement_id": movement_id}, headers=auth_headers)
    assert found.status_code == 200, found.text
    assert found.json()["total"] == 1
    assert found.json()["items"][0]["id"] == movement_id
    assert listing(client, auth_headers, category="cash_withdrawal")["total"] == 1


def test_exact_movement_branch_contract_for_unscoped_owner_no_primary_fallback(client, tenant, auth_headers):
    movement = add_movement(client, tenant["register_id"], auth_headers, key="exact", movement_type="withdrawal", amount=5)
    with SessionLocal.begin() as db:
        branch = Branch(business_id=tenant["business_id"], slug="second", name="Second")
        db.add(branch)
        db.flush()
        other = CashRegister(business_id=tenant["business_id"], branch_id=branch.id, name="Otra caja")
        same = CashRegister(business_id=tenant["business_id"], branch_id=tenant["branch_id"], name="Otra de la misma sucursal")
        db.add_all([other, same])
        db.flush()
        second_branch, other_register, same_register = branch.id, other.id, same.id
        register = db.get(CashRegister, tenant["register_id"])
        register.active = False
        register.archived_at = datetime.now(timezone.utc)
        session = db.get(CashSession, movement["session_id"])
        session.status = "closed"
    headers = {key: value for key, value in auth_headers.items() if key != "X-Branch-Id"}
    path = f"/api/v1/cash/registers/{tenant['register_id']}/movements"
    params = {"branch_id": tenant["branch_id"], "movement_id": movement["id"], "page": 1, "page_size": 10}
    result = client.get(path, params=params, headers=headers)
    assert result.status_code == 200, result.text
    assert result.json()["branch_id"] == tenant["branch_id"]
    assert result.json()["register"] == {"id": tenant["register_id"], "name": "Caja", "active": False}
    assert result.json()["total"] == 1
    assert client.get(path, params={**params, "branch_id": second_branch}, headers=headers).status_code == 404
    assert client.get(f"/api/v1/cash/registers/{other_register}/movements", params=params, headers=headers).status_code == 404
    empty = client.get(f"/api/v1/cash/registers/{same_register}/movements", params=params, headers=headers)
    assert empty.status_code == 200
    assert empty.json()["items"] == [] and empty.json()["total"] == 0
    assert client.get(path, params={**params, "movement_id": 0}, headers=headers).status_code == 422
    assert client.get(path, params={**params, "branch_id": 0}, headers=headers).status_code == 422
    with SessionLocal() as db:
        assert db.query(CashSession).count() == 1


def test_legacy_scope_no_catalog_no_guessed_movement_and_malformed_ids(client, tenant, auth_headers):
    order = create_order(client, tenant, auth_headers)
    recovered = add_event(tenant, entity_id=order["id"])
    unproven = [add_event(tenant, entity_id=value, payload={"order_id": order["id"], "branch_id": tenant["branch_id"]}) for value in [None, "garbage", "1x", "01", "99999"]]
    with SessionLocal.begin() as db:
        session = CashSession(business_id=tenant["business_id"], branch_id=tenant["branch_id"], register_id=tenant["register_id"], opened_by="test")
        db.add(session)
        db.flush()
        movements = [CashMovement(cash_session_id=session.id, movement_type="withdrawal", amount=7, note="Nota historica"), CashMovement(cash_session_id=session.id, movement_type="income", amount=10)]
        db.add_all(movements)
        db.flush()
        session_id, movement_id = session.id, movements[0].id
    cash_event = add_event(tenant, action="cash.movement_created", entity_type="cash_movement", entity_id=movement_id)
    ambiguous = add_event(tenant, action="cash.movement_created", entity_type="cash_session", entity_id=session_id)
    ids = {row["id"] for row in listing(client, auth_headers, page_size=100)["items"]}
    assert {recovered, cash_event, ambiguous} <= ids
    assert not ids.intersection(unproven)
    recovered_detail = detail(client, auth_headers, recovered)
    recovered_list_item = next(row for row in listing(client, auth_headers, page_size=100)["items"] if row["id"] == recovered)
    assert recovered_list_item["branch_id"] == tenant["branch_id"]
    assert recovered_list_item["summary"] == "cancel\u00f3 un pedido"
    assert recovered_detail["target"]["order_id"] == order["id"]
    assert recovered_detail["branch_id"] == tenant["branch_id"]
    assert fields(recovered_detail)["Motivo de cancelaci\u00f3n"] is None
    assert fields(recovered_detail)["Monto cobrado"] is None
    assert "no pagado" not in recovered_detail["summary"]
    cash_detail = detail(client, auth_headers, cash_event)
    assert fields(cash_detail)["Caja"] is None
    assert fields(cash_detail)["Nota"] == "Nota historica"
    assert detail(client, auth_headers, ambiguous)["target"] is None
    assert fields(detail(client, auth_headers, ambiguous))["ID de movimiento"] is None
    assert listing(client, auth_headers, category="cash_withdrawal")["total"] == 1
    business_headers = {key: value for key, value in auth_headers.items() if key != "X-Branch-Id"}
    for unknown in unproven:
        assert detail(client, business_headers, unknown)["target"] is None
        assert client.get(f"/api/v1/settings/audit/{unknown}", headers=auth_headers).status_code == 404


def test_scope_and_permissions_match_list_and_detail(client, tenant, auth_headers):
    with SessionLocal.begin() as db:
        branch = Branch(business_id=tenant["business_id"], slug="second", name="Second")
        db.add(branch)
        db.flush()
        second_branch = branch.id
    own = add_event(tenant, branch_id=tenant["branch_id"])
    second = add_event(tenant, branch_id=second_branch)
    other = add_event(tenant, business_id=tenant["other_business_id"], branch_id=tenant["other_branch_id"])
    assert [row["id"] for row in listing(client, auth_headers)["items"]] == [own]
    for event in [second, other, 9999]:
        assert client.get(f"/api/v1/settings/audit/{event}", headers=auth_headers).status_code == 404
    for path in ["/api/v1/settings/audit", f"/api/v1/settings/audit/{own}"]:
        assert client.get(path, headers=auth_headers, params={"branch_id": second_branch}).status_code == 403
        assert client.get(path, headers=auth_headers, params={"business_id": tenant["other_business_id"]}).status_code == 403
        for role in ["cashier", "waiter", "kitchen"]:
            assert client.get(path, headers={**auth_headers, "X-Dev-Role": role}).status_code == 403
        assert client.get(path, headers={**auth_headers, "X-Dev-Role": "manager"}).status_code == 200
    business_headers = {key: value for key, value in auth_headers.items() if key != "X-Branch-Id"}
    assert listing(client, business_headers)["total"] == 2
    assert listing(client, business_headers, branch_id=second_branch)["items"][0]["id"] == second


def test_legacy_revision_uses_exact_ticket_pair_and_not_current_promotions(client, tenant, auth_headers):
    created = create_order(client, tenant, auth_headers, items=[
        {"product_id": tenant["product_id"], "quantity": 1},
        {"product_id": tenant["product_id"], "quantity": 2},
    ])
    order = send_order(client, created, auth_headers)["order"]
    operations = [edit(order["items"][0]["id"], tenant["product_id"], 15),
        {"type": "cancel", "item_id": order["items"][1]["id"], "reason": "No lo quiere"}]
    revised = revise(client, auth_headers, order, operations, "old-revision")
    assert revised.status_code == 201, revised.text
    audit_id = event_id("order.items_revised")
    first = detail(client, auth_headers, audit_id)
    with SessionLocal.begin() as db:
        event = db.get(AuditEvent, audit_id)
        event.branch_id = None
        event.payload = {"operations": [{key: value for key, value in op.items() if key not in {"before", "after"}} for op in event.payload["operations"]]}
        db.get(OrderItem, revised.json()["replacement_item_ids"][0]).promotion_discount = Decimal("9")
        db.get(Product, tenant["product_id"]).name = "No usar el catalogo"
    recovered = detail(client, auth_headers, audit_id)
    assert recovered["sections"] == first["sections"]
    assert recovered["branch_id"] == tenant["branch_id"]
    assert listing(client, auth_headers, category="amount_reduction")["total"] == 1
    assert listing(client, auth_headers, category="item_cancellation")["total"] == 1
    with SessionLocal.begin() as db:
        ticket = db.scalar(select(KitchenTicket).where(KitchenTicket.order_id == order["id"]))
        ticket.context_snapshot = {}
    unavailable = detail(client, auth_headers, audit_id)
    assert fields(unavailable["sections"][0])["Importe posterior"] is None
    assert fields(unavailable["sections"][0])["Precio posterior"] == "S/ 15.00"


def test_legacy_cross_tenant_entities_never_recovered_or_linked(client, tenant, auth_headers):
    foreign_headers = {**auth_headers, "X-Business-Id": str(tenant["other_business_id"]), "X-Branch-Id": str(tenant["other_branch_id"])}
    foreign = client.post("/api/v1/orders", json={
        "branch_id": tenant["other_branch_id"], "channel": "counter", "items": [{"name": "Privado", "unit_price": 12, "quantity": 1}],
    }, headers={**foreign_headers, "Idempotency-Key": "foreign"})
    assert foreign.status_code == 201, foreign.text
    audit_id = add_event(tenant, entity_id=foreign.json()["id"])
    mismatch = add_event(tenant, branch_id=tenant["branch_id"], entity_id=foreign.json()["id"])
    assert client.get(f"/api/v1/settings/audit/{audit_id}", headers=auth_headers).status_code == 404
    assert detail(client, auth_headers, mismatch)["target"] is None
    assert fields(detail(client, auth_headers, mismatch))["ID de pedido"] is None
    business_headers = {key: value for key, value in auth_headers.items() if key != "X-Branch-Id"}
    assert detail(client, business_headers, audit_id)["target"] is None
    with SessionLocal.begin() as db:
        session = CashSession(business_id=tenant["other_business_id"], branch_id=tenant["other_branch_id"], register_id=tenant["register_id"], opened_by="foreign")
        db.add(session)
        db.flush()
        movement = CashMovement(cash_session_id=session.id, movement_type="withdrawal", amount=13, note="Nota privada")
        db.add(movement)
        db.flush()
        session_id, movement_id = session.id, movement.id
    for entity, entity_id in [("cash_session", session_id), ("cash_movement", movement_id)]:
        event = add_event(tenant, action="cash.movement_created", entity_type=entity, entity_id=entity_id)
        assert client.get(f"/api/v1/settings/audit/{event}", headers=auth_headers).status_code == 404
        result = detail(client, business_headers, event)
        assert result["target"] is None
        assert "Nota privada" not in str(result)


def test_unknown_legacy_replacement_does_not_follow_another_orders_item(client, tenant, auth_headers):
    original = create_order(client, tenant, auth_headers)
    other = create_order(client, tenant, auth_headers, key="other", items=[{"name": "Otro pedido", "unit_price": 1, "quantity": 1}])
    event = add_event(tenant, entity_id=original["id"], action="order.items_revised", payload={"operations": [{
        "type": "edit", "item_id": original["items"][0]["id"], "replacement_item_id": other["items"][0]["id"],
    }]})
    result = detail(client, auth_headers, event)
    assert fields(result["sections"][0])["Producto posterior"] is None
    assert fields(result["sections"][0])["Importe posterior"] is None
    assert listing(client, auth_headers, category="amount_reduction")["total"] == 0


def test_cash_legacy_snapshot_precedence_and_income_not_withdrawal(client, tenant, auth_headers):
    movement = add_movement(client, tenant["register_id"], auth_headers, key="withdraw", movement_type="withdrawal", amount=30)
    income = add_movement(client, tenant["register_id"], auth_headers, key="income", movement_type="income", amount=50)
    assert listing(client, auth_headers, category="cash_withdrawal")["total"] == 1
    with SessionLocal.begin() as db:
        event = db.scalar(select(AuditEvent).where(AuditEvent.action == "cash.movement_created", AuditEvent.entity_id == str(movement["id"])))
        audit_id = event.id
        event.branch_id = None
        event.payload = {"register_id": tenant["register_id"], "amount": 30, "movement_type": "withdrawal", "note": "Nota guardada"}
        # The immutable event takes priority even if the retained movement is corrected later.
        db.get(CashMovement, movement["id"]).note = "Nota posterior"
    result = detail(client, auth_headers, audit_id)
    assert fields(result)["Nota"] == "Nota guardada"
    assert fields(result)["Caja"] is None
    assert fields(result)["Monto"] == "S/ 30.00"
    assert result["target"]["movement_id"] == movement["id"]
    assert result["target"]["movement_id"] != income["id"]


def test_semantic_pagination_above_200_stable_dates_are_lima(client, tenant, auth_headers):
    ids = [add_event(tenant, branch_id=tenant["branch_id"], created_at=datetime(2026, 9, 13, 4, 59, 59, tzinfo=timezone.utc)) for _ in range(213)]
    outside = add_event(tenant, branch_id=tenant["branch_id"], created_at=datetime(2026, 9, 13, 5, tzinfo=timezone.utc))
    params = {"category": "order_cancellation", "from": "2026-09-12", "to": "2026-09-12", "page_size": 12}
    first = listing(client, auth_headers, **params)
    assert first["total"] == 213
    assert [row["id"] for row in first["items"]] == ids[::-1][:12]
    last = listing(client, auth_headers, **params, page=18)
    assert [row["id"] for row in last["items"]] == ids[::-1][204:]
    assert outside not in [row["id"] for row in first["items"]]
    assert listing(client, auth_headers, category="order_cancellation")["total"] == 214
    assert listing(client, auth_headers, action="order.cancelled", **params)["total"] == 213
    assert listing(client, auth_headers, action="payment", **params)["total"] == 0
    assert client.get("/api/v1/settings/audit", params={"category": "unknown"}, headers=auth_headers).status_code == 422
    assert client.get("/api/v1/settings/audit", params={"from": "2026-09-13", "to": "2026-09-12"}, headers=auth_headers).status_code == 422
    assert listing(client, auth_headers, **params, page=100)["items"] == []


def test_details_allowlist_never_return_arbitrary_payload(client, tenant, auth_headers):
    audit_id = add_event(tenant, action="integration.changed", entity_type="integration", branch_id=tenant["branch_id"],
        payload={"token": "not-a-real-secret", "internal_prompt": "do not publish", "url": "https://example.test/private"})
    result = detail(client, auth_headers, audit_id)
    assert result["target"] is None
    assert all(secret not in str(result) for secret in ["not-a-real-secret", "do not publish", "example.test"])
    listed = listing(client, auth_headers)["items"][0]
    assert set(listed) == {"id", "business_id", "branch_id", "actor_id", "actor_display_name", "action", "entity_type", "entity_id", "payload", "created_at", "summary", "categories"}


@pytest.mark.parametrize("action,summary", [
    ("order.created", "cre\u00f3 un pedido"),
    ("cash.cut_created", "realiz\u00f3 un corte de caja"),
    ("settings.member.updated", "actualiz\u00f3 un miembro del equipo"),
    ("area.archived", "archiv\u00f3 una zona"),
    ("new.unknown", "realiz\u00f3 una acci\u00f3n registrada"),
])
def test_known_actions_have_action_only_human_summaries(client, tenant, auth_headers, action, summary):
    event = add_event(tenant, action=action, branch_id=tenant["branch_id"])
    result = detail(client, auth_headers, event)
    assert result["summary"] == summary
    assert result["actor_name"] == "Operador de prueba"
    assert listing(client, auth_headers)["items"][0]["summary"] == summary


@pytest.mark.parametrize("operation", ["revision", "cancel", "modern_cash", "legacy_cash"])
def test_audit_and_mutation_rollback_together(client, tenant, auth_headers, monkeypatch, operation):
    import app.api as api
    import app.services as services

    order = send_order(client, create_order(client, tenant, auth_headers), auth_headers)["order"]
    with SessionLocal.begin() as db:
        session = CashSession(business_id=tenant["business_id"], branch_id=tenant["branch_id"], register_id=tenant["register_id"], opened_by="test")
        db.add(session)
        db.flush()
        session_id, session_version = session.id, session.version
        before = (db.query(AuditEvent).count(), db.query(IdempotencyRecord).count(), db.query(OrderItem).count(), db.get(InventoryItem, tenant["inventory_id"]).quantity)
        kitchen_before = deepcopy(db.scalar(select(KitchenTicket).where(KitchenTicket.order_id == order["id"])).context_snapshot)
    original_audit = services.audit

    def fail_after_audit(*args, **kwargs):
        original_audit(*args, **kwargs)
        args[0].flush()
        raise RuntimeError("isolated audit failure")

    monkeypatch.setattr(services if operation in {"revision", "cancel"} else api, "audit", fail_after_audit)
    with pytest.raises(RuntimeError, match="isolated audit failure"):
        if operation == "revision":
            revise(client, auth_headers, order, [edit(order["items"][0]["id"], tenant["product_id"], 15)], "rollback")
        elif operation == "cancel":
            cancel(client, auth_headers, order)
        else:
            path = f"/api/v1/cash/registers/{tenant['register_id']}/movements" if operation == "modern_cash" else f"/api/v1/cash/sessions/{session_id}/movements"
            client.post(path, json={"movement_type": "withdrawal", "amount": 5, "note": "Rollback", "expected_version": session_version}, headers={**auth_headers, "Idempotency-Key": "rollback"})
    with SessionLocal() as db:
        after = (db.query(AuditEvent).count(), db.query(IdempotencyRecord).count(), db.query(OrderItem).count(), db.get(InventoryItem, tenant["inventory_id"]).quantity)
        assert after == before
        assert db.query(CashMovement).count() == 0
        assert db.get(CashSession, session_id).version == session_version
        assert db.get(Order, order["id"]).status == order["status"]
        assert db.get(Order, order["id"]).version == order["version"]
        assert db.scalar(select(KitchenTicket).where(KitchenTicket.order_id == order["id"])).context_snapshot == kitchen_before
