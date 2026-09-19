from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import scoped_order_for_user
from app.auth import AuthContext
from app.command_revisions import effective_ticket_items
from app.database import SessionLocal
from app.models import IntegrationEvent, KitchenTicket, Order
from app.schemas import OrderItemRevisionOperation
from app.services import apply_order_item_revisions
from test_command_workflow import create_order, send_order


def preparing_order(client, tenant, headers):
    sent = send_order(client, create_order(client, tenant, headers), headers)
    ticket = sent["tickets"][0]
    response = client.post(
        f"/api/v1/kitchen/tickets/{ticket['id']}/transition",
        json={
            "status": "preparing",
            "expected_status": "queued",
            "expected_version": ticket["version"],
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    detail = client.get(
        f"/api/v1/orders/{sent['order']['id']}/detail", headers=headers
    )
    assert detail.status_code == 200, detail.text
    return detail.json()["order"], response.json()


def revision_operation(order, tenant):
    return OrderItemRevisionOperation(
        type="edit",
        item_id=order["items"][0]["id"],
        replacement={
            "product_id": tenant["product_id"],
            "quantity": 1,
            "notes": "Sin sal",
        },
    )


def test_legacy_transition_rejects_concurrent_revision_in_identity_map(
    client, tenant, auth_headers, monkeypatch
):
    order, ticket = preparing_order(client, tenant, auth_headers)
    user = AuthContext(
        "owner-test", "owner", tenant["business_id"], tenant["branch_id"]
    )
    original_scalar = Session.scalar
    interleaved = {}

    def scalar_with_revision(db, statement, *args, **kwargs):
        result = original_scalar(db, statement, *args, **kwargs)
        if (
            isinstance(result, KitchenTicket)
            and result.id == ticket["id"]
            and not interleaved
        ):
            interleaved["started"] = True
            # Commit after the first read, before the endpoint locks the order.
            with SessionLocal() as other:
                current_order = scoped_order_for_user(
                    other, user, order["id"], for_update=True
                )
                _, _, revised = apply_order_item_revisions(
                    other, user, current_order, [revision_operation(order, tenant)]
                )
                other.commit()
                interleaved["ticket_version"] = revised[0].version
                interleaved["order_version"] = current_order.version
            interleaved["cached_version"] = result.version
        return result

    monkeypatch.setattr(Session, "scalar", scalar_with_revision)
    response = client.post(
        f"/api/v1/kitchen/tickets/{ticket['id']}/transition",
        json={
            "status": "ready",
            "expected_status": "preparing",
            "expected_version": ticket["version"],
        },
        headers=auth_headers,
    )

    assert interleaved["cached_version"] == ticket["version"]
    assert interleaved["ticket_version"] == ticket["version"] + 1
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "KITCHEN_TICKET_STALE"
    with SessionLocal() as db:
        stored = db.get(KitchenTicket, ticket["id"])
        stored_order = db.get(Order, order["id"])
        assert stored.status == "preparing"
        assert stored.version == interleaved["ticket_version"]
        assert stored.ready_at is None
        assert effective_ticket_items(stored)[0]["notes"] == "Sin sal"
        assert stored_order.status == "preparing"
        assert stored_order.version == interleaved["order_version"]
        assert db.scalar(
            select(IntegrationEvent.id).where(
                IntegrationEvent.aggregate_id == str(order["id"]),
                IntegrationEvent.event_type == "order.ready",
            )
        ) is None


def test_legacy_transition_completes_revised_ticket_with_current_version(
    client, tenant, auth_headers
):
    order, ticket = preparing_order(client, tenant, auth_headers)
    revised = client.post(
        f"/api/v1/orders/{order['id']}/item-revisions",
        json={
            "expected_version": order["version"],
            "operations": [revision_operation(order, tenant).model_dump(mode="json")],
        },
        headers={**auth_headers, "Idempotency-Key": "legacy-current-revision"},
    )
    assert revised.status_code == 201, revised.text
    current = revised.json()["tickets"][0]
    assert current["version"] == ticket["version"] + 1

    response = client.post(
        f"/api/v1/kitchen/tickets/{ticket['id']}/transition",
        json={
            "status": "ready",
            "expected_status": "preparing",
            "expected_version": current["version"],
        },
        headers=auth_headers,
    )

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "ready"
    assert result["version"] == current["version"] + 1
    assert result["items"][0]["notes"] == "Sin sal"
    with SessionLocal() as db:
        stored = db.get(KitchenTicket, ticket["id"])
        assert stored.status == "ready"
        assert stored.version == result["version"]
        assert stored.ready_at is not None
        assert db.get(Order, order["id"]).status == "ready"
