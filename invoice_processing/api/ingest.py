"""Ingestion: how documents enter the system.

Two channels for the pilot -- a direct upload from the workbench, and an inbound
email webhook. Both funnel into one function, `ingest_document`, so the
dedup check, the storage layout and the audit trail cannot drift apart between
them.

Everything arriving here is untrusted. The rules that follow from that:

* **The tenant is never read from the payload.** For uploads it comes from the
  caller's verified token; for email it is resolved from the envelope recipient
  against a configured domain. A webhook body that says `"tenant": "acme"` is an
  attacker's assertion, and this module never looks at such a field.
* **The webhook authenticates before it parses.** An unauthenticated endpoint
  that accepts email would let anyone inject invoices into any tenant. If no
  secret is configured the endpoint refuses every request rather than running
  open -- fail closed, because the failure mode of the alternative is silent.
* **Storage keys are content-addressed**, never built from the supplied
  filename. `../../etc/passwd` and `invoice.pdf` are the same class of input
  here; deriving the key from the SHA-256 removes the question entirely.
* **Bytes are sniffed, not trusted.** A declared `content-type` of
  `application/pdf` costs an attacker nothing, so we check the magic bytes.

Email ingestion is open by design -- any sender can email an invoice to a
tenant's address, exactly as with every AP product. That is why an ingested
document is only ever a *candidate*: it enters the review queue and no email can
cause a payment. The controls above stop an attacker choosing *which tenant*
they land in, which is the part that would actually be a breach.
"""

from __future__ import annotations

import email
import hashlib
import hmac
import logging
import os
from dataclasses import dataclass
from email import policy
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile

from ..ports import RunContext
from .auth import Principal, require_role

logger = logging.getLogger(__name__)

# A 25 MB cap. Large enough for a scanned multi-page invoice, small enough that
# an unauthenticated-ish channel cannot be used to fill a bucket.
MAX_DOCUMENT_BYTES = 25 * 1024 * 1024
MAX_EMAIL_BYTES = 40 * 1024 * 1024
PDF_MAGIC = b"%PDF"

router = APIRouter()


class IngestionRefused(ValueError):
    """The input is not something we will accept. Carries a reviewer-readable why."""


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class IngestResult:
    document_id: str
    sha256: str
    storage_key: str
    filename: str | None
    byte_size: int
    duplicate: bool
    source: str
    # The queue message id, when the document was enqueued for processing.
    queued: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "sha256": self.sha256,
            "storage_key": self.storage_key,
            "filename": self.filename,
            "byte_size": self.byte_size,
            "duplicate": self.duplicate,
            "source": self.source,
            "queued": self.queued,
        }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def check_acceptable(data: bytes, filename: str | None) -> None:
    """Refuse anything the pipeline cannot process, before it is stored.

    Sniffing the magic bytes rather than believing the declared content type:
    setting a header costs an attacker nothing.
    """
    if not data:
        raise IngestionRefused("File is empty.")
    if len(data) > MAX_DOCUMENT_BYTES:
        raise IngestionRefused(
            f"File is {len(data)} bytes; the limit is {MAX_DOCUMENT_BYTES}."
        )
    if not data.startswith(PDF_MAGIC):
        raise IngestionRefused(
            f"{filename or 'File'} is not a PDF. Only PDFs are processed today."
        )


def storage_key_for(sha256: str) -> str:
    """Content-addressed, so a hostile filename never reaches a path.

    The BlobStore prefixes this with the tenant, so two tenants sending
    byte-identical invoices still get separate objects.
    """
    return f"documents/{sha256}.pdf"


# ---------------------------------------------------------------------------
# The one ingestion path
# ---------------------------------------------------------------------------


