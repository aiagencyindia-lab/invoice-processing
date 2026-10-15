"""v2 trust layer, Wave 0: evidence graph, history import, findings v2

Revision ID: e2f3a4b5c6d7
Revises: d1a2b3c4e5f6
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from invoice_processing.db.rls import (
    drop_trust_citation_trigger_statements,
    enable_rls_statements,
    trust_citation_trigger_statements,
)

revision: str = "e2f3a4b5c6d7"
down_revision: str | None = "d1a2b3c4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The tables *this* migration creates, named rather than imported.
RLS_TABLES = [
    "graph_nodes",
    "graph_edges",
    "entity_links",
    "trust_history_imports",
    "history_records",
    "trust_findings",
    "trust_finding_citations",
    "trust_finding_events",
]

JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _id():
    return sa.Column("id", sa.String(36), primary_key=True)


def _tenant():
    return sa.Column(
        "tenant_id",
        sa.String(36),
        sa.ForeignKey("tenants.id"),
        nullable=False,
        index=True,
    )


def _stamps():
    return (
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def _valid():
    return (
        sa.Column(
            "valid_from",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("valid_to", sa.DateTime(timezone=True)),
    )


def _node_fk(name: str):
    return sa.Column(
        name,
        sa.String(36),
        sa.ForeignKey("graph_nodes.id"),
        nullable=False,
        index=True,
    )


def _open_index(
    name: str, table: str, cols: list[str], when: str = "valid_to IS NULL"
) -> None:
    op.create_index(
        name,
        table,
        cols,
        unique=True,
        postgresql_where=sa.text(when),
        sqlite_where=sa.text(when),
    )


def upgrade() -> None:
    op.create_table(
        "graph_nodes",
        _id(),
        _tenant(),
        sa.Column("kind", sa.String(32), nullable=False, index=True),
        sa.Column("natural_key", sa.String(512), nullable=False),
        sa.Column("ref_table", sa.String(64)),
        sa.Column("ref_id", sa.String(36), index=True),
        sa.Column("attrs", JSON, nullable=False, server_default="{}"),
        sa.Column("natural_keys", JSON, nullable=False, server_default="{}"),
        *_valid(),
        *_stamps(),
    )
    _open_index(
        "uq_graph_nodes_open",
        "graph_nodes",
        ["tenant_id", "kind", "natural_key"],
    )

    op.create_table(
        "graph_edges",
        _id(),
        _tenant(),
        _node_fk("src"),
        _node_fk("dst"),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("source_system", sa.String(64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provenance", sa.String(16), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("attrs", JSON, nullable=False, server_default="{}"),
        *_valid(),
        *_stamps(),
    )
    _open_index(
        "uq_graph_edges_open",
        "graph_edges",
        ["tenant_id", "src", "dst", "kind", "source_system"],
    )

    op.create_table(
        "entity_links",
        _id(),
        _tenant(),
        _node_fk("node_a"),
        _node_fk("node_b"),
        sa.Column("score", sa.Numeric(5, 4), nullable=False),
        sa.Column("method", sa.String(64), nullable=False),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="proposed"
        ),
        sa.Column("decided_by", sa.String(320)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("reversed_at", sa.DateTime(timezone=True)),
        sa.Column("reason", sa.String(500)),
        *_stamps(),
    )
    _open_index(
        "uq_entity_links_open",
        "entity_links",
        ["tenant_id", "node_a", "node_b"],
        when="reversed_at IS NULL",
    )

    op.create_table(
        "trust_history_imports",
        _id(),
        _tenant(),
        sa.Column("source_name", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("file_sha256", sa.String(64), nullable=False),
        sa.Column("filename", sa.String(255)),
        sa.Column("mapping", JSON, nullable=False, server_default="{}"),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="complete"
        ),
        sa.Column("rows_ok", sa.Integer, nullable=False, server_default="0"),
        sa.Column("rows_bad", sa.Integer, nullable=False, server_default="0"),
        sa.Column("errors", JSON, nullable=False, server_default="[]"),
        sa.Column("created_by", sa.String(320)),
        *_stamps(),
        sa.UniqueConstraint(
            "tenant_id", "file_sha256", name="uq_trust_history_imports_sha"
        ),
    )

    op.create_table(
        "history_records",
        _id(),
        _tenant(),
        sa.Column(
            "import_id",
            sa.String(36),
            sa.ForeignKey("trust_history_imports.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("kind", sa.String(32), nullable=False, index=True),
        sa.Column("row_number", sa.Integer, nullable=False),
        sa.Column("external_id", sa.String(128), index=True),
        sa.Column("fields", JSON, nullable=False, server_default="{}"),
        sa.Column("amount", sa.Numeric(18, 4)),
        sa.Column("currency", sa.String(8)),
        sa.Column("record_date", sa.Date),
        sa.Column("vendor_key", sa.String(512), index=True),
        sa.Column("doc_number", sa.String(128), index=True),
        *_stamps(),
        sa.UniqueConstraint(
            "tenant_id",
            "import_id",
            "row_number",
            name="uq_history_records_row",
        ),
    )

    op.create_table(
        "trust_findings",
        _id(),
        _tenant(),
        sa.Column("type", sa.String(64), nullable=False, index=True),
        _node_fk("transaction_node"),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="detected",
            index=True,
        ),
        sa.Column("impact", sa.Numeric(18, 4)),
        sa.Column("currency", sa.String(8)),
        sa.Column("confidence", sa.Numeric(5, 4)),
        sa.Column("owner_id", sa.String(320)),
        sa.Column("engine_version", sa.String(32)),
        sa.Column("rule_version", sa.String(32)),
        sa.Column("model_version", sa.String(64)),
        sa.Column("dedupe_key", sa.String(256), nullable=False),
        sa.Column("summary", sa.Text),
        sa.Column("details", JSON, nullable=False, server_default="{}"),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        *_stamps(),
        sa.UniqueConstraint(
            "tenant_id", "dedupe_key", name="uq_trust_findings_dedupe"
        ),
    )

    op.create_table(
        "trust_finding_citations",
        _id(),
        _tenant(),
        sa.Column(
            "finding_id",
            sa.String(36),
            sa.ForeignKey("trust_findings.id"),
            nullable=False,
            index=True,
        ),
        _node_fk("node_id"),
        sa.Column("excerpt", sa.Text),
    )

    op.create_table(
        "trust_finding_events",
        _id(),
        _tenant(),
        sa.Column(
            "finding_id",
            sa.String(36),
            sa.ForeignKey("trust_findings.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("from_status", sa.String(32)),
        sa.Column("to_status", sa.String(32), nullable=False),
        sa.Column("amount", sa.Numeric(18, 4)),
        sa.Column("actor", sa.String(320), nullable=False),
        sa.Column("reason", sa.String(500)),
        *_stamps(),
    )

    if op.get_bind().dialect.name != "postgresql":
        return
    for statement in enable_rls_statements(RLS_TABLES):
        op.execute(statement)
    for statement in trust_citation_trigger_statements():
        op.execute(statement)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for statement in drop_trust_citation_trigger_statements():
            op.execute(statement)
    for table in reversed(RLS_TABLES):
        op.drop_table(table)
