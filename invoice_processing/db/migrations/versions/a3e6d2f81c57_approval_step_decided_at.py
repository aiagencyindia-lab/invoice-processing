"""record when each approval step was decided

Revision ID: a3e6d2f81c57
Revises: f2c7a19d8e43
Create Date: 2026-10-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3e6d2f81c57"
down_revision: str | None = "f2c7a19d8e43"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A column on a table that already has RLS; no policy change needed.
    op.add_column(
        "approval_steps",
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("approval_steps", "decided_at")
