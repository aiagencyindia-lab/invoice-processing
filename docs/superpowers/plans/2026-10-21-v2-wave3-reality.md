# Wave 3 Reality checks (FR-23) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fill the scaffold's `reality` engine, evidence-request routes, explain templates and benchmark family so an invoice's quantity, hours or seats are checked against recorded evidence and every gap is a cited finding.

**Architecture:** `trust/reality/run(ectx)` builds tenant-wide indexes once (lines, vendors, PO links, history evidence), classifies each invoice, bands Σ evidence vs Σ invoiced, writes a `reality_check` node plus `evidences` edges, and returns `FindingSpec`s; plausibility (price/volume/shutdown) runs on every invoice. `evaluate()` (scaffold) owns modes and holds. Evidence requests live in `trust_evidence_requests` through `reality/requests.RequestStore`; routes in `api/trust_reality_routes.py`.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy, pytest; Decimal for money.

**Spec:** `docs/superpowers/specs/2026-10-21-v2-wave3-reality-design.md` (+ scaffold spec, program plan U3/U4/U6, trust-layer §3.1).

## Global Constraints

- Touch only: `invoice_processing/trust/reality/**`, `invoice_processing/api/trust_reality_routes.py`, `invoice_processing/trust/explain/templates/reality.py`, `benchmark/history/faults/reality.py`, `benchmark/history/reality.py`, `tests/test_trust_reality*.py`, `tests/fixtures/trust_reality/**`, `tests/fixtures/trust_explain/REAL-*.json`, this plan.
- `ENGINE_VERSION = "reality-1"`, `RULE_VERSION = "FR-23-v1"`, `model_version="none"`.
- Money is Decimal; impact quantized to 0.01. Every finding cites the invoice, the reality node and each evidence node.
- Dedupe keys `REAL-<KIND>:<invoice natural_key>` (`:<line_number>` for price/volume). Never node ids.
- No ids or bank numbers in log `extra`. Every route gets a two-tenant probe; `tests/test_trust_reality_routes.py` exports `PROBED`.
- Only `REAL-CONTRADICTED` holds (U3), and only in `enforcing` (evaluate does it).

## Builder's calls (recorded here)

- **Tolerances are fractions**: `reality.qty_tolerance_pct 0.02` = 2 %. (Leakage divides its pct by 100; 0.02 % on integer quantities would mean "exact", which the spec's ×3 PARTIAL band contradicts.)
- **Band**: E = invoiced, F = found. No evidence node at all → NO_EVIDENCE. F ≥ E·(1−t) → CORROBORATED (over-delivery corroborates). F > 0 and E−F ≤ 3t·E → PARTIAL. Else CONTRADICTED. Evidence that exists but is excluded (late delivery, undelivered shipment) counts as present with 0.
- **Classification order**: services (WA ref or a time unit) → subscription (seat/user/licence/subscription description) → goods (PO ref, or qty > 1 with a non-time unit) → other. One kind per invoice; only that kind's lines are summed. Time units = `wa_matching.HOUR_UNITS` ∪ {day, days, md}; a day is 8 hours.
- **Unit price** for impact = Σ(qty×price)/Σqty over the summed lines.
- **Goods evidence** is per PO (Σ over every GRN/shipment fulfilling it). ponytail: no allocation across several invoices on one PO.
- **Proofs**: a fulfilled evidence request's proof turns NO_EVIDENCE into CORROBORATED (cited `document_version` node); it never overrides PARTIAL/CONTRADICTED — a clerk's upload must not lift a hold an approver must reject. The engine finds proofs through its own table (`RequestStore.proofs`), since provenance re-versions the `document_version` node and closes its edges.
- **Price-jump clause rule**: explained when a contract governing the invoice (`governed_by`) or its vendor (`governs`) has a `rate`/`escalator` clause node written after the vendor's price history began — the contract owns that price and leakage judges it.
- **REAL-SHUTDOWN is not gated**: it needs `trust_shutdown_periods`, which the eval's settings do not carry (as leakage's DUP-SPLIT).
- **Benchmark volume spikes**: the spec gates REAL-VOLUME-SPIKE but plants none; the family plants 10 so the gate measures something.

## Review Focus

