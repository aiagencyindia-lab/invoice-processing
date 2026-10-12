-- One-time Postgres bootstrap: two roles.
--
-- The application connects as :"app", which owns nothing, cannot run DDL,
-- and can only append to and read the audit trail. Migrations run as
-- :"owner" in a release step (`python -m invoice_processing.db.migrate`).
-- An owner can TRUNCATE, ALTER or DROP any table it owns, policies included,
-- so the role serving traffic must not be the owner: that is the whole point.
--
-- Run as the built-in `postgres` user, connected to the application database.
-- Plain SQL with psql variables. Plain Postgres (Railway, Docker), one
-- section at a time (sed prints §1 up to the §2 marker, or §2 to the end):
--   sed -n '1,/^-- §2/p' bootstrap.sql | psql "$ADMIN_URL" -v ON_ERROR_STOP=1 \
--       -v owner=ledgerly_owner -v app=ledgerly_app \
--       -v owner_pw='...' -v app_pw='...' -f -
--   sed -n '/^-- §2/,$p' bootstrap.sql | psql "$ADMIN_URL" -v ON_ERROR_STOP=1 \
--       -v owner=ledgerly_owner -v app=ledgerly_app -f -
-- Cloud SQL IAM: create both users first with
--   gcloud sql users create <sa>@<project>.iam --type=CLOUD_IAM_SERVICE_ACCOUNT
-- delete the two CREATE ROLE lines, and pass the IAM names as owner and app.
-- Do NOT delete the `GRANT :"owner" TO CURRENT_USER` line: Cloud SQL's
-- `postgres` is not a superuser, and without that membership the default
-- privileges and the §2 REVOKE on audit_events fail or only WARN, leaving
-- the trail writable by the app.
--
-- Order: §1, then migrate as :"owner", then §2 (safe to re-run any time;
-- re-run it after every migrate).
-- The grants come after the first migrate because default privileges apply
-- only to tables created later, and audit_events already exists by then.

-- §1 -- before the first migrate ------------------------------------------

CREATE ROLE :"owner" LOGIN PASSWORD :'owner_pw'
    NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB;
CREATE ROLE :"app" LOGIN PASSWORD :'app_pw'
    NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB;

-- ALTER DEFAULT PRIVILEGES FOR ROLE owner, and §2's REVOKE on tables the
-- owner owns, need membership in the owner role. A real superuser has it
-- implicitly (this is then harmless); Cloud SQL's `postgres` does not, and a
-- REVOKE by a non-owner only WARNs, which ON_ERROR_STOP does not catch.
GRANT :"owner" TO CURRENT_USER;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO :"owner";
GRANT USAGE ON SCHEMA public TO :"app";

-- Every table a future migration creates is granted to the app automatically.
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"app";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO :"app";

-- §2 -- after migrate; idempotent ------------------------------------------

-- A database migrated under the old single IAM user: hand its tables to the
-- owner first, or later migrations cannot alter them. Uncomment to use
-- (psql does not expand variables inside a DO $$ body, hence \gexec).
-- SELECT format('ALTER TABLE %I OWNER TO %I', tablename, :'owner')
--   FROM pg_tables WHERE schemaname = 'public' \gexec

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO :"app";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO :"app";

-- Last, so no grant above can hand it back: the trail is append-only, and
-- the app may read the schema version but never move it.
-- Re-run §2 after every migrate: a migration that recreates a table
-- re-grants through default privileges.
REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
    ON audit_events FROM :"app";
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON alembic_version FROM :"app";

-- VERIFY -- expected: false, true, then false/false for both roles.
SELECT has_table_privilege(:'app', 'audit_events', 'UPDATE') AS app_can_update_audit,
       has_table_privilege(:'app', 'audit_events', 'INSERT') AS app_can_insert_audit;
SELECT rolname, rolbypassrls, rolsuper FROM pg_roles
 WHERE rolname IN (:'owner', :'app');
