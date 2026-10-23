# Wave 5 — Scaffold design (`v2/w5-scaffold`)

**Program plan:** `docs/superpowers/plans/2026-10-23-v2-wave5-program.md` (U1–U4, B1–B10). Builds on `main` @ `20b530e`; alembic head `c3d4e5f6a7b8` stays the head (B1: no migration).

**Owns every shared file** (program plan, Part 2 table). Merged before any branch starts. Behaviour change with the `agents` engine `off`: none — the two new governed call sites arrive in branch B, and `guard.check` returns before anything is read while the engine is off.

## S1 Registries

- `trust/agents/__init__.py`: `ACTIONS += ("po.create", "document.upload")` (nine kinds). `mandates.validate` reads the tuple, so a mandate may name them at once.
- `api/trust_agent_routes.py`: `SUBJECT_TABLES += {"po": "purchase_orders", "document": "documents"}` (keyed by the kind's prefix, `kind.split(".")[0]`).
- `trust/thresholds.py DEFAULTS += {"agents.banner_window_hours": 24, "agents.retention_days": 400}`; `tests/test_trust_settings.py` snapshot gains both.
- `explain/templates/agents.py`: unchanged (no new finding codes in Wave 5).

## S2 Policy and the atomic record (B2, B6)

- `policy.Action.vendor_id: str | None = None` (after `vendor_key`). Filled wherever the vendor is known: `guard.invoice_action` from `vendor["id"]`; `vendor_routes` patch and bank-change propose/decide from the path `vendor_id`; `_judge` via `service.get_vendors().find_by_name(row["vendor"])` when a vendors store exists and a vendor was named. `vendor.create` has none (the vendor does not exist yet).
- `findings/store.py`: the body of `FindingStore.upsert` moves to `_upsert(self, s, ctx, spec, *, hold=False) -> tuple[str, str]` using the caller's session; `upsert` keeps its signature and wraps `_upsert` in `tenant_session` (precedent: `transition` composes `audit_event_row` + `outbox.emit` in one session).
- `record.record(..., s=None, hold: bool = False)`: one session for the ledger row (`store.record_action(s, …)`), the finding (`findings._upsert(s, …, hold=hold)`), the `finding_id` stamp on the row object and `outbox.emit(s, …)` for `agent.action.blocked`. `s=None` opens `tenant_session(ctx.tenant_id, store.url)`; a caller's `s` is used as is (the guard passes its lock session). The `acted_on` graph edge is drawn after the session block with the existing `graph.upsert_edge` call (lineage only; no FK on the agent row). `details["hold"]` mirrors `hold`. `record.person` and `record.injection` keep their own sessions. The module docstring drops "ordered, not atomic".
- `record` and the finding store must point at the same database: `record` asserts `findings.url == store.url` (both `None` in production = the deployment's database; tests build both on one scratch url). A failure anywhere in the one session rolls back the ledger row too; the guard's existing handler then answers as B8 says — 503 in enforcing (the action is not taken), a logged no-op below (the action proceeds unrecorded, as any guard failure does today).
- `guard._one` passes its lock session into `_record` → `record(..., s=s)`. `store._lock_query` drops `key_share=True` (plain `FOR UPDATE`): the ledger insert's FK check now runs on the same connection, so the Wave 4 deadlock workaround is gone; the comment goes with it. Writers outside the guard (`record.person`, `record.injection`, the log import, `set_outcome`) now wait for a held agent lock instead of passing it: accepted, the guard holds it for one evaluation (my call).
- `guard._check`, list decisions: when action *k* of a list is refused (403), stamp `failed` on the ledger rows already recorded for actions 1…k−1 (`store.set_outcome`) before re-raising, so the ledger never shows allowed creates that were not written. Today's vendor load has the same gap; this fixes both.
- `store.count_since` (velocity) counts distinct `request_id`s, a row without one counting alone: one bulk request (a vendor load, a PO import) is one burst of activity, not N (my call; the velocity threshold keeps its meaning of "requests in the window").
- `api/trust_agent_routes._judge`: resolves `vendor_id` (above; `vendors.normalized_name` is unique per tenant, so `find_by_name` gives one vendor or None) and, for a subject `invoices:<case_id>`, `transaction_node = service._trust_node(tenant_id, case_id)` (None when the case is unknown). Nothing else in `_judge` changes; `hold` stays `False` here (branch B flips it in `record`).
- `trust/agents/store.py`:
  - `count_blocked(tenant_id, since) -> dict`: `{"blocked": n, "reviews": m}` over rows with `occurred_at >= since` and `source != "dryrun"`. `blocked` = rows that were actually stopped: `decision == "BLOCK"`, `mode == "enforcing"` and either `source == "inline"` (refused) or `source == "log"` with `subject_table == "invoices"` and `transaction_node` set (held, branch B). `reviews` = every other non-`ALLOW` row (a BLOCK below enforcing, a log BLOCK that held nothing, ALLOW-WITH-REVIEW).
  - `purge_before(tenant_id, before) -> int`: deletes rows with `occurred_at < before` and `action != "vendor.create"`; returns the count.

## S3 Routes and isolation (B3, U1 stub)

- `GET /api/trust/agents/blocks/count` (Approver), declared with the other static paths before `/{agent_id}`: `since = now − agents.banner_window_hours` from the merged thresholds; answer `{"blocked", "reviews", "window_hours"}`. Listed in `tests/test_trust_agent_routes.PROBED` with a two-tenant probe.
- `POST /v2/agents/actions` as a named stub `intake_actions(principal: Approver, body: dict)` in `api/v2_routes.py` raising `HTTPException(503, "Agent intake is not built yet.")`. `tests/test_tenant_isolation_api.py` gets a `TestWave5StubsIsolation` with `STUBS = [("post", "/v2/agents/actions", "approver")]`, the stub set unioned in `TestCoverage._probed`, and the hand-over already in `branch_probed()`: once `tests.test_trust_api_routes.PROBED` lists the path the stub check skips it. The entry goes at wave close.
- `docs/API_V2.md`: one row `| POST | `/v2/agents/actions` | approver (API key or user) | — |` plus the line "Not built yet (503); branch C fills it", so `test_the_doc_lists_every_v2_route_and_nothing_else` stays green.

- `GET /api/trust/agents` rows gain `mandate`: the agent's current mandate annotated as `list_mandates` annotates it (`version, valid_to, expired, verified, status`), or `null`. One `current_mandate` read per agent server-side; the agents screen needs it per row and must not fetch N agents (my call).
- `scripts/e2e_api.py environment()` sets `TRUST_MANDATE_KEY` to a fixed test value so the e2e server can approve and verify mandates (branch A's spec depends on it).

## S4 Frontend shell (B10)

- `frontend/src/lib/api.ts`: types `Agent` (`id, name, owner, description, kind, status, principal_id, principal_email, registered_by, revoked_at, revoked_by, revoke_reason`), `Mandate` (`id, agent_id, version, scope, valid_from, valid_to, status, expired, verified, proposed_by, approved_by, approved_at, revoked_by, revoked_at, reason`), `AgentLedger` (the shape `ledger.agent_ledger` returns), `TenantLedger` (`ledger.tenant_ledger`), `AgentBlockCount` (`blocked, reviews, window_hours`). Copied from the Python return shapes; the builder reads `trust/agents/{store,mandates,ledger}.py`. Branch A may correct these agent types in place if a shape proves different (the only exception to `api.ts` being scaffold-owned).
- `AppShell.tsx` NAV: `{ href: "/agents", label: "Agents", glyph: "⌁", min: "approver" }` after "Trust engines". One line; the banner block is branch A's.
- `frontend/src/app/agents/page.tsx`: stub with the shared skeleton (`usePoll<Me>("/api/me")`, role check `can(me, "approver")`, `<h1>Agents</h1>`, an `empty-state` "No agents registered."). Branch A replaces its body.
- `tests/test_frontend_reaches_the_flow.py VIEWS += "agents"`; `frontend/e2e/golden-path.spec.ts` sidebar array += `"Agents"` (test count unchanged).

## S5 Docs

`docs/PENDING.md` Wave 4 block: item 8 becomes a Wave 5 block listing the four branches and their scope; "mandate expiry sweep" ticked as not needed (expiry computed on read; a status rewrite would break the signature); G1 (drift detector) and G2 (Controller approval of registration) listed as surfaced, not built.

## Tests

- `tests/test_trust_agents_policy.py`: `vendor_id` defaults None; an atomicity test — `FindingStore._upsert` patched to raise → `record` raises and leaves no `agent_actions` row and no outbox row.
- `tests/test_trust_agents_store.py`: `count_blocked` (window, dryrun excluded, inline enforcing BLOCK and held-shape log BLOCK count as blocked, a log BLOCK on a vendor subject and a BLOCK below enforcing count as reviews), `purge_before` (keeps `vendor.create`, returns the count, per tenant), `count_since` counts one bulk request once.
- `tests/test_trust_agents_guard.py`: a vendor load whose third create is refused stamps the first two `failed`; a record failure under the lock in enforcing → 503 and no ledger row.
- `tests/test_trust_agents_guard.py`: the existing lock/concurrency test (`test_two_concurrent_approvals_cannot_both_fit_one_cap`) still passes with the plain `FOR UPDATE`; `test_record_runs_while_the_guard_holds_the_agent_lock` adjusted to the one-session shape.
- `tests/test_trust_agent_routes.py`: the count route (two tenants, window, approver only); `_judge` fills `vendor_id` and `transaction_node` for a known invoice.
- `tests/test_trust_api_routes.py`: doc test green with the stub row; `tests/test_tenant_isolation_api.py`: stub 503 for both tenants.
- Frontend: `npm run lint && npx tsc --noEmit && npm run build`; e2e 47; `tests/test_frontend_reaches_the_flow.py` with `agents`.
- Wave gates unchanged: eval `--seed 7` 56/56; Postgres `tests/test_trust_*.py`.
