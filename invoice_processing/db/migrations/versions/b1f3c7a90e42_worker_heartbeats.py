"""worker heartbeats

Liveness moves out of a file on one container's disk and into a table every
container can read. The worker wrote the file locally and the API, running
separately, reported that no worker had ever run while invoices were being
processed seconds earlier.

No row-level security on it, deliberately: a worker serves every tenant, so
scoping its liveness to one would make the banner wrong for all the others and
RLS would hide the row from the query that needs it.

Revision ID: b1f3c7a90e42
Revises: cfa7d4579f24
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b1f3c7a90e42"
down_revision: str | None = "cfa7d4579f24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "worker_heartbeats",
        # One row per worker process, so two workers do not overwrite each
        # other and the newest beat still answers "is anything alive".
        sa.Column("worker_id", sa.String(length=128), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("worker_id"),
    )
    op.create_index(
        "ix_worker_heartbeats_last_seen",
        "worker_heartbeats",
        ["last_seen_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_worker_heartbeats_last_seen", table_name="worker_heartbeats"
    )
    op.drop_table("worker_heartbeats")
