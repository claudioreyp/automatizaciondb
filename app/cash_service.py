from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .auth import AuthContext
from .errors import CodedHTTPException
from .models import (
    Branch,
    CashMovement,
    CashRegister,
    CashSession,
    Membership,
    Order,
    Payment,
    utcnow,
)
from .schemas import CashCutCreate


TWOPLACES = Decimal("0.01")
TRANSFER_METHODS = frozenset({"yape", "plin", "transfer", "online"})
ALLOWED_DENOMINATIONS = (
    Decimal("0.05"),
    Decimal("0.10"),
    Decimal("0.20"),
    Decimal("0.50"),
    Decimal("1"),
    Decimal("2"),
    Decimal("5"),
    Decimal("10"),
    Decimal("20"),
    Decimal("50"),
    Decimal("100"),
    Decimal("200"),
)
DENOMINATION_LABELS = {
    denomination: format(denomination, ".2f")
    for denomination in ALLOWED_DENOMINATIONS
}
MANUAL_MOVEMENT_TYPES = frozenset({"income", "withdrawal", "expense", "refund"})


def cash_money(value: Decimal | int | float | str | None) -> Decimal:
    return Decimal(str(value or 0)).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def actor_display_name(
    db: Session,
    user: AuthContext,
    *,
    business_id: int,
    branch_id: int,
) -> str:
    memberships = list(
        db.scalars(
            select(Membership).where(
                Membership.auth_user_id == user.user_id,
                Membership.active.is_(True),
            )
        )
    )
    membership = next(
        (
            item
            for item in memberships
            if item.business_id == business_id and item.branch_id == branch_id
        ),
        None,
    )
    membership = membership or next(
        (
            item
            for item in memberships
            if item.business_id == business_id and item.branch_id is None
        ),
        None,
    )
    membership = membership or next(
        (item for item in memberships if item.role == "superadmin"),
        None,
    )
    if membership and membership.full_name.strip():
        return membership.full_name.strip()
    if user.email:
        return user.email.strip().lower()
    stable_suffix = hashlib.sha256(user.user_id.encode("utf-8")).hexdigest()[:8]
    return f"Usuario {stable_suffix}"


def actor_name_by_id(
    db: Session,
    actor_id: str | None,
    *,
    business_id: int,
    branch_id: int,
) -> str | None:
    if not actor_id:
        return None
    memberships = list(
        db.scalars(
            select(Membership).where(
                Membership.auth_user_id == actor_id,
                Membership.active.is_(True),
            )
        )
    )
    membership = next(
        (
            item
            for item in memberships
            if item.business_id == business_id and item.branch_id == branch_id
        ),
        None,
    )
    membership = membership or next(
        (
            item
            for item in memberships
            if item.business_id == business_id and item.branch_id is None
        ),
        None,
    )
    membership = membership or next(
        (item for item in memberships if item.role == "superadmin"),
        None,
    )
    if membership and membership.full_name.strip():
        return membership.full_name.strip()
    if membership and membership.email:
        return membership.email.strip().lower()
    stable_suffix = hashlib.sha256(actor_id.encode("utf-8")).hexdigest()[:8]
    return f"Usuario {stable_suffix}"


def _lock_branch(db: Session, business_id: int, branch_id: int) -> Branch:
    branch = db.scalar(
        select(Branch)
        .where(Branch.id == branch_id, Branch.business_id == business_id)
        .with_for_update()
    )
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found")
    return branch


