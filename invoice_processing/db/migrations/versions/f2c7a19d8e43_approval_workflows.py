"""approval workflows: groups, rules, delegations and per-invoice chains

Revision ID: f2c7a19d8e43
Revises: e7a2c9d41b56
Create Date: 2026-10-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from invoice_processing.db.rls import enable_rls_statements

revision: str = "f2c7a19d8e43"
down_revision: str | None = "e7a2c9d41b56"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The tables *this* migration creates, named rather than imported. The live
# TENANT_TABLES grows; this list is history and must not.
RLS_TABLES = [
    "approver_groups",
    "approval_rules",
    "delegations",
    "approval_steps",
]


# JSONB on Postgres, as the models declare (JSONType); plain JSON elsewhere.
JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _tenant_id() -> sa.Column:
    return sa.Column(
        "tenant_id",
        sa.String(36),
        sa.ForeignKey("tenants.id"),
        nullable=False,
        index=True,
    )


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            name,
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        )
        for name in ("created_at", "updated_at")
    ]


def upgrade() -> None:
    op.create_table(
        "approver_groups",
        sa.Column("id", sa.String(36), primary_key=True),
        _tenant_id(),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("members", JSON, nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "tenant_id", "name", name="uq_approver_groups_name"
        ),
    )
    op.create_table(
        "approval_rules",
        sa.Column("id", sa.String(36), primary_key=True),
        _tenant_id(),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("match", JSON, nullable=False),
        sa.Column("steps", JSON, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False),
        *_timestamps(),
    )
    op.create_table(
        "delegations",
        sa.Column("id", sa.String(36), primary_key=True),
        _tenant_id(),
        sa.Column(
            "delegator_email", sa.String(320), nullable=False, index=True
        ),
        sa.Column("delegate_email", sa.String(320), nullable=False, index=True),
        sa.Column("starts_on", sa.Date, nullable=False),
        sa.Column("ends_on", sa.Date, nullable=False),
        sa.Column("created_by", sa.String(320), nullable=False),
        *_timestamps(),
    )
    op.create_table(
        "approval_steps",
        sa.Column("id", sa.String(36), primary_key=True),
        _tenant_id(),
        sa.Column("case_id", sa.String(128), nullable=False, index=True),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("source_rule_id", sa.String(36)),
        sa.Column("assignee_kind", sa.String(16), nullable=False),
        sa.Column("assignee_value", sa.String(320), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True)),
        sa.Column("decided_by", sa.String(320)),
        sa.Column("acting_for", sa.String(320)),
        sa.Column("override_reason", sa.Text),
        sa.Column("escalated_to", sa.String(320)),
        sa.Column("escalated_at", sa.DateTime(timezone=True)),
        sa.Column("note", sa.Text),
        *_timestamps(),
        sa.UniqueConstraint(
            "tenant_id",
            "case_id",
            "position",
            name="uq_approval_steps_position",
        ),
    )
    op.create_index(
        "ix_approval_steps_pending",
        "approval_steps",
        ["tenant_id", "status", "due_at"],
    )
    op.add_column(
        "users", sa.Column("manager_email", sa.String(320), nullable=True)
    )

    if op.get_bind().dialect.name != "postgresql":
        return
    for statement in enable_rls_statements(RLS_TABLES):
        op.execute(statement)


def downgrade() -> None:
    op.drop_column("users", "manager_email")
    op.drop_table("approval_steps")
    op.drop_table("delegations")
    op.drop_table("approval_rules")
    op.drop_table("approver_groups")
