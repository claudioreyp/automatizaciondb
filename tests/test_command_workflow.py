from decimal import Decimal

from app.database import SessionLocal
from app.models import InventoryItem, KitchenTicket, Order, Payment, Product, RecipeItem


def create_order(client, tenant, auth_headers, *, items=None, dine_in=False, key="order"):
    response = client.post(
        "/api/v1/orders",
        json={
            "branch_id": tenant["branch_id"],
            "channel": "dine_in" if dine_in else "counter",
            "table_id": tenant["table_id"] if dine_in else None,
            "customer_name": "Claudio Rey",
            "items": items if items is not None else [
                {"product_id": tenant["product_id"], "quantity": 1}
            ],
        },
        headers={**auth_headers, "Idempotency-Key": key},
    )
    assert response.status_code == 201, response.text
    return response.json()


def send_order(client, order, auth_headers, *, key="send"):
    response = client.post(
        f"/api/v1/orders/{order['id']}/confirm-and-send",
        json={"expected_version": order["version"]},
        headers={**auth_headers, "Idempotency-Key": key},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_deferred_payment_creates_one_real_command_and_commits_stock_once(client, tenant, auth_headers):
    order = create_order(client, tenant, auth_headers, key="deferred-order")
    sent = send_order(client, order, auth_headers, key="deferred-send")
    assert sent["order"]["status"] == "sent_to_kitchen"
    assert sent["order"]["payment_status"] == "pending"
    assert sent["order"]["total"] == 20
    assert len(sent["tickets"]) == 1
    assert sent["tickets"][0]["items"][0]["item_id"] == order["items"][0]["id"]
    assert sent["tickets"][0]["items"][0]["quantity"] == 1
    replay = send_order(client, order, auth_headers, key="deferred-send")
    assert replay == sent
    detail = client.get(f"/api/v1/orders/{order['id']}/detail", headers=auth_headers).json()
    assert detail["payment_summary"] == {"paid": 0, "remaining": 20}
    with SessionLocal() as db:
        assert db.query(Order).count() == 1
        assert db.query(KitchenTicket).filter_by(order_id=order["id"]).count() == 1
        assert db.query(Payment).filter_by(order_id=order["id"]).count() == 0
        assert db.get(InventoryItem, tenant["inventory_id"]).quantity == Decimal("9.5")


def test_new_commands_are_global_and_ignore_product_stations(
    client,
    tenant,
    auth_headers,
):
    with SessionLocal.begin() as db:
        second = Product(
            business_id=tenant["business_id"],
            branch_id=tenant["branch_id"],
            sku="DRINK",
            name="Limonada",
            price="8",
            preparation_station="bar",
        )
        db.add(second)
        db.flush()
        db.add(
            RecipeItem(
                product_id=second.id,
                inventory_item_id=tenant["inventory_id"],
                quantity="0.25",
            )
        )
        second_id = second.id

    order = create_order(
        client,
        tenant,
        auth_headers,
        items=[
            {"product_id": tenant["product_id"], "quantity": 1},
            {"product_id": second_id, "quantity": 1},
        ],
        key="global-command-order",
    )
    sent = send_order(client, order, auth_headers, key="global-command-send")
    assert len(sent["tickets"]) == 1
    assert sent["tickets"][0]["station"] == "kitchen"
    assert sent["tickets"][0]["sequence"] == 1
    assert {item["name"] for item in sent["tickets"][0]["items"]} == {
        "Pizza",
        "Limonada",
    }

    appended = client.post(
        f"/api/v1/orders/{order['id']}/item-batches",
        json={
            "expected_version": sent["order"]["version"],
            "items": [{"product_id": second_id, "quantity": 1}],
        },
        headers={**auth_headers, "Idempotency-Key": "global-command-append"},
    )
    assert appended.status_code == 201, appended.text
    assert appended.json()["tickets"][0]["sequence"] == 2
    assert appended.json()["tickets"][0]["kind"] == "addition"
    with SessionLocal() as db:
        sequences = [
            row.sequence
            for row in db.query(KitchenTicket)
            .filter_by(order_id=order["id"])
            .order_by(KitchenTicket.sequence)
        ]
        assert sequences == [1, 2]


def test_complete_history_and_reopen_only_selected_command(
    client,
    tenant,
    auth_headers,
):
    order = create_order(client, tenant, auth_headers, key="history-order")
    sent = send_order(client, order, auth_headers, key="history-send")
    command = sent["tickets"][0]

    active = client.get(
        f"/api/v1/kitchen/commands?branch_id={tenant['branch_id']}&view=active",
        headers=auth_headers,
    )
    assert active.status_code == 200, active.text
    assert active.json()["active_count"] == 1
    assert active.json()["items"][0]["id"] == command["id"]

    completed = client.post(
        f"/api/v1/kitchen/commands/{command['id']}/complete",
        json={"expected_status": "queued"},
        headers={**auth_headers, "Idempotency-Key": "complete-command"},
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["command"]["status"] == "ready"
    repeated = client.post(
        f"/api/v1/kitchen/commands/{command['id']}/complete",
        json={"expected_status": "queued"},
        headers={**auth_headers, "Idempotency-Key": "complete-command"},
    )
    assert repeated.status_code == 200
    assert repeated.json() == completed.json()

    history = client.get(
        f"/api/v1/kitchen/commands?branch_id={tenant['branch_id']}&view=history",
        headers=auth_headers,
    )
    assert history.status_code == 200, history.text
    assert [item["id"] for item in history.json()["items"]] == [command["id"]]

    reopened = client.post(
        f"/api/v1/kitchen/commands/{command['id']}/reopen",
        json={"expected_status": "ready"},
        headers={**auth_headers, "Idempotency-Key": "reopen-command"},
    )
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["command"]["status"] == "preparing"
    history_after = client.get(
        f"/api/v1/kitchen/commands?branch_id={tenant['branch_id']}&view=history",
        headers=auth_headers,
    )
    assert history_after.json()["items"] == []


def test_item_revisions_preserve_audit_recalculate_and_adjust_stock(
    client,
    tenant,
    auth_headers,
):
    order = create_order(client, tenant, auth_headers, key="revision-order")
    sent = send_order(client, order, auth_headers, key="revision-send")
    original_item = sent["order"]["items"][0]

    edited = client.post(
        f"/api/v1/orders/{order['id']}/item-revisions",
        json={
            "expected_version": sent["order"]["version"],
            "operations": [
                {
                    "type": "edit",
                    "item_id": original_item["id"],
                    "replacement": {
                        "product_id": tenant["product_id"],
                        "quantity": 2,
                        "notes": "Agregar queso",
                    },
                }
            ],
        },
        headers={**auth_headers, "Idempotency-Key": "revision-edit"},
    )
    assert edited.status_code == 201, edited.text
    edit_payload = edited.json()
    assert edit_payload["order"]["total"] == 40.0
    assert edit_payload["tickets"][0]["id"] == sent["tickets"][0]["id"]
    assert edit_payload["tickets"][0]["sequence"] == 1
    assert edit_payload["tickets"][0]["version"] == 2
    assert edit_payload["created_ticket_ids"] == []
    assert edit_payload["updated_ticket_ids"] == [sent["tickets"][0]["id"]]
    assert edit_payload["tickets"][0]["items"][0]["action"] == "modified"
    assert edit_payload["tickets"][0]["items"][0]["notes"] == "Agregar queso"
    assert next(
        item for item in edit_payload["order"]["items"] if item["id"] == original_item["id"]
    )["status"] == "superseded"
    replacement_id = edit_payload["replacement_item_ids"][0]
    with SessionLocal() as db:
        assert Decimal(str(db.get(InventoryItem, tenant["inventory_id"]).quantity)) == Decimal("9.0")

    cancelled = client.post(
        f"/api/v1/orders/{order['id']}/item-revisions",
        json={
            "expected_version": edit_payload["order"]["version"],
            "operations": [
                {
                    "type": "cancel",
                    "item_id": replacement_id,
                    "reason": "El cliente cambió de opinión",
                }
            ],
        },
        headers={**auth_headers, "Idempotency-Key": "revision-cancel"},
    )
    assert cancelled.status_code == 409, cancelled.text
    assert cancelled.json()["code"] == "ORDER_REQUIRES_CANCELLATION"
    with SessionLocal() as db:
        assert Decimal(str(db.get(InventoryItem, tenant["inventory_id"]).quantity)) == Decimal("9.0")
        assert db.get(Order, order["id"]).total == Decimal("40")


def test_table_checkout_releases_table_while_kitchen_continues(
    client,
    tenant,
    auth_headers,
):
    order = create_order(
        client,
        tenant,
        auth_headers,
        dine_in=True,
        key="checkout-order",
    )
    sent = send_order(client, order, auth_headers, key="checkout-send")
    started = client.post(
        f"/api/v1/orders/{order['id']}/table-checkout/start",
        json={"expected_version": sent["order"]["version"]},
        headers={**auth_headers, "Idempotency-Key": "checkout-start"},
    )
    assert started.status_code == 200, started.text
    assert started.json()["order"]["checkout_started_at"] is not None

    cash_session = client.post(
        "/api/v1/cash/sessions/open",
        json={"register_id": tenant["register_id"], "opening_amount": 100},
        headers=auth_headers,
    )
    assert cash_session.status_code == 201, cash_session.text
    paid = client.post(
        f"/api/v1/orders/{order['id']}/table-checkout/pay",
        json={
            "expected_version": started.json()["order"]["version"],
            "payments": [
                {
                    "method": "cash",
                    "amount": 20,
                    "cash_session_id": cash_session.json()["id"],
                }
            ],
        },
        headers={**auth_headers, "Idempotency-Key": "checkout-pay"},
    )
    assert paid.status_code == 200, paid.text
    assert paid.json()["order"]["payment_status"] == "paid"
    assert paid.json()["order"]["table_released_at"] is not None
    assert paid.json()["order"]["status"] == "sent_to_kitchen"

    tables = client.get(
        f"/api/v1/tables?branch_id={tenant['branch_id']}",
        headers=auth_headers,
    )
    table = next(item for item in tables.json() if item["id"] == tenant["table_id"])
    assert table["status"] == "available"
    assert table["active_order_id"] is None

    command_id = sent["tickets"][0]["id"]
    completed = client.post(
        f"/api/v1/kitchen/commands/{command_id}/complete",
        json={"expected_status": "queued"},
        headers={**auth_headers, "Idempotency-Key": "checkout-complete"},
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["order"]["status"] == "closed"
    tables_after = client.get(
        f"/api/v1/tables?branch_id={tenant['branch_id']}",
        headers=auth_headers,
    )
    assert next(
        item for item in tables_after.json() if item["id"] == tenant["table_id"]
    )["status"] == "available"

    locked = client.post(
        f"/api/v1/orders/{order['id']}/item-batches",
        json={
            "expected_version": completed.json()["order"]["version"],
            "items": [{"product_id": tenant["product_id"], "quantity": 1}],
        },
        headers={**auth_headers, "Idempotency-Key": "checkout-items-locked"},
    )
    assert locked.status_code == 409
    assert locked.json()["code"] == "ORDER_ITEMS_LOCKED"


def test_empty_table_checkout_explains_and_can_be_cancelled(
    client,
    tenant,
    auth_headers,
):
    order = create_order(
        client,
        tenant,
        auth_headers,
        dine_in=True,
        items=[],
        key="empty-table-order",
    )
    checkout = client.post(
        f"/api/v1/orders/{order['id']}/table-checkout/start",
        json={"expected_version": order["version"]},
        headers={**auth_headers, "Idempotency-Key": "empty-table-checkout"},
    )
    assert checkout.status_code == 409
    assert checkout.json()["code"] == "TABLE_HAS_NO_PRODUCTS"

    cancelled = client.post(
        f"/api/v1/orders/{order['id']}/transition",
        json={"status": "cancelled", "expected_version": order["version"]},
        headers=auth_headers,
    )
    assert cancelled.status_code == 200, cancelled.text
    with SessionLocal() as db:
        stored = db.get(Order, order["id"])
        assert stored.status == "cancelled"
