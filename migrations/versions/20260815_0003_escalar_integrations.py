"""Escalar AI integration credentials, payment evidence and durable events.

Revision ID: 20260815_0003
Revises: 20260717_0002
"""

from alembic import op
import sqlalchemy as sa


revision = "20260815_0003"
down_revision = "20260717_0002"
branch_labels = None
depends_on = None


def column_names(table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def index_names(table_name: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table_name)}


def upgrade() -> None:
    branch_columns = column_names("branches")
    with op.batch_alter_table("branches") as batch:
        if "maps_url" not in branch_columns:
            batch.add_column(sa.Column("maps_url", sa.Text(), nullable=True))
        if "yape_qr_storage_path" not in branch_columns:
            batch.add_column(sa.Column("yape_qr_storage_path", sa.Text(), nullable=True))

    order_columns = column_names("orders")
    with op.batch_alter_table("orders") as batch:
        if "payment_method" not in order_columns:
            batch.add_column(sa.Column("payment_method", sa.String(length=30), nullable=True))
        if "whatsapp_chat_id" not in order_columns:
            batch.add_column(sa.Column("whatsapp_chat_id", sa.String(length=120), nullable=True))
        if "whatsapp_message_id" not in order_columns:
            batch.add_column(sa.Column("whatsapp_message_id", sa.String(length=180), nullable=True))
        if "submitted_at" not in order_columns:
            batch.add_column(sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True))

    order_indexes = index_names("orders")
    with op.batch_alter_table("orders") as batch:
        if "ix_orders_payment_method" not in order_indexes:
            batch.create_index("ix_orders_payment_method", ["payment_method"])
        if "ix_orders_whatsapp_chat_id" not in order_indexes:
            batch.create_index("ix_orders_whatsapp_chat_id", ["whatsapp_chat_id"])
        if "ix_orders_whatsapp_message_id" not in order_indexes:
            batch.create_index("ix_orders_whatsapp_message_id", ["whatsapp_message_id"])
        if "ix_orders_submitted_at" not in order_indexes:
            batch.create_index("ix_orders_submitted_at", ["submitted_at"])

    evidence_columns = column_names("payment_evidence")
    with op.batch_alter_table("payment_evidence") as batch:
        if "security_code" not in evidence_columns:
            batch.add_column(sa.Column("security_code", sa.String(length=3), nullable=True))
        if "image_sha256" not in evidence_columns:
            batch.add_column(sa.Column("image_sha256", sa.String(length=64), nullable=True))
        if "whatsapp_message_id" not in evidence_columns:
            batch.add_column(sa.Column("whatsapp_message_id", sa.String(length=180), nullable=True))
        if "warnings" not in evidence_columns:
            batch.add_column(sa.Column("warnings", sa.JSON(), nullable=False, server_default="[]"))
    if "image_sha256" not in evidence_columns:
        op.execute(sa.text("UPDATE payment_evidence SET image_sha256 = 'legacy-' || id WHERE image_sha256 IS NULL"))
        with op.batch_alter_table("payment_evidence") as batch:
            batch.alter_column("image_sha256", existing_type=sa.String(length=64), nullable=False)

    evidence_indexes = index_names("payment_evidence")
    with op.batch_alter_table("payment_evidence") as batch:
        if "ix_payment_evidence_security_code" not in evidence_indexes:
            batch.create_index("ix_payment_evidence_security_code", ["security_code"])
        if "ix_payment_evidence_image_sha256" not in evidence_indexes:
            batch.create_index("ix_payment_evidence_image_sha256", ["image_sha256"])
        if "ix_payment_evidence_whatsapp_message_id" not in evidence_indexes:
            batch.create_index("ix_payment_evidence_whatsapp_message_id", ["whatsapp_message_id"])

    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "integration_credentials" not in tables:
        op.create_table(
            "integration_credentials",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("business_id", sa.Integer(), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
            sa.Column("branch_id", sa.Integer(), sa.ForeignKey("branches.id", ondelete="CASCADE"), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("token_prefix", sa.String(length=32), nullable=False, unique=True),
            sa.Column("token_hash", sa.String(length=64), nullable=False, unique=True),
            sa.Column("scopes", sa.JSON(), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_by", sa.String(length=120), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_integration_credentials_business_id", "integration_credentials", ["business_id"])
        op.create_index("ix_integration_credentials_branch_id", "integration_credentials", ["branch_id"])
        op.create_index("ix_integration_credentials_token_prefix", "integration_credentials", ["token_prefix"], unique=True)
        op.create_index("ix_integration_credentials_token_hash", "integration_credentials", ["token_hash"], unique=True)
        op.create_index("ix_integration_credentials_active", "integration_credentials", ["active"])

    if "integration_events" not in tables:
        op.create_table(
            "integration_events",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("business_id", sa.Integer(), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
            sa.Column("branch_id", sa.Integer(), sa.ForeignKey("branches.id", ondelete="CASCADE"), nullable=False),
            sa.Column("event_type", sa.String(length=100), nullable=False),
            sa.Column("aggregate_type", sa.String(length=60), nullable=False),
            sa.Column("aggregate_id", sa.String(length=120), nullable=False),
            sa.Column("customer_phone", sa.String(length=40), nullable=True),
            sa.Column("whatsapp_chat_id", sa.String(length=120), nullable=True),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("acknowledged_by", sa.String(length=120), nullable=True),
            sa.Column("delivery_attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_integration_events_business_id", "integration_events", ["business_id"])
        op.create_index("ix_integration_events_branch_id", "integration_events", ["branch_id"])
        op.create_index("ix_integration_events_event_type", "integration_events", ["event_type"])
        op.create_index("ix_integration_events_aggregate_id", "integration_events", ["aggregate_id"])
        op.create_index("ix_integration_events_customer_phone", "integration_events", ["customer_phone"])
        op.create_index("ix_integration_events_whatsapp_chat_id", "integration_events", ["whatsapp_chat_id"])
        op.create_index("ix_integration_events_available_at", "integration_events", ["available_at"])
        op.create_index("ix_integration_events_acknowledged_at", "integration_events", ["acknowledged_at"])
        op.create_index("ix_integration_events_created_at", "integration_events", ["created_at"])
        op.create_index(
            "ix_integration_events_branch_pending",
            "integration_events",
            ["branch_id", "acknowledged_at", "created_at"],
        )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "integration_events" in tables:
        op.drop_table("integration_events")
    if "integration_credentials" in tables:
        op.drop_table("integration_credentials")

    with op.batch_alter_table("payment_evidence") as batch:
        for name in ["warnings", "whatsapp_message_id", "image_sha256", "security_code"]:
            if name in column_names("payment_evidence"):
                batch.drop_column(name)
    with op.batch_alter_table("orders") as batch:
        for name in ["submitted_at", "whatsapp_message_id", "whatsapp_chat_id", "payment_method"]:
            if name in column_names("orders"):
                batch.drop_column(name)
    with op.batch_alter_table("branches") as batch:
        for name in ["yape_qr_storage_path", "maps_url"]:
            if name in column_names("branches"):
                batch.drop_column(name)
