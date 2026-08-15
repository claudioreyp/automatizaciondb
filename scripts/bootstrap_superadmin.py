"""Create or update the global superadmin membership for an existing Supabase user."""

import argparse

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Membership


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Link an existing Supabase Auth user to the Impulsa superadmin role."
    )
    parser.add_argument("--auth-user-id", required=True, help="Supabase Auth user UUID")
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", default="Superadmin Impulsa")
    return parser.parse_args()


def bootstrap(auth_user_id: str, email: str, name: str) -> None:
    normalized_email = email.strip().lower()
    with SessionLocal.begin() as db:
        existing_superadmins = list(
            db.scalars(
                select(Membership).where(
                    Membership.business_id.is_(None),
                    Membership.branch_id.is_(None),
                    Membership.role == "superadmin",
                )
            )
        )
        disabled = 0
        for existing in existing_superadmins:
            if existing.auth_user_id != auth_user_id and existing.active:
                existing.active = False
                disabled += 1
        db.flush()

        membership = db.scalar(
            select(Membership).where(
                Membership.auth_user_id == auth_user_id,
                Membership.business_id.is_(None),
                Membership.branch_id.is_(None),
            )
        )
        if membership:
            membership.email = normalized_email
            membership.full_name = name
            membership.role = "superadmin"
            membership.active = True
            action = "updated"
        else:
            db.add(
                Membership(
                    auth_user_id=auth_user_id,
                    email=normalized_email,
                    full_name=name,
                    business_id=None,
                    branch_id=None,
                    role="superadmin",
                    active=True,
                )
            )
            action = "created"
    print(
        f"Superadmin membership {action} for {normalized_email}; "
        f"disabled_previous={disabled}"
    )


if __name__ == "__main__":
    arguments = parse_args()
    bootstrap(arguments.auth_user_id, arguments.email, arguments.name)
