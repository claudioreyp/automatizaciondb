from __future__ import annotations

import csv
import io
import json
import hashlib
import secrets
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .auth import (
    AuthContext,
    IntegrationAuthContext,
    ensure_branch_scope,
    ensure_business_scope,
    get_authenticated_identity,
    get_current_user,
    hash_integration_token,
    require_integration_token,
    require_integration_scope,
    require_roles,
)
from .config import get_settings
from .database import get_db
from .models import (
    AuditEvent,
    Branch,
    Business,
    CashMovement,
    CashRegister,
    CashSession,
    Category,
    Courier,
    DeliveryAssignment,
    DiningArea,
    IdempotencyRecord,
    IntegrationCredential,
    IntegrationEvent,
    Invitation,
    InventoryItem,
    KitchenTicket,
    Membership,
    Modifier,
    ModifierGroup,
    ModuleEntitlement,
    Order,
    OrderItem,
    Payment,
    PaymentAllocation,
    PaymentEvidence,
    Product,
    ProductModifierGroup,
    ProductVariant,
    RecipeItem,
    Reservation,
    ReservationTable,
    RestaurantTable,
    StockMovement,
    utcnow,
)
from .realtime import hub
from .schemas import (
    AddOrderItem,
    AreaCreate,
    BranchCreate,
    BranchUpdate,
    BusinessCreate,
    BusinessUpdate,
    CashMovementCreate,
    CashSessionClose,
    CashSessionOpen,
    CategoryCreate,
    CourierCreate,
    DeliveryAssign,
    DeliveryTransition,
    EvidenceReview,
    InventoryItemCreate,
    IntegrationEvidenceCreate,
    IntegrationCredentialCreate,
    InvitationAccept,
    InvitationCreate,
    LegacyDraftCreate,
    MembershipUpdate,
    OrderCreate,
    OrderPatch,
    OrderTransition,
    PaymentCreate,
    ProductCreate,
    ProductUpdate,
    PublicOrderCreate,
    PublicReservationCreate,
    RecipeReplace,
    RegisterCreate,
    RestaurantOnboardingCreate,
    ReservationCreate,
    ReservationUpdate,
    SplitPreview,
    StockAdjustment,
    TableCreate,
    TableUpdate,
    TicketTransition,
)
from .services import (
    add_order_item,
    add_payment,
    assert_version,
    audit,
    available_tables,
    build_order_item,
    cash_session_expected,
    confirm_order,
    create_integration_event,
    create_order,
    create_reservation,
    get_idempotent_response,
    load_order,
    money,
    parse_legacy_items,
    product_capacity,
    reverse_order_stock,
    recalculate_order,
    save_idempotent_response,
    send_order_to_kitchen,
    serialize_order,
    split_amounts,
    transition_order,
)
from .storage import analyze_payment_image, load_private_file, store_private_file
import httpx


api = APIRouter(prefix="/api/v1")
legacy = APIRouter(prefix="/api")


MODULES = ["pos", "tables", "kds", "inventory", "cash", "delivery", "reservations", "whatsapp"]


def serialize_business(db: Session, business: Business) -> dict:
    modules = {
        item.module: item.enabled
        for item in db.scalars(
            select(ModuleEntitlement).where(ModuleEntitlement.business_id == business.id)
        )
    }
    return {
        "id": business.id,
        "slug": business.slug,
        "name": business.name,
        "status": business.status,
        "plan": business.plan,
        "currency": business.currency,
        "timezone": business.timezone,
        "logo_url": business.logo_url,
        "phone": business.phone,
        "auto_accept_payment_evidence": business.auto_accept_payment_evidence,
        "auto_accept_limit": float(business.auto_accept_limit or 0),
        "modules": {module: modules.get(module, False) for module in MODULES},
        "created_at": business.created_at,
        "updated_at": business.updated_at,
    }


def serialize_invitation(invitation: Invitation) -> dict:
    return {
        "id": invitation.id,
        "business_id": invitation.business_id,
        "branch_id": invitation.branch_id,
        "email": invitation.email,
        "role": invitation.role,
        "status": invitation.status,
        "expires_at": invitation.expires_at,
        "accepted_at": invitation.accepted_at,
        "created_at": invitation.created_at,
    }


def create_invitation_record(
    db: Session,
    user: AuthContext,
    business: Business,
    email: str,
    role: str,
    branch_id: int | None = None,
) -> tuple[Invitation, str]:
    normalized_email = email.strip().lower()
    for pending in db.scalars(
        select(Invitation).where(
            Invitation.business_id == business.id,
            Invitation.email == normalized_email,
            Invitation.status == "pending",
        )
    ):
        pending.status = "superseded"
    raw_token = secrets.token_urlsafe(36)
    invitation = Invitation(
        business_id=business.id,
        branch_id=branch_id,
        email=normalized_email,
        role=role,
        token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
        expires_at=utcnow() + timedelta(days=7),
        created_by=user.user_id,
    )
    db.add(invitation)
    db.flush()
    audit(
        db,
        user,
        "invitation.created",
        "invitation",
        invitation.id,
        business.id,
        {"email": normalized_email, "role": role},
    )
    return invitation, raw_token


async def deliver_invitation(invitation: Invitation, raw_token: str) -> dict:
    settings = get_settings()
    delivery_status = "not_configured"
    redirect_url = f"{settings.invite_redirect_url}?token={raw_token}"
    if settings.supabase_url and settings.supabase_service_role_key:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    f"{settings.supabase_url.rstrip('/')}/auth/v1/invite",
                    headers={
                        "apikey": settings.supabase_service_role_key,
                        "Authorization": f"Bearer {settings.supabase_service_role_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "email": invitation.email,
                        "redirect_to": redirect_url,
                        "data": {
                            "impulsa_invitation_id": invitation.id,
                            "business_id": invitation.business_id,
                        },
                    },
                )
                response.raise_for_status()
                delivery_status = "sent"
        except httpx.HTTPError:
            delivery_status = "failed"
    result = {**serialize_invitation(invitation), "delivery_status": delivery_status}
    if settings.is_development:
        result["development_accept_url"] = redirect_url
    return result


def serialize_branch(branch: Branch) -> dict:
    return {
        "id": branch.id,
        "business_id": branch.business_id,
        "slug": branch.slug,
        "name": branch.name,
        "address": branch.address,
        "phone": branch.phone,
        "opening_hours": branch.opening_hours,
        "accepted_payment_methods": branch.accepted_payment_methods,
        "delivery_enabled": branch.delivery_enabled,
        "takeaway_enabled": branch.takeaway_enabled,
        "delivery_fee": float(branch.delivery_fee or 0),
        "yape_number": branch.yape_number,
        "plin_number": branch.plin_number,
        "payment_recipient_name": branch.payment_recipient_name,
        "maps_url": branch.maps_url,
        "yape_qr_storage_path": branch.yape_qr_storage_path,
        "active": branch.active,
    }


def branch_for_user(db: Session, user: AuthContext, branch_id: int) -> Branch:
    branch = db.get(Branch, branch_id)
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found")
    ensure_branch_scope(user, branch.business_id, branch.id)
    return branch


def scoped_business_id(user: AuthContext, business_id: int | None) -> int:
    if business_id is not None:
        ensure_business_scope(user, business_id)
        return business_id
    if user.business_id is None:
        raise HTTPException(status_code=422, detail="business_id is required for superadmin requests")
    return user.business_id


def serialize_inventory(item: InventoryItem) -> dict:
    return {
        "id": item.id,
        "business_id": item.business_id,
        "branch_id": item.branch_id,
        "sku": item.sku,
        "name": item.name,
        "unit": item.unit,
        "quantity": float(item.quantity),
        "minimum_stock": float(item.minimum_stock),
        "unit_cost": float(item.unit_cost),
        "low_stock": item.quantity <= item.minimum_stock,
        "active": item.active,
        "version": item.version,
    }


def serialize_table(table: RestaurantTable) -> dict:
    return {
        "id": table.id,
        "business_id": table.business_id,
        "branch_id": table.branch_id,
        "area_id": table.area_id,
        "code": table.code,
        "name": table.name,
        "capacity": table.capacity,
        "position_x": table.position_x,
        "position_y": table.position_y,
        "width": table.width,
        "height": table.height,
        "shape": table.shape,
        "status": table.status,
        "version": table.version,
    }


def serialize_reservation(db: Session, reservation: Reservation) -> dict:
    table_ids = list(
        db.scalars(
            select(ReservationTable.table_id).where(ReservationTable.reservation_id == reservation.id)
        )
    )
    return {
        "id": reservation.id,
        "business_id": reservation.business_id,
        "branch_id": reservation.branch_id,
        "customer_name": reservation.customer_name,
        "customer_phone": reservation.customer_phone,
        "party_size": reservation.party_size,
        "start_at": reservation.start_at,
        "end_at": reservation.end_at,
        "status": reservation.status,
        "source": reservation.source,
        "notes": reservation.notes,
        "table_ids": table_ids,
        "version": reservation.version,
    }


def serialize_ticket(ticket: KitchenTicket) -> dict:
    return {
        "id": ticket.id,
        "business_id": ticket.business_id,
        "branch_id": ticket.branch_id,
        "order_id": ticket.order_id,
        "station": ticket.station,
        "status": ticket.status,
        "sequence": ticket.sequence,
        "items": ticket.items_snapshot,
        "fired_at": ticket.fired_at,
        "created_at": ticket.fired_at,
        "started_at": ticket.started_at,
        "ready_at": ticket.ready_at,
        "print_count": ticket.print_count,
    }


@api.get("/health", tags=["system"])
def health(db: Session = Depends(get_db)):
    db.execute(select(1))
    return {"status": "ok", "service": "impulsa-pos-api", "version": "1.0.0"}


@api.get("/me", tags=["auth"])
def me(user: AuthContext = Depends(get_current_user)):
    return {
        "user_id": user.user_id,
        "email": user.email,
        "role": user.role,
        "business_id": user.business_id,
        "branch_id": user.branch_id,
    }


@api.get("/admin/businesses", tags=["superadmin"])
def list_businesses(
    user: AuthContext = Depends(require_roles("superadmin")),
    db: Session = Depends(get_db),
):
    businesses = list(db.scalars(select(Business).order_by(Business.created_at.desc())))
    return [serialize_business(db, business) for business in businesses]


@api.post("/admin/businesses", status_code=201, tags=["superadmin"])
def create_business_endpoint(
    payload: BusinessCreate,
    user: AuthContext = Depends(require_roles("superadmin")),
    db: Session = Depends(get_db),
):
    business = Business(name=payload.name, slug=payload.slug, plan=payload.plan)
    db.add(business)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Business slug already exists") from exc
    for module in MODULES:
        db.add(ModuleEntitlement(business_id=business.id, module=module, enabled=module in payload.modules))
    audit(db, user, "business.created", "business", business.id, business.id)
    db.commit()
    return serialize_business(db, business)


@api.patch("/admin/businesses/{business_id}", tags=["superadmin"])
def update_business_endpoint(
    business_id: int,
    payload: BusinessUpdate,
    user: AuthContext = Depends(require_roles("superadmin")),
    db: Session = Depends(get_db),
):
    business = db.get(Business, business_id)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")
    values = payload.model_dump(exclude_unset=True, exclude={"modules"})
    for key, value in values.items():
        setattr(business, key, value)
    if payload.modules is not None:
        existing = {
            item.module: item
            for item in db.scalars(
                select(ModuleEntitlement).where(ModuleEntitlement.business_id == business.id)
            )
        }
        for module, enabled in payload.modules.items():
            if module not in MODULES:
                continue
            if module in existing:
                existing[module].enabled = enabled
            else:
                db.add(ModuleEntitlement(business_id=business.id, module=module, enabled=enabled))
    audit(db, user, "business.updated", "business", business.id, business.id, values)
    db.commit()
    return serialize_business(db, business)


@api.get("/admin/metrics", tags=["superadmin"])
def admin_metrics(
    user: AuthContext = Depends(require_roles("superadmin")),
    db: Session = Depends(get_db),
):
    today = datetime.combine(date.today(), time.min, tzinfo=timezone.utc)
    return {
        "businesses": db.scalar(select(func.count(Business.id))) or 0,
        "active_businesses": db.scalar(select(func.count(Business.id)).where(Business.status == "active")) or 0,
        "branches": db.scalar(select(func.count(Branch.id))) or 0,
        "orders_today": db.scalar(select(func.count(Order.id)).where(Order.created_at >= today)) or 0,
        "sales_today": float(
            db.scalar(
                select(func.coalesce(func.sum(Order.total), 0)).where(
                    Order.created_at >= today,
                    Order.status == "closed",
                )
            )
            or 0
        ),
    }


@api.get("/admin/audit", tags=["superadmin"])
def admin_audit(
    limit: int = Query(default=100, ge=1, le=500),
    user: AuthContext = Depends(require_roles("superadmin")),
    db: Session = Depends(get_db),
):
    events = list(db.scalars(select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(limit)))
    return [
        {
            "id": event.id,
            "business_id": event.business_id,
            "actor_id": event.actor_id,
            "action": event.action,
            "entity_type": event.entity_type,
            "entity_id": event.entity_id,
            "payload": event.payload,
            "created_at": event.created_at,
        }
        for event in events
    ]


@api.get("/admin/businesses/{business_id}/overview", tags=["superadmin"])
def business_overview(
    business_id: int,
    user: AuthContext = Depends(require_roles("superadmin")),
    db: Session = Depends(get_db),
):
    business = db.get(Business, business_id)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")
    last_activity = db.scalar(select(func.max(AuditEvent.created_at)).where(AuditEvent.business_id == business.id))
    return {
        "business": serialize_business(db, business),
        "branches": db.scalar(select(func.count(Branch.id)).where(Branch.business_id == business.id)) or 0,
        "users": db.scalar(
            select(func.count(Membership.id)).where(
                Membership.business_id == business.id,
                Membership.active.is_(True),
            )
        ) or 0,
        "orders": db.scalar(select(func.count(Order.id)).where(Order.business_id == business.id)) or 0,
        "sales": float(
            db.scalar(
                select(func.coalesce(func.sum(Order.total), 0)).where(
                    Order.business_id == business.id,
                    Order.status == "closed",
                )
            ) or 0
        ),
        "pending_invitations": db.scalar(
            select(func.count(Invitation.id)).where(
                Invitation.business_id == business.id,
                Invitation.status == "pending",
            )
        ) or 0,
        "last_activity": last_activity,
    }


@api.get("/admin/invitations", tags=["superadmin"])
def list_invitations(
    business_id: int | None = None,
    user: AuthContext = Depends(require_roles("superadmin")),
    db: Session = Depends(get_db),
):
    statement = select(Invitation).order_by(Invitation.created_at.desc()).limit(300)
    if business_id is not None:
        statement = statement.where(Invitation.business_id == business_id)
    return [serialize_invitation(item) for item in db.scalars(statement)]


