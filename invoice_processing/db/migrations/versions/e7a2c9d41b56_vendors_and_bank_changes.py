"""vendors and bank changes: a supplier record both containers can read, and
the dual-control queue for its payee details

Revision ID: e7a2c9d41b56
Revises: d5b81a6c39f4
Create Date: 2026-10-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from invoice_processing.db.rls import enable_rls_statements

revision: str = "e7a2c9d41b56"
down_revision: str | None = "d5b81a6c39f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The tables *this* migration creates, named rather than imported. The live
# TENANT_TABLES grows; this list is history and must not.
RLS_TABLES = ["vendors", "vendor_bank_changes"]


def upgrade() -> None:
    op.create_table(
        "vendors",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("normalized_name", sa.String(512), nullable=False),
        sa.Column("tax_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), server_default=""),
        sa.Column("email_domain", sa.String(255)),
        sa.Column("bank_account_name", sa.String(255)),
        sa.Column("bank_account_number", sa.String(64)),
        sa.Column("bank_code", sa.String(64)),
        sa.Column("bank_iban", sa.String(64)),
        sa.Column("bank_verified_at", sa.DateTime(timezone=True)),
        sa.Column("bank_verified_by", sa.String(320)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id", "normalized_name", name="uq_vendors_name"
        ),
    )
    op.create_table(
        "vendor_bank_changes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "vendor_id",
            sa.String(36),
            sa.ForeignKey("vendors.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("proposed_account_name", sa.String(255)),
        sa.Column("proposed_account_number", sa.String(64)),
        sa.Column("proposed_code", sa.String(64)),
        sa.Column("proposed_iban", sa.String(64)),
        sa.Column("previous_account_name", sa.String(255)),
        sa.Column("previous_account_number", sa.String(64)),
        sa.Column("previous_code", sa.String(64)),
        sa.Column("previous_iban", sa.String(64)),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("source_case_id", sa.String(64)),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="pending",
            index=True,
        ),
        sa.Column("proposed_by", sa.String(320), nullable=False),
        sa.Column("decided_by", sa.String(320)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("decision_note", sa.Text),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
    )
    op.add_column(
        "documents", sa.Column("sender", sa.String(320), nullable=True)
    )
    op.add_column(
        "documents", sa.Column("reply_to", sa.String(320), nullable=True)
    )

    if op.get_bind().dialect.name != "postgresql":
        return
    # One pending change per vendor, enforced where two API containers cannot
    # race past a check in Python.
    op.execute(
        "CREATE UNIQUE INDEX uq_vendor_bank_changes_pending "
        "ON vendor_bank_changes (vendor_id) WHERE status = 'pending'"
    )
    for statement in enable_rls_statements(RLS_TABLES):
        op.execute(statement)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS uq_vendor_bank_changes_pending")
    op.drop_column("documents", "reply_to")
    op.drop_column("documents", "sender")
    op.drop_table("vendor_bank_changes")
    op.drop_table("vendors")
