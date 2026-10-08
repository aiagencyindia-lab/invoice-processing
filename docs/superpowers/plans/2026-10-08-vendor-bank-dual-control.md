# Vendor Bank Details, Bank-Change Dual Control and BEC Signals — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Vendors become a Postgres table with verified bank details and an email domain; bank details change only through a dual-control queue (proposer ≠ approver); a new pipeline step 3.4 holds an invoice for review on five BEC signals.

**Architecture:** Two tenant-scoped tables (`vendors`, `vendor_bank_changes`) behind `PgVendorRepository`; `apply_to_pipeline` injects vendor records into the engine config so the existing `VendorMaster` class serves both DB and file; a pure `bank_check` module computes signals and Phase 3 appends the 3.4 validation and forces `_routing=REVIEW`; inbound email records sender/reply-to on the document and the worker passes it into the config; `vendor_routes.py` exposes the queue; a `/vendors` page and an invoice-page button drive it.

**Tech Stack:** Python 3.12 (`./.venv/bin/python`), FastAPI, SQLAlchemy 2 + Alembic (Postgres with RLS; SQLite locally and in tests), pytest, ruff; Next.js 16 / React 19 / Tailwind 4 / Playwright in `frontend/`.

**Spec:** `docs/superpowers/specs/2026-10-08-vendor-bank-dual-control-design.md` (committed as `4830b90`). Read it first.

**Plan file on disk (first task copies this plan there):** `docs/superpowers/plans/2026-10-08-vendor-bank-dual-control.md`

## Global Constraints

- Always `./.venv/bin/python`, never system python. Run tests as `./.venv/bin/python -m pytest <path> -q -p no:cacheprovider`. Never pipe pytest into `tail`.
- ruff clean after every task: `./.venv/bin/python -m ruff check . && ./.venv/bin/python -m ruff format .`
- Every fix/feature gets a test that fails without it. Verify by `git stash push -- invoice_processing` → run → `git stash pop` when in doubt.
- Code comments explain *why*, in prose; deliberate simplifications marked `# ponytail: <ceiling + upgrade path>`.
- No endpoint takes `tenant_id`; it comes from `principal.tenant_id`. Every new route needs a probe + `PROBED` entry in `tests/test_tenant_isolation_api.py` (the guard test fails otherwise).
- Roles: `require_role("clerk"|"approver"|"admin")`; actors are named by `principal.email`.
- Hold, never reject: step 3.4 is `informational_only: true` in YAML and `"informational": True` on the validation dict. No rejection template.
- **No O/0 folding** on account numbers or bank codes.
- Commit after each task, locally only. Imperative subject saying *why*; body with root cause / measured result. Trailer: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. **Do not push** until the user says so.
- Production is down (closed billing). Everything runs locally against GCP project `rasayan`; do not propose production work.
- Reference `benchmark/CHANGELOG.md` style for the measurement entry (root cause, evidence, before/after).

## File structure

| File | Responsibility |
|---|---|
| `invoice_processing/db/models.py` | add `Vendor`, `VendorBankChange` models; `Document.sender`, `Document.reply_to`; both tables in `TENANT_TABLES` |
| `invoice_processing/db/migrations/versions/e7a2c9d41b56_vendors_and_bank_changes.py` | create both tables + RLS; add document columns |
| `invoice_processing/db/repositories.py` | `PgVendorRepository`; `"vendor"` in `_ENTITY_TYPES`; `find_by_sha` returns `source`, `sender`, `reply_to` |
| `invoice_processing/vendor_master.py` | `Vendor` gains bank fields + `email_domain`; `VendorMaster.find_by_id`; no logic change |
| `invoice_processing/bank_check.py` (new) | pure signal computation: `compare(invoice, vendor, changes, sender, recent_days) -> BankCheck` |
| `invoice_processing/shared_libraries/invoice_master_data.yaml` | step 3.4 declaration; `bank_change_recent_days` default |
| `invoice_processing/shared_libraries/acting/general_invoice_agent.py` | `_load_vendors()` prefers `vendor_master_records`; Phase 3 calls `bank_check`, appends 3.4, forces REVIEW, re-saves `02_extraction.json` |
| `invoice_processing/tenant_settings.py` | `bank_change_recent_days` settable; `apply_to_pipeline` injects DB records + bank changes |
| `invoice_processing/api/service.py` | `get_vendors()` accessor; include `vendor_routes.router`; `POST /api/invoices/{case_id}/bank-change` |
| `invoice_processing/api/vendor_routes.py` (new) | vendor list/upsert/patch, bank-change propose/list/decide |
| `invoice_processing/api/config_routes.py` | delete the old `/api/vendors` GET/PUT (moved) |
| `invoice_processing/api/ingest.py` | parse From/Reply-To; store on Document |
| `invoice_processing/worker/runner.py` | `config["document_sender"]` before `process_invoice` |
| `benchmark/harness/dataset.py` | `vendor_records` carries bank/email columns |
| `frontend/src/lib/api.ts`, `frontend/src/components/AppShell.tsx`, `frontend/src/app/vendors/page.tsx` (new), `frontend/src/app/invoices/page.tsx` | UI |
| tests: `tests/test_bank_check.py` (new), `tests/test_vendor_routes.py` (new), `tests/test_vendor_master.py`, `tests/test_tenant_isolation_api.py`, `tests/test_ingestion.py`, `tests/test_worker.py`, `tests/test_config_routes.py` | |

---

## Task 0: Save this plan into the repo

**Files:**
- Create: `docs/superpowers/plans/2026-10-08-vendor-bank-dual-control.md`

- [ ] **Step 1: Copy the plan** — copy this file's full content to the path above.
- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/plans/2026-10-08-vendor-bank-dual-control.md
git commit -m "Plan the vendor bank dual-control implementation" -m "Task-by-task plan for docs/superpowers/specs/2026-10-08-vendor-bank-dual-control-design.md." -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Task 1: Models and migration

**Files:**
- Modify: `invoice_processing/db/models.py` (Document class ~:128-150; add two models after `Correction`; `TENANT_TABLES` ~:504-518)
- Create: `invoice_processing/db/migrations/versions/e7a2c9d41b56_vendors_and_bank_changes.py`
- Test: `tests/test_vendor_repository.py` (new; created here, extended in Task 2)

**Interfaces:**
- Produces: ORM classes `Vendor` (table `vendors`) and `VendorBankChange` (table `vendor_bank_changes`); `Document.sender`, `Document.reply_to` (`String(320)`, nullable). Status constants on `VendorBankChange`: `PENDING = "pending"`, `APPROVED = "approved"`, `REJECTED = "rejected"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_vendor_repository.py
"""Vendors live in the database, not a file: the API and the worker are
separate containers in production, so a JSON master written by one was
invisible to the other. Bank details and the dual-control queue that governs
them need a record both can read."""

import pytest
from sqlalchemy import inspect

from invoice_processing.db import models
from invoice_processing.db.session import create_all


class TestSchema:
    def test_vendor_tables_are_tenant_tables(self):
        assert "vendors" in models.TENANT_TABLES
        assert "vendor_bank_changes" in models.TENANT_TABLES

    def test_create_all_builds_the_tables_and_document_columns(self, scratch_db):
        engine = create_all(scratch_db("schema.db"))
        inspector = inspect(engine)
        assert inspector.has_table("vendors")
        assert inspector.has_table("vendor_bank_changes")
        document_columns = {c["name"] for c in inspector.get_columns("documents")}
        assert {"sender", "reply_to"} <= document_columns
        vendor_columns = {c["name"] for c in inspector.get_columns("vendors")}
        assert {
            "name", "tax_id", "kind", "normalized_name", "email_domain",
            "bank_account_name", "bank_account_number", "bank_code", "bank_iban",
            "bank_verified_at", "bank_verified_by",
        } <= vendor_columns
```

- [ ] **Step 2: Run it to verify it fails**

Run: `./.venv/bin/python -m pytest tests/test_vendor_repository.py -q -p no:cacheprovider`
Expected: FAIL — `AssertionError` on `TENANT_TABLES` / `has_table("vendors")`.

- [ ] **Step 3: Add the models**

In `invoice_processing/db/models.py`, add two columns to `Document` after `source_ref`:

```python
    # Who the email said it came from, for the sender-domain BEC signal. Both
    # kept: Reply-To is where a reply would go, which is what a fraudster
    # redirects; From is what the reviewer sees.
    sender: Mapped[str | None] = mapped_column(String(320))
    reply_to: Mapped[str | None] = mapped_column(String(320))
```

After the `Correction` class add:

```python
class Vendor(Base, TenantMixin, TimestampMixin):
    """A supplier, with the bank details the tenant has verified for it.

    The bank fields are written by exactly one path: approving a
    `VendorBankChange`. The bulk import never touches them, so the only way a
    payee account enters the system is through two people.
    """

    __tablename__ = "vendors"
    __table_args__ = (
        UniqueConstraint("tenant_id", "normalized_name", name="uq_vendors_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(512), nullable=False)
    tax_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default="")
    email_domain: Mapped[str | None] = mapped_column(String(255))
    bank_account_name: Mapped[str | None] = mapped_column(String(255))
    bank_account_number: Mapped[str | None] = mapped_column(String(64))
    bank_code: Mapped[str | None] = mapped_column(String(64))
    bank_iban: Mapped[str | None] = mapped_column(String(64))
    bank_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bank_verified_by: Mapped[str | None] = mapped_column(String(320))


class VendorBankChange(Base, TenantMixin, TimestampMixin):
    """A proposed change to a vendor's payee details, awaiting a second person.

    Segregation of duties is enforced in the repository (`decided_by` must
    differ from `proposed_by`), not merely recorded as it is for rules.
    """

    __tablename__ = "vendor_bank_changes"

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    vendor_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("vendors.id"), nullable=False, index=True
    )
    proposed_account_name: Mapped[str | None] = mapped_column(String(255))
    proposed_account_number: Mapped[str | None] = mapped_column(String(64))
    proposed_code: Mapped[str | None] = mapped_column(String(64))
    proposed_iban: Mapped[str | None] = mapped_column(String(64))
    previous_account_name: Mapped[str | None] = mapped_column(String(255))
    previous_account_number: Mapped[str | None] = mapped_column(String(64))
    previous_code: Mapped[str | None] = mapped_column(String(64))
    previous_iban: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    source_case_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default=PENDING, index=True)
    proposed_by: Mapped[str] = mapped_column(String(320), nullable=False)
    decided_by: Mapped[str | None] = mapped_column(String(320))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)
```

Check the imports at the top of `models.py` already include `UniqueConstraint`, `ForeignKey`, `DateTime`, `Text`, `datetime` (they are used by `RecordLock`/`Rule`; add any that is missing). Add `"vendors"` and `"vendor_bank_changes"` to `TENANT_TABLES`.

- [ ] **Step 4: Write the migration**

```python
"""vendors and bank changes: a supplier record both containers can read, and
the dual-control queue for its payee details

Revision ID: e7a2c9d41b56
Revises: d5b81a6c39f4
Create Date: 2026-10-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from invoice_processing.db.rls import enable_rls_statements

revision: str = "e7a2c9d41b56"
down_revision: str | None = "d5b81a6c39f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RLS_TABLES = ["vendors", "vendor_bank_changes"]


def upgrade() -> None:
    op.create_table(
        "vendors",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False, index=True),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("normalized_name", sa.String(512), nullable=False),
        sa.Column("tax_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), server_default=""),
        sa.Column("email_domain", sa.String(255)),
        sa.Column("bank_account_name", sa.String(255)),
        sa.Column("bank_account_number", sa.String(64)),
        sa.Column("bank_code", sa.String(64)),
        sa.Column("bank_iban", sa.String(64)),
        sa.Column("bank_verified_at", sa.DateTime(timezone=True)),
        sa.Column("bank_verified_by", sa.String(320)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "normalized_name", name="uq_vendors_name"),
    )
    op.create_table(
        "vendor_bank_changes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False, index=True),
        sa.Column("vendor_id", sa.String(36), sa.ForeignKey("vendors.id"), nullable=False, index=True),
        sa.Column("proposed_account_name", sa.String(255)),
        sa.Column("proposed_account_number", sa.String(64)),
        sa.Column("proposed_code", sa.String(64)),
        sa.Column("proposed_iban", sa.String(64)),
        sa.Column("previous_account_name", sa.String(255)),
        sa.Column("previous_account_number", sa.String(64)),
        sa.Column("previous_code", sa.String(64)),
        sa.Column("previous_iban", sa.String(64)),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("source_case_id", sa.String(64)),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending", index=True),
        sa.Column("proposed_by", sa.String(320), nullable=False),
        sa.Column("decided_by", sa.String(320)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("decision_note", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.add_column("documents", sa.Column("sender", sa.String(320), nullable=True))
    op.add_column("documents", sa.Column("reply_to", sa.String(320), nullable=True))

    if op.get_bind().dialect.name != "postgresql":
        return
    # One pending change per vendor, enforced where two API containers cannot
    # race past a check in Python.
    op.execute(
        "CREATE UNIQUE INDEX uq_vendor_bank_changes_pending "
        "ON vendor_bank_changes (vendor_id) WHERE status = 'pending'"
    )
    for statement in enable_rls_statements(RLS_TABLES):
        op.execute(statement)


def downgrade() -> None:
    op.drop_column("documents", "reply_to")
    op.drop_column("documents", "sender")
    op.drop_table("vendor_bank_changes")
    op.drop_table("vendors")
```

- [ ] **Step 5: Run the test and the existing migration/schema tests**

Run: `./.venv/bin/python -m pytest tests/test_vendor_repository.py tests/test_db* tests/test_migration* -q -p no:cacheprovider` (use `ls tests | grep -i -E "db|migra|rls|schema"` to find the actual names and include them).
Expected: PASS. If a test asserts the alembic head equals `d5b81a6c39f4`, update it to `e7a2c9d41b56`. If a test diff-checks `Base.metadata` against an alembic-upgraded SQLite DB, both must agree — fix the migration, not the test.

- [ ] **Step 6: ruff and commit**

