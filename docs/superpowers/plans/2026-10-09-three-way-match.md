# Three-Way Match Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Match each invoice line against its PO line and the goods received, holding (never rejecting) invoices whose lines are short-received, over-priced, over-ordered or unpaired, and re-match them automatically when a GRN arrives.

**Architecture:** Four new RLS tenant tables (`purchase_orders`, `po_lines`, `goods_receipts`, `grn_lines`) fed by CSV/JSON import; a pure matcher `three_way.py`; a new informational pipeline step 2.3 that holds like step 3.4; a worker `rematch` job queued on GRN import; an approval-time re-check so two invoices cannot both consume the same received units.

**Tech Stack:** Python 3.12 (`./.venv/bin/python`), FastAPI, SQLAlchemy 2 + Alembic (Postgres with RLS; SQLite locally), Next.js 16 / React 19, Playwright, pytest.

**Spec:** `docs/superpowers/specs/2026-10-09-three-way-match-design.md` (read it first; this plan argues from it).

## Context

Step 2.2 today is a header-only two-way match (`invoice_processing/po_matching.py`): PO exists, right vendor, open, not over-billed in total. POs have no lines, there is no receipt data, and POs live in a per-tenant JSON file the worker container cannot see in production. The product plan (Phase 3) calls for line-level 3-way match with PO/GRN ingest. The user chose: structured upload now (scanned GRN/PO documents are the next sub-project), receipt required per PO line with vendor-kind fallback, holds instead of rejections, fixed-order fail-safe pairing, billed = ERP opening balance + Ledgerly-approved invoices since, approval-time re-check.

## Global Constraints

