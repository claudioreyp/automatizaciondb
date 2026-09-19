"""Add blind cash-cut reconciliation snapshots.

Revision ID: 20260831_0017
Revises: 20260831_0016
"""

import sqlalchemy as sa
from alembic import op


revision = "20260831_0017"
down_revision = "20260831_0016"
branch_labels = None
depends_on = None


DEFAULT_REGISTER_INDEX = "uq_cash_registers_one_default_per_branch"
OPEN_SESSION_INDEX = "uq_cash_sessions_one_open_per_register"
HISTORY_INDEX = "ix_cash_sessions_branch_status_closed"


def _columns(table_name: str) -> set[str]:
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def _indexes(table_name: str) -> set[str]:
    return {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
        if index.get("name")
    }


def _has_foreign_key(table_name: str, column_name: str) -> bool:
    return any(
        column_name in foreign_key.get("constrained_columns", [])
        for foreign_key in sa.inspect(op.get_bind()).get_foreign_keys(table_name)
    )


def _ensure_default_registers() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "INSERT INTO cash_registers "
            "(business_id, branch_id, name, active, is_default, created_at, updated_at) "
            "SELECT branches.business_id, branches.id, 'Caja Principal', TRUE, TRUE, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP "
            "FROM branches "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM cash_registers WHERE cash_registers.branch_id = branches.id"
            ")"
        )
    )
    connection.execute(sa.text("UPDATE cash_registers SET is_default = FALSE"))
    connection.execute(
        sa.text(
            "UPDATE cash_registers SET is_default = TRUE, active = TRUE "
            "WHERE id IN ("
            "SELECT COALESCE("
            "MIN(CASE WHEN LOWER(name) IN ('principal', 'caja principal') THEN id END), "
            "MIN(id)"
            ") FROM cash_registers GROUP BY branch_id"
            ")"
        )
    )


def _backfill_unambiguous_payments(connection) -> None:
    payment_ids = connection.execute(
        sa.text(
            "SELECT payments.id FROM payments "
            "WHERE payments.cash_session_id IS NULL "
            "AND payments.status = 'confirmed'"
        )
    ).scalars().all()
    for payment_id in payment_ids:
        candidate_ids = connection.execute(
            sa.text(
                "SELECT cash_sessions.id "
                "FROM payments "
                "JOIN orders ON orders.id = payments.order_id "
                "JOIN cash_sessions ON "
                "cash_sessions.business_id = orders.business_id "
                "AND cash_sessions.branch_id = orders.branch_id "
                "AND cash_sessions.opened_at <= payments.received_at "
                "AND (cash_sessions.closed_at IS NULL "
                "OR cash_sessions.closed_at >= payments.received_at) "
                "WHERE payments.id = :payment_id "
                "ORDER BY cash_sessions.id"
            ),
            {"payment_id": payment_id},
        ).scalars().all()
        if len(candidate_ids) == 1:
            connection.execute(
                sa.text(
                    "UPDATE payments SET cash_session_id = :session_id "
                    "WHERE id = :payment_id AND cash_session_id IS NULL"
                ),
                {"session_id": candidate_ids[0], "payment_id": payment_id},
            )


