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
6. **Approach: one typed-column row per invoice in the window, aggregated in
   Python, plus one new column** (`invoices.decided_at`). No artifact JSON is
   loaded and there is no rollup table.

## 1. Definitions

All numbers cover invoices whose **document was received in the window**.
`days` ∈ {0, 7, 30, 90}; 0 = all time; default 30. Scanned POs/GRNs
(`documents.kind` set to anything other than `invoice`) are excluded, the same
filter `SheetRepository._base`
uses.

Per invoice:

- **Received**: `documents.created_at` (already exposed as `received_at`),
  falling back to `invoices.created_at` when the case has no document row
  (seeded and legacy cases). Left join, never inner.
- **Decided**: new column `invoices.decided_at`.
  - Human decision → the workflow artifact's `decided_at` (written by
    `_record_workflow` in `api/service.py`).
  - Touchless (routing AUTO, machine verdict accept, no failed step-2.3
    finding, no approval chain) → the time the case was first projected.
  - Null while the invoice waits on a person.
  The worker routes the approval chain *after* projecting the case, so a
  touchless `decided_at` can exist before a chain does. The query therefore
  treats an invoice as decided only when `decided_at` is set and either a
  person decided it (`human_decision` not null) or it has no approval chain.
- **Touchless**: decided (as defined above) and `human_decision` is null.
- **Exception**: not touchless. One bucket per invoice, first match wins:
  1. `rejected_by_rules` — `case_projection.machine_verdict` is reject.
  2. `three_way_hold` — a `findings` row with `step='2.3'` and `passed=false`.
  3. `low_confidence` — `routing='REVIEW'`.
  4. `approval_required` — has `approval_steps` rows (pending or decided).
  5. `manual_decision` — none of the above, but a person decided it (for
     example an AUTO invoice approved from the workbench).
  6. `other` — an exception matching none of the above (for example an invoice
     projected before `decided_at` existed, see §2: touchless rows projected
     before this change). Counted so the buckets always sum
     to the exceptions; a non-zero `other` is a data-quality signal, not a
     category to design around.
  The buckets are exhaustive over exceptions and say why a person was needed,
  not the current state: an invoice that was REVIEW and is now approved still
  counts under `low_confidence`.
- **Cycle time**: `decided_at − received` in hours; median and p90 over
  decided invoices (as defined above) (nearest-rank). Rows with `decided_at < received` or a null
  received time are excluded and counted in `skipped`, so a bad clock can
  never make the median look better (fail closed).
- **Open**: not decided under the rule above (`decided_at` null, or set with
  no `human_decision` while an approval chain exists) → count and the age of
  the oldest, in hours.
- **Three-way hold rate**: invoices with a failed 2.3 finding ÷ invoices with
  a non-null `po_number`.
- **AI cost per invoice**: mean of `pipeline_runs.cost_usd` over invoices that
  have a run (one row per invoice after the fix in §2), with the total.
  `per_invoice_usd` is null when no invoice in the window has a run.
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
`~exists(documents.kind not null and != 'invoice')` the sheet uses (the same
filter as §1). The window
filter is on received. The numbers — counts, buckets, nearest-rank
percentiles, open age, hold rate, cost — are then computed in Python over
those rows. No artifact JSON is loaded.

ponytail: one row per invoice in the window; aggregate in SQL if a window ever
exceeds ~100k invoices.

The bucket rule is one pure function `exception_bucket(decision, held,
routing, chain, human_decision) -> str` and the percentile is one pure
function `percentile(values, p)`, both in the repository module and
unit-tested directly.

`decision` is the raw `invoices.decision` column (e.g. `REJECT`); it is
classified with the same rule as `case_projection.machine_verdict`. `other` is
the fallthrough when no earlier bucket matches.

## 4. API

`invoice_processing/api/kpi_routes.py`, mounted beside the other routers:

```
GET /api/kpis?days=30     viewer+; days ∈ {0,7,30,90} else 422
```

DB-only: without a database both routes return 503 whose text contains
"needs a database" (same guard as `api/sheet.py`). Read-only, so no audit
event and no memoised accessor: the repository is constructed per request,
as the sheet's is.

Second route, for the drill-down:

```
GET /api/kpis/cases?reason=<bucket>&days=30   viewer+
→ {"case_ids": ["..."], "truncated": false}
```

`reason` must be one of the six buckets, else 422.

Both routes validate `days` the same way (default 30; anything outside {0, 7, 30,
90} is 422).

Response:

```json
{"window":{"days":30,"since":"2026-09-11T00:00:00+00:00"},
 "received":120,"decided":104,"touchless":48,
 "touchless_rate":0.4,"exception_rate":0.6,
 "exceptions":{"rejected_by_rules":9,"three_way_hold":6,
               "low_confidence":40,"approval_required":15,"manual_decision":1,"other":0},
 "cycle_hours":{"median":5.2,"p90":41.0,"skipped":0},
 "open":{"count":16,"oldest_hours":96.5},
 "three_way":{"po_invoices":30,"held":6,"hold_rate":0.2},
 "ai_cost":{"per_invoice_usd":0.0131,"total_usd":1.57}}
```

The six bucket keys are always present, with 0 when empty.

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
- `/invoices` accepts `?reason=<bucket>&days=N` (the KPI page always passes
  `days`). When present it fetches `/api/kpis/cases` once and filters the
  invoice list — which holds every invoice, decided or not — to the returned
  case ids. A banner reads "Showing N invoices that needed a person: <reason>"
  where N is the rows shown, adds "(newest 500)" when `truncated`, and links
  "Show all" back to `/invoices`. If the call fails (for example an unknown
  reason) the banner shows the API's message and the full list is shown.
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
  for `/api/kpis` and `/api/kpis/cases`; module listed in the
  no-tenant-in-request test.
- `tests/test_gcs_and_migrations.py`: migration round trip including backfill
  of a row that has a workflow `decided_at`.
- e2e: open `/kpis`, assert the tiles for the seeded cases, click a reason row
  and land on the filtered invoice list. Seeded cases in `scripts/e2e_api.py`
  get a `decided_at`.
- Measure before claiming: run `summary` against the manual stack's SQLite DB
  and reconcile each count by hand against the invoice list.
- Repository: an invoice with no documents row is counted via
  invoices.created_at; an invoice with decided_at set, an approval chain and
  no human_decision is open; every exception lands in exactly one of the six
  buckets; cases() returns newest-first, caps at 500 and sets truncated.
- API: /api/kpis/cases rejects an unknown reason and a bad days with 422,
  returns 503 without a database, and is open to viewers.

## Later (out of scope)

Daily trend buckets and charts; fully loaded cost; export cycle time;
per-vendor or per-approver breakdowns; backfilling touchless `decided_at`
for historical rows.
