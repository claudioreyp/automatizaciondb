"""Durable local-POS print intents and jobs; no printer I/O in order transactions."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import logging
from typing import Literal
from uuid import UUID, uuid4

from fastapi.encoders import jsonable_encoder
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .command_revisions import effective_ticket_items
from .models import (
    Branch, BranchSettings, Business, DiningArea, KitchenTicket, Membership, Order, Payment,
    PrintJob, RestaurantTable, StaffMember, utcnow,
)
from .printing_settings import printing_json

logger = logging.getLogger(__name__)
INTENT = "_pos_printing"
TRANSPORT = "pos-local-v1"
OPERATIONAL_ROLES = {"superadmin", "owner", "manager", "cashier", "waiter"}


class PrintItem(BaseModel):
    id: int
    product_id: int | None
    status: str
    name: str
    variant_name: str | None = None
    quantity: float
    unit_price: float
    line_total: float
    promotion_discount: float
    modifiers: list[dict] = Field(default_factory=list)
    notes: str | None = None


class PrintOrder(BaseModel):
    id: int
    business_id: int
    branch_id: int
    version: int
    status: str
    payment_status: str
    payment_method: str | None
    number: str
    folio: int | None
    channel: str
    source: str
    created_at: datetime
    customer_name: str | None
    customer_phone: str | None
    delivery_address: dict | None
    table_id: int | None
    notes: str | None
    subtotal: float
    discount: float
    manual_discount: float
    promotion_discount: float
    delivery_fee: float
    total: float
    items: list[PrintItem]
    payments: list[dict] = Field(default_factory=list)
    kitchen_tickets: list[dict] = Field(default_factory=list)
    table_context: dict | None = None
    paid_amount: float = 0
    remaining_amount: float = 0


class PrintBusiness(BaseModel):
    id: int
    name: str
    currency: str
    timezone: str


class PrintBranch(BaseModel):
    id: int
    name: str
    address: str | None
    phone: str | None


class PrintTicket(BaseModel):
    id: int
    order_id: int
    status: str
    print_count: int
    context: dict
    sequence: int
    version: int
    kind: str
    station: str
    created_at: datetime
    created_by_name: str | None
    table_name: str | None
    items: list[dict]


class PrintPayload(BaseModel):
    snapshot_version: Literal[1] = 1
    printer_name: str
    print_language: Literal["pixel", "escpos"] = "pixel"
    paper_width_mm: Literal[58, 80]
    copies: int = Field(ge=1, le=5)
    template: dict
    business: PrintBusiness
    branch: PrintBranch
    order: PrintOrder
    ticket: PrintTicket | None
    created_by_name: str | None
    table_name: str | None
    paid_amount: float
    remaining_amount: float


class PosPrintJob(BaseModel):
    id: str
    order_id: int
    branch_id: int
    kitchen_ticket_id: int | None
    job_type: Literal["customer_receipt", "kitchen_ticket"]
    status: Literal["pending", "claimed", "printed", "failed", "cancelled"]
    attempts: int
    retryable: bool
    error_message: str | None
    created_at: datetime
    payload: PrintPayload


class PosPrintingResponse(BaseModel):
    order_id: int
    items: list[PosPrintJob]
    recoverable_error: bool = False


class PosQzResponse(BaseModel):
    mode: Literal["signed", "manual-approval"]
    certificate: str | None


class PosPrintClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    terminal_id: UUID
    claim_token: UUID
    retry_not_sent: bool = False


class PosPrintClaimResponse(BaseModel):
    job: PosPrintJob
    dispatch_allowed: bool


class PosPrintComplete(BaseModel):
    model_config = ConfigDict(extra="forbid")
    terminal_id: UUID
    claim_token: UUID
    outcome: Literal["printed", "not_sent", "unknown"]
    error_message: str | None = Field(default=None, max_length=500)


class PosPrintCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_type: Literal["customer_receipt", "kitchen_ticket"]
    kitchen_ticket_id: int | None = Field(default=None, ge=1)
    expected_order_version: int = Field(ge=1)
    expected_ticket_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_ticket(self):
        if self.job_type == "kitchen_ticket":
            if self.kitchen_ticket_id is None or self.expected_ticket_version is None:
                raise ValueError("Kitchen printing requires ticket ID and expected ticket version")
        elif self.kitchen_ticket_id is not None or self.expected_ticket_version is not None:
            raise ValueError("Customer receipts do not accept a kitchen ticket")
        return self


def digest(value) -> str:
    return hashlib.sha256(json.dumps(jsonable_encoder(value), sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def order_snapshot(order: Order) -> dict:
    return PrintOrder(
        **{key: getattr(order, key) for key in (
            "id", "business_id", "branch_id", "version", "status", "payment_status",
            "payment_method", "number", "folio", "channel", "source", "customer_name",
            "customer_phone", "delivery_address", "table_id", "notes",
        )},
        created_at=aware(order.created_at),
        **{key: float(getattr(order, key) or 0) for key in (
            "subtotal", "discount", "manual_discount", "promotion_discount",
            "delivery_fee", "total",
        )},
        items=[PrintItem(
            id=item.id, product_id=item.product_id, status=item.status,
            name=item.product_name, variant_name=item.variant_name,
            quantity=float(item.quantity), unit_price=float(item.unit_price),
            line_total=float(item.line_total),
            promotion_discount=float(item.promotion_discount or 0),
            modifiers=deepcopy(item.modifiers or []), notes=item.notes,
        ) for item in sorted(order.items, key=lambda item: item.id)
            if item.status not in {"cancelled", "superseded"}],
    ).model_dump(mode="json")


def content_digest(snapshot: dict, *, kitchen: bool = False) -> str:
    keys = ("number", "folio", "channel", "source", "customer_name", "customer_phone",
            "delivery_address", "table_id", "notes")
    if not kitchen:
        keys += ("items", "subtotal", "discount", "manual_discount", "promotion_discount",
                 "delivery_fee", "total")
    return digest({key: snapshot[key] for key in keys})


def local_printer_config(settings: BranchSettings | None) -> dict | None:
    config = settings.printer_config if settings else None
    if (not settings or not settings.advanced_printing or not isinstance(config, dict)
            or not isinstance(config.get("printer_name"), str) or not config["printer_name"].strip()):
        return None
    return config


def table_print_context(db: Session, order: Order) -> dict:
    context = {"table_id": order.table_id, "table_name": None, "area_id": None, "area_name": None}
    if order.table_id is None:
        return context
    row = db.execute(select(RestaurantTable, DiningArea).outerjoin(DiningArea, (
        (DiningArea.id == RestaurantTable.area_id)
        & (DiningArea.business_id == order.business_id) & (DiningArea.branch_id == order.branch_id)
    )).where(RestaurantTable.id == order.table_id, RestaurantTable.business_id == order.business_id,
             RestaurantTable.branch_id == order.branch_id)).first()
    if row:
        table, area = row
        context.update(table_name=table.name, area_id=area.id if area else None,
                       area_name=area.name if area else None)
    return context


def print_actor_name(db: Session, order: Order, actor_id: str | None) -> str | None:
    staff_id = int(actor_id[6:]) if isinstance(actor_id, str) and actor_id.startswith("staff:") and actor_id[6:].isdigit() else None
    staff = db.scalar(select(StaffMember).where(
        StaffMember.business_id == order.business_id,
        StaffMember.id == staff_id if staff_id else StaffMember.auth_user_id == actor_id,
    )) if actor_id else None
    membership_name = db.scalar(select(Membership.full_name).where(
        Membership.business_id == order.business_id, Membership.auth_user_id == actor_id,
        Membership.branch_id.in_([order.branch_id]) | Membership.branch_id.is_(None),
    ).order_by(Membership.id)) if actor_id else None
    return (f"{staff.first_name} {staff.last_name or ''}".strip() if staff else membership_name) or None


def recorded_print_actor(context: dict) -> str | None:
    if "created_by_name" in context:
        return context["created_by_name"]
    # Older local intents already recorded the name; never guess it from today's staff directory.
    for definition in context.get(INTENT, {}).get("jobs", []):
        payload = definition.get("payload", {})
        if "created_by_name" in payload:
            return payload["created_by_name"]
    return None


def prepare_pos_print_intent(db: Session, order: Order, ticket: KitchenTicket) -> bool:
    """Persist intent with the command, so a failed queue insert is recoverable.

    Only newly created POS commands receive this marker. Reading old orders must
    never reconstruct jobs from today's printer settings or product catalog.
    """
    if order.source != "pos":
        return False
    settings = db.scalar(select(BranchSettings).where(BranchSettings.branch_id == order.branch_id))
    config = local_printer_config(settings)
    if config is None:
        return False
    if config.get("automatic_printing", True) is not True:
        return True
    definitions = []
    table_order = order.channel == "dine_in" and order.table_id is not None
    if ticket.sequence == 1 and not table_order and config.get("manual_customer_receipt") is False:
        definitions.append(("customer_receipt", settings.customer_ticket_template or {}))
    if config.get("auto_print_kitchen") is True:
        definitions.append(("kitchen_ticket", settings.kitchen_ticket_template or {}))
    if not definitions:
        return True
    jobs = []
    for kind, template in definitions:
        payload = build_print_payload(db, order, ticket, config, kind, template)
        jobs.append({
            "id": str(uuid4()), "key": f"pos-local:{ticket.id}:{kind}",
            "job_type": kind, "payload": payload,
            "guard": {"order": content_digest(payload["order"], kitchen=kind == "kitchen_ticket"),
                      "ticket_version": ticket.version},
        })
    ticket.context_snapshot = {**(ticket.context_snapshot or {}), INTENT: {"transport": TRANSPORT, "jobs": jobs}}
    db.flush()
    recover_pos_print_jobs(db, order, [ticket])
    return True


def build_print_payload(db: Session, order: Order, ticket: KitchenTicket | None,
                        config: dict, kind: str, template: dict, *, snapshot_context: dict | None = None) -> dict:
    """Capture one document identically for automatic and explicitly requested jobs."""
    business = db.get(Business, order.business_id)
    branch = db.get(Branch, order.branch_id)
    context = snapshot_context if snapshot_context is not None else ((ticket.context_snapshot or {}) if ticket else {})
    author = recorded_print_actor(context)
    table_context = {"table_id": context.get("table_id", order.table_id),
                     "table_name": context.get("table_name"),
                     "area_id": context.get("area_id"), "area_name": context.get("area_name")}
    payments = list(db.scalars(select(Payment).where(
        Payment.order_id == order.id, Payment.status == "confirmed",
    ).order_by(Payment.id)))
    paid = float(sum(payment.amount for payment in payments))
    snapshot = order_snapshot(order)
    snapshot.update(
        payments=[{"id": payment.id, "order_id": order.id, "method": payment.method,
                   "amount": float(payment.amount), "status": payment.status,
                   "created_at": aware(payment.created_at).isoformat()} for payment in payments],
        paid_amount=paid, remaining_amount=max(0, round(float(order.total) - paid, 2)),
        table_context=table_context,
    )
    recorded_context = {}
    for definition in context.get(INTENT, {}).get("jobs", []):
        if definition.get("job_type") == "kitchen_ticket":
            recorded_context = (definition.get("payload", {}).get("ticket") or {}).get("context") or {}
            break
    # Presence is authoritative, including nulls. Missing history is never today's destination.
    printed_context = {key: deepcopy(context[key] if key in context else recorded_context.get(key)) for key in (
        "order_number", "order_folio", "channel", "source", "customer_name", "customer_phone",
        "delivery_address", "notes",
    )}
    printed_context.update(**table_context, created_by_name=author)
    ticket_data = PrintTicket(
        id=ticket.id, order_id=order.id, status=ticket.status, print_count=ticket.print_count,
        context=printed_context, sequence=ticket.sequence, version=ticket.version,
        kind=ticket.kind, station=ticket.station, created_at=aware(ticket.fired_at),
        created_by_name=author, table_name=context.get("table_name"),
        items=effective_ticket_items(ticket),
    ) if ticket else None
    configured_copies = config.get("copies", 1)
    copies = max(1, min(5, configured_copies)) if type(configured_copies) is int else 1
    return PrintPayload(
        printer_name=config["printer_name"].strip(),
        print_language="escpos" if config.get("print_language") == "escpos" else "pixel",
        paper_width_mm=58 if config.get("paper_width_mm") == 58 else 80,
        copies=copies, template=printing_json(
            "customer_ticket_template" if kind == "customer_receipt" else "kitchen_ticket_template", template),
        business=PrintBusiness(id=business.id, name=business.name,
                               currency=business.currency, timezone=business.timezone),
        branch=PrintBranch(id=branch.id, name=branch.name, address=branch.address, phone=branch.phone),
        order=PrintOrder(**snapshot), ticket=ticket_data if kind == "kitchen_ticket" else None,
        created_by_name=author, table_name=context.get("table_name"),
        paid_amount=paid, remaining_amount=max(0, round(float(order.total) - paid, 2)),
    ).model_dump(mode="json")


def create_checkout_print_job(db: Session, order: Order) -> PrintJob | None:
    """The checkout transition and its document commit together, with no printer I/O."""
    if order.source != "pos":
        return None
    settings = db.scalar(select(BranchSettings).where(
        BranchSettings.business_id == order.business_id, BranchSettings.branch_id == order.branch_id,
    ))
    config = local_printer_config(settings)
    if (config is None or config.get("automatic_printing", True) is not True
            or config.get("manual_customer_receipt") is not False):
        return None
    context = {**table_print_context(db, order), "created_by": order.created_by,
               "created_by_name": print_actor_name(db, order, order.created_by)}
    payload = build_print_payload(db, order, None, config, "customer_receipt",
                                  settings.customer_ticket_template or {}, snapshot_context=context)
    job = PrintJob(
        id=str(uuid4()), business_id=order.business_id, branch_id=order.branch_id,
        order_id=order.id, kitchen_ticket_id=None, printer_id=None, paired_device_id=None,
        job_type="customer_receipt", status="pending",
        idempotency_key=f"pos-local:checkout:{order.id}:{order.version}:customer_receipt",
        payload={**payload, "_transport": TRANSPORT, "_trigger": "table_checkout_started",
                 "_guard": {"order": content_digest(payload["order"]),
                            "checkout_started_at": aware(order.checkout_started_at).isoformat()}},
    )
    db.add(job)
    try:
        db.flush()
    except SQLAlchemyError as exc:
        raise HTTPException(503, "No se pudo guardar el ticket; la mesa no se cerro. Reintenta la misma operacion.") from exc
    return job


def invalidate_checkout_print_jobs(db: Session, order: Order) -> None:
    rows = db.scalars(select(PrintJob).where(
        PrintJob.business_id == order.business_id, PrintJob.branch_id == order.branch_id,
        PrintJob.order_id == order.id, PrintJob.status.in_(["pending", "claimed", "failed"]),
    ).with_for_update())
    for job in rows:
        if (not is_local_job(job) or job.payload.get("_trigger") != "table_checkout_started"
                or (job.status == "failed" and job.payload.get("_outcome") != "not_sent")):
            continue
        # Retain ownership for late ACKs, including a not_sent ACK after another close cycle.
        job.payload = {**job.payload, "_checkout_invalidated": True}
        invalidate_stale_job(db, order, job)


def checkout_receipt_context(db: Session, order: Order) -> dict | None:
    if order.checkout_started_at is None:
        return None
    started_at = aware(order.checkout_started_at).isoformat()
    rows = db.scalars(select(PrintJob).where(
        PrintJob.business_id == order.business_id, PrintJob.branch_id == order.branch_id,
        PrintJob.order_id == order.id, PrintJob.job_type == "customer_receipt",
    ).order_by(PrintJob.created_at.desc(), PrintJob.id.desc()))
    for job in rows:
        if (not is_local_job(job) or job.payload.get("_trigger") != "table_checkout_started"
                or job.payload.get("_checkout_invalidated")
                or job.payload.get("_guard", {}).get("checkout_started_at") != started_at):
            continue
        context = job.payload.get("order", {}).get("table_context") or {}
        if order.table_id is None or context.get("table_id") == order.table_id:
            return {**deepcopy(context), "created_by_name": job.payload.get("created_by_name")}
    return None


def _insert_jobs(db: Session, order: Order, tickets: list[KitchenTicket]) -> None:
    for ticket in tickets:
        intent = (ticket.context_snapshot or {}).get(INTENT, {})
        if intent.get("transport") != TRANSPORT:
            continue
        for definition in intent["jobs"]:
            if db.scalar(select(PrintJob.id).where(
                PrintJob.business_id == order.business_id,
                PrintJob.idempotency_key == definition["key"],
            )):
                continue
            db.add(PrintJob(
                id=definition["id"], business_id=order.business_id, branch_id=order.branch_id,
                order_id=order.id,
                kitchen_ticket_id=ticket.id if definition["job_type"] == "kitchen_ticket" else None,
                job_type=definition["job_type"], idempotency_key=definition["key"],
                payload={**deepcopy(definition["payload"]), "_transport": TRANSPORT,
                         "_guard": deepcopy(definition["guard"])},
            ))
    db.flush()


def recover_pos_print_jobs(db: Session, order: Order, tickets: list[KitchenTicket]) -> bool:
    try:
        # A queue failure rolls back this savepoint, not the confirmed operation.
        with db.begin_nested():
            _insert_jobs(db, order, tickets)
        return True
    except Exception:
        logger.warning("POS printing queue unavailable for order %s; intent retained", order.id)
        return False


def is_local_job(job: PrintJob) -> bool:
    return (job.paired_device_id is None and job.printer_id is None
            and (job.payload or {}).get("_transport") == TRANSPORT)


def stale_job(db: Session, order: Order, job: PrintJob) -> bool:
    # An explicit historical receipt carries the cancellation label. A document
    # captured before cancellation must never gain this exception on replay.
    cancelled_receipt = (
        job.job_type == "customer_receipt" and job.payload.get("_manual") is True
        and job.payload.get("order", {}).get("status") == "cancelled"
    )
    if order.status == "cancelled" and not cancelled_receipt:
        return True
    if cancelled_receipt and order.status != "cancelled":
        return True
    guard = job.payload.get("_guard", {})
    if job.payload.get("_trigger") == "table_checkout_started":
        started_at = aware(order.checkout_started_at).isoformat() if order.checkout_started_at else None
        if job.payload.get("_checkout_invalidated") or guard.get("checkout_started_at") != started_at:
            return True
    if guard.get("order") != content_digest(order_snapshot(order), kitchen=job.job_type == "kitchen_ticket"):
        return True
    if job.kitchen_ticket_id is not None:
        ticket = db.get(KitchenTicket, job.kitchen_ticket_id, populate_existing=True)
        if (not ticket or ticket.status == "cancelled"
                or ticket.version != guard.get("ticket_version")):
            return True
    return False


def invalidate_stale_job(db: Session, order: Order, job: PrintJob) -> bool:
    # A claimed document may already be in the spooler. Preserve its ownership
    # and accept late acknowledgements, even after cancellation or a revision.
    unsent = job.status == "pending" or (
        job.status == "failed" and job.payload.get("_outcome") == "not_sent"
    )
    if unsent and stale_job(db, order, job):
        job.status = "cancelled"
        job.error_message = "El pedido o la comanda cambiaron; no se enviara esta impresion."
        return True
    return job.status == "cancelled"


def public_job(job: PrintJob) -> dict:
    return PosPrintJob(
        id=job.id, order_id=job.order_id, branch_id=job.branch_id,
        kitchen_ticket_id=job.kitchen_ticket_id, job_type=job.job_type,
        status=job.status, attempts=job.attempts,
        retryable=job.status == "failed" and job.payload.get("_outcome") == "not_sent",
        error_message=job.error_message, created_at=aware(job.created_at),
        payload=PrintPayload(**job.payload),
    ).model_dump(mode="json")