def lock_cash_register(
    db: Session,
    *,
    business_id: int,
    branch_id: int,
    register_id: int | None = None,
) -> CashRegister:
    if register_id is not None:
        register = db.scalar(
            select(CashRegister)
            .where(
                CashRegister.id == register_id,
                CashRegister.business_id == business_id,
                CashRegister.branch_id == branch_id,
                CashRegister.active.is_(True),
            )
            .with_for_update()
        )
        if not register:
            raise CodedHTTPException(
                404,
                "Cash register not found for this branch",
                "CASH_REGISTER_NOT_FOUND",
            )
        return register

    _lock_branch(db, business_id, branch_id)
    register = db.scalar(
        select(CashRegister)
        .where(
            CashRegister.business_id == business_id,
            CashRegister.branch_id == branch_id,
            CashRegister.active.is_(True),
            CashRegister.is_default.is_(True),
        )
        .with_for_update()
    )
    if register:
        return register

    branch_registers = list(
        db.scalars(
            select(CashRegister)
            .where(
                CashRegister.business_id == business_id,
                CashRegister.branch_id == branch_id,
            )
            .order_by(CashRegister.id)
            .with_for_update()
        )
    )
    for item in branch_registers:
        item.is_default = False
    if branch_registers:
        db.flush()

    candidates = [item for item in branch_registers if item.active]
    if candidates:
        register = next(
            (
                item
                for item in candidates
                if item.name.strip().lower() in {"principal", "caja principal"}
            ),
            candidates[0],
        )
        register.is_default = True
        db.flush()
        return register

    register = CashRegister(
        business_id=business_id,
        branch_id=branch_id,
        name="Caja Principal",
        active=True,
        is_default=True,
    )
    db.add(register)
    db.flush()
    return register


def scoped_cash_register(
    db: Session,
    user: AuthContext,
    register_id: int,
    *,
    for_update: bool = False,
) -> CashRegister:
    statement = select(CashRegister).where(CashRegister.id == register_id)
    if not user.is_superadmin:
        if user.business_id is None:
            raise CodedHTTPException(404, "Cash register not found", "CASH_REGISTER_NOT_FOUND")
        statement = statement.where(CashRegister.business_id == user.business_id)
        if user.branch_id is not None:
            statement = statement.where(CashRegister.branch_id == user.branch_id)
    if for_update:
        statement = statement.with_for_update()
    register = db.scalar(statement)
    if not register:
        raise CodedHTTPException(404, "Cash register not found", "CASH_REGISTER_NOT_FOUND")
    return register


def _latest_closed_session(db: Session, register_id: int) -> CashSession | None:
    return db.scalar(
        select(CashSession)
        .where(
            CashSession.register_id == register_id,
            CashSession.status == "closed",
        )
        .order_by(CashSession.closed_at.desc(), CashSession.id.desc())
        .limit(1)
    )


def open_or_get_cash_session(
    db: Session,
    register: CashRegister,
    *,
    actor_id: str,
) -> tuple[CashSession, bool]:
    session = db.scalar(
        select(CashSession)
        .where(
            CashSession.register_id == register.id,
            CashSession.status == "open",
        )
        .with_for_update()
    )
    if session:
        return session, False

    previous = _latest_closed_session(db, register.id)
    session = CashSession(
        business_id=register.business_id,
        branch_id=register.branch_id,
        register_id=register.id,
        previous_session_id=previous.id if previous else None,
        opening_amount=cash_money(previous.retained_fund_amount if previous else 0),
        opened_by=actor_id,
        version=1,
    )
    db.add(session)
    db.flush()
    return session, True