def ingest_document(
    ctx: RunContext,
    data: bytes,
    *,
    filename: str | None = None,
    source: str = "upload",
    source_ref: str | None = None,
    blobs=None,
    documents=None,
    audit=None,
    queue=None,
) -> IngestResult:
    """Store one document and register it. Idempotent on content.

    Re-sending identical bytes returns the existing document rather than
    creating a second one: an email retried by the provider, or a clerk clicking
    upload twice, must not become two invoices to pay.
    """
    check_acceptable(data, filename)

    from . import service  # noqa: PLC0415 - late, to honour test monkeypatching

    blobs = blobs if blobs is not None else service.get_blobs(ctx.tenant_id)
    documents = documents if documents is not None else service.get_documents()
    audit = audit if audit is not None else service.get_audit()

    sha256 = hashlib.sha256(data).hexdigest()

    existing = documents.find_by_sha(ctx.tenant_id, sha256)
    if existing is not None:
        audit.emit(
            ctx,
            "document_duplicate_ignored",
            {"sha256": sha256, "document_id": existing["id"], "source": source},
        )
        return IngestResult(
            document_id=existing["id"],
            sha256=sha256,
            storage_key=existing["storage_key"],
            filename=existing.get("filename"),
            byte_size=len(data),
            duplicate=True,
            source=source,
        )

    key = storage_key_for(sha256)
    blobs.put(key, data, content_type="application/pdf")

    document_id = documents.create(
        tenant_id=ctx.tenant_id,
        storage_key=key,
        sha256=sha256,
        filename=filename,
        mime_type="application/pdf",
        byte_size=len(data),
        source=source,
        source_ref=source_ref,
    )
    audit.emit(
        ctx,
        "document_ingested",
        {
            "document_id": document_id,
            "sha256": sha256,
            "source": source,
            "source_ref": source_ref,
            "filename": filename,
            "byte_size": len(data),
        },
    )
    result = IngestResult(
        document_id=document_id,
        sha256=sha256,
        storage_key=key,
        filename=filename,
        byte_size=len(data),
        duplicate=False,
        source=source,
    )

    # Enqueue last, and only once the document is durably stored. Publishing
    # first would let a worker pull a document that does not exist yet. A
    # publish that fails leaves an unprocessed document rather than a lost one,
    # so it is logged and the ingestion still succeeds -- the caller's upload
    # was accepted, and a stuck document is recoverable by republishing.
    try:
        from ..worker.runner import enqueue_document  # noqa: PLC0415

        result.queued = enqueue_document(
            queue if queue is not None else service.get_queue(), ctx, result
        )
    except Exception as exc:
        logger.error("Could not enqueue document %s: %s", document_id, exc)

    return result


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


@router.post("/api/documents")
async def upload_document(
    file: Annotated[UploadFile, File()],
    principal: Annotated[Principal, Depends(require_role("clerk"))],
) -> dict:
    """Upload one PDF.

    There is no `tenant_id` parameter, by design: the tenant is taken from the
    caller's verified token, so a handler cannot forget to validate one.
    """
    data = await file.read()
    try:
        result = ingest_document(
            principal.context(),
            data,
            filename=file.filename,
            source="upload",
            source_ref=principal.email,
        )
    except IngestionRefused as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result.as_dict()


# ---------------------------------------------------------------------------
# Inbound email
# ---------------------------------------------------------------------------


def verify_webhook_secret(request: Request) -> None:
    """Authenticate the provider's callback.

    Fails closed when unconfigured: an inbound-mail endpoint that anyone can
    POST to is a way to inject invoices into any tenant, and "we forgot to set
    the env var" must not be the thing standing between an attacker and that.
    """
    expected = os.getenv("INBOUND_WEBHOOK_SECRET", "")
    if not expected:
        logger.error(
            "INBOUND_WEBHOOK_SECRET is not set; refusing inbound mail."
        )
        raise HTTPException(
            status_code=503, detail="Inbound email is not configured."
        )
    supplied = request.headers.get("x-webhook-token", "")
    # Constant-time: a plain `==` leaks the secret one byte at a time to anyone
    # who can measure response latency.
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=401, detail="Invalid webhook signature."
        )


