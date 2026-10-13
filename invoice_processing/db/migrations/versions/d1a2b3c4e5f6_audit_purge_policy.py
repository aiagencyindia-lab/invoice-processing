"""audit_events_purge: the offboarding purge's DELETE policy

The audit trail had no DELETE policy, so with RLS forced even the owner
deleted nothing. Offboarding must erase a tenant's whole trail; this lets
the owner role do that for the one tenant named in app.purge_tenant. The
app role still has no DELETE privilege on the table.

Revision ID: d1a2b3c4e5f6
Revises: c9d5e2f3a4b6
Create Date: 2026-10-13
"""

from collections.abc import Sequence

from alembic import op

from invoice_processing.db.rls import (
    audit_purge_policy_statements,
    drop_audit_purge_policy_statements,
)

revision: str = "d1a2b3c4e5f6"
down_revision: str | None = "c9d5e2f3a4b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RLS_TABLES: list[str] = []


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for statement in audit_purge_policy_statements():
        op.execute(statement)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for statement in drop_audit_purge_policy_statements():
        op.execute(statement)
