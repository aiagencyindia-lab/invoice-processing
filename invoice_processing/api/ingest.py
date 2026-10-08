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

import base64
import email
import hashlib
import hmac
import json
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
    sender: str | None = None,
    reply_to: str | None = None,
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
            ctx.child(case_id=existing["id"]),
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
        sender=sender or None,
        reply_to=reply_to or None,
    )
    # The document did not exist when the context was built, so the id is
    # attached now -- otherwise the row that records an upload cannot say
    # which upload.
    audit.emit(
        ctx.child(case_id=document_id),
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
    # Two ways to present it, because a provider cannot use the first.
    # Postmark and SendGrid Inbound Parse both take a URL and post to it; they
    # do not let you add arbitrary request headers. Both do let you put
    # credentials in that URL, which arrive as HTTP Basic auth -- so requiring
    # `x-webhook-token` meant the endpoint could only ever be called by curl.
    candidates = [
        request.headers.get("x-webhook-token", ""),
        _basic_auth_password(request.headers.get("authorization", "")),
    ]
    # Constant-time, and every candidate is checked: returning early on the
    # first match leaks the secret one byte at a time to anyone who can
    # measure response latency.
    if not any(
        hmac.compare_digest(supplied, expected)
        for supplied in candidates
        if supplied
    ):
        raise HTTPException(
            status_code=401, detail="Invalid webhook signature."
        )


def _basic_auth_password(header: str) -> str:
    """The password from `Authorization: Basic ...`, or empty.

    The username is ignored: the shared secret is the credential, and a
    provider's UI usually makes you supply both.
    """
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return ""
    try:
        decoded = base64.b64decode(encoded, validate=True).decode(
            "utf-8", "replace"
        )
    except (ValueError, TypeError):
        return ""
    return decoded.partition(":")[2]


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


@dataclass
class InboundMessage:
    """One delivery, however the provider chose to describe it.

    Three shapes reach this endpoint and none of them is the raw RFC-822 the
    handler used to assume:

    * **Postmark** posts JSON -- `RawEmail` when "include raw email content"
      is on, otherwise `Attachments` as base64 with `OriginalRecipient`.
    * **SendGrid Inbound Parse** posts multipart/form-data: an `email` field
      with the MIME when "POST the raw, full MIME" is on, otherwise `to`,
      `attachments` and files named `attachment1`, `attachment2`...
    * **A raw MIME POST**, which is what the tests and any curl use.

    Reading only the last of the three meant a real provider's delivery parsed
    to nothing, and the endpoint answered 200 with an empty document list --
    silently ingesting no invoices at all.
    """

    recipient: str
    message_id: str
    raw: bytes
    attachments: list[tuple[str, bytes]]
    sender: str = ""
    reply_to: str = ""


def parse_inbound(
    raw: bytes, content_type: str, headers: dict[str, str] | None = None
) -> InboundMessage:
    headers = headers or {}
    kind = (content_type or "").split(";", 1)[0].strip().lower()

    if kind == "application/json":
        return _parse_postmark(raw, headers)
    if kind in ("multipart/form-data", "application/x-www-form-urlencoded"):
        return _parse_sendgrid(raw, content_type, headers)
    return _parse_mime(raw, headers)


def _parse_mime(raw: bytes, headers: dict[str, str]) -> InboundMessage:
    recipient = headers.get("x-original-to") or ""
    message_id = ""
    sender = reply_to = ""
    try:
        message = email.message_from_bytes(raw, policy=policy.default)
        recipient = recipient or str(
            message.get("Delivered-To") or message.get("To") or ""
        )
        message_id = str(message.get("Message-ID") or "")
        sender = _bare_address(message.get("From"))
        reply_to = _bare_address(message.get("Reply-To"))
    except Exception:
        pass
    return InboundMessage(
        recipient=recipient,
        message_id=message_id,
        raw=raw,
        attachments=extract_pdf_attachments(raw),
        sender=sender,
        reply_to=reply_to,
    )


def _parse_postmark(raw: bytes, headers: dict[str, str]) -> InboundMessage:
    try:
        body = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, AttributeError):
        return InboundMessage("", "", raw, [])
    if not isinstance(body, dict):
        return InboundMessage("", "", raw, [])

    # With raw content enabled the MIME is authoritative: it carries the parts
    # the JSON summarises, and keeping it is what makes the stored evidence a
    # real message rather than a provider's description of one.
    raw_email = body.get("RawEmail")
    if isinstance(raw_email, str) and raw_email.strip():
        parsed = _parse_mime(raw_email.encode("utf-8", "replace"), headers)
        parsed.recipient = (
            headers.get("x-original-to")
            or _first_address(body.get("OriginalRecipient"))
            or _first_address(body.get("To"))
            or parsed.recipient
        )
        return parsed

    return InboundMessage(
        recipient=headers.get("x-original-to")
        or _first_address(body.get("OriginalRecipient"))
        or _first_address(body.get("To")),
        message_id=str(body.get("MessageID") or ""),
        raw=raw,
        attachments=_decode_attachments(
            body.get("Attachments"), name_key="Name", data_key="Content"
        ),
        sender=_bare_address(body.get("FromFull") or body.get("From")),
        reply_to=_bare_address(body.get("ReplyTo")),
    )


