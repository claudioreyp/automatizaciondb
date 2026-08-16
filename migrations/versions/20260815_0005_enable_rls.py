"""Block direct anon/auth access to POS tables with RLS.

Revision ID: 20260815_0005
Revises: 20260815_0004
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20260815_0005"
down_revision: str | None = "20260815_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


APPLICATION_TABLES = (
    "businesses",
    "branches",
    "module_entitlements",
    "customers",
    "modifier_groups",
    "audit_events",
    "idempotency_records",
    "memberships",
    "invitations",
    "dining_areas",
    "categories",
    "modifiers",
    "inventory_items",
    "cash_registers",
    "reservations",
    "couriers",
    "integration_credentials",
    "integration_events",
    "restaurant_tables",
    "products",
    "stock_movements",
    "cash_sessions",
    "product_variants",
    "product_modifier_groups",
    "recipe_items",
    "orders",
    "cash_movements",
    "reservation_tables",
    "order_items",
    "kitchen_tickets",
    "payments",
    "delivery_assignments",
    "payment_evidence",
    "payment_allocations",
)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    # FastAPI connects as the table owner and continues to enforce tenant rules.
    # Supabase anon/authenticated roles receive no direct table policies.
    for table_name in APPLICATION_TABLES:
        op.execute(f'ALTER TABLE public."{table_name}" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table_name in APPLICATION_TABLES:
        op.execute(f'ALTER TABLE public."{table_name}" DISABLE ROW LEVEL SECURITY')
