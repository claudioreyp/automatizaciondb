"""Secure the Alembic version table from Supabase Data API roles.

Revision ID: 20260815_0006
Revises: 20260815_0005
"""

from alembic import op


revision = "20260815_0006"
down_revision = "20260815_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE alembic_version ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE alembic_version DISABLE ROW LEVEL SECURITY")
