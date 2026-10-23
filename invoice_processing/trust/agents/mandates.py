"""Mandates: what a human authorised an agent to do (FR-28.5, B4, B16).

A mandate's digest (the store's `digest`) covers its scope, validity, who
proposed and approved it, and the digest of the version before it. On
approval it is signed with HMAC-SHA256 under a per-tenant key derived from
`TRUST_MANDATE_KEY`; nothing secret is stored. The honest claim is
tamper-evidence against edits in the database, not non-repudiation.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from ...db.models import AgentMandate
from ...db.session import tenant_session
from ...po_matching import normalize_vendor
from . import ACTIONS
from .store import _dict as store_row
from .store import digest

__all__ = [
    "MandateKeyMissing",
    "annotate",
    "approve",
    "in_force",
    "sign",
    "validate",
    "verify",
]

CAPS = ("per_action", "per_period", "per_vendor", "per_category")
PERIODS = ("day", "month")
MAX_SPAN = timedelta(days=366)
_CURRENCY = re.compile(r"^[A-Za-z]{3}$")
_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
EACH_PARTY = "Each counterparty must be a vendor name or id."


class MandateKeyMissing(Exception):
    """`TRUST_MANDATE_KEY` is not set: nothing can be signed."""


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _amount(value, name: str) -> str:
    # Strings only: a JSON number has already been through a float.
    try:
        if not isinstance(value, str):
            raise InvalidOperation
        amount = Decimal(value.strip())
    except InvalidOperation:
        raise ValueError(f"{name} must be an amount as a string.") from None
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{name} must be a non-negative amount.")
    return str(amount)


def _caps(caps) -> dict:
    if caps is None:
        return {}
    if not isinstance(caps, dict) or not set(caps) <= set(CAPS):
        raise ValueError(f"caps may only name {', '.join(CAPS)}.")
    out = {}
    for name in ("per_action", "per_vendor"):
        if caps.get(name) is not None:
            out[name] = _amount(caps[name], name)
    period = caps.get("per_period")
    if period is not None:
        if not isinstance(period, dict):
            raise ValueError("per_period must be {period, amount}.")
        if period.get("period") not in PERIODS:
            raise ValueError("per_period.period must be day or month.")
        out["per_period"] = {
            "period": period["period"],
            "amount": _amount(period.get("amount"), "per_period.amount"),
        }
    category = caps.get("per_category")
    if category is not None:
        if not isinstance(category, dict):
            raise ValueError(
                "per_category must map a vendor kind to an amount."
            )
        out["per_category"] = {
            str(k): _amount(v, f"per_category.{k}") for k, v in category.items()
        }
    return out


def _vendor_party(vendor: dict) -> dict:
    # The vendors repository derives `normalized_name` this way.
    return {"vendor_id": vendor["id"], "key": normalize_vendor(vendor["name"])}


def _party(item, vendors, tenant_id) -> dict:
    """One counterparty as stored: `{vendor_id, key}`. An id must name a
    stored vendor; a name resolves to its vendor when there is one."""
    if isinstance(item, dict) and set(item) == {"vendor_id"}:
        vendor_id = item["vendor_id"]
        if not isinstance(vendor_id, str) or not vendor_id.strip():
            raise ValueError(EACH_PARTY)
        found = vendors.get(tenant_id, vendor_id.strip().lower()) if vendors else None
        if found is None:
            raise ValueError(f"Unknown vendor id: {vendor_id.strip()[:64]}")
        return _vendor_party(found)
    if isinstance(item, dict) and set(item) == {"name"}:
        item = item["name"]
    if not isinstance(item, str):
        raise ValueError(EACH_PARTY)
    text = item.strip()
    if _UUID.fullmatch(text.lower()):
        # An id typo must not become a name that never matches.
        return _party({"vendor_id": text}, vendors, tenant_id)
    key = normalize_vendor(text)
    if not key:
        raise ValueError(EACH_PARTY)
    found = vendors.find_by_name(tenant_id, text) if vendors else None
    return _vendor_party(found) if found else {"vendor_id": None, "key": key}


def _counterparties(items, vendors=None, tenant_id=None):
    if items is None:
        return None
    if not isinstance(items, list) or not items:
        raise ValueError(
            "counterparties must be a list of vendor names or ids, or null."
        )
    unique = {
        (p["key"], p["vendor_id"]): p
        for p in (_party(i, vendors, tenant_id) for i in items)
    }
    return [unique[k] for k in sorted(unique, key=lambda k: (k[0], k[1] or ""))]


def validate(
    body: dict, *, now: datetime, vendors=None, tenant_id: str | None = None
) -> tuple[dict, datetime, datetime]:
    """The normalised scope and UTC validity, or ValueError naming the first
    problem. With `vendors` (the vendors repository), counterparties resolve
    to vendor ids; without it they stay names."""
    scope = body.get("scope")
    if not isinstance(scope, dict):
        raise ValueError("scope is required.")
    actions = scope.get("actions")
    if (
        not isinstance(actions, list)
        or not actions
        or not all(isinstance(a, str) for a in actions)
        or not set(actions) <= set(ACTIONS)
    ):
        raise ValueError(
            f"actions must be a non-empty subset of {', '.join(ACTIONS)}."
        )
    currency = scope.get("currency")
    if not isinstance(currency, str) or not _CURRENCY.match(currency):
        raise ValueError("currency must be a three-letter ISO code.")
    start, end = body.get("valid_from"), body.get("valid_to")
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        raise ValueError("valid_from and valid_to are required.")
    start, end = _utc(start), _utc(end)
    if not start < end:
        raise ValueError("valid_to must be after valid_from.")
    if end - start > MAX_SPAN:
        raise ValueError("A mandate may run at most 366 days.")
    return (
        {
            "actions": sorted(set(actions), key=ACTIONS.index),
            "counterparties": _counterparties(
                scope.get("counterparties"), vendors, tenant_id
            ),
            "caps": _caps(scope.get("caps")),
            "currency": currency.upper(),
        },
        start,
        end,
    )


def _key(tenant_id: str) -> bytes:
    secret = os.getenv("TRUST_MANDATE_KEY")
    if not secret:
        raise MandateKeyMissing("Mandate signing is not configured.")
    return hmac.new(
        secret.encode(), tenant_id.encode(), hashlib.sha256
    ).digest()


def sign(tenant_id: str, row_digest: str) -> str:
    return hmac.new(
        _key(tenant_id), row_digest.encode(), hashlib.sha256
    ).hexdigest()


def _same(a: str, b: str | None) -> bool:
    # Bytes: compare_digest refuses a non-ASCII str (a tampered column).
    return hmac.compare_digest(a.encode(), (b or "").encode())


def _state(row: dict) -> str:
    """What the signature covers: the content digest (which the version
    chain links to, so it never changes) and the row's state, so reviving or
    un-revoking a mandate in the database breaks it."""
    at = row.get("revoked_at")
    at = _utc(at).isoformat() if at else ""
    return f"{row.get('digest')}|{row.get('status')}|{row.get('revoked_by') or ''}|{at}"


def seal(row: dict) -> str:
    return sign(row["tenant_id"], _state(row))


def verify(row: dict, prev: dict | None) -> bool:
    """The row is what was signed, in the state it was signed in, and
    follows the version before it."""
    if row.get("prev_digest") != (prev or {}).get("digest"):
        return False
    if not _same(digest(row), row.get("digest")):
        return False
    if row.get("status") == "proposed" and row.get("approved_by") is None:
        return True  # nobody has signed it yet
    try:
        expected = seal(row)
    except MandateKeyMissing:
        return False
    return _same(expected, row.get("signature"))


def reseal(ctx, store, mandate_ids) -> None:
    """Sign the state of exactly the rows a call just changed. Never "every
    row that does not match": that would bless a row edited in the database.
    Without the key the rows are left unsigned (unverified), never blocked:
    revoking must always work."""
    for mandate_id in mandate_ids:
        with tenant_session(ctx.tenant_id, store.url) as s:
            row = s.get(AgentMandate, mandate_id)
            if row is None or row.tenant_id != ctx.tenant_id:
                continue
            try:
                row.signature = seal(store_row(row))
            except MandateKeyMissing:
                row.signature = None


def revoke(ctx, store, mandate_id: str, reason: str) -> dict | None:
    row = store.revoke_mandate(ctx, mandate_id, reason)
    if row is None:
        return None
    if row["changed"]:  # a repeat revoke must not sign what the DB now holds
        reseal(ctx, store, [mandate_id])
    return next(
        m
        for m in store.mandates(ctx.tenant_id, row["agent_id"])
        if m["id"] == mandate_id
    )


def annotate(rows: list[dict], now: datetime) -> list[dict]:
    """`rows` in version order, each with `verified` and `expired`."""
    out, prev = [], None
    for row in rows:
        out.append(
            {
                **row,
                "verified": verify(row, prev),
                "expired": row["status"] == "active" and row["valid_to"] <= now,
            }
        )
        prev = row
    return out


def in_force(rows: list[dict], at: datetime) -> dict | None:
    """The version that authorised the agent at `at`: approved by then,
    valid then, not yet revoked or superseded. A logged action is judged by
    it, not by today's mandate. Returned as it stood then (active)."""
    held = [
        m
        for m in rows
        if m["approved_at"] is not None
        and m["approved_at"] <= at
        and m["valid_from"] <= at < m["valid_to"]
        and (m["revoked_at"] is None or at < m["revoked_at"])
    ]
    if not held:
        return None
    return {**max(held, key=lambda m: m["version"]), "status": "active"}


def approve(ctx, store, mandate_id: str) -> dict:
    """A second admin approves the pending latest version, and it is signed.

    The store stamps `approved_at`, so the digest is known only after it
    commits; the signature follows in a second session. A crash between leaves
    an active mandate that reads `verified: false` -- visible, not silent.
    """
    _key(ctx.tenant_id)  # refuse before anything changes
    row = store.approve_mandate(ctx, mandate_id, signature=None)
    reseal(ctx, store, [mandate_id, *row["superseded"]])
    return next(
        m
        for m in store.mandates(ctx.tenant_id, row["agent_id"])
        if m["id"] == mandate_id
    )
