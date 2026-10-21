# Wave 3 — API keys, `/v2` façade, webhooks (FR-30, PRD §9) design (`v2/w3-api`)

**Program plan:** `docs/superpowers/plans/2026-10-21-v2-wave3-program.md`. **Scaffold contracts:** `docs/superpowers/specs/2026-10-21-v2-wave3-scaffold-design.md`. User decision U5 (per-tenant API keys, HMAC-signed webhooks). Builder's calls B2 (outbox + sweep, `requests`, stdlib `hmac`), B6 (thin façade, resolver hook).

**Owns:** `invoice_processing/trust/apikeys.py` (fills the stub), `invoice_processing/trust/webhooks/deliver.py` (fills), `invoice_processing/api/integration_routes.py`, `invoice_processing/api/v2_routes.py`, `tests/test_trust_api*.py`, `tests/test_trust_webhooks*.py`, `docs/API_V2.md`, its plan doc. Nothing else (the outbox emit side, `auth.py` hook, models and worker sweep are the scaffold's).

## API keys (`apikeys.py`)

- `create(ctx, name, role) -> (row_dict, plaintext)`; plaintext `ik_{prefix}_{secret}` with `secrets.token_urlsafe(32)`; `key_hash = sha256(secret)`; `role ≤ approver` (admin keys refused). Audit `api_key_created {name, role, prefix}`.
- `resolve(token) -> Principal | None`: parse the prefix, look the row up through an engine-level session (never a tenant session — the prefix lookup is cross-tenant by nature, as `_read_heartbeat_row` does), `hmac.compare_digest` on the hash, refuse `revoked_at`, touch `last_used_at` best-effort; `Principal(user_id=f"api-key:{id}", email=f"{name}@api-key.{tenant_id}", tenant_id, role)`. A malformed or unknown key, or a key of a closed tenant (`Tenant.status != "active"`, the rule `sso.enforce_active` applies to users) → `None`, which `auth` turns into a 401. A signed-in user's bearer always wins over a key header (scaffold).
- `revoke(ctx, key_id)`; audit `api_key_revoked`.
- Routes (admin): `GET /api/integrations/api-keys`, `POST` `{name, role}` → 201 with `key` shown once, `DELETE /api/integrations/api-keys/{id}`. A key-made principal (`user_id` starting `api-key:`) gets 403 on every `/api/integrations/*` route.

## Webhooks

- Routes (admin): `GET/POST /api/integrations/webhooks` (`{url https-only, events ⊆ EVENTS or ["*"], description}` → secret shown once, masked afterwards), `PATCH /{id}` (`active`, `events`, `description`), `DELETE /{id}`, `POST /{id}/test` (emits `ping`), `GET /{id}/deliveries` (last 50 outbox rows, payload masked through `masking.mask_row`).
- `deliver.due(now, *, post=requests.post, limit=50) -> int`: for each tenant with rows due (`next_attempt_at ≤ now`, `delivered_at IS NULL`, `dead_at IS NULL`), POST the JSON body with headers `X-Trust-Signature: sha256=<hex HMAC-SHA256(secret, f"{ts}.{body}")>`, `X-Trust-Timestamp: <ts>`, `X-Trust-Event`, `X-Trust-Delivery: <row id>`, `timeout=10`. 2xx → `delivered_at`, `last_status`; otherwise `attempts += 1`, `next_attempt_at = now + min(2**attempts, 64) minutes`, `last_status`, `last_error` (masked, ≤ 500); `attempts ≥ 8` → `dead_at`, audit `webhook_dead_lettered`. Returns the number attempted. Driven by the worker sweep (scaffold).
- `docs/API_V2.md` documents auth, events, signature verification (a 6-line Python example) and the `/v2` table.

## `/v2` façade (`v2_routes.py`, `APIRouter(prefix="/v2")`)

Every handler is import + call + return; no reshaping beyond the path. Transaction id = invoice graph node id (what `/v2/findings` rows carry).

| v2 | calls |
|---|---|
| `GET /v2/transactions/{id}/trust` | `trust_score_routes.transaction_trust` |
| `GET /v2/transactions/{id}/explanation` | `trust_routes.list_findings` by `transaction_node` + `trust_explain_routes` explanation per open finding → `{transaction, findings: [{finding, explanation}]}` |
| `GET /v2/transactions/{id}/lineage` | `trust_routes.get_lineage` |
| `GET /v2/findings` | `trust_routes.list_findings` |
| `POST /v2/findings/{id}/decision {decision: accept|reject|needs_vendor_input, reason}` | `trust_routes.transition_finding` (detected → triaged first when needed) |
| `GET /v2/vendors/{id}/trust` | `trust_score_routes.vendor_trust` |
| `POST /v2/documents/{id}/authenticity` | `trust_provenance_routes.score_document` |
| `GET /v2/evidence/{node_id}` | `trust_routes.get_node` |
| `POST /v2/trust-audits`, `POST /v2/trust-audits/{id}/runs`, `GET /v2/trust-audits/{id}/report` | `trust_audit_routes.create_audit / request_run / get_report` |

No rate limiting and no idempotency keys this wave (`expect` on transitions already guards); PENDING if partners ask.

## Tests

Key lifecycle and resolver (bad / revoked / other-tenant key → 401; a valid bearer wins over a key; a key never changes a bearer's tenant); façade parity (each `/v2` response equals the `/api` one for the same principal); emission from store / evaluate / mode change produces outbox rows per subscribed endpoint; delivery with a fake `post` (success, backoff schedule, dead-letter + audit, signature verifiable with `hmac`); two-tenant probes for every `/v2` and `/api/integrations` path with a bearer **and** with a foreign tenant's API key (`tests/test_trust_api_routes.py` exports `PROBED`).

**Done:** ruff; suite on both dialects; probes; `docs/API_V2.md` matches the router.
