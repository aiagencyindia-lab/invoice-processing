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
    "require_work_authorization": bool,
    "disabled_checks": list,
    "po_tolerance_pct": (int, float),
    "po_tolerance_abs": (int, float),
    "high_value_threshold": (int, float),
    "rules_binding": bool,
    # The tax identifier suppliers in this country carry. Settable because it
    # is a property of where the customer buys, not of the product: a single
    # master-data value meant every tenant validated Australian ABNs, so an
    # Indian GSTIN was rejected for being the wrong length -- the right verdict
    # for the wrong reason, and the same verdict for the valid ones.
    "tax_id_format": str,
    # The tax registrations this organisation is invoiced under. A name can be
    # typed; a registration is issued, so where both are known both must agree.
    "organization_tax_ids": list,
    "require_tax_breakup": bool,
    # Statutory rate per classification code, where the rate depends on what
    # was sold rather than on who sold it.
    "tax_rates_by_hsn": dict,
    "tax_rate_tolerance": (int, float),
    "tax_rate_tolerance_pct": (int, float),
    # How long after a bank change is approved an invoice paying to it still
    # draws a review signal: fraud is most often caught in that window.
    "bank_change_recent_days": (int, float),
    # How long an approval step may sit before it escalates; a policy number
    # each organisation sets for itself, like the bank-change window.
    "approval_sla_days": (int, float),
}

# Settings the validation engine knows nothing about, with their defaults.
# `DEFAULT_CONFIG` supplies the default for every other key; this one belongs to
# the approved-invoices sheet, so its default is stated here -- otherwise an
# unset threshold reaches the settings form as `null` in a number field.
NON_ENGINE_DEFAULTS: dict[str, Any] = {
    "high_value_threshold": 0,
    # Off. A rule starts by being watched, not obeyed.
    "rules_binding": False,
    "approval_sla_days": 3,
}

