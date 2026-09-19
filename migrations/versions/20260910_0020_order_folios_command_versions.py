"""Add restaurant-wide order folios and versioned kitchen revisions.

Revision ID: 20260910_0020
Revises: 20260908_0019
"""
from collections import defaultdict

import sqlalchemy as sa
from alembic import op

revision = "20260910_0020"
down_revision = "20260908_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    for table, column in (
        ("businesses", sa.Column("order_folio_counter", sa.Integer(), nullable=False, server_default="0")),
        ("orders", sa.Column("folio", sa.Integer(), nullable=True)),
        ("kitchen_tickets", sa.Column("version", sa.Integer(), nullable=False, server_default="1")),
    ):
        if column.name not in {item["name"] for item in sa.inspect(connection).get_columns(table)}:
            op.add_column(table, column)
    counters = defaultdict(int)
    rows = connection.execute(sa.text("SELECT id, business_id, folio FROM orders ORDER BY business_id, created_at, id")).mappings().all()
    for row in rows:
        counters[row["business_id"]] = max(counters[row["business_id"]], row["folio"] or 0)
    for row in rows:
        if row["folio"] is not None:
            continue
        business_id = row["business_id"]
        counters[business_id] += 1
        connection.execute(sa.text("UPDATE orders SET folio = :folio WHERE id = :id"), {"folio": counters[business_id], "id": row["id"]})
    for business_id, counter in counters.items():
        connection.execute(sa.text("UPDATE businesses SET order_folio_counter = :counter WHERE id = :id"), {"counter": counter, "id": business_id})
    inspector = sa.inspect(connection)
    names = {entry["name"] for entry in inspector.get_unique_constraints("orders") + inspector.get_indexes("orders")}
    if "uq_order_business_folio" not in names:
        # An index enforces the same uniqueness without rebuilding a referenced SQLite table.
        op.create_index("uq_order_business_folio", "orders", ["business_id", "folio"], unique=True)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "uq_order_business_folio" in {entry["name"] for entry in inspector.get_indexes("orders")}:
        op.drop_index("uq_order_business_folio", table_name="orders")
    with op.batch_alter_table("orders") as batch:
        if "uq_order_business_folio" in {entry["name"] for entry in inspector.get_unique_constraints("orders")}:
            batch.drop_constraint("uq_order_business_folio", type_="unique")
        batch.drop_column("folio")
    with op.batch_alter_table("businesses") as batch:
        batch.drop_column("order_folio_counter")
    with op.batch_alter_table("kitchen_tickets") as batch:
        batch.drop_column("version")
