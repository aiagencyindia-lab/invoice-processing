# Data lifecycle — design

SOC 2 readiness, sub-project 5 of 5. Brainstormed with the user 2026-10-13;
the four decisions and the four design sections were approved in turn.

## Why now

Sub-projects 1–4 (audit integrity, hardening, SDLC evidence, logging) are
merged. An auditor's remaining questions are "how long do you keep customer
data", "what happens when a customer leaves" and "show me you can restore".
Today there is no answer to any of them: nothing deletes a tenant, nothing
writes the retention rule down, and no backup or restore procedure exists.

## Where we start

Measured 2026-10-13 on `main` (`b6ee813`):

- **`tenants.status`** exists (default `active`) but only
  `TenantRepository.tenant_id_for_slug` (inbound email) reads it. Sign-in and
  the API ignore it, so a tenant marked anything else still works.
- **`GcsBlobStore.delete`** exists, documented "only for retention and tenant
  offboarding", and nothing calls it. `LocalBlobStore` has neither `delete`
  nor `list_keys`. Local blobs live under `DATA_DIR/blobs/<tenant_id>/`; GCS
  blobs under `t/<tenant_id>/`.
- **`audit_events`** has SELECT and INSERT policies only, and RLS is forced.
  So even the owner role's `DELETE FROM audit_events` removes zero rows
  silently. The app role also has UPDATE/DELETE/TRUNCATE revoked (PR #18).
- **26 `TENANT_TABLES`**, all with `tenant_id` → `tenants.id`. A test checks
  every listed table has `tenant_id`; nothing checks the reverse.
- **`admin/provision.py`** is the operator CLI (`create-tenant`, `add-user`,
  `set-claims`, `list`).
- **No backup or restore documentation**; `deployment/README.md` covers Agent
  Runtime only. No `pg_dump`/`psql` on the dev machine (Docker is there).
- Production is down (closed billing), so a real Cloud SQL restore cannot be
  exercised in this sub-project.

## Decisions

1. **Goal: readiness evidence.** Written policies plus the minimum tools that
   enforce them; no customer-facing UI.
2. **Offboarding: suspend, then purge everything.** Suspension is immediate.
   After a 30-day recovery window the purge deletes every tenant row,
   document and audit event. The tenants row survives as the record.
3. **Retention: for the life of the account.** The customer is the
   record-keeper under its own tax law (5 years AU, 6–8 under India GST); we
   do not delete by age.
4. **Backups: a runbook plus a scripted restore check**, run once locally now
   and again on the real database at the restore.
5. **The 30 days are a recovery window, not an export window.** A suspended
   tenant cannot sign in, so customers export before their subscription
   ends. A late export means reactivating briefly.

## Design

### 1. Suspension

**States.** `tenants.status`: `active → suspended → purged`.
`reactivate-tenant` moves `suspended → active`. Nothing leaves `purged`.

**Commands** (`admin/provision.py`, logic in `invoice_processing/offboarding.py`):

- `suspend-tenant --tenant <slug> --operator <name> --reason "<text>"`:
  refuses unless `active`; sets `suspended`; writes
  `settings.offboarding = {suspended_at, suspended_by, reason}`; emits a
  `tenant_suspended` audit event in the tenant.
- `reactivate-tenant --tenant <slug> --operator <name>`: refuses unless
  `suspended`; sets `active`; adds `reactivated_at/by` to the record (the
  next suspension overwrites `suspended_*`); emits `tenant_reactivated`.

Both event names are mapped in `repositories._ENTITY_TYPES` (`tenant`).

**Enforcement, one shared point per path:**

- **API and sign-in.** `PgUserDirectory.find` already joins `tenants` for the
  name; it also returns `tenant_status`. `sso._refuse_inactive`, which both
  the token path and the dev bypass call on every request, refuses any
  status other than `active` with 403 "This organisation's account is
  suspended. Contact support." and emits `sign_in_refused` with reason
  `tenant_suspended`. The tenants row is guaranteed by the foreign key, so a
  missing status is treated as a refusal, not as active.
- **Domain-bound join** (`sso.resolve_principal`, no tenant claim): the bound
  domain's tenant must be `active`, else the same 403.
- **Email intake.** `tenant_id_for_slug` already matches `active` only.
- **Worker.** `Worker._handle`, before claiming a document job, looks up the
  tenant's status; non-active → dead-letter with reason "tenant suspended"
  (not dropped: a reactivated tenant's operator can replay it). Rematch
  messages get the same check.
- **`add-user` / `set-claims`** refuse a non-active tenant.

A failed status lookup is a 503 in the API and a nack in the worker, never
a pass.

### 2. Purge

`purge-tenant --tenant <slug> --operator <name> --confirm <slug>
[--skip-grace --reason "<text>"]`

**Refusals:** `--confirm` differs from `--tenant`; status is not
`suspended`; suspended less than `GRACE_DAYS = 30` ago without
`--skip-grace`; `--skip-grace` without `--reason`; on Postgres, no
`MIGRATE_DATABASE_URL` (the purge never runs as the app role). SQLite (local
and tests) uses `DATABASE_URL`.

**Order** — a crash must never leave a `purged` record over data that is
still there:

1. **Documents.** The tenant's blob store (`service._blob_store` logic,
   shared) lists every key and deletes it. `LocalBlobStore` gains
   `list_keys()` and `delete(key)` with the same tenant-root confinement as
   `_path`.
2. **Database, one transaction.** Set `app.tenant_id` and `app.purge_tenant`
   to the tenant id. Delete from each table in `TENANT_TABLES`, ordered by
   `reversed(Base.metadata.sorted_tables)` so foreign keys resolve and a new
   table needs no edit here; record each row count. Then set the tenants row
   to `status = purged` and replace `settings` with only
   `{"offboarding": {suspended_at, suspended_by, reason, purged_at,
   purged_by, skip_grace, skip_reason, rows_deleted: {table: n},
   blobs_deleted: n}}`. Id, slug, name and region stay; the slug stays
   reserved so an old inbound address cannot route to a new tenant.
3. **Verify.** Count the tenant's rows in every tenant table and list its
   blobs again. Anything left → exit 1 with the leftovers named.
4. **Print the record** as one JSON line for the operator to file in the
   offboarding register (see §3).

A failure in 1 or 2 leaves the tenant `suspended`; re-running is safe.

**Audit purge policy.** New Alembic migration after `c9d5e2f3a4b6`:

```sql
CREATE POLICY audit_events_purge ON audit_events FOR DELETE
    USING (tenant_id = current_setting('app.purge_tenant', true));
```

`rls.enable_rls_statements` / `disable_rls_statements` gain the same policy
so fresh databases match; the migration's downgrade drops it. The app role's
DELETE on `audit_events` stays revoked, so setting the variable gives it
nothing; only the owner role can use the policy.

**Untouched by the purge:** Identity Platform accounts (no users row → refused;
the runbook deletes them in the console), logs and Sentry (ids only, own
expiry), backups (§3).

### 3. Backup and restore

**`scripts/restore_check.py --restored <url> [--source <url>]`** — run after
any restore, however it was made. Exit 0 = pass; otherwise each failure on
its own line.

- `alembic_version` equals the repo head.
- Every `TENANT_TABLES` table has `relrowsecurity` and `relforcerowsecurity`
  and its expected policies (from `rls.py`), including
  `audit_events_purge`.
- The app role (`--app-role`, default `ledgerly_app`) is `NOBYPASSRLS` and
  lacks UPDATE/DELETE/TRUNCATE on `audit_events` — catches a restore that
  skipped bootstrap §2.
- Prints row counts per table per tenant, the newest
  `audit_events.created_at` (the effective restore point), and every
  non-`active` tenant.
- With `--source`: any row-count difference fails (for the local drill,
  where the source is still).

It reads tables as the owner role with `app.tenant_id` set per tenant
(tenant ids from `tenants`, which has no RLS).

**Local drill, now:** seed a Postgres 16 container through the app, then
(inside the container, for `pg_dump`) `pg_dump -Fc` → fresh container →
bootstrap §1 → `pg_restore` → bootstrap §2 → `restore_check --source
--restored`. Commands and the recorded output go in the runbook.

**`deployment/BACKUP_RESTORE.md`:**

- Cloud SQL: automated daily backups, 7 kept; point-in-time recovery with 7
  days of logs; restore = clone/restore to a **new instance**, bootstrap §2,
  `restore_check`, repoint `DATABASE_URL` / `MIGRATE_DATABASE_URL`.
- GCS: soft delete (7 days) on the documents bucket. Documents are write-once,
  so soft delete covers accidental deletion; no object versioning.
- Targets: RPO minutes (PITR), RTO 4 hours, drill every 6 months and after any
  production restore. A drill log section records each run.
- **Restored purges.** A backup from before a purge brings the tenant back as
  `suspended`, because its record is in the same database. After any
  restore: for each register entry purged after the restore point, run
  `purge-tenant --skip-grace --reason "re-purge after restore"`.
  `restore_check` lists suspended tenants as the prompt. Backups last 7 days,
  so the exposure is at most 7 days old.

### 4. Policies and documents

- **`SECURITY.md` → "Data lifecycle":** retention for the life of the
  account; offboarding (export before the subscription ends, suspension,
  30-day recovery window, full purge including the audit trail, immediate
  deletion on request); residual copies (backups ≤ 7 days, GCS soft delete
  ≤ 7 days, Railway logs 7 days, Sentry 90 days, ids only); backup targets;
  links to the runbooks.
- **`deployment/OFFBOARDING.md`:** operator steps — confirm the request,
  suspend, wait (or `--skip-grace` with the customer's written request),
  purge as the owner role, delete the Identity Platform users, file the JSON
  record in the offboarding register (a private document outside the
  database).

### 5. Tests

TDD; each new test proved failing on `main` in a scratch worktree. DB tests
go on the CI Postgres list.

- **`tests/test_offboarding.py`** (SQLite and Postgres):
  suspend/reactivate/purge transitions and their records; refusals (active
  tenant, inside the window, wrong `--confirm`, `--skip-grace` without a
  reason, Postgres without `MIGRATE_DATABASE_URL`); after a purge **every**
  `TENANT_TABLES` table has zero rows for the tenant and the blob store is
  empty; a second tenant's rows and blobs are untouched; a run interrupted
  after the blobs re-runs to completion; the record's counts are right; the
  JSON line is printed.
- **Suspension enforcement:** token and dev-bypass requests get 403 plus a
  `sign_in_refused` row; a failing lookup gets 503; domain-bound join refused;
  worker dead-letters with "tenant suspended" (document and rematch);
  `add-user` refused.
- **Postgres only, NOBYPASSRLS roles:** owner with `app.purge_tenant` set
  deletes that tenant's audit rows and no other's; the app role cannot delete
  audit rows even with the variable set; the migration upgrades and
  downgrades.
- **`restore_check`:** passes on a correctly bootstrapped Postgres; fails on
  a table without forced RLS, an app role with DELETE on `audit_events`, and
  a wrong head.
- **Schema policy:** every table with a `tenant_id` column is in
  `TENANT_TABLES` (the reverse of the existing test), so the purge cannot
  miss one.
- `LocalBlobStore.list_keys/delete`, including refusing a key that escapes.

### 6. Verification

Full Python suite, ruff, the 9 gates, the CI Postgres list locally, the local
restore drill (output recorded in the runbook), and a manual run on the
local stack: suspend `bench-a` → API 403 in the browser and via curl →
reactivate → works → suspend → `purge-tenant --skip-grace` → zero rows and
blobs.

## Out of scope

Customer self-service offboarding in the app; an export bundle; age-based or
per-tenant retention; automatic Identity Platform account deletion; a real
Cloud SQL restore drill (waits for production); an offsite copy of the audit
trail.
