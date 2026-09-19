"""Separate temporary availability from archived catalog entries."""

from alembic import op
import sqlalchemy as sa

revision = "20260908_0019"
down_revision = "20260902_0018"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("product_variants", "modifiers"):
        columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}
        if "available" not in columns:
            op.add_column(table, sa.Column("available", sa.Boolean(), nullable=False, server_default=sa.true()))
            if table == "product_variants":
                op.execute(sa.text("UPDATE product_variants SET available = false WHERE product_id IN (SELECT id FROM products WHERE available = false)"))


def downgrade():
    for table in ("modifiers", "product_variants"):
        with op.batch_alter_table(table) as batch:
            batch.drop_column("available")
