"""Per-tenant configuration that the pipeline actually reads.

The rulebook ships configured for a fictional company, so every real invoice
fails check 1.2 ("addressed to different company") until someone edits a YAML
file. That is a terminal task standing between a new customer and their first
successful invoice, which is exactly the kind of thing the UI exists to remove.

Two properties make this safe to expose over an API:

* **The settable keys are an allow-list.** These values are layered directly
  into the engine's config, so an unrestricted passthrough would let anyone who
  reaches the settings endpoint set arbitrary pipeline internals -- model names,
  paths, retry budgets. Only the keys below can be written, and each is
  type-checked on the way in.
* **Tenant settings layer OVER master data, never replace it.** A tenant that
  has set nothing behaves exactly as it does today, so adding this changes no
  existing behaviour. Removing a key falls back rather than blanking the value.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# What an administrator may change, and what shape it must take. Anything not
# listed here is not settable over the API at any privilege level.
SETTABLE: dict[str, type | tuple[type, ...]] = {
    "organization_names": list,
    "duplicate_check_enabled": bool,
    "require_purchase_order": bool,
    "po_tolerance_pct": (int, float),
    "po_tolerance_abs": (int, float),
}

# Shown alongside each field in the UI so an administrator can tell what a
# setting does without reading the rulebook.
DESCRIPTIONS: dict[str, str] = {
    "organization_names": (
        "Names your company is invoiced under. An invoice addressed to none of "
        "these is rejected as 'addressed to different company'."
    ),
    "duplicate_check_enabled": (
        "Refuse an invoice that matches one already received. A duplicate "
        "invoice is a duplicate payment."
    ),
    "require_purchase_order": (
        "Reject invoices that quote no purchase order. Leave off to check only "
        "the invoices that name one."
    ),
    "po_tolerance_pct": "Permitted over-billing against a PO, as a fraction (0.02 = 2%).",
    "po_tolerance_abs": "Permitted over-billing against a PO, as a flat amount.",
}


class SettingsError(ValueError):
    """The submitted settings were refused. Carries a reviewer-readable why."""


def validate(payload: dict) -> dict:
    """Return the accepted subset, or raise with the first problem found."""
    if not isinstance(payload, dict):
        raise SettingsError("Settings must be an object.")

    unknown = sorted(set(payload) - set(SETTABLE))
    if unknown:
        raise SettingsError(
            f"Not settable: {', '.join(unknown)}. "
            f"Allowed: {', '.join(sorted(SETTABLE))}."
        )

    cleaned: dict[str, Any] = {}
    for key, value in payload.items():
        expected = SETTABLE[key]
        # bool is a subclass of int, so a stray True would pass a numeric check.
        if expected in ((int, float),) and isinstance(value, bool):
            raise SettingsError(
                f"{key} must be a number, not a true/false value."
            )
        if not isinstance(value, expected):
            name = (
                expected.__name__
                if isinstance(expected, type)
                else " or ".join(t.__name__ for t in expected)
            )
            raise SettingsError(f"{key} must be {name}.")

        if key == "organization_names":
            names = [str(n).strip() for n in value if str(n).strip()]
            if not names:
                raise SettingsError(
                    "At least one organisation name is required, or every "
                    "invoice will be rejected as addressed to someone else."
                )
            cleaned[key] = names
        elif key in ("po_tolerance_pct", "po_tolerance_abs"):
            if value < 0:
                raise SettingsError(f"{key} cannot be negative.")
            cleaned[key] = float(value)
        else:
            cleaned[key] = value
    return cleaned


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


class JsonTenantSettings:
    """Tenant settings in one JSON file, for local development."""

    def __init__(self, path: Path | None = None):
        if path is None:
            from . import paths  # noqa: PLC0415

            path = paths.DATA_DIR / "tenant_settings.json"
        self.path = Path(path)

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def get(self, tenant_id: str) -> dict:
        return self._read().get(tenant_id, {})

    def put(self, tenant_id: str, settings: dict) -> dict:
        data = self._read()
        merged = {**data.get(tenant_id, {}), **settings}
        data[tenant_id] = merged
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
        return merged


class PgTenantSettings:
    """Tenant settings in the `tenants.settings` JSON column."""

    def __init__(self, url: str | None = None):
        self.url = url

    def get(self, tenant_id: str) -> dict:
        from sqlalchemy import select  # noqa: PLC0415

        from .db.models import Tenant  # noqa: PLC0415
        from .db.session import tenant_session  # noqa: PLC0415

        with tenant_session(tenant_id, self.url) as session:
            tenant = session.scalar(
                select(Tenant).where(Tenant.id == tenant_id)
            )
            return dict(tenant.settings or {}) if tenant else {}

    def put(self, tenant_id: str, settings: dict) -> dict:
        from sqlalchemy import select  # noqa: PLC0415

        from .db.models import Tenant  # noqa: PLC0415
        from .db.session import tenant_session  # noqa: PLC0415

        with tenant_session(tenant_id, self.url) as session:
            tenant = session.scalar(
                select(Tenant).where(Tenant.id == tenant_id)
            )
            if tenant is None:
                raise SettingsError(f"No tenant {tenant_id}.")
            merged = {**(tenant.settings or {}), **settings}
            # Reassign rather than mutate: SQLAlchemy does not track in-place
            # edits of a JSON column, so a mutation would be silently discarded.
            tenant.settings = merged
            return merged


# ---------------------------------------------------------------------------
# Applying them to the engine
# ---------------------------------------------------------------------------


def effective(tenant_id: str, store=None) -> dict:
    """Master-data defaults with this tenant's overrides layered on top."""
    # Imported lazily: the acting agent pulls in the whole ADK and Vertex
    # stack, which every consumer of this module would otherwise pay for.
    from .shared_libraries.acting import (  # noqa: PLC0415
        general_invoice_agent as agent,
    )

    if store is None:
        from .api import service  # noqa: PLC0415

        store = service.get_tenant_settings()

    defaults = {k: agent.DEFAULT_CONFIG.get(k) for k in SETTABLE}
    overrides = {
        k: v for k, v in (store.get(tenant_id) or {}).items() if k in SETTABLE
    }
    return {**defaults, **overrides}


def apply_to_pipeline(tenant_id: str, store=None) -> dict:
    """Load this tenant's settings into the engine config before a run.

    The acting pipeline reads a module-level config dict, so a worker handling
    two tenants in sequence must reload between them -- otherwise the second
    invoice is validated against the first tenant's organisation names, which
    would reject it for belonging to somebody else.

    Returns the config that was applied, so a caller can log or assert on it.
    """
    # Imported lazily: the acting agent pulls in the whole ADK and Vertex
    # stack, which every consumer of this module would otherwise pay for.
    from . import paths  # noqa: PLC0415
    from .shared_libraries.acting import (  # noqa: PLC0415
        general_invoice_agent as agent,
    )

    config = {**agent.DEFAULT_CONFIG, **effective(tenant_id, store)}
    # Purchase orders are per-tenant: one shared file would let a customer see
    # and overwrite another's supplier commitments.
    config["purchase_order_file"] = str(paths.purchase_order_file(tenant_id))
    agent._config_store["CONFIG"] = config
    return config
