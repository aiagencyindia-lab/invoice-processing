# Invoice Processing → AP Automation Platform: Implementation Plan

## Context

We have a working Google ADK invoice-processing prototype (running locally, 170 tests green, v1.2.0). The PRD proposes turning it into a multi-tenant AP automation SaaS across three phases over 12 months, positioned as "the auditable, governed AP brain."

Before planning, three parallel code audits verified the PRD's assumptions against the actual codebase. **Several load-bearing claims are wrong**, and correcting them changes what we build first. The PRD's positioning rests on three differentiators — ALF self-learning rules with measured blast radius, the investigation critic, and decision explainability. The audit found that **two of the three do not currently work as advertised**. Fixing that is the highest-value work available, and it comes before any new feature.

Decisions already taken (from planning questions):
- **Builder:** solo (you + Claude Code), but the plan must cover all phases and full scope.
- **First milestone:** pilot with 2–3 design partners in 8–12 weeks.
- **Compliance:** generic core first, country modules as plugins.
- **Codebase:** hybrid — engines become a versioned library with injected adapters; platform built around it.

---

## Part A — What we already have (verified, not assumed)

### A1. Genuinely strong, keep and build on

| Asset | Reality | Evidence |
|---|---|---|
| **Decision explainability** | Real and better than most competitors. Every decision carries phase, step, rule, evidence, and rejection template, traceable to a written rulebook. This is the EU AI Act Article 86 story and it is genuine. | `general_invoice_agent.py:926-1342` |
| **ALF correction engine** | Solid deterministic core: 21 operators, 11 action types, scope-based mutual exclusion, 3-tier Collect-Plan-Execute capped at 2 LLM calls/case. Well tested. | `alf_engine.py:1318-1963` |
| **Rules book as constitution** | 754-line versioned markdown, both regex-parsed and LLM-fed. Real governance artifact. | `data/reconstructed_rules_book.md` |
| **Safety loop** | generate → validate → impact-assess → auto-tighten, max 3 attempts, author≠approver enforceable. Genuinely novel vs opaque competitor retraining. | `safe_rule_orchestrator.py:49-214` |
| **Eval harness** | Two-layer (deterministic + LLM judge), comparison groups, decision matrix. Good bones for a CI gate. | `eval/eval.py` |
| **Test suite** | 170 tests, fully offline, covering ALF operators/actions/impact/writer. | `tests/` |

### A2. The PRD is wrong about these — corrections

| PRD/research claim | Verified reality |
|---|---|
| "24 deterministic operators" | **21.** Fix the number in all marketing/PRD copy. |
| Duplicate detection "Partial" | **Absent.** Hardcoded `"passed": True, "evidence": "Duplicate check not implemented"` (`:1156-1166`). |
| PO matching "Partial (Phase 2)" | **Absent.** Hardcoded `passed: True` (`:1106-1116`), and PO number isn't even in the extraction schema — nothing to match on. |
| Critic "Layer 1 deterministic" | **False.** Layer 1 is LLM-backed (6 calls). Full critic run = **9–27 gemini-2.5-pro calls per case**. This caused real 15–25 min hangs during testing. |
| "Domain-agnostic — swap `master_data.yaml`" | **~15% true.** YAML sections 1–5 and 8–11 have **zero code callers**. The acting agent never imports `master_data_loader`; it has a duplicated `DEFAULT_CONFIG` at `:139-168`. Only `eval_schema` + `investigation` sections are read. |
| Extraction uses vision | Partly. The extractor sends the whole PDF to Gemini Pro (which rasterizes internally), but there is no page rendering, DPI control, or per-page handling. The *classifier* uses extracted text. |
| ALF impact assessment = "measured blast radius" | **Overstated.** It `random.sample`s 10 cases (`impact_assessor.py:115-122`) — non-deterministic and non-reproducible. Sound at 3 cases, meaningless as a safety guarantee at 10,000. |

### A3. Correctness bugs found (fix before building on top)