@api.get("/admin/businesses/{business_id}/memberships", tags=["superadmin"])
def list_business_memberships(
    business_id: int,
    user: AuthContext = Depends(require_roles("superadmin")),
    db: Session = Depends(get_db),
):
    if not db.get(Business, business_id):
        raise HTTPException(status_code=404, detail="Business not found")
    memberships = db.scalars(
        select(Membership)
        .where(Membership.business_id == business_id)
        .order_by(Membership.active.desc(), Membership.full_name, Membership.email)
    )
    return [
        {
            "id": item.id,
            "email": item.email,
            "full_name": item.full_name,
            "business_id": item.business_id,
            "branch_id": item.branch_id,
            "role": item.role,
            "active": item.active,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
        }
        for item in memberships
    ]


@api.patch("/admin/memberships/{membership_id}", tags=["superadmin"])
def update_business_membership(
    membership_id: int,
    payload: MembershipUpdate,
    user: AuthContext = Depends(require_roles("superadmin")),
    db: Session = Depends(get_db),
):
    membership = db.get(Membership, membership_id)
    if not membership or membership.business_id is None:
        raise HTTPException(status_code=404, detail="Membership not found")
    values = payload.model_dump(exclude_unset=True)
    for key, value in values.items():
        setattr(membership, key, value)
    audit(
        db,
        user,
        "membership.updated",
        "membership",
        membership.id,
        membership.business_id,
        values,
    )
    db.commit()
    return {
        "id": membership.id,
        "email": membership.email,
        "full_name": membership.full_name,
        "business_id": membership.business_id,
        "branch_id": membership.branch_id,
        "role": membership.role,
        "active": membership.active,
        "updated_at": membership.updated_at,
    }


@api.post("/admin/invitations", status_code=201, tags=["superadmin"])
async def create_invitation(
    payload: InvitationCreate,
    user: AuthContext = Depends(require_roles("superadmin")),
    db: Session = Depends(get_db),
):
    business = db.get(Business, payload.business_id)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")
    if payload.branch_id:
        branch = db.get(Branch, payload.branch_id)
        if not branch or branch.business_id != business.id:
            raise HTTPException(status_code=422, detail="Branch does not belong to business")
    invitation, raw_token = create_invitation_record(
        db,
        user,
        business,
        payload.email,
        payload.role,
        payload.branch_id,
    )
    db.commit()
    return await deliver_invitation(invitation, raw_token)


@api.post("/invitations/accept", tags=["auth"])
def accept_invitation(
    payload: InvitationAccept,
    user: AuthContext = Depends(get_authenticated_identity),
    db: Session = Depends(get_db),
):
    token_hash = hashlib.sha256(payload.token.encode()).hexdigest()
    invitation = db.scalar(
        select(Invitation).where(
            Invitation.token_hash == token_hash,
            Invitation.status == "pending",
        ).with_for_update()
    )
    if not invitation:
        raise HTTPException(status_code=404, detail="Invitation is invalid or already used")
    expires_at = invitation.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < utcnow():
        invitation.status = "expired"
        db.commit()
        raise HTTPException(status_code=410, detail="Invitation has expired")
    if user.email and user.email.lower() != invitation.email:
        raise HTTPException(status_code=403, detail="Invitation belongs to another email address")
    membership = db.scalar(
        select(Membership).where(
            Membership.auth_user_id == user.user_id,
            Membership.business_id == invitation.business_id,
            Membership.branch_id == invitation.branch_id,
        )
    )
    if not membership:
        membership = Membership(
            auth_user_id=user.user_id,
            email=invitation.email,
            full_name=invitation.email.split("@", 1)[0],
            business_id=invitation.business_id,
            branch_id=invitation.branch_id,
            role=invitation.role,
            active=True,
        )
        db.add(membership)
    else:
        membership.role = invitation.role
        membership.active = True
    invitation.status = "accepted"
    invitation.accepted_at = utcnow()
    audit(db, user, "invitation.accepted", "invitation", invitation.id, invitation.business_id)
    db.commit()
    return {
        "status": "accepted",
        "business_id": invitation.business_id,
        "branch_id": invitation.branch_id,
        "role": invitation.role,
    }


@api.get("/context", tags=["restaurant"])
def restaurant_context(
    business_id: int | None = None,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    resolved = scoped_business_id(user, business_id)
    business = db.get(Business, resolved)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")
    branches = list(db.scalars(select(Branch).where(Branch.business_id == resolved).order_by(Branch.name)))
    return {"business": serialize_business(db, business), "branches": [serialize_branch(item) for item in branches]}


@api.get("/branches", tags=["branches"])
def list_branches(
    business_id: int | None = None,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    resolved = scoped_business_id(user, business_id)
    return [
        serialize_branch(item)
        for item in db.scalars(select(Branch).where(Branch.business_id == resolved).order_by(Branch.name))
    ]


@api.post("/branches", status_code=201, tags=["branches"])
def create_branch_endpoint(
    business_id: int,
    payload: BranchCreate,
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager")),
    db: Session = Depends(get_db),
):
    ensure_business_scope(user, business_id)
    branch = Branch(business_id=business_id, **payload.model_dump())
    db.add(branch)
    db.flush()
    db.add(DiningArea(business_id=business_id, branch_id=branch.id, name="Salón principal"))
    db.add(CashRegister(business_id=business_id, branch_id=branch.id, name="Caja principal"))
    audit(db, user, "branch.created", "branch", branch.id, business_id)
    db.commit()
    return serialize_branch(branch)


@api.patch("/branches/{branch_id}", tags=["branches"])
def update_branch_endpoint(
    branch_id: int,
    payload: BranchUpdate,
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager")),
    db: Session = Depends(get_db),
):
    branch = branch_for_user(db, user, branch_id)
    values = payload.model_dump(exclude_unset=True)
    for key, value in values.items():
        setattr(branch, key, value)
    audit(db, user, "branch.updated", "branch", branch.id, branch.business_id, values)
    db.commit()
    return serialize_branch(branch)


def serialize_integration_credential(credential: IntegrationCredential) -> dict:
    return {
        "id": credential.id,
        "business_id": credential.business_id,
        "branch_id": credential.branch_id,
        "name": credential.name,
        "token_prefix": credential.token_prefix,
        "scopes": credential.scopes,
        "active": credential.active,
        "last_used_at": credential.last_used_at,
        "expires_at": credential.expires_at,
        "revoked_at": credential.revoked_at,
        "created_at": credential.created_at,
        "updated_at": credential.updated_at,
    }


def issue_integration_token() -> tuple[str, str, str]:
    token_id = secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12]
    prefix = f"esc_live_{token_id}"
    token = f"{prefix}.{secrets.token_urlsafe(32)}"
    return token, prefix, hash_integration_token(token)


def supabase_admin_headers() -> dict[str, str]:
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_service_role_key:
        raise HTTPException(
            status_code=503,
            detail="Supabase Auth no está configurado para crear el acceso del restaurante",
        )
    return {
        "apikey": settings.supabase_service_role_key,
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
        "Content-Type": "application/json",
    }


def supabase_owner_function_url() -> str:
    settings = get_settings()
    if not settings.supabase_url:
        raise HTTPException(
            status_code=503,
            detail="Supabase Auth no está configurado para crear el acceso del restaurante",
        )
    function_name = settings.supabase_owner_provision_function.strip()
    if not function_name:
        raise HTTPException(
            status_code=503,
            detail="La función segura para crear propietarios no está configurada",
        )
    return f"{settings.supabase_url.rstrip('/')}/functions/v1/{function_name}"


def caller_bearer_headers(caller_authorization: str | None) -> dict[str, str]:
    if not caller_authorization or not caller_authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=503,
            detail="La sesión del superadmin es necesaria para crear el acceso del restaurante",
        )
    return {
        "Authorization": caller_authorization,
        "Content-Type": "application/json",
    }


async def create_supabase_owner_account(
    email: str,
    password: str,
    full_name: str,
    business_id: int,
    caller_authorization: str | None = None,
) -> str:
    settings = get_settings()
    use_admin_api = bool(settings.supabase_service_role_key)
    if use_admin_api:
        url = f"{settings.supabase_url.rstrip('/')}/auth/v1/admin/users"
        headers = supabase_admin_headers()
        body = {
            "email": email,
            "password": password,
            "email_confirm": True,
            "user_metadata": {"full_name": full_name},
            "app_metadata": {
                "provisioned_by": "escalar_admin",
                "business_id": business_id,
            },
        }
    else:
        url = supabase_owner_function_url()
        headers = caller_bearer_headers(caller_authorization)
        body = {
            "action": "create",
            "email": email,
            "password": password,
            "full_name": full_name,
            "business_id": business_id,
        }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                url,
                headers=headers,
                json=body,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail="Supabase Auth no respondió al crear el usuario del restaurante",
        ) from exc

    if not response.is_success:
        error_text = response.text.lower()
        if response.status_code in {401, 403}:
            raise HTTPException(
                status_code=response.status_code,
                detail="La sesión del superadmin no autorizó la creación del usuario",
            )
        if response.status_code in {400, 409, 422} and any(
            marker in error_text for marker in ("already", "registered", "exists")
        ):
            raise HTTPException(
                status_code=409,
                detail="Ese usuario ya existe en Supabase Auth. Usa otro correo o gestiona su acceso existente.",
            )
        raise HTTPException(
            status_code=502,
            detail="Supabase Auth rechazó la creación del usuario del restaurante",
        )

    result = response.json()
    user_id = result.get("id") or (result.get("user") or {}).get("id")
    if not user_id:
        raise HTTPException(
            status_code=502,
            detail="Supabase Auth creó una respuesta sin identificador de usuario",
        )
    return str(user_id)


async def delete_supabase_auth_user(
    user_id: str,
    caller_authorization: str | None = None,
) -> None:
    """Best-effort compensation when the POS transaction cannot be committed."""
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            if settings.supabase_service_role_key:
                await client.delete(
                    f"{settings.supabase_url.rstrip('/')}/auth/v1/admin/users/{user_id}",
                    headers=supabase_admin_headers(),
                )
            else:
                await client.post(
                    supabase_owner_function_url(),
                    headers=caller_bearer_headers(caller_authorization),
                    json={"action": "delete", "user_id": user_id},
                )
    except (HTTPException, httpx.HTTPError):
        # The original onboarding error remains authoritative. Operators can
        # reconcile this exceptional case from Supabase's audit trail.
        return


@api.post("/admin/onboarding/restaurants", status_code=201, tags=["superadmin"])
async def onboard_restaurant(
    payload: RestaurantOnboardingCreate,
    request: Request,
    user: AuthContext = Depends(require_roles("superadmin")),
    db: Session = Depends(get_db),
):
    if db.scalar(select(Business.id).where(Business.slug == payload.business.slug)):
        raise HTTPException(status_code=409, detail="Business slug already exists")

    business = Business(
        name=payload.business.name,
        slug=payload.business.slug,
        plan=payload.business.plan,
    )
    db.add(business)
    supabase_user_id: str | None = None
    caller_authorization = request.headers.get("Authorization")
    try:
        db.flush()
        for module in MODULES:
            db.add(
                ModuleEntitlement(
                    business_id=business.id,
                    module=module,
                    enabled=module in payload.business.modules,
                )
            )

        branch = Branch(business_id=business.id, **payload.branch.model_dump())
        db.add(branch)
        db.flush()
        db.add(DiningArea(business_id=business.id, branch_id=branch.id, name="Salón principal"))
        db.add(CashRegister(business_id=business.id, branch_id=branch.id, name="Caja principal"))

        integration_token, prefix, token_hash = issue_integration_token()
        credential = IntegrationCredential(
            business_id=business.id,
            branch_id=branch.id,
            name=payload.credential_name,
            token_prefix=prefix,
            token_hash=token_hash,
            scopes=payload.integration_scopes,
            created_by=user.user_id,
        )
        db.add(credential)
        db.flush()

        owner_email = payload.owner_email.strip().lower()
        owner_name = payload.owner_name.strip()
        supabase_user_id = await create_supabase_owner_account(
            owner_email,
            payload.owner_password.get_secret_value(),
            owner_name,
            business.id,
            caller_authorization,
        )
        membership = Membership(
            auth_user_id=supabase_user_id,
            email=owner_email,
            full_name=owner_name,
            business_id=business.id,
            branch_id=None,
            role="owner",
            active=True,
        )
        db.add(membership)
        db.flush()

        audit(db, user, "business.created", "business", business.id, business.id)
        audit(db, user, "branch.created", "branch", branch.id, business.id)
        audit(
            db,
            user,
            "integration_credential.created",
            "integration_credential",
            credential.id,
            business.id,
            {"branch_id": branch.id, "scopes": payload.integration_scopes},
        )
        audit(
            db,
            user,
            "restaurant.onboarded",
            "business",
            business.id,
            business.id,
            {
                "branch_id": branch.id,
                "owner_email": owner_email,
                "owner_user_id": supabase_user_id,
            },
        )
        db.commit()
    except HTTPException:
        db.rollback()
        if supabase_user_id:
            await delete_supabase_auth_user(supabase_user_id, caller_authorization)
        raise
    except IntegrityError as exc:
        db.rollback()
        if supabase_user_id:
            await delete_supabase_auth_user(supabase_user_id, caller_authorization)
        raise HTTPException(status_code=409, detail="Restaurant onboarding conflicts with existing data") from exc
    except Exception as exc:
        db.rollback()
        if supabase_user_id:
            await delete_supabase_auth_user(supabase_user_id, caller_authorization)
        raise HTTPException(status_code=500, detail="No se pudo completar el alta del restaurante") from exc

    settings = get_settings()
    api_base_url = (settings.public_api_base_url or f"{str(request.base_url).rstrip('/')}/api/v1").rstrip("/")
    integration_base = f"{api_base_url}/integrations"
    return {
        "business": serialize_business(db, business),
        "branch": serialize_branch(branch),
        "owner_access": {
            "user_id": supabase_user_id,
            "email": owner_email,
            "full_name": owner_name,
            "role": "owner",
            "status": "active",
            "business_id": business.id,
            "branch_id": None,
        },
        "credential": {
            **serialize_integration_credential(credential),
            "token": integration_token,
        },
        "n8n": {
            "api_base_url": api_base_url,
            "business_id": business.id,
            "branch_id": branch.id,
            "authentication": "bearer",
            "write_idempotency_header": "Idempotency-Key",
            "workflow_template": "Agente de POS Propio",
            "endpoints": {
                "restaurant_context": {"method": "GET", "url": f"{integration_base}/context"},
                "yape_qr": {"method": "GET", "url": f"{integration_base}/context/yape-qr"},
                "menu": {"method": "GET", "url": f"{integration_base}/context/menu"},
                "inventory": {"method": "GET", "url": f"{integration_base}/context/inventory"},
                "adjust_inventory": {"method": "POST", "url": f"{integration_base}/inventory/{{item_id}}/adjust"},
                "tables": {"method": "GET", "url": f"{integration_base}/context/tables"},
                "reservation_availability": {"method": "GET", "url": f"{integration_base}/context/availability"},
                "create_order_draft": {"method": "POST", "url": f"{integration_base}/orders/draft"},
                "update_order": {"method": "PATCH", "url": f"{integration_base}/orders/{{order_id}}"},
                "confirm_order": {"method": "POST", "url": f"{integration_base}/orders/{{order_id}}/confirm"},
                "confirm_cash_order": {"method": "POST", "url": f"{integration_base}/orders/{{order_id}}/cash-confirm"},
                "payment_evidence": {"method": "POST", "url": f"{integration_base}/orders/{{order_id}}/payment-evidence"},
                "order_status": {"method": "GET", "url": f"{integration_base}/orders/{{order_id}}/status"},
                "request_human": {"method": "POST", "url": f"{integration_base}/orders/{{order_id}}/request-human"},
                "create_reservation": {"method": "POST", "url": f"{integration_base}/reservations"},
                "events": {"method": "GET", "url": f"{integration_base}/events"},
                "ack_event": {"method": "POST", "url": f"{integration_base}/events/{{event_id}}/ack"},
            },
        },
    }


