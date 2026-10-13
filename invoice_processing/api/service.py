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
import logging
import os
import re
import time
from contextlib import asynccontextmanager, nullcontext
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError

from .. import (
    approval_flow,
    approvals,
    case_projection,
    exports,
    observability,
    paths,
    po_matching,
)
from ..adapters import (
    FsCaseRepository,
    JsonDocumentRepository,
    JsonlAuditSink,
    JsonlCorrectionStore,
    JsonSupportingDocumentRepository,
    LocalBlobStore,
)
from ..masking import BANK_FIELDS, MASK_PREFIX, mask_bank, mask_correction
from ..ports import LockHeld
from . import auth, preflight
from .auth import Principal, current_principal, require_role

logger = logging.getLogger(__name__)

# The schema pages describe every route to anyone who asks. Fine on a
# laptop; on a deployed host they are a map handed to a stranger. ENV is
# fixed for the life of a process, so this is decided once at import.
_SERVE_DOCS = not preflight.is_deployed()
app = FastAPI(
    title="Invoice Review Workbench",
    version="0.1.0",
    docs_url="/docs" if _SERVE_DOCS else None,
    redoc_url="/redoc" if _SERVE_DOCS else None,
    openapi_url="/openapi.json" if _SERVE_DOCS else None,
)

# What a browser may do with a response. The app in `frontend/` sets the
# same set on its own pages; the API repeats them so a direct call is no
# weaker than a proxied one.
_FIXED_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
# The API's own HTML is the landing page and the schema browser: static,
# styled inline, scripts from jsdelivr for Swagger and ReDoc.
_HTML_CSP = (
    "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net "
    "'unsafe-inline'; style-src 'self' https://cdn.jsdelivr.net "
    "'unsafe-inline'; img-src 'self' data: https://fastapi.tiangolo.com; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'; "
    "object-src 'none'"
)


_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


