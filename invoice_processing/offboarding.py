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

from datetime import UTC, datetime, timedelta

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
# Statuses every request path lets through. None: no tenants row at all,
# which no write can reach (the foreign key).
OPEN = (None, ACTIVE)


class OffboardingError(RuntimeError):
    """Refused before anything was changed."""


def _now() -> datetime:
    return datetime.now(UTC)


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


def _running_jobs(session, tenant_id: str) -> int:
    claims = Base.metadata.tables["document_claims"]
    return session.scalar(
        select(func.count())
        .select_from(claims)
        .where(claims.c.tenant_id == tenant_id, claims.c.state == "running")
    )


def _refuse_running_jobs(slug: str, running: int) -> None:
    # ponytail: a job that passed the worker's status check but has not yet
    # claimed is not counted; the runbook stops the worker for the purge.
    if running:
        raise OffboardingError(
            f"{slug} has {running} job(s) in flight, which would write rows "
            "after the purge. Stop the worker or let them finish, then run "
            "the purge again."
        )


def _note_blobs_deleted(tenant_id: str, count: int, url: str | None) -> None:
    """Keep the documents' count before the rows go, so a run that fails
    after deleting them still reports them on the re-run."""
    with tenant_session(tenant_id, url) as session:
        tenant = session.scalar(
            select(Tenant).where(Tenant.id == tenant_id).with_for_update()
        )
        settings = dict(tenant.settings or {})
        record = dict(settings.get("offboarding") or {})
        record["pending_blobs_deleted"] = (
            record.get("pending_blobs_deleted", 0) + count
        )
        tenant.settings = {**settings, "offboarding": record}


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

    `url` must be the owner role's (`require_owner_role`): as the app role
    the audit rows are not deleted, and the re-count refuses the purge.

    Documents first, then the rows in one transaction that also marks the
    tenant purged. A crash at any point leaves it as it was, so the record
    never claims a purge over data that is still there, and a re-run picks
    up where it stopped. A purged tenant can be purged again, for what a
    restore or a late write left under it; the first purge's record stays
    and the re-run is added to `repurges`.
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
        running = _running_jobs(session, tenant_id)
    if status not in (SUSPENDED, PURGED):
        raise OffboardingError(
            f"{slug} is {status}; only a suspended tenant can be purged."
        )
    if status == SUSPENDED:
        if not record.get("suspended_at"):
            raise OffboardingError(
                f"{slug} has no suspension record, so there is no date to "
                "count the recovery window from. Reactivate it and suspend "
                "it with suspend-tenant."
            )
        waited = now - datetime.fromisoformat(record["suspended_at"])
        if not skip_grace and waited < timedelta(days=GRACE_DAYS):
            raise OffboardingError(
                f"{slug} was suspended {waited.days} days ago; the recovery "
                f"window is {GRACE_DAYS} days. Wait, or pass --skip-grace "
                "with the customer's written request as --reason."
            )
    _refuse_running_jobs(slug, running)

    blobs = blobs_for(tenant_id)
    documents = blobs.list_keys()
    for key in documents:
        blobs.delete(key)
    if blobs.list_keys():
        raise OffboardingError(f"{slug}'s documents could not all be deleted.")
    if documents:
        _note_blobs_deleted(tenant_id, len(documents), url)

    with tenant_session(tenant_id, url) as session:
        tenant = session.scalar(
            select(Tenant).where(Tenant.id == tenant_id).with_for_update()
        )
        # Again, under the lock: a reactivation while the documents were
        # going must keep the rows.
        if tenant.status != status:
            raise OffboardingError(
                f"{slug} became {tenant.status} during the purge; no rows "
                "were deleted."
            )
        _refuse_running_jobs(slug, _running_jobs(session, tenant_id))
        record = dict((tenant.settings or {}).get("offboarding") or {})
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
        blobs_deleted = record.pop("pending_blobs_deleted", 0)
        if status == PURGED:
            record["repurges"] = [
                *record.get("repurges", []),
                {
                    "at": now.isoformat(),
                    "by": operator,
                    "reason": reason,
                    "rows_deleted": rows,
                    "blobs_deleted": blobs_deleted,
                },
            ]
        else:
            record.update(
                purged_at=now.isoformat(),
                purged_by=operator,
                skip_grace=skip_grace,
                skip_reason=reason if skip_grace else None,
                rows_deleted=rows,
                blobs_deleted=blobs_deleted,
            )
        session.execute(
            update(Tenant)
            .where(Tenant.id == tenant_id)
            .values(status=PURGED, settings={"offboarding": record})
        )
    return {"tenant_id": tenant_id, "slug": slug, **record}