@api.get("/admin/integration-credentials", tags=["admin"])
def list_integration_credentials(
    branch_id: int,
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager")),
    db: Session = Depends(get_db),
):
    branch_for_user(db, user, branch_id)
    credentials = db.scalars(
        select(IntegrationCredential)
        .where(IntegrationCredential.branch_id == branch_id)
        .order_by(IntegrationCredential.created_at.desc())
    )
    return [serialize_integration_credential(item) for item in credentials]


@api.post("/admin/integration-credentials", status_code=201, tags=["admin"])
def create_integration_credential(
    payload: IntegrationCredentialCreate,
    user: AuthContext = Depends(require_roles("superadmin", "owner")),
    db: Session = Depends(get_db),
):
    branch = branch_for_user(db, user, payload.branch_id)
    token, prefix, token_hash = issue_integration_token()
    credential = IntegrationCredential(
        business_id=branch.business_id,
        branch_id=branch.id,
        name=payload.name,
        token_prefix=prefix,
        token_hash=token_hash,
        scopes=payload.scopes,
        expires_at=payload.expires_at,
        created_by=user.user_id,
    )
    db.add(credential)
    db.flush()
    audit(
        db,
        user,
        "integration_credential.created",
        "integration_credential",
        credential.id,
        branch.business_id,
        {"branch_id": branch.id, "scopes": payload.scopes},
    )
    db.commit()
    return {**serialize_integration_credential(credential), "token": token}


@api.post("/admin/integration-credentials/{credential_id}/rotate", tags=["admin"])
def rotate_integration_credential(
    credential_id: int,
    user: AuthContext = Depends(require_roles("superadmin", "owner")),
    db: Session = Depends(get_db),
):
    credential = db.get(IntegrationCredential, credential_id)
    if not credential:
        raise HTTPException(status_code=404, detail="Integration credential not found")
    branch_for_user(db, user, credential.branch_id)
    token, prefix, token_hash = issue_integration_token()
    credential.token_prefix = prefix
    credential.token_hash = token_hash
    credential.active = True
    credential.revoked_at = None
    audit(
        db,
        user,
        "integration_credential.rotated",
        "integration_credential",
        credential.id,
        credential.business_id,
    )
    db.commit()
    return {**serialize_integration_credential(credential), "token": token}


@api.post("/admin/integration-credentials/{credential_id}/revoke", tags=["admin"])
def revoke_integration_credential(
    credential_id: int,
    user: AuthContext = Depends(require_roles("superadmin", "owner")),
    db: Session = Depends(get_db),
):
    credential = db.get(IntegrationCredential, credential_id)
    if not credential:
        raise HTTPException(status_code=404, detail="Integration credential not found")
    branch_for_user(db, user, credential.branch_id)
    credential.active = False
    credential.revoked_at = utcnow()
    audit(
        db,
        user,
        "integration_credential.revoked",
        "integration_credential",
        credential.id,
        credential.business_id,
    )
    db.commit()
    return serialize_integration_credential(credential)


@api.post("/branches/{branch_id}/yape-qr", tags=["branches"])
async def upload_branch_yape_qr(
    branch_id: int,
    file: UploadFile = File(...),
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager")),
    db: Session = Depends(get_db),
):
    branch = branch_for_user(db, user, branch_id)
    content_type = file.content_type or "application/octet-stream"
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=422, detail="Yape QR must be an image")
    data = await file.read()
    if not data or len(data) > 5 * 1024 * 1024:
        raise HTTPException(status_code=422, detail="Yape QR image must be between 1 byte and 5 MB")
    branch.yape_qr_storage_path = await store_private_file(
        data,
        file.filename or "yape-qr.png",
        content_type,
        "branch-payment-qr",
    )
    audit(db, user, "branch.yape_qr_updated", "branch", branch.id, branch.business_id)
    db.commit()
    return serialize_branch(branch)


