from datetime import datetime, timedelta, timezone

from app.api import kitchen_author_names
from app.database import SessionLocal
from app.models import Branch, KitchenTicket, Membership, StaffMember, StaffMemberBranch
from test_command_workflow import create_order, send_order


def test_command_responsible_utc_snapshots_and_idempotent_actions(client, tenant, auth_headers):
    with SessionLocal.begin() as db:
        db.add(Membership(auth_user_id="owner-test", full_name="Ana Perez", role="owner",
                          business_id=tenant["business_id"], branch_id=tenant["branch_id"]))
    order = create_order(client, tenant, auth_headers)
    sent = send_order(client, order, auth_headers)
    command_id = sent["tickets"][0]["id"]
    old_instant = (datetime.now(timezone.utc) - timedelta(days=2)).replace(microsecond=0)
    with SessionLocal.begin() as db:
        ticket = db.get(KitchenTicket, command_id)
        ticket.fired_at = old_instant.replace(tzinfo=None)
        original_items = ticket.items_snapshot

    listed = client.get(f"/api/v1/kitchen/commands?branch_id={tenant['branch_id']}", headers=auth_headers)
    assert listed.status_code == 200
    command = listed.json()["items"][0]
    assert command["created_by"] == "owner-test"
    assert command["created_by_name"] == "Ana Perez"
    assert datetime.fromisoformat(command["created_at"]) == old_instant
    assert listed.json()["active_count"] == 1  # Pending commands survive a change of day.

    for action, expected, resulting in [("complete", "queued", "ready"), ("reopen", "ready", "preparing")]:
        path = f"/api/v1/kitchen/commands/{command_id}/{action}"
        headers = {**auth_headers, "Idempotency-Key": f"presentation-{action}"}
        payload = {"expected_status": expected}
        result = client.post(path, json=payload, headers=headers)
        assert result.status_code == 200, result.text
        assert result.json()["command"]["created_by_name"] == "Ana Perez"
        assert result.json()["command"]["status"] == resulting
        assert result.json()["command"]["items"] == original_items
        assert client.post(path, json=payload, headers=headers).json() == result.json()
        detail = client.get(f"/api/v1/orders/{order['id']}/detail", headers=auth_headers)
        assert detail.status_code == 200
        saved_command = detail.json()["tickets"][0]
        assert saved_command["status"] == resulting
        assert saved_command["version"] == result.json()["command"]["version"]
        assert saved_command["ready_at"] == result.json()["command"]["ready_at"]
        assert saved_command["items"] == original_items
        assert datetime.fromisoformat(saved_command["created_at"]) == old_instant
        assert detail.json()["payments"] == []
        assert detail.json()["payment_summary"] == {"paid": 0, "remaining": 20}
    with SessionLocal() as db:
        assert db.get(KitchenTicket, command_id).items_snapshot == original_items


def test_author_names_never_cross_business_or_branch_and_support_scoped_staff(tenant):
    with SessionLocal.begin() as db:
        sibling = Branch(business_id=tenant["business_id"], slug="sibling", name="Sibling")
        db.add(sibling)
        db.flush()
        db.add_all([
            Membership(auth_user_id="shared", full_name="Other business", role="owner", business_id=tenant["other_business_id"], branch_id=tenant["other_branch_id"]),
            Membership(auth_user_id="shared", full_name="Other branch", role="manager", business_id=tenant["business_id"], branch_id=sibling.id),
            Membership(auth_user_id="global-owner", full_name="Business owner", role="owner", business_id=tenant["business_id"], branch_id=None),
            Membership(auth_user_id="shared", full_name="Global admin", role="superadmin", business_id=None, branch_id=None),
        ])
        staff = StaffMember(business_id=tenant["business_id"], auth_user_id="staff-auth", first_name="Luis", last_name="Perez")
        db.add(staff)
        db.flush()
        db.add(StaffMemberBranch(staff_member_id=staff.id, business_id=tenant["business_id"], branch_id=tenant["branch_id"]))
        db.flush()
        names = kitchen_author_names(db, tenant["business_id"], tenant["branch_id"], {"shared", "global-owner", "staff-auth", "unknown"})
        assert names == {"global-owner": "Business owner", "staff-auth": "Luis Perez"}
        assert "staff-auth" not in kitchen_author_names(db, tenant["business_id"], sibling.id, {"staff-auth"})


def test_command_list_and_actions_remain_scoped(client, tenant, auth_headers):
    order = create_order(client, tenant, auth_headers)
    sent = send_order(client, order, auth_headers)
    foreign = {**auth_headers, "X-Business-Id": str(tenant["other_business_id"]), "X-Branch-Id": str(tenant["other_branch_id"])}
    own_list = client.get(f"/api/v1/kitchen/commands?branch_id={tenant['other_branch_id']}", headers=foreign)
    assert own_list.status_code == 200
    assert own_list.json()["items"] == []
    assert client.get(f"/api/v1/kitchen/commands?branch_id={tenant['branch_id']}", headers=foreign).status_code in (403, 404)
    for action in ("complete", "reopen"):
        result = client.post(f"/api/v1/kitchen/commands/{sent['tickets'][0]['id']}/{action}", json={"expected_status": "queued"}, headers={**foreign, "Idempotency-Key": action})
        assert result.status_code == 404
