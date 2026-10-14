# v2.0 Transaction Trust Layer — program design and Wave 0

Status: Wave 0 implemented in PR #52 (2026-10-15); Waves 1–4 pending.

Date: 2026-10-14. Source: `PRD_v2.0_Transaction_Trust_Layer.md` (2.0-draft-1,
12 Oct 2026; kept outside the repo). Classification: architectural, decomposed.

This spec covers two things:

1. **The program**: how the whole PRD is built: waves, the contracts every
   engine obeys, gates, process.
2. **Wave 0 in full**: the shared foundation every later engine stands on.

Waves 1–4 each get a short spec per sub-project when the wave starts,
written against the contracts here.

## 1. Decisions taken in brainstorming

| Question | Decision |
|---|---|
| Scope | The whole PRD, in its order: V2-A → V2-B → V2-C (V2-C built, shipped dark). |
| Execution | Foundation first, sequential; then independent sub-projects in parallel worktrees, each with its own spec → plan → review → PR. |
| External providers | Every provider behind an adapter (PRD ADR-15) with a deterministic fake; real providers plug in when an account exists. Free public ones (VIES, ABN Lookup) go real in their wave. Nothing waits on sign-ups. |
| Infrastructure | Stay on Railway in the existing single app. Engines are Python modules plus worker job types. Postgres is the graph store (PRD ADR-12). SQL views stand in for BigQuery marts. Evidence files are hashed on the volume with append-only records. Split into services only when a measured limit is hit. |
| Done-in-code vs. partner gates | A phase is done in code when its benchmark gates (PRD §14 fault families on our generator) pass in CI. Gates that need design partners (AC-20 partner histories, "≥0.5% of spend", contingency invoice paid, AC-23 coverage at partners) stay open in `docs/PENDING.md`. |

### What v1 actually provides (the PRD assumes all of it)

Present: invoices + lines, v1 findings, vendor master with dual-control bank
changes, POs/GRNs and three-way match, duplicates (exact/normalised/fuzzy),
approvals, GL coding, audit trail, rules book, RLS tenancy, India tax ids,
Gemini pipeline, Xero connector.

Missing: Tally/Zoho/NetSuite/SAP B1 connectors, partner/CA-firm console,
BigQuery, mobile surface, a separate critic service, Cloud Run. v2 works
around these: history arrives as files (Wave 0); partner mode is part of
the Trust Audit workspace (Wave 2); the rest stay out of scope until a
wave needs them.

## 2. Waves

| Wave | Sub-projects (parallel within a wave) | PRD | Phase |
|---|---|---|---|
| **0 Foundation** (sequential) | History import · Evidence Graph (nodes, edges, lineage, time-travel, entity resolution) · findings v2 · `decide()` · provider adapter base · benchmark v2 harness | FR-19, FR-20.7, FR-27 (files), §12.4, §14 | V2-A |
| **1 Engines** | Leakage (contract clauses → line compliance, near/split/cross-entity duplicates, statement reconciliation, missed discounts, renewal radar, claim pack) · Payment Trust (ownership verification, behavioural baseline, BEC signals, pre-payment gate, replay harness) · Provenance v1 (hashing, metadata, channel, template consistency, tamper after approval, provenance ledger) · GST/ITC v1 | FR-20, FR-21, FR-22 (part), FR-26 | V2-A |
| **2 Surfaces** | Trust Audit workspace (engagement, batch runs, sampling, report, recovery and contingency ledger, partner mode) · Causal "Why" engine (citation-enforced, counterfactual, auditor export) · findings UI (triage queue, Payment Trust queue, "Why?" button, evidence viewer, clause editor) | FR-29, FR-24, FR-30 (part), §8 | V2-A → V2-B |
| **3 Always-on** | Reality checks (goods, services, subscriptions, plausibility, evidence requests) · Provenance v2 (KYC and delivery proofs, forensic ensemble) · Trust Score + vendor profile + posture dashboards · v2 public API + webhooks · shadow/advisory/enforcing progression · remaining connectors as adapters | FR-23, FR-22 (rest), FR-25, FR-30, §9, FR-27 | V2-B |
| **4 Agent control plane** (dark) | Registry, mandate ledger, policy engine, structural and behavioural detection, agent-spend ledger, simulation harness | FR-28 | V2-C |

Phase exits: V2-A = Waves 0–2 merged with gates green; V2-B = Wave 3;
V2-C = Wave 4 (release stays gated per PRD §5).

## 3. Architecture

v2 is a package in the existing app:

```
invoice_processing/trust/
  graph/        nodes, edges, lineage, time-travel, entity resolution   (Wave 0)
  findings/     finding model, lifecycle, ₹ ledger, decisions            (Wave 0)
  history/      file import of ERP/bank/vendor history, column mappings  (Wave 0)
  decide.py     decide(state, questions) -> typed answers                 (Wave 0)
  providers/    adapter interfaces, registry, fakes                       (Wave 0)
  leakage/ payment/ provenance/ gst_itc/                                  (Wave 1)
  audit/ explain/                                                         (Wave 2)
  reality/ score/                                                         (Wave 3)
  agents/                                                                 (Wave 4)
```

