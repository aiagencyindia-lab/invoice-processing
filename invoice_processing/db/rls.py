"""Row-level security: tenant isolation enforced by Postgres, not by trust.

Application queries filter on `tenant_id`. RLS enforces the same rule inside the
database, so a missing `WHERE` clause -- the single most likely way a
multi-tenant leak happens -- returns nothing instead of another tenant's
invoices.

Both layers are deliberate. Either alone is one bug away from a breach:

* Query filters alone fail the moment someone writes a query that forgets one,
  and nothing in the system notices.
* RLS alone fails if a policy is misconfigured or the connection runs as a role
  that bypasses it.

The session sets `app.tenant_id`; the policies compare each row against it.
`audit_events` additionally has no UPDATE policy, and its one DELETE policy
answers only to `app.purge_tenant`, which the offboarding purge sets as the
owner role: the app role has no DELETE on the table at all, so for the
service the trail is append-only by construction, not by convention.

These statements are Postgres-specific and are no-ops on SQLite, which the test
suite uses for schema and repository behaviour. Cross-tenant isolation must
therefore also be tested at the query layer -- see test_db_repositories.py --
because SQLite cannot exercise the policies themselves.
"""

from __future__ import annotations

from .models import TENANT_TABLES

# Set per connection/transaction, read by every policy.
TENANT_SETTING = "app.tenant_id"

# Set only by the offboarding purge, read only by audit_events_purge.
PURGE_SETTING = "app.purge_tenant"

# The tenant setting a pre-tenant domain lookup runs under. No real tenant id
# can equal it (ids are uuids), so it unlocks nothing but the one SELECT below.
SSO_LOOKUP_TENANT = "sso-lookup"


def enable_rls_statements(tables: list[str] | None = None) -> list[str]:
    """DDL enabling RLS and installing per-tenant policies.

    A migration passes its own list. `TENANT_TABLES` is the *current* set of
    tenant tables, and a migration is history: deriving one from the other
    means adding a table today changes what a migration written last month
    does, and the first fresh database to run the chain fails on a table that
    does not exist yet at that point.
    """
    statements: list[str] = []
    for table in tables if tables is not None else TENANT_TABLES:
        statements.append(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        # FORCE applies the policy to the table owner too. Without it, the
        # migration role -- often the same role the app connects as -- silently
        # bypasses every policy and the isolation is decorative.
        statements.append(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")

        if table == "audit_events":
            # Append-only: readable and insertable within the tenant, never
            # updated or deleted. Absence of those policies is the enforcement.
            statements.append(
                f"CREATE POLICY {table}_select ON {table} FOR SELECT "
                f"USING (tenant_id = current_setting('{TENANT_SETTING}', true));"
            )
            statements.append(
                f"CREATE POLICY {table}_insert ON {table} FOR INSERT "
                f"WITH CHECK (tenant_id = current_setting('{TENANT_SETTING}', true));"
            )
            continue

        if table == "sso_domains":
            # The usual isolation policy, plus: a request that has not yet
            # resolved its tenant must be able to ask "which tenant owns
            # acme.com?". Permissive policies OR together, so this grants
            # SELECT on this table only, and only to the lookup session.
            statements.append(
                f"CREATE POLICY {table}_isolation ON {table} "
                f"USING (tenant_id = current_setting('{TENANT_SETTING}', true)) "
                f"WITH CHECK (tenant_id = current_setting('{TENANT_SETTING}', true));"
            )
            statements.append(
                f"CREATE POLICY {table}_lookup ON {table} FOR SELECT "
                f"USING (current_setting('{TENANT_SETTING}', true) = '{SSO_LOOKUP_TENANT}');"
            )
            continue

        statements.append(
            f"CREATE POLICY {table}_isolation ON {table} "
            f"USING (tenant_id = current_setting('{TENANT_SETTING}', true)) "
            f"WITH CHECK (tenant_id = current_setting('{TENANT_SETTING}', true));"
        )
    return statements


def disable_rls_statements(tables: list[str] | None = None) -> list[str]:
    statements: list[str] = []
    for table in tables if tables is not None else TENANT_TABLES:
        if table == "audit_events":
            statements.append(
                f"DROP POLICY IF EXISTS {table}_select ON {table};"
            )
            statements.append(
                f"DROP POLICY IF EXISTS {table}_insert ON {table};"
            )
        else:
            statements.append(
                f"DROP POLICY IF EXISTS {table}_isolation ON {table};"
            )
            if table == "sso_domains":
                statements.append(
                    f"DROP POLICY IF EXISTS {table}_lookup ON {table};"
                )
        statements.append(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;")
        statements.append(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")
    return statements


def revoke_audit_mutation_statements() -> list[str]:
    """Remove UPDATE/DELETE grants on the audit trail.

    Belt-and-braces with the missing policies: an append-only log that can be
    rewritten is not evidence, and SOX-grade auditability is a stated product
    promise rather than a nice-to-have.
    """
    return [
        "REVOKE UPDATE, DELETE ON audit_events FROM PUBLIC;",
    ]


def audit_purge_policy_statements() -> list[str]:
    """The one way an audit row is ever deleted: a tenant's whole trail, by
    its offboarding purge (offboarding.purge).

    Not part of `enable_rls_statements`, which migrations replay as history:
    adding it there would make the initial migration create a policy the
    later one then fails to create again.
    """
    return [
        "CREATE POLICY audit_events_purge ON audit_events FOR DELETE "
        f"USING (tenant_id = current_setting('{PURGE_SETTING}', true));"
    ]


def drop_audit_purge_policy_statements() -> list[str]:
    return ["DROP POLICY IF EXISTS audit_events_purge ON audit_events;"]


def set_tenant_statement() -> str:
    """Parameterised statement to scope a session to one tenant."""
    return f"SELECT set_config('{TENANT_SETTING}', :tenant_id, true)"


def trust_citation_trigger_statements() -> list[str]:
    """A finding without a citation cannot be committed (PRD FR-24.2).

    Deferred to commit so the finding and its citations land in one
    transaction. Not part of `enable_rls_statements`: migrations replay that
    as history. Postgres only; SQLite relies on the Python writer.
    """
    return [
        """
        CREATE OR REPLACE FUNCTION trust_finding_needs_citation()
        RETURNS trigger AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM trust_finding_citations c
                WHERE c.finding_id = NEW.id
            ) THEN
                RAISE EXCEPTION 'trust_findings row % has no citation', NEW.id;
            END IF;
            RETURN NULL;
        END $$ LANGUAGE plpgsql;
        """,
        """
        CREATE CONSTRAINT TRIGGER trust_finding_citation_check
        AFTER INSERT ON trust_findings DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION trust_finding_needs_citation();
        """,
    ]


def drop_trust_citation_trigger_statements() -> list[str]:
    return [
        "DROP TRIGGER IF EXISTS trust_finding_citation_check ON trust_findings;",
        "DROP FUNCTION IF EXISTS trust_finding_needs_citation();",
    ]
