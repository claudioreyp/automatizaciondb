"""Add operational promotions and promotion discount snapshots.

Revision ID: 20260827_0011
Revises: 20260827_0010
"""

import sqlalchemy as sa
from alembic import op


revision = "20260827_0011"
down_revision = "20260827_0010"
branch_labels = None
depends_on = None


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _column_names(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    tables = _table_names()
    if "promotions" not in tables:
        op.create_table(
            "promotions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("business_id", sa.Integer(), nullable=False),
            sa.Column("branch_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=180), nullable=False),
            sa.Column("promotion_type", sa.String(length=30), nullable=False),
            sa.Column("discount_type", sa.String(length=30), nullable=True),
            sa.Column("discount_value", sa.Numeric(12, 2), nullable=True),
            sa.Column("receive_quantity", sa.Integer(), nullable=True),
            sa.Column("pay_quantity", sa.Integer(), nullable=True),
            sa.Column("target_scope", sa.String(length=30), nullable=False),
            sa.Column("starts_on", sa.Date(), nullable=True),
            sa.Column("ends_on", sa.Date(), nullable=True),
            sa.Column("weekdays", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("service_channels", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "promotion_type IN ('product_discount', 'buy_x_pay_y')",
                name="ck_promotions_type",
            ),
            sa.CheckConstraint(
                "target_scope IN ('products', 'categories')",
                name="ck_promotions_target_scope",
            ),
            sa.CheckConstraint(
                "discount_type IS NULL OR discount_type IN ('percentage', 'fixed_amount')",
                name="ck_promotions_discount_type",
            ),
            sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["branch_id"], ["branches.id"], ondelete="CASCADE"),
        )
        op.create_index("ix_promotions_business_id", "promotions", ["business_id"])
        op.create_index("ix_promotions_branch_id", "promotions", ["branch_id"])
        op.create_index("ix_promotions_promotion_type", "promotions", ["promotion_type"])
        op.create_index("ix_promotions_active", "promotions", ["active"])
        op.create_index("ix_promotions_archived_at", "promotions", ["archived_at"])
        op.create_index(
            "ix_promotions_branch_active",
            "promotions",
            ["branch_id", "active", "archived_at"],
        )

    tables = _table_names()
    if "promotion_targets" not in tables:
        op.create_table(
            "promotion_targets",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("promotion_id", sa.Integer(), nullable=False),
            sa.Column("product_id", sa.Integer(), nullable=True),
            sa.Column("category_id", sa.Integer(), nullable=True),
            sa.CheckConstraint(
                "(product_id IS NOT NULL AND category_id IS NULL) OR "
                "(product_id IS NULL AND category_id IS NOT NULL)",
                name="ck_promotion_targets_single_target",
            ),
            sa.ForeignKeyConstraint(["promotion_id"], ["promotions.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["category_id"], ["categories.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("promotion_id", "product_id", name="uq_promotion_target_product"),
            sa.UniqueConstraint("promotion_id", "category_id", name="uq_promotion_target_category"),
        )
        op.create_index("ix_promotion_targets_promotion_id", "promotion_targets", ["promotion_id"])
        op.create_index("ix_promotion_targets_product_id", "promotion_targets", ["product_id"])
        op.create_index("ix_promotion_targets_category_id", "promotion_targets", ["category_id"])

    order_columns = _column_names("orders")
    with op.batch_alter_table("orders") as batch:
        if "manual_discount" not in order_columns:
            batch.add_column(
                sa.Column("manual_discount", sa.Numeric(12, 2), nullable=False, server_default="0")
            )
        if "promotion_discount" not in order_columns:
            batch.add_column(
                sa.Column("promotion_discount", sa.Numeric(12, 2), nullable=False, server_default="0")
            )
        if "applied_promotions" not in order_columns:
            batch.add_column(
                sa.Column("applied_promotions", sa.JSON(), nullable=False, server_default=sa.text("'[]'"))
            )
    op.execute(sa.text("UPDATE orders SET manual_discount = COALESCE(discount, 0)"))

    item_columns = _column_names("order_items")
    with op.batch_alter_table("order_items") as batch:
        if "promotion_discount" not in item_columns:
            batch.add_column(
                sa.Column("promotion_discount", sa.Numeric(12, 2), nullable=False, server_default="0")
            )
        if "promotion_snapshot" not in item_columns:
            batch.add_column(sa.Column("promotion_snapshot", sa.JSON(), nullable=True))


def downgrade() -> None:
    item_columns = _column_names("order_items")
    with op.batch_alter_table("order_items") as batch:
        if "promotion_snapshot" in item_columns:
            batch.drop_column("promotion_snapshot")
        if "promotion_discount" in item_columns:
            batch.drop_column("promotion_discount")

    order_columns = _column_names("orders")
    with op.batch_alter_table("orders") as batch:
        if "applied_promotions" in order_columns:
            batch.drop_column("applied_promotions")
        if "promotion_discount" in order_columns:
            batch.drop_column("promotion_discount")
        if "manual_discount" in order_columns:
            batch.drop_column("manual_discount")

    tables = _table_names()
    if "promotion_targets" in tables:
        op.drop_table("promotion_targets")
    tables = _table_names()
    if "promotions" in tables:
        op.drop_table("promotions")