Engines run as new job types on the existing worker queue. No new
services. The v1 pipeline gains one hook: after an invoice is processed it
enqueues `trust.evaluate(invoice)`, which does nothing while every engine is
`off`.

### 3.1 Contracts every engine obeys

1. **Read the graph, write findings.** An engine never writes another
   engine's tables. Its outputs are v2 findings and graph edges (with the
   four mandatory edge fields, §4.2).
2. **No finding without a citation.** Every finding cites at least one
   graph node; the database enforces it. ₹ impact comes from deterministic
   arithmetic; the model never computes money (PRD §12.2).
3. **Versions on everything.** Every finding records engine, rule and model
   version.
4. **Fail closed for holds, open for insights** (ADR-13). A payment-gating
   engine that cannot decide holds; an insight engine that cannot decide
   skips and logs the gap.
5. **Per-tenant engine mode** `off → shadow → advisory → enforcing`,
   default `off`, stored in the existing tenant settings. Only `enforcing`
   places holds.
6. **One model door.** Engines reach Gemini only through `decide()`.
7. **One provider door.** Engines reach external systems only through
   `providers/` adapters.
8. **v1 behaviour is unchanged** (the only v1 code change is the
   `trust.evaluate` hook above). v1 `findings` stays the per-invoice validation
   record; the sheet, KPIs and approval gates keep reading it. v2 findings
   live in their own tables.

## 4. Wave 0 design

### 4.1 History import (FR-19.3; file half of FR-27)

- Upload CSV or Excel, one file per kind: `vendor`, `ap_invoice`,
  `payment`, `po`, `grn`, `bank_line`, `vendor_statement_line`. Contract
  PDFs are stored as documents of kind `contract` (clauses are Wave 1).
- **Column mapping:** `decide()` proposes a header → field mapping; a human
  confirms; the mapping is saved per tenant per source name. Tally, Zoho and
  other exports become saved mappings, not code.
- Rows land in `history_records` (§4.3). One file = one database
  transaction: a file either lands whole (minus reported bad rows) or not at
  all. Bad rows go to an error report with the row number and reason; the
  run is `partial`. Nothing is silently dropped.
- Re-importing a file with the same SHA-256 is a no-op. The file itself is
  stored as hashed evidence.

### 4.2 Evidence Graph (FR-19)

- **Nodes** of the FR-19.1 kinds. A node backed by an existing row points
  at it (`ref_table`, `ref_id`): an invoice node references `invoices.id`,
  and no data is copied. Other nodes carry `attrs` (JSON, masked: bank
  numbers and personal data only in their protected source records, via
  `masking.py`).
- **Edges** carry, mandatorily: `source_system`, `observed_at`,
  `provenance` ∈ {`document`, `system_record`, `human_assertion`,
  `model_inference`}, `confidence` ∈ [0, 1].
- **Time-travel (FR-19.5):** rows are never updated in place. A change
  closes the current row (`valid_to`) and inserts a new one; "as of D"
  filters `valid_from ≤ D < valid_to`.
- **Graph builder:** worker job `trust.graph.sync`, idempotent, projects v1
  tables (invoices, lines, vendors, bank changes, POs, GRNs, approvals,
  audit events) and history records into nodes and edges. Full run after an
  import; incremental after each processed invoice.
- **Canonical accessor:** `transactions(tenant, as_of=None)` yields one
  `Transaction` shape whether the source is a live invoice or a history
  row. Engines read this, never v1 or history tables.
- **Lineage (FR-19.6):** `lineage(node)` returns the chain Contract → PO →
  GRN/Delivery → Invoice → Approval → Payment → GL, each link `found`,
  `no_evidence` or `contradicts`, by recursive query.
- **Entity resolution (FR-19.4):** deterministic keys first: GSTIN, PAN,
  ABN, bank account, normalised name, email domain. Score ≥ 0.95 links
  automatically; 0.7–0.95 is proposed to a human with a `decide()`
  recommendation; < 0.7 never links. A link joins two nodes in
  `entity_links` and never deletes either; reversal sets `reversed_at` and
  is audited. No embeddings or pgvector in Wave 0.

### 4.3 Tables (all tenant-scoped, RLS forced)

