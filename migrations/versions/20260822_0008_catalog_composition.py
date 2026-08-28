"""Add structured catalog composition for variants, extras and combos.

Revision ID: 20260822_0008
Revises: 20260821_0007
"""

import sqlalchemy as sa
from alembic import op


revision = "20260822_0008"
down_revision = "20260821_0007"
branch_labels = None
depends_on = None


def _column_names(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    product_columns = _column_names("products")
    if "product_type" not in product_columns:
        with op.batch_alter_table("products") as batch:
            batch.add_column(
                sa.Column("product_type", sa.String(length=20), nullable=False, server_default="standard")
            )

    group_columns = _column_names("modifier_groups")
    if "branch_id" not in group_columns:
        with op.batch_alter_table("modifier_groups") as batch:
            batch.add_column(sa.Column("branch_id", sa.Integer(), nullable=True))
            batch.create_foreign_key(
                "fk_modifier_groups_branch_id",
                "branches",
                ["branch_id"],
                ["id"],
                ondelete="CASCADE",
            )
            batch.create_index("ix_modifier_groups_branch_id", ["branch_id"], unique=False)
        op.execute(
            sa.text(
                """
                UPDATE modifier_groups
                SET branch_id = COALESCE(
                    (
                        SELECT products.branch_id
                        FROM product_modifier_groups
                        JOIN products ON products.id = product_modifier_groups.product_id
                        WHERE product_modifier_groups.group_id = modifier_groups.id
                        LIMIT 1
                    ),
                    (
                        SELECT branches.id
                        FROM branches
                        WHERE branches.business_id = modifier_groups.business_id
                        ORDER BY branches.id
                        LIMIT 1
                    )
                )
                """
            )
        )

    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "combo_items" not in tables:
        op.create_table(
            "combo_items",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("product_id", sa.Integer(), nullable=False),
            sa.Column("component_product_id", sa.Integer(), nullable=False),
            sa.Column("quantity", sa.Numeric(12, 3), nullable=False, server_default="1"),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["component_product_id"], ["products.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("product_id", "component_product_id", name="uq_combo_component"),
        )
        op.create_index("ix_combo_items_product_id", "combo_items", ["product_id"], unique=False)
        op.create_index(
            "ix_combo_items_component_product_id",
            "combo_items",
            ["component_product_id"],
            unique=False,
        )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "combo_items" in tables:
        op.drop_index("ix_combo_items_component_product_id", table_name="combo_items")
        op.drop_index("ix_combo_items_product_id", table_name="combo_items")
        op.drop_table("combo_items")

    group_columns = _column_names("modifier_groups")
    if "branch_id" in group_columns:
        with op.batch_alter_table("modifier_groups") as batch:
            batch.drop_index("ix_modifier_groups_branch_id")
            batch.drop_constraint("fk_modifier_groups_branch_id", type_="foreignkey")
            batch.drop_column("branch_id")

    product_columns = _column_names("products")
    if "product_type" in product_columns:
        with op.batch_alter_table("products") as batch:
            batch.drop_column("product_type")
