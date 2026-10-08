"""gl rules: tenant rules that code invoice lines to GL accounts

Revision ID: b21ac023e07e
Revises: a3e6d2f81c57
Create Date: 2026-10-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from invoice_processing.db.rls import enable_rls_statements

revision: str = "b21ac023e07e"
down_revision: str | None = "a3e6d2f81c57"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The tables *this* migration creates, named rather than imported. The live
# TENANT_TABLES grows; this list is history and must not.
RLS_TABLES = ["gl_rules"]


def upgrade() -> None:
    op.create_table(
        "gl_rules",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("vendor_name", sa.String(255)),
        sa.Column("keyword", sa.String(200)),
        sa.Column("gl_account", sa.String(64), nullable=False),
        sa.Column(
            "enabled", sa.Boolean, nullable=False, server_default=sa.true()
        ),
        sa.Column("created_by", sa.String(320), nullable=False),
        *[
            sa.Column(
                name,
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            )
            for name in ("created_at", "updated_at")
        ],
    )

    if op.get_bind().dialect.name != "postgresql":
        return
    for statement in enable_rls_statements(RLS_TABLES):
        op.execute(statement)


def downgrade() -> None:
    op.drop_table("gl_rules")