@app.middleware("http")
async def _identify_request(request, call_next):
    """Give one request one id, and remember where it came from.

    Both are audit columns the trail declared and never filled: every event in
    production had a null `request_id` and a null `ip`, so two events from the
    same action could not be tied together and no action could be placed.
    It also writes one log line per request, which uvicorn's access log
    (turned off in observability.configure) used to do without the id.
    """
    from uuid import uuid4  # noqa: PLC0415

    offered = request.headers.get("x-request-id", "")
    # The audit column is 64 characters and a log search keys on this value:
    # anything else a client offers is replaced, not stored.
    request_id = offered if _REQUEST_ID.fullmatch(offered) else uuid4().hex
    # Railway terminates TLS in front of the app, so `request.client` is the
    # proxy. The first entry of the forwarded chain is the actual caller.
    forwarded = request.headers.get("x-forwarded-for", "")
    client = forwarded.split(",")[0].strip() or (
        request.client.host if request.client else ""
    )

    id_token = auth.current_request_id.set(request_id)
    ip_token = auth.current_client_ip.set(client)
    tenant_token = observability.current_tenant_id.set("")
    started = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
    except Exception:
        # Caught here rather than left to Starlette, whose 500 would go out
        # without the request id or any of the headers below. The tenant is
        # set first so this line, and the Sentry event it raises, name it.
        observability.current_tenant_id.set(
            getattr(request.state, "tenant_id", "")
        )
        logger.exception(
            "Unhandled error on %s %s", request.method, request.url.path
        )
        response = JSONResponse(
            status_code=500, content={"detail": "Internal Server Error"}
        )
    finally:
        # Before the resets, so the line still carries the ids. The tenant
        # comes from auth.current_principal via the shared request state.
        observability.current_tenant_id.set(
            getattr(request.state, "tenant_id", "")
        )
        # The route template groups lines; an unmatched path is whatever a
        # scanner typed, so it is not one.
        route = getattr(request.scope.get("route"), "path", "<unmatched>")
        # Every open tab polls the status every few seconds; at INFO that
        # would be most of the retained log. Failures still show.
        quiet = route == "/api/system/status" and status == 200
        logger.log(
            logging.DEBUG if quiet else logging.INFO,
            "request",
            extra={
                "method": request.method,
                "route": route,
                "status": status,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                "ip": client,
            },
        )
        auth.current_request_id.reset(id_token)
        auth.current_client_ip.reset(ip_token)
        observability.current_tenant_id.reset(tenant_token)
    response.headers["x-request-id"] = request_id
    response.headers.update(_FIXED_HEADERS)
    if preflight.is_deployed():
        # Only where TLS is real: a browser that saw HSTS from a local http
        # host would refuse that host for a year.
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("text/html"):
        response.headers["Content-Security-Policy"] = _HTML_CSP
    elif (
        request.url.path.startswith("/api/")
        and content_type.startswith("application/json")
        and "content-disposition" not in response.headers
    ):
        # Tenant data must not sit in a shared cache; downloads keep the
        # default so "Save as" works.
        response.headers["Cache-Control"] = "no-store"
    return response


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Refuse to serve a deployed environment that is misconfigured, and bring
    the schema up to date before serving.

    The alternative to the first is a process that starts happily and loses
    documents. The alternative to the second was somebody remembering to run
    Alembic by hand -- which is how production came to be one migration ahead
    of nothing at all, with no check that the tables it queries exist.
    """
    from .preflight import enforce  # noqa: PLC0415

    observability.configure("api")
    enforce()

    if _database_configured():
        from ..db.migrate import upgrade_to_head  # noqa: PLC0415

        upgrade_to_head()
    yield


app.router.lifespan_context = _lifespan


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
_corrections = None
_claim_store = None
_locks = None
_vendors = None
_approvals = None
_gl_rules = None
_purchase_orders = None
_supporting_documents = None
_rules = None
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


def get_corrections():
    """The correction store: Postgres when configured, else a local file.

    Same reasoning as the audit sink. A correction is the accuracy record and
    the raw material for a future rule; on a container filesystem it does not
    survive the next deploy, and two API replicas each keep half of it.
    """
    global _corrections
    if _corrections is None:
        if _database_configured():
            from ..db.repositories import PgCorrectionStore  # noqa: PLC0415

            _corrections = PgCorrectionStore()
        else:
            _corrections = JsonlCorrectionStore()
    return _corrections


def get_claim_store(worker_id: str = ""):
    """Who is processing what: Postgres when configured, else local files.

    The file store excludes only the processes on one machine. With the API
    and the worker in separate containers it excluded nothing that mattered --
    two workers could hold the same claim, and the API could not see either.
    """
    global _claim_store
    if _claim_store is None:
        if _database_configured():
            from ..db.repositories import PgClaimStore  # noqa: PLC0415

            _claim_store = PgClaimStore(worker_id=worker_id)
        else:
            from .. import paths  # noqa: PLC0415
            from ..worker.runner import ClaimStore  # noqa: PLC0415

            _claim_store = ClaimStore(paths.DATA_DIR / "claims")
    return _claim_store


def get_rules():
    """This tenant's correction rules. Postgres when configured, else a file.

    Postgres is not a preference in a deployed system, it is the only thing
    that works: the API and the worker are separate containers, so a rule
    approved through the API would never reach a worker reading its own disk.

    The rule base is the thing that was written and never read -- the port has
    carried a "no live consumer" note since it was designed. This is the live
    consumer.
    """
    global _rules
    if _rules is None:
        if _database_configured():
            from ..db.repositories import PgRuleRepository  # noqa: PLC0415

            _rules = PgRuleRepository()
        else:
            from ..adapters import JsonRuleRepository  # noqa: PLC0415

            _rules = JsonRuleRepository()
    return _rules


def get_locks():
    """Who is editing what. `None` when there is no database.

    Without one there is nowhere two containers could both see a lock, and the
    filesystem stores are a single-developer mode where nobody is racing
    anybody. Returning None makes the guard a no-op there rather than
    inventing an exclusion that does not hold.

    # ponytail: a file-backed lock would exclude one machine's processes and
    # read as safety everywhere else. Postgres or nothing is the honest pair.
    """
    global _locks
    if (
        _locks is None
        and _database_configured()
        and _table_exists("record_locks", "editing")
    ):
        from ..db.repositories import PgRecordLocks  # noqa: PLC0415

        _locks = PgRecordLocks()
    return _locks


def _table_exists(
    name: str, unprotected: str, *, raise_on_error: bool = False
) -> bool:
    """Whether the schema a feature needs is actually there.

    A database can be configured and unmigrated -- an in-memory one in a test,
    or a deployment with `RUN_MIGRATIONS=0`. Asking the database beats assuming
    either way: assuming the table exists turns every request into a 500, and
    assuming it does not would disable the feature on a real deployment. Said
    out loud when it is missing, because silence is how the storage and RLS gaps
    in this codebase's own audit stayed invisible.
    """
    from sqlalchemy import inspect  # noqa: PLC0415

    from ..db.session import get_engine  # noqa: PLC0415

    try:
        if inspect(get_engine()).has_table(name):
            return True
    except Exception:
        if raise_on_error:
            raise
        logger.warning("Could not check for the %s table", name)
        return False
    logger.warning(
        "No %s table: %s is unprotected until migrations run", name, unprotected
    )
    return False


def get_vendors():
    """Suppliers and their verified bank details. `None` without the table.

    Same reasoning as `get_locks`: a file would be private to one container,
    and a bank-change approval the worker cannot see is no control at all.
    """
    global _vendors
    if (
        _vendors is None
        and _database_configured()
        and _table_exists("vendors", "vendor bank-detail control")
    ):
        from ..db.repositories import PgVendorRepository  # noqa: PLC0415

        _vendors = PgVendorRepository()
    return _vendors


def get_approvals():
    """Approval chains, rules, groups and delegations. `None` without the table.

    A database that is configured but cannot be asked raises rather than
    answering None: None means "no routing here", and `act` would then record
    a decision on an invoice whose chain nobody checked.
    """
    global _approvals
    if (
        _approvals is None
        and _database_configured()
        and _table_exists(
            "approval_steps", "approval routing", raise_on_error=True
        )
    ):
        from ..db.repositories import PgApprovalRepository  # noqa: PLC0415

        _approvals = PgApprovalRepository()
    return _approvals


def get_gl_rules():
    """Tenant GL coding rules. `None` without the table.

    Raises when a configured database cannot be asked, like `get_approvals`:
    None means "no rules here", and the worker would then file every line
    uncoded without saying why.
    """
    global _gl_rules
    if (
        _gl_rules is None
        and _database_configured()
        and _table_exists("gl_rules", "GL coding", raise_on_error=True)
    ):
        from ..db.repositories import PgGlRuleRepository  # noqa: PLC0415

        _gl_rules = PgGlRuleRepository()
    return _gl_rules


_user_directory = None
_sso_domains = None


def get_user_directory():
    """The users rows as the request path reads them. None without a
    database, and None when the users table is not there -- the suite runs
    with an in-memory database that has no schema, and a file-store
    deployment must keep answering from claims alone."""
    global _user_directory
    if (
        _user_directory is None
        and _database_configured()
        and _table_exists("users", "sign-in")
    ):
        from ..db.repositories import PgUserDirectory  # noqa: PLC0415

        _user_directory = PgUserDirectory()
    return _user_directory


def get_sso_domains():
    """Domain bindings. None without the table."""
    global _sso_domains
    if (
        _sso_domains is None
        and _database_configured()
        and _table_exists("sso_domains", "single sign-on")
    ):
        from ..db.repositories import PgSsoDomainRepository  # noqa: PLC0415

        _sso_domains = PgSsoDomainRepository()
    return _sso_domains


def _sso_services():
    """Sign-in's dependencies, or None where there is no database."""
    from .. import sso  # noqa: PLC0415

    global _user_directory
    if not _database_configured():
        return None
    if _user_directory is None:
        # Raises when the database cannot be asked: a configured database that
        # is down must refuse sign-in, not fall back to claims and admit a
        # deactivated user. Only a table that is really absent means claims.
        if not _table_exists("users", "sign-in", raise_on_error=True):
            return None
        from ..db.repositories import PgUserDirectory  # noqa: PLC0415

        _user_directory = PgUserDirectory()
    return sso.SsoServices(_user_directory, get_claims_writer(), get_audit())


