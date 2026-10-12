# Audit integrity — design

SOC 2 readiness, sub-project 1 of 5. Brainstormed with the user 2026-10-12;
the decisions below were each approved in turn, and the plan built from them
was approved before this file was written.

## Why now

Phase 3's last items are ERP connectors and "SOC 2 Type I readiness". The
user chose readiness as *a short gap list for a future audit*: close the
engineering gaps now so a later Vanta/auditor engagement finds little to fix.
Policy documents wait until there is a production environment to describe.
Production is down (closed-billing GCP project; everything runs locally on
`rasayan`), so nothing here touches Railway or Cloud SQL: the code, the
bootstrap SQL and the tests are made ready for the restore.

A survey of the repo found five sub-projects; this is the first. The others,
each its own spec and PR, in the order they should be built:

2. Application hardening: security headers, `/docs` and `/openapi.json` off
   outside local, bank account numbers masked in API output, `.env` excluded
   from `.railwayignore`.
3. SDLC evidence: Dependabot, `pip-audit` and `npm audit` in CI, CODEOWNERS,
   ruff's `S` rules.
4. Logging and error reporting: structured logs carrying the request id,
   Sentry (bought, per the product plan).
5. Data lifecycle: tenant offboarding, document retention, backup and restore
   procedure (needs production back to test a restore for real).

## Where we start

- `audit_events` is append-only by RLS policy (SELECT and INSERT only,
  `db/rls.py`) and by `REVOKE UPDATE, DELETE ... FROM PUBLIC`. But the API
  connects as the role that *owns* every table (`db/bootstrap.sql`: one IAM
  user creates and owns everything). An owner can `TRUNCATE`,
  `ALTER TABLE ... DISABLE ROW LEVEL SECURITY` or `DROP` the table, and
  nothing detects it.
- Migrations run inside the API process at startup (`service._lifespan` →
  `db/migrate.upgrade_to_head`), which is why the runtime role has to own
  the tables today. `RUN_MIGRATIONS=0` already exists to turn that off.
- Refused access is never recorded: a role that is too low
  (`auth.require_role`) and a deactivated account (`sso._refuse_inactive`)
  both answer 403 and leave no trace.
- `GET /api/audit` returns at most the latest 500 rows, drops `request_id`,
  `ip` and `actor_type`, has no date range and no export. An auditor cannot
  be handed "everything between these dates".

## Decisions (user rulings)

1. **Two Postgres roles; migrations become a separate deploy step.** An
   *owner* role runs Alembic (`python -m invoice_processing.db.migrate`, the
   Railway pre-deploy command). The API and worker connect as an *app* role:
   `NOSUPERUSER NOBYPASSRLS`, no DDL, `SELECT/INSERT/UPDATE/DELETE` on app
   tables, **only `SELECT, INSERT` on `audit_events`**. Deployed environments
   set `RUN_MIGRATIONS=0`; local and manual stacks keep startup migration.
2. **Tenant-known refusals are audited**: `access_denied` (role too low) and
   `sign_in_refused` (deactivated account). Refusals with no tenant yet (bad
   token, no tenant claim, unverified email) stay in the server log:
   `audit_events.tenant_id` is a foreign key and the RLS insert policy
   checks it, so there is nowhere to put them.
3. **Date range + CSV download.** `GET /api/audit` gains `since`/`until`,
   keyset paging and every stored field. `GET /api/audit/export` streams a
   CSV for a date range, admin only, and is itself audited. The Activity page
   gets a date range and an Export button.

Not in scope (noted for later): hash-chained rows (once the role cannot
mutate the table a chain adds little), a reserved platform tenant for
tenantless refusals, audit retention or an offsite copy, preflight refusing
to start when `RUN_MIGRATIONS` is still on in a deployed environment (with
the app role, a pending migration fails at startup with "permission denied",
which is a clear error rather than a silent one).

## Design

### 1. Roles and bootstrap (`db/bootstrap.sql`, rewritten)

Run as the `postgres` superuser, connected to the app database. Two sections,
parameterised with psql variables (`:"owner"`, `:"app"`, `:'owner_pw'`,
`:'app_pw'`) so one file serves plain Postgres (Railway, local Docker:
`CREATE ROLE ... LOGIN PASSWORD`) and Cloud SQL IAM (roles pre-created with
`gcloud sql users create --type=CLOUD_IAM_SERVICE_ACCOUNT`; skip the
`CREATE ROLE` lines and pass the IAM names).

**§1, before the first migrate.** Create both roles
`NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB`.
`REVOKE CREATE ON SCHEMA public FROM PUBLIC`.
`GRANT USAGE, CREATE ON SCHEMA public TO owner`;
`GRANT USAGE ON SCHEMA public TO app`.
`ALTER DEFAULT PRIVILEGES FOR ROLE owner IN SCHEMA public GRANT SELECT,
INSERT, UPDATE, DELETE ON TABLES TO app` and `... GRANT USAGE, SELECT ON
SEQUENCES TO app`, so every table a future migration creates is granted to
the app automatically.