@api.get("/catalog", tags=["catalog"])
def get_catalog(
    branch_id: int,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch = branch_for_user(db, user, branch_id)
    categories = list(
        db.scalars(
            select(Category)
            .where(Category.branch_id == branch.id, Category.active.is_(True))
            .order_by(Category.sort_order, Category.name)
        )
    )
    products = list(
        db.scalars(
            select(Product)
            .where(Product.branch_id == branch.id)
            .order_by(Product.sort_order, Product.name)
        )
    )
    variants = list(
        db.scalars(select(ProductVariant).where(ProductVariant.product_id.in_([item.id for item in products])))
    ) if products else []
    links = list(
        db.execute(
            select(ProductModifierGroup.product_id, ProductModifierGroup.group_id).where(
                ProductModifierGroup.product_id.in_([item.id for item in products])
            )
        )
    ) if products else []
    group_ids = {row.group_id for row in links}
    groups = list(db.scalars(select(ModifierGroup).where(ModifierGroup.id.in_(group_ids)))) if group_ids else []
    modifiers = list(db.scalars(select(Modifier).where(Modifier.group_id.in_(group_ids)))) if group_ids else []
    return {
        "branch": serialize_branch(branch),
        "categories": [
            {"id": item.id, "name": item.name, "color": item.color, "sort_order": item.sort_order}
            for item in categories
        ],
        "products": [
            {
                "id": item.id,
                "category_id": item.category_id,
                "sku": item.sku,
                "name": item.name,
                "description": item.description,
                "price": float(item.price),
                "image_url": item.image_url,
                "available": item.available,
                "track_stock": item.track_stock,
                "preparation_station": item.preparation_station,
                "variants": [
                    {"id": variant.id, "name": variant.name, "price_delta": float(variant.price_delta)}
                    for variant in variants
                    if variant.product_id == item.id and variant.active
                ],
                "modifier_groups": [
                    {
                        "id": group.id,
                        "name": group.name,
                        "minimum": group.minimum,
                        "maximum": group.maximum,
                        "required": group.required,
                        "modifiers": [
                            {"id": modifier.id, "name": modifier.name, "price_delta": float(modifier.price_delta)}
                            for modifier in modifiers
                            if modifier.group_id == group.id and modifier.active
                        ],
                    }
                    for group in groups
                    if any(link.product_id == item.id and link.group_id == group.id for link in links)
                ],
            }
            for item in products
        ],
    }


@api.post("/catalog/categories", status_code=201, tags=["catalog"])
def create_category(
    payload: CategoryCreate,
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager")),
    db: Session = Depends(get_db),
):
    branch = branch_for_user(db, user, payload.branch_id)
    category = Category(business_id=branch.business_id, **payload.model_dump())
    db.add(category)
    db.commit()
    db.refresh(category)
    return {"id": category.id, "name": category.name, "color": category.color, "sort_order": category.sort_order}


@api.post("/catalog/products", status_code=201, tags=["catalog"])
def create_product(
    payload: ProductCreate,
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager")),
    db: Session = Depends(get_db),
):
    branch = branch_for_user(db, user, payload.branch_id)
    if payload.category_id:
        category = db.get(Category, payload.category_id)
        if not category or category.branch_id != branch.id:
            raise HTTPException(status_code=422, detail="Category does not belong to branch")
    product = Product(business_id=branch.business_id, **payload.model_dump())
    db.add(product)
    audit(db, user, "product.created", "product", None, branch.business_id, {"name": product.name})
    db.commit()
    db.refresh(product)
    return {"id": product.id, "name": product.name, "price": float(product.price), "available": product.available}


@api.patch("/catalog/products/{product_id}", tags=["catalog"])
def update_product(
    product_id: int,
    payload: ProductUpdate,
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager")),
    db: Session = Depends(get_db),
):
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    branch_for_user(db, user, product.branch_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(product, key, value)
    audit(db, user, "product.updated", "product", product.id, product.business_id)
    db.commit()
    return {"id": product.id, "name": product.name, "price": float(product.price), "available": product.available}


@api.post("/catalog/import-csv", tags=["catalog"])
async def import_catalog_csv(
    branch_id: int,
    file: UploadFile = File(...),
    dry_run: bool = Query(default=True),
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager")),
    db: Session = Depends(get_db),
):
    branch = branch_for_user(db, user, branch_id)
    raw = await file.read()
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Catalog CSV exceeds 5 MB")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="Catalog CSV must use UTF-8 encoding") from exc
    reader = csv.DictReader(io.StringIO(text))
    required = {"sku", "name", "category", "price"}
    if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
        raise HTTPException(
            status_code=422,
            detail="CSV columns required: sku,name,category,price",
        )

    parsed: list[dict] = []
    errors: list[dict] = []
    seen_skus: set[str] = set()
    for row_number, row in enumerate(reader, start=2):
        try:
            sku = (row.get("sku") or "").strip()
            name = (row.get("name") or "").strip()
            category_name = (row.get("category") or "").strip()
            if not sku or not name or not category_name:
                raise ValueError("sku, name and category cannot be empty")
            if sku in seen_skus:
                raise ValueError("duplicate sku in CSV")
            seen_skus.add(sku)
            price = money(row.get("price"))
            if price <= 0:
                raise ValueError("price must be greater than zero")
            stock_raw = (row.get("stock_quantity") or "").strip()
            stock_quantity = Decimal(stock_raw) if stock_raw else None
            if stock_quantity is not None and stock_quantity < 0:
                raise ValueError("stock_quantity cannot be negative")
            recipe_raw = (row.get("recipe_quantity") or "").strip()
            recipe_quantity = Decimal(recipe_raw) if recipe_raw else Decimal("1")
            if recipe_quantity <= 0:
                raise ValueError("recipe_quantity must be greater than zero")
            parsed.append(
                {
                    "sku": sku,
                    "name": name,
                    "category": category_name,
                    "price": price,
                    "description": (row.get("description") or "").strip() or None,
                    "available": (row.get("available") or "true").strip().lower() not in {"0", "false", "no"},
                    "preparation_station": (row.get("preparation_station") or "kitchen").strip() or "kitchen",
                    "stock_quantity": stock_quantity,
                    "stock_unit": (row.get("stock_unit") or "unit").strip() or "unit",
                    "minimum_stock": Decimal((row.get("minimum_stock") or "0").strip() or "0"),
                    "recipe_quantity": recipe_quantity,
                }
            )
        except (ValueError, ArithmeticError) as exc:
            errors.append({"row": row_number, "sku": row.get("sku"), "error": str(exc)})

    preview = {
        "dry_run": dry_run,
        "rows": len(parsed) + len(errors),
        "valid_rows": len(parsed),
        "errors": errors,
        "preview": [
            {**row, "price": float(row["price"]), "stock_quantity": float(row["stock_quantity"]) if row["stock_quantity"] is not None else None}
            for row in parsed[:20]
        ],
    }
    if dry_run or errors:
        if errors and not dry_run:
            raise HTTPException(status_code=422, detail=preview)
        return preview

    categories = {
        item.name.lower(): item
        for item in db.scalars(select(Category).where(Category.branch_id == branch.id))
    }
    created = 0
    updated = 0
    for row in parsed:
        category = categories.get(row["category"].lower())
        if not category:
            category = Category(
                business_id=branch.business_id,
                branch_id=branch.id,
                name=row["category"],
            )
            db.add(category)
            db.flush()
            categories[row["category"].lower()] = category
        product = db.scalar(
            select(Product).where(Product.branch_id == branch.id, Product.sku == row["sku"])
        )
        if not product:
            product = Product(
                business_id=branch.business_id,
                branch_id=branch.id,
                sku=row["sku"],
                name=row["name"],
                price=row["price"],
            )
            db.add(product)
            created += 1
        else:
            updated += 1
        product.category_id = category.id
        product.name = row["name"]
        product.description = row["description"]
        product.price = row["price"]
        product.available = row["available"]
        product.preparation_station = row["preparation_station"]
        product.track_stock = row["stock_quantity"] is not None
        db.flush()

        if row["stock_quantity"] is not None:
            inventory_sku = f"STOCK-{row['sku']}"
            inventory = db.scalar(
                select(InventoryItem).where(
                    InventoryItem.branch_id == branch.id,
                    InventoryItem.sku == inventory_sku,
                )
            )
            if not inventory:
                inventory = InventoryItem(
                    business_id=branch.business_id,
                    branch_id=branch.id,
                    sku=inventory_sku,
                    name=f"Stock {row['name']}",
                    unit=row["stock_unit"],
                    quantity=row["stock_quantity"],
                    minimum_stock=row["minimum_stock"],
                )
                db.add(inventory)
                db.flush()
                delta = row["stock_quantity"]
            else:
                delta = row["stock_quantity"] - Decimal(str(inventory.quantity))
                inventory.name = f"Stock {row['name']}"
                inventory.unit = row["stock_unit"]
                inventory.quantity = row["stock_quantity"]
                inventory.minimum_stock = row["minimum_stock"]
                inventory.version += 1
            if delta:
                db.add(
                    StockMovement(
                        business_id=branch.business_id,
                        branch_id=branch.id,
                        inventory_item_id=inventory.id,
                        movement_type="catalog_import",
                        quantity_delta=delta,
                        balance_after=inventory.quantity,
                        reference_type="product",
                        reference_id=str(product.id),
                        created_by=user.user_id,
                    )
                )
            for component in db.scalars(select(RecipeItem).where(RecipeItem.product_id == product.id)):
                db.delete(component)
            db.add(
                RecipeItem(
                    product_id=product.id,
                    inventory_item_id=inventory.id,
                    quantity=row["recipe_quantity"],
                )
            )

    audit(
        db,
        user,
        "catalog.csv_imported",
        "branch",
        branch.id,
        branch.business_id,
        {"created": created, "updated": updated},
    )
    db.commit()
    return {**preview, "dry_run": False, "created": created, "updated": updated}


@api.get("/inventory", tags=["inventory"])
def list_inventory(
    branch_id: int,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch_for_user(db, user, branch_id)
    items = list(db.scalars(select(InventoryItem).where(InventoryItem.branch_id == branch_id).order_by(InventoryItem.name)))
    return [serialize_inventory(item) for item in items]


@api.post("/inventory", status_code=201, tags=["inventory"])
def create_inventory_item(
    payload: InventoryItemCreate,
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager")),
    db: Session = Depends(get_db),
):
    branch = branch_for_user(db, user, payload.branch_id)
    item = InventoryItem(business_id=branch.business_id, **payload.model_dump())
    db.add(item)
    db.flush()
    if item.quantity:
        db.add(
            StockMovement(
                business_id=item.business_id,
                branch_id=item.branch_id,
                inventory_item_id=item.id,
                movement_type="initial",
                quantity_delta=item.quantity,
                balance_after=item.quantity,
                created_by=user.user_id,
            )
        )
    audit(db, user, "inventory.created", "inventory_item", item.id, item.business_id)
    db.commit()
    return serialize_inventory(item)


@api.post("/inventory/{item_id}/adjust", tags=["inventory"])
async def adjust_inventory(
    item_id: int,
    payload: StockAdjustment,
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager")),
    db: Session = Depends(get_db),
):
    item = db.scalar(select(InventoryItem).where(InventoryItem.id == item_id).with_for_update())
    if not item:
        raise HTTPException(status_code=404, detail="Inventory item not found")
    branch_for_user(db, user, item.branch_id)
    assert_version(item.version, payload.expected_version)
    item.quantity = Decimal(str(item.quantity)) + payload.quantity_delta
    item.version += 1
    db.add(
        StockMovement(
            business_id=item.business_id,
            branch_id=item.branch_id,
            inventory_item_id=item.id,
            movement_type=payload.movement_type,
            quantity_delta=payload.quantity_delta,
            balance_after=item.quantity,
            note=payload.note,
            created_by=user.user_id,
        )
    )
    audit(db, user, "inventory.adjusted", "inventory_item", item.id, item.business_id)
    db.commit()
    result = serialize_inventory(item)
    await hub.broadcast(item.branch_id, "inventory.updated", result)
    return result


@api.put("/catalog/products/{product_id}/recipe", tags=["inventory"])
def replace_recipe(
    product_id: int,
    payload: RecipeReplace,
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager")),
    db: Session = Depends(get_db),
):
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    branch_for_user(db, user, product.branch_id)
    for existing in db.scalars(select(RecipeItem).where(RecipeItem.product_id == product.id)):
        db.delete(existing)
    for component in payload.components:
        item = db.get(InventoryItem, component.inventory_item_id)
        if not item or item.branch_id != product.branch_id:
            raise HTTPException(status_code=422, detail="Recipe inventory item does not belong to branch")
        db.add(RecipeItem(product_id=product.id, **component.model_dump()))
    product.track_stock = bool(payload.components)
    audit(db, user, "product.recipe_replaced", "product", product.id, product.business_id)
    db.commit()
    return {"product_id": product.id, "components": payload.model_dump(mode="json")["components"]}


@api.get("/areas", tags=["tables"])
def list_areas(
    branch_id: int,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch_for_user(db, user, branch_id)
    return [
        {"id": item.id, "branch_id": item.branch_id, "name": item.name, "sort_order": item.sort_order}
        for item in db.scalars(select(DiningArea).where(DiningArea.branch_id == branch_id).order_by(DiningArea.sort_order))
    ]


@api.post("/areas", status_code=201, tags=["tables"])
def create_area(
    payload: AreaCreate,
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager")),
    db: Session = Depends(get_db),
):
    branch = branch_for_user(db, user, payload.branch_id)
    area = DiningArea(business_id=branch.business_id, **payload.model_dump())
    db.add(area)
    db.commit()
    db.refresh(area)
    return {"id": area.id, "branch_id": area.branch_id, "name": area.name, "sort_order": area.sort_order}


@api.get("/tables", tags=["tables"])
def list_tables(
    branch_id: int,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch_for_user(db, user, branch_id)
    return [serialize_table(item) for item in db.scalars(select(RestaurantTable).where(RestaurantTable.branch_id == branch_id))]


@api.post("/tables", status_code=201, tags=["tables"])
def create_table(
    payload: TableCreate,
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager")),
    db: Session = Depends(get_db),
):
    branch = branch_for_user(db, user, payload.branch_id)
    table = RestaurantTable(business_id=branch.business_id, **payload.model_dump())
    db.add(table)
    db.commit()
    db.refresh(table)
    return serialize_table(table)


@api.patch("/tables/{table_id}", tags=["tables"])
async def update_table(
    table_id: int,
    payload: TableUpdate,
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager", "cashier", "waiter")),
    db: Session = Depends(get_db),
):
    table = db.scalar(select(RestaurantTable).where(RestaurantTable.id == table_id).with_for_update())
    if not table:
        raise HTTPException(status_code=404, detail="Table not found")
    branch_for_user(db, user, table.branch_id)
    assert_version(table.version, payload.expected_version)
    values = payload.model_dump(exclude_unset=True, exclude={"expected_version"})
    for key, value in values.items():
        setattr(table, key, value)
    table.version += 1
    audit(db, user, "table.updated", "table", table.id, table.business_id, values)
    db.commit()
    result = serialize_table(table)
    await hub.broadcast(table.branch_id, "table.updated", result)
    return result


@api.get("/orders", tags=["orders"])
def list_orders(
    branch_id: int,
    status: str | None = None,
    channel: str | None = None,
    search: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch_for_user(db, user, branch_id)
    statement = (
        select(Order)
        .where(Order.branch_id == branch_id)
        .options(selectinload(Order.items))
        .order_by(Order.created_at.desc())
        .limit(limit)
    )
    if status:
        statement = statement.where(Order.status == status)
    if channel:
        statement = statement.where(Order.channel == channel)
    if search:
        pattern = f"%{search.strip()}%"
        statement = statement.where(
            or_(
                Order.number.ilike(pattern),
                Order.customer_name.ilike(pattern),
                Order.customer_phone.ilike(pattern),
            )
        )
    return [serialize_order(item) for item in db.scalars(statement)]


@api.post("/orders", status_code=201, tags=["orders"])
async def create_order_endpoint(
    payload: OrderCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager", "cashier", "waiter")),
    db: Session = Depends(get_db),
):
    scope = f"pos-order:{payload.branch_id}"
    existing = get_idempotent_response(db, scope, idempotency_key)
    if existing:
        return existing
    order = create_order(db, user, payload)
    db.flush()
    response = serialize_order(order)
    save_idempotent_response(db, scope, idempotency_key, order.business_id, response)
    db.commit()
    response = serialize_order(load_order(db, order.id))
    await hub.broadcast(order.branch_id, "order.created", response)
    return response


@api.get("/orders/{order_id}", tags=["orders"])
def get_order_endpoint(
    order_id: int,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    order = load_order(db, order_id)
    ensure_branch_scope(user, order.business_id, order.branch_id)
    return serialize_order(order)


@api.patch("/orders/{order_id}", tags=["orders"])
async def patch_order_endpoint(
    order_id: int,
    payload: OrderPatch,
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager", "cashier", "waiter")),
    db: Session = Depends(get_db),
):
    order = load_order(db, order_id, for_update=True)
    ensure_branch_scope(user, order.business_id, order.branch_id)
    if order.status not in {"draft", "pending_confirmation", "confirmed"}:
        raise HTTPException(status_code=409, detail="Order can no longer be edited")
    assert_version(order.version, payload.expected_version)
    values = payload.model_dump(exclude_unset=True, exclude={"expected_version"})
    for key, value in values.items():
        setattr(order, key, value)
    recalculate_order(order)
    order.version += 1
    audit(db, user, "order.updated", "order", order.id, order.business_id, values)
    db.commit()
    result = serialize_order(load_order(db, order.id))
    await hub.broadcast(order.branch_id, "order.updated", result)
    return result


@api.post("/orders/{order_id}/items", status_code=201, tags=["orders"])
async def add_order_item_endpoint(
    order_id: int,
    payload: AddOrderItem,
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager", "cashier", "waiter")),
    db: Session = Depends(get_db),
):
    order = load_order(db, order_id, for_update=True)
    ensure_branch_scope(user, order.business_id, order.branch_id)
    assert_version(order.version, payload.expected_version)
    add_order_item(db, user, order, payload.item)
    db.commit()
    result = serialize_order(load_order(db, order.id))
    await hub.broadcast(order.branch_id, "order.updated", result)
    return result


@api.delete("/orders/{order_id}/items/{item_id}", tags=["orders"])
async def remove_order_item_endpoint(
    order_id: int,
    item_id: int,
    expected_version: int | None = None,
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager", "cashier", "waiter")),
    db: Session = Depends(get_db),
):
    order = load_order(db, order_id, for_update=True)
    ensure_branch_scope(user, order.business_id, order.branch_id)
    assert_version(order.version, expected_version)
    if order.status not in {"draft", "pending_confirmation", "confirmed"}:
        raise HTTPException(status_code=409, detail="Order can no longer be edited")
    item = next((candidate for candidate in order.items if candidate.id == item_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Order item not found")
    db.delete(item)
    order.items.remove(item)
    recalculate_order(order)
    order.version += 1
    audit(db, user, "order.item_removed", "order", order.id, order.business_id, {"item_id": item_id})
    db.commit()
    result = serialize_order(load_order(db, order.id))
    await hub.broadcast(order.branch_id, "order.updated", result)
    return result


@api.post("/orders/{order_id}/confirm", tags=["orders"])
async def confirm_order_endpoint(
    order_id: int,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager", "cashier", "waiter")),
    db: Session = Depends(get_db),
):
    scope = f"confirm-order:{order_id}"
    existing = get_idempotent_response(db, scope, idempotency_key)
    if existing:
        return existing
    order = load_order(db, order_id, for_update=True)
    ensure_branch_scope(user, order.business_id, order.branch_id)
    confirm_order(db, user, order)
    db.flush()
    result = serialize_order(order)
    save_idempotent_response(db, scope, idempotency_key, order.business_id, result)
    db.commit()
    result = serialize_order(load_order(db, order.id))
    await hub.broadcast(order.branch_id, "order.updated", result)
    return result


@api.post("/orders/{order_id}/send-to-kitchen", tags=["orders"])
async def send_to_kitchen_endpoint(
    order_id: int,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager", "cashier", "waiter")),
    db: Session = Depends(get_db),
):
    scope = f"send-kitchen:{order_id}"
    existing = get_idempotent_response(db, scope, idempotency_key)
    if existing:
        return existing
    order = load_order(db, order_id, for_update=True)
    ensure_branch_scope(user, order.business_id, order.branch_id)
    tickets = send_order_to_kitchen(db, user, order)
    db.flush()
    result = {"order": serialize_order(order), "tickets": [serialize_ticket(ticket) for ticket in tickets]}
    save_idempotent_response(db, scope, idempotency_key, order.business_id, result)
    db.commit()
    await hub.broadcast(order.branch_id, "kitchen.ticket_created", result)
    return result


@api.post("/orders/{order_id}/transition", tags=["orders"])
async def transition_order_endpoint(
    order_id: int,
    payload: OrderTransition,
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager", "cashier", "waiter", "dispatcher")),
    db: Session = Depends(get_db),
):
    order = load_order(db, order_id, for_update=True)
    ensure_branch_scope(user, order.business_id, order.branch_id)
    assert_version(order.version, payload.expected_version)
    previous_status = order.status
    transition_order(db, user, order, payload.status)
    if order.status != previous_status and order.status in {"ready", "dispatched", "delivered", "cancelled"}:
        create_integration_event(
            db,
            order,
            f"order.{order.status}",
            {"status": order.status},
        )
    db.commit()
    result = serialize_order(load_order(db, order.id))
    await hub.broadcast(order.branch_id, "order.updated", result)
    return result


@api.post("/orders/{order_id}/payments", status_code=201, tags=["payments"])
async def create_payment_endpoint(
    order_id: int,
    payload: PaymentCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager", "cashier")),
    db: Session = Depends(get_db),
):
    scope = f"order-payment:{order_id}"
    existing = get_idempotent_response(db, scope, idempotency_key)
    if existing:
        return existing
    order = load_order(db, order_id, for_update=True)
    ensure_branch_scope(user, order.business_id, order.branch_id)
    payment = add_payment(db, user, order, payload)
    db.flush()
    result = {
        "payment": {
            "id": payment.id,
            "order_id": payment.order_id,
            "method": payment.method,
            "amount": float(payment.amount),
            "status": payment.status,
        },
        "order": serialize_order(order),
    }
    save_idempotent_response(db, scope, idempotency_key, order.business_id, result)
    db.commit()
    await hub.broadcast(order.branch_id, "payment.created", result)
    return result


@api.post("/orders/{order_id}/split-preview", tags=["payments"])
def split_order_preview(
    order_id: int,
    payload: SplitPreview,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    order = load_order(db, order_id)
    ensure_branch_scope(user, order.business_id, order.branch_id)
    paid = money(
        db.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.order_id == order.id, Payment.status == "confirmed"))
    )
    remaining = max(money(order.total) - paid, Decimal("0"))
    return {"remaining": float(remaining), "parts": [float(value) for value in split_amounts(remaining, payload.parts)]}


@api.get("/kitchen/tickets", tags=["kitchen"])
def list_kitchen_tickets(
    branch_id: int,
    status: str | None = None,
    station: str | None = None,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch_for_user(db, user, branch_id)
    statement = select(KitchenTicket).where(KitchenTicket.branch_id == branch_id).order_by(KitchenTicket.fired_at.asc())
    if status:
        statement = statement.where(KitchenTicket.status == status)
    else:
        statement = statement.where(KitchenTicket.status.in_(["queued", "preparing", "ready"]))
    if station:
        statement = statement.where(KitchenTicket.station == station)
    return [serialize_ticket(item) for item in db.scalars(statement)]


@api.post("/kitchen/tickets/{ticket_id}/transition", tags=["kitchen"])
async def transition_ticket(
    ticket_id: int,
    payload: TicketTransition,
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager", "kitchen")),
    db: Session = Depends(get_db),
):
    ticket = db.get(KitchenTicket, ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Kitchen ticket not found")
    branch_for_user(db, user, ticket.branch_id)
    allowed = {
        "queued": {"preparing", "cancelled"},
        "preparing": {"ready", "cancelled"},
        "ready": {"served"},
    }
    if payload.status != ticket.status and payload.status not in allowed.get(ticket.status, set()):
        raise HTTPException(status_code=409, detail="Invalid kitchen transition")
    ticket.status = payload.status
    if payload.status == "preparing":
        ticket.started_at = utcnow()
    if payload.status == "ready":
        ticket.ready_at = utcnow()
    order = load_order(db, ticket.order_id, for_update=True)
    sibling_statuses = list(
        db.scalars(select(KitchenTicket.status).where(KitchenTicket.order_id == order.id, KitchenTicket.id != ticket.id))
    ) + [payload.status]
    previous_order_status = order.status
    if sibling_statuses and all(value in {"ready", "served"} for value in sibling_statuses):
        order.status = "ready"
    elif any(value == "preparing" for value in sibling_statuses):
        order.status = "preparing"
    order.version += 1
    if previous_order_status != "ready" and order.status == "ready":
        create_integration_event(db, order, "order.ready", {"status": "ready"})
    audit(db, user, f"kitchen.{payload.status}", "kitchen_ticket", ticket.id, ticket.business_id)
    db.commit()
    result = serialize_ticket(ticket)
    await hub.broadcast(ticket.branch_id, "kitchen.ticket_updated", result)
    return result


@api.post("/kitchen/tickets/{ticket_id}/print", tags=["kitchen"])
def register_ticket_print(
    ticket_id: int,
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager", "kitchen", "cashier")),
    db: Session = Depends(get_db),
):
    ticket = db.get(KitchenTicket, ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Kitchen ticket not found")
    branch_for_user(db, user, ticket.branch_id)
    ticket.print_count += 1
    db.commit()
    return serialize_ticket(ticket)


@api.get("/cash/registers", tags=["cash"])
def list_registers(
    branch_id: int,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch_for_user(db, user, branch_id)
    return [
        {"id": item.id, "branch_id": item.branch_id, "name": item.name, "active": item.active}
        for item in db.scalars(select(CashRegister).where(CashRegister.branch_id == branch_id))
    ]


@api.post("/cash/registers", status_code=201, tags=["cash"])
def create_register(
    payload: RegisterCreate,
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager")),
    db: Session = Depends(get_db),
):
    branch = branch_for_user(db, user, payload.branch_id)
    register = CashRegister(business_id=branch.business_id, **payload.model_dump())
    db.add(register)
    db.commit()
    db.refresh(register)
    return {"id": register.id, "branch_id": register.branch_id, "name": register.name, "active": register.active}


@api.get("/cash/sessions", tags=["cash"])
def list_cash_sessions(
    branch_id: int,
    status: str | None = None,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch_for_user(db, user, branch_id)
    statement = select(CashSession).where(CashSession.branch_id == branch_id).order_by(CashSession.opened_at.desc())
    if status:
        statement = statement.where(CashSession.status == status)
    return [
        {
            "id": item.id,
            "register_id": item.register_id,
            "branch_id": item.branch_id,
            "status": item.status,
            "opening_amount": float(item.opening_amount),
            "expected_amount": float(cash_session_expected(db, item) if item.status == "open" else item.expected_amount),
            "declared_amount": float(item.declared_amount) if item.declared_amount is not None else None,
            "difference": float(item.difference) if item.difference is not None else None,
            "opened_at": item.opened_at,
            "closed_at": item.closed_at,
        }
        for item in db.scalars(statement)
    ]


@api.post("/cash/sessions/open", status_code=201, tags=["cash"])
def open_cash_session(
    payload: CashSessionOpen,
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager", "cashier")),
    db: Session = Depends(get_db),
):
    register = db.get(CashRegister, payload.register_id)
    if not register:
        raise HTTPException(status_code=404, detail="Cash register not found")
    branch = branch_for_user(db, user, register.branch_id)
    existing = db.scalar(
        select(CashSession).where(CashSession.register_id == register.id, CashSession.status == "open")
    )
    if existing:
        raise HTTPException(status_code=409, detail="Cash register already has an open session")
    session = CashSession(
        business_id=branch.business_id,
        branch_id=branch.id,
        register_id=register.id,
        opening_amount=money(payload.opening_amount),
        opened_by=user.user_id,
    )
    db.add(session)
    db.flush()
    audit(db, user, "cash.opened", "cash_session", session.id, branch.business_id)
    db.commit()
    return {"id": session.id, "status": session.status, "opening_amount": float(session.opening_amount)}


@api.post("/cash/sessions/{session_id}/movements", status_code=201, tags=["cash"])
def create_cash_movement(
    session_id: int,
    payload: CashMovementCreate,
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager", "cashier")),
    db: Session = Depends(get_db),
):
    session = db.get(CashSession, session_id)
    if not session or session.status != "open":
        raise HTTPException(status_code=404, detail="Open cash session not found")
    branch_for_user(db, user, session.branch_id)
    movement = CashMovement(
        cash_session_id=session.id,
        movement_type=payload.movement_type,
        amount=money(payload.amount),
        payment_method=payload.payment_method,
        note=payload.note,
        created_by=user.user_id,
    )
    db.add(movement)
    audit(db, user, "cash.movement_created", "cash_session", session.id, session.business_id)
    db.commit()
    return {"id": movement.id, "expected_amount": float(cash_session_expected(db, session))}


@api.post("/cash/sessions/{session_id}/close", tags=["cash"])
def close_cash_session(
    session_id: int,
    payload: CashSessionClose,
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager", "cashier")),
    db: Session = Depends(get_db),
):
    session = db.scalar(select(CashSession).where(CashSession.id == session_id).with_for_update())
    if not session or session.status != "open":
        raise HTTPException(status_code=404, detail="Open cash session not found")
    branch_for_user(db, user, session.branch_id)
    expected = cash_session_expected(db, session)
    session.expected_amount = expected
    session.declared_amount = money(payload.declared_amount)
    session.difference = money(session.declared_amount - expected)
    session.status = "closed"
    session.closed_at = utcnow()
    session.closed_by = user.user_id
    session.close_notes = payload.notes
    audit(db, user, "cash.closed", "cash_session", session.id, session.business_id, {"difference": float(session.difference)})
    db.commit()
    return {
        "id": session.id,
        "status": session.status,
        "expected_amount": float(session.expected_amount),
        "declared_amount": float(session.declared_amount),
        "difference": float(session.difference),
    }


@api.get("/reservations/availability", tags=["reservations"])
def reservation_availability(
    branch_id: int,
    start_at: datetime,
    party_size: int = Query(ge=1, le=100),
    duration_minutes: int = Query(default=90, ge=15, le=480),
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch_for_user(db, user, branch_id)
    tables = available_tables(db, branch_id, start_at, duration_minutes, party_size)
    return {"available": bool(tables), "tables": [serialize_table(table) for table in tables]}


@api.get("/reservations", tags=["reservations"])
def list_reservations(
    branch_id: int,
    start: datetime | None = None,
    end: datetime | None = None,
    status: str | None = None,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch_for_user(db, user, branch_id)
    statement = select(Reservation).where(Reservation.branch_id == branch_id).order_by(Reservation.start_at)
    if start:
        statement = statement.where(Reservation.end_at >= start)
    if end:
        statement = statement.where(Reservation.start_at <= end)
    if status:
        statement = statement.where(Reservation.status == status)
    return [serialize_reservation(db, item) for item in db.scalars(statement)]


@api.post("/reservations", status_code=201, tags=["reservations"])
async def create_reservation_endpoint(
    payload: ReservationCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager", "cashier", "waiter")),
    db: Session = Depends(get_db),
):
    scope = f"reservation:{payload.branch_id}"
    existing = get_idempotent_response(db, scope, idempotency_key)
    if existing:
        return existing
    reservation = create_reservation(db, user, payload)
    db.flush()
    result = serialize_reservation(db, reservation)
    save_idempotent_response(db, scope, idempotency_key, reservation.business_id, result)
    db.commit()
    await hub.broadcast(reservation.branch_id, "reservation.created", result)
    return result


@api.patch("/reservations/{reservation_id}", tags=["reservations"])
async def update_reservation_endpoint(
    reservation_id: int,
    payload: ReservationUpdate,
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager", "cashier", "waiter")),
    db: Session = Depends(get_db),
):
    reservation = db.scalar(select(Reservation).where(Reservation.id == reservation_id).with_for_update())
    if not reservation:
        raise HTTPException(status_code=404, detail="Reservation not found")
    branch_for_user(db, user, reservation.branch_id)
    assert_version(reservation.version, payload.expected_version)
    values = payload.model_dump(exclude_unset=True, exclude={"expected_version", "duration_minutes", "table_ids"})
    start_at = payload.start_at or reservation.start_at
    duration = payload.duration_minutes or int((reservation.end_at - reservation.start_at).total_seconds() / 60)
    end_at = start_at + timedelta(minutes=duration)
    table_ids = payload.table_ids
    if table_ids is not None:
        conflicts = db.scalar(
            select(func.count(ReservationTable.table_id))
            .join(Reservation, Reservation.id == ReservationTable.reservation_id)
            .where(
                ReservationTable.table_id.in_(table_ids),
                Reservation.id != reservation.id,
                Reservation.status.in_(["confirmed", "seated"]),
                Reservation.start_at < end_at,
                Reservation.end_at > start_at,
            )
        )
        if conflicts:
            raise HTTPException(status_code=409, detail="One or more tables are already reserved")
        for link in db.scalars(select(ReservationTable).where(ReservationTable.reservation_id == reservation.id)):
            db.delete(link)
        for table_id in table_ids:
            table = db.get(RestaurantTable, table_id)
            if not table or table.branch_id != reservation.branch_id:
                raise HTTPException(status_code=422, detail="Invalid reservation table")
            db.add(ReservationTable(reservation_id=reservation.id, table_id=table_id))
    for key, value in values.items():
        setattr(reservation, key, value)
    reservation.start_at = start_at
    reservation.end_at = end_at
    reservation.version += 1
    if payload.status == "seated":
        for table_id in db.scalars(
            select(ReservationTable.table_id).where(ReservationTable.reservation_id == reservation.id)
        ):
            table = db.get(RestaurantTable, table_id)
            if table:
                table.status = "occupied"
                table.version += 1
    if payload.status in {"completed", "cancelled", "no_show"}:
        for table_id in db.scalars(
            select(ReservationTable.table_id).where(ReservationTable.reservation_id == reservation.id)
        ):
            table = db.get(RestaurantTable, table_id)
            if table and table.status == "reserved":
                table.status = "available"
                table.version += 1
    audit(db, user, "reservation.updated", "reservation", reservation.id, reservation.business_id, values)
    db.commit()
    result = serialize_reservation(db, reservation)
    await hub.broadcast(reservation.branch_id, "reservation.updated", result)
    return result


@api.get("/delivery/couriers", tags=["delivery"])
def list_couriers(
    branch_id: int,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch_for_user(db, user, branch_id)
    return [
        {"id": item.id, "name": item.name, "phone": item.phone, "status": item.status, "active": item.active}
        for item in db.scalars(select(Courier).where(Courier.branch_id == branch_id).order_by(Courier.name))
    ]


@api.post("/delivery/couriers", status_code=201, tags=["delivery"])
def create_courier(
    payload: CourierCreate,
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager", "dispatcher")),
    db: Session = Depends(get_db),
):
    branch = branch_for_user(db, user, payload.branch_id)
    courier = Courier(business_id=branch.business_id, **payload.model_dump())
    db.add(courier)
    db.commit()
    db.refresh(courier)
    return {"id": courier.id, "name": courier.name, "phone": courier.phone, "status": courier.status}


@api.get("/delivery/orders", tags=["delivery"])
def list_delivery_orders(
    branch_id: int,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch_for_user(db, user, branch_id)
    orders = list(
        db.scalars(
            select(Order)
            .where(Order.branch_id == branch_id, Order.channel == "delivery", Order.status != "closed")
            .options(selectinload(Order.items))
            .order_by(Order.created_at.desc())
        )
    )
    assignments = {
        item.order_id: item
        for item in db.scalars(select(DeliveryAssignment).where(DeliveryAssignment.branch_id == branch_id))
    }
    result = []
    for order in orders:
        assignment = assignments.get(order.id)
        result.append(
            {
                "order": serialize_order(order),
                "delivery": {
                    "id": assignment.id,
                    "courier_id": assignment.courier_id,
                    "status": assignment.status,
                    "tracking_code": assignment.tracking_code,
                    "estimated_at": assignment.estimated_at,
                }
                if assignment
                else None,
            }
        )
    return result


@api.post("/delivery/orders/{order_id}/assign", tags=["delivery"])
async def assign_delivery(
    order_id: int,
    payload: DeliveryAssign,
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager", "dispatcher")),
    db: Session = Depends(get_db),
):
    order = load_order(db, order_id)
    ensure_branch_scope(user, order.business_id, order.branch_id)
    if order.channel != "delivery":
        raise HTTPException(status_code=422, detail="Order is not a delivery")
    if payload.courier_id:
        courier = db.get(Courier, payload.courier_id)
        if not courier or courier.branch_id != order.branch_id or not courier.active:
            raise HTTPException(status_code=422, detail="Courier is not available for this branch")
        courier.status = "busy"
    assignment = db.scalar(select(DeliveryAssignment).where(DeliveryAssignment.order_id == order.id))
    if not assignment:
        assignment = DeliveryAssignment(
            business_id=order.business_id,
            branch_id=order.branch_id,
            order_id=order.id,
            address=order.delivery_address or {},
            fee=order.delivery_fee,
            tracking_code=uuid4().hex[:12].upper(),
        )
        db.add(assignment)
    assignment.courier_id = payload.courier_id
    assignment.estimated_at = payload.estimated_at
    assignment.status = "assigned" if payload.courier_id else "ready"
    audit(db, user, "delivery.assigned", "order", order.id, order.business_id)
    db.commit()
    result = {"order_id": order.id, "courier_id": assignment.courier_id, "status": assignment.status, "tracking_code": assignment.tracking_code}
    await hub.broadcast(order.branch_id, "delivery.updated", result)
    return result


@api.post("/delivery/orders/{order_id}/transition", tags=["delivery"])
async def transition_delivery(
    order_id: int,
    payload: DeliveryTransition,
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager", "dispatcher")),
    db: Session = Depends(get_db),
):
    order = load_order(db, order_id, for_update=True)
    ensure_branch_scope(user, order.business_id, order.branch_id)
    assignment = db.scalar(select(DeliveryAssignment).where(DeliveryAssignment.order_id == order.id))
    if not assignment:
        raise HTTPException(status_code=404, detail="Delivery assignment not found")
    allowed = {
        "preparing": {"ready", "cancelled"},
        "ready": {"assigned", "dispatched", "cancelled"},
        "assigned": {"dispatched", "cancelled"},
        "dispatched": {"delivered", "cancelled"},
    }
    if payload.status != assignment.status and payload.status not in allowed.get(assignment.status, set()):
        raise HTTPException(status_code=409, detail="Invalid delivery transition")
    previous_status = assignment.status
    assignment.status = payload.status
    if payload.status == "dispatched":
        assignment.dispatched_at = utcnow()
        order.status = "dispatched"
    if payload.status == "delivered":
        assignment.delivered_at = utcnow()
        order.status = "delivered"
        if assignment.courier_id:
            courier = db.get(Courier, assignment.courier_id)
            if courier:
                courier.status = "available"
    if payload.status == "cancelled":
        transition_order(db, user, order, "cancelled")
    if payload.status != previous_status and payload.status in {"dispatched", "delivered", "cancelled"}:
        create_integration_event(
            db,
            order,
            f"order.{payload.status}",
            {"status": payload.status},
        )
    order.version += 1
    audit(db, user, f"delivery.{payload.status}", "order", order.id, order.business_id)
    db.commit()
    result = {"order_id": order.id, "status": assignment.status, "order_status": order.status}
    await hub.broadcast(order.branch_id, "delivery.updated", result)
    return result


@api.get("/reports/daily", tags=["reports"])
def daily_report(
    branch_id: int,
    day: date | None = None,
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager", "cashier")),
    db: Session = Depends(get_db),
):
    branch = branch_for_user(db, user, branch_id)
    from zoneinfo import ZoneInfo

    local_day = day or datetime.now(ZoneInfo("America/Lima")).date()
    local_start = datetime.combine(local_day, time.min, tzinfo=ZoneInfo("America/Lima"))
    local_end = local_start + timedelta(days=1)
    start_utc = local_start.astimezone(timezone.utc)
    end_utc = local_end.astimezone(timezone.utc)
    orders = list(
        db.scalars(
            select(Order)
            .where(
                Order.branch_id == branch.id,
                Order.created_at >= start_utc,
                Order.created_at < end_utc,
                Order.status != "cancelled",
            )
            .options(selectinload(Order.items))
        )
    )
    closed_orders = [order for order in orders if order.status == "closed"]
    payments = list(
        db.scalars(
            select(Payment)
            .join(Order, Order.id == Payment.order_id)
            .where(
                Order.branch_id == branch.id,
                Payment.received_at >= start_utc,
                Payment.received_at < end_utc,
                Payment.status == "confirmed",
            )
        )
    )
    by_channel: dict[str, float] = {}
    product_totals: dict[str, dict] = {}
    for order in closed_orders:
        by_channel[order.channel] = by_channel.get(order.channel, 0) + float(order.total)
        for item in order.items:
            entry = product_totals.setdefault(item.product_name, {"name": item.product_name, "quantity": 0.0, "sales": 0.0})
            entry["quantity"] += float(item.quantity)
            entry["sales"] += float(item.line_total)
    by_payment: dict[str, float] = {}
    for payment in payments:
        by_payment[payment.method] = by_payment.get(payment.method, 0) + float(payment.amount)
    gross_sales = sum(float(order.total) for order in closed_orders)
    return {
        "day": local_day.isoformat(),
        "branch": serialize_branch(branch),
        "orders": len(orders),
        "closed_orders": len(closed_orders),
        "gross_sales": round(gross_sales, 2),
        "average_ticket": round(gross_sales / len(closed_orders), 2) if closed_orders else 0,
        "by_channel": by_channel,
        "by_payment_method": by_payment,
        "top_products": sorted(product_totals.values(), key=lambda item: item["sales"], reverse=True)[:10],
    }


def evidence_values(metadata: dict, analysis: dict) -> dict:
    def parsed_datetime(value):
        if isinstance(value, datetime):
            return value
        if isinstance(value, str) and value:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        return None

    amount = metadata.get("amount_detected")
    if amount is None:
        amount = analysis.get("amount")
    confidence = metadata.get("confidence")
    if confidence is None:
        confidence = analysis.get("confidence")
    return {
        "provider": metadata.get("provider") or analysis.get("provider") or "unknown",
        "amount_detected": money(amount) if amount not in {None, ""} else None,
        "operation_number": metadata.get("operation_number") or analysis.get("operation_number"),
        "security_code": metadata.get("security_code") or analysis.get("security_code"),
        "whatsapp_message_id": metadata.get("whatsapp_message_id"),
        "occurred_at": parsed_datetime(metadata.get("occurred_at") or analysis.get("occurred_at")),
        "recipient": metadata.get("recipient") or analysis.get("recipient"),
        "confidence": Decimal(str(confidence)) if confidence not in {None, ""} else None,
        "warnings": analysis.get("warnings") if isinstance(analysis.get("warnings"), list) else [],
    }


def determine_evidence_status(db: Session, order: Order, values: dict) -> str:
    # Vision extracts metadata, but a human remains the source of truth for payment approval.
    return "under_review"


def serialize_payment_evidence(evidence: PaymentEvidence) -> dict:
    return {
        "id": evidence.id,
        "business_id": evidence.business_id,
        "order_id": evidence.order_id,
        "provider": evidence.provider,
        "amount_detected": float(evidence.amount_detected) if evidence.amount_detected is not None else None,
        "operation_number": evidence.operation_number,
        "security_code": evidence.security_code,
        "whatsapp_message_id": evidence.whatsapp_message_id,
        "occurred_at": evidence.occurred_at,
        "recipient": evidence.recipient,
        "confidence": float(evidence.confidence) if evidence.confidence is not None else None,
        "status": evidence.status,
        "rejection_reason": evidence.rejection_reason,
        "reviewed_by": evidence.reviewed_by,
        "reviewed_at": evidence.reviewed_at,
        "analysis": evidence.analysis,
        "warnings": evidence.warnings,
        "image_url": f"/api/v1/payment-evidence/{evidence.id}/image",
        "created_at": evidence.created_at,
        "updated_at": evidence.updated_at,
    }


@api.get("/payment-evidence", tags=["payments"])
def list_payment_evidence(
    branch_id: int,
    status_filter: str | None = Query(default=None, alias="status"),
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    branch_for_user(db, user, branch_id)
    statement = (
        select(PaymentEvidence)
        .join(Order, Order.id == PaymentEvidence.order_id)
        .where(Order.branch_id == branch_id)
        .order_by(PaymentEvidence.created_at.desc())
    )
    if status_filter:
        statement = statement.where(PaymentEvidence.status == status_filter)
    return [serialize_payment_evidence(item) for item in db.scalars(statement)]


@api.get("/payment-evidence/{evidence_id}/image", tags=["payments"])
async def get_payment_evidence_image(
    evidence_id: int,
    user: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    evidence = db.get(PaymentEvidence, evidence_id)
    if not evidence:
        raise HTTPException(status_code=404, detail="Payment evidence not found")
    order = load_order(db, evidence.order_id)
    ensure_branch_scope(user, order.business_id, order.branch_id)
    try:
        data, content_type = await load_private_file(evidence.storage_path)
    except (FileNotFoundError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=404, detail="Payment evidence image not found") from exc
    return Response(content=data, media_type=content_type, headers={"Cache-Control": "private, no-store"})


@api.post("/orders/{order_id}/payment-evidence", status_code=201, tags=["payments"])
async def upload_payment_evidence(
    order_id: int,
    file: UploadFile = File(...),
    provider: str = Form(...),
    amount_detected: Decimal | None = Form(default=None),
    operation_number: str | None = Form(default=None),
    security_code: str | None = Form(default=None),
    whatsapp_message_id: str | None = Form(default=None),
    occurred_at: datetime | None = Form(default=None),
    recipient: str | None = Form(default=None),
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager", "cashier", "waiter")),
    db: Session = Depends(get_db),
):
    order = load_order(db, order_id, for_update=True)
    ensure_branch_scope(user, order.business_id, order.branch_id)
    content_type = file.content_type or "application/octet-stream"
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=422, detail="Payment evidence must be an image")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="Payment evidence image is empty")
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Payment evidence exceeds 10 MB")
    storage_path = await store_private_file(data, file.filename or "evidence.png", content_type)
    analysis = await analyze_payment_image(data, content_type)
    metadata = {
        "provider": provider.lower(),
        "amount_detected": amount_detected,
        "operation_number": operation_number,
        "security_code": security_code,
        "whatsapp_message_id": whatsapp_message_id,
        "occurred_at": occurred_at,
        "recipient": recipient,
    }
    values = evidence_values(metadata, analysis)
    evidence = PaymentEvidence(
        business_id=order.business_id,
        order_id=order.id,
        storage_path=storage_path,
        image_sha256=hashlib.sha256(data).hexdigest(),
        analysis=analysis,
        **values,
    )
    evidence.status = determine_evidence_status(db, order, values)
    db.add(evidence)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Payment operation number was already used") from exc
    if order.status in {"draft", "pending_confirmation"}:
        confirm_order(db, user, order)
    order.payment_method = values["provider"]
    order.payment_status = "evidence_received"
    order.submitted_at = order.submitted_at or utcnow()
    order.version += 1
    audit(db, user, "payment_evidence.created", "payment_evidence", evidence.id, order.business_id)
    db.commit()
    result = serialize_payment_evidence(evidence)
    await hub.broadcast(order.branch_id, "payment_evidence.created", result)
    return result


@api.post("/payment-evidence/{evidence_id}/review", tags=["payments"])
async def review_payment_evidence(
    evidence_id: int,
    payload: EvidenceReview,
    user: AuthContext = Depends(require_roles("superadmin", "owner", "manager", "cashier")),
    db: Session = Depends(get_db),
):
    evidence = db.scalar(select(PaymentEvidence).where(PaymentEvidence.id == evidence_id).with_for_update())
    if not evidence:
        raise HTTPException(status_code=404, detail="Payment evidence not found")
    order = load_order(db, evidence.order_id, for_update=True)
    ensure_branch_scope(user, order.business_id, order.branch_id)
    if evidence.status in {"paid", "rejected"}:
        return {"id": evidence.id, "status": evidence.status, "order": serialize_order(order)}
    evidence.reviewed_by = user.user_id
    evidence.reviewed_at = utcnow()
    if not payload.approve:
        evidence.status = "rejected"
        evidence.rejection_reason = payload.note or "Rejected by cashier"
        order.payment_status = "rejected"
        if order.status not in {"cancelled", "closed"}:
            if order.status in {"confirmed", "sent_to_kitchen", "preparing", "ready"}:
                reverse_order_stock(db, user, order)
            order.status = "cancelled"
            order.version += 1
        create_integration_event(
            db,
            order,
            "payment.rejected",
            {"evidence_id": evidence.id, "reason": evidence.rejection_reason},
        )
    else:
        if not evidence.image_sha256 or not evidence.storage_path:
            raise HTTPException(status_code=409, detail="Payment evidence has no persisted image")
        amount = order.total
        remaining = money(order.total) - money(
            db.scalar(
                select(func.coalesce(func.sum(Payment.amount), 0)).where(
                    Payment.order_id == order.id,
                    Payment.status == "confirmed",
                )
            )
        )
        amount = min(money(amount), remaining)
        if amount <= 0:
            raise HTTPException(status_code=409, detail="Order has no outstanding balance")
        payment = add_payment(
            db,
            user,
            order,
            PaymentCreate(
                method=evidence.provider if evidence.provider in {"yape", "plin"} else "transfer",
                amount=amount,
                external_reference=evidence.operation_number,
                note=payload.note or "Approved payment evidence",
            ),
        )
        evidence.status = "paid"
        tickets = send_order_to_kitchen(db, user, order)
        create_integration_event(
            db,
            order,
            "payment.approved",
            {
                "evidence_id": evidence.id,
                "payment_id": payment.id,
                "status": order.status,
                "message": (
                    f"¡Pago confirmado! Tu pedido #{order.number} fue aprobado y ya está en preparación. "
                    "Te avisaremos cuando esté listo. 🍕"
                ),
            },
        )
    audit(db, user, f"payment_evidence.{evidence.status}", "payment_evidence", evidence.id, evidence.business_id)
    db.commit()
    result = {
        "evidence": serialize_payment_evidence(evidence),
        "order": serialize_order(load_order(db, order.id)),
    }
    await hub.broadcast(order.branch_id, "payment_evidence.reviewed", result)
    return result


@api.get("/public/{business_slug}/menu", tags=["public"])
def public_menu(
    business_slug: str,
    branch_slug: str | None = None,
    db: Session = Depends(get_db),
):
    business = db.scalar(select(Business).where(Business.slug == business_slug, Business.status == "active"))
    if not business:
        raise HTTPException(status_code=404, detail="Restaurant not found")
    branch_statement = select(Branch).where(Branch.business_id == business.id, Branch.active.is_(True))
    if branch_slug:
        branch_statement = branch_statement.where(Branch.slug == branch_slug)
    branch = db.scalar(branch_statement.order_by(Branch.id))
    if not branch:
        raise HTTPException(status_code=404, detail="Restaurant branch not found")
    categories = list(db.scalars(select(Category).where(Category.branch_id == branch.id, Category.active.is_(True))))
    products = list(
        db.scalars(
            select(Product).where(Product.branch_id == branch.id, Product.available.is_(True)).order_by(Product.sort_order, Product.name)
        )
    )
    return {
        "business": {"id": business.id, "slug": business.slug, "name": business.name, "logo_url": business.logo_url},
        "branch": serialize_branch(branch),
        "categories": [{"id": item.id, "name": item.name, "color": item.color} for item in categories],
        "products": [
            {
                "id": item.id,
                "category_id": item.category_id,
                "name": item.name,
                "description": item.description,
                "price": float(item.price),
                "image_url": item.image_url,
            }
            for item in products
        ],
    }


@api.get("/public/{business_slug}/reservations/availability", tags=["public"])
def public_reservation_availability(
    business_slug: str,
    branch_id: int,
    start_at: datetime,
    party_size: int = Query(ge=1, le=30),
    duration_minutes: int = Query(default=90, ge=15, le=240),
    db: Session = Depends(get_db),
):
    business = db.scalar(select(Business).where(Business.slug == business_slug, Business.status == "active"))
    branch = db.get(Branch, branch_id)
    if not business or not branch or branch.business_id != business.id:
        raise HTTPException(status_code=404, detail="Restaurant branch not found")
    tables = available_tables(db, branch.id, start_at, duration_minutes, party_size)
    return {"available": bool(tables), "suggested_table_ids": [table.id for table in tables[:3]]}


@api.post("/public/{business_slug}/orders", status_code=201, tags=["public"])
async def public_create_order(
    business_slug: str,
    payload: PublicOrderCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
):
    business = db.scalar(select(Business).where(Business.slug == business_slug, Business.status == "active"))
    branch = db.get(Branch, payload.branch_id)
    if not business or not branch or branch.business_id != business.id or not branch.active:
        raise HTTPException(status_code=404, detail="Restaurant branch not found")
    if payload.fulfillment == "delivery" and not branch.delivery_enabled:
        raise HTTPException(status_code=409, detail="Delivery is not enabled for this branch")
    if payload.fulfillment == "takeaway" and not branch.takeaway_enabled:
        raise HTTPException(status_code=409, detail="Takeaway is not enabled for this branch")
    if payload.fulfillment == "delivery" and not payload.delivery_address:
        raise HTTPException(status_code=422, detail="Delivery address is required")
    if any(item.product_id is None for item in payload.items):
        raise HTTPException(status_code=422, detail="Public orders only accept catalog products")
    scope = f"public-order:{business.id}:{branch.id}"
    existing = get_idempotent_response(db, scope, idempotency_key)
    if existing:
        return existing
    system_user = AuthContext("public-store", "owner", business.id, branch.id)
    order = create_order(
        db,
        system_user,
        OrderCreate(
            branch_id=branch.id,
            channel=payload.fulfillment,
            source="public_store",
            customer_name=payload.customer_name,
            customer_phone=payload.customer_phone,
            delivery_address=payload.delivery_address,
            delivery_fee=branch.delivery_fee if payload.fulfillment == "delivery" else Decimal("0"),
            notes=payload.notes,
            items=payload.items,
        ),
    )
    order.status = "pending_confirmation"
    db.flush()
    result = serialize_order(order)
    save_idempotent_response(db, scope, idempotency_key, business.id, result)
    db.commit()
    result = serialize_order(load_order(db, order.id))
    await hub.broadcast(branch.id, "order.created", result)
    return result


@api.post("/public/{business_slug}/reservations", status_code=201, tags=["public"])
async def public_create_reservation(
    business_slug: str,
    payload: PublicReservationCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
):
    business = db.scalar(select(Business).where(Business.slug == business_slug, Business.status == "active"))
    branch = db.get(Branch, payload.branch_id)
    if not business or not branch or branch.business_id != business.id:
        raise HTTPException(status_code=404, detail="Restaurant branch not found")
    scope = f"public-reservation:{business.id}:{branch.id}"
    existing = get_idempotent_response(db, scope, idempotency_key)
    if existing:
        return existing
    candidates = available_tables(db, branch.id, payload.start_at, 90, payload.party_size)
    if not candidates:
        raise HTTPException(status_code=409, detail="No tables available for the requested time")
    system_user = AuthContext("public-reservation", "owner", business.id, branch.id)
    reservation = create_reservation(
        db,
        system_user,
        ReservationCreate(
            branch_id=branch.id,
            customer_name=payload.customer_name,
            customer_phone=payload.customer_phone,
            party_size=payload.party_size,
            start_at=payload.start_at,
            table_ids=[candidates[0].id],
            source="public_portal",
            notes=payload.notes,
        ),
    )
    result = serialize_reservation(db, reservation)
    save_idempotent_response(db, scope, idempotency_key, business.id, result)
    db.commit()
    await hub.broadcast(branch.id, "reservation.created", result)
    return result


def integration_user_for_branch(db: Session, branch_id: int) -> tuple[AuthContext, Branch]:
    branch = db.get(Branch, branch_id)
    if not branch or not branch.active:
        raise HTTPException(status_code=404, detail="Branch not found")
    return AuthContext("integration", "owner", branch.business_id, branch.id), branch


def integration_branch_from_auth(
    db: Session,
    integration: IntegrationAuthContext,
    requested_branch_id: int | None = None,
) -> tuple[AuthContext, Branch]:
    if integration.branch_id is not None:
        if requested_branch_id is not None and requested_branch_id != integration.branch_id:
            raise HTTPException(status_code=403, detail="Integration credential cannot access another branch")
        requested_branch_id = integration.branch_id
    if requested_branch_id is None:
        raise HTTPException(status_code=422, detail="branch_id is required for legacy integration tokens")
    user, branch = integration_user_for_branch(db, requested_branch_id)
    if integration.business_id is not None and branch.business_id != integration.business_id:
        raise HTTPException(status_code=403, detail="Integration credential cannot access another business")
    return user, branch


def ensure_integration_order_scope(integration: IntegrationAuthContext, order: Order) -> None:
    if integration.business_id is not None and integration.business_id != order.business_id:
        raise HTTPException(status_code=403, detail="Integration credential cannot access another business")
    if integration.branch_id is not None and integration.branch_id != order.branch_id:
        raise HTTPException(status_code=403, detail="Integration credential cannot access another branch")


@api.get("/integrations/context", tags=["integrations"])
def integration_context(
    branch_id: int | None = None,
    integration: IntegrationAuthContext = Depends(require_integration_scope("menu:read")),
    db: Session = Depends(get_db),
):
    _, branch = integration_branch_from_auth(db, integration, branch_id)
    business = db.get(Business, branch.business_id)
    branch_payload = serialize_branch(branch)
    branch_payload.pop("yape_qr_storage_path", None)
    return {
        "business": {
            "id": business.id,
            "slug": business.slug,
            "name": business.name,
            "currency": business.currency,
            "timezone": business.timezone,
            "phone": business.phone,
        },
        "branch": {
            **branch_payload,
            "yape_qr_configured": bool(branch.yape_qr_storage_path),
            "yape_qr_url": (
                f"/api/v1/public/{business.slug}/{branch.slug}/yape-qr"
                if branch.yape_qr_storage_path
                else None
            ),
        },
    }


@api.get("/integrations/context/yape-qr", tags=["integrations"])
async def integration_yape_qr(
    branch_id: int | None = None,
    integration: IntegrationAuthContext = Depends(require_integration_scope("menu:read")),
    db: Session = Depends(get_db),
):
    _, branch = integration_branch_from_auth(db, integration, branch_id)
    if not branch.yape_qr_storage_path:
        raise HTTPException(status_code=404, detail="Yape QR is not configured")
    try:
        data, content_type = await load_private_file(branch.yape_qr_storage_path)
    except (FileNotFoundError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=404, detail="Yape QR image not found") from exc
    return Response(content=data, media_type=content_type, headers={"Cache-Control": "private, no-store"})


@api.get("/public/{business_slug}/{branch_slug}/yape-qr", tags=["public"])
async def public_branch_yape_qr(
    business_slug: str,
    branch_slug: str,
    db: Session = Depends(get_db),
):
    business = db.scalar(
        select(Business).where(Business.slug == business_slug, Business.status == "active")
    )
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")
    branch = db.scalar(
        select(Branch).where(
            Branch.business_id == business.id,
            Branch.slug == branch_slug,
            Branch.active.is_(True),
        )
    )
    if not branch or not branch.yape_qr_storage_path:
        raise HTTPException(status_code=404, detail="Yape QR is not configured")
    try:
        data, content_type = await load_private_file(branch.yape_qr_storage_path)
    except (FileNotFoundError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=404, detail="Yape QR image not found") from exc
    return Response(
        content=data,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=300"},
    )


@api.get("/integrations/context/inventory", tags=["integrations"])
def integration_inventory(
    branch_id: int | None = None,
    integration: IntegrationAuthContext = Depends(require_integration_scope("inventory:read")),
    db: Session = Depends(get_db),
):
    _, branch = integration_branch_from_auth(db, integration, branch_id)
    inventory = list(
        db.scalars(select(InventoryItem).where(InventoryItem.branch_id == branch.id).order_by(InventoryItem.name))
    )
    products = list(
        db.scalars(select(Product).where(Product.branch_id == branch.id).order_by(Product.name))
    )
    return {
        "branch_id": branch.id,
        "inventory": [serialize_inventory(item) for item in inventory],
        "product_capacity": [
            {"product_id": product.id, "name": product.name, **product_capacity(db, product)}
            for product in products
        ],
    }


@api.post("/integrations/inventory/{item_id}/adjust", tags=["integrations"])
async def integration_adjust_inventory(
    item_id: int,
    payload: StockAdjustment,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("inventory:write")),
    db: Session = Depends(get_db),
):
    if not idempotency_key:
        raise HTTPException(status_code=422, detail="Idempotency-Key is required")
    item = db.scalar(select(InventoryItem).where(InventoryItem.id == item_id).with_for_update())
    if not item:
        raise HTTPException(status_code=404, detail="Inventory item not found")
    integration_branch_from_auth(db, integration, item.branch_id)
    scope = f"integration-inventory-adjust:{item.id}"
    existing = get_idempotent_response(db, scope, idempotency_key)
    if existing:
        return existing
    assert_version(item.version, payload.expected_version)
    next_quantity = Decimal(str(item.quantity)) + payload.quantity_delta
    if next_quantity < 0:
        raise HTTPException(status_code=409, detail="Inventory adjustment cannot make stock negative")
    item.quantity = next_quantity
    item.version += 1
    db.add(
        StockMovement(
            business_id=item.business_id,
            branch_id=item.branch_id,
            inventory_item_id=item.id,
            movement_type=payload.movement_type,
            quantity_delta=payload.quantity_delta,
            balance_after=item.quantity,
            reference_type="integration",
            reference_id=str(integration.credential_id or "legacy"),
            note=payload.note,
            created_by=f"integration:{integration.credential_id or 'legacy'}",
        )
    )
    result = serialize_inventory(item)
    save_idempotent_response(db, scope, idempotency_key, item.business_id, result)
    db.commit()
    await hub.broadcast(item.branch_id, "inventory.updated", result)
    return result


@api.get("/integrations/context/tables", tags=["integrations"])
def integration_tables(
    start_at: datetime,
    party_size: int = Query(ge=1, le=100),
    duration_minutes: int = 90,
    branch_id: int | None = None,
    integration: IntegrationAuthContext = Depends(require_integration_scope("reservations:write")),
    db: Session = Depends(get_db),
):
    _, branch = integration_branch_from_auth(db, integration, branch_id)
    tables = available_tables(db, branch.id, start_at, duration_minutes, party_size)
    return {"available": bool(tables), "tables": [serialize_table(table) for table in tables]}


@api.get(
    "/integrations/context/menu",
    tags=["integrations"],
)
def integration_menu(
    branch_id: int | None = None,
    integration: IntegrationAuthContext = Depends(require_integration_scope("menu:read")),
    db: Session = Depends(get_db),
):
    _, branch = integration_branch_from_auth(db, integration, branch_id)
    products = list(
        db.scalars(
            select(Product).where(Product.branch_id == branch.id, Product.available.is_(True)).order_by(Product.name)
        )
    )
    product_ids = [product.id for product in products]
    variants = list(
        db.scalars(select(ProductVariant).where(ProductVariant.product_id.in_(product_ids)))
    ) if product_ids else []
    links = list(
        db.execute(
            select(ProductModifierGroup.product_id, ProductModifierGroup.group_id).where(
                ProductModifierGroup.product_id.in_(product_ids)
            )
        )
    ) if product_ids else []
    group_ids = {row.group_id for row in links}
    groups = list(db.scalars(select(ModifierGroup).where(ModifierGroup.id.in_(group_ids)))) if group_ids else []
    modifiers = list(db.scalars(select(Modifier).where(Modifier.group_id.in_(group_ids)))) if group_ids else []
    return {
        "business_id": branch.business_id,
        "branch": serialize_branch(branch),
        "products": [
            {
                "id": product.id,
                "sku": product.sku,
                "name": product.name,
                "description": product.description,
                "price": float(product.price),
                "image_url": product.image_url,
                **product_capacity(db, product),
                "track_stock": product.track_stock,
                "preparation_station": product.preparation_station,
                "variants": [
                    {
                        "id": variant.id,
                        "name": variant.name,
                        "price_delta": float(variant.price_delta),
                    }
                    for variant in variants
                    if variant.product_id == product.id and variant.active
                ],
                "modifier_groups": [
                    {
                        "id": group.id,
                        "name": group.name,
                        "minimum": group.minimum,
                        "maximum": group.maximum,
                        "required": group.required,
                        "modifiers": [
                            {
                                "id": modifier.id,
                                "name": modifier.name,
                                "price_delta": float(modifier.price_delta),
                            }
                            for modifier in modifiers
                            if modifier.group_id == group.id and modifier.active
                        ],
                    }
                    for group in groups
                    if any(link.product_id == product.id and link.group_id == group.id for link in links)
                ],
            }
            for product in products
        ],
    }


@api.get(
    "/integrations/context/availability",
    tags=["integrations"],
)
def integration_availability(
    start_at: datetime,
    party_size: int = Query(ge=1, le=100),
    duration_minutes: int = 90,
    branch_id: int | None = None,
    integration: IntegrationAuthContext = Depends(require_integration_scope("reservations:write")),
    db: Session = Depends(get_db),
):
    _, branch = integration_branch_from_auth(db, integration, branch_id)
    tables = available_tables(db, branch.id, start_at, duration_minutes, party_size)
    return {"available": bool(tables), "tables": [serialize_table(table) for table in tables]}


@api.post(
    "/integrations/orders/draft",
    status_code=201,
    tags=["integrations"],
)
async def integration_create_order(
    payload: OrderCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("orders:write")),
    db: Session = Depends(get_db),
):
    if not idempotency_key:
        raise HTTPException(status_code=422, detail="Idempotency-Key is required")
    user, branch = integration_branch_from_auth(db, integration, payload.branch_id)
    scope = f"integration-order:{branch.id}"
    existing = get_idempotent_response(db, scope, idempotency_key)
    if existing:
        return existing
    payload.source = payload.source or "integration"
    order = create_order(db, user, payload)
    db.flush()
    result = serialize_order(order)
    save_idempotent_response(db, scope, idempotency_key, order.business_id, result)
    db.commit()
    await hub.broadcast(order.branch_id, "order.created", result)
    return result


@api.patch(
    "/integrations/orders/{order_id}",
    tags=["integrations"],
)
async def integration_patch_order(
    order_id: int,
    payload: OrderPatch,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("orders:write")),
    db: Session = Depends(get_db),
):
    if not idempotency_key:
        raise HTTPException(status_code=422, detail="Idempotency-Key is required")
    scope = f"integration-order-patch:{order_id}"
    existing = get_idempotent_response(db, scope, idempotency_key)
    if existing:
        return existing
    order = load_order(db, order_id, for_update=True)
    ensure_integration_order_scope(integration, order)
    user = AuthContext("integration", "owner", order.business_id, order.branch_id)
    assert_version(order.version, payload.expected_version)
    changes = payload.model_dump(exclude_unset=True, exclude={"expected_version"})
    replacement_items = changes.pop("items", None)
    if replacement_items is not None:
        if order.status not in {"draft", "pending_confirmation"}:
            raise HTTPException(
                status_code=409,
                detail="Items can only be replaced before the order is confirmed",
            )
        order.items.clear()
        for line in payload.items or []:
            order.items.append(build_order_item(db, order.business_id, order.branch_id, line))
    for key, value in changes.items():
        setattr(order, key, value)
    recalculate_order(order)
    order.version += 1
    audit(db, user, "integration.order_updated", "order", order.id, order.business_id)
    db.flush()
    result = serialize_order(order)
    save_idempotent_response(db, scope, idempotency_key, order.business_id, result)
    db.commit()
    await hub.broadcast(order.branch_id, "order.updated", result)
    return result


@api.post(
    "/integrations/orders/{order_id}/confirm",
    tags=["integrations"],
)
async def integration_confirm_order(
    order_id: int,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("orders:write")),
    db: Session = Depends(get_db),
):
    if not idempotency_key:
        raise HTTPException(status_code=422, detail="Idempotency-Key is required")
    order = load_order(db, order_id, for_update=True)
    ensure_integration_order_scope(integration, order)
    user = AuthContext("integration", "owner", order.business_id, order.branch_id)
    scope = f"integration-confirm:{order.id}"
    existing = get_idempotent_response(db, scope, idempotency_key)
    if existing:
        return existing
    confirm_order(db, user, order)
    tickets = send_order_to_kitchen(db, user, order)
    db.flush()
    result = {"order": serialize_order(order), "tickets": [serialize_ticket(ticket) for ticket in tickets]}
    save_idempotent_response(db, scope, idempotency_key, order.business_id, result)
    db.commit()
    await hub.broadcast(order.branch_id, "kitchen.ticket_created", result)
    return result


@api.post("/integrations/orders/{order_id}/cash-confirm", tags=["integrations"])
async def integration_cash_confirm_order(
    order_id: int,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("orders:write")),
    db: Session = Depends(get_db),
):
    if not idempotency_key:
        raise HTTPException(status_code=422, detail="Idempotency-Key is required")
    order = load_order(db, order_id, for_update=True)
    ensure_integration_order_scope(integration, order)
    scope = f"integration-cash-confirm:{order.id}"
    existing = get_idempotent_response(db, scope, idempotency_key)
    if existing:
        return existing
    user = AuthContext("integration", "owner", order.business_id, order.branch_id)
    order.payment_method = "cash"
    order.payment_status = "pending"
    order.submitted_at = order.submitted_at or utcnow()
    confirm_order(db, user, order)
    tickets = send_order_to_kitchen(db, user, order)
    event = create_integration_event(
        db,
        order,
        "order.cash_confirmed",
        {"payment_status": order.payment_status, "status": order.status},
    )
    result = {
        "order": serialize_order(order),
        "tickets": [serialize_ticket(ticket) for ticket in tickets],
        "event_id": event.id,
    }
    save_idempotent_response(db, scope, idempotency_key, order.business_id, result)
    db.commit()
    await hub.broadcast(order.branch_id, "kitchen.ticket_created", result)
    return result


@api.post("/integrations/orders/{order_id}/request-human", tags=["integrations"])
async def integration_request_human(
    order_id: int,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    reason: str = Query(default="customer_requested_human", max_length=240),
    integration: IntegrationAuthContext = Depends(require_integration_scope("orders:write")),
    db: Session = Depends(get_db),
):
    if not idempotency_key:
        raise HTTPException(status_code=422, detail="Idempotency-Key is required")
    order = load_order(db, order_id, for_update=True)
    ensure_integration_order_scope(integration, order)
    scope = f"integration-human-request:{order.id}"
    existing = get_idempotent_response(db, scope, idempotency_key)
    if existing:
        return existing
    event = create_integration_event(db, order, "human.requested", {"reason": reason})
    result = {"event_id": event.id, "order_id": order.id, "status": "queued"}
    save_idempotent_response(db, scope, idempotency_key, order.business_id, result)
    db.commit()
    await hub.broadcast(order.branch_id, "human.requested", result)
    return result


@api.post(
    "/integrations/orders/{order_id}/payment-evidence",
    status_code=201,
    tags=["integrations"],
)
async def integration_upload_payment_evidence(
    order_id: int,
    file: UploadFile = File(...),
    provider: str = Form(default="yape"),
    amount_detected: Decimal | None = Form(default=None),
    operation_number: str | None = Form(default=None),
    security_code: str | None = Form(default=None),
    occurred_at: datetime | None = Form(default=None),
    recipient: str | None = Form(default=None),
    whatsapp_message_id: str | None = Form(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("payments:write")),
    db: Session = Depends(get_db),
):
    if not idempotency_key:
        raise HTTPException(status_code=422, detail="Idempotency-Key is required")
    if provider.lower() not in {"yape", "plin"}:
        raise HTTPException(status_code=422, detail="provider must be yape or plin")
    if security_code is not None and (len(security_code) != 3 or not security_code.isdigit()):
        raise HTTPException(status_code=422, detail="security_code must contain exactly three digits")
    order = load_order(db, order_id, for_update=True)
    ensure_integration_order_scope(integration, order)
    scope = f"integration-evidence-upload:{order.id}"
    existing = get_idempotent_response(db, scope, idempotency_key)
    if existing:
        return existing
    content_type = file.content_type or "application/octet-stream"
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=422, detail="A real payment evidence image is required")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="Payment evidence image is empty")
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Payment evidence exceeds 10 MB")
    image_sha256 = hashlib.sha256(data).hexdigest()
    duplicate_image = db.scalar(
        select(PaymentEvidence).where(
            PaymentEvidence.business_id == order.business_id,
            PaymentEvidence.image_sha256 == image_sha256,
        )
    )
    if duplicate_image:
        raise HTTPException(status_code=409, detail="This payment evidence image was already used")
    storage_path = await store_private_file(data, file.filename or "evidence.png", content_type)
    analysis = await analyze_payment_image(data, content_type)
    values = evidence_values(
        {
            "provider": provider.lower(),
            "amount_detected": amount_detected,
            "operation_number": operation_number,
            "security_code": security_code,
            "occurred_at": occurred_at,
            "recipient": recipient,
            "whatsapp_message_id": whatsapp_message_id,
        },
        analysis,
    )
    evidence = PaymentEvidence(
        business_id=order.business_id,
        order_id=order.id,
        storage_path=storage_path,
        image_sha256=image_sha256,
        analysis=analysis,
        status="under_review",
        **values,
    )
    db.add(evidence)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Payment operation number was already used") from exc
    user = AuthContext("integration", "owner", order.business_id, order.branch_id)
    if order.status in {"draft", "pending_confirmation"}:
        confirm_order(db, user, order)
    order.payment_method = provider.lower()
    order.payment_status = "evidence_received"
    order.submitted_at = order.submitted_at or utcnow()
    order.version += 1
    result = {
        "evidence": serialize_payment_evidence(evidence),
        "order": serialize_order(order),
        "requires_human_review": True,
    }
    save_idempotent_response(db, scope, idempotency_key, order.business_id, result)
    audit(db, user, "integration.payment_evidence.created", "payment_evidence", evidence.id, order.business_id)
    db.commit()
    await hub.broadcast(order.branch_id, "payment_evidence.created", result)
    return result


@api.get(
    "/integrations/orders/{order_id}/status",
    tags=["integrations"],
)
def integration_order_status(
    order_id: int,
    integration: IntegrationAuthContext = Depends(require_integration_scope("orders:read")),
    db: Session = Depends(get_db),
):
    order = load_order(db, order_id)
    ensure_integration_order_scope(integration, order)
    return serialize_order(order)


@api.get("/integrations/events", tags=["integrations"])
def integration_events(
    branch_id: int | None = None,
    pending_only: bool = True,
    limit: int = Query(default=50, ge=1, le=200),
    integration: IntegrationAuthContext = Depends(require_integration_scope("events:read")),
    db: Session = Depends(get_db),
):
    _, branch = integration_branch_from_auth(db, integration, branch_id)
    statement = (
        select(IntegrationEvent)
        .where(
            IntegrationEvent.branch_id == branch.id,
            IntegrationEvent.available_at <= utcnow(),
        )
        .order_by(IntegrationEvent.created_at)
        .limit(limit)
    )
    if pending_only:
        statement = statement.where(IntegrationEvent.acknowledged_at.is_(None))
    events = list(db.scalars(statement))
    for event in events:
        event.delivery_attempts += 1
    db.commit()
    return [
        {
            "id": event.id,
            "event_type": event.event_type,
            "aggregate_type": event.aggregate_type,
            "aggregate_id": event.aggregate_id,
            "customer_phone": event.customer_phone,
            "whatsapp_chat_id": event.whatsapp_chat_id,
            "payload": event.payload,
            "delivery_attempts": event.delivery_attempts,
            "created_at": event.created_at,
        }
        for event in events
    ]


@api.post("/integrations/events/{event_id}/ack", tags=["integrations"])
def acknowledge_integration_event(
    event_id: str,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("events:read")),
    db: Session = Depends(get_db),
):
    if not idempotency_key:
        raise HTTPException(status_code=422, detail="Idempotency-Key is required")
    event = db.scalar(select(IntegrationEvent).where(IntegrationEvent.id == event_id).with_for_update())
    if not event:
        raise HTTPException(status_code=404, detail="Integration event not found")
    if integration.branch_id is not None and event.branch_id != integration.branch_id:
        raise HTTPException(status_code=403, detail="Integration credential cannot access another branch")
    scope = f"integration-event-ack:{event.id}"
    existing = get_idempotent_response(db, scope, idempotency_key)
    if existing:
        return existing
    event.acknowledged_at = event.acknowledged_at or utcnow()
    event.acknowledged_by = str(integration.credential_id or "legacy-integration")
    result = {"id": event.id, "acknowledged_at": event.acknowledged_at}
    save_idempotent_response(db, scope, idempotency_key, event.business_id, result)
    db.commit()
    return result


@api.post(
    "/integrations/reservations",
    status_code=201,
    tags=["integrations"],
)
async def integration_create_reservation(
    payload: ReservationCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("reservations:write")),
    db: Session = Depends(get_db),
):
    if not idempotency_key:
        raise HTTPException(status_code=422, detail="Idempotency-Key is required")
    user, branch = integration_branch_from_auth(db, integration, payload.branch_id)
    scope = f"integration-reservation:{branch.id}"
    existing = get_idempotent_response(db, scope, idempotency_key)
    if existing:
        return existing
    reservation = create_reservation(db, user, payload)
    db.flush()
    result = serialize_reservation(db, reservation)
    save_idempotent_response(db, scope, idempotency_key, reservation.business_id, result)
    db.commit()
    await hub.broadcast(branch.id, "reservation.created", result)
    return result


@api.post(
    "/integrations/payment-evidence",
    status_code=201,
    tags=["integrations"],
)
async def integration_payment_evidence(
    payload: IntegrationEvidenceCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    integration: IntegrationAuthContext = Depends(require_integration_scope("payments:write")),
    db: Session = Depends(get_db),
):
    raise HTTPException(
        status_code=410,
        detail=(
            "This metadata-only endpoint is disabled. Upload the real image to "
            "/api/v1/integrations/orders/{order_id}/payment-evidence."
        ),
    )
    # Kept below only as migration history for old clients; execution stops above.
    if not idempotency_key:
        raise HTTPException(status_code=422, detail="Idempotency-Key is required")
    order = load_order(db, payload.order_id, for_update=True)
    ensure_integration_order_scope(integration, order)
    scope = f"integration-evidence:{order.id}"
    existing = get_idempotent_response(db, scope, idempotency_key)
    if existing:
        return existing
    values = evidence_values(payload.model_dump(exclude={"order_id", "storage_path"}), {})
    evidence = PaymentEvidence(
        business_id=order.business_id,
        order_id=order.id,
        storage_path=payload.storage_path,
        analysis={},
        **values,
    )
    evidence.status = determine_evidence_status(db, order, values)
    db.add(evidence)
    db.flush()
    order.payment_status = "provisional" if evidence.status == "provisionally_verified" else "evidence_received"
    order.version += 1
    result = {"id": evidence.id, "order_id": order.id, "status": evidence.status}
    save_idempotent_response(db, scope, idempotency_key, order.business_id, result)
    db.commit()
    await hub.broadcast(order.branch_id, "payment_evidence.created", result)
    return result


def allow_legacy_read() -> None:
    if not get_settings().legacy_public_reads_enabled:
        raise HTTPException(status_code=410, detail="Legacy public API is disabled")


@legacy.get("/datos/negocios", tags=["legacy"])
def legacy_businesses(db: Session = Depends(get_db)):
    allow_legacy_read()
    businesses = list(db.scalars(select(Business).order_by(Business.id)))
    return [
        {
            "id": business.id,
            "nombre": business.name,
            "contrasena": None,
            "estatus": "Activo" if business.status == "active" else "Suspendido",
            "plan": business.plan,
        }
        for business in businesses
    ]


@legacy.get("/datos/restaurantes_perfiles", tags=["legacy"])
def legacy_profiles(db: Session = Depends(get_db)):
    allow_legacy_read()
    profiles = []
    for business in db.scalars(select(Business).order_by(Business.id)):
        branches_payload = []
        branches = list(db.scalars(select(Branch).where(Branch.business_id == business.id).order_by(Branch.id)))
        for branch in branches:
            products = list(db.scalars(select(Product).where(Product.branch_id == branch.id).order_by(Product.sort_order, Product.name)))
            branches_payload.append(
                {
                    "nombre": branch.name,
                    "direccion": branch.address or "",
                    "horario": branch.opening_hours,
                    "pagos": ", ".join(branch.accepted_payment_methods or []),
                    "menu": [
                        {
                            "id": product.id,
                            "nombre": product.name,
                            "precio": float(product.price),
                            "desc": product.description or "",
                            "img": product.image_url or "",
                            "available": product.available,
                        }
                        for product in products
                    ],
                }
            )
        profiles.append(
            {
                "id": business.id,
                "negocio_id": business.id,
                "logo_url": business.logo_url,
                "telefono": business.phone,
                "izipay_shop_id": None,
                "izipay_public": None,
                "sucursales_json": branches_payload,
            }
        )
    return profiles


@legacy.post("/datos/pedidos_draft", tags=["legacy"])
async def legacy_create_draft(payload: LegacyDraftCreate, db: Session = Depends(get_db)):
    business = None
    if payload.negocio_id:
        business = db.get(Business, payload.negocio_id)
    if not business and payload.negocio_nombre:
        business = db.scalar(select(Business).where(func.lower(Business.name) == payload.negocio_nombre.lower()))
    if not business:
        business = db.scalar(select(Business).where(Business.slug == payload.tenant_id))
    if not business:
        raise HTTPException(status_code=404, detail="Legacy business mapping was not found")
    branch = db.scalar(select(Branch).where(Branch.business_id == business.id, Branch.active.is_(True)).order_by(Branch.id))
    if not branch:
        raise HTTPException(status_code=422, detail="Business has no active branch")
    if payload.message_id:
        existing = db.scalar(
            select(Order).where(Order.business_id == business.id, Order.external_reference == payload.message_id).options(selectinload(Order.items))
        )
        if existing:
            return {"mensaje": "Dato guardado con éxito", "dato_guardado": serialize_order(existing)}
    system_user = AuthContext("legacy-n8n", "owner", business.id, branch.id)
    lines = parse_legacy_items(payload.items_json)
    order = create_order(
        db,
        system_user,
        OrderCreate(
            branch_id=branch.id,
            channel="whatsapp",
            source=payload.source,
            customer_name=payload.customer_name,
            customer_phone=payload.customer_phone,
            external_reference=payload.message_id,
            notes=payload.notes,
            items=lines,
        ),
    )
    db.commit()
    result = serialize_order(load_order(db, order.id))
    await hub.broadcast(branch.id, "order.created", result)
    return {"mensaje": "Dato guardado con éxito", "dato_guardado": result}


@legacy.get("/datos/pedidos_draft", tags=["legacy"])
def legacy_list_drafts(db: Session = Depends(get_db)):
    allow_legacy_read()
    orders = list(
        db.scalars(
            select(Order)
            .where(Order.source == "whatsapp_agent", Order.status.in_(["draft", "pending_confirmation"]))
            .options(selectinload(Order.items))
            .order_by(Order.created_at.desc())
            .limit(200)
        )
    )
    return [serialize_order(order) for order in orders]


@legacy.api_route(
    "/tablas/{table_name}",
    methods=["GET", "POST", "PUT", "DELETE"],
    tags=["legacy"],
    include_in_schema=False,
)
def disabled_dynamic_tables(table_name: str):
    raise HTTPException(status_code=410, detail="Dynamic table administration was removed for security")


@legacy.api_route(
    "/datos/{table_name}",
    methods=["GET", "POST", "PUT", "DELETE"],
    tags=["legacy"],
    include_in_schema=False,
)
def disabled_dynamic_data(table_name: str):
    raise HTTPException(status_code=410, detail="Generic data access was removed; use /api/v1 domain endpoints")
