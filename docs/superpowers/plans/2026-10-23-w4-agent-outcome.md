# Count only what happened: agent action outcomes (Wave 4 follow-up)

**Goal:** an agent's approval that fails after the policy check (already decided, routing refused or unreachable, books unavailable) no longer uses up its spend cap, and the ledger's "exported" counts each invoice once.

**Why:** Wave 4 audit (PENDING): the ledger records decisions before the write (B3), so retries that answer 409 still count, and a repeated export doubles "paid". User chose (2026-10-23): outcome stamp, plus de-duplicated exports.

**Design (decided in chat):**
- Migration `c3d4e5f6a7b8` (revises `b2c3d4e5f6a7`): `agent_actions.outcome` String(16), nullable. No table, so `RLS_TABLES = []`.
- `guard.settling(decision)`: a context manager the approve/reject (`service.act`) and export (`service.export_invoice`) call sites wrap around their write. It stamps `executed` when the body finishes and `failed` when it raises (any exception, `HTTPException` included), then re-raises. No-op when the guard returned `None`. `store.set_outcome(tenant_id, action_id, outcome)`.
- Unstamped rows (a crash mid-request, imported logs) still count: caps stay conservative, and two approvals in flight both count until one fails, so the B17 lock still holds.
- `store.spent` and the ledger's approved sums skip `outcome = 'failed'`.
- The ledger's `exported` sums one export per invoice (`subject_ref`), the latest non-failed one. Approvals are already one per decision.
- Vendor and bank-change call sites are unchanged (they do not affect spend).

## Tasks (TDD; one commit each)

1. Migration + model + pins (`tests/test_restore_check.py`, `tests/test_gcs_and_migrations.py` `("c3d4e5f6a7b8", 0)`), `store.set_outcome`; `spent` skips failed. Tests in `tests/test_trust_agents_store.py`.
2. `guard.settling`; wrap `act` and `export_invoice`. Tests in `tests/test_trust_agents_guard.py`: an approve that then answers 409 is stamped `failed` and does not count; a successful approve/export is `executed`; with the engine off nothing is written.
3. Ledger: approved skips failed; exported counts each invoice once. Tests in `tests/test_trust_agent_routes.py`.
4. PENDING ticks.

**Done:** ruff as CI; SQLite suite; Postgres agent tests; alembic up/down on Postgres; eval `--seed 7` 56/56; fable review; one fix pass; PR.
