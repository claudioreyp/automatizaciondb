"""Operational local-POS queue, separate from manager/device printing contracts."""

from datetime import timedelta
import secrets
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from .auth import AuthContext, get_current_user
from .database import get_db
from .models import AuditEvent, BranchSettings, IdempotencyRecord, KitchenTicket, Order, PrintJob, utcnow
from .pos_printing import (
    OPERATIONAL_ROLES, TRANSPORT, PosPrintClaim, PosPrintClaimResponse, PosPrintComplete, PosPrintCreate,
    PosPrintJob, PosPrintingResponse, PosQzResponse, digest, invalidate_stale_job, is_local_job,
    public_job, recover_pos_print_jobs, build_print_payload, content_digest, local_printer_config,
    checkout_receipt_context,
)
from .settings_service import effective_roles, scoped_branch
from .qz_signing import qz_connection_settings


def _no_store(response: Response):
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(tags=["POS printing"], dependencies=[Depends(_no_store)])


def _order(db: Session, user: AuthContext, order_id: int, *, discovery: bool = False) -> Order:
    statement = select(Order).where(Order.id == order_id)
    if not user.is_superadmin:
        statement = statement.where(Order.business_id == user.business_id)
        if user.branch_id is not None:
            statement = statement.where(Order.branch_id == user.branch_id)
    order = db.scalar(statement)
    if order is None:
        raise HTTPException(404, "Order not found")
    scoped_branch(db, user, order.branch_id)
    roles = OPERATIONAL_ROLES | {"kitchen"} if discovery else OPERATIONAL_ROLES
    if not effective_roles(db, user, order.business_id).intersection(roles):
        raise HTTPException(403, "Insufficient POS printing permission")
    if discovery:
        return order
    # SQLite has no FOR UPDATE. Take its write lock before loading any queue state.
    if db.get_bind().dialect.name == "sqlite":
        db.execute(update(Order).where(Order.id == order.id).values(
            version=Order.version, updated_at=Order.updated_at,
        ).execution_options(synchronize_session=False))
    return db.scalar(statement.with_for_update().options(selectinload(Order.items))
                     .execution_options(populate_existing=True))


def _jobs(db: Session, order: Order) -> tuple[list[PrintJob], bool]:
    tickets = list(db.scalars(select(KitchenTicket).where(
        KitchenTicket.business_id == order.business_id,
        KitchenTicket.branch_id == order.branch_id, KitchenTicket.order_id == order.id,
    ).order_by(KitchenTicket.sequence)))
    recovered = recover_pos_print_jobs(db, order, tickets)
    rows = list(db.scalars(select(PrintJob).where(
        PrintJob.business_id == order.business_id, PrintJob.branch_id == order.branch_id,
        PrintJob.order_id == order.id, PrintJob.paired_device_id.is_(None),
        PrintJob.printer_id.is_(None),
    ).order_by(PrintJob.created_at, PrintJob.id).with_for_update()
        .execution_options(populate_existing=True)))
    jobs = [job for job in rows if is_local_job(job)]
    for job in jobs:
        invalidate_stale_job(db, order, job)
    return jobs, recovered


def _job(db: Session, order: Order, job_id: str) -> PrintJob:
    jobs, recovered = _jobs(db, order)
    job = next((row for row in jobs if row.id == job_id), None)
    if job is None:
        raise HTTPException(404 if recovered else 503,
                            "Print job not found" if recovered else "Printing queue unavailable; retry")
    return job


def _owner(user: AuthContext, terminal_id) -> str:
    return digest([user.user_id, user.staff_member_id, user.device_id, str(terminal_id)])


def _idempotency(db: Session, user: AuthContext, job: PrintJob, payload,
                 key: str | None, action: str) -> bool:
    if not key or not key.strip() or len(key) > 180:
        raise HTTPException(422, "Idempotency-Key is required (maximum 180 characters)")
    scope = f"pos-print:{job.id}:{action}:{_owner(user, payload.terminal_id)}"
    fingerprint = digest(payload.model_dump(mode="json"))
    record = db.scalar(select(IdempotencyRecord).where(
        IdempotencyRecord.business_id == job.business_id,
        IdempotencyRecord.scope == scope, IdempotencyRecord.idempotency_key == key,
    ))
    if record:
        if record.response_body.get("request_hash") != fingerprint:
            raise HTTPException(409, "Idempotency-Key was used for a different printing request")
        return True
    # Store only the request hash. Never cache a dispatch authorization or token.
    db.add(IdempotencyRecord(
        business_id=job.business_id, scope=scope, idempotency_key=key,
        response_body={"request_hash": fingerprint}, expires_at=utcnow() + timedelta(days=7),
    ))
    return False


