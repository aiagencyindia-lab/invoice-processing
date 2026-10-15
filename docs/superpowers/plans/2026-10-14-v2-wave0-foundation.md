# v2 Wave 0 — Trust Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the shared foundation every v2 engine stands on: history import, the Evidence Graph, findings v2, `decide()`, provider adapters and the benchmark v2 harness, with nothing a user sees changing.

**Architecture:** One new package `invoice_processing/trust/` inside the existing app. Eight new tenant-scoped tables (Postgres is the graph store, ADR-12). Two new worker job types on the existing queue. One hook in the v1 pipeline that enqueues `trust.evaluate` after an invoice is processed and does nothing while every engine is `off`. Time-travel is "never update in place": close the old row, insert a new one.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 + Alembic (Postgres with forced RLS; SQLite in tests), pydantic 2, openpyxl (read-only), existing `gemini.py`/`llm_guard.py` for the model, pytest (`--import-mode=importlib`), ruff.

**Spec:** `docs/superpowers/specs/2026-10-14-v2-trust-layer-design.md` (§3.1 contracts, §4 Wave 0).

**Context:** The user chose to build PRD v2.0 whole and in order, with independent sub-projects running in parallel from Wave 1. Wave 0 is the one sequential wave: every later engine writes findings into this graph, so the contracts here (read graph → write cited findings, versions on everything, fail closed for holds, one model door, one provider door) must land first and be right. Production is paused on Railway; nothing here needs it running.

**Branch:** `v2/wave0-foundation` from `main` (3581dd7). On approval of this plan, save it to `docs/superpowers/plans/2026-10-14-v2-wave0-foundation.md` and commit it as the branch's first commit. Execution method already chosen by the user's standing workflow: **native** (`superpowers:executing-plans`), one implementer, then a whole-branch review on the `fable` model, one fix pass including minors, verification, PR.

## Global Constraints

- Money is `Numeric(18, 4)`, never float; arithmetic in `Decimal`. The model never computes money (spec §3.1.2).
- Every new table carries `TenantMixin`, is appended to `TENANT_TABLES`, and is listed in the migration's own `RLS_TABLES`; RLS via `enable_rls_statements(RLS_TABLES)` only. Never add policies inside `rls.enable_rls_statements` (spec §5; `rls.py:135-138`).
- No endpoint takes `tenant_id`; every new route gets a cross-tenant probe or an `EXEMPT` reason in `tests/test_tenant_isolation_api.py` (HANDOFF15 §4).
- Refusals are plain sentences: 400 for bad rows (`"row N: …"` joined by `"; "`), 403 role, 409 state, 413 size, 503 `"<X> needs a database. Configure DATABASE_URL."` (pattern: `api/po_routes.py`).
- Logging: `logger.info("event", extra={...})`; never put `request_id`/`tenant_id`/`document_id` in `extra` (KeyError); set `observability.current_*` ContextVars instead.
- New audit event names must resolve in `repositories._ENTITY_TYPES`; it is an exact match on the head (`event.rsplit("_",1)[0]`) then the prefix (`event.split("_",1)[0]`).
- Graph `attrs` are masked with `masking.mask_row` before storage; raw bank numbers only in their v1 source rows.
- Engine mode per tenant: `off → shadow → advisory → enforcing`, default `off`.
- Every finding has ≥ 1 citation (refused in Python on both DBs; a deferred constraint trigger on Postgres).
- Time-travel rows (`graph_nodes`, `graph_edges`, `entity_links`) are never updated in place.
- `decide()` never raises on model trouble: it returns each question's declared fallback marked `fallback=True`.
- Providers never raise to engines: an error becomes an `unavailable` evidence record.
- Tests: TDD, watch the test fail first. DB test files are added to the Postgres list in `.github/workflows/ci.yml` (lines 157-183). `benchmark/history/` is linted by ruff (only `benchmark/dataset/generator` is excluded): no `/home/claude` paths, no pandas.
- Commit trailer: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Never `git commit -a` (`.impeccable/hook.cache.json` is dirty); stage explicit paths.

## Review Focus

1. **A re-imported file with the same bytes but a different filename** must still be a no-op (dedupe by sha256, not name). Pinned in Task 5 `test_same_sha256_is_noop_even_with_a_new_filename`.
2. **An Excel cell holding a number as text (`"1,00,000.50"`, Indian grouping)** must be parsed to `Decimal("100000.5000")` or reported as a bad row, never silently stored as 1. Pinned in Task 5 `test_grouped_number_text_is_parsed_or_refused`.
3. **A graph sync that crashes halfway and is redelivered** must not duplicate open rows or close rows it already closed. Pinned in Task 11 `test_trust_sync_job_is_idempotent_on_redelivery`.
4. **A finding a human already decided** must never be reopened or have its ₹ changed by an engine re-run. Pinned in Task 7 `test_rerun_refreshes_but_never_reopens_a_decided_finding`.
5. **Two vendors with different GSTINs but identical normalised names** must never auto-link (contradicting statutory ids beat name). Pinned in Task 8 `test_conflicting_gstins_never_link_even_with_same_name`.

---

## File map

| Path | Responsibility |
|---|---|
| `invoice_processing/db/models.py` | 8 new ORM classes + `TENANT_TABLES` entries |
| `invoice_processing/db/rls.py` | `trust_citation_trigger_statements()` / `drop_…` (Postgres only) |
| `invoice_processing/db/migrations/versions/e2f3a4b5c6d7_trust_wave0.py` | creates the 8 tables, RLS, trigger |
| `invoice_processing/trust/__init__.py` | package doc only |
| `invoice_processing/trust/modes.py` | `ENGINES`, `MODES`, `engine_mode()` |
| `invoice_processing/tenant_settings.py` | `trust_engine_modes`, `trust_providers` settings |
| `invoice_processing/trust/decide.py` | `Question`, `Answer`, `decide()`, `GeminiDecider`, `FakeDecider` |
| `invoice_processing/trust/graph/store.py` | `GraphStore`: time-travel nodes/edges |
| `invoice_processing/trust/graph/resolve.py` | pure entity-resolution scoring |
| `invoice_processing/trust/graph/links.py` | `LinkStore`: entity links lifecycle |
| `invoice_processing/trust/graph/sync.py` | `sync_tenant()`: v1 + history → graph |
| `invoice_processing/trust/graph/transactions.py` | `Transaction`, `transactions()` |
| `invoice_processing/trust/graph/lineage.py` | `lineage()` with found/no_evidence/contradicts |
| `invoice_processing/trust/history/readers.py` | CSV/XLSX → headers + rows |
| `invoice_processing/trust/history/mapping.py` | field catalogue, synonym + `decide()` proposal |
| `invoice_processing/trust/history/importer.py` | `HistoryImporter`: one file, one transaction |
| `invoice_processing/trust/history/__main__.py` | CLI |
| `invoice_processing/trust/findings/model.py` | lifecycle, `FindingSpec`, type registry |
| `invoice_processing/trust/findings/store.py` | `FindingStore`: upsert, transition |
| `invoice_processing/trust/providers/base.py` | capability Protocols, `ProviderResult` |
| `invoice_processing/trust/providers/registry.py` | fake/real per tenant, evidence capture |
| `invoice_processing/trust/providers/fakes.py` | scripted `FakeProvider` |
| `invoice_processing/trust/evaluate.py` | `evaluate()`: incremental sync, engines gated by mode |
| `invoice_processing/api/trust_routes.py` | 3 history routes |
| `invoice_processing/api/service.py` | `get_graph_store()`, `get_history_importer()`, router include |
| `invoice_processing/worker/runner.py` | typed-job dispatch, 2 handlers, the v1 hook, 2 enqueue helpers |
| `benchmark/history/generate.py`, `eval.py` | tenant-history generator, round-trip gate |
| `tests/test_trust_*.py`, `tests/test_benchmark_history.py` | one test module per task |

---

### Task 1: Schema — eight tables, migration, RLS, citation trigger

**Files:**
- Modify: `invoice_processing/db/models.py` (after `SupportingDocument`, ~line 902; `TENANT_TABLES` at 905-932)
- Modify: `invoice_processing/db/rls.py` (append two functions)
- Create: `invoice_processing/db/migrations/versions/e2f3a4b5c6d7_trust_wave0.py`
- Modify: `tests/test_gcs_and_migrations.py:476` (parametrize) and `:547-559` (table list)
- Test: `tests/test_trust_schema.py`

**Interfaces:**
- Produces ORM classes `GraphNode, GraphEdge, EntityLink, TrustHistoryImport, HistoryRecord, TrustFinding, TrustFindingCitation, TrustFindingEvent` (columns below) and `rls.trust_citation_trigger_statements() -> list[str]`, `rls.drop_trust_citation_trigger_statements() -> list[str]`.

- [ ] **Step 1: Write the failing schema test**

```python
# tests/test_trust_schema.py
"""The Wave 0 tables: time-travel uniqueness and the citation rule."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from invoice_processing.db import models
from invoice_processing.db.models import TENANT_TABLES
from invoice_processing.db.session import create_all, get_engine, tenant_session

T = "tenant-acme"
NOW = datetime(2026, 10, 14, tzinfo=timezone.utc)


@pytest.fixture
def url(scratch_db):
    url = scratch_db("trust_schema.db")
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
    with tenant_session(T, url) as s:
        s.add(models.Tenant(id=T, name="Acme", slug="acme", status="active"))
    return url


def test_the_eight_tables_are_tenant_tables():
    for name in (
        "graph_nodes", "graph_edges", "entity_links", "trust_history_imports",
        "history_records", "trust_findings", "trust_finding_citations",
        "trust_finding_events",
    ):
        assert name in TENANT_TABLES, name


def test_open_node_natural_key_is_unique_but_closed_rows_may_repeat(url):
    with tenant_session(T, url) as s:
        s.add(models.GraphNode(tenant_id=T, kind="vendor", natural_key="v1", valid_from=NOW))
    with pytest.raises(IntegrityError):
        with tenant_session(T, url) as s:
            s.add(models.GraphNode(tenant_id=T, kind="vendor", natural_key="v1", valid_from=NOW))
    with tenant_session(T, url) as s:
        first = s.query(models.GraphNode).one()
        first.valid_to = NOW
    with tenant_session(T, url) as s:  # a second open row is fine once the first is closed
        s.add(models.GraphNode(tenant_id=T, kind="vendor", natural_key="v1", valid_from=NOW))
    with tenant_session(T, url) as s:
        assert s.query(models.GraphNode).count() == 2


def test_dedupe_key_is_unique_per_tenant(url):
    with tenant_session(T, url) as s:
        node = models.GraphNode(tenant_id=T, kind="invoice", natural_key="i1", valid_from=NOW)
        s.add(node)
        s.flush()
        s.add(models.TrustFinding(tenant_id=T, type="BENCH-IMPORT", transaction_node=node.id, dedupe_key="k"))
    with pytest.raises(IntegrityError):
        with tenant_session(T, url) as s:
            node_id = s.query(models.GraphNode).one().id
            s.add(models.TrustFinding(tenant_id=T, type="BENCH-IMPORT", transaction_node=node_id, dedupe_key="k"))
```

- [ ] **Step 2: Run it to see it fail**

Run: `./.venv/bin/python -m pytest tests/test_trust_schema.py -q -p no:cacheprovider`
Expected: FAIL with `AttributeError: module ... has no attribute 'GraphNode'`.

- [ ] **Step 3: Add the models**

Append after `SupportingDocument` in `invoice_processing/db/models.py` (imports `Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, text` are already present):

```python
# ---------------------------------------------------------------------------
# v2 Trust layer (Wave 0). Time-travel tables are never updated in place: a
# change closes the current row (valid_to) and inserts a new one, so "what did
# we know on date D" is a filter, not a reconstruction.
# ---------------------------------------------------------------------------


def _open(name: str, *cols: str, when: str = "valid_to IS NULL") -> Index:
    """One open row per key; closed rows may repeat the key."""
    return Index(
        name, *cols, unique=True,
        postgresql_where=text(when), sqlite_where=text(when),
    )


class GraphNode(Base, TenantMixin, TimestampMixin):
    """An artefact or party in the Evidence Graph (PRD FR-19.1)."""

    __tablename__ = "graph_nodes"
    __table_args__ = (_open("uq_graph_nodes_open", "tenant_id", "kind", "natural_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    natural_key: Mapped[str] = mapped_column(String(512), nullable=False)
    # Backed by an existing row: nothing is copied, only pointed at.
    ref_table: Mapped[str | None] = mapped_column(String(64))
    ref_id: Mapped[str | None] = mapped_column(String(36), index=True)
    attrs: Mapped[dict] = mapped_column(JSONType, default=dict)
    natural_keys: Mapped[dict] = mapped_column(JSONType, default=dict)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GraphEdge(Base, TenantMixin, TimestampMixin):
    """A link between two nodes, with where it came from and how sure we are."""

    __tablename__ = "graph_edges"
    __table_args__ = (_open("uq_graph_edges_open", "tenant_id", "src", "dst", "kind", "source_system"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    src: Mapped[str] = mapped_column(String(36), ForeignKey("graph_nodes.id"), nullable=False, index=True)
    dst: Mapped[str] = mapped_column(String(36), ForeignKey("graph_nodes.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # document | system_record | human_assertion | model_inference
    provenance: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    attrs: Mapped[dict] = mapped_column(JSONType, default=dict)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EntityLink(Base, TenantMixin, TimestampMixin):
    """Two nodes judged to be one entity. Never deletes a node; reversible."""

    __tablename__ = "entity_links"
    __table_args__ = (_open("uq_entity_links_open", "tenant_id", "node_a", "node_b", when="reversed_at IS NULL"),)

    AUTO, PROPOSED, ACCEPTED, REJECTED = "auto", "proposed", "accepted", "rejected"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    node_a: Mapped[str] = mapped_column(String(36), ForeignKey("graph_nodes.id"), nullable=False, index=True)
    node_b: Mapped[str] = mapped_column(String(36), ForeignKey("graph_nodes.id"), nullable=False, index=True)
    score: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    method: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=PROPOSED)
    decided_by: Mapped[str | None] = mapped_column(String(320))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(String(500))


class TrustHistoryImport(Base, TenantMixin, TimestampMixin):
    """One uploaded history file. Re-uploading the same bytes is a no-op."""

    __tablename__ = "trust_history_imports"
    __table_args__ = (UniqueConstraint("tenant_id", "file_sha256", name="uq_trust_history_imports_sha"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    source_name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    filename: Mapped[str | None] = mapped_column(String(255))
    mapping: Mapped[dict] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="complete")
    rows_ok: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_bad: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors: Mapped[list] = mapped_column(JSONType, default=list)
    created_by: Mapped[str | None] = mapped_column(String(320))


class HistoryRecord(Base, TenantMixin, TimestampMixin):
    """One normalised row of imported history."""

    __tablename__ = "history_records"
    __table_args__ = (UniqueConstraint("tenant_id", "import_id", "row_number", name="uq_history_records_row"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    import_id: Mapped[str] = mapped_column(String(36), ForeignKey("trust_history_imports.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(128), index=True)
    fields: Mapped[dict] = mapped_column(JSONType, default=dict)
    amount: Mapped[float | None] = mapped_column(Numeric(18, 4))
    currency: Mapped[str | None] = mapped_column(String(8))
    record_date: Mapped[date | None] = mapped_column(Date)
    vendor_key: Mapped[str | None] = mapped_column(String(512), index=True)
    doc_number: Mapped[str | None] = mapped_column(String(128), index=True)


class TrustFinding(Base, TenantMixin, TimestampMixin):
    """A quantified, cited claim that money is being lost (PRD FR-20.7)."""

    __tablename__ = "trust_findings"
    __table_args__ = (UniqueConstraint("tenant_id", "dedupe_key", name="uq_trust_findings_dedupe"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    transaction_node: Mapped[str] = mapped_column(String(36), ForeignKey("graph_nodes.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="detected", index=True)
    impact: Mapped[float | None] = mapped_column(Numeric(18, 4))
    currency: Mapped[str | None] = mapped_column(String(8))
    confidence: Mapped[float | None] = mapped_column(Numeric(5, 4))
    owner_id: Mapped[str | None] = mapped_column(String(320))
    engine_version: Mapped[str | None] = mapped_column(String(32))
    rule_version: Mapped[str | None] = mapped_column(String(32))
    model_version: Mapped[str | None] = mapped_column(String(64))
    dedupe_key: Mapped[str] = mapped_column(String(256), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSONType, default=dict)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TrustFindingCitation(Base, TenantMixin):
    """Why we believe a finding: a graph node and an excerpt. At least one each."""

    __tablename__ = "trust_finding_citations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    finding_id: Mapped[str] = mapped_column(String(36), ForeignKey("trust_findings.id"), nullable=False, index=True)
    node_id: Mapped[str] = mapped_column(String(36), ForeignKey("graph_nodes.id"), nullable=False, index=True)
    excerpt: Mapped[str | None] = mapped_column(Text)


class TrustFindingEvent(Base, TenantMixin, TimestampMixin):
    """Every status change with the money at that stage: the recovery ledger."""

    __tablename__ = "trust_finding_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    finding_id: Mapped[str] = mapped_column(String(36), ForeignKey("trust_findings.id"), nullable=False, index=True)
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[float | None] = mapped_column(Numeric(18, 4))
    actor: Mapped[str] = mapped_column(String(320), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500))
```

Then extend `TENANT_TABLES` (keep it last in the file):

```python
    "sso_domains",
    "graph_nodes",
    "graph_edges",
    "entity_links",
    "trust_history_imports",
    "history_records",
    "trust_findings",
    "trust_finding_citations",
    "trust_finding_events",
)
```

- [ ] **Step 4: Run the schema test to see it pass**

Run: `./.venv/bin/python -m pytest tests/test_trust_schema.py -q -p no:cacheprovider`
Expected: 3 passed.

- [ ] **Step 5: Add the citation trigger DDL to `rls.py`**

Append to `invoice_processing/db/rls.py`:

```python
def trust_citation_trigger_statements() -> list[str]:
    """A finding without a citation cannot be committed (PRD FR-24.2).

    Deferred to commit so the finding and its citations land in one
    transaction. Not part of `enable_rls_statements`: migrations replay that
    as history. Postgres only; SQLite relies on the Python writer.
    """
    return [
        """
        CREATE OR REPLACE FUNCTION trust_finding_needs_citation() RETURNS trigger AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM trust_finding_citations c WHERE c.finding_id = NEW.id
            ) THEN
                RAISE EXCEPTION 'trust_findings row % has no citation', NEW.id;
            END IF;
            RETURN NULL;
        END $$ LANGUAGE plpgsql;
        """,
        """
        CREATE CONSTRAINT TRIGGER trust_finding_citation_check
        AFTER INSERT ON trust_findings DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION trust_finding_needs_citation();
        """,
    ]


def drop_trust_citation_trigger_statements() -> list[str]:
    return [
        "DROP TRIGGER IF EXISTS trust_finding_citation_check ON trust_findings;",
        "DROP FUNCTION IF EXISTS trust_finding_needs_citation();",
    ]
```

- [ ] **Step 6: Write the migration**

Create `invoice_processing/db/migrations/versions/e2f3a4b5c6d7_trust_wave0.py`:

```python
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
    return sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False, index=True)


def _stamps():
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def _valid():
    return (
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("valid_to", sa.DateTime(timezone=True)),
    )


def _open_index(name: str, table: str, cols: list[str], when: str = "valid_to IS NULL") -> None:
    op.create_index(name, table, cols, unique=True, postgresql_where=sa.text(when), sqlite_where=sa.text(when))


def upgrade() -> None:
    op.create_table(
        "graph_nodes",
        _id(), _tenant(),
        sa.Column("kind", sa.String(32), nullable=False, index=True),
        sa.Column("natural_key", sa.String(512), nullable=False),
        sa.Column("ref_table", sa.String(64)),
        sa.Column("ref_id", sa.String(36), index=True),
        sa.Column("attrs", JSON, nullable=False, server_default="{}"),
        sa.Column("natural_keys", JSON, nullable=False, server_default="{}"),
        *_valid(), *_stamps(),
    )
    _open_index("uq_graph_nodes_open", "graph_nodes", ["tenant_id", "kind", "natural_key"])

    op.create_table(
        "graph_edges",
        _id(), _tenant(),
        sa.Column("src", sa.String(36), sa.ForeignKey("graph_nodes.id"), nullable=False, index=True),
        sa.Column("dst", sa.String(36), sa.ForeignKey("graph_nodes.id"), nullable=False, index=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("source_system", sa.String(64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provenance", sa.String(16), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("attrs", JSON, nullable=False, server_default="{}"),
        *_valid(), *_stamps(),
    )
    _open_index("uq_graph_edges_open", "graph_edges", ["tenant_id", "src", "dst", "kind", "source_system"])

    op.create_table(
        "entity_links",
        _id(), _tenant(),
        sa.Column("node_a", sa.String(36), sa.ForeignKey("graph_nodes.id"), nullable=False, index=True),
        sa.Column("node_b", sa.String(36), sa.ForeignKey("graph_nodes.id"), nullable=False, index=True),
        sa.Column("score", sa.Numeric(5, 4), nullable=False),
        sa.Column("method", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="proposed"),
        sa.Column("decided_by", sa.String(320)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("reversed_at", sa.DateTime(timezone=True)),
        sa.Column("reason", sa.String(500)),
        *_stamps(),
    )
    _open_index("uq_entity_links_open", "entity_links", ["tenant_id", "node_a", "node_b"], when="reversed_at IS NULL")

    op.create_table(
        "trust_history_imports",
        _id(), _tenant(),
        sa.Column("source_name", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("file_sha256", sa.String(64), nullable=False),
        sa.Column("filename", sa.String(255)),
        sa.Column("mapping", JSON, nullable=False, server_default="{}"),
        sa.Column("status", sa.String(16), nullable=False, server_default="complete"),
        sa.Column("rows_ok", sa.Integer, nullable=False, server_default="0"),
        sa.Column("rows_bad", sa.Integer, nullable=False, server_default="0"),
        sa.Column("errors", JSON, nullable=False, server_default="[]"),
        sa.Column("created_by", sa.String(320)),
        *_stamps(),
        sa.UniqueConstraint("tenant_id", "file_sha256", name="uq_trust_history_imports_sha"),
    )

    op.create_table(
        "history_records",
        _id(), _tenant(),
        sa.Column("import_id", sa.String(36), sa.ForeignKey("trust_history_imports.id"), nullable=False, index=True),
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
        sa.UniqueConstraint("tenant_id", "import_id", "row_number", name="uq_history_records_row"),
    )

    op.create_table(
        "trust_findings",
        _id(), _tenant(),
        sa.Column("type", sa.String(64), nullable=False, index=True),
        sa.Column("transaction_node", sa.String(36), sa.ForeignKey("graph_nodes.id"), nullable=False, index=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="detected", index=True),
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
        sa.UniqueConstraint("tenant_id", "dedupe_key", name="uq_trust_findings_dedupe"),
    )

    op.create_table(
        "trust_finding_citations",
        _id(), _tenant(),
        sa.Column("finding_id", sa.String(36), sa.ForeignKey("trust_findings.id"), nullable=False, index=True),
        sa.Column("node_id", sa.String(36), sa.ForeignKey("graph_nodes.id"), nullable=False, index=True),
        sa.Column("excerpt", sa.Text),
    )

    op.create_table(
        "trust_finding_events",
        _id(), _tenant(),
        sa.Column("finding_id", sa.String(36), sa.ForeignKey("trust_findings.id"), nullable=False, index=True),
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
```

- [ ] **Step 7: Extend the two hand-maintained migration tests**

