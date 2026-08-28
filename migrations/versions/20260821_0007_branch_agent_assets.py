"""Add private branch assets and agent conversation context.

Revision ID: 20260821_0007
Revises: 20260815_0006
"""

import sqlalchemy as sa
from alembic import op


revision = "20260821_0007"
down_revision = "20260815_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("branches")}
    with op.batch_alter_table("branches") as batch:
        if "menu_card_storage_path" not in columns:
            batch.add_column(sa.Column("menu_card_storage_path", sa.Text(), nullable=True))
        if "agent_context_notes" not in columns:
            batch.add_column(sa.Column("agent_context_notes", sa.Text(), nullable=True))


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("branches")}
    with op.batch_alter_table("branches") as batch:
        if "agent_context_notes" in columns:
            batch.drop_column("agent_context_notes")
        if "menu_card_storage_path" in columns:
            batch.drop_column("menu_card_storage_path")
