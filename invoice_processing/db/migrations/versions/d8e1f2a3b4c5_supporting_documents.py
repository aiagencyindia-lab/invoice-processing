"""scanned POs and GRNs: supporting_documents, documents.kind, document links

Revision ID: d8e1f2a3b4c5
Revises: c4d7e8f91a02
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from invoice_processing.db.rls import enable_rls_statements

revision: str = "d8e1f2a3b4c5"
down_revision: str | None = "c4d7e8f91a02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The tables *this* migration creates, named rather than imported.
RLS_TABLES = ["supporting_documents"]


def upgrade() -> None:
    op.create_table(
        "supporting_documents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "document_id",
            sa.String(36),
            sa.ForeignKey("documents.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="pending"
        ),
        sa.Column(
            "extracted",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("po_number", sa.String(128), index=True),
        sa.Column("blocked_reason", sa.String(500)),
        sa.Column("extraction_error", sa.String(500)),
        sa.Column("reviewed_by", sa.String(320)),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("note", sa.String(500)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id", "document_id", name="uq_supporting_documents_document"
        ),
    )
    # Batch mode: SQLite cannot add a FK constraint with ALTER; Postgres
    # runs these as plain ALTERs.
    with op.batch_alter_table("documents") as batch:
        batch.add_column(sa.Column("kind", sa.String(16)))
    for table in ("purchase_orders", "goods_receipts"):
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("document_id", sa.String(36)))
            batch.create_foreign_key(
                f"fk_{table}_document_id", "documents", ["document_id"], ["id"]
            )

    if op.get_bind().dialect.name != "postgresql":
        return
    for statement in enable_rls_statements(RLS_TABLES):
        op.execute(statement)


def downgrade() -> None:
    for table in ("goods_receipts", "purchase_orders"):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f"fk_{table}_document_id", type_="foreignkey")
            batch.drop_column("document_id")
    with op.batch_alter_table("documents") as batch:
        batch.drop_column("kind")
    op.drop_table("supporting_documents")
