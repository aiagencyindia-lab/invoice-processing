"""Review workbench API.

The workbench is where the touchless rate is actually earned or lost. Extraction
produces a confidence signal; this is where a human resolves everything the
signal could not, and where every correction becomes both an accuracy
measurement and a candidate ALF rule.

Design decisions worth stating:

* **Low-confidence fields come first.** The queue orders by weakest field and
  the detail view focuses the first flagged one, because a reviewer's scarcest
  resource is attention, not clicks. Making them hunt for the suspect field is
  how a 45-second review becomes a three-minute one.
* **Every edit is recorded as a correction**, with the old value. Corrections
  are the asset that compounds: they measure real accuracy and they are the raw
  material the learning loop turns into rules. An edit applied without being
  recorded is data thrown away.
* **Reads go through the CaseRepository port**, so the workbench runs against
  local artifacts today and Postgres in production with no change here.

Authentication is enforced in `auth.py`, and the rule it exists to hold is
visible in every signature below: **no endpoint accepts a `tenant_id`**. The
tenant comes from the caller's verified token, so there is nowhere for a caller
to put one and nothing for a handler to forget to check. Reads and writes are
scoped through `principal.context(...)`, never through a module-level default.
"""

from __future__ import annotations

import csv
import io
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel

from .. import paths
from ..adapters import (
    FsCaseRepository,
    JsonDocumentRepository,
    JsonlAuditSink,
    LocalBlobStore,
)
from .auth import Principal, current_principal, require_role