```bash
./.venv/bin/python -m ruff check . && ./.venv/bin/python -m ruff format .
git add invoice_processing/db/models.py invoice_processing/db/migrations/versions/e7a2c9d41b56_vendors_and_bank_changes.py tests/test_vendor_repository.py
git commit -m "Give vendors a table the worker can read, and bank changes a queue" -m "A JSON vendor master written by the API container never reaches the worker container, so dual control on a file would be theatre. Two tenant tables under RLS; documents gain sender and reply-to for the sender-domain signal." -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Task 2: `PgVendorRepository`

**Files:**
- Modify: `invoice_processing/db/repositories.py` (add `"vendor": "vendor"` to `_ENTITY_TYPES` ~:567; `find_by_sha` ~:500-515 returns `source`, `sender`, `reply_to`; new class after `PgRecordLocks`)
- Modify: `invoice_processing/api/service.py` (new `_vendors = None` global ~:131 and `get_vendors()` accessor after `get_locks`)
- Modify: `tests/conftest.py` (add `"_vendors"` to the reset list ~:72-82)
- Test: `tests/test_vendor_repository.py`

**Interfaces:**
- Produces `PgVendorRepository(url=None)` with:
  - `upsert_many(tenant_id, records: list[dict]) -> int` — records have `name`, `tax_id`, `kind`; keyed by `po_matching.normalize_vendor(name)`; never writes bank/email fields; returns count.
  - `list(tenant_id) -> list[dict]` — full rows as dicts (keys = column names, plus `pending_change_id: str | None`), ordered by name. **Unmasked.**
  - `get(tenant_id, vendor_id) -> dict | None`
  - `find_by_name(tenant_id, name) -> dict | None`
  - `patch(tenant_id, vendor_id, *, email_domain) -> dict` — raises `KeyError` if unknown.
  - `load_records(tenant_id) -> list[dict]` — `VendorMaster`-shaped dicts: `name, tax_id, kind, email_domain, bank_account_name, bank_account_number, bank_code, bank_iban, bank_verified_at (iso|None), vendor_id, pending_change (bool), last_bank_change_at (iso|None)`.
  - `propose(tenant_id, vendor_id, *, proposed: dict, reason: str, proposed_by: str, source_case_id=None) -> dict` — raises `PendingChangeExists` (new exception class in repositories.py) if one is pending; `KeyError` if vendor unknown; snapshots `previous_*`.
  - `list_changes(tenant_id, status: str | None = None) -> list[dict]` — newest first; each dict has every column plus `vendor_name`.
  - `get_change(tenant_id, change_id) -> dict | None`
  - `decide(tenant_id, change_id, *, action: str, decided_by: str, note: str | None) -> dict` — raises `SelfApproval` if `decided_by == proposed_by`; `ChangeNotPending` if status != pending; `ValueError` for an action other than `approve|reject`. Approve copies `proposed_*` onto the vendor and sets `bank_verified_at=now, bank_verified_by=decided_by`.
- `service.get_vendors()` returns the repository when `_database_configured()` else `None`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_vendor_repository.py`)

```python
from invoice_processing.db.repositories import (
    ChangeNotPending,
    PendingChangeExists,
    PgVendorRepository,
    SelfApproval,
)

T = "tenant-a"
ACME = {"name": "Cloudhaven Infra Services Pvt Ltd", "tax_id": "29ZOICE4828P1ZD", "kind": "service"}
BANK = {
    "bank_account_name": "Cloudhaven Infra Services Pvt Ltd",
    "bank_account_number": "901031925572",
    "bank_code": "KCBL0230056",
    "bank_iban": None,
}


@pytest.fixture
def repo(scratch_db):
    url = scratch_db("vendors.db")
    create_all(url)
    from invoice_processing.db.session import tenant_session
    with tenant_session("", url) as session:
        session.add(models.Tenant(id=T, name="A", slug="a", status="active"))
        session.add(models.Tenant(id="tenant-b", name="B", slug="b", status="active"))
    return PgVendorRepository(url)


class TestUpsert:
    def test_upsert_is_keyed_by_normalised_name_and_keeps_bank_fields(self, repo):
        assert repo.upsert_many(T, [ACME]) == 1
        vendor = repo.find_by_name(T, "Cloudhaven Infra Services Private Limited")
        assert vendor["tax_id"] == ACME["tax_id"]
        change = repo.propose(T, vendor["id"], proposed=BANK, reason="onboarding", proposed_by="clerk@a")
        repo.decide(T, change["id"], action="approve", decided_by="boss@a", note=None)
        # A re-import of the plain list must not wipe what two people verified.
        repo.upsert_many(T, [{**ACME, "tax_id": "29ZOICE4828P1ZD", "bank_account_number": "000"}])
        again = repo.get(T, vendor["id"])
        assert again["bank_account_number"] == BANK["bank_account_number"]
        assert again["bank_verified_by"] == "boss@a"

    def test_list_is_tenant_scoped(self, repo):
        repo.upsert_many(T, [ACME])
        assert repo.list("tenant-b") == []


class TestDualControl:
    def test_pending_change_does_not_touch_the_vendor(self, repo):
        repo.upsert_many(T, [ACME])
        vendor = repo.find_by_name(T, ACME["name"])
        change = repo.propose(T, vendor["id"], proposed=BANK, reason="new account", proposed_by="clerk@a")
        assert change["status"] == "pending"
        assert repo.get(T, vendor["id"])["bank_account_number"] is None
        assert repo.get(T, vendor["id"])["pending_change_id"] == change["id"]

    def test_second_pending_change_is_refused(self, repo):
        repo.upsert_many(T, [ACME])
        vendor = repo.find_by_name(T, ACME["name"])
        repo.propose(T, vendor["id"], proposed=BANK, reason="x", proposed_by="clerk@a")
        with pytest.raises(PendingChangeExists):
            repo.propose(T, vendor["id"], proposed=BANK, reason="y", proposed_by="clerk@a")

    def test_proposer_cannot_approve(self, repo):
        repo.upsert_many(T, [ACME])
        vendor = repo.find_by_name(T, ACME["name"])
        change = repo.propose(T, vendor["id"], proposed=BANK, reason="x", proposed_by="clerk@a")
        with pytest.raises(SelfApproval):
            repo.decide(T, change["id"], action="approve", decided_by="clerk@a", note=None)

    def test_approve_copies_values_and_stamps_verification(self, repo):
        repo.upsert_many(T, [ACME])
        vendor = repo.find_by_name(T, ACME["name"])
        change = repo.propose(T, vendor["id"], proposed=BANK, reason="x", proposed_by="clerk@a")
        decided = repo.decide(T, change["id"], action="approve", decided_by="boss@a", note="called them")
        assert decided["status"] == "approved"
        after = repo.get(T, vendor["id"])
        assert after["bank_code"] == "KCBL0230056"
        assert after["bank_verified_by"] == "boss@a"
        assert after["bank_verified_at"] is not None
        assert after["pending_change_id"] is None

    def test_reject_is_terminal(self, repo):
        repo.upsert_many(T, [ACME])
        vendor = repo.find_by_name(T, ACME["name"])
        change = repo.propose(T, vendor["id"], proposed=BANK, reason="x", proposed_by="clerk@a")
        repo.decide(T, change["id"], action="reject", decided_by="boss@a", note="not verified")
        with pytest.raises(ChangeNotPending):
            repo.decide(T, change["id"], action="approve", decided_by="boss@a", note=None)
        assert repo.get(T, vendor["id"])["bank_account_number"] is None

    def test_load_records_carry_bank_and_change_state(self, repo):
        repo.upsert_many(T, [ACME])
        vendor = repo.find_by_name(T, ACME["name"])
        repo.propose(T, vendor["id"], proposed=BANK, reason="x", proposed_by="clerk@a")
        (record,) = repo.load_records(T)
        assert record["name"] == ACME["name"]
        assert record["pending_change"] is True
        assert record["bank_account_number"] is None
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/bin/python -m pytest tests/test_vendor_repository.py -q -p no:cacheprovider`
Expected: FAIL — `ImportError: cannot import name 'PgVendorRepository'`.

- [ ] **Step 3: Implement the repository**

In `repositories.py`: add `"vendor": "vendor",` to `_ENTITY_TYPES`. In `find_by_sha` add `"source": document.source, "sender": document.sender, "reply_to": document.reply_to` to the returned dict. Then add:

```python
class PendingChangeExists(Exception):
    """A vendor already has a bank change awaiting decision."""


class SelfApproval(Exception):
    """The proposer tried to approve their own bank change."""


class ChangeNotPending(Exception):
    """The change was already decided."""


BANK_FIELDS = ("account_name", "account_number", "code", "iban")


class PgVendorRepository:
    """Suppliers and the dual-control queue for their payee details.

    Bank columns on `vendors` are written only by `decide(approve)`. The
    bulk `upsert_many` is the customer's supplier export and deliberately
    cannot carry bank details: an export is one person's file.
    """

    def __init__(self, url: str | None = None):
        self.url = url

    # -- shapes ----------------------------------------------------------
    @staticmethod
    def _iso(value):
        return value.isoformat() if value else None

    def _vendor_dict(self, session, vendor: Vendor) -> dict:
        pending = session.scalar(
            select(VendorBankChange.id).where(
                VendorBankChange.vendor_id == vendor.id,
                VendorBankChange.status == VendorBankChange.PENDING,
            )
        )
        return {
            "id": vendor.id,
            "name": vendor.name,
            "tax_id": vendor.tax_id,
            "kind": vendor.kind or "",
            "email_domain": vendor.email_domain,
            "bank_account_name": vendor.bank_account_name,
            "bank_account_number": vendor.bank_account_number,
            "bank_code": vendor.bank_code,
            "bank_iban": vendor.bank_iban,
            "bank_verified_at": self._iso(vendor.bank_verified_at),
            "bank_verified_by": vendor.bank_verified_by,
            "pending_change_id": pending,
        }

    @staticmethod
    def _change_dict(change: VendorBankChange, vendor_name: str = "") -> dict:
        out = {
            "id": change.id,
            "vendor_id": change.vendor_id,
            "vendor_name": vendor_name,
            "reason": change.reason,
            "source_case_id": change.source_case_id,
            "status": change.status,
            "proposed_by": change.proposed_by,
            "decided_by": change.decided_by,
            "decided_at": change.decided_at.isoformat() if change.decided_at else None,
            "decision_note": change.decision_note,
            "created_at": change.created_at.isoformat() if change.created_at else None,
        }
        for f in BANK_FIELDS:
            out[f"proposed_{f}"] = getattr(change, f"proposed_{f}")
            out[f"previous_{f}"] = getattr(change, f"previous_{f}")
        return out

    def _vendor(self, session, tenant_id: str, vendor_id: str) -> Vendor:
        vendor = session.scalar(
            select(Vendor).where(Vendor.tenant_id == tenant_id, Vendor.id == vendor_id)
        )
        if vendor is None:
            raise KeyError(vendor_id)
        return vendor

    # -- vendors ---------------------------------------------------------
    def upsert_many(self, tenant_id: str, records: list[dict]) -> int:
        with tenant_session(tenant_id, self.url) as session:
            existing = {
                v.normalized_name: v
                for v in session.scalars(select(Vendor).where(Vendor.tenant_id == tenant_id))
            }
            for record in records:
                name = str(record.get("name") or "").strip()
                key = po_matching.normalize_vendor(name)
                if not key:
                    continue
                vendor = existing.get(key)
                if vendor is None:
                    vendor = Vendor(tenant_id=tenant_id, name=name, normalized_name=key, tax_id="")
                    session.add(vendor)
                    existing[key] = vendor
                vendor.name = name
                vendor.tax_id = str(record.get("tax_id") or "").strip()
                vendor.kind = str(record.get("kind") or "").lower()
            session.flush()
        return len(records)

    def list(self, tenant_id: str) -> list[dict]:
        with tenant_session(tenant_id, self.url) as session:
            rows = session.scalars(
                select(Vendor).where(Vendor.tenant_id == tenant_id).order_by(Vendor.name)
            ).all()
            return [self._vendor_dict(session, v) for v in rows]

    def get(self, tenant_id: str, vendor_id: str) -> dict | None:
        with tenant_session(tenant_id, self.url) as session:
            try:
                return self._vendor_dict(session, self._vendor(session, tenant_id, vendor_id))
            except KeyError:
                return None

    def find_by_name(self, tenant_id: str, name: str) -> dict | None:
        key = po_matching.normalize_vendor(name)
        with tenant_session(tenant_id, self.url) as session:
            vendor = session.scalar(
                select(Vendor).where(Vendor.tenant_id == tenant_id, Vendor.normalized_name == key)
            )
            return self._vendor_dict(session, vendor) if vendor else None

    def patch(self, tenant_id: str, vendor_id: str, *, email_domain: str | None) -> dict:
        with tenant_session(tenant_id, self.url) as session:
            vendor = self._vendor(session, tenant_id, vendor_id)
            vendor.email_domain = (email_domain or "").strip().lower() or None
            session.flush()
            return self._vendor_dict(session, vendor)

    def load_records(self, tenant_id: str) -> list[dict]:
        """What the pipeline's VendorMaster is built from."""
        with tenant_session(tenant_id, self.url) as session:
            rows = session.scalars(select(Vendor).where(Vendor.tenant_id == tenant_id)).all()
            out = []
            for v in rows:
                changes = session.scalars(
                    select(VendorBankChange)
                    .where(VendorBankChange.vendor_id == v.id)
                    .order_by(VendorBankChange.created_at.desc())
                ).all()
                approved = [c for c in changes if c.status == VendorBankChange.APPROVED]
                out.append(
                    {
                        "vendor_id": v.id,
                        "name": v.name,
                        "tax_id": v.tax_id,
                        "kind": v.kind or "",
                        "email_domain": v.email_domain,
                        "bank_account_name": v.bank_account_name,
                        "bank_account_number": v.bank_account_number,
                        "bank_code": v.bank_code,
                        "bank_iban": v.bank_iban,
                        "bank_verified_at": self._iso(v.bank_verified_at),
                        "pending_change": any(c.status == VendorBankChange.PENDING for c in changes),
                        "last_bank_change_at": self._iso(approved[0].decided_at) if approved else None,
                    }
                )
            return out

    # -- bank changes ----------------------------------------------------
    def propose(self, tenant_id, vendor_id, *, proposed: dict, reason: str, proposed_by: str, source_case_id=None) -> dict:
        with tenant_session(tenant_id, self.url) as session:
            vendor = self._vendor(session, tenant_id, vendor_id)
            if session.scalar(
                select(VendorBankChange.id).where(
                    VendorBankChange.vendor_id == vendor.id,
                    VendorBankChange.status == VendorBankChange.PENDING,
                )
            ):
                raise PendingChangeExists(vendor.name)
            change = VendorBankChange(
                tenant_id=tenant_id,
                vendor_id=vendor.id,
                reason=reason,
                proposed_by=proposed_by,
                source_case_id=source_case_id,
                status=VendorBankChange.PENDING,
            )
            for f in BANK_FIELDS:
                setattr(change, f"proposed_{f}", proposed.get(f"bank_{f}"))
                setattr(change, f"previous_{f}", getattr(vendor, f"bank_{f}"))
            session.add(change)
            session.flush()
            session.refresh(change)
            return self._change_dict(change, vendor.name)

    def list_changes(self, tenant_id: str, status: str | None = None) -> list[dict]:
        with tenant_session(tenant_id, self.url) as session:
            query = (
                select(VendorBankChange, Vendor.name)
                .join(Vendor, Vendor.id == VendorBankChange.vendor_id)
                .where(VendorBankChange.tenant_id == tenant_id)
                .order_by(VendorBankChange.created_at.desc())
            )
            if status:
                query = query.where(VendorBankChange.status == status)
            return [self._change_dict(c, name) for c, name in session.execute(query)]

    def get_change(self, tenant_id: str, change_id: str) -> dict | None:
        with tenant_session(tenant_id, self.url) as session:
            row = session.execute(
                select(VendorBankChange, Vendor.name)
                .join(Vendor, Vendor.id == VendorBankChange.vendor_id)
                .where(VendorBankChange.tenant_id == tenant_id, VendorBankChange.id == change_id)
            ).first()
            return self._change_dict(*row) if row else None

    def decide(self, tenant_id, change_id, *, action: str, decided_by: str, note: str | None) -> dict:
        if action not in ("approve", "reject"):
            raise ValueError(action)
        with tenant_session(tenant_id, self.url) as session:
            change = session.scalar(
                select(VendorBankChange).where(
                    VendorBankChange.tenant_id == tenant_id, VendorBankChange.id == change_id
                )
            )
            if change is None:
                raise KeyError(change_id)
            if change.status != VendorBankChange.PENDING:
                raise ChangeNotPending(change.status)
            if change.proposed_by == decided_by:
                raise SelfApproval(decided_by)
            vendor = self._vendor(session, tenant_id, change.vendor_id)
            now = datetime.now(timezone.utc)
            if action == "approve":
                for f in BANK_FIELDS:
                    setattr(vendor, f"bank_{f}", getattr(change, f"proposed_{f}"))
                vendor.bank_verified_at = now
                vendor.bank_verified_by = decided_by
                change.status = VendorBankChange.APPROVED
            else:
                change.status = VendorBankChange.REJECTED
            change.decided_by = decided_by
            change.decided_at = now
            change.decision_note = note
            session.flush()
            return self._change_dict(change, vendor.name)
```