auth.set_sso_services(_sso_services)


def get_purchase_orders():
    """Purchase orders, their lines and goods receipts. `None` without the table.

    Raises when a configured database cannot be asked, like `get_gl_rules`:
    None means "no orders here", and the pipeline would then match against
    the file instead of the orders the tenant actually uploaded.
    """
    global _purchase_orders
    if (
        _purchase_orders is None
        and _database_configured()
        and _table_exists(
            "purchase_orders", "Purchase orders", raise_on_error=True
        )
    ):
        from ..db.repositories import (  # noqa: PLC0415
            PgPurchaseOrderRepository,
        )

        _purchase_orders = PgPurchaseOrderRepository()
    return _purchase_orders


def get_supporting_documents():
    """Scanned POs and receipts awaiting confirmation.

    A file without a database, so single-process development still works;
    `None` when a database is configured without the table, and raises when
    it cannot be asked -- the worker then retries rather than dropping a scan.
    """
    global _supporting_documents
    if _supporting_documents is None:
        if not _database_configured():
            _supporting_documents = JsonSupportingDocumentRepository()
        elif _table_exists(
            "supporting_documents", "Scanned documents", raise_on_error=True
        ):
            from ..db.repositories import (  # noqa: PLC0415
                PgSupportingDocumentRepository,
            )

            _supporting_documents = PgSupportingDocumentRepository()
    return _supporting_documents


def non_invoice_ids(tenant_id: str) -> set[str]:
    """Documents classified as something other than an invoice.

    Cases are keyed by document id, so these are the cases to leave out of
    invoice listings. A lookup that fails hides nothing: a stray row in a
    list beats a list that will not load.
    """
    try:
        return get_documents().non_invoice_ids(tenant_id)
    except Exception as exc:
        logger.warning("Document kinds not read for %s: %s", tenant_id, exc)
        return set()


_SCANNED = {
    "purchase_order": "purchase order",
    "goods_receipt": "goods receipt",
}


def require_invoice_case(tenant_id: str, case_id: str) -> None:
    """404 a case whose document is a scanned PO or receipt.

    Its case holds an extraction awaiting confirmation; reading, editing or
    deciding it as an invoice would act on something that is not a payable.
    """
    try:
        document = get_documents().get(tenant_id, case_id) or {}
    except Exception as exc:
        # ponytail: unreadable kind lets the route run; the case then has no
        # invoice extraction to act on. Fail closed if that ever matters.
        logger.warning("Document kind of %s not read: %s", case_id, exc)
        return
    if (label := _SCANNED.get(document.get("kind"))) is not None:
        raise HTTPException(
            status_code=404,
            detail=f"This document is a scanned {label}; review it on "
            "Purchase orders.",
        )


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

    # The credentials are process-wide, so they belong to exactly one customer
    # -- and this function took a tenant id and ignored it. On a deployment
    # with two tenants, both would have posted their bills into whichever Xero
    # organisation those variables point at: one customer's invoices appearing
    # in another's ledger, from a function whose signature promised otherwise.
    #
    # `XERO_OWNER_TENANT` names the tenant those credentials belong to. Until
    # there is a per-tenant credential store with an OAuth flow behind it, an
    # unnamed owner means the connector serves whichever tenant is the only
    # one -- and refuses as soon as that stops being true.
    owner = os.getenv("XERO_OWNER_TENANT", "")
    if owner:
        if tenant_id != owner:
            return None
    elif not _is_only_tenant(tenant_id):
        logger.warning(
            "Xero credentials are process-wide and no XERO_OWNER_TENANT is "
            "set, but this deployment has more than one tenant. Refusing to "
            "export tenant %s into an organisation that may belong to "
            "another.",
            tenant_id,
        )
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


def _is_only_tenant(tenant_id: str) -> bool:
    """Whether this deployment serves this tenant and nobody else.

    Fails closed: if the question cannot be answered, the answer is no.
    """
    if not _database_configured():
        return True
    try:
        from sqlalchemy import func, select  # noqa: PLC0415
        from sqlalchemy.orm import Session  # noqa: PLC0415

        from ..db.models import Tenant  # noqa: PLC0415
        from ..db.session import get_engine  # noqa: PLC0415

        with Session(get_engine()) as session:
            return session.scalar(select(func.count(Tenant.id))) <= 1
    except Exception as exc:
        logger.warning("Could not count tenants: %s", exc)
        return False


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


# Why a reviewer decided what they did. Short on purpose: a long list is a
# list nobody reads honestly, and the only distinction the system acts on is
# whether the machine was wrong about the *data* or about the *policy*.
REASON_CATEGORIES = (
    "extraction_wrong",  # the machine read the invoice wrong
    "duplicate",  # already paid or already in flight
    "policy_exception",  # the rulebook does not cover this case
    "vendor_issue",  # something the supplier must fix
    "other",
)


class ActionRequest(BaseModel):
    action: str  # approve | reject
    reason: str | None = None
    # Defaulted rather than required, so a client written before this existed
    # keeps working. The workbench always sends one.
    reason_category: str | None = None
    # Admin only: why this decision goes against the routing rule. A non-admin
    # sending it gets 403 in file mode too (the field used to be ignored).
    override_reason: str | None = Field(default=None, max_length=2000)


class ExportRequest(BaseModel):
    target: str = "csv"  # csv | xero


# Neither model carries an `actor`. It is taken from the verified token instead:
# a caller-supplied actor would let anyone attribute a correction or an approval
# to a colleague, and the audit trail is only worth keeping if it cannot be.


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