1. **`has_waf` computed two different ways** — `alf_engine.py:2166` (from `extraction.waf_count`) vs `case_loader.py:121-123` (phase1 override). **The impact assessor and the live engine can therefore disagree about the same case**, which directly undermines the safety guarantee we intend to sell. Highest-priority bug.
2. **`save_rule_base()` silently drops** disabled rules and unknown top-level keys (`alf_engine.py:2051`) — data loss.
3. **Step 4.1** is `informational_only: true` in YAML but **enforcing in code**, failing the phase with `rejection_template=None`.
4. **Step 4.3** ignores the `require_work_authorization` gate its own YAML spec declares.
5. **`next_rule_id()`** is max+1 with no locking — two concurrent sessions get the same ID.
6. **Session logger persists nothing until `save()`** — a crash loses the entire SME session. No user or tenant identity on any event.
7. **Dead code** presented as features: `check_vendor_name_similarity`, `WorkTypeClassification`, `ItemCodeClassification`, `SET_ASIDE` (never produced), `valid_po_prefixes`, `waf_exempt_vendors`.

### A4. Absent — and needed for a pilot

Per-field confidence (three confidence fields exist but are **dead code**) · bounding boxes · confidence-based routing · duplicate detection · PO/GRN matching · vendor master · GL coding · multi-invoice PDF splitting (multi-invoice PDFs are **rejected**, not split) · document types beyond invoice+WAF · extraction of PO number, due date, payment terms, bank details, per-rate tax breakdown, vendor address, HSN/SAC · any concept of tenant, user, or auth.

### A5. Multi-tenancy blockers

Module-level singletons (`tools.py:44`, `agent.py:89`, `general_invoice_agent.py:170` mutable global config dict, class-level `_model` caches) · package-relative output paths in **four** duplicated path roots · rule base is one JSON file rewritten wholesale with no locking · rule-discovery cache is one global file keyed only by rulebook hash, no tenant dimension · zero occurrences of `tenant`/`user_id`/`org_id` anywhere.

### A6. Deployment reality

Current target is **Agent Engine** (Vertex AI Reasoning Engine) — a conversational-agent host, not a platform host. Correct for the Learning-mode chat agent; wrong for a multi-tenant AP platform. Confirms the PRD's Cloud Run direction.

---

## Part B — Strategy: five judgments that shape this plan

**B1. Harden the moat before we sell it.** The PRD's entire positioning is governed autonomy. Two of the three pillars are currently overstated: impact assessment randomly samples, and the critic is a 9–27-call latency bomb whose "deterministic layer" isn't. The first design partner who audits our "measured blast radius" claim will find `random.sample`. Fixing this is ~2 weeks and converts marketing claims into true claims. It comes first, before any new feature.

**B2. "Generic first" is not a detour — it *is* the platform.** You chose generic core with country modules as plugins. That happens to be precisely the largest gap: the master-data YAML is ~85% aspirational, and the acting agent doesn't read it. Making the pipeline genuinely config-driven is what turns India/ANZ/EU from forks into plugins. Do this once, in Phase 0–1, and every country pack afterwards is additive config plus a small validator module.

**B3. The pilot slice is much narrower than PRD P1.** PRD P1 bundles multi-tenant spine + 3 capture channels + India GST + workbench + connectors + dashboards. That is a team-quarter or more. For one person in 8–12 weeks the pilot slice is: **upload + email capture → extraction with confidence → review workbench → audit trail → one export**. Everything else in P1 moves to months 4–6. Cutting this is what makes the pilot real.

**B4. Buy everything that isn't the AP brain.** Solo capacity is the binding constraint, so default to managed services and partners: Identity Platform for auth, Postmark/SendGrid for inbound mail, Pub/Sub for queueing, Cloud SQL, GCS, Sentry, Vanta/Drata for SOC 2 evidence when it's time. Never build in-house: OCR engine, Peppol Access Point, payment rails. The PRD already says this; the plan holds the line on it.

**B5. Redesign the critic rather than tune it.** At 9–27 `gemini-2.5-pro` calls per case it is both the dominant COGS line and the cause of observed 15–25 minute hangs. Most of Layer 1 is field comparison that doesn't need an LLM at all. Target state: genuinely deterministic Layer 1 (0 LLM calls), LLM reserved for subjective judgments, run on a sampled percentage rather than every case, aggressively cached. Goal ≤2 LLM calls/case on the sampled path, 0 on the default path — while *keeping* the explainability output that makes it valuable.

**Honest note on scope.** You asked for all phases and full scope, and this plan covers all three. But P1–P3 as the PRD defines them (multi-region, SOC 2 Type II, ISO 27001, Peppol, payments, 5 ERP connectors) is genuinely multi-engineer, multi-year work for one person. The plan is sequenced so that value lands early and continuously, and so that the point where you must hire is visible rather than discovered late — see Part E.

