"""Preserve archived tables and their historical identities."""
from alembic import op
import sqlalchemy as sa

revision = "20261002_0025"
down_revision = "20260920_0024"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    # The initial bootstrap uses current metadata on fresh databases.
    if "archived_at" not in {column["name"] for column in inspector.get_columns("restaurant_tables")}:
        op.add_column("restaurant_tables", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    if "ix_restaurant_tables_archived_at" not in {index["name"] for index in inspector.get_indexes("restaurant_tables")}:
        op.create_index("ix_restaurant_tables_archived_at", "restaurant_tables", ["archived_at"])


def downgrade():
    # Keep the archive marker on application rollback; dropping it would restore
    # deleted tables as operational resources and lose the archive history.
    pass
