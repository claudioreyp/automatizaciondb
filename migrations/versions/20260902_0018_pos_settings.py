"""Add versioned restaurant settings and operational configuration.

Revision ID: 20260902_0018
Revises: 20260831_0017
"""

from __future__ import annotations

import json
from datetime import datetime, time, timezone

import sqlalchemy as sa
from alembic import op


revision = "20260902_0018"
down_revision = "20260831_0017"
branch_labels = None
depends_on = None


NEW_TABLES = (
    "branch_settings",
    "delivery_bands",
    "delivery_quotes",
    "staff_members",
    "staff_member_roles",
    "staff_member_branches",
    "paired_devices",
    "service_schedules",
    "schedule_shifts",
    "schedule_assignments",
    "printer_devices",
    "print_jobs",
)


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _columns(table_name: str) -> set[str]:
    if not _has_table(table_name):
        return set()
    return {column["name"] for column in _inspector().get_columns(table_name)}


def _indexes(table_name: str) -> set[str]:
    if not _has_table(table_name):
        return set()
    return {
        index["name"]
        for index in _inspector().get_indexes(table_name)
        if index.get("name")
    }


def _foreign_keys(table_name: str) -> list[dict]:
    return _inspector().get_foreign_keys(table_name) if _has_table(table_name) else []


def _add_column(table_name: str, column: sa.Column) -> None:
    if column.name not in _columns(table_name):
        op.add_column(table_name, column)


def _ensure_index(
    table_name: str,
    index_name: str,
    columns: list[str],
    *,
    unique: bool = False,
    postgresql_where=None,
    sqlite_where=None,
) -> None:
    if index_name not in _indexes(table_name):
        op.create_index(
            index_name,
            table_name,
            columns,
            unique=unique,
            postgresql_where=postgresql_where,
            sqlite_where=sqlite_where,
        )


def _ensure_fk(
    table_name: str,
    constraint_name: str,
    local_column: str,
    remote_table: str,
    remote_column: str = "id",
    *,
    ondelete: str = "SET NULL",
) -> None:
    if any(local_column in fk.get("constrained_columns", []) for fk in _foreign_keys(table_name)):
        return
    with op.batch_alter_table(table_name) as batch_op:
        batch_op.create_foreign_key(
            constraint_name,
            remote_table,
            [local_column],
            [remote_column],
            ondelete=ondelete,
        )


