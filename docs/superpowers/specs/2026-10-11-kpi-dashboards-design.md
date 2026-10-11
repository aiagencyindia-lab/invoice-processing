# KPI dashboards — design

Phase 3, sub-project 5. Brainstormed with the user 2026-10-11; every section
below was approved in turn.

## Goal

Give an AP manager the weekly operating numbers — how much came in, how much
cleared untouched, why the rest needed a person, how long decisions took, how
often receiving held an invoice, and what the model spend was — over a chosen
window, with a drill-down to the invoices behind each number.

## Decisions (user rulings)

1. **Audience: AP manager, weekly ops.** Windowed numbers (7/30/90 days, all
   time), not pilot-proof accuracy figures.
2. **Cycle time ends at the final decision**, not at export. Touchless
   invoices count with near-zero cycle time.
3. **An exception is anything not auto-posted**: review, rule rejection,
   three-way hold, or waiting for an approver. Exception rate = 1 − touchless.
4. **Three-way hold rate is over PO-backed invoices only.**
5. **AI cost per invoice is included**, labelled as model cost, not fully
   loaded cost.
6. **Approach: SQL aggregates over typed columns plus one new column**
   (`invoices.decided_at`). No Python-over-all-cases, no rollup table.

## 1. Definitions

All numbers cover invoices whose **document was received in the window**.
`days` ∈ {0, 7, 30, 90}; 0 = all time; default 30. Scanned POs/GRNs
(`documents.kind` not null) are excluded, the same filter `SheetRepository._base`
uses.

Per invoice:

- **Received**: `documents.created_at` (already exposed as `received_at`).
- **Decided**: new column `invoices.decided_at`.
  - Human decision → the workflow artifact's `decided_at` (written by
    `_record_workflow` in `api/service.py`).
  - Touchless (routing AUTO, machine verdict accept, no failed step-2.3
    finding, no approval chain) → the time the case was first projected.
  - Null while the invoice waits on a person.
- **Touchless**: decided and `human_decision` is null.
- **Exception**: not touchless. One bucket per invoice, first match wins:
  1. `rejected_by_rules` — `case_projection.machine_verdict` is reject.
  2. `three_way_hold` — a `findings` row with `step='2.3'` and `passed=false`.
  3. `low_confidence` — `routing='REVIEW'`.
  4. `approval_required` — has `approval_steps` rows (pending or decided).
  5. `manual_decision` — none of the above, but a person decided it (for
     example an AUTO invoice approved from the workbench).
  The buckets are exhaustive over exceptions and say why a person was needed,
  not the current state: an invoice that was REVIEW and is now approved still
  counts under `low_confidence`.
- **Cycle time**: `decided_at − received` in hours; median and p90 over
  decided invoices (nearest-rank). Rows with `decided_at < received` or a null
  received time are excluded and counted in `skipped`, so a bad clock can
  never make the median look better (fail closed).
- **Open**: `decided_at` null → count and the age of the oldest, in hours.
- **Three-way hold rate**: invoices with a failed 2.3 finding ÷ invoices with
  a non-null `po_number`.
- **AI cost per invoice**: mean of `pipeline_runs.cost_usd` over invoices that
  have a run (one row per invoice after the fix in §2), with the total.
- Every rate is `null` when its denominator is 0, as `touchless_rate` is today.

## 2. Storage

- **Migration** (new head after `d8e1f2a3b4c5`): add
  `invoices.decided_at TIMESTAMPTZ NULL` and index
  `ix_invoices_tenant_decided (tenant_id, decided_at)`. Backfill in the same
  migration: parse `fields->'workflow'->>'decided_at'` for every row; set the
  column when it parses, otherwise leave it null. The backfill reads JSON only
  and never writes `fields`. Touchless history stays null-decided; only rows
  projected after this change get a touchless `decided_at` (documented limit).
- **Projection** (`PgCaseRepository.project_case` and
  `case_projection.invoice_columns`): compute `decided_at` as defined above,
  and **delete the invoice's existing `pipeline_runs` rows before adding the
  new one**. Today every re-projection (approval, field correction, GL coding,
  rematch) adds a duplicate run row, so summing `cost_usd` overcounts. The fix
  goes in the shared projection, not in the KPI query.
- No new tenant table; `TENANT_TABLES` and the RLS lists are unchanged.

## 3. Query

One class, `KpiRepository(url)`, with one method
`summary(tenant_id, since: datetime | None) -> dict`. It opens one
`tenant_session(tenant_id, url)` and filters `tenant_id` explicitly on every
table it touches:

- base: `Invoice ⋈ Document` where `Document.kind IS NULL` and
  (`since` is null or `Document.created_at >= since`).
- counts: received, decided, touchless, and the exception buckets (a CASE
  expression over EXISTS subqueries on `findings` and `approval_steps`).
- cycle: fetch `(decided_at, received)` for decided rows; percentiles in
  Python (SQLite has no percentile function; a window of tens of thousands of
  rows is fine).
- open: count and `min(received)`.
- three-way: count of `po_number IS NOT NULL`; held via
  `EXISTS(findings step='2.3' AND passed=false)`.
- cost: `avg` and `sum` of `pipeline_runs.cost_usd` joined on invoice id.

The only pure logic is `percentile(values, p)` and the bucket rule; both are
small functions in the repository module with direct unit tests. No separate
pure module for two functions.

## 4. API

`invoice_processing/api/kpi_routes.py`, mounted beside the other routers:

```
GET /api/kpis?days=30     viewer+; days ∈ {0,7,30,90} else 422
```

DB-only: without a database it returns 503 whose text contains
"needs a database" (same guard as `api/sheet.py`). Read-only, so no audit
event. Memoised accessor `get_kpis()` in `service.py`, reset in
`tests/conftest.py`.

Response:

```json
{"window":{"days":30,"since":"2026-09-11T00:00:00+00:00"},
 "received":120,"decided":104,"touchless":48,
 "touchless_rate":0.4,"exception_rate":0.6,
 "exceptions":{"rejected_by_rules":9,"three_way_hold":6,
               "low_confidence":40,"approval_required":15,"manual_decision":1},
 "cycle_hours":{"median":5.2,"p90":41.0,"skipped":0},
 "open":{"count":16,"oldest_hours":96.5},
 "three_way":{"po_invoices":30,"held":6,"hold_rate":0.2},
 "ai_cost":{"per_invoice_usd":0.0131,"total_usd":1.57}}
```

`since` is null when `days=0`.

## 5. UI

- The dashboard's `Tile` moves to `src/components/Tile.tsx`; both pages use it.
- New page `/kpis`: window picker (7 / 30 / 90 / All) kept in `?days=`;
  fetched once per change through `api()`, not polled. Tiles: Received,
  Touchless rate, Exception rate, Median cycle time (hint: p90, and
  "n skipped" when non-zero), Open (hint: oldest age), Three-way hold rate
  (hint: held of PO-backed), AI cost per invoice (hint: total).
- Exceptions by reason: a table of reason / count / share; each row links to
  `/invoices?reason=<bucket>`.
- `/invoices` accepts `?reason=` and filters the loaded list client-side with
  the same bucket rule. If the list row lacks a field the rule needs, the
  field is added to the row rather than a second endpoint.
- Empty window → "No invoices received in this window". 503 → the friendly
  "needs a database" message through `ApiError.friendly`.
- Sidebar entry "KPIs"; the dashboard's touchless tile links to `/kpis`.
- No chart library. Trends by day are out of scope.

## 6. Error handling

- Unreadable or inconsistent timestamps are skipped and counted, never
  silently dropped or clamped.
- A missing database refuses with 503, matching the other DB-only routes.
- Zero denominators return null, never 0%.

## 7. Testing (TDD; each test proven to fail without its change)

- Projection: `decided_at` from workflow; touchless `decided_at` set; pending
  null; re-projection leaves exactly one `pipeline_runs` row.
- `tests/test_kpi_repository.py` (DB-backed, SQLite locally, on the CI
  Postgres list): seeded invoices covering every bucket, window boundary,
  scans excluded, zero denominators → null, bad clock → skipped, cost mean
  over one run per invoice.
- `tests/test_kpi_api.py`: viewer allowed, `days` validation, 503 without DB.
- `tests/test_tenant_isolation_api.py`: cross-tenant probe and `PROBED` entry
  for `/api/kpis`; module listed in the no-tenant-in-request test.
- `tests/test_gcs_and_migrations.py`: migration round trip including backfill
  of a row that has a workflow `decided_at`.
- e2e: open `/kpis`, assert the tiles for the seeded cases, click a reason row
  and land on the filtered invoice list. Seeded cases in `scripts/e2e_api.py`
  get a `decided_at`.
- Measure before claiming: run `summary` against the manual stack's SQLite DB
  and reconcile each count by hand against the invoice list.

## Later (out of scope)

Daily trend buckets and charts; fully loaded cost; export cycle time;
per-vendor or per-approver breakdowns; backfilling touchless `decided_at`
for historical rows.
