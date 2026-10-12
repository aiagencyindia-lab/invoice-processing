"""The approved-invoice master sheet.

A reader, and only a reader. Every value it serves was written by the pipeline
or by a person approving an invoice through the workbench; nothing here changes
how an invoice is processed, approved or exported. That is deliberate: the
existing workflow is the record, and a second surface that also wrote to it
would be a second place for the truth to diverge.

What it adds is the ability to *ask questions* of that record. Until now the
only query over invoices was "give me all of them", answered by loading every
case and sorting in Python -- so "which invoices did we approve in August, from
this vendor, over ten thousand" had no answer short of reading them all.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Response,
)

from .. import sheet_columns
from ..masking import mask_row
from .auth import Principal, current_principal, require_role

router = APIRouter()

# A page a person can scan, and a ceiling that keeps one request honest.
DEFAULT_LIMIT = 100
MAX_LIMIT = 500


def _repository():
    """The sheet only works where invoices are queryable.

    The filesystem adapter stores a case as files in a directory, which answers
    "give me this case" and nothing else. Rather than pretend, an unconfigured
    deployment is told plainly that this page needs a database.
    """
    from . import service  # noqa: PLC0415

    if not service._database_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "The master sheet needs a database. This deployment stores "
                "cases on local disk, where invoices cannot be queried."
            ),
        )
    from ..db.repositories import SheetRepository  # noqa: PLC0415

    return SheetRepository()


def _high_value_threshold(tenant_id: str) -> float:
    from . import service  # noqa: PLC0415

    try:
        settings = service.get_tenant_settings().get(tenant_id) or {}
        return float(settings.get("high_value_threshold") or 0)
    except Exception:
        # A filter that cannot read a setting is a filter that matches
        # nothing, not a page that fails to load.
        return 0.0


def _filters(
    tenant_id: str,
    status: str,
    q: str | None,
    vendor: str | None,
    currency: str | None,
    date_from: str | None,
    date_to: str | None,
    approved_from: str | None,
    approved_to: str | None,
    min_total: float | None,
    max_total: float | None,
    po: str | None,
    tax: str | None,
    quality: list[str] | None,
    offset: int = 0,
) -> dict:
    return {
        "status": status,
        "q": q,
        "vendor": vendor,
        "currency": currency,
        "date_from": date_from,
        "date_to": date_to,
        "approved_from": approved_from,
        "approved_to": approved_to,
        "min_total": min_total,
        "max_total": max_total,
        "po": po,
        "tax": tax,
        "quality": quality or [],
        "offset": offset,
        "high_value_threshold": _high_value_threshold(tenant_id),
    }


@router.get("/api/sheet/invoices")
def list_sheet(
    principal: Annotated[Principal, Depends(current_principal)],
    status: str = "approved",
    q: str | None = None,
    vendor: str | None = None,
    currency: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    approved_from: str | None = None,
    approved_to: str | None = None,
    min_total: float | None = None,
    max_total: float | None = None,
    po: str | None = None,
    tax: str | None = None,
    quality: Annotated[list[str] | None, Query()] = None,
    sort: str = "approved_at",
    order: str = "desc",
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> dict:
    """One page of the sheet, with the column catalogue that describes it.

    Approved by default. Anything else has to be asked for by name, so a page
    titled "approved invoices" can never quietly include the rejected ones.
    """
    filters = _filters(
        principal.tenant_id,
        status,
        q,
        vendor,
        currency,
        date_from,
        date_to,
        approved_from,
        approved_to,
        min_total,
        max_total,
        po,
        tax,
        quality,
        offset,
    )
    rows, total = _repository().page(
        principal.tenant_id,
        filters,
        sort=sort,
        order="desc" if order == "desc" else "asc",
        limit=max(1, min(limit, MAX_LIMIT)),
    )
    if not principal.can("approver"):
        # The repository has no caller; the role is known only here.
        rows = [mask_row(row) for row in rows]
    return {
        "rows": rows,
        "total": total,
        "offset": offset,
        "limit": limit,
        "columns": sheet_columns.catalogue(),
        "quality_flags": sheet_columns.FILTER_FLAGS,
    }


@router.get("/api/sheet/summary")
def sheet_summary(
    principal: Annotated[Principal, Depends(current_principal)],
    status: str = "approved",
    q: str | None = None,
    vendor: str | None = None,
    currency: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    approved_from: str | None = None,
    approved_to: str | None = None,
    min_total: float | None = None,
    max_total: float | None = None,
    po: str | None = None,
    tax: str | None = None,
    quality: Annotated[list[str] | None, Query()] = None,
) -> dict:
    """The tiles above the sheet, over exactly the rows the grid is showing.

    Same filters as the grid on purpose: a total that counts rows the reader
    cannot see is a number they will reconcile against something else and lose.
    """
    filters = _filters(
        principal.tenant_id,
        status,
        q,
        vendor,
        currency,
        date_from,
        date_to,
        approved_from,
        approved_to,
        min_total,
        max_total,
        po,
        tax,
        quality,
    )
    return _repository().summary(principal.tenant_id, filters)


@router.get("/api/sheet/facets")
def sheet_facets(
    principal: Annotated[Principal, Depends(current_principal)],
    status: str = "approved",
) -> dict:
    """Vendors and currencies present, for the filter dropdowns."""
    filters = _filters(
        principal.tenant_id,
        status,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    )
    return _repository().facets(principal.tenant_id, filters)


# ---------------------------------------------------------------------------
# Saved views
# ---------------------------------------------------------------------------


def _views():
    from ..db.repositories import PgSheetViews  # noqa: PLC0415

    _repository()  # same database requirement, same 503
    return PgSheetViews()


def _validate_definition(definition: Any) -> dict:
    """A view may only name columns the sheet actually has.

    Caught on write, so a view saved today cannot break a page tomorrow --
    the same reason the rule writer validates field paths before storing a
    rule that would silently never fire.
    """
    if not isinstance(definition, dict):
        raise HTTPException(
            status_code=400, detail="A view definition must be an object."
        )
    unknown = [
        entry.get("key")
        for entry in definition.get("columns") or []
        if isinstance(entry, dict)
        and entry.get("key") not in sheet_columns.BY_KEY
    ]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown columns: {', '.join(str(u) for u in unknown)}",
        )
    sort = (definition.get("sort") or {}).get("key")
    if sort and not sheet_columns.is_sortable(sort):
        raise HTTPException(status_code=400, detail=f"Cannot sort by {sort}.")
    if len(json.dumps(definition, default=str)) > 20_000:
        raise HTTPException(
            status_code=400, detail="View definition is too large."
        )
    return definition


@router.get("/api/sheet/views")
def list_views(
    principal: Annotated[Principal, Depends(current_principal)],
) -> dict:
    """This user's views, plus any the tenant shares."""
    return {"views": _views().list(principal.tenant_id, principal.email)}


