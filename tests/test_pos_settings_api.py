from __future__ import annotations

import base64

from app.database import SessionLocal
from app.models import Branch, CashRegister, DiningArea, PrintJob, StaffMember


def keyed(headers: dict, key: str) -> dict:
    return {**headers, "Idempotency-Key": key}


def test_canonical_settings_contracts_are_versioned_and_idempotent(client, tenant, auth_headers):
    business = client.get("/api/v1/settings/business", headers=auth_headers)
    assert business.status_code == 200
    assert business.json()["version"] == 1

    update = client.patch(
        "/api/v1/settings/business",
        headers=keyed(auth_headers, "business-name-1"),
        json={"name": "Escalar Pizza", "expected_version": 1},
    )
    assert update.status_code == 200
    assert update.json()["name"] == "Escalar Pizza"
    assert update.json()["version"] == 2

    replay = client.patch(
        "/api/v1/settings/business",
        headers=keyed(auth_headers, "business-name-1"),
        json={"name": "A different retry payload", "expected_version": 1},
    )
    assert replay.status_code == 200
    assert replay.json() == update.json()

    stale = client.patch(
        "/api/v1/settings/business",
        headers=keyed(auth_headers, "business-name-stale"),
        json={"name": "Stale", "expected_version": 1},
    )
    assert stale.status_code == 409

    services = client.patch(
        f"/api/v1/settings/branches/{tenant['branch_id']}/services",
        headers=keyed(auth_headers, "services-1"),
        json={
            "pos_tables": True,
            "pos_counter": True,
            "pos_takeaway": True,
            "pos_delivery": False,
            "digital_tables": False,
            "digital_takeaway": True,
            "digital_delivery": False,
            "expected_version": 1,
        },
    )
    assert services.status_code == 200
    assert services.json()["branch_id"] == tenant["branch_id"]
    assert services.json()["pos_delivery"] is False
    assert services.json()["version"] == 2

    methods = client.patch(
        f"/api/v1/settings/branches/{tenant['branch_id']}/payment-methods",
        headers=keyed(auth_headers, "methods-1"),
        json={
            "payment_methods": {
                "delivery": ["cash", "yape"],
                "takeaway": ["cash", "card"],
            },
            "expected_version": 2,
        },
    )
    assert methods.status_code == 200
    assert methods.json()["payment_methods"]["delivery"] == ["cash", "yape"]
    assert methods.json()["version"] == 3


def test_branch_scope_hides_other_branches(client, tenant, auth_headers):
    with SessionLocal.begin() as db:
        extra = Branch(
            business_id=tenant["business_id"],
            slug="second",
            name="Second",
        )
        db.add(extra)
        db.flush()
        extra_id = extra.id

    branches = client.get("/api/v1/branches", headers=auth_headers)
    assert branches.status_code == 200
    assert [item["id"] for item in branches.json()] == [tenant["branch_id"]]

    forbidden = client.get(
        f"/api/v1/settings/branches/{extra_id}/profile",
        headers=auth_headers,
    )
    assert forbidden.status_code == 403

    cross_tenant = client.get(
        f"/api/v1/settings/branches/{tenant['other_branch_id']}/services",
        headers=auth_headers,
    )
    assert cross_tenant.status_code == 403