def resolve_payment_cash_session(
    db: Session,
    user: AuthContext,
    order: Order,
    *,
    cash_session_id: int | None,
    register_id: int | None,
) -> CashSession:
    if cash_session_id is not None:
        candidate = db.scalar(
            select(CashSession).where(
                CashSession.id == cash_session_id,
                CashSession.business_id == order.business_id,
                CashSession.branch_id == order.branch_id,
            )
        )
        if not candidate:
            raise CodedHTTPException(
                422,
                "Cash period is not available for this branch",
                "CASH_SESSION_INVALID",
            )
        if register_id is not None and candidate.register_id != register_id:
            raise CodedHTTPException(
                422,
                "Cash period does not belong to the selected register",
                "CASH_SESSION_REGISTER_MISMATCH",
            )
        register = lock_cash_register(
            db,
            business_id=order.business_id,
            branch_id=order.branch_id,
            register_id=candidate.register_id,
        )
        session = db.scalar(
            select(CashSession)
            .where(
                CashSession.id == cash_session_id,
                CashSession.register_id == register.id,
                CashSession.status == "open",
            )
            .with_for_update()
        )
        if not session:
            raise CodedHTTPException(
                422,
                "Cash period is no longer open",
                "CASH_SESSION_CLOSED",
            )
        return session

    if register_id is None:
        # Serialize automatic attribution at branch level. A payment may only
        # choose a period implicitly when there is at most one open period.
        _lock_branch(db, order.business_id, order.branch_id)
        open_sessions = list(
            db.scalars(
                select(CashSession)
                .join(CashRegister, CashRegister.id == CashSession.register_id)
                .where(
                    CashSession.business_id == order.business_id,
                    CashSession.branch_id == order.branch_id,
                    CashSession.status == "open",
                    CashRegister.active.is_(True),
                )
                .order_by(CashSession.id)
                .with_for_update()
            )
        )
        if len(open_sessions) > 1:
            raise CodedHTTPException(
                409,
                "Select a cash register because this branch has multiple open periods",
                "CASH_REGISTER_AMBIGUOUS",
            )
        if open_sessions:
            register_id = open_sessions[0].register_id

    register = lock_cash_register(
        db,
        business_id=order.business_id,
        branch_id=order.branch_id,
        register_id=register_id,
    )
    session, _ = open_or_get_cash_session(db, register, actor_id=user.user_id)
    return session


def cash_reconciliation(db: Session, session: CashSession) -> dict:
    payment_rows = db.execute(
        select(Payment.method, func.coalesce(func.sum(Payment.amount), 0))
        .where(
            Payment.cash_session_id == session.id,
            Payment.status == "confirmed",
        )
        .group_by(Payment.method)
    ).all()
    payment_totals = {
        method: cash_money(amount)
        for method, amount in payment_rows
    }
    movements = list(
        db.scalars(
            select(CashMovement).where(
                CashMovement.cash_session_id == session.id,
                CashMovement.movement_type.in_(MANUAL_MOVEMENT_TYPES),
            )
        )
    )
    cash_expected = cash_money(session.opening_amount) + payment_totals.get(
        "cash",
        Decimal("0"),
    )
    for movement in movements:
        if movement.movement_type == "income":
            cash_expected += cash_money(movement.amount)
        else:
            cash_expected -= cash_money(movement.amount)
    card_expected = payment_totals.get("card", Decimal("0"))
    transfer_expected = cash_money(
        sum(
            (payment_totals.get(method, Decimal("0")) for method in TRANSFER_METHODS),
            Decimal("0"),
        )
    )
    return {
        "cash_expected": cash_money(cash_expected),
        "card_expected": cash_money(card_expected),
        "transfer_expected": transfer_expected,
        "payment_count": sum(1 for amount in payment_totals.values() if amount != 0),
        "movement_count": len(movements),
        "has_activity": bool(payment_totals or movements),
        "has_card_activity": card_expected > 0,
    }


def pending_orders_snapshot(
    db: Session,
    *,
    business_id: int,
    branch_id: int,
    before: datetime,
) -> list[dict]:
    orders = list(
        db.scalars(
            select(Order)
            .where(
                Order.business_id == business_id,
                Order.branch_id == branch_id,
                Order.status.not_in(["draft", "cancelled"]),
                Order.created_at <= before,
                Order.total > 0,
            )
            .order_by(Order.created_at, Order.id)
        )
    )
    if not orders:
        return []
    paid_rows = db.execute(
        select(Payment.order_id, func.coalesce(func.sum(Payment.amount), 0))
        .where(
            Payment.order_id.in_([order.id for order in orders]),
            Payment.status == "confirmed",
        )
        .group_by(Payment.order_id)
    ).all()
    paid_by_order = {
        order_id: cash_money(amount)
        for order_id, amount in paid_rows
    }
    snapshot: list[dict] = []
    for order in orders:
        total = cash_money(order.total)
        paid = paid_by_order.get(order.id, Decimal("0"))
        if paid >= total:
            continue
        snapshot.append(
            {
                "id": order.id,
                "number": order.number,
                "status": order.status,
                "payment_status": order.payment_status,
                "total": float(total),
                "paid": float(paid),
                "balance": float(cash_money(total - paid)),
                "created_at": order.created_at.isoformat(),
            }
        )
    return snapshot


