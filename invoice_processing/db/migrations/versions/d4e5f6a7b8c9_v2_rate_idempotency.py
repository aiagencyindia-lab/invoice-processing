"""/v2 rate-limit windows and idempotency keys

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from invoice_processing.db.rls import enable_rls_statements

revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The tables *this* migration creates, named rather than imported.
RLS_TABLES = ["api_rate_windows", "api_idempotency"]

JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _common():
    return (
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("principal", sa.String(64), nullable=False),
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
    )


def upgrade() -> None:
    op.create_table(
        "api_rate_windows",
        *_common(),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer, nullable=False, server_default="0"),
        sa.UniqueConstraint(
            "tenant_id", "principal", "window_start", name="uq_api_rate_window"
        ),
    )
    op.create_table(
        "api_idempotency",
        *_common(),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="in_progress",
        ),
        sa.Column("status_code", sa.Integer),
        sa.Column("body", JSON),
        sa.Column("content_type", sa.String(128)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "principal", "key", name="uq_api_idempotency_key"
        ),
    )
    if op.get_bind().dialect.name != "postgresql":
        return
    for statement in enable_rls_statements(RLS_TABLES):
        op.execute(statement)


def downgrade() -> None:
    for table in reversed(RLS_TABLES):
        op.drop_table(table)