def test_service_and_payment_settings_are_enforced_when_creating_orders(
    client, tenant, auth_headers
):
    disabled = client.patch(
        f"/api/v1/settings/branches/{tenant['branch_id']}/services",
        headers=keyed(auth_headers, "disable-counter"),
        json={
            "pos_tables": True,
            "pos_counter": False,
            "pos_takeaway": True,
            "pos_delivery": True,
            "digital_tables": False,
            "digital_takeaway": True,
            "digital_delivery": True,
            "expected_version": 1,
        },
    )
    assert disabled.status_code == 200

    blocked_order = client.post(
        "/api/v1/orders",
        headers=keyed(auth_headers, "blocked-counter-order"),
        json={
            "branch_id": tenant["branch_id"],
            "channel": "counter",
            "source": "pos",
            "items": [{"product_id": tenant["product_id"], "quantity": 1}],
        },
    )
    assert blocked_order.status_code == 409
    assert blocked_order.json()["code"] == "SERVICE_DISABLED"

    enabled = client.patch(
        f"/api/v1/settings/branches/{tenant['branch_id']}/services",
        headers=keyed(auth_headers, "enable-counter"),
        json={
            "pos_tables": True,
            "pos_counter": True,
            "pos_takeaway": True,
            "pos_delivery": True,
            "digital_tables": False,
            "digital_takeaway": True,
            "digital_delivery": True,
            "expected_version": 2,
        },
    )
    assert enabled.status_code == 200

    methods = client.patch(
        f"/api/v1/settings/branches/{tenant['branch_id']}/payment-methods",
        headers=keyed(auth_headers, "cash-only"),
        json={
            "payment_methods": {"delivery": ["cash"], "takeaway": ["cash"]},
            "expected_version": 3,
        },
    )
    assert methods.status_code == 200

    blocked_payment = client.post(
        "/api/v1/orders",
        headers=keyed(auth_headers, "blocked-yape-order"),
        json={
            "branch_id": tenant["branch_id"],
            "channel": "counter",
            "source": "pos",
            "payment_method": "yape",
            "items": [{"product_id": tenant["product_id"], "quantity": 1}],
        },
    )
    assert blocked_payment.status_code == 409
    assert blocked_payment.json()["code"] == "PAYMENT_METHOD_DISABLED"


def test_delivery_bands_are_atomic_and_quote_is_deterministic(client, tenant, auth_headers):
    response = client.patch(
        f"/api/v1/settings/branches/{tenant['branch_id']}/delivery",
        headers=keyed(auth_headers, "delivery-bands-1"),
        json={
            "delivery_mode": "bands",
            "fixed_delivery_fee": 0,
            "distance_base_fee": 0,
            "distance_fee_per_km": 0,
            "distance_max_km": 10,
            "free_delivery_threshold": 100,
            "minimum_order_amount": 15,
            "bands": [
                {"minimum_km": 0, "maximum_km": 3, "fee": 5, "sort_order": 0},
                {"minimum_km": 3, "maximum_km": 8, "fee": 8, "sort_order": 1},
            ],
            "expected_version": 1,
        },
    )
    assert response.status_code == 200
    assert response.json()["delivery_mode"] == "bands"
    assert len(response.json()["bands"]) == 2
    assert response.json()["version"] == 2

    quote = client.post(
        f"/api/v1/settings/branches/{tenant['branch_id']}/delivery/quotes",
        headers=keyed(auth_headers, "delivery-quote-1"),
        json={
            "subtotal": 30,
            "destination": {"latitude": -12.1, "longitude": -77.1},
            "distance_km": 2.5,
        },
    )
    assert quote.status_code == 201
    assert quote.json()["mode"] == "bands"
    assert quote.json()["fee"] == 5.0
    assert quote.json()["configuration_version"] == 2

    overlap = client.patch(
        f"/api/v1/settings/branches/{tenant['branch_id']}/delivery",
        headers=keyed(auth_headers, "delivery-overlap"),
        json={
            "delivery_mode": "bands",
            "bands": [
                {"minimum_km": 0, "maximum_km": 4, "fee": 4},
                {"minimum_km": 3, "maximum_km": 6, "fee": 6},
            ],
            "expected_version": 2,
        },
    )
    assert overlap.status_code == 409