- Workflow (HANDOFF10 §4): **one implementer at a time**; TDD — every behaviour gets a test that fails without it (prove with `git stash push -- invoice_processing`); commit after each task with an imperative *why* subject, a body, and trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Controls fail closed: anything that cannot read its input holds or refuses; never passes silently.
- No endpoint takes `tenant_id`; roles via `require_role`; actors are `principal.email`; `Field(max_length=…)` on every stored string; refusals are 403 sentences naming the rule; DB-only routes 503 without a DB; every route gets a cross-tenant probe + `PROBED` entry in `tests/test_tenant_isolation_api.py`.
- RLS: new tables in `TENANT_TABLES` (`db/models.py`) **and** in the migration's own `RLS_TABLES`; repositories filter `tenant_id` explicitly (SQLite has no RLS); never edit an applied migration (head is `b21ac023e07e`).
- Money/quantity columns are `Numeric(18, 4)`; JSON columns `sa.JSON().with_variant(postgresql.JSONB(), "postgresql")`.
- Timestamps serialised via `repositories._iso`.
- DB-backed test files go on the CI Postgres list in `.github/workflows/ci.yml` (~line 157).
- Deliberate simplifications carry `# ponytail: <ceiling + upgrade path>`.
- Vendor kind values are `"goods"` / `"service"` (singular, `vendor_master.py:35`).
- `GET /api/purchase-orders` stays **clerk+** (user ruling; spec said viewer+ — spec is corrected in Task 0).
- Local runs set `CLOUD_SQL_INSTANCE=""`, `GCS_BUCKET=""`, `PUBSUB_TOPIC=""` (empty, not unset).
- Disk is nearly full: no new worktrees, no `npm ci` (use the main checkout's `frontend/node_modules`).

## File Structure

| File | Responsibility |
|---|---|
| `invoice_processing/db/migrations/versions/c4d7e8f91a02_three_way.py` | Create the four tables, RLS |
| `invoice_processing/db/models.py` | `PurchaseOrder`, `PoLine`, `GoodsReceipt`, `GrnLine` ORM; `TENANT_TABLES` |
| `invoice_processing/db/repositories.py` | `PgPurchaseOrderRepository` (upsert, receipts, book, lists, billed-since) |
| `invoice_processing/three_way.py` | Pure matcher: pairing, checks, signals, evidence |
| `invoice_processing/po_book.py` | `PurchaseOrderBook` port + `FileBook` + `DbBook` (`book(po_number)`) |
| `invoice_processing/shared_libraries/acting/general_invoice_agent.py` | Step 2.3 in `Phase2ValidatorAgent.run`; `_load_purchase_orders` returns the injected book |
| `invoice_processing/shared_libraries/invoice_master_data.yaml` | Step 2.3 declaration (`informational_only: true`), `three_way_qty_tolerance` default |
| `invoice_processing/tenant_settings.py` | `three_way_qty_tolerance` setting; inject `purchase_order_book` |
| `invoice_processing/api/po_routes.py` | Imports (PO, GRN), `GET /api/purchase-orders/{po_number}`, `GET /api/goods-receipts`; CSV parsing |
| `invoice_processing/api/config_routes.py` | Existing `GET`/`PUT /api/purchase-orders` read/write the DB when configured |
| `invoice_processing/api/service.py` | `get_purchase_orders()` accessor, router include, `_findings` passes `lines`, 409 for `OverBilled` |
| `invoice_processing/rematch.py` | `rematch_case(ctx, cases, book, ...)`: rerun 2.3 on stored artifacts, clear/keep hold |
| `invoice_processing/worker/runner.py` | `REMATCH = "case.rematch"` job dispatch, `enqueue_rematch` |
| `invoice_processing/approval_flow.py` | `OverBilled(Refused)`, final-step re-check in `decide()` |
| `frontend/src/app/purchase-orders/page.tsx`, `frontend/src/app/invoices/page.tsx`, `frontend/src/lib/api.ts` | Uploads, PO lines/GRNs, three-way panel |
| `scripts/e2e_api.py`, `frontend/e2e/golden-path.spec.ts` | PO case seed, re-match thread, e2e tests |
| `tests/fixtures/three_way/planted.json`, `tests/test_three_way_planted.py`, `benchmark/CHANGELOG.md` | Planted-fault measurement |

---

### Task 0: Spec corrections

**Files:** Modify `docs/superpowers/specs/2026-10-09-three-way-match-design.md`

- [ ] **Step 1:** In §4 change `GET /api/purchase-orders` role text to "(existing route, extended; **clerk+** — user ruling 2026-10-09: PO data stays off the read-only viewer role)". In §2 rule 2 replace "services" with `"service"`. In §5 e2e bullet add: "the e2e API starts a background thread that drains only `case.rematch` jobs from the local queue (`scripts/e2e_api.py`)".
- [ ] **Step 2:** Commit: `git commit -am "Record two rulings in the three-way spec: PO list stays clerk-only; e2e drains re-match jobs in a thread"`.

---

### Task 1: Tables, models, migration

**Files:**
- Create: `invoice_processing/db/migrations/versions/c4d7e8f91a02_three_way.py`
- Modify: `invoice_processing/db/models.py` (after `GlRule`, ~line 695; `TENANT_TABLES` line 698)
- Test: `tests/test_purchase_order_repository.py` (new), `tests/test_gcs_and_migrations.py:329-359, 361-419`

**Interfaces — Produces:** ORM classes `PurchaseOrder`, `PoLine`, `GoodsReceipt`, `GrnLine`; table names `purchase_orders`, `po_lines`, `goods_receipts`, `grn_lines`.

- [ ] **Step 1: Failing tests.** New file `tests/test_purchase_order_repository.py` (copy the fixture pattern of `tests/test_gl_rule_repository.py`: `scratch_db("po.db")`, `drop_all`, `create_all`, add `models.Tenant` rows for `"t1"`/`"t2"` inside `tenant_session`):

```python
from invoice_processing.db import models
from invoice_processing.db.session import create_all, get_engine, tenant_session
from sqlalchemy import inspect

def test_tables_are_tenant_tables_and_build(scratch_db):
    url = scratch_db("po.db")
    models.Base.metadata.drop_all(get_engine(url)); create_all(url)
    names = ("purchase_orders", "po_lines", "goods_receipts", "grn_lines")
    assert set(names) <= set(models.TENANT_TABLES)
    assert all(inspect(get_engine(url)).has_table(n) for n in names)
```

In `tests/test_gcs_and_migrations.py` add `("c4d7e8f91a02", 4)` to the parametrize at line ~333, and `"purchase_orders", "po_lines", "goods_receipts", "grn_lines"` to the `tables` list at ~line 391.

- [ ] **Step 2:** Run `./.venv/bin/python -m pytest tests/test_purchase_order_repository.py tests/test_gcs_and_migrations.py -q -p no:cacheprovider -k "tenant_tables or protects_exactly"` → FAIL (no such tables / module not found).

- [ ] **Step 3: Models** (`db/models.py`, after `GlRule`):

```python
class PurchaseOrder(Base, TenantMixin, TimestampMixin):
    """A purchase order as uploaded from the ERP; lines in PoLine.

    po_number is stored normalised (po_matching.normalize_po_number) so the
    match and the upsert agree on identity; po_number_display is what the
    ERP printed, for people.
    """
    __tablename__ = "purchase_orders"
    __table_args__ = (UniqueConstraint("tenant_id", "po_number", name="uq_purchase_orders_number"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    po_number: Mapped[str] = mapped_column(String(128), nullable=False)
    po_number_display: Mapped[str] = mapped_column(String(128), nullable=False)
    vendor_name: Mapped[str] = mapped_column(String(512), nullable=False)
    total_amount: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="AUD")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    amount_invoiced: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False, default=0)
    customer_name: Mapped[str | None] = mapped_column(String(512))
    valid_from: Mapped[str | None] = mapped_column(String(10))
    valid_to: Mapped[str | None] = mapped_column(String(10))
    amount_basis: Mapped[str] = mapped_column(String(8), nullable=False, default="inc_tax")
    approved_quantity: Mapped[float | None] = mapped_column(Numeric(18, 4))
    approved_unit_price: Mapped[float | None] = mapped_column(Numeric(18, 4))
    hsn_sac: Mapped[str | None] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="upload")
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    uploaded_by: Mapped[str] = mapped_column(String(320), nullable=False)
    lines: Mapped[list[PoLine]] = relationship(back_populates="order", cascade="all, delete-orphan", order_by="PoLine.line_number")


class PoLine(Base, TenantMixin):
    __tablename__ = "po_lines"
    __table_args__ = (UniqueConstraint("purchase_order_id", "line_number", name="uq_po_lines_number"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    purchase_order_id: Mapped[str] = mapped_column(String(36), ForeignKey("purchase_orders.id"), nullable=False, index=True)
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    item_code: Mapped[str | None] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    qty_ordered: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    unit_price: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    uom: Mapped[str | None] = mapped_column(String(16))
    receipt_required: Mapped[bool | None] = mapped_column(Boolean)
    qty_invoiced_opening: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False, default=0)
    order: Mapped[PurchaseOrder] = relationship(back_populates="lines")


class GoodsReceipt(Base, TenantMixin):
    """Evidence that goods arrived. Immutable: a correction is a new receipt
    with negative quantities and a reason."""
    __tablename__ = "goods_receipts"
    __table_args__ = (UniqueConstraint("tenant_id", "grn_number", name="uq_goods_receipts_number"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    grn_number: Mapped[str] = mapped_column(String(128), nullable=False)
    purchase_order_id: Mapped[str] = mapped_column(String(36), ForeignKey("purchase_orders.id"), nullable=False, index=True)
    received_on: Mapped[str] = mapped_column(String(10), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500))
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="upload")
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    recorded_by: Mapped[str] = mapped_column(String(320), nullable=False)
    lines: Mapped[list[GrnLine]] = relationship(cascade="all, delete-orphan")


class GrnLine(Base, TenantMixin):
    __tablename__ = "grn_lines"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    goods_receipt_id: Mapped[str] = mapped_column(String(36), ForeignKey("goods_receipts.id"), nullable=False, index=True)
    po_line_id: Mapped[str] = mapped_column(String(36), ForeignKey("po_lines.id"), nullable=False, index=True)
    qty_received: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
```

Append the four names to `TENANT_TABLES` (keep order: `"purchase_orders", "po_lines", "goods_receipts", "grn_lines"`). Add `Boolean`, `Integer`, `Text` to the sqlalchemy import if missing.

- [ ] **Step 4: Migration** `c4d7e8f91a02_three_way.py`, `down_revision = "b21ac023e07e"`, `RLS_TABLES = ["purchase_orders", "po_lines", "goods_receipts", "grn_lines"]`. Four `op.create_table(...)` calls mirroring the columns above exactly (`sa.String`, `sa.Numeric(18, 4)`, `sa.Boolean`, `sa.DateTime(timezone=True)`, `sa.ForeignKey("tenants.id")` on every `tenant_id` with `index=True`; the two `sa.UniqueConstraint`s; FKs `purchase_orders.id`, `goods_receipts.id`, `po_lines.id`). Then the same tail as `b21ac023e07e_gl_rules.py`: skip RLS unless `op.get_bind().dialect.name == "postgresql"`, else `for statement in enable_rls_statements(RLS_TABLES): op.execute(statement)`. `downgrade()` drops in reverse order: `grn_lines`, `goods_receipts`, `po_lines`, `purchase_orders`.

- [ ] **Step 5:** Run the two tests → PASS. Then `cd /Users/vineet/Downloads/ADK/invoice-processing && DATABASE_URL=sqlite:////tmp/tw.db CLOUD_SQL_INSTANCE="" GCS_BUCKET="" ./.venv/bin/python -m alembic upgrade head && DATABASE_URL=sqlite:////tmp/tw.db CLOUD_SQL_INSTANCE="" GCS_BUCKET="" ./.venv/bin/python -m alembic downgrade base && rm /tmp/tw.db` → both succeed; `alembic heads` shows `c4d7e8f91a02`.

- [ ] **Step 6:** Add `tests/test_purchase_order_repository.py` to the CI Postgres list (`.github/workflows/ci.yml` ~line 162, after `test_gl_rule_repository.py`). Commit: `"Store purchase orders with lines and goods receipts per tenant"`.

---

### Task 2: `PgPurchaseOrderRepository`

**Files:**
- Modify: `invoice_processing/db/repositories.py` (after `PgGlRuleRepository`, ~line 1720)
- Test: `tests/test_purchase_order_repository.py`

**Interfaces — Produces** (all dict-returning, tenant-filtered explicitly):

```python
class PgPurchaseOrderRepository:
    def __init__(self, url: str | None = None)
    def upsert_orders(self, tenant_id, orders: list[dict], *, uploaded_by: str, source="upload", now=None) -> dict   # {"created": int, "updated": int, "lines": int}
    def record_receipts(self, tenant_id, receipts: list[dict], *, recorded_by: str, source="upload", now=None) -> dict  # {"recorded": [grn_number...], "skipped_duplicates": [...], "po_numbers": [normalised...]}; raises ValueError("row N: ...") for unknown PO / line
    def list_orders(self, tenant_id) -> list[dict]        # header + line_count, received_total, billed_total
    def get_order(self, tenant_id, po_number) -> dict | None   # header + lines[{line_number,item_code,description,qty_ordered,unit_price,uom,receipt_required,received,billed,remaining}] + receipts[...]
    def list_receipts(self, tenant_id, po_number=None) -> list[dict]
    def book(self, tenant_id, po_number) -> dict | None   # {"po": <PurchaseOrder.from_dict-compatible dict>, "lines": [...], "received": {line_number: float}, "billed": {line_number: float}, "uploaded_at": iso}
    def has_orders(self, tenant_id) -> bool
```

`orders[i]` input shape: `{po_number, vendor_name, total_amount, currency?, status?, amount_invoiced?, customer_name?, valid_from?, valid_to?, amount_basis?, approved_quantity?, approved_unit_price?, hsn_sac?, lines: [{line_number, item_code?, description, qty_ordered, unit_price, uom?, receipt_required?, qty_invoiced?}]}` (already validated by the route).
`receipts[i]`: `{grn_number, po_number, received_on, reason?, lines: [{line_number | item_code, qty_received}]}`.

`billed[line] = qty_invoiced_opening + Σ` over `Invoice` rows of this tenant with `Invoice.po_number` normalised == po_number, `human_decision == "approve"`, workflow `decided_at` > `uploaded_at`, of the stored step-2.3 `lines[].quantity` grouped by `po_line_number` (read `invoice.fields["phase2"]["validations"]`, the entry with `step == "2.3"`). Compare `decided_at` in Python (both ISO; use `datetime.fromisoformat`). `# ponytail: scans this PO's approved invoices on every match; index/materialise if a PO gathers thousands.`

- [ ] **Step 1: Failing tests** (append to `tests/test_purchase_order_repository.py`; fixture `repo` returns `PgPurchaseOrderRepository(url)` and the url):

```python
ORDER = {"po_number": "PO-9001", "vendor_name": "Acme Pty Ltd", "total_amount": 1000, "currency": "INR",
         "lines": [{"line_number": 1, "item_code": "B-6205", "description": "Ball bearing 6205", "qty_ordered": 10, "unit_price": 100, "qty_invoiced": 2},
                   {"line_number": 2, "description": "Freight", "qty_ordered": 1, "unit_price": 50, "receipt_required": False}]}

def test_upsert_replaces_lines_and_resets_opening(repo):
    r, url = repo
    assert r.upsert_orders("t1", [ORDER], uploaded_by="a@x") == {"created": 1, "updated": 0, "lines": 2}
    again = {**ORDER, "lines": [{"line_number": 1, "description": "Ball bearing 6205", "qty_ordered": 12, "unit_price": 100, "qty_invoiced": 5}]}
    assert r.upsert_orders("t1", [again], uploaded_by="a@x") == {"created": 0, "updated": 1, "lines": 1}
    order = r.get_order("t1", "po9001")
    assert [l["line_number"] for l in order["lines"]] == [1] and order["lines"][0]["billed"] == 5.0

def test_receipts_are_idempotent_and_sum_with_corrections(repo):
    r, _ = repo
    r.upsert_orders("t1", [ORDER], uploaded_by="a@x")
    grn = {"grn_number": "GRN-1", "po_number": "PO-9001", "received_on": "2026-10-01", "lines": [{"line_number": 1, "qty_received": 6}]}
    assert r.record_receipts("t1", [grn], recorded_by="c@x")["recorded"] == ["GRN-1"]
    assert r.record_receipts("t1", [grn], recorded_by="c@x")["skipped_duplicates"] == ["GRN-1"]
    fix = {**grn, "grn_number": "GRN-1-FIX", "reason": "miscount", "lines": [{"item_code": "b6205", "qty_received": -1}]}
    r.record_receipts("t1", [fix], recorded_by="c@x")
    assert r.book("t1", "PO-9001")["received"] == {1: 5.0, 2: 0.0}

def test_receipt_for_unknown_po_or_line_is_refused(repo):
    r, _ = repo
    r.upsert_orders("t1", [ORDER], uploaded_by="a@x")
    with pytest.raises(ValueError, match="PO-NOPE"):
        r.record_receipts("t1", [{"grn_number": "G", "po_number": "PO-NOPE", "received_on": "2026-10-01", "lines": [{"line_number": 1, "qty_received": 1}]}], recorded_by="c@x")
    with pytest.raises(ValueError, match="line 9"):
        r.record_receipts("t1", [{"grn_number": "G", "po_number": "PO-9001", "received_on": "2026-10-01", "lines": [{"line_number": 9, "qty_received": 1}]}], recorded_by="c@x")

def test_other_tenant_sees_nothing(repo):
    r, _ = repo
    r.upsert_orders("t1", [ORDER], uploaded_by="a@x")
    assert r.list_orders("t2") == [] and r.book("t2", "PO-9001") is None and r.has_orders("t2") is False

def test_billed_counts_only_invoices_approved_after_upload(repo):
    r, url = repo
    r.upsert_orders("t1", [ORDER], uploaded_by="a@x", now=datetime(2026, 10, 1, tzinfo=timezone.utc))
    lines23 = [{"line_number": 1, "po_line_number": 1, "quantity": 3}]
    with tenant_session("t1", url) as s:
        for case, decision, when in (("c-old", "approve", "2026-09-30T00:00:00+00:00"), ("c-new", "approve", "2026-10-02T00:00:00+00:00"), ("c-rej", "reject", "2026-10-03T00:00:00+00:00")):
            s.add(models.Invoice(tenant_id="t1", case_id=case, po_number="PO-9001", human_decision=decision,
                  fields={"workflow": {"decision": decision, "decided_at": when}, "phase2": {"validations": [{"step": "2.3", "lines": lines23}]}}))
    assert r.book("t1", "PO-9001")["billed"] == {1: 5.0, 2: 0.0}   # opening 2 + c-new 3
```

(Check `models.Invoice` required columns — add `status="processed"` etc. as the model demands; see `tests/test_sheet_api.py` `invoice()` helper for a minimal row.)

- [ ] **Step 2:** Run → FAIL (`PgPurchaseOrderRepository` missing).
- [ ] **Step 3: Implement.** One `tenant_session` per public method. `upsert_orders`: per order, normalise number, `select(PurchaseOrder).where(tenant, po_number)`; create or update header fields; `order.lines = [PoLine(tenant_id=..., **line)]` (replaces); `uploaded_at = now or datetime.now(timezone.utc)`. `record_receipts`: for each receipt, skip if `grn_number` exists (→ `skipped_duplicates`); resolve PO (else `ValueError(f"row {i+1}: purchase order {po_number!r} is not on file")`); resolve each line by `line_number` or by casefolded `item_code` stripped of spaces/dashes (else `ValueError(f"row {i+1}: PO {po} has no line {n}")`); `reason` required if any qty < 0 (`ValueError`); add `GoodsReceipt` + `GrnLine`s. `book`: header via `_header_dict`, lines, `received` = `select(PoLine.line_number, func.coalesce(func.sum(GrnLine.qty_received), 0)) ... outerjoin ... group_by`, `billed` per the formula above (helper `_approved_since(session, tenant_id, po_number, since) -> dict[int, float]`). All floats via `float(...)`. `_header_dict` returns the `PurchaseOrder.from_dict`-compatible keys plus `id`, `source`, `uploaded_at` (`_iso`), `uploaded_by`.
- [ ] **Step 4:** Run the file → PASS. `ruff check . && ruff format --check .`.
- [ ] **Step 5:** Commit `"Give the match a purchase-order book: upsert, receipts, received and billed per line"`.

---

### Task 3: Pure matcher `three_way.py`

**Files:** Create `invoice_processing/three_way.py`; Test `tests/test_three_way.py`

**Interfaces — Produces:**

```python
NOT_RECEIVED="not_received"; SHORT_RECEIVED="short_received"; PRICE_OVER_PO="price_over_po"
OVER_ORDERED="over_ordered"; UNPAIRED_LINE="unpaired_line"; UNREADABLE_LINE="unreadable_line"
DEFAULT_QTY_TOL = 0.0

@dataclass(frozen=True)
class LineResult:
    line_number: int; description: str; po_line_number: int | None; paired_by: str | None  # "item_code"|"line_number"|"description"
    quantity: float | None; unit_price: float | None; billed: float | None; received: float | None
    signals: tuple[str, ...]; evidence: str
    def to_dict(self) -> dict

@dataclass(frozen=True)
class ThreeWay:
    lines: tuple[LineResult, ...]; signals: tuple[str, ...]; evidence: tuple[str, ...]
    @property passed -> bool   # no signals

def match(invoice_lines: list[dict], po_lines: list[dict], received: dict[int, float], billed: dict[int, float], *,
          vendor_kind: str | None, qty_tol=0.0, price_tol_pct=0.02, price_tol_abs=50.0) -> ThreeWay
def pair(invoice_lines, po_lines) -> list[tuple[int|None, str|None]]   # exposed for tests
```

`invoice_lines[i]` = extraction `line_items` dicts (`description`, `quantity`, `unit_price`, optional `item_code`, optional `po_line_number`), numbered `i+1`. `po_lines[j]` = repository line dicts (`line_number`, `item_code`, `description`, `qty_ordered`, `unit_price`, `receipt_required`). `received`/`billed` keyed by `line_number`; a missing key means 0.

- [ ] **Step 1: Failing tests** (`tests/test_three_way.py`; use small helpers `inv(desc, q, p, **kw)` and `po(n, desc, q, p, **kw)`):

```python
def test_pairs_by_item_code_then_line_number_then_description():
    inv_lines = [inv("whatever", 1, 1, item_code="b-6205"), inv("x", 1, 1, po_line_number=3), inv("Deep groove ball bearing 6205 2RS", 1, 1)]
    po_lines = [po(1, "Ball bearing 6205-2RS", 10, 100, item_code="B6205"), po(2, "Deep groove ball bearing 6205-2RS", 10, 100), po(3, "Grease", 1, 1)]
    assert pair(inv_lines, po_lines) == [(1, "item_code"), (3, "line_number"), (2, "description")]

def test_near_tie_description_is_unpaired():
    po_lines = [po(1, "Hex bolt M8 zinc", 1, 1), po(2, "Hex bolt M10 zinc", 1, 1)]
    assert pair([inv("Hex bolt zinc", 1, 1)], po_lines) == [(None, None)]

def test_a_po_line_is_claimed_once():
    po_lines = [po(1, "Bearing 6205", 1, 1)]
    assert pair([inv("Bearing 6205", 1, 1), inv("Bearing 6205", 1, 1)], po_lines)[1] == (None, None)

def test_short_receipt_holds_with_grn_evidence():
    r = match([inv("Bearing 6205", 100, 100)], [po(1, "Bearing 6205", 100, 100)], {1: 60}, {1: 0}, vendor_kind="goods")
    assert r.signals == (SHORT_RECEIVED,) and "billed 100, received 60" in r.lines[0].evidence and not r.passed

def test_not_received_vs_split_delivery_passes():
    assert match([inv("B", 10, 1)], [po(1, "B", 10, 1)], {}, {}, vendor_kind="goods").signals == (NOT_RECEIVED,)
    assert match([inv("B", 4, 1)], [po(1, "B", 10, 1)], {1: 10}, {1: 6}, vendor_kind="goods").passed

def test_billed_plus_this_invoice_over_received_or_ordered():
    assert match([inv("B", 5, 1)], [po(1, "B", 10, 1)], {1: 10}, {1: 6}, vendor_kind="goods").signals == (OVER_ORDERED,)
    assert match([inv("B", 5, 1)], [po(1, "B", 20, 1)], {1: 10}, {1: 6}, vendor_kind="goods").signals == (SHORT_RECEIVED,)

def test_receipt_required_fallback_and_unknown_vendor_holds():
    assert match([inv("Freight", 1, 50)], [po(1, "Freight", 1, 50, receipt_required=False)], {}, {}, vendor_kind="goods").passed
    assert match([inv("Hours", 8, 50)], [po(1, "Hours", 8, 50)], {}, {}, vendor_kind="service").passed
    assert match([inv("B", 1, 1)], [po(1, "B", 1, 1)], {}, {}, vendor_kind=None).signals == (NOT_RECEIVED,)

def test_price_tolerance_is_pct_or_abs_whichever_larger_and_under_passes():
    assert match([inv("B", 1, 149)], [po(1, "B", 1, 100, receipt_required=False)], {}, {}, vendor_kind="goods").passed   # +49 < 50 abs
    assert match([inv("B", 1, 151)], [po(1, "B", 1, 100, receipt_required=False)], {}, {}, vendor_kind="goods").signals == (PRICE_OVER_PO,)
    assert match([inv("B", 1, 10_300)], [po(1, "B", 1, 10_000, receipt_required=False)], {}, {}, vendor_kind="goods").signals == (PRICE_OVER_PO,)  # 3% > 2%
    assert match([inv("B", 1, 80)], [po(1, "B", 1, 100, receipt_required=False)], {}, {}, vendor_kind="goods").passed

def test_unreadable_quantity_is_held_not_zero():
    r = match([inv("B", None, 100)], [po(1, "B", 1, 100)], {1: 1}, {}, vendor_kind="goods")
    assert r.signals == (UNREADABLE_LINE,)

def test_unpaired_line_holds_and_names_the_line():
    r = match([inv("Mystery surcharge", 1, 1)], [po(1, "Bearing", 1, 1)], {1: 1}, {}, vendor_kind="goods")
    assert r.signals == (UNPAIRED_LINE,) and "line 1 'Mystery surcharge' matched no PO line" in r.evidence[0]

def test_qty_tolerance_and_to_dict_roundtrip():
    r = match([inv("B", 10, 1)], [po(1, "B", 10, 1)], {1: 9.5}, {}, vendor_kind="goods", qty_tol=1)
    assert r.passed and r.lines[0].to_dict()["po_line_number"] == 1
```

- [ ] **Step 2:** Run → FAIL (module missing).
- [ ] **Step 3: Implement.** Module docstring explains why pairing is fixed-order and why holds not rejects (spec rulings 3, 4, 6). Helpers: `_num(v) -> float | None` (reuse `po_matching._as_float`); `_code(v)` = casefold, strip spaces/dashes; `_words(text)` = casefold, drop punctuation, drop unit tokens `{"nos","pcs","pc","kg","kgs","ltr","l","m","mm","cm","ea","each","unit","units","set","sets","box","pack","no"}`, return `set`; `_jaccard(a, b)`. `pair()`: three passes in order over still-unpaired invoice lines, each claiming from the unclaimed PO set; description pass: score every unclaimed PO line, accept best only if `best >= 0.6 and best - second >= 0.2`. `match()`: for each line compute quantity/price; if either is `None` → `UNREADABLE_LINE`; unpaired → `UNPAIRED_LINE`; else `required = line.get("receipt_required")`; if `None`: `required = vendor_kind != "service"` (unknown → True); checks in order: `billed + q > qty_ordered` → `OVER_ORDERED` (evidence `"...: billed {billed:g} + {q:g} exceeds {qty_ordered:g} ordered"`), elif required and `billed + q > received + qty_tol` → `NOT_RECEIVED` if received == 0 else `SHORT_RECEIVED` (evidence `"Line {n} '{desc}': billed {billed+q:g}, received {received:g}; {billed+q-received:g} still to arrive."`), and independently `p > unit_price + max(pct*unit_price, abs)` → `PRICE_OVER_PO` (evidence `"...: {p:.2f} against PO {unit_price:.2f} (+{pct:.0%})"`). A line may carry both a quantity and a price signal. `ThreeWay.signals` = ordered unique over lines; `evidence` = one sentence per signalled line. Note: the GRN numbers in evidence come from the caller (Task 4 appends `(GRN-…)` using the book's receipts list) — keep the matcher pure: accept optional `grn_numbers: dict[int, list[str]] = None` and include `" ({', '.join(...)})"` after "received N" when given; the test above passes `None` and checks a substring.
- [ ] **Step 4:** Run → PASS. ruff clean. **Step 5:** Commit `"Match invoice lines to PO lines and goods received, holding instead of rejecting"`.

---

### Task 4: PO book port + step 2.3 in the pipeline

**Files:**
- Create: `invoice_processing/po_book.py`
- Modify: `general_invoice_agent.py` (`Phase2ValidatorAgent.run` ~1925-1955; `_PurchaseOrderFile` ~1976-2030; `_load_purchase_orders` ~2029), `invoice_master_data.yaml` (phase 2 steps ~line 244; `config_defaults` ~line 473), `tenant_settings.py` (`SETTABLE` ~29, `DESCRIPTIONS`, `validate` ~247 numeric list, `apply_to_pipeline` ~388-412), `checks.py` if the catalogue needs the id (it reads the yaml), `sheet_columns.py` `FLAG_STEPS`? — no (hold, not a mismatch flag).
- Test: `tests/test_three_way_pipeline.py` (new), `tests/test_tenant_settings.py`, `tests/test_disabled_checks.py` (known ids now include 2.3)

**Interfaces — Produces:**

```python
# po_book.py
class PurchaseOrderBook(Protocol):
    def find(self, po_number) -> dict | None            # header (po_matching keeps using this)
    def book(self, po_number) -> dict | None            # {"po", "lines", "received", "billed", "grn_numbers": {line: [..]}} or None
class FileBook:  # wraps today's _PurchaseOrderFile records; optional "lines" [{..., "qty_received", "qty_invoiced"}]
    def __init__(self, path=None)
class DbBook:
    def __init__(self, repo, tenant_id)
# general_invoice_agent.py: _load_purchase_orders() -> config.get("purchase_order_book") or FileBook()
# validation entry: {"step": "2.3", "rule": "Invoice lines match PO lines and goods received", "passed", "informational": True,
#                    "evidence": str, "signals": [...], "lines": [LineResult.to_dict()...], "routing_before": "AUTO"|"REVIEW"|None, "rejection_template": None}
```

Move `_PurchaseOrderFile` into `po_book.py` as `FileBook` (keep `find` identical; `general_invoice_agent` imports it and keeps the name `_PurchaseOrderFile = FileBook` for existing tests). `FileBook.book()` returns `None` when the record has no `lines`; else lines with `received[n] = line.get("qty_received", 0)`, `billed[n] = line.get("qty_invoiced", 0)`. `DbBook.find` = `repo.book(...)["po"]`-compatible via `repo.get_order` header; `DbBook.book` = `repo.book(tenant_id, po_number)` plus `grn_numbers` from `repo.list_receipts`.

- [ ] **Step 1: Failing tests** `tests/test_three_way_pipeline.py`. Build config like `tests/test_bank_check_planted.py::_hold` (`monkeypatch.setitem(general_invoice_agent._config_store, "CONFIG", {**DEFAULT_CONFIG, "duplicate_check_enabled": False, "require_purchase_order": False, "purchase_order_book": book, "vendor_master_records": [vendor], **overrides})`). Use a tiny fake book class with `find`/`book` returning dicts from a `po` fixture (`PO-1`, vendor "Acme Pty Ltd", lines `[po(1,"Bearing 6205",10,100)]`). Extraction: `{"invoice": {"po_number": "PO-1", "vendor_name": "Acme Pty Ltd", "invoice_total_inc_tax": 500, "currency": "AUD", "line_items": [{"description": "Bearing 6205", "quantity": 5, "unit_price": 100, "amount_ex_tax": 500}], "_routing": "AUTO", "_evidence": {"_summary": {"fields_needing_review": []}}}}`.

```python
def test_short_receipt_holds_for_review_and_never_rejects(tmp_path, pipeline):
    out = pipeline(received={1: 2})
    step = next(v for v in out["validations"] if v["step"] == "2.3")
    assert out["decision"] == "CONTINUE" and step["passed"] is False and step["informational"] is True
    assert step["signals"] == ["short_received"] and step["routing_before"] == "AUTO"
    assert pipeline.extraction["invoice"]["_routing"] == "REVIEW"
    assert "receipt: short_received" in pipeline.extraction["invoice"]["_evidence"]["_summary"]["fields_needing_review"]

def test_full_receipt_passes_untouched(pipeline):
    out = pipeline(received={1: 10}); step = next(v for v in out["validations"] if v["step"] == "2.3")
    assert step["passed"] and pipeline.extraction["invoice"]["_routing"] == "AUTO"

def test_step_2_2_rejections_still_reject_and_skip_2_3(pipeline):
    out = pipeline(po_number="PO-NOPE")
    assert out["decision"] == "REJECT" and not any(v["step"] == "2.3" for v in out["validations"])

def test_po_without_lines_is_header_only(pipeline):
    out = pipeline(lines=None); step = next(v for v in out["validations"] if v["step"] == "2.3")
    assert step["passed"] and "header match only" in step["evidence"]

def test_book_failure_holds_not_passes(pipeline):
    out = pipeline(book_raises=RuntimeError("db down")); step = next(v for v in out["validations"] if v["step"] == "2.3")
    assert step["passed"] is False and step["signals"] == ["unchecked"] and pipeline.extraction["invoice"]["_routing"] == "REVIEW"

def test_disabled_check_skips(pipeline):
    out = pipeline(received={1: 0}, disabled_checks=["2.3"])
    assert next(v for v in out["validations"] if v["step"] == "2.3").get("skipped")
```

`tests/test_tenant_settings.py`: `three_way_qty_tolerance` accepts `0`, `1.5`, refuses `-1` and `True`. Grep `tests/test_disabled_checks.py` for the known-id assertion and add `"2.3"`.

- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement.** In `Phase2ValidatorAgent.run`, after the 2.2 entry is appended and only when `entry["passed"]` and `invoice.get("po_number")` and `"2.3" not in disabled_checks`:

```python
# Step 2.3: line-level three-way match. Holds, never rejects (see three_way).
if entry["passed"] and invoice.get("po_number") and "2.3" not in (config.get("disabled_checks") or []):
    orders = _load_purchase_orders()
    try:
        book = orders.book(invoice["po_number"])
    except Exception as exc:  # a book that cannot be read holds; it never passes silently
        book, failure = None, f"Purchase-order lines could not be checked: {exc}"
    else:
        failure = None
    if failure:
        step23 = {"step": "2.3", "rule": RULE_23, "passed": False, "informational": True,
                  "evidence": failure, "signals": ["unchecked"], "lines": [], "rejection_template": None}
    elif book is None or not book.get("lines"):
        step23 = {..., "passed": True, "evidence": "No PO lines on file; header match only.", "signals": [], "lines": []}
    else:
        vendor = _load_vendors().find(invoice.get("vendor_name"))
        result = three_way.match(invoice.get("line_items") or [], book["lines"], book["received"], book["billed"],
                                 vendor_kind=vendor.kind or None if vendor else None,
                                 qty_tol=float(config.get("three_way_qty_tolerance", 0) or 0),
                                 price_tol_pct=config.get("po_tolerance_pct", po_matching.DEFAULT_TOLERANCE_PCT),
                                 price_tol_abs=config.get("po_tolerance_abs", po_matching.DEFAULT_TOLERANCE_ABS),
                                 grn_numbers=book.get("grn_numbers"))
        step23 = {..., "passed": result.passed, "evidence": "; ".join(result.evidence) or "Every line is paired, received and priced within the order.",
                  "signals": list(result.signals), "lines": [l.to_dict() for l in result.lines]}
    if not step23["passed"]:
        step23["routing_before"] = invoice.get("_routing")
        invoice["_routing"] = "REVIEW"
        needing = invoice.setdefault("_evidence", {}).setdefault("_summary", {}).setdefault("fields_needing_review", [])
        needing.extend(f"receipt: {s}" for s in step23["signals"] if f"receipt: {s}" not in needing)
        self.save_artifact(output_folder, "02_extraction.json", extraction)
    validations.append(step23)
```

(Verify how `save_artifact` is called in step 3.4 and mirror it; `_settle` already ignores informational failures.) YAML: add step `{id: "2.3", name: "Three-way match", type: "informational", informational_only: true, rejection_template: null}` after 2.2, and `three_way_qty_tolerance: 0` under `config_defaults`. `tenant_settings`: `"three_way_qty_tolerance": (int, float)` in `SETTABLE`, a `DESCRIPTIONS` sentence ("Units an invoice line may exceed the quantity received before it is held. 0 holds any shortfall."), add the key to the non-negative numeric list in `validate`. `apply_to_pipeline`: after vendors, `po = service.get_purchase_orders()` (Task 6 adds the accessor; until then guard with `getattr(service, "get_purchase_orders", None)`); `config["purchase_order_book"] = po_book.DbBook(po, tenant_id) if po is not None and po.has_orders(tenant_id) else None`. `_load_purchase_orders()`: `return _get_config().get("purchase_order_book") or po_book.FileBook()`.
- [ ] **Step 4:** Run new tests + `tests/test_po_matching*.py tests/test_validation_spec_drift.py tests/test_disabled_checks.py tests/test_tenant_settings.py` → PASS; then full suite. **Step 5:** Commit `"Add step 2.3: hold invoices whose lines outrun the order or the goods received"`.

---

### Task 5: Routes: imports, PO detail, GRN list; `GET`/`PUT` onto the DB

**Files:**
- Create: `invoice_processing/api/po_routes.py`
- Modify: `invoice_processing/api/service.py` (globals ~123-134: `_purchase_orders = None`; accessor after `get_gl_rules` ~379; `include_router` ~1674), `invoice_processing/api/config_routes.py:183-282`, `tests/conftest.py:63-88` (add `_purchase_orders`), `tests/test_tenant_isolation_api.py` (`PROBED`, module list ~1150, new probe), `tests/test_config_routes.py` (file-mode tests keep passing: without a DB they still use the file)
- Test: `tests/test_po_routes.py` (new; add to CI Postgres list)

**Interfaces — Produces:**

```python
# service.py
def get_purchase_orders():  # memoised like get_gl_rules; _table_exists("purchase_orders", "Purchase orders", raise_on_error=True)
# po_routes.py (router included in service)
POST /api/purchase-orders/import   clerk+   body: JSON {"purchase_orders": [...]} OR multipart file (.csv/.json) → {"created", "updated", "lines"} ; 400 {"detail": "row 7: qty_ordered must be a number ≥ 0; row 9: ..."}
GET  /api/purchase-orders/{po_number}  clerk+ → repo.get_order(...) or 404
POST /api/goods-receipts/import   clerk+   → {"recorded": n, "skipped_duplicates": n, "rematch_queued": n}
GET  /api/goods-receipts?po_number=  clerk+ → {"goods_receipts": [...]}
NEEDS_DB = "Purchase orders need a database. Configure DATABASE_URL."
MAX_IMPORT_BYTES = 5 * 1024 * 1024
def parse_po_csv(text) -> list[dict]; def parse_grn_csv(text) -> list[dict]   # one row per line, header columns repeated
def validate_orders(records) -> list[str]  # every problem, "row N: ..."
def validate_receipts(records) -> list[str]
def queue_rematches(principal, po_numbers) -> int   # Task 7 fills this in; here returns 0 with a TODO-free stub that calls runner.enqueue_rematch when it exists
```

CSV columns (documented in the route docstring and the UI templates): PO — `po_number,vendor_name,total_amount,currency,status,amount_invoiced,customer_name,valid_from,valid_to,amount_basis,line_number,item_code,description,qty_ordered,unit_price,uom,receipt_required,qty_invoiced`; GRN — `grn_number,po_number,received_on,reason,line_number,item_code,qty_received`. `receipt_required` accepts `true/false/yes/no/1/0/""`.

`config_routes`: `list_purchase_orders` → if `service.get_purchase_orders()` is not None use `repo.list_orders(...)` (rows gain `line_count`, `received_total`, `billed_total`; `source: "database"`), else the file; `replace_purchase_orders` → with a repo: delete every PO of the tenant not in the upload and `upsert_orders(records)` (replace-all semantics preserved; `lines` optional), audit unchanged, else the file. Keep `require_role("clerk")` / `("admin")`.

- [ ] **Step 1: Failing tests** `tests/test_po_routes.py` (fixture `env` copied from `tests/test_gl_routes.py` with `PgPurchaseOrderRepository` monkeypatched as `service._purchase_orders` and a `LocalQueue` as `service._queue` — check `service.get_queue` global name in `resolve_adapters_afresh`):

```python
def test_clerk_imports_json_and_lists_and_reads_detail(env):
    r = env["client"]("clerk").post("/api/purchase-orders/import", json={"purchase_orders": [ORDER]})
    assert r.status_code == 201 and r.json() == {"created": 1, "updated": 0, "lines": 2}
    assert env["client"]("clerk").get("/api/purchase-orders").json()["purchase_orders"][0]["line_count"] == 2
    assert env["client"]("clerk").get("/api/purchase-orders/PO-9001").json()["lines"][0]["remaining"] == 8.0
    assert env["client"]("viewer").get("/api/purchase-orders").status_code == 403
    assert "purchase_orders_imported" in [e["event_type"] for e in env["events"]()]

def test_csv_import_one_row_per_line(env):
    csv_text = "po_number,vendor_name,total_amount,currency,line_number,description,qty_ordered,unit_price,receipt_required\nPO-1,Acme,100,INR,1,Bolt,10,5,yes\nPO-1,Acme,100,INR,2,Freight,1,50,no\n"
    r = env["client"]("clerk").post("/api/purchase-orders/import", files={"file": ("po.csv", csv_text, "text/csv")})
    assert r.status_code == 201 and r.json()["lines"] == 2

def test_every_bad_row_is_named_and_nothing_is_stored(env):
    bad = {"purchase_orders": [{**ORDER, "lines": [{"line_number": 1, "description": "x", "qty_ordered": -1, "unit_price": "abc"}]}]}
    r = env["client"]("clerk").post("/api/purchase-orders/import", json=bad)
    assert r.status_code == 400 and "row 1: qty_ordered" in r.json()["detail"] and "unit_price" in r.json()["detail"]
    assert env["client"]("clerk").get("/api/purchase-orders").json()["count"] == 0

def test_import_over_5mb_is_refused(env):
    big = "po_number,vendor_name,total_amount\n" + "PO-1,Acme,1\n" * 400_000
    assert env["client"]("clerk").post("/api/purchase-orders/import", files={"file": ("po.csv", big, "text/csv")}).status_code == 413

def test_grn_import_refuses_unknown_po_and_is_idempotent(env):
    env["client"]("clerk").post("/api/purchase-orders/import", json={"purchase_orders": [ORDER]})
    grn = {"goods_receipts": [{"grn_number": "GRN-1", "po_number": "PO-9001", "received_on": "2026-10-01", "lines": [{"line_number": 1, "qty_received": 6}]}]}
    assert env["client"]("clerk").post("/api/goods-receipts/import", json=grn).json()["recorded"] == 1
    assert env["client"]("clerk").post("/api/goods-receipts/import", json=grn).json()["skipped_duplicates"] == 1
    nope = {"goods_receipts": [{**grn["goods_receipts"][0], "grn_number": "G2", "po_number": "PO-NOPE"}]}
    assert env["client"]("clerk").post("/api/goods-receipts/import", json=nope).status_code == 400
    assert env["client"]("clerk").get("/api/goods-receipts?po_number=PO-9001").json()["goods_receipts"][0]["grn_number"] == "GRN-1"

def test_503_without_database(monkeypatch, tmp_path):
    # follow test_gl_routes' no-DB test: service._purchase_orders stays None, DATABASE_URL unset
    ...assert r.status_code == 503 and "needs a database" in r.json()["detail"]

def test_put_replace_all_writes_the_database(env):
    env["client"]("clerk").post("/api/purchase-orders/import", json={"purchase_orders": [ORDER]})
    r = env["client"]("admin").put("/api/purchase-orders", json={"purchase_orders": [{"po_number": "PO-2", "vendor_name": "B", "total_amount": 5}]})
    assert r.status_code == 200
    assert [p["po_number"] for p in env["client"]("clerk").get("/api/purchase-orders").json()["purchase_orders"]] == ["PO-2"]
```

Isolation: a `po_db` fixture like `gl_db` (two tenants, repo monkeypatched) and `test_purchase_orders_do_not_cross_tenants`: tenant B's `GET /api/purchase-orders/PO-9001` → 404, GRN import against A's PO → 400, A untouched. Add the four new paths to `PROBED` and `po_routes` to the module list.

- [ ] **Step 2:** Run → FAIL. **Step 3: Implement** per the interface. Size check: for multipart read `data = await file.read()`; `if len(data) > MAX_IMPORT_BYTES: raise HTTPException(413, "Import is over 5 MB.")`; JSON bodies use Pydantic models (`PoImport(purchase_orders: list[dict])`) — enforce the same cap on `len(request.body)` via a dependency reading `Content-Length`. Validation: every problem collected into one list; `po_number` present and normalisable; `vendor_name` non-blank ≤512; numbers finite ≥0 (`total_amount`, `qty_ordered`, `unit_price`, `qty_invoiced`, `amount_invoiced`); `line_number` positive int unique within the PO; `description` ≤8000; `currency` 3 letters; `status` ≤32; dates `YYYY-MM-DD`; `grn_number` ≤128 ; each GRN line has `line_number` or `item_code`; negative `qty_received` needs `reason`. Repository `ValueError` → 400 with its message. Audit via `service.get_audit().emit(principal.context(), "purchase_orders_imported", {"created", "updated", "lines"})` and one `goods_receipt_recorded` per recorded GRN (`{"grn_number", "po_number"}`); add `"goods": "goods_receipt"` and `"three": "invoice"`-safe mappings in `repositories._ENTITY_TYPES` (`goods_receipt_recorded` → head `goods_receipt`; `three_way_rematched` falls back to invoice via case_id — fine).
- [ ] **Step 4:** Run `tests/test_po_routes.py tests/test_config_routes.py tests/test_tenant_isolation_api.py` → PASS; full suite; ruff. **Step 5:** Commit `"Import purchase orders with lines and goods receipts, whole file or nothing"`.

---

### Task 6: Re-match job (`rematch.py`, worker dispatch, enqueue on GRN import)

**Files:**
- Create: `invoice_processing/rematch.py`
- Modify: `invoice_processing/worker/runner.py` (`REMATCH = "case.rematch"` beside line 52; `Worker.handle` lines 661-721; `enqueue_rematch`), `invoice_processing/api/po_routes.py` (`queue_rematches`), `invoice_processing/db/repositories.py` (`PgCaseRepository.held_by_receipt(tenant_id, po_number) -> list[case_id]`: invoices with `po_number` normalised equal, no workflow decision, and `"receipt: "` entries in the stored extraction's `fields_needing_review` — simplest: `routing == "REVIEW"` rows whose `fields["phase2"]` step 2.3 has `passed is False`, filtered in Python)
- Test: `tests/test_rematch.py` (new), `tests/test_worker.py` (dispatch), `tests/test_po_routes.py` (`rematch_queued`)

**Interfaces — Produces:**

```python
# rematch.py
def rematch_case(ctx, cases, book, *, vendors, config: dict, audit, now) -> dict  # {"before": [...signals], "after": [...], "cleared": bool}
# runner.py
REMATCH = "case.rematch"
def enqueue_rematch(queue, tenant_id: str, case_id: str, po_number: str) -> str   # payload {"type": REMATCH, "tenant_id", "case_id", "po_number"}
# Worker.handle: if payload.get("type") == REMATCH: return self._handle_rematch(message)  (before the document_id check)
```

`rematch_case`: `artifacts = cases.get_artifacts(ctx)`; return early if `artifacts["workflow"].get("decision")`; recompute the 2.3 entry with the exact code of Task 4 factored into a shared function `general_invoice_agent.three_way_entry(invoice, orders, config, vendors) -> dict` (Task 6 refactors Task 4's inline block into this function and the pipeline calls it — one implementation, two callers). Then one `cases.update_artifact(ctx, "phase2", mutate)` replacing the step-2.3 entry, and one `cases.update_artifact(ctx, "extraction", mutate)` that rewrites `fields_needing_review` (drop every `receipt: *`, add the new ones) and sets `_routing`: if the new entry passes **and** no `receipt:` remains **and** the list is empty → `routing_before` from the old entry (default `"AUTO"`); if it fails → `"REVIEW"`; otherwise leave. Then `cases.project_case(ctx, fresh)` and `service.reroute(ctx, fresh, ["receipt"])`, audit `three_way_rematched` `{"po_number", "before", "after", "cleared"}`. Any exception → audit `three_way_rematch_failed` and re-raise (worker retries up to 3, then dead-letters; the case stays held).

`Worker._handle_rematch`: build `RunContext(tenant_id, case_id, run_id=message.id, actor="worker")`; `tenant_settings.apply_to_pipeline(tenant_id)` (so the book, vendors and tolerances are the tenant's); call `rematch_case(...)` with `book = config["purchase_order_book"]`; ack on success, nack on exception (max attempts as for documents, keyed by `case_id`), dead-letter if malformed. No claim-store involvement (re-matching twice is idempotent).

`po_routes.queue_rematches(principal, po_numbers)`: `cases = service.get_cases(); queue = service.get_queue()`; for each PO, `for case_id in cases.held_by_receipt(tenant, po): enqueue_rematch(...)`; returns the count; `FsCaseRepository` gets a simple `held_by_receipt` that scans artifacts (dev fallback).

- [ ] **Step 1: Failing tests.** `tests/test_rematch.py` with the Task-4 pipeline fixture to produce a held case into an `FsCaseRepository` (or `PgCaseRepository` on scratch SQLite — prefer Pg, it is what production uses): (a) a fuller receipt clears: `after == []`, `cleared`, extraction `_routing == "AUTO"`, `fields_needing_review == []`, audit `three_way_rematched`; (b) still short: stays `REVIEW`, signals updated; (c) another hold reason (`fields_needing_review` also has `"bank: bank_unverified"`) stays `REVIEW` even when receipts clear; (d) decided case untouched; (e) book raising → `three_way_rematch_failed` audited and exception propagates. `tests/test_worker.py`: a `REMATCH` message reaches `_handle_rematch` (monkeypatch `rematch.rematch_case`) and is acked; a malformed one is dead-lettered; a document message still goes through claims as before. `tests/test_po_routes.py`: after seeding a held case for `PO-9001`, the GRN import returns `rematch_queued == 1` and the LocalQueue depth is 1 with `type == "case.rematch"`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement as above (refactor Task 4's block into `three_way_entry` first, run Task 4's tests to confirm nothing moved). **Step 4:** Run `tests/test_rematch.py tests/test_worker.py tests/test_po_routes.py tests/test_three_way_pipeline.py` → PASS; full suite; ruff. **Step 5:** Commit `"Re-match held invoices when a goods receipt arrives, clearing the hold when nothing else holds"`.

---

### Task 7: Approval-time re-check (`OverBilled` → 409)

**Files:**
- Modify: `invoice_processing/approval_flow.py` (new `class OverBilled(Refused)`; in `decide()` before `repo.decide` ~line 254), `invoice_processing/api/service.py:1245-1248` (`except approval_flow.OverBilled → 409` **before** the `Refused` clause), `approval_flow.py` `NEEDS_DB`-style `BOOK_DOWN = "Purchase-order lines could not be re-checked. Try again."`
- Test: `tests/test_approval_routes.py`

**Interfaces — Consumes:** `three_way.match`, `service.get_purchase_orders()`, `po_book.DbBook`. **Produces:** `approval_flow.OverBilled`.

Logic in `decide()` when `approve` and this is the final pending step (`current` is the last pending in `repo.chain`) and the stored 2.3 entry exists with `lines`: `book = DbBook(service.get_purchase_orders(), t).book(po_number)` (`None` repo → skip the check: file-backed stacks have no cumulative record); re-run `three_way.match` with the live `billed`; if any line now carries `over_ordered`/`short_received`/`not_received` that the stored entry did **not** carry → `raise OverBilled(f"Line {n} would bill {billed+q:g} of {received:g} received; another invoice was approved since this was checked.")` (name the newest approved invoice for that PO line via `repo.last_approved_for(t, po_number, line_number)` if cheap; else the generic sentence). Admin `override_reason` bypasses as for other refusals (keep inside the existing `refusal` path: set `refusal = sentence` and let the existing `if refusal and not (admin and override_reason)` raise — but raise `OverBilled` instead of `Refused` so the API can answer 409). Book exception → `raise Refused(BOOK_DOWN)` mapped to 503 in `act` (add `except approval_flow.BookUnavailable → 503`).

- [ ] **Step 1: Failing tests** (`tests/test_approval_routes.py`, env has a Pg approvals repo; add a `PgPurchaseOrderRepository` to the env and seed `ORDER` + a GRN of 10 on line 1; give `CASE`'s extraction `po_number="PO-9001"` and a stored passing 2.3 entry with `lines=[{line_number:1, po_line_number:1, quantity:6}]`): (a) approve succeeds when `billed+6 ≤ 10`; (b) after inserting an approved invoice row consuming 6 (as in Task 2's billed test), approving returns **409** with "would bill 12 of 10 received"; (c) reject still works; (d) admin with `override_reason` approves and `approval_overridden` is audited; (e) book raising → 503.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS; full suite; ruff. **Step 5:** Commit `"Refuse the final approval when another invoice has consumed the received units since the match"`.

---

### Task 8: Frontend

**Files:** `frontend/src/lib/api.ts`, `frontend/src/app/purchase-orders/page.tsx`, `frontend/src/app/invoices/page.tsx`, `invoice_processing/api/service.py:661-684` (`_findings` passes `lines`), `invoice_processing/case_projection.py` `findings` (pass `signals`, `lines` through to the findings table JSON if it has an evidence/JSON column; else leave — the UI reads `/api/invoices/{id}` which uses `_findings` from artifacts)

- [ ] **Step 1 (backend, test-first):** in `tests/test_approval_routes.py` or `tests/test_service_findings*.py`: `GET /api/invoices/{CASE}` finding for step 2.3 includes `lines` and `signals`. Implement in `_findings`. Commit `"Hand the workbench each line's three-way result"`.
- [ ] **Step 2 (types):** `api.ts`: `Finding.lines?: ThreeWayLine[] | null`; `type ThreeWayLine = {line_number; description; po_line_number: number|null; paired_by: "item_code"|"line_number"|"description"|null; quantity; unit_price; billed; received; signals: string[]; evidence: string}`; `PurchaseOrder` gains `line_count?: number; received_total?: number; billed_total?: number`; `type PurchaseOrderDetail = PurchaseOrder & {lines: PoLine[]; goods_receipts: Grn[]}` with `PoLine = {line_number, item_code, description, qty_ordered, unit_price, uom, receipt_required, received, billed, remaining}` and `Grn = {grn_number, received_on, reason, recorded_by, lines: {line_number, qty_received}[]}`; `type ImportResult = {created?: number; updated?: number; lines?: number; recorded?: number; skipped_duplicates?: number; rematch_queued?: number}`.
- [ ] **Step 3 (`/purchase-orders`):** keep the existing admin "Replace from JSON" control; add two clerk+ controls "Import purchase orders (CSV/JSON)" and "Import goods receipts (CSV/JSON)" using the `documents/page.tsx:44-62` FormData pattern (`body.append("file", file)`, `api("/api/purchase-orders/import", {method: "POST", body})`), toast the counts (`"Recorded 2 receipts; 1 invoice queued for re-match"`), errors via `ApiError.friendly` (the 400 detail lists rows). Two "Download template" links built client-side as blobs with the CSV headers from Task 5. Each PO row gets an expand toggle loading `GET /api/purchase-orders/{po_number}` and rendering a lines table (Line / Item / Description / Ordered / Received / Billed / Remaining / Receipt?) and a GRN list. Keep the empty-state text `"No purchase orders loaded"` and the `#po-upload` confirm flow unchanged (existing e2e depends on them).
- [ ] **Step 4 (workbench):** in `invoices/page.tsx` beside `BankChangeShortcut`, a `ThreeWayPanel({detail})`: `const step = detail.findings.find(f => f.step === "2.3")`; render nothing if absent; eyebrow "Three-way match"; one row per `step.lines`: `#n description`, "PO line k (by item code)" or "unpaired", `billed`/`received`, `unit_price`, chips per signal (`flag` class, label `signal.replace(/_/g, " ")`), evidence under the row. Approve 409 already surfaces via `ApiError.friendly` (non-403 → message).
- [ ] **Step 5:** `cd frontend && npm run lint && npx tsc --noEmit` → clean. Commit `"Show PO lines, receipts and each line's three-way result"`.

---

### Task 9: e2e seed, re-match thread, Playwright tests

**Files:** `scripts/e2e_api.py`, `frontend/e2e/golden-path.spec.ts`

- [ ] **Step 1 (seed):** in `seed()` clone `case_005` as `case_006_po` (vendor "Bearing Test Supplies"); after cloning, edit its `02_extraction.json` (`invoice.po_number = "PO-E2E-1"`, `_routing = "REVIEW"`, `fields_needing_review = ["receipt: short_received"]`) and `04_phase2_validation.json` (append a 2.2 passed entry `{"step": "2.2", "rule": "Valid purchase order", "passed": true, "implemented": true, "outcome": "matched", "po_number": "poe2e1", "evidence": "..."}` and a failing 2.3 entry with `lines=[{line_number:1, po_line_number:1, paired_by:"description", quantity:2, unit_price:…, billed:0, received:1, signals:["short_received"], evidence:"Line 1 ...: billed 2, received 1; 1 still to arrive."}, {line_number:2, ...passed, receipt_required false}]`, `routing_before: "AUTO"`). Read the golden extraction to copy its two line descriptions/quantities exactly. In `seed_database()` after the users: `PgPurchaseOrderRepository().upsert_orders(TENANT, [{"po_number": "PO-E2E-1", "vendor_name": "Bearing Test Supplies", "total_amount": <golden total>, "currency": <golden currency>, "lines": [{line 1 from the extraction, qty_ordered = its quantity, receipt_required True}, {line 2 ..., receipt_required False}]}], uploaded_by="seed")` and `record_receipts(... GRN-E2E-0 with line 1 qty_received 1 ...)`. Ensure `case_006_po` sorts **after** `case_005`? — No: `case_005` must stay the queue's last row (existing test); name it `case_004_po` (sorts before `case_005`).
- [ ] **Step 2 (thread):** in `main()` after `seed_database()`, before uvicorn, start `threading.Thread(target=_drain_rematches, daemon=True)`: a loop over `service.get_queue().pull(10)` every 0.5 s that, for messages with `payload["type"] == "case.rematch"`, calls `Worker(queue, claims=service.get_claim_store(...), audit=service.get_audit()).handle(message)` and otherwise leaves them (documents stay queued as today — do not ack them). Since uvicorn runs in a subprocess, run the drain as a **second subprocess** (`python -c` or `scripts/e2e_api.py --rematch-worker`) with the same env, started before `subprocess.call(uvicorn)`, killed in `finally`. Document in the module docstring.
- [ ] **Step 3 (tests):** in `golden-path.spec.ts`:
  1. "PO import shows lines and receipts": `as(page, "alice@e2e.test:clerk")`, `goto("/purchase-orders")`, upload a CSV via `setInputFiles` on the PO import input with one PO `PO-E2E-9` and 2 lines, expect toast "Imported 1 purchase order", expand the row, expect cells "Ordered 10", then upload a GRN CSV for it, expect "Recorded 1 receipt", expand, expect "Received 4". Delete nothing (POs have no delete; use unique numbers per test so it is order-independent).
  2. "Held invoice shows the three-way panel and a GRN clears it": `goto("/invoices?case=case_004_po")`, expect text "Three-way match", chip "short received", evidence "1 still to arrive"; upload GRN CSV `GRN-E2E-1,PO-E2E-1,2026-10-09,,1,,1` on `/purchase-orders`, expect toast containing "1 invoice queued"; `expect.poll` up to 10 s on `GET /api/invoices/case_004_po` (`page.request.get`) until `routing === "AUTO"`; reload the workbench and expect the 2.3 finding rendered as "pass".
- [ ] **Step 4:** `cd frontend && npm run test:e2e` → all pass (now 24). Commit `"Prove in the browser that a goods receipt clears a held invoice"`.

---

### Task 10: Planted measurement + benchmark guard + CHANGELOG

**Files:** `tests/fixtures/three_way/planted.json`, `tests/test_three_way_planted.py`, `benchmark/CHANGELOG.md`

- [ ] **Step 1 (fixture):** script under the scratchpad (not committed) reads `benchmark/runs/after-ecb9bbd/raw/B.jsonl` and `benchmark/dataset/ground_truth.json`; selects Company B records with `record_kind == "INVOICE"`, `expected.final_decision == "ACCEPT"` and a `correct.po_number` (the 18 clean PO invoices); for each writes `{invoice_id, extracted (the stored extraction incl. line_items and po_number), po: {po_number, vendor_name, total_amount, currency, lines: from correct.lines with qty_ordered=quantity, unit_price, receipt_required true}, received: {n: quantity}, billed: {n: 0}, vendor_kind: "goods", fault: null}`; then for 9 of them plant exactly one fault by editing `received`/`po.lines`/`extracted.line_items`: `short_received` (received = qty-1), `not_received` (0), `price_over_po` (PO price ×0.9), `unpaired_line` (append a "Handling surcharge" line to the extraction), `over_ordered` (qty_ordered = quantity-1), and 3 `split_delivery` controls (received = quantity, billed = quantity - this quantity? — no: billed 0, received = quantity but the invoice quantity halved: expected pass). Save as `planted.json` with a top-level `"source"` note like the bank fixture.
- [ ] **Step 2 (test):** `tests/test_three_way_planted.py` runs `three_way.match(rec.extracted.line_items, rec.po.lines, rec.received, rec.billed, vendor_kind=rec.vendor_kind)` per record and asserts: every record with `fault is None` passes; every planted record's `signals` contains exactly its fault; prints a summary (clean passed N/N, faults caught M/M, description-pairing rate) — the assertions are the score. Note repeated descriptions in B invoices (MFG-INV-014) and confirm pairing still resolves by line order fallback? — No fallback by order exists (spec); if a record is unpaired because of duplicate descriptions, **record it in the CHANGELOG as a known gap**, do not add a pairing rule.
- [ ] **Step 3 (guard):** `CLOUD_SQL_INSTANCE="" GCS_BUCKET="" ./.venv/bin/python -m benchmark.harness.replay --run after-ecb9bbd --profile benchmark/harness/profiles/after.json --out replay-three-way` then `score`; compare against `replay-approvals` (the last run present; CHANGELOG records `replay-gl-coding` was identical to it): expected 40/45 posted untouched, 1 posted wrongly, 98.0%, byte-identical `results.csv`. `benchmark/comparison/` untouched (do not run `compare`, or `git checkout benchmark/comparison/` after).
- [ ] **Step 4:** CHANGELOG entry "2026-10-09 — Three-way match: no benchmark decision moves; planted set N/N" with the numbers, the gap list, and the pairing-rate figure. Commit `"Measure three-way match on planted receipt faults; benchmark unchanged"`.

---

### Task 11: Gates, review, PR

- [ ] `bash benchmark/harness/gates.sh` → 9/9 PASS (pytest, ruff check/format, verify_phase0/1, eval gate, frontend lint/tsc/e2e).
- [ ] If Docker is running: Postgres 16 run of `tests/test_purchase_order_repository.py tests/test_po_routes.py tests/test_rematch.py tests/test_approval_routes.py tests/test_gcs_and_migrations.py` with `TEST_DATABASE_URL=postgresql+pg8000://postgres:postgres@localhost:55432/t`; else note CI is the first Postgres run.
- [ ] Whole-branch review (most capable model, read-only) → one fix wave with tests.
- [ ] Independent scripted verification against a fresh DB-backed stack (reuse the audit approach from the GL slice: `scripts/e2e_api.seed()` + TestClient as several identities): PO/GRN import, held → re-match → cleared, approval 409 race, tenant isolation, 503 without DB.
- [ ] Browser check on the manual stack (`preview_start manual-a-api-worker` + `manual-frontend`): import a PO and GRN, see lines, see a held invoice's panel.
- [ ] Ask the user, then push `phase3/three-way-match`, open the PR (body ends with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`), merge only on the user's explicit word with `gh pr merge N --merge --match-head-commit <full sha>`.

## Verification (end-to-end)

1. `./.venv/bin/python -m pytest tests/ -q -p no:cacheprovider` — all pass (expect ~2061 + ~60 new).
2. `bash benchmark/harness/gates.sh` — 9/9.
3. Manual stack: `/purchase-orders` → import `PO-M1` (2 lines) and `GRN-M1` (line 1 short) → a processed PO invoice for that PO shows "Three-way match · short received"; import a completing GRN → within 60 s the invoice routing shows AUTO and the 2.3 finding passes; approve it; import a second invoice for the same units → approval returns 409 naming the line.
4. CI green on the PR (test, postgres, frontend, e2e).

## Self-review notes

- Spec §1 ingest → Tasks 1, 2, 5; §2 matcher → Task 3; §3 step/re-match/approval → Tasks 4, 6, 7; §4 API/UI/errors → Tasks 5, 8 (fail-closed: Task 4 `unchecked`, Task 6 re-raise + audit, Task 7 503); §5 tests/measurement → every task + Tasks 9, 10. Settings → Task 4.
- Names used consistently: `PgPurchaseOrderRepository.book/upsert_orders/record_receipts/get_order/list_orders/list_receipts/has_orders`; `po_book.FileBook/DbBook`; `three_way.match/pair/LineResult/ThreeWay`; `general_invoice_agent.three_way_entry`; `rematch.rematch_case`; `runner.REMATCH/enqueue_rematch`; `approval_flow.OverBilled`; `service.get_purchase_orders`.
- Deviations recorded in Task 0 (clerk-only list; e2e drain process).
