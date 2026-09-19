"""Add optional delivery policy without changing existing fees or coverage.

Revision ID: 20260912_0021
Revises: 20260910_0020
"""
import sqlalchemy as sa
from alembic import op

revision = "20260912_0021"
down_revision = "20260910_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Earlier bootstrap migrations create missing tables from current metadata.
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("branch_settings")}
    if "delivery_policy" not in columns:
        op.add_column(
            "branch_settings",
            sa.Column("delivery_policy", sa.JSON(none_as_null=True), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("branch_settings", "delivery_policy")