def test_staff_pin_and_paired_device_flow(client, tenant, auth_headers):
    member = client.post(
        "/api/v1/settings/members",
        headers=keyed(auth_headers, "member-create-1"),
        json={
            "first_name": "Ana",
            "last_name": "Caja",
            "pin": "4821",
            "roles": ["cashier"],
            "branch_ids": [tenant["branch_id"]],
        },
    )
    assert member.status_code == 201
    assert member.json()["pin_configured"] is True
    assert "pin_hash" not in member.json()

    with SessionLocal() as db:
        persisted = db.get(StaffMember, member.json()["id"])
        assert persisted.pin_hash.startswith("$argon2id$")
        assert "4821" not in persisted.pin_hash

    device = client.post(
        "/api/v1/settings/devices",
        headers=keyed(auth_headers, "device-create-1"),
        json={"branch_id": tenant["branch_id"], "name": "Caja tablet"},
    )
    assert device.status_code == 201
    assert len(device.json()["pairing_code"]) == 6

    paired = client.post(
        "/api/v1/settings/devices/pair",
        headers={"Idempotency-Key": "device-pair-1"},
        json={"pairing_code": device.json()["pairing_code"]},
    )
    assert paired.status_code == 200
    token = paired.json()["device_token"]

    verified = client.post(
        "/api/v1/settings/devices/pin/verify",
        headers={
            "X-Device-Token": token,
            "Idempotency-Key": "pin-verify-1",
        },
        json={"staff_member_id": member.json()["id"], "pin": "4821"},
    )
    assert verified.status_code == 200
    assert verified.json()["verified"] is True
    assert verified.json()["member"]["roles"] == ["cashier"]

    archived = client.request(
        "DELETE",
        f"/api/v1/settings/members/{member.json()['id']}",
        headers=keyed(auth_headers, "member-archive-1"),
        json={"expected_version": member.json()["version"]},
    )
    assert archived.status_code == 200
    assert archived.json()["active"] is False


def test_overnight_schedules_validate_overlap_and_version(client, tenant, auth_headers):
    created = client.post(
        f"/api/v1/settings/branches/{tenant['branch_id']}/schedules",
        headers=keyed(auth_headers, "schedule-create-1"),
        json={
            "name": "Cena",
            "kind": "additional",
            "shifts": [
                {"day_of_week": 5, "starts_at": "20:00:00", "ends_at": "02:00:00"}
            ],
        },
    )
    assert created.status_code == 201
    assert created.json()["shifts"][0]["ends_at"] == "02:00:00"
    assert created.json()["version"] == 1

    assigned = client.put(
        f"/api/v1/settings/branches/{tenant['branch_id']}/schedules/{created.json()['id']}/assignments",
        headers=keyed(auth_headers, "schedule-assign-1"),
        json={
            "product_ids": [tenant["product_id"]],
            "promotion_ids": [],
            "expected_version": 1,
        },
    )
    assert assigned.status_code == 200
    assert assigned.json()["product_ids"] == [tenant["product_id"]]
    assert assigned.json()["version"] == 2

    stale = client.patch(
        f"/api/v1/settings/branches/{tenant['branch_id']}/schedules/{created.json()['id']}",
        headers=keyed(auth_headers, "schedule-stale"),
        json={"name": "Stale", "expected_version": 1},
    )
    assert stale.status_code == 409

    overlap = client.post(
        f"/api/v1/settings/branches/{tenant['branch_id']}/schedules",
        headers=keyed(auth_headers, "schedule-overlap"),
        json={
            "name": "Overlapping",
            "kind": "additional",
            "shifts": [
                {"day_of_week": 1, "starts_at": "10:00:00", "ends_at": "14:00:00"},
                {"day_of_week": 1, "starts_at": "13:00:00", "ends_at": "15:00:00"},
            ],
        },
    )
    assert overlap.status_code == 409