@router.post("/api/sheet/views")
def create_view(
    body: dict,
    principal: Annotated[Principal, Depends(current_principal)],
) -> dict:
    """Save the current filters under a name.

    Open to a viewer, unlike every other write on the sheet: a private view is
    a bookmark over rows that reader is already allowed to see, and a role that
    may read a ledger but not remember where it was looking is a role nobody
    can work in. Sharing one with the tenant is still approver's -- it is
    sharing, not saving, that puts a filter on someone else's screen.
    """
    from . import service  # noqa: PLC0415

    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="A view needs a name.")
    shared = bool(body.get("shared"))
    if shared and not principal.can("approver"):
        raise HTTPException(
            status_code=403,
            detail="Sharing a view with the tenant needs approver.",
        )
    view = _views().create(
        principal.tenant_id,
        name=name[:128],
        owner=principal.email,
        shared=shared,
        definition=_validate_definition(body.get("definition") or {}),
    )
    service.get_audit().emit(
        principal.context(),
        "sheet_view_created",
        {"view": view["id"], "name": name},
    )
    if shared:
        _audit_shared(principal, view["id"], name)
    return view


@router.patch("/api/sheet/views/{view_id}")
def update_view(
    view_id: str,
    body: dict,
    principal: Annotated[Principal, Depends(current_principal)],
) -> dict:
    from . import service  # noqa: PLC0415

    views = _views()
    existing = views.get(principal.tenant_id, view_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Unknown view.")
    if existing["owner"] != principal.email and not principal.can("admin"):
        raise HTTPException(
            status_code=403, detail="That view belongs to someone else."
        )

    changes: dict = {}
    if "name" in body:
        changes["name"] = str(body["name"]).strip()[:128]
    if "definition" in body:
        changes["definition"] = _validate_definition(body["definition"])
    if "shared" in body:
        if body["shared"] and not principal.can("approver"):
            raise HTTPException(
                status_code=403, detail="Sharing a view needs approver."
            )
        changes["shared"] = bool(body["shared"])

    updated = views.update(principal.tenant_id, view_id, **changes)
    service.get_audit().emit(
        principal.context(),
        "sheet_view_updated",
        {
            "view": view_id,
            **{k: v for k, v in changes.items() if k != "definition"},
        },
        before={"name": existing["name"], "shared": existing["shared"]},
    )
    if changes.get("shared") and not existing["shared"]:
        # Its own event, not a field inside an update: this is the moment a
        # view stops being one person's and starts being the tenant's, and an
        # auditor looking for that will search for the act, not for an update
        # that happens to carry a flag.
        _audit_shared(principal, view_id, updated["name"])
    return updated


def _audit_shared(principal: Principal, view_id: str, name: str) -> None:
    from . import service  # noqa: PLC0415

    service.get_audit().emit(
        principal.context(),
        "sheet_view_shared",
        {"view": view_id, "name": name},
    )


@router.post("/api/sheet/views/{view_id}/duplicate")
def duplicate_view(
    view_id: str,
    principal: Annotated[Principal, Depends(current_principal)],
) -> dict:
    """Copy a view to work from, without touching the one it came from.

    The copy is private and owned by whoever asked for it, whatever the
    original was: the usual reason to duplicate a shared view is to change it,
    and changing a copy that stayed shared would edit everyone's screen.
    """
    from . import service  # noqa: PLC0415

    views = _views()
    original = views.get(principal.tenant_id, view_id)
    if original is None or (
        original["owner"] != principal.email and not original["shared"]
    ):
        # Not "403 someone else's": a view a reader cannot see should not be
        # confirmed to exist by the error it produces.
        raise HTTPException(status_code=404, detail="Unknown view.")

    copy = views.create(
        principal.tenant_id,
        name=f"{original['name']} (copy)"[:128],
        owner=principal.email,
        shared=False,
        definition=original["definition"],
    )
    service.get_audit().emit(
        principal.context(),
        "sheet_view_created",
        {"view": copy["id"], "name": copy["name"], "copy_of": view_id},
    )
    return copy


@router.delete("/api/sheet/views/{view_id}")
def delete_view(
    view_id: str,
    principal: Annotated[Principal, Depends(current_principal)],
) -> dict:
    from . import service  # noqa: PLC0415

    views = _views()
    existing = views.get(principal.tenant_id, view_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Unknown view.")
    if existing["owner"] != principal.email and not principal.can("admin"):
        raise HTTPException(
            status_code=403, detail="That view belongs to someone else."
        )

    views.delete(principal.tenant_id, view_id)
    service.get_audit().emit(
        principal.context(),
        "sheet_view_deleted",
        {"view": view_id},
        before={"name": existing["name"], "shared": existing["shared"]},
    )
    return {"deleted": view_id}


# ---------------------------------------------------------------------------
# Export centre
# ---------------------------------------------------------------------------


def _jobs():
    from ..db.repositories import PgExportJobs  # noqa: PLC0415

    _repository()  # same database requirement, same 503
    return PgExportJobs()


@router.post("/api/sheet/exports")
def create_export(
    body: dict,
    background: BackgroundTasks,
    principal: Annotated[Principal, Depends(require_role("clerk"))],
) -> dict:
    """Ask for a file of the rows currently on screen.

    A small export is written before the response returns, because waiting two
    seconds beats polling for them. A large one continues in the background and
    the job says when it is ready -- the browser will not hold a connection
    open for ten thousand invoices, and neither should the user.
    """
    from .. import exports  # noqa: PLC0415
    from . import service  # noqa: PLC0415

    fmt = str(body.get("format") or "csv").lower()
    if fmt not in exports.FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown format {fmt}. Available: {', '.join(exports.FORMATS)}.",
        )

    filters = _filters(
        principal.tenant_id,
        str(body.get("status") or "approved"),
        *(
            (body.get("filters") or {}).get(key)
            for key in (
                "q",
                "vendor",
                "currency",
                "date_from",
                "date_to",
                "approved_from",
                "approved_to",
                "min_total",
                "max_total",
                "po",
                "tax",
            )
        ),
        (body.get("filters") or {}).get("quality"),
    )
    spec = {
        "filters": filters,
        "columns": body.get("columns") or [],
        "case_ids": body.get("case_ids") or [],
        "sort": body.get("sort") or "approved_at",
        "order": body.get("order") or "desc",
        # The runner has no caller; carry the decision with the job.
        "mask_bank": not principal.can("approver"),
    }

    job = _jobs().create(
        principal.tenant_id,
        requested_by=principal.email,
        fmt=fmt,
        spec=spec,
    )
    service.get_audit().emit(
        principal.context(),
        "export_requested",
        {"export": job["id"], "format": fmt, "columns": len(spec["columns"])},
    )

    # How many rows this will be decides whether the user waits.
    _, total = _repository().page(
        principal.tenant_id, filters, "approved_at", "desc", 1
    )
    if spec["case_ids"]:
        total = len(spec["case_ids"])

    if total <= exports.INLINE_ROW_LIMIT:
        return exports.run(job["id"], principal.tenant_id)

    background.add_task(exports.run, job["id"], principal.tenant_id)
    return job


@router.get("/api/sheet/exports")
def list_exports(
    principal: Annotated[Principal, Depends(current_principal)],
) -> dict:
    return {"exports": _jobs().list(principal.tenant_id)}


@router.get("/api/sheet/exports/{job_id}")
def get_export(
    job_id: str,
    principal: Annotated[Principal, Depends(current_principal)],
) -> dict:
    job = _jobs().get(principal.tenant_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown export.")
    return job


@router.post("/api/sheet/exports/{job_id}/retry")
def retry_export(
    job_id: str,
    background: BackgroundTasks,
    principal: Annotated[Principal, Depends(require_role("clerk"))],
) -> dict:
    """Run a stale or failed export again, from the spec it already carries.

    The runner lives in the API process, so a deploy in the middle of a large
    export leaves a job that will never finish. Rather than make the user
    rebuild the same filters, offer them the job back.
    """
    from .. import exports  # noqa: PLC0415
    from . import service  # noqa: PLC0415

    jobs = _jobs()
    job = jobs.get(principal.tenant_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown export.")
    if job["status"] not in ("failed", "queued", "running") or (
        job["status"] != "failed" and not job["stale"]
    ):
        raise HTTPException(
            status_code=409,
            detail=f"That export is {job['status']}, not stuck.",
        )

    queued = jobs.update(
        principal.tenant_id, job_id, status="queued", error=None
    )
    service.get_audit().emit(
        principal.context(),
        "export_requested",
        {"export": job_id, "format": job["format"], "retry": True},
    )
    background.add_task(exports.run, job_id, principal.tenant_id)
    return queued


@router.get("/api/sheet/exports/{job_id}/download")
def download_export(
    job_id: str,
    principal: Annotated[Principal, Depends(current_principal)],
) -> Response:
    """The finished file.

    Proxied rather than handed out as a signed URL, the same way an invoice PDF
    is: the browser fetches this with a bearer token, and a plain link carries
    none.
    """
    from .. import exports  # noqa: PLC0415
    from . import service  # noqa: PLC0415

    job = _jobs().get(principal.tenant_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown export.")
    if job["status"] != "done" or not job["storage_key"]:
        raise HTTPException(
            status_code=409,
            detail=f"That export is {job['status']}, not ready to download.",
        )

    try:
        payload = service.get_blobs(principal.tenant_id).get(job["storage_key"])
    except Exception as exc:
        raise HTTPException(
            status_code=404, detail="The export file is no longer stored."
        ) from exc

    stamp = (job["created_at"] or "")[:10]
    filename = f"approved-invoices-{stamp}.{job['format']}"
    return Response(
        content=payload,
        media_type=exports.CONTENT_TYPES[job["format"]],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
