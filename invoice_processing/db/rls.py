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
`audit_events` additionally has no UPDATE or DELETE policy: the audit trail is
append-only by construction, not by convention.

These statements are Postgres-specific and are no-ops on SQLite, which the test
suite uses for schema and repository behaviour. Cross-tenant isolation must
therefore also be tested at the query layer -- see test_db_repositories.py --
because SQLite cannot exercise the policies themselves.
"""

from __future__ import annotations

from .models import TENANT_TABLES

# Set per connection/transaction, read by every policy.
TENANT_SETTING = "app.tenant_id"


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


def set_tenant_statement() -> str:
    """Parameterised statement to scope a session to one tenant."""
    return f"SELECT set_config('{TENANT_SETTING}', :tenant_id, true)"
