"""Independent additional receipts; historical receipts remain initial payments."""
from alembic import op
import sqlalchemy as sa

revision = "20260920_0024"
down_revision = "20260919_0023"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    # Older bootstrap revisions use live metadata on fresh databases.
    if "order_payment_requests" not in inspector.get_table_names():
        op.create_table("order_payment_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("business_id", sa.Integer(), sa.ForeignKey("businesses.id"), nullable=False, index=True),
        sa.Column("branch_id", sa.Integer(), sa.ForeignKey("branches.id"), nullable=False, index=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=False, index=True),
        sa.Column("purpose", sa.String(20), nullable=False),
        sa.Column("method", sa.String(20), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("payment_id", sa.Integer(), sa.ForeignKey("payments.id"), unique=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    columns = {c["name"] for c in inspector.get_columns("payment_evidence")}
    indexes = {i["name"] for i in inspector.get_indexes("payment_evidence")}
    with op.batch_alter_table("payment_evidence") as batch:
        if "payment_request_id" not in columns:
            batch.add_column(sa.Column("payment_request_id", sa.String(36), nullable=True))
            batch.create_foreign_key("fk_evidence_payment_request", "order_payment_requests", ["payment_request_id"], ["id"])
        if "ix_payment_evidence_payment_request_id" not in indexes:
            batch.create_index("ix_payment_evidence_payment_request_id", ["payment_request_id"])
        if "uq_payment_evidence_one_open_per_order" in indexes:
            batch.drop_index("uq_payment_evidence_one_open_per_order")
        batch.create_index("uq_payment_evidence_one_open_per_order", ["order_id"], unique=True,
            postgresql_where=sa.text("payment_request_id IS NULL AND status IN ('evidence_received', 'under_review')"),
            sqlite_where=sa.text("payment_request_id IS NULL AND status IN ('evidence_received', 'under_review')"))
        if "uq_payment_evidence_open_request" in indexes:
            batch.drop_index("uq_payment_evidence_open_request")
        batch.create_index("uq_payment_evidence_open_request", ["payment_request_id"], unique=True,
            postgresql_where=sa.text("payment_request_id IS NOT NULL AND status IN ('evidence_received', 'under_review')"),
            sqlite_where=sa.text("payment_request_id IS NOT NULL AND status IN ('evidence_received', 'under_review')"))
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE order_payment_requests ENABLE ROW LEVEL SECURITY")
        # Hosting-specific runtime grants are applied separately by the private role setup.
        op.execute("DO $$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname='anon') THEN REVOKE ALL ON order_payment_requests FROM anon; END IF; IF EXISTS (SELECT FROM pg_roles WHERE rolname='authenticated') THEN REVOKE ALL ON order_payment_requests FROM authenticated; END IF; END $$")


def downgrade():
    # A compatible application rollback keeps receipts and requests for reconciliation.
    pass
