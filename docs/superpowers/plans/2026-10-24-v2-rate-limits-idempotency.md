# /v2 rate limits and idempotency keys Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Per-caller rate limits on every `/v2` request and `Idempotency-Key` replay on `/v2` POSTs, stored in the database so they hold across containers and restarts.

**Architecture:** One HTTP middleware for paths under `/v2` (`invoice_processing/api/v2_guard.py`): resolve the caller once (reused by `current_principal` through `request.state`), count the request in a per-minute window row, answer 429 over budget, and for a POST carrying `Idempotency-Key` replay or record the response. Two tenant tables (one migration). A daily worker sweep deletes expired rows.

**Tech Stack:** FastAPI/Starlette middleware, SQLAlchemy 2 (dialect upsert), Alembic, pytest.

**Spec:** agreed in chat 2026-10-24 (the user said "go ahead" to the proposal and to database-backed counters).

## Decisions (from the chat)

- **Idempotency** (POST on `/v2` only): header `Idempotency-Key`, 1–128 visible ASCII characters (else 400). Keyed by tenant + caller (`Principal.user_id`, i.e. the API key or the user) + key, kept **24 h**. First request stores the request-body SHA-256, then the final 2xx status and body; a retry with the same key and body gets the stored response with `Idempotent-Replay: true`; same key, different body → **422**; a retry while the first is still running → **409**. A non-2xx answer deletes the row (the caller may retry). No header → unchanged.
- **Rate limit**: every `/v2` request (any method) from an authenticated caller counts in a fixed one-minute window per caller; default **120 per minute**, tenant-settable through the trust thresholds key `v2.rate_limit_per_minute`. Every answer carries `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset` (seconds to the window's end); over budget → **429** with `Retry-After`. `/api` is untouched. An unauthenticated request passes through to the route (which answers 401) uncounted.
- **Storage**: database tables, not process memory, so the limit stays true however many containers run.

## Builder's calls

- One middleware instead of per-route decorators: it covers every `/v2` route uniformly and is the only place response bodies can be captured. `auth.current_principal` reuses a principal the middleware resolved (`request.state.principal`) so an API key is looked up once per request.
- A replayed response is stored only when ≤ 256 KB; a larger 2xx body is answered normally and its row deleted (a retry then runs again) — no `/v2` POST answers that much today.
- Tables: `api_rate_windows (tenant_id, principal, window_start, count)` unique `(tenant_id, principal, window_start)`; `api_idempotency (tenant_id, principal, key, request_hash, status in_progress|done, status_code, body JSON, content_type, expires_at)` unique `(tenant_id, principal, key)`. Both in `TENANT_TABLES` and the migration's `RLS_TABLES`; migration `d4e5f6a7b8c9` revises `c3d4e5f6a7b8`.
- Expiry is enforced on read (an expired row counts as absent and is replaced); the worker's daily sweep (beside the retention sweep) deletes expired idempotency rows and windows older than a day, per active tenant.

## Global Constraints

- No behaviour change for `/api` or for a `/v2` caller without the header under the limit (apart from the three `X-RateLimit-*` headers).
- Tenant isolation: every query in a `tenant_session`; a key from tenant A never replays tenant B's response (the key is scoped by tenant and principal).
- No ids, keys or bodies in log `extra`.

## Review Focus

- Two concurrent first requests with one key → exactly one runs, the other gets 409.
- A 500 or 4xx first attempt → the row is gone and the retry runs.
- The same key from two different API keys of one tenant → independent.
- The limit at exactly the budget: request number 120 passes, 121 gets 429.
- A request whose auth fails → 401 from the route, not counted, no idempotency row.

---

### Task 1: Tables

**Files:** migration `invoice_processing/db/migrations/versions/d4e5f6a7b8c9_v2_rate_idempotency.py`, `invoice_processing/db/models.py` (`ApiRateWindow`, `ApiIdempotency`, `TENANT_TABLES`), `tests/test_restore_check.py` pin, `tests/test_gcs_and_migrations.py` `("d4e5f6a7b8c9", 2)`.

- [ ] Failing: restore pin and migration-param tests; implement; alembic upgrade/downgrade round trip on Postgres; commit.

### Task 2: The guard

**Files:** `invoice_processing/api/v2_guard.py` (middleware body + store helpers), `invoice_processing/api/service.py` (register it inside the request-id middleware), `invoice_processing/api/auth.py` (`current_principal` reuses `request.state.principal`), `invoice_processing/trust/thresholds.py` (`v2.rate_limit_per_minute: 120`), tests `tests/test_v2_guard.py` (new; fixture as `tests/test_trust_api_routes.py`), `tests/test_trust_settings.py` snapshot.

- [ ] Failing tests: headers on a normal `/v2` GET; 429 + `Retry-After` past the limit (limit monkeypatched to 3); `/api` uncounted; unauthenticated → 401 uncounted; idempotent POST replays (`Idempotent-Replay: true`, same body, the handler ran once); same key different body → 422; an in-progress row → 409; a failing first attempt → retry runs; two API keys with one Idempotency-Key are independent; another tenant's key with the same Idempotency-Key never sees the first response; a bad key (empty / 129 chars / control characters) → 400.
- [ ] Implement; run the new file, `tests/test_trust_api_routes.py`, `tests/test_tenant_isolation_api.py`, `tests/test_auth.py`; commit.

### Task 3: Sweep and docs

**Files:** `invoice_processing/worker/runner.py` (expired rows in the existing daily retention sweep, or a sibling `_x_if_due`), `docs/API_V2.md` ("Limits and retries" section replacing "There is no rate limit and no idempotency key yet"), `docs/PENDING.md` tick.

- [ ] Failing sweep test; implement; commit.

### Task 4: Close-out

- [ ] Full suite; Postgres (fresh database) trust + isolation + new file; concurrency check on Postgres for the 409 race; eval 56/56; fable review; one fix pass; PR; merge on the user's word.