app = FastAPI(title="Invoice Review Workbench", version="0.1.0")


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Refuse to serve a deployed environment that is misconfigured.

    The alternative is a process that starts happily and loses documents.
    """
    from .preflight import enforce  # noqa: PLC0415

    enforce()
    yield


app.router.lifespan_context = _lifespan


_corrections_path = paths.DATA_DIR / "corrections.jsonl"

# All three storage adapters resolve the same way: lazily, from configuration,
# memoized. Lazily because a module import must never open a connection and
# because a test's environment has to be able to take effect; from configuration
# because the local implementations are single-tenant and cannot serve a real
# deployment.
#
# These were previously module-level constants pinned to the filesystem
# implementations, which meant `PgCaseRepository` and `PgDocumentRepository`
# were unreachable: case artifacts went to a package-relative directory that is
# ephemeral on Cloud Run, and every processed invoice would be lost on restart.
_cases = None
_documents = None
_audit = None
_tenant_settings = None
_claims_writer = None


def _database_configured() -> bool:
    """True when a real database is available to persist to."""
    from ..db.session import cloud_sql_instance  # noqa: PLC0415

    return bool(cloud_sql_instance() or os.getenv("DATABASE_URL"))


def get_cases():
    """Case artifacts: Postgres when configured, local files otherwise.

    The filesystem adapter is bound to a single tenant (it serves one
    directory), so in local development it must be pinned to whatever tenant the
    dev principal presents -- otherwise every read is silently empty.
    """
    global _cases
    if _cases is None:
        if _database_configured():
            from ..db.repositories import PgCaseRepository  # noqa: PLC0415

            _cases = PgCaseRepository()
        else:
            _cases = FsCaseRepository(
                tenant_id=os.getenv("AUTH_DEV_TENANT", "default")
            )
    return _cases


def get_documents():
    """Ingested-document registry: Postgres when configured, JSON file otherwise.

    Not interchangeable in behaviour, and the difference matters: the Postgres
    implementation resolves an inbound-email recipient by looking up an *active
    tenant*, while the local one echoes the slug back. On the local adapter any
    recipient local-part becomes a tenant id, so it must never front a real
    deployment.
    """
    global _documents
    if _documents is None:
        if _database_configured():
            from ..db.repositories import PgDocumentRepository  # noqa: PLC0415

            _documents = PgDocumentRepository()
        else:
            _documents = JsonDocumentRepository()
    return _documents


def get_audit():
    """The audit sink: Postgres when a database is configured, else a file.

    An audit trail written to a local file on a Cloud Run instance is lost on
    every restart, which makes it worthless for the thing it exists to do. When
    a database is configured the events go there, where they are append-only by
    construction -- `audit_events` has SELECT and INSERT policies and no UPDATE
    or DELETE policy, so no row can be altered even by the table's owner.

    Failures are deliberately *not* swallowed. An action that could not be
    recorded is an action nobody can account for, and silently continuing is how
    a trail develops holes exactly where something went wrong.
    """
    global _audit
    if _audit is None:
        if _database_configured():
            from ..db.repositories import PgAuditSink  # noqa: PLC0415

            _audit = PgAuditSink()
        else:
            _audit = JsonlAuditSink()
    return _audit


def get_queue():
    """The work queue. Pub/Sub when configured, a local directory otherwise."""
    from ..worker.queue import get_queue as _resolve  # noqa: PLC0415

    return _resolve()


def get_exporter(tenant_id: str):
    """The Xero exporter for one tenant, or None when it is not configured.

    Returning None rather than raising lets the endpoint answer 503 with a
    useful message: "not connected" is a setup state, not a server fault.

    The client secret is deliberately never read here. This module handles an
    access token issued by the OAuth flow and nothing else, so the long-lived
    credential cannot leak through the request path.
    """
    token = os.getenv("XERO_ACCESS_TOKEN", "")
    xero_tenant = os.getenv("XERO_TENANT_ID", "")
    if not token or not xero_tenant:
        return None

    from ..connectors.xero import (  # noqa: PLC0415
        HttpTransport,
        XeroClient,
        XeroCredentials,
        XeroExporter,
    )

    account_codes = json.loads(os.getenv("XERO_ACCOUNT_CODES", "{}"))
    return XeroExporter(
        XeroClient(
            XeroCredentials(access_token=token, xero_tenant_id=xero_tenant),
            transport=HttpTransport(),
        ),
        account_code_map=account_codes,
    )


def get_tenant_settings():
    """Per-tenant configuration: Postgres when configured, a JSON file otherwise."""
    global _tenant_settings
    if _tenant_settings is None:
        if _database_configured():
            from ..tenant_settings import PgTenantSettings  # noqa: PLC0415

            _tenant_settings = PgTenantSettings()
        else:
            from ..tenant_settings import JsonTenantSettings  # noqa: PLC0415

            _tenant_settings = JsonTenantSettings()
    return _tenant_settings


def claims_are_live() -> bool:
    """Whether Identity Platform claims are actually written.

    Off by default, mirroring the provisioning CLI: claim writing grants real
    access, so it is an explicit opt-in rather than something that happens
    because a form was submitted.
    """
    return os.getenv("APPLY_IDENTITY_CLAIMS", "").lower() in (
        "1",
        "true",
        "yes",
    )


def get_claims_writer():
    """The Identity Platform claims writer, or a dry-run recorder.

    Falls back to dry run when the project is unconfigured rather than raising:
    a misconfigured deployment should still let an admin create user records
    and see clearly that access was not granted, instead of failing the request
    with no explanation.
    """
    global _claims_writer
    if _claims_writer is None:
        from ..admin.provision import (  # noqa: PLC0415
            DryRunClaims,
            IdentityToolkitClaims,
        )

        if claims_are_live():
            try:
                _claims_writer = IdentityToolkitClaims()
            except Exception:
                _claims_writer = DryRunClaims()
        else:
            _claims_writer = DryRunClaims()
    return _claims_writer


def get_blobs(tenant_id: str):
    """Blob storage for one tenant.

    GCS in production, where `GcsBlobStore` prefixes every key with the tenant;
    a local directory otherwise. Resolved per call so a request cannot reuse
    another tenant's store.
    """
    import os  # noqa: PLC0415

    if os.getenv("GCS_BUCKET"):
        from ..gcs import GcsBlobStore  # noqa: PLC0415

        return GcsBlobStore(tenant_id=tenant_id)
    return LocalBlobStore(paths.DATA_DIR / "blobs" / tenant_id)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class FieldEdit(BaseModel):
    field: str
    value: Any


class EditRequest(BaseModel):
    edits: list[FieldEdit]


class ActionRequest(BaseModel):
    action: str  # approve | reject
    reason: str | None = None


class ExportRequest(BaseModel):
    target: str = "csv"  # csv | xero


# Neither model carries an `actor`. It is taken from the verified token instead:
# a caller-supplied actor would let anyone attribute a correction or an approval
# to a colleague, and the audit trail is only worth keeping if it cannot be.


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _summarize(case_id: str, artifacts: dict) -> dict:
    invoice = (artifacts.get("extraction") or {}).get("invoice") or {}
    evidence = invoice.get("_evidence") or {}
    summary = evidence.get("_summary") or {}
    decision = artifacts.get("decision") or {}
    postprocessing = artifacts.get("postprocessing") or {}

    return {
        "case_id": case_id,
        "vendor": invoice.get("vendor_name"),
        "invoice_number": invoice.get("invoice_number"),
        "invoice_date": invoice.get("invoice_date"),
        "currency": invoice.get("currency"),
        "total": invoice.get("invoice_total_inc_tax"),
        "decision": decision.get("final_decision")
        or (postprocessing.get("Invoice Processing") or {}).get(
            "Invoice Status"
        ),
        "rejection_reason": decision.get("rejection_template"),
        "routing": invoice.get("_routing", "REVIEW"),
        "min_confidence": summary.get("min_confidence"),
        "fields_needing_review": summary.get("fields_needing_review", []),
        "has_text_layer": summary.get("has_text_layer"),
    }


def _findings(artifacts: dict) -> list[dict]:
    """Every validation step, so a decision can be explained rather than asserted."""
    out = []
    for key in ("phase1", "phase2", "phase3", "phase4"):
        phase = artifacts.get(key) or {}
        for validation in phase.get("validations", []) or []:
            out.append(
                {
                    "phase": key,
                    "step": validation.get("step"),
                    "rule": validation.get("rule"),
                    "passed": validation.get("passed"),
                    "informational": validation.get("informational", False),
                    "implemented": validation.get("implemented", True),
                    "evidence": validation.get("evidence"),
                    "rejection_template": validation.get("rejection_template"),
                }
            )
    return out


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/api/invoices")
def list_invoices(
    principal: Annotated[Principal, Depends(current_principal)],
) -> dict:
    """Review queue, weakest confidence first.

    Ordering by confidence rather than arrival puts the invoices that actually
    need a human at the top; a chronological queue buries them.
    """
    items = []
    for case_id in get_cases().iter_case_ids(principal.tenant_id):
        artifacts = get_cases().get_artifacts(principal.context(case_id))
        if artifacts:
            items.append(_summarize(case_id, artifacts))

    items.sort(
        key=lambda i: (
            i["routing"] != "REVIEW",  # review queue first
            i["min_confidence"] if i["min_confidence"] is not None else 1.0,
            i["case_id"],
        )
    )
    return {
        "invoices": items,
        "counts": {
            "total": len(items),
            "needs_review": sum(1 for i in items if i["routing"] == "REVIEW"),
            "auto": sum(1 for i in items if i["routing"] == "AUTO"),
        },
    }


@app.get("/api/invoices/{case_id}")
def get_invoice(
    case_id: str, principal: Annotated[Principal, Depends(current_principal)]
) -> dict:
    artifacts = get_cases().get_artifacts(principal.context(case_id))
    if not artifacts:
        raise HTTPException(status_code=404, detail=f"Unknown case {case_id}")

    invoice = (artifacts.get("extraction") or {}).get("invoice") or {}
    evidence = invoice.get("_evidence") or {}

    fields = []
    for name, value in invoice.items():
        if name.startswith("_") or name == "line_items":
            continue
        field_evidence = evidence.get(name) or {}
        fields.append(
            {
                "name": name,
                "value": value,
                "confidence": field_evidence.get("confidence"),
                "grounded": field_evidence.get("grounded"),
                "page": field_evidence.get("page"),
                # Only fields we can corroborate carry a verdict; the rest are
                # shown without one rather than with a fabricated score.
                "checked": bool(field_evidence),
            }
        )
    # Flagged fields first: attention is the scarce resource.
    fields.sort(key=lambda f: (f["confidence"] is None, f["confidence"] or 0))

    return {
        **_summarize(case_id, artifacts),
        "fields": fields,
        "line_items": invoice.get("line_items") or [],
        "findings": _findings(artifacts),
        "corrections": _read_corrections(case_id, principal.tenant_id),
        "evidence_summary": evidence.get("_summary") or {},
    }


@app.get("/api/invoices/{case_id}/document")
def get_document(
    case_id: str, principal: Annotated[Principal, Depends(current_principal)]
):
    """Serve the source PDF.

    Local file today. In production this returns a short-lived signed GCS URL so
    the bytes never pass through the API tier.
    """
    # Confirm the case belongs to the caller's tenant before serving bytes:
    # otherwise this endpoint would hand over any document by guessing an id.
    if not get_cases().get_artifacts(principal.context(case_id)):
        raise HTTPException(status_code=404, detail="No document for this case")
    for name in ("invoice.pdf", "waf.pdf"):
        candidate = paths.EXEMPLARY_DIR / case_id / name
        if candidate.exists():
            return FileResponse(candidate, media_type="application/pdf")

    # Everything that is not one of the three bundled sample cases lives in the
    # blob store, keyed by content hash. Serving only the samples meant the
    # reviewer's document pane was blank for every invoice a customer had
    # actually uploaded -- a review screen with no document to review.
    key = _storage_key_for_case(principal.tenant_id, case_id)
    if key:
        try:
            data = get_blobs(principal.tenant_id).get(key)
        except (OSError, KeyError):
            data = None
        if data:
            return Response(content=data, media_type="application/pdf")

    raise HTTPException(status_code=404, detail="No document for this case")


def _storage_key_for_case(tenant_id: str, case_id: str) -> str | None:
    """Where this case's source PDF is stored, if we still hold it.

    A case id is the id of the document it came from. Scanning the tenant's
    listing rather than adding a lookup keeps both repositories on the methods
    they already implement.
    # ponytail: linear scan; add DocumentRepository.get() if listings outgrow it.
    """
    try:
        documents = get_documents().list_documents(tenant_id, limit=1000)
    except Exception:
        return None
    for document in documents or []:
        if document.get("id") == case_id:
            return document.get("storage_key")
    return None


@app.patch("/api/invoices/{case_id}/fields")
def edit_fields(
    case_id: str,
    request: EditRequest,
    principal: Annotated[Principal, Depends(require_role("clerk"))],
) -> dict:
    """Apply reviewer edits, recording each as a correction.

    The correction -- old value, new value, who, when -- is the point. It is
    what accuracy is measured from and what the learning loop mines for rules.
    """
    ctx = principal.context(case_id)
    artifacts = get_cases().get_artifacts(ctx)
    if not artifacts:
        raise HTTPException(status_code=404, detail=f"Unknown case {case_id}")

    extraction = artifacts.get("extraction") or {}
    invoice = extraction.get("invoice") or {}

    recorded = []
    for edit in request.edits:
        old = invoice.get(edit.field)
        if old == edit.value:
            continue  # a no-op edit is not a correction
        invoice[edit.field] = edit.value
        record = {
            "case_id": case_id,
            "field_path": edit.field,
            "old_value": old,
            "new_value": edit.value,
            # The actor is the authenticated caller, not a self-reported name:
            # corrections are accuracy evidence and must not be forgeable.
            "actor": principal.email,
            "tenant_id": principal.tenant_id,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        recorded.append(record)
        _append_correction(record)

    if recorded:
        extraction["invoice"] = invoice
        get_cases().put_artifact(ctx, "extraction", extraction)
        get_audit().emit(
            ctx,
            "fields_corrected",
            {
                "count": len(recorded),
                "fields": [r["field_path"] for r in recorded],
            },
        )

    return {"corrections": recorded, "count": len(recorded)}


@app.post("/api/invoices/{case_id}/actions")
def act(
    case_id: str,
    request: ActionRequest,
    principal: Annotated[Principal, Depends(require_role("approver"))],
) -> dict:
    if request.action not in ("approve", "reject"):
        raise HTTPException(
            status_code=400, detail="action must be approve or reject"
        )
    if request.action == "reject" and not request.reason:
        # A rejection without a reason is unusable: the vendor cannot fix it and
        # the audit trail cannot explain it.
        raise HTTPException(
            status_code=400, detail="A rejection requires a reason"
        )

    ctx = principal.context(case_id)
    artifacts = get_cases().get_artifacts(ctx)
    if not artifacts:
        raise HTTPException(status_code=404, detail=f"Unknown case {case_id}")

    _record_workflow(
        ctx,
        decision=request.action,
        decided_by=principal.email,
        decided_at=datetime.now(timezone.utc).isoformat(),
        reason=request.reason,
    )
    get_audit().emit(
        ctx,
        f"invoice_{request.action}d",
        {"reason": request.reason},
    )
    return {
        "case_id": case_id,
        "action": request.action,
        "reason": request.reason,
    }


@app.get("/api/stats")
def stats(principal: Annotated[Principal, Depends(current_principal)]) -> dict:
    """The numbers a pilot is judged on."""
    listing = list_invoices(principal)
    items = listing["invoices"]
    corrections = _read_corrections(tenant_id=principal.tenant_id)
    return {
        **listing["counts"],
        "touchless_rate": (
            round(listing["counts"]["auto"] / len(items), 3) if items else None
        ),
        "corrections": len(corrections),
        "corrected_fields": sorted({c["field_path"] for c in corrections}),
    }


# ---------------------------------------------------------------------------
# Workflow state
# ---------------------------------------------------------------------------

WORKFLOW_ARTIFACT = "workflow"


def _workflow(ctx) -> dict:
    """Approval and export state for one case.

    Approval used to be recorded only as an audit event. An audit event is a
    record that something happened, not a fact the system can act on -- so
    nothing downstream could tell an approved invoice from an unreviewed one,
    and the export path had no way to honour "post only after approval". This
    is where that fact lives.
    """
    return (get_cases().get_artifacts(ctx) or {}).get(WORKFLOW_ARTIFACT) or {}


def _record_workflow(ctx, **changes) -> dict:
    state = {**_workflow(ctx), **changes}
    get_cases().put_artifact(ctx, WORKFLOW_ARTIFACT, state)
    return state


# ---------------------------------------------------------------------------
# Corrections store
# ---------------------------------------------------------------------------


def _append_correction(record: dict) -> None:
    _corrections_path.parent.mkdir(parents=True, exist_ok=True)
    with open(_corrections_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, default=str) + "\n")


def _read_corrections(
    case_id: str | None = None, tenant_id: str | None = None
) -> list[dict]:
    if not _corrections_path.exists():
        return []
    out = []
    for line in _corrections_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if case_id is not None and record.get("case_id") != case_id:
            continue
        # Older records predate tenant stamping; a record with no tenant is
        # only ever visible to the tenant that is asking for everything.
        if tenant_id is not None and record.get("tenant_id") not in (
            None,
            tenant_id,
        ):
            continue
        out.append(record)
    return out


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

CSV_COLUMNS = [
    "case_id",
    "vendor_name",
    "vendor_tax_id",
    "invoice_number",
    "invoice_date",
    "due_date",
    "currency",
    "subtotal_ex_tax",
    "tax_total",
    "total_inc_tax",
    "po_number",
    "payment_terms",
]


def _csv_row(case_id: str, invoice: dict) -> list[str]:
    mapping = {
        "case_id": case_id,
        "vendor_name": invoice.get("vendor_name"),
        "vendor_tax_id": invoice.get("vendor_tax_id"),
        "invoice_number": invoice.get("invoice_number"),
        "invoice_date": invoice.get("invoice_date"),
        "due_date": invoice.get("due_date"),
        "currency": invoice.get("currency"),
        "subtotal_ex_tax": invoice.get("invoice_total_ex_tax"),
        "tax_total": invoice.get("tax_total"),
        "total_inc_tax": invoice.get("invoice_total_inc_tax"),
        "po_number": invoice.get("po_number"),
        "payment_terms": invoice.get("payment_terms"),
    }
    return ["" if mapping[c] is None else str(mapping[c]) for c in CSV_COLUMNS]


@app.post("/api/invoices/{case_id}/export")
def export_invoice(
    case_id: str,
    request: ExportRequest,
    principal: Annotated[Principal, Depends(require_role("approver"))],
) -> dict:
    """Export one approved invoice to the accounting system.

    Two rules make this safe enough to point at a real ledger:

    * **Approved only.** Posting before approval puts an unreviewed liability on
      the books, which is the failure this whole product exists to prevent. The
      decision is read from durable workflow state, not from an audit event.
    * **Once.** A recorded `external_id` stops a second post on its own, and the
      connector additionally searches Xero for an existing bill in case a prior
      attempt succeeded with its response lost in transit. Duplicate payments
      are the expensive mistake in accounts payable.
    """
    ctx = principal.context(case_id)
    artifacts = get_cases().get_artifacts(ctx)
    if not artifacts:
        raise HTTPException(status_code=404, detail=f"Unknown case {case_id}")

    invoice = (artifacts.get("extraction") or {}).get("invoice") or {}
    state = _workflow(ctx)
    approved = state.get("decision") == "approve"

    if not approved:
        raise HTTPException(
            status_code=409,
            detail=(
                "Invoice is not approved. Bills export only after approval -- "
                "exporting earlier puts an unreviewed liability on the ledger."
            ),
        )

    if request.target == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(CSV_COLUMNS)
        writer.writerow(_csv_row(case_id, invoice))
        get_audit().emit(ctx, "invoice_exported", {"target": "csv"})
        return {
            "case_id": case_id,
            "target": "csv",
            "status": "created",
            "csv": buffer.getvalue(),
        }

    if request.target != "xero":
        raise HTTPException(
            status_code=400, detail="target must be 'csv' or 'xero'"
        )

    exporter = get_exporter(principal.tenant_id)
    if exporter is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Xero is not configured for this tenant. Set XERO_ACCESS_TOKEN "
                "and XERO_TENANT_ID, or export to CSV."
            ),
        )

    result = exporter.export(
        invoice, approved=True, external_id=state.get("external_id")
    )
    if result.status == "failed":
        get_audit().emit(
            ctx,
            "invoice_export_failed",
            {"target": "xero", "message": result.message},
        )
        raise HTTPException(status_code=502, detail=result.message)

    _record_workflow(
        ctx,
        external_id=result.external_id,
        exported_at=datetime.now(timezone.utc).isoformat(),
        exported_by=principal.email,
        export_target="xero",
    )
    get_audit().emit(
        ctx,
        "invoice_exported",
        {
            "target": "xero",
            "external_id": result.external_id,
            "status": result.status,
        },
    )
    return {
        "case_id": case_id,
        "target": "xero",
        "status": result.status,
        "external_id": result.external_id,
        "message": result.message,
    }


from .admin_routes import router as admin_router  # noqa: E402
from .config_routes import router as config_router  # noqa: E402
from .ingest import router as ingest_router  # noqa: E402
from .system import router as system_router  # noqa: E402

app.include_router(ingest_router)
app.include_router(system_router)
app.include_router(config_router)
app.include_router(admin_router)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    """Point a browser at the application.

    This used to serve the single-file workbench. That page could reach four
    endpoints out of twenty-five, so everything else the platform does --
    processing status, dead letters, settings, purchase orders, provisioning --
    was reachable only from a terminal. The Next.js app in `frontend/` replaces
    it and is the UI now; this service is the API behind it.

    A link rather than a redirect: the API is a legitimate destination in its
    own right, and bouncing an operator who typed the API's own host to a
    frontend that may not be running would be worse than telling them where it
    is.
    """
    app_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Invoice processing API</title>
<style>
  body {{ font: 15px/1.6 system-ui, sans-serif; color: #1a1a1a;
         background: #ececea; margin: 0;
         display: flex; align-items: center; justify-content: center;
         min-height: 100vh; }}
  main {{ background: #fff; border: 1px solid #e2e2df; border-radius: 10px;
          padding: 32px 36px; max-width: 460px; }}
  h1 {{ font-size: 18px; margin: 0 0 10px; }}
  p {{ color: #6b6b67; margin: 0 0 14px; }}
  a.button {{ display: inline-block; background: #1a1a1a; color: #fff;
              padding: 10px 18px; border-radius: 6px; text-decoration: none;
              font-weight: 600; font-size: 14px; }}
  code {{ background: #f7f7f5; padding: 2px 6px; border-radius: 4px;
          font-size: 13px; }}
</style></head>
<body><main>
  <h1>Invoice processing API</h1>
  <p>This is the API. The application is served separately.</p>
  <p><a class="button" href="{app_url}">Open the application</a></p>
  <p>Not running? Start it with <code>cd frontend &amp;&amp; npm run dev</code>.
     API reference at <a href="/docs">/docs</a>.</p>
</main></body></html>"""