---

## Part C — Implementation

### Phase 0 — Truth & Foundation (Weeks 1–3)

Goal: every claim we make is true, and the engines become an embeddable library. **No new features.** `pytest` must be green after every step.

**0.0 Safety net first (2 days). Strictly before anything else.** Freeze a golden fixture — the 3 `exemplary_data` cases' full artifact sets plus expected ALF output — and add a characterization test asserting `run_alf_on_case` output byte-for-byte. This is the only thing standing between the refactor and a silent regression. In the same pass, delete the verified dead code while the blast radius is 3 cases (`check_vendor_name_similarity`, `WorkTypeClassification`, `ItemCodeClassification`, `SET_ASIDE`, `valid_po_prefixes`, `waf_exempt_vendors`, master-data sections 1–5 and 8–11). Everything deleted now is something you never have to port.

**0.1 Correctness bugs (2–3 days) — runs in parallel, blocked on nothing.** Each gets a regression test, mutation-checked the way we validated Step 4.4. Step 4.1 `informational_only`; Step 4.3's ignored `require_work_authorization` gate; `save_rule_base()` round-trip (add a `load(save(load(x))) == x` test with a disabled rule *and* an unknown key); durable session logging.

Also: change the `passed: True` stubs (duplicate detection `:1156`, PO matching `:1106`) to report `not_implemented` explicitly. **A stub that reports success is worse than a missing feature — a design partner will believe it.**

**0.2 Config & path collapse (2 days).** One `EngineSettings` object. Delete the duplicated `DEFAULT_CONFIG` (`general_invoice_agent.py:139-168`), `_config_store` (`:170`), and the `alf_engine.py` copies of the path constants. Remove the `sys.path.insert` hack at `core/config.py:38` — it likely makes modules importable under two names right now, which means duplicated class objects and `isinstance` surprises. Paths stay hardcoded; only the duplication dies.

