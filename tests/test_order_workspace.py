from decimal import Decimal

import pytest

from app import api as api_module
from app.database import SessionLocal
from app.models import (
    InventoryItem,
    KitchenTicket,
    Order,
    PaymentEvidence,
    RestaurantTable,
    StockMovement,
)


def create_order(client, tenant, auth_headers, *, quantity=1, key="create-order"):
    response = client.post(
        "/api/v1/orders",
        json={
            "branch_id": tenant["branch_id"],
            "channel": "counter",
            "source": "pos",
            "customer_name": "Claudio Rey",
            "customer_phone": "+51944197385",
            "items": [{"product_id": tenant["product_id"], "quantity": quantity}],
        },
        headers={**auth_headers, "Idempotency-Key": key},
    )
    assert response.status_code == 201, response.text
    return response.json()


def confirm_and_send(client, order, auth_headers, *, key="confirm-send"):
    response = client.post(
        f"/api/v1/orders/{order['id']}/confirm-and-send",
        json={"expected_version": order["version"]},
        headers={**auth_headers, "Idempotency-Key": key},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_workspace_and_detail_are_paginated_and_operational(
    client,
    tenant,
    auth_headers,
):
    first = create_order(client, tenant, auth_headers, key="workspace-1")
    second = create_order(client, tenant, auth_headers, key="workspace-2")
    with SessionLocal.begin() as db:
        db.get(Order, second["id"]).status = "pending_confirmation"

    workspace = client.get(
        f"/api/v1/orders/workspace?branch_id={tenant['branch_id']}&page=1&page_size=1",
        headers=auth_headers,
    )
    assert workspace.status_code == 200, workspace.text
    payload = workspace.json()
    assert payload["total"] == 2
    assert payload["page"] == 1
    assert payload["page_size"] == 1
    assert len(payload["items"]) == 1
    assert payload["review_count"] == 1
    assert payload["items"][0]["id"] == second["id"]
    assert payload["items"][0]["requires_review"] is True
    assert payload["items"][0]["customer_phone"] == "+51944197385"
    assert payload["items"][0]["item_count"] == 1
    assert payload["items"][0]["version"] == second["version"]

    sent = confirm_and_send(client, first, auth_headers, key="workspace-confirm")
    detail = client.get(
        f"/api/v1/orders/{first['id']}/detail",
        headers=auth_headers,
    )
    assert detail.status_code == 200, detail.text
    detail_payload = detail.json()
    assert detail_payload["order"]["id"] == first["id"]
    assert detail_payload["payment_summary"] == {"paid": 0.0, "remaining": 20.0}
    assert detail_payload["payments"] == []
    assert detail_payload["payment_evidence"] == []
    assert [ticket["id"] for ticket in detail_payload["tickets"]] == [
        sent["tickets"][0]["id"]
    ]


def test_order_workspace_contracts_do_not_leak_or_mutate_across_tenants(
    client,
    tenant,
    auth_headers,
):
    order = create_order(client, tenant, auth_headers, key="tenant-isolation-create")
    foreign_headers = {
        **auth_headers,
        "X-Business-Id": str(tenant["other_business_id"]),
        "X-Branch-Id": str(tenant["other_branch_id"]),
        "X-Dev-User": "foreign-owner",
    }
    with SessionLocal() as db:
        original = db.get(Order, order["id"])
        original_state = (
            original.status,
            original.version,
            len(original.items),
            float(db.get(InventoryItem, tenant["inventory_id"]).quantity),
        )

    workspace = client.get(
        f"/api/v1/orders/workspace?branch_id={tenant['branch_id']}",
        headers=foreign_headers,
    )
    assert workspace.status_code == 404
    assert workspace.json()["code"] == "BRANCH_NOT_FOUND"

    detail = client.get(
        f"/api/v1/orders/{order['id']}/detail",
        headers=foreign_headers,
    )
    assert detail.status_code == 404
    assert detail.json()["code"] == "ORDER_NOT_FOUND"

    confirm = client.post(
        f"/api/v1/orders/{order['id']}/confirm-and-send",
        json={"expected_version": order["version"]},
        headers={**foreign_headers, "Idempotency-Key": "foreign-confirm"},
    )
    assert confirm.status_code == 404
    assert confirm.json()["code"] == "ORDER_NOT_FOUND"

    batch = client.post(
        f"/api/v1/orders/{order['id']}/item-batches",
        json={
            "expected_version": order["version"],
            "items": [{"product_id": tenant["product_id"], "quantity": 1}],
        },
        headers={**foreign_headers, "Idempotency-Key": "foreign-batch"},
    )
    assert batch.status_code == 404
    assert batch.json()["code"] == "ORDER_NOT_FOUND"

    with SessionLocal() as db:
        unchanged = db.get(Order, order["id"])
        assert (
            unchanged.status,
            unchanged.version,
            len(unchanged.items),
            float(db.get(InventoryItem, tenant["inventory_id"]).quantity),
        ) == original_state
        assert db.query(KitchenTicket).filter_by(order_id=order["id"]).count() == 0


def test_idempotency_keys_are_isolated_per_business(
    client,
    tenant,
    auth_headers,
):
    shared_key = "same-whatsapp-message-id"
    first = create_order(client, tenant, auth_headers, key=shared_key)
    foreign_headers = {
        **auth_headers,
        "X-Business-Id": str(tenant["other_business_id"]),
        "X-Branch-Id": str(tenant["other_branch_id"]),
        "X-Dev-User": "foreign-owner",
        "Idempotency-Key": shared_key,
    }
    foreign = client.post(
        "/api/v1/orders",
        json={
            "branch_id": tenant["other_branch_id"],
            "channel": "counter",
            "source": "pos",
            "items": [{"name": "Producto externo", "quantity": 1, "unit_price": 8}],
        },
        headers=foreign_headers,
    )
    assert foreign.status_code == 201, foreign.text
    assert foreign.json()["business_id"] == tenant["other_business_id"]
    assert foreign.json()["id"] != first["id"]

    replay = client.post(
        "/api/v1/orders",
        json={
            "branch_id": tenant["other_branch_id"],
            "channel": "counter",
            "source": "pos",
            "items": [{"name": "Producto externo", "quantity": 1, "unit_price": 8}],
        },
        headers=foreign_headers,
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == foreign.json()["id"]


def test_confirm_and_send_is_blocked_while_yape_evidence_is_under_review(
    client,
    tenant,
    auth_headers,
):
    order = create_order(client, tenant, auth_headers, key="evidence-guard-create")
    with SessionLocal.begin() as db:
        db.add(
            PaymentEvidence(
                business_id=tenant["business_id"],
                order_id=order["id"],
                provider="yape",
                storage_path="private/review.webp",
                image_sha256="b" * 64,
                status="under_review",
            )
        )

    blocked = client.post(
        f"/api/v1/orders/{order['id']}/confirm-and-send",
        json={"expected_version": order["version"]},
        headers={**auth_headers, "Idempotency-Key": "evidence-guard-confirm"},
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["code"] == "PAYMENT_EVIDENCE_UNDER_REVIEW"

    edited = client.patch(
        f"/api/v1/orders/{order['id']}",
        json={"delivery_fee": 5, "expected_version": order["version"]},
        headers=auth_headers,
    )
    assert edited.status_code == 409, edited.text
    assert edited.json()["code"] == "PAYMENT_EVIDENCE_UNDER_REVIEW"

    added = client.post(
        f"/api/v1/orders/{order['id']}/items",
        json={
            "item": {"product_id": tenant["product_id"], "quantity": 1},
            "expected_version": order["version"],
        },
        headers=auth_headers,
    )
    assert added.status_code == 409, added.text
    assert added.json()["code"] == "PAYMENT_EVIDENCE_UNDER_REVIEW"

    removed = client.delete(
        f"/api/v1/orders/{order['id']}/items/{order['items'][0]['id']}",
        params={"expected_version": order["version"]},
        headers=auth_headers,
    )
    assert removed.status_code == 409, removed.text
    assert removed.json()["code"] == "PAYMENT_EVIDENCE_UNDER_REVIEW"

    confirmed = client.post(
        f"/api/v1/orders/{order['id']}/confirm",
        headers={**auth_headers, "Idempotency-Key": "evidence-guard-confirm-only"},
    )
    assert confirmed.status_code == 409, confirmed.text
    assert confirmed.json()["code"] == "PAYMENT_EVIDENCE_UNDER_REVIEW"

    cancelled = client.post(
        f"/api/v1/orders/{order['id']}/transition",
        json={"status": "cancelled", "expected_version": order["version"]},
        headers=auth_headers,
    )
    assert cancelled.status_code == 409, cancelled.text
    assert cancelled.json()["code"] == "PAYMENT_EVIDENCE_UNDER_REVIEW"

    payment = client.post(
        f"/api/v1/orders/{order['id']}/payments",
        json={"method": "cash", "amount": 20, "expected_version": order["version"]},
        headers={**auth_headers, "Idempotency-Key": "evidence-guard-payment"},
    )
    assert payment.status_code == 409, payment.text
    assert payment.json()["code"] == "PAYMENT_EVIDENCE_UNDER_REVIEW"
    with SessionLocal() as db:
        persisted = db.get(Order, order["id"])
        assert persisted.status == "draft"
        assert float(persisted.delivery_fee) == 0
        assert float(db.get(InventoryItem, tenant["inventory_id"]).quantity) == 10.0
        assert db.query(KitchenTicket).filter_by(order_id=order["id"]).count() == 0


def test_rejecting_payment_evidence_releases_the_order_table(
    client,
    tenant,
    auth_headers,
):
    created = client.post(
        "/api/v1/orders",
        json={
            "branch_id": tenant["branch_id"],
            "channel": "dine_in",
            "table_id": tenant["table_id"],
            "items": [{"product_id": tenant["product_id"], "quantity": 1}],
        },
        headers={**auth_headers, "Idempotency-Key": "reject-evidence-table"},
    )
    assert created.status_code == 201, created.text
    with SessionLocal.begin() as db:
        order = db.get(Order, created.json()["id"])
        order.payment_status = "evidence_received"
        evidence = PaymentEvidence(
            business_id=tenant["business_id"],
            order_id=order.id,
            provider="yape",
            storage_path="private/reject-table.webp",
            image_sha256="d" * 64,
            status="under_review",
        )
        db.add(evidence)
        db.flush()
        evidence_id = evidence.id

    rejected = client.post(
        f"/api/v1/payment-evidence/{evidence_id}/review",
        json={"approve": False, "note": "No corresponde al pedido"},
        headers=auth_headers,
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["evidence"]["status"] == "rejected"
    assert rejected.json()["order"]["status"] == "cancelled"
    with SessionLocal() as db:
        assert db.get(RestaurantTable, tenant["table_id"]).status == "available"


def test_combo_confirmation_consumes_component_recipe_stock(
    client,
    tenant,
    auth_headers,
):
    combo = client.post(
        "/api/v1/catalog/products",
        json={
            "branch_id": tenant["branch_id"],
            "sku": "COMBO-STOCK",
            "name": "Combo familiar",
            "price": 30,
            "product_type": "combo",
        },
        headers=auth_headers,
    )
    assert combo.status_code == 201, combo.text
    configured = client.put(
        f"/api/v1/catalog/products/{combo.json()['id']}/combo",
        json={"components": [{"product_id": tenant["product_id"], "quantity": 2}]},
        headers=auth_headers,
    )
    assert configured.status_code == 200, configured.text
    created = client.post(
        "/api/v1/orders",
        json={
            "branch_id": tenant["branch_id"],
            "channel": "counter",
            "source": "pos",
            "items": [{"product_id": combo.json()["id"], "quantity": 2}],
        },
        headers={**auth_headers, "Idempotency-Key": "combo-stock-create"},
    )
    assert created.status_code == 201, created.text
    sent = confirm_and_send(
        client,
        created.json(),
        auth_headers,
        key="combo-stock-confirm",
    )
    assert sent["order"]["status"] == "sent_to_kitchen"
    with SessionLocal() as db:
        assert float(db.get(InventoryItem, tenant["inventory_id"]).quantity) == 8.0


def test_confirm_and_send_is_atomic_idempotent_and_requires_a_key(
    client,
    tenant,
    auth_headers,
):
    order = create_order(client, tenant, auth_headers, quantity=2, key="atomic-create")
    missing_key = client.post(
        f"/api/v1/orders/{order['id']}/confirm-and-send",
        json={"expected_version": order["version"]},
        headers=auth_headers,
    )
    assert missing_key.status_code == 422

    sent = confirm_and_send(client, order, auth_headers, key="atomic-confirm")
    assert sent["order"]["status"] == "sent_to_kitchen"
    assert len(sent["tickets"]) == 1
    assert sent["tickets"][0]["sequence"] == 1
    assert len(sent["tickets"][0]["items"]) == 1
    replay = client.post(
        f"/api/v1/orders/{order['id']}/confirm-and-send",
        json={"expected_version": order["version"]},
        headers={**auth_headers, "Idempotency-Key": "atomic-confirm"},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["tickets"] == sent["tickets"]
    with SessionLocal() as db:
        assert db.query(KitchenTicket).filter_by(order_id=order["id"]).count() == 1
        assert float(db.get(InventoryItem, tenant["inventory_id"]).quantity) == 9.0


def test_item_batch_creates_a_second_ticket_without_rededucting_old_items(
    client,
    tenant,
    auth_headers,
):
    order = create_order(client, tenant, auth_headers, key="batch-create")
    sent = confirm_and_send(client, order, auth_headers, key="batch-confirm")
    first_ticket = sent["tickets"][0]
    first_item_id = first_ticket["items"][0]["item_id"]

    batch = client.post(
        f"/api/v1/orders/{order['id']}/item-batches",
        json={
            "expected_version": sent["order"]["version"],
            "items": [{"product_id": tenant["product_id"], "quantity": 1}],
        },
        headers={**auth_headers, "Idempotency-Key": "batch-2"},
    )
    assert batch.status_code == 201, batch.text
    payload = batch.json()
    assert payload["order"]["total"] == 40.0
    assert payload["tickets"][0]["station"] == first_ticket["station"]
    assert payload["tickets"][0]["sequence"] == 2
    assert [item["item_id"] for item in payload["tickets"][0]["items"]] == payload[
        "appended_item_ids"
    ]
    assert first_item_id not in payload["appended_item_ids"]

    replay = client.post(
        f"/api/v1/orders/{order['id']}/item-batches",
        json={
            "expected_version": sent["order"]["version"],
            "items": [{"product_id": tenant["product_id"], "quantity": 1}],
        },
        headers={**auth_headers, "Idempotency-Key": "batch-2"},
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["appended_item_ids"] == payload["appended_item_ids"]

    resend = client.post(
        f"/api/v1/orders/{order['id']}/send-to-kitchen",
        headers={**auth_headers, "Idempotency-Key": "resend-after-batch"},
    )
    assert resend.status_code == 200, resend.text
    with SessionLocal() as db:
        tickets = list(
            db.query(KitchenTicket)
            .filter_by(order_id=order["id"], station="kitchen")
            .order_by(KitchenTicket.sequence)
        )
        assert [ticket.sequence for ticket in tickets] == [1, 2]
        assert tickets[0].items_snapshot == first_ticket["items"]
        assert float(db.get(InventoryItem, tenant["inventory_id"]).quantity) == 9.0


def test_confirmed_batch_reserves_only_new_lines_and_emits_initial_ticket(
    client,
    tenant,
    auth_headers,
):
    order = create_order(client, tenant, auth_headers, key="confirmed-batch-create")
    confirmed = client.post(
        f"/api/v1/orders/{order['id']}/confirm",
        headers={**auth_headers, "Idempotency-Key": "confirmed-only"},
    )
    assert confirmed.status_code == 200, confirmed.text
    batch = client.post(
        f"/api/v1/orders/{order['id']}/item-batches",
        json={
            "expected_version": confirmed.json()["version"],
            "items": [{"product_id": tenant["product_id"], "quantity": 2}],
        },
        headers={**auth_headers, "Idempotency-Key": "confirmed-batch"},
    )
    assert batch.status_code == 201, batch.text
    assert batch.json()["order"]["status"] == "sent_to_kitchen"
    assert len(batch.json()["tickets"]) == 1
    assert len(batch.json()["tickets"][0]["items"]) == 2
    with SessionLocal() as db:
        assert float(db.get(InventoryItem, tenant["inventory_id"]).quantity) == 8.5


def test_draft_batch_recalculates_promotions_without_stock_or_tickets(
    client,
    tenant,
    auth_headers,
):
    promotion = client.post(
        "/api/v1/catalog/promotions",
        json={
            "branch_id": tenant["branch_id"],
            "name": "Diez por ciento",
            "promotion_type": "product_discount",
            "discount_type": "percentage",
            "discount_value": 10,
            "target_scope": "products",
            "target_ids": [tenant["product_id"]],
            "weekdays": [],
            "service_channels": ["pos_counter"],
            "active": True,
        },
        headers=auth_headers,
    )
    assert promotion.status_code == 201, promotion.text
    order = create_order(client, tenant, auth_headers, key="draft-promotion-create")
    assert order["promotion_discount"] == 2.0
    batch = client.post(
        f"/api/v1/orders/{order['id']}/item-batches",
        json={
            "expected_version": order["version"],
            "items": [{"product_id": tenant["product_id"], "quantity": 1}],
        },
        headers={**auth_headers, "Idempotency-Key": "draft-promotion-batch"},
    )
    assert batch.status_code == 201, batch.text
    assert batch.json()["order"]["subtotal"] == 40.0
    assert batch.json()["order"]["promotion_discount"] == 4.0
    assert batch.json()["order"]["total"] == 36.0
    assert batch.json()["tickets"] == []
    with SessionLocal() as db:
        assert float(db.get(InventoryItem, tenant["inventory_id"]).quantity) == 10.0
        assert db.query(KitchenTicket).filter_by(order_id=order["id"]).count() == 0


def test_payments_lock_products_and_cancellation_reverses_stock_once(
    client,
    tenant,
    auth_headers,
):
    order = create_order(client, tenant, auth_headers, key="payment-batch-create")
    sent = confirm_and_send(client, order, auth_headers, key="payment-batch-confirm")
    opened = client.post(
        "/api/v1/cash/sessions/open",
        json={"register_id": tenant["register_id"], "opening_amount": 100},
        headers=auth_headers,
    )
    assert opened.status_code == 201, opened.text
    paid = client.post(
        f"/api/v1/orders/{order['id']}/payments",
        json={
            "method": "cash",
            "amount": 20,
            "cash_session_id": opened.json()["id"],
        },
        headers={**auth_headers, "Idempotency-Key": "payment-batch-paid"},
    )
    assert paid.status_code == 201, paid.text
    batch = client.post(
        f"/api/v1/orders/{order['id']}/item-batches",
        json={
            "expected_version": paid.json()["order"]["version"],
            "items": [{"product_id": tenant["product_id"], "quantity": 1}],
        },
        headers={**auth_headers, "Idempotency-Key": "payment-batch-add"},
    )
    assert batch.status_code == 409, batch.text
    assert batch.json()["code"] == "ORDER_HAS_PAYMENTS"

    cancelled = client.post(
        f"/api/v1/orders/{order['id']}/transition",
        json={
            "status": "cancelled",
            "expected_version": paid.json()["order"]["version"],
        },
        headers=auth_headers,
    )
    assert cancelled.status_code == 200, cancelled.text
    repeated = client.post(
        f"/api/v1/orders/{order['id']}/transition",
        json={"status": "cancelled"},
        headers=auth_headers,
    )
    assert repeated.status_code == 200, repeated.text
    with SessionLocal() as db:
        assert float(db.get(InventoryItem, tenant["inventory_id"]).quantity) == 10.0
        sale_movements = db.query(StockMovement).filter_by(
            reference_type="order",
            reference_id=str(order["id"]),
            movement_type="sale_consumption",
        ).all()
        reversal_movements = db.query(StockMovement).filter_by(
            reference_type="order_reversal",
            reference_id=str(order["id"]),
            movement_type="cancellation_reversal",
        ).all()
        assert sum((abs(item.quantity_delta) for item in sale_movements), Decimal("0")) == Decimal("0.5")
        assert sum((item.quantity_delta for item in reversal_movements), Decimal("0")) == Decimal("0.5")


def test_ready_is_blocked_until_every_ticket_is_ready(
    client,
    tenant,
    auth_headers,
):
    order = create_order(client, tenant, auth_headers, key="ready-create")
    sent = confirm_and_send(client, order, auth_headers, key="ready-confirm")
    preparing = client.post(
        f"/api/v1/orders/{order['id']}/transition",
        json={"status": "preparing", "expected_version": sent["order"]["version"]},
        headers=auth_headers,
    )
    assert preparing.status_code == 200, preparing.text
    blocked = client.post(
        f"/api/v1/orders/{order['id']}/transition",
        json={"status": "ready", "expected_version": preparing.json()["version"]},
        headers=auth_headers,
    )
    assert blocked.status_code == 409
    assert "kitchen tickets" in blocked.json()["detail"]
    assert blocked.json()["code"] == "KITCHEN_TICKETS_PENDING"

    ticket_id = sent["tickets"][0]["id"]
    ticket_preparing = client.post(
        f"/api/v1/kitchen/tickets/{ticket_id}/transition",
        json={"status": "preparing", "expected_status": "queued"},
        headers=auth_headers,
    )
    assert ticket_preparing.status_code == 200, ticket_preparing.text
    ticket_ready = client.post(
        f"/api/v1/kitchen/tickets/{ticket_id}/transition",
        json={"status": "ready", "expected_status": "preparing"},
        headers=auth_headers,
    )
    assert ticket_ready.status_code == 200, ticket_ready.text
    detail = client.get(f"/api/v1/orders/{order['id']}/detail", headers=auth_headers)
    assert detail.json()["order"]["status"] == "ready"

    appended = client.post(
        f"/api/v1/orders/{order['id']}/item-batches",
        json={
            "expected_version": detail.json()["order"]["version"],
            "items": [{"product_id": tenant["product_id"], "quantity": 1}],
        },
        headers={**auth_headers, "Idempotency-Key": "ready-items-locked"},
    )
    assert appended.status_code == 201, appended.text
    assert appended.json()["order"]["status"] == "sent_to_kitchen"
    assert appended.json()["tickets"][0]["sequence"] == 2


def test_cancelling_order_cancels_queued_and_preparing_tickets(
    client,
    tenant,
    auth_headers,
):
    created = client.post(
        "/api/v1/orders",
        json={
            "branch_id": tenant["branch_id"],
            "channel": "dine_in",
            "table_id": tenant["table_id"],
            "items": [{"product_id": tenant["product_id"], "quantity": 1}],
        },
        headers={**auth_headers, "Idempotency-Key": "cancel-tickets-create"},
    )
    assert created.status_code == 201, created.text
    sent = confirm_and_send(
        client,
        created.json(),
        auth_headers,
        key="cancel-tickets-confirm",
    )
    batch = client.post(
        f"/api/v1/orders/{created.json()['id']}/item-batches",
        json={
            "expected_version": sent["order"]["version"],
            "items": [{"product_id": tenant["product_id"], "quantity": 1}],
        },
        headers={**auth_headers, "Idempotency-Key": "cancel-tickets-batch"},
    )
    assert batch.status_code == 201, batch.text
    first_ticket_id = sent["tickets"][0]["id"]
    second_ticket_id = batch.json()["tickets"][0]["id"]
    preparing = client.post(
        f"/api/v1/kitchen/tickets/{first_ticket_id}/transition",
        json={"status": "preparing", "expected_status": "queued"},
        headers=auth_headers,
    )
    assert preparing.status_code == 200, preparing.text
    detail = client.get(
        f"/api/v1/orders/{created.json()['id']}/detail",
        headers=auth_headers,
    ).json()

    cancelled = client.post(
        f"/api/v1/orders/{created.json()['id']}/transition",
        json={
            "status": "cancelled",
            "expected_version": detail["order"]["version"],
        },
        headers=auth_headers,
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"

    visible_tickets = client.get(
        "/api/v1/kitchen/tickets",
        params={"branch_id": tenant["branch_id"]},
        headers=auth_headers,
    )
    assert visible_tickets.status_code == 200, visible_tickets.text
    assert visible_tickets.json() == []
    with SessionLocal() as db:
        assert db.get(KitchenTicket, first_ticket_id).status == "cancelled"
        assert db.get(KitchenTicket, second_ticket_id).status == "cancelled"
        assert db.get(Order, created.json()["id"]).status == "cancelled"
        assert db.get(RestaurantTable, tenant["table_id"]).status == "available"


def test_cancelled_order_blocks_later_ticket_transition(
    client,
    tenant,
    auth_headers,
):
    order = create_order(client, tenant, auth_headers, key="blocked-ticket-create")
    sent = confirm_and_send(client, order, auth_headers, key="blocked-ticket-confirm")
    ticket_id = sent["tickets"][0]["id"]
    cancelled = client.post(
        f"/api/v1/orders/{order['id']}/transition",
        json={
            "status": "cancelled",
            "expected_version": sent["order"]["version"],
        },
        headers=auth_headers,
    )
    assert cancelled.status_code == 200, cancelled.text

    blocked = client.post(
        f"/api/v1/kitchen/tickets/{ticket_id}/transition",
        json={"status": "preparing", "expected_status": "cancelled"},
        headers=auth_headers,
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["code"] == "ORDER_CANCELLED"
    with SessionLocal() as db:
        assert db.get(Order, order["id"]).status == "cancelled"
        assert db.get(KitchenTicket, ticket_id).status == "cancelled"


@pytest.mark.parametrize("active_status", ["sent_to_kitchen", "preparing"])
def test_dine_in_order_can_transfer_table_after_send(
    client,
    tenant,
    auth_headers,
    monkeypatch,
    active_status,
):
    with SessionLocal.begin() as db:
        target = RestaurantTable(
            business_id=tenant["business_id"],
            branch_id=tenant["branch_id"],
            code=f"DEST-{active_status}",
            name=f"Destino {active_status}",
            capacity=4,
        )
        db.add(target)
        db.flush()
        target_table_id = target.id

    created = client.post(
        "/api/v1/orders",
        json={
            "branch_id": tenant["branch_id"],
            "channel": "dine_in",
            "table_id": tenant["table_id"],
            "items": [{"product_id": tenant["product_id"], "quantity": 1}],
        },
        headers={**auth_headers, "Idempotency-Key": f"transfer-create-{active_status}"},
    )
    assert created.status_code == 201, created.text
    sent = confirm_and_send(
        client,
        created.json(),
        auth_headers,
        key=f"transfer-confirm-{active_status}",
    )
    current_order = sent["order"]
    if active_status == "preparing":
        transitioned = client.post(
            f"/api/v1/orders/{created.json()['id']}/transition",
            json={
                "status": "preparing",
                "expected_version": current_order["version"],
            },
            headers=auth_headers,
        )
        assert transitioned.status_code == 200, transitioned.text
        current_order = transitioned.json()

    stale = client.patch(
        f"/api/v1/orders/{created.json()['id']}",
        json={
            "table_id": target_table_id,
            "expected_version": current_order["version"] - 1,
        },
        headers=auth_headers,
    )
    assert stale.status_code == 409, stale.text

    broadcasts = []

    async def capture_broadcast(branch_id, event, payload):
        broadcasts.append((branch_id, event, payload))

    monkeypatch.setattr(api_module.hub, "broadcast", capture_broadcast)
    moved = client.patch(
        f"/api/v1/orders/{created.json()['id']}",
        json={
            "table_id": target_table_id,
            "expected_version": current_order["version"],
        },
        headers=auth_headers,
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["status"] == active_status
    assert moved.json()["table_id"] == target_table_id
    assert moved.json()["version"] == current_order["version"] + 1
    assert [event for _, event, _ in broadcasts] == [
        "table.updated",
        "table.updated",
        "order.updated",
    ]
    with SessionLocal() as db:
        assert db.get(RestaurantTable, tenant["table_id"]).status == "available"
        assert db.get(RestaurantTable, target_table_id).status == "occupied"


def test_waiter_can_register_kitchen_ticket_print(
    client,
    tenant,
    auth_headers,
):
    order = create_order(client, tenant, auth_headers, key="waiter-print-create")
    sent = confirm_and_send(client, order, auth_headers, key="waiter-print-confirm")
    waiter_headers = {
        **auth_headers,
        "X-Dev-Role": "waiter",
        "X-Dev-User": "waiter-test",
    }
    printed = client.post(
        f"/api/v1/kitchen/tickets/{sent['tickets'][0]['id']}/print",
        headers=waiter_headers,
    )
    assert printed.status_code == 200, printed.text
    assert printed.json()["print_count"] == 1


def test_kitchen_ticket_list_includes_dine_in_order_and_table_context(
    client,
    tenant,
    auth_headers,
):
    created = client.post(
        "/api/v1/orders",
        json={
            "branch_id": tenant["branch_id"],
            "channel": "dine_in",
            "table_id": tenant["table_id"],
            "customer_name": "Mesa de Claudio",
            "items": [{"product_id": tenant["product_id"], "quantity": 1}],
        },
        headers={**auth_headers, "Idempotency-Key": "ticket-context-create"},
    )
    assert created.status_code == 201, created.text
    sent = confirm_and_send(
        client,
        created.json(),
        auth_headers,
        key="ticket-context-confirm",
    )

    response = client.get(
        "/api/v1/kitchen/tickets",
        params={"branch_id": tenant["branch_id"]},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    ticket = next(item for item in response.json() if item["id"] == sent["tickets"][0]["id"])
    assert ticket["order_number"] == created.json()["number"]
    assert ticket["channel"] == "dine_in"
    assert ticket["customer_name"] == "Mesa de Claudio"
    assert ticket["table_id"] == tenant["table_id"]
    assert ticket["table_name"] == "Mesa 1"