Add `Vendor, VendorBankChange` to the `from .models import ...` line and `from .. import po_matching` to the imports (check for a circular import: `po_matching` must not import `db`; it does not today).

In `service.py` add `_vendors = None` beside the other globals and:

```python
def get_vendors():
    """Suppliers and their verified bank details. `None` without a database.

    Same reasoning as `get_locks`: a file would be private to one container,
    and a bank-change approval the worker cannot see is no control at all.
    """
    global _vendors
    if _vendors is None and _database_configured():
        from ..db.repositories import PgVendorRepository  # noqa: PLC0415

        _vendors = PgVendorRepository()
    return _vendors
```

Add `"_vendors"` to the reset tuple in `tests/conftest.py`.

- [ ] **Step 4: Run tests**

Run: `./.venv/bin/python -m pytest tests/test_vendor_repository.py -q -p no:cacheprovider`
Expected: PASS (8 tests).

- [ ] **Step 5: ruff and commit**

```bash
./.venv/bin/python -m ruff check . && ./.venv/bin/python -m ruff format .
git add invoice_processing/db/repositories.py invoice_processing/api/service.py tests/conftest.py tests/test_vendor_repository.py
git commit -m "Refuse a bank change approved by the person who proposed it" -m "PgVendorRepository: upsert of the supplier list never writes bank fields; a change is applied to the vendor only by a second person's approval; one pending change per vendor." -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Task 3: `Vendor` dataclass and the pure `bank_check` module

**Files:**
- Modify: `invoice_processing/vendor_master.py` (`Vendor` ~:31-43)
- Create: `invoice_processing/bank_check.py`
- Test: `tests/test_bank_check.py` (new); `tests/test_vendor_master.py` (one class appended)

**Interfaces:**
- `vendor_master.Vendor` gains optional fields: `email_domain: str | None = None`, `bank_account_name`, `bank_account_number`, `bank_code`, `bank_iban` (all `str | None = None`), `bank_verified_at: str | None = None`, `pending_change: bool = False`, `last_bank_change_at: str | None = None`; `from_dict` reads them. `Vendor.has_bank` property: `bool(bank_account_number or bank_iban)`.
- `bank_check.compare(invoice: dict, vendor: Vendor | None, *, sender: str | None, reference_date: date, recent_days: int) -> BankCheck`
  - `BankCheck` dataclass: `signals: list[str]` (ids below, most severe first), `evidence: list[str]` (one line per signal; `["Bank details match vendor record"]` when passed and compared), `compared: bool` (False when nothing to compare), `passed: bool` (= not signals).
  - Signal ids: `BANK_MISMATCH="bank_mismatch"`, `BANK_UNVERIFIED="bank_unverified"`, `PAYEE_NAME_DIFFERS="payee_name_differs"`, `RECENT_BANK_CHANGE="recent_bank_change"`, `SENDER_DOMAIN_MISMATCH="sender_domain_mismatch"`.
  - Helpers: `norm_account(s) -> str` (digits only), `norm_code(s) -> str` (uppercase, no whitespace), `domain_of(address) -> str` (lowercase part after `@`, angle brackets stripped; `""` when none).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_bank_check.py
"""Step 3.4. Payment redirection works by changing an account number, so the
bank details printed on an invoice are compared with the ones two people
verified for the vendor. Every signal holds the invoice for review; none
rejects, because a rejection letter tells a fraudster the control exists."""

from datetime import date

from invoice_processing import bank_check
from invoice_processing.vendor_master import Vendor

TODAY = date(2026, 9, 3)
VERIFIED = Vendor(
    name="Precision Weld Fabricators",
    tax_id="27AAAAA0000A1Z5",
    email_domain="precisionweld.example",
    bank_account_name="Precision Weld Fabricators",
    bank_account_number="181386752041",
    bank_code="SCBL0477815",
    bank_verified_at="2026-01-10T00:00:00+00:00",
    last_bank_change_at="2026-01-10T00:00:00+00:00",
)
UNVERIFIED = Vendor(name="Precision Weld Fabricators", tax_id="27AAAAA0000A1Z5")


def invoice(**overrides):
    base = {
        "vendor_name": "Precision Weld Fabricators",
        "bank_account_name": "Precision Weld Fabricators",
        "bank_account_number": "181386752041",
        "bank_code": "SCBL0477815",
    }
    return {**base, **overrides}


def check(inv, vendor=VERIFIED, sender=None, recent_days=30):
    return bank_check.compare(inv, vendor, sender=sender, reference_date=TODAY, recent_days=recent_days)


class TestExactMatchIsSilent:
    def test_match_passes_with_evidence(self):
        result = check(invoice())
        assert result.passed and result.compared
        assert result.evidence == ["Bank details match vendor record"]

    def test_spaces_in_account_number_do_not_matter(self):
        assert check(invoice(bank_account_number="1813 8675 2041")).passed


class TestNothingToCompare:
    def test_no_bank_details_printed(self):
        result = check(invoice(bank_account_number=None, bank_code=None, bank_account_name=None))
        assert not result.compared and result.passed

    def test_unknown_vendor_is_3_2s_problem(self):
        assert not check(invoice(), vendor=None).compared

    def test_unverified_vendor_and_no_details_printed(self):
        result = check(invoice(bank_account_number=None, bank_code=None, bank_account_name=None), vendor=UNVERIFIED)
        assert not result.compared


class TestMismatch:
    def test_changed_account_number(self):
        result = check(invoice(bank_account_number="476661095239"))
        assert result.signals == [bank_check.BANK_MISMATCH]
        assert "…5239" in result.evidence[0] and "…2041" in result.evidence[0]

    def test_changed_code(self):
        assert check(invoice(bank_code="HDFC0001234")).signals == [bank_check.BANK_MISMATCH]

    def test_no_o_zero_folding(self):
        """MFG-INV-011: OCR read the IFSC's zero as O. A hold is the correct
        outcome -- a changed code is exactly what fraud changes."""
        assert check(invoice(bank_code="SCBLO477815")).signals == [bank_check.BANK_MISMATCH]


class TestUnverified:
    def test_first_seen_bank_details(self):
        result = check(invoice(), vendor=UNVERIFIED)
        assert result.signals == [bank_check.BANK_UNVERIFIED]
        assert "No verified bank details" in result.evidence[0]


class TestPayeeName:
    def test_third_party_payee(self):
        result = check(invoice(bank_account_name="R K Traders"))
        assert bank_check.PAYEE_NAME_DIFFERS in result.signals

    def test_legal_suffix_differences_are_not_a_signal(self):
        assert check(invoice(bank_account_name="Precision Weld Fabricators Pvt Ltd")).passed


class TestRecentChange:
    def test_pending_change(self):
        vendor = Vendor(**{**VERIFIED.__dict__, "pending_change": True})
        assert bank_check.RECENT_BANK_CHANGE in check(invoice(), vendor=vendor).signals

    def test_approved_within_window(self):
        vendor = Vendor(**{**VERIFIED.__dict__, "last_bank_change_at": "2026-08-20T10:00:00+00:00"})
        assert bank_check.RECENT_BANK_CHANGE in check(invoice(), vendor=vendor).signals

    def test_approved_outside_window(self):
        vendor = Vendor(**{**VERIFIED.__dict__, "last_bank_change_at": "2026-07-01T10:00:00+00:00"})
        assert check(invoice(), vendor=vendor).passed


class TestSenderDomain:
    def test_lookalike_domain(self):
        result = check(invoice(), sender="Billing <billing@precisionweld-invoices.example>")
        assert result.signals == [bank_check.SENDER_DOMAIN_MISMATCH]
        assert "precisionweld.example" in result.evidence[0]

    def test_matching_domain_any_case(self):
        assert check(invoice(), sender="BILLING@PrecisionWeld.example").passed

    def test_no_vendor_domain_means_no_signal(self):
        vendor = Vendor(**{**VERIFIED.__dict__, "email_domain": None})
        assert check(invoice(), vendor=vendor, sender="x@free.example").passed

    def test_upload_has_no_sender(self):
        assert check(invoice(), sender=None).passed


class TestOrdering:
    def test_most_severe_first(self):
        vendor = Vendor(**{**VERIFIED.__dict__, "pending_change": True})
        result = check(invoice(bank_account_number="1", bank_account_name="X"), vendor=vendor, sender="a@b.example")
        assert result.signals == [
            bank_check.BANK_MISMATCH,
            bank_check.PAYEE_NAME_DIFFERS,
            bank_check.RECENT_BANK_CHANGE,
            bank_check.SENDER_DOMAIN_MISMATCH,
        ]
```

Append to `tests/test_vendor_master.py`:

```python
class TestVendorRecordCarriesBankDetails:
    """The DB-backed master hands the pipeline the fields step 3.4 compares."""

    def test_from_dict_reads_bank_and_email_fields(self):
        v = vendor_master.Vendor.from_dict(
            {**ACME, "email_domain": "Cloudhaven.example", "bank_account_number": "901031925572",
             "bank_code": "KCBL0230056", "pending_change": True}
        )
        assert v.email_domain == "cloudhaven.example"
        assert v.has_bank and v.pending_change

    def test_missing_fields_default_to_none(self):
        v = vendor_master.Vendor.from_dict(ACME)
        assert v.bank_account_number is None and not v.has_bank
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/bin/python -m pytest tests/test_bank_check.py tests/test_vendor_master.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: invoice_processing.bank_check`; `TypeError` on `Vendor(email_domain=...)`.

- [ ] **Step 3: Extend `Vendor`**

```python
@dataclass(frozen=True)
class Vendor:
    name: str
    tax_id: str
    kind: str = ""  # "goods" or "service", where the tenant records it
    # Verified payee details and the state of their dual-control queue. All
    # optional: a file-backed master or a freshly imported vendor has none,
    # and step 3.4 treats "none verified" as its own signal.
    email_domain: str | None = None
    bank_account_name: str | None = None
    bank_account_number: str | None = None
    bank_code: str | None = None
    bank_iban: str | None = None
    bank_verified_at: str | None = None
    pending_change: bool = False
    last_bank_change_at: str | None = None

    @property
    def has_bank(self) -> bool:
        return bool(self.bank_account_number or self.bank_iban)

    @classmethod
    def from_dict(cls, data: dict) -> Vendor:
        def text(key):
            value = data.get(key)
            return str(value).strip() if value not in (None, "") else None

        return cls(
            name=str(data.get("name") or ""),
            tax_id=str(data.get("tax_id") or ""),
            kind=str(data.get("kind") or "").lower(),
            email_domain=(text("email_domain") or "").lower() or None,
            bank_account_name=text("bank_account_name"),
            bank_account_number=text("bank_account_number"),
            bank_code=text("bank_code"),
            bank_iban=text("bank_iban"),
            bank_verified_at=text("bank_verified_at"),
            pending_change=bool(data.get("pending_change")),
            last_bank_change_at=text("last_bank_change_at"),
        )
```