**0.3 Context unification — the keystone (3 days).** One `invoice_core/context.py` with a `FIELDS` registry and a single canonical `build_case_context()`. Both divergent builders (`alf_engine.py:2161`, `case_loader.py:_build_context`) are deleted and call it.
- **Do not silently pick a `has_waf` winner.** Keep `has_waf` = raw evidence (matches the live engine, so production behaviour doesn't change) and add an explicit `waf_present_phase1` for the Phase-1 override. Write a replay script that runs every existing rule against every case under both old builders and reports which verdicts change — that script *is* the deliverable of this step, not an optional extra.
- `core/prompts.py:65-101` stops being hand-maintained prose and becomes generated `describe_fields()` output, so the field list can no longer lie to the LLM.
- Add `validate_field_path()` called from both the rule writer and the repository save. Today an unresolvable field path silently returns `None` and the condition just never fires. **Highest value for lowest effort in the entire plan.**

**0.4 Critic cost redesign (3–4 days) — parallel, highest business urgency.**
- **Delete the triple-check loop (one line).** Every validator runs at `temperature=0`, so we pay 3× for a near-deterministic call to confirm itself. Layer 3 goes from 9 calls to 3 immediately.
- **Layer 1's prompts instruct the model to "assume valid" and "if any doubt exists, set valid=true."** We are spending six Pro calls per case on a validator prompted into near-unconditional agreement — its output is close to a constant. Move `validate_abn_matching`, `validate_waf_hours`, `validate_duplicate_detection`, `validate_invoice_type` into pure functions over the unified context; they are field comparisons, not judgment.
- Target: **Tier 0** deterministic (0 LLM calls, every case) → **Tier 1** one batched Flash call only for cases Tier 0 flags → **Tier 2** one Pro call only on genuine disagreement, where a human will read the narrative anyway. Amortized ≈0.15 Pro calls/case vs 9–27 today.
- **Run the critic off the hot path.** It can only downgrade or stop, never approve, so it needn't block the response. This removes critic latency from the user-visible path independently of the cost work.
- Add `max_llm_calls` and `deadline_seconds` to settings. The 15–25 minute hangs happen because **there is no deadline anywhere** — that's the actual bug behind the symptom.
- **Fix the discovery cache key**: `sha256(rules_book)` alone means one tenant's discovery leaks into another's. Must be `(tenant_id, model, prompt_version, rules_hash)` — a data-leak incident the moment there are two tenants.

**0.5 Ports and package move (1 week).** `invoice_core` as a package in a **uv workspace, not a second repo** (a second repo buys nothing solo and costs a two-repo dance per change).
- Five protocols in `ports.py`: `BlobStore`, `CaseRepository`, `RuleRepository`, `AuditSink`, `LlmClient` — plus a `RunContext` dataclass (`tenant_id`, `case_id`, `run_id`, `actor`, `settings`) threaded through every core call. **`RunContext` is what kills the singletons**; the only global that survives is an `lru_cache`d model factory keyed by `(model_name, temperature)`, since the model object is stateless but the config is not.
- `RuleRepository.save(..., expected_version=)` with optimistic concurrency and DB-allocated rule IDs — replaces the unlocked full-file rewrite and `max+1` scan. ~15 lines, removes a whole class of silent rule loss.
- **Move by shim-then-drain:** physically move the code, leave the old modules as pure re-export shims, and run the suite with **zero test edits**. If it isn't green, you moved semantics rather than code — revert that piece. Rewrite test imports later, one file at a time.

**0.6 Pluggable validators (2 days) — the "generic first" enabler.** Extract the ABN checksum into a `tax_id_validator` registry so GSTIN/VAT/EIN become registrations rather than forks. Make the acting agent actually read `master_data_loader`.

**Exit criteria:** 170+ tests green with zero edits through the move; the 3 reference cases reproduce identical decisions; critic measured at ≤1 LLM call/case typical; impact assessment reproducible across runs; `invoice_core` importable from a scratch project.

### Phase 1 — Pilot spine (Weeks 4–8)

Narrow vertical slice. Recommended host: **Cloud Run + Cloud SQL + GCS + Pub/Sub**, single region to start — Vertex AI is already GCP, and residency is a core product promise, so splitting clouds is a false economy. (Railway is a legitimate faster alternative for the web tier; keep the app 12-factor either way.)

| # | Work | Notes |
|---|---|---|
| 1.1 | Postgres schema + Alembic + RLS | Implement only the PRD §10 subset the pilot needs: `tenant`, `user`, `document`, `invoice`, `invoice_line`, `finding`, `pipeline_run`, `rule`, `correction`, `audit_event`. Defer PO/GRN/payment/2B tables. |
| 1.2 | GCS storage adapter | Slots into the 0.2 port. Per-tenant prefixes from day one. |
| 1.3 | Pub/Sub + worker | Durable async pipeline; ingestion acks before processing. |
| 1.4 | FastAPI service + auth | Identity Platform; tenant scoping enforced server-side; **automated cross-tenant test suite in CI from the first commit** — retrofitting isolation tests is how leaks happen. |
| 1.5 | Ingestion: upload + email | Postmark/SendGrid inbound; store raw `.eml` as evidence; SHA-256 file-duplicate check. Defer API/Peppol/portal. |
| 1.6 | **Confidence scoring v1** | See the design note below — text-layer grounding first, then arithmetic consistency. Both deterministic, both free. This is the mechanism that converts extraction into a touchless number a buyer pays for. |
| 1.7 | Real duplicate detection | Replace the placeholder: exact (vendor+number), normalised, and fuzzy (vendor + amount ±0.5% + date ±7d). Pure logic, high value, cheap. |
| 1.8 | Review workbench | Next.js 16 / React 19 / Tailwind 4 — **reuse the existing patterns in `on-brand-genmedia/frontend/src/components/` (`AppShell.tsx`, `Chrome.tsx`, `GoogleSignIn.tsx`)** rather than starting fresh. Document viewer + field form, low-confidence fields focused first, keyboard-first, every edit captured as a `correction`. |
| 1.9 | Audit trail to Postgres | Append-only `audit_event`; actor/tenant on every row — the gap the session logger has today. |
| 1.10 | One export path | CSV first, then Xero **or** Zoho Books (pick by whichever design partner you land). Not bidirectional yet. |

**Confidence design (1.6) — do not extend the existing pattern.** The three dead `confidence` fields are LLM self-reports, and self-reported confidence is famously uncalibrated; routing on it is a coin flip dressed as a number.

1. **Text-layer grounding, first.** `pdfplumber` is already a dependency and already parses these PDFs. After extraction, search the page text for each extracted value: deterministic, free, and it yields **real page + bbox coordinates for the reviewer UI**. "Not found verbatim in the document" is a strong hallucination signal. Normalize (currency symbols, thousands separators, date formats) before matching or every correct number gets flagged. No text layer → return `None`, which is honest and routes to review rather than raising a false alarm.
2. **Arithmetic consistency, second.** Pure Python: lines sum to subtotal, ex+tax=inc, qty×price=line total, tax rate in the configured set. These are the errors that cost money and exactly the ones a model's self-report will never catch — it will confidently report 0.95 on a number it transposed.
3. **Compose into coarse buckets** (grounded ∧ arithmetic → high; one → medium; neither → low; no text layer → always review). A three-valued signal you can defend to a design partner beats an indefensible float.

Defer dual-pass agreement (doubles extraction cost for a signal largely redundant with grounding) and vendor-history match (needs a vendor master).

**Schema disruption is minimal:** leave `InvoiceExtraction` (`:226-238`) alone — it's the model's contract. Confidence lands in a sibling `_evidence` key inside `02_extraction.json`, mirroring the existing `_tax_id_validation` convention. Zero change to the Gemini call.

**Extraction schema extension** (do it now, in the same pass): add PO number, due date, payment terms, bank details, per-rate tax breakdown, vendor address to the schema (`:226-238`) and prompt (`:839-864`). One line each — and every one added later means re-extracting the whole backlog.

### Phase 2 — Pilot with design partners (Weeks 9–12)

- Onboard 2–3 partners in **"review everything"** mode; raise the touchless threshold progressively.
- Build the golden dataset from their real invoices (target ≥200 labelled) — this is the asset that compounds.
- Learning mode in the UI: "Teach the system" from a reviewed invoice, with the now-honest impact report.
- **Measure and publish internally: touchless rate, header/line accuracy, cycle time, exception rate, cost/invoice.** These decide whether this is a business; the PRD's targets (≥40% touchless, ≥95% header at P1) are the bar.

### Phase 3 — Parity (Months 4–6, PRD P2)

Approval workflows (conditional routing, delegation, escalation, mobile) · line-level 3-way match with PO/GRN ingest · non-PO GL auto-coding · vendor master + **bank-change dual control and BEC signals** (the AFP data makes this a buying criterion, not a nice-to-have) · QuickBooks/NetSuite/Dynamics connectors · SSO · KPI dashboards · **first country pack** (India GST/IRN/2B or ANZ ABN/Peppol — decide by design-partner geography) · SOC 2 Type I readiness.

### Phase 4 — Differentiation (Months 7–12, PRD P3)

Peppol PINT A-NZ + EU formats via a **certified AP partner** · mandate-calendar engine · multi-entity/multi-currency · vendor portal · payments via **licensed partner** · governed autonomy (auto-code/auto-approve inside SME rules with the critic as guardrail) · EU AI Act pack · ISO 27001.

### Critical files

| File | Role in this plan |
|---|---|
| `invoice_processing/shared_libraries/alf_engine.py` | Splits into `invoice_core/rules/*`. Holds `ConditionEvaluator` (:207), `ActionExecutor` (:554), `RuleAggregator` (:1287), `ALFEngine` (:1765), the `save_rule_base` data-loss bug (:2051), and one of the two divergent context builders (:2161). |
| `invoice_processing/core/case_loader.py` | The other divergent builder. Becomes a `CaseRepository` adapter with its context logic deleted. |
| `invoice_processing/core/impact_assessor.py` | `random.sample` (:115-122); primary consumer of the unified context builder. |
| `invoice_processing/core/config.py` | Path root #1, `sys.path.insert` hack (:38), `ARTIFACT_MAP`. Collapses into `EngineSettings`. |
| `invoice_processing/core/prompts.py` | Hand-maintained field list (:65-101) → generated `describe_fields()`. |
| `invoice_processing/shared_libraries/acting/general_invoice_agent.py` | Duplicated `DEFAULT_CONFIG` (:139-168), stubs (:1106, :1156), extraction schema (:226-238) and prompt (:839-864). |
| `invoice_processing/shared_libraries/investigation/investigate_agent_reconst.py` | Cost redesign: triple-check loop, the six Layer-1 validators, untenanted cache key. |
| `on-brand-genmedia/frontend/src/components/{AppShell,Chrome,GoogleSignIn}.tsx` | Reuse for the workbench — same Next.js 16 / React 19 / Tailwind 4 stack you already run. |

---

## Part D — Verification

Run at every phase boundary; the first three are the regression net for the whole refactor.

```bash
cd /Users/vineet/Downloads/ADK/invoice-processing
./.venv/bin/python -m pytest tests/ -q                     # must stay ≥170 passing
./.venv/bin/python -m ruff check .
./.venv/bin/python eval/eval.py --ground-truth invoice_processing/exemplary_data \
    --agent-output invoice_processing/data/agent_output --skip-llm
```

- **Behaviour preservation (Phase 0):** the 3 reference cases must produce byte-identical decisions and findings before and after the refactor. case_001 REJECT@P3 (tax ID), case_002 ACCEPT, case_005 REJECT@P4 (WAF) → ALF-001 → Pending Payment.
- **Bug fixes:** each of the seven A3 bugs gets a test that fails when the fix is reverted (mutation-check it, as we did for Step 4.4).
- **Critic cost:** instrument LLM calls per case; assert ≤2 on the sampled path in CI.
- **Impact assessment:** same rule + same corpus → identical report across runs (this is the test that proves the safety claim).
- **Tenant isolation (Phase 1+):** automated suite hitting every endpoint as two tenants across all roles; any cross-tenant read fails the build.
- **Eval gate (Phase 1+):** `eval.py` returns a non-zero exit code and threshold flags so CI blocks on a >0.5pt accuracy regression. Today `main()` always exits 0 — that must change.
- **End-to-end (Phase 1+):** Playwright over upload → extract → review → correct → approve → export.

---

## Part E — Risks and open decisions

| Risk | Mitigation |
|---|---|
| **Solo capacity vs full PRD scope** | The plan front-loads truth-telling and the pilot. The visible hiring trigger is Phase 3: workflows + matching + connectors + a country pack in parallel is where one person stalls. Plan to add an engineer by ~month 4, or extend Phase 3 to 6 months. |
| **Refactor stalls behind a big-bang migration** | 0.2 ships local adapters first so tests stay green; GCS/Postgres implementations slot in later behind the same ports. Never leave the system unrunnable overnight. |
| **Extraction accuracy on real partner invoices** | Unknown until Phase 2 — the 3 sample cases prove nothing about accuracy. If confidence can't drive ≥50% auto-routing on pilot data, add Document AI as an ensemble parser behind the same interface rather than fighting prompts. |
| **Vertex AI intermittent hangs** | Observed twice (15–25 min) in this session. Add timeouts, retry with backoff and jitter, and a circuit breaker in the LLM client port during 0.2. The acting pipeline currently has **no retry at all** (only a blanket 1s sleep); the investigation agent already has a backoff helper to copy. |
| **Rule sprawl degrades decisions** | Already partly designed: conflict detection, scope exclusion. Add shadow mode, override-rate telemetry, and the promotion review from PRD FR-ALF-3/4/5. |
| **Context unification (0.3) can silently change production verdicts** | The `has_waf` replay script is the deliverable, not an extra. Without it you're guessing which of two behaviours your existing rules depend on. Same treatment for `line_items_mapped`, which both builders set. |
| **Critic redesign will change verdicts** | Deterministic checks won't reproduce the LLM's fuzzy agreement — that's the point, but a partner may see a case flagged today that wasn't yesterday. Run old and new side by side over the golden fixture for a week and diff before switching. |
| **Three cases can't validate impact assessment** | The entire "measured blast radius" story is untested at scale. Prioritise getting real case volume from partner #1 over building impact-assessment UI. |
| **Prompt injection from documents** | Documents are untrusted input. Enforce PRD NFR-SEC-5 in Phase 1: schema-validated outputs only, and the LLM never on the write path to an ERP or payment. |

**Decisions to confirm before Phase 1:**
1. **Host** — recommending all-GCP (Cloud Run/Cloud SQL/GCS); Railway is viable for the web tier if you prefer speed. 
2. **First country pack** — deferred by your "generic first" choice, but Phase 3 needs it chosen; drive it off design-partner geography.
3. **Design partners** — who are the 2–3? Their ERP and country determine 1.10 and the Phase 3 pack.
4. **Learning-mode agent hosting** — keep the conversational agent on Agent Engine (it fits) or embed in the platform. PRD open question #4.

