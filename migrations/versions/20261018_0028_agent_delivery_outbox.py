"""Durable consumer responses; uncertain delivery is never dispatched twice."""
from alembic import op
import sqlalchemy as sa

revision = "20261018_0028"
down_revision = "20261018_0027"
branch_labels = None
depends_on = None


def upgrade():
    table = "agent_message_deliveries"
    if table not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(table,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("business_id", sa.Integer(), sa.ForeignKey("businesses.id"), nullable=False),
            sa.Column("branch_id", sa.Integer(), sa.ForeignKey("branches.id"), nullable=False),
            sa.Column("conversation_id", sa.String(36), sa.ForeignKey("agent_conversations.id"), nullable=False),
            sa.Column("message_id", sa.String(240), nullable=False),
            sa.Column("sender", sa.String(120), nullable=False),
            sa.Column("response", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("claim_hash", sa.String(64)),
            sa.Column("claimed_at", sa.DateTime(timezone=True)),
            sa.Column("receipt", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("conversation_id", "message_id", name="uq_agent_delivery_input"))
    for name, columns in (("ix_agent_message_deliveries_business_id", ["business_id"]),
        ("ix_agent_message_deliveries_branch_id", ["branch_id"]),
        ("ix_agent_delivery_pending", ["branch_id", "status", "created_at"])):
        if name not in {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}:
            op.create_index(name, table, columns)
    if op.get_bind().dialect.name == "postgresql":
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON {table} FROM PUBLIC")
        for role in ("anon", "authenticated"):
            op.execute(f"DO $$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname='{role}') THEN REVOKE ALL ON {table} FROM {role}; END IF; END $$")
        op.execute(f"DO $$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname='escalar_pos_api') THEN GRANT SELECT,INSERT,UPDATE,DELETE ON {table} TO escalar_pos_api; IF NOT EXISTS (SELECT FROM pg_policy WHERE polrelid='{table}'::regclass AND polname='pos_api_server_access') THEN CREATE POLICY pos_api_server_access ON {table} TO escalar_pos_api USING (true) WITH CHECK (true); END IF; END IF; END $$")


def downgrade():
    # Keep the ledger, including uncertain responses, during application rollback.
    pass