# The worker's step counter is stored with the case's artifacts because that
# store is the only thing the API container and the worker container both
# reach. It is not a stage artifact, and every reader has to strip it: a case
# that has done nothing but report a step is a document being worked on, not
# an invoice, and listing it as one put a blank row in the workbench -- no
# vendor, no fields, and "no validation records".
PROGRESS_KEY = "progress"
ATTEMPTS_KEY = "attempts"
REMATCH_ATTEMPTS_KEY = "rematch_attempts"

# The worker's own bookkeeping, stored beside the artifacts because that store
# is the only thing every container reaches. None of it is pipeline output, and
# a reader that counts it as such reports a document nobody has processed yet
# as a finished invoice -- which is exactly how an in-flight upload came to sit
# in the workbench with no vendor and no validation records.
INTERNAL_KEYS = (PROGRESS_KEY, ATTEMPTS_KEY, REMATCH_ATTEMPTS_KEY)


def read_case(cases, ctx) -> tuple[dict, dict]:
    """A case's artifacts as readers understand them, and the bookkeeping."""
    return split_internal(cases.get_artifacts(ctx))


def split_internal(artifacts: dict | None) -> tuple[dict, dict]:
    artifacts = dict(artifacts or {})
    internal = {
        key: artifacts.pop(key)
        for key in INTERNAL_KEYS
        if isinstance(artifacts.get(key), dict)
    }
    for key in INTERNAL_KEYS:
        artifacts.pop(key, None)
    return artifacts, internal