def normalize_denominations(
    denominations: dict[str, int] | None,
    cash_counted: Decimal,
) -> dict[str, int] | None:
    if denominations is None:
        return None
    normalized: dict[str, int] = {}
    total = Decimal("0")
    for raw_value, count in denominations.items():
        try:
            denomination = Decimal(str(raw_value))
        except Exception as exc:
            raise CodedHTTPException(
                422,
                f"Unsupported cash denomination: {raw_value}",
                "CASH_CUT_INVALID_DENOMINATION",
            ) from exc
        if denomination not in DENOMINATION_LABELS:
            raise CodedHTTPException(
                422,
                f"Unsupported cash denomination: {raw_value}",
                "CASH_CUT_INVALID_DENOMINATION",
            )
        label = DENOMINATION_LABELS[denomination]
        if label in normalized:
            raise CodedHTTPException(
                422,
                f"Cash denomination was supplied more than once: {raw_value}",
                "CASH_CUT_INVALID_DENOMINATION",
            )
        normalized[label] = count
        total += denomination * count
    if cash_money(total) != cash_money(cash_counted):
        raise CodedHTTPException(
            422,
            "Cash denomination total must equal the counted cash amount",
            "CASH_CUT_DENOMINATION_MISMATCH",
        )
    return normalized


def close_cash_cut(
    db: Session,
    user: AuthContext,
    register: CashRegister,
    payload: CashCutCreate,
) -> tuple[CashSession, CashSession]:
    session = db.scalar(
        select(CashSession)
        .where(
            CashSession.register_id == register.id,
            CashSession.status == "open",
        )
        .with_for_update()
    )
    if session is None:
        if payload.expected_version != 0:
            raise CodedHTTPException(
                409,
                "Cash period changed on another terminal",
                "CASH_CUT_STALE",
            )
        session, _ = open_or_get_cash_session(db, register, actor_id=user.user_id)
    elif session.version != payload.expected_version:
        raise CodedHTTPException(
            409,
            "Cash period changed on another terminal",
            "CASH_CUT_STALE",
        )

    reconciliation = cash_reconciliation(db, session)
    if not reconciliation["has_activity"]:
        raise CodedHTTPException(
            409,
            "No sales or cash movements were recorded in this period",
            "CASH_CUT_NO_ACTIVITY",
        )

    now = utcnow()
    pending_orders = pending_orders_snapshot(
        db,
        business_id=session.business_id,
        branch_id=session.branch_id,
        before=now,
    )
    if pending_orders and not payload.ignore_pending_orders:
        raise CodedHTTPException(
            409,
            "Pending orders must be paid or explicitly ignored before closing the cut",
            "CASH_CUT_PENDING_ORDERS",
        )
    if pending_orders and user.role not in {"superadmin", "owner", "manager", "cashier"}:
        raise CodedHTTPException(
            403,
            "This role cannot ignore pending orders",
            "CASH_CUT_OVERRIDE_FORBIDDEN",
        )

    cash_counted = cash_money(payload.cash_counted)
    retained_fund = cash_money(payload.retained_fund)
    if retained_fund > cash_counted:
        raise CodedHTTPException(
            422,
            "Retained fund cannot exceed counted cash",
            "CASH_CUT_INVALID_RETAINED_FUND",
        )
    denominations = normalize_denominations(payload.denominations, cash_counted)

    card_expected = reconciliation["card_expected"]
    if card_expected > 0 and payload.card_counted is None:
        raise CodedHTTPException(
            422,
            "Counted card payments are required for this period",
            "CASH_CUT_CARD_COUNT_REQUIRED",
        )
    card_counted = cash_money(payload.card_counted)
    if card_expected == 0 and card_counted != 0:
        raise CodedHTTPException(
            422,
            "Card count is not applicable because this period has no card payments",
            "CASH_CUT_CARD_NOT_APPLICABLE",
        )

    cash_expected = reconciliation["cash_expected"]
    transfer_expected = reconciliation["transfer_expected"]
    cash_difference = cash_money(cash_counted - cash_expected)
    card_difference = cash_money(card_counted - card_expected)
    total_expected = cash_money(cash_expected + card_expected + transfer_expected)
    total_difference = cash_money(cash_difference + card_difference)
    result = (
        "balanced"
        if total_difference == 0
        else "surplus"
        if total_difference > 0
        else "shortage"
    )
    display_name = actor_display_name(
        db,
        user,
        business_id=session.business_id,
        branch_id=session.branch_id,
    )

    session.expected_amount = cash_expected
    session.declared_amount = cash_counted
    session.difference = cash_difference
    session.card_expected_amount = card_expected
    session.card_declared_amount = card_counted
    session.card_difference = card_difference
    session.transfer_expected_amount = transfer_expected
    session.total_expected_amount = total_expected
    session.total_difference = total_difference
    session.retained_fund_amount = retained_fund
    session.cash_withdrawn_amount = cash_money(cash_counted - retained_fund)
    session.result = result
    session.denominations = denominations
    session.pending_orders_snapshot = pending_orders
    session.pending_orders_ignored = bool(pending_orders and payload.ignore_pending_orders)
    session.pending_orders_override_by = user.user_id if session.pending_orders_ignored else None
    session.pending_orders_override_at = now if session.pending_orders_ignored else None
    session.actor_display_name = display_name
    session.status = "closed"
    session.closed_at = now
    session.closed_by = user.user_id
    session.close_notes = payload.note
    session.version += 1
    db.flush()

    next_session = CashSession(
        business_id=session.business_id,
        branch_id=session.branch_id,
        register_id=session.register_id,
        previous_session_id=session.id,
        opening_amount=retained_fund,
        opened_by=user.user_id,
        version=1,
    )
    db.add(next_session)
    db.flush()
    return session, next_session