In `tests/test_gcs_and_migrations.py` add `("e2f3a4b5c6d7", 8),` after `("d1a2b3c4e5f6", 0),` (~line 476), and append the eight table names after `"sso_domains",` in the `tables = [...]` list (~line 547-559).

- [ ] **Step 8: Run the migration and schema suites**

Run: `./.venv/bin/python -m pytest tests/test_gcs_and_migrations.py tests/test_trust_schema.py tests/test_offboarding.py tests/test_db_repositories.py -q -p no:cacheprovider`
Expected: all pass (the offboarding seeder inserts one row per new table; `test_migration_protects_exactly_the_tables_it_creates[e2f3a4b5c6d7-8]` passes).

If Postgres is available locally (`docker run -d --rm --name pg-x -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=t -p 55432:5432 postgres:16`), also run with `TEST_DATABASE_URL=postgresql+pg8000://postgres:postgres@localhost:55432/t` the quoted node id `tests/test_gcs_and_migrations.py::TestMigration::test_new_tenant_tables_have_forced_isolation_policies_after_upgrade` and `tests/test_restore_check.py`.

- [ ] **Step 9: Commit**

```bash
git add invoice_processing/db/models.py invoice_processing/db/rls.py invoice_processing/db/migrations/versions/e2f3a4b5c6d7_trust_wave0.py tests/test_trust_schema.py tests/test_gcs_and_migrations.py
git commit -m "Add the Wave 0 trust tables: evidence graph, history import, findings v2

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Engine mode and provider settings per tenant

**Files:**
- Create: `invoice_processing/trust/__init__.py`, `invoice_processing/trust/modes.py`
- Modify: `invoice_processing/tenant_settings.py` (`SETTABLE` ~30-63, `NON_ENGINE_DEFAULTS` ~69-74, `DESCRIPTIONS` ~78, `validate()` ~166)
- Test: `tests/test_trust_settings.py`

**Interfaces:**
- Produces `modes.ENGINES = ("leakage", "payment", "provenance", "gst_itc")`, `modes.MODES = ("off", "shadow", "advisory", "enforcing")`, `modes.CAPABILITIES` (the nine provider capability names, shared with Task 12), `modes.engine_mode(tenant_id: str, engine: str, store=None) -> str`, `modes.provider_name(tenant_id: str, capability: str, store=None) -> str` (default `"fake"`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_trust_settings.py
"""Engine modes and provider choices are tenant settings, off and fake by default."""

import pytest

from invoice_processing import tenant_settings
from invoice_processing.tenant_settings import SettingsError, validate
from invoice_processing.trust import modes


class Store:
    def __init__(self, data=None):
        self.data = data or {}

    def get(self, tenant_id):
        return self.data.get(tenant_id)


def test_engine_mode_defaults_to_off():
    assert modes.engine_mode("t", "leakage", store=Store()) == "off"


def test_engine_mode_reads_the_setting():
    store = Store({"t": {"trust_engine_modes": {"leakage": "shadow"}}})
    assert modes.engine_mode("t", "leakage", store=store) == "shadow"
    assert modes.engine_mode("t", "payment", store=store) == "off"


def test_trust_engine_modes_rejects_unknown_engine():
    with pytest.raises(SettingsError, match="No such engine"):
        validate({"trust_engine_modes": {"x": "off"}})


def test_trust_engine_modes_rejects_unknown_mode():
    with pytest.raises(SettingsError, match="must be one of"):
        validate({"trust_engine_modes": {"leakage": "on"}})


def test_trust_providers_rejects_unknown_capability():
    with pytest.raises(SettingsError, match="No such capability"):
        validate({"trust_providers": {"telepathy": "fake"}})


def test_provider_name_defaults_to_fake():
    assert modes.provider_name("t", "bank_verification", store=Store()) == "fake"


def test_settings_are_settable_and_described():
    for key in ("trust_engine_modes", "trust_providers"):
        assert key in tenant_settings.SETTABLE
        assert key in tenant_settings.DESCRIPTIONS
```

- [ ] **Step 2: Run to see it fail**

Run: `./.venv/bin/python -m pytest tests/test_trust_settings.py -q -p no:cacheprovider`
Expected: FAIL with `ModuleNotFoundError: invoice_processing.trust`.

- [ ] **Step 3: Create the package and `modes.py`**

`invoice_processing/trust/__init__.py`:

```python
"""v2 Transaction Trust layer.

Engines read the Evidence Graph and write cited findings; they never write
each other's tables, reach the model only through `decide()`, and external
systems only through `providers`. See docs/superpowers/specs/2026-10-14-v2-trust-layer-design.md §3.1.
"""
```

`invoice_processing/trust/modes.py`:

```python
"""Per-tenant switches: which engines run, how hard, and which providers."""

from __future__ import annotations

ENGINES = ("leakage", "payment", "provenance", "gst_itc")
# Only `enforcing` places holds; the rest observe (spec §3.1.5).
MODES = ("off", "shadow", "advisory", "enforcing")
CAPABILITIES = (
    "bank_verification",
    "gstin",
    "company_registry",
    "abn",
    "vat",
    "mailbox",
    "tickets",
    "timesheets",
    "logistics",
)


def engine_mode(tenant_id: str, engine: str, store=None) -> str:
    from .. import tenant_settings  # noqa: PLC0415 - avoids an import cycle

    if engine not in ENGINES:
        raise ValueError(f"No such engine: {engine}")
    setting = tenant_settings.effective(tenant_id, store).get("trust_engine_modes") or {}
    return setting.get(engine, "off")


def provider_name(tenant_id: str, capability: str, store=None) -> str:
    from .. import tenant_settings  # noqa: PLC0415

    if capability not in CAPABILITIES:
        raise ValueError(f"No such capability: {capability}")
    setting = tenant_settings.effective(tenant_id, store).get("trust_providers") or {}
    return setting.get(capability, "fake")
```

- [ ] **Step 4: Register the settings**

In `invoice_processing/tenant_settings.py` add to `SETTABLE`:

```python
    # v2 trust engines: off until an operator turns one on, per engine.
    "trust_engine_modes": dict,
    # Which adapter answers each external capability; "fake" until a real
    # provider is configured (spec §4.6).
    "trust_providers": dict,
```

to `NON_ENGINE_DEFAULTS`:

```python
    "trust_engine_modes": {},
    "trust_providers": {},
```

to `DESCRIPTIONS`:

```python
    "trust_engine_modes": (
        "Per engine (leakage, payment, provenance, gst_itc): off, shadow, "
        "advisory or enforcing. Only enforcing places holds."
    ),
    "trust_providers": (
        "Per capability (bank_verification, gstin, …): the adapter name; "
        "fake answers from fixtures until a real provider is set."
    ),
```

and in `validate()`, before the final `else` of the `if key == …` chain:

```python
        elif key == "trust_engine_modes":
            from .trust.modes import ENGINES, MODES  # noqa: PLC0415

            cleaned[key] = {}
            for engine, mode in value.items():
                if engine not in ENGINES:
                    raise SettingsError(f"No such engine: {engine}. Engines: {', '.join(ENGINES)}.")
                if mode not in MODES:
                    raise SettingsError(f"{engine} mode must be one of {', '.join(MODES)}.")
                cleaned[key][engine] = mode
        elif key == "trust_providers":
            from .trust.modes import CAPABILITIES  # noqa: PLC0415

            cleaned[key] = {}
            for capability, name in value.items():
                if capability not in CAPABILITIES:
                    raise SettingsError(f"No such capability: {capability}.")
                cleaned[key][capability] = str(name).strip() or "fake"
```

- [ ] **Step 5: Run the tests; then the settings and config-route suites**

Run: `./.venv/bin/python -m pytest tests/test_trust_settings.py tests/test_tenant_settings.py tests/test_config_routes.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add invoice_processing/trust/__init__.py invoice_processing/trust/modes.py invoice_processing/tenant_settings.py tests/test_trust_settings.py
git commit -m "Add per-tenant trust engine modes and provider choices, off and fake by default

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: `decide()` — the one door to the model

**Files:**
- Create: `invoice_processing/trust/decide.py`
- Test: `tests/test_trust_decide.py`

**Interfaces:**
- Produces:
  - `Question(id: str, kind: Literal["choice","score","yes_no"], text: str, fallback: Any, options: tuple[str, ...] = ())`
  - `Answer(id: str, value: Any, probability: float, model_version: str, fallback: bool)`
  - `decide(state: dict, questions: Sequence[Question], decider: Decider | None = None) -> list[Answer]`
  - `GeminiDecider(model_name: str | None = None)` and `FakeDecider(answers: dict[str, Any], probability: float = 0.9, fail: bool = False)`; a `Decider` is `Callable[[str], tuple[str, str]]` returning `(json_text, model_version)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_trust_decide.py
"""decide(): typed questions in, typed answers out, fallbacks on every failure."""

import logging

import pytest

from invoice_processing.llm_guard import LlmUnavailable
from invoice_processing.trust.decide import Answer, FakeDecider, Question, decide

Q = (
    Question("field", "choice", "Which field is 'Bill No'?", fallback="", options=("doc_number", "external_id")),
    Question("sure", "score", "How sure?", fallback=0.0),
    Question("same", "yes_no", "Same vendor?", fallback=False),
)


def test_valid_answers_carry_probability_and_version():
    answers = decide({"header": "Bill No"}, Q, FakeDecider({"field": "doc_number", "sure": 0.4, "same": True}))
    assert answers[0] == Answer("field", "doc_number", 0.9, "fake", False)
    assert answers[1].value == 0.4 and answers[2].value is True


def test_unavailable_model_returns_every_fallback_marked():
    answers = decide({}, Q, FakeDecider({}, fail=True))
    assert [a.fallback for a in answers] == [True, True, True]
    assert [a.value for a in answers] == ["", 0.0, False]


def test_choice_outside_options_and_missing_id_fall_back():
    answers = decide({}, Q, FakeDecider({"field": "zzz", "sure": 0.5}))
    assert answers[0].fallback and answers[0].value == ""
    assert not answers[1].fallback
    assert answers[2].fallback and answers[2].value is False


def test_score_outside_unit_interval_falls_back():
    assert decide({}, Q[1:2], FakeDecider({"sure": 7}))[0].fallback is True


def test_log_line_names_ids_not_state(caplog):
    with caplog.at_level(logging.INFO, logger="invoice_processing.trust.decide"):
        decide({"secret": "GSTIN-123"}, Q, FakeDecider({}, fail=True))
    record = next(r for r in caplog.records if r.getMessage() == "trust_decide")
    assert record.question_ids == ["field", "sure", "same"] and record.fallback_count == 3
    assert "GSTIN-123" not in caplog.text


def test_fake_fail_raises_the_outage_type_internally():
    with pytest.raises(LlmUnavailable):
        FakeDecider({}, fail=True)("prompt")
```

- [ ] **Step 2: Run to see it fail**

Run: `./.venv/bin/python -m pytest tests/test_trust_decide.py -q -p no:cacheprovider`
Expected: FAIL with `ModuleNotFoundError: invoice_processing.trust.decide`.

- [ ] **Step 3: Implement**

```python
# invoice_processing/trust/decide.py
"""decide(state, questions): the only way a v2 engine asks the model.

Typed questions go in, typed answers with probabilities come out, and every
failure -- outage, timeout, malformed reply, out-of-range value -- becomes the
question's declared fallback, marked as such. The caller decides whether a
fallback means hold (payment) or skip (insight). Nothing here raises for
model trouble, and nothing here logs the state (spec §4.5).
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from .. import gemini
from ..llm_guard import LlmBudgetExceeded, LlmCallTimeout, LlmUnavailable, guarded_generate

logger = logging.getLogger(__name__)

Kind = Literal["choice", "score", "yes_no"]
Decider = Callable[[str], tuple[str, str]]  # prompt -> (json text, model version)


@dataclass(frozen=True)
class Question:
    id: str
    kind: Kind
    text: str
    fallback: Any
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class Answer:
    id: str
    value: Any
    probability: float
    model_version: str
    fallback: bool


class _Item(BaseModel):
    id: str
    value: Any
    probability: float = 0.5


class _Reply(BaseModel):
    answers: list[_Item]


_PREAMBLE = (
    "You answer typed questions about a financial record. Reply with JSON "
    '{"answers": [{"id": ..., "value": ..., "probability": 0..1}]}. '
    "For a choice question the value is exactly one of the options; for a "
    "score it is a number from 0 to 1; for a yes_no it is true or false. "
    "Never compute money; never invent facts not in the record.\n"
)


def _prompt(state: dict, questions: Sequence[Question]) -> str:
    asked = [
        {"id": q.id, "kind": q.kind, "question": q.text, **({"options": list(q.options)} if q.options else {})}
        for q in questions
    ]
    return _PREAMBLE + "Record:\n" + json.dumps(state, default=str, sort_keys=True) + "\nQuestions:\n" + json.dumps(asked)


def _valid(q: Question, value: Any) -> bool:
    if q.kind == "choice":
        return isinstance(value, str) and value in q.options
    if q.kind == "score":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1
    return isinstance(value, bool)


class GeminiDecider:
    """Gemini structured output through the existing guarded call."""

    def __init__(self, model_name: str | None = None):
        self.model_name = model_name or os.getenv("GEMINI_FLASH_MODEL", "gemini-2.5-flash")

    def __call__(self, prompt: str) -> tuple[str, str]:
        model = gemini.Model(self.model_name, temperature=0, seed=42, response_mime_type="application/json")
        response = guarded_generate(model, prompt)
        return response.text, self.model_name


class FakeDecider:
    """Scripted answers for tests and fakes; `fail=True` behaves like an outage."""

    def __init__(self, answers: dict[str, Any], probability: float = 0.9, fail: bool = False):
        self.answers, self.probability, self.fail = answers, probability, fail
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> tuple[str, str]:
        self.prompts.append(prompt)
        if self.fail:
            raise LlmUnavailable("fake outage")
        items = [{"id": k, "value": v, "probability": self.probability} for k, v in self.answers.items()]
        return json.dumps({"answers": items}), "fake"


def decide(state: dict, questions: Sequence[Question], decider: Decider | None = None) -> list[Answer]:
    decider = decider or GeminiDecider()
    replies: dict[str, _Item] = {}
    version = "fallback"
    try:
        text, version = decider(_prompt(state, questions))
        replies = {item.id: item for item in _Reply.model_validate_json(text).answers}
    except (LlmUnavailable, LlmCallTimeout, LlmBudgetExceeded, ValidationError, ValueError, gemini.GeminiEmptyResponse):
        replies = {}

    answers = []
    for q in questions:
        item = replies.get(q.id)
        if item is not None and _valid(q, item.value) and 0 <= item.probability <= 1:
            answers.append(Answer(q.id, item.value, float(item.probability), version, False))
        else:
            answers.append(Answer(q.id, q.fallback, 0.0, version, True))
    logger.info(
        "trust_decide",
        extra={"question_ids": [q.id for q in questions], "fallback_count": sum(a.fallback for a in answers)},
    )
    return answers
```

- [ ] **Step 4: Run to see it pass**

Run: `./.venv/bin/python -m pytest tests/test_trust_decide.py -q -p no:cacheprovider`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add invoice_processing/trust/decide.py tests/test_trust_decide.py
git commit -m "Add decide(): typed questions to the model with fallbacks on every failure

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: GraphStore — time-travel nodes and edges

**Files:**
- Create: `invoice_processing/trust/graph/__init__.py`, `invoice_processing/trust/graph/store.py`
- Test: `tests/test_trust_graph_store.py`

**Interfaces:**
- Produces `GraphStore(url: str | None = None)` with:
  - `upsert_node(ctx: RunContext, *, kind: str, natural_key: str, ref_table: str | None = None, ref_id: str | None = None, attrs: dict | None = None, natural_keys: dict | None = None, at: datetime | None = None) -> str` (node id; unchanged content returns the open row's id)
  - `upsert_edge(ctx, *, src: str, dst: str, kind: str, source_system: str, provenance: str, confidence: float, observed_at: datetime, attrs: dict | None = None, at: datetime | None = None) -> str`
  - `node(tenant_id, node_id) -> dict | None`, `open_node(tenant_id, kind, natural_key) -> dict | None`
  - `nodes(tenant_id, *, kind=None, as_of=None) -> list[dict]`, `edges(tenant_id, *, src=None, dst=None, kind=None, as_of=None) -> list[dict]`
  - `nodes_by_ref(tenant_id, ref_table, ref_ids: list[str]) -> dict[str, str]` (ref_id → node id, open rows only)
  - `PROVENANCE = ("document", "system_record", "human_assertion", "model_inference")`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_trust_graph_store.py
"""Graph rows are never updated in place; the past stays queryable."""

from datetime import datetime, timedelta, timezone

import pytest

from invoice_processing.db import models
from invoice_processing.db.session import create_all, get_engine, tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust.graph.store import GraphStore

T = "tenant-acme"
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
T1 = T0 + timedelta(days=1)
CTX = RunContext(tenant_id=T, case_id="", run_id="r", actor="t")


@pytest.fixture
def store(scratch_db):
    url = scratch_db("graph.db")
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
    with tenant_session(T, url) as s:
        s.add(models.Tenant(id=T, name="Acme", slug="acme", status="active"))
    return GraphStore(url)


def test_upsert_same_attrs_is_idempotent(store):
    a = store.upsert_node(CTX, kind="vendor", natural_key="v1", attrs={"name": "Acme"}, at=T0)
    b = store.upsert_node(CTX, kind="vendor", natural_key="v1", attrs={"name": "Acme"}, at=T1)
    assert a == b and len(store.nodes(T, kind="vendor")) == 1


def test_changed_attrs_closes_old_row_and_as_of_sees_it(store):
    a = store.upsert_node(CTX, kind="vendor", natural_key="v1", attrs={"name": "Acme"}, at=T0)
    b = store.upsert_node(CTX, kind="vendor", natural_key="v1", attrs={"name": "Acme Ltd"}, at=T1)
    assert a != b
    assert [n["attrs"]["name"] for n in store.nodes(T, kind="vendor")] == ["Acme Ltd"]
    assert [n["attrs"]["name"] for n in store.nodes(T, kind="vendor", as_of=T0)] == ["Acme"]
    assert store.node(T, a)["valid_to"] is not None


def test_bank_attrs_are_masked_in_graph(store):
    nid = store.upsert_node(CTX, kind="vendor", natural_key="v1", attrs={"bank_account_number": "123456789012"})
    assert store.node(T, nid)["attrs"]["bank_account_number"] == "…9012"


def test_edge_rejects_bad_provenance_and_confidence(store):
    a = store.upsert_node(CTX, kind="invoice", natural_key="i1")
    b = store.upsert_node(CTX, kind="vendor", natural_key="v1")
    with pytest.raises(ValueError, match="provenance"):
        store.upsert_edge(CTX, src=a, dst=b, kind="issued_by", source_system="v1", provenance="guess", confidence=1, observed_at=T0)
    with pytest.raises(ValueError, match="confidence"):
        store.upsert_edge(CTX, src=a, dst=b, kind="issued_by", source_system="v1", provenance="system_record", confidence=2, observed_at=T0)


def test_edges_are_idempotent_and_filterable(store):
    a = store.upsert_node(CTX, kind="invoice", natural_key="i1")
    b = store.upsert_node(CTX, kind="vendor", natural_key="v1")
    e1 = store.upsert_edge(CTX, src=a, dst=b, kind="issued_by", source_system="v1", provenance="system_record", confidence=1, observed_at=T0)
    e2 = store.upsert_edge(CTX, src=a, dst=b, kind="issued_by", source_system="v1", provenance="system_record", confidence=1, observed_at=T0)
    assert e1 == e2
    assert [e["dst"] for e in store.edges(T, src=a, kind="issued_by")] == [b]
    assert store.nodes_by_ref(T, "invoices", ["x"]) == {}


def test_other_tenant_sees_nothing(store):
    store.upsert_node(CTX, kind="vendor", natural_key="v1")
    assert store.nodes("tenant-globex", kind="vendor") == []
```

- [ ] **Step 2: Run to see it fail**

Run: `./.venv/bin/python -m pytest tests/test_trust_graph_store.py -q -p no:cacheprovider`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`invoice_processing/trust/graph/__init__.py`: a one-line docstring `"""The Evidence Graph: nodes, edges, lineage, entity resolution."""`.

```python
# invoice_processing/trust/graph/store.py
"""Time-travel storage for graph nodes and edges.

A row is never updated. Writing a node whose content changed closes the open
row (valid_to) and inserts a new one; writing identical content returns the
open row. `as_of` filters `valid_from <= D < valid_to`, which answers "what did
we know on D" without reconstruction (PRD FR-19.5).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from ... import masking
from ...db.models import GraphEdge, GraphNode
from ...db.session import tenant_session
from ...ports import RunContext

PROVENANCE = ("document", "system_record", "human_assertion", "model_inference")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _node_dict(n: GraphNode) -> dict:
    return {
        "id": n.id, "kind": n.kind, "natural_key": n.natural_key, "ref_table": n.ref_table,
        "ref_id": n.ref_id, "attrs": dict(n.attrs or {}), "natural_keys": dict(n.natural_keys or {}),
        "valid_from": _iso(n.valid_from), "valid_to": _iso(n.valid_to),
    }


def _edge_dict(e: GraphEdge) -> dict:
    return {
        "id": e.id, "src": e.src, "dst": e.dst, "kind": e.kind, "source_system": e.source_system,
        "observed_at": _iso(e.observed_at), "provenance": e.provenance,
        "confidence": float(e.confidence), "attrs": dict(e.attrs or {}),
        "valid_from": _iso(e.valid_from), "valid_to": _iso(e.valid_to),
    }


def _as_of(model, query, as_of: datetime | None):
    if as_of is None:
        return query.where(model.valid_to.is_(None))
    return query.where(model.valid_from <= as_of).where((model.valid_to.is_(None)) | (model.valid_to > as_of))


