"""Harden payment evidence and tenant-scoped idempotency.

Revision ID: 20260827_0014
Revises: 20260827_0013
"""

import sqlalchemy as sa
from alembic import op


revision = "20260827_0014"
down_revision = "20260827_0013"
branch_labels = None
depends_on = None


OPEN_EVIDENCE = "status IN ('evidence_received', 'under_review')"
OPEN_EVIDENCE_INDEX = "uq_payment_evidence_one_open_per_order"
OLD_IDEMPOTENCY_CONSTRAINT = "uq_idempotency_scope_key"
TENANT_IDEMPOTENCY_CONSTRAINT = "uq_idempotency_business_scope_key"


def index_names(table_name: str) -> set[str]:
    return {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
        if index.get("name")
    }


def unique_constraint_names(table_name: str) -> set[str]:
    return {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_unique_constraints(table_name)
        if constraint.get("name")
    }


def column_is_nullable(table_name: str, column_name: str) -> bool:
    columns = {
        column["name"]: column
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }
    return bool(columns[column_name]["nullable"])


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE payment_evidence "
            "SET status = 'superseded', "
            "rejection_reason = COALESCE(rejection_reason, 'Superseded during migration'), "
            "reviewed_at = COALESCE(reviewed_at, CURRENT_TIMESTAMP), "
            "updated_at = CURRENT_TIMESTAMP "
            "WHERE status IN ('evidence_received', 'under_review') "
            "AND id NOT IN ("
            "SELECT MAX(id) FROM payment_evidence "
            "WHERE status IN ('evidence_received', 'under_review') GROUP BY order_id"
            ")"
        )
    )
    if OPEN_EVIDENCE_INDEX not in index_names("payment_evidence"):
        op.create_index(
            OPEN_EVIDENCE_INDEX,
            "payment_evidence",
            ["order_id"],
            unique=True,
            postgresql_where=sa.text(OPEN_EVIDENCE),
            sqlite_where=sa.text(OPEN_EVIDENCE),
        )
    if column_is_nullable("idempotency_records", "business_id"):
        null_business_records = op.get_bind().execute(
            sa.text(
                "SELECT COUNT(*) FROM idempotency_records WHERE business_id IS NULL"
            )
        ).scalar_one()
        if null_business_records:
            raise RuntimeError(
                "Cannot scope idempotency records until every row has business_id"
            )
        with op.batch_alter_table("idempotency_records") as batch_op:
            batch_op.alter_column(
                "business_id",
                existing_type=sa.Integer(),
                nullable=False,
            )
    idempotency_constraints = unique_constraint_names("idempotency_records")
    if (
        TENANT_IDEMPOTENCY_CONSTRAINT not in idempotency_constraints
        or OLD_IDEMPOTENCY_CONSTRAINT in idempotency_constraints
    ):
        with op.batch_alter_table("idempotency_records") as batch_op:
            if OLD_IDEMPOTENCY_CONSTRAINT in idempotency_constraints:
                batch_op.drop_constraint(OLD_IDEMPOTENCY_CONSTRAINT, type_="unique")
            if TENANT_IDEMPOTENCY_CONSTRAINT not in idempotency_constraints:
                batch_op.create_unique_constraint(
                    TENANT_IDEMPOTENCY_CONSTRAINT,
                    ["business_id", "scope", "idempotency_key"],
                )


def downgrade() -> None:
    idempotency_constraints = unique_constraint_names("idempotency_records")
    if (
        OLD_IDEMPOTENCY_CONSTRAINT not in idempotency_constraints
        or TENANT_IDEMPOTENCY_CONSTRAINT in idempotency_constraints
    ):
        with op.batch_alter_table("idempotency_records") as batch_op:
            if TENANT_IDEMPOTENCY_CONSTRAINT in idempotency_constraints:
                batch_op.drop_constraint(TENANT_IDEMPOTENCY_CONSTRAINT, type_="unique")
            if OLD_IDEMPOTENCY_CONSTRAINT not in idempotency_constraints:
                batch_op.create_unique_constraint(
                    OLD_IDEMPOTENCY_CONSTRAINT,
                    ["scope", "idempotency_key"],
                )
    if OPEN_EVIDENCE_INDEX in index_names("payment_evidence"):
        op.drop_index(
            OPEN_EVIDENCE_INDEX,
            table_name="payment_evidence",
        )
    if not column_is_nullable("idempotency_records", "business_id"):
        with op.batch_alter_table("idempotency_records") as batch_op:
            batch_op.alter_column(
                "business_id",
                existing_type=sa.Integer(),
                nullable=True,
            )
