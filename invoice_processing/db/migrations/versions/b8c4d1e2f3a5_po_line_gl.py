"""po_lines.gl_account: the buyer's GL coding on the order

An invoice line paired to a PO line should take the account the buyer chose
when raising the order, instead of being guessed again by vendor and keyword
rules. The column is fed only by the ERP PO upload.

Revision ID: b8c4d1e2f3a5
Revises: a7b3c9d2e4f6
Create Date: 2026-10-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8c4d1e2f3a5"
down_revision: str | None = "a7b3c9d2e4f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# No new table, so nothing to protect with RLS here.
RLS_TABLES: list[str] = []


def upgrade() -> None:
    op.add_column("po_lines", sa.Column("gl_account", sa.String(64)))


def downgrade() -> None:
    # batch mode: SQLite drops a column by rebuilding the table.
    with op.batch_alter_table("po_lines") as batch:
        batch.drop_column("gl_account")
