"""invoices.decided_at: when an invoice stopped needing anyone

The KPI page asks "how long did decisions take last month". The answer lived
only inside the workflow JSON, so the question meant reading every row's
artifacts. One typed column, backfilled from that JSON for the rows that
already have a decision.

Revision ID: a7b3c9d2e4f6
Revises: d8e1f2a3b4c5
Create Date: 2026-10-11
"""

import json
from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision: str = "a7b3c9d2e4f6"
down_revision: str | None = "d8e1f2a3b4c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# No new table, so nothing to protect with RLS here.
RLS_TABLES: list[str] = []


def _parse(value) -> datetime | None:
    # Same tolerance as case_projection.parse_timestamp, repeated rather than
    # imported: a migration must not change meaning when app code does.
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _workflow(fields) -> dict:
    doc = fields
    if isinstance(doc, str):
        try:
            doc = json.loads(doc)
        except ValueError:
            doc = {}
    return (doc or {}).get("workflow") or {}


def upgrade() -> None:
    op.add_column(
        "invoices", sa.Column("decided_at", sa.DateTime(timezone=True))
    )

    bind = op.get_bind()
    postgres = bind.dialect.name == "postgresql"
    # One tenant at a time. On Postgres every invoices policy compares rows
    # against app.tenant_id and the table is FORCE ROW LEVEL SECURITY, so a
    # role without BYPASSRLS (production's, deliberately) that never sets it
    # sees zero invoices and the backfill silently does nothing. `tenants`
    # itself has no policy, so it is the safe place to start. set_config(...,
    # true) is transaction-local: it ends with the migration's transaction.
    tenants = [t for (t,) in bind.execute(sa.text("SELECT id FROM tenants"))]
    for tenant in tenants:
        if postgres:
            bind.execute(
                sa.text("SELECT set_config('app.tenant_id', :t, true)"),
                {"t": tenant},
            )
        # Raw text SQL, not a typed table: pg8000 hands JSONB back already
        # parsed, SQLite hands back a string, and a typed JSON column mangles
        # one of them.
        rows = bind.execute(
            sa.text("SELECT id, fields FROM invoices WHERE tenant_id = :t"),
            {"t": tenant},
        ).all()
        for row_id, fields in rows:
            workflow = _workflow(fields)
            if workflow.get("decision") not in ("approve", "reject"):
                continue
            decided = _parse(workflow.get("decided_at"))
            if decided is None:
                continue
            bind.execute(
                sa.text(
                    "UPDATE invoices SET decided_at = :d "
                    "WHERE id = :i AND tenant_id = :t"
                ),
                {"d": decided, "i": row_id, "t": tenant},
            )


def downgrade() -> None:
    with op.batch_alter_table("invoices") as batch:
        batch.drop_column("decided_at")