def _create_tables() -> None:
    if not _has_table("branch_settings"):
        op.create_table(
            "branch_settings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("business_id", sa.Integer(), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
            sa.Column("branch_id", sa.Integer(), sa.ForeignKey("branches.id", ondelete="CASCADE"), nullable=False),
            sa.Column("pos_tables", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("pos_counter", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("pos_takeaway", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("pos_delivery", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("digital_tables", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("digital_takeaway", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("digital_delivery", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("delivery_mode", sa.String(30), nullable=False, server_default="fixed"),
            sa.Column("fixed_delivery_fee", sa.Numeric(12, 2), nullable=False, server_default="0"),
            sa.Column("distance_base_fee", sa.Numeric(12, 2), nullable=False, server_default="0"),
            sa.Column("distance_fee_per_km", sa.Numeric(12, 2), nullable=False, server_default="0"),
            sa.Column("distance_max_km", sa.Numeric(8, 2)),
            sa.Column("free_delivery_threshold", sa.Numeric(12, 2)),
            sa.Column("minimum_order_amount", sa.Numeric(12, 2)),
            sa.Column("payment_methods", sa.JSON(), nullable=False),
            sa.Column("delivery_min_minutes", sa.Integer(), nullable=False, server_default="25"),
            sa.Column("delivery_max_minutes", sa.Integer(), nullable=False, server_default="45"),
            sa.Column("pickup_minutes", sa.Integer(), nullable=False, server_default="15"),
            sa.Column("advanced_printing", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("printer_config", sa.JSON(), nullable=False),
            sa.Column("customer_ticket_template", sa.JSON(), nullable=False),
            sa.Column("kitchen_ticket_template", sa.JSON(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("branch_id", name="uq_branch_settings_branch"),
        )

    if not _has_table("delivery_bands"):
        op.create_table(
            "delivery_bands",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("business_id", sa.Integer(), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
            sa.Column("branch_id", sa.Integer(), sa.ForeignKey("branches.id", ondelete="CASCADE"), nullable=False),
            sa.Column("minimum_km", sa.Numeric(8, 2), nullable=False),
            sa.Column("maximum_km", sa.Numeric(8, 2), nullable=False),
            sa.Column("fee", sa.Numeric(12, 2), nullable=False),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("archived_at", sa.DateTime(timezone=True)),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("minimum_km >= 0", name="ck_delivery_band_minimum_nonnegative"),
            sa.CheckConstraint("maximum_km > minimum_km", name="ck_delivery_band_valid_range"),
            sa.CheckConstraint("fee >= 0", name="ck_delivery_band_fee_nonnegative"),
            sa.UniqueConstraint("branch_id", "sort_order", name="uq_delivery_band_branch_order"),
        )

    if not _has_table("delivery_quotes"):
        op.create_table(
            "delivery_quotes",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("business_id", sa.Integer(), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
            sa.Column("branch_id", sa.Integer(), sa.ForeignKey("branches.id", ondelete="CASCADE"), nullable=False),
            sa.Column("mode", sa.String(30), nullable=False),
            sa.Column("subtotal", sa.Numeric(12, 2), nullable=False),
            sa.Column("distance_km", sa.Numeric(8, 2)),
            sa.Column("fee", sa.Numeric(12, 2)),
            sa.Column("minimum_order_amount", sa.Numeric(12, 2)),
            sa.Column("configuration_version", sa.Integer(), nullable=False),
            sa.Column("input_snapshot", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        )

    if not _has_table("staff_members"):
        op.create_table(
            "staff_members",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("business_id", sa.Integer(), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
            sa.Column("auth_user_id", sa.String(120)),
            sa.Column("email", sa.String(240)),
            sa.Column("first_name", sa.String(120), nullable=False),
            sa.Column("last_name", sa.String(120), nullable=False, server_default=""),
            sa.Column("pin_hash", sa.Text()),
            sa.Column("email_access", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("failed_pin_attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("pin_locked_until", sa.DateTime(timezone=True)),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("archived_at", sa.DateTime(timezone=True)),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("business_id", "auth_user_id", name="uq_staff_business_auth_user"),
            sa.UniqueConstraint("business_id", "email", name="uq_staff_business_email"),
        )

    if not _has_table("staff_member_roles"):
        op.create_table(
            "staff_member_roles",
            sa.Column("staff_member_id", sa.Integer(), sa.ForeignKey("staff_members.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("role", sa.String(40), primary_key=True),
            sa.Column("business_id", sa.Integer(), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        )

    if not _has_table("staff_member_branches"):
        op.create_table(
            "staff_member_branches",
            sa.Column("staff_member_id", sa.Integer(), sa.ForeignKey("staff_members.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("branch_id", sa.Integer(), sa.ForeignKey("branches.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("business_id", sa.Integer(), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        )

    if not _has_table("paired_devices"):
        op.create_table(
            "paired_devices",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("business_id", sa.Integer(), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
            sa.Column("branch_id", sa.Integer(), sa.ForeignKey("branches.id", ondelete="CASCADE"), nullable=False),
            sa.Column("name", sa.String(180), nullable=False),
            sa.Column("token_hash", sa.String(64)),
            sa.Column("pairing_code_hash", sa.String(64)),
            sa.Column("pairing_expires_at", sa.DateTime(timezone=True)),
            sa.Column("paired_at", sa.DateTime(timezone=True)),
            sa.Column("last_used_at", sa.DateTime(timezone=True)),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("archived_at", sa.DateTime(timezone=True)),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("token_hash", name="uq_paired_device_token_hash"),
        )

    if not _has_table("service_schedules"):
        op.create_table(
            "service_schedules",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("business_id", sa.Integer(), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
            sa.Column("branch_id", sa.Integer(), sa.ForeignKey("branches.id", ondelete="CASCADE"), nullable=False),
            sa.Column("name", sa.String(180), nullable=False),
            sa.Column("kind", sa.String(30), nullable=False, server_default="additional"),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("archived_at", sa.DateTime(timezone=True)),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("branch_id", "name", name="uq_service_schedule_branch_name"),
        )

    if not _has_table("schedule_shifts"):
        op.create_table(
            "schedule_shifts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("schedule_id", sa.Integer(), sa.ForeignKey("service_schedules.id", ondelete="CASCADE"), nullable=False),
            sa.Column("day_of_week", sa.Integer(), nullable=False),
            sa.Column("starts_at", sa.Time(), nullable=False),
            sa.Column("ends_at", sa.Time(), nullable=False),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("day_of_week >= 0 AND day_of_week <= 6", name="ck_schedule_shift_weekday"),
            sa.UniqueConstraint("schedule_id", "day_of_week", "starts_at", "ends_at", name="uq_schedule_shift_window"),
        )

    if not _has_table("schedule_assignments"):
        op.create_table(
            "schedule_assignments",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("business_id", sa.Integer(), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
            sa.Column("branch_id", sa.Integer(), sa.ForeignKey("branches.id", ondelete="CASCADE"), nullable=False),
            sa.Column("schedule_id", sa.Integer(), sa.ForeignKey("service_schedules.id", ondelete="CASCADE"), nullable=False),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="CASCADE")),
            sa.Column("promotion_id", sa.Integer(), sa.ForeignKey("promotions.id", ondelete="CASCADE")),
            sa.CheckConstraint(
                "(product_id IS NOT NULL AND promotion_id IS NULL) OR (product_id IS NULL AND promotion_id IS NOT NULL)",
                name="ck_schedule_assignment_single_target",
            ),
            sa.UniqueConstraint("schedule_id", "product_id", name="uq_schedule_assignment_product"),
            sa.UniqueConstraint("schedule_id", "promotion_id", name="uq_schedule_assignment_promotion"),
        )

    if not _has_table("printer_devices"):
        op.create_table(
            "printer_devices",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("business_id", sa.Integer(), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
            sa.Column("branch_id", sa.Integer(), sa.ForeignKey("branches.id", ondelete="CASCADE"), nullable=False),
            sa.Column("paired_device_id", sa.Integer(), sa.ForeignKey("paired_devices.id", ondelete="SET NULL")),
            sa.Column("name", sa.String(180), nullable=False),
            sa.Column("system_name", sa.String(255), nullable=False),
            sa.Column("purpose", sa.String(40), nullable=False, server_default="kitchen"),
            sa.Column("paper_width_mm", sa.Integer(), nullable=False, server_default="80"),
            sa.Column("copies", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("archived_at", sa.DateTime(timezone=True)),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("branch_id", "system_name", name="uq_printer_branch_system_name"),
        )

    if not _has_table("print_jobs"):
        op.create_table(
            "print_jobs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("business_id", sa.Integer(), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
            sa.Column("branch_id", sa.Integer(), sa.ForeignKey("branches.id", ondelete="CASCADE"), nullable=False),
            sa.Column("paired_device_id", sa.Integer(), sa.ForeignKey("paired_devices.id", ondelete="SET NULL")),
            sa.Column("printer_id", sa.Integer(), sa.ForeignKey("printer_devices.id", ondelete="SET NULL")),
            sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id", ondelete="SET NULL")),
            sa.Column("kitchen_ticket_id", sa.Integer(), sa.ForeignKey("kitchen_tickets.id", ondelete="SET NULL")),
            sa.Column("job_type", sa.String(40), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
            sa.Column("idempotency_key", sa.String(240), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("claimed_at", sa.DateTime(timezone=True)),
            sa.Column("completed_at", sa.DateTime(timezone=True)),
            sa.Column("failed_at", sa.DateTime(timezone=True)),
            sa.Column("error_message", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("business_id", "idempotency_key", name="uq_print_job_business_key"),
        )


def _create_indexes() -> None:
    index_specs = {
        "branch_settings": [("ix_branch_settings_business_id", ["business_id"]), ("ix_branch_settings_branch_id", ["branch_id"])],
        "delivery_bands": [("ix_delivery_bands_business_id", ["business_id"]), ("ix_delivery_bands_branch_id", ["branch_id"]), ("ix_delivery_bands_archived_at", ["archived_at"])],
        "delivery_quotes": [("ix_delivery_quotes_business_id", ["business_id"]), ("ix_delivery_quotes_branch_id", ["branch_id"]), ("ix_delivery_quotes_expires_at", ["expires_at"])],
        "staff_members": [("ix_staff_members_business_id", ["business_id"]), ("ix_staff_members_auth_user_id", ["auth_user_id"]), ("ix_staff_members_email", ["email"]), ("ix_staff_members_active", ["active"])],
        "staff_member_roles": [("ix_staff_member_roles_business_id", ["business_id"])],
        "staff_member_branches": [("ix_staff_member_branches_business_id", ["business_id"]), ("ix_staff_member_branches_branch_id", ["branch_id"])],
        "paired_devices": [("ix_paired_devices_business_id", ["business_id"]), ("ix_paired_devices_branch_id", ["branch_id"]), ("ix_paired_devices_pairing_code_hash", ["pairing_code_hash"]), ("ix_paired_devices_active", ["active"])],
        "service_schedules": [("ix_service_schedules_business_id", ["business_id"]), ("ix_service_schedules_branch_id", ["branch_id"]), ("ix_service_schedules_kind", ["kind"])],
        "schedule_shifts": [("ix_schedule_shifts_schedule_id", ["schedule_id"]), ("ix_schedule_shifts_day_of_week", ["day_of_week"])],
        "schedule_assignments": [("ix_schedule_assignments_business_id", ["business_id"]), ("ix_schedule_assignments_branch_id", ["branch_id"]), ("ix_schedule_assignments_schedule_id", ["schedule_id"]), ("ix_schedule_assignments_product_id", ["product_id"]), ("ix_schedule_assignments_promotion_id", ["promotion_id"])],
        "printer_devices": [("ix_printer_devices_business_id", ["business_id"]), ("ix_printer_devices_branch_id", ["branch_id"]), ("ix_printer_devices_paired_device_id", ["paired_device_id"]), ("ix_printer_devices_active", ["active"])],
        "print_jobs": [("ix_print_jobs_business_id", ["business_id"]), ("ix_print_jobs_branch_id", ["branch_id"]), ("ix_print_jobs_printer_id", ["printer_id"]), ("ix_print_jobs_status", ["status"]), ("ix_print_jobs_created_at", ["created_at"]), ("ix_print_jobs_device_status_created", ["paired_device_id", "status", "created_at"])],
    }
    for table_name, specs in index_specs.items():
        for index_name, columns in specs:
            _ensure_index(table_name, index_name, columns)
    _ensure_index(
        "service_schedules",
        "uq_service_schedule_primary_branch",
        ["branch_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'primary' AND archived_at IS NULL"),
        sqlite_where=sa.text("kind = 'primary' AND archived_at IS NULL"),
    )


def _add_existing_table_columns() -> None:
    _add_column("businesses", sa.Column("country_code", sa.String(2), nullable=False, server_default="PE"))
    _add_column("businesses", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))

    branch_additions = (
        sa.Column("google_place_id", sa.String(255)),
        sa.Column("latitude", sa.Numeric(10, 7)),
        sa.Column("longitude", sa.Numeric(10, 7)),
        sa.Column("logo_storage_path", sa.Text()),
        sa.Column("cover_storage_path", sa.Text()),
        sa.Column("whatsapp_number", sa.String(40)),
        sa.Column("whatsapp_status", sa.String(30), nullable=False, server_default="unknown"),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    for column in branch_additions:
        _add_column("branches", column)
    _ensure_index("branches", "ix_branches_archived_at", ["archived_at"])

    _add_column("dining_areas", sa.Column("archived_at", sa.DateTime(timezone=True)))
    _ensure_index("dining_areas", "ix_dining_areas_archived_at", ["archived_at"])

    _add_column("cash_registers", sa.Column("archived_at", sa.DateTime(timezone=True)))
    _add_column("cash_registers", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    _ensure_index("cash_registers", "ix_cash_registers_archived_at", ["archived_at"])

    _add_column("audit_events", sa.Column("branch_id", sa.Integer()))
    _add_column("audit_events", sa.Column("actor_display_name", sa.String(180)))
    _ensure_fk("audit_events", "fk_audit_events_branch_id", "branch_id", "branches")
    _ensure_index("audit_events", "ix_audit_events_branch_id", ["branch_id"])

    _add_column("orders", sa.Column("delivery_quote_id", sa.String(36)))
    _add_column("orders", sa.Column("delivery_fee_status", sa.String(30), nullable=False, server_default="final"))
    _ensure_fk("orders", "fk_orders_delivery_quote_id", "delivery_quote_id", "delivery_quotes")
    _ensure_index("orders", "ix_orders_delivery_quote_id", ["delivery_quote_id"])


def _json_value(value, default):
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


WEEKDAYS = {
    "monday": 0,
    "lunes": 0,
    "tuesday": 1,
    "martes": 1,
    "wednesday": 2,
    "miercoles": 2,
    "miércoles": 2,
    "thursday": 3,
    "jueves": 3,
    "friday": 4,
    "viernes": 4,
    "saturday": 5,
    "sabado": 5,
    "sábado": 5,
    "sunday": 6,
    "domingo": 6,
}


def _parse_clock(value) -> time | None:
    if not isinstance(value, str):
        return None
    try:
        hour, minute = value.strip().split(":", 1)
        return time(int(hour), int(minute[:2]))
    except (TypeError, ValueError):
        return None


def _opening_shifts(payload) -> list[dict]:
    payload = _json_value(payload, {})
    if not isinstance(payload, dict):
        return []
    shifts: list[dict] = []
    for raw_day, raw_windows in payload.items():
        try:
            day = int(raw_day)
        except (TypeError, ValueError):
            day = WEEKDAYS.get(str(raw_day).lower(), -1)
        if day not in range(7):
            continue
        windows = raw_windows if isinstance(raw_windows, list) else [raw_windows]
        for index, window in enumerate(windows):
            start = end = None
            if isinstance(window, dict):
                start = _parse_clock(window.get("open") or window.get("start") or window.get("from"))
                end = _parse_clock(window.get("close") or window.get("end") or window.get("to"))
            elif isinstance(window, str) and "-" in window:
                raw_start, raw_end = window.split("-", 1)
                start, end = _parse_clock(raw_start), _parse_clock(raw_end)
            if start is not None and end is not None:
                shifts.append({"day_of_week": day, "starts_at": start, "ends_at": end, "sort_order": index})
    return shifts


def _backfill() -> None:
    connection = op.get_bind()
    now = datetime.now(timezone.utc)
    metadata = sa.MetaData()
    branches = sa.Table("branches", metadata, autoload_with=connection)
    branch_settings = sa.Table("branch_settings", metadata, autoload_with=connection)
    schedules = sa.Table("service_schedules", metadata, autoload_with=connection)
    shifts = sa.Table("schedule_shifts", metadata, autoload_with=connection)
    staff = sa.Table("staff_members", metadata, autoload_with=connection)
    staff_roles = sa.Table("staff_member_roles", metadata, autoload_with=connection)
    staff_branches = sa.Table("staff_member_branches", metadata, autoload_with=connection)
    memberships = sa.Table("memberships", metadata, autoload_with=connection)

    for branch in connection.execute(sa.select(branches)).mappings():
        existing = connection.scalar(
            sa.select(branch_settings.c.id).where(branch_settings.c.branch_id == branch["id"])
        )
        methods = _json_value(branch.get("accepted_payment_methods"), []) or ["cash", "card", "yape", "plin"]
        if existing is None:
            connection.execute(
                branch_settings.insert().values(
                    business_id=branch["business_id"],
                    branch_id=branch["id"],
                    pos_tables=True,
                    pos_counter=True,
                    pos_takeaway=bool(branch.get("takeaway_enabled", True)),
                    pos_delivery=bool(branch.get("delivery_enabled", True)),
                    digital_tables=False,
                    digital_takeaway=bool(branch.get("takeaway_enabled", True)),
                    digital_delivery=bool(branch.get("delivery_enabled", True)),
                    delivery_mode="fixed",
                    fixed_delivery_fee=branch.get("delivery_fee") or 0,
                    distance_base_fee=0,
                    distance_fee_per_km=0,
                    payment_methods={
                        "delivery": methods,
                        "takeaway": methods,
                        "counter": methods,
                    },
                    delivery_min_minutes=25,
                    delivery_max_minutes=45,
                    pickup_minutes=15,
                    advanced_printing=False,
                    printer_config={},
                    customer_ticket_template={},
                    kitchen_ticket_template={},
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
        schedule_id = connection.scalar(
            sa.select(schedules.c.id).where(
                schedules.c.branch_id == branch["id"],
                schedules.c.kind == "primary",
                schedules.c.archived_at.is_(None),
            )
        )
        if schedule_id is None:
            result = connection.execute(
                schedules.insert().values(
                    business_id=branch["business_id"],
                    branch_id=branch["id"],
                    name="Menú general",
                    kind="primary",
                    active=True,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            schedule_id = result.inserted_primary_key[0]
            parsed_shifts = _opening_shifts(branch.get("opening_hours"))
            if parsed_shifts:
                connection.execute(
                    shifts.insert(),
                    [
                        dict(
                            schedule_id=schedule_id,
                            created_at=now,
                            updated_at=now,
                            **item,
                        )
                        for item in parsed_shifts
                    ],
                )

    grouped: dict[tuple[int, str], list[dict]] = {}
    for membership in connection.execute(
        sa.select(memberships).where(
            memberships.c.business_id.is_not(None),
            memberships.c.role != "superadmin",
        )
    ).mappings():
        grouped.setdefault((membership["business_id"], membership["auth_user_id"]), []).append(dict(membership))
    for (business_id, auth_user_id), records in grouped.items():
        member_id = connection.scalar(
            sa.select(staff.c.id).where(
                staff.c.business_id == business_id,
                staff.c.auth_user_id == auth_user_id,
            )
        )
        if member_id is None:
            display_name = (records[0].get("full_name") or "Usuario").strip()
            first_name, _, last_name = display_name.partition(" ")
            result = connection.execute(
                staff.insert().values(
                    business_id=business_id,
                    auth_user_id=auth_user_id,
                    email=records[0].get("email"),
                    first_name=first_name,
                    last_name=last_name,
                    email_access=True,
                    failed_pin_attempts=0,
                    active=any(bool(item.get("active")) for item in records),
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            member_id = result.inserted_primary_key[0]
        existing_roles = set(
            connection.execute(
                sa.select(staff_roles.c.role).where(staff_roles.c.staff_member_id == member_id)
            ).scalars()
        )
        for role in {item["role"] for item in records} - existing_roles:
            connection.execute(
                staff_roles.insert().values(staff_member_id=member_id, role=role, business_id=business_id)
            )
        explicit_branch_ids = {item["branch_id"] for item in records if item.get("branch_id") is not None}
        if any(item.get("branch_id") is None for item in records):
            explicit_branch_ids.update(
                connection.execute(
                    sa.select(branches.c.id).where(
                        branches.c.business_id == business_id,
                        branches.c.active.is_(True),
                    )
                ).scalars()
            )
        existing_branch_ids = set(
            connection.execute(
                sa.select(staff_branches.c.branch_id).where(staff_branches.c.staff_member_id == member_id)
            ).scalars()
        )
        for branch_id in explicit_branch_ids - existing_branch_ids:
            connection.execute(
                staff_branches.insert().values(
                    staff_member_id=member_id,
                    branch_id=branch_id,
                    business_id=business_id,
                )
            )


def upgrade() -> None:
    _create_tables()
    _create_indexes()
    _add_existing_table_columns()
    _backfill()

    if op.get_bind().dialect.name == "postgresql":
        for table_name in NEW_TABLES:
            op.execute(f'ALTER TABLE public."{table_name}" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table_name in NEW_TABLES:
            if _has_table(table_name):
                op.execute(f'ALTER TABLE public."{table_name}" DISABLE ROW LEVEL SECURITY')

    for table_name, columns in (
        ("orders", ["delivery_fee_status", "delivery_quote_id"]),
        ("audit_events", ["actor_display_name", "branch_id"]),
        ("cash_registers", ["version", "archived_at"]),
        ("dining_areas", ["archived_at"]),
        (
            "branches",
            [
                "version",
                "archived_at",
                "whatsapp_status",
                "whatsapp_number",
                "cover_storage_path",
                "logo_storage_path",
                "longitude",
                "latitude",
                "google_place_id",
            ],
        ),
        ("businesses", ["version", "country_code"]),
    ):
        existing = _columns(table_name)
        if not existing:
            continue
        with op.batch_alter_table(table_name) as batch_op:
            for column_name in columns:
                if column_name in existing:
                    batch_op.drop_column(column_name)

    for table_name in reversed(NEW_TABLES):
        if _has_table(table_name):
            op.drop_table(table_name)