def cut_preview(db: Session, register: CashRegister) -> dict:
    session = db.scalar(
        select(CashSession).where(
            CashSession.register_id == register.id,
            CashSession.status == "open",
        )
    )
    previous = _latest_closed_session(db, register.id)
    if session:
        reconciliation = cash_reconciliation(db, session)
        opening_amount = cash_money(session.opening_amount)
        started_at = session.opened_at
        session_id = session.id
        version = session.version
    else:
        reconciliation = {
            "transfer_expected": Decimal("0"),
            "has_card_activity": False,
        }
        opening_amount = cash_money(previous.retained_fund_amount if previous else 0)
        started_at = previous.closed_at if previous else None
        session_id = None
        version = 0
    pending_orders = pending_orders_snapshot(
        db,
        business_id=register.business_id,
        branch_id=register.branch_id,
        before=utcnow(),
    )
    return {
        "register": {
            "id": register.id,
            "branch_id": register.branch_id,
            "name": register.name,
            "is_default": register.is_default,
        },
        "session_id": session_id,
        "version": version,
        "period_started_at": started_at,
        "opening_fund": float(opening_amount),
        "has_card_activity": reconciliation["has_card_activity"],
        "transfer_expected_amount": float(reconciliation["transfer_expected"]),
        "pending_orders": pending_orders,
        "pending_order_count": len(pending_orders),
    }


def _session_result(session: CashSession) -> str:
    if session.result:
        return session.result
    difference = cash_money(session.total_difference if session.total_difference is not None else session.difference)
    if difference == 0:
        return "balanced"
    return "surplus" if difference > 0 else "shortage"


