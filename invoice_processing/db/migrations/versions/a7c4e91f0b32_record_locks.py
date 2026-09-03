"""record locks: one editor per invoice at a time

Additive. Nothing existing changes -- the lock is a new table and a guard on
two existing routes, and an absent row means what it meant before this
migration: nobody is editing, go ahead.

Revision ID: a7c4e91f0b32
Revises: f3d8b2c15e07
Create Date: 2026-09-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from invoice_processing.db.rls import enable_rls_statements

revision: str = "a7c4e91f0b32"
down_revision: str | None = "f3d8b2c15e07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Named rather than imported: TENANT_TABLES is the current set of tenant
# tables and this migration is history.
RLS_TABLES = ["record_locks"]


def upgrade() -> None:
    op.create_table(
        "record_locks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("case_id", sa.String(128), nullable=False),
        sa.Column("holder", sa.String(320), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # The lock itself: one row per case, so the database picks the winner.
        sa.UniqueConstraint(
            "tenant_id", "case_id", name="uq_record_locks_case"
        ),
    )

    if op.get_bind().dialect.name != "postgresql":
        return
    for statement in enable_rls_statements(RLS_TABLES):
        op.execute(statement)


def downgrade() -> None:
    op.drop_table("record_locks")
