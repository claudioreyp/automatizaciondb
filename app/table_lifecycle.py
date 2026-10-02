"""Operational table locks and archive guards; historical rows remain intact."""
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .errors import CodedHTTPException
from .models import DiningArea, Order, Reservation, ReservationTable, RestaurantTable, utcnow


def lock_rows(db: Session, statement, model):
    if db.get_bind().dialect.name == "sqlite":
        # SQLite ignores FOR UPDATE. Take its write lock before reading current state.
        db.execute(update(model).where(statement.whereclause).values(
            updated_at=model.updated_at,
        ).execution_options(synchronize_session=False))
    return list(db.scalars(statement.with_for_update().execution_options(populate_existing=True)))


def lock_tables(db: Session, table_ids, business_id: int, branch_id: int):
    if not table_ids:
        return []
    return lock_rows(db, select(RestaurantTable).where(
        RestaurantTable.id.in_(table_ids),
        RestaurantTable.business_id == business_id,
        RestaurantTable.branch_id == branch_id,
    ).order_by(RestaurantTable.id), RestaurantTable)


def active_table_clause():
    return RestaurantTable.archived_at.is_(None) & ~select(DiningArea.id).where(
            DiningArea.id == RestaurantTable.area_id,
            DiningArea.business_id == RestaurantTable.business_id,
            DiningArea.branch_id == RestaurantTable.branch_id,
            DiningArea.archived_at.is_not(None),
        ).exists()


def ensure_tables_active(db: Session, tables) -> None:
    ids = {table.id for table in tables}
    if ids and set(db.scalars(select(RestaurantTable.id).where(
        RestaurantTable.id.in_(ids), active_table_clause(),
    ))) != ids:
        raise CodedHTTPException(409, "Table has been archived", "TABLE_ARCHIVED")


def ensure_tables_archivable(db: Session, tables) -> None:
    # Callers hold the table locks. Opening, transferring and reserving take the
    # same locks, so no new operation can appear between these checks and commit.
    ids = [table.id for table in tables]
    if not ids:
        return
    if db.scalar(select(Order.id).where(
        Order.table_id.in_(ids),
        Order.business_id == tables[0].business_id,
        Order.branch_id == tables[0].branch_id,
        Order.status.not_in(["closed", "cancelled", "delivered"]),
        Order.table_released_at.is_(None),
    ).limit(1)) is not None:
        raise CodedHTTPException(409, "Close the table account before archiving", "TABLE_HAS_ACTIVE_ORDER")
    if db.scalar(select(Reservation.id).join(
        ReservationTable, ReservationTable.reservation_id == Reservation.id,
    ).where(
        ReservationTable.table_id.in_(ids),
        Reservation.business_id == tables[0].business_id,
        Reservation.branch_id == tables[0].branch_id,
        Reservation.status.in_(["confirmed", "seated"]),
        Reservation.end_at > utcnow(),
    ).limit(1)) is not None:
        raise CodedHTTPException(409, "Resolve the table reservations before archiving", "TABLE_HAS_ACTIVE_RESERVATION")
