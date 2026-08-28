"""Allow open modifier limits and cap repeated options.

Revision ID: 20260827_0012
Revises: 20260827_0011
"""

import sqlalchemy as sa
from alembic import op


revision = "20260827_0012"
down_revision = "20260827_0011"
branch_labels = None
depends_on = None


def _column_names(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    columns = _column_names("modifier_groups")
    with op.batch_alter_table("modifier_groups") as batch:
        if "max_per_option" not in columns:
            batch.add_column(sa.Column("max_per_option", sa.Integer(), nullable=True))
        batch.alter_column(
            "maximum",
            existing_type=sa.Integer(),
            nullable=True,
        )


def downgrade() -> None:
    op.execute(sa.text("UPDATE modifier_groups SET maximum = 1 WHERE maximum IS NULL"))
    columns = _column_names("modifier_groups")
    with op.batch_alter_table("modifier_groups") as batch:
        if "max_per_option" in columns:
            batch.drop_column("max_per_option")
        batch.alter_column(
            "maximum",
            existing_type=sa.Integer(),
            nullable=False,
        )
