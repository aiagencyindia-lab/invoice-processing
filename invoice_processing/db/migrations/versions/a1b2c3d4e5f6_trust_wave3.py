"""v2 trust layer, Wave 3: hold ledger, evidence requests, API keys, webhooks

Revision ID: a1b2c3d4e5f6
Revises: f4a5b6c7d8e9
Create Date: 2026-10-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from invoice_processing.db.rls import enable_rls_statements

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "f4a5b6c7d8e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The tables *this* migration creates, named rather than imported.
RLS_TABLES = [
    "trust_holds",
    "trust_evidence_requests",
    "api_keys",
    "webhook_endpoints",
    "webhook_outbox",
]

JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _id():
    return sa.Column("id", sa.String(36), primary_key=True)


def _tenant():
    return sa.Column(
        "tenant_id",
        sa.String(36),
        sa.ForeignKey("tenants.id"),
        nullable=False,
        index=True,
    )


def _stamps():
    return (
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


def _finding_fk():
    return sa.Column(
        "finding_id",
        sa.String(36),
        sa.ForeignKey("trust_findings.id"),
        nullable=False,
        index=True,
    )


def _at(name: str, **kw):
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def upgrade() -> None:
    op.create_table(
        "trust_holds",
        _id(),
        _tenant(),
        _finding_fk(),
        sa.Column(
            "transaction_node", sa.String(36), nullable=False, index=True
        ),
        sa.Column("engine", sa.String(32), nullable=False),
        sa.Column("type", sa.String(64), nullable=False),
        _at("placed_at", nullable=False, server_default=sa.func.now()),
        sa.Column("placed_by", sa.String(320)),
        _at("lifted_at"),
        sa.Column("lifted_by", sa.String(320)),
        sa.Column("lift_reason", sa.String(500)),
        *_stamps(),
    )

    op.create_table(
        "trust_evidence_requests",
        _id(),
        _tenant(),
        _finding_fk(),
        sa.Column("owner", sa.String(320), nullable=False),
        sa.Column("due_date", sa.Date),
        sa.Column("note", sa.String(500)),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="open"
        ),
        sa.Column("proof_document_id", sa.String(36)),
        sa.Column("requested_by", sa.String(320)),
        _at("fulfilled_at"),
        *_stamps(),
    )

    op.create_table(
        "api_keys",
        _id(),
        _tenant(),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("key_prefix", sa.String(12), nullable=False, index=True),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column(
            "role", sa.String(16), nullable=False, server_default="viewer"
        ),
        sa.Column("created_by", sa.String(320)),
        _at("last_used_at"),
        _at("revoked_at"),
        *_stamps(),
        sa.UniqueConstraint(
            "tenant_id", "key_prefix", name="uq_api_keys_prefix"
        ),
    )

    op.create_table(
        "webhook_endpoints",
        _id(),
        _tenant(),
        sa.Column("url", sa.String(1024), nullable=False),
        sa.Column("secret", sa.String(128), nullable=False),
        sa.Column("events", JSON, nullable=False, server_default="[]"),
        sa.Column(
            "active", sa.Boolean, nullable=False, server_default=sa.true()
        ),
        sa.Column("description", sa.String(256)),
        sa.Column("created_by", sa.String(320)),
        *_stamps(),
    )

    op.create_table(
        "webhook_outbox",
        _id(),
        _tenant(),
        sa.Column(
            "endpoint_id",
            sa.String(36),
            sa.ForeignKey("webhook_endpoints.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("event", sa.String(48), nullable=False),
        sa.Column("payload", JSON, nullable=False, server_default="{}"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        _at(
            "next_attempt_at",
            nullable=False,
            server_default=sa.func.now(),
            index=True,
        ),
        _at("delivered_at"),
        sa.Column("last_status", sa.Integer),
        sa.Column("last_error", sa.String(500)),
        _at("dead_at"),
        *_stamps(),
    )

    if op.get_bind().dialect.name != "postgresql":
        return
    for statement in enable_rls_statements(RLS_TABLES):
        op.execute(statement)


def downgrade() -> None:
    for table in reversed(RLS_TABLES):
        op.drop_table(table)
