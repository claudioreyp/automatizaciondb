"""Add complete modifier group editor fields.

Revision ID: 20260827_0010
Revises: 20260825_0009
"""

import sqlalchemy as sa
from alembic import op


revision = "20260827_0010"
down_revision = "20260825_0009"
branch_labels = None
depends_on = None


def _column_names(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    group_columns = _column_names("modifier_groups")
    with op.batch_alter_table("modifier_groups") as batch:
        if "internal_label" not in group_columns:
            batch.add_column(sa.Column("internal_label", sa.String(length=140), nullable=True))
        if "allow_repeats" not in group_columns:
            batch.add_column(
                sa.Column("allow_repeats", sa.Boolean(), nullable=False, server_default=sa.false())
            )
        if "sort_order" not in group_columns:
            batch.add_column(
                sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0")
            )

    modifier_columns = _column_names("modifiers")
    with op.batch_alter_table("modifiers") as batch:
        if "sort_order" not in modifier_columns:
            batch.add_column(
                sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0")
            )


def downgrade() -> None:
    modifier_columns = _column_names("modifiers")
    with op.batch_alter_table("modifiers") as batch:
        if "sort_order" in modifier_columns:
            batch.drop_column("sort_order")

    group_columns = _column_names("modifier_groups")
    with op.batch_alter_table("modifier_groups") as batch:
        if "sort_order" in group_columns:
            batch.drop_column("sort_order")
        if "allow_repeats" in group_columns:
            batch.drop_column("allow_repeats")
        if "internal_label" in group_columns:
            batch.drop_column("internal_label")