class GraphStore:
    def __init__(self, url: str | None = None):
        self.url = url

    # -- nodes -------------------------------------------------------------

    def upsert_node(
        self, ctx: RunContext, *, kind: str, natural_key: str, ref_table: str | None = None,
        ref_id: str | None = None, attrs: dict | None = None, natural_keys: dict | None = None,
        at: datetime | None = None,
    ) -> str:
        at = at or _now()
        attrs = masking.mask_row(dict(attrs or {}))
        natural_keys = dict(natural_keys or {})
        with tenant_session(ctx.tenant_id, self.url) as s:
            open_row = s.scalars(
                select(GraphNode).where(
                    GraphNode.tenant_id == ctx.tenant_id, GraphNode.kind == kind,
                    GraphNode.natural_key == natural_key, GraphNode.valid_to.is_(None),
                )
            ).first()
            if open_row is not None:
                same = (
                    (open_row.attrs or {}) == attrs and (open_row.natural_keys or {}) == natural_keys
                    and open_row.ref_table == ref_table and open_row.ref_id == ref_id
                )
                if same:
                    return open_row.id
                open_row.valid_to = at
                s.flush()
            row = GraphNode(
                tenant_id=ctx.tenant_id, kind=kind, natural_key=natural_key, ref_table=ref_table,
                ref_id=ref_id, attrs=attrs, natural_keys=natural_keys, valid_from=at,
            )
            s.add(row)
            s.flush()
            return row.id

    def node(self, tenant_id: str, node_id: str) -> dict | None:
        with tenant_session(tenant_id, self.url) as s:
            row = s.get(GraphNode, node_id)
            return _node_dict(row) if row is not None and row.tenant_id == tenant_id else None

    def open_node(self, tenant_id: str, kind: str, natural_key: str) -> dict | None:
        with tenant_session(tenant_id, self.url) as s:
            row = s.scalars(
                select(GraphNode).where(
                    GraphNode.tenant_id == tenant_id, GraphNode.kind == kind,
                    GraphNode.natural_key == natural_key, GraphNode.valid_to.is_(None),
                )
            ).first()
            return _node_dict(row) if row else None

    def nodes(self, tenant_id: str, *, kind: str | None = None, as_of: datetime | None = None) -> list[dict]:
        with tenant_session(tenant_id, self.url) as s:
            q = select(GraphNode).where(GraphNode.tenant_id == tenant_id)
            if kind:
                q = q.where(GraphNode.kind == kind)
            q = _as_of(GraphNode, q, as_of).order_by(GraphNode.kind, GraphNode.natural_key)
            return [_node_dict(n) for n in s.scalars(q)]

    def nodes_by_ref(self, tenant_id: str, ref_table: str, ref_ids: list[str]) -> dict[str, str]:
        if not ref_ids:
            return {}
        with tenant_session(tenant_id, self.url) as s:
            q = select(GraphNode.ref_id, GraphNode.id).where(
                GraphNode.tenant_id == tenant_id, GraphNode.ref_table == ref_table,
                GraphNode.ref_id.in_(ref_ids), GraphNode.valid_to.is_(None),
            )
            return dict(s.execute(q).all())

    # -- edges -------------------------------------------------------------

    def upsert_edge(
        self, ctx: RunContext, *, src: str, dst: str, kind: str, source_system: str, provenance: str,
        confidence: float, observed_at: datetime, attrs: dict | None = None, at: datetime | None = None,
    ) -> str:
        if provenance not in PROVENANCE:
            raise ValueError(f"provenance must be one of {', '.join(PROVENANCE)}")
        if not 0 <= float(confidence) <= 1:
            raise ValueError("confidence must be between 0 and 1")
        at = at or _now()
        attrs = masking.mask_row(dict(attrs or {}))
        conf = Decimal(str(confidence)).quantize(Decimal("0.0001"))
        with tenant_session(ctx.tenant_id, self.url) as s:
            open_row = s.scalars(
                select(GraphEdge).where(
                    GraphEdge.tenant_id == ctx.tenant_id, GraphEdge.src == src, GraphEdge.dst == dst,
                    GraphEdge.kind == kind, GraphEdge.source_system == source_system, GraphEdge.valid_to.is_(None),
                )
            ).first()
            if open_row is not None:
                same = (
                    open_row.provenance == provenance and Decimal(open_row.confidence) == conf
                    and (open_row.attrs or {}) == attrs
                )
                if same:
                    return open_row.id
                open_row.valid_to = at
                s.flush()
            row = GraphEdge(
                tenant_id=ctx.tenant_id, src=src, dst=dst, kind=kind, source_system=source_system,
                observed_at=observed_at, provenance=provenance, confidence=conf, attrs=attrs, valid_from=at,
            )
            s.add(row)
            s.flush()
            return row.id

    def edges(
        self, tenant_id: str, *, src: str | None = None, dst: str | None = None,
        kind: str | None = None, as_of: datetime | None = None,
    ) -> list[dict]:
        with tenant_session(tenant_id, self.url) as s:
            q = select(GraphEdge).where(GraphEdge.tenant_id == tenant_id)
            if src:
                q = q.where(GraphEdge.src == src)
            if dst:
                q = q.where(GraphEdge.dst == dst)
            if kind:
                q = q.where(GraphEdge.kind == kind)
            q = _as_of(GraphEdge, q, as_of).order_by(GraphEdge.kind, GraphEdge.src, GraphEdge.dst)
            return [_edge_dict(e) for e in s.scalars(q)]
```

- [ ] **Step 4: Run to see it pass**

Run: `./.venv/bin/python -m pytest tests/test_trust_graph_store.py -q -p no:cacheprovider`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add invoice_processing/trust/graph/__init__.py invoice_processing/trust/graph/store.py tests/test_trust_graph_store.py
git commit -m "Add the GraphStore: time-travel nodes and edges, masked, idempotent

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: History import — readers, mapping, importer

**Files:**
- Create: `invoice_processing/trust/history/__init__.py`, `readers.py`, `mapping.py`, `importer.py`
- Modify: `invoice_processing/db/repositories.py:748` (`_ENTITY_TYPES` gains `"trust": "trust"`)
- Test: `tests/test_trust_history_import.py`

**Interfaces:**
- Produces:
  - `readers.read_table(data: bytes, filename: str) -> tuple[list[str], list[dict[str, str]]]` (raises `ValueError` with a plain sentence for unreadable files)
  - `mapping.KINDS = ("vendor", "ap_invoice", "payment", "po", "grn", "bank_line", "vendor_statement_line")`, `mapping.FIELDS: dict[str, tuple[str, ...]]`, `mapping.REQUIRED: dict[str, tuple[str, ...]]`
  - `mapping.propose(kind: str, headers: list[str], decider=None) -> dict[str, str]` (header → field; `""` = ignore)
  - `mapping.validate(kind: str, mapping: dict[str, str], headers: list[str]) -> list[str]` (problems, empty when fine)
  - `importer.ImportResult(import_id: str, status: str, rows_ok: int, rows_bad: int, errors: list[dict], duplicate_of: str | None)` frozen dataclass with `.as_dict()`
  - `importer.HistoryImporter(url=None)` with `import_file(ctx: RunContext, *, kind: str, source_name: str, filename: str, data: bytes, mapping: dict[str, str]) -> ImportResult`, `saved_mapping(tenant_id, source_name, kind) -> dict | None`, `list_imports(tenant_id) -> list[dict]`
  - `importer.parse_amount(text: str) -> Decimal` (accepts `1,00,000.50`, `1,000.50`, `₹ 100`, `(100)` as negative), `importer.parse_date(text: str) -> date` (ISO, `dd/mm/yyyy`, `dd-mm-yyyy`, Excel datetimes)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_trust_history_import.py
"""History arrives as files: whole or not at all, every bad row named."""

import io
from datetime import date
from decimal import Decimal

import openpyxl
import pytest

from invoice_processing.db import models
from invoice_processing.db.session import create_all, get_engine, tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust.decide import FakeDecider
from invoice_processing.trust.history import mapping, readers
from invoice_processing.trust.history.importer import HistoryImporter, parse_amount, parse_date

T = "tenant-acme"
CTX = RunContext(tenant_id=T, case_id="", run_id="r", actor="clerk@x.test")
CSV = b"Bill No,Supplier,Amount,Date\nINV-1,Acme,100.50,2026-01-05\nINV-2,Acme,oops,2026-01-06\nINV-3,Globex,200,06/01/2026\n"
MAP = {"Bill No": "doc_number", "Supplier": "vendor_key", "Amount": "amount", "Date": "record_date"}


@pytest.fixture
def importer(scratch_db):
    url = scratch_db("history.db")
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
    with tenant_session(T, url) as s:
        s.add(models.Tenant(id=T, name="Acme", slug="acme", status="active"))
    return HistoryImporter(url)


def test_csv_lands_whole_with_bad_rows_reported(importer):
    out = importer.import_file(CTX, kind="ap_invoice", source_name="tally", filename="ap.csv", data=CSV, mapping=MAP)
    assert (out.status, out.rows_ok, out.rows_bad) == ("partial", 2, 1)
    assert out.errors == [{"row": 3, "reason": "amount: 'oops' is not a number"}]
    with tenant_session(T, importer.url) as s:
        rows = s.query(models.HistoryRecord).order_by(models.HistoryRecord.row_number).all()
        assert [(r.doc_number, r.amount, r.record_date) for r in rows] == [
            ("INV-1", Decimal("100.5000"), date(2026, 1, 5)),
            ("INV-3", Decimal("200.0000"), date(2026, 1, 6)),
        ]
        assert rows[0].fields == {"Bill No": "INV-1", "Supplier": "Acme", "Amount": "100.50", "Date": "2026-01-05"}


def test_same_sha256_is_noop_even_with_a_new_filename(importer):
    first = importer.import_file(CTX, kind="ap_invoice", source_name="tally", filename="a.csv", data=CSV, mapping=MAP)
    again = importer.import_file(CTX, kind="ap_invoice", source_name="tally", filename="b.csv", data=CSV, mapping=MAP)
    assert again.duplicate_of == first.import_id and again.rows_ok == 0
    with tenant_session(T, importer.url) as s:
        assert s.query(models.HistoryRecord).count() == 2
        assert s.query(models.TrustHistoryImport).count() == 1


def test_missing_required_field_refuses_the_whole_file(importer):
    with pytest.raises(ValueError, match="amount"):
        importer.import_file(CTX, kind="ap_invoice", source_name="t", filename="a.csv", data=CSV, mapping={"Bill No": "doc_number"})
    with tenant_session(T, importer.url) as s:
        assert s.query(models.TrustHistoryImport).count() == 0


def test_xlsx_reads_dates_and_numbers(importer):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Bill No", "Supplier", "Amount", "Date"])
    ws.append(["INV-9", "Acme", 100.5, date(2026, 1, 5)])
    buf = io.BytesIO()
    wb.save(buf)
    out = importer.import_file(CTX, kind="ap_invoice", source_name="x", filename="a.xlsx", data=buf.getvalue(), mapping=MAP)
    assert out.rows_ok == 1
    with tenant_session(T, importer.url) as s:
        r = s.query(models.HistoryRecord).one()
        assert (r.amount, r.record_date) == (Decimal("100.5000"), date(2026, 1, 5))


def test_grouped_number_text_is_parsed_or_refused():
    assert parse_amount("1,00,000.50") == Decimal("100000.50")
    assert parse_amount("₹ 1,000") == Decimal("1000")
    assert parse_amount("(250.00)") == Decimal("-250.00")
    with pytest.raises(ValueError):
        parse_amount("ten")


def test_parse_date_accepts_indian_and_iso_forms():
    assert parse_date("06/01/2026") == date(2026, 1, 6)
    assert parse_date("06-01-2026") == date(2026, 1, 6)
    assert parse_date("2026-01-06T10:00:00") == date(2026, 1, 6)
    with pytest.raises(ValueError):
        parse_date("someday")


def test_read_table_refuses_unknown_extension():
    with pytest.raises(ValueError, match=".csv or .xlsx"):
        readers.read_table(b"x", "a.xls")


def test_propose_uses_synonyms_then_decider_fallback():
    proposed = mapping.propose("ap_invoice", ["Bill No", "Supplier", "Zorg"], decider=FakeDecider({}, fail=True))
    assert proposed == {"Bill No": "doc_number", "Supplier": "vendor_key", "Zorg": ""}
    with_model = mapping.propose("ap_invoice", ["Zorg"], decider=FakeDecider({"Zorg": "amount"}))
    assert with_model == {"Zorg": "amount"}


def test_saved_mapping_comes_from_the_last_import(importer):
    importer.import_file(CTX, kind="ap_invoice", source_name="tally", filename="a.csv", data=CSV, mapping=MAP)
    assert importer.saved_mapping(T, "tally", "ap_invoice") == MAP
    assert importer.saved_mapping(T, "zoho", "ap_invoice") is None
    assert importer.saved_mapping("tenant-globex", "tally", "ap_invoice") is None
```

- [ ] **Step 2: Run to see it fail**

Run: `./.venv/bin/python -m pytest tests/test_trust_history_import.py -q -p no:cacheprovider`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement the readers**

`invoice_processing/trust/history/__init__.py`: docstring `"""History import: files in, normalised records out (spec §4.1)."""`.

```python
# invoice_processing/trust/history/readers.py
"""CSV and XLSX to headers plus rows of strings. Nothing else is interpreted here."""

from __future__ import annotations

import csv
import io
from datetime import date, datetime


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def read_table(data: bytes, filename: str) -> tuple[list[str], list[dict[str, str]]]:
    name = (filename or "").lower()
    if name.endswith(".csv"):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValueError("The file is not UTF-8 text.") from None
        reader = csv.DictReader(io.StringIO(text))
        headers = [h.strip() for h in (reader.fieldnames or []) if h and h.strip()]
        rows = [{h: _cell(r.get(h)) for h in headers} for r in reader]
    elif name.endswith(".xlsx"):
        import openpyxl  # noqa: PLC0415 - heavy, and only for spreadsheets

        try:
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception:
            raise ValueError("The file is not a readable .xlsx workbook.") from None
        try:
            ws = wb.worksheets[0]
            lines = [list(r) for r in ws.iter_rows(values_only=True)]
        finally:
            wb.close()
        lines = [line for line in lines if any(_cell(c) for c in line)]
        if not lines:
            return [], []
        headers = [_cell(c) for c in lines[0]]
        rows = [{h: _cell(c) for h, c in zip(headers, line, strict=False) if h} for line in lines[1:]]
        headers = [h for h in headers if h]
    else:
        raise ValueError("Upload a .csv or .xlsx file.")
    if not headers:
        raise ValueError("The file has no header row.")
    return headers, rows
```

- [ ] **Step 4: Implement the mapping catalogue and proposal**

```python
# invoice_processing/trust/history/mapping.py
"""Which column means what, per kind of history file.

Synonyms cover the common exports (Tally, Zoho, bank CSVs); anything left
over is one `decide()` question per header, and a human confirms the whole
mapping before any row is stored. The mapping is saved per tenant per source
name, so a Tally export is a saved mapping, not code (spec §4.1).
"""

from __future__ import annotations

import re

from ..decide import Question, decide

KINDS = ("vendor", "ap_invoice", "payment", "po", "grn", "bank_line", "vendor_statement_line")

FIELDS: dict[str, tuple[str, ...]] = {
    "vendor": ("external_id", "vendor_key", "gstin", "pan", "abn", "bank_account", "bank_code", "email_domain"),
    "ap_invoice": ("external_id", "doc_number", "vendor_key", "amount", "currency", "record_date", "po_number"),
    "payment": ("external_id", "doc_number", "vendor_key", "amount", "currency", "record_date", "bank_reference"),
    "po": ("external_id", "doc_number", "vendor_key", "amount", "currency", "record_date", "line_number", "quantity", "unit_price"),
    "grn": ("external_id", "doc_number", "po_number", "record_date", "line_number", "quantity"),
    "bank_line": ("external_id", "record_date", "amount", "currency", "counterparty", "bank_reference"),
    "vendor_statement_line": ("external_id", "vendor_key", "doc_number", "amount", "currency", "record_date"),
}

REQUIRED: dict[str, tuple[str, ...]] = {
    "vendor": ("vendor_key",),
    "ap_invoice": ("doc_number", "vendor_key", "amount"),
    "payment": ("amount", "record_date"),
    "po": ("doc_number", "vendor_key"),
    "grn": ("doc_number", "po_number"),
    "bank_line": ("amount", "record_date"),
    "vendor_statement_line": ("vendor_key", "doc_number", "amount"),
}

_SYNONYMS: dict[str, tuple[str, ...]] = {
    "doc_number": ("bill no", "bill number", "invoice no", "invoice number", "inv no", "voucher no", "document no", "po no", "po number", "grn no", "statement ref"),
    "external_id": ("id", "record id", "guid", "master id"),
    "vendor_key": ("supplier", "vendor", "party", "party name", "vendor name", "supplier name", "ledger"),
    "amount": ("amount", "total", "gross", "value", "net amount", "invoice amount", "debit", "credit"),
    "currency": ("currency", "ccy", "cur"),
    "record_date": ("date", "invoice date", "bill date", "voucher date", "posting date", "payment date", "txn date", "value date"),
    "po_number": ("po", "po ref", "purchase order", "order no", "order number"),
    "gstin": ("gstin", "gst no", "gst number"),
    "pan": ("pan",),
    "abn": ("abn",),
    "bank_account": ("account no", "account number", "bank account", "a/c no"),
    "bank_code": ("ifsc", "bsb", "swift", "bank code"),
    "email_domain": ("email", "email domain", "domain"),
    "bank_reference": ("utr", "reference", "ref no", "transaction id"),
    "counterparty": ("counterparty", "narration", "description", "beneficiary"),
    "line_number": ("line", "line no", "sr no", "s no"),
    "quantity": ("qty", "quantity"),
    "unit_price": ("rate", "unit price", "price"),
}


def _norm(header: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", header.lower()).strip()


def _by_synonym(kind: str, header: str) -> str:
    h = _norm(header).replace("_", " ")
    for field in FIELDS[kind]:
        if h == field.replace("_", " ") or h in _SYNONYMS.get(field, ()):
            return field
    return ""


def propose(kind: str, headers: list[str], decider=None) -> dict[str, str]:
    if kind not in KINDS:
        raise ValueError(f"No such history kind: {kind}. Kinds: {', '.join(KINDS)}.")
    proposed = {h: _by_synonym(kind, h) for h in headers}
    taken = {f for f in proposed.values() if f}
    unknown = [h for h, f in proposed.items() if not f]
    if unknown:
        options = tuple(f for f in FIELDS[kind] if f not in taken) + ("",)
        questions = [
            Question(h, "choice", f"Which {kind} field is the column {h!r}? Answer '' if none.", fallback="", options=options)
            for h in unknown
        ]
        for answer in decide({"kind": kind, "headers": headers}, questions, decider):
            proposed[answer.id] = answer.value
    return proposed


def validate(kind: str, mapping: dict[str, str], headers: list[str]) -> list[str]:
    problems = []
    if kind not in KINDS:
        return [f"No such history kind: {kind}."]
    for header, field in mapping.items():
        if header not in headers:
            problems.append(f"mapped column {header!r} is not in the file")
        if field and field not in FIELDS[kind]:
            problems.append(f"{field!r} is not a {kind} field")
    used = [f for f in mapping.values() if f]
    for field in sorted(set(used)):
        if used.count(field) > 1:
            problems.append(f"{field} is mapped from more than one column")
    for field in REQUIRED[kind]:
        if field not in used:
            problems.append(f"required field {field} is not mapped")
    return problems
```

- [ ] **Step 5: Implement the importer**

```python
# invoice_processing/trust/history/importer.py
"""One file, one transaction: it lands whole (minus named bad rows) or not at all."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select

from ...db.models import HistoryRecord, TrustHistoryImport
from ...db.repositories import audit_event_row
from ...db.session import tenant_session
from ...ports import RunContext
from . import mapping as mp
from .readers import read_table

_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")
_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%Y/%m/%d", "%d %b %Y", "%d-%b-%Y")


@dataclass(frozen=True)
class ImportResult:
    import_id: str
    status: str
    rows_ok: int
    rows_bad: int
    errors: list[dict]
    duplicate_of: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def parse_amount(text: str) -> Decimal:
    raw = str(text).strip()
    negative = raw.startswith("(") and raw.endswith(")")
    cleaned = re.sub(r"[^\d.\-]", "", raw)  # drops currency symbols and grouping commas
    if not _NUMBER.match(cleaned):
        raise ValueError(f"{raw!r} is not a number")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(f"{raw!r} is not a number") from None
    return -value if negative else value


def parse_date(text: str) -> date:
    raw = str(text).strip()
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"{raw!r} is not a date")


def _normalise(fields: dict[str, str]) -> dict:
    """Typed columns from the mapped fields; raises ValueError naming the field."""
    out: dict = {}
    if "amount" in fields:
        try:
            out["amount"] = parse_amount(fields["amount"])
        except ValueError as exc:
            raise ValueError(f"amount: {exc}") from None
    if "record_date" in fields and fields["record_date"]:
        try:
            out["record_date"] = parse_date(fields["record_date"])
        except ValueError as exc:
            raise ValueError(f"record_date: {exc}") from None
    for key in ("external_id", "doc_number", "vendor_key", "currency"):
        if fields.get(key):
            out[key] = fields[key].strip()[: {"vendor_key": 512, "currency": 8}.get(key, 128)]
    return out


class HistoryImporter:
    def __init__(self, url: str | None = None):
        self.url = url

    def import_file(
        self, ctx: RunContext, *, kind: str, source_name: str, filename: str, data: bytes, mapping: dict[str, str]
    ) -> ImportResult:
        headers, rows = read_table(data, filename)
        problems = mp.validate(kind, mapping, headers)
        if problems:
            raise ValueError("; ".join(problems))
        sha = hashlib.sha256(data).hexdigest()
        with tenant_session(ctx.tenant_id, self.url) as s:
            existing = s.scalars(
                select(TrustHistoryImport).where(
                    TrustHistoryImport.tenant_id == ctx.tenant_id, TrustHistoryImport.file_sha256 == sha
                )
            ).first()
            if existing is not None:
                return ImportResult(existing.id, existing.status, 0, 0, [], duplicate_of=existing.id)

            run = TrustHistoryImport(
                tenant_id=ctx.tenant_id, source_name=source_name, kind=kind, file_sha256=sha,
                filename=filename, mapping=dict(mapping), created_by=ctx.actor or None,
            )
            s.add(run)
            s.flush()
            errors: list[dict] = []
            ok = 0
            for index, raw in enumerate(rows, start=2):  # the header is row 1
                mapped = {field: raw.get(header, "") for header, field in mapping.items() if field}
                try:
                    typed = _normalise(mapped)
                    for field in mp.REQUIRED[kind]:
                        if field == "amount":
                            if "amount" not in typed:
                                raise ValueError("amount: missing")
                        elif not typed.get(field):
                            raise ValueError(f"{field}: missing")
                except ValueError as exc:
                    errors.append({"row": index, "reason": str(exc)})
                    continue
                s.add(
                    HistoryRecord(
                        tenant_id=ctx.tenant_id, import_id=run.id, kind=kind, row_number=index,
                        external_id=typed.get("external_id"), fields=raw, amount=typed.get("amount"),
                        currency=typed.get("currency"), record_date=typed.get("record_date"),
                        vendor_key=typed.get("vendor_key"), doc_number=typed.get("doc_number"),
                    )
                )
                ok += 1
            run.rows_ok, run.rows_bad, run.errors = ok, len(errors), errors
            run.status = "partial" if errors else "complete"
            s.add(
                audit_event_row(
                    ctx, "trust_history_imported",
                    {"import_id": run.id, "kind": kind, "source_name": source_name, "rows_ok": ok, "rows_bad": len(errors)},
                )
            )
            return ImportResult(run.id, run.status, ok, len(errors), errors)

    def saved_mapping(self, tenant_id: str, source_name: str, kind: str) -> dict | None:
        with tenant_session(tenant_id, self.url) as s:
            row = s.scalars(
                select(TrustHistoryImport)
                .where(
                    TrustHistoryImport.tenant_id == tenant_id, TrustHistoryImport.source_name == source_name,
                    TrustHistoryImport.kind == kind,
                )
                .order_by(TrustHistoryImport.created_at.desc())
            ).first()
            return dict(row.mapping) if row else None

    def list_imports(self, tenant_id: str) -> list[dict]:
        with tenant_session(tenant_id, self.url) as s:
            rows = s.scalars(
                select(TrustHistoryImport)
                .where(TrustHistoryImport.tenant_id == tenant_id)
                .order_by(TrustHistoryImport.created_at.desc())
            )
            return [
                {
                    "id": r.id, "source_name": r.source_name, "kind": r.kind, "filename": r.filename,
                    "status": r.status, "rows_ok": r.rows_ok, "rows_bad": r.rows_bad, "errors": list(r.errors or []),
                    "created_at": r.created_at.isoformat() if r.created_at else None, "created_by": r.created_by,
                }
                for r in rows
            ]
```

Add to `repositories._ENTITY_TYPES` (line ~748): `"trust": "trust",` so every `trust_*` event resolves (the prefix match).

- [ ] **Step 6: Run to see it pass**

Run: `./.venv/bin/python -m pytest tests/test_trust_history_import.py tests/test_db_repositories.py -q -p no:cacheprovider`
Expected: all pass. Note the test for `test_csv_lands_whole_with_bad_rows_reported` expects the error text `amount: 'oops' is not a number`; `parse_amount` raises `'oops' is not a number` and `_normalise` prefixes `amount: `.

