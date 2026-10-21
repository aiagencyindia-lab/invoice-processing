# v2 Wave 3 — Always-on: Program Plan

**Spec:** `docs/superpowers/specs/2026-10-14-v2-trust-layer-design.md` §2 (Wave 3 row), §3.1. PRD FR-22 (rest), FR-23, FR-25, FR-27 (file half), FR-30, §9.
**Builds on:** `main` @ `f96fe58` (Waves 0–2 + #67–#69). Alembic head `f4a5b6c7d8e9`.
**Shape:** as Waves 1–2 — one sequential scaffold branch `v2/w3-scaffold` owning every shared file, then five parallel worktree branches with disjoint ownership; each: plan → TDD → fable review → one fix pass → PR; merge on the user's word with `--match-head-commit`, never `--auto`.

## Context

Waves 0–2 detect and explain. Nothing yet makes prevention a subscription: holds are computed per call and invisible to the workbench, export recognises an override by string equality, modes are a raw settings dict with no evidence for promoting, there is no per-transaction score, no reality engine, no machine auth, no outbound events. Wave 3 adds exactly those with the smallest designs that fit existing seams. (`ip/` = `invoice_processing/`, `fe/` = `frontend/src/`.)

## Decisions taken with the user (2026-10-21)

| # | Decision | Why |
|---|---|---|
| U1 | **Pre-payment enforcement ships first**; score + progression are the spine. | First always-on tenant. |
| U2 | **Progression is operator-driven** with a per-engine readiness report + recommendation; admin promotes/demotes with a reason, audited; never automatic. | No surprises for customers. |
| U3 | **Reality holds only on CONTRADICTED**; NO_EVIDENCE / PARTIAL / plausibility are insights. | Tenants without GRN/ticket data are not held on everything. |
| U4 | **Evidence requests = internal tracker only** (finding → `needs_vendor_input`, owner, due date, proof attached as a document). No public link, no email. | No outbound channel exists. |
| U5 | **Per-tenant API keys** for `/v2`; webhooks HMAC-signed per endpoint. | Revocable machine identity. |
| U6 | **Connectors = adapters + fakes + file import** (ticket, timesheet, shipment, seat_count history kinds; `usage` capability). No live OAuth. | Scope. |
| U7 | **Trust Score everywhere**: per transaction (workbench + finding detail), vendor profile, one posture dashboard. | FR-25 in full. |

## Builder's calls (mine; say so in the specs)

| # | Call | Why |
|---|---|---|
| B1 | **Holds: `gate.payment_hold()` keeps computing** from open hold-type findings × enforcing engines; a **ledger table `trust_holds`** records placed/lifted so `hold.placed`/`hold.lifted` exist, posture counts prevented-pre-payment, and export compares finding ids, not strings. Written in exactly two paths: `evaluate()` after `upsert` when `mode=="enforcing" and type in HOLD_TYPES`; `FindingStore.transition(to="rejected")` lifts. `modes.set_engine_mode()` entering/leaving `enforcing` calls `holds.reconcile(engine)`. | Findings stay the truth; one writer per direction. |
| B2 | **Outbox rows written in the same `tenant_session`** as the finding/hold write; delivery is a worker sweep on the existing 60 s cadence (`Worker._escalate_if_due` pattern, `ip/worker/runner.py:1270`), not a queue job (backoff needs `next_attempt_at`). HTTP via `requests` (already a dependency); signature `X-Trust-Signature: sha256=HMAC(secret, f"{ts}.{body}")` with stdlib `hmac`. | Atomic, at-least-once, zero new deps. |
| B3 | **Score and reality results are graph nodes** (`trust_score:{invoice key}`, `reality:{invoice key}`), not tables: `GraphStore.upsert_node` already versions, masks and re-points edges; `?as_of` is free. Vendor profile and posture computed on read. | "Versions on everything" for free. |
| B4 | **Dimensions drive holds, composite never does.** Every hold is a finding of a hold type; the score only reports which dimension each open finding sits in; the gate never reads the score. | FR-25.1 by construction. |
| B5 | **Static type registry `ip/trust/types.py`** `{code: (engine, hold, dimension, description)}` registered at `findings/model.py` import; engines keep `register_type` and a test asserts they match. | The API process never imports engines (`hold_open`/engine filters are wrong there today). |
| B6 | **`/v2` is a thin façade** (`APIRouter(prefix="/v2")`, each handler calls the existing handler function). Transaction id = invoice graph node id. API-key auth = a resolver hook in `auth._resolve_principal` tried before Firebase; key `ik_{prefix}_{secret}`, SHA-256 at rest, prefix lookup through an engine-level session (never a tenant session). No rate limit / idempotency this wave. | No duplicated logic; probes inherit. |
| B7 | **Provenance v2 = more deterministic families** (`producer`, `edits` = incremental-save count, `fonts`) folded into the weighted score as `provenance-2`; KYC and proofs scored by the same `_ledger` with `invoice_node=None`; `Document.kind` gains `kyc` and `proof`. No copy-move, no ML. | Cheap, dependency-free. |
| B8 | **Frontend is one branch**; three new admin/viewer views (`/trust-posture`, `/trust-engines`, `/integrations`) + `/vendors/[id]`; Settings keeps raw JSON. | Settings page edits dicts as JSON; a readiness table is its own view. |
| B9 | **New engine `reality`** in `ENGINES` and `HOLD_ENGINES` (fails closed in enforcing like the others). | Spec §3.1.4. |

## Part 1 — Scaffold (`v2/w3-scaffold`, sequential, merged first)

Own plan `docs/superpowers/plans/2026-10-21-v2-wave3-scaffold.md` (written via `superpowers:writing-plans` after this is approved).

**S1 Migration + models.** `ip/db/migrations/versions/a1b2c3d4e5f6_trust_wave3.py` (revises `f4a5b6c7d8e9`; own `RLS_TABLES`; copy the Wave 2 file's helpers). Five tables, all in `TENANT_TABLES` (`ip/db/models.py:1224`):

| Table | Columns (beyond id, tenant_id, stamps) |
|---|---|
| `trust_holds` | `finding_id` FK idx, `transaction_node(36)` idx, `engine(32)`, `type(64)`, `placed_at`, `placed_by(320)`, `lifted_at` null, `lifted_by`, `lift_reason(500)` |
| `trust_evidence_requests` | `finding_id` FK idx, `owner(320)`, `due_date` Date, `note(500)`, `status(16)` open/fulfilled/cancelled, `proof_document_id(36)`, `requested_by`, `fulfilled_at` |
| `api_keys` | `name(128)`, `key_prefix(12)` idx, `key_hash(64)`, `role(16)`, `created_by`, `last_used_at`, `revoked_at`; unique `(tenant_id, key_prefix)` |
| `webhook_endpoints` | `url(1024)`, `secret(128)`, `events` JSON, `active` bool, `description(256)`, `created_by` |
| `webhook_outbox` | `endpoint_id` FK idx, `event(48)`, `payload` JSON, `attempts` int, `next_attempt_at` idx, `delivered_at`, `last_status`, `last_error(500)`, `dead_at` |

Tests: `tests/test_trust_schema.py`; `tests/test_gcs_and_migrations.py:478` param `("a1b2c3d4e5f6", 5)`; `tests/test_restore_check.py:31` head pin.

**S2 Types + modes + settings.** `ip/trust/types.py` (B5) + `DIMENSIONS`; `findings/model.register_type(..., dimension=)` → `TYPE_DIMENSION`. `ip/trust/modes.py`: `ENGINES += ("reality",)`, `HOLD_ENGINES |= {"reality"}`, `CAPABILITIES += ("usage",)`, `mode_since(tenant, engine, store)`, `set_engine_mode(ctx, engine, mode, *, store, audit, now, reason)` (writes `trust_engine_modes` + `trust_engine_mode_since`, audits `trust_engine_mode_changed`, calls `holds.reconcile`). `tenant_settings.py`: `SETTABLE += trust_shutdown_periods: list` (validated `[from, to]` ISO pairs); `config_routes.update_settings` (`:122`) detours `trust_engine_modes` through `set_engine_mode`. `trust/audit/report.py:19` PREVENTION gains the reality row. `thresholds.DEFAULTS` += `reality.qty_tolerance_pct .02, reality.hours_tolerance_pct .10, reality.seats_tolerance_pct .05, reality.evidence_window_days 45, reality.price_jump_zscore 3.0, reality.volume_zscore 3.0, reality.min_history 5, provenance.edits_suspect 2, readiness.min_days 14, readiness.min_findings 20, readiness.min_decided 10, readiness.min_precision 0.8` → `tests/test_trust_settings.py:91` snapshot.

**S3 Holds + outbox (emit side).** `ip/trust/holds.py` `HoldStore(url)`: `place(s, ctx, finding)`, `lift(s, ctx, finding_id, by, reason)`, `open_for(tenant, transaction_node)`, `reconcile(ctx, engine, *, findings, enforcing)`. `ip/trust/webhooks/{__init__,outbox}.py`: `EVENTS = ("finding.created","finding.decided","hold.placed","hold.lifted","ping")`, `emit(s, tenant_id, event, payload)` (one row per active endpoint subscribed; payload `{event, tenant, occurred_at, data}`); stub `deliver.py::due(now, *, post=None, limit=50) -> 0`. `FindingStore.upsert` emits `finding.created`; `transition` emits `finding.decided` on `DECIDED` and lifts the hold on `rejected`; `evaluate()` places holds (B1).

**S4 Gate + callers.** `gate.payment_hold_detail(tenant, case_id, *, url, settings_store) -> {held, message, holds:[{finding_id,type,engine,summary}], waiting:[engine], transaction_node}`; `payment_hold()` = `detail()["message"]`. `approval_flow.Decision.hold_overridden` → `{"message", "finding_ids"} | None`; `service.py` actions (`:1673`) store it; export (`:2013`) compares finding-id sets and `waiting == []`. `GET /api/invoices/{case_id}` adds `hold` and `trust_node`. `service.py`: `get_hold_store()`, mounts the five new routers, registers `auth.set_api_key_resolver(apikeys.resolve)`.

**S5 Auth hook.** `ip/api/auth.py`: `API_KEY_HEADER`, `api_key_token(request)` (header, or bearer starting `ik_`), `set_api_key_resolver(fn)`; `_resolve_principal` tries the resolver first (None → fall through; bad key → `AuthError`). Stub `ip/trust/apikeys.py` (`PREFIX="ik_"`, `resolve -> None`).

**S6 Engine/graph/history/provider seams.** `graph/sync.py`: project `unit`, `service_period_start/end`, `work_authorization_reference`, `project` from `Invoice.fields["extraction"]`; `_HISTORY_ORDER` += ticket 7, timesheet 8, shipment 9, seat_count 10; `fulfils` edges for shipment/ticket/timesheet with `po_number`. `graph/lineage.py:117` GRN stage reads `("qty_received","quantity")` (closes the history-GRN gap). `history/mapping.py` KINDS/FIELDS/REQUIRED/_SYNONYMS for the four kinds (ticket: external_id, vendor_key, doc_number, record_date, status, hours, project, po_number; timesheet: external_id, vendor_key, record_date, hours, rate, project, po_number, approved_by; shipment: external_id, doc_number, po_number, vendor_key, record_date, quantity, status, carrier; seat_count: external_id, vendor_key, record_date, product, quantity). `providers/base.py` `Usage.seats(vendor, period)`, `METHOD["usage"]`; `fakes.py` alias. `evaluate.py`: after the engine loop `score.compute(ectx, report)` in try/except (never fails the run). Package skeletons `trust/reality/__init__.py` (`run -> []`, codes registered), `trust/score/__init__.py` (`SCORE_VERSION="score-1"`, `compute`), `explain/templates/reality.py` (+ import line in `templates/__init__.py`). Route stubs with fixed function names returning 503: `api/trust_score_routes.py` (`transaction_trust, vendor_trust, posture, readiness, set_mode`), `api/trust_reality_routes.py` (`create_evidence_request, list_evidence_requests, fulfil_evidence_request`), `api/trust_provenance_routes.py` (`upload_vendor_document, score_document`), `api/integration_routes.py`, `api/v2_routes.py` (empty prefixed router).

**S7 Worker.** `runner.py`: `WEBHOOK_SCAN_SECONDS = 60`, `_deliver_webhooks_if_due()` beside `_escalate_if_due` (imports `trust.webhooks.deliver.due`; errors logged). Test in `tests/test_trust_worker.py`.

**S8 Benchmark + isolation + seed.** `benchmark/history/generate.py` KINDS/HEADERS += the four kinds; `eval.py` try-imports `reality.measure` and `score.measure` like `_explain_module`; `tests/test_benchmark_history.py` `wave0_only` stubs them too (baseline stays green). `tests/test_tenant_isolation_api.py`: PROBED += the five stub paths; `EXTRA_PROBE_MODULES += (tests.test_trust_reality_routes, tests.test_trust_provenance_routes, tests.test_trust_api_routes, tests.test_trust_score_routes)`; route-module tuple += five modules. `scripts/e2e_api.py seed_finding()` also seeds one `REAL-NO-EVIDENCE` finding, one `trust_score` node and one open `trust_holds` row for `case_005`.

**S9 Frontend shell + docs.** `fe/lib/api.ts` types (`TrustHold, HoldDetail, TrustScore, VendorTrustProfile, TrustPosture, Readiness, EvidenceRequest, ApiKey, ApiKeyCreated, WebhookEndpoint, WebhookDelivery`; `InvoiceDetail.hold/trust_node`; `Workflow.hold_overridden`). `fe/lib/trust.ts` `ENGINE_LABELS.reality`, `DIMENSION_LABELS`, `REALITY_LABELS`. `AppShell.tsx` NAV += Trust posture (`/trust-posture`), Trust engines (`/trust-engines`, admin), Integrations (`/integrations`, admin). Placeholder pages (h1) + `vendors/[id]`. `e2e/golden-path.spec.ts:217` list and `tests/test_frontend_reaches_the_flow.py:24` VIEWS updated. Specs committed: `docs/superpowers/specs/2026-10-21-v2-wave3-{scaffold,reality,provenance,score,api,frontend}-design.md`; `docs/PENDING.md` Wave 3 bullets.

## Part 2 — Parallel branches

### A. `v2/w3-reality` — Reality checks (FR-23)
Owns `ip/trust/reality/**` (`classify, goods, services, subscriptions, plausibility, requests`), `api/trust_reality_routes.py`, `explain/templates/reality.py`, `benchmark/history/faults/reality.py`, `benchmark/history/reality.py`, `tests/test_trust_reality*.py`, `tests/fixtures/trust_reality/**`.
- **Classify** (deterministic): goods (PO ref or qty>1 with non-time unit) / services (`work_authorization_reference` or unit ∈ hour/day/md) / subscription (line matches `seat|user|licen[cs]e|subscription`) / other (plausibility only).
- **Bands:** Σ evidence (grn `qty_received|quantity`, shipment delivered qty; timesheet/ticket hours vs `wa_matching.invoiced_hours()`-style line hours; latest seat_count ≤ period end) vs Σ invoiced within tolerance → CORROBORATED; short → PARTIAL; short beyond tolerance or delivery after invoice beyond `evidence_window_days` → CONTRADICTED; none → NO_EVIDENCE. Providers (`tickets/timesheets/logistics/usage`) called only when the graph has nothing; every call leaves a `provider_evidence` node.
- **Plausibility** (reuse `payment.signals.robust_z`): `REAL-PRICE-JUMP` (unit price per vendor+description vs history, no `governed_by` clause change), `REAL-VOLUME-SPIKE`, `REAL-SHUTDOWN` (overlaps `trust_shutdown_periods`).
- **Outputs:** `reality_check` node `{result, kind, expected, found, unit, evidence, window_days, engine_version}` + `evidences` edges. Types: `REAL-CONTRADICTED` (hold, reality, impact `(invoiced−found)×unit_price`), `REAL-PARTIAL`, `REAL-NO-EVIDENCE`, `REAL-PRICE-JUMP` (price), `REAL-VOLUME-SPIKE` (consistency), `REAL-SHUTDOWN`.
- **Routes:** `POST /api/trust/findings/{id}/evidence-request {owner, due_date, note}` (clerk → row + `needs_vendor_input`); `GET /api/trust/evidence-requests?status&overdue`; `POST /api/trust/evidence-requests/{id}/proof` multipart (clerk → `Document(kind="proof")` via the blob store like `_store_contract` `trust_routes.py:189`, `document_version` node + `evidences` edge, request fulfilled, enqueue `trust.evaluate` for proof and invoice); `POST .../cancel`.
- **Gates:** `REAL-CONTRADICTED {.9/.8/impact .1}`, `REAL-PRICE-JUMP {.85/.7/.1}`, `REAL-VOLUME-SPIKE {.8/.7/.15}`; `reality_coverage_goods ≥ .70`, `reality_coverage_services ≥ .50` (AC-23 on the synthetic set; reviewer agreement stays a partner gate).

### B. `v2/w3-provenance` — Provenance v2 (FR-22.1/22.3)
Owns `ip/trust/provenance/**` (+`forensics.py`), `api/trust_provenance_routes.py`, `explain/templates/provenance.py`, `benchmark/history/faults/provenance.py`, `tests/test_trust_provenance*.py`, fixtures.
- Families: `producer` (editor list → .3, renderer → .8, empty .6, matches vendor's prior fingerprints 1.0), `edits` (`startxref` count−1, `mod−creation`; ≥ `provenance.edits_suspect` → .2), `fonts` (mixed subset fonts on amount lines → .3). `WEIGHTS` v2 `{channel .3, template .2, text .1, meta .1, producer .15, edits .1, fonts .05}`; `ENGINE_VERSION="provenance-2"`; `pdfmeta.facts()` += `incremental_updates`, `font_names`.
- KYC/proofs: `run()` scores `document.kind in ("kyc","proof")` with `invoice_node=None`; `evidences` → vendor (kyc) / invoice (proof). Types `PROV-KYC-SUSPECT` (insight, authenticity, node = vendor), `PROV-EDITED` (insight). Routes: `POST/GET /api/vendors/{vendor_id}/documents` (subkind ∈ gst_certificate/pan/udyam/cancelled_cheque/abn), `POST /api/trust/documents/{document_id}/authenticity` (sync score).
- Gate `PROV-EDITED {1.0/.9}`; v1 fixtures still score within ±0.05.

### C. `v2/w3-score` — Score, vendor profile, posture, readiness (FR-25, U2)
Owns `ip/trust/score/**` (`dimensions, vendor, posture, readiness`), `api/trust_score_routes.py`, `benchmark/history/score.py`, `tests/test_trust_score*.py`.
- `compute(ectx, report)` writes `trust_score` node `{score_version, computed_at, composite|null, dimensions:{dim:{value, hold, drivers:[finding ids], note}}, versions, holds, mode}`. Rules (pure, table-driven): identity = 1−max(open identity finding severity: PAY-UNVERIFIED/NAME-MISMATCH/BEC 1.0, GST holds 1.0, GST insights .4); authorization = chain complete 1.0 / overridden .7 / pending .5 / rejected 0 (v1 chain is the hold); reality = CORROBORATED 1 / PARTIAL .6 / NO_EVIDENCE .4 / CONTRADICTED 0 / absent null; price = 1−min(1, Σ open LEAK-* impact ÷ amount), no contract + PRICE-JUMP .3, no baseline null; authenticity = `document_version.score`, TAMPER 0; consistency = 1−.5·|PAY-ANOMALY signals|, VOLUME-SPIKE ≤ .4. Composite = mean of non-null, advisory.
- Routes: `GET /api/trust/transactions/{node}/trust?as_of`; `GET /api/trust/vendors/{node}/trust` (dimension means over last 50 scores, findings by type, GST standing from latest `gstin` evidence, bank verified, KYC docs, network via `natural_keys` collisions + `entity_links`, monthly timeline); `GET /api/trust/posture?since` (`found/prevented/recovered/written_off/open_exposure/by_type/by_vendor/pre_payment_share/provenance_distribution/verification_coverage/holds`); `GET /api/trust/readiness` + `POST /api/trust/engines/{engine}/mode {mode, reason}` (admin).
- Readiness per engine: `{mode, since, days, findings, would_have_held, held, open_holds, triage{decided,accepted,rejected,precision}, failures, recommendation}`; shadow→advisory when days ≥ min_days ∧ findings ≥ min_findings; advisory→enforcing when days ≥ min_days ∧ decided ≥ min_decided ∧ precision ≥ min_precision ∧ failures == 0; insight-only engines: "enforcing adds nothing".
- Gates: `score_coverage == 1.0`, `dimension_hold_consistency == 1.0`.

### D. `v2/w3-api` — API keys, `/v2` façade, webhooks (FR-30, §9)
Owns `ip/trust/apikeys.py`, `ip/trust/webhooks/deliver.py`, `api/integration_routes.py`, `api/v2_routes.py`, `tests/test_trust_api*.py`, `tests/test_trust_webhooks*.py`, `docs/API_V2.md`.
- Keys: `create(ctx, name, role≤approver) -> (row, "ik_{prefix}_{secret}")`, `resolve(token) -> Principal|None` (engine-level session for prefix lookup, `hmac.compare_digest` on sha256, refuse revoked, `user_id=f"api-key:{id}"`), `revoke`. Routes `GET/POST /api/integrations/api-keys`, `DELETE .../{id}` (admin; audited). Key principals get 403 on integration routes.
- Webhooks: `GET/POST/PATCH/DELETE /api/integrations/webhooks` (https only; secret shown once), `POST .../{id}/test` (`ping`), `GET .../{id}/deliveries`. `deliver.due()`: per tenant with due rows, `requests.post(timeout=10)` with `X-Trust-Signature/-Timestamp/-Event/-Delivery`; 2xx → delivered; else backoff `min(2**attempts, 64)` min; ≥ 8 attempts → `dead_at` + audit.
- Façade (each = import + call): `/v2/transactions/{id}/trust|explanation|lineage`, `GET /v2/findings`, `POST /v2/findings/{id}/decision {decision, reason}`, `/v2/vendors/{id}/trust`, `POST /v2/documents/{id}/authenticity`, `/v2/evidence/{node_id}`, `/v2/trust-audits` (+ runs, report).
- Tests: key lifecycle (bad/revoked/foreign key → 401, bearer wins), façade parity with `/api`, emission from store/evaluate/mode change, delivery with a fake `post` (success, backoff, dead-letter, verifiable signature), probes for every `/v2` and `/api/integrations` path with bearer **and** foreign API key.

### E. `v2/w3-frontend` — surfaces (FR-30, §8)
Owns `fe/app/invoices/page.tsx` (edit), `fe/app/findings/[id]/page.tsx` (edit), `fe/app/vendors/page.tsx` (link), `fe/app/vendors/[id]`, `trust-posture`, `trust-engines`, `integrations` pages (fill), new `fe/components/trust/{TrustPanel,HoldBanner,DimensionMeter,EvidenceRequestDialog,ReadinessTable,ApiKeysPanel,WebhooksPanel}.tsx`, `fe/e2e/trust-wave3.spec.ts`.
- Workbench: `HoldBanner` (holds + waiting; admin override hint; links to findings) + `TrustPanel` (six `<meter>`s with hold badges, drivers, versions, composite "advisory"). Finding detail: dimension chip, "Request evidence" dialog (`<input type="date">`), proof upload, overdue badge. Vendor detail: tiles (`Tile.tsx`), findings by type, GST standing, KYC docs + upload, network, monthly composite as CSS bars. Posture: money tiles, tables, `<progress>` shares, coverage meters. Trust engines: `ReadinessTable` + Promote/Demote dialog (reason). Integrations: keys (shown once), endpoints, test, deliveries. No chart library.
- e2e against the scaffold seed: hold banner + dimensions on `case_005` → request evidence → vendor page → posture → promote/demote → create/revoke key.

## File ownership (every file → one owner)

| Owner | Files |
|---|---|
| scaffold | `models.py`, migration, `repositories.py`; `trust/{modes,types,thresholds,holds,evaluate,gate}.py`, `tenant_settings.py`, `findings/{model,store}.py`, `webhooks/{__init__,outbox}.py`, `approval_flow.py`, `api/{service,config_routes,trust_routes,auth}.py`, `worker/runner.py`, `graph/{sync,lineage}.py`, `history/mapping.py`, `providers/{base,fakes}.py`, `audit/report.py`, `explain/templates/__init__.py`; `benchmark/history/{generate,eval}.py`; `tests/{test_tenant_isolation_api,test_trust_settings,test_trust_schema,test_gcs_and_migrations,test_restore_check,test_frontend_reaches_the_flow,test_trust_gate,test_trust_engine,test_trust_findings,test_trust_worker,test_benchmark_history}.py`; `scripts/e2e_api.py`, `docs/PENDING.md`, the six specs; `fe/lib/{api,trust}.ts`, `AppShell.tsx`, `globals.css`, `golden-path.spec.ts`, placeholder pages |
| reality | per §A |
| provenance | per §B (incl. `faults/provenance.py`, `templates/provenance.py`) |
| score | per §C |
| api | per §D (fills the scaffold's `apikeys.py`, `deliver.py`, `integration_routes.py`, `v2_routes.py` stubs) |
| frontend | per §E |

Resolved contentions: `evaluate.py` (scaffold adds the `score.compute` call; score fills the package); `findings/store.py` emission (scaffold); `auth.py` (scaffold hook, api fills `resolve`); `provenance/__init__.py` (provenance only; score reads `document_version.score` by data contract); `trust_routes.py` (scaffold only — every branch adds routes in its own module); `findings/[id]/page.tsx` (frontend only). A needed shared change = scaffold follow-up commit on `main`, then rebase.

## Execution mechanics

1. On approval: commit the program plan + six specs under `docs/superpowers/{plans,specs}/2026-10-21-v2-wave3-*.md` (branch `v2/w3-scaffold`), then `superpowers:writing-plans` for the scaffold, TDD, verification, fable review, one fix pass, PR, user's "merge".
2. Worktrees from merged scaffold: `.worktrees/<name>`, branch `v2/w3-<name>` ×5; `uv sync --frozen`; `npm ci` (frontend); Postgres DBs `trust_w3_{reality,provenance,score,api}` on `pg-w1`.
3. Each branch: own plan doc, strict TDD, ownership row in the brief, repo rules (no ids in log extra, masked attrs, Decimal money, every route probed, HTML via `trust/html.py`), ruff + suite + Postgres + eval (where gated) + lint/build/Playwright (frontend) → fable review → one fix pass → PR.
4. **Merge order:** scaffold → reality, provenance, score (any order, each rebased) → api (rebased last among backends; its façade imports the real route functions) → frontend last (its e2e drives everything).
5. Wave close: gates green on `main` together, worktrees removed, PENDING + memory updated, post-wave cleanup PR.

## Verification (wave exit)

- `main`: full suite; Postgres glob; `alembic upgrade head && downgrade base`; `restore_check.repo_head() == "a1b2c3d4e5f6"`; `python -m benchmark.history.eval --seed 7`: all prior gates + `REAL-CONTRADICTED/PRICE-JUMP/VOLUME-SPIKE`, `PROV-EDITED`, `reality_coverage_goods ≥ .70`, `reality_coverage_services ≥ .50`, `score_coverage == 1.0`, `dimension_hold_consistency == 1.0`, explain citation coverage still 1.0; `npm run lint && npm run build`; Playwright golden-path + `trust.spec.ts` + `trust-wave3.spec.ts`.
- Isolation: every new path in a `PROBED` set with a real two-tenant probe; `/v2/*` probed with bearer and with a foreign API key.
- Manual smoke (local, `AUTH_DISABLED=1`, worker running): import `shipment.csv` + `timesheet.csv` → upload a goods invoice short of delivery → `REAL-CONTRADICTED` in advisory (no hold) → `/trust-engines` promote reality to enforcing with reason → hold banner on the workbench, `hold.placed` delivered to a local endpoint with a verifiable signature → reject the finding → banner clears, `hold.lifted` delivered → evidence request on a NO_EVIDENCE finding, upload a POD → provenance scores it, re-evaluation corroborates → vendor profile shows KYC + GST standing → posture tiles reconcile with the Wave 2 ledger → `curl -H 'X-API-Key: ik_…' /v2/findings` equals `/api/trust/findings`.

## Risks / deferred

- `GraphStore._retire` must re-point `evidences` edges on `document_version`/`trust_score` re-version — scaffold test before branches rely on it.
- API-key prefix lookup is cross-tenant by nature: engine-level session only; probes check a foreign key yields 401, never data.
- Promotion to enforcing holds existing open advisory hold-type findings immediately (intended; readiness shows `open_holds` first; `reconcile` emits `hold.placed` each).
- Outbox rows are not purged this wave → PENDING (retention sweep).
- Partner gates (AC-23 reviewer agreement, ≥ 70 % findings pre-payment, cost/transaction) → PENDING.
- Deferred: notifications (Slack/WhatsApp/email), public upload links, copy-move forensics, live connectors/OAuth, rate limiting + idempotency on `/v2`, `/v2/agents*` (Wave 4).
