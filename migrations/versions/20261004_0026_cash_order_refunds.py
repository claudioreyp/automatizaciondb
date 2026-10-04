"""Link new cancellation refunds without rewriting historical money records."""
from alembic import op
import sqlalchemy as sa

revision = "20261004_0026"
down_revision = "20261002_0025"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "order_id" not in {column["name"] for column in inspector.get_columns("cash_movements")}:
        # SQLite requires a batch for adding a foreign key. Existing rows receive
        # NULL, never a guessed order or a synthetic historical refund.
        with op.batch_alter_table("cash_movements") as batch:
            batch.add_column(sa.Column("order_id", sa.Integer(), nullable=True))
            batch.create_foreign_key("fk_cash_movements_refund_order", "orders", ["order_id"], ["id"], ondelete="RESTRICT")
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("cash_movements")}
    if "ix_cash_movements_order_id" not in indexes:
        op.create_index("ix_cash_movements_order_id", "cash_movements", ["order_id"])
    if "uq_cash_movements_order_refund_method" not in indexes:
        op.create_index(
            "uq_cash_movements_order_refund_method", "cash_movements",
            ["order_id", "payment_method"], unique=True,
            sqlite_where=sa.text("movement_type = 'refund' AND order_id IS NOT NULL"),
            postgresql_where=sa.text("movement_type = 'refund' AND order_id IS NOT NULL"),
        )


def downgrade():
    # Application rollback must retain the refund ledger and its identities.
    pass