1. A goods invoice on a PO with no GRN yet but a late GRN (beyond window) → CONTRADICTED, not CORROBORATED. Test in Task 2.
2. A vendor whose price history never varies (MAD 0) and one cent off → inf z must still respect `min_history`. Test in Task 3.
3. Re-running on unchanged data must not duplicate findings or edges (natural-key dedupe). Test in Task 4.
4. A foreign tenant's finding id / request id on every route → 404, never data. Task 6 probes.
5. Proof upload of bytes already stored as an invoice → 409, not a silent re-kind. Test in Task 6.

---

### Task 1: Classification and bands

**Files:** Create `invoice_processing/trust/reality/classify.py`, `invoice_processing/trust/reality/bands.py`; Test `tests/test_trust_reality.py`

**Interfaces — Produces:** `classify(invoice: dict, lines: list[dict]) -> str` (goods|services|subscription|other); `TIME_UNITS`, `hours_of(line) -> Decimal | None`, `is_seat(line) -> bool`; `bands.Check(kind, unit, expected: Decimal, found: Decimal, evidence: list[str], unit_price: Decimal | None, present: bool)`; `band(expected, found, present, tol: Decimal) -> str` (CORROBORATED|PARTIAL|CONTRADICTED|NO_EVIDENCE).

- [ ] Tests: table-driven `band` (E=100,t=.02: F=98→CORROBORATED, 97→PARTIAL, 94→PARTIAL, 93→CONTRADICTED, 0 present→CONTRADICTED, not present→NO_EVIDENCE, 120→CORROBORATED); `classify` cases (PO ref → goods; unit "hr" → services even with PO; WA ref → services; "User licence seats" → subscription; qty 1 no PO → other; qty 5 unit "kg" → goods).
- [ ] Run, fail; implement; pass; commit.

### Task 2: Kind checks (goods, services, subscriptions) + node/edges + findings

**Files:** Create `reality/goods.py`, `reality/services.py`, `reality/subscriptions.py`; Modify `reality/__init__.py`; Test `tests/test_trust_reality.py`

**Interfaces — Produces:** `_Run(ectx)` in `__init__` with cached indexes (`lines`, `vendor`, `po_of`, `fulfils`, `by_vendor(kind)`, `canon`), `provider(capability, *args) -> ProviderResult`; each kind module `check(r, invoice, lines) -> Check | None`; `run(ectx) -> list[FindingSpec]`.

- [ ] Tests: GRN short beyond 3t → CONTRADICTED with impact `(E−F)×price` and citations {invoice, reality node, grn}; GRN late beyond `evidence_window_days` → CONTRADICTED found 0; undelivered shipment ignored, delivered counted; v1 grn summed from `grn_line.qty_received`; services timesheet+ticket hours in the invoice month (no PO) vs line hours; `service_period_*` overrides the month; latest seat_count ≤ period end; no evidence → provider called once, `provider_evidence` cited, NO_EVIDENCE (no impact); provider answering `{"quantity": n}` counts; CORROBORATED writes the node with attrs `{result, kind, expected, found, unit, evidence, window_days, engine_version}` and `evidences` edges, no finding; contract document (invoice_node None) → []; logs carry no ids.
- [ ] Run, fail; implement; pass; commit.

### Task 3: Plausibility

**Files:** Create `reality/plausibility.py`; Test `tests/test_trust_reality.py`

**Interfaces — Produces:** `plausibility.findings(r, invoice, lines) -> list[FindingSpec]` using `payment.signals.robust_z`.

- [ ] Tests: price 3× a constant 5-point history → PRICE-JUMP impact `(p−median)×qty`, dedupe `…:<line_number>`; 4 points → nothing; same with a governing contract whose rate clause postdates the history start → nothing; qty spike → VOLUME-SPIKE impact `(q−median)×price`; invoice date inside a `trust_shutdown_periods` pair → SHUTDOWN impact = amount; outside → nothing.
- [ ] Run, fail; implement; pass; commit.

### Task 4: Evaluate integration and evidence-request store

**Files:** Create `reality/requests.py`; Test `tests/test_trust_reality.py`

**Interfaces — Produces:** `RequestStore(url)`: `create(ctx, finding_id, *, owner, due_date, note, requested_by) -> dict`, `get(tenant, id)`, `list(tenant, *, status=None, overdue=False, today) -> list[dict]`, `fulfil(ctx, id, document_id, at) -> dict`, `cancel(ctx, id) -> dict`, `proofs(tenant) -> dict[transaction_node, list[document_id]]`. Row dict: `{id, finding_id, owner, due_date, note, status, proof_document_id, requested_by, fulfilled_at, created_at, overdue}`.

