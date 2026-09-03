"""index approval date on its own, for the sheet's date ranges

`ix_invoices_approval` leads with the decision, so it only helps a query that
also constrains the decision. The sheet's `status=all` view with an approval
date range does not, and that scan grows with the whole invoice table. One more
expression index, Postgres-only for the same reason as the others.

Revision ID: f3d8b2c15e07
Revises: e91c48d7a2b5
Create Date: 2026-09-03
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f3d8b2c15e07"
down_revision: str | None = "e91c48d7a2b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DECIDED_AT = "((fields -> 'workflow') ->> 'decided_at')"


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        "CREATE INDEX ix_invoices_approved_at ON invoices "
        f"(tenant_id, {DECIDED_AT} DESC)"
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_invoices_approved_at")
