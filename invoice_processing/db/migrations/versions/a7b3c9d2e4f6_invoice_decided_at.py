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


def upgrade() -> None:
    op.add_column(
        "invoices", sa.Column("decided_at", sa.DateTime(timezone=True))
    )
    op.create_index(
        "ix_invoices_tenant_decided", "invoices", ["tenant_id", "decided_at"]
    )

    bind = op.get_bind()
    # Raw text SQL, not a typed table: pg8000 hands JSONB back already parsed,
    # SQLite hands back a string, and a typed JSON column mangles one of them.
    for row_id, fields in bind.execute(
        sa.text("SELECT id, fields FROM invoices")
    ).all():
        doc = fields
        if isinstance(doc, str):
            try:
                doc = json.loads(doc)
            except ValueError:
                doc = {}
        workflow = (doc or {}).get("workflow") or {}
        if workflow.get("decision") not in ("approve", "reject"):
            continue
        decided = _parse(workflow.get("decided_at"))
        if decided is None:
            continue
        bind.execute(
            sa.text("UPDATE invoices SET decided_at = :d WHERE id = :i"),
            {"d": decided, "i": row_id},
        )


def downgrade() -> None:
    op.drop_index("ix_invoices_tenant_decided", table_name="invoices")
    with op.batch_alter_table("invoices") as batch:
        batch.drop_column("decided_at")
