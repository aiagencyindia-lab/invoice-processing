"""document claims

The claim that stops two workers processing the same invoice was a file on one
container's disk. Correct mutual exclusion on one machine, none at all across
two: each container has its own filesystem, so both could hold the same claim
and run the same extraction side by side.

The unique constraint is the lock -- `INSERT ... ON CONFLICT DO NOTHING`
resolves the race for every process at once. Row-level security applies: a
claim names a document, and a document belongs to a tenant.

Revision ID: c2e5a71b4f80
Revises: b1f3c7a90e42
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from invoice_processing.db.rls import enable_rls_statements

revision: str = "c2e5a71b4f80"
down_revision: str | None = "b1f3c7a90e42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "document_claims",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("worker_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "document_id", name="uq_document_claims_document"
        ),
    )
    op.create_index(
        "ix_document_claims_tenant", "document_claims", ["tenant_id"]
    )

    if op.get_bind().dialect.name != "postgresql":
        return

    for statement in enable_rls_statements(["document_claims"]):
        op.execute(statement)


def downgrade() -> None:
    op.drop_index("ix_document_claims_tenant", table_name="document_claims")
    op.drop_table("document_claims")