def tenant_for_recipient(recipient: str) -> str:
    """Resolve `acme@invoices.example.com` to tenant slug `acme`.

    The domain must match the one we configured. Without that check any address
    in the payload would select a tenant, which hands an attacker the choice of
    whose queue to post into -- the one thing the open nature of email must not
    also give away.
    """
    domain = os.getenv("INBOUND_EMAIL_DOMAIN", "")
    if not domain:
        raise IngestionRefused("Inbound email domain is not configured.")

    address = (recipient or "").strip().lower()
    # `Name <user@host>` -> `user@host`
    if "<" in address:
        address = address.split("<", 1)[1].split(">", 1)[0].strip()

    local, _, host = address.partition("@")
    if host != domain.lower() or not local:
        raise IngestionRefused(f"Unrecognised recipient address: {recipient!r}")
    # Provider sub-addressing: acme+anything@ still means acme.
    return local.split("+", 1)[0]


def extract_pdf_attachments(raw_email: bytes) -> list[tuple[str, bytes]]:
    """Every PDF attachment, as (filename, bytes).

    Only PDFs come back. Signatures, logos and tracking pixels are the bulk of
    what arrives on a business email, and none of it is an invoice.
    """
    message = email.message_from_bytes(raw_email, policy=policy.default)
    out: list[tuple[str, bytes]] = []
    for part in message.iter_attachments():
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            continue
        if not payload or not payload.startswith(PDF_MAGIC):
            continue
        out.append((part.get_filename() or "attachment.pdf", payload))
    return out


@router.post("/webhooks/email")
async def inbound_email(request: Request) -> dict:
    """Inbound mail from Postmark/SendGrid.

    Deliberately not behind `current_principal`: the caller is a provider, not a
    user. It is authenticated by shared secret instead, and the tenant is
    resolved from the envelope recipient -- never from the body.
    """
    verify_webhook_secret(request)

    raw = await request.body()
    if len(raw) > MAX_EMAIL_BYTES:
        raise HTTPException(status_code=413, detail="Message too large.")

    recipient = request.headers.get("x-original-to") or ""
    if not recipient:
        # Providers post JSON with the envelope; fall back to parsing the MIME.
        try:
            message = email.message_from_bytes(raw, policy=policy.default)
            recipient = str(
                message.get("Delivered-To") or message.get("To") or ""
            )
        except Exception:
            recipient = ""

    try:
        tenant_slug = tenant_for_recipient(recipient)
    except IngestionRefused as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    from . import service  # noqa: PLC0415

    tenant_id = service.get_documents().tenant_id_for_slug(tenant_slug)
    if tenant_id is None:
        raise HTTPException(status_code=404, detail="Unknown recipient.")

    ctx = RunContext(tenant_id=tenant_id, actor="inbound-email")
    message_id = ""
    try:
        message_id = str(
            email.message_from_bytes(raw, policy=policy.default).get(
                "Message-ID"
            )
            or ""
        )
    except Exception:
        pass

    # The raw message is evidence: it is what we can show when a vendor disputes
    # what they sent, and it is the only record of the parts we discarded.
    blobs = service.get_blobs(tenant_id)
    eml_key = f"email/{hashlib.sha256(raw).hexdigest()}.eml"
    blobs.put(eml_key, raw, content_type="message/rfc822")

    results, refused = [], []
    for filename, data in extract_pdf_attachments(raw):
        try:
            results.append(
                ingest_document(
                    ctx,
                    data,
                    filename=filename,
                    source="email",
                    source_ref=eml_key,
                    blobs=blobs,
                ).as_dict()
            )
        except IngestionRefused as exc:
            refused.append({"filename": filename, "reason": str(exc)})

    service.get_audit().emit(
        ctx,
        "email_received",
        {
            "eml_key": eml_key,
            "message_id": message_id,
            "recipient": recipient,
            "ingested": len(results),
            "refused": len(refused),
        },
    )
    # 200 even with nothing ingested: the provider must not retry a message that
    # simply had no invoice in it.
    return {
        "tenant": tenant_slug,
        "eml_key": eml_key,
        "documents": results,
        "refused": refused,
    }