def read_cases(cases, tenant_id: str, case_ids: list[str]) -> dict[str, tuple]:
    """The same, for many cases in one round trip.

    A query per case is fine against a local file and ruinous against a
    database three hundred milliseconds away: the queue and the document list
    each ran one, so both pages took five and a half seconds to answer and got
    slower with every invoice added.
    """
    fetched = cases.get_many_artifacts(tenant_id, case_ids) or {}
    return {
        case_id: split_internal(fetched.get(case_id)) for case_id in case_ids
    }


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
                    "signals": validation.get("signals"),
                    "lines": validation.get("lines"),
                }
            )
    # The rules that fired, so a corrected decision can say what corrected it.
    out.extend(case_projection.alf_findings(artifacts))
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
    cases = get_cases()
    skip = non_invoice_ids(principal.tenant_id)
    case_ids = [
        case_id
        for case_id in cases.iter_case_ids(principal.tenant_id)
        if case_id not in skip
    ]
    read = read_cases(cases, principal.tenant_id, case_ids)
    items = []
    for case_id in case_ids:
        artifacts, _ = read[case_id]
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
    require_invoice_case(principal.tenant_id, case_id)
    artifacts, _ = read_case(get_cases(), principal.context(case_id))
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
    if not principal.can("approver"):
        # Reading an invoice is not paying one: the full number is for the
        # people who verify a vendor's bank change, and the PDF shows it anyway.
        for field in fields:
            if field["name"] in BANK_FIELDS:
                field["value"] = mask_bank(field["value"])

    return {
        **_summarize(case_id, artifacts),
        "fields": fields,
        "line_items": invoice.get("line_items") or [],
        # The projected lines carry the numbering the GL endpoint addresses
        # and the account each line is coded to, so the workbench never has
        # to guess which extraction row is which line.
        "lines": case_projection.lines(artifacts),
        "gl_coding": artifacts.get("gl_coding"),
        "findings": _findings(artifacts),
        "corrections": [
            correction
            if principal.can("approver")
            else mask_correction(correction)
            for correction in get_corrections().list(
                principal.tenant_id, case_id
            )
        ],
        "evidence_summary": evidence.get("_summary") or {},
        # Who decided, why, and whether they agreed with the pipeline. The
        # detail endpoint returned none of this, so the one screen a reviewer
        # opens could not say that the last person to look at this invoice
        # had overruled a validation on it.
        "workflow": artifacts.get(WORKFLOW_ARTIFACT) or {},
        "agreement": case_projection.agreement(artifacts),
        # So a reader can see the record is being edited without asking a
        # second endpoint -- and so the editor knows whether it may start.
        "lock": (
            locks.get(principal.tenant_id, case_id)
            if (locks := get_locks())
            else None
        ),
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
    # The fields get_invoice lists are the fields a person may edit. Lines and
    # internals (evidence) are not fields: replacing line_items here would
    # change what a decided invoice exports without anyone re-approving it.
    for edit in request.edits:
        if edit.field.startswith("_") or edit.field == "line_items":
            raise HTTPException(
                status_code=400,
                detail=f"{edit.field} is not an editable field; edit the "
                "invoice's header fields here.",
            )
        if edit.field in BANK_FIELDS and str(edit.value or "").startswith(
            MASK_PREFIX
        ):
            # Non-approvers are shown the mask; typing it back would replace
            # a real number with a placeholder.
            raise HTTPException(
                status_code=422,
                detail="That looks like a masked value; enter the full number.",
            )
    require_invoice_case(principal.tenant_id, case_id)
    _require_unlocked(principal, case_id)
    ctx = principal.context(case_id)
    artifacts = get_cases().get_artifacts(ctx)
    if not artifacts:
        raise HTTPException(status_code=404, detail=f"Unknown case {case_id}")

    extraction = artifacts.get("extraction") or {}
    invoice = extraction.get("invoice") or {}

    changed = {
        e.field for e in request.edits if invoice.get(e.field) != e.value
    }
    # Checked before any correction is recorded, so a refused edit leaves no
    # half-applied trace. A decided invoice keeps its decision only while the
    # facts that routed it stand: changing who is paid, or how much, after
    # sign-off would ship a payment nobody approved.
    if (artifacts.get(WORKFLOW_ARTIFACT) or {}).get("decision") and (
        changed & ROUTING_FIELDS
    ):
        raise HTTPException(
            status_code=409,
            detail="This invoice was already decided; its amount, vendor, "
            "customer and currency are closed.",
        )
    for edit in request.edits:
        if (
            edit.field == "invoice_total_inc_tax"
            and edit.field in changed
            and approvals.parse_amount(edit.value) is None
        ):
            # An unreadable total would route to the highest tier anyway; a
            # person typing one has made a mistake worth saying so.
            raise HTTPException(
                status_code=400,
                detail="invoice_total_inc_tax must be a number, e.g. 12500.00.",
            )

    rerouted = False
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
        get_corrections().record(ctx, record)

    if recorded:
        # Only these fields, onto the extraction as stored now, so an edit to
        # another field that landed meanwhile is kept.
        # ponytail: old_value comes from the read above; under a race it can
        # name the value before the other edit. Exact if corrections matter.
        edits = {r["field_path"]: r["new_value"] for r in recorded}
        extraction = get_cases().update_artifact(
            ctx,
            "extraction",
            lambda cur: {
                **(cur or {}),
                "invoice": {**((cur or {}).get("invoice") or {}), **edits},
            },
        )
        artifacts["extraction"] = extraction
        _reproject(ctx, artifacts)
        get_audit().emit(
            ctx,
            "fields_corrected",
            {
                "count": len(recorded),
                "fields": [r["field_path"] for r in recorded],
                "values": {
                    r["field_path"]: r["new_value"]
                    for r in map(mask_correction, recorded)
                },
            },
            # The trail said which fields changed and by whom, never what they
            # were. That answer lived only in the corrections store, so an
            # auditor reading the trail alone could not reconstruct the edit.
            # Bank numbers go in masked: every role reads the trail, and the
            # corrections store keeps the full values, as vendor bank-change
            # events already do.
            before={
                "values": {
                    r["field_path"]: r["old_value"]
                    for r in map(mask_correction, recorded)
                }
            },
        )
        # After fields_corrected so the trail reads cause then effect.
        rerouted = _reroute_if_needed(ctx, artifacts, extraction, recorded)

    return {
        "corrections": recorded
        if principal.can("approver")
        else [mask_correction(r) for r in recorded],
        "count": len(recorded),
        "approvals_rerouted": rerouted,
    }


# Fields that decide who must approve (amount tier, vendor and entity rules).
ROUTING_FIELDS = {
    "invoice_total_inc_tax",
    "vendor_name",
    "customer_name",
    "currency",
}


def _reroute_if_needed(ctx, artifacts, extraction, recorded) -> bool | str:
    """Re-route only when an edit touched a field routing reads."""
    fields = sorted(ROUTING_FIELDS & {r["field_path"] for r in recorded})
    if not fields:
        return False
    return reroute(ctx, {**artifacts, "extraction": extraction}, fields)


def reroute(ctx, artifacts: dict, fields: list[str]) -> bool | str:
    """Rebuild the pending chain after `fields` changed.

    True if the chain really changed, False if not, "failed" on an error. A
    decided case is a no-op inside `route`, so the answer comes from
    comparing the chain before and after rather than from having called it.
    """
    try:
        repo = get_approvals()
    except Exception as exc:
        logger.exception(
            "Approval routing unreachable re-routing %s", ctx.case_id
        )
        get_audit().emit(ctx, "approval_reroute_failed", {"error": str(exc)})
        return "failed"
    if repo is None:
        return False

    def shape():
        return [
            # No ids: replace_pending re-creates pending rows with new ones.
            (
                s["position"],
                s["status"],
                s["assignee_kind"],
                s["assignee_value"],
            )
            for s in repo.chain(ctx.tenant_id, ctx.case_id)
        ]

    try:
        before = shape()
        approval_flow.route(
            ctx,
            artifacts,
            repo=repo,
            now=datetime.now(timezone.utc),
        )
        changed = shape() != before
    except Exception as exc:
        # The correction is already stored; a failed re-route must not undo it,
        # but it must not be silent either.
        logger.exception("Re-routing %s after an edit failed", ctx.case_id)
        get_audit().emit(ctx, "approval_reroute_failed", {"error": str(exc)})
        return "failed"
    if changed:
        get_audit().emit(ctx, "approval_rerouted", {"fields": fields})
    return changed


def _reproject(ctx, artifacts: dict) -> None:
    """Mirror a corrected artifact back into the typed columns.

    The artifacts are the record and the columns are the queryable view of
    them, written by `project_case` when the worker finishes a case. A
    reviewer's correction changed the record and left the view behind: the
    sheet, the review queue and every filter read columns, so a PO corrected in
    the drawer still showed the old one in the row beside it -- which reads as
    a save that did not work.

    Same call the worker makes, wrapped the same way: the correction is already
    stored, and a projection that fails must not undo it.
    """
    projector = getattr(get_cases(), "project_case", None)
    if projector is None:  # filesystem stores have no columns to project into
        return
    try:
        projector(ctx, artifacts)
    except Exception as exc:
        logger.warning(
            "Case %s corrected but not re-projected into the relational "
            "tables: %s",
            ctx.case_id,
            exc,
        )


def _require_unlocked(principal: Principal, case_id: str) -> None:
    """Refuse a write to a record someone else is editing.

    This is the one place the sheet work was allowed to change existing
    behaviour, and it is deliberately the smallest possible change: two
    callers, one lookup, no lock taken. Not holding a lock stays legal --
    holding one only excludes other people -- so every existing client keeps
    working and only learns about locks when it collides with one.
    """
    locks = get_locks()
    if locks is None:
        return
    held = locks.get(principal.tenant_id, case_id)
    if held and held["holder"] != principal.email:
        raise HTTPException(status_code=423, detail=held)


@app.post("/api/invoices/{case_id}/lock")
def lock_record(
    case_id: str,
    principal: Annotated[Principal, Depends(require_role("clerk"))],
) -> dict:
    """Claim the invoice for editing, or renew a claim already held.

    Renewal is this same call, so an open editor keeps its lock by saying "I am
    still here" on a timer rather than through an endpoint of its own.
    """
    require_invoice_case(principal.tenant_id, case_id)
    artifacts = get_cases().get_artifacts(principal.context(case_id))
    if not artifacts:
        raise HTTPException(status_code=404, detail=f"Unknown case {case_id}")

    locks = get_locks()
    if locks is None:
        # Honest rather than encouraging: the caller learns it has no lock,
        # instead of being told it holds one that excludes nobody.
        return {"lock": None, "mine": False, "locking": "unavailable"}

    mine, lock = locks.acquire(principal.tenant_id, case_id, principal.email)
    if not mine:
        raise HTTPException(status_code=423, detail=lock)

    get_audit().emit(
        principal.context(case_id),
        "record_locked",
        {"until": lock["expires_at"]},
    )
    return {"lock": lock, "mine": True, "locking": "enabled"}


@app.delete("/api/invoices/{case_id}/lock")
def unlock_record(
    case_id: str,
    principal: Annotated[Principal, Depends(require_role("clerk"))],
    force: bool = False,
) -> dict:
    """Give up the lock, or -- as an admin -- break someone else's.

    A lock nobody can break outlives whoever left it; a lock anybody can break
    is not a lock. So: the holder releases their own, an admin breaks another's
    and the trail records that they did.
    """
    locks = get_locks()
    if locks is None:
        return {"released": None}

    if force and not principal.can("admin"):
        raise HTTPException(
            status_code=403, detail="Breaking someone else's lock needs admin."
        )

    try:
        broken = locks.release(
            principal.tenant_id, case_id, principal.email, force=force
        )
    except LockHeld as held:
        raise HTTPException(status_code=423, detail=held.lock) from held

    if broken:
        get_audit().emit(
            principal.context(case_id),
            "record_lock_broken"
            if broken["holder"] != principal.email
            else "record_unlocked",
            {},
            before={"holder": broken["holder"]},
        )
    return {"released": broken}


def _propose_rule_if_warranted(
    principal: Principal,
    case_id: str,
    category: str,
    agreement: str | None,
    state: dict,
) -> None:
    """Note that this case may be worth a rule. Writes a row, nothing more.

    Two conditions, and both matter. The reviewer has to have *overruled* the
    pipeline -- agreeing with it teaches nothing. And they have to have said
    the reason was policy, because that is the one thing no classifier can
    infer: "we read the invoice wrong" and "the rulebook is missing a rule"
    look identical from the outside and mean opposite things. Teaching a rule
    from the first is how a control becomes a hole.

    No LLM here. Drafting the rule costs several Vertex calls and Vertex has
    been seen hanging for twenty minutes; a reviewer pressing Reject must not
    wait for that, or find their rejection failed because a model did.
    """
    if category != "policy_exception" or agreement in (None, "agree"):
        return
    rules = get_rules()
    if not hasattr(rules, "propose"):  # the file-backed store has no drafts
        return
    try:
        proposal = rules.propose(
            principal.tenant_id,
            case_id=case_id,
            author=principal.email,
            note=state.get("reason") or "",
            reason_category=category,
        )
    except Exception as exc:
        # A decision is not lost because a suggestion could not be recorded.
        logger.warning("Could not propose a rule for %s: %s", case_id, exc)
        return
    get_audit().emit(
        principal.context(case_id),
        "rule_proposed",
        {"rule": proposal["rule_id"], "reason": state.get("reason")},
    )


def _final_approval_lock(tenant_id: str, artifacts: dict, action: str):
    """`db.po_lock` for an approval of an invoice quoting a PO, else nothing."""
    invoice = (artifacts.get("extraction") or {}).get("invoice") or {}
    po_number = invoice.get("po_number")
    if action != "approve" or not po_matching.normalize_po_number(po_number):
        return nullcontext()
    try:
        orders = get_purchase_orders()
    except Exception:
        logger.exception("Purchase orders could not be reached at approval")
        raise HTTPException(
            status_code=503, detail=approval_flow.BOOK_DOWN
        ) from None
    if orders is None:
        return nullcontext()  # no DB book: nothing cumulative to protect
    from ..db.po_lock import po_lock  # noqa: PLC0415

    return po_lock(tenant_id, po_number, orders.url)


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
    category = request.reason_category or "other"
    if category not in REASON_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown reason: {category}. "
            f"One of: {', '.join(REASON_CATEGORIES)}.",
        )
    if request.override_reason and not principal.can("admin"):
        raise HTTPException(
            status_code=403,
            detail="Only an admin can override an approval rule.",
        )

    require_invoice_case(principal.tenant_id, case_id)
    _require_unlocked(principal, case_id)
    ctx = principal.context(case_id)
    artifacts = get_cases().get_artifacts(ctx)
    if not artifacts:
        raise HTTPException(status_code=404, detail=f"Unknown case {case_id}")

    # From the artifacts already in hand rather than a second read.
    previous = artifacts.get(WORKFLOW_ARTIFACT) or {}
    # Held from the re-check to the projection that bills this invoice, so
    # two final approvals on one PO cannot both pass against the same units.
    with _final_approval_lock(principal.tenant_id, artifacts, request.action):
        try:
            repo = get_approvals()
        except Exception:
            logger.exception("Approval routing could not be reached")
            raise HTTPException(
                status_code=503, detail="Approval routing is unavailable."
            ) from None
        if repo is not None:
            if previous.get("decision"):
                raise HTTPException(
                    status_code=409, detail="This invoice was already decided."
                )
            try:
                outcome = approval_flow.decide(
                    ctx,
                    principal,
                    artifacts,
                    repo=repo,
                    documents=get_documents(),
                    corrections=get_corrections(),
                    approve=request.action == "approve",
                    note=request.reason,
                    override_reason=request.override_reason,
                    now=datetime.now(timezone.utc),
                )
            except (
                approval_flow.AlreadyDecided,
                approval_flow.OverBilled,
            ) as exc:
                raise HTTPException(
                    status_code=409, detail=exc.detail
                ) from None
            except approval_flow.BookUnavailable as exc:
                raise HTTPException(
                    status_code=503, detail=exc.detail
                ) from None
            except approval_flow.Refused as exc:
                raise HTTPException(
                    status_code=403, detail=exc.detail
                ) from None
            step = outcome.step
            get_audit().emit(
                ctx,
                "approval_overridden"
                if outcome.overridden
                else "approval_step_decided",
                {
                    "step": step["position"],
                    "assignee": f"{step['assignee_kind']}:{step['assignee_value']}",
                    "action": request.action,
                    "why": outcome.why,
                    "acting_for": step.get("acting_for"),
                    **(
                        {"override_reason": request.override_reason}
                        if outcome.overridden
                        else {}
                    ),
                },
            )
            if not outcome.completed:
                return {
                    "case_id": case_id,
                    "action": request.action,
                    "completed": False,
                    "step": step["position"],
                    "next": repo.current(principal.tenant_id, case_id),
                }
            get_audit().emit(
                ctx, "approval_chain_completed", {"action": request.action}
            )
            if outcome.three_way is not None:
                # An "unchecked" hold, matched live by the approval: stored
                # before the workflow decision is written, under the PO lock,
                # so billed counts this invoice's lines once it is approved.
                artifacts = {
                    **artifacts,
                    "phase2": _store_three_way(ctx, outcome.three_way),
                }
        state = _record_workflow(
            ctx,
            decision=request.action,
            decided_by=principal.email,
            decided_at=datetime.now(timezone.utc).isoformat(),
            reason=request.reason,
            reason_category=category,
        )
        # The columns as well as the artifact. "Show me every invoice where a
        # person overruled the pipeline" is the question this whole feature is
        # built on, and answering it by scanning a JSON path over every row is the
        # wrong answer to it.
        _reproject(ctx, {**artifacts, WORKFLOW_ARTIFACT: state})
    agreement = case_projection.agreement(
        {**artifacts, WORKFLOW_ARTIFACT: state}
    )
    _propose_rule_if_warranted(principal, case_id, category, agreement, state)
    # Spelled out rather than built from the action: `f"invoice_{action}d"`
    # produced "invoice_rejectd", and an audit trail is immutable, so the typo
    # is now permanent in every row already written.
    get_audit().emit(
        ctx,
        "invoice_approved"
        if request.action == "approve"
        else "invoice_rejected",
        {
            "reason": request.reason,
            "reason_category": category,
            "agreement": agreement,
        },
        before={"decision": previous.get("decision")} if previous else None,
    )
    return {
        "case_id": case_id,
        "action": request.action,
        "reason": request.reason,
        "reason_category": category,
        "agreement": agreement,
        "completed": True,
    }


