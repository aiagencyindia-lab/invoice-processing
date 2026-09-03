"""record what the reviewer decided, and whether it agreed with the machine

Three nullable columns on `invoices`. Nullable and additive, so on Postgres
this is a catalogue change with no table rewrite and no lock worth naming.

`invoices` is already in TENANT_TABLES and already has its RLS policy: policies
are per table, not per column, so there is nothing to enable here.

Revision ID: d5b81a6c39f4
Revises: a7c4e91f0b32
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5b81a6c39f4"
down_revision: str | None = "a7c4e91f0b32"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "invoices", sa.Column("human_decision", sa.String(16), nullable=True)
    )
    op.add_column(
        "invoices",
        sa.Column("decision_agreement", sa.String(24), nullable=True),
    )
    op.add_column(
        "invoices",
        sa.Column("decision_category", sa.String(32), nullable=True),
    )
    # The index the learning loop reads: "every case where a person overruled
    # the pipeline" must not be a sequential scan of the ledger.
    op.create_index(
        "ix_invoices_agreement",
        "invoices",
        ["tenant_id", "decision_agreement"],
    )


def downgrade() -> None:
    op.drop_index("ix_invoices_agreement", table_name="invoices")
    op.drop_column("invoices", "decision_category")
    op.drop_column("invoices", "decision_agreement")
    op.drop_column("invoices", "human_decision")