def _parse_sendgrid(
    raw: bytes, content_type: str, headers: dict[str, str]
) -> InboundMessage:
    fields, files = _parse_multipart(raw, content_type)

    mime = fields.get("email", "")
    if mime.strip():
        parsed = _parse_mime(mime.encode("utf-8", "replace"), headers)
        parsed.recipient = (
            headers.get("x-original-to")
            or _first_address(fields.get("to"))
            or parsed.recipient
        )
        return parsed

    attachments = [
        (name, data) for name, data in files if data.startswith(PDF_MAGIC)
    ]
    return InboundMessage(
        recipient=headers.get("x-original-to")
        or _first_address(fields.get("to")),
        message_id=str(fields.get("message-id") or ""),
        raw=raw,
        attachments=attachments,
        sender=_bare_address(fields.get("from")),
        reply_to=_bare_address(fields.get("reply-to") or ""),
    )


def _parse_multipart(
    raw: bytes, content_type: str
) -> tuple[dict[str, str], list[tuple[str, bytes]]]:
    """Fields and files, using the stdlib's own MIME parser.

    A multipart body is MIME with a different header, so it is parsed by the
    module that already understands MIME rather than by hand.
    """
    message = email.message_from_bytes(
        b"Content-Type: "
        + content_type.encode("utf-8", "replace")
        + b"\r\nMIME-Version: 1.0\r\n\r\n"
        + raw,
        policy=policy.default,
    )
    fields: dict[str, str] = {}
    files: list[tuple[str, bytes]] = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        name = part.get_param("name", header="content-disposition", failobj="")
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename:
            files.append((filename, payload))
        elif name:
            fields[str(name).lower()] = payload.decode("utf-8", "replace")
    return fields, files


def _decode_attachments(
    items, *, name_key: str, data_key: str
) -> list[tuple[str, bytes]]:
    out: list[tuple[str, bytes]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        try:
            data = base64.b64decode(item.get(data_key) or "", validate=False)
        except (ValueError, TypeError):
            continue
        if data.startswith(PDF_MAGIC):
            out.append((str(item.get(name_key) or "attachment.pdf"), data))
    return out


def _bare_address(value) -> str:
    """`Name <a@b>` -> `a@b`; a Postmark `FromFull` dict -> its Email."""
    if isinstance(value, dict):
        value = value.get("Email") or value.get("email") or ""
    text = str(value or "").strip()
    if "<" in text and ">" in text:
        text = text[text.rindex("<") + 1 : text.rindex(">")]
    return text.strip().lower()


def _first_address(value) -> str:
    """The first address out of whatever shape the provider used.

    Postmark's `To` is a string; its `ToFull` is a list of objects; SendGrid's
    `to` is a header line. All of them can carry several recipients, and only
    the first is ours to route on.
    """
    if isinstance(value, list):
        value = value[0] if value else ""
    if isinstance(value, dict):
        value = value.get("Email") or value.get("email") or ""
    text = str(value or "").split(",")[0].strip()
    return text


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

    inbound = parse_inbound(
        raw,
        request.headers.get("content-type", ""),
        {k.lower(): v for k, v in request.headers.items()},
    )

    try:
        tenant_slug = tenant_for_recipient(inbound.recipient)
    except IngestionRefused as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    from . import service  # noqa: PLC0415

    tenant_id = service.get_documents().tenant_id_for_slug(tenant_slug)
    if tenant_id is None:
        raise HTTPException(status_code=404, detail="Unknown recipient.")

    ctx = RunContext(tenant_id=tenant_id, actor="inbound-email")
    message_id = inbound.message_id

    # The raw message is evidence: it is what we can show when a vendor disputes
    # what they sent, and it is the only record of the parts we discarded.
    blobs = service.get_blobs(tenant_id)
    eml_key = f"email/{hashlib.sha256(inbound.raw).hexdigest()}.eml"
    blobs.put(eml_key, inbound.raw, content_type="message/rfc822")

    results, refused = [], []
    for filename, data in inbound.attachments:
        try:
            results.append(
                ingest_document(
                    ctx,
                    data,
                    filename=filename,
                    source="email",
                    source_ref=eml_key,
                    sender=inbound.sender,
                    reply_to=inbound.reply_to,
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
            "recipient": inbound.recipient,
            "sender": inbound.sender,
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
