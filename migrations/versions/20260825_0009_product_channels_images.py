"""Add product service channels and private image storage.

Revision ID: 20260825_0009
Revises: 20260822_0008
"""

import sqlalchemy as sa
from alembic import op


revision = "20260825_0009"
down_revision = "20260822_0008"
branch_labels = None
depends_on = None


DEFAULT_CHANNELS = [
    "pos_tables",
    "pos_counter",
    "pos_takeaway",
    "pos_delivery",
    "digital_tables",
    "digital_takeaway",
    "digital_delivery",
]


def _column_names(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    columns = _column_names("products")
    with op.batch_alter_table("products") as batch:
        if "image_storage_path" not in columns:
            batch.add_column(sa.Column("image_storage_path", sa.Text(), nullable=True))
        if "service_channels" not in columns:
            batch.add_column(sa.Column("service_channels", sa.JSON(), nullable=True))

    if "service_channels" not in columns:
        products = sa.table("products", sa.column("service_channels", sa.JSON()))
        op.execute(products.update().values(service_channels=DEFAULT_CHANNELS))
        with op.batch_alter_table("products") as batch:
            batch.alter_column("service_channels", existing_type=sa.JSON(), nullable=False)


def downgrade() -> None:
    columns = _column_names("products")
    with op.batch_alter_table("products") as batch:
        if "service_channels" in columns:
            batch.drop_column("service_channels")
        if "image_storage_path" in columns:
            batch.drop_column("image_storage_path")
