"""The end of a customer's data: suspend, wait, purge.

Three states and one direction: `active -> suspended -> purged`, with
`reactivate` as the only way back and only from `suspended`. Suspension is
what every request path checks (sign-in, the worker, provisioning); the
purge is the one place this product deletes evidence, so it is refused far
more often than it runs.

The tenants row is the surviving record. Its `settings` keep the tenant's
own configuration while it is suspended, and become only the
`offboarding` record once it is purged.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, text, update

from .db.models import TENANT_TABLES, Base, Tenant
from .db.repositories import audit_event_row
from .db.rls import PURGE_SETTING
from .db.session import is_postgres, tenant_session
from .ports import RunContext

# A recovery window, not an export window: a suspended tenant cannot sign in,
# so customers export before their subscription ends (SECURITY.md).
GRACE_DAYS = 30
ACTIVE, SUSPENDED, PURGED = "active", "suspended", "purged"


class OffboardingError(RuntimeError):
    """Refused before anything was changed."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _required(value: str | None, what: str) -> str:
    if not (value or "").strip():
        raise OffboardingError(f"{what} is required.")
    return value.strip()


def _tenant_id(slug: str, url: str | None) -> str:
    with tenant_session("", url) as session:
        tenant_id = session.scalar(select(Tenant.id).where(Tenant.slug == slug))
    if tenant_id is None:
        raise OffboardingError(f"No tenant {slug!r}.")
    return tenant_id


def _move(
    slug: str,
    *,
    expected: str,
    new: str,
    event: str,
    operator: str,
    stamp: dict,
    url: str | None,
) -> dict:
    """One state change, its record and its audit row in one transaction."""
    tenant_id = _tenant_id(slug, url)
    # The tenant's own session, so the audit insert passes its RLS policy.
    with tenant_session(tenant_id, url) as session:
        tenant = session.scalar(
            select(Tenant).where(Tenant.id == tenant_id).with_for_update()
        )
        if tenant.status != expected:
            raise OffboardingError(
                f"{slug} is {tenant.status}; it must be {expected} to "
                f"become {new}."
            )
        settings = dict(tenant.settings or {})
        record = {**settings.get("offboarding", {}), **stamp}
        tenant.status = new
        # A new dict, so the JSON column notices the change.
        tenant.settings = {**settings, "offboarding": record}
        session.add(
            audit_event_row(
                RunContext(tenant_id=tenant_id, actor=operator),
                event,
                {"slug": slug, **stamp},
            )
        )
    return {
        "tenant_id": tenant_id,
        "slug": slug,
        "status": new,
        "offboarding": record,
    }


def suspend(
    slug: str,
    *,
    operator: str,
    reason: str,
    url: str | None = None,
    now: datetime | None = None,
) -> dict:
    operator = _required(operator, "An operator")
    reason = _required(reason, "A reason")
    return _move(
        slug,
        expected=ACTIVE,
        new=SUSPENDED,
        event="tenant_suspended",
        operator=operator,
        stamp={
            "suspended_at": (now or _now()).isoformat(),
            "suspended_by": operator,
            "reason": reason,
        },
        url=url,
    )


def reactivate(
    slug: str,
    *,
    operator: str,
    url: str | None = None,
    now: datetime | None = None,
) -> dict:
    operator = _required(operator, "An operator")
    return _move(
        slug,
        expected=SUSPENDED,
        new=ACTIVE,
        event="tenant_reactivated",
        operator=operator,
        stamp={
            "reactivated_at": (now or _now()).isoformat(),
            "reactivated_by": operator,
        },
        url=url,
    )


def require_owner_role() -> None:
    """Run the purge as the owner, or not at all on Postgres.

    The app role cannot delete an audit row (PR #18's REVOKE), which is the
    point; the purge is the one deletion that needs it, so it borrows the
    release step's connection rather than widening the app's.
    """
    from .db.migrate import use_owner_role  # noqa: PLC0415 - alembic is heavy
    from .db.session import cloud_sql_instance, database_url  # noqa: PLC0415

    if use_owner_role():
        return
    if cloud_sql_instance() or database_url().startswith("postgresql"):
        raise OffboardingError(
            "The purge runs as the owner role: set MIGRATE_DATABASE_URL (or "
            "MIGRATE_CLOUD_SQL_USER), the release step's connection."
        )


