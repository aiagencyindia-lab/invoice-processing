"""v2 trust layer, Wave 2: Trust Audit engagements, runs, samples; explanation ratings

Revision ID: f4a5b6c7d8e9
Revises: e2f3a4b5c6d7
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from invoice_processing.db.rls import enable_rls_statements

revision: str = "f4a5b6c7d8e9"
down_revision: str | None = "e2f3a4b5c6d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The tables *this* migration creates, named rather than imported.
RLS_TABLES = [
    "trust_audits",
    "trust_audit_runs",
    "trust_audit_samples",
    "trust_explanation_ratings",
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


def _audit_fk():
    return sa.Column(
        "audit_id",
        sa.String(36),
        sa.ForeignKey("trust_audits.id"),
        nullable=False,
        index=True,
    )


def upgrade() -> None:
    op.create_table(
        "trust_audits",
        _id(),
        _tenant(),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="draft"
        ),
        sa.Column("period_from", sa.Date),
        sa.Column("period_to", sa.Date),
        sa.Column("vendors", JSON, nullable=False, server_default="[]"),
        sa.Column("categories", JSON, nullable=False, server_default="[]"),
        sa.Column("terms", JSON, nullable=False, server_default="{}"),
        sa.Column("approver", sa.String(320)),
        sa.Column("created_by", sa.String(320)),
        *_stamps(),
    )

    op.create_table(
        "trust_audit_runs",
        _id(),
        _tenant(),
        _audit_fk(),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="queued"
        ),
        sa.Column("engines", JSON, nullable=False, server_default="[]"),
        sa.Column("progress", JSON, nullable=False, server_default="{}"),
        sa.Column("error", sa.String(500)),
        sa.Column("requested_by", sa.String(320)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        *_stamps(),
    )

    op.create_table(
        "trust_audit_samples",
        _id(),
        _tenant(),
        _audit_fk(),
        sa.Column("size", sa.Integer, nullable=False),
        sa.Column("seed", sa.String(32)),
        sa.Column("finding_ids", JSON, nullable=False, server_default="[]"),
        sa.Column("verdicts", JSON, nullable=False, server_default="{}"),
        sa.Column("created_by", sa.String(320)),
        *_stamps(),
    )

    op.create_table(
        "trust_explanation_ratings",
        _id(),
        _tenant(),
        sa.Column(
            "finding_id",
            sa.String(36),
            sa.ForeignKey("trust_findings.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("rater", sa.String(320), nullable=False),
        sa.Column("correct", sa.Boolean, nullable=False),
        sa.Column("useful", sa.Boolean, nullable=False),
        sa.Column("note", sa.String(500)),
        *_stamps(),
        sa.UniqueConstraint(
            "tenant_id",
            "finding_id",
            "rater",
            name="uq_trust_explanation_ratings_rater",
        ),
    )

    if op.get_bind().dialect.name != "postgresql":
        return
    for statement in enable_rls_statements(RLS_TABLES):
        op.execute(statement)


def downgrade() -> None:
    for table in reversed(RLS_TABLES):
        op.drop_table(table)