# Shown alongside each field in the UI so an administrator can tell what a
# setting does without reading the rulebook.
DESCRIPTIONS: dict[str, str] = {
    "approval_sla_days": (
        "Days an approval step may wait before it is escalated to the "
        "approver's manager, or an admin."
    ),
    "bank_change_recent_days": (
        "An invoice paid to bank details approved within this many days is "
        "held for review. The window in which a fraudulent change is most "
        "often caught."
    ),
    "organization_names": (
        "Names your company is invoiced under. An invoice addressed to none of "
        "these is rejected as 'addressed to different company'."
    ),
    "duplicate_check_enabled": (
        "Refuse an invoice that matches one already received. A duplicate "
        "invoice is a duplicate payment."
    ),
    "disabled_checks": (
        "Validation steps to skip, by number. A skipped step is recorded as "
        "skipped rather than passed, so a decision never claims a check that "
        "nobody ran. Step 1.1 cannot be skipped."
    ),
    "require_work_authorization": (
        "Reject invoices for work that has no authorisation form. Turn on if "
        "your suppliers bill labour against approved work orders; the check "
        "skips work types that never need one."
    ),
    "require_purchase_order": (
        "Reject invoices that quote no purchase order. Leave off to check only "
        "the invoices that name one."
    ),
    "po_tolerance_pct": "Permitted over-billing against a PO, as a fraction (0.02 = 2%).",
    "po_tolerance_abs": "Permitted over-billing against a PO, as a flat amount.",
    "high_value_threshold": (
        "Invoices at or above this amount are flagged as high value on the "
        "approved-invoices sheet, so they can be filtered and reviewed as a "
        "group. Zero turns the flag off. It changes no decision."
    ),
    "rules_binding": (
        "Let approved correction rules change decisions. Off, every rule is "
        "still evaluated and what it would have changed is recorded against "
        "the invoice -- so you can read a month of real cases before a rule "
        "is allowed to alter one."
    ),
    "tax_id_format": (
        "The tax registration your suppliers carry: ABN in Australia, GSTIN "
        "in India. It decides which checksum verifies a supplier's tax ID, so "
        "setting the wrong one rejects perfectly valid invoices."
    ),
    "organization_tax_ids": (
        "Your own tax registrations. When an invoice states who it is billed "
        "to by registration number, it must be one of these -- a name can be "
        "typed, a registration is issued."
    ),
    "tax_rates_by_hsn": (
        "Statutory tax rate for each classification code your suppliers use "
        "(HSN/SAC in India), as a fraction: 0.18 for 18%. Without it every "
        "line on an invoice is checked against one rate, which cannot catch a "
        "12% line taxed at 18%."
    ),
    "tax_rate_tolerance": (
        "How far an invoice's stated tax may differ from the calculated "
        "amount before it is rejected. Per-line rounding makes a difference "
        "of a rupee or two normal."
    ),
    "tax_rate_tolerance_pct": (
        "The same allowance as a share of the tax due, for invoices large "
        "enough that a fixed amount is meaningless. 0.002 is a fifth of a "
        "percent. The larger of the two applies."
    ),
    "require_tax_breakup": (
        "Require every invoice to be a tax invoice: a supplier registration "
        "and tax stated. Turn it on where you can only claim input tax "
        "against a proper tax invoice; leave it off where suppliers "
        "legitimately bill without tax."
    ),
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

        if key == "disabled_checks":
            from .checks import MANDATORY, known_ids  # noqa: PLC0415

            wanted = {str(i).strip() for i in value if str(i).strip()}
            known = known_ids()
            # Only refuse when the catalogue could be read: an unreadable
            # rulebook must not silently discard a customer's settings.
            unknown = sorted(wanted - known) if known else []
            if unknown:
                raise SettingsError(
                    f"No such validation step: {', '.join(unknown)}. "
                    f"Known steps: {', '.join(sorted(known))}."
                )
            blocked = sorted(wanted & set(MANDATORY))
            if blocked:
                raise SettingsError(
                    f"Step {', '.join(blocked)} cannot be switched off: it "
                    "reports whether the invoice could be read at all, so "
                    "skipping it would leave the other checks nothing to "
                    "examine."
                )
            cleaned[key] = sorted(wanted)
        elif key == "organization_tax_ids":
            cleaned[key] = [str(t).strip() for t in value if str(t).strip()]
        elif key == "organization_names":
            names = [str(n).strip() for n in value if str(n).strip()]
            if not names:
                raise SettingsError(
                    "At least one organisation name is required, or every "
                    "invoice will be rejected as addressed to someone else."
                )
            cleaned[key] = names
        elif key == "tax_id_format":
            from .tax_ids import supported_formats  # noqa: PLC0415

            wanted = str(value).strip().upper()
            if wanted not in supported_formats():
                raise SettingsError(
                    f"No validator for tax ID format {value!r}. "
                    f"Available: {', '.join(supported_formats())}. "
                    "Choosing an unknown format would leave supplier tax IDs "
                    "unchecked without saying so."
                )
            cleaned[key] = wanted
        elif key == "tax_rates_by_hsn":
            rates = {}
            for code, rate in value.items():
                try:
                    rates[str(code).strip()] = float(rate)
                except (TypeError, ValueError):
                    raise SettingsError(
                        f"Tax rate for {code!r} must be a number, e.g. 0.18 "
                        "for 18%."
                    ) from None
                if not 0 <= rates[str(code).strip()] <= 1:
                    raise SettingsError(
                        f"Tax rate for {code!r} must be a fraction between 0 "
                        "and 1, e.g. 0.18 for 18%."
                    )
            cleaned[key] = rates
        elif key in (
            "po_tolerance_pct",
            "po_tolerance_abs",
            "high_value_threshold",
            "tax_rate_tolerance",
            "tax_rate_tolerance_pct",
            "bank_change_recent_days",
            "approval_sla_days",
        ):
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

    defaults = {
        k: agent.DEFAULT_CONFIG.get(k, NON_ENGINE_DEFAULTS.get(k))
        for k in SETTABLE
    }
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
    # Reference data the checks compare against, scoped the same way and for
    # the same reason: these name a customer's suppliers and the work they
    # have authorised.
    config["vendor_master_file"] = str(paths.vendor_master_file(tenant_id))
    # The database is the record of truth for vendors where there is one; the
    # file stays as the single-process fallback. Empty records (a stack seeded
    # by files, as the benchmark is) fall through to the file in _load_vendors.
    from .api import service  # noqa: PLC0415

    vendors = service.get_vendors()
    config["vendor_master_records"] = (
        vendors.load_records(tenant_id) if vendors else None
    )
    config["work_authorization_file"] = str(
        paths.work_authorization_file(tenant_id)
    )
    agent._config_store["CONFIG"] = config
    return config
