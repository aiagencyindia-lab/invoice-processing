# v2 Wave 4 — Agent control plane (built dark): Program Plan

**Spec:** `docs/superpowers/specs/2026-10-14-v2-trust-layer-design.md` §2 (Wave 4 row), §3 (`trust/agents/`), §3.1. PRD FR-28.1–28.6, AC-28, §9 (`/v2/agents*`, `agent.action.blocked`), §10 (`agent`, `agent_mandate`, `agent_action`), ADR-16.
**Builds on:** `main` @ `31763c8` (Waves 0–3 + #70–#82). Alembic head `a1b2c3d4e5f6`.
**Shape:** as Waves 1–3 — one sequential scaffold branch `v2/w4-scaffold` owning every shared file, then four parallel worktree branches with disjoint ownership; each: plan → TDD → fable review → one fix pass → PR; merge on the user's word with `--match-head-commit`, never `--auto`. Ledgers in `.superpowers/sdd/<plan>/progress.md` **in the main checkout**.

**On approval of this plan:** commit it as `docs/superpowers/plans/2026-10-22-v2-wave4-program.md`, write the five specs (`docs/superpowers/specs/2026-10-22-v2-wave4-{scaffold,registry,policy,behaviour,simulation}-design.md`; builder-check each), get the user's approval of the specs, then `superpowers:writing-plans` for the scaffold.

## Context

Nothing in the product knows what an agent is. The one ADK agent (`ip/agent.py:520`) only extracts; every business write is a person, one of three fixed machine actors (`worker`, `system`, `inbound-email`), or an API key (`ip/trust/apikeys.py`, role ≤ approver, `user_id="api-key:{id}"`). There is no choke point for writes: each of the six governed handlers calls its repository then `get_audit().emit()`. Wave 4 adds a registry with two-person mandates, one policy function every governed write asks first, a decision ledger, behavioural detectors over that ledger, and a simulated fleet that gates AC-28 — with no screen, and no behaviour change until an admin registers an agent **and** turns the engine on. (`ip/` = `invoice_processing/`, `fe/` = `frontend/src/`.)

## Decisions taken with the user (2026-10-22)

| # | Decision | Why |
|---|---|---|
| U1 | **Inline on our own write paths** (PRD Q6 answered; ADR-16): `evaluate_action` runs before the write on vendor create/patch, bank-change propose/decide, invoice approve/reject, export. Log-based evaluation is a thin import feeding the same function; decisions recorded after the fact. | Where we are in the path we block; elsewhere we evaluate from logs (FR-28.2). ERP observers wait on partner stacks. |
| U2 | **Registered only**: an actor is an agent once an admin registers it with a mandate. Worker, inbound-email, unregistered keys behave as today. Zero change with the engine off. | No surprises for tenants that never register one. |
| U3 | **BLOCK in enforcing → 403 with explanation, nothing written**, plus an `agent_actions` row, a finding and webhook `agent.action.blocked`. ALLOW-WITH-REVIEW proceeds + finding. Shadow/advisory record and proceed. | FR-28.2; refusal is the lever, not a hold. |
| U4 | **Mandate grant = two-person**: admin proposes, a *different admin* approves (bank-change dual-control pattern, `repositories.py:1273` `SelfApproval`). No active mandate → every action BLOCK. | FR-28.5 "what a human authorised"; PRD §4 Controller approval. |
| U5 | **All behavioural detections in scope**: circular/round-trip, threshold splitting, action velocity, prompt-injection indicators (deterministic patterns; `decide()` only to classify). | FR-28.4 in full. |
| U6 | **Dark = API + engine only**: `/api/trust/agents*` and `/v2/agents*` exist, engine default `off`, no frontend view, no NAV entry; the Trust engines page hides an engine that is `off` with no readiness data (one client-side filter). | Release-gated (V2-C). |
| U7 | **Simulation harness = benchmark fault family** `benchmark/history/agents.py` with AC-28 gates (100% structural blocked, ≥90% behavioural flagged, 0 false blocks on the compliant set). | Same discipline as every engine. |
| U9 | **An owner and their agent are one approver, both ways.** An agent is blocked when its owner already decided the chain (or initiated it); a person is refused at their own approve/reject when an agent they own already decided that chain. The second check is the only time the guard touches a human, and only on chains an agent of theirs took part in. | One person must not satisfy two-person approval. |
| U10 | **A mandate names one currency.** An approve in another currency is BLOCK `AGENT-OUT-OF-MANDATE`; spend sums rows in that currency only. No FX. | Strict, simple, no base-currency setting exists. |
| U8 | **API keys stay ≤ approver** (`apikeys.ROLES` unchanged). Vendor create/patch guards are wired, but fire only for a registered human-admin service user. | A machine credential still cannot mint keys or change settings. |

## Builder's calls (mine; say so in the specs)

| # | Call | Why |
|---|---|---|
| B1 | **Agent identity = principal.** `agents.principal_id` = Principal `user_id` (`"api-key:{id}"` or a Firebase `sub`), unique per tenant, nullable for `kind=external`; `principal_email` denormalised at registration because every v1 record of who did what stores an email (`approval_steps.decided_by`, `vendor_bank_changes.proposed_by`, `documents.source_ref`). No column on `api_keys`, no change to `Principal`/`RunContext`. | Smallest binding; self/chain checks compare the emails v1 already wrote. |
| B2 | **One decision ledger; detection never reads the graph.** `agent_actions` is written for every evaluated action (inline, log, dry-run). Rules read the ledger and v1 tables by `vendor_key = po_matching.normalize_vendor(name)` (the key `vendors.normalized_name` holds). The graph gets an `agent` node per agent and an `acted_on` edge per allowed inline action for lineage/explanations only. | `sync_tenant` runs later in the worker; the approve call must not wait on it. |
| B3 | **Decision ledger, not execution ledger.** The row is written before the governed write. A write that then fails still counts toward spend (conservative). Audit events remain the execution record. | One write path; caps never under-count. |
| B4 | **Mandates signed with HMAC-SHA256 over canonical JSON** (`sort_keys=True, separators=(",",":")`), key = `hmac(TRUST_MANDATE_KEY, tenant_id)` (per-deployment env, per-tenant derivation, nothing stored); payload includes `prev_digest` of the agent's previous version, `valid_from/valid_to`, `approved_by/approved_at`. `verify(row)` recomputes; `GET …/mandates` returns `verified`. Approval → 503 while the key is unset. Honest claim: tamper-evidence against DB edits, not non-repudiation. | stdlib, zero storage, time-bound (FR-28.5). External mandate-format adapters deferred. |
| B5 | **Engine `agents` in `ENGINES`, not `HOLD_ENGINES`; every `AGENT-*` type `hold=False`, dimension `authorization`.** The guard reads its mode via `engine_mode()`; `trust/agents/__init__.py::run(ectx)` returns `[]` (actions are evaluated when they happen, not per invoice). New `modes.BLOCKING_ENGINES = frozenset({"agents"})`; `score/readiness.insight_only()` treats it as not insight-only ("enforcing refuses blocked agent actions"). | Reuses mode storage, `set_engine_mode`, audit, readiness — without faking a per-invoice engine. |
| B6 | **One finding per non-ALLOW decision**, type = first violated rule in severity order, `details.reasons` = all; dedupe key `agent_action:{action_id}`; citations = the `agent` node + the subject's node when the graph has it (else the agent node alone — satisfies the DB citation rule). `agent.action.blocked` is the only new webhook event; reviews ride `finding.created`. | Minimal types and events; explanations get the full list. |
| B7 | **Engine-off fast path = one `tenants` PK read, no cache.** `engine_mode()` → `PgTenantSettings.get()` is one SELECT, the same cost `approval_flow.payment_hold` already pays. Agent lookup (indexed `(tenant_id, principal_id)`) only when the mode is not `off`. Upgrade: 30 s process-local TTL in `engine_mode` if p95 shows it. | "Zero change with the engine off" is behaviour, not query count. |
| B8 | **Fail closed only in enforcing.** A guard exception (DB down) in `enforcing` refuses the action with 503; in shadow/advisory it is logged and the action proceeds. Unregistered actors are never affected. | Same contract as holds (spec §3.1.4). |
| B9 | **Spend caps count `invoice.approve` only**; `invoice.export` is reported as "paid" in the ledger, never summed (one invoice, one amount). Period = calendar month or day, UTC. Category = `vendors.kind`. | FR-28.6 without double counting. |
| B10 | **Structural → BLOCK, behavioural → ALLOW-WITH-REVIEW** (incl. circular and injection). AC-28 counts "blocked" for the first set and "flagged" (non-ALLOW) for the second. | FR-28.3 are controls; FR-28.4 are detections with false-positive risk. |
| B11 | **An agent never decides an agent-proposed bank change**, whatever the owner (strict reading of FR-28.3 "always require human verification"). | Safer reading; relax later if a partner needs it. |
| B12 | **Guard for approve/reject runs before `approval_flow.decide`**; a decision is ledgered even when routing then refuses the step. Counterparties keyed by normalised vendor name (two vendors with one name share a key — PENDING note). Injection inputs = PDF text layer + email body only; no OCR (PENDING note). AC-28's "within 60 s" is the fleet's elapsed time (synchronous import, honest proxy). | Each is the smallest honest option; none changes what gets built. |

## Part 1 — Scaffold (`v2/w4-scaffold`, sequential, merged first)

**S1 Migration + models.** `ip/db/migrations/versions/b2c3d4e5f6a7_trust_wave4.py` (revises `a1b2c3d4e5f6`; own `RLS_TABLES`; copy the Wave 3 helpers). Three tables in `TENANT_TABLES` (`ip/db/models.py`): `Agent`, `AgentMandate`, `AgentAction`.

| Table | Columns (beyond id, tenant_id, stamps) |
|---|---|
| `agents` | `name(128)`, `owner(320)` idx, `principal_id(64)` null (unique `(tenant_id, principal_id)`), `principal_email(320)` idx, `kind(16)` api_key/user/external, `description(500)`, `status(16)` active/revoked, `registered_by(320)`, `revoked_at`, `revoked_by(320)`, `revoke_reason(500)`, `graph_node(36)` |
| `agent_mandates` | `agent_id` FK idx, `version` int (unique `(agent_id, version)`), `scope` JSON `{actions:[…], counterparties:[vendor_key…]|null, caps:{per_action, per_period:{period: month|day, amount}, per_vendor, per_category:{kind: amount}}}` (amounts as strings → `Decimal`), `valid_from`, `valid_to`, `status(16)` proposed/active/revoked (expired computed on read), `proposed_by(320)`, `approved_by(320)`, `approved_at`, `revoked_by`, `revoked_at`, `reason(500)`, `prev_digest(64)`, `digest(64)`, `signature(64)` |
| `agent_actions` | `agent_id` FK idx, `mandate_id(36)` null, `mandate_version` int null, `action(32)`, `subject_table(32)`, `subject_ref(128)` idx, `transaction_node(36)` null, `vendor_key(128)` idx, `category(64)`, `amount` Numeric(18,4) null, `currency(8)`, `decision(20)`, `reasons` JSON `[{rule,type,severity,message}]`, `explanation(1000)`, `mode(16)`, `source(8)` inline/log/dryrun, `occurred_at` (index `(tenant_id, agent_id, occurred_at)`), `finding_id(36)` null, `request_id(64)` |

No spend table/view: sums are `GROUP BY` in `AgentStore.spent()`. Tests: `tests/test_trust_schema.py`; `tests/test_gcs_and_migrations.py` param `("b2c3d4e5f6a7", 3)`; `tests/test_restore_check.py` pin → `"b2c3d4e5f6a7"`.

**S2 Types + modes + settings + events.** `ip/trust/types.py` TABLE += nine types, all `("agents", False, "authorization", …)`:

| Code | Severity | Meaning |
|---|---|---|
| `AGENT-OUT-OF-MANDATE` | BLOCK | No active mandate (none/proposed/revoked/outside validity), agent revoked, action not in `scope.actions`, counterparty not in `scope.counterparties` |
| `AGENT-SELF-APPROVAL` | BLOCK | Agent decides what it initiated (uploaded the document; proposed the bank change) |
| `AGENT-SAME-OWNER-CHAIN` | BLOCK | A prior approver on the chain (or the bank-change proposer) is an agent with the same owner |
| `AGENT-OVER-LIMIT` | BLOCK | Amount, or amount + period/vendor/category spend, exceeds the cap |
| `AGENT-UNVERIFIED-VENDOR` | BLOCK | Approve/export for a vendor any agent created whose bank is not human-verified; an agent deciding an agent-proposed bank change (B11) |
| `AGENT-CIRCULAR` | REVIEW | Vendor created by this agent or one with its owner, invoice approved by this agent |
| `AGENT-SPLIT` | REVIEW | ≥ `agents.split_min_count` approvals for one vendor by one agent inside `agents.split_window_days`, each under the per-action cap, together over it |
| `AGENT-VELOCITY` | REVIEW | More than `agents.velocity_max` actions in `agents.velocity_window_minutes` |
| `AGENT-INJECTION` | REVIEW | Instruction-like text in the document/email the action touches |

`ip/trust/modes.py`: `ENGINES += ("agents",)`, `BLOCKING_ENGINES` (B5). `score/readiness.py insight_only` honours it. `tests/test_trust_findings.py` engine assertions updated. `webhooks/__init__.py EVENTS += ("agent.action.blocked",)`. `thresholds.DEFAULTS` += `agents.velocity_window_minutes 10, agents.velocity_max 20, agents.split_window_days 7, agents.split_min_count 2, agents.injection_min_hits 1` → `tests/test_trust_settings.py` snapshot. No new `SETTABLE` key (caps live in mandates). Frontend: `ENGINE_LABELS.agents = "Agent control"` in `fe/lib/trust.ts` + the U6 filter in `fe/app/trust-engines` (hide engines that are `off` with no readiness data); `tests/test_frontend_reaches_the_flow.py` unchanged (no new view).

**S3 Package + store.** `ip/trust/agents/__init__.py`: `ENGINE_VERSION="agents-1"`, `ACTIONS = ("vendor.create","vendor.patch","bank_change.propose","bank_change.decide","invoice.approve","invoice.reject","invoice.export")`, `DECISIONS`, `run(ectx) -> []`, registers the nine codes. `ip/trust/agents/store.py` `AgentStore(url)`: `register`, `get`, `by_principal`, `by_emails`, `list`, `revoke`, `propose_mandate` (version = max+1, `prev_digest`), `approve_mandate` (raises `SelfApproval` when approver == proposer; supersedes the prior active), `revoke_mandate`, `current_mandate(tenant, agent_id, now)`, `record_action(s, …)` (session-taking so the outbox row shares it), `actions(...)`, `spent(...) -> Decimal`, `count_since`, `creators_of(vendor_key)`. Also `canonical(row)`/`digest(row)` (B16) so `propose_mandate` can chain `prev_digest`. TDD `tests/test_trust_agents_store.py`.

**S4 Policy + guard seams + record.** `ip/trust/agents/policy.py`: frozen dataclasses `Action` (kind, subject_table, subject_ref, vendor_key, category, amount, currency, transaction_node, initiated_by, prior_deciders, vendor_verified, vendor_verified_by, `text: Callable[[], str] | None` lazy loader, occurred_at), `Reason(rule, type, severity, message)`, `Decision(decision, reasons, explanation, action_id, agent_id)`; `evaluate_action(ctx, action, *, agent, mandate, store, settings, now, mode="advisory", decider=None) -> Decision` (`mode` decides whether a rule that raises blocks: only in enforcing) composes `rules.RULES + behaviour.RULES` (each `fn(action, agent, mandate, store, settings, now, decider) -> Reason | None`): BLOCK if any BLOCK, else REVIEW if any, else ALLOW; explanation one sentence per reason, plain text (the API returns JSON; pages escape it). Scaffold ships `rules.py`/`behaviour.py` with `RULES: tuple = ()`. `ip/trust/agents/record.py::record(ctx, decision, action, *, agent, mandate, store, findings, graph, mode, source, refused, now) -> action_id` (`refused` = the action was actually stopped; the agent must have a graph node — build `AgentStore(url, graph=GraphStore(url))`) writes the ledger row, then the finding, then stamps `finding_id` and emits `agent.action.blocked` (B13), and the `acted_on` edge for an allowed inline action. `ip/trust/agents/guard.py::check(principal, action, *, settings_store=None, now=None) -> Decision | None` stub returning `None`. `api/trust_agent_routes.py`: 503 stubs with fixed names (`register_agent, list_agents, get_agent, revoke_agent, propose_mandate, list_mandates, approve_mandate, revoke_mandate, evaluate_action, get_ledger, tenant_ledger, import_actions`), mounted from `service.py` with `get_agent_store()`. `explain/templates/agents.py` (empty) + import line.

**S5 Benchmark slot + isolation + docs.** `benchmark/history/eval.py`: `_measure_modules()` (the existing Wave 3 plug-in: `measure(tenant_id, graph, findings, manifest, url) -> {..., gates}`) also looks for `agents`; branch D ships `benchmark/history/agents.py`. `tests/test_tenant_isolation_api.py`: PROBED += twelve stub paths; `EXTRA_PROBE_MODULES += ("tests.test_trust_agent_routes",)`; route-module tuple += `trust_agent_routes`. `docs/API_V2.md` untouched (D adds routes and rows together). `docs/PENDING.md` Wave 4 bullets.

## Part 2 — Parallel branches

### A. `v2/w4-registry` — Registry, mandates, ledger, routes (FR-28.1, 28.5, 28.6)
Owns `ip/trust/agents/{registry,mandates,ledger}.py`, `api/trust_agent_routes.py` (fills), `tests/test_trust_agent_routes.py` (exports `PROBED`), `tests/test_trust_agents_mandates.py`.
- Registry: `register(ctx, body, *, store, apikeys_url)`: `{"api_key_id"}` → tenant's live key → `principal_id="api-key:{id}"`, `principal_email=f"{key.name}@api-key.{tenant_id}"` (mirrors `apikeys.resolve`); `{"user_id","email"}` as given; absent → `kind="external"` (log-only). Audits `agent_registered/agent_revoked`.
- Mandates: `canonical`, `sign`, `verify` (B4; `TRUST_MANDATE_KEY`, 503 `MandateKeyMissing`); `validate_scope` (actions ⊆ `ACTIONS`, caps non-negative `Decimal`, `valid_from < valid_to`, counterparties normalised). Approve = different admin, re-signs. Audits `agent_mandate_proposed/approved/revoked`.
- Ledger: `agent_ledger(...) -> {spend:{approved, exported, by_vendor, by_category, period:{cap, used, remaining}}, actions, exceptions}`; `tenant_ledger` by owner.
- Routes (`Admin` = human admin, 403 for key principals, as `integration_routes`; reads = approver; `evaluate` = approver or the agent's own principal for its own id):

| Method | Path | Role |
|---|---|---|
| POST | `/api/trust/agents` | Admin |
| GET | `/api/trust/agents?status` | approver |
| GET | `/api/trust/agents/{agent_id}` | approver |
| POST | `/api/trust/agents/{agent_id}/revoke` | Admin |
| POST | `/api/trust/agents/{agent_id}/mandates` | Admin |
| GET | `/api/trust/agents/{agent_id}/mandates` | approver |
| POST | `/api/trust/agents/{agent_id}/mandates/{mandate_id}/approve` | Admin ≠ proposer |
| POST | `/api/trust/agents/{agent_id}/mandates/{mandate_id}/revoke` | Admin |
| POST | `/api/trust/agents/{agent_id}/actions/evaluate` | approver / own — `{action, subject_ref, vendor_key?, category?, amount?, currency?, occurred_at?, source: dryrun\|log}` |
| GET | `/api/trust/agents/{agent_id}/ledger?since&until` | approver |
| GET | `/api/trust/agents/ledger?since&until` | approver |
| POST | `/api/trust/agents/actions/import` | Admin — multipart CSV/JSON via `history/readers.read_table`; columns `agent, action, subject_ref, vendor, category, amount, currency, occurred_at`; each row → `evaluate_action` + `record(source="log")` |
- Tests: two-tenant probes (bearer and foreign key); self-approval 403; proposed mandate → BLOCK; expiry; signature verifies and fails after a column edit; import happy path + bad rows; ledger sums.

### B. `v2/w4-policy` — Structural rules + interceptor wiring (FR-28.2, 28.3)
Owns `ip/trust/agents/rules.py`, `ip/trust/agents/guard.py` (fills), the six call-site edits in `api/vendor_routes.py` and `api/service.py`, `tests/test_trust_agents_rules.py`, `tests/test_trust_agents_guard.py`.
- `guard.check`: `mode = engine_mode(tenant, "agents")`; `off` → `None`. `agent = store.by_principal(tenant, principal.user_id)`; `None` → `None`. `mandate = store.current_mandate(...)`; `decision = evaluate_action(...)`; `record(...)`; `enforcing and BLOCK` → `HTTPException(403, {detail: explanation, action_id, reasons})`; exceptions: enforcing → 503, else log + `None` (B8). No ids in log `extra`.
- Call sites (build `Action`, call `guard.check`, then the existing write):

| Handler | Where | Action |
|---|---|---|
| `replace_vendors` `vendor_routes.py:183` | after `_validate_list`, before `upsert_many` | one `vendor.create` per record (all checked before any write) |
| `patch_vendor` `:274` | before `repo.patch` | `vendor.patch` |
| `propose_change` `:300` | before `repo.propose` | `bank_change.propose` |
| `decide_bank_change` `:233` | before `repo.decide` | `bank_change.decide`, `initiated_by=proposed_by` |
| `act` `service.py:1605` | after artifacts read, before `_final_approval_lock` | `invoice.approve`/`reject`, `amount=total`, `initiated_by` = uploader/sender, `prior_deciders` from chain, `vendor_verified*` from vendors, `transaction_node` via `nodes_by_ref`, `text` loader = C's `inputs.text_of` |
| `export_invoice` `:2051` | after the hold check, before the connector | `invoice.export` |
- `rules.RULES` in severity order: `mandate_active`, `in_scope`, `self_approval`, `same_owner_chain`, `over_limit` (B9), `unverified_vendor` (B11). `RULE_VERSION="agents-rules-1"`.
- Tests: each rule; route-level: enforcing BLOCK → 403 and no row written; advisory → write + ledger row + finding; off → `AgentStore` never queried (monkeypatch to raise); unregistered key in enforcing → untouched; two agents of one owner on one chain → 403 on the second.

### C. `v2/w4-behaviour` — Behavioural detectors (FR-28.4)
Owns `ip/trust/agents/{behaviour,injection,inputs}.py`, `explain/templates/agents.py` (fills), `tests/test_trust_agents_behaviour.py`, `tests/fixtures/trust_agents/**`, a text helper in `provenance/pdfmeta.py` if `facts()` does not already expose page text.
- `behaviour.RULES = (circular, split, velocity, injection)`. `injection.scan(text)`: deterministic patterns ("ignore (previous|prior|above) instructions", "(system|assistant|developer) ?(prompt)?:", "you are (now )?(an?|the) (ai|assistant|agent)", "approve … (immediately|without)", "do not (flag|hold|review)", ≥3 zero-width chars, base64 runs ≥64) → ≥ `injection_min_hits` → `decide(...)` classification with `fallback=True` (fail toward review). Excerpts masked via `masking.mask_text`.
- `inputs.text_of(document, blobs)`: PDF text layer + email body (`payment.signals.parse_email`); no OCR.
- Templates for the nine codes; `Explanation.check()` passes for every fixture.
- Tests: each detector positive/negative; injection corpus (10 injected, 20 clean) with zero false hits.

### D. `v2/w4-simulation` — Fleet harness, `/v2` façade, docs (AC-28, §9)
Owns `benchmark/history/agents.py`, `api/v2_routes.py` (four routes), `docs/API_V2.md` (agents section), `tests/test_trust_agents_sim.py`, `tests/test_trust_api_routes.py` (PROBED += four `/v2/agents*`).
- `benchmark/history/agents.py`: `measure(tenant_id, graph, findings, manifest, url)` registers a fleet on the bench tenant (3 compliant, 5 rogue; two admins), drives scripts through `evaluate_action` + `record(source="inline")` (no HTTP): compliant set; violations — self-approval, same-owner chain, over-limit, unapproved/expired mandate, out-of-scope counterparty, agent-created vendor approved unverified, circular, split (3×40k under 100k in 7 days), velocity (25 in a minute), injection. Returns `{structural:{n,blocked}, behavioural:{n,flagged}, compliant:{n,false_blocks}, elapsed_s, gates:{agents_structural_blocked ==1.0, agents_behavioural_flagged ≥.9, agents_false_blocks ==0, agents_latency_60s}}`.
- `/v2`: `POST /v2/agents`, `POST /v2/agents/{agent_id}/mandates`, `POST /v2/agents/{agent_id}/actions/evaluate`, `GET /v2/agents/{agent_id}/ledger` (thin façade; the POSTs need a human admin, documented). `docs/API_V2.md` rows keep `test_the_doc_lists_every_v2_route_and_nothing_else` green.
- Tests: `simulate` meets all four gates on SQLite; `/v2` == `/api` parity; probes.

## File ownership (every file → one owner)

| Owner | Files |
|---|---|
| scaffold | migration `b2c3d4e5f6a7_trust_wave4.py`, `db/models.py`; `trust/{types,modes,thresholds}.py`, `trust/webhooks/__init__.py`, `trust/score/readiness.py`, `trust/audit/report.py`, `trust/agents/{__init__,store,policy,record}.py`, `trust/agents/{rules,behaviour}.py` (empty `RULES`), `trust/agents/guard.py` (stub), `explain/templates/{__init__,agents}.py`, `api/service.py` (mount + getter), `api/trust_agent_routes.py` (503 stubs); `benchmark/history/eval.py`; `tests/{test_trust_schema,test_gcs_and_migrations,test_restore_check,test_trust_settings,test_trust_findings,test_tenant_isolation_api,test_benchmark_history,test_trust_agents_store}.py`; `fe/lib/trust.ts`, `fe/app/trust-engines/page.tsx` (filter); `docs/PENDING.md`, the five specs |
| registry (A) | `trust/agents/{registry,mandates,ledger}.py`, `api/trust_agent_routes.py` (fills), `tests/test_trust_agent_routes.py`, `tests/test_trust_agents_mandates.py` |
| policy (B) | `trust/agents/rules.py`, `trust/agents/guard.py` (fills), `api/vendor_routes.py` + `api/service.py` call-site edits only, `tests/test_trust_agents_rules.py`, `tests/test_trust_agents_guard.py` |
| behaviour (C) | `trust/agents/{behaviour,injection,inputs}.py`, `explain/templates/agents.py` (fills), `provenance/pdfmeta.py` (text helper only), `tests/test_trust_agents_behaviour.py`, `tests/fixtures/trust_agents/**` |
| simulation (D) | `benchmark/history/agents.py`, `api/v2_routes.py`, `docs/API_V2.md`, `tests/test_trust_agents_sim.py`, `tests/test_trust_api_routes.py` |

Contentions resolved: `policy.py` composes `rules.RULES + behaviour.RULES` so B and C never share a file; `record.py` is the one writer of ledger + finding + webhook + edge, used by B's guard, A's evaluate/import routes and D's fleet; `service.py` — scaffold mounts, B edits only `act`/`export_invoice`. A needed shared change = scaffold follow-up PR on `main`, then the branches merge `main`.

## Execution mechanics

1. Program plan + five specs committed on `v2/w4-scaffold`; scaffold plan via `superpowers:writing-plans`; TDD; fable review; one fix pass; PR; user's "merge".
2. Four parallel Claude sessions (`start_session`, `use_worktree: true`) from the merged scaffold, branches `v2/w4-{registry,policy,behaviour,simulation}`; `uv sync --frozen`; Postgres DBs `trust_w4_<name>` on `pg-w1`. This session answers cross-session questions and does every merge.
3. Each branch: own plan doc, TDD, ownership row in the brief, repo rules (no ids/secrets in log `extra`, masked excerpts, `Decimal` money, every route probed, HTML via `trust/html.py`), ruff + suite + Postgres (+ eval for D) → fable review → one fix pass → PR.
4. **Merge order:** scaffold → A, B, C (any order, each with `main` merged and CI green on the exact head) → D last (its fleet needs B's rules and C's detectors; its façade imports A's handlers).
5. Wave close: gates green on `main`; worktrees removed; PENDING + memory updated; close-out PR; audit against PRD/specs with a live smoke.

## Verification (wave exit)

- `main`: full SQLite suite; Postgres `tests/test_trust_*.py`; `alembic upgrade head && downgrade base`; `restore_check.repo_head() == "b2c3d4e5f6a7"`; `python -m benchmark.history.eval --seed 7`: every prior gate + `agents_structural_blocked`, `agents_behavioural_flagged`, `agents_false_blocks`, `agents_latency_60s`; explanation citation coverage still 1.0; `npm run lint && npx tsc --noEmit && npm run build && npm run test:e2e` (47 unchanged).
- Isolation: every new path in a `PROBED` set with a two-tenant probe; `/v2/agents*` probed with bearer and foreign key; registry mutations 403 for key principals.
- Dark check: with `agents` off and no agent registered, the six governed routes' test modules are unchanged; `AgentStore` never queried (guard test); Trust engines page does not list `agents`.
- Live smoke (local, `AUTH_DISABLED=1`, worker running, `TRUST_MANDATE_KEY` set): create approver key → register as agent (owner `ops@acme`) → propose mandate (`invoice.approve`, one counterparty, per_period 100 000) → approve as a second admin (own approval → 403) → `GET …/mandates` shows `verified: true` → engine `shadow` → key approves an in-scope invoice → ledger ALLOW, nothing else changes → engine `enforcing` → key approves an invoice it uploaded → 403 with explanation, `AGENT-SELF-APPROVAL` finding, `agent.action.blocked` delivered with a verifiable signature, `approval_steps` untouched → 60k then 60k → second is `AGENT-OVER-LIMIT` → import a CSV log with a rogue row → `source="log"` decisions → `/v2/agents/{id}/ledger` with the key equals `/api/trust/agents/{id}/ledger` → `/trust-engines` does not show the engine while off.

## Non-goals / deferred (→ PENDING)

- UI: no agents view, NAV or dashboard (U6).
- ERP/third-party observers (SAP Joule, Oracle, Dynamics): file import only; no live adapters.
- Mandate-standard adapters; notifications on blocks (webhook only).
- Governance of PO creation and document upload (upload is `initiated_by`, not evaluated); payment execution ("paid" = exported).
- Holds from log-based BLOCKs; mandate expiry sweep; `agent_actions` retention; rate limits / idempotency on `/v2`; OCR text for injection scanning; `executed_at` on the ledger (B3); vendor-name key collision (B12).

## Rulings after the builder check (2026-10-22)

User: U9, U10 above. Mine:

| # | Call | Why |
|---|---|---|
| B13 | **`record()` is ordered, not atomic.** `FindingStore`/`GraphStore` open their own sessions and stay untouched: write the ledger row; upsert the finding; stamp `finding_id` and emit `agent.action.blocked` in one session. A crash between leaves a ledger row without a finding (the decision still stands; the guard's answer does not depend on the finding). A BLOCK fires both `finding.created` and `agent.action.blocked`. | No change to hot shared stores; the gap is visible, not silent. |
| B14 | **Finding `transaction_node`** = the invoice node when the graph has it, else the agent node. Agent findings on the agent node are not in any invoice's score; on an invoice node they land in its `authorization` dimension. | `trust_findings.transaction_node` is NOT NULL. |
| B15 | **Sources:** `dryrun` rows are questions — never counted (spend, velocity, split, `creators_of`), never a finding or webhook. `log` rows are real past actions — counted, findings raised, windows measured at the row's `occurred_at` (never `now`). `agent.action.blocked` fires only when an action was actually refused (enforcing, inline); a shadow/advisory BLOCK or a log BLOCK raises the finding only. | A question must not spend; "blocked" must mean blocked. |
| B16 | **Mandates: one pending version per agent** (propose → 409 while one is `proposed`); approve refuses any version that is not the latest. `canonical`/`digest` live in the scaffold's store; branch A owns `sign`/`verify`. `verify` also checks `prev_digest` against the previous version (chain). | Keeps the chain load-bearing. |
| B17 | **Caps are hard limits:** the guard takes a per-agent lock (`SELECT … FOR UPDATE` on the `agents` row; SQLite serialises anyway) around `spent()` + insert. | Two parallel approvals must not both fit one cap. |
| B18 | **AC-28 counts labelled violating actions**: the fleet labels exactly the actions that should be blocked/flagged (velocity actions 21–25; split's over-the-line approval), not every scripted action. | The gate measures detection, not script size. |
| B19 | Scaffold restores the stub-probe hand-over (removed at Wave 3 close, 5da399c; restore from 4f836b3). Import reads CSV/XLSX via `history/readers.read_table` (no JSON). Call sites may add the one lookup they need (`get_change`, vendor by id). Registration refuses an API key whose derived email (`{name}@api-key.{tenant}`) is shared with another live key. | Code facts from the check. |
