"""Add order workspace index, sequenced tickets, and RLS coverage.

Revision ID: 20260827_0013
Revises: 20260827_0012
"""

import sqlalchemy as sa
from alembic import op


revision = "20260827_0013"
down_revision = "20260827_0012"
branch_labels = None
depends_on = None


def _unique_constraint_names(table: str) -> set[str]:
    return {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_unique_constraints(table)
        if constraint.get("name")
    }


def _index_names(table: str) -> set[str]:
    return {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes(table)
        if index.get("name")
    }


def upgrade() -> None:
    constraints = _unique_constraint_names("kitchen_tickets")
    with op.batch_alter_table("kitchen_tickets") as batch:
        if "uq_ticket_order_station" in constraints:
            batch.drop_constraint("uq_ticket_order_station", type_="unique")
        if "uq_ticket_order_station_sequence" not in constraints:
            batch.create_unique_constraint(
                "uq_ticket_order_station_sequence",
                ["order_id", "station", "sequence"],
            )

    if "ix_orders_branch_created_id" not in _index_names("orders"):
        op.create_index(
            "ix_orders_branch_created_id",
            "orders",
            ["branch_id", "created_at", "id"],
        )

    if op.get_bind().dialect.name == "postgresql":
        for table_name in ("combo_items", "promotions", "promotion_targets"):
            op.execute(f'ALTER TABLE public."{table_name}" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    duplicate_station = op.get_bind().execute(
        sa.text(
            "SELECT 1 FROM kitchen_tickets "
            "GROUP BY order_id, station HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate_station:
        raise RuntimeError(
            "Cannot restore one-ticket-per-station uniqueness after additional comandas exist"
        )

    constraints = _unique_constraint_names("kitchen_tickets")
    with op.batch_alter_table("kitchen_tickets") as batch:
        if "uq_ticket_order_station_sequence" in constraints:
            batch.drop_constraint("uq_ticket_order_station_sequence", type_="unique")
        if "uq_ticket_order_station" not in constraints:
            batch.create_unique_constraint(
                "uq_ticket_order_station",
                ["order_id", "station"],
            )

    if "ix_orders_branch_created_id" in _index_names("orders"):
        op.drop_index("ix_orders_branch_created_id", table_name="orders")

    if op.get_bind().dialect.name == "postgresql":
        for table_name in ("combo_items", "promotions", "promotion_targets"):
            op.execute(f'ALTER TABLE public."{table_name}" DISABLE ROW LEVEL SECURITY')
