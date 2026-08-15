"""Secure owner and employee invitations.

Revision ID: 20260717_0002
Revises: 20260717_0001
"""

from alembic import op

from app.models import Invitation


revision = "20260717_0002"
down_revision = "20260717_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Invitation.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    Invitation.__table__.drop(bind=op.get_bind(), checkfirst=True)
