# Enforce and extend mandate signatures (Wave 4 follow-up)

**Goal:** an agent's action is allowed only under a mandate whose signature verifies, and the signature covers the mandate's state (status and revocation) as well as its content, so a database edit that revives, un-revokes or alters a mandate blocks the agent instead of authorising it.

**Why:** the Wave 4 audit (PENDING, top item) found `verified` shown but not enforced, and the signature covering neither `status` nor `revoked_*`. User chose (2026-10-23): enforce, and sign revocations too.

**Design (decided in chat):**
- `digest` stays the content digest (`store.SIGNED`), so the version chain (`prev_digest`) is unchanged by state changes.
- `signature` = HMAC (per-tenant key from `TRUST_MANDATE_KEY`) over `"{digest}|{status}|{revoked_by}|{revoked_at}"` — `mandates.seal(row)`. No migration.
- Every state change re-seals exactly the rows it changed (never "whatever does not match", which would bless a tampered row): `mandates.approve` (the approved row and the active row it supersedes), the revoke route, `registry.revoke` (every mandate the agent revoke closed). The store reports what it changed: `approve_mandate(...)["superseded"]`, `revoke(...)["revoked_mandates"]`.
- Revocation is never refused for a missing key: the row is revoked and left unsealed (`verified: false`, and not in force).
- `verify(row, prev)`: chain and content digest as today; a `proposed` row with no approver needs no signature; any other row needs `signature == seal(row)`.
- `rules.mandate_active`: after the revoked/no-mandate checks, load the agent's stored versions; BLOCK `AGENT-OUT-OF-MANDATE` when the stored row of the mandate in force does not verify, or when a later version had been approved by the action's time (`occurred_at`, else now) — a replayed superseded version.

**Known limit (stays in PENDING):** replaying an entire earlier row, signature included, cannot be detected from inside the database; it needs an anchor outside it.

## Tasks (TDD; one commit each)

1. `mandates.seal` / `verify` over state; store reports changed ids; `approve`, revoke route and `registry.revoke` re-seal. Tests in `tests/test_trust_agents_mandates.py`: a revoked row verifies; flipping `status` (or `revoked_at`) in the DB fails; a proposed row flipped to `active` without a signature fails; re-sealing never touches a row the call did not change; revoke without a key still revokes.
2. `rules.mandate_active` enforces it. Tests in `tests/test_trust_agents_rules.py`: valid mandate passes; altered or unsigned mandate BLOCKs; a later version approved before the action BLOCKs; an imported log action judged by the version then in force still passes.
3. Move existing tests that use stand-in signatures (`signature="sig"`, unsigned dict mandates) onto really sealed mandates under a test key.
4. PENDING: tick the item; keep the replay limit.

**Done:** ruff as CI; SQLite suite; Postgres agents/isolation tests; eval `--seed 7` 56/56 (the fleet signs through `mandates.approve`); fable review; one fix pass; PR.