class BankChangeFromInvoice(BaseModel):
    reason: str = Field(default="", max_length=2000)


@app.post("/api/invoices/{case_id}/bank-change", status_code=201)
def propose_bank_change_from_invoice(
    case_id: str,
    request: BankChangeFromInvoice,
    principal: Annotated[Principal, Depends(require_role("clerk"))],
) -> dict:
    """Raise a vendor bank change from the details printed on this invoice.

    A reviewer holding an invoice on a bank signal has the new details in
    front of them; retyping them on another page is where transcription errors
    come from. Still only a proposal: a second person approves.
    """
    from . import vendor_routes  # noqa: PLC0415

    require_invoice_case(principal.tenant_id, case_id)
    repo = get_vendors()
    if repo is None:
        raise HTTPException(status_code=503, detail=vendor_routes.NEEDS_DB)
    ctx = principal.context(case_id)
    artifacts = get_cases().get_artifacts(ctx)
    if not artifacts:
        raise HTTPException(status_code=404, detail=f"Unknown case {case_id}")
    invoice = (artifacts.get("extraction") or {}).get("invoice") or {}
    vendor = repo.find_by_name(
        principal.tenant_id, invoice.get("vendor_name") or ""
    )
    if vendor is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{invoice.get('vendor_name') or 'This vendor'} is not in the "
                "vendor master. Add it first."
            ),
        )
    if not (invoice.get("bank_account_number") or invoice.get("bank_iban")):
        raise HTTPException(
            status_code=409,
            detail="This invoice prints no bank details to propose.",
        )
    if not request.reason.strip():
        raise HTTPException(
            status_code=400,
            detail="A bank change needs a reason the approver can check.",
        )
    try:
        proposal = vendor_routes.BankChangeProposal(
            bank_account_name=invoice.get("bank_account_name"),
            bank_account_number=invoice.get("bank_account_number"),
            bank_code=invoice.get("bank_code"),
            bank_iban=invoice.get("bank_iban"),
            reason=request.reason,
            source_case_id=case_id,
        )
    except ValidationError:
        # Extracted text is untrusted length-wise; refuse rather than truncate
        # a payee account.
        raise HTTPException(
            status_code=409,
            detail=(
                "An extracted bank value is too long to propose; enter it "
                "on the Vendors page."
            ),
        ) from None
    return vendor_routes.propose_change(principal, vendor["id"], proposal)


