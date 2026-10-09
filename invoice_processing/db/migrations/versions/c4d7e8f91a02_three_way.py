"""three-way match: purchase orders, lines, goods receipts

Revision ID: c4d7e8f91a02
Revises: b21ac023e07e
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from invoice_processing.db.rls import enable_rls_statements

revision: str = "c4d7e8f91a02"
down_revision: str | None = "b21ac023e07e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The tables *this* migration creates, named rather than imported. The live
# TENANT_TABLES grows; this list is history and must not.
RLS_TABLES = ["purchase_orders", "po_lines", "goods_receipts", "grn_lines"]


def upgrade() -> None:
    op.create_table(
        "purchase_orders",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("po_number", sa.String(128), nullable=False),
        sa.Column("po_number_display", sa.String(128), nullable=False),
        sa.Column("vendor_name", sa.String(512), nullable=False),
        sa.Column("total_amount", sa.Numeric(18, 4), nullable=False),
        sa.Column(
            "currency", sa.String(3), nullable=False, server_default="AUD"
        ),
        sa.Column(
            "status", sa.String(32), nullable=False, server_default="open"
        ),
        sa.Column(
            "amount_invoiced",
            sa.Numeric(18, 4),
            nullable=False,
            server_default="0",
        ),
        sa.Column("customer_name", sa.String(512)),
        sa.Column("valid_from", sa.String(10)),
        sa.Column("valid_to", sa.String(10)),
        sa.Column(
            "amount_basis",
            sa.String(8),
            nullable=False,
            server_default="inc_tax",
        ),
        sa.Column("approved_quantity", sa.Numeric(18, 4)),
        sa.Column("approved_unit_price", sa.Numeric(18, 4)),
        sa.Column("hsn_sac", sa.String(16)),
        sa.Column(
            "source", sa.String(16), nullable=False, server_default="upload"
        ),
        sa.Column(
            "uploaded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("uploaded_by", sa.String(320), nullable=False),
        *[
            sa.Column(
                name,
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            )
            for name in ("created_at", "updated_at")
        ],
        sa.UniqueConstraint(
            "tenant_id", "po_number", name="uq_purchase_orders_number"
        ),
    )

    op.create_table(
        "po_lines",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "purchase_order_id",
            sa.String(36),
            sa.ForeignKey("purchase_orders.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("line_number", sa.Integer, nullable=False),
        sa.Column("item_code", sa.String(64)),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("qty_ordered", sa.Numeric(18, 4), nullable=False),
        sa.Column("unit_price", sa.Numeric(18, 4), nullable=False),
        sa.Column("uom", sa.String(16)),
        sa.Column("receipt_required", sa.Boolean),
        sa.Column(
            "qty_invoiced_opening",
            sa.Numeric(18, 4),
            nullable=False,
            server_default="0",
        ),
        sa.UniqueConstraint(
            "purchase_order_id", "line_number", name="uq_po_lines_number"
        ),
    )

    op.create_table(
        "goods_receipts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("grn_number", sa.String(128), nullable=False),
        sa.Column(
            "purchase_order_id",
            sa.String(36),
            sa.ForeignKey("purchase_orders.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("received_on", sa.String(10), nullable=False),
        sa.Column("reason", sa.String(500)),
        sa.Column(
            "source", sa.String(16), nullable=False, server_default="upload"
        ),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("recorded_by", sa.String(320), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "grn_number", name="uq_goods_receipts_number"
        ),
    )

    op.create_table(
        "grn_lines",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "goods_receipt_id",
            sa.String(36),
            sa.ForeignKey("goods_receipts.id"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "po_line_id",
            sa.String(36),
            sa.ForeignKey("po_lines.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("qty_received", sa.Numeric(18, 4), nullable=False),
    )

    if op.get_bind().dialect.name != "postgresql":
        return
    for statement in enable_rls_statements(RLS_TABLES):
        op.execute(statement)


def downgrade() -> None:
    op.drop_table("grn_lines")
    op.drop_table("goods_receipts")
    op.drop_table("po_lines")
    op.drop_table("purchase_orders")
