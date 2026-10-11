"""sso_domains and the account link on users

A tenant admin binds an email domain to a sign-in provider; a first sign-in
from that domain provisions the person. The users row learns which Identity
Platform account it belongs to and when it last signed in.

Revision ID: c9d5e2f3a4b6
Revises: b8c4d1e2f3a5
Create Date: 2026-10-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from invoice_processing.db.rls import enable_rls_statements

revision: str = "c9d5e2f3a4b6"
down_revision: str | None = "b8c4d1e2f3a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RLS_TABLES = ["sso_domains"]


def upgrade() -> None:
    op.create_table(
        "sso_domains",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("domain", sa.String(253), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("default_role", sa.String(32), nullable=False),
        sa.Column("idp_tenant_id", sa.String(64)),
        sa.Column("created_by", sa.String(320), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("domain", name="uq_sso_domain"),
    )
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("uid", sa.String(128)))
        batch.add_column(sa.Column("last_login_at", sa.DateTime(timezone=True)))
        batch.create_unique_constraint("uq_user_uid", ["tenant_id", "uid"])

    if op.get_bind().dialect.name != "postgresql":
        return
    for statement in enable_rls_statements(RLS_TABLES):
        op.execute(statement)


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_constraint("uq_user_uid", type_="unique")
        batch.drop_column("last_login_at")
        batch.drop_column("uid")
    op.drop_table("sso_domains")
