# Wave 3 — Reality checks (FR-23) design (`v2/w3-reality`)

**Program plan:** `docs/superpowers/plans/2026-10-21-v2-wave3-program.md`. **Scaffold contracts:** `docs/superpowers/specs/2026-10-21-v2-wave3-scaffold-design.md`. User decisions: U3 (only CONTRADICTED holds), U4 (evidence requests are an internal tracker), U6 (file-imported evidence only).

**Owns:** `invoice_processing/trust/reality/**` (`__init__`, `classify`, `goods`, `services`, `subscriptions`, `plausibility`, `requests`), `invoice_processing/api/trust_reality_routes.py` (fills the stub), `invoice_processing/trust/explain/templates/reality.py` (fills), `benchmark/history/faults/reality.py`, `benchmark/history/reality.py`, `tests/test_trust_reality*.py`, `tests/fixtures/trust_reality/**`, its plan doc. Nothing else.

## Engine (`run(ectx) -> list[FindingSpec]`)

**Inputs** (graph only; providers as a fallback): the invoice node and its `invoice_line` nodes (`quantity`, `unit_price`, `unit`, `description`); `references` → po → `fulfils` ← grn / shipment / ticket / timesheet; `issued_by` vendor; history `ticket` / `timesheet` / `seat_count` nodes matched by vendor + period (`service_period_start/end`, else the invoice month) when no PO link exists. Providers `tickets.search`, `timesheets.hours`, `logistics.track`, `usage.seats` are called only when the graph holds nothing; the fake answers `{"result": "unknown"}` = no evidence; every call leaves a `provider_evidence` node that is cited.

**Classification** (`classify.py`, deterministic, no model): `goods` when the invoice references a PO or any line has `quantity > 1` with a non-time unit; `services` when `work_authorization_reference` is set or a line unit ∈ {hour, hr, hrs, day, md}; `subscription` when a line description matches `\b(seat|user|licen[cs]e|subscription)s?\b`; else `other` (plausibility only).

**Bands**: Σ evidence vs Σ invoiced. Goods: grn `qty_received` (then `quantity`) + shipment `quantity` with `status == "delivered"`; a delivery dated after the invoice by more than `reality.evidence_window_days` does not count. Services: timesheet + ticket `hours` vs the invoice's labour hours (the `wa_matching.invoiced_hours()` rule). Subscriptions: the latest `seat_count.quantity` dated ≤ period end vs invoiced quantity. Within tolerance (`reality.qty_tolerance_pct` / `hours_tolerance_pct` / `seats_tolerance_pct`) → `CORROBORATED`; evidence > 0 but short within tolerance ×3 → `PARTIAL`; short beyond that → `CONTRADICTED`; no evidence → `NO_EVIDENCE`.

**Plausibility** (`plausibility.py`, reuses `trust.payment.signals.robust_z`; needs ≥ `reality.min_history` points): `REAL-PRICE-JUMP` when a line's unit price vs the vendor's history for the normalised description has z > `reality.price_jump_zscore` and the invoice is not `governed_by` a contract whose rate clause changed in the period; `REAL-VOLUME-SPIKE` when quantity z > `reality.volume_zscore`; `REAL-SHUTDOWN` when the invoice date or service period overlaps a `trust_shutdown_periods` entry.

**Outputs**: a `reality_check` node (natural key `reality:{invoice natural_key}`, attrs `{result, kind, expected, found, unit, evidence: [node ids], window_days, engine_version}`) and `evidences` edges from each evidence node to the invoice; on `CORROBORATED` no finding. Findings cite the invoice node, the reality node and each evidence node; impact is Decimal:

| Code | Hold | Dimension | Impact |
|---|---|---|---|
| `REAL-CONTRADICTED` | yes | reality | `(invoiced − found) × unit_price` |
| `REAL-PARTIAL` | no | reality | same |
| `REAL-NO-EVIDENCE` | no | reality | none |
| `REAL-PRICE-JUMP` | no | price | `(unit_price − median) × qty` |
| `REAL-VOLUME-SPIKE` | no | consistency | `(qty − median) × unit_price` |
| `REAL-SHUTDOWN` | no | reality | invoice amount |

Dedupe keys: `REAL-<KIND>:<invoice natural_key>` (plus the line number for price/volume). `ENGINE_VERSION = "reality-1"`, `RULE_VERSION = "FR-23-v1"`.

## Evidence requests (`requests.py` + routes)

Table `trust_evidence_requests` (scaffold). Routes (clerk unless noted): `POST /api/trust/findings/{finding_id}/evidence-request {owner, due_date, note}` → 201 row and the finding moved to `needs_vendor_input` (via `FindingStore.transition`, using `trust_routes._role_for`); `GET /api/trust/evidence-requests?status&overdue=true` (viewer; overdue computed on read); `POST /api/trust/evidence-requests/{id}/proof` multipart → stores a `Document(kind="proof")` through the blob store the way `trust_routes._store_contract` does, writes a `document_version` node with an `evidences` edge to the invoice, marks the request `fulfilled`, enqueues `trust.evaluate` for the proof document and for the invoice's document; `POST .../cancel` (clerk). Every route has a two-tenant probe; `tests/test_trust_reality_routes.py` exports `PROBED`.

## Explain templates

`explain/templates/reality.py` registers the six codes: `what_happened` from the `reality_check` attrs (expected vs found with the unit), `would_change` names the missing document (GRN / POD / timesheet / seat report), `next_action` is "raise an evidence request". `check()` must pass for every code (citation coverage stays 1.0).

## Benchmark

`benchmark/history/faults/reality.py`: `inject` adds `ticket`, `timesheet`, `shipment`, `seat_count` rows for ≥ 70 % of PO-backed and ≥ 50 % of services invoices; plants 30 short deliveries, 15 over-billed hours, 10 seat overruns, 20 price jumps (3× with no clause), 5 shutdown invoices; `seed_graph` plants the invoice lines the generator lacks. `GATES`: `REAL-CONTRADICTED {precision .9, recall .8, impact_error .1}`, `REAL-PRICE-JUMP {.85, .7, .1}`, `REAL-VOLUME-SPIKE {.8, .7, .15}`. `benchmark/history/reality.py::measure()` → `reality_coverage_goods ≥ 0.70`, `reality_coverage_services ≥ 0.50` (share of invoices whose result ∈ {CORROBORATED, CONTRADICTED}). Reviewer agreement (AC-23 at a partner) stays in PENDING.

## Tests

Bands table-driven; classification; plausibility with and without a clause change; node written and no finding on CORROBORATED; hold only in `enforcing` (through `evaluate`); evidence-request lifecycle incl. proof bytes and re-evaluation enqueue; routes with `PROBED`.

**Done:** gates green in `python -m benchmark.history.eval --seed 7`; explain `check()` passes for all six codes; isolation probes; ruff; suite on SQLite and Postgres.
