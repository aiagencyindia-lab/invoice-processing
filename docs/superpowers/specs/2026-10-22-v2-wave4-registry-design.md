# Wave 4 — Registry, mandates, ledger, routes design (`v2/w4-registry`)

**Program plan:** `docs/superpowers/plans/2026-10-22-v2-wave4-program.md`, branch A. FR-28.1, FR-28.5, FR-28.6. Decisions U2, U4, U6, U8; calls B1, B3, B4, B9.

**Owns:** `trust/agents/{registry,mandates,ledger}.py`, `api/trust_agent_routes.py` (fills the stubs), `tests/test_trust_agent_routes.py` (exports `PROBED`), `tests/test_trust_agents_mandates.py`, its plan doc. Uses the scaffold's `AgentStore`, `policy.evaluate_action`, `record.record`.

## Registry

`register(ctx, body, *, store, url)`. `body = {name, owner (email), description?, principal: {api_key_id} | {user_id, email} | null}`.
- `api_key_id` → the tenant's own live (unrevoked) key, else 404; `principal_id = f"api-key:{id}"`, `principal_email = f"{key.name}@api-key.{tenant_id}"`, `kind = "api_key"`.
- `{user_id, email}` → `kind = "user"` as given (a service account the tenant created).
- absent → `kind = "external"`, no principal (log-only agent).
- A principal already bound to an agent → 409. An API key whose derived email is shared with another live key → 409 (rename the key; B19). `owner` must be a syntactically valid email; it is a person, not checked against users (owners can be outside the app).
Audits `agent_registered {name, kind, owner}` / `agent_revoked {name, reason}`. Revoking an agent revokes its active mandate (reason `agent revoked`).

## Mandates

- `validate_scope(scope)`: `actions` non-empty ⊆ `ACTIONS`; `counterparties` null or list of vendor names normalised with `po_matching.normalize_vendor`; `caps` keys ⊆ `{per_action, per_period, per_vendor, per_category}`, amounts non-negative `Decimal` strings, `per_period.period ∈ {day, month}`; `currency` required (ISO code; U10); `valid_from < valid_to`; `valid_to - valid_from ≤ 366 days`. 400 with the first problem.
- `canonical(row) -> bytes`: JSON of `{tenant_id, agent_id, version, scope, valid_from, valid_to, proposed_by, approved_by, approved_at, prev_digest}` with `sort_keys=True, separators=(",", ":")`, datetimes ISO UTC. `digest = sha256(canonical)`; `signature = hmac_sha256(hmac_sha256(TRUST_MANDATE_KEY, tenant_id), digest)`.
- Propose: refused (409) while a version is `proposed` (B16); version `max+1`, `prev_digest` = digest of the agent's latest version (any status), digest computed with `approved_by = approved_at = null`, no signature. Approve: only the latest version; a different admin (`SelfApproval` → 403); recompute digest with the approver and time, sign; `TRUST_MANDATE_KEY` unset → 503 `"Mandate signing is not configured."`, nothing changes. `verify(row)` → `prev_digest` matches the previous version's digest; digest and signature recomputed and compared with `hmac.compare_digest`; proposed rows verify on digest alone.
- Audits `agent_mandate_proposed {version}`, `agent_mandate_approved {version}`, `agent_mandate_revoked {version, reason}` (no scope dump in audit; the row is the record).

## Ledger (FR-28.6)

`agent_ledger(tenant, agent_id, *, since, until, now)` → `{agent, mandate (current, verified), spend: {approved, exported, by_vendor: {key: amt}, by_category: {kind: amt}, period: {period, cap, used, remaining} | null}, actions: [latest 200], exceptions: [non-ALLOW in range]}`; approved = non-BLOCK `invoice.approve` amounts; exported = non-BLOCK `invoice.export` amounts (B9). `tenant_ledger(tenant, *, since, until)` → per owner `{owner, agents, approved, exported, blocked, reviewed}`. Defaults: `since` = start of the current month, `until` = now.

## Routes

Paths and roles as the program plan's table. `Admin` = `integration_routes.Admin` (human admin; API-key principals 403). `actions/evaluate`: approver, or the agent's own principal on its own id; body `{action, subject_ref, vendor?, category?, amount?, currency?, occurred_at?, source: "dryrun" | "log"}` builds an `Action` (vendor normalised to `vendor_key`) and runs `evaluate_action` + `record(source=…)`; the agent's engine mode is recorded but `dryrun` never refuses, counts, raises a finding or fires a webhook (B15); `log` rows count and raise findings, measured at their `occurred_at`, and never fire `agent.action.blocked`. `actions/import`: multipart CSV/XLSX, ≤ 5 000 rows, read with `history/readers.read_table`; each row resolves `agent` by name or principal email (unknown → row error); returns `{rows, by_decision, errors: [{row, message}]}`; one bad row never stops the rest. All money `Decimal`; bad amounts are row errors.

## Tests

Probes for all twelve paths with bearer and foreign API key (`PROBED`); registry 409/404; key principal 403 on every admin route; mandate validation table; self-approval 403; key unset 503; proposed mandate → evaluate returns BLOCK `AGENT-OUT-OF-MANDATE`; expired; supersede; signature verifies, fails after editing `scope` or `valid_to` in the DB; ledger sums by vendor/category/period; import happy path + mixed bad rows; dryrun never 403s.

**Done:** ruff; suite both dialects; probes; stub hand-over (the twelve paths leave the stub set).
