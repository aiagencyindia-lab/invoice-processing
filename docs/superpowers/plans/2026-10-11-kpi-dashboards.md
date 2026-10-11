# KPI Dashboards Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `/kpis` page that shows an AP manager, for a chosen window, how many invoices arrived, how many cleared untouched, why the rest needed a person, how long decisions took, how often receiving held an invoice, and the model spend — with a drill-down to the invoices behind each reason.

**Architecture:** One new typed column `invoices.decided_at` (filled by the existing projection), one read-only repository `KpiRepository` that selects one row per invoice in the window from typed columns (no JSON artifacts loaded) and computes the numbers in Python, two read-only routes (`/api/kpis`, `/api/kpis/cases`), and one Next.js page reusing the dashboard tile. The projection also stops duplicating `pipeline_runs` rows, which is the root cause of overcounted cost.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic, SQLite locally / Postgres in CI; Next.js 16, React 19, Playwright. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-11-kpi-dashboards-design.md` (amended in Task 1; read it before every task).

## Global Constraints

- Always run Python as `./.venv/bin/python` from the repo root. Tests: `./.venv/bin/python -m pytest <file> -q -p no:cacheprovider`. Lint: `./.venv/bin/python -m ruff check . && ./.venv/bin/python -m ruff format .`
- No endpoint takes `tenant_id`; the tenant is always `principal.tenant_id`. Every repository query filters `tenant_id` explicitly on every table it touches, inside `tenant_session(tenant_id, self.url)`.
- DB-only routes refuse with 503 whose text contains **"needs a database"** when `service._database_configured()` is false.
- Every new route gets a cross-tenant probe and a `PROBED` entry in `tests/test_tenant_isolation_api.py`; its module goes in `test_no_endpoint_reads_a_tenant_from_the_request`.
- New DB-backed test files go on the CI Postgres list in `.github/workflows/ci.yml` (the `DB-backed tests against Postgres` step).
- Never edit an applied migration. New head revises `d8e1f2a3b4c5`.
- Comments explain *why*. Deliberate shortcuts carry `# ponytail: <ceiling> — <upgrade path>`.
- Commit messages: imperative subject that says why; body; trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Every rate is `null` when its denominator is 0. Never 0%.
- SQLite returns naive datetimes for `DateTime(timezone=True)` columns; treat naive as UTC before subtracting.

## Review Focus

1. **An invoice whose approval chain is still pending but whose `decided_at` was set at projection (touchless path).** Expected: counted as open, not decided, not touchless. Test in Task 3 (`test_a_pending_chain_is_open_even_with_a_touchless_decided_at`).
2. **A workflow `decided_at` that is naive, has a `Z` suffix, or is garbage.** Expected: `Z` and naive parse as UTC; garbage leaves the column null and never raises. Test in Task 2 (`test_decided_at_parses_z_and_naive_and_ignores_garbage`).
3. **A retried (re-projected) case after a person decided it.** Expected: the worker's artifacts carry no `workflow`, so `decided_at` and `human_decision` must survive the re-projection. Test in Task 2 (`test_reprojecting_from_pipeline_output_keeps_the_human_decision_time`).
4. **A legacy invoice with several `pipeline_runs` rows.** Expected: cost uses the latest row, not the sum. Test in Task 3 (`test_cost_uses_the_latest_run_when_duplicates_exist`).
5. **A tenant with invoices but none in the window.** Expected: `received` 0, every rate null, page says "No invoices received in this window". Tests in Task 3 (`test_an_empty_window_has_null_rates`) and Task 5 (manual browser check with `?days=7` on the manual stack).

---

### Task 1: Amend the spec for what the code can actually see

**Files:**
- Modify: `docs/superpowers/specs/2026-10-11-kpi-dashboards-design.md`

**Interfaces:** none (documentation only).

- [ ] **Step 1: Edit §1 "Exception" buckets** — append a sixth bucket after `manual_decision`:

```
  6. `other` — an exception matching none of the above (for example an invoice
     projected before `decided_at` existed). Counted so the buckets always sum
     to the exceptions; a non-zero `other` is a data-quality signal, not a
     category to design around.
```

- [ ] **Step 2: Edit §1 "Received"** to read:

```
- **Received**: `documents.created_at` (already exposed as `received_at`),
  falling back to `invoices.created_at` when the case has no document row
  (seeded and legacy cases). Left join, never inner.
```

- [ ] **Step 3: Edit §1 "Decided"** — add after the three sub-bullets:

```
  The worker routes the approval chain *after* projecting the case, so a
  touchless `decided_at` can exist before a chain does. The query therefore
  treats an invoice as decided only when `decided_at` is set and either a
  person decided it (`human_decision` not null) or it has no approval chain.
```

- [ ] **Step 4: Replace §3 "Query"** with:

```
One class, `KpiRepository(url)`, in `db/repositories.py`, with two methods:

- `summary(tenant_id, since: datetime | None) -> dict`
- `cases(tenant_id, since, reason) -> dict` — `{"case_ids": [...], "truncated": bool}`,
  at most 500 ids, newest received first.

Both call one private `_rows(session, tenant_id, since)` that selects **one row
per invoice in the window** from typed columns only: `case_id`, received
(`coalesce(documents.created_at, invoices.created_at)`), `decided_at`,
`human_decision`, `decision`, `routing`, `po_number`, `held` (EXISTS a
`findings` row with `step='2.3'` and `passed=false`), `chain` (EXISTS an
`approval_steps` row for the case) and `cost_usd` (the latest `pipeline_runs`
row for the invoice, as a scalar subquery). Scans are excluded with the same
`~exists(documents.kind not null and != 'invoice')` the sheet uses. The window
filter is on received. The numbers — counts, buckets, nearest-rank
percentiles, open age, hold rate, cost — are then computed in Python over
those rows. No artifact JSON is loaded.

ponytail: one row per invoice in the window; aggregate in SQL if a window ever
exceeds ~100k invoices.

The bucket rule is one pure function `exception_bucket(decision, held,
routing, chain, human_decision) -> str` and the percentile is one pure
function `percentile(values, p)`, both in the repository module and
unit-tested directly.
```

- [ ] **Step 5: Edit §4 "API"** — replace the paragraph starting "DB-only:" with:

```
DB-only: without a database both routes return 503 whose text contains
"needs a database" (same guard as `api/sheet.py`). Read-only, so no audit
event and no memoised accessor: the repository is constructed per request,
as the sheet's is.

Second route, for the drill-down:

    GET /api/kpis/cases?reason=<bucket>&days=30   viewer+
    → {"case_ids": ["..."], "truncated": false}

`reason` must be one of the six buckets, else 422.
```

- [ ] **Step 6: Edit §5 "UI"** — replace the `/invoices` bullet with:

```
- `/invoices` accepts `?reason=<bucket>&days=N`. When present it fetches
  `/api/kpis/cases` once and shows only the queue items whose case id is in
  the answer, with a banner "Showing N invoices needing a person: <reason>"
  and a "Show all" link back to `/invoices`.
```

- [ ] **Step 7: Commit**

