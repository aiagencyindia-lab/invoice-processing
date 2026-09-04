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
import os
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import checks, master_files, po_matching, tenant_settings, vendor_master
from .auth import Principal, current_principal, require_role

router = APIRouter()


class SettingsUpdate(BaseModel):
    settings: dict[str, Any]


class PurchaseOrderUpdate(BaseModel):
    purchase_orders: list[dict[str, Any]]


class VendorUpdate(BaseModel):
    vendors: list[dict[str, Any]]


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
    so it is reported explicitly rather than as an absent file.
    """
    out = []
    for record in _read_purchase_orders(principal.tenant_id):
        order = po_matching.PurchaseOrder.from_dict(record)
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
            }
        )
    return {
        "purchase_orders": out,
        "count": len(out),
        "source": str(_po_path(principal.tenant_id).name),
    }


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
    path = paths.PURCHASE_ORDER_DIR / f"{safe or 'default'}.json"
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
# Vendors
# ---------------------------------------------------------------------------
#
# Step 3.2 used to verify a supplier tax ID's checksum and stop there, which
# leaves the fraud case untouched: an invoice quoting somebody else's
# registration is arithmetically perfect. Comparing it against the supplier
# list is what closes that, and the list has to come from the customer.


@router.get("/api/vendors")
def list_vendors(
    principal: Annotated[Principal, Depends(require_role("clerk"))],
) -> dict:
    """Suppliers this tenant has registered, and the tax ID each is known by.

    An empty list is reported explicitly: it is the answer to "why is nobody's
    tax ID being matched?", and check 3.2 then runs as a checksum alone.
    """
    from .. import paths  # noqa: PLC0415

    path = paths.vendor_master_file(principal.tenant_id)
    records = master_files.read_records(path, vendor_master.RECORD_KEY)
    vendors = [vendor_master.Vendor.from_dict(r) for r in records]
    return {
        "vendors": [
            {"name": v.name, "tax_id": v.tax_id, "kind": v.kind}
            for v in vendors
        ],
        "count": len(vendors),
        "source": path.name,
    }


@router.put("/api/vendors")
def replace_vendors(
    request: VendorUpdate,
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> dict:
    """Replace the supplier list wholesale.

    Validated in full before anything is written: a half-loaded master is worse
    than an empty one, because the suppliers that failed to load come back as
    "not in the vendor master" and their invoices are rejected as if the tax ID
    were forged.
    """
    seen: set[str] = set()
    for index, record in enumerate(request.vendors):
        if not isinstance(record, dict):
            raise HTTPException(
                status_code=400, detail=f"Entry {index + 1} is not an object."
            )
        name = str(record.get("name") or "").strip()
        if not name:
            raise HTTPException(
                status_code=400,
                detail=f"Entry {index + 1} has no supplier name.",
            )
        if not str(record.get("tax_id") or "").strip():
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{name} has no tax ID. Without one there is nothing to "
                    "compare an invoice against, and check 3.2 would report a "
                    "match it never made."
                ),
            )
        key = po_matching.normalize_vendor(name)
        if key in seen:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{name} appears twice. The check would use whichever "
                    "loaded last."
                ),
            )
        seen.add(key)

    from .. import paths  # noqa: PLC0415

    master_files.write_records(
        paths.vendor_master_file(principal.tenant_id),
        vendor_master.RECORD_KEY,
        request.vendors,
    )

    from . import service  # noqa: PLC0415

    service.get_audit().emit(
        principal.context(), "vendors_replaced", {"count": len(request.vendors)}
    )
    return {
        "count": len(request.vendors),
        "message": f"Loaded {len(request.vendors)} suppliers.",
    }
