"""Add scoped agent coordination and durable attention without rewriting orders."""
from alembic import op
import sqlalchemy as sa

revision = "20261018_0027"
down_revision = "20261004_0026"
branch_labels = None
depends_on = None


def common():
    return [sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("business_id", sa.Integer(), sa.ForeignKey("businesses.id"), nullable=False),
        sa.Column("branch_id", sa.Integer(), sa.ForeignKey("branches.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False)]


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "profile" not in {column["name"] for column in inspector.get_columns("integration_credentials")}:
        op.add_column("integration_credentials", sa.Column("profile", sa.String(30), nullable=False, server_default="generic"))
    def create_table(name, *args):
        if name not in sa.inspect(op.get_bind()).get_table_names():
            op.create_table(name, *args)
    def create_index(name, table, columns):
        if name not in {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}:
            op.create_index(name, table, columns)
    create_table("agent_conversations", *common(),
        sa.Column("channel", sa.String(30), nullable=False), sa.Column("sender", sa.String(120), nullable=False),
        sa.Column("state", sa.JSON(), nullable=False), sa.Column("pending", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False), sa.Column("lease_hash", sa.String(64)),
        sa.Column("lease_until", sa.DateTime(timezone=True)), sa.Column("current_message_id", sa.String(240)),
        sa.UniqueConstraint("business_id", "branch_id", "channel", "sender", name="uq_agent_conversation_scope"))
    create_table("agent_attention_requests", *common(),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id")),
        sa.Column("sender", sa.String(120), nullable=False), sa.Column("kind", sa.String(50), nullable=False),
        sa.Column("summary", sa.String(2000), nullable=False), sa.Column("source_key", sa.String(240), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False), sa.Column("status", sa.String(20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False), sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_by", sa.String(120)), sa.Column("resolution_note", sa.String(2000)),
        sa.UniqueConstraint("business_id", "branch_id", "source_key", name="uq_agent_attention_source"))
    for table in ("agent_conversations", "agent_attention_requests"):
        for column in ("business_id", "branch_id"):
            create_index(f"ix_{table}_{column}", table, [column])
    create_index("ix_agent_attention_requests_order_id", "agent_attention_requests", ["order_id"])
    create_index("ix_agent_attention_branch_status", "agent_attention_requests", ["branch_id", "status", "created_at"])
    if op.get_bind().dialect.name == "postgresql":
        for table in ("agent_conversations", "agent_attention_requests"):
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"REVOKE ALL ON {table} FROM PUBLIC")
            for role in ("anon", "authenticated"):
                op.execute(f"DO $$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname='{role}') THEN REVOKE ALL ON {table} FROM {role}; END IF; END $$")
            op.execute(f"DO $$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname='escalar_pos_api') THEN GRANT SELECT,INSERT,UPDATE,DELETE ON {table} TO escalar_pos_api; IF NOT EXISTS (SELECT FROM pg_policy WHERE polrelid='{table}'::regclass AND polname='pos_api_server_access') THEN CREATE POLICY pos_api_server_access ON {table} TO escalar_pos_api USING (true) WITH CHECK (true); END IF; END IF; END $$")


def downgrade():
    # Rolling back application code must not delete conversations or human review.
    pass
