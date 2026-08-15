from sqlalchemy import select

from app.database import SessionLocal
from app.models import Membership
from scripts.bootstrap_superadmin import bootstrap


def test_bootstrap_keeps_exactly_one_active_global_superadmin():
    bootstrap("first-admin", "first@example.com", "First Admin")
    bootstrap("canonical-admin", "admin@escalar.ai", "Escalar AI")

    with SessionLocal() as db:
        memberships = list(
            db.scalars(
                select(Membership).where(
                    Membership.business_id.is_(None),
                    Membership.branch_id.is_(None),
                    Membership.role == "superadmin",
                )
            )
        )

    active = [membership for membership in memberships if membership.active]
    assert len(active) == 1
    assert active[0].auth_user_id == "canonical-admin"
    assert active[0].email == "admin@escalar.ai"
