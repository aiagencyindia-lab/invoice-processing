"""agent_actions.outcome: did the governed write happen after the decision

The ledger records a decision before the write (Wave 4, B3), so an approval
that then failed (already decided, refused, unreachable) used up the agent's
cap. The call sites stamp `executed` or `failed`; spend skips `failed`.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# No new table, so nothing to protect with RLS here.
RLS_TABLES: list[str] = []


def upgrade() -> None:
    op.add_column(
        "agent_actions", sa.Column("outcome", sa.String(16), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("agent_actions", "outcome")