def _audit(db: Session, user: AuthContext, job: PrintJob, action: str) -> None:
    db.add(AuditEvent(
        business_id=job.business_id, branch_id=job.branch_id, actor_id=user.user_id,
        action=f"pos.print_job.{action}", entity_type="print_job",
        entity_id=job.id, payload={"order_id": job.order_id, "job_type": job.job_type,
                                   "attempt": job.attempts},
    ))


@router.get("/orders/{order_id}/printing", response_model=PosPrintingResponse)
def get_order_printing(order_id: int, user: AuthContext = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    """Recover committed intents, cancel stale work and return only local-POS jobs."""
    order = _order(db, user, order_id)
    jobs, recovered = _jobs(db, order)
    response = {"order_id": order.id, "items": [public_job(job) for job in jobs],
                "recoverable_error": not recovered}
    db.commit()
    return response


@router.get("/orders/{order_id}/printing/qz", response_model=PosQzResponse)
def get_order_qz(order_id: int, user: AuthContext = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    _order(db, user, order_id, discovery=True)
    return qz_connection_settings()


@router.post("/orders/{order_id}/printing", response_model=PosPrintJob, status_code=201)
def create_order_printing(order_id: int, payload: PosPrintCreate,
                          idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                          user: AuthContext = Depends(get_current_user), db: Session = Depends(get_db)):
    """Request one intentional print/reprint; technical retries keep its identity."""
    order = _order(db, user, order_id)
    if not idempotency_key or not idempotency_key.strip() or len(idempotency_key) > 180:
        raise HTTPException(422, "Idempotency-Key is required (maximum 180 characters)")
    request_hash = digest(payload.model_dump(mode="json"))
    key = f"pos-manual:{order.id}:{digest([user.user_id, user.staff_member_id, user.device_id, idempotency_key])}"
    existing = db.scalar(select(PrintJob).where(
        PrintJob.business_id == order.business_id, PrintJob.branch_id == order.branch_id,
        PrintJob.order_id == order.id, PrintJob.idempotency_key == key,
    ).with_for_update())
    if existing:
        if existing.payload.get("_request_hash") != request_hash:
            raise HTTPException(409, "Idempotency-Key was used for a different printing request")
        invalidate_stale_job(db, order, existing)
        response = public_job(existing)
        db.commit()
        return response
    if order.version != payload.expected_order_version:
        raise HTTPException(409, "Order changed; refresh before requesting a print")
    if order.status == "cancelled" and payload.job_type != "customer_receipt":
        raise HTTPException(409, "Cancelled orders cannot dispatch kitchen tickets")
    settings = db.scalar(select(BranchSettings).where(
        BranchSettings.business_id == order.business_id, BranchSettings.branch_id == order.branch_id,
    ))
    config = local_printer_config(settings)
    if config is None:
        raise HTTPException(409, "Enable advanced printing and select a printer first")
    tickets = select(KitchenTicket).where(
        KitchenTicket.business_id == order.business_id, KitchenTicket.branch_id == order.branch_id,
        KitchenTicket.order_id == order.id,
    )
    if payload.job_type == "kitchen_ticket":
        ticket = db.scalar(tickets.where(KitchenTicket.id == payload.kitchen_ticket_id).with_for_update())
        if ticket is None:
            raise HTTPException(404, "Kitchen ticket not found for this order")
        if ticket.version != payload.expected_ticket_version or ticket.status == "cancelled":
            raise HTTPException(409, "Kitchen ticket changed or was cancelled; refresh before printing")
        template = settings.kitchen_ticket_template
    else:
        ticket = db.scalar(tickets.order_by(KitchenTicket.sequence, KitchenTicket.id).limit(1))
        template = settings.customer_ticket_template
    snapshot = build_print_payload(db, order, ticket, config, payload.job_type, template or {},
        snapshot_context=checkout_receipt_context(db, order) if payload.job_type == "customer_receipt" else None)
    job = PrintJob(
        id=str(uuid4()), business_id=order.business_id, branch_id=order.branch_id,
        order_id=order.id, kitchen_ticket_id=payload.kitchen_ticket_id,
        job_type=payload.job_type, idempotency_key=key,
        payload={**snapshot, "_transport": TRANSPORT, "_request_hash": request_hash,
                 "_manual": True, "_guard": {
                     "order": content_digest(snapshot["order"], kitchen=payload.job_type == "kitchen_ticket"),
                     "ticket_version": ticket.version if ticket else None,
                 }},
    )
    db.add(job)
    db.flush()
    _audit(db, user, job, "manually_requested")
    response = public_job(job)
    db.commit()
    return response


@router.post("/orders/{order_id}/printing/{job_id}/claim", response_model=PosPrintClaimResponse)
def claim_order_printing(order_id: int, job_id: str, payload: PosPrintClaim,
                         idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                         user: AuthContext = Depends(get_current_user), db: Session = Depends(get_db)):
    """One dispatch permit, never reissued on replay, timeout or uncertain outcome."""
    order = _order(db, user, order_id)
    job = _job(db, order, job_id)
    if job.status == "cancelled":
        db.commit()
        raise HTTPException(409, "Print job is cancelled or stale")
    owner = _owner(user, payload.terminal_id)
    token_hash = digest(str(payload.claim_token))
    previous_claim = job.payload.get("_claim", {})
    replay = _idempotency(db, user, job, payload, idempotency_key, "claim")
    if replay:
        response = {"job": public_job(job), "dispatch_allowed": False}
        db.commit()
        return response
    if job.status == "claimed":
        if previous_claim == {"owner": owner, "token_hash": token_hash}:
            response = {"job": public_job(job), "dispatch_allowed": False}
            db.commit()
            return response
        raise HTTPException(409, "Print job already claimed; dispatch outcome may be unknown")
    retry = (job.status == "failed" and job.payload.get("_outcome") == "not_sent"
             and payload.retry_not_sent and token_hash != previous_claim.get("token_hash"))
    if job.status != "pending" and not retry:
        raise HTTPException(409, "Print job cannot be dispatched again")
    job.payload = {**job.payload, "_claim": {"owner": owner, "token_hash": token_hash},
                   "_outcome": None}
    job.status = "claimed"
    job.claimed_at = utcnow()
    job.attempts += 1
    job.error_message = None
    job.failed_at = None
    _audit(db, user, job, "claimed")
    db.flush()
    response = {"job": public_job(job), "dispatch_allowed": True}
    db.commit()
    return response


@router.post("/orders/{order_id}/printing/{job_id}/complete", response_model=PosPrintJob)
def complete_order_printing(order_id: int, job_id: str, payload: PosPrintComplete,
                            idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                            user: AuthContext = Depends(get_current_user), db: Session = Depends(get_db)):
    """Acknowledge QZ/spooler submission, not physical paper delivery."""
    order = _order(db, user, order_id)
    job = _job(db, order, job_id)
    claim = job.payload.get("_claim", {})
    if (not secrets.compare_digest(claim.get("owner", ""), _owner(user, payload.terminal_id))
            or not secrets.compare_digest(claim.get("token_hash", ""), digest(str(payload.claim_token)))):
        raise HTTPException(403, "Only the owning terminal and member can complete this claim")
    if _idempotency(db, user, job, payload, idempotency_key, "complete"):
        response = public_job(job)
        db.commit()
        return response
    if job.status == "cancelled":
        db.commit()
        raise HTTPException(409, "Print job is cancelled or stale")
    if job.status != "claimed":
        if job.payload.get("_outcome") == payload.outcome:
            response = public_job(job)
            db.commit()
            return response
        raise HTTPException(409, "Print job was already completed with another outcome")
    job.payload = {**job.payload, "_outcome": payload.outcome}
    job.status = "printed" if payload.outcome == "printed" else "failed"
    # Store stable operational text, not arbitrary QZ errors with paths or secrets.
    job.error_message = {"printed": None, "not_sent": "No se envio a QZ Tray; se puede reintentar.",
                         "unknown": "Resultado de impresion incierto; no reintentar automaticamente."}[payload.outcome]
    if payload.outcome == "printed":
        job.completed_at = utcnow()
    else:
        job.failed_at = utcnow()
    _audit(db, user, job, payload.outcome)
    if payload.outcome == "not_sent":
        invalidate_stale_job(db, order, job)
    db.flush()
    response = public_job(job)
    db.commit()
    return response