@app.get("/api/stats")
def stats(principal: Annotated[Principal, Depends(current_principal)]) -> dict:
    """The numbers a pilot is judged on."""
    listing = list_invoices(principal)
    items = listing["invoices"]
    corrections = get_corrections().list(principal.tenant_id)
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


def _store_three_way(ctx, entry: dict) -> dict:
    """Swap the stored step-2.3 entry for `entry`; the new phase2."""

    def swap(current: dict) -> dict:
        phase2 = current.get("phase2") or {}
        validations = [
            entry if v.get("step") == "2.3" else v
            for v in phase2.get("validations") or []
        ]
        return {"phase2": {**phase2, "validations": validations}}

    return get_cases().update_artifacts(ctx, swap)["phase2"]


def _record_workflow(ctx, **changes) -> dict:
    state = {**_workflow(ctx), **changes}
    get_cases().put_artifact(ctx, WORKFLOW_ARTIFACT, state)
    return state


# ---------------------------------------------------------------------------
# Corrections store
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

INVOICE_COLUMNS = [
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
# One row per line, the shape AP bill imports (Xero's among them) expect, so
# each line's account sits beside that line rather than in a joined cell.
LINE_COLUMNS = [
    "line_number",
    "line_description",
    "quantity",
    "unit_price",
    "line_amount_ex_tax",
    "line_tax",
    "gl_account",
]
CSV_COLUMNS = INVOICE_COLUMNS + LINE_COLUMNS


def _with_gl(ctx, invoice: dict, artifacts: dict) -> dict:
    """The invoice with each line's GL account from the projected lines.

    Export reads the extraction, which carries no coding. The projection is
    where gl_coding is applied, so it is read here rather than re-deciding
    which coding wins. Accounts are attached only when every extraction line
    pairs with its projected line; otherwise none are, and the connector
    falls back to its account map or default.
    """
    # ponytail: extraction line N <-> projected line N, checked by count and
    # description. Pairing breaks when the transformer renumbers or drops
    # lines, or when line items were replaced after coding (as /fields once
    # allowed); an unpaired export is audited rather than guessed.
    items = invoice.get("line_items") or []
    projected = case_projection.lines(artifacts)
    paired = len(items) == len(projected) and all(
        isinstance(item, dict)
        and case_projection._text(item.get("description"), 8000)
        == line["description"]
        for item, line in zip(items, projected, strict=False)
    )
    if not paired:
        get_audit().emit(
            ctx,
            "gl_export_unpaired",
            {"extraction_lines": len(items), "projected_lines": len(projected)},
        )
        return invoice
    return {
        **invoice,
        "line_items": [
            {**item, "gl_account": line["gl_account"]}
            for item, line in zip(items, projected, strict=True)
        ],
    }


def _csv_safe(value) -> str:
    return exports.formula_safe("" if value is None else str(value))


def _csv_rows(case_id: str, invoice: dict) -> list[list[str]]:
    """One row per line, each repeating the invoice; a line-less invoice is
    still one row, with the line cells empty."""
    head = {
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
    lines = [
        line if isinstance(line, dict) else {}
        for line in invoice.get("line_items") or []
    ]
    rows = [
        {
            **head,
            # Numbered as the Xero connector numbers them: position, from 1.
            "line_number": number,
            "line_description": line.get("description"),
            "quantity": line.get("quantity"),
            "unit_price": line.get("unit_price"),
            "line_amount_ex_tax": line.get("amount_ex_tax"),
            "line_tax": line.get("tax_amount"),
            "gl_account": line.get("gl_account"),
        }
        for number, line in enumerate(lines, start=1)
    ] or [head]
    return [[_csv_safe(row.get(c)) for c in CSV_COLUMNS] for row in rows]


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
    require_invoice_case(principal.tenant_id, case_id)
    ctx = principal.context(case_id)
    artifacts = get_cases().get_artifacts(ctx)
    if not artifacts:
        raise HTTPException(status_code=404, detail=f"Unknown case {case_id}")

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
    invoice = _with_gl(
        ctx, (artifacts.get("extraction") or {}).get("invoice") or {}, artifacts
    )

    if request.target == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(CSV_COLUMNS)
        writer.writerows(_csv_rows(case_id, invoice))
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
from .approval_routes import router as approval_router  # noqa: E402
from .config_routes import router as config_router  # noqa: E402
from .gl_routes import router as gl_router  # noqa: E402
from .ingest import router as ingest_router  # noqa: E402
from .kpi_routes import router as kpi_router  # noqa: E402
from .po_routes import router as po_router  # noqa: E402
from .rules import router as rules_router  # noqa: E402
from .sheet import router as sheet_router  # noqa: E402
from .signup import router as signup_router  # noqa: E402
from .supporting_routes import router as supporting_router  # noqa: E402
from .system import router as system_router  # noqa: E402
from .vendor_routes import router as vendor_router  # noqa: E402

app.include_router(ingest_router)
app.include_router(system_router)
app.include_router(config_router)
app.include_router(admin_router)
app.include_router(signup_router)
app.include_router(sheet_router)
app.include_router(rules_router)
app.include_router(vendor_router)
app.include_router(approval_router)
app.include_router(gl_router)
app.include_router(kpi_router)
app.include_router(po_router)
app.include_router(supporting_router)


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
<title>Ledgerly API</title>
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
  .brand {{ display: flex; align-items: center; gap: 12px; margin-bottom: 18px; }}
  .brand .name {{ font: 600 22px/1 system-ui, sans-serif; letter-spacing: -.02em;
                  color: #101828; }}
  .brand .by {{ font: 500 10px/1.6 system-ui, sans-serif; letter-spacing: .08em;
                text-transform: uppercase; color: #98a2b3; }}
</style></head>
<body><main>
  <div class="brand">
    <svg width="40" height="40" viewBox="0 0 120 120" fill="none" role="img"
         aria-label="Ledgerly">
      <path d="M24 16 V70 a14 14 0 0 0 14 14 H100" stroke="#2563EB"
            stroke-width="20" stroke-linecap="round" stroke-linejoin="round"/>
      <rect x="50" y="16" width="52" height="14" rx="7" fill="#101828"/>
      <rect x="50" y="41" width="52" height="14" rx="7" fill="#101828"/>
      <rect x="50" y="66" width="58" height="14" rx="7" fill="#2563EB"/>
    </svg>
    <span><span class="name">Ledgerly</span><br><span class="by">by AIxAI</span></span>
  </div>
  <h1>Ledgerly API</h1>
  <p>This is the API. The application is served separately.</p>
  <p><a class="button" href="{app_url}">Open the application</a></p>
  <p>Not running? Start it with <code>cd frontend &amp;&amp; npm run dev</code>.
     API reference at <a href="/docs">/docs</a>.</p>
</main></body></html>"""
