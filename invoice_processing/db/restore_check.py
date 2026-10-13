"""After a restore: is this the database we promised?

Run against a database restored by any means (a Cloud SQL backup or
point-in-time clone, or `pg_restore` in the local drill), as the owner
role, after bootstrap §2:

    python -m invoice_processing.db.restore_check --restored <owner-url> \\
        [--source <owner-url>] [--app-role ledgerly_app]

It checks what a restore can silently lose -- the schema version, forced
row-level security and every policy, and the app role's lack of power over
the audit trail (bootstrap §2, which a restore does not carry) -- and
prints row counts per tenant, the newest audit event (the effective
restore point) and every tenant that is not active, which after a restore
from before a purge is the cue to purge it again
(deployment/BACKUP_RESTORE.md).
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime

from sqlalchemy import create_engine, text

from .models import TENANT_TABLES
from .rls import set_tenant_statement


def expected_policies(table: str) -> set[str]:
    if table == "audit_events":
        return {
            "audit_events_select",
            "audit_events_insert",
            "audit_events_purge",
        }
    if table == "sso_domains":
        return {"sso_domains_isolation", "sso_domains_lookup"}
    return {f"{table}_isolation"}


def repo_head() -> str:
    from alembic.config import Config  # noqa: PLC0415 - only this needs it
    from alembic.script import ScriptDirectory  # noqa: PLC0415

    from .. import paths  # noqa: PLC0415

    config = Config()
    config.set_main_option(
        "script_location",
        str(paths.AGENT_ROOT / "invoice_processing" / "db" / "migrations"),
    )
    return ScriptDirectory.from_config(config).get_current_head()


def check(conn, app_role: str) -> list[str]:
    failures = []
    version = conn.execute(
        text("select version_num from alembic_version")
    ).scalar()
    head = repo_head()
    if version != head:
        failures.append(f"schema is at {version}, the code expects {head}")

    flags = {
        name: (enabled, forced)
        for name, enabled, forced in conn.execute(
            text(
                "select relname, relrowsecurity, relforcerowsecurity "
                "from pg_class where relkind = 'r' "
                "and relnamespace = 'public'::regnamespace"
            )
        )
    }
    policies: dict[str, set[str]] = {}
    for table, policy in conn.execute(
        text(
            "select tablename, policyname from pg_policies "
            "where schemaname = 'public'"
        )
    ):
        policies.setdefault(table, set()).add(policy)
    for table in TENANT_TABLES:
        if table not in flags:
            failures.append(f"{table}: missing")
            continue
        if flags[table] != (True, True):
            failures.append(
                f"{table}: row-level security is not enabled and forced"
            )
        missing = expected_policies(table) - policies.get(table, set())
        if missing:
            failures.append(f"{table}: missing policies {sorted(missing)}")

    role = conn.execute(
        text("select rolbypassrls from pg_roles where rolname = :r"),
        {"r": app_role},
    ).first()
    if role is None:
        failures.append(f"app role {app_role} does not exist")
        return failures
    if role[0]:
        failures.append(f"app role {app_role} bypasses row-level security")
    for privilege in ("UPDATE", "DELETE", "TRUNCATE"):
        if conn.execute(
            text("select has_table_privilege(:r, 'audit_events', :p)"),
            {"r": app_role, "p": privilege},
        ).scalar():
            failures.append(f"app role {app_role} can {privilege} audit_events")
    return failures


def counts(conn) -> tuple[dict[str, dict[str, int]], datetime | None]:
    """Rows per tenant table per tenant, each read under that tenant's own
    setting (RLS is forced, so the owner sees nothing without one)."""
    rows: dict[str, dict[str, int]] = {}
    newest = None
    tenant_ids = conn.execute(text("select id from tenants order by id"))
    for tenant_id in list(tenant_ids.scalars()):
        conn.execute(text(set_tenant_statement()), {"tenant_id": tenant_id})
        rows[tenant_id] = {
            table: conn.execute(
                # Names from TENANT_TABLES, a constant, never from input.
                text(f"select count(*) from {table}")  # noqa: S608
            ).scalar()
            for table in TENANT_TABLES
        }
        latest = conn.execute(
            text("select max(occurred_at) from audit_events")
        ).scalar()
        if latest is not None and (newest is None or latest > newest):
            newest = latest
        conn.rollback()  # the setting is transaction-local; start clean
    return rows, newest


def compare(
    source: dict[str, dict[str, int]], restored: dict[str, dict[str, int]]
) -> list[str]:
    failures = []
    for tenant_id in sorted(set(source) | set(restored)):
        want, got = source.get(tenant_id, {}), restored.get(tenant_id, {})
        for table in sorted(set(want) | set(got)):
            a, b = want.get(table, 0), got.get(table, 0)
            if a != b:
                failures.append(
                    f"{tenant_id} {table}: source {a}, restored {b}"
                )
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="restore_check")
    parser.add_argument("--restored", required=True, help="Owner-role URL.")
    parser.add_argument(
        "--source", help="Owner-role URL of a source that is not changing."
    )
    parser.add_argument("--app-role", default="ledgerly_app")
    args = parser.parse_args(argv)

    engine = create_engine(args.restored)
    try:
        with engine.connect() as conn:
            failures = check(conn, args.app_role)
            conn.rollback()
            rows, newest = counts(conn)
            closed = conn.execute(
                text(
                    "select slug, status from tenants "
                    "where status <> 'active' order by slug"
                )
            ).all()
    finally:
        engine.dispose()
    if args.source:
        source = create_engine(args.source)
        try:
            with source.connect() as conn:
                failures += compare(counts(conn)[0], rows)
        finally:
            source.dispose()

    print(json.dumps({"rows": rows}, indent=2, sort_keys=True))
    print(f"newest audit event: {newest}")
    for slug, status in closed:
        print(f"not active: {slug}: {status}")
    for failure in failures:
        print(f"FAIL {failure}")
    print(f"{len(failures)} failures" if failures else "PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