def upgrade() -> None:
    register_columns = _columns("cash_registers")
    if "is_default" not in register_columns:
        op.add_column(
            "cash_registers",
            sa.Column(
                "is_default",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )
    _ensure_default_registers()

    register_indexes = _indexes("cash_registers")
    if DEFAULT_REGISTER_INDEX not in register_indexes:
        op.create_index(
            DEFAULT_REGISTER_INDEX,
            "cash_registers",
            ["branch_id"],
            unique=True,
            postgresql_where=sa.text("is_default IS TRUE"),
            sqlite_where=sa.text("is_default = 1"),
        )

    duplicate_open_registers = op.get_bind().execute(
        sa.text(
            "SELECT register_id FROM cash_sessions WHERE status = 'open' "
            "GROUP BY register_id HAVING COUNT(*) > 1"
        )
    ).scalars().all()
    if duplicate_open_registers:
        raise RuntimeError(
            "Cannot enforce one open cash period per register; duplicate register IDs: "
            + ", ".join(str(register_id) for register_id in duplicate_open_registers)
        )

    session_columns = _columns("cash_sessions")
    additions = {
        "previous_session_id": sa.Column("previous_session_id", sa.Integer(), nullable=True),
        "card_expected_amount": sa.Column(
            "card_expected_amount",
            sa.Numeric(12, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        "card_declared_amount": sa.Column("card_declared_amount", sa.Numeric(12, 2), nullable=True),
        "card_difference": sa.Column("card_difference", sa.Numeric(12, 2), nullable=True),
        "transfer_expected_amount": sa.Column(
            "transfer_expected_amount",
            sa.Numeric(12, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        "total_expected_amount": sa.Column(
            "total_expected_amount",
            sa.Numeric(12, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        "total_difference": sa.Column("total_difference", sa.Numeric(12, 2), nullable=True),
        "retained_fund_amount": sa.Column(
            "retained_fund_amount",
            sa.Numeric(12, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        "cash_withdrawn_amount": sa.Column(
            "cash_withdrawn_amount",
            sa.Numeric(12, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        "result": sa.Column("result", sa.String(30), nullable=True),
        "denominations": sa.Column("denominations", sa.JSON(), nullable=True),
        "pending_orders_snapshot": sa.Column(
            "pending_orders_snapshot",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        "pending_orders_ignored": sa.Column(
            "pending_orders_ignored",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        "pending_orders_override_by": sa.Column(
            "pending_orders_override_by",
            sa.String(120),
            nullable=True,
        ),
        "pending_orders_override_at": sa.Column(
            "pending_orders_override_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        "actor_display_name": sa.Column("actor_display_name", sa.String(180), nullable=True),
        "version": sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    }
    for column_name, column in additions.items():
        if column_name not in session_columns:
            op.add_column("cash_sessions", column)

    if not _has_foreign_key("cash_sessions", "previous_session_id"):
        with op.batch_alter_table("cash_sessions") as batch_op:
            batch_op.create_foreign_key(
                "fk_cash_sessions_previous_session_id",
                "cash_sessions",
                ["previous_session_id"],
                ["id"],
                ondelete="SET NULL",
            )

    session_indexes = _indexes("cash_sessions")
    if "ix_cash_sessions_previous_session_id" not in session_indexes:
        op.create_index(
            "ix_cash_sessions_previous_session_id",
            "cash_sessions",
            ["previous_session_id"],
        )
    if "ix_cash_sessions_result" not in session_indexes:
        op.create_index("ix_cash_sessions_result", "cash_sessions", ["result"])
    if OPEN_SESSION_INDEX not in session_indexes:
        op.create_index(
            OPEN_SESSION_INDEX,
            "cash_sessions",
            ["register_id"],
            unique=True,
            postgresql_where=sa.text("status = 'open'"),
            sqlite_where=sa.text("status = 'open'"),
        )
    if HISTORY_INDEX not in session_indexes:
        op.create_index(
            HISTORY_INDEX,
            "cash_sessions",
            ["branch_id", "status", "closed_at"],
        )

    # Legacy payments are attached only when business, branch and timestamp
    # identify one and only one historical cash period.
    _backfill_unambiguous_payments(op.get_bind())


def downgrade() -> None:
    session_indexes = _indexes("cash_sessions")
    for index_name in (
        HISTORY_INDEX,
        OPEN_SESSION_INDEX,
        "ix_cash_sessions_result",
        "ix_cash_sessions_previous_session_id",
    ):
        if index_name in session_indexes:
            op.drop_index(index_name, table_name="cash_sessions")

    removable_session_columns = [
        "version",
        "actor_display_name",
        "pending_orders_override_at",
        "pending_orders_override_by",
        "pending_orders_ignored",
        "pending_orders_snapshot",
        "denominations",
        "result",
        "cash_withdrawn_amount",
        "retained_fund_amount",
        "total_difference",
        "total_expected_amount",
        "transfer_expected_amount",
        "card_difference",
        "card_declared_amount",
        "card_expected_amount",
        "previous_session_id",
    ]
    existing_session_columns = _columns("cash_sessions")
    with op.batch_alter_table("cash_sessions") as batch_op:
        if _has_foreign_key("cash_sessions", "previous_session_id"):
            for foreign_key in sa.inspect(op.get_bind()).get_foreign_keys("cash_sessions"):
                if "previous_session_id" in foreign_key.get("constrained_columns", []) and foreign_key.get("name"):
                    batch_op.drop_constraint(foreign_key["name"], type_="foreignkey")
        for column_name in removable_session_columns:
            if column_name in existing_session_columns:
                batch_op.drop_column(column_name)

    if DEFAULT_REGISTER_INDEX in _indexes("cash_registers"):
        op.drop_index(DEFAULT_REGISTER_INDEX, table_name="cash_registers")
    if "is_default" in _columns("cash_registers"):
        with op.batch_alter_table("cash_registers") as batch_op:
            batch_op.drop_column("is_default")