def cut_history_item(
    db: Session,
    session: CashSession,
    register: CashRegister,
) -> dict:
    total_expected = cash_money(session.total_expected_amount)
    if session.result is None:
        total_expected = cash_money(
            session.expected_amount
            + session.card_expected_amount
            + session.transfer_expected_amount
        )
    display_name = session.actor_display_name or actor_name_by_id(
        db,
        session.closed_by,
        business_id=session.business_id,
        branch_id=session.branch_id,
    )
    return {
        "id": session.id,
        "number": session.id,
        "branch_id": session.branch_id,
        "register": {"id": register.id, "name": register.name},
        "created_by": display_name,
        "result": _session_result(session),
        "total_expected_amount": float(total_expected),
        "retained_fund_amount": float(cash_money(session.retained_fund_amount)),
        "closed_at": session.closed_at,
    }


def cash_cut_detail(
    db: Session,
    session: CashSession,
    register: CashRegister,
) -> dict:
    payments = db.execute(
        select(Payment, Order.number)
        .join(Order, Order.id == Payment.order_id)
        .where(
            Payment.cash_session_id == session.id,
            Payment.status == "confirmed",
            Order.business_id == session.business_id,
            Order.branch_id == session.branch_id,
        )
        .order_by(Payment.received_at, Payment.id)
    ).all()
    movements = list(
        db.scalars(
            select(CashMovement)
            .where(
                CashMovement.cash_session_id == session.id,
                CashMovement.movement_type.in_(MANUAL_MOVEMENT_TYPES),
            )
            .order_by(CashMovement.created_at, CashMovement.id)
        )
    )
    transactions = {"cash": [], "card": [], "transfer": []}
    for payment, order_number in payments:
        group = (
            "cash"
            if payment.method == "cash"
            else "card"
            if payment.method == "card"
            else "transfer"
        )
        transactions[group].append(
            {
                "id": payment.id,
                "kind": "payment",
                "method": payment.method,
                "amount": float(cash_money(payment.amount)),
                "order_id": payment.order_id,
                "order_number": order_number,
                "reference": payment.external_reference,
                "note": payment.note,
                "created_by": actor_name_by_id(
                    db,
                    payment.created_by,
                    business_id=session.business_id,
                    branch_id=session.branch_id,
                ),
                "created_at": payment.received_at,
            }
        )
    for movement in movements:
        transactions["cash"].append(
            {
                "id": movement.id,
                "kind": "movement",
                "movement_type": movement.movement_type,
                "amount": float(cash_money(movement.amount)),
                "note": movement.note,
                "created_by": actor_name_by_id(
                    db,
                    movement.created_by,
                    business_id=session.business_id,
                    branch_id=session.branch_id,
                ),
                "created_at": movement.created_at,
            }
        )

    history = cut_history_item(db, session, register)
    total_expected = cash_money(history["total_expected_amount"])
    total_difference = cash_money(
        session.total_difference if session.total_difference is not None else session.difference
    )
    return {
        **history,
        "status": session.status,
        "previous_session_id": session.previous_session_id,
        "period_started_at": session.opened_at,
        "opening_amount": float(cash_money(session.opening_amount)),
        "cash_withdrawn_amount": float(cash_money(session.cash_withdrawn_amount)),
        "total_expected_amount": float(total_expected),
        "total_difference": float(total_difference),
        "denominations": session.denominations,
        "pending_orders": session.pending_orders_snapshot or [],
        "pending_orders_ignored": session.pending_orders_ignored,
        "pending_orders_override_at": session.pending_orders_override_at,
        "notes": session.close_notes,
        "methods": [
            {
                "key": "cash",
                "label": "Efectivo",
                "counted": float(cash_money(session.declared_amount)),
                "expected": float(cash_money(session.expected_amount)),
                "difference": float(cash_money(session.difference)),
                "transactions": transactions["cash"],
            },
            {
                "key": "card",
                "label": "Tarjeta",
                "counted": float(cash_money(session.card_declared_amount)),
                "expected": float(cash_money(session.card_expected_amount)),
                "difference": float(cash_money(session.card_difference)),
                "transactions": transactions["card"],
            },
            {
                "key": "transfer",
                "label": "Transferencias",
                "counted": None,
                "expected": float(cash_money(session.transfer_expected_amount)),
                "difference": None,
                "transactions": transactions["transfer"],
            },
        ],
    }
