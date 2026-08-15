"""Allow only one active global superadmin.

Revision ID: 20260815_0004
Revises: 20260815_0003
"""

from alembic import op
import sqlalchemy as sa


revision = "20260815_0004"
down_revision = "20260815_0003"
branch_labels = None
depends_on = None


INDEX_NAME = "uq_memberships_single_active_superadmin"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_indexes = {index["name"] for index in inspector.get_indexes("memberships")}
    if INDEX_NAME in existing_indexes:
        return

    active_ids = list(
        bind.execute(
            sa.text(
                """
                SELECT id
                FROM memberships
                WHERE role = 'superadmin'
                  AND business_id IS NULL
                  AND branch_id IS NULL
                  AND active = true
                ORDER BY updated_at DESC, id DESC
                """
            )
        ).scalars()
    )
    if len(active_ids) > 1:
        bind.execute(
            sa.text("UPDATE memberships SET active = false WHERE id IN :ids").bindparams(
                sa.bindparam("ids", expanding=True)
            ),
            {"ids": active_ids[1:]},
        )

    dialect = bind.dialect.name
    op.create_index(
        INDEX_NAME,
        "memberships",
        ["role"],
        unique=True,
        sqlite_where=sa.text(
            "role = 'superadmin' AND business_id IS NULL AND branch_id IS NULL AND active = 1"
        )
        if dialect == "sqlite"
        else None,
        postgresql_where=sa.text(
            "role = 'superadmin' AND business_id IS NULL AND branch_id IS NULL AND active IS TRUE"
        )
        if dialect == "postgresql"
        else None,
    )


def downgrade() -> None:
    existing_indexes = {
        index["name"] for index in sa.inspect(op.get_bind()).get_indexes("memberships")
    }
    if INDEX_NAME in existing_indexes:
        op.drop_index(INDEX_NAME, table_name="memberships")
