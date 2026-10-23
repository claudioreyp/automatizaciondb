"""Add durable model-usage reservations without rewriting existing records."""
from alembic import op
import sqlalchemy as sa

revision = "20261020_0029"
down_revision = "20261018_0028"
branch_labels = None
depends_on = None


def upgrade():
    table = "agent_model_usage"
    if table not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(table,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("business_id", sa.Integer(), sa.ForeignKey("businesses.id"), nullable=False),
            sa.Column("branch_id", sa.Integer(), sa.ForeignKey("branches.id"), nullable=False),
            sa.Column("conversation_id", sa.String(36), sa.ForeignKey("agent_conversations.id"), nullable=False),
            sa.Column("message_id", sa.String(240), nullable=False),
            sa.Column("call_id", sa.String(100), nullable=False),
            sa.Column("sender_hash", sa.String(64)),
            sa.Column("request_fingerprint", sa.String(64), nullable=False),
            sa.Column("reserve_fingerprint", sa.String(64), nullable=False),
            sa.Column("reserve_key", sa.String(240), nullable=False),
            sa.Column("model_id", sa.String(120), nullable=False),
            sa.Column("role", sa.String(20), nullable=False),
            sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
            sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
            sa.Column("reserved_tokens", sa.BigInteger(), nullable=False),
            sa.Column("charged_tokens", sa.BigInteger(), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("denial_reason", sa.String(50)),
            sa.Column("attention_recorded", sa.Boolean(), nullable=False),
            sa.Column("usage_known", sa.Boolean()),
            sa.Column("input_tokens", sa.BigInteger(), nullable=False),
            sa.Column("output_tokens", sa.BigInteger(), nullable=False),
            sa.Column("cache_creation_input_tokens", sa.BigInteger(), nullable=False),
            sa.Column("cache_read_input_tokens", sa.BigInteger(), nullable=False),
            sa.Column("settle_key", sa.String(240)),
            sa.Column("settlement_fingerprint", sa.String(64)),
            sa.Column("settled_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("conversation_id", "message_id", "call_id", name="uq_agent_usage_call"),
            sa.UniqueConstraint("conversation_id", "reserve_key", name="uq_agent_usage_reserve_key"),
            sa.UniqueConstraint("conversation_id", "settle_key", name="uq_agent_usage_settle_key"),
            sa.CheckConstraint("reserved_tokens > 0 AND charged_tokens >= 0", name="ck_agent_usage_tokens"),
            sa.CheckConstraint("status IN ('reserved','settled','uncertain','denied')", name="ck_agent_usage_status"))
    for name, columns in (
        ("ix_agent_usage_business_window", ["business_id", "window_start"]),
        ("ix_agent_usage_sender_window", ["business_id", "sender_hash", "window_start"]),
        ("ix_agent_usage_conversation_window", ["conversation_id", "window_start"]),
    ):
        if name not in {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(table)}:
            op.create_index(name, table, columns)
    if op.get_bind().dialect.name == "postgresql":
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON {table} FROM PUBLIC")
        for role in ("anon", "authenticated"):
            op.execute(f"DO $$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname='{role}') THEN REVOKE ALL ON {table} FROM {role}; END IF; END $$")
        op.execute(f"DO $$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname='escalar_pos_api') THEN GRANT SELECT,INSERT,UPDATE ON {table} TO escalar_pos_api; IF NOT EXISTS (SELECT FROM pg_policy WHERE polrelid='{table}'::regclass AND polname='pos_api_server_access') THEN CREATE POLICY pos_api_server_access ON {table} TO escalar_pos_api USING (true) WITH CHECK (true); END IF; END IF; END $$")


def downgrade():
    # A code rollback must preserve all charges, uncertain calls and replay guards.
    pass