```bash
git add docs/superpowers/specs/2026-10-11-kpi-dashboards-design.md
git commit -m "Spell out what the KPI query can see

Seeded and legacy cases have no document row, the approval chain is routed
after projection, and the invoice list cannot compute a reason bucket on its
own. The spec now says how each is handled instead of leaving it to the code.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: `invoices.decided_at` — model, migration with backfill, projection, one run row per invoice

**Files:**
- Modify: `invoice_processing/db/models.py` (class `Invoice`, `__table_args__` ~L162-172 and columns ~L220-224)
- Create: `invoice_processing/db/migrations/versions/a7b3c9d2e4f6_invoice_decided_at.py`
- Modify: `invoice_processing/case_projection.py` (after `agreement`, ~L124)
- Modify: `invoice_processing/db/repositories.py` (`PgCaseRepository.project_case`, ~L195-267)
- Test: `tests/test_db_repositories.py` (class `TestProjection`, ~L460-545), `tests/test_gcs_and_migrations.py` (class `TestMigration`, ~L240)

**Interfaces:**
- Produces: `Invoice.decided_at: datetime | None` column; `case_projection.parse_timestamp(value) -> datetime | None`; `case_projection.decided_at(artifacts) -> datetime | None`; `case_projection.is_touchless(artifacts) -> bool`; `case_projection.classify_verdict(text) -> "accept" | "reject" | "undecided"` (extracted from `machine_verdict`, which now calls it).

- [ ] **Step 1: Write the failing projection tests** — append to `class TestProjection` in `tests/test_db_repositories.py` (the class has `_project(self, db, case_id, artifacts)` and the module-level `ARTIFACTS` dict with `_routing: "REVIEW"`, verdict REJECT, and an `audit_log`):

```python
    def test_a_human_decision_time_lands_in_its_own_column(self, db):
        decided = {
            **ARTIFACTS,
            "workflow": {
                "decision": "approve",
                "decided_by": "a@acme.test",
                "decided_at": "2026-10-01T10:00:00+00:00",
            },
        }
        self._project(db, artifacts=decided)
        with tenant_session(ACME, db) as session:
            invoice = session.query(Invoice).filter_by(case_id="case-1").one()
            assert invoice.decided_at is not None
            assert invoice.decided_at.replace(tzinfo=None) == datetime(
                2026, 10, 1, 10, 0, 0
            )

    def test_a_touchless_case_is_decided_when_it_is_projected(self, db):
        touchless = {
            "extraction": {
                "invoice": {
                    **ARTIFACTS["extraction"]["invoice"],
                    "_routing": "AUTO",
                }
            },
            "decision": {"final_decision": "ACCEPT"},
        }
        self._project(db, case_id="auto-1", artifacts=touchless)
        with tenant_session(ACME, db) as session:
            invoice = session.query(Invoice).filter_by(case_id="auto-1").one()
            assert invoice.decided_at is not None
            assert invoice.human_decision is None

    def test_a_held_auto_case_is_not_touchless(self, db):
        held = {
            "extraction": {
                "invoice": {
                    **ARTIFACTS["extraction"]["invoice"],
                    "_routing": "AUTO",
                }
            },
            "decision": {"final_decision": "ACCEPT"},
            "phase2": {
                "validations": [
                    {
                        "step": "2.3",
                        "rule": "Three-way",
                        "passed": False,
                        "informational": True,
                    }
                ]
            },
        }
        self._project(db, case_id="held-1", artifacts=held)
        with tenant_session(ACME, db) as session:
            invoice = session.query(Invoice).filter_by(case_id="held-1").one()
            assert invoice.decided_at is None

    def test_a_review_case_stays_undecided(self, db):
        self._project(db)
        with tenant_session(ACME, db) as session:
            invoice = session.query(Invoice).filter_by(case_id="case-1").one()
            assert invoice.decided_at is None

    def test_reprojecting_from_pipeline_output_keeps_the_human_decision_time(
        self, db
    ):
        """A retry re-projects from the worker's files, which never hold the
        workflow artifact. That must not erase when a person decided."""
        decided = {
            **ARTIFACTS,
            "workflow": {
                "decision": "reject",
                "decided_by": "a@acme.test",
                "decided_at": "2026-10-01T10:00:00+00:00",
            },
        }
        self._project(db, artifacts=decided)
        self._project(db, artifacts=ARTIFACTS)
        with tenant_session(ACME, db) as session:
            invoice = session.query(Invoice).filter_by(case_id="case-1").one()
            assert invoice.decided_at is not None
            assert invoice.human_decision == "reject"

    def test_decided_at_parses_z_and_naive_and_ignores_garbage(self):
        from invoice_processing.case_projection import parse_timestamp

        z = parse_timestamp("2026-10-01T10:00:00Z")
        naive = parse_timestamp("2026-10-01T10:00:00")
        assert z is not None and z.utcoffset().total_seconds() == 0
        assert naive is not None and naive.utcoffset().total_seconds() == 0
        assert parse_timestamp("yesterday") is None
        assert parse_timestamp(None) is None
        assert parse_timestamp(1700000000) is None

    def test_reprojecting_leaves_exactly_one_run_row(self, db):
        """Every approval, correction, GL coding and rematch re-projects the
        case; each used to add another pipeline_runs row, so summing cost
        counted the same run five times."""
        self._project(db)
        self._project(db)
        self._project(db)
        with tenant_session(ACME, db) as session:
            assert session.query(PipelineRun).count() == 1
```

Add `from datetime import datetime` to the imports at the top of the file.

- [ ] **Step 2: Run them to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_db_repositories.py -q -p no:cacheprovider -k "decided or touchless or undecided or run_row or garbage"`
Expected: FAIL — `Invoice` has no attribute `decided_at`; `parse_timestamp` import error; run count is 3.

- [ ] **Step 3: Add the column and index to the model** — in `invoice_processing/db/models.py`, class `Invoice`:

In `__table_args__`, after `Index("ix_invoices_agreement", ...)`:
```python
        # The KPI window: "decided in the last 30 days" must not scan every
        # row's JSON to find out.
        Index("ix_invoices_tenant_decided", "tenant_id", "decided_at"),
```
After the `posted_at` column:
```python
    # When the invoice stopped needing anyone: a person's decision time from
    # the workflow artifact, or the projection time for a touchless invoice.
    # Null while someone still has to act.
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
```

- [ ] **Step 4: Write the migration** — create `invoice_processing/db/migrations/versions/a7b3c9d2e4f6_invoice_decided_at.py`:

```python
"""invoices.decided_at: when an invoice stopped needing anyone

The KPI page asks "how long did decisions take last month". The answer lived
only inside the workflow JSON, so the question meant reading every row's
artifacts. One typed column, backfilled from that JSON for the rows that
already have a decision.

Revision ID: a7b3c9d2e4f6
Revises: d8e1f2a3b4c5
Create Date: 2026-10-11
"""

from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision: str = "a7b3c9d2e4f6"
down_revision: str | None = "d8e1f2a3b4c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# No new table, so nothing to protect with RLS here.
RLS_TABLES: list[str] = []


def _parse(value) -> datetime | None:
    # Same tolerance as case_projection.parse_timestamp, repeated rather than
    # imported: a migration must not change meaning when app code does.
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def upgrade() -> None:
    op.add_column(
        "invoices", sa.Column("decided_at", sa.DateTime(timezone=True))
    )
    op.create_index(
        "ix_invoices_tenant_decided", "invoices", ["tenant_id", "decided_at"]
    )

    invoices = sa.table(
        "invoices",
        sa.column("id", sa.String),
        sa.column("fields", sa.JSON),
        sa.column("decided_at", sa.DateTime(timezone=True)),
    )
    bind = op.get_bind()
    rows = bind.execute(sa.select(invoices.c.id, invoices.c.fields)).all()
    for row_id, fields in rows:
        workflow = (fields or {}).get("workflow") or {}
        if workflow.get("decision") not in ("approve", "reject"):
            continue
        decided = _parse(workflow.get("decided_at"))
        if decided is None:
            continue
        bind.execute(
            invoices.update()
            .where(invoices.c.id == row_id)
            .values(decided_at=decided)
        )


def downgrade() -> None:
    op.drop_index("ix_invoices_tenant_decided", table_name="invoices")
    with op.batch_alter_table("invoices") as batch:
        batch.drop_column("decided_at")
```

- [ ] **Step 5: Add the pure helpers to `case_projection.py`** — insert after `agreement()` (~L124). Also refactor `machine_verdict` so its classification is reusable:

```python
def classify_verdict(text) -> str:
    """accept | reject | undecided, from whatever the pipeline wrote."""
    verdict = str(text or "").strip().upper()
    if verdict in _ACCEPTED:
        return "accept"
    if verdict in _REJECTED:
        return "reject"
    # SET_ASIDE, "To Verify", an unfinished pipeline: nothing to disagree with.
    return "undecided"
```

Change the tail of `machine_verdict` from the `verdict = (...)` assignment onward to:
```python
    return classify_verdict(
        decision.get("final_decision")
        # The flattened rule-evaluation context spells it this way. Both
        # are read here so one answer to "what did the machine decide"
        # serves the sheet, the disagreement column and the impact report.
        or decision.get("decision")
        or postprocessing.get("Invoice Status")
    )
```

