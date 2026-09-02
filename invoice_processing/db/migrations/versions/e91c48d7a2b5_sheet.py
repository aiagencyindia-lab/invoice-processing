"""approved-invoice sheet: saved views, export jobs, query indexes

Strictly additive. No existing column changes and no data is rewritten: the
sheet reads approval out of the workflow artifact the approval flow already
writes into `invoices.fields`, so the only thing missing was somewhere to put
a saved view, somewhere to track an export, and indexes that make the query
cheap.

The three expression indexes are Postgres-only. SQLite has no `->>` operator
and the test suite scans a handful of rows, which is free.

Revision ID: e91c48d7a2b5
Revises: c2e5a71b4f80
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from invoice_processing.db.rls import enable_rls_statements

revision: str = "e91c48d7a2b5"
down_revision: str | None = "c2e5a71b4f80"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The tables *this* migration creates, named rather than imported. The live
# TENANT_TABLES grows; this list is history and must not.
RLS_TABLES = ["sheet_views", "export_jobs"]

# How approval is stored: `fields -> 'workflow' ->> 'decision'`.
DECISION = "((fields -> 'workflow') ->> 'decision')"
DECIDED_AT = "((fields -> 'workflow') ->> 'decided_at')"


def upgrade() -> None:
    op.create_table(
        "sheet_views",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("owner", sa.String(length=320), nullable=False),
        sa.Column("shared", sa.Boolean(), nullable=False),
        sa.Column("definition", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_sheet_views_tenant_owner", "sheet_views", ["tenant_id", "owner"]
    )

    op.create_table(
        "export_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("requested_by", sa.String(length=320), nullable=True),
        sa.Column("fmt", sa.String(length=16), nullable=False),
        sa.Column("spec", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("storage_key", sa.String(length=1024), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_export_jobs_tenant_time", "export_jobs", ["tenant_id", "created_at"]
    )

    if op.get_bind().dialect.name != "postgresql":
        return

    for statement in enable_rls_statements(RLS_TABLES):
        op.execute(statement)

    # The sheet's whole predicate is "approved, newest first, for this tenant".
    op.execute(
        "CREATE INDEX ix_invoices_approval ON invoices "
        f"(tenant_id, {DECISION}, {DECIDED_AT} DESC)"
    )
    op.execute(
        "CREATE INDEX ix_invoices_vendor ON invoices (tenant_id, vendor_name)"
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_invoices_vendor")
        op.execute("DROP INDEX IF EXISTS ix_invoices_approval")
    op.drop_index("ix_export_jobs_tenant_time", table_name="export_jobs")
    op.drop_table("export_jobs")
    op.drop_index("ix_sheet_views_tenant_owner", table_name="sheet_views")
    op.drop_table("sheet_views")
