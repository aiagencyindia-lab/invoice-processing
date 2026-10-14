"""Configuration a customer can change without a terminal.

Two things stood between a new tenant and their first successful invoice, and
both required editing a file on disk:

* the **organisation name**, without which check 1.2 rejects every real invoice
  as "addressed to different company";
* the **purchase orders**, without which the Step 2.2 two-way match reports
  every quoted PO as not found.

Both are now editable over the API, which is what lets the UI expose them.

The settings endpoint writes values that are layered into the engine's config,
so it accepts an allow-list of typed keys and nothing else -- an unrestricted
passthrough would turn an admin account into arbitrary control of pipeline
internals.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import (
    checks,
    master_files,
    po_matching,
    tenant_settings,
    wa_matching,
)
from .auth import Principal, current_principal, require_role

logger = logging.getLogger(__name__)
router = APIRouter()


class SettingsUpdate(BaseModel):
    settings: dict[str, Any]


class PurchaseOrderUpdate(BaseModel):
    purchase_orders: list[dict[str, Any]]


class WorkAuthorizationUpdate(BaseModel):
    work_authorizations: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@router.get("/api/settings")
def read_settings(
    principal: Annotated[Principal, Depends(current_principal)],
) -> dict:
    """Current effective settings, with each field's meaning.

    Readable by every role: a clerk who cannot see which organisation names are
    configured cannot understand why an invoice was rejected for being
    addressed elsewhere. Only writing is restricted.
    """
    from . import service  # noqa: PLC0415

    effective = tenant_settings.effective(
        principal.tenant_id, service.get_tenant_settings()
    )
    disabled = set(effective.get("disabled_checks") or [])
    return {
        "settings": effective,
        "integrations": _integrations(),
        # The catalogue comes with the settings so the UI never has to keep its
        # own list of checks -- one that would drift from the rulebook and
        # offer a switch for a step the engine no longer runs.
        "checks": [
            {**step, "enabled": step["id"] not in disabled}
            for step in checks.catalogue()
        ],
        "fields": [
            {
                "key": key,
                "type": (
                    expected.__name__
                    if isinstance(expected, type)
                    else "number"
                ),
                "description": tenant_settings.DESCRIPTIONS.get(key, ""),
                "value": effective.get(key),
            }
            for key, expected in sorted(tenant_settings.SETTABLE.items())
        ],
    }


def _integrations() -> dict:
    """Which outbound connectors this deployment can reach.

    A boolean and nothing else. The point is to answer "why is Export to Xero
    not working?" without anyone opening a terminal -- the token itself must
    never leave the server, and a UI has no use for it.

    Configuration is per deployment rather than per tenant today, which is a
    limit worth stating plainly rather than implying otherwise in the UI.
    """
    return {
        "xero": {
            "configured": bool(
                os.getenv("XERO_ACCESS_TOKEN") and os.getenv("XERO_TENANT_ID")
            ),
            "sets": ["XERO_ACCESS_TOKEN", "XERO_TENANT_ID"],
        },
        "csv": {"configured": True, "sets": []},
    }


@router.put("/api/settings")
def update_settings(
    request: SettingsUpdate,
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> dict:
    """Change settings for this tenant.

    Admin only, and audited: these values decide whether invoices are accepted,
    so a change is a business event, not a preference.
    """
    from . import service  # noqa: PLC0415

    try:
        cleaned = tenant_settings.validate(request.settings)
    except tenant_settings.SettingsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    store = service.get_tenant_settings()
    before = dict(store.get(principal.tenant_id) or {})
    try:
        store.put(principal.tenant_id, cleaned)
    except tenant_settings.SettingsError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    service.get_audit().emit(
        principal.context(),
        "settings_changed",
        {"changed": sorted(cleaned), "before": before, "after": cleaned},
    )
    return {
        "settings": tenant_settings.effective(principal.tenant_id, store),
        "changed": sorted(cleaned),
    }


# ---------------------------------------------------------------------------
# Purchase orders
# ---------------------------------------------------------------------------


def _po_path(tenant_id: str):
    """This tenant's purchase-order file.

    Scoped per tenant: purchase orders name suppliers and commitment amounts,
    so one shared file would let a customer read and overwrite another's.
    """
    from .. import paths  # noqa: PLC0415

    return paths.purchase_order_file(tenant_id)


def _read_purchase_orders(tenant_id: str) -> list[dict]:
    path = _po_path(tenant_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    records = raw.get("purchase_orders", raw) if isinstance(raw, dict) else raw
    if isinstance(records, dict):
        records = list(records.values())
    return [r for r in (records or []) if isinstance(r, dict)]


@router.get("/api/purchase-orders")
def list_purchase_orders(
    principal: Annotated[Principal, Depends(require_role("clerk"))],
) -> dict:
    """Purchase orders the two-way match can see.

    An empty list is the answer to "why does every PO come back not found?",
    so it is reported explicitly rather than as an absent file. With a
    database the rows also carry `line_count`, `received_total` and
    `billed_total`.
    """
    repo = _po_repo()
    if repo is not None:
        records = [
            {**row, "po_number": row["po_number_display"]}
            for row in repo.list_orders(principal.tenant_id)
        ]
        source = "database"
    else:
        records = _read_purchase_orders(principal.tenant_id)
        source = str(_po_path(principal.tenant_id).name)
    out = []
    for record in records:
        order = po_matching.PurchaseOrder.from_dict(record)
        extra = {
            k: record[k]
            for k in ("line_count", "received_total", "billed_total")
            if k in record
        }
        out.append(
            {
                "po_number": record.get("po_number"),
                "vendor_name": order.vendor_name,
                "total_amount": order.total_amount,
                "amount_invoiced": order.amount_invoiced,
                "remaining": round(order.remaining, 2),
                "currency": order.currency,
                "status": order.status,
                "open": order.status in po_matching.OPEN_STATUSES,
                **extra,
            }
        )
    return {"purchase_orders": out, "count": len(out), "source": source}


def _po_repo():
    """The database's purchase orders, None in file mode; 503 when a
    configured database cannot be asked (never a silent fall back to a file
    the pipeline is not reading)."""
    from . import service  # noqa: PLC0415

    try:
        return service.get_purchase_orders()
    except Exception:
        logger.exception("Purchase orders could not be reached")
        raise HTTPException(503, "Purchase orders are unavailable.") from None


@router.put("/api/purchase-orders")
def replace_purchase_orders(
    request: PurchaseOrderUpdate,
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> dict:
    """Replace the purchase-order set wholesale.

    Replace rather than merge: this is an export from the customer's ERP, and
    merging would silently keep orders their ERP has since closed -- an invoice
    would then match a PO that no longer exists.

    Every record is validated before anything is written, so a malformed export
    cannot leave the match half-loaded and quietly wrong.
    """
    records = request.purchase_orders
    if not isinstance(records, list):
        raise HTTPException(
            status_code=400, detail="purchase_orders must be a list."
        )
    repo = _po_repo()
    if repo is not None:
        from . import po_routes, service  # noqa: PLC0415

        po_routes.store_orders(
            repo,
            principal,
            po_routes.without_internal_keys(records),
            replace=True,
        )
        service.get_audit().emit(
            principal.context(),
            "purchase_orders_replaced",
            {"count": len(records)},
        )
        po_routes.unblock_receipts(
            principal, [r.get("po_number") for r in records]
        )
        return {
            "count": len(records),
            "message": f"Loaded {len(records)} purchase orders.",
        }

    seen: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise HTTPException(
                status_code=400, detail=f"Entry {index + 1} is not an object."
            )
        number = po_matching.normalize_po_number(record.get("po_number"))
        if not number:
            raise HTTPException(
                status_code=400,
                detail=f"Entry {index + 1} has no purchase order number.",
            )
        if number in seen:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Purchase order {record.get('po_number')!r} appears twice. "
                    "The match would use whichever loaded last."
                ),
            )
        seen.add(number)
        if not str(record.get("vendor_name") or "").strip():
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Purchase order {record.get('po_number')!r} has no vendor. "
                    "Without one the vendor-mismatch check cannot run."
                ),
            )

    from .. import paths  # noqa: PLC0415

    # Writes always go to the tenant's own file, never the shared legacy one --
    # the fallback in `purchase_order_file` is a read convenience, and writing
    # through it would let one tenant overwrite the file another reads.
    safe = "".join(
        c if c.isalnum() or c in "-_" else "_" for c in principal.tenant_id
    )
    path = paths.purchase_order_dir() / f"{safe or 'default'}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"purchase_orders": records}, indent=2), encoding="utf-8"
    )
    import os  # noqa: PLC0415

    os.replace(tmp, path)

    from . import service  # noqa: PLC0415

    service.get_audit().emit(
        principal.context(), "purchase_orders_replaced", {"count": len(records)}
    )
    return {
        "count": len(records),
        "message": f"Loaded {len(records)} purchase orders.",
    }


# ---------------------------------------------------------------------------
# Work authorizations
# ---------------------------------------------------------------------------
#
# Checks 1.4 and 4.3 used to require the authorization *form* to be attached to
# the submission. Most invoices instead quote a reference, and the authorization
# itself sits in the customer's records -- so without this the checks could
# only ever fail.


@router.get("/api/work-authorizations")
def list_work_authorizations(
    principal: Annotated[Principal, Depends(require_role("clerk"))],
) -> dict:
    """What this tenant has authorised: who may bill what, at what rate."""
    from .. import paths  # noqa: PLC0415

    path = paths.work_authorization_file(principal.tenant_id)
    records = master_files.read_records(path, wa_matching.RECORD_KEY)
    out = []
    for record in records:
        authorization = wa_matching.WorkAuthorization.from_dict(record)
        out.append(
            {
                "reference": record.get("reference")
                or record.get("work_authorization_id"),
                "vendor_name": authorization.vendor_name,
                "project": authorization.project,
                "approved_hours": authorization.approved_hours,
                "hourly_rate": authorization.hourly_rate,
                "period_start": authorization.period_start,
                "period_end": authorization.period_end,
                "status": authorization.status,
                "active": authorization.active,
            }
        )
    return {
        "work_authorizations": out,
        "count": len(out),
        "source": path.name,
    }


@router.put("/api/work-authorizations")
def replace_work_authorizations(
    request: WorkAuthorizationUpdate,
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> dict:
    """Replace the authorization set wholesale.

    Validated before anything is written. An authorization that loaded without
    its supplier would match any supplier quoting the reference, which is the
    single thing this check exists to prevent.
    """
    seen: set[str] = set()
    for index, record in enumerate(request.work_authorizations):
        if not isinstance(record, dict):
            raise HTTPException(
                status_code=400, detail=f"Entry {index + 1} is not an object."
            )
        reference = wa_matching.normalize_reference(
            record.get("reference") or record.get("work_authorization_id")
        )
        if not reference:
            raise HTTPException(
                status_code=400,
                detail=f"Entry {index + 1} has no work authorization reference.",
            )
        if reference in seen:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Work authorization {record.get('reference')!r} appears "
                    "twice. The check would use whichever loaded last."
                ),
            )
        seen.add(reference)
        if not str(record.get("vendor_name") or "").strip():
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Work authorization {record.get('reference')!r} has no "
                    "supplier. Without one it would authorise anybody who "
                    "quotes the reference."
                ),
            )

    from .. import paths  # noqa: PLC0415

    master_files.write_records(
        paths.work_authorization_file(principal.tenant_id),
        wa_matching.RECORD_KEY,
        request.work_authorizations,
    )

    from . import service  # noqa: PLC0415

    service.get_audit().emit(
        principal.context(),
        "work_authorizations_replaced",
        {"count": len(request.work_authorizations)},
    )
    return {
        "count": len(request.work_authorizations),
        "message": f"Loaded {len(request.work_authorizations)} work authorizations.",
    }
