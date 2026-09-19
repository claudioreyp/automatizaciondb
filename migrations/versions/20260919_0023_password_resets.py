"""Durable owner password renewals, without modifying existing POS data."""
from alembic import op
import sqlalchemy as sa

revision = "20260919_0023"
down_revision = "20260912_0022"
branch_labels = None
depends_on = None


def upgrade():
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "auth_security_states" not in existing:
        op.create_table("auth_security_states",
            sa.Column("auth_user_id", sa.String(120), primary_key=True),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("pending_operation_id", sa.String(36)),
            sa.Column("requires_password_reset", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    if "password_reset_operations" not in existing:
        op.create_table("password_reset_operations",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("auth_user_id", sa.String(120), nullable=False, index=True),
            sa.Column("membership_id", sa.Integer(), sa.ForeignKey("memberships.id"), nullable=False),
            sa.Column("business_id", sa.Integer(), sa.ForeignKey("businesses.id"), nullable=False, index=True),
            sa.Column("actor_id", sa.String(120), nullable=False),
            sa.Column("key_hash", sa.String(64), nullable=False),
            sa.Column("request_digest", sa.String(64), nullable=False),
            sa.Column("security_version", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("error_code", sa.String(60)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("auth_user_id", "key_hash", name="uq_password_reset_key"))
    if op.get_bind().dialect.name == "postgresql":
        roles = set(op.get_bind().execute(sa.text("SELECT rolname FROM pg_roles")).scalars())
        for table in ("auth_security_states", "password_reset_operations"):
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"REVOKE ALL ON TABLE {table} FROM PUBLIC")
            for role in ("anon", "authenticated"):
                if role in roles:
                    op.execute(f"REVOKE ALL ON TABLE {table} FROM {role}")


def downgrade():
    op.drop_table("password_reset_operations")
    op.drop_table("auth_security_states")