- [ ] **Step 4: Write `bank_check.py`**

```python
"""Step 3.4: the bank details printed on an invoice against the ones verified
for the vendor.

Payment redirection works by changing an account number. The extractor has
read bank details since Phase 1 and the grounder has checked they appear on
the page; this is the first thing that asks whether they are the *vendor's*.

Every signal is a hold for review, never a rejection: a rejection letter
tells the sender the control exists, and a hold lets a person phone the
vendor on a number they already had.

Comparison is deliberately literal. No O/0 folding: tolerating it on
identifiers once confirmed 24 wrong values, and a changed bank code is the
very thing this check exists to catch. OCR noise therefore lands in review,
which is the right place for it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import po_matching
from .vendor_master import Vendor

BANK_MISMATCH = "bank_mismatch"
BANK_UNVERIFIED = "bank_unverified"
PAYEE_NAME_DIFFERS = "payee_name_differs"
RECENT_BANK_CHANGE = "recent_bank_change"
SENDER_DOMAIN_MISMATCH = "sender_domain_mismatch"

# Most severe first; the order evidence is listed in.
SEVERITY = (BANK_MISMATCH, BANK_UNVERIFIED, PAYEE_NAME_DIFFERS, RECENT_BANK_CHANGE, SENDER_DOMAIN_MISMATCH)


@dataclass
class BankCheck:
    signals: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    compared: bool = False

    @property
    def passed(self) -> bool:
        return not self.signals


def norm_account(value) -> str:
    return re.sub(r"\D", "", str(value or ""))


def norm_code(value) -> str:
    return re.sub(r"\s", "", str(value or "")).upper()


def domain_of(address) -> str:
    text = str(address or "")
    if "<" in text and ">" in text:
        text = text[text.rindex("<") + 1 : text.rindex(">")]
    return text.rsplit("@", 1)[1].strip().lower() if "@" in text else ""


def _tail(value: str) -> str:
    return f"…{value[-4:]}" if len(value) > 4 else value


def _recent(stamp: str | None, reference: date, days: int) -> bool:
    if not stamp:
        return False
    try:
        when = datetime.fromisoformat(stamp).date()
    except ValueError:
        return False
    return reference - when <= timedelta(days=days)


def compare(invoice: dict, vendor: Vendor | None, *, sender: str | None, reference_date: date, recent_days: int) -> BankCheck:
    result = BankCheck()
    if vendor is None:
        return result  # unknown vendor is step 3.2's finding

    account = norm_account(invoice.get("bank_account_number"))
    iban = norm_code(invoice.get("bank_iban"))
    code = norm_code(invoice.get("bank_code"))
    payee = str(invoice.get("bank_account_name") or "")
    printed = bool(account or iban)
    if not printed:
        return result

    result.compared = True
    found: dict[str, str] = {}

    if vendor.has_bank:
        same_account = (account and account == norm_account(vendor.bank_account_number)) or (
            iban and iban == norm_code(vendor.bank_iban)
        )
        same_code = not code or not vendor.bank_code or code == norm_code(vendor.bank_code)
        if not (same_account and same_code):
            found[BANK_MISMATCH] = (
                f"Account {_tail(account or iban)} / {code or '(no code)'} on the invoice; "
                f"{vendor.name} verified as {_tail(norm_account(vendor.bank_account_number) or norm_code(vendor.bank_iban))} "
                f"/ {norm_code(vendor.bank_code) or '(no code)'}"
            )
    else:
        found[BANK_UNVERIFIED] = (
            f"No verified bank details for {vendor.name}; invoice shows "
            f"{_tail(account or iban)} / {code or '(no code)'}"
        )

    if payee and po_matching.normalize_vendor(payee) != po_matching.normalize_vendor(vendor.name):
        found[PAYEE_NAME_DIFFERS] = f"Payee '{payee}' is not the vendor '{vendor.name}'"

    if vendor.pending_change:
        found[RECENT_BANK_CHANGE] = f"A bank change for {vendor.name} is awaiting approval"
    elif _recent(vendor.last_bank_change_at, reference_date, recent_days):
        found[RECENT_BANK_CHANGE] = (
            f"Bank details for {vendor.name} changed on {vendor.last_bank_change_at[:10]}; "
            f"within {recent_days} days of this invoice"
        )

    sender_domain = domain_of(sender)
    if sender_domain and vendor.email_domain and sender_domain != vendor.email_domain:
        found[SENDER_DOMAIN_MISMATCH] = (
            f"Sent from {sender_domain}; vendor domain is {vendor.email_domain}"
        )

    result.signals = [s for s in SEVERITY if s in found]
    result.evidence = [found[s] for s in result.signals] or ["Bank details match vendor record"]
    return result
```

- [ ] **Step 5: Run tests**

Run: `./.venv/bin/python -m pytest tests/test_bank_check.py tests/test_vendor_master.py -q -p no:cacheprovider`
Expected: PASS. If `TestPayeeName::test_legal_suffix_differences_are_not_a_signal` fails, check what `po_matching.normalize_vendor` does with "Pvt Ltd" (its docstring says Pvt survives) and adjust the *test's* payee to a suffix it does drop (e.g. "Precision Weld Fabricators Ltd") — do not change the normaliser.

- [ ] **Step 6: ruff and commit**