Then add:
```python
def parse_timestamp(value) -> datetime | None:
    """An ISO timestamp as an aware datetime, or None.

    Tolerant on purpose: the workflow artifact was written by several
    versions of this code, some with a `Z`, some naive. Garbage is None, not
    an exception -- a KPI must never stop a projection.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def decided_at(artifacts: dict) -> datetime | None:
    """When a person decided this invoice; None if nobody has."""
    workflow = artifacts.get("workflow") or {}
    if workflow.get("decision") not in ("approve", "reject"):
        return None
    return parse_timestamp(workflow.get("decided_at"))


def is_touchless(artifacts: dict) -> bool:
    """Nothing asked for a person: confident extraction, the rulebook
    accepted, and receiving did not hold it."""
    if extraction(artifacts).get("_routing") != "AUTO":
        return False
    if machine_verdict(artifacts) != "accept":
        return False
    return not any(
        f["step"] == "2.3" and f["passed"] is False for f in findings(artifacts)
    )
```

The module currently imports nothing from `datetime`; add `from datetime import datetime, timezone` next to `from decimal import Decimal, InvalidOperation`.

- [ ] **Step 6: Update `project_case`** in `invoice_processing/db/repositories.py`. After the `for column, value in columns.items(): setattr(...)` loop add:

```python
            # The human decision time wins. A touchless invoice is decided
            # the moment it is projected, and keeps that time on retries so a
            # re-run does not move it. Nothing here ever writes None: the
            # worker re-projects from files that never hold the workflow
            # artifact, and clearing the column would erase a real decision.
            decided = case_projection.decided_at(artifacts)
            if decided is not None:
                invoice.decided_at = decided
            elif invoice.decided_at is None and case_projection.is_touchless(
                artifacts
            ):
                invoice.decided_at = datetime.now(timezone.utc)
```

Replace the `run = case_projection.run(artifacts)` block with:
```python
            # One run row per invoice. Every approval, correction, GL coding
            # and rematch re-projects; appending on each made the same cost
            # count several times over.
            session.query(PipelineRun).filter(
                PipelineRun.tenant_id == ctx.tenant_id,
                PipelineRun.invoice_id == invoice.id,
            ).delete(synchronize_session=False)
            run = case_projection.run(artifacts)
            if run is not None:
                session.add(
                    PipelineRun(
                        tenant_id=ctx.tenant_id,
                        invoice_id=invoice.id,
                        master_data_version=None,
                        **run,
                    )
                )
```

- [ ] **Step 7: Run the projection tests**

Run: `./.venv/bin/python -m pytest tests/test_db_repositories.py -q -p no:cacheprovider`
Expected: all PASS.

- [ ] **Step 8: Write the failing migration test** — in `tests/test_gcs_and_migrations.py`, class `TestMigration`, after `test_upgrade_then_downgrade` (the file has `_alembic(args, db)` returning a `CompletedProcess`, and imports `sqlite3`, `json`):

```python
    def test_decided_at_is_backfilled_from_the_workflow_artifact(self, tmp_path):
        """History must not start blank: a decision made last month has a
        time, and the new column should know it."""
        db = tmp_path / "b.db"
        assert _alembic("upgrade d8e1f2a3b4c5", db).returncode == 0

        conn = sqlite3.connect(db)
        conn.execute(
            "insert into tenants (id, slug, name, region, status, settings, "
            "created_at, updated_at) values ('t1', 't1', 'T1', 'au', 'active', "
            "'{}', '2026-01-01', '2026-01-01')"
        )
        rows = [
            ("decided", {"workflow": {"decision": "approve",
                                      "decided_at": "2026-09-30T08:00:00Z"}}),
            ("pending", {"workflow": {}}),
            ("garbage", {"workflow": {"decision": "reject",
                                      "decided_at": "last tuesday"}}),
        ]
        for case_id, fields in rows:
            conn.execute(
                "insert into invoices (id, tenant_id, case_id, status, fields, "
                "created_at, updated_at) values (?, 't1', ?, 'processed', ?, "
                "'2026-09-01', '2026-09-01')",
                (case_id, case_id, json.dumps(fields)),
            )
        conn.commit()
        conn.close()

        up = _alembic("upgrade head", db)
        assert up.returncode == 0, up.stderr
        got = dict(
            sqlite3.connect(db).execute(
                "select case_id, decided_at from invoices"
            ).fetchall()
        )
        assert got["decided"] is not None and got["decided"].startswith("2026-09-30")
        assert got["pending"] is None
        assert got["garbage"] is None

        assert _alembic("downgrade d8e1f2a3b4c5", db).returncode == 0
```

If `json` is not imported in that file, add `import json`.

- [ ] **Step 9: Run migration tests**