| Table | Key columns |
|---|---|
| `graph_nodes` | `kind`, `ref_table`, `ref_id`, `attrs`, `natural_keys`, `valid_from`, `valid_to` |
| `graph_edges` | `src`, `dst`, `kind`, `source_system`, `observed_at`, `provenance`, `confidence`, `valid_from`, `valid_to` |
| `entity_links` | `node_a`, `node_b`, `score`, `method`, `status` (auto/proposed/accepted/rejected), `decided_by`, `reversed_at` |
| `history_records` | `import_id`, `kind`, `external_id`, `fields` (JSON), `amount` (Numeric 18,4), `currency`, `record_date`, `vendor_key`, `doc_number` |
| `trust_history_imports` | `source_name`, `kind`, `file_sha256`, `mapping`, `status`, `rows_ok`, `rows_bad`, `errors` |
| `trust_findings` | `type`, `transaction_node`, `status`, `impact` (Numeric 18,4), `currency`, `confidence`, `owner_id`, `engine_version`, `rule_version`, `model_version`, `dedupe_key` (unique per tenant) |
| `trust_finding_citations` | `finding_id`, `node_id`, `excerpt` |
| `trust_finding_events` | `finding_id`, `from_status`, `to_status`, `amount` (₹ at this stage), `actor`, `reason` |

Money is `Numeric(18,4)`, as in v1. Status changes go through one function
that writes the event row and an `audit_events` row; new event names map in
`repositories._ENTITY_TYPES`.

### 4.4 Findings v2 (FR-20.7)

- Lifecycle: `detected → triaged → accepted | rejected | needs_vendor_input
  → claimed → recovered | prevented | written_off`. Illegal transitions are
  refused (409). Rejection requires a reason; reasons are kept for the
  learning loop.
- Re-running an engine upserts by `dedupe_key`: a still-true finding is
  refreshed, never duplicated; a finding a human has decided is never
  reopened by an engine.
- Finding types are namespaced strings registered by each engine
  (`LEAK-RATE`, `PAY-UNVERIFIED`, `DUP-SPLIT`, …); Wave 0 ships the registry
  and no types of its own beyond the benchmark's.

### 4.5 `decide(state, questions)` (PRD §12.4)

- Questions are typed: `choice(options)`, `score(0..1)`, `yes_no`. Answers
  are typed, with a probability, the model version and `fallback: bool`.
- Implementation: Gemini structured output through the existing
  `gemini.py` and `llm_guard.py`. Each question declares its fallback
  answer; when the model is unavailable or returns invalid output, the
  fallback is returned and marked. The caller applies fail-closed or
  fail-open per §3.1.4.
- Every call is logged with the question ids, not the state contents.

### 4.6 Providers (ADR-15)

- One interface per capability: bank account verification, GSTIN lookup,
  company registry (MCA), ABN, VAT (VIES), mailbox, tickets, timesheets,
  logistics.
- A registry chooses fake or real per tenant and capability. Every call
  stores its raw response as hashed evidence; an error becomes an
  `unavailable` evidence record, never a pass.
- Fakes are deterministic and scripted by test fixtures. Wave 0 ships the
  interfaces, registry, evidence capture and the fake framework; each wave
  adds the adapters it needs.

### 4.7 Benchmark v2 harness (PRD §14)

- The dataset generator gains a tenant-history mode: a synthetic company
  with vendors, contracts, POs, GRNs, invoices, payments and statements over
  12–24 months, plus a fault manifest (ground truth and expected ₹).
- An eval runner scores each finding type: precision, recall, ₹ error,
  against per-type gates; it runs in CI next to the existing 9 gates.
- Wave 0 ships the harness and one family: **import round-trip**: every
  injected record is imported and linked (vendor, PO line, GRN) at the
  AC-19 coverage levels. Each engine adds its families and gates in its
  wave.

### 4.8 What Wave 0 does not do

No new UI (import is API + CLI; the upload screen comes with Wave 2's
workspace), no finding types beyond the benchmark, no holds, no real
providers, no embeddings.

## 5. Errors, security, testing, rollout

- **Errors:** import per §4.1; graph sync re-runnable; `decide()` per §4.5;
  provider errors per §4.6.
- **Security:** every new table in `TENANT_TABLES` and the migration RLS
  list, with a forced-RLS test as a NOBYPASSRLS role; every new route a
  cross-tenant probe in `tests/test_tenant_isolation_api.py`; no route takes
  `tenant_id`; masked attrs in the graph; uploaded files hashed.
- **Testing:** test-first. Pure functions for matching, lineage gaps and
  money arithmetic. Postgres-only tests on the CI Postgres list. Each wave
  merges only with its benchmark gates green.
- **Rollout:** engines default `off` per tenant. Wave 0 changes nothing a
  user sees. Production stays paused until the operator resumes it; v2 work
  does not need it running.

## 6. Process per wave

1. One short spec per sub-project, against §3.1, approved together.
2. One plan per sub-project.
3. Implementers in parallel worktrees (Wave 0: one at a time), each with a
   whole-branch review on the `fable` model, one fix pass (minors
   included), verification, its own PR.
4. Merge only on the user's word, CI green, `--match-head-commit`.
5. A wave ends when all its PRs are merged and its gates pass; partner
   gates are added to `docs/PENDING.md`.

## 7. Open questions carried from the PRD

PRD §21 questions 1–8 stay open; none blocks Wave 0. Contingency terms
(Q1) are needed by Wave 2; graph-store limits (Q4) are measured during
Wave 3; agent priorities (Q6) by Wave 4.