**§2, after migrate; idempotent, safe to re-run.**
`GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app`;
`GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app`; then, last,
`REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON audit_events FROM
app`. Default privileges never touch tables that already exist, and
`audit_events` is created by the initial migration; that is why the REVOKE
lives after migrate rather than in a migration that would have to read a
role name from the environment. A database migrated under the old single
user also needs `ALTER TABLE ... OWNER TO owner` per table (a commented
`DO $$` block in the file).

**VERIFY block** (replaces the old one): `has_table_privilege(app,
'audit_events', 'UPDATE')` is false and `'INSERT'` is true; `rolbypassrls`
and `rolsuper` are false for both roles.

`rls.revoke_audit_mutation_statements()` is left as it is: it is called only
by the initial migration, which is never edited. The bootstrap is the
enforcement.

### 2. Runtime role and the migrate command

- `DATABASE_URL` / `CLOUD_SQL_USER` stay the **app** credentials;
  `get_engine` precedence (`db/session.py`) is unchanged.
- `db/migrate.py` gains `main()` and `if __name__ == "__main__":
  sys.exit(main())`. `main` copies `MIGRATE_DATABASE_URL` → `DATABASE_URL`
  and `MIGRATE_CLOUD_SQL_USER` → `CLOUD_SQL_USER` into `os.environ` when set
  (Alembic's `env.py` and `_cloud_sql_engine` read those names), forces
  `RUN_MIGRATIONS=1`, calls `upgrade_to_head()`, prints the revision and
  returns 0; any exception is logged with `logger.exception` and returns 1.
- `.env.example` documents the two `MIGRATE_*` variables, that deployed
  environments set `RUN_MIGRATIONS=0`, and the Railway pre-deploy command.
  `deployment/README.md` gains a "Release" section: bootstrap §1 → migrate as
  owner → bootstrap §2 → verify.

### 3. Refusal auditing

- `sso.SsoServices` gains a third member `audit: Any = None` (a NamedTuple
  default, so every existing `SsoServices(directory, claims)` call keeps
  working). `service._sso_services()` returns
  `SsoServices(_user_directory, get_claims_writer(), get_audit())`.
- `auth.require_role(required)`: the dependency takes `request: Request`.
  On refusal: `services = _services()`; when `services` and `services.audit`
  are set, emit `access_denied` with `principal.context()` and payload
  `{"path": <route template, e.g. /api/invoices/{case_id}/actions, falling
  back to request.url.path>, "method", "required_role", "actual_role"}`. An
  exception from the sink becomes `sso._unavailable(exc)` (503): audit
  failures are never swallowed, and the request is still refused. Then the
  existing 403.
- `sso._refuse_inactive(row, tenant_id, email, services)`: the three call
  sites (`enforce_active`, the claimed path and the domain path of
  `resolve_principal`) all have those values in scope. When the row is
  inactive and `services.audit` is set, emit `sign_in_refused` with
  `Principal(user_id=row.get("uid") or "", email=email, tenant_id=tenant_id,
  role=str(row.get("role") or "viewer")).context()` (which carries the
  request id and client IP from the ContextVars) and payload
  `{"email", "user_id": row["id"], "reason": "deactivated"}`; a sink error
  becomes `_unavailable`. Then the existing 403.
- `repositories._ENTITY_TYPES` gains `"access": "user"`, `"sign_in": "user"`
  and `"audit": "audit_trail"` (`entity_type_for` takes the head before the
  last `_`).
- A deployment with no database (`_services()` is None) does not audit
  refusals, the same rule as the rest of sign-in (`# ponytail:` comment).

### 4. Audit read and export API

- `PgAuditSink.query(tenant_id, limit=100, since=None, until=None,
  before=None)`: `occurred_at >= since`, `occurred_at <= until`;
  `before=(occurred_at, id)` keyset →
  `or_(occurred_at < t, and_(occurred_at == t, id < i))`; order
  `occurred_at desc, id desc`. The export iterates `query` in pages of 1000
  until a short page.
- `system.list_audit` takes `since: datetime | None`, `until`, `cursor: str |
  None` (`"<iso>|<id>"`, split on the last `|`; malformed → 422, checked
  before the `_safe` fallback). One `_audit_dict(e)` serialiser is shared by
  the list, the export and the JSONL fallback: the existing keys `at, actor,
  action, entity_type, entity_id, payload, before` plus `id, actor_type,
  request_id, ip`. The response gains `next_cursor` (built from the last row
  when `len == limit`, else null). `_read_jsonl_audit(tenant_id, limit,
  since, until)` filters on the parsed timestamp and fills the new keys
  (`id` null, `request_id` from the record's `run_id`, `ip` null).
- `GET /api/audit/export?since=&until=` requires `admin`. It emits
  `audit_exported {since, until}` **before** streaming, so a dropped
  connection still leaves the record. `StreamingResponse` with
  `text/csv` and `Content-Disposition: attachment;
  filename="audit-<since>-<until>.csv"`; `csv.writer` over a `StringIO`
  drained per row; header `id,occurred_at,actor,actor_type,action,
  entity_type,entity_id,request_id,ip,before,after`; ISO timestamps;
  `before`/`after` as `json.dumps(..., default=str)`. The JSONL fallback
  reuses `_read_jsonl_audit`.
- Tenant isolation: `/api/audit/export` joins `PROBED` in
  `tests/test_tenant_isolation_api.py` with a probe (another tenant's export
  after this one acted returns the header row only). `system` is already in
  `test_no_endpoint_reads_a_tenant_from_the_request`.

### 5. Frontend

- `lib/api.ts`: `AuditEvent` gains `id, actor_type, request_id, ip` (all
  `string | null`); new `downloadFile(path, filename)` with the shape of
  `objectUrl` (bearer header, `response.blob()`, anchor click, revoke).
- `app/activity/page.tsx`: two `<input type="datetime-local">` (aria-labels
  "Since" and "Until"); the query is built with `URLSearchParams` and ISO
  strings; an "Export CSV" button, shown to admins only (role read from
  `/api/me` the way `admin/page.tsx` does), calls `downloadFile`.
- One Playwright test: open `/activity`, set Since, click Export CSV, wait
  for the download, filename matches `/^audit-.*\.csv$/`.

## Testing

Each new test fails on `main` (`72c6a9b`) before its change; proven in a
scratch worktree, removed afterwards.

- `tests/test_db_repositories.py`: the three new `entity_type_for` mappings.
- `tests/test_auth.py`: a clerk on `/approve` gets 403 **and** the injected
  fake sink holds `access_denied` with `required_role="approver"`,
  `actual_role="clerk"`, `path="/approve"`, `method="POST"`; a sink that
  raises → 503; no services → 403 and nothing recorded.
- `tests/test_sso.py`: a deactivated user → `sign_in_refused` recorded with
  the tenant id; a raising sink → 503. A `FakeAudit` (`.events`, `fail`)
  joins `FakeDirectory` and `FakeClaims`.
- `tests/test_system_api.py`: the exact-shape test gains `next_cursor:
  None`; `since`/`until` exclude an older event; cursor paging returns the
  remainder with no overlap; events carry `request_id` and `ip`; export as
  admin → 200, `text/csv`, header plus one row, and `audit_exported` then
  appears in `/api/audit`; export as approver → 403; malformed cursor → 422.
- `tests/test_gcs_and_migrations.py::TestMigration::
  test_app_role_cannot_rewrite_the_audit_trail` (Postgres only, the pattern
  of `test_sso_lookup_sentinel_reads_domains_and_nothing_else`): scratch
  database; bootstrap §1 with substituted names; `alembic upgrade head` as
  **owner**; bootstrap §2; seed a tenant and a user; connect as **app** under
  `app.tenant_id`: INSERT and SELECT on `audit_events` succeed; `UPDATE`,
  `DELETE`, `TRUNCATE`, `ALTER TABLE audit_events DISABLE ROW LEVEL
  SECURITY`, `DROP TABLE audit_events`, `ALTER TABLE users ADD COLUMN`,
  `CREATE TABLE` each raise `DBAPIError`; `UPDATE tenants` succeeds;
  `has_table_privilege(app, 'audit_events', 'UPDATE')` is false. Teardown
  drops the database `WITH (FORCE)` and both roles. Added to the CI Postgres
  list.
- `tests/test_gcs_and_migrations.py`: `migrate.main()` returns 1 on an
  unreachable URL, 0 on SQLite, and honours `MIGRATE_DATABASE_URL`.

Gates: full pytest, ruff check and format, `benchmark/harness/gates.sh`, the
CI Postgres list locally (Docker `postgres:16` on 55432) including the new
role test and the `alembic upgrade head && alembic downgrade base` round
trip, frontend lint, `tsc` and e2e, a browser check of the Activity page on
a temporary port-3200 config, and a manual-stack check that a clerk refused
on an approver route shows as `access_denied` on the Activity page.

## Residual risk

- The Railway pre-deploy command and the Cloud SQL IAM roles are configured
  at restore time, not here; until then the two-role arrangement is proven
  only on plain Postgres in CI.
- A deployment that forgets `RUN_MIGRATIONS=0` and has a pending migration
  fails at startup with "permission denied for schema public": loud, not
  silent, but a failed deploy.
- Refusals before a tenant is known remain only in the server log until
  sub-project 4 makes that log structured and searchable.