Run: `./.venv/bin/python -m pytest tests/test_gcs_and_migrations.py -q -p no:cacheprovider -k "Migration"`
Expected: all PASS, including `test_upgrade_then_downgrade`, `test_migration_protects_exactly_the_tables_it_creates` (the new migration has an empty `RLS_TABLES`; if that test's parametrize enumerates every revision, add `("a7b3c9d2e4f6", 0)` to it) and the new backfill test.

- [ ] **Step 10: Run the full Python suite and lint**

Run: `./.venv/bin/python -m pytest tests/ -q -p no:cacheprovider && ./.venv/bin/python -m ruff check . && ./.venv/bin/python -m ruff format .`
Expected: all pass (2428 + the new ones), ruff clean.

- [ ] **Step 11: Commit**

```bash
git add invoice_processing/db/models.py invoice_processing/db/migrations/versions/a7b3c9d2e4f6_invoice_decided_at.py invoice_processing/case_projection.py invoice_processing/db/repositories.py tests/test_db_repositories.py tests/test_gcs_and_migrations.py
git commit -m "Record when an invoice stopped needing anyone

Cycle time needs a decided time per invoice and it only existed inside the
workflow JSON. One typed column, filled by the projection and backfilled by
the migration. The projection also keeps one pipeline_runs row per invoice:
every re-projection appended another, so summed cost was a multiple of the
truth.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: `KpiRepository`

**Files:**
- Modify: `invoice_processing/db/repositories.py` (append a new section before `# Approved-invoice sheet`, ~L2745)
- Create: `tests/test_kpi_repository.py`
- Modify: `.github/workflows/ci.yml` (add `tests/test_kpi_repository.py \` to the Postgres list, ~L157)

**Interfaces:**
- Consumes: `Invoice.decided_at`, `case_projection.classify_verdict` (Task 2); models `Invoice`, `Document`, `Finding`, `ApprovalStep`, `PipelineRun`.
- Produces:
  - `BUCKETS = ("rejected_by_rules", "three_way_hold", "low_confidence", "approval_required", "manual_decision", "other")`
  - `percentile(values: list[float], p: float) -> float | None` (nearest rank, rounded to 2 dp)
  - `exception_bucket(decision: str | None, held: bool, routing: str | None, chain: bool, human_decision: str | None) -> str`
  - `KpiRepository(url: str | None = None)` with `summary(tenant_id, since) -> dict` and `cases(tenant_id, since, reason) -> dict`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_kpi_repository.py`:

```python
"""The KPI numbers: computed from typed columns, honest about what they skip."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from invoice_processing.db.models import (
    ApprovalStep,
    Base,
    Document,
    Finding,
    Invoice,
    PipelineRun,
    Tenant,
)
from invoice_processing.db.repositories import (
    BUCKETS,
    KpiRepository,
    exception_bucket,
    percentile,
)
from invoice_processing.db.session import create_all, get_engine, tenant_session

ACME = "tenant-acme"
GLOBEX = "tenant-globex"
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


def days_ago(n: float) -> datetime:
    return NOW - timedelta(days=n)


@pytest.fixture
def db(scratch_db):
    url = scratch_db("kpi.db")
    engine = get_engine(url)
    Base.metadata.drop_all(engine)
    create_all(url)
    for tenant_id, slug in ((ACME, "acme"), (GLOBEX, "globex")):
        with tenant_session(tenant_id, url) as session:
            session.add(Tenant(id=tenant_id, slug=slug, name=slug))
    return url


def seed(
    session,
    case_id,
    *,
    tenant_id=ACME,
    received=None,
    decided=None,
    human=None,
    decision="ACCEPT",
    routing="AUTO",
    po=None,
    held=False,
    chain=False,
    cost=None,
    kind=None,
    document=True,
):
    """One invoice with exactly the facts the KPI query reads."""
    received = received or days_ago(1)
    if document:
        session.add(
            Document(
                id=case_id,
                tenant_id=tenant_id,
                storage_key=f"k/{case_id}",
                sha256="0" * 64,
                created_at=received,
                kind=kind,
            )
        )
    invoice = Invoice(
        tenant_id=tenant_id,
        case_id=case_id,
        document_id=case_id if document else None,
        created_at=received,
        decided_at=decided,
        human_decision=human,
        decision=decision,
        routing=routing,
        po_number=po,
        fields={},
    )
    session.add(invoice)
    session.flush()
    if held:
        session.add(
            Finding(
                tenant_id=tenant_id,
                invoice_id=invoice.id,
                phase="phase2",
                step="2.3",
                passed=False,
            )
        )
    if chain:
        session.add(
            ApprovalStep(
                tenant_id=tenant_id,
                case_id=case_id,
                position=1,
                assignee_kind="user",
                assignee_value="a@acme.test",
                status="pending",
            )
        )
    if cost is not None:
        for i, value in enumerate(cost if isinstance(cost, list) else [cost]):
            session.add(
                PipelineRun(
                    tenant_id=tenant_id,
                    invoice_id=invoice.id,
                    cost_usd=value,
                    created_at=received + timedelta(minutes=i),
                )
            )
    return invoice


class TestPureParts:
    def test_percentile_is_nearest_rank(self):
        assert percentile([1, 2, 3, 4], 0.5) == 2
        assert percentile([1, 2, 3, 4], 0.9) == 4
        assert percentile([5], 0.5) == 5
        assert percentile([], 0.5) is None

    @pytest.mark.parametrize(
        ("facts", "bucket"),
        [
            (dict(decision="REJECT", held=True, routing="REVIEW", chain=True, human_decision="approve"), "rejected_by_rules"),
            (dict(decision="ACCEPT", held=True, routing="REVIEW", chain=True, human_decision=None), "three_way_hold"),
            (dict(decision="ACCEPT", held=False, routing="REVIEW", chain=True, human_decision=None), "low_confidence"),
            (dict(decision="ACCEPT", held=False, routing="AUTO", chain=True, human_decision="approve"), "approval_required"),
            (dict(decision="ACCEPT", held=False, routing="AUTO", chain=False, human_decision="reject"), "manual_decision"),
            (dict(decision="ACCEPT", held=False, routing="AUTO", chain=False, human_decision=None), "other"),
            (dict(decision=None, held=False, routing=None, chain=False, human_decision=None), "other"),
        ],
    )
    def test_bucket_order_is_first_match_wins(self, facts, bucket):
        assert exception_bucket(**facts) == bucket
        assert bucket in BUCKETS


class TestSummary:
    def test_counts_touchless_and_every_bucket(self, db):
        with tenant_session(ACME, db) as session:
            seed(session, "auto", decided=days_ago(0.9))
            seed(session, "rej", decision="REJECT", routing="REVIEW")
            seed(session, "held", routing="REVIEW", po="PO-1", held=True)
            seed(session, "low", routing="REVIEW")
            seed(session, "chain", chain=True, decided=days_ago(0.9))
            seed(session, "manual", human="approve", decided=days_ago(0.5))
            seed(session, "legacy")  # AUTO, accept, never decided: other
        body = KpiRepository(db).summary(ACME, days_ago(30), now=NOW)
        assert body["received"] == 7
        assert body["touchless"] == 1
        assert body["decided"] == 2  # auto + manual; the chain is pending
        assert body["touchless_rate"] == pytest.approx(1 / 7, abs=0.001)
        assert body["exception_rate"] == pytest.approx(6 / 7, abs=0.001)
        assert body["exceptions"] == {
            "rejected_by_rules": 1,
            "three_way_hold": 1,
            "low_confidence": 1,
            "approval_required": 1,
            "manual_decision": 1,
            "other": 1,
        }

    def test_a_pending_chain_is_open_even_with_a_touchless_decided_at(self, db):
        """The worker projects before it routes the chain, so a touchless
        decided_at can exist while an approver still has to act."""
        with tenant_session(ACME, db) as session:
            seed(session, "chain", chain=True, decided=days_ago(0.9))
        body = KpiRepository(db).summary(ACME, None, now=NOW)
        assert body["decided"] == 0
        assert body["touchless"] == 0
        assert body["open"]["count"] == 1

    def test_cycle_time_percentiles_and_bad_clocks(self, db):
        with tenant_session(ACME, db) as session:
            seed(session, "a", received=days_ago(10), decided=days_ago(9.5), human="approve")
            seed(session, "b", received=days_ago(10), decided=days_ago(9), human="approve")
            seed(session, "c", received=days_ago(10), decided=days_ago(8), human="approve")
            # Decided before it arrived: a clock problem, never a fast decision.
            seed(session, "bad", received=days_ago(1), decided=days_ago(2), human="approve")
        body = KpiRepository(db).summary(ACME, None, now=NOW)
        assert body["cycle_hours"] == {"median": 24.0, "p90": 48.0, "skipped": 1}

    def test_open_age_is_the_oldest(self, db):
        with tenant_session(ACME, db) as session:
            seed(session, "young", received=days_ago(1), routing="REVIEW")
            seed(session, "old", received=days_ago(4), routing="REVIEW")
        body = KpiRepository(db).summary(ACME, None, now=NOW)
        assert body["open"] == {"count": 2, "oldest_hours": 96.0}

    def test_hold_rate_is_over_po_backed_invoices(self, db):
        with tenant_session(ACME, db) as session:
            seed(session, "po-held", po="PO-1", routing="REVIEW", held=True)
            seed(session, "po-ok", po="PO-2", decided=days_ago(0.5))
            seed(session, "non-po", decided=days_ago(0.5))
        body = KpiRepository(db).summary(ACME, None, now=NOW)
        assert body["three_way"] == {"po_invoices": 2, "held": 1, "hold_rate": 0.5}

    def test_cost_uses_the_latest_run_when_duplicates_exist(self, db):
        with tenant_session(ACME, db) as session:
            seed(session, "dup", cost=[0.01, 0.01, 0.02], decided=days_ago(0.5))
            seed(session, "one", cost=0.04, decided=days_ago(0.5))
            seed(session, "none", decided=days_ago(0.5))
        body = KpiRepository(db).summary(ACME, None, now=NOW)
        assert body["ai_cost"] == {"per_invoice_usd": 0.03, "total_usd": 0.06}

    def test_window_scans_and_other_tenants_are_excluded(self, db):
        with tenant_session(ACME, db) as session:
            seed(session, "in", received=days_ago(5), decided=days_ago(4))
            seed(session, "out", received=days_ago(40), decided=days_ago(39))
            seed(session, "scan", kind="goods_receipt", decision="SUPPORTING")
            seed(session, "nodoc", document=False, decided=days_ago(0.5))
        with tenant_session(GLOBEX, db) as session:
            seed(session, "theirs", tenant_id=GLOBEX, decided=days_ago(0.5))
        body = KpiRepository(db).summary(ACME, days_ago(30), now=NOW)
        assert body["received"] == 2  # "in" and "nodoc"
        assert KpiRepository(db).summary(GLOBEX, None, now=NOW)["received"] == 1

    def test_an_empty_window_has_null_rates(self, db):
        with tenant_session(ACME, db) as session:
            seed(session, "old", received=days_ago(40))
        body = KpiRepository(db).summary(ACME, days_ago(7), now=NOW)
        assert body["received"] == 0
        assert body["touchless_rate"] is None
        assert body["exception_rate"] is None
        assert body["cycle_hours"] == {"median": None, "p90": None, "skipped": 0}
        assert body["open"] == {"count": 0, "oldest_hours": None}
        assert body["three_way"] == {"po_invoices": 0, "held": 0, "hold_rate": None}
        assert body["ai_cost"] == {"per_invoice_usd": None, "total_usd": 0.0}


class TestCases:
    def test_lists_the_case_ids_behind_a_reason_newest_first(self, db):
        with tenant_session(ACME, db) as session:
            seed(session, "older", received=days_ago(3), routing="REVIEW")
            seed(session, "newer", received=days_ago(2), routing="REVIEW")
            seed(session, "auto", decided=days_ago(0.5))
        body = KpiRepository(db).cases(ACME, None, "low_confidence", now=NOW)
        assert body == {"case_ids": ["newer", "older"], "truncated": False}

    def test_is_capped(self, db, monkeypatch):
        from invoice_processing.db import repositories

        monkeypatch.setattr(repositories, "KPI_CASES_LIMIT", 2)
        with tenant_session(ACME, db) as session:
            for i in range(3):
                seed(session, f"c{i}", received=days_ago(i + 1), routing="REVIEW")
        body = KpiRepository(db).cases(ACME, None, "low_confidence", now=NOW)
        assert len(body["case_ids"]) == 2
        assert body["truncated"] is True
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/bin/python -m pytest tests/test_kpi_repository.py -q -p no:cacheprovider`
Expected: FAIL — `ImportError: cannot import name 'KpiRepository'`.

- [ ] **Step 3: Implement** — in `invoice_processing/db/repositories.py`, add `import math` to the imports, make sure `ApprovalStep` is in the `from .models import (...)` list, and insert this section before `# Approved-invoice sheet`:

```python
# ---------------------------------------------------------------------------
# KPIs
# ---------------------------------------------------------------------------

# Why an invoice needed a person, first match wins. "other" exists so the
# buckets always add up to the exceptions; a non-zero "other" is a data
# problem to look at, not a category to design around.
BUCKETS = (
    "rejected_by_rules",
    "three_way_hold",
    "low_confidence",
    "approval_required",
    "manual_decision",
    "other",
)
KPI_CASES_LIMIT = 500


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile; None of nothing."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p * len(ordered)))
    return round(float(ordered[rank - 1]), 2)


def exception_bucket(
    decision: str | None,
    held: bool,
    routing: str | None,
    chain: bool,
    human_decision: str | None,
) -> str:
    from .. import case_projection  # noqa: PLC0415

    if case_projection.classify_verdict(decision) == "reject":
        return "rejected_by_rules"
    if held:
        return "three_way_hold"
    if routing == "REVIEW":
        return "low_confidence"
    if chain:
        return "approval_required"
    if human_decision is not None:
        return "manual_decision"
    return "other"


def _aware(value: datetime | None) -> datetime | None:
    # SQLite hands back naive datetimes for timezone-aware columns.
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 3) if denominator else None


def _hours(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 3600


class KpiRepository:
    """The operating numbers an AP manager is judged on, over one window.

    Reads typed columns only -- never the artifact JSON -- so a month of
    invoices costs one indexed select. One row per invoice comes back and the
    arithmetic happens here; SQLite has no percentile function and the
    drill-down wants the same rows anyway.
    """

    # ponytail: one row per invoice in the window; aggregate in SQL if a
    # window ever exceeds ~100k invoices.

    def __init__(self, url: str | None = None):
        self.url = url

    def _rows(self, session, tenant_id: str, since: datetime | None) -> list:
        received = func.coalesce(Document.created_at, Invoice.created_at)
        held = exists(
            select(Finding.id).where(
                Finding.tenant_id == tenant_id,
                Finding.invoice_id == Invoice.id,
                Finding.step == "2.3",
                Finding.passed.is_(False),
            )
        )
        chain = exists(
            select(ApprovalStep.id).where(
                ApprovalStep.tenant_id == tenant_id,
                ApprovalStep.case_id == Invoice.case_id,
            )
        )
        # The latest run, not the sum: databases projected before the
        # one-row-per-invoice fix still hold duplicates of the same cost.
        cost = (
            select(PipelineRun.cost_usd)
            .where(
                PipelineRun.tenant_id == tenant_id,
                PipelineRun.invoice_id == Invoice.id,
            )
            .order_by(PipelineRun.created_at.desc())
            .limit(1)
            .scalar_subquery()
        )
        statement = (
            select(
                Invoice.case_id,
                received.label("received"),
                Invoice.decided_at,
                Invoice.human_decision,
                Invoice.decision,
                Invoice.routing,
                Invoice.po_number,
                held.label("held"),
                chain.label("chain"),
                cost.label("cost_usd"),
            )
            .select_from(Invoice)
            .outerjoin(
                Document,
                and_(
                    Document.tenant_id == tenant_id,
                    Document.id == Invoice.case_id,
                ),
            )
            .where(
                Invoice.tenant_id == tenant_id,
                # A scanned PO or receipt is never an invoice (same test as
                # the sheet).
                or_(Document.kind.is_(None), Document.kind == "invoice"),
            )
            .order_by(received.desc())
        )
        if since is not None:
            statement = statement.where(received >= since)
        return session.execute(statement).all()

    @staticmethod
    def _decided(row) -> bool:
        # The worker projects a touchless invoice as decided before it routes
        # the approval chain; a chain nobody has finished means it is not.
        return row.decided_at is not None and (
            row.human_decision is not None or not row.chain
        )

    def summary(
        self, tenant_id: str, since: datetime | None, now: datetime | None = None
    ) -> dict:
        now = now or datetime.now(timezone.utc)
        with tenant_session(tenant_id, self.url) as session:
            rows = self._rows(session, tenant_id, since)

        decided = [r for r in rows if self._decided(r)]
        touchless = [r for r in decided if r.human_decision is None]
        touchless_ids = {r.case_id for r in touchless}

        buckets = dict.fromkeys(BUCKETS, 0)
        for r in rows:
            if r.case_id not in touchless_ids:
                buckets[
                    exception_bucket(
                        r.decision, r.held, r.routing, r.chain, r.human_decision
                    )
                ] += 1

        cycle: list[float] = []
        skipped = 0
        for r in decided:
            start, end = _aware(r.received), _aware(r.decided_at)
            hours = _hours(start, end) if start and end else None
            if hours is None or hours < 0:
                skipped += 1  # fail closed: a bad clock never shortens the median
            else:
                cycle.append(hours)

        open_rows = [r for r in rows if not self._decided(r)]
        oldest = min((_aware(r.received) for r in open_rows if r.received), default=None)

        po_rows = [r for r in rows if r.po_number]
        held = [r for r in rows if r.held]

        costs = [float(r.cost_usd) for r in rows if r.cost_usd is not None]

        return {
            "received": len(rows),
            "decided": len(decided),
            "touchless": len(touchless),
            "touchless_rate": _rate(len(touchless), len(rows)),
            "exception_rate": _rate(len(rows) - len(touchless), len(rows)),
            "exceptions": buckets,
            "cycle_hours": {
                "median": percentile(cycle, 0.5),
                "p90": percentile(cycle, 0.9),
                "skipped": skipped,
            },
            "open": {
                "count": len(open_rows),
                "oldest_hours": round(_hours(oldest, now), 1) if oldest else None,
            },
            "three_way": {
                "po_invoices": len(po_rows),
                "held": len(held),
                "hold_rate": _rate(len(held), len(po_rows)),
            },
            "ai_cost": {
                "per_invoice_usd": round(sum(costs) / len(costs), 4) if costs else None,
                "total_usd": round(sum(costs), 4),
            },
        }

    def cases(
        self,
        tenant_id: str,
        since: datetime | None,
        reason: str,
        now: datetime | None = None,
    ) -> dict:
        """The invoices behind one bucket, newest received first."""
        with tenant_session(tenant_id, self.url) as session:
            rows = self._rows(session, tenant_id, since)
        matching = [
            r.case_id
            for r in rows
            if not (self._decided(r) and r.human_decision is None)
            and exception_bucket(
                r.decision, r.held, r.routing, r.chain, r.human_decision
            )
            == reason
        ]
        return {
            "case_ids": matching[:KPI_CASES_LIMIT],
            "truncated": len(matching) > KPI_CASES_LIMIT,
        }
```

(`now` is a keyword parameter purely so tests can pin the clock; routes never pass it. The three-way `held` tile counts holds among all invoices in the window, which equals holds among PO-backed invoices because step 2.3 only runs with a PO.)

- [ ] **Step 4: Run the tests**

Run: `./.venv/bin/python -m pytest tests/test_kpi_repository.py -q -p no:cacheprovider`
Expected: all PASS. If `test_cycle_time_percentiles_and_bad_clocks` is off by rounding, check `_aware` was applied to both ends.

- [ ] **Step 5: Add the file to the CI Postgres list** — in `.github/workflows/ci.yml`, in the `uv run pytest tests/test_sheet_api.py \` block, add a line `tests/test_kpi_repository.py \` after `tests/test_supporting_document_repository.py \`.

- [ ] **Step 6: Lint and full suite**

Run: `./.venv/bin/python -m ruff check . && ./.venv/bin/python -m ruff format . && ./.venv/bin/python -m pytest tests/ -q -p no:cacheprovider`
Expected: clean, all pass.

- [ ] **Step 7: Commit**

```bash
git add invoice_processing/db/repositories.py tests/test_kpi_repository.py .github/workflows/ci.yml
git commit -m "Compute the AP operating numbers from typed columns

Received, touchless, exception buckets, decision cycle time, open age,
three-way hold rate and model cost over one window, from one select per
tenant -- never from the artifact JSON. A pending approval chain keeps an
invoice open, a decided-before-received row is skipped and counted, and cost
reads the latest run so older duplicates do not inflate it.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Routes `/api/kpis` and `/api/kpis/cases`

**Files:**
- Create: `invoice_processing/api/kpi_routes.py`
- Modify: `invoice_processing/api/service.py` (router imports and `app.include_router` block, ~L1828-1843)
- Create: `tests/test_kpi_api.py`
- Modify: `tests/test_tenant_isolation_api.py` (`PROBED` ~L1227, the sheet DB-backed probe class ~L1007, `test_no_endpoint_reads_a_tenant_from_the_request` ~L1322 and the `from invoice_processing.api import (...)` block ~L35)
- Modify: `.github/workflows/ci.yml` (add `tests/test_kpi_api.py \`)

**Interfaces:**
- Consumes: `KpiRepository`, `BUCKETS` (Task 3); `service._database_configured()`; `Principal`, `current_principal` from `api/auth.py`.
- Produces: `GET /api/kpis?days=` → `{"window": {"days", "since"}, ...summary}`; `GET /api/kpis/cases?reason=&days=` → `{"case_ids", "truncated"}`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_kpi_api.py`:

```python
"""The KPI routes: who may read them, what windows exist, and that they
refuse plainly without a database."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from invoice_processing.api import auth
from invoice_processing.api.service import app
from invoice_processing.db.models import Base, Invoice, Tenant
from invoice_processing.db.session import create_all, get_engine, tenant_session

TENANT = "tenant-acme"
CLAIMS = {
    f"tok-{role}": {
        "user_id": f"u-{role}",
        "email": f"{role}@acme.test",
        "tenant_id": TENANT,
        "role": role,
    }
    for role in ("viewer", "clerk", "approver", "admin")
}


class FakeVerifier:
    def verify(self, token):
        if token not in CLAIMS:
            raise auth.AuthError("Invalid token")
        return CLAIMS[token]


@pytest.fixture
def db(scratch_db, monkeypatch):
    url = scratch_db("kpi-api.db")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("CLOUD_SQL_INSTANCE", raising=False)
    engine = get_engine(url)
    Base.metadata.drop_all(engine)
    create_all(url)
    with tenant_session(TENANT, url) as session:
        session.add(Tenant(id=TENANT, slug="acme", name="Acme"))
        session.flush()
        session.add(
            Invoice(
                tenant_id=TENANT,
                case_id="c1",
                routing="REVIEW",
                decision="ACCEPT",
                created_at=datetime.now(timezone.utc) - timedelta(days=2),
                fields={},
            )
        )
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    monkeypatch.setattr(auth, "_verifier", FakeVerifier())
    return url


def client(role="viewer"):
    return TestClient(app, headers={"Authorization": f"Bearer tok-{role}"})


def test_a_viewer_can_read_the_window(db):
    body = client("viewer").get("/api/kpis").json()
    assert body["window"]["days"] == 30
    assert body["window"]["since"] is not None
    assert body["received"] == 1
    assert body["exceptions"]["low_confidence"] == 1


def test_all_time_has_no_since(db):
    body = client().get("/api/kpis", params={"days": 0}).json()
    assert body["window"] == {"days": 0, "since": None}


def test_only_the_known_windows_are_accepted(db):
    assert client().get("/api/kpis", params={"days": 31}).status_code == 422
    assert client().get("/api/kpis", params={"days": "week"}).status_code == 422


def test_cases_behind_a_reason(db):
    body = client().get(
        "/api/kpis/cases", params={"reason": "low_confidence", "days": 7}
    ).json()
    assert body == {"case_ids": ["c1"], "truncated": False}


def test_an_unknown_reason_is_refused(db):
    response = client().get("/api/kpis/cases", params={"reason": "vibes"})
    assert response.status_code == 422


def test_no_token_is_refused(db):
    assert TestClient(app).get("/api/kpis").status_code == 401


def test_without_a_database_it_says_so(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("CLOUD_SQL_INSTANCE", raising=False)
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    monkeypatch.setattr(auth, "_verifier", FakeVerifier())
    for path in ("/api/kpis", "/api/kpis/cases?reason=other"):
        response = client().get(path)
        assert response.status_code == 503
        assert "needs a database" in response.json()["detail"]
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/bin/python -m pytest tests/test_kpi_api.py -q -p no:cacheprovider`
Expected: FAIL with 404 on `/api/kpis`.

- [ ] **Step 3: Create the router** — `invoice_processing/api/kpi_routes.py`:

```python
"""The KPI page: the operating numbers over one window.

A reader only. Everything it serves was written by the pipeline, the
projection or a person deciding an invoice; nothing here changes an invoice.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from .auth import Principal, current_principal

router = APIRouter()

WINDOWS = (0, 7, 30, 90)


def _repository():
    from . import service  # noqa: PLC0415

    if not service._database_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "KPIs need a database. This deployment stores cases on "
                "local disk, where invoices cannot be counted."
            ),
        )
    from ..db.repositories import KpiRepository  # noqa: PLC0415

    return KpiRepository()


def _since(days: int) -> datetime | None:
    if days not in WINDOWS:
        raise HTTPException(
            status_code=422,
            detail=f"days must be one of {', '.join(map(str, WINDOWS))}.",
        )
    if days == 0:
        return None
    return datetime.now(timezone.utc) - timedelta(days=days)


@router.get("/api/kpis")
def kpis(
    principal: Annotated[Principal, Depends(current_principal)],
    days: int = 30,
) -> dict:
    since = _since(days)
    repository = _repository()
    return {
        "window": {"days": days, "since": since.isoformat() if since else None},
        **repository.summary(principal.tenant_id, since),
    }


@router.get("/api/kpis/cases")
def kpi_cases(
    principal: Annotated[Principal, Depends(current_principal)],
    reason: str,
    days: int = 30,
) -> dict:
    """The invoices behind one exception bucket, for the drill-down."""
    from ..db.repositories import BUCKETS  # noqa: PLC0415

    if reason not in BUCKETS:
        raise HTTPException(
            status_code=422,
            detail=f"reason must be one of {', '.join(BUCKETS)}.",
        )
    since = _since(days)
    return _repository().cases(principal.tenant_id, since, reason)
```

- [ ] **Step 4: Mount it** — in `invoice_processing/api/service.py`, next to `from .gl_routes import router as gl_router  # noqa: E402` add `from .kpi_routes import router as kpi_router  # noqa: E402`, and after `app.include_router(gl_router)` add `app.include_router(kpi_router)`.

- [ ] **Step 5: Run the route tests**

Run: `./.venv/bin/python -m pytest tests/test_kpi_api.py -q -p no:cacheprovider`
Expected: all PASS.

- [ ] **Step 6: Isolation coverage** — in `tests/test_tenant_isolation_api.py`:
  - Add `"/api/kpis"` and `"/api/kpis/cases"` to `TestCoverage.PROBED`.
  - Add `kpi_routes` to the `from invoice_processing.api import (...)` block and to the tuple in `test_no_endpoint_reads_a_tenant_from_the_request`.
  - In the class that owns the `sheet_clients` fixture, after `test_the_summary_counts_only_this_tenant`, add:

```python
    def test_kpis_count_only_this_tenant(self, sheet_clients):
        body = sheet_clients("globex").get("/api/kpis", params={"days": 0}).json()
        assert body["received"] == 1

    def test_kpi_drill_down_names_only_this_tenants_cases(self, sheet_clients):
        """The seeded rows were inserted directly, not projected: no routing,
        no decision column, no human_decision. They fall in `other`, and
        acme's must not appear for globex."""
        body = (
            sheet_clients("globex")
            .get("/api/kpis/cases", params={"reason": "other", "days": 0})
            .json()
        )
        assert body["case_ids"] == ["globex-case"]
```

- [ ] **Step 7: Run isolation tests and add the API test file to CI**

Run: `./.venv/bin/python -m pytest tests/test_tenant_isolation_api.py -q -p no:cacheprovider`
Expected: all PASS including `test_every_route_is_covered`.

Add `tests/test_kpi_api.py \` after `tests/test_kpi_repository.py \` in `.github/workflows/ci.yml`.

- [ ] **Step 8: Lint and full suite, then commit**

Run: `./.venv/bin/python -m ruff check . && ./.venv/bin/python -m ruff format . && ./.venv/bin/python -m pytest tests/ -q -p no:cacheprovider`

```bash
git add invoice_processing/api/kpi_routes.py invoice_processing/api/service.py tests/test_kpi_api.py tests/test_tenant_isolation_api.py .github/workflows/ci.yml
git commit -m "Serve the KPI window and the invoices behind each reason

Two read-only routes for any role of the tenant, refusing plainly without a
database and accepting only the windows the page offers.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: The `/kpis` page

**Files:**
- Create: `frontend/src/components/Tile.tsx`
- Modify: `frontend/src/app/dashboard/page.tsx` (remove the local `Tile`, import the shared one; link the touchless tile to `/kpis`)
- Create: `frontend/src/app/kpis/page.tsx`
- Modify: `frontend/src/lib/api.ts` (add `Kpis`, `KpiReason`, `KpiCases` types after `Stats`)
- Modify: `frontend/src/components/AppShell.tsx` (`NAV`, after the Dashboard entry)

**Interfaces:**
- Consumes: `GET /api/kpis?days=`; `api<T>()` and `ApiError` from `@/lib/api`; `usePoll` from `@/lib/hooks` (with `intervalMs` 0 it fetches once per path change).
- Produces: `Tile` component `{label, value, hint?, href?, testId?}`; `REASON_LABELS` exported from `src/lib/api.ts` for the invoices page.

- [ ] **Step 1: Types** — in `frontend/src/lib/api.ts` after the `Stats` type:

```ts
export type KpiReason =
  | "rejected_by_rules"
  | "three_way_hold"
  | "low_confidence"
  | "approval_required"
  | "manual_decision"
  | "other";

export const REASON_LABELS: Record<KpiReason, string> = {
  rejected_by_rules: "Rejected by rules",
  three_way_hold: "Held for goods receipt",
  low_confidence: "Low-confidence extraction",
  approval_required: "Approval chain",
  manual_decision: "Decided by hand",
  other: "Other",
};

export type Kpis = {
  window: { days: number; since: string | null };
  received: number;
  decided: number;
  touchless: number;
  touchless_rate: number | null;
  exception_rate: number | null;
  exceptions: Record<KpiReason, number>;
  cycle_hours: { median: number | null; p90: number | null; skipped: number };
  open: { count: number; oldest_hours: number | null };
  three_way: { po_invoices: number; held: number; hold_rate: number | null };
  ai_cost: { per_invoice_usd: number | null; total_usd: number };
};

export type KpiCases = { case_ids: string[]; truncated: boolean };
```

- [ ] **Step 2: Shared tile** — create `frontend/src/components/Tile.tsx` (the body is the dashboard's `Tile` verbatim plus `testId`):

```tsx
import Link from "next/link";

export function Tile({
  label,
  value,
  hint,
  href,
  testId,
}: {
  label: string;
  value: string;
  hint?: string;
  href?: string;
  testId?: string;
}) {
  const body = (
    <div className="panel" style={{ padding: 18, height: "100%" }} data-tile={testId}>
      <div className="eyebrow">{label}</div>
      <div className="tile-value" style={{ font: "600 26px var(--font-ui)", margin: "6px 0 2px" }}>
        {value}
      </div>
      {hint ? (
        <div className="muted" style={{ fontSize: 12 }}>
          {hint}
        </div>
      ) : null}
    </div>
  );
  return href ? (
    <Link href={href} style={{ textDecoration: "none", color: "inherit" }}>
      {body}
    </Link>
  ) : (
    body
  );
}
```

In `frontend/src/app/dashboard/page.tsx`: delete the local `Tile` function and its `Link` import if nothing else uses it (the "See all" link does, so keep `Link`); add `import { Tile } from "@/components/Tile";`; on the "Touchless rate" tile add `href="/kpis"` and change its hint to `` stats?.total ? `of ${stats.total} invoices · see KPIs` : "no invoices yet" ``.

- [ ] **Step 3: The page** — create `frontend/src/app/kpis/page.tsx`:

```tsx
"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { Tile } from "@/components/Tile";
import { REASON_LABELS, type KpiReason, type Kpis } from "@/lib/api";
import { usePoll } from "@/lib/hooks";

const WINDOWS: { days: number; label: string }[] = [
  { days: 7, label: "7 days" },
  { days: 30, label: "30 days" },
  { days: 90, label: "90 days" },
  { days: 0, label: "All time" },
];

function percent(rate: number | null | undefined): string {
  // null is "no denominator", which is not 0%.
  if (rate === null || rate === undefined) return "—";
  return `${Math.round(rate * 100)}%`;
}

function hours(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (value < 1) return `${Math.round(value * 60)} min`;
  if (value < 48) return `${value.toFixed(1)} h`;
  return `${(value / 24).toFixed(1)} d`;
}

function money(value: number | null | undefined, digits = 4): string {
  if (value === null || value === undefined) return "—";
  return `$${value.toFixed(digits)}`;
}

function KpisView() {
  const params = useSearchParams();
  const router = useRouter();
  const days = Number(params.get("days") ?? "30");
  const { data, error, loading } = usePoll<Kpis>(`/api/kpis?days=${days}`);

  const reasons = data
    ? (Object.keys(data.exceptions) as KpiReason[]).filter(
        (reason) => data.exceptions[reason] > 0,
      )
    : [];
  const exceptions = data ? data.received - data.touchless : 0;

  return (
    <div className="stack" style={{ gap: 24 }}>
      <div className="row-between">
        <h1>KPIs</h1>
        <div role="group" aria-label="Window" style={{ display: "flex", gap: 6 }}>
          {WINDOWS.map((w) => (
            <button
              key={w.days}
              type="button"
              className="btn-chip"
              aria-pressed={w.days === days}
              style={w.days === days ? { borderColor: "var(--ink)", background: "var(--surface-alt)" } : undefined}
              onClick={() => router.replace(`/kpis?days=${w.days}`)}
            >
              {w.label}
            </button>
          ))}
        </div>
      </div>

      {error ? <div className="empty-state">{error}</div> : null}
      {!error && !loading && data && data.received === 0 ? (
        <div className="empty-state">No invoices received in this window.</div>
      ) : null}

      {data && data.received > 0 ? (
        <>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
              gap: 16,
            }}
          >
            <Tile
              label="Received"
              value={String(data.received)}
              hint={`${data.decided} decided`}
              testId="received"
            />
            <Tile
              label="Touchless rate"
              value={percent(data.touchless_rate)}
              hint={`${data.touchless} cleared without a person`}
              testId="touchless"
            />
            <Tile
              label="Exception rate"
              value={percent(data.exception_rate)}
              hint={`${exceptions} needed a person`}
              testId="exceptions"
            />
            <Tile
              label="Median cycle time"
              value={hours(data.cycle_hours.median)}
              hint={
                `p90 ${hours(data.cycle_hours.p90)}` +
                (data.cycle_hours.skipped
                  ? ` · ${data.cycle_hours.skipped} skipped (decided before received)`
                  : "")
              }
              testId="cycle"
            />
            <Tile
              label="Open"
              value={String(data.open.count)}
              hint={
                data.open.oldest_hours === null
                  ? "nothing waiting"
                  : `oldest waiting ${hours(data.open.oldest_hours)}`
              }
              href="/invoices"
              testId="open"
            />
            <Tile
              label="Three-way hold rate"
              value={percent(data.three_way.hold_rate)}
              hint={`${data.three_way.held} held of ${data.three_way.po_invoices} PO-backed`}
              testId="three-way"
            />
            <Tile
              label="AI cost per invoice"
              value={money(data.ai_cost.per_invoice_usd)}
              hint={`${money(data.ai_cost.total_usd, 2)} total model spend`}
              testId="cost"
            />
          </div>

          <section className="stack" style={{ gap: 10 }}>
            <h2>Why invoices needed a person</h2>
            {reasons.length === 0 ? (
              <div className="empty-state">Every invoice in this window cleared untouched.</div>
            ) : (
              <div className="panel table-scroll" style={{ padding: 8 }}>
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Reason</th>
                      <th>Invoices</th>
                      <th>Share</th>
                    </tr>
                  </thead>
                  <tbody>
                    {reasons.map((reason) => (
                      <tr key={reason} data-reason={reason}>
                        <td>
                          <Link href={`/invoices?reason=${reason}&days=${days}`}>
                            {REASON_LABELS[reason]}
                          </Link>
                        </td>
                        <td>{data.exceptions[reason]}</td>
                        <td className="muted">
                          {exceptions
                            ? `${Math.round((data.exceptions[reason] / exceptions) * 100)}%`
                            : "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      ) : null}
    </div>
  );
}

export default function KpisPage() {
  // useSearchParams needs a Suspense boundary for this route to stay static.
  return (
    <Suspense fallback={<div className="empty-state">Loading…</div>}>
      <KpisView />
    </Suspense>
  );
}
```

`btn-chip` is the existing toggle class (see `frontend/src/app/sheet/page.tsx`); it has no pressed style of its own, hence the inline border/background on the active one. Do not add CSS.

- [ ] **Step 4: Navigation** — in `frontend/src/components/AppShell.tsx` `NAV`, after the Dashboard entry add `{ href: "/kpis", label: "KPIs", glyph: "◫" },`.

- [ ] **Step 5: Lint and type-check**

Run: `cd frontend && npm run lint && npx tsc --noEmit`
Expected: clean.

- [ ] **Step 6: Browser check on the manual stack** — `preview_start` with `manual-a-api-worker` and `manual-frontend` from `.claude/launch.json`; open `/kpis`, switch windows, confirm tiles and the reasons table render, and that `?days=7` on a quiet DB shows the empty-window message. Take a screenshot for the ledger.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/Tile.tsx frontend/src/app/dashboard/page.tsx frontend/src/app/kpis/page.tsx frontend/src/lib/api.ts frontend/src/components/AppShell.tsx
git commit -m "Show the operating numbers over a window

A KPIs page with the window in the URL, the dashboard's tile reused, and a
reasons table that links to the invoices behind each one.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Drill-down on `/invoices?reason=`

**Files:**
- Modify: `frontend/src/app/invoices/page.tsx` (`InvoicesWorkbench`: the `queue` memo ~L77, and the header area ~L330)

**Interfaces:**
- Consumes: `GET /api/kpis/cases?reason=&days=` (Task 4), `REASON_LABELS`, `KpiCases`, `KpiReason` (Task 5).

- [ ] **Step 1: Fetch the case ids when a reason is present** — after `const params = useSearchParams();`:

```tsx
  // A KPI drill-down: only the invoices behind one reason. The list route
  // cannot compute the reason itself (it needs the findings and the approval
  // chain), so the KPI route names the cases and this page keeps to them.
  const reason = params.get("reason") as KpiReason | null;
  const reasonDays = params.get("days") ?? "30";
  const { data: drill } = usePoll<KpiCases>(
    reason ? `/api/kpis/cases?reason=${reason}&days=${reasonDays}` : null,
  );
```

Change the `queue` memo to:
```tsx
  const queue = useMemo(() => {
    const all = list?.invoices ?? [];
    if (!reason) return all;
    if (!drill) return [];
    const keep = new Set(drill.case_ids);
    return all.filter((invoice) => keep.has(invoice.case_id));
  }, [list, reason, drill]);
```

Add `KpiCases`, `KpiReason`, `REASON_LABELS` to the `@/lib/api` import.

- [ ] **Step 2: The banner** — directly under the `<div className="row-between">` header block (before the in-flight list), add:

```tsx
      {reason ? (
        <div className="panel" style={{ padding: "8px 12px" }} data-drill={reason}>
          Showing {queue.length} invoice{queue.length === 1 ? "" : "s"} needing a
          person: <strong>{REASON_LABELS[reason] ?? reason}</strong>
          {drill?.truncated ? " (first 500)" : ""} ·{" "}
          <Link href="/invoices">Show all</Link>
        </div>
      ) : null}
```

Import `Link` from `next/link` if the file does not already.

- [ ] **Step 3: Lint, type-check, browser check**

Run: `cd frontend && npm run lint && npx tsc --noEmit`
Then on the manual stack open `/kpis`, click a reason, confirm the queue is filtered and "Show all" restores it.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/app/invoices/page.tsx
git commit -m "Let a KPI reason narrow the invoice queue

The reasons table links here with ?reason=; the queue keeps to the cases the
KPI route names and says so, with a way back to everything.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: End-to-end test

**Files:**
- Modify: `frontend/e2e/golden-path.spec.ts` (add `"KPIs"` to the sidebar list in `every sidebar view opens`, ~L178; add one test after the dashboard test ~L28)

**Interfaces:**
- Consumes: the seeded e2e stack (`scripts/e2e_api.py`): golden cases plus clones, all projected now, so they fall inside every window; the held PO cases give `three_way_hold`, the rest `low_confidence`.

- [ ] **Step 1: Add the test**

```ts
test("the KPI page reports the window and drills down to the invoices behind a reason", async ({
  page,
}) => {
  await goto(page, "/kpis");
  await expect(page.getByRole("heading", { name: "KPIs" })).toBeVisible();
  for (const tile of [
    "Received",
    "Touchless rate",
    "Exception rate",
    "Median cycle time",
    "Open",
    "Three-way hold rate",
    "AI cost per invoice",
  ]) {
    await expect(page.getByText(tile, { exact: true })).toBeVisible();
  }
  // The seeded cases were projected at startup, so every window holds them.
  await expect
    .poll(async () =>
      Number(await page.locator("[data-tile='received'] .tile-value").textContent()),
    )
    .toBeGreaterThanOrEqual(3);

  await page.getByRole("button", { name: "All time" }).click();
  await expect(page).toHaveURL(/days=0/);

  // Every seeded case needed a person, so the reasons table is never empty.
  const row = page.locator("tr[data-reason]").first();
  await expect(row).toBeVisible();
  const reason = await row.getAttribute("data-reason");
  await row.getByRole("link").click();

  await expect(page).toHaveURL(new RegExp(`/invoices\\?reason=${reason}`));
  await expect(page.locator(`[data-drill='${reason}']`)).toContainText("Showing");
  await expect(page.locator(".queue-item").first()).toBeVisible();
  await page.getByRole("link", { name: "Show all" }).click();
  await expect(page).toHaveURL(/\/invoices$/);
});
```

Add `"KPIs"` after `"Invoices"` in the `every sidebar view opens` list.

- [ ] **Step 2: Run e2e**

Run: `cd frontend && npm run test:e2e`
Expected: 33 pass (31 + the new test; the sidebar test now also visits KPIs). If the Received tile's value selector misses, confirm the `data-tile` attribute landed on the panel div in `Tile.tsx`.

- [ ] **Step 3: Commit**

```bash
git add frontend/e2e/golden-path.spec.ts
git commit -m "Prove a person can read the KPIs and reach the invoices behind them

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: Verification by the controller (not an implementer task)

- [ ] Run every gate:

```bash
./.venv/bin/python -m pytest tests/ -q -p no:cacheprovider
./.venv/bin/python -m ruff check . && ./.venv/bin/python -m ruff format --check .
bash benchmark/harness/gates.sh
cd frontend && npm run lint && npx tsc --noEmit && npm run test:e2e
```

- [ ] Independent reconciliation against the manual stack DB (`benchmark/runs/manual/stacks/A/app.db`, tenant `bench-a`): run `KpiRepository(url).summary("bench-a", None)` from a scratch script, then list the invoices via the API and hand-count received, REVIEW, held and decided. Numbers must match before any figure is claimed.
- [ ] Prove each new test fails without its change: `git stash push -- invoice_processing frontend/src` then run the new test files, then `git stash pop`.
- [ ] Postgres parity if Docker is up: `TEST_DATABASE_URL=postgresql+pg8000://postgres:postgres@localhost:55432/t ./.venv/bin/python -m pytest tests/test_kpi_repository.py tests/test_kpi_api.py tests/test_db_repositories.py -q`.
- [ ] Browser screenshot of `/kpis` (30 days and 7 days) and of a drill-down.
- [ ] Ask before pushing; open the PR with a body ending `🤖 Generated with [Claude Code](https://claude.com/claude-code)`; merge only on explicit wording.
