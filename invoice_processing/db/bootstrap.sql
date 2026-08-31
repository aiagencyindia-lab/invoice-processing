-- One-time Cloud SQL bootstrap.
--
-- A fresh Cloud SQL IAM user can connect and nothing else, and Postgres has no
-- way for a role to grant privileges to itself. So exactly one privileged
-- session is required, once. Afterwards the application authenticates purely by
-- IAM and no password exists in its configuration.
--
-- Run as the built-in `postgres` user, connected to the `invoices` database.
-- Plain SQL, no psql meta-commands, so Cloud SQL Studio runs it as-is.

GRANT USAGE, CREATE ON SCHEMA public
    TO "adk2026@project-cb9934aa-2b3a-4bd4-9ac.iam";


-- Why CREATE on the schema is enough, and schema ownership is not needed:
-- row-level security policies attach to *tables*, not to the schema, and a
-- table is owned by whoever creates it. The migration runs as the IAM user, so
-- it owns every table it creates and can attach policies to them. Transferring
-- ownership of `public` would be a broader grant for no benefit.


-- ===========================================================================
-- VERIFY -- run after the grant. Expected: true, then false, false.
-- BYPASSRLS is the one that matters: a role that has it silently ignores every
-- policy, which would leave the tenant isolation decorative but looking fine.
-- ===========================================================================

SELECT
    has_schema_privilege(
        'adk2026@project-cb9934aa-2b3a-4bd4-9ac.iam', 'public', 'CREATE'
    ) AS can_create,
    rolbypassrls,
    rolsuper
FROM pg_roles
WHERE rolname = 'adk2026@project-cb9934aa-2b3a-4bd4-9ac.iam';
