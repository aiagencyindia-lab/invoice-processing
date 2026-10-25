"""sso_domains.idp_provider_id: the OIDC provider a Microsoft binding trusts

A Microsoft binding admits a token only through the Identity Platform OIDC
provider created for that customer's Entra tenant; this is its id
(`oidc.<name>`). Set by the operator, never by a tenant admin.

Revision ID: a8d4e6f1b2c3
Revises: d4e5f6a7b8c9
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a8d4e6f1b2c3"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RLS_TABLES: list[str] = []


def upgrade() -> None:
    with op.batch_alter_table("sso_domains") as batch:
        batch.add_column(sa.Column("idp_provider_id", sa.String(64)))


def downgrade() -> None:
    with op.batch_alter_table("sso_domains") as batch:
        batch.drop_column("idp_provider_id")
