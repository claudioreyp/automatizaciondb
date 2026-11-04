from sqlalchemy import update
from sqlalchemy.orm import Session

from .models import Business


def parse_order_folio(value: str) -> int | None:
    digits = value.strip().removeprefix("#")
    if not digits.isascii() or not digits.isdecimal() or len(digits) > 10:
        return None
    folio = int(digits)
    return folio if 0 < folio <= 2147483647 else None


def reserve_order_folio(db: Session, business_id: int) -> int:
    # UPDATE serializes allocation in both PostgreSQL and SQLite; rollback also
    # rolls back the reservation, unlike a process-local counter or MAX + 1.
    return db.execute(
        update(Business)
        .where(Business.id == business_id)
        .values(
            order_folio_counter=Business.order_folio_counter + 1,
            updated_at=Business.updated_at,
        )
        .returning(Business.order_folio_counter)
        .execution_options(synchronize_session=False)
    ).scalar_one()
