"""Add command history, item revisions, and table checkout state.

Revision ID: 20260831_0016
Revises: 20260827_0015
"""

from collections import defaultdict

import sqlalchemy as sa
from alembic import op


revision = "20260831_0016"
down_revision = "20260827_0015"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def _indexes(table_name: str) -> set[str]:
    return {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
    }


def _unique_constraints(table_name: str) -> set[str]:
    return {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_unique_constraints(table_name)
        if constraint.get("name")
    }


def _foreign_keys(table_name: str) -> set[str]:
    return {
        foreign_key["name"]
        for foreign_key in sa.inspect(op.get_bind()).get_foreign_keys(table_name)
        if foreign_key.get("name")
    }


def _backfill_global_sequences() -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT id, order_id FROM kitchen_tickets "
            "ORDER BY order_id, fired_at, id"
        )
    ).mappings()
    sequences: dict[int, int] = defaultdict(int)
    for row in rows:
        order_id = int(row["order_id"])
        sequences[order_id] += 1
        connection.execute(
            sa.text("UPDATE kitchen_tickets SET sequence = :sequence WHERE id = :id"),
            {"sequence": sequences[order_id], "id": row["id"]},
        )


def upgrade() -> None:
    order_columns = _columns("orders")
    if "checkout_started_at" not in order_columns:
        op.add_column(
            "orders",
            sa.Column("checkout_started_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "table_released_at" not in order_columns:
        op.add_column(
            "orders",
            sa.Column("table_released_at", sa.DateTime(timezone=True), nullable=True),
        )

    order_item_columns = _columns("order_items")
    if "replaces_item_id" not in order_item_columns:
        op.add_column("order_items", sa.Column("replaces_item_id", sa.Integer(), nullable=True))
    if "cancellation_reason" not in order_item_columns:
        op.add_column("order_items", sa.Column("cancellation_reason", sa.Text(), nullable=True))
    if "fk_order_items_replaces_item_id" not in _foreign_keys("order_items"):
        with op.batch_alter_table("order_items") as batch_op:
            batch_op.create_foreign_key(
                "fk_order_items_replaces_item_id",
                "order_items",
                ["replaces_item_id"],
                ["id"],
                ondelete="SET NULL",
            )

    ticket_columns = _columns("kitchen_tickets")
    if "kind" not in ticket_columns:
        op.add_column(
            "kitchen_tickets",
            sa.Column(
                "kind",
                sa.String(length=30),
                nullable=False,
                server_default=sa.text("'standard'"),
            ),
        )
    if "context_snapshot" not in ticket_columns:
        op.add_column(
            "kitchen_tickets",
            sa.Column(
                "context_snapshot",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            ),
        )

    _backfill_global_sequences()
    if "uq_ticket_order_sequence" not in _unique_constraints("kitchen_tickets"):
        with op.batch_alter_table("kitchen_tickets") as batch_op:
            batch_op.create_unique_constraint(
                "uq_ticket_order_sequence",
                ["order_id", "sequence"],
            )

    indexes = _indexes("orders")
    if "ix_orders_checkout_started_at" not in indexes:
        op.create_index("ix_orders_checkout_started_at", "orders", ["checkout_started_at"])
    if "ix_orders_table_released_at" not in indexes:
        op.create_index("ix_orders_table_released_at", "orders", ["table_released_at"])
    if "ix_order_items_replaces_item_id" not in _indexes("order_items"):
        op.create_index(
            "ix_order_items_replaces_item_id",
            "order_items",
            ["replaces_item_id"],
        )
    if "ix_kitchen_tickets_kind" not in _indexes("kitchen_tickets"):
        op.create_index("ix_kitchen_tickets_kind", "kitchen_tickets", ["kind"])


def downgrade() -> None:
    if "ix_kitchen_tickets_kind" in _indexes("kitchen_tickets"):
        op.drop_index("ix_kitchen_tickets_kind", table_name="kitchen_tickets")
    if "ix_order_items_replaces_item_id" in _indexes("order_items"):
        op.drop_index("ix_order_items_replaces_item_id", table_name="order_items")
    order_indexes = _indexes("orders")
    if "ix_orders_table_released_at" in order_indexes:
        op.drop_index("ix_orders_table_released_at", table_name="orders")
    if "ix_orders_checkout_started_at" in order_indexes:
        op.drop_index("ix_orders_checkout_started_at", table_name="orders")

    if "uq_ticket_order_sequence" in _unique_constraints("kitchen_tickets"):
        with op.batch_alter_table("kitchen_tickets") as batch_op:
            batch_op.drop_constraint("uq_ticket_order_sequence", type_="unique")
    ticket_columns = _columns("kitchen_tickets")
    with op.batch_alter_table("kitchen_tickets") as batch_op:
        if "context_snapshot" in ticket_columns:
            batch_op.drop_column("context_snapshot")
        if "kind" in ticket_columns:
            batch_op.drop_column("kind")

    order_item_columns = _columns("order_items")
    with op.batch_alter_table("order_items") as batch_op:
        if "fk_order_items_replaces_item_id" in _foreign_keys("order_items"):
            batch_op.drop_constraint("fk_order_items_replaces_item_id", type_="foreignkey")
        if "cancellation_reason" in order_item_columns:
            batch_op.drop_column("cancellation_reason")
        if "replaces_item_id" in order_item_columns:
            batch_op.drop_column("replaces_item_id")

    order_columns = _columns("orders")
    with op.batch_alter_table("orders") as batch_op:
        if "table_released_at" in order_columns:
            batch_op.drop_column("table_released_at")
        if "checkout_started_at" in order_columns:
            batch_op.drop_column("checkout_started_at")
