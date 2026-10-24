# v2 Wave 5 — Agent control surfaced: Program Plan

**Builds on** `main` @ `20b530e` (Waves 0–4, #83–#99). Alembic head `c3d4e5f6a7b8`. Production paused on the v1 schema; nothing from Wave 4 is deployed.
**Shape (U4):** as Wave 4 — one sequential scaffold branch `v2/w5-scaffold` owning every shared file, then four parallel worktree branches with disjoint ownership; each: spec → plan → strict TDD → fable review → one fix pass → PR; merge only on the user's word with `--match-head-commit`. Ledgers in `.superpowers/sdd/<plan>/progress.md` in the main checkout.

**Next:** write five specs `docs/superpowers/specs/2026-10-23-v2-wave5-{scaffold,screen,governed,intake,hygiene}-design.md` (builder-check each); get the user's approval of the specs; then `superpowers:writing-plans` for the scaffold.

## Context

PENDING item 8 lists eleven things deferred from Wave 4. Exploration (2026-10-23) found: one is not needed (mandate **expiry sweep** — expiry is computed on every read in `store.current_mandate` / `mandates.in_force` / `annotate`, and rewriting `status` would invalidate the mandate signature, which signs `status`); two are blocked outside the repo (live **ERP observers** need a partner's stack; **external mandate adapters** need a standard — the PRD names none, FR-28.5). The user chose to build a generic intake seam for those two and the remaining eight in full. No Wave 5 exists in the spec (§2 ends at Wave 4); this program is it. Release stays gated per PRD §5.

## Decisions taken with the user (2026-10-23)

| # | Decision |
|---|---|
| U1 | **Observers/adapters = one generic intake seam**: API-key-authenticated `POST /v2/agents/actions` taking a batch of log rows (same contract as the CSV import), plus the mandate-scope JSON documented in `docs/API_V2.md`. No vendor-specific ERP adapter. |
| U2 | **Notifications on blocks = in-app + webhook**: a shell banner for approver+ when an agent action was blocked / needs review, linking to the agents screen; `agent.action.blocked` webhooks already fire. No email/Slack (no outbound channel exists in the product). |
| U3 | **Counterparties by vendor id, names allowed**: a scope may list ids or names; names resolve to ids at proposal when the vendor exists, else stay names; `rules.in_scope` matches id first, normalised-name key as fallback (`vendor.create` has no id). |
| U4 | **Wave 4 delivery shape** (scaffold, then parallel worktrees, file-ownership table). |

## Builder's calls (mine; the specs say so)

| # | Call | Why |
|---|---|---|
| B1 | **No migration.** Scope JSON shape only (`counterparties: [{"vendor_id": str|null, "key": str}]`); retention deletes rows; the banner count is a `SELECT count(*)`; holds reuse `trust_holds`. | `store.SIGNED` covers `scope`, so digests change for new mandates only; none exist anywhere. |
| B2 | **`policy.Action.vendor_id: str | None`**, filled at every builder that has it (`guard.invoice_action` from `vendor["id"]`; vendor patch/propose from the path id; `_judge` via `vendors.find_by_name`). `vendor.create` → None → key fallback. | One field; C's rule change touches only `rules.in_scope`. |
| B3 | **Own count endpoint** `GET /api/trust/agents/blocks/count` (approver) → `{blocked, reviews, window_hours}` over `agents.banner_window_hours` (new threshold, default 24). `AppShell` polls it only when `can(me,"approver")`. | `/api/system/status` is any-role and `test_system_status_leaks_no_tenant_data` forbids tenant data there. |
| B4 | **Two new kinds** `po.create` (subject `purchase_orders:{po_number}`, vendor from the PO, amount recorded, never summed — Wave 4 B9 stands) and `document.upload` (subject `documents:{sha256[:32]}`; KYC upload adds the vendor). `SUBJECT_TABLES += {"po": "purchase_orders", "document": "documents"}`. `po.create` fires only for numbers not already stored (one lookup, like `normalized_names`). | Caps stay approve-only; the ledger still shows what the agent did. |
| B5 | **Hold from a log BLOCK** only when `source=="log"` ∧ `BLOCK` ∧ `mode=="enforcing"` ∧ subject `invoices:<case_id>` ∧ `service._trust_node` resolves. `_judge` resolves `transaction_node`; `record` sets `details["hold"]=True` and passes `hold=True` to the finding upsert. `AGENT-*` rows in `types.TABLE` stay `hold=False` so `_owns_holds("agents")` stays false and `_release_cleared` never lifts them: only a person's finding decision ends the hold (said in the explanation). | `gate.py:97-101` reads per-finding `details["hold"]` + engine enforcing; nothing else changes. |
| B6 | **`record()` atomic.** `FindingStore._upsert(s, …)` takes the caller's session (`upsert` wraps it; precedent `transition`). `record(..., s=None)`: ledger row, finding, `finding_id` stamp, outbox in one session — the guard passes its lock session, so `store._lock_query` drops `key_share=True`. The `acted_on` graph edge stays a separate call after commit. | Closes PENDING "ordered, not atomic"; one connection removes the FK-wait workaround. |
| B7 | **Retention = third `_x_if_due` sweep**, daily, per tenant: `AgentStore.purge_before(tenant_id, before)` deletes `occurred_at < before` and `action != "vendor.create"` (`creators_of` is unbounded). `before = now − max(agents.retention_days (default 400), split_window_days+1, 31)`. | Smallest sweep that keeps every cap/split window and the circular rule intact. |
| B8 | **OCR fallback, worker-only.** `confidence.ocr_page_text` gains a bytes path (pypdfium2 accepts bytes); `inputs.text_of(…, ocr=False)` appends OCR of ≤ `MAX_OCR_PAGES=5` when `has_readable_text` is false; only `later.classify` passes `ocr=True`. | The inline guard never loads text (it defers), so requests are untouched; tesseract absent → `[]`, no signal. |
| B9 | **Intake auth = API key with role approver** (the one role `evaluate` already demands for `source:"log"`). Body `{"actions":[{agent?, action, subject_ref, vendor?, category?, amount?, currency?, occurred_at}]}`, ≤ `MAX_IMPORT_ROWS`; `agent` omitted → the key's own agent (`by_principal`), else name / principal email as `import_actions` resolves it; each row → `_judge(source="log")`; 409 while off. | U1: the CSV import over JSON; `Admin` (human-only) would make a machine feed impossible. A key attests for any named agent, as an approver's log upload does today — noted in API_V2/PENDING (rate limit/idempotency still deferred). |
| B10 | **Agents screen role = approver**: NAV `{href:"/agents", label:"Agents", min:"approver"}`, h1 "Agents"; register / propose / approve / revoke / import controls only for `can(me,"admin")`; dialogs state the two-person rule. | Mirrors server roles. |

## Part 1 — Scaffold `v2/w5-scaffold` (sequential, merged first)

No migration (B1). With `agents` off, zero behaviour change (the two new call sites go through `guard.check`, which returns before anything is read).

- **S1 Registries.** `trust/agents/__init__.py ACTIONS += ("po.create","document.upload")`; `api/trust_agent_routes.py SUBJECT_TABLES` += two keys; `trust/thresholds.py DEFAULTS += {"agents.banner_window_hours": 24, "agents.retention_days": 400}` (+ `tests/test_trust_settings.py` snapshot). `explain/templates/agents.py` needs no new codes.
- **S2 Policy + atomic record.** `policy.Action.vendor_id`; `findings/store.py` `_upsert(s, …)` split; `record.record(..., s=None, hold=False)` one-session body (B6; branch B flips `hold`); `guard._one` passes its lock session; `store._lock_query` drops `key_share`; `_judge` resolves `vendor_id` and, for `invoices:` subjects, `transaction_node`. Store additions: `count_blocked(tenant_id, since) -> dict`, `purge_before(tenant_id, before) -> int`. Tests: `tests/test_trust_agents_store.py`, `tests/test_trust_agents_policy.py` (+ atomicity: a failing finding upsert leaves no ledger row).
- **S3 Routes.** `GET /api/trust/agents/blocks/count` built in full (declared before `/{agent_id}`), in `tests/test_trust_agent_routes.PROBED`. `POST /v2/agents/actions` as a named 503 stub `intake_actions` in `api/v2_routes.py`, with the stub-probe hand-over restored in `tests/test_tenant_isolation_api.py` for this path; one row in `docs/API_V2.md` so `test_the_doc_lists_every_v2_route_and_nothing_else` stays green (C rewrites it).
- **S4 Frontend shell.** `fe/lib/api.ts` types `Agent`, `Mandate`, `AgentLedger`, `AgentBlockCount`; `AppShell.tsx` NAV entry (one line; the banner block is A's); `fe/app/agents/page.tsx` stub (`<h1>Agents</h1>` + empty state); `tests/test_frontend_reaches_the_flow.py VIEWS += "agents"`; `golden-path.spec.ts` sidebar array += `"Agents"`.
- **S5 Docs.** Wave 5 block in `docs/PENDING.md` (item 8 split into the four branches; expiry sweep closed as "not needed"; G1/G2 below listed).

Done when: ruff (CI form); SQLite suite; Postgres `tests/test_trust_*.py`; eval `--seed 7` 56/56; `npm run lint && npx tsc --noEmit && npm run build`; e2e 47.

## Part 2 — Parallel branches and ownership

| Branch | Owns (exclusive) | Shared files: may touch |
|---|---|---|
| **A `v2/w5-screen`** — FR-28.6 agents view, dashboard tiles, banner (U2, B3, B10) | `fe/app/agents/page.tsx` (agents table; mandates with propose/approve/revoke dialogs on the `.dialog-scrim` pattern; per-owner spend from `GET /api/trust/agents/ledger`; blocks/reviews tiles; log-import upload), `fe/components/trust/AgentMandateDialog.tsx` (new), `fe/app/dashboard/page.tsx` (two tiles), `frontend/e2e/agents.spec.ts` (new; seeds via API in the spec using `as(page, who)` for the second admin) | `AppShell.tsx` banner block only; `globals.css` append-only; `test_frontend_reaches_the_flow.py` append-only |
| **B `v2/w5-governed`** — PO/upload governance, holds from logs (B4, B5) | `api/po_routes.py` (`store_orders` call site; `existing_numbers` in the PO repo in `db/repositories.py`), `api/ingest.py` (`upload_document` via `run_in_threadpool`), `api/trust_provenance_routes.py` (KYC upload), `trust/agents/guard.py` (`po_action`, `document_action` builders), `trust/agents/record.py` (B5 hold branch only), `tests/test_trust_agents_guard_po_upload.py`, `tests/test_trust_agents_holds.py` | none |
| **C `v2/w5-intake`** — intake API, counterparty ids, scope doc (U1, U3, B9) | `api/v2_routes.py` (fills `intake_actions`), `api/trust_agent_routes.py` (`propose_mandate` passes `vendors` to `mandates.validate`), `trust/agents/mandates.py` (`_counterparties(names_or_ids, vendors)`), `trust/agents/rules.py` (`in_scope` id-first), `docs/API_V2.md`, `tests/test_trust_api_routes.py` (PROBED += `/v2/agents/actions`), `tests/test_trust_agents_mandates.py`, `tests/test_trust_agents_rules.py` | none |
| **D `v2/w5-hygiene`** — retention, OCR (B7, B8) | `worker/runner.py` (`_purge_agent_actions_if_due` + constant + `run_forever` line), `trust/agents/inputs.py`, `trust/agents/later.py` (`ocr=True`), `confidence.py` (bytes path), `tests/test_trust_agents_retention.py`, `tests/test_trust_agents_inputs.py` | none |

A shared change mid-wave = a scaffold follow-up PR on `main`, then branches merge `main`. Merge order: scaffold → B, C, D (any order, `main` merged in, CI green on the exact head) → A last (its e2e drives C's propose-with-ids and B's hold tile).

## Acceptance gates

- **A:** lint/tsc/build; e2e ≥ 49 (`agents.spec.ts`: register → propose → self-approve shows the error → second admin approves → "verified"; a banner appears after an enforcing BLOCK seeded by a vendor replace as the registered key); dashboard tiles hidden for a viewer; `test_frontend_reaches_the_flow` green.
- **B:** PO import / `PUT` by a registered key: enforcing + out of mandate → 403 and no PO row; advisory → row + ledger; off → `AgentStore` never queried; refused upload stores no document; KYC upload governed. Log BLOCK in enforcing on an invoice → `trust_holds` row, `gate.payment_hold_detail` held, export 409; advisory → no hold; a person's finding decision lifts it; an engine run on the invoice leaves it.
- **C:** two-tenant probes (bearer and foreign key) for `/v2/agents/actions`; key with no agent and no `agent` field → 400; off → 409; mixed rows; propose with names → ids where vendors exist, digest verifies; `in_scope` by id, by key, and after a vendor rename (id still matches); API doc test green.
- **D:** sweep keeps `vendor.create` rows and everything inside the window, deletes older rows, idempotent, per tenant, one tenant's error isolated; `text_of(ocr=False)` never calls tesseract; `ocr=True` with a thin text layer calls the bytes path; the inline guard never loads text (existing defer test).
- **Wave:** eval `--seed 7` 56/56 unchanged; Postgres `tests/test_trust_*.py`; `alembic` head still `c3d4e5f6a7b8`; every new path in a `PROBED` set; live smoke on the seeded e2e server: register key agent → propose with a vendor name + an id → second admin approves → enforcing → PO import by the key out of scope → 403 → intake a log BLOCK on an invoice → hold on the workbench, banner count 1 → decide the finding → hold lifted → `/agents` shows the ledger.

## Risks

1. The agent lock now spans the finding write (one extra INSERT batch under the lock). 2. A log-derived hold has no automatic release; the finding summary says so. 3. Same-bytes re-upload shares a `document.upload` subject (it is the same document). 4. OCR needs tesseract in the worker image; absent → no signal, logged once. 5. The banner is time-windowed (24 h), not acknowledgement-based. 6. An approver key can log rows for any named agent (B9) — noted; rate limit / idempotency on `/v2` remain deferred.

## Surfaced, not built (for the user to decide later)

- **G1 FR-28.4 "drift from mandate":** no detector compares an agent's recent behaviour to its baseline or mandate; candidate `behaviour.drift` over `AgentStore.actions` windows, needs a threshold design.
- **G2 PRD "agent registration requires Controller approval":** `register_agent` is one admin; only mandates are two-person. Safe today because an agent without an approved mandate is BLOCK everywhere. Option: `agents.status` starts `proposed` with a second-admin approve (one route, no migration).

## Verification (how the wave is proven end to end)

Per branch as above; for the wave, the CI five jobs green on each PR's exact head, then the live smoke on `scripts/e2e_api.py` (`E2E_API_PORT=8260 TRUST_MANDATE_KEY=<anything>`) walking the sequence in "Wave" above, and `docs/PENDING.md` updated: item 8 closed except the partner gates; G1/G2 recorded.
