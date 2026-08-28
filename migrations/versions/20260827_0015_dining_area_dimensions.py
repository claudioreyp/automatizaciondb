"""Add layout dimensions and optimistic versioning to dining areas.

Revision ID: 20260827_0015
Revises: 20260827_0014
"""

import sqlalchemy as sa
from alembic import op


revision = "20260827_0015"
down_revision = "20260827_0014"
branch_labels = None
depends_on = None


def column_names() -> set[str]:
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("dining_areas")
    }


def upgrade() -> None:
    existing_columns = column_names()
    if "columns" not in existing_columns:
        op.add_column(
            "dining_areas",
            sa.Column("columns", sa.Integer(), nullable=False, server_default=sa.text("7")),
        )
    if "rows" not in existing_columns:
        op.add_column(
            "dining_areas",
            sa.Column("rows", sa.Integer(), nullable=False, server_default=sa.text("5")),
        )
    if "version" not in existing_columns:
        op.add_column(
            "dining_areas",
            sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        )


def downgrade() -> None:
    existing_columns = column_names()
    removable_columns = [
        column_name
        for column_name in ("version", "rows", "columns")
        if column_name in existing_columns
    ]
    if removable_columns:
        with op.batch_alter_table("dining_areas") as batch_op:
            for column_name in removable_columns:
                batch_op.drop_column(column_name)