```bash
./.venv/bin/python -m ruff check . && ./.venv/bin/python -m ruff format .
git add invoice_processing/vendor_master.py invoice_processing/bank_check.py tests/test_bank_check.py tests/test_vendor_master.py
git commit -m "Compare the bank details an invoice prints with the ones the vendor verified" -m "Five hold-for-review signals: mismatch, unverified, payee name, recent change, sender domain. Literal comparison, no O/0 folding; OCR noise is meant to land in review." -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Task 4: Step 3.4 in the pipeline, the setting, and DB-backed vendor loading

**Files:**
- Modify: `invoice_processing/shared_libraries/invoice_master_data.yaml` (phase3 steps ~:254-274; config defaults section — find where `balance_tolerance` is defined and add `bank_change_recent_days: 30` beside it)
- Modify: `invoice_processing/shared_libraries/acting/general_invoice_agent.py` (`_load_vendors` ~:2030; Phase 3 `run` after 3.2 ~:2154; `Phase3ValidatorAgent.__init__` if it needs nothing new — it does not)
- Modify: `invoice_processing/tenant_settings.py` (`SETTABLE` ~:29, `DESCRIPTIONS` ~:68, `apply_to_pipeline` ~:345)
- Test: `tests/test_vendor_master.py` (new class `TestStep34HoldsForReview`), `tests/test_tenant_settings.py` (find the existing file: `ls tests | grep settings`; add one test)

**Interfaces:**
- Consumes `bank_check.compare`, `Vendor` from Task 3; `service.get_vendors()` from Task 2.
- Produces: config keys `vendor_master_records: list[dict] | None`, `document_sender: str | None`, `bank_change_recent_days: int`; validation dict `{"step": "3.4", "rule": "Bank details match the vendor's verified account", "passed", "informational": True, "evidence": "; ".join(lines), "signals": [...], "rejection_template": None}`; on signals, `extraction["invoice"]["_routing"] = "REVIEW"` and `fields_needing_review += ["bank: <signal>", ...]`, `02_extraction.json` re-saved.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_vendor_master.py` (reuse its existing helpers `master`, `invoice`, and whatever fixture the file already uses to run `Phase3ValidatorAgent` with a patched `_load_vendors` — read the file's first test class and mirror it):

```python
class TestStep34HoldsForReview:
    """A bank mismatch holds the invoice; it never rejects."""

    VERIFIED = {**ACME, "bank_account_number": "901031925572", "bank_code": "KCBL0230056"}

    def _run(self, monkeypatch, tmp_path, records, inv, config_extra=None):
        monkeypatch.setattr(general_invoice_agent, "_load_vendors", lambda: master(*records))
        config = {**general_invoice_agent.DEFAULT_CONFIG, "duplicate_check_enabled": False, **(config_extra or {})}
        monkeypatch.setitem(general_invoice_agent._config_store, "CONFIG", config)
        extraction = {"invoice": {**inv, "_routing": "AUTO", "_evidence": {"_summary": {"fields_needing_review": []}}}}
        (tmp_path / "02_extraction.json").write_text(json.dumps(extraction))
        out = Phase3ValidatorAgent().run(tmp_path, extraction, {"decision": "CONTINUE"})
        return out, extraction

    def _step(self, out):
        return next(v for v in out["validations"] if v["step"] == "3.4")

    def test_mismatch_holds_and_does_not_reject(self, monkeypatch, tmp_path):
        out, extraction = self._run(
            monkeypatch, tmp_path, [self.VERIFIED],
            invoice(bank_account_number="476661095239", bank_code="KCBL0230056", bank_account_name=ACME["name"]),
        )
        assert out["decision"] == "CONTINUE"
        step = self._step(out)
        assert step["passed"] is False and step["informational"] is True
        assert step["signals"] == ["bank_mismatch"]
        assert extraction["invoice"]["_routing"] == "REVIEW"
        assert "bank: bank_mismatch" in extraction["invoice"]["_evidence"]["_summary"]["fields_needing_review"]
        saved = json.loads((tmp_path / "02_extraction.json").read_text())
        assert saved["invoice"]["_routing"] == "REVIEW"

    def test_match_passes_and_leaves_routing_alone(self, monkeypatch, tmp_path):
        out, extraction = self._run(
            monkeypatch, tmp_path, [self.VERIFIED],
            invoice(bank_account_number="901031925572", bank_code="KCBL0230056", bank_account_name=ACME["name"]),
        )
        assert self._step(out)["passed"] is True
        assert extraction["invoice"]["_routing"] == "AUTO"

    def test_no_bank_details_printed_adds_no_step(self, monkeypatch, tmp_path):
        out, _ = self._run(monkeypatch, tmp_path, [self.VERIFIED], invoice())
        assert all(v["step"] != "3.4" for v in out["validations"])

    def test_sender_domain_comes_from_config(self, monkeypatch, tmp_path):
        out, _ = self._run(
            monkeypatch, tmp_path, [{**self.VERIFIED, "email_domain": "cloudhaven.example"}],
            invoice(bank_account_number="901031925572", bank_code="KCBL0230056", bank_account_name=ACME["name"]),
            config_extra={"document_sender": "billing@cloudhaven-pay.example"},
        )
        assert self._step(out)["signals"] == ["sender_domain_mismatch"]

    def test_disabled_check_is_skipped(self, monkeypatch, tmp_path):
        out, extraction = self._run(
            monkeypatch, tmp_path, [self.VERIFIED],
            invoice(bank_account_number="1", bank_code="X", bank_account_name=ACME["name"]),
            config_extra={"disabled_checks": ["3.4"]},
        )
        assert all(v["step"] != "3.4" for v in out["validations"])
        assert extraction["invoice"]["_routing"] == "AUTO"


class TestVendorsFromTheDatabase:
    def test_load_vendors_prefers_injected_records(self, monkeypatch):
        monkeypatch.setitem(
            general_invoice_agent._config_store, "CONFIG",
            {**general_invoice_agent.DEFAULT_CONFIG, "vendor_master_records": [ACME], "vendor_master_file": "/nonexistent.json"},
        )
        assert general_invoice_agent._load_vendors().find(ACME["name"]) is not None

    def test_empty_records_fall_back_to_the_file(self, monkeypatch, tmp_path):
        path = tmp_path / "vendors.json"
        path.write_text(json.dumps({"vendors": [OTHER]}))
        monkeypatch.setitem(
            general_invoice_agent._config_store, "CONFIG",
            {**general_invoice_agent.DEFAULT_CONFIG, "vendor_master_records": [], "vendor_master_file": str(path)},
        )
        assert general_invoice_agent._load_vendors().find(OTHER["name"]) is not None
```

In the tenant-settings test file add:

```python
def test_bank_change_recent_days_is_settable_and_defaults_to_30():
    assert tenant_settings.validate({"bank_change_recent_days": 45}) == {"bank_change_recent_days": 45}
    assert tenant_settings.effective("nobody", store={})["bank_change_recent_days"] == 30
```

(`effective` accepts a `store` with `.get`; if a plain dict does not satisfy the existing tests' pattern, use whatever fake store that file already uses.)

Also one test that `apply_to_pipeline` injects DB records, in the same file:

```python
def test_apply_to_pipeline_injects_vendor_records(monkeypatch):
    class Repo:
        def load_records(self, tenant_id):
            return [{"name": "Acme", "tax_id": "X", "kind": ""}]
    from invoice_processing.api import service
    monkeypatch.setattr(service, "get_vendors", lambda: Repo())
    config = tenant_settings.apply_to_pipeline("t1", store={})
    assert config["vendor_master_records"] == [{"name": "Acme", "tax_id": "X", "kind": ""}]
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/bin/python -m pytest tests/test_vendor_master.py tests/test_tenant_settings.py -q -p no:cacheprovider`
Expected: FAIL — no step "3.4"; `KeyError: 'bank_change_recent_days'`; `KeyError: 'vendor_master_records'`.

- [ ] **Step 3: YAML**

In phase3 `steps`, after 3.3:

```yaml
        - id: "3.4"
          name: "Bank Details"
          type: "bank_details_match_vendor"
          field: "extraction.invoice.bank_account_number"
          recent_days_from_config: "bank_change_recent_days"
          informational_only: true
```

In the config-defaults block (where `balance_tolerance` lives), add:

```yaml
  bank_change_recent_days: 30   # a bank change approved within this many days of an invoice is a review signal
```

Verify `DEFAULT_CONFIG["bank_change_recent_days"] == 30` with `./.venv/bin/python -c "from invoice_processing.shared_libraries.acting.general_invoice_agent import DEFAULT_CONFIG as c; print(c['bank_change_recent_days'])"`. If `_load_config_defaults()` reads a different YAML section than you edited, follow where `balance_tolerance` actually comes from.

- [ ] **Step 4: `_load_vendors` and Phase 3**

```python
def _load_vendors() -> "vendor_master.VendorMaster":
    """This tenant's supplier list, injected through config by the worker.

    Records come from the database where there is one (the API and the worker
    are different containers, so a file written by one is invisible to the
    other); the file path is the single-process fallback the benchmark and the
    local stack still use.
    """
    config = _get_config()
    records = config.get("vendor_master_records")
    if records:
        return vendor_master.VendorMaster(records=list(records))
    return vendor_master.VendorMaster(config.get("vendor_master_file"))
```

In `Phase3ValidatorAgent.run`, after the 3.2 block and before 3.3 (so the step order in the artifact stays numeric: put it after 3.3 instead, immediately before `failed = _settle(validations)`):

```python
        # Step 3.4: Bank details against the vendor's verified account. The
        # payment-redirection control. Holds, never rejects -- see bank_check.
        if "3.4" not in (_get_config().get("disabled_checks") or []):
            vendor = match.vendor if match.outcome != vendor_master.NOT_CONFIGURED else None
            result = bank_check.compare(
                invoice,
                vendor,
                sender=_get_config().get("document_sender"),
                reference_date=today,
                recent_days=int(_get_config().get("bank_change_recent_days") or 30),
            )
            if result.compared:
                validations.append(
                    {
                        "step": "3.4",
                        "rule": "Bank details match the vendor's verified account",
                        "passed": result.passed,
                        "informational": True,
                        "evidence": "; ".join(result.evidence),
                        "signals": result.signals,
                        "rejection_template": None,
                    }
                )
                if not result.passed:
                    # The routing decision was made at extraction time from
                    # grounding alone. A bank signal is the strongest cheap
                    # reason there is to put a person in front of an invoice,
                    # so it reopens that decision and the artifact is re-saved
                    # for every reader that takes routing from it.
                    invoice["_routing"] = "REVIEW"
                    summary = invoice.setdefault("_evidence", {}).setdefault("_summary", {})
                    needing = summary.setdefault("fields_needing_review", [])
                    needing.extend(f"bank: {s}" for s in result.signals if f"bank: {s}" not in needing)
                    self.save_artifact(output_folder, "02_extraction.json", extraction)
```

`match` is the `VendorMatch` from 3.2 — it is already in scope (`match = vendor_master.check_vendor_tax_id(...)`). `today` is `_reference_date()` from 3.3 — move its assignment above 3.3 if needed. Add `from .. import bank_check` beside the existing `vendor_master` import (same relative form as `vendor_master` is imported at ~:75/106).

- [ ] **Step 5: Settings**

In `tenant_settings.SETTABLE` add `"bank_change_recent_days": (int, float),` with a comment; in `DESCRIPTIONS` add `"bank_change_recent_days": "An invoice paid to bank details approved within this many days is held for review. The window in which a fraudulent change is most often caught."`. In `apply_to_pipeline`, after the `vendor_master_file` line:

```python
    # The database is the record of truth for vendors where there is one; the
    # file stays as the single-process fallback. Empty records (a stack seeded
    # by files, as the benchmark is) fall through to the file in _load_vendors.
    from .api import service  # noqa: PLC0415

    vendors = service.get_vendors()
    config["vendor_master_records"] = vendors.load_records(tenant_id) if vendors else None
```

(If `service` import inside `tenant_settings` creates a cycle at import time, it does not: it is already imported lazily inside `effective`.)

- [ ] **Step 6: Run the focused tests, then the whole suite**

Run: `./.venv/bin/python -m pytest tests/test_vendor_master.py tests/test_tenant_settings.py tests/test_bank_check.py -q -p no:cacheprovider` → PASS.
Run: `./.venv/bin/python -m pytest tests/ -q -p no:cacheprovider` → all pass (expect ~1700). A test asserting the exact list of settable keys or the step catalogue count must be updated to include the new key / step 3.4.

- [ ] **Step 7: ruff and commit**

```bash
./.venv/bin/python -m ruff check . && ./.venv/bin/python -m ruff format .
git add invoice_processing/shared_libraries/invoice_master_data.yaml invoice_processing/shared_libraries/acting/general_invoice_agent.py invoice_processing/tenant_settings.py tests/
git commit -m "Hold an invoice for review when its bank details are not the vendor's" -m "Step 3.4, informational: a bank signal reopens the routing decision made at extraction and re-saves the extraction artifact so every reader sees REVIEW. Vendors load from the database when there is one." -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Task 5: Capture the email sender and hand it to the pipeline

**Files:**
- Modify: `invoice_processing/api/ingest.py` (`InboundMessage` ~:364-385; `_parse_mime` ~:401; `_parse_postmark` ~:420; `_parse_sendgrid` ~:454; `ingest_document` signature ~:131 and `documents.create(...)` ~:179; the email webhook call ~:582-593)
- Modify: `invoice_processing/adapters.py` `JsonDocumentRepository.create` — verify it stores arbitrary `**fields` (it should; if it whitelists, add `sender`, `reply_to`)
- Modify: `invoice_processing/worker/runner.py` (`process_document` ~:219-221)
- Test: `tests/test_ingestion.py`, `tests/test_worker.py`

**Interfaces:**
- `InboundMessage` gains `sender: str = ""`, `reply_to: str = ""` (dataclass defaults, so the existing positional constructions `InboundMessage("", "", raw, [])` keep working).
- `ingest_document(..., sender: str | None = None, reply_to: str | None = None)` passes both to `documents.create`.
- Worker sets `settings["document_sender"] = document.get("reply_to") or document.get("sender") or None` where `settings` is the dict `apply_to_pipeline` returned (it *is* `_config_store["CONFIG"]`).

- [ ] **Step 1: Write the failing tests**

In `tests/test_ingestion.py`, inside `class TestProviderPayloads` (the class holding `test_postmark_json_with_base64_attachments`) add:

```python
    def test_postmark_sender_and_reply_to(self):
        body = json.dumps(
            {
                "OriginalRecipient": "acme@invoices.example.com",
                "MessageID": "pm-2",
                "FromFull": {"Email": "billing@vendor.test", "Name": "Billing"},
                "ReplyTo": "pay-me@lookalike.test",
                "Attachments": [],
            }
        ).encode()
        parsed = ingest.parse_inbound(body, "application/json")
        assert parsed.sender == "billing@vendor.test"
        assert parsed.reply_to == "pay-me@lookalike.test"

    def test_sendgrid_sender(self):
        boundary = "xYzBoundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="to"\r\n\r\n'
            "acme@invoices.example.com\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="from"\r\n\r\n'
            "Vendor Billing <billing@vendor.test>\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        parsed = ingest.parse_inbound(body, f"multipart/form-data; boundary={boundary}")
        assert parsed.sender == "billing@vendor.test"

    def test_raw_mime_sender_and_reply_to(self):
        message = EmailMessage()
        message["From"] = "Vendor <supplier@vendor.test>"
        message["Reply-To"] = "pay-me@lookalike.test"
        message["To"] = "acme@invoices.example.com"
        message.set_content("x")
        parsed = ingest.parse_inbound(message.as_bytes(), "message/rfc822")
        assert parsed.sender == "supplier@vendor.test"
        assert parsed.reply_to == "pay-me@lookalike.test"
```

In `class TestInboundEmailIngestion` add:

```python
    def test_sender_is_stored_on_the_document(self, client, wired):
        """The sender-domain BEC signal reads it from here; the raw .eml alone
        would mean parsing a blob on every pipeline run."""
        _, documents, _ = wired
        post_email(client, build_email([("a.pdf", PDF, "pdf")]))
        assert documents.rows["doc-1"]["sender"] == "vendor@supplier.test"
        assert documents.rows["doc-1"]["reply_to"] is None
```

In `tests/test_worker.py`, next to `test_a_dict_backed_repository_sees_the_logical_keys`, add:

```python
    def test_the_email_sender_reaches_the_pipeline_config(self, tmp_path, monkeypatch):
        """Reply-To wins: it is where a reply would go, which is what a
        fraudster redirects."""
        output = tmp_path / "case-2"
        output.mkdir()
        (output / "08_decision.json").write_text(json.dumps({"final_decision": "REJECT"}))
        seen = {}

        def capture_settings(tenant_id):
            return seen

        class Sink:
            def put_artifact(self, ctx, key, payload): ...
            def get_artifacts(self, ctx): return {}
            def project_case(self, ctx, artifacts): ...

        class Blobs:
            def get(self, key): return b"%PDF-1.4"

        monkeypatch.setattr(gia, "get_output_folder", _folder(output))
        monkeypatch.setattr(gia, "process_invoice", _rejects)
        monkeypatch.setattr(tenant_settings, "apply_to_pipeline", capture_settings)
        process_document(
            RunContext(tenant_id="t", case_id="case-2"),
            {"id": "case-2", "storage_key": "k", "sha256": "s", "source": "email",
             "sender": "a@vendor.test", "reply_to": "b@lookalike.test"},
            cases=Sink(),
            blobs=Blobs(),
        )
        assert seen["document_sender"] == "b@lookalike.test"
```

(`_apply_rules` is called inside `process_document`; if it reaches for `service.get_rules()` and fails on this bare test, follow how the existing worker tests neutralise it — grep `_apply_rules` in `tests/test_worker.py` and copy the monkeypatch.)

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/bin/python -m pytest tests/test_ingestion.py tests/test_worker.py -q -p no:cacheprovider -k "sender or reply_to"`
Expected: FAIL — `AttributeError: 'InboundMessage' object has no attribute 'sender'`; `KeyError: 'sender'`; `KeyError: 'document_sender'`.

- [ ] **Step 3: Parse the sender**

Add a helper near `_first_address` in `ingest.py`:

```python
def _bare_address(value) -> str:
    """`Name <a@b>` -> `a@b`; a Postmark `FromFull` dict -> its Email."""
    if isinstance(value, dict):
        value = value.get("Email") or value.get("email") or ""
    text = str(value or "").strip()
    if "<" in text and ">" in text:
        text = text[text.rindex("<") + 1 : text.rindex(">")]
    return text.strip().lower()
```

`InboundMessage`: add `sender: str = ""` and `reply_to: str = ""` after `attachments`.

`_parse_mime`: inside the `try`, after `message_id = ...`, add `sender = _bare_address(message.get("From"))` and `reply_to = _bare_address(message.get("Reply-To"))` (initialise both to `""` before the try); pass them to the constructor.

`_parse_postmark`: in the RawEmail branch nothing changes (the MIME carries From/Reply-To). In the JSON-only branch add `sender=_bare_address(body.get("FromFull") or body.get("From")), reply_to=_bare_address(body.get("ReplyTo"))`.

`_parse_sendgrid`: in the MIME branch nothing changes. In the fields branch add `sender=_bare_address(fields.get("from")), reply_to=_bare_address(fields.get("reply-to") or "")`.

- [ ] **Step 4: Store it and pass it on**

`ingest_document`: add keyword parameters `sender: str | None = None, reply_to: str | None = None`; pass `sender=sender or None, reply_to=reply_to or None` to `documents.create(...)`. In the webhook loop (~:582) pass `sender=inbound.sender, reply_to=inbound.reply_to`. Add `"sender": inbound.sender` to the `email_received` audit payload.

`runner.process_document`, right after `settings = apply_to_pipeline(ctx.tenant_id)`:

```python
        # Where the email said replies go, for the sender-domain signal in
        # step 3.4. The pipeline reads config, not the document row, so it is
        # handed over here. Reply-To beats From: it is what a fraudster sets.
        settings["document_sender"] = (
            document.get("reply_to") or document.get("sender") or None
        )
```

Check `adapters.JsonDocumentRepository.create` keeps unknown fields (open the file and read `create`). If it builds the row from a fixed set of keys, add `sender` and `reply_to`.

- [ ] **Step 5: Run tests**

Run: `./.venv/bin/python -m pytest tests/test_ingestion.py tests/test_worker.py -q -p no:cacheprovider` → PASS.

- [ ] **Step 6: ruff and commit**

```bash
./.venv/bin/python -m ruff check . && ./.venv/bin/python -m ruff format .
git add invoice_processing/api/ingest.py invoice_processing/adapters.py invoice_processing/worker/runner.py tests/test_ingestion.py tests/test_worker.py
git commit -m "Record who an emailed invoice came from, and tell the pipeline" -m "From and Reply-To stored on the document; the worker hands Reply-To (else From) to step 3.4 as document_sender. Until now the sender lived only inside the raw .eml blob." -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Task 6: Vendor API routes, the workbench shortcut, and isolation probes

**Files:**
- Create: `invoice_processing/api/vendor_routes.py`
- Modify: `invoice_processing/api/config_routes.py` — delete `list_vendors` and `replace_vendors` (~:300-392) and `VendorUpdate` (~:49-50); keep `vendor_master`/`master_files` imports only if still used (ruff will tell you)
- Modify: `invoice_processing/api/service.py` — `include_router(vendor_router)` (~:1243-1257); `POST /api/invoices/{case_id}/bank-change` after `act` (~:1053)
- Modify: `tests/test_tenant_isolation_api.py` (`PROBED` ~:792; `test_vendors_do_not_cross_tenants` ~:254; `test_no_endpoint_reads_a_tenant_from_the_request` ~:862 add `vendor_routes`)
- Create: `tests/test_vendor_routes.py`

**Interfaces:**
- Consumes `service.get_vendors()`, `PgVendorRepository` + exceptions (Task 2), `require_role`, `Principal.context()`.
- Produces the routes in the spec table. Response shapes:
  - `GET /api/vendors` → `{"vendors": [VendorOut...], "count": n, "source": "database" | "<file>.json"}`; `VendorOut = {id, name, tax_id, kind, email_domain, bank_status: "verified"|"unverified"|"pending", bank_account_masked: "…5572"|None, bank_code, bank_account_name, bank_verified_at, pending_change_id}`.
  - `POST .../bank-changes` → the change dict with `proposed_account_number`/`previous_account_number` **masked** (`"…1234"`), 201.
  - `GET /api/vendors/bank-changes` → `{"changes": [...masked...], "count"}`.
  - `POST .../decide` → the masked change dict.
  - `POST /api/invoices/{case_id}/bank-change` → same as propose; 404 unknown case; 409 no vendor in master / no bank details on the invoice / pending exists.
  - Without a database: GET/PUT behave as today on the file; every other route → 503 `"Bank-change control needs a database. Configure DATABASE_URL."`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_vendor_routes.py
"""Dual control over the API. The repository refuses self-approval; these
tests pin the HTTP contract the UI relies on and that masking holds at the
boundary."""

import pytest
from fastapi.testclient import TestClient

from invoice_processing.adapters import JsonlAuditSink
from invoice_processing.api import auth, service
from invoice_processing.api.service import app
from invoice_processing.db import models
from invoice_processing.db.repositories import PgVendorRepository
from invoice_processing.db.session import create_all, tenant_session

TENANT = "tenant-acme"
ACME = {"name": "Cloudhaven Infra Services Pvt Ltd", "tax_id": "29ZOICE4828P1ZD", "kind": "service"}
BANK = {"bank_account_name": ACME["name"], "bank_account_number": "901031925572", "bank_code": "KCBL0230056"}


class FakeVerifier:
    def verify(self, token):
        role, _, who = token.partition(":")
        if role not in ("viewer", "clerk", "approver", "admin"):
            raise auth.AuthError("Invalid token")
        return {"sub": f"u-{token}", "email": f"{who or role}@x.test", "email_verified": True,
                "tenant_id": TENANT, "role": role}


@pytest.fixture
def env(tmp_path, monkeypatch, scratch_db):
    url = scratch_db("vendors.db")
    create_all(url)
    with tenant_session("", url) as session:
        session.add(models.Tenant(id=TENANT, name="Acme", slug="acme", status="active"))
    repo = PgVendorRepository(url)
    monkeypatch.setattr(service, "_vendors", repo)
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr(service, "_audit", JsonlAuditSink(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(auth, "_verifier", FakeVerifier())
    monkeypatch.delenv("AUTH_DISABLED", raising=False)

    def client(token="admin"):
        return TestClient(app, headers={"Authorization": f"Bearer {token}"})

    client("admin").put("/api/vendors", json={"vendors": [ACME]})
    vendor_id = client("clerk").get("/api/vendors").json()["vendors"][0]["id"]
    return {"client": client, "repo": repo, "vendor_id": vendor_id, "tmp": tmp_path}


def propose(env, token="clerk:alice", **extra):
    return env["client"](token).post(
        f"/api/vendors/{env['vendor_id']}/bank-changes",
        json={**BANK, "reason": "New account on letterhead", **extra},
    )


class TestList:
    def test_list_masks_and_reports_status(self, env):
        body = env["client"]("clerk").get("/api/vendors").json()
        (v,) = body["vendors"]
        assert body["source"] == "database"
        assert v["bank_status"] == "unverified" and v["bank_account_masked"] is None

    def test_viewer_cannot_list(self, env):
        assert env["client"]("viewer").get("/api/vendors").status_code == 403

    def test_bulk_put_never_writes_bank_fields(self, env):
        env["client"]("admin").put("/api/vendors", json={"vendors": [{**ACME, "bank_account_number": "000"}]})
        assert env["repo"].get(TENANT, env["vendor_id"])["bank_account_number"] is None


class TestPropose:
    def test_clerk_proposes_and_sees_masked_values(self, env):
        response = propose(env)
        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "pending" and body["proposed_by"] == "alice@x.test"
        assert body["proposed_account_number"] == "…5572"
        listed = env["client"]("clerk").get("/api/vendors").json()["vendors"][0]
        assert listed["bank_status"] == "pending" and listed["pending_change_id"] == body["id"]

    def test_viewer_cannot_propose(self, env):
        assert propose(env, token="viewer").status_code == 403

    def test_reason_is_required(self, env):
        response = env["client"]("clerk").post(
            f"/api/vendors/{env['vendor_id']}/bank-changes", json={**BANK, "reason": ""}
        )
        assert response.status_code == 400

    def test_second_pending_is_409(self, env):
        propose(env)
        assert propose(env).status_code == 409

    def test_queue_lists_pending_newest_first(self, env):
        change = propose(env).json()
        body = env["client"]("clerk").get("/api/vendors/bank-changes?status=pending").json()
        assert [c["id"] for c in body["changes"]] == [change["id"]]
        assert body["changes"][0]["vendor_name"] == ACME["name"]


class TestDecide:
    def test_proposer_cannot_approve(self, env):
        change = propose(env, token="clerk:alice").json()
        # alice is also an approver in this scenario; the rule is about identity, not role
        response = env["client"]("approver:alice").post(
            f"/api/vendors/bank-changes/{change['id']}/decide", json={"action": "approve"}
        )
        assert response.status_code == 403
        assert "proposed" in response.json()["detail"]

    def test_clerk_cannot_decide(self, env):
        change = propose(env).json()
        response = env["client"]("clerk:bob").post(
            f"/api/vendors/bank-changes/{change['id']}/decide", json={"action": "approve"}
        )
        assert response.status_code == 403

    def test_approve_verifies_the_vendor(self, env):
        change = propose(env).json()
        response = env["client"]("approver:bob").post(
            f"/api/vendors/bank-changes/{change['id']}/decide",
            json={"action": "approve", "note": "Called the number on file"},
        )
        assert response.status_code == 200 and response.json()["status"] == "approved"
        (v,) = env["client"]("clerk").get("/api/vendors").json()["vendors"]
        assert v["bank_status"] == "verified" and v["bank_account_masked"] == "…5572"
        assert v["bank_code"] == "KCBL0230056"
        audit = (env["tmp"] / "audit.jsonl").read_text()
        assert "vendor_bank_change_approved" in audit

    def test_reject_needs_a_note_and_is_terminal(self, env):
        change = propose(env).json()
        url = f"/api/vendors/bank-changes/{change['id']}/decide"
        assert env["client"]("approver:bob").post(url, json={"action": "reject"}).status_code == 400
        assert env["client"]("approver:bob").post(url, json={"action": "reject", "note": "unverified"}).status_code == 200
        assert env["client"]("approver:bob").post(url, json={"action": "approve"}).status_code == 409


class TestEmailDomain:
    def test_admin_patches_domain(self, env):
        response = env["client"]("admin").patch(
            f"/api/vendors/{env['vendor_id']}", json={"email_domain": "Cloudhaven.example"}
        )
        assert response.status_code == 200 and response.json()["email_domain"] == "cloudhaven.example"

    def test_approver_cannot_patch(self, env):
        assert env["client"]("approver").patch(
            f"/api/vendors/{env['vendor_id']}", json={"email_domain": "x.example"}
        ).status_code == 403


class TestWithoutADatabase:
    def test_bank_change_routes_say_so(self, monkeypatch, tmp_path):
        monkeypatch.setattr(service, "_vendors", None)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("CLOUD_SQL_INSTANCE", raising=False)
        monkeypatch.setattr(auth, "_verifier", FakeVerifier())
        monkeypatch.delenv("AUTH_DISABLED", raising=False)
        client = TestClient(app, headers={"Authorization": "Bearer clerk"})
        response = client.post("/api/vendors/x/bank-changes", json={**BANK, "reason": "r"})
        assert response.status_code == 503
        assert "database" in response.json()["detail"].lower()
```

Then the workbench shortcut, appended to the same file (it needs a case; reuse the golden fixtures the way `tests/test_tenant_isolation_api.py` does with `FsCaseRepository(cases_root, tenant_id=...)` and `GOLDEN_CASES`):

```python
class TestWorkbenchShortcut:
    @pytest.fixture
    def with_case(self, env, tmp_path, monkeypatch):
        import shutil
        from golden import GOLDEN_CASES
        from invoice_processing.adapters import FsCaseRepository

        cases_root = tmp_path / "agent_output"
        shutil.copytree(GOLDEN_CASES, cases_root)
        cases = FsCaseRepository(cases_root, tenant_id=TENANT)
        monkeypatch.setattr(service, "_cases", cases)
        case_id = sorted(p.name for p in cases_root.iterdir() if p.is_dir())[0]
        # Point the case at our vendor and give it bank details to propose.
        from invoice_processing.ports import RunContext
        ctx = RunContext(tenant_id=TENANT, case_id=case_id)
        extraction = cases.get_artifacts(ctx)["extraction"]
        extraction["invoice"].update({"vendor_name": ACME["name"], **BANK})
        cases.put_artifact(ctx, "extraction", extraction)
        return case_id

    def test_proposes_from_the_invoice(self, env, with_case):
        response = env["client"]("clerk:alice").post(f"/api/invoices/{with_case}/bank-change", json={"reason": "Printed on invoice"})
        assert response.status_code == 201
        body = response.json()
        assert body["source_case_id"] == with_case
        assert body["proposed_account_number"] == "…5572"

    def test_unknown_case_is_404(self, env):
        assert env["client"]("clerk").post("/api/invoices/nope/bank-change", json={"reason": "r"}).status_code == 404
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/bin/python -m pytest tests/test_vendor_routes.py -q -p no:cacheprovider`
Expected: FAIL — 404/405 on the new paths; `KeyError: 'id'` on the list (old file route has no ids).

- [ ] **Step 3: Write `vendor_routes.py`**

```python
"""Suppliers, their verified bank details, and the two-person queue that
changes them.

Payment redirection is the fraud this exists for: a convincing email asks AP
to update a vendor's account, one person does, the next invoice is paid to the
attacker. So a bank change here is proposed by one person and applied by a
different one, and the repository refuses the same email in both roles.

Where no database is configured the supplier list still works from the
per-tenant file as it always did, and the bank-change routes say plainly that
they need a database: the API and the worker are separate containers in any
real deployment, and a queue on one container's disk is not a control.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .. import master_files, po_matching, vendor_master
from ..db import repositories as repos
from .auth import Principal, require_role

router = APIRouter()

NEEDS_DB = "Bank-change control needs a database. Configure DATABASE_URL."


class VendorUpdate(BaseModel):
    vendors: list[dict[str, Any]]


class VendorPatch(BaseModel):
    email_domain: str | None = None


class BankChangeProposal(BaseModel):
    bank_account_name: str | None = None
    bank_account_number: str | None = None
    bank_code: str | None = None
    bank_iban: str | None = None
    reason: str = ""
    source_case_id: str | None = None


class BankChangeDecision(BaseModel):
    action: str  # approve | reject
    note: str | None = None


def mask(value: str | None) -> str | None:
    if not value:
        return None
    digits = str(value).replace(" ", "")
    return f"…{digits[-4:]}" if len(digits) > 4 else digits


def vendor_out(v: dict) -> dict:
    return {
        "id": v["id"],
        "name": v["name"],
        "tax_id": v["tax_id"],
        "kind": v.get("kind", ""),
        "email_domain": v.get("email_domain"),
        "bank_status": "pending" if v.get("pending_change_id") else ("verified" if v.get("bank_account_number") or v.get("bank_iban") else "unverified"),
        "bank_account_masked": mask(v.get("bank_account_number") or v.get("bank_iban")),
        "bank_account_name": v.get("bank_account_name"),
        "bank_code": v.get("bank_code"),
        "bank_verified_at": v.get("bank_verified_at"),
        "bank_verified_by": v.get("bank_verified_by"),
        "pending_change_id": v.get("pending_change_id"),
    }


def change_out(c: dict) -> dict:
    out = dict(c)
    for key in ("proposed_account_number", "previous_account_number", "proposed_iban", "previous_iban"):
        out[key] = mask(c.get(key))
    return out


def _repo():
    from . import service  # noqa: PLC0415

    return service.get_vendors()


def _validate_list(records: list[dict]) -> None:
    seen: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise HTTPException(400, f"Entry {index + 1} is not an object.")
        name = str(record.get("name") or "").strip()
        if not name:
            raise HTTPException(400, f"Entry {index + 1} has no supplier name.")
        if not str(record.get("tax_id") or "").strip():
            raise HTTPException(400, f"{name} has no tax ID. Without one there is nothing to compare an invoice against, and check 3.2 would report a match it never made.")
        key = po_matching.normalize_vendor(name)
        if key in seen:
            raise HTTPException(400, f"{name} appears twice. The check would use whichever loaded last.")
        seen.add(key)


@router.get("/api/vendors")
def list_vendors(principal: Annotated[Principal, Depends(require_role("clerk"))]) -> dict:
    repo = _repo()
    if repo is None:
        from .. import paths  # noqa: PLC0415

        path = paths.vendor_master_file(principal.tenant_id)
        records = master_files.read_records(path, vendor_master.RECORD_KEY)
        vendors = [vendor_out({"id": "", **r, "pending_change_id": None}) for r in records]
        return {"vendors": vendors, "count": len(vendors), "source": path.name}
    vendors = [vendor_out(v) for v in repo.list(principal.tenant_id)]
    return {"vendors": vendors, "count": len(vendors), "source": "database"}


@router.put("/api/vendors")
def replace_vendors(request: VendorUpdate, principal: Annotated[Principal, Depends(require_role("admin"))]) -> dict:
    """Bulk-load name, tax ID and kind. Bank details and email domain are
    ignored on purpose: an export is one person's file."""
    _validate_list(request.vendors)
    plain = [{"name": r["name"], "tax_id": r["tax_id"], "kind": r.get("kind", "")} for r in request.vendors]
    repo = _repo()
    if repo is None:
        from .. import paths  # noqa: PLC0415

        master_files.write_records(paths.vendor_master_file(principal.tenant_id), vendor_master.RECORD_KEY, plain)
    else:
        repo.upsert_many(principal.tenant_id, plain)
    from . import service  # noqa: PLC0415

    service.get_audit().emit(principal.context(), "vendors_replaced", {"count": len(plain)})
    return {"count": len(plain), "message": f"Loaded {len(plain)} suppliers."}


@router.patch("/api/vendors/{vendor_id}")
def patch_vendor(vendor_id: str, request: VendorPatch, principal: Annotated[Principal, Depends(require_role("admin"))]) -> dict:
    repo = _repo()
    if repo is None:
        raise HTTPException(503, NEEDS_DB)
    before = repo.get(principal.tenant_id, vendor_id)
    if before is None:
        raise HTTPException(404, "Unknown vendor")
    after = repo.patch(principal.tenant_id, vendor_id, email_domain=request.email_domain)
    from . import service  # noqa: PLC0415

    service.get_audit().emit(principal.context(), "vendor_updated", {"vendor_id": vendor_id, "email_domain": after["email_domain"]}, before={"email_domain": before["email_domain"]})
    return vendor_out(after)


def propose_change(principal: Principal, vendor_id: str, proposal: BankChangeProposal) -> dict:
    """Shared by the vendors page and the invoice shortcut."""
    repo = _repo()
    if repo is None:
        raise HTTPException(503, NEEDS_DB)
    if not proposal.reason.strip():
        raise HTTPException(400, "A bank change needs a reason the approver can check.")
    if not (proposal.bank_account_number or proposal.bank_iban):
        raise HTTPException(400, "A bank change needs an account number or IBAN.")
    try:
        change = repo.propose(
            principal.tenant_id, vendor_id,
            proposed=proposal.model_dump(include={"bank_account_name", "bank_account_number", "bank_code", "bank_iban"}),
            reason=proposal.reason.strip(), proposed_by=principal.email, source_case_id=proposal.source_case_id,
        )
    except KeyError:
        raise HTTPException(404, "Unknown vendor") from None
    except repos.PendingChangeExists as exc:
        raise HTTPException(409, f"A bank change for {exc} is already awaiting approval.") from None
    from . import service  # noqa: PLC0415

    service.get_audit().emit(principal.context(proposal.source_case_id or ""), "vendor_bank_change_proposed", change_out(change))
    return change_out(change)


@router.post("/api/vendors/{vendor_id}/bank-changes", status_code=201)
def propose_bank_change(vendor_id: str, request: BankChangeProposal, principal: Annotated[Principal, Depends(require_role("clerk"))]) -> dict:
    return propose_change(principal, vendor_id, request)


@router.get("/api/vendors/bank-changes")
def list_bank_changes(principal: Annotated[Principal, Depends(require_role("clerk"))], status: str | None = Query(default=None)) -> dict:
    repo = _repo()
    if repo is None:
        raise HTTPException(503, NEEDS_DB)
    changes = [change_out(c) for c in repo.list_changes(principal.tenant_id, status)]
    return {"changes": changes, "count": len(changes)}


@router.post("/api/vendors/bank-changes/{change_id}/decide")
def decide_bank_change(change_id: str, request: BankChangeDecision, principal: Annotated[Principal, Depends(require_role("approver"))]) -> dict:
    repo = _repo()
    if repo is None:
        raise HTTPException(503, NEEDS_DB)
    if request.action not in ("approve", "reject"):
        raise HTTPException(400, "action must be approve or reject")
    if request.action == "reject" and not (request.note or "").strip():
        raise HTTPException(400, "A rejection needs a note the proposer can act on.")
    try:
        change = repo.decide(principal.tenant_id, change_id, action=request.action, decided_by=principal.email, note=request.note)
    except KeyError:
        raise HTTPException(404, "Unknown bank change") from None
    except repos.SelfApproval:
        raise HTTPException(403, "The person who proposed a bank change cannot approve it.") from None
    except repos.ChangeNotPending as exc:
        raise HTTPException(409, f"This change was already {exc}.") from None
    from . import service  # noqa: PLC0415

    service.get_audit().emit(
        principal.context(change.get("source_case_id") or ""),
        "vendor_bank_change_approved" if request.action == "approve" else "vendor_bank_change_rejected",
        change_out(change),
    )
    return change_out(change)
```

**Route order matters:** FastAPI matches in declaration order, and `/api/vendors/bank-changes` must be declared *before* `/api/vendors/{vendor_id}` would swallow it — the GET on `/api/vendors/bank-changes` collides with nothing (PATCH/POST on `{vendor_id}` are different methods), but keep `list_bank_changes` above `patch_vendor` anyway and add a comment saying why.

Delete `list_vendors`, `replace_vendors`, `VendorUpdate` from `config_routes.py`. In `service.py` add `from .vendor_routes import router as vendor_router  # noqa: E402` and `app.include_router(vendor_router)`.

- [ ] **Step 4: The workbench shortcut in `service.py`** (after `act`)

```python
class BankChangeFromInvoice(BaseModel):
    reason: str = ""


@app.post("/api/invoices/{case_id}/bank-change", status_code=201)
def propose_bank_change_from_invoice(
    case_id: str,
    request: BankChangeFromInvoice,
    principal: Annotated[Principal, Depends(require_role("clerk"))],
) -> dict:
    """Raise a vendor bank change from the details printed on this invoice.

    A reviewer holding an invoice on a bank signal has the new details in
    front of them; retyping them on another page is where transcription errors
    come from. Still only a proposal: a second person approves.
    """
    from . import vendor_routes  # noqa: PLC0415

    repo = get_vendors()
    if repo is None:
        raise HTTPException(status_code=503, detail=vendor_routes.NEEDS_DB)
    ctx = principal.context(case_id)
    artifacts = get_cases().get_artifacts(ctx)
    if not artifacts:
        raise HTTPException(status_code=404, detail=f"Unknown case {case_id}")
    invoice = (artifacts.get("extraction") or {}).get("invoice") or {}
    vendor = repo.find_by_name(principal.tenant_id, invoice.get("vendor_name") or "")
    if vendor is None:
        raise HTTPException(status_code=409, detail=f"{invoice.get('vendor_name') or 'This vendor'} is not in the vendor master. Add it first.")
    proposal = vendor_routes.BankChangeProposal(
        bank_account_name=invoice.get("bank_account_name"),
        bank_account_number=invoice.get("bank_account_number"),
        bank_code=invoice.get("bank_code"),
        bank_iban=invoice.get("bank_iban"),
        reason=request.reason or f"Bank details printed on invoice {invoice.get('invoice_number') or case_id}",
        source_case_id=case_id,
    )
    return vendor_routes.propose_change(principal, vendor["id"], proposal)
```

- [ ] **Step 5: Isolation probes**

In `tests/test_tenant_isolation_api.py`:
- Add to `PROBED`: `"/api/vendors/{vendor_id}"`, `"/api/vendors/{vendor_id}/bank-changes"`, `"/api/vendors/bank-changes"`, `"/api/vendors/bank-changes/{change_id}/decide"`, `"/api/invoices/{case_id}/bank-change"`.
- Add `vendor_routes` to the module tuple in `test_no_endpoint_reads_a_tenant_from_the_request` (import it beside `config_routes`).
- Replace `test_vendors_do_not_cross_tenants` with a DB-backed version plus a bank-change probe:

```python
    @pytest.fixture
    def vendor_db(self, scratch_db, monkeypatch):
        url = scratch_db("iso-vendors.db")
        create_all(url)
        with db_session("", url) as session:
            for tid, slug in ((ACME, "acme"), (GLOBEX, "globex")):
                session.add(Tenant(id=tid, name=slug, slug=slug, status="active"))
        monkeypatch.setattr(service, "_vendors", PgVendorRepository(url))
        monkeypatch.setenv("DATABASE_URL", url)

    def test_vendors_do_not_cross_tenants(self, clients, vendor_db):
        clients("acme").put("/api/vendors", json={"vendors": [{"name": "Acme Supplier", "tax_id": "51824753556"}]})
        assert clients("globex").get("/api/vendors").json()["vendors"] == []
        clients("globex").put("/api/vendors", json={"vendors": [{"name": "Globex Supplier", "tax_id": "29AAFCZ4821L1ZS"}]})
        assert [v["name"] for v in clients("acme").get("/api/vendors").json()["vendors"]] == ["Acme Supplier"]

    def test_bank_changes_do_not_cross_tenants(self, clients, vendor_db):
        """A vendor id guessed from another tenant must be a 404, and the other
        tenant's queue must stay empty: an attacker who could propose into
        someone else's queue has half of a payment redirection."""
        clients("acme").put("/api/vendors", json={"vendors": [{"name": "Acme Supplier", "tax_id": "51824753556"}]})
        vendor_id = clients("acme").get("/api/vendors").json()["vendors"][0]["id"]
        body = {"bank_account_number": "123456789", "bank_code": "X", "reason": "r"}
        assert clients("globex", "clerk").post(f"/api/vendors/{vendor_id}/bank-changes", json=body).status_code == 404
        assert clients("globex").patch(f"/api/vendors/{vendor_id}", json={"email_domain": "x"}).status_code == 404
        change = clients("acme", "clerk").post(f"/api/vendors/{vendor_id}/bank-changes", json=body).json()
        assert clients("globex", "clerk").get("/api/vendors/bank-changes").json()["changes"] == []
        assert clients("globex", "approver").post(f"/api/vendors/bank-changes/{change['id']}/decide", json={"action": "approve"}).status_code == 404

    def test_invoice_bank_change_shortcut_does_not_cross_tenants(self, clients, vendor_db):
        for case_id in self.any_case_ids(clients):  # see below
            assert clients("globex", "clerk").post(f"/api/invoices/{case_id}/bank-change", json={"reason": "r"}).status_code == 404
            break
```

For the shortcut probe, find how the existing invoice probes obtain an ACME case id (search the file for `GOLDEN_CASES` / `case_id =`) and use that instead of `self.any_case_ids`. Import `PgVendorRepository` alongside `PgRuleRepository`.

- [ ] **Step 6: Run the focused files, then the suite**

Run: `./.venv/bin/python -m pytest tests/test_vendor_routes.py tests/test_tenant_isolation_api.py tests/test_config_routes.py -q -p no:cacheprovider` → PASS. `test_every_route_is_covered` is the one that proves the probes are complete.
Run the full suite → PASS.

- [ ] **Step 7: ruff and commit**

```bash
./.venv/bin/python -m ruff check . && ./.venv/bin/python -m ruff format .
git add invoice_processing/api/ tests/test_vendor_routes.py tests/test_tenant_isolation_api.py
git commit -m "Let one person propose a vendor's bank details and require another to approve" -m "vendor_routes: list/import, email-domain patch, bank-change propose/list/decide, and the workbench shortcut from a held invoice. Self-approval is 403; a second pending change is 409; without a database the bank-change routes say so (503) and the supplier list works from the file as before." -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Task 7: Frontend — `/vendors` page and the invoice shortcut

**Files:**
- Modify: `frontend/src/lib/api.ts` (types after `PurchaseOrderList` ~:313; `Finding` ~:213 gains `signals?: string[]`)
- Modify: `frontend/src/components/AppShell.tsx` (`NAV` ~:33-41)
- Create: `frontend/src/app/vendors/page.tsx`
- Modify: `frontend/src/app/invoices/page.tsx` (below `<Findings .../>` ~:474; `Findings` component)
- Modify: `invoice_processing/api/service.py::_findings` (~:589) — add `"signals": validation.get("signals")` so the UI can tell a 3.4 finding apart
- Modify: `frontend/e2e/golden-path.spec.ts` (nav list ~:171-189; one new test)

**Interfaces:** consumes the JSON shapes from Task 6 exactly as named there.

- [ ] **Step 1: Types and nav**

```ts
export type Vendor = {
  id: string;
  name: string;
  tax_id: string;
  kind: string;
  email_domain: string | null;
  bank_status: "verified" | "unverified" | "pending";
  bank_account_masked: string | null;
  bank_account_name: string | null;
  bank_code: string | null;
  bank_verified_at: string | null;
  bank_verified_by: string | null;
  pending_change_id: string | null;
};
export type VendorList = { vendors: Vendor[]; count: number; source: string };
export type BankChange = {
  id: string;
  vendor_id: string;
  vendor_name: string;
  status: "pending" | "approved" | "rejected";
  reason: string;
  source_case_id: string | null;
  proposed_by: string;
  decided_by: string | null;
  decided_at: string | null;
  decision_note: string | null;
  created_at: string | null;
  proposed_account_name: string | null;
  proposed_account_number: string | null;
  proposed_code: string | null;
  proposed_iban: string | null;
  previous_account_name: string | null;
  previous_account_number: string | null;
  previous_code: string | null;
  previous_iban: string | null;
};
export type BankChangeList = { changes: BankChange[]; count: number };
```

Add `signals?: string[] | null;` to `Finding`. In `AppShell.tsx` `NAV`, insert after Purchase orders: `{ href: "/vendors", label: "Vendors", glyph: "◆", min: "clerk" },`.

In `service._findings` add `"signals": validation.get("signals"),` to the dict.

- [ ] **Step 2: The vendors page**

`frontend/src/app/vendors/page.tsx` — follow `purchase-orders/page.tsx` exactly for imports, `usePoll`, `can`, notice/problem panels, the JSON import control (same `replace()` body but `/api/vendors`, key `vendors`, label "Import list", input id `vendor-upload`), `panel table-scroll` + `data-table`. Add:

```tsx
const { data: changes, reload: reloadChanges } = usePoll<BankChangeList>(
  "/api/vendors/bank-changes?status=pending",
);
const [proposing, setProposing] = useState<string | null>(null); // vendor id with the form open
const [form, setForm] = useState({ bank_account_name: "", bank_account_number: "", bank_code: "", bank_iban: "", reason: "" });
const canPropose = can(me, "clerk");
const canDecide = can(me, "approver");
const isAdmin = can(me, "admin");

async function propose(vendorId: string) {
  try {
    await api<BankChange>(`/api/vendors/${vendorId}/bank-changes`, { method: "POST", body: JSON.stringify(form) });
    setNotice("Bank change proposed. A second person must approve it before it is used.");
    setProposing(null);
    setForm({ bank_account_name: "", bank_account_number: "", bank_code: "", bank_iban: "", reason: "" });
    reload(); reloadChanges();
  } catch (caught) { setProblem(caught instanceof ApiError ? caught.friendly : String(caught)); }
}

async function decide(change: BankChange, action: "approve" | "reject") {
  const note = action === "reject" ? window.prompt("Why is this change rejected?") : null;
  if (action === "reject" && !note) return;
  if (!window.confirm(`${action === "approve" ? "Approve" : "Reject"} the bank change for ${change.vendor_name}?`)) return;
  try {
    await api<BankChange>(`/api/vendors/bank-changes/${change.id}/decide`, { method: "POST", body: JSON.stringify({ action, note }) });
    setNotice(action === "approve" ? `Bank details for ${change.vendor_name} are now verified.` : `Change for ${change.vendor_name} rejected.`);
    reload(); reloadChanges();
  } catch (caught) { setProblem(caught instanceof ApiError ? caught.friendly : String(caught)); }
}
```

Render, top to bottom: heading row ("Vendors" + Import list for admins) → problem/notice panels → **Pending changes** panel (only when `changes?.count`): one card per change with a two-column "Previous / Proposed" list of account name, account (masked), code, IBAN; proposer + `created_at`; reason; `source_case_id` as a link to `/invoices?case=<id>`; for `canDecide` two buttons, both `disabled={change.proposed_by === me?.email}` with `title="You proposed this change"` → table with columns Vendor, Tax ID, Kind, Email domain (admin: inline `<input>` + Save calling `PATCH /api/vendors/{id}`), Bank (badge: `badge-ok` verified with `bank_account_masked` and date, `badge-neutral` unverified, `badge-warn` pending), Actions ("Request bank change" for `canPropose` when `bank_status !== "pending"`; clicking toggles an inline form row with the five inputs and Propose/Cancel). Empty state text: "No vendors loaded. Step 3.2 runs as a checksum alone and step 3.4 cannot compare bank details. Import your supplier list (name, tax_id, kind)." When `data?.source !== "database"`, show a muted line: "Bank-change control needs a database; this deployment lists vendors from a file." and hide the propose buttons.

Use `id`s on the inputs (`vendor-upload`, `bank-account-number`, `bank-reason`) so the e2e can target them.

- [ ] **Step 3: Invoice page shortcut**

In `invoices/page.tsx`, after `<Findings findings={detail.findings} />`:

```tsx
<BankChangeShortcut detail={detail} me={me} onDone={say} />
```

and the component (same file, below `Findings`):

```tsx
/** A bank signal on this invoice: offer to raise the vendor bank change from
 *  the details already read, so nobody retypes an account number. */
function BankChangeShortcut({ detail, me, onDone }: { detail: InvoiceDetail; me: Me | null; onDone: (m: string) => void }) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const bank = detail.findings.find((f) => f.step === "3.4" && f.passed === false);
  if (!bank || !can(me, "clerk")) return null;
  const pending = bank.signals?.includes("recent_bank_change") && /awaiting approval/.test(bank.evidence ?? "");
  if (pending) {
    return <div className="muted" style={{ fontSize: 12.5 }}>A bank change for this vendor is awaiting approval. <a href="/vendors">Open vendors</a></div>;
  }
  return (
    <div className="stack" style={{ gap: 6 }}>
      <button className="btn-secondary" disabled={busy} onClick={async () => {
        setBusy(true); setProblem(null);
        try {
          await api(`/api/invoices/${detail.case_id}/bank-change`, { method: "POST", body: JSON.stringify({ reason: "" }) });
          onDone(`Bank change proposed for ${detail.vendor ?? "this vendor"}. A second person must approve it on the Vendors page.`);
        } catch (caught) { setProblem(caught instanceof ApiError ? caught.friendly : String(caught)); }
        finally { setBusy(false); }
      }}>
        Propose these bank details for {detail.vendor ?? "this vendor"}
      </button>
      {problem ? <div role="alert" style={{ color: "var(--error)", fontSize: 12.5 }}>{problem}</div> : null}
    </div>
  );
}
```

`say` is the page's existing notice setter (grep `function say` / `const say` in the file; use whatever it is named). Import `Me`, `can`, `ApiError` if not already imported.

- [ ] **Step 4: Playwright**

In `golden-path.spec.ts`: add `"Vendors"` to the nav-label list in the "every sidebar view opens" test. Add:

```ts
test("the vendors page explains it needs a database for bank changes", async ({ page }) => {
  await goto(page, "/vendors");
  await expect(page.getByRole("heading", { name: "Vendors" })).toBeVisible();
  // e2e runs on file stores; the page must say so rather than show a broken queue.
  await expect(page.getByText(/needs a database/)).toBeVisible();
});
```

(The two-role dual-control flow cannot run in e2e: the dev principal is always admin and e2e has no database. It is covered end to end in `tests/test_vendor_routes.py`.)

- [ ] **Step 5: Lint, typecheck, e2e**

```bash
cd frontend && npm run lint && npx tsc --noEmit && npm run build && npx playwright test; cd ..
./.venv/bin/python -m pytest tests/test_frontend* tests/test_vendor_routes.py -q -p no:cacheprovider
```

Expected: all green. Then start the manual stack (`preview_start manual-a-api-worker` and `manual-frontend` from `.claude/launch.json`) and walk the page once with the browser pane: import list, request a change, see it pending, confirm the Approve button is disabled for the proposer. Screenshot for the user.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/lib/api.ts frontend/src/components/AppShell.tsx frontend/src/app/vendors/page.tsx frontend/src/app/invoices/page.tsx frontend/e2e/golden-path.spec.ts invoice_processing/api/service.py
git commit -m "Show vendors, their bank status, and the queue a second person approves" -m "/vendors page with import, request-change form and pending-changes panel (Approve/Reject disabled for the proposer); a held invoice offers to propose its printed bank details in one click." -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Task 8: Benchmark measurement and CHANGELOG

**Files:**
- Modify: `benchmark/harness/dataset.py` (`vendor_records` ~:125)
- Modify: `benchmark/CHANGELOG.md` (new entry at the top, same format as the previous entries)
- Modify: `benchmark/harness/score.py` only if "held by 3.4" needs counting — prefer reading it from `results.json` findings with a one-off script in the scratchpad.

- [ ] **Step 1: Give the benchmark master its bank columns**

```python
def vendor_records(company_id: str) -> list[dict]:
    """The supplier list as the tenant would have loaded it, with the bank
    details and billing domain the dataset's vendor master carries: without
    them step 3.4 has nothing to compare and the measurement is of nothing."""
    return [
        {
            "name": v["name"],
            "tax_id": v["gstin"],
            "kind": v["kind"],
            "email_domain": (v.get("email") or "").split("@")[-1].lower() or None,
            "bank_account_name": v["name"],
            "bank_account_number": v["account"],
            "bank_code": v["ifsc"],
            # Verified long before the reference date, so the recent-change
            # signal measures invoices, not the seeding.
            "bank_verified_at": "2025-01-01T00:00:00+00:00",
            "last_bank_change_at": "2025-01-01T00:00:00+00:00",
        }
        for v in vendors(company_id).values()
    ]
```

Add a test in whichever file tests `benchmark/harness/dataset.py` (`grep -rl vendor_records tests benchmark` — if none, `tests/test_benchmark_dataset.py`): the record for `VA01` has `bank_code == "KCBL0230056"` and `email_domain == "cloudhaven.example"`.

- [ ] **Step 2: Replay and score**

```bash
./.venv/bin/python -m benchmark.harness.replay --run after-ecb9bbd --profile benchmark/harness/profiles/after.json --out replay-bank-check
./.venv/bin/python -m benchmark.harness.score benchmark/runs/replay-bank-check --profile benchmark/harness/profiles/after.json
./.venv/bin/python -m benchmark.harness.compare benchmark/runs/replay-currency-on-ecb9bbd benchmark/runs/replay-bank-check
```

Then count the 3.4 outcomes from `benchmark/runs/replay-bank-check/results.json` with a scratchpad script: passed / held / not compared, and list the held invoice ids with their evidence. Expected: 245 pass, 2 held (MFG-INV-011, MFG-INV-003), 53 not compared.

- [ ] **Step 3: Check the two numbers that matter**

From `report.md`: *posted untouched wrongly* must still be 1 (MFG-INV-039). *Clean invoices posted untouched* must be 40 unless MFG-INV-011 or MFG-INV-003 is among the 45 clean invoices — check `benchmark/dataset/fault_manifest.csv` for both ids. If one is clean and now held, the number drops by one and the CHANGELOG says so as the price of not folding O/0; **do not add folding**.

- [ ] **Step 4: Plant two bank faults by hand**

The dataset planted no bank faults, so the faults are planted in the *master*, not the PDF: a real extraction from the stored run is checked against a vendor record whose account (then IFSC) was changed. This is a pytest so it is reproducible, and it skips when the gitignored run is absent. Create `tests/test_bank_check_planted.py`:

```python
"""Two faults the benchmark never planted: the vendor master holds a different
account, then a different IFSC, from what a real extraction printed. Both must
hold the invoice for review."""

import json
from pathlib import Path

import pytest

from invoice_processing import vendor_master
from invoice_processing.shared_libraries.acting import general_invoice_agent
from invoice_processing.shared_libraries.acting.general_invoice_agent import Phase3ValidatorAgent

RUN = Path("benchmark/runs/after-ecb9bbd/raw/A.jsonl")

pytestmark = pytest.mark.skipif(not RUN.exists(), reason="stored benchmark run not present")


def _record() -> dict:
    for line in RUN.read_text().splitlines():
        rec = json.loads(line)
        if (rec.get("extracted") or {}).get("bank_account_number") and rec.get("final_decision") == "ACCEPT":
            return rec
    pytest.skip("no accepted record with bank details")


def _hold(tmp_path, monkeypatch, vendor_overrides: dict) -> tuple[dict, dict]:
    rec = _record()
    inv = dict(rec["extracted"])
    vendor = {"name": inv["vendor_name"], "tax_id": inv["vendor_tax_id"],
              "bank_account_name": inv["vendor_name"], "bank_account_number": inv["bank_account_number"],
              "bank_code": inv["bank_code"], **vendor_overrides}
    monkeypatch.setitem(
        general_invoice_agent._config_store, "CONFIG",
        {**general_invoice_agent.DEFAULT_CONFIG, "duplicate_check_enabled": False, "vendor_master_records": [vendor]},
    )
    extraction = {"invoice": {**inv, "_routing": "AUTO", "_evidence": {"_summary": {"fields_needing_review": []}}}}
    (tmp_path / "02_extraction.json").write_text(json.dumps(extraction))
    out = Phase3ValidatorAgent(seen_invoices=lambda exclude_case_id="": []).run(tmp_path, extraction, {"decision": "CONTINUE"})
    step = next(v for v in out["validations"] if v["step"] == "3.4")
    return step, extraction


def test_changed_account_holds(tmp_path, monkeypatch):
    step, extraction = _hold(tmp_path, monkeypatch, {"bank_account_number": "000000000001"})
    assert step["signals"] == ["bank_mismatch"]
    assert extraction["invoice"]["_routing"] == "REVIEW"


def test_changed_ifsc_holds(tmp_path, monkeypatch):
    step, extraction = _hold(tmp_path, monkeypatch, {"bank_code": "HDFC0000001"})
    assert step["signals"] == ["bank_mismatch"]
    assert extraction["invoice"]["_routing"] == "REVIEW"


def test_the_real_master_passes(tmp_path, monkeypatch):
    step, extraction = _hold(tmp_path, monkeypatch, {})
    assert step["passed"] is True and extraction["invoice"]["_routing"] == "AUTO"
```

Run: `./.venv/bin/python -m pytest tests/test_bank_check_planted.py -q -p no:cacheprovider` → 3 passed.

- [ ] **Step 5: CHANGELOG entry**

Top of `benchmark/CHANGELOG.md`, in the existing entry format: title "Step 3.4: bank details against the vendor master"; root cause (nothing compared the extracted bank fields); evidence (the 245/2/53 count, the two held ids and why, the safety and cost numbers before/after from `compare`); result; what was declined (O/0 folding on bank codes, with the 24-wrong-values reference).

- [ ] **Step 6: Commit**

```bash
git add benchmark/harness/dataset.py benchmark/CHANGELOG.md tests/
git commit -m "Measure step 3.4 over the full run: 245 pass, 2 held, none posted wrongly" -m "<paste the before/after lines from compare>" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Task 9: Gates, handoff notes, and stop

- [ ] **Step 1: All gates**

```bash
bash benchmark/harness/gates.sh
```
Expected: every row green (pytest, ruff, verify_phase0/1, eval gate, frontend lint/tsc/e2e).

- [ ] **Step 2: Docs**

Add a short section to `README.md` (or wherever `/api/vendors` is documented — `grep -rn "api/vendors" README.md docs/`) describing the bank-change flow, the five signals and the 503 without a database. Add `manual-verification` row ideas for companies B/C to `benchmark/manual-verification/README.md` only if that file lists future rows; otherwise skip.

- [ ] **Step 3: Commit, do not push.** Report to the user: commits made, test count, the measured numbers, and the two deviations from the spec (e2e scope; file fallback without a database). Ask whether to push and open a PR.

---

## Self-review against the spec

- Section 1 (tables, migration, `VendorMaster` records, PUT never writes bank fields): Tasks 1, 2, 4, 6. ✔
- Section 2 (step 3.4, five signals, comparison rules, routing re-save, sender capture, setting): Tasks 3, 4, 5. ✔ YAML key is `informational_only` (the catalogue reads that), validation dict key is `informational`.
- Section 3 (routes, roles, 403 self-approval, 409s, repository, entity type, masking at boundary, invoice approval independent): Tasks 2, 6. ✔ Addition not in spec: file fallback + 503 without a database (needed for e2e and local no-DB mode).
- Section 4 (vendors page, pending panel, shortcut button, types, e2e): Task 7. ✔ Deviation: e2e cannot exercise two roles; API tests cover the flow.
- Section 5 (tests, replay numbers, safety/cost, planted faults, CHANGELOG, rollout order): Tasks 1–8 in that order. ✔
- Type consistency: repository method names (`upsert_many, list, get, find_by_name, patch, load_records, propose, list_changes, get_change, decide`) match between Task 2 and Task 6; exception names `PendingChangeExists, SelfApproval, ChangeNotPending` match; `bank_check.compare(invoice, vendor, *, sender, reference_date, recent_days)` matches Tasks 3 and 4; `Vendor` fields match Tasks 3/4/8; config keys `vendor_master_records`, `document_sender`, `bank_change_recent_days` match Tasks 4 and 5; `Finding.signals` matches Task 6's `_findings` change (made in Task 7) and the UI.
