# v2 Wave 1 — Four Engines in Parallel: Program Plan

**Spec it implements:** `docs/superpowers/specs/2026-10-14-v2-trust-layer-design.md` §2 (Wave 1 row), §3.1 (the eight engine contracts), §6 (per-wave process).
**Builds on:** Wave 0, branch `v2/wave0-foundation` (PR #52, HEAD `5ee50a8`), not yet merged.

## Context

Wave 0 delivered the foundation: the Evidence Graph, history import, findings v2, `decide()`, provider adapters, two worker jobs and the benchmark v2 harness. Wave 1 adds the four V2-A engines — **Leakage (FR-20), Payment Trust (FR-21), Provenance v1 (FR-22 part), GST/ITC v1 (FR-26)** — and the user chose to build **all four at once** in separate worktrees. That choice is safe only if the four branches never touch the same file, so this plan has two halves:

1. **A sequential scaffold** (one small PR, done by me first) that adds every shared extension point the engines need, so each engine branch touches only its own package, tests, fault module and docs.
2. **Four engine sub-projects** run in parallel, each with its own short spec (below, approved together with this plan), its own detailed plan, its own worktree, implementer, `fable` review, fix pass and PR.

"Go ahead" was not the explicit "merge" the process requires, so PR #52 stays open. The scaffold branches off `v2/wave0-foundation` and the engine worktrees off the scaffold, so nothing here waits on a merge; each PR targets `main` and needs your "merge" in turn (six PRs in total).

## Decisions (rulings; correct me if wrong)

| # | Decision | Why | Cost if wrong |
|---|---|---|---|
| 1 | **No Wave 1 migration.** Clauses, provenance records and verification evidence are graph nodes (`contract`, `contract_clause`, `document_version`, `provider_evidence`); hold state is "an open finding of a hold type". | The citation trigger only accepts graph-node ids, so anything an engine cites must be a node anyway; nodes get time-travel, RLS and masking free. One Alembic head stays one head. | PRD §10 names a `document_provenance` table; if a Wave 2 audit pack needs columns, add a table then (graph → table is a projection). |
| 2 | **Engines register by convention**: `invoice_processing/trust/<name>/__init__.py` exposes `run(ectx) -> list[FindingSpec]`; `evaluate.py` imports `trust.<name>` for every name in `modes.ENGINES` and skips a missing one. | No shared registry file for four branches to edit. | None; a typo in a package name shows up as "engine skipped" in logs and a failing engine test. |
| 3 | **Mode semantics are generic**, decided once in `evaluate.py`: `shadow` = findings written with `details.mode="shadow"`; `advisory` = written and surfaced; `enforcing` = hold-type findings gate approval and export. Engines are mode-agnostic. | Spec §3.1.5 in one place. | None. |
| 4 | **One tenant setting `trust_thresholds`** (dict), all names declared in the scaffold's `trust/thresholds.py`. | Engines read `ectx.threshold("leakage.dup_window_days")`; `tenant_settings.py` is edited once. | A threshold an engine invents later needs a scaffold follow-up. |
| 5 | **The payment hold gate is generic** (`trust/gate.py: payment_hold(tenant_id, case_id)`) and wired by the scaffold into `approval_flow.decide` (chain-completing step, beside `over_billed`) and `service.export_invoice`. Admin override with a reason, like `OverBilled`. | The two v1 files are edited once; engines only write findings. Fail-closed for hold types in `enforcing`; fail-open when nothing was evaluated yet (spec §3.1.4 is about engine failure, not timing). | An invoice approved before its async evaluation ran slips through; Wave 2 adds an "evaluated-at" marker. Recorded in PENDING. |
| 6 | **Contracts upload through a trust route** (`POST /api/trust/contracts`), not the v1 supporting-document flow. | The PO/GRN flow hard-codes two kinds across six files and the frontend. | A Wave 2 UI lists contracts from the trust route, not the supporting-documents page. |
| 7 | **Engine failure contract**: insight engines log and continue; an engine that owns hold types, in `enforcing`, writes `TRUST-ENGINE-FAILED` (hold) and a later clean run retires it. | §3.1.4 fail-closed for holds. | None. |
| 8 | **Benchmark fault families are plug-ins** (`benchmark/history/faults/<engine>.py`), discovered by `pkgutil`; `eval.py` scores per finding type with per-type gates each module exports. | No shared benchmark file for four branches. | None. |
| 9 | **Postgres tests use four database names** (one per worktree) on the one local server. | `eval.py` and schema tests `drop_all`; a shared DB would corrupt a sibling mid-run. | None. |

## Part 1 — Scaffold (sequential, branch `v2/w1-scaffold` from `v2/wave0-foundation`)

Executed by me, TDD, with its own short plan written at execution start (same shape as Wave 0's). Deliverables:

### S1. Engine runtime (`invoice_processing/trust/engine.py`, new)
```python
@dataclass
class EngineContext:
    ctx: RunContext; mode: str; document: dict | None; invoice_node: str | None
    graph: GraphStore; links: LinkStore; findings: FindingStore; providers: Registry
    settings: dict; decider; blobs; url: str | None; now: datetime
    def threshold(self, name: str) -> float      # settings["trust_thresholds"][name], default from thresholds.DEFAULTS
    def transactions(self, as_of=None) -> list[Transaction]   # memoised per evaluate() call (one tenant scan, not four)
```
`evaluate.py`: drop the "no invoice → skip" early return (a contract document has no invoice); build `document` (id, kind, sha256, storage_key, source, source_ref, sender, reply_to, filename) and `invoice_node` (None for non-invoice documents); `document_id=None` means batch/tenant-wide (benchmark, later batch runs). For each engine with mode ≠ `off`: `run(ectx)`, stamp `details["mode"]`, `findings.upsert`. Per-engine `try/except` per Decision 7. Tests inject fakes into `evaluate.RUNNERS`.

`findings/model.py`: `register_type(code, description, *, engine: str, hold: bool = False)`; adds `HOLD_TYPES`, `TYPE_ENGINE`. `findings/store.py`: `list(..., transaction_node=None)` filter.

### S2. Hold gate (`invoice_processing/trust/gate.py`, new) + two v1 call sites
`payment_hold(tenant_id, case_id, *, url=None, settings_store=None) -> str | None`: invoice id for the case → its node → open findings whose `type in HOLD_TYPES` and whose engine is `enforcing` for this tenant → a sentence naming them, else `None`. Returns `None` when `service.get_graph_store()` is `None` (v1 without a DB is unchanged). Wired: `approval_flow.decide` raises `PaymentHold(Refused)` on the chain-completing approval unless `principal.can("admin") and override_reason`; `service.export_invoice` returns 409 after its approve check. Tests: a cross-tenant probe is not needed (no route), but `tests/test_approval_flow`-style tests for both call sites, including the admin override and the `None` path.

### S3. Thresholds (`invoice_processing/trust/thresholds.py`, new; `tenant_settings.py` edited once)
`DEFAULTS` with every Wave 1 name (listed in each spec below). `SETTABLE["trust_thresholds"] = dict`, `NON_ENGINE_DEFAULTS[...] = DEFAULTS`, `DESCRIPTIONS` entry, one `validate()` branch (known name, numeric, not bool).

### S4. Graph sync additions (`trust/graph/sync.py`, the only Wave 1 edit to the builder)
Invoice node attrs += `document_id, document_sha256, document_source, document_source_ref, document_sender_domain, document_reply_to_domain, payment_terms, due_date, total_ex_tax, tax_amount` (cgst+sgst+igst from `Invoice.fields` via `_money`, else `total_inc − total_ex`), `place_of_supply, customer_tax_id, discount`. Line attrs += `hsn_sac, line_discount`. Vendor attrs += `bank_verified_at, bank_account_name`. History nodes carry every mapped field for their kind (`counterparty, bank_reference, line_number, quantity, unit_price`). `_HISTORY_ORDER` += `bank_line`, `vendor_statement_line`. Sync tests extended for each.

### S5. Contract upload (`api/trust_routes.py`)
`POST /api/trust/contracts` (clerk): multipart `file` (PDF ≤ 25 MB, magic bytes checked via `ingest.check_acceptable`) + `vendor_id`; stores `documents/{sha}.pdf` through the existing document repository with `kind="contract"`, `source="upload"`; upserts node `contract` (ref `documents`) + edge `governs` → vendor node; enqueues `trust.evaluate(document_id)`. `GET /api/trust/contracts`. Cross-tenant probes added to `tests/test_tenant_isolation_api.py`.

### S6. Benchmark plug-ins (`benchmark/history/faults/`, `generate.py`, `eval.py`)
`faults/__init__.py` discovers `faults/<engine>.py` via `pkgutil.iter_modules`. Each module exports `inject(rng, rows, faults, *, start, months) -> None`, `GATES: dict[type, {"precision", "recall", "impact_error"}]`, optional `seed_graph(ctx, graph, data_dir)` and `provider_scripts`. `generate.py`: `KINDS = mapping.KINDS`, vendor headers += `pan, bank_account, bank_code`, writes only kinds with rows, manifest `faults` filled by modules. `eval.py`: imports whatever CSVs exist, builds a batch `EngineContext` (mode `advisory`, `FakeDecider(fail=True)`, fake providers + scripts), runs every engine, scores per type (match on `(type, doc_number)` through the finding's transaction node; precision, recall, `impact_error = Σ|actual−expected| / Σexpected`), gates = Wave 0's two + the union of module gates; a type with no module present is not gated. CI's eval step is unchanged.

### S7. Done once for all four branches
`.github/workflows/ci.yml` Postgres list += `tests/test_trust_leakage.py tests/test_trust_payment.py tests/test_trust_provenance.py tests/test_trust_gst_itc.py` (exact names; extra `tests/test_trust_<engine>_*.py` files run on SQLite without CI edits). `.gitignore` += `.worktrees/`. `docs/PENDING.md`: the four sub-bullets, the approval-before-evaluation gap, "enforcing with fake providers holds everything". Spec `docs/superpowers/specs/2026-10-15-v2-wave1-scaffold-design.md` recording node/edge kinds, thresholds and the ownership table. Spec files for the four engines (Part 2 text) committed under `docs/superpowers/specs/2026-10-15-v2-wave1-<engine>-design.md`.

### File ownership (enforced in each implementer's brief and checked at review)
| Branch | MAY touch | MUST NOT touch |
|---|---|---|
| `v2/w1-<engine>` | `invoice_processing/trust/<engine>/**`, `tests/test_trust_<engine>*.py`, `tests/fixtures/trust_<engine>/**`, `benchmark/history/faults/<engine>.py`, `docs/superpowers/plans/2026-10-15-v2-wave1-<engine>.md` | everything else — in particular `modes.py, evaluate.py, engine.py, gate.py, thresholds.py, tenant_settings.py, models.py, migrations/**, ci.yml, PENDING.md, graph/*, findings/*, providers/*, trust_routes.py, generate.py, eval.py, approval_flow.py, service.py, duplicates.py, confidence.py, frontend/**` |

A helper an engine needs from a shared module (e.g. a new `duplicates.py` function) is written inside the engine package; the post-wave cleanup promotes it. An engine that discovers it needs a sync/graph change stops and files it as a scaffold follow-up commit on `main`, then rebases.

## Part 2 — The four engine specs (approved together with this plan)

Common to all four: package `invoice_processing/trust/<engine>/`, `ENGINE_VERSION` constant, `register_type(...)` calls at the top of `__init__.py`, `run(ectx: EngineContext) -> list[FindingSpec]` (batch when `ectx.document is None`), every finding cites ≥ 1 node of the same tenant, ₹ impact by deterministic arithmetic only, dedupe keys prefixed by type, excerpts and `details` pass through `masking.mask_bank`, model access only via `decide()` with the budgeted `GeminiDecider`, providers only via `ectx.providers.call(...)`.

### E1. Leakage (FR-20) — `trust/leakage/`
- **Purpose:** contract-vs-invoice compliance, duplicates beyond v1, supplier-statement reconciliation, missed discounts, renewal/escalator radar.
- **Inputs:** contract documents (clause extraction when `document.kind == "contract"`), invoice/line/vendor/payment/`vendor_statement_line`/`contract`/`contract_clause` nodes, `links.canonical_map`, `ectx.transactions()`; contract PDF bytes via `ectx.blobs.get(storage_key)` → `confidence.extract_page_text` + `confidence.locate` for page citations. No providers.
- **Clause extraction:** deterministic candidate spans per page (regex: %, ₹/INR amounts, dates, "per unit / tier / rebate / escalat / renew / notice / net N"); for each page one `decide()` call whose questions classify each candidate (`choice` ∈ rate|escalator|tier|rebate|discount|term|none, plus a `score`); `params` parsed by regex from the excerpt, never by the model; node `contract_clause` (natural key `clause:{contract}:{page}:{sha(excerpt)[:12]}`, attrs `type, params, page, excerpt, confidence, reviewed=False`), edge `part_of` → contract (`model_inference`). Invoice → contract: edge `governed_by` to the vendor's contract whose term covers the invoice date, else the latest. Cap `leakage.max_pages`.
- **Findings:** `LEAK-RATE` (unit price > rate×(1+tol) on a matched item; impact `(unit_price−rate)×qty`), `LEAK-ESCALATOR` (allowed = base×(1+esc)^years), `LEAK-TIER` (cumulative qty ≥ tier), `LEAK-REBATE` (rolling spend ≥ threshold with no statement credit; impact `spend×pct`), `LEAK-DISCOUNT` (`payment_terms` like `2/10 net 30` parsed deterministically; a payment on/before day d for the full amount; impact `amount×pct`), `LEAK-TERM` (invoice outside term; impact None), `DUP-NEAR` (same canonical vendor, `normalize_number` equal or edit distance ≤ 2 with equal amount, within `dup_window_days`; impact = later amount), `DUP-SPLIT` (≥ 2 invoices within `split_window_days`, each below `high_value_threshold`, sum ≥ it; impact None), `DUP-CROSS` (same number + amount on two vendor nodes with one canonical; impact amount), `STMT-UNAPPLIED-CREDIT`, `STMT-DOUBLE-PAID`, `STMT-DISPUTED`, `RADAR-RENEWAL`, `RADAR-ESCALATOR`. Finding confidence = rule × clause confidence; unreviewed clauses mark `details.needs_review`. Reuses `duplicates.normalize_number / normalize_vendor / same_vendor`.
- **Thresholds:** `leakage.desc_match 0.6, dup_window_days 7, split_window_days 3, rate_tolerance_pct 0.5, clause_min_confidence 0.7, renewal_notice_days 60, max_pages 40`.
- **Modes:** generic; no hold types.
- **Benchmark `faults/leakage.py`:** rate overcharge, near-dup, split, unapplied credit, missed discount; `seed_graph` plants clause nodes from `contract_clauses.json`; gates precision ≥ 0.9, recall ≥ 0.8, impact_error ≤ 0.02.
- **Non-goals (Wave 1):** `LEAK-SLA`, claim packs, clause editor UI, scanned contracts (OCR), FX, PO-based tiers.

### E2. Payment Trust (FR-21) — `trust/payment/`
- **Purpose:** hold payment to unverified or impersonated payees; flag behavioural anomalies.
- **Inputs:** invoice + vendor nodes (vendor row via ref for bank fields, `bank_verified_at`, `email_domain`), `bank_change` nodes, `ectx.transactions()` for the vendor, the stored `.eml` via `ectx.blobs.get(document.source_ref)` when `source == "email"` (raw MIME or the Postmark/SendGrid JSON ingest stores — both parsed), provider `bank_verification.verify(account, code, name)`.
- **Findings:** `PAY-UNVERIFIED` (**hold**: `bank_verified_at` None and the provider does not return `verified`, or is `unavailable`; impact invoice amount), `PAY-NAME-MISMATCH` (**hold**: name match below `payment.name_match_min`), `PAY-ANOMALY` (advisory: robust z-score `(amount−median)/(1.4826×MAD)` > `amount_zscore`, cadence gap < `cadence_ratio_min`×median, or first-time vendor with fewer than `min_history` transactions; impact `amount−median` when positive), `PAY-BEC` (**hold** at ≥ `bec_hold_signals` signals) / `PAY-BEC-SIGNAL` (advisory at 1): From domain ≠ `vendor.email_domain`; look-alike domain (`difflib` ratio ≥ `lookalike_ratio` after confusable folding, not equal); Reply-To domain ≠ From domain; urgency words; an account/IFSC in the body ≠ vendor master; a `bank_change` within `bank_change_recent_days`.
- **Thresholds:** `payment.amount_zscore 3.0, min_history 5, cadence_ratio_min 0.25, name_match_min 0.8, bec_hold_signals 2, lookalike_ratio 0.8, bank_change_recent_days 30`.
- **Modes:** generic; hold types gate through the scaffold's `payment_hold` in approval and export.
- **Replay harness (AC-21):** `tests/fixtures/trust_payment/<scenario>/{vendor.json, invoice.json, email.eml, expected.json}`, parametrised in `test_trust_payment.py`: clean, look-alike domain, reply-to swap, bank change + urgency, unverified first payment, provider down.
- **Benchmark `faults/payment.py`:** amount outliers (5×), first-payment-unverified; gates precision ≥ 0.9, recall ≥ 0.8. BEC is proven by the replay harness, not the CSV benchmark.
- **Non-goals:** payment execution, a real verifier, mailbox polling, a Treasury queue UI, a sheet "hold" column (an enforced hold never reaches `decision=approve`; export is the second guard).

### E3. Provenance v1 (FR-22 part) — `trust/provenance/`
- **Purpose:** a deterministic authenticity score per document, template consistency, tamper after approval, a provenance ledger.
- **Inputs:** document bytes → `pdfplumber` metadata (Producer/Creator/CreationDate/ModDate), page count, page sizes, font names, text-layer presence (`confidence.has_readable_text`); the vendor's earlier `document_version` nodes; approval nodes; invoice attrs `document_sha256, decision, invoice_number`.
- **Ledger:** node `document_version` (natural key `documents:{id}:{sha}`, attrs `sha256, source, sender_domain, producer, creator, creation_date, mod_date, page_count, has_text, fingerprint, channel, template, score, components, actor`), edge `evidences` → invoice (`system_record`, 1.0). Graph-only (Decision 1).
- **Score:** `channel` (email from `vendor.email_domain` 1.0 / api 0.8 / upload 0.6 / other 0.3), `template` (share of the vendor's prior fingerprints that match; 1.0 under `template_min_history`), `text` (1/0), `meta` (producer present and `creation ≤ mod ≤ now`); `score = 0.4·channel + 0.3·template + 0.2·text + 0.1·meta`; fingerprint = sha256 of rounded page size + sorted font set + producer.
- **Findings:** `PROV-SUSPECT` (advisory: score < `suspect_below`; impact None), `PROV-TAMPER` (**hold**: another invoice with the same normalised number and vendor that was approved, with a different `document_sha256`; impact invoice amount; cites both invoices and both versions).
- **Thresholds:** `provenance.suspect_below 0.5, template_min_history 3`.
- **Benchmark `faults/provenance.py`:** `seed_graph` plants approved history invoices with a prior sha, then re-submits a changed one; gate `PROV-TAMPER` precision = recall = 1.0.
- **Non-goals:** KYC and delivery proofs, forensic/ML ensemble, image analysis, OCR (no text layer only lowers the score), non-PDF documents (skipped and logged).

### E4. GST/ITC v1 (FR-26) — `trust/gst_itc/`
- **Purpose:** vendor GST standing and identity-sharing signals; ITC-at-risk ₹ per invoice.
- **Inputs:** vendor `gstin` (or invoice `vendor_tax_id`, checked with `tax_ids.validate_gstin`), `ectx.transactions()`, vendor nodes + `resolve.candidates` + `links.canonical_map`, invoice `tax_amount`; provider `gstin.lookup(gstin)` → `{status: active|cancelled|suspended, missed_periods, registered_on}`.
- **Findings:** `GST-INACTIVE` (**hold**: status ≠ active), `GST-UNVERIFIED` (**hold**: provider unavailable — fail closed), `GST-NONFILER` (advisory: `missed_periods ≥ nonfiler_missed_periods`), `GST-NEW-LARGE-FIRST` (advisory: first seen within `new_vendor_days` and amount > `first_invoice_multiple` × tenant median), `GST-SHARED-IDENTITY` (advisory: a candidate pair sharing `bank_account`/`pan`/`email_domain` with two different GSTINs), `GST-ITC-RISK` (impact `tax_amount × max(factor)` over present signals: `risk_inactive 1.0, risk_nonfiler 0.5, risk_shared 0.3, risk_new 0.2`).
- **Thresholds:** `gst.nonfiler_missed_periods 2, new_vendor_days 90, first_invoice_multiple 3` plus the four factors.
- **Benchmark `faults/gst_itc.py`:** provider script marks vendors cancelled / non-filer; vendor rows sharing `bank_account`/`pan` across GSTINs; a new vendor with an oversized first invoice; gates precision ≥ 0.95, recall ≥ 0.9, `GST-ITC-RISK` impact_error ≤ 0.01.
- **Not possible in Wave 1, stated plainly:** IRN/e-invoice verification (no IRN field exists), address/place-of-business checks (no vendor address in the master), 2A/2B reconciliation.
- **Non-goals:** real GSP adapter, ITC ledger, PAN validator.

## Part 3 — Execution mechanics

1. **Disk pre-step** (free is ~4.4 GB): `npm cache clean --force` (6.2 GB), `docker builder prune -f` (5.7 GB), delete `benchmark/runs/*` (0.6 GB, git-ignored). Stop if free < 1.5 GB afterwards. Four venvs hardlink from the uv cache (~360 MB each at most).
2. **Scaffold** on `v2/w1-scaffold` from `v2/wave0-foundation`: plan → TDD → full suite + Postgres list + eval → `fable` review → fix pass → PR (targets `main`; stacked on #52 until #52 merges).
3. **Engine plans:** one `docs/superpowers/plans/2026-10-15-v2-wave1-<engine>.md` each, written by me with `writing-plans` from the spec above, before any worktree starts.
4. **Worktrees:** `git worktree add .worktrees/<engine> -b v2/w1-<engine> <scaffold HEAD>` ×4; `uv sync --frozen` in each; no frontend installs or builds. Postgres: one local `postgres:16`, databases `trust_w1_<engine>`.
5. **Implementers:** four subagents in parallel (`superpowers:subagent-driven-development` per worktree), each briefed with: its spec and plan paths, its ownership row, the eight §3.1 contracts, the exact Wave 0/scaffold signatures (`EngineContext`, `run()`, `GraphStore.*`, `transactions`, `LinkStore.canonical_map`, `resolve.natural_keys/candidates`, `FindingSpec/Citation`, `register_type`, `decide/FakeDecider`, `Registry.call/FakeProvider`, `blobs.get`, `confidence.extract_page_text/locate/has_readable_text`, `duplicates.*`, `masking.mask_bank`), the fault-module API, and the repo rules (TDD, ruff, no ids in log `extra`, no raw bank numbers anywhere, dedupe keys by type, one citation minimum). Each ends with its own `fable` review, one fix pass, and a PR with CI green.
6. **Merge order:** any; after each merge the remaining three `git rebase main` (clean by construction). After every merge on `main`: full suite, Postgres list, `python -m benchmark.history.eval --seed 7`. Each merge on your word, with `--match-head-commit`.
7. **Wave close:** all four PRs merged and all per-type gates green together; worktrees removed; `docs/PENDING.md` and memory updated; a post-wave cleanup PR promotes any helper duplicated inside engine packages.

## Verification (end of Wave 1)
- Scaffold: full suite green; `payment_hold` tests for both call sites incl. admin override; contract upload + isolation probes; sync attr tests; eval runs four engines with zero modules present and still passes Wave 0's two gates.
- Each engine PR: its unit tests + replay/fixtures; `python -m benchmark.history.eval --seed 7` with its fault module meeting its gates; Postgres run of `tests/test_trust_<engine>.py`; `fable` review clean after one fix pass.
- Integration on `main` after the last merge: suite, Postgres list, eval with all gates, `gates.sh` (frontend gates once, on `main`).
- Manual smoke (local, `AUTH_DISABLED=1`): upload a contract → clauses appear as nodes; set `trust_engine_modes.payment=enforcing` → an invoice from an unverified vendor cannot complete approval; export returns 409; admin override with a reason succeeds.

## Risks carried into execution
- Approval-before-evaluation race (Decision 5) → PENDING, Wave 2.
- Hold engines in `enforcing` with fake providers hold every invoice: correct per contract; defaults stay `off`; documented.
- Gemini cost on long contracts: one `decide()` per page, `max_pages` cap, budgeted decider.
- `Invoice.fields` tax components are untyped; the scaffold's sync must `_money()` them and tolerate absence (GST engine emits nothing and logs the gap when `tax_amount` is None).
- Attr filtering is Python-side (SQLite has no JSONB operators).
- `.eml` blobs may be raw MIME or provider JSON; fixtures for both.
- Lineage still reports `contract: no_evidence`; Wave 2 teaches it `governed_by`.
