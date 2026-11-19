"""Add one optional private menu PDF without changing the image gallery."""
from alembic import op
import sqlalchemy as sa

revision = "20261119_0030"
down_revision = "20261020_0029"
branch_labels = None
depends_on = None


def upgrade():
    if "agent_menu_pdf" not in {column["name"] for column in sa.inspect(op.get_bind()).get_columns("branches")}:
        op.add_column("branches", sa.Column("agent_menu_pdf", sa.JSON(none_as_null=True), nullable=True))


def downgrade():
    op.drop_column("branches", "agent_menu_pdf")