- [ ] Tests: through `evaluate` with mode enforcing → REAL-CONTRADICTED is a hold, advisory → not; re-run on same data → no new finding, same node; clean re-run after evidence arrives releases the hold; store lifecycle (create→list open→overdue when due < today→fulfil→proofs maps the invoice; cancel only from open); a fulfilled proof makes a NO_EVIDENCE invoice CORROBORATED and a CONTRADICTED one stay.
- [ ] Run, fail; implement; pass; commit.

### Task 5: Explain templates + fixtures

**Files:** Modify `invoice_processing/trust/explain/templates/reality.py`; Create `tests/fixtures/trust_explain/REAL-{CONTRADICTED,PARTIAL,NO-EVIDENCE,PRICE-JUMP,VOLUME-SPIKE,SHUTDOWN}.json`; Test `tests/test_trust_reality.py`

- [ ] Tests: the existing `tests/test_trust_explain.py` un-skips the six codes; plus own test that CONTRADICTED's `what_happened` names expected vs found with the unit, `would_change` names GRN/POD for goods, timesheet for services, seat report for subscriptions, `next_action` mentions an evidence request.
- [ ] Run, fail; implement; pass; commit.

### Task 6: Routes

**Files:** Modify `invoice_processing/api/trust_reality_routes.py`; Create `tests/test_trust_reality_routes.py`

**Interfaces — Consumes:** `trust_routes._findings/_graph/_role_for`, `RequestStore`, `service.get_documents/get_blobs/get_queue/get_audit`, `worker.runner.enqueue_trust_evaluate`.
Routes: `create_evidence_request` (201; finding detected→triaged→needs_vendor_input), `list_evidence_requests` (viewer; `status`, `overdue`), `fulfil_evidence_request` (multipart `file`; Document(kind="proof"), `document_version` node `documents:{doc}:{sha}` + `evidences` edge to the invoice, request fulfilled, enqueue evaluate for proof + invoice document), new `cancel_evidence_request` `POST /api/trust/evidence-requests/{request_id}/cancel` (clerk).

- [ ] Tests: lifecycle; viewer 403 on POSTs; bad status 400; finding already decided (accepted) → 409; proof bytes stored and readable via the blob store, node + edge present, two jobs enqueued; duplicate bytes of a non-proof → 409; cancel twice → 409; `PROBED` with two-tenant probes (globex never sees or touches acme's rows).
- [ ] Run, fail; implement; pass; commit.

### Task 7: Benchmark family + measure

**Files:** Create `benchmark/history/faults/reality.py`, `benchmark/history/reality.py`; Test `tests/test_trust_reality.py`

- `inject`: per base vendor constant goods price/qty (GRN `quantity` rewritten to it); 10 % of GRNs become delivered shipments; 30 short GRNs, 20 price jumps (3×), 10 volume spikes (one per vendor, GRN matches); 120 services invoices (vendor-month unique; 75 % with timesheets/tickets whose `project` = doc number; 15 over-billed by ~50 %); 40 subscription invoices on distinct vendors (80 % with a seat_count whose `product` = doc number; 10 overruns ~40 %). Faults carry `invoiced_quantity`/`unit_price`/`kind`; uncovered services/subscriptions are `REAL-NO-EVIDENCE` entries (unpriced).
- `seed_graph`: plants one `invoice_line` per planted invoice from the CSVs + manifest.
- `GATES`: `REAL-CONTRADICTED {.9,.8,.1}`, `REAL-PRICE-JUMP {.85,.7,.1}`, `REAL-VOLUME-SPIKE {.8,.7,.15}`. `measure()` → `reality_coverage_goods ≥ .70`, `reality_coverage_services ≥ .50`.
- [ ] Tests: inject is deterministic and plants the counts with exact Decimal impacts; then `python -m benchmark.history.eval --seed 7` passes every gate.
- [ ] Commit.

### Task 8: Verify, review, PR

ruff check/format; full suite; Postgres glob; eval seed 7; fable review; one fix pass; progress.md; push; PR.