def test_print_jobs_are_idempotent_and_device_scoped(client, tenant, auth_headers):
    device = client.post(
        "/api/v1/settings/devices",
        headers=keyed(auth_headers, "print-device-create"),
        json={"branch_id": tenant["branch_id"], "name": "Kitchen terminal"},
    ).json()
    paired = client.post(
        "/api/v1/settings/devices/pair",
        headers={"Idempotency-Key": "print-device-pair"},
        json={"pairing_code": device["pairing_code"]},
    ).json()

    printer = client.post(
        f"/api/v1/settings/branches/{tenant['branch_id']}/printers",
        headers=keyed(auth_headers, "printer-create-1"),
        json={
            "name": "Cocina",
            "system_name": "EPSON-TM-T20",
            "paired_device_id": device["id"],
            "purpose": "kitchen",
            "paper_width_mm": 80,
            "copies": 1,
        },
    )
    assert printer.status_code == 201
    assert printer.json()["version"] == 1

    first = client.post(
        f"/api/v1/settings/branches/{tenant['branch_id']}/print-jobs",
        headers=keyed(auth_headers, "print-job-1"),
        json={
            "printer_id": printer.json()["id"],
            "job_type": "kitchen_ticket",
            "payload": {"order_number": "A-100"},
        },
    )
    replay = client.post(
        f"/api/v1/settings/branches/{tenant['branch_id']}/print-jobs",
        headers=keyed(auth_headers, "print-job-1"),
        json={
            "printer_id": printer.json()["id"],
            "job_type": "kitchen_ticket",
            "payload": {"order_number": "SHOULD-NOT-DUPLICATE"},
        },
    )
    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"]
    with SessionLocal() as db:
        assert db.scalar(__import__("sqlalchemy").select(__import__("sqlalchemy").func.count(PrintJob.id))) == 1

    claim = client.post(
        "/api/v1/settings/devices/print-jobs/claim",
        headers={
            "X-Device-Token": paired["device_token"],
            "Idempotency-Key": "claim-1",
        },
    )
    assert claim.status_code == 200
    assert claim.json()["job"]["status"] == "claimed"

    completed = client.patch(
        f"/api/v1/settings/devices/print-jobs/{first.json()['id']}",
        headers={
            "X-Device-Token": paired["device_token"],
            "Idempotency-Key": "complete-1",
        },
        json={"status": "printed"},
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "printed"


def test_qz_endpoints_fail_closed_without_secrets(client, auth_headers):
    certificate = client.get(
        "/api/v1/settings/printing/qz/certificate",
        headers=auth_headers,
    )
    signature = client.post(
        "/api/v1/settings/printing/qz/sign",
        headers=auth_headers,
        json={"payload": "request-to-sign"},
    )
    assert certificate.status_code == 503
    assert signature.status_code == 503
    assert "begin private key" not in signature.text.lower()


def test_branch_media_is_versioned_and_served_without_exposing_storage(client, tenant, auth_headers):
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    uploaded = client.post(
        f"/api/v1/settings/branches/{tenant['branch_id']}/profile",
        headers=keyed(auth_headers, "branch-logo-1"),
        data={"media_kind": "logo", "expected_version": "1"},
        files={"file": ("logo.png", png, "image/png")},
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["logo_configured"] is True
    assert uploaded.json()["logo_url"].startswith(
        f"/api/v1/settings/branches/{tenant['branch_id']}/media/logo"
    )
    assert "storage" not in uploaded.text.lower()

    public_media = client.get(uploaded.json()["logo_url"])
    assert public_media.status_code == 200
    assert public_media.headers["content-type"] == "image/png"
    assert public_media.content == png

    stale = client.post(
        f"/api/v1/settings/branches/{tenant['branch_id']}/profile",
        headers=keyed(auth_headers, "branch-logo-stale"),
        data={"media_kind": "logo", "expected_version": "1"},
        files={"file": ("logo.png", png, "image/png")},
    )
    assert stale.status_code == 409


def test_area_and_register_archives_are_safe_and_versioned(client, tenant, auth_headers):
    area = client.post(
        "/api/v1/areas",
        headers=auth_headers,
        json={
            "branch_id": tenant["branch_id"],
            "name": "Terraza",
            "sort_order": 2,
            "columns": 7,
            "rows": 5,
        },
    )
    assert area.status_code == 201
    archived_area = client.request(
        "DELETE",
        f"/api/v1/areas/{area.json()['id']}",
        headers=keyed(auth_headers, "area-archive-1"),
        json={"expected_version": area.json()["version"]},
    )
    assert archived_area.status_code == 200
    assert archived_area.json()["active"] is False

    blocked_area = client.request(
        "DELETE",
        f"/api/v1/areas/{tenant['table_id']}",
        headers=keyed(auth_headers, "area-archive-blocked"),
        json={"expected_version": 1},
    )
    assert blocked_area.status_code in {404, 409}

    second = client.post(
        "/api/v1/cash/registers",
        headers=auth_headers,
        json={"branch_id": tenant["branch_id"], "name": "Caja 2", "is_default": True},
    )
    assert second.status_code == 201
    assert second.json()["is_default"] is True
    renamed = client.patch(
        f"/api/v1/cash/registers/{tenant['register_id']}",
        headers=keyed(auth_headers, "register-update-1"),
        json={"name": "Caja auxiliar", "expected_version": 1},
    )
    assert renamed.status_code == 200
    archived = client.request(
        "DELETE",
        f"/api/v1/cash/registers/{tenant['register_id']}",
        headers=keyed(auth_headers, "register-archive-1"),
        json={"expected_version": renamed.json()["version"]},
    )
    assert archived.status_code == 200
    assert archived.json()["active"] is False

    with SessionLocal() as db:
        assert db.get(DiningArea, area.json()["id"]).archived_at is not None
        assert db.get(CashRegister, tenant["register_id"]).archived_at is not None


def test_confirm_and_send_enqueues_one_automatic_kitchen_job(client, tenant, auth_headers):
    device = client.post(
        "/api/v1/settings/devices",
        headers=keyed(auth_headers, "auto-print-device"),
        json={"branch_id": tenant["branch_id"], "name": "Cocina"},
    ).json()
    client.post(
        "/api/v1/settings/devices/pair",
        headers={"Idempotency-Key": "auto-print-pair"},
        json={"pairing_code": device["pairing_code"]},
    )
    printer = client.post(
        f"/api/v1/settings/branches/{tenant['branch_id']}/printers",
        headers=keyed(auth_headers, "auto-print-printer"),
        json={
            "name": "Cocina",
            "system_name": "Kitchen-80",
            "paired_device_id": device["id"],
            "purpose": "kitchen",
            "paper_width_mm": 80,
            "copies": 1,
        },
    )
    assert printer.status_code == 201
    printing = client.patch(
        f"/api/v1/settings/branches/{tenant['branch_id']}/printing",
        headers=keyed(auth_headers, "auto-print-settings"),
        json={
            "advanced_printing": True,
            "printer_config": {"auto_print_kitchen": True},
            "customer_ticket_template": {},
            "kitchen_ticket_template": {},
            "expected_version": 1,
        },
    )
    assert printing.status_code == 200
    order = client.post(
        "/api/v1/orders",
        headers=keyed(auth_headers, "auto-print-order"),
        json={
            "branch_id": tenant["branch_id"],
            "channel": "counter",
            "source": "pos",
            "items": [{"product_id": tenant["product_id"], "quantity": 1}],
        },
    )
    assert order.status_code == 201
    sent = client.post(
        f"/api/v1/orders/{order.json()['id']}/confirm-and-send",
        headers=keyed(auth_headers, "auto-print-send"),
        json={"expected_version": order.json()["version"]},
    )
    assert sent.status_code == 200
    with SessionLocal() as db:
        jobs = list(db.query(PrintJob).filter(PrintJob.order_id == order.json()["id"]).all())
        assert len(jobs) == 1
        assert jobs[0].job_type == "kitchen_ticket"
        assert jobs[0].paired_device_id == device["id"]