- [ ] **Step 7: Commit**

```bash
git add invoice_processing/trust/history invoice_processing/db/repositories.py tests/test_trust_history_import.py
git commit -m "Import history files: CSV or XLSX, mapped columns, bad rows named, same bytes a no-op

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: History API routes, service wiring, CLI

**Files:**
- Create: `invoice_processing/api/trust_routes.py`, `invoice_processing/trust/history/__main__.py`
- Modify: `invoice_processing/api/service.py` (two getters next to `get_purchase_orders` ~549; import + include router ~2009-2035)
- Modify: `tests/conftest.py:76-93` (`resolve_adapters_afresh` tuple: add `"_graph_store", "_history_importer"`)
- Modify: `tests/test_tenant_isolation_api.py` (import `trust_routes`; add to the module tuple in `test_no_endpoint_reads_a_tenant_from_the_request`; `PROBED` + a probe class)
- Test: `tests/test_trust_routes.py`

**Interfaces:**
- Produces routes (all `require_role("clerk")`):
  - `POST /api/trust/history/mapping` multipart `file`, form `kind`, `source_name` → 200 `{"headers": [...], "proposed": {...}, "saved": {...} | null}`
  - `POST /api/trust/history/import` multipart `file`, form `kind`, `source_name`, `mapping` (JSON object as a string) → 201 `ImportResult.as_dict()`; 200 when `duplicate_of`; 400 on mapping problems or when `rows_ok == 0`; 413 over 5 MB; 503 `"History import needs a database. Configure DATABASE_URL."`
  - `GET /api/trust/history/imports` → 200 list
- `service.get_graph_store() -> GraphStore | None`, `service.get_history_importer() -> HistoryImporter | None` (memoised as `_graph_store`, `_history_importer`, `None` without a database)
- `python -m invoice_processing.trust.history --tenant <id> --kind ap_invoice --source tally --file ap.csv [--mapping '{"Bill No":"doc_number",…}'] [--propose]`

- [ ] **Step 1: Write the failing route tests**

```python
# tests/test_trust_routes.py
"""The history upload routes: mapping proposal, import, listing."""

import json

import pytest
from fastapi.testclient import TestClient

from invoice_processing.adapters import JsonlAuditSink
from invoice_processing.api import auth, service
from invoice_processing.api.service import app
from invoice_processing.db import models
from invoice_processing.db.session import create_all, get_engine, tenant_session
from invoice_processing.trust.history.importer import HistoryImporter

TENANT = "tenant-acme"
CSV = b"Bill No,Supplier,Amount,Date\nINV-1,Acme,100.50,2026-01-05\nINV-2,Acme,oops,2026-01-06\n"
MAP = {"Bill No": "doc_number", "Supplier": "vendor_key", "Amount": "amount", "Date": "record_date"}


class FakeVerifier:
    def verify(self, token):
        if token not in ("viewer", "clerk", "approver", "admin"):
            raise auth.AuthError("Invalid token")
        return {"sub": f"u-{token}", "email": f"{token}@x.test", "email_verified": True, "tenant_id": TENANT, "role": token}


