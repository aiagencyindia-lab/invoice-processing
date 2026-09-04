# BEFORE baseline

What the application does with 300 synthetic invoices **before** any change was
made for them.

| | |
|---|---|
| Branch | `benchmark/india-gst`, cut from `main` at `260d7e9` |
| Repo state at capture | clean; all gates green (`current-test-results.md`) |
| Dataset | `benchmark/dataset/` — 300 submissions, 3 fictional Indian companies, 14 checks, reference date **2026-09-03**, generator seed 20260903 |
| Profile | `benchmark/harness/profiles/baseline.json` — only what an administrator can set through `PUT /api/settings` and `PUT /api/purchase-orders` today |
| Run command | `./.venv/bin/python -m benchmark.harness.run --run baseline --profile benchmark/harness/profiles/baseline.json --fresh` |
| Results | `report.md`, `results.csv` (4,200 rows), `results.json`, `extraction.csv` in this directory |

## The four documents here

- **`architecture.md`** — how one invoice travels the system, with file and line
  for every step. Read this first.
- **`current-validation-map.md`** — the 14 checks as implemented: inputs,
  gating, exact rejection strings, and the gap against what this dataset asks.
- **`current-test-results.md`** — every repo gate at the branch point. The
  regression floor for everything that follows.
- **`report.md`** — the measured BEFORE numbers (written by the scorer).

## What "baseline" deliberately does and does not include

**Configured, because an administrator would:** the company's legal name as
`organization_names`, its 30–46 purchase orders loaded in today's PO record
shape, duplicate checking on.

**Not configured, because no administrator can:** `tax_id_format` (fixed at
`ABN` in master data for every tenant), the tax-rate table (no INR entry), every
tolerance. These fail against an Indian corpus and the BEFORE numbers say so.
That is the measurement, not a handicap.

**`require_work_authorization` is off.** See the CHANGELOG entry of 2026-09-04
for the reasoning and the evidence.

## Honesty rules the scorer enforces

1. A check the pipeline never reached is `NOT_RUN`, never a pass. Phases stop at
   the first failure, so most checks on a rejected invoice have no verdict.
2. Where the dataset expects a failure and that check is the document's *first*
   failing check, `NOT_RUN` counts as a **false negative** — the application had
   its chance and did not catch it.
3. Where the dataset expects a failure that another failure hides by design, the
   row is excluded rather than scored either way, per the dataset's own README.
4. Every population states its denominator, and both readings are published:
   cascade-aware (what a first-failure pipeline can be held to) and all-findings
   (every check actually reached).
5. Byte-identical duplicates refused at ingest before the pipeline runs are
   recorded as check 3.1 detections with `outcome: file_hash`, so the two
   mechanisms can be told apart.
