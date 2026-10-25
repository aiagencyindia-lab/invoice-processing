"""v2 trust layer, Wave 4: agent registry, mandates, action ledger

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-10-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from invoice_processing.db.rls import enable_rls_statements

revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The tables *this* migration creates, named rather than imported.
RLS_TABLES = ["agents", "agent_mandates", "agent_actions"]

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


def _at(name: str, **kw):
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def _agent_fk():
    return sa.Column(
        "agent_id",
        sa.String(36),
        sa.ForeignKey("agents.id"),
        nullable=False,
        index=True,
    )


def upgrade() -> None:
    op.create_table(
        "agents",
        _id(),
        _tenant(),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("owner", sa.String(320), nullable=False, index=True),
        sa.Column("principal_id", sa.String(64)),
        sa.Column("principal_email", sa.String(320), index=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("description", sa.String(500)),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="active"
        ),
        sa.Column("registered_by", sa.String(320)),
        _at("revoked_at"),
        sa.Column("revoked_by", sa.String(320)),
        sa.Column("revoke_reason", sa.String(500)),
        sa.Column("graph_node", sa.String(36)),
        *_stamps(),
        sa.UniqueConstraint(
            "tenant_id", "principal_id", name="uq_agents_principal"
        ),
    )
    op.create_table(
        "agent_mandates",
        _id(),
        _tenant(),
        _agent_fk(),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("scope", JSON, nullable=False, server_default="{}"),
        _at("valid_from", nullable=False),
        _at("valid_to", nullable=False),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="proposed"
        ),
        sa.Column("proposed_by", sa.String(320), nullable=False),
        sa.Column("approved_by", sa.String(320)),
        _at("approved_at"),
        sa.Column("revoked_by", sa.String(320)),
        _at("revoked_at"),
        sa.Column("reason", sa.String(500)),
        sa.Column("prev_digest", sa.String(64)),
        sa.Column("digest", sa.String(64)),
        sa.Column("signature", sa.String(64)),
        *_stamps(),
        sa.UniqueConstraint(
            "agent_id", "version", name="uq_agent_mandates_version"
        ),
    )
    op.create_table(
        "agent_actions",
        _id(),
        _tenant(),
        _agent_fk(),
        sa.Column("mandate_id", sa.String(36)),
        sa.Column("mandate_version", sa.Integer),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("subject_table", sa.String(32)),
        sa.Column("subject_ref", sa.String(128), index=True),
        sa.Column("transaction_node", sa.String(36)),
        sa.Column("vendor_key", sa.String(128), index=True),
        sa.Column("category", sa.String(64)),
        sa.Column("amount", sa.Numeric(18, 4)),
        sa.Column("currency", sa.String(8)),
        sa.Column("decision", sa.String(20), nullable=False),
        sa.Column("reasons", JSON, nullable=False, server_default="[]"),
        sa.Column("explanation", sa.String(1000)),
        sa.Column("mode", sa.String(16)),
        sa.Column(
            "source", sa.String(8), nullable=False, server_default="inline"
        ),
        _at("occurred_at", nullable=False),
        sa.Column("finding_id", sa.String(36)),
        sa.Column("request_id", sa.String(64)),
        *_stamps(),
    )
    op.create_index(
        "ix_agent_actions_agent_time",
        "agent_actions",
        ["tenant_id", "agent_id", "occurred_at"],
    )

    if op.get_bind().dialect.name != "postgresql":
        return
    for statement in enable_rls_statements(RLS_TABLES):
        op.execute(statement)


def downgrade() -> None:
    op.drop_index("ix_agent_actions_agent_time", table_name="agent_actions")
    for table in reversed(RLS_TABLES):
        op.drop_table(table)