@pytest.fixture
def env(tmp_path, monkeypatch, scratch_db):
    url = scratch_db("trust_routes.db")
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
    with tenant_session(TENANT, url) as s:
        s.add(models.Tenant(id=TENANT, name="Acme", slug="acme", status="active"))
    monkeypatch.setattr(service, "_history_importer", HistoryImporter(url))
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr(service, "_audit", JsonlAuditSink(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(auth, "_verifier", FakeVerifier())
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    return lambda token: TestClient(app, headers={"Authorization": f"Bearer {token}"})


def _post(client, path, data, **form):
    return client.post(path, files={"file": ("ap.csv", data, "text/csv")}, data=form)


def test_mapping_route_proposes_and_returns_saved(env, monkeypatch):
    from invoice_processing.trust.history import mapping

    monkeypatch.setattr(mapping, "decide", lambda state, questions, decider=None: [])
    r = _post(env("clerk"), "/api/trust/history/mapping", CSV, kind="ap_invoice", source_name="tally")
    assert r.status_code == 200
    assert r.json()["proposed"] == MAP and r.json()["saved"] is None


def test_import_route_returns_201_with_row_errors(env):
    r = _post(env("clerk"), "/api/trust/history/import", CSV, kind="ap_invoice", source_name="tally", mapping=json.dumps(MAP))
    assert r.status_code == 201
    assert r.json()["rows_ok"] == 1 and r.json()["errors"][0]["row"] == 3
    again = _post(env("clerk"), "/api/trust/history/import", CSV, kind="ap_invoice", source_name="tally", mapping=json.dumps(MAP))
    assert again.status_code == 200 and again.json()["duplicate_of"] == r.json()["import_id"]
    listed = env("viewer").get("/api/trust/history/imports")
    assert listed.status_code == 200 and listed.json()[0]["rows_bad"] == 1


def test_viewer_cannot_import(env):
    r = _post(env("viewer"), "/api/trust/history/import", CSV, kind="ap_invoice", source_name="t", mapping=json.dumps(MAP))
    assert r.status_code == 403


def test_bad_mapping_is_400_in_words(env):
    r = _post(env("clerk"), "/api/trust/history/import", CSV, kind="ap_invoice", source_name="t", mapping=json.dumps({"Bill No": "doc_number"}))
    assert r.status_code == 400 and "required field amount is not mapped" in r.json()["detail"]


def test_import_without_database_is_503(env, monkeypatch):
    monkeypatch.setattr(service, "_history_importer", None)
    monkeypatch.setattr(service, "_database_configured", lambda: False)
    r = _post(env("clerk"), "/api/trust/history/import", CSV, kind="ap_invoice", source_name="t", mapping=json.dumps(MAP))
    assert r.status_code == 503 and "needs a database" in r.json()["detail"]
```

- [ ] **Step 2: Run to see it fail**

Run: `./.venv/bin/python -m pytest tests/test_trust_routes.py -q -p no:cacheprovider`
Expected: FAIL (404s: routes do not exist).

- [ ] **Step 3: Add the service getters**

In `invoice_processing/api/service.py`, next to the other `_x = None` module globals add `_graph_store = None` and `_history_importer = None`, and next to `get_purchase_orders`:

```python
def get_graph_store():
    """The v2 Evidence Graph. `None` without a database."""
    global _graph_store
    if _graph_store is None and _database_configured() and _table_exists("graph_nodes", "Evidence graph", raise_on_error=True):
        from ..trust.graph.store import GraphStore  # noqa: PLC0415

        _graph_store = GraphStore()
    return _graph_store


def get_history_importer():
    """History file imports (v2). `None` without a database."""
    global _history_importer
    if (
        _history_importer is None
        and _database_configured()
        and _table_exists("trust_history_imports", "History imports", raise_on_error=True)
    ):
        from ..trust.history.importer import HistoryImporter  # noqa: PLC0415

        _history_importer = HistoryImporter()
    return _history_importer
```

In `tests/conftest.py` add `"_graph_store", "_history_importer",` to the `resolve_adapters_afresh` tuple.

- [ ] **Step 4: Write the routes**

```python
# invoice_processing/api/trust_routes.py
"""History uploads for the v2 trust layer.

A file is stored whole or not at all; every bad row is named with its row
number (the header is row 1). A re-upload of the same bytes is a no-op.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from starlette.concurrency import run_in_threadpool

from ..trust.history import mapping as mp
from ..trust.history.readers import read_table
from .auth import Principal, require_role

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_IMPORT_BYTES = 5 * 1024 * 1024
TOO_BIG = "Import is over 5 MB."
NEEDS_DB = "History import needs a database. Configure DATABASE_URL."


def _importer():
    from . import service  # noqa: PLC0415

    try:
        importer = service.get_history_importer()
    except Exception:
        logger.exception("History imports could not be reached")
        raise HTTPException(503, "History imports are unavailable.") from None
    if importer is None:
        raise HTTPException(503, NEEDS_DB)
    return importer


def _audit():
    from . import service  # noqa: PLC0415

    return service.get_audit()


async def _read(request: Request) -> tuple[bytes, str, dict]:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_IMPORT_BYTES + 64 * 1024:
        raise HTTPException(413, TOO_BIG)
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise HTTPException(400, "Send multipart form data with the file as 'file'.")
    form = await request.form()
    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        raise HTTPException(400, "Attach the file as 'file'.")
    data = await upload.read(MAX_IMPORT_BYTES + 1)
    if len(data) > MAX_IMPORT_BYTES:
        raise HTTPException(413, TOO_BIG)
    fields = {k: str(v) for k, v in form.items() if k != "file"}
    kind = fields.get("kind", "")
    if kind not in mp.KINDS:
        raise HTTPException(400, f"kind must be one of {', '.join(mp.KINDS)}.")
    if not fields.get("source_name", "").strip():
        raise HTTPException(400, "source_name is required.")
    return data, upload.filename or "upload", fields


@router.post("/api/trust/history/mapping")
async def propose_mapping(request: Request, principal: Annotated[Principal, Depends(require_role("clerk"))]):
    importer = _importer()
    data, filename, fields = await _read(request)
    try:
        headers, _rows = read_table(data, filename)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    kind, source = fields["kind"], fields["source_name"].strip()
    saved = await run_in_threadpool(importer.saved_mapping, principal.tenant_id, source, kind)
    proposed = await run_in_threadpool(mp.propose, kind, headers)
    return {"headers": headers, "proposed": proposed, "saved": saved}


@router.post("/api/trust/history/import", status_code=201)
async def import_history(request: Request, principal: Annotated[Principal, Depends(require_role("clerk"))]):
    importer = _importer()
    data, filename, fields = await _read(request)
    try:
        mapping = json.loads(fields.get("mapping") or "{}")
    except ValueError:
        raise HTTPException(400, "mapping must be a JSON object.") from None
    if not isinstance(mapping, dict) or not all(isinstance(v, str) for v in mapping.values()):
        raise HTTPException(400, "mapping must be a JSON object of column to field.")
    try:
        out = await run_in_threadpool(
            importer.import_file, principal.context(), kind=fields["kind"], source_name=fields["source_name"].strip(),
            filename=filename, data=data, mapping=mapping,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    body = out.as_dict()
    if out.duplicate_of:
        from fastapi.responses import JSONResponse  # noqa: PLC0415

        return JSONResponse(body, status_code=200)
    if out.rows_ok == 0:
        raise HTTPException(400, "; ".join(f"row {e['row']}: {e['reason']}" for e in out.errors) or "The file has no rows.")
    _audit().emit(principal.context(), "trust_history_import_accepted", body)
    _queue_sync(principal.tenant_id, principal.context().run_id)
    return body


def _queue_sync(tenant_id: str, request_id: str) -> None:
    """Best effort, like purchase-order re-matching: a queue outage must not refuse the upload."""
    try:
        from ..worker.runner import enqueue_trust_graph_sync  # noqa: PLC0415
        from . import service  # noqa: PLC0415

        enqueue_trust_graph_sync(service.get_queue(), tenant_id, request_id)
    except Exception:
        logger.warning("Graph sync could not be queued after a history import", exc_info=True)


@router.get("/api/trust/history/imports")
def list_imports(principal: Annotated[Principal, Depends(require_role("viewer"))]):
    return _importer().list_imports(principal.tenant_id)
```

`enqueue_trust_graph_sync` does not exist until Task 11; until then `_queue_sync` logs a warning and the route still works (that is the point of best effort). Task 11 removes the warning by adding the function.

In `api/service.py` add `from .trust_routes import router as trust_router  # noqa: E402` beside the other router imports and `app.include_router(trust_router)` beside the other includes.

- [ ] **Step 5: Register the routes in the isolation test**

In `tests/test_tenant_isolation_api.py`: import `trust_routes` with the other route modules (lines 36-48) and add it to the module tuple in `test_no_endpoint_reads_a_tenant_from_the_request`; add the three paths to `PROBED`; add a probe:

```python
class TestTrustHistoryIsolation:
    @pytest.fixture
    def history_db(self, monkeypatch, scratch_db):
        from invoice_processing.trust.history.importer import HistoryImporter

        url = scratch_db("iso-history.db")
        Base.metadata.drop_all(get_engine(url))
        create_all(url)
        for tid, slug in ((ACME, "acme"), (GLOBEX, "globex")):
            with db_session(tid, url) as session:
                session.add(Tenant(id=tid, name=slug, slug=slug, status="active"))
        monkeypatch.setattr(service, "_history_importer", HistoryImporter(url))
        monkeypatch.setenv("DATABASE_URL", url)
        return url

    def test_imports_stay_with_their_tenant(self, clients, history_db):
        csv = b"Bill No,Supplier,Amount\nINV-1,Acme,10\n"
        mapping = '{"Bill No":"doc_number","Supplier":"vendor_key","Amount":"amount"}'
        r = clients(ACME, "clerk").post(
            "/api/trust/history/import", files={"file": ("a.csv", csv, "text/csv")},
            data={"kind": "ap_invoice", "source_name": "t", "mapping": mapping},
        )
        assert r.status_code == 201
        assert clients(GLOBEX).get("/api/trust/history/imports").json() == []
        assert clients(ACME).get("/api/trust/history/imports").json()[0]["rows_ok"] == 1
        probe = clients(GLOBEX, "clerk").post(
            "/api/trust/history/mapping", files={"file": ("a.csv", csv, "text/csv")},
            data={"kind": "ap_invoice", "source_name": "t"},
        )
        assert probe.status_code == 200 and probe.json()["saved"] is None
```

(`clients`, `ACME`, `GLOBEX`, `db_session`, `Base`, `Tenant`, `create_all`, `get_engine` are the fixture and names that file already uses; copy the `vendor_db` fixture's shape at lines 283-294.)

- [ ] **Step 6: Write the CLI**

```python
# invoice_processing/trust/history/__main__.py
"""Import a history file from the shell.

    python -m invoice_processing.trust.history --tenant <id> --kind ap_invoice \
        --source tally --file ap.csv --propose
    python -m invoice_processing.trust.history --tenant <id> --kind ap_invoice \
        --source tally --file ap.csv --mapping '{"Bill No": "doc_number", ...}'
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ...ports import RunContext
from . import mapping as mp
from .importer import HistoryImporter
from .readers import read_table


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="trust-history")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--kind", required=True, choices=mp.KINDS)
    parser.add_argument("--source", required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--mapping", help="JSON object column -> field")
    parser.add_argument("--propose", action="store_true", help="print a proposed mapping and stop")
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args(argv)

    data = Path(args.file).read_bytes()
    importer = HistoryImporter(args.database_url)
    if args.propose or not args.mapping:
        headers, _ = read_table(data, args.file)
        print(json.dumps({"headers": headers, "proposed": mp.propose(args.kind, headers),
                          "saved": importer.saved_mapping(args.tenant, args.source, args.kind)}, indent=2))
        return 0
    ctx = RunContext(tenant_id=args.tenant, case_id="", run_id="cli", actor="cli")
    try:
        out = importer.import_file(ctx, kind=args.kind, source_name=args.source, filename=Path(args.file).name,
                                   data=data, mapping=json.loads(args.mapping))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(out.as_dict(), indent=2))
    return 0 if out.rows_ok or out.duplicate_of else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 7: Run the route, isolation and CLI checks**

Run: `./.venv/bin/python -m pytest tests/test_trust_routes.py tests/test_tenant_isolation_api.py -q -p no:cacheprovider`
Expected: all pass (including `test_every_route_is_covered`).
Run: `./.venv/bin/python -m invoice_processing.trust.history --help`
Expected: usage text, exit 0.

- [ ] **Step 8: Commit**

```bash
git add invoice_processing/api/trust_routes.py invoice_processing/api/service.py invoice_processing/trust/history/__main__.py tests/conftest.py tests/test_tenant_isolation_api.py tests/test_trust_routes.py
git commit -m "Expose history import over the API and the shell

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Findings v2 — lifecycle, citations, dedupe

**Files:**
- Create: `invoice_processing/trust/findings/__init__.py`, `model.py`, `store.py`
- Test: `tests/test_trust_findings.py`

**Interfaces:**
- Produces:
  - `model.STATUSES`, `model.TRANSITIONS: dict[str, frozenset[str]]`, `model.DECIDED: frozenset[str]`, `model.REASON_REQUIRED = frozenset({"rejected", "written_off"})`, `model.can_transition(a: str, b: str) -> bool`
  - `model.Citation(node_id: str, excerpt: str | None = None)`, `model.FindingSpec(type, transaction_node, dedupe_key, impact: Decimal | None, currency, confidence: float | None, engine_version, rule_version, model_version, summary, details: dict, citations: tuple[Citation, ...])`
  - `model.TYPES: dict[str, str]`, `model.register_type(code: str, description: str) -> None` (Wave 0 registers `"BENCH-IMPORT"`)
  - `model.IllegalTransition(ValueError)`
  - `store.FindingStore(url=None)` with `upsert(ctx, spec) -> tuple[str, str]` (`(id, "created"|"refreshed"|"untouched")`), `transition(ctx, finding_id, to_status, *, actor, reason=None, amount=None) -> dict`, `get(tenant_id, finding_id) -> dict | None`, `list(tenant_id, *, status=None, type=None) -> list[dict]`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_trust_findings.py
"""Findings v2: cited, versioned, deduplicated, with a money ledger of every step."""

from decimal import Decimal

import pytest

from invoice_processing.db import models
from invoice_processing.db.session import create_all, get_engine, tenant_session
from invoice_processing.ports import RunContext, StatusChanged
from invoice_processing.trust.findings.model import Citation, FindingSpec, IllegalTransition, can_transition
from invoice_processing.trust.findings.store import FindingStore
from invoice_processing.trust.graph.store import GraphStore

T = "tenant-acme"
CTX = RunContext(tenant_id=T, case_id="", run_id="r", actor="engine")
HUMAN = RunContext(tenant_id=T, case_id="", run_id="r2", actor="controller@x.test")


@pytest.fixture
def env(scratch_db):
    url = scratch_db("findings.db")
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
    with tenant_session(T, url) as s:
        s.add(models.Tenant(id=T, name="Acme", slug="acme", status="active"))
    graph = GraphStore(url)
    node = graph.upsert_node(CTX, kind="invoice", natural_key="i1")
    return url, FindingStore(url), node


def spec(node, impact="100.00", **over):
    base = dict(
        type="BENCH-IMPORT", transaction_node=node, dedupe_key="BENCH-IMPORT:i1", impact=Decimal(impact), currency="INR",
        confidence=0.9, engine_version="0.1", rule_version="r1", model_version="fake", summary="imported",
        details={}, citations=(Citation(node, "row 2"),),
    )
    base.update(over)
    return FindingSpec(**base)


def test_upsert_without_citation_is_refused(env):
    url, store, node = env
    with pytest.raises(ValueError, match="citation"):
        store.upsert(CTX, spec(node, citations=()))
    with tenant_session(T, url) as s:
        assert s.query(models.TrustFinding).count() == 0


def test_upsert_creates_then_refreshes_by_dedupe_key(env):
    _, store, node = env
    fid, how = store.upsert(CTX, spec(node))
    assert how == "created"
    fid2, how2 = store.upsert(CTX, spec(node, impact="120.00"))
    assert (fid2, how2) == (fid, "refreshed")
    got = store.get(T, fid)
    assert got["impact"] == "120.0000" and got["status"] == "detected"
    assert [e["to_status"] for e in got["events"]] == ["detected"]


def test_rerun_refreshes_but_never_reopens_a_decided_finding(env):
    _, store, node = env
    fid, _ = store.upsert(CTX, spec(node))
    store.transition(HUMAN, fid, "triaged", actor="controller@x.test")
    store.transition(HUMAN, fid, "accepted", actor="controller@x.test")
    assert store.upsert(CTX, spec(node, impact="999.00")) == (fid, "untouched")
    assert store.get(T, fid)["impact"] == "100.0000"


def test_illegal_transition_is_refused_and_rejection_needs_reason(env):
    _, store, node = env
    fid, _ = store.upsert(CTX, spec(node))
    with pytest.raises(IllegalTransition):
        store.transition(HUMAN, fid, "claimed", actor="x")
    store.transition(HUMAN, fid, "triaged", actor="x")
    with pytest.raises(ValueError, match="reason"):
        store.transition(HUMAN, fid, "rejected", actor="x")
    assert not can_transition("recovered", "detected")


def test_transition_writes_event_and_audit_rows_atomically(env):
    url, store, node = env
    fid, _ = store.upsert(CTX, spec(node))
    out = store.transition(HUMAN, fid, "triaged", actor="controller@x.test", amount=Decimal("100.00"))
    assert out["status"] == "triaged" and out["previous"] == "detected"
    with tenant_session(T, url) as s:
        events = s.query(models.TrustFindingEvent).order_by(models.TrustFindingEvent.created_at).all()
        assert [(e.from_status, e.to_status) for e in events] == [(None, "detected"), ("detected", "triaged")]
        audit = s.query(models.AuditEvent).filter_by(action="trust_finding_triaged").one()
        assert audit.entity_type == "trust" and audit.entity_id == fid


def test_concurrent_change_is_reported_not_overwritten(env):
    _, store, node = env
    fid, _ = store.upsert(CTX, spec(node))
    store.transition(HUMAN, fid, "triaged", actor="a")
    with pytest.raises(StatusChanged):
        store.transition(HUMAN, fid, "triaged", actor="b", expect="detected")


def test_list_filters_by_status_and_type(env):
    _, store, node = env
    store.upsert(CTX, spec(node))
    assert [f["type"] for f in store.list(T, status="detected", type="BENCH-IMPORT")] == ["BENCH-IMPORT"]
    assert store.list("tenant-globex") == []
```

- [ ] **Step 2: Run to see it fail**

Run: `./.venv/bin/python -m pytest tests/test_trust_findings.py -q -p no:cacheprovider`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement the model**

`invoice_processing/trust/findings/__init__.py`: docstring `"""Findings v2: quantified, cited claims with a lifecycle (PRD FR-20.7)."""`.

```python
# invoice_processing/trust/findings/model.py
"""What a finding is and which status changes are legal."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

STATUSES = (
    "detected", "triaged", "accepted", "rejected", "needs_vendor_input",
    "claimed", "recovered", "prevented", "written_off",
)
TRANSITIONS: dict[str, frozenset[str]] = {
    "detected": frozenset({"triaged"}),
    "triaged": frozenset({"accepted", "rejected", "needs_vendor_input"}),
    "needs_vendor_input": frozenset({"accepted", "rejected"}),
    "accepted": frozenset({"claimed", "prevented"}),
    "claimed": frozenset({"recovered", "prevented", "written_off"}),
    "rejected": frozenset(), "recovered": frozenset(), "prevented": frozenset(), "written_off": frozenset(),
}
# Once a human has ruled, an engine re-run may not touch the finding.
DECIDED = frozenset({"accepted", "rejected", "needs_vendor_input", "claimed", "recovered", "prevented", "written_off"})
REASON_REQUIRED = frozenset({"rejected", "written_off"})


class IllegalTransition(ValueError):
    pass


def can_transition(a: str, b: str) -> bool:
    return b in TRANSITIONS.get(a, frozenset())


@dataclass(frozen=True)
class Citation:
    node_id: str
    excerpt: str | None = None


@dataclass(frozen=True)
class FindingSpec:
    type: str
    transaction_node: str
    dedupe_key: str
    impact: Decimal | None
    currency: str | None
    confidence: float | None
    engine_version: str
    rule_version: str
    model_version: str
    summary: str
    details: dict = field(default_factory=dict)
    citations: tuple[Citation, ...] = ()


# Each engine registers the finding types it emits; the registry is what the
# UI and the eval harness enumerate.
TYPES: dict[str, str] = {}


def register_type(code: str, description: str) -> None:
    TYPES[code] = description


register_type("BENCH-IMPORT", "Benchmark harness: a record the import round-trip could not link")
```

- [ ] **Step 4: Implement the store**

```python
# invoice_processing/trust/findings/store.py
"""The only writer of trust_findings, its citations and its events."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import delete, select, update

from ...db.models import TrustFinding, TrustFindingCitation, TrustFindingEvent
from ...db.repositories import audit_event_row
from ...db.session import tenant_session
from ...ports import RunContext, StatusChanged
from .model import DECIDED, REASON_REQUIRED, FindingSpec, IllegalTransition, can_transition


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _money(value) -> str | None:
    return None if value is None else str(Decimal(value).quantize(Decimal("0.0001")))


def _finding_dict(f: TrustFinding, citations=(), events=()) -> dict:
    return {
        "id": f.id, "type": f.type, "transaction_node": f.transaction_node, "status": f.status,
        "impact": _money(f.impact), "currency": f.currency,
        "confidence": None if f.confidence is None else float(f.confidence), "owner_id": f.owner_id,
        "engine_version": f.engine_version, "rule_version": f.rule_version, "model_version": f.model_version,
        "dedupe_key": f.dedupe_key, "summary": f.summary, "details": dict(f.details or {}),
        "decided_at": f.decided_at.isoformat() if f.decided_at else None,
        "citations": [{"node_id": c.node_id, "excerpt": c.excerpt} for c in citations],
        "events": [
            {"from_status": e.from_status, "to_status": e.to_status, "amount": _money(e.amount),
             "actor": e.actor, "reason": e.reason, "at": e.created_at.isoformat() if e.created_at else None}
            for e in events
        ],
    }


class FindingStore:
    def __init__(self, url: str | None = None):
        self.url = url

    def upsert(self, ctx: RunContext, spec: FindingSpec) -> tuple[str, str]:
        if not spec.citations:
            raise ValueError("A finding needs at least one citation.")
        with tenant_session(ctx.tenant_id, self.url) as s:
            row = s.scalars(
                select(TrustFinding).where(TrustFinding.tenant_id == ctx.tenant_id, TrustFinding.dedupe_key == spec.dedupe_key)
            ).first()
            if row is not None and row.status in DECIDED:
                return row.id, "untouched"
            created = row is None
            if created:
                row = TrustFinding(tenant_id=ctx.tenant_id, type=spec.type, transaction_node=spec.transaction_node,
                                   dedupe_key=spec.dedupe_key, status="detected")
                s.add(row)
                s.flush()
            row.impact, row.currency, row.confidence = spec.impact, spec.currency, spec.confidence
            row.engine_version, row.rule_version, row.model_version = spec.engine_version, spec.rule_version, spec.model_version
            row.summary, row.details = spec.summary, dict(spec.details)
            s.execute(delete(TrustFindingCitation).where(TrustFindingCitation.finding_id == row.id))
            for c in spec.citations:
                s.add(TrustFindingCitation(tenant_id=ctx.tenant_id, finding_id=row.id, node_id=c.node_id, excerpt=c.excerpt))
            if created:
                s.add(TrustFindingEvent(tenant_id=ctx.tenant_id, finding_id=row.id, from_status=None, to_status="detected",
                                        amount=spec.impact, actor=ctx.actor or "engine"))
            return row.id, "created" if created else "refreshed"

    def transition(
        self, ctx: RunContext, finding_id: str, to_status: str, *, actor: str, reason: str | None = None,
        amount: Decimal | None = None, expect: str | None = None,
    ) -> dict:
        if to_status in REASON_REQUIRED and not (reason or "").strip():
            raise ValueError(f"A reason is required to mark a finding {to_status}.")
        with tenant_session(ctx.tenant_id, self.url) as s:
            row = s.scalars(
                select(TrustFinding).where(TrustFinding.tenant_id == ctx.tenant_id, TrustFinding.id == finding_id)
            ).first()
            if row is None:
                raise KeyError(finding_id)
            was = row.status
            if expect is not None and was != expect:
                raise StatusChanged(was)
            if not can_transition(was, to_status):
                raise IllegalTransition(f"A finding cannot go from {was} to {to_status}.")
            values = {"status": to_status, "updated_at": _now()}
            if to_status in DECIDED:
                values["decided_at"] = _now()
                values["owner_id"] = actor
            changed = s.execute(
                update(TrustFinding)
                .where(TrustFinding.tenant_id == ctx.tenant_id, TrustFinding.id == finding_id, TrustFinding.status == was)
                .values(**values)
            ).rowcount
            if changed != 1:  # someone moved it between our read and our write
                raise StatusChanged(was)
            s.add(TrustFindingEvent(tenant_id=ctx.tenant_id, finding_id=finding_id, from_status=was, to_status=to_status,
                                    amount=amount, actor=actor, reason=reason))
            s.add(audit_event_row(
                ctx.child(case_id=finding_id), f"trust_finding_{to_status}",
                {"finding_id": finding_id, "from": was, "to": to_status, "amount": _money(amount), "reason": reason},
            ))
            s.flush()
            s.refresh(row)
            return {**_finding_dict(row), "previous": was}

    def get(self, tenant_id: str, finding_id: str) -> dict | None:
        with tenant_session(tenant_id, self.url) as s:
            row = s.scalars(select(TrustFinding).where(TrustFinding.tenant_id == tenant_id, TrustFinding.id == finding_id)).first()
            if row is None:
                return None
            cites = s.scalars(select(TrustFindingCitation).where(TrustFindingCitation.finding_id == row.id)).all()
            events = s.scalars(
                select(TrustFindingEvent).where(TrustFindingEvent.finding_id == row.id).order_by(TrustFindingEvent.created_at)
            ).all()
            return _finding_dict(row, cites, events)

    def list(self, tenant_id: str, *, status: str | None = None, type: str | None = None) -> list[dict]:
        with tenant_session(tenant_id, self.url) as s:
            q = select(TrustFinding).where(TrustFinding.tenant_id == tenant_id)
            if status:
                q = q.where(TrustFinding.status == status)
            if type:
                q = q.where(TrustFinding.type == type)
            return [_finding_dict(f) for f in s.scalars(q.order_by(TrustFinding.created_at.desc()))]
```

`RunContext.child(**overrides)` exists at `ports.py:54`; `entity_id` on the audit row comes from `ctx.case_id`, so the finding id is set as the case id for the audit row only.

- [ ] **Step 5: Run to see it pass**

Run: `./.venv/bin/python -m pytest tests/test_trust_findings.py -q -p no:cacheprovider`
Expected: 7 passed.

- [ ] **Step 6: Commit**

```bash
git add invoice_processing/trust/findings tests/test_trust_findings.py
git commit -m "Add findings v2: cited, versioned, deduplicated, with a ledger of every status change

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: Entity resolution — scoring and links

**Files:**
- Create: `invoice_processing/trust/graph/resolve.py`, `invoice_processing/trust/graph/links.py`
- Test: `tests/test_trust_entity_resolution.py`

**Interfaces:**
- Produces:
  - `resolve.normalize_name(s: str) -> str`, `resolve.natural_keys(attrs: dict) -> dict` (`gstin, pan, abn, bank_account, name, email_domain`, present only when non-empty), `resolve.score(a: dict, b: dict) -> tuple[float, str]`, `resolve.band(score: float) -> Literal["auto", "proposed", "none"]`, `resolve.candidates(nodes: list[dict]) -> list[tuple[dict, dict]]` (pairs sharing any key value)
  - `links.LinkStore(url=None)` with `propose_or_link(ctx, node_a, node_b, score, method, reason=None) -> dict`, `decide(ctx, link_id, *, accept: bool, actor, reason=None) -> dict`, `reverse(ctx, link_id, *, actor, reason) -> dict`, `resolved_id(tenant_id, node_id) -> str`, `open_links(tenant_id, *, status=None) -> list[dict]`
  - `resolve.run(ctx, graph: GraphStore, links: LinkStore, *, kind="vendor", decider=None) -> dict` (counts: `auto`, `proposed`)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_trust_entity_resolution.py
"""Two records are one vendor when the evidence says so; a human sees the doubtful ones."""

import pytest

from invoice_processing.db import models
from invoice_processing.db.session import create_all, get_engine, tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust.decide import FakeDecider
from invoice_processing.trust.graph import resolve
from invoice_processing.trust.graph.links import LinkStore
from invoice_processing.trust.graph.store import GraphStore

T = "tenant-acme"
CTX = RunContext(tenant_id=T, case_id="", run_id="r", actor="sync")


def keys(**attrs):
    return resolve.natural_keys(attrs)


def test_gstin_match_scores_one_and_pan_is_derived():
    a = keys(gstin="27AAPFU0939F1ZV", name="Acme Pvt Ltd")
    assert a["pan"] == "AAPFU0939F"
    assert resolve.score(a, keys(gstin="27AAPFU0939F1ZV", name="ACME")) == (1.0, "gstin")


def test_name_plus_domain_auto_links_but_name_alone_is_proposed():
    a = keys(name="Acme Private Limited", email_domain="acme.in")
    b = keys(name="ACME PVT. LTD", email_domain="acme.in")
    s, method = resolve.score(a, b)
    assert resolve.band(s) == "auto" and "name" in method and "email_domain" in method
    s2, _ = resolve.score(keys(name="Acme Pvt Ltd"), keys(name="acme private limited"))
    assert resolve.band(s2) == "proposed"


def test_below_070_never_links():
    assert resolve.band(resolve.score(keys(name="Acme Steel Works"), keys(name="Acme Tools"))[0]) == "none"


def test_conflicting_gstins_never_link_even_with_same_name():
    a = keys(gstin="27AAPFU0939F1ZV", name="Acme Pvt Ltd")
    b = keys(gstin="29AABCU9603R1ZX", name="Acme Pvt Ltd")
    assert resolve.score(a, b) == (0.0, "gstin-conflict")


@pytest.fixture
def env(scratch_db):
    url = scratch_db("links.db")
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
    with tenant_session(T, url) as s:
        s.add(models.Tenant(id=T, name="Acme", slug="acme", status="active"))
    return GraphStore(url), LinkStore(url)


def test_run_links_and_proposes_and_resolved_id_follows_accepted_links(env):
    graph, links = env
    a = graph.upsert_node(CTX, kind="vendor", natural_key="v:a", natural_keys=keys(gstin="27AAPFU0939F1ZV", name="Acme Pvt Ltd"))
    b = graph.upsert_node(CTX, kind="vendor", natural_key="v:b", natural_keys=keys(gstin="27AAPFU0939F1ZV", name="ACME"))
    c = graph.upsert_node(CTX, kind="vendor", natural_key="v:c", natural_keys=keys(name="acme private limited"))
    out = resolve.run(CTX, graph, links, decider=FakeDecider({"same": True}))
    assert out == {"auto": 1, "proposed": 2}
    assert links.resolved_id(T, a) == links.resolved_id(T, b) == min(a, b)
    assert links.resolved_id(T, c) == c
    proposed = [link for link in links.open_links(T, status="proposed")]
    assert len(proposed) == 2 and all("model says: yes" in (p["reason"] or "") for p in proposed)
    accepted = links.decide(CTX, proposed[0]["id"], accept=True, actor="controller@x.test")
    assert accepted["status"] == "accepted"
    assert links.resolved_id(T, c) == min(a, b, c)
    assert resolve.run(CTX, graph, links, decider=FakeDecider({})) == {"auto": 0, "proposed": 0}  # idempotent


def test_reversal_keeps_both_nodes_and_is_audited(env):
    graph, links = env
    a = graph.upsert_node(CTX, kind="vendor", natural_key="v:a")
    b = graph.upsert_node(CTX, kind="vendor", natural_key="v:b")
    link = links.propose_or_link(CTX, a, b, 1.0, "gstin")
    assert link["status"] == "auto"
    out = links.reverse(CTX, link["id"], actor="controller@x.test", reason="different branches")
    assert out["reversed_at"] is not None and links.resolved_id(T, b) == b
    assert len(graph.nodes(T, kind="vendor")) == 2
    with tenant_session(T, links.url) as s:
        assert s.query(models.AuditEvent).filter_by(action="entity_link_reversed").count() == 1
```

- [ ] **Step 2: Run to see it fail**

Run: `./.venv/bin/python -m pytest tests/test_trust_entity_resolution.py -q -p no:cacheprovider`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement scoring**

```python
# invoice_processing/trust/graph/resolve.py
"""Deterministic entity resolution (PRD FR-19.4).

Statutory ids decide; names and domains suggest; nothing below 0.7 is ever
linked. Scores on agreeing keys combine noisy-or; two different GSTINs (or
ABNs) are a contradiction and score zero whatever the name says.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Literal

from ...ports import RunContext
from ..decide import Question, decide

_SUFFIXES = {"pvt", "private", "ltd", "limited", "llp", "inc", "co", "company", "corp", "corporation", "the"}
_KEY_SCORE = {"gstin": 1.0, "abn": 1.0, "pan": 0.98, "bank_account": 0.97, "name": 0.85, "email_domain": 0.75}
_HARD = ("gstin", "abn")


def normalize_name(s: str) -> str:
    words = re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).split()
    return " ".join(w for w in words if w not in _SUFFIXES)


def natural_keys(attrs: dict) -> dict:
    out: dict = {}
    gstin = (attrs.get("gstin") or attrs.get("vendor_tax_id") or attrs.get("tax_id") or "").strip().upper()
    if len(gstin) == 15:
        out["gstin"] = gstin
        out["pan"] = gstin[2:12]
    pan = (attrs.get("pan") or "").strip().upper()
    if len(pan) == 10:
        out["pan"] = pan
    abn = re.sub(r"\D", "", attrs.get("abn") or "")
    if len(abn) == 11:
        out["abn"] = abn
    acct = re.sub(r"\s", "", attrs.get("bank_account_number") or attrs.get("bank_account") or "")
    code = (attrs.get("bank_code") or "").strip().upper()
    if acct and not acct.startswith("…"):
        out["bank_account"] = f"{acct}|{code}"
    name = normalize_name(attrs.get("name") or attrs.get("vendor_name") or attrs.get("vendor_key") or "")
    if name:
        out["name"] = name
    domain = (attrs.get("email_domain") or "").strip().lower()
    if "@" in domain:
        domain = domain.rsplit("@", 1)[1]
    if domain:
        out["email_domain"] = domain
    return out


def score(a: dict, b: dict) -> tuple[float, str]:
    for key in _HARD:
        if a.get(key) and b.get(key) and a[key] != b[key]:
            return 0.0, f"{key}-conflict"
    agreeing = [k for k, s in _KEY_SCORE.items() if a.get(k) and a.get(k) == b.get(k)]
    if not agreeing:
        return 0.0, "none"
    miss = 1.0
    for k in agreeing:
        miss *= 1.0 - _KEY_SCORE[k]
    return round(1.0 - miss, 4), "+".join(agreeing)


def band(value: float) -> Literal["auto", "proposed", "none"]:
    if value >= 0.95:
        return "auto"
    if value >= 0.7:
        return "proposed"
    return "none"


def candidates(nodes: list[dict]) -> list[tuple[dict, dict]]:
    """Pairs that share at least one key value: blocking, not an n² scan.

    # ponytail: exact-value blocking; add trigram blocking if a tenant exceeds ~50k vendors.
    """
    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for n in nodes:
        for k, v in (n.get("natural_keys") or {}).items():
            buckets[(k, v)].append(n)
    seen: set[tuple[str, str]] = set()
    pairs = []
    for group in buckets.values():
        for i, x in enumerate(group):
            for y in group[i + 1:]:
                pair = tuple(sorted((x["id"], y["id"])))
                if pair not in seen:
                    seen.add(pair)
                    pairs.append((x, y) if x["id"] < y["id"] else (y, x))
    return pairs


def run(ctx: RunContext, graph, links, *, kind: str = "vendor", decider=None) -> dict:
    counts = {"auto": 0, "proposed": 0}
    existing = {(link["node_a"], link["node_b"]) for link in links.open_links(ctx.tenant_id)}
    for x, y in candidates(graph.nodes(ctx.tenant_id, kind=kind)):
        if (x["id"], y["id"]) in existing:
            continue
        value, method = score(x.get("natural_keys") or {}, y.get("natural_keys") or {})
        which = band(value)
        if which == "none":
            continue
        reason = None
        if which == "proposed":
            [answer] = decide(
                {"a": x.get("natural_keys"), "b": y.get("natural_keys")},
                [Question("same", "yes_no", "Are these the same vendor?", fallback=False)],
                decider,
            )
            reason = "model says: " + ("unknown" if answer.fallback else ("yes" if answer.value else "no"))
        links.propose_or_link(ctx, x["id"], y["id"], value, method, reason=reason)
        counts[which] += 1
    return counts
```

- [ ] **Step 4: Implement the link store**

```python
# invoice_processing/trust/graph/links.py
"""Entity links: reversible, audited, never deleting a node."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from ...db.models import EntityLink
from ...db.repositories import audit_event_row
from ...db.session import tenant_session
from ...ports import RunContext
from .resolve import band


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dict(link: EntityLink) -> dict:
    return {
        "id": link.id, "node_a": link.node_a, "node_b": link.node_b, "score": float(link.score), "method": link.method,
        "status": link.status, "decided_by": link.decided_by, "reason": link.reason,
        "decided_at": link.decided_at.isoformat() if link.decided_at else None,
        "reversed_at": link.reversed_at.isoformat() if link.reversed_at else None,
    }


class LinkStore:
    def __init__(self, url: str | None = None):
        self.url = url

    def propose_or_link(self, ctx: RunContext, node_a: str, node_b: str, score: float, method: str, reason: str | None = None) -> dict:
        node_a, node_b = sorted((node_a, node_b))
        status = EntityLink.AUTO if band(score) == "auto" else EntityLink.PROPOSED
        with tenant_session(ctx.tenant_id, self.url) as s:
            open_row = s.scalars(
                select(EntityLink).where(EntityLink.tenant_id == ctx.tenant_id, EntityLink.node_a == node_a,
                                         EntityLink.node_b == node_b, EntityLink.reversed_at.is_(None))
            ).first()
            if open_row is not None:
                return _dict(open_row)
            row = EntityLink(tenant_id=ctx.tenant_id, node_a=node_a, node_b=node_b,
                             score=Decimal(str(score)).quantize(Decimal("0.0001")), method=method, status=status, reason=reason)
            s.add(row)
            s.add(audit_event_row(ctx, f"entity_link_{status}", {"node_a": node_a, "node_b": node_b, "score": score, "method": method}))
            s.flush()
            return _dict(row)

    def decide(self, ctx: RunContext, link_id: str, *, accept: bool, actor: str, reason: str | None = None) -> dict:
        with tenant_session(ctx.tenant_id, self.url) as s:
            row = s.scalars(select(EntityLink).where(EntityLink.tenant_id == ctx.tenant_id, EntityLink.id == link_id)).first()
            if row is None:
                raise KeyError(link_id)
            if row.status != EntityLink.PROPOSED:
                raise ValueError(f"This link was already {row.status}.")
            row.status = EntityLink.ACCEPTED if accept else EntityLink.REJECTED
            row.decided_by, row.decided_at = actor, _now()
            if reason:
                row.reason = reason
            s.add(audit_event_row(ctx, f"entity_link_{row.status}", {"link_id": link_id, "actor": actor, "reason": reason}))
            s.flush()
            return _dict(row)

    def reverse(self, ctx: RunContext, link_id: str, *, actor: str, reason: str) -> dict:
        if not (reason or "").strip():
            raise ValueError("A reason is required to reverse a link.")
        with tenant_session(ctx.tenant_id, self.url) as s:
            row = s.scalars(select(EntityLink).where(EntityLink.tenant_id == ctx.tenant_id, EntityLink.id == link_id)).first()
            if row is None:
                raise KeyError(link_id)
            row.reversed_at, row.decided_by, row.reason = _now(), actor, reason
            s.add(audit_event_row(ctx, "entity_link_reversed", {"link_id": link_id, "actor": actor, "reason": reason}))
            s.flush()
            return _dict(row)

    def open_links(self, tenant_id: str, *, status: str | None = None) -> list[dict]:
        with tenant_session(tenant_id, self.url) as s:
            q = select(EntityLink).where(EntityLink.tenant_id == tenant_id, EntityLink.reversed_at.is_(None))
            if status:
                q = q.where(EntityLink.status == status)
            return [_dict(r) for r in s.scalars(q.order_by(EntityLink.created_at))]

    def resolved_id(self, tenant_id: str, node_id: str) -> str:
        """The canonical node: the smallest id in the component joined by auto/accepted links."""
        joined = [(l["node_a"], l["node_b"]) for l in self.open_links(tenant_id) if l["status"] in (EntityLink.AUTO, EntityLink.ACCEPTED)]
        component, frontier = {node_id}, [node_id]
        while frontier:
            cur = frontier.pop()
            for a, b in joined:
                other = b if a == cur else a if b == cur else None
                if other and other not in component:
                    component.add(other)
                    frontier.append(other)
        return min(component)
```

Add `"entity": "entity_link",` to `repositories._ENTITY_TYPES` so `entity_link_*` events resolve (prefix `entity`).

- [ ] **Step 5: Run to see it pass**

Run: `./.venv/bin/python -m pytest tests/test_trust_entity_resolution.py tests/test_db_repositories.py -q -p no:cacheprovider`
Expected: all pass. (`resolved_id` is O(links) per call: `# ponytail: full scan per lookup; cache components per sync if a tenant exceeds ~10k links`.)

- [ ] **Step 6: Commit**

```bash
git add invoice_processing/trust/graph/resolve.py invoice_processing/trust/graph/links.py invoice_processing/db/repositories.py tests/test_trust_entity_resolution.py
git commit -m "Resolve vendors across sources by statutory ids first, names and domains second

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: Graph sync and the canonical `transactions()` accessor

**Files:**
- Create: `invoice_processing/trust/graph/sync.py`, `invoice_processing/trust/graph/transactions.py`
- Test: `tests/test_trust_graph_sync.py`

**Interfaces:**
- Produces:
  - `sync.sync_tenant(ctx: RunContext, graph: GraphStore, links: LinkStore, *, url: str | None = None, document_id: str | None = None, decider=None) -> dict` (counts per node kind; `document_id` restricts to that invoice's subtree; a full run ends with `resolve.run`)
  - `sync.SOURCE_V1 = "v1"`; history edges use `f"history:{source_name}"`
  - Node kinds written: `vendor, invoice, invoice_line, po, po_line, grn, grn_line, approval, bank_change, payment, bank_line, vendor_statement_line`; edge kinds: `issued_by, references, part_of, fulfils, receives, approves, changes, settles`
  - `transactions.Transaction(node_id, kind, vendor_key: str | None, doc_number: str | None, amount: Decimal | None, currency: str | None, date: date | None, source: Literal["live", "history"], ref: tuple[str, str])` frozen
  - `transactions.transactions(tenant_id: str, *, graph: GraphStore, as_of: datetime | None = None) -> list[Transaction]`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_trust_graph_sync.py
"""v1 rows and imported history become one graph; engines read one shape."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from invoice_processing.db import models
from invoice_processing.db.session import create_all, get_engine, tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust.decide import FakeDecider
from invoice_processing.trust.graph.links import LinkStore
from invoice_processing.trust.graph.store import GraphStore
from invoice_processing.trust.graph.sync import sync_tenant
from invoice_processing.trust.graph.transactions import transactions
from invoice_processing.trust.history.importer import HistoryImporter

T = "tenant-acme"
CTX = RunContext(tenant_id=T, case_id="", run_id="r", actor="sync")
NOW = datetime(2026, 10, 14, tzinfo=timezone.utc)


@pytest.fixture
def env(scratch_db):
    url = scratch_db("sync.db")
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
    with tenant_session(T, url) as s:
        s.add(models.Tenant(id=T, name="Acme", slug="acme", status="active"))
        s.flush()
        s.add(models.Document(tenant_id=T, id="doc-1", sha256="a" * 64, storage_key="documents/a.pdf", filename="a.pdf"))
        s.add(models.Vendor(tenant_id=T, id="ven-1", name="Acme Steel Pvt Ltd", normalized_name="acme steel", tax_id="27AAPFU0939F1ZV"))
        po = models.PurchaseOrder(tenant_id=T, id="po-1", po_number="PO1", po_number_display="PO-1", vendor_name="Acme Steel Pvt Ltd",
                                  total_amount=Decimal("1000"), currency="INR", status="open")
        s.add(po)
        s.flush()
        line = models.PoLine(tenant_id=T, id="pol-1", purchase_order_id="po-1", line_number=1, description="bolts",
                             qty_ordered=Decimal("10"), unit_price=Decimal("100"))
        s.add(line)
        s.flush()
        grn = models.GoodsReceipt(tenant_id=T, id="grn-1", grn_number="GRN1", purchase_order_id="po-1", received_on="2026-01-04")
        s.add(grn)
        s.flush()
        s.add(models.GrnLine(tenant_id=T, id="grnl-1", goods_receipt_id="grn-1", po_line_id="pol-1", qty_received=Decimal("10")))
        s.add(models.Invoice(tenant_id=T, id="inv-1", document_id="doc-1", case_id="case-1", invoice_number="INV-7",
                             invoice_date="2026-01-05", po_number="PO-1", currency="INR", vendor_name="ACME STEEL",
                             vendor_tax_id="27AAPFU0939F1ZV", total_inc_tax=Decimal("1000"), status="done"))
    return url, GraphStore(url), LinkStore(url)


def test_sync_twice_creates_no_new_rows(env):
    url, graph, links = env
    first = sync_tenant(CTX, graph, links, url=url, decider=FakeDecider({}))
    assert first["invoice"] == 1 and first["vendor"] == 1 and first["po_line"] == 1 and first["grn"] == 1
    before = (len(graph.nodes(T)), len(graph.edges(T)))
    sync_tenant(CTX, graph, links, url=url, decider=FakeDecider({}))
    assert (len(graph.nodes(T)), len(graph.edges(T))) == before


def test_invoice_links_to_vendor_po_and_grn(env):
    url, graph, links = env
    sync_tenant(CTX, graph, links, url=url, decider=FakeDecider({}))
    inv = graph.nodes_by_ref(T, "invoices", ["inv-1"])["inv-1"]
    ven = graph.nodes_by_ref(T, "vendors", ["ven-1"])["ven-1"]
    po = graph.nodes_by_ref(T, "purchase_orders", ["po-1"])["po-1"]
    assert [e["dst"] for e in graph.edges(T, src=inv, kind="issued_by")] == [ven]
    assert [e["dst"] for e in graph.edges(T, src=inv, kind="references")] == [po]
    grn = graph.nodes_by_ref(T, "goods_receipts", ["grn-1"])["grn-1"]
    assert [e["dst"] for e in graph.edges(T, src=grn, kind="fulfils")] == [po]
    assert graph.edges(T, src=inv, kind="issued_by")[0]["provenance"] == "system_record"


def test_history_and_live_invoices_share_one_transaction_shape(env):
    url, graph, links = env
    importer = HistoryImporter(url)
    csv = b"Bill No,Supplier,Amount,Date\nINV-OLD,Acme Steel Pvt Ltd,500,2025-03-01\n"
    importer.import_file(CTX, kind="ap_invoice", source_name="tally", filename="a.csv", data=csv,
                         mapping={"Bill No": "doc_number", "Supplier": "vendor_key", "Amount": "amount", "Date": "record_date"})
    sync_tenant(CTX, graph, links, url=url, decider=FakeDecider({}))
    txns = sorted(transactions(T, graph=graph), key=lambda t: t.doc_number)
    assert [(t.doc_number, t.source, t.amount, t.date) for t in txns] == [
        ("INV-7", "live", Decimal("1000.0000"), date(2026, 1, 5)),
        ("INV-OLD", "history", Decimal("500.0000"), date(2025, 3, 1)),
    ]
    assert txns[0].vendor_key == txns[1].vendor_key  # the same vendor, by name through resolution
    assert {e["source_system"] for e in graph.edges(T, kind="issued_by")} == {"v1", "history:tally"}


def test_incremental_sync_touches_only_that_invoice(env):
    url, graph, links = env
    out = sync_tenant(CTX, graph, links, url=url, document_id="doc-1", decider=FakeDecider({}))
    assert out["invoice"] == 1 and out.get("po", 0) == 0
    assert graph.nodes_by_ref(T, "purchase_orders", ["po-1"]) == {}


def test_as_of_excludes_rows_closed_before_date(env):
    url, graph, links = env
    sync_tenant(CTX, graph, links, url=url, decider=FakeDecider({}))
    later = NOW + timedelta(days=1)
    with tenant_session(T, url) as s:
        s.get(models.Invoice, "inv-1").total_inc_tax = Decimal("1200")
    sync_tenant(CTX, graph, links, url=url, decider=FakeDecider({}), at=later)
    assert transactions(T, graph=graph)[0].amount == Decimal("1200.0000")
    assert transactions(T, graph=graph, as_of=NOW)[0].amount == Decimal("1000.0000")
```

(`models.Document` requires only `storage_key` and `sha256`; `source` and `malware_status` have defaults.)

- [ ] **Step 2: Run to see it fail**

Run: `./.venv/bin/python -m pytest tests/test_trust_graph_sync.py -q -p no:cacheprovider`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement the sync**

```python
# invoice_processing/trust/graph/sync.py
"""Project v1 tables and imported history into the graph. Idempotent.

Engines never read v1 or history tables: everything they need is copied as
scalars into node attrs here, masked, and linked by edges whose provenance
and confidence say how we know.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from ... import po_matching
from ...db.models import (
    ApprovalStep, GoodsReceipt, GrnLine, HistoryRecord, Invoice, InvoiceLine, PoLine, PurchaseOrder,
    TrustHistoryImport, Vendor, VendorBankChange,
)
from ...db.session import tenant_session
from ...ports import RunContext
from ..history.importer import parse_date
from . import resolve

SOURCE_V1 = "v1"


def _money(v) -> str | None:
    return None if v is None else str(Decimal(v).quantize(Decimal("0.0001")))


def _date(v) -> str | None:
    if not v:
        return None
    try:
        return parse_date(str(v)).isoformat()
    except ValueError:
        return None


def _edge(graph, ctx, src, dst, kind, source, at, confidence=1.0):
    graph.upsert_edge(ctx, src=src, dst=dst, kind=kind, source_system=source, provenance="system_record",
                      confidence=confidence, observed_at=at, at=at)


class _Vendors:
    """Vendor nodes keyed by tax id and normalised name, through entity links."""

    def __init__(self, ctx, graph, links):
        self.ctx, self.graph, self.links = ctx, graph, links
        self.by_key: dict[tuple[str, str], str] = {}
        for n in graph.nodes(ctx.tenant_id, kind="vendor"):
            for k, v in (n.get("natural_keys") or {}).items():
                self.by_key.setdefault((k, v), n["id"])

    def node_for(self, *, name: str | None, tax_id: str | None, source: str, at) -> tuple[str, float]:
        keys = resolve.natural_keys({"name": name, "gstin": tax_id})
        for k in ("gstin", "pan", "name"):
            if k in keys and (k, keys[k]) in self.by_key:
                return self.links.resolved_id(self.ctx.tenant_id, self.by_key[(k, keys[k])]), 1.0 if k != "name" else 0.9
        # Unknown vendor: a node from the record itself, so the edge still exists.
        nid = self.graph.upsert_node(self.ctx, kind="vendor", natural_key=f"{source}:{keys.get('gstin') or keys.get('name') or name}",
                                     attrs={"name": name, "gstin": tax_id}, natural_keys=keys, at=at)
        for k, v in keys.items():
            self.by_key.setdefault((k, v), nid)
        return nid, 0.9


def sync_tenant(ctx: RunContext, graph, links, *, url: str | None = None, document_id: str | None = None,
                decider=None, at: datetime | None = None) -> dict:
    at = at or datetime.now(timezone.utc)
    counts: dict[str, int] = {}

    def bump(kind):
        counts[kind] = counts.get(kind, 0) + 1

    with tenant_session(ctx.tenant_id, url) as s:
        if document_id is None:
            for v in s.scalars(select(Vendor).where(Vendor.tenant_id == ctx.tenant_id)):
                attrs = {"name": v.name, "gstin": v.tax_id, "email_domain": v.email_domain, "bank_account_number": v.bank_account_number,
                         "bank_code": v.bank_code}
                graph.upsert_node(ctx, kind="vendor", natural_key=f"vendors:{v.id}", ref_table="vendors", ref_id=v.id, attrs=attrs,
                                  natural_keys=resolve.natural_keys({**attrs, "bank_account_number": v.bank_account_number}), at=at)
                bump("vendor")
        vendors = _Vendors(ctx, graph, links)

        po_nodes: dict[str, str] = {}
        if document_id is None:
            for po in s.scalars(select(PurchaseOrder).where(PurchaseOrder.tenant_id == ctx.tenant_id)):
                po_id = graph.upsert_node(ctx, kind="po", natural_key=f"purchase_orders:{po.id}", ref_table="purchase_orders", ref_id=po.id,
                                          attrs={"po_number": po.po_number, "vendor_name": po.vendor_name, "amount": _money(po.total_amount),
                                                 "currency": po.currency, "status": po.status}, at=at)
                po_nodes[po.po_number] = po_id
                bump("po")
                ven, conf = vendors.node_for(name=po.vendor_name, tax_id=None, source=SOURCE_V1, at=at)
                _edge(graph, ctx, po_id, ven, "issued_by", SOURCE_V1, at, conf)
                for line in s.scalars(select(PoLine).where(PoLine.purchase_order_id == po.id)):
                    lid = graph.upsert_node(ctx, kind="po_line", natural_key=f"po_lines:{line.id}", ref_table="po_lines", ref_id=line.id,
                                            attrs={"line_number": line.line_number, "qty_ordered": _money(line.qty_ordered),
                                                   "unit_price": _money(line.unit_price)}, at=at)
                    _edge(graph, ctx, lid, po_id, "part_of", SOURCE_V1, at)
                    bump("po_line")
            po_line_nodes = graph.nodes_by_ref(ctx.tenant_id, "po_lines", [r[0] for r in s.execute(select(PoLine.id).where(PoLine.tenant_id == ctx.tenant_id))])
            for grn in s.scalars(select(GoodsReceipt).where(GoodsReceipt.tenant_id == ctx.tenant_id)):
                gid = graph.upsert_node(ctx, kind="grn", natural_key=f"goods_receipts:{grn.id}", ref_table="goods_receipts", ref_id=grn.id,
                                        attrs={"grn_number": grn.grn_number, "received_on": _date(grn.received_on)}, at=at)
                bump("grn")
                po_node = graph.nodes_by_ref(ctx.tenant_id, "purchase_orders", [grn.purchase_order_id]).get(grn.purchase_order_id)
                if po_node:
                    _edge(graph, ctx, gid, po_node, "fulfils", SOURCE_V1, at)
                for gl in s.scalars(select(GrnLine).where(GrnLine.goods_receipt_id == grn.id)):
                    glid = graph.upsert_node(ctx, kind="grn_line", natural_key=f"grn_lines:{gl.id}", ref_table="grn_lines", ref_id=gl.id,
                                             attrs={"qty_received": _money(gl.qty_received)}, at=at)
                    _edge(graph, ctx, glid, gid, "part_of", SOURCE_V1, at)
                    if gl.po_line_id in po_line_nodes:
                        _edge(graph, ctx, glid, po_line_nodes[gl.po_line_id], "receives", SOURCE_V1, at)
                    bump("grn_line")
            for ch in s.scalars(select(VendorBankChange).where(VendorBankChange.tenant_id == ctx.tenant_id)):
                cid = graph.upsert_node(ctx, kind="bank_change", natural_key=f"vendor_bank_changes:{ch.id}", ref_table="vendor_bank_changes",
                                        ref_id=ch.id, attrs={"status": ch.status, "proposed_by": ch.proposed_by,
                                                             "bank_account_number": ch.proposed_account_number}, at=at)
                ven = graph.nodes_by_ref(ctx.tenant_id, "vendors", [ch.vendor_id]).get(ch.vendor_id)
                if ven:
                    _edge(graph, ctx, cid, ven, "changes", SOURCE_V1, at)
                bump("bank_change")
        else:
            po_nodes = {n["attrs"].get("po_number"): n["id"] for n in graph.nodes(ctx.tenant_id, kind="po")}

        inv_q = select(Invoice).where(Invoice.tenant_id == ctx.tenant_id)
        if document_id is not None:
            inv_q = inv_q.where(Invoice.document_id == document_id)
        for inv in s.scalars(inv_q):
            attrs = {"invoice_number": inv.invoice_number, "invoice_date": _date(inv.invoice_date), "po_number": inv.po_number,
                     "amount": _money(inv.total_inc_tax), "currency": inv.currency, "vendor_name": inv.vendor_name,
                     "vendor_tax_id": inv.vendor_tax_id, "decision": inv.decision, "case_id": inv.case_id}
            iid = graph.upsert_node(ctx, kind="invoice", natural_key=f"invoices:{inv.id}", ref_table="invoices", ref_id=inv.id, attrs=attrs, at=at)
            bump("invoice")
            ven, conf = vendors.node_for(name=inv.vendor_name, tax_id=inv.vendor_tax_id, source=SOURCE_V1, at=at)
            _edge(graph, ctx, iid, ven, "issued_by", SOURCE_V1, at, conf)
            po_key = po_matching.normalize_po_number(inv.po_number) if inv.po_number else None
            if po_key and po_key in po_nodes:
                _edge(graph, ctx, iid, po_nodes[po_key], "references", SOURCE_V1, at)
            for line in s.scalars(select(InvoiceLine).where(InvoiceLine.invoice_id == inv.id)):
                lid = graph.upsert_node(ctx, kind="invoice_line", natural_key=f"invoice_lines:{line.id}", ref_table="invoice_lines", ref_id=line.id,
                                        attrs={"line_number": line.line_number, "description": line.description, "quantity": _money(line.quantity),
                                               "unit_price": _money(line.unit_price), "amount": _money(line.amount_ex_tax)}, at=at)
                _edge(graph, ctx, lid, iid, "part_of", SOURCE_V1, at)
                bump("invoice_line")
            for step in s.scalars(select(ApprovalStep).where(ApprovalStep.tenant_id == ctx.tenant_id, ApprovalStep.case_id == inv.case_id)):
                aid = graph.upsert_node(ctx, kind="approval", natural_key=f"approval_steps:{step.id}", ref_table="approval_steps", ref_id=step.id,
                                        attrs={"position": step.position, "status": step.status, "decided_by": step.decided_by}, at=at)
                _edge(graph, ctx, aid, iid, "approves", SOURCE_V1, at)
                bump("approval")

        if document_id is None:
            sources = {r.id: r.source_name for r in s.scalars(select(TrustHistoryImport).where(TrustHistoryImport.tenant_id == ctx.tenant_id))}
            hist_invoices: dict[str, str] = {}
            # Parties and orders before the documents that point at them.
            order = {"vendor": 0, "po": 1, "grn": 2, "ap_invoice": 3, "payment": 4}
            records = sorted(
                s.scalars(select(HistoryRecord).where(HistoryRecord.tenant_id == ctx.tenant_id)),
                key=lambda r: (order.get(r.kind, 9), r.row_number),
            )
            for rec in records:
                source = f"history:{sources.get(rec.import_id, '?')}"
                kind = {"ap_invoice": "invoice", "vendor": "vendor"}.get(rec.kind, rec.kind)
                attrs = {"doc_number": rec.doc_number, "invoice_number": rec.doc_number, "invoice_date": rec.record_date.isoformat() if rec.record_date else None,
                         "amount": _money(rec.amount), "currency": rec.currency, "vendor_name": rec.vendor_key, "external_id": rec.external_id,
                         "po_number": (rec.fields or {}).get("po_number") or None}
                if kind == "vendor":
                    f = rec.fields or {}
                    keys = resolve.natural_keys({"name": rec.vendor_key, "gstin": f.get("gstin"), "pan": f.get("pan"), "abn": f.get("abn"),
                                                 "email_domain": f.get("email_domain")})
                    nid = graph.upsert_node(ctx, kind="vendor", natural_key=f"history_records:{rec.id}", ref_table="history_records", ref_id=rec.id,
                                            attrs={"name": rec.vendor_key, **{k: f.get(k) for k in ("gstin", "pan", "abn", "email_domain")}},
                                            natural_keys=keys, at=at)
                    for k, v in keys.items():
                        vendors.by_key.setdefault((k, v), nid)
                    bump("vendor")
                    continue
                nid = graph.upsert_node(ctx, kind=kind, natural_key=f"history_records:{rec.id}", ref_table="history_records", ref_id=rec.id, attrs=attrs, at=at)
                bump(kind)
                if rec.vendor_key:
                    ven, conf = vendors.node_for(name=rec.vendor_key, tax_id=(rec.fields or {}).get("gstin"), source=source, at=at)
                    _edge(graph, ctx, nid, ven, "issued_by", source, at, conf)
                if kind == "po" and rec.doc_number:
                    po_nodes[po_matching.normalize_po_number(rec.doc_number)] = nid
                if kind == "grn" and attrs["po_number"]:
                    po_key = po_matching.normalize_po_number(attrs["po_number"])
                    if po_key in po_nodes:
                        _edge(graph, ctx, nid, po_nodes[po_key], "fulfils", source, at, 0.9)
                if kind == "invoice" and rec.doc_number:
                    hist_invoices[rec.doc_number] = nid
                if kind == "payment" and rec.doc_number and rec.doc_number in hist_invoices:
                    _edge(graph, ctx, nid, hist_invoices[rec.doc_number], "settles", source, at, 0.9)
                if kind == "invoice" and attrs["po_number"]:
                    po_key = po_matching.normalize_po_number(attrs["po_number"])
                    if po_key in po_nodes:
                        _edge(graph, ctx, nid, po_nodes[po_key], "references", source, at, 0.9)

    if document_id is None:
        counts["links"] = resolve.run(ctx, graph, links, decider=decider)
    return counts
```

`po_matching.normalize_po_number(value) -> str` is at `invoice_processing/po_matching.py:154` and is what `po_book.FileBook` uses.

- [ ] **Step 4: Implement `transactions()`**

```python
# invoice_processing/trust/graph/transactions.py
"""One shape for every transaction, live or historical (spec §4.2)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True)
class Transaction:
    node_id: str
    kind: str
    vendor_key: str | None
    doc_number: str | None
    amount: Decimal | None
    currency: str | None
    date: date | None
    source: Literal["live", "history"]
    ref: tuple[str, str]


def transactions(tenant_id: str, *, graph, as_of: datetime | None = None) -> list[Transaction]:
    vendor_of = {e["src"]: e["dst"] for e in graph.edges(tenant_id, kind="issued_by", as_of=as_of)}
    out = []
    for n in graph.nodes(tenant_id, kind="invoice", as_of=as_of):
        a = n["attrs"]
        out.append(Transaction(
            node_id=n["id"], kind="invoice", vendor_key=vendor_of.get(n["id"]),
            doc_number=a.get("invoice_number") or a.get("doc_number"),
            amount=Decimal(a["amount"]) if a.get("amount") else None, currency=a.get("currency"),
            date=date.fromisoformat(a["invoice_date"]) if a.get("invoice_date") else None,
            source="live" if n["ref_table"] == "invoices" else "history", ref=(n["ref_table"] or "", n["ref_id"] or ""),
        ))
    return out
```

- [ ] **Step 5: Run to see it pass**

Run: `./.venv/bin/python -m pytest tests/test_trust_graph_sync.py -q -p no:cacheprovider`
Expected: 5 passed. If `test_as_of_excludes_rows_closed_before_date` fails because the first sync used wall-clock `at` later than `NOW`, pass `at=NOW` to the first `sync_tenant` call in that test.

- [ ] **Step 6: Commit**

```bash
git add invoice_processing/trust/graph/sync.py invoice_processing/trust/graph/transactions.py tests/test_trust_graph_sync.py
git commit -m "Project v1 rows and imported history into one graph, and read it as one transaction shape

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: Lineage — found, no evidence, contradicts

**Files:**
- Create: `invoice_processing/trust/graph/lineage.py`
- Test: `tests/test_trust_lineage.py`

**Interfaces:**
- Produces `CHAIN = ("contract", "po", "grn", "invoice", "approval", "payment", "gl")`, `Link(stage: str, status: Literal["found", "no_evidence", "contradicts"], node_id: str | None, note: str)` frozen, `lineage(tenant_id: str, node_id: str, *, graph: GraphStore, links: LinkStore | None = None, as_of: datetime | None = None) -> list[Link]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_trust_lineage.py
"""Every gap in Contract -> PO -> GRN -> Invoice -> Approval -> Payment -> GL is named."""

from datetime import datetime, timezone

import pytest

from invoice_processing.db import models
from invoice_processing.db.session import create_all, get_engine, tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust.graph.lineage import CHAIN, lineage
from invoice_processing.trust.graph.store import GraphStore

T = "tenant-acme"
CTX = RunContext(tenant_id=T, case_id="", run_id="r", actor="t")
AT = datetime(2026, 10, 14, tzinfo=timezone.utc)


@pytest.fixture
def graph(scratch_db):
    url = scratch_db("lineage.db")
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
    with tenant_session(T, url) as s:
        s.add(models.Tenant(id=T, name="Acme", slug="acme", status="active"))
    return GraphStore(url)


def edge(graph, src, dst, kind, confidence=1.0):
    graph.upsert_edge(CTX, src=src, dst=dst, kind=kind, source_system="v1", provenance="system_record", confidence=confidence, observed_at=AT)


def build(graph, *, grn=True, same_vendor=True, grn_qty="10"):
    v1 = graph.upsert_node(CTX, kind="vendor", natural_key="v1")
    v2 = graph.upsert_node(CTX, kind="vendor", natural_key="v2")
    po = graph.upsert_node(CTX, kind="po", natural_key="po", attrs={"amount": "1000.0000"})
    inv = graph.upsert_node(CTX, kind="invoice", natural_key="inv", attrs={"amount": "1000.0000", "quantity": "10"})
    edge(graph, po, v1, "issued_by")
    edge(graph, inv, v1 if same_vendor else v2, "issued_by")
    edge(graph, inv, po, "references")
    if grn:
        g = graph.upsert_node(CTX, kind="grn", natural_key="grn", attrs={"qty_received": grn_qty})
        edge(graph, g, po, "fulfils")
    ap = graph.upsert_node(CTX, kind="approval", natural_key="ap", attrs={"status": "approved"})
    edge(graph, ap, inv, "approves")
    pay = graph.upsert_node(CTX, kind="payment", natural_key="pay", attrs={"amount": "1000.0000"})
    edge(graph, pay, inv, "settles")
    return inv


def by_stage(links):
    return {link.stage: link.status for link in links}


def test_full_chain_is_found_except_contract_and_gl_in_wave0(graph):
    out = lineage(T, build(graph), graph=graph)
    assert [link.stage for link in out] == list(CHAIN)
    assert by_stage(out) == {"contract": "no_evidence", "po": "found", "grn": "found", "invoice": "found",
                             "approval": "found", "payment": "found", "gl": "no_evidence"}
    assert "Wave 1" in next(link.note for link in out if link.stage == "contract")


def test_missing_grn_is_no_evidence(graph):
    assert by_stage(lineage(T, build(graph, grn=False), graph=graph))["grn"] == "no_evidence"


def test_po_for_other_vendor_contradicts(graph):
    out = lineage(T, build(graph, same_vendor=False), graph=graph)
    po = next(link for link in out if link.stage == "po")
    assert po.status == "contradicts" and "vendor" in po.note


def test_short_receipt_contradicts(graph):
    out = lineage(T, build(graph, grn_qty="4"), graph=graph)
    grn = next(link for link in out if link.stage == "grn")
    assert grn.status == "contradicts" and "4" in grn.note


def test_unknown_node_is_an_error(graph):
    with pytest.raises(KeyError):
        lineage(T, "nope", graph=graph)
```

- [ ] **Step 2: Run to see it fail**

Run: `./.venv/bin/python -m pytest tests/test_trust_lineage.py -q -p no:cacheprovider`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# invoice_processing/trust/graph/lineage.py
"""Contract -> PO -> GRN -> Invoice -> Approval -> Payment -> GL, with the gaps named (PRD FR-19.6).

# ponytail: a bounded walk over the store's edge queries; a recursive CTE if
# lineage p95 passes 2 s at 5M edges per tenant (ADR-12).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

CHAIN = ("contract", "po", "grn", "invoice", "approval", "payment", "gl")
Status = Literal["found", "no_evidence", "contradicts"]


@dataclass(frozen=True)
class Link:
    stage: str
    status: Status
    node_id: str | None
    note: str


def _dec(value) -> Decimal | None:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except ArithmeticError:
        return None


def lineage(tenant_id: str, node_id: str, *, graph, links=None, as_of: datetime | None = None) -> list[Link]:
    inv = graph.node(tenant_id, node_id)
    if inv is None:
        raise KeyError(node_id)

    def out(kind, src):
        return [e["dst"] for e in graph.edges(tenant_id, src=src, kind=kind, as_of=as_of)]

    def into(kind, dst):
        return [e["src"] for e in graph.edges(tenant_id, dst=dst, kind=kind, as_of=as_of)]

    def same(a, b):
        if links is None:
            return a == b
        return links.resolved_id(tenant_id, a) == links.resolved_id(tenant_id, b)

    result = [Link("contract", "no_evidence", None, "Contract clauses arrive in Wave 1.")]
    vendors = out("issued_by", node_id)
    pos = out("references", node_id)
    po_id = pos[0] if pos else None
    if po_id is None:
        result.append(Link("po", "no_evidence", None, "No purchase order referenced."))
    else:
        po_vendors = out("issued_by", po_id)
        if vendors and po_vendors and not same(vendors[0], po_vendors[0]):
            result.append(Link("po", "contradicts", po_id, "The purchase order was issued to a different vendor."))
        else:
            result.append(Link("po", "found", po_id, "Purchase order referenced."))

    grns = into("fulfils", po_id) if po_id else []
    if not grns:
        result.append(Link("grn", "no_evidence", None, "No goods receipt against the purchase order."))
    else:
        received = sum((_dec(graph.node(tenant_id, g)["attrs"].get("qty_received")) or Decimal(0)) for g in grns)
        wanted = _dec(inv["attrs"].get("quantity"))
        if wanted is not None and received < wanted:
            result.append(Link("grn", "contradicts", grns[0], f"Received {received} against {wanted} invoiced."))
        else:
            result.append(Link("grn", "found", grns[0], f"{len(grns)} receipt(s) found."))

    result.append(Link("invoice", "found", node_id, "This invoice."))
    approvals = into("approves", node_id)
    result.append(Link("approval", "found", approvals[0], f"{len(approvals)} approval step(s).") if approvals
                  else Link("approval", "no_evidence", None, "No approval recorded."))
    payments = into("settles", node_id)
    result.append(Link("payment", "found", payments[0], f"{len(payments)} payment(s).") if payments
                  else Link("payment", "no_evidence", None, "No payment matched."))
    result.append(Link("gl", "no_evidence", None, "GL postings are not projected in Wave 0."))
    return result
```

- [ ] **Step 4: Run to see it pass**

Run: `./.venv/bin/python -m pytest tests/test_trust_lineage.py -q -p no:cacheprovider`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add invoice_processing/trust/graph/lineage.py tests/test_trust_lineage.py
git commit -m "Answer the lineage of a transaction with every gap named

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: Worker jobs and the v1 hook

**Files:**
- Modify: `invoice_processing/worker/runner.py` (constants ~54-57; dispatch ~949-951; after-ack hook ~1003; enqueue helpers ~1283-1320)
- Create: `invoice_processing/trust/evaluate.py`
- Test: `tests/test_trust_worker.py`

**Interfaces:**
- Produces in `runner.py`: `TRUST_GRAPH_SYNC = "trust.graph.sync"`, `TRUST_EVALUATE = "trust.evaluate"`, `TRUST_ATTEMPTS = "trust_attempts"`, `enqueue_trust_graph_sync(queue, tenant_id: str, request_id: str = "") -> str`, `enqueue_trust_evaluate(queue, tenant_id: str, document_id: str, request_id: str = "") -> str`, `Worker._typed: dict[str, Callable[[Message], str]]`, `Worker._handle_trust_sync`, `Worker._handle_trust_evaluate`, `Worker._run_claimless(message, *, key: str, fn: Callable[[], None], event: str) -> str`.
- Produces `evaluate.evaluate(ctx: RunContext, document_id: str, *, graph, links, url=None, settings_store=None) -> dict` (`{"skipped": "no invoice"}` when the document has no invoice; else `{"synced": counts, "engines": {name: mode}}`; runs no engine in Wave 0).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_trust_worker.py
"""Graph sync and trust evaluation ride the existing queue, and never run twice by mistake."""

import pytest

from invoice_processing.db import models
from invoice_processing.db.session import create_all, get_engine, tenant_session
from invoice_processing.api import service
from invoice_processing.trust.graph.store import GraphStore
from invoice_processing.worker.queue import LocalQueue
from invoice_processing.worker.runner import (
    TRUST_EVALUATE, TRUST_GRAPH_SYNC, ClaimStore, Worker, enqueue_trust_evaluate, enqueue_trust_graph_sync,
)

T = "tenant-acme"


@pytest.fixture
def env(tmp_path, monkeypatch, scratch_db):
    url = scratch_db("trust_worker.db")
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
    with tenant_session(T, url) as s:
        s.add(models.Tenant(id=T, name="Acme", slug="acme", status="active"))
        s.add(models.Vendor(tenant_id=T, id="ven-1", name="Acme", normalized_name="acme"))
    monkeypatch.setenv("DATABASE_URL", url)
    graph = GraphStore(url)
    monkeypatch.setattr(service, "_graph_store", graph)
    queue = LocalQueue(tmp_path / "q")
    worker = Worker(queue=queue, claims=ClaimStore(tmp_path / "claims"), handler=lambda m: {"decision": "ACCEPT"})
    return queue, worker, graph


def test_sync_job_builds_the_graph_and_is_idempotent_on_redelivery(env):
    queue, worker, graph = env
    enqueue_trust_graph_sync(queue, T, "req-1")
    assert worker.run_once() == ["trust-synced"]
    n = len(graph.nodes(T))
    assert n >= 1
    enqueue_trust_graph_sync(queue, T, "req-2")
    assert worker.run_once() == ["trust-synced"] and len(graph.nodes(T)) == n


def test_malformed_trust_message_is_dead_lettered(env):
    queue, worker, _ = env
    queue.publish({"type": TRUST_GRAPH_SYNC})
    assert worker.run_once() == ["dead-lettered:malformed"]
    assert len(queue.dead_letters()) == 1


def test_processed_document_enqueues_an_evaluation(env):
    queue, worker, _ = env
    queue.publish({"type": "document.ingested", "tenant_id": T, "document_id": "doc-9", "sha256": "x", "storage_key": "k"})
    assert worker.run_once() == ["done"]
    [msg] = queue.pull()
    assert msg.payload["type"] == TRUST_EVALUATE and msg.payload["document_id"] == "doc-9"


def test_evaluate_skips_a_document_with_no_invoice_and_runs_no_engine(env):
    queue, worker, _ = env
    enqueue_trust_evaluate(queue, T, "doc-without-invoice", "req")
    assert worker.run_once() == ["trust-evaluated"]
    with tenant_session(T) as s:  # url=None reads DATABASE_URL, which the fixture set
        assert s.query(models.TrustFinding).count() == 0


def test_sync_without_a_database_is_dead_lettered(env, monkeypatch):
    queue, worker, _ = env
    monkeypatch.setattr(service, "_graph_store", None)
    monkeypatch.setattr(service, "_database_configured", lambda: False)
    enqueue_trust_graph_sync(queue, T, "req")
    assert worker.run_once() == ["dead-lettered:no-database"]
```

- [ ] **Step 2: Run to see it fail**

Run: `./.venv/bin/python -m pytest tests/test_trust_worker.py -q -p no:cacheprovider`
Expected: FAIL with `ImportError: cannot import name 'TRUST_EVALUATE'`.

- [ ] **Step 3: Write `evaluate.py`**

```python
# invoice_processing/trust/evaluate.py
"""trust.evaluate for one processed document: sync its subtree, then run the
engines whose per-tenant mode is not `off`. Wave 0 ships no engine, so this
syncs and reports the modes it would have honoured."""

from __future__ import annotations

from sqlalchemy import select

from ..db.models import Invoice
from ..db.session import tenant_session
from ..ports import RunContext
from .graph.sync import sync_tenant
from .modes import ENGINES, engine_mode


def evaluate(ctx: RunContext, document_id: str, *, graph, links, url: str | None = None, settings_store=None) -> dict:
    with tenant_session(ctx.tenant_id, url) as s:
        has_invoice = s.scalars(
            select(Invoice.id).where(Invoice.tenant_id == ctx.tenant_id, Invoice.document_id == document_id)
        ).first()
    if has_invoice is None:
        return {"skipped": "no invoice"}
    counts = sync_tenant(ctx, graph, links, url=url, document_id=document_id)
    modes = {name: engine_mode(ctx.tenant_id, name, settings_store) for name in ENGINES}
    # Engines plug in here from Wave 1: `for name, mode in modes.items(): if mode != "off": run(name, mode)`.
    return {"synced": counts, "engines": modes}
```

- [ ] **Step 4: Wire the worker**

In `invoice_processing/worker/runner.py`:

Constants, next to `REMATCH` (~line 55):

```python
TRUST_GRAPH_SYNC = "trust.graph.sync"
TRUST_EVALUATE = "trust.evaluate"
TRUST_ATTEMPTS = "trust_attempts"
```

In `Worker.__init__`, after `self.attempts = attempts`:

```python
        # Jobs that carry no document and hold no claim, by payload type.
        self._typed = {
            REMATCH: self._handle_rematch,
            TRUST_GRAPH_SYNC: self._handle_trust_sync,
            TRUST_EVALUATE: self._handle_trust_evaluate,
        }
```

Replace the dispatch at ~949-951:

```python
        typed = self._typed.get(str(message.payload.get("type") or ""))
        if typed is not None:
            return typed(message)
```

After the ack and `_emit` at ~1003 (inside `_handle`, before `return "done"`):

```python
        # v2: evaluate what we now know about this document. Best effort,
        # like the audit row: a queue hiccup must not undo a finished job.
        try:
            enqueue_trust_evaluate(self.queue, tenant_id, document_id,
                                   observability.current_request_id.get("") or message.id)
        except Exception:
            logger.warning("trust.evaluate could not be queued", exc_info=True)
```

Add the handlers after `_handle_rematch`:

```python
    def _run_claimless(self, message: Message, *, key: str, fn, event: str, done: str) -> str:
        """Retry-and-dead-letter shell shared by the trust jobs (same shape as re-match)."""
        tenant_id = str(message.payload.get("tenant_id") or "")
        log = AttemptLog(self.attempts.cases, key=TRUST_ATTEMPTS) if self.attempts is not None else None
        try:
            fn()
        except _NoDatabase as exc:
            self._dead_letter(message, str(exc), event=event)
            return "dead-lettered:no-database"
        except Exception as exc:
            attempt = self._count_failure(tenant_id, key, message, exc, log=log)
            logger.exception("%s attempt %s/%s failed for %s", message.payload.get("type"), attempt, self.max_attempts, key)
            if attempt >= self.max_attempts:
                sentry_sdk.capture_exception(exc)
                self._dead_letter(message, _brief(exc), event=event)
                return "dead-lettered:exhausted"
            self.queue.nack(message)
            return "retry"
        self._clear_failures(tenant_id, key, log=log)
        self.queue.ack(message)
        return done

    def _trust_context(self, message: Message, case_id: str) -> RunContext:
        return RunContext(tenant_id=str(message.payload.get("tenant_id") or ""), case_id=case_id,
                          run_id=observability.current_request_id.get("") or message.id, actor="worker")

    def _handle_trust_sync(self, message: Message) -> str:
        tenant_id = str(message.payload.get("tenant_id") or "")
        if not tenant_id:
            self._dead_letter(message, "Graph sync message is missing tenant_id", event="trust_dead_lettered")
            return "dead-lettered:malformed"

        def run():
            from ..api import service  # noqa: PLC0415
            from ..trust.graph.links import LinkStore  # noqa: PLC0415
            from ..trust.graph.sync import sync_tenant  # noqa: PLC0415

            graph = service.get_graph_store()
            if graph is None:
                raise _NoDatabase("Graph sync needs a database.")
            sync_tenant(self._trust_context(message, ""), graph, LinkStore())

        return self._run_claimless(message, key=f"sync:{tenant_id}", fn=run, event="trust_dead_lettered", done="trust-synced")

    def _handle_trust_evaluate(self, message: Message) -> str:
        tenant_id = str(message.payload.get("tenant_id") or "")
        document_id = str(message.payload.get("document_id") or "")
        if not tenant_id or not document_id:
            self._dead_letter(message, "Evaluate message is missing tenant_id or document_id", event="trust_dead_lettered")
            return "dead-lettered:malformed"

        def run():
            from ..api import service  # noqa: PLC0415
            from ..trust.evaluate import evaluate  # noqa: PLC0415
            from ..trust.graph.links import LinkStore  # noqa: PLC0415

            graph = service.get_graph_store()
            if graph is None:
                raise _NoDatabase("Trust evaluation needs a database.")
            evaluate(self._trust_context(message, document_id), document_id, graph=graph, links=LinkStore())

        return self._run_claimless(message, key=f"evaluate:{document_id}", fn=run, event="trust_dead_lettered", done="trust-evaluated")
```

and, at module level near `PermanentFailure`:

```python
class _NoDatabase(RuntimeError):
    """A trust job on a deployment without a database: nothing to retry into."""
```

and the enqueue helpers beside `enqueue_rematch`:

```python
def enqueue_trust_graph_sync(queue: Queue, tenant_id: str, request_id: str = "") -> str:
    return queue.publish({"type": TRUST_GRAPH_SYNC, "tenant_id": tenant_id, "request_id": request_id}, tenant_id=tenant_id)


def enqueue_trust_evaluate(queue: Queue, tenant_id: str, document_id: str, request_id: str = "") -> str:
    return queue.publish(
        {"type": TRUST_EVALUATE, "tenant_id": tenant_id, "document_id": document_id, "request_id": request_id},
        tenant_id=tenant_id,
    )
```

Also make `_refuse_closed_tenant` (~908-937) pick `"trust_dead_lettered"` for the two trust types where it picks `rematch_dead_lettered` for `REMATCH`, and add `"trust": "trust"` already covers `trust_dead_lettered` in `_ENTITY_TYPES`.

- [ ] **Step 5: Run the trust worker tests and the existing worker suites**

Run: `./.venv/bin/python -m pytest tests/test_trust_worker.py tests/test_worker.py tests/test_rematch.py tests/test_worker_logging.py tests/test_alf_in_the_worker.py -q -p no:cacheprovider`
Expected: all pass after one adjustment. Exactly two assertions in `tests/test_worker.py` check an empty queue after a document job (`assert queue.pull() == [], "the message was not acked"` at lines 259 and 1269). Each now finds the `trust.evaluate` follow-up instead; change both to:

```python
        [follow] = queue.pull()
        assert follow.payload["type"] == TRUST_EVALUATE, "the document job was not acked"
```

importing `TRUST_EVALUATE` from `invoice_processing.worker.runner`. The four `queue.pull() == []` assertions at lines 106-179 are queue-semantics tests with no worker and stay as they are.

- [ ] **Step 6: Commit**

```bash
git add invoice_processing/worker/runner.py invoice_processing/trust/evaluate.py tests/test_trust_worker.py tests/test_worker.py
git commit -m "Run graph sync and trust evaluation as worker jobs; evaluate every processed document

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 12: Providers — interfaces, registry, evidence capture, fakes

**Files:**
- Create: `invoice_processing/trust/providers/__init__.py`, `base.py`, `registry.py`, `fakes.py`
- Test: `tests/test_trust_providers.py`

**Interfaces:**
- Produces:
  - `base.ProviderResult(status: Literal["ok", "unavailable"], data: dict, raw_sha256: str, provider: str, capability: str, observed_at: datetime, evidence_node: str | None)` frozen
  - `base.CAPABILITIES` (re-exported from `modes`), Protocols `BankAccountVerifier.verify(account: str, code: str, name: str) -> dict`, `GstinLookup.lookup(gstin: str) -> dict`, `CompanyRegistry.lookup(query: str) -> dict`, `AbnLookup.lookup(abn: str) -> dict`, `VatLookup.check(vat: str) -> dict`, `Mailbox.fetch(since: str) -> list[dict]`, `Tickets.search(query: str) -> list[dict]`, `Timesheets.hours(vendor: str, period: str) -> dict`, `Logistics.track(reference: str) -> dict`; `base.METHOD: dict[str, str]` capability → method name
  - `registry.Registry(graph: GraphStore)` with `register(capability: str, name: str, factory: Callable[[], object]) -> None`, `call(ctx: RunContext, capability: str, *args, settings_store=None, **kwargs) -> ProviderResult` (never raises for provider trouble)
  - `fakes.FakeProvider(script: dict[str, dict | Exception])` keyed by the first positional argument; `fakes.install(registry: Registry) -> None` registers a `"fake"` factory for every capability returning an empty-script `FakeProvider` (answers `{"result": "unknown"}`)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_trust_providers.py
"""External answers are evidence; a provider outage is an evidence record too, never a pass."""

import pytest

from invoice_processing.db import models
from invoice_processing.db.session import create_all, get_engine, tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust.graph.store import GraphStore
from invoice_processing.trust.providers import fakes
from invoice_processing.trust.providers.registry import Registry

T = "tenant-acme"
CTX = RunContext(tenant_id=T, case_id="", run_id="r", actor="engine")


class Store:
    def __init__(self, data=None):
        self.data = data or {}

    def get(self, tenant_id):
        return self.data.get(tenant_id)


@pytest.fixture
def registry(scratch_db):
    url = scratch_db("providers.db")
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
    with tenant_session(T, url) as s:
        s.add(models.Tenant(id=T, name="Acme", slug="acme", status="active"))
    reg = Registry(GraphStore(url))
    fakes.install(reg)
    return reg


def test_fake_response_is_stored_as_hashed_evidence_node(registry):
    registry.register("bank_verification", "fake", lambda: fakes.FakeProvider({"123": {"name_match": True, "name": "ACME STEEL"}}))
    out = registry.call(CTX, "bank_verification", "123", "HDFC0001", "Acme Steel", settings_store=Store())
    assert out.status == "ok" and out.data["name_match"] is True and out.provider == "fake"
    node = registry.graph.node(T, out.evidence_node)
    assert node["kind"] == "provider_evidence" and node["attrs"]["raw_sha256"] == out.raw_sha256
    assert node["natural_key"] == f"bank_verification:{out.raw_sha256}"


def test_provider_error_becomes_unavailable_evidence_not_pass(registry):
    registry.register("gstin", "fake", lambda: fakes.FakeProvider({"27X": RuntimeError("timeout")}))
    out = registry.call(CTX, "gstin", "27X", settings_store=Store())
    assert out.status == "unavailable" and out.data == {"error": "timeout"}
    assert registry.graph.node(T, out.evidence_node)["attrs"]["status"] == "unavailable"


def test_registry_picks_fake_by_default_and_real_by_setting(registry):
    registry.register("vat", "vies", lambda: fakes.FakeProvider({"DE1": {"valid": True}}))
    default = registry.call(CTX, "vat", "DE1", settings_store=Store())
    assert default.provider == "fake" and default.data == {"result": "unknown"}
    real = registry.call(CTX, "vat", "DE1", settings_store=Store({T: {"trust_providers": {"vat": "vies"}}}))
    assert real.provider == "vies" and real.data == {"valid": True}


def test_unknown_capability_or_provider_is_refused(registry):
    with pytest.raises(ValueError, match="capability"):
        registry.call(CTX, "telepathy", "x", settings_store=Store())
    with pytest.raises(ValueError, match="No provider"):
        registry.call(CTX, "vat", "DE1", settings_store=Store({T: {"trust_providers": {"vat": "nope"}}}))


def test_evidence_attrs_are_masked(registry):
    registry.register("bank_verification", "fake", lambda: fakes.FakeProvider({"999": {"bank_account_number": "123456789012"}}))
    out = registry.call(CTX, "bank_verification", "999", "C", "N", settings_store=Store())
    assert registry.graph.node(T, out.evidence_node)["attrs"]["data"]["bank_account_number"] == "…9012"
```

- [ ] **Step 2: Run to see it fail**

Run: `./.venv/bin/python -m pytest tests/test_trust_providers.py -q -p no:cacheprovider`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`invoice_processing/trust/providers/__init__.py`: docstring `"""External lookups behind adapters with evidence capture (ADR-15)."""`.

```python
# invoice_processing/trust/providers/base.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from ..modes import CAPABILITIES

__all__ = ["CAPABILITIES", "METHOD", "ProviderResult"]


@dataclass(frozen=True)
class ProviderResult:
    status: Literal["ok", "unavailable"]
    data: dict
    raw_sha256: str
    provider: str
    capability: str
    observed_at: datetime
    evidence_node: str | None


@runtime_checkable
class BankAccountVerifier(Protocol):
    def verify(self, account: str, code: str, name: str) -> dict: ...


@runtime_checkable
class GstinLookup(Protocol):
    def lookup(self, gstin: str) -> dict: ...


@runtime_checkable
class CompanyRegistry(Protocol):
    def lookup(self, query: str) -> dict: ...


@runtime_checkable
class AbnLookup(Protocol):
    def lookup(self, abn: str) -> dict: ...


@runtime_checkable
class VatLookup(Protocol):
    def check(self, vat: str) -> dict: ...


@runtime_checkable
class Mailbox(Protocol):
    def fetch(self, since: str) -> list[dict]: ...


@runtime_checkable
class Tickets(Protocol):
    def search(self, query: str) -> list[dict]: ...


@runtime_checkable
class Timesheets(Protocol):
    def hours(self, vendor: str, period: str) -> dict: ...


@runtime_checkable
class Logistics(Protocol):
    def track(self, reference: str) -> dict: ...


# The one method each capability exposes; the registry calls it by name.
METHOD = {
    "bank_verification": "verify", "gstin": "lookup", "company_registry": "lookup", "abn": "lookup",
    "vat": "check", "mailbox": "fetch", "tickets": "search", "timesheets": "hours", "logistics": "track",
}
```

```python
# invoice_processing/trust/providers/registry.py
"""Pick fake or real per tenant, call it, and keep the raw answer as evidence.

# ponytail: evidence is a graph node, not its own table; add one if evidence
# volume outgrows graph_nodes.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone

from ...ports import RunContext
from ..modes import CAPABILITIES, provider_name
from .base import METHOD, ProviderResult

logger = logging.getLogger(__name__)


class Registry:
    def __init__(self, graph):
        self.graph = graph
        self._factories: dict[tuple[str, str], Callable[[], object]] = {}

    def register(self, capability: str, name: str, factory: Callable[[], object]) -> None:
        if capability not in CAPABILITIES:
            raise ValueError(f"No such capability: {capability}")
        self._factories[(capability, name)] = factory

    def call(self, ctx: RunContext, capability: str, *args, settings_store=None, **kwargs) -> ProviderResult:
        if capability not in CAPABILITIES:
            raise ValueError(f"No such capability: {capability}")
        name = provider_name(ctx.tenant_id, capability, settings_store)
        factory = self._factories.get((capability, name))
        if factory is None:
            raise ValueError(f"No provider {name!r} registered for {capability}.")
        observed_at = datetime.now(timezone.utc)
        try:
            data = getattr(factory(), METHOD[capability])(*args, **kwargs)
            status = "ok"
        except Exception as exc:  # the provider's trouble is evidence, not a pass
            data, status = {"error": str(exc)[:500]}, "unavailable"
            logger.warning("provider_unavailable", extra={"capability": capability, "provider": name})
        raw = json.dumps(data, sort_keys=True, default=str)
        sha = hashlib.sha256(raw.encode()).hexdigest()
        node = self.graph.upsert_node(
            ctx, kind="provider_evidence", natural_key=f"{capability}:{sha}",
            attrs={"capability": capability, "provider": name, "status": status, "raw_sha256": sha,
                   "observed_at": observed_at.isoformat(), "data": data if isinstance(data, dict) else {"items": data}},
            at=observed_at,
        )
        return ProviderResult(status, data if isinstance(data, dict) else {"items": data}, sha, name, capability, observed_at, node)
```

Note `GraphStore.upsert_node` masks only top-level `BANK_FIELDS`; the evidence `data` is nested, so extend the registry to mask it before storing: `from ... import masking` and `"data": masking.mask_row(data) if isinstance(data, dict) else {...}` — do this, the last test depends on it.

```python
# invoice_processing/trust/providers/fakes.py
"""Deterministic providers for tests and demos."""

from __future__ import annotations

from ..modes import CAPABILITIES
from .base import METHOD


class FakeProvider:
    """Answers scripted by the first positional argument; an Exception value is raised."""

    def __init__(self, script: dict[str, dict | list | Exception] | None = None):
        self.script = dict(script or {})
        self.calls: list[tuple] = []

    def _answer(self, *args, **kwargs):
        self.calls.append(args)
        key = str(args[0]) if args else ""
        value = self.script.get(key, {"result": "unknown"})
        if isinstance(value, Exception):
            raise value
        return value

    verify = lookup = check = fetch = search = hours = track = _answer


def install(registry) -> None:
    for capability in CAPABILITIES:
        registry.register(capability, "fake", FakeProvider)
    assert all(hasattr(FakeProvider, m) for m in METHOD.values())
```

- [ ] **Step 4: Run to see it pass**

Run: `./.venv/bin/python -m pytest tests/test_trust_providers.py -q -p no:cacheprovider`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add invoice_processing/trust/providers tests/test_trust_providers.py
git commit -m "Put every external lookup behind an adapter that keeps its answer as evidence

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 13: Benchmark v2 — tenant-history generator and the import round-trip gate

**Files:**
- Create: `benchmark/history/__init__.py`, `benchmark/history/generate.py`, `benchmark/history/eval.py`, `benchmark/history/README.md`
- Test: `tests/test_benchmark_history.py`

**Interfaces:**
- Produces:
  - `generate.Manifest(seed: int, counts: dict[str, int], expected_links: list[dict], faults: list[dict])` frozen, with `.as_dict()`
  - `generate.generate(seed: int, out: Path, *, months: int = 18, vendors: int = 60) -> Manifest` writing `vendor.csv, po.csv, grn.csv, ap_invoice.csv, payment.csv, manifest.json`
  - `eval.GATES = {"invoice_vendor_coverage": 0.95, "po_backed_coverage": 0.80}`
  - `eval.run(url: str, data_dir: Path, tenant_id: str = "bench-history") -> dict` returning `{"invoice_vendor_coverage": float, "po_backed_coverage": float, "contract_clause_coverage": None, "not_measurable_until": {"contract_clause_coverage": "wave1"}, "gates": {name: bool}, "passed": bool}`
  - CLI `python -m benchmark.history.eval [--seed 7] [--db sqlite:///...] [--out benchmark/runs/history]` exit 1 on a failed gate

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_benchmark_history.py
"""The round-trip gate: what the generator injected, the graph must link."""

import json

import pytest

from benchmark.history import eval as history_eval
from benchmark.history import generate


def test_generator_is_deterministic_for_a_seed(tmp_path):
    a = generate.generate(7, tmp_path / "a")
    b = generate.generate(7, tmp_path / "b")
    assert a == b and (tmp_path / "a" / "ap_invoice.csv").read_bytes() == (tmp_path / "b" / "ap_invoice.csv").read_bytes()
    assert a.counts["ap_invoice"] >= 200 and a.counts["vendor"] == 60
    manifest = json.loads((tmp_path / "a" / "manifest.json").read_text())
    assert manifest["expected_links"][0].keys() >= {"invoice", "vendor", "po", "grn"}
    assert manifest["faults"] == []


def test_round_trip_meets_wave0_gates_on_sqlite(tmp_path, scratch_db):
    data = tmp_path / "data"
    generate.generate(7, data)
    out = history_eval.run(scratch_db("bench.db"), data)
    assert out["gates"] == {"invoice_vendor_coverage": True, "po_backed_coverage": True}
    assert out["invoice_vendor_coverage"] >= 0.95 and out["po_backed_coverage"] >= 0.80
    assert out["contract_clause_coverage"] is None and out["not_measurable_until"]["contract_clause_coverage"] == "wave1"
    assert out["passed"] is True


def test_a_dropped_vendor_file_fails_the_vendor_gate(tmp_path, scratch_db):
    data = tmp_path / "data"
    generate.generate(7, data)
    (data / "vendor.csv").write_text("external_id,vendor_key,gstin\n")
    out = history_eval.run(scratch_db("bench2.db"), data)
    assert out["passed"] is False and out["gates"]["invoice_vendor_coverage"] is False


def test_cli_exits_nonzero_on_a_failed_gate(tmp_path, scratch_db, monkeypatch):
    data = tmp_path / "data"
    generate.generate(7, data)
    (data / "grn.csv").write_text("external_id,doc_number,po_number,record_date,quantity\n")
    with pytest.raises(SystemExit) as stop:
        history_eval.main(["--db", scratch_db("bench3.db"), "--data", str(data), "--out", str(tmp_path / "runs")])
    assert stop.value.code == 1
    assert list((tmp_path / "runs").glob("*.json"))
```

- [ ] **Step 2: Run to see it fail**

Run: `./.venv/bin/python -m pytest tests/test_benchmark_history.py -q -p no:cacheprovider`
Expected: FAIL with `ModuleNotFoundError: benchmark.history`.

- [ ] **Step 3: Write the generator**

`benchmark/history/__init__.py`: docstring `"""Benchmark v2: a synthetic tenant history with a fault manifest (PRD §14)."""`.

```python
# benchmark/history/generate.py
"""A synthetic company's 18 months of AP history, as the files a customer would export.

Deterministic for a seed. Headers are the import field names so the mapping
proposer links them without a model. Wave 0 injects no faults: the manifest's
`faults` list exists for the engine waves to fill.
"""

from __future__ import annotations

import csv
import json
import random
import string
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

KINDS = ("vendor", "po", "grn", "ap_invoice", "payment")
HEADERS = {
    "vendor": ["external_id", "vendor_key", "gstin", "email_domain"],
    "po": ["external_id", "doc_number", "vendor_key", "amount", "currency", "record_date"],
    "grn": ["external_id", "doc_number", "po_number", "record_date", "quantity"],
    "ap_invoice": ["external_id", "doc_number", "vendor_key", "amount", "currency", "record_date", "po_number"],
    "payment": ["external_id", "doc_number", "vendor_key", "amount", "currency", "record_date", "bank_reference"],
}


@dataclass(frozen=True)
class Manifest:
    seed: int
    counts: dict
    expected_links: list
    faults: list

    def as_dict(self) -> dict:
        return asdict(self)


def _gstin(rng: random.Random) -> str:
    state = f"{rng.randint(1, 37):02d}"
    pan = "".join(rng.choices(string.ascii_uppercase, k=5)) + f"{rng.randint(0, 9999):04d}" + rng.choice(string.ascii_uppercase)
    return f"{state}{pan}1Z{rng.choice(string.ascii_uppercase + string.digits)}"


def generate(seed: int, out: Path, *, months: int = 18, vendors: int = 60) -> Manifest:
    rng = random.Random(seed)
    out.mkdir(parents=True, exist_ok=True)
    start = date(2025, 1, 1)
    rows = {k: [] for k in KINDS}
    links = []

    names = [f"{rng.choice(['Acme', 'Globex', 'Initech', 'Umbrella', 'Stark', 'Wayne'])} {rng.choice(['Steel', 'Tools', 'Logistics', 'Services', 'Foods'])} {i}" for i in range(vendors)]
    for i, name in enumerate(names):
        rows["vendor"].append({"external_id": f"V{i:03d}", "vendor_key": name, "gstin": _gstin(rng), "email_domain": f"{name.split()[0].lower()}{i}.in"})

    po_seq = inv_seq = grn_seq = pay_seq = 0
    for month in range(months):
        for i, name in enumerate(names):
            if rng.random() < 0.35:
                continue  # not every vendor invoices every month
            day = start + timedelta(days=30 * month + rng.randint(0, 27))
            amount = Decimal(rng.randint(5_000, 500_000))
            inv_seq += 1
            inv = {"external_id": f"I{inv_seq:05d}", "doc_number": f"INV-{i}-{inv_seq}", "vendor_key": name, "amount": str(amount),
                   "currency": "INR", "record_date": day.isoformat(), "po_number": ""}
            link = {"invoice": inv["external_id"], "vendor": f"V{i:03d}", "po": None, "grn": None}
            if rng.random() < 0.7:  # PO-backed, received
                po_seq += 1
                grn_seq += 1
                po_no = f"PO-{po_seq:05d}"
                rows["po"].append({"external_id": f"P{po_seq:05d}", "doc_number": po_no, "vendor_key": name, "amount": str(amount),
                                   "currency": "INR", "record_date": (day - timedelta(days=rng.randint(5, 40))).isoformat()})
                rows["grn"].append({"external_id": f"G{grn_seq:05d}", "doc_number": f"GRN-{grn_seq:05d}", "po_number": po_no,
                                    "record_date": (day - timedelta(days=rng.randint(0, 4))).isoformat(), "quantity": str(rng.randint(1, 100))})
                inv["po_number"] = po_no
                link["po"], link["grn"] = f"P{po_seq:05d}", f"G{grn_seq:05d}"
            rows["ap_invoice"].append(inv)
            links.append(link)
            if rng.random() < 0.9:
                pay_seq += 1
                rows["payment"].append({"external_id": f"Y{pay_seq:05d}", "doc_number": inv["doc_number"], "vendor_key": name, "amount": str(amount),
                                        "currency": "INR", "record_date": (day + timedelta(days=rng.randint(10, 60))).isoformat(),
                                        "bank_reference": f"UTR{rng.randint(10**11, 10**12 - 1)}"})

    for kind in KINDS:
        with (out / f"{kind}.csv").open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=HEADERS[kind])
            writer.writeheader()
            writer.writerows(rows[kind])
    manifest = Manifest(seed=seed, counts={k: len(v) for k, v in rows.items()}, expected_links=links, faults=[])
    (out / "manifest.json").write_text(json.dumps(manifest.as_dict(), indent=2), encoding="utf-8")
    return manifest
```

- [ ] **Step 4: Write the eval runner**

```python
# benchmark/history/eval.py
"""Import a generated history, build the graph, and measure what got linked (AC-19)."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from invoice_processing.db import models
from invoice_processing.db.session import create_all, get_engine, tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust.decide import FakeDecider
from invoice_processing.trust.graph.links import LinkStore
from invoice_processing.trust.graph.store import GraphStore
from invoice_processing.trust.graph.sync import sync_tenant
from invoice_processing.trust.history import mapping
from invoice_processing.trust.history.importer import HistoryImporter
from invoice_processing.trust.history.readers import read_table

from .generate import KINDS, generate

GATES = {"invoice_vendor_coverage": 0.95, "po_backed_coverage": 0.80}


def _ratio(num: int, den: int) -> float:
    return round(num / den, 4) if den else 0.0


def run(url: str, data_dir: Path, tenant_id: str = "bench-history") -> dict:
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
    with tenant_session(tenant_id, url) as s:
        s.add(models.Tenant(id=tenant_id, name="Bench", slug=tenant_id, status="active"))
    ctx = RunContext(tenant_id=tenant_id, case_id="", run_id="bench", actor="bench")
    importer = HistoryImporter(url)
    for kind in KINDS:
        data = (data_dir / f"{kind}.csv").read_bytes()
        headers, _ = read_table(data, f"{kind}.csv")
        importer.import_file(ctx, kind=kind, source_name="bench", filename=f"{kind}.csv", data=data,
                             mapping=mapping.propose(kind, headers, decider=FakeDecider({}, fail=True)))
    graph, links = GraphStore(url), LinkStore(url)
    sync_tenant(ctx, graph, links, url=url, decider=FakeDecider({}, fail=True))

    manifest = json.loads((data_dir / "manifest.json").read_text())
    by_ext = {}
    with tenant_session(tenant_id, url) as s:
        for r in s.query(models.HistoryRecord).filter_by(tenant_id=tenant_id):
            by_ext[(r.kind, r.external_id)] = r.id
    node_of = graph.nodes_by_ref(tenant_id, "history_records", list(by_ext.values()))
    issued = {e["src"] for e in graph.edges(tenant_id, kind="issued_by")}
    references = {e["src"]: e["dst"] for e in graph.edges(tenant_id, kind="references")}
    fulfilled = {e["dst"] for e in graph.edges(tenant_id, kind="fulfils")}

    invoices = po_backed = inv_linked = po_linked = 0
    for link in manifest["expected_links"]:
        inv_node = node_of.get(by_ext.get(("ap_invoice", link["invoice"])))
        invoices += 1
        if inv_node in issued:
            inv_linked += 1
        if link["po"]:
            po_backed += 1
            po_node = references.get(inv_node)
            if po_node and po_node in fulfilled:
                po_linked += 1
    out = {
        "invoice_vendor_coverage": _ratio(inv_linked, invoices),
        "po_backed_coverage": _ratio(po_linked, po_backed),
        "contract_clause_coverage": None,
        "not_measurable_until": {"contract_clause_coverage": "wave1"},
        "counts": manifest["counts"],
    }
    out["gates"] = {name: out[name] >= floor for name, floor in GATES.items()}
    out["passed"] = all(out["gates"].values())
    return out


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="benchmark.history.eval")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--db", default=None, help="SQLAlchemy URL; default a temporary SQLite file")
    parser.add_argument("--data", default=None, help="generated data dir; default: generate into a temp dir")
    parser.add_argument("--out", default="benchmark/runs/history")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as tmp:
        data = Path(args.data) if args.data else Path(tmp) / "data"
        if not args.data:
            generate(args.seed, data)
        url = args.db or f"sqlite:///{Path(tmp) / 'bench.db'}"
        result = run(url, data)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (out_dir / f"{stamp}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("invoice_vendor_coverage", "po_backed_coverage", "gates", "passed")}))
    sys.exit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
```

`benchmark/history/README.md` (short): what the generator makes, the two gates, that contract-clause coverage is recorded as not measurable until Wave 1, and the CLI line.

- [ ] **Step 5: Run to see it pass**

Run: `./.venv/bin/python -m pytest tests/test_benchmark_history.py -q -p no:cacheprovider`
Expected: 4 passed. Then `./.venv/bin/python -m benchmark.history.eval --seed 7` prints `"passed": true` and exits 0; delete the written `benchmark/runs/history/*.json` unless the repo already tracks `benchmark/runs` (check `.gitignore`; keep runs untracked).

- [ ] **Step 6: Commit**

```bash
git add benchmark/history tests/test_benchmark_history.py
git commit -m "Add the benchmark v2 harness: a generated tenant history and the import round-trip gate

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 14: Gates, CI, docs

**Files:**
- Modify: `benchmark/harness/gates.sh` (add a 10th gate), `.github/workflows/ci.yml` (Postgres list ~157-183; a new step in the `test` job), `docs/PENDING.md`, `docs/superpowers/specs/2026-10-14-v2-trust-layer-design.md` (status line only)

- [ ] **Step 1: Add the gate**

In `benchmark/harness/gates.sh`, after the `eval gate` line:

```bash
gate "trust history gate" "$PY" -m benchmark.history.eval --seed 7 --out "$OUT/history"
```

(use whatever variable the script already uses for the interpreter; the explorer saw `./.venv/bin/python`).

- [ ] **Step 2: Wire CI**

In `.github/workflows/ci.yml`:
- `test` job, after "Evaluation gate": a step `Trust history gate` running `uv run python -m benchmark.history.eval --seed 7 --out /tmp/history-gate`.
- `postgres` job list: add, before the quoted `test_gcs_and_migrations.py::…` node ids:

```
                        tests/test_trust_schema.py \
                        tests/test_trust_graph_store.py \
                        tests/test_trust_history_import.py \
                        tests/test_trust_routes.py \
                        tests/test_trust_findings.py \
                        tests/test_trust_entity_resolution.py \
                        tests/test_trust_graph_sync.py \
                        tests/test_trust_lineage.py \
                        tests/test_trust_worker.py \
                        tests/test_trust_providers.py \
                        tests/test_benchmark_history.py \
```

- [ ] **Step 3: Add a Postgres-only citation-trigger test**

Append to `tests/test_trust_findings.py`:

```python
import os

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

pg_only = pytest.mark.skipif(not (os.getenv("TEST_DATABASE_URL") or "").startswith("postgresql"), reason="trigger exists only on Postgres")


@pg_only
def test_db_refuses_a_finding_without_a_citation(env):
    """The Python writer refuses; so does the database, for any other writer."""
    from tests.pg_roles import bootstrapped_database

    with bootstrapped_database() as db, tenant_session(T, db.owner_url) as s:
        s.add(models.Tenant(id=T, name="Acme", slug="acme", status="active"))
        s.flush()
        s.execute(text("insert into graph_nodes (id, tenant_id, kind, natural_key, valid_from, created_at, updated_at) values ('n1', :t, 'invoice', 'i1', now(), now(), now())"), {"t": T})
        s.execute(text("insert into trust_findings (id, tenant_id, type, transaction_node, status, dedupe_key, created_at, updated_at) values ('f1', :t, 'X', 'n1', 'detected', 'k', now(), now())"), {"t": T})
        with pytest.raises(IntegrityError):
            s.commit()
```

Add its node id to the CI Postgres list only if the file is not already listed whole (it is listed whole above, so nothing more).

- [ ] **Step 4: Update the docs**

In `docs/PENDING.md` under "Production (Railway) — PAUSED" add nothing; add a new section:

```markdown
## v2 Trust layer

Spec: `docs/superpowers/specs/2026-10-14-v2-trust-layer-design.md`. Plan: `docs/superpowers/plans/2026-10-14-v2-wave0-foundation.md`.

- [x] Wave 0 foundation (this PR).
- [ ] AC-19 contract-clause coverage (≥ 60% of top-50-vendor invoices): not measurable until Wave 1's clause extraction; the harness records it as `null`.
- [ ] Wave 1 (parallel): Leakage · Payment Trust · Provenance v1 · GST/ITC v1 — one short spec each.
- [ ] Waves 2–4 per the spec §2.
```

In the spec, add under the title: `Status: Wave 0 implemented in PR <n> (2026-10-xx).` once the PR number exists.

- [ ] **Step 5: Run everything**

```bash
./.venv/bin/python -m pytest tests/ -q -p no:cacheprovider
./.venv/bin/python -m ruff check . && ./.venv/bin/python -m ruff format --check .
bash benchmark/harness/gates.sh benchmark/runs/gates-wave0
cd frontend && npm run lint && npx tsc --noEmit && npm run test:e2e
python3 scripts/audit_gate.py
```

Expected: full suite green (2827 + the new tests), ruff clean, 10/10 gates PASS, e2e 38/38, audit gate passes. With Postgres running locally, also run the CI Postgres list with `TEST_DATABASE_URL=postgresql+pg8000://postgres:postgres@localhost:55432/t`.

- [ ] **Step 6: Commit**

```bash
git add benchmark/harness/gates.sh .github/workflows/ci.yml docs/PENDING.md tests/test_trust_findings.py
git commit -m "Gate the trust history round-trip in CI and record the Wave 0 status

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Verification (end of Wave 0)

1. **Suite and lint:** the commands in Task 14 Step 5; CI green on both jobs.
2. **Migration round-trip on Postgres:** `alembic upgrade head` then `downgrade base` (CI does this); `python -m invoice_processing.db.restore_check --restored <owner url>` still prints PASS (every new table has `{table}_isolation`).
3. **Round-trip gate:** `python -m benchmark.history.eval --seed 7` → `invoice_vendor_coverage ≥ 0.95`, `po_backed_coverage ≥ 0.80`, `contract_clause_coverage: null`.
4. **Nothing user-visible changed:** `GET /api/settings` shows the two new keys with empty defaults; the sheet, KPIs, approvals and e2e are untouched (38/38).
5. **Manual smoke (local SQLite, dev bypass):** start the API with `AUTH_DISABLED=1 ENV=local`, `POST /api/trust/history/mapping` with a 3-row CSV → proposal; `POST /api/trust/history/import` → 201 with one bad row named; run the worker once → `trust-synced`; `python -c` call `lineage()` on the imported invoice → `po: no_evidence` etc.
6. **Review:** whole-branch review on the `fable` model; one fix pass including minors; then PR `v2/wave0-foundation` → `main` with the spec and plan linked. Merge only on the user's word. Production stays paused and disconnected, so the merge deploys nothing.

## Self-review notes

- **Spec coverage:** §4.1 import → T5/T6; §4.2 graph, time-travel, builder, accessor, lineage, entity resolution → T4/T8/T9/T10; §4.3 tables → T1; §4.4 findings → T7; §4.5 decide → T3; §4.6 providers → T12; §4.7 harness + import family → T13/T14; §3.1.5 modes → T2; §3.1 hook → T11; §4.8 "no UI" honoured (routes only). Spec §4.2 says lineage is "by recursive query": implemented as a bounded walk over store queries with the CTE named as the upgrade path (ADR-12 threshold), noted in the module docstring.
- **Type consistency:** `GraphStore.upsert_node/upsert_edge/node/open_node/nodes/edges/nodes_by_ref` signatures are used identically in T8–T13; `LinkStore.resolved_id/open_links/propose_or_link` as defined in T8; `HistoryImporter.import_file` kwargs match T6/T13; `FindingSpec`/`Citation` match T7; `decide(state, questions, decider)` and `FakeDecider(answers, probability, fail)` match T3 everywhere.
- **Checked against the repo while planning:** `Document` requires only `storage_key` + `sha256`; `po_matching.normalize_po_number` exists at line 154; the two `test_worker.py` empty-queue assertions after a document job are at lines 259 and 1269 (T11 Step 5 says how to change them).
