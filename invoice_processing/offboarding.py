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

from datetime import datetime, timezone

from sqlalchemy import select

from .db.models import Tenant
from .db.repositories import audit_event_row
from .db.session import tenant_session
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
