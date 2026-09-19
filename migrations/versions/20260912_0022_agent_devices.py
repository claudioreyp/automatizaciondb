"""Add agent gallery and revocable device sessions without replacing legacy data."""
import sqlalchemy as sa
from alembic import op

revision = "20260912_0022"
down_revision = "20260912_0021"
branch_labels = None
depends_on = None


def upgrade():
    additions = {
        "branches": [sa.Column("agent_name", sa.String(80)), sa.Column("agent_menu_images", sa.JSON(none_as_null=True))],
        "paired_devices": [
            sa.Column("credential_expires_at", sa.DateTime(timezone=True)),
            sa.Column("staff_session_hash", sa.String(64)),
            sa.Column("staff_session_expires_at", sa.DateTime(timezone=True)),
            sa.Column("session_staff_id", sa.Integer()),
            sa.Column("session_access_hash", sa.String(64)),
            sa.Column("failed_pin_attempts", sa.Integer(), server_default="0", nullable=False),
            sa.Column("pin_locked_until", sa.DateTime(timezone=True)),
        ],
    }
    for table, columns in additions.items():
        present = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}
        for column in columns:
            if column.name not in present:
                op.add_column(table, column)
    branches = sa.table("branches", sa.column("id", sa.Integer()), sa.column("menu_card_storage_path", sa.Text()), sa.column("agent_menu_images", sa.JSON()))
    for row in op.get_bind().execute(sa.select(branches)).mappings():
        if row["menu_card_storage_path"] and row["agent_menu_images"] is None:
            op.get_bind().execute(branches.update().where(branches.c.id == row["id"]).values(
                agent_menu_images=[{"id": "legacy", "path": row["menu_card_storage_path"]}]))


def downgrade():
    for name in ("credential_expires_at", "staff_session_hash", "staff_session_expires_at", "session_staff_id", "session_access_hash", "failed_pin_attempts", "pin_locked_until"):
        op.drop_column("paired_devices", name)
    op.drop_column("branches", "agent_name")
    op.drop_column("branches", "agent_menu_images")