def _tenant_tables():
    """Children before parents, so every foreign key is gone before its row."""
    return [
        t
        for t in reversed(Base.metadata.sorted_tables)
        if t.name in TENANT_TABLES
    ]


def _delete_rows(session, tenant_id: str) -> dict[str, int]:
    return {
        table.name: session.execute(
            delete(table).where(table.c.tenant_id == tenant_id)
        ).rowcount
        for table in _tenant_tables()
    }


def _remaining_rows(session, tenant_id: str) -> dict[str, int]:
    counts = {
        table.name: session.scalar(
            select(func.count())
            .select_from(table)
            .where(table.c.tenant_id == tenant_id)
        )
        for table in _tenant_tables()
    }
    return {name: n for name, n in counts.items() if n}


def leftovers(tenant_id: str, blobs, url: str | None = None) -> dict[str, int]:
    """What of a tenant is still here: rows per table, and documents."""
    with tenant_session(tenant_id, url) as session:
        left = _remaining_rows(session, tenant_id)
    documents = len(blobs.list_keys())
    return {**left, **({"documents": documents} if documents else {})}


def purge(
    slug: str,
    *,
    operator: str,
    confirm: str,
    blobs_for,
    skip_grace: bool = False,
    reason: str | None = None,
    url: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Delete every row and document of a suspended tenant.

    Documents first, then the rows in one transaction that also marks the
    tenant purged. A crash at any point leaves it `suspended`, so the record
    never claims a purge over data that is still there, and a re-run picks
    up where it stopped.
    """
    operator = _required(operator, "An operator")
    if confirm != slug:
        raise OffboardingError("--confirm must repeat the tenant slug exactly.")
    if skip_grace:
        reason = _required(reason, "A reason for --skip-grace")
    now = now or _now()
    tenant_id = _tenant_id(slug, url)
    with tenant_session(tenant_id, url) as session:
        tenant = session.get(Tenant, tenant_id)
        status = tenant.status
        record = dict((tenant.settings or {}).get("offboarding") or {})
    if status != SUSPENDED:
        raise OffboardingError(
            f"{slug} is {status}; only a suspended tenant can be purged."
        )
    waited = now - datetime.fromisoformat(record["suspended_at"])
    if not skip_grace and waited < timedelta(days=GRACE_DAYS):
        raise OffboardingError(
            f"{slug} was suspended {waited.days} days ago; the recovery "
            f"window is {GRACE_DAYS} days. Wait, or pass --skip-grace with "
            "the customer's written request as --reason."
        )

    blobs = blobs_for(tenant_id)
    documents = blobs.list_keys()
    for key in documents:
        blobs.delete(key)
    if blobs.list_keys():
        raise OffboardingError(f"{slug}'s documents could not all be deleted.")

    with tenant_session(tenant_id, url) as session:
        if is_postgres(session.get_bind()):
            # The only setting `audit_events_purge` answers to; the app role
            # cannot use it, having no DELETE on the table at all.
            session.execute(
                text("SELECT set_config(:name, :tenant_id, true)"),
                {"name": PURGE_SETTING, "tenant_id": tenant_id},
            )
        rows = _delete_rows(session, tenant_id)
        left = _remaining_rows(session, tenant_id)
        if left:
            raise OffboardingError(
                f"{slug} still holds rows after the deletes: {left}. "
                "Nothing was committed."
            )
        record.update(
            purged_at=now.isoformat(),
            purged_by=operator,
            skip_grace=skip_grace,
            skip_reason=reason if skip_grace else None,
            rows_deleted=rows,
            blobs_deleted=len(documents),
        )
        session.execute(
            update(Tenant)
            .where(Tenant.id == tenant_id)
            .values(status=PURGED, settings={"offboarding": record})
        )
    return {"tenant_id": tenant_id, "slug": slug, **record}
