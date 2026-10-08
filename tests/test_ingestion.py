"""Ingestion: upload and inbound email.

Email is the channel an attacker can reach without credentials, so most of this
file is about what ingestion *refuses*. The properties being defended:

* the tenant is resolved from the envelope recipient, never from the payload;
* the webhook fails closed when its secret is unconfigured;
* identical bytes ingest once, however many times they arrive;
* a hostile filename never reaches a storage path;
* a declared content type is not evidence of anything.

Nothing here touches GCS or Postgres: the blob store and document registry are
in-memory fakes satisfying the same contracts.
"""

import base64
import email.message
import hashlib
import json
from email.message import EmailMessage

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from invoice_processing.api import auth, ingest, service
from invoice_processing.api.ingest import (
    IngestionRefused,
    check_acceptable,
    extract_pdf_attachments,
    ingest_document,
    storage_key_for,
    tenant_for_recipient,
)
from invoice_processing.api.service import app
from invoice_processing.ports import RunContext

PDF = b"%PDF-1.4\nfake invoice bytes\n%%EOF"
OTHER_PDF = b"%PDF-1.4\na different invoice\n%%EOF"


class FakeBlobs:
    def __init__(self):
        self.objects = {}

    def put(self, key, data, content_type="application/octet-stream"):
        self.objects[key] = (data, content_type)
        return f"fake://{key}"

    def get(self, key):
        return self.objects[key][0]

    def exists(self, key):
        return key in self.objects

    def signed_url(self, key, ttl_seconds=3600):
        return f"https://signed/{key}"


class FakeDocuments:
    def __init__(self, tenants=None):
        self.rows = {}
        self.tenants = tenants or {
            "acme": "tenant-acme",
            "globex": "tenant-globex",
        }
        self._n = 0

    def find_by_sha(self, tenant_id, sha256):
        for row in self.rows.values():
            if row["tenant_id"] == tenant_id and row["sha256"] == sha256:
                return row
        return None

    def create(self, tenant_id, **fields):
        self._n += 1
        document_id = f"doc-{self._n}"
        self.rows[document_id] = {
            "id": document_id,
            "tenant_id": tenant_id,
            **fields,
        }
        return document_id

    def tenant_id_for_slug(self, slug):
        return self.tenants.get(slug)


class FakeAudit:
    def __init__(self):
        self.events = []

    def emit(self, ctx, event_type, payload):
        self.events.append((ctx.tenant_id, event_type, payload))


@pytest.fixture
def wired(monkeypatch, tmp_path):
    blobs, documents, audit = FakeBlobs(), FakeDocuments(), FakeAudit()
    monkeypatch.setattr(service, "get_blobs", lambda tenant_id: blobs)
    monkeypatch.setattr(service, "get_documents", lambda: documents)
    monkeypatch.setattr(service, "_audit", audit)
    # Also the queue: without this, every ingestion test publishes a real
    # message into the developer's own queue directory, which then sits there
    # waiting for a worker to process a fake document.
    from invoice_processing.worker.queue import LocalQueue  # noqa: PLC0415

    monkeypatch.setattr(
        service, "get_queue", lambda: LocalQueue(tmp_path / "default-queue")
    )
    monkeypatch.setenv("INBOUND_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setenv("INBOUND_EMAIL_DOMAIN", "invoices.example.com")
    return blobs, documents, audit


def ctx(tenant="tenant-acme"):
    return RunContext(tenant_id=tenant, actor="clerk@acme.test")


# ---------------------------------------------------------------------------
# What we accept
# ---------------------------------------------------------------------------


class TestAcceptance:
    def test_a_pdf_is_accepted(self):
        check_acceptable(PDF, "invoice.pdf")

    def test_empty_is_refused(self):
        with pytest.raises(IngestionRefused, match="empty"):
            check_acceptable(b"", "invoice.pdf")

    def test_oversize_is_refused(self):
        with pytest.raises(IngestionRefused, match="limit"):
            check_acceptable(
                b"%PDF" + b"x" * ingest.MAX_DOCUMENT_BYTES, "big.pdf"
            )

    def test_a_pdf_filename_does_not_make_it_a_pdf(self):
        """Declared type and extension cost an attacker nothing; bytes don't."""
        with pytest.raises(IngestionRefused, match="not a PDF"):
            check_acceptable(b"MZ\x90\x00 this is an executable", "invoice.pdf")

    def test_html_is_refused(self):
        with pytest.raises(IngestionRefused):
            check_acceptable(b"<html><body>hi</body></html>", "invoice.pdf")


class TestStorageKeys:
    def test_key_is_content_addressed(self):
        sha = hashlib.sha256(PDF).hexdigest()
        assert storage_key_for(sha) == f"documents/{sha}.pdf"

    @pytest.mark.parametrize(
        "filename",
        ["../../etc/passwd", "..\\..\\windows", "/absolute.pdf", "a/b/c.pdf"],
    )
    def test_a_hostile_filename_never_reaches_the_key(self, wired, filename):
        blobs, _, _ = wired
        ingest_document(ctx(), PDF, filename=filename)
        key = next(iter(blobs.objects))
        assert ".." not in key and key.startswith("documents/")


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------


class TestDeduplication:
    def test_identical_bytes_ingest_once(self, wired):
        """A provider retry must not become a second invoice to pay."""
        _, documents, _ = wired
        first = ingest_document(ctx(), PDF, filename="a.pdf")
        second = ingest_document(ctx(), PDF, filename="a-again.pdf")

        assert second.duplicate is True
        assert second.document_id == first.document_id
        assert len(documents.rows) == 1

    def test_different_bytes_are_separate_documents(self, wired):
        _, documents, _ = wired
        ingest_document(ctx(), PDF)
        ingest_document(ctx(), OTHER_PDF)
        assert len(documents.rows) == 2

    def test_dedup_does_not_cross_tenants(self, wired):
        """Two customers sending the same invoice is not a duplicate."""
        _, documents, _ = wired
        a = ingest_document(ctx("tenant-acme"), PDF)
        b = ingest_document(ctx("tenant-globex"), PDF)
        assert b.duplicate is False
        assert a.document_id != b.document_id
        assert len(documents.rows) == 2

    def test_a_duplicate_is_still_audited(self, wired):
        _, _, audit = wired
        ingest_document(ctx(), PDF)
        ingest_document(ctx(), PDF)
        assert [e[1] for e in audit.events] == [
            "document_ingested",
            "document_duplicate_ignored",
        ]


# ---------------------------------------------------------------------------
# Email: resolving the tenant
# ---------------------------------------------------------------------------


class TestTenantResolution:
    def test_local_part_selects_the_tenant(self, wired):
        assert tenant_for_recipient("acme@invoices.example.com") == "acme"

    def test_display_name_form_is_handled(self, wired):
        assert tenant_for_recipient("AP <acme@invoices.example.com>") == "acme"

    def test_sub_addressing_still_resolves(self, wired):
        assert tenant_for_recipient("acme+scans@invoices.example.com") == "acme"

    def test_case_is_ignored(self, wired):
        assert tenant_for_recipient("ACME@Invoices.Example.Com") == "acme"

    @pytest.mark.parametrize(
        "address",
        [
            "acme@evil.example.com",
            "acme@invoices.example.com.evil.com",
            "acme@",
            "@invoices.example.com",
            "",
            "not-an-address",
        ],
    )
    def test_a_foreign_domain_never_selects_a_tenant(self, wired, address):
        """Otherwise the sender picks whose queue their document lands in."""
        with pytest.raises(IngestionRefused):
            tenant_for_recipient(address)

    def test_unconfigured_domain_refuses_everything(self, monkeypatch):
        monkeypatch.delenv("INBOUND_EMAIL_DOMAIN", raising=False)
        with pytest.raises(IngestionRefused, match="not configured"):
            tenant_for_recipient("acme@invoices.example.com")


# ---------------------------------------------------------------------------
# Email: parsing
# ---------------------------------------------------------------------------


def build_email(attachments, to="acme@invoices.example.com"):
    message = email.message.EmailMessage()
    message["To"] = to
    message["From"] = "vendor@supplier.test"
    message["Subject"] = "Invoice"
    message["Message-ID"] = "<m-1@supplier.test>"
    message.set_content("Please find attached.")
    for filename, data, subtype in attachments:
        message.add_attachment(
            data, maintype="application", subtype=subtype, filename=filename
        )
    return message.as_bytes()


class TestAttachmentExtraction:
    def test_pdf_attachments_are_returned(self):
        raw = build_email([("invoice.pdf", PDF, "pdf")])
        assert [name for name, _ in extract_pdf_attachments(raw)] == [
            "invoice.pdf"
        ]

    def test_non_pdf_attachments_are_dropped(self):
        """Signatures, logos and tracking pixels are not invoices."""
        raw = build_email(
            [
                ("invoice.pdf", PDF, "pdf"),
                ("logo.png", b"\x89PNG\r\n", "octet-stream"),
            ]
        )
        assert len(extract_pdf_attachments(raw)) == 1

    def test_a_pdf_named_file_that_is_not_a_pdf_is_dropped(self):
        raw = build_email([("invoice.pdf", b"MZ\x90 executable", "pdf")])
        assert extract_pdf_attachments(raw) == []

    def test_multiple_pdfs_all_come_through(self):
        raw = build_email([("a.pdf", PDF, "pdf"), ("b.pdf", OTHER_PDF, "pdf")])
        assert len(extract_pdf_attachments(raw)) == 2

    def test_an_email_with_no_attachments_is_not_an_error(self):
        assert extract_pdf_attachments(build_email([])) == []


# ---------------------------------------------------------------------------
# Email: the endpoint
# ---------------------------------------------------------------------------


@pytest.fixture
def client(wired):
    return TestClient(app)


def post_email(
    client, raw, token="s3cret", recipient="acme@invoices.example.com"
):
    headers = {"Content-Type": "message/rfc822"}
    if token is not None:
        headers["X-Webhook-Token"] = token
    if recipient is not None:
        headers["X-Original-To"] = recipient
    return client.post("/webhooks/email", content=raw, headers=headers)


class TestInboundEmailAuth:
    def test_no_token_is_401(self, client):
        response = post_email(
            client, build_email([("a.pdf", PDF, "pdf")]), token=None
        )
        assert response.status_code == 401

    def test_wrong_token_is_401(self, client):
        response = post_email(client, build_email([]), token="guess")
        assert response.status_code == 401

    def test_unconfigured_secret_refuses_every_request(
        self, client, monkeypatch
    ):
        """Fail closed: 'we forgot the env var' must not mean 'open to all'."""
        monkeypatch.delenv("INBOUND_WEBHOOK_SECRET", raising=False)
        response = post_email(client, build_email([]), token="anything")
        assert response.status_code == 503

    def test_the_endpoint_needs_no_user_login(self, client):
        """The caller is a provider, not a user; it authenticates by secret."""
        assert post_email(client, build_email([])).status_code == 200


class TestInboundEmailIngestion:
    def test_sender_is_stored_on_the_document(self, client, wired):
        """The sender-domain BEC signal reads it from here; the raw .eml alone
        would mean parsing a blob on every pipeline run."""
        _, documents, _ = wired
        post_email(client, build_email([("a.pdf", PDF, "pdf")]))
        assert documents.rows["doc-1"]["sender"] == "vendor@supplier.test"
        assert documents.rows["doc-1"]["reply_to"] is None

    def test_an_attached_invoice_is_ingested(self, client, wired):
        _, documents, _ = wired
        body = post_email(client, build_email([("a.pdf", PDF, "pdf")])).json()
        assert len(body["documents"]) == 1
        assert documents.rows["doc-1"]["tenant_id"] == "tenant-acme"
        assert documents.rows["doc-1"]["source"] == "email"

    def test_the_raw_message_is_kept_as_evidence(self, client, wired):
        """It is what we show when a vendor disputes what they sent."""
        blobs, _, _ = wired
        body = post_email(client, build_email([("a.pdf", PDF, "pdf")])).json()
        assert body["eml_key"].startswith("email/")
        assert blobs.objects[body["eml_key"]][1] == "message/rfc822"

    def test_the_document_links_back_to_the_message(self, client, wired):
        _, documents, _ = wired
        body = post_email(client, build_email([("a.pdf", PDF, "pdf")])).json()
        assert documents.rows["doc-1"]["source_ref"] == body["eml_key"]

    def test_recipient_decides_the_tenant_not_the_body(self, client, wired):
        """The body is the sender's assertion; the envelope is the provider's."""
        _, documents, _ = wired
        raw = build_email(
            [("a.pdf", PDF, "pdf")], to="globex@invoices.example.com"
        )
        post_email(client, raw, recipient="acme@invoices.example.com")
        assert documents.rows["doc-1"]["tenant_id"] == "tenant-acme"

    def test_an_unknown_recipient_is_404(self, client):
        response = post_email(
            client, build_email([]), recipient="nobody@invoices.example.com"
        )
        assert response.status_code == 404

    def test_a_foreign_domain_is_400(self, client):
        response = post_email(
            client, build_email([]), recipient="acme@evil.test"
        )
        assert response.status_code == 400

    def test_no_attachments_still_returns_200(self, client):
        """A 4xx would make the provider retry a message with no invoice in it."""
        response = post_email(client, build_email([]))
        assert response.status_code == 200
        assert response.json()["documents"] == []

    def test_a_retried_delivery_does_not_duplicate(self, client, wired):
        _, documents, _ = wired
        raw = build_email([("a.pdf", PDF, "pdf")])
        post_email(client, raw)
        second = post_email(client, raw).json()
        assert second["documents"][0]["duplicate"] is True
        assert len(documents.rows) == 1

    def test_the_message_is_audited(self, client, wired):
        _, _, audit = wired
        post_email(client, build_email([("a.pdf", PDF, "pdf")]))
        assert "email_received" in [e[1] for e in audit.events]


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


class FakeVerifier:
    def __init__(self, claims):
        self.claims = claims

    def verify(self, token):
        if token not in self.claims:
            raise auth.AuthError("Invalid token")
        return self.claims[token]


@pytest.fixture
def upload_client(wired, monkeypatch):
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    monkeypatch.setattr(
        auth,
        "_verifier",
        FakeVerifier(
            {
                "clerk": {
                    "sub": "u1",
                    "email": "clerk@acme.test",
                    "email_verified": True,
                    "tenant_id": "tenant-acme",
                    "role": "clerk",
                },
                "viewer": {
                    "sub": "u2",
                    "email": "viewer@acme.test",
                    "email_verified": True,
                    "tenant_id": "tenant-acme",
                    "role": "viewer",
                },
            }
        ),
    )
    return TestClient(app)


class TestUpload:
    def test_a_clerk_can_upload(self, upload_client, wired):
        _, documents, _ = wired
        response = upload_client.post(
            "/api/documents",
            files={"file": ("invoice.pdf", PDF, "application/pdf")},
            headers={"Authorization": "Bearer clerk"},
        )
        assert response.status_code == 200
        assert documents.rows["doc-1"]["tenant_id"] == "tenant-acme"

    def test_upload_requires_authentication(self, upload_client):
        response = upload_client.post(
            "/api/documents",
            files={"file": ("invoice.pdf", PDF, "application/pdf")},
        )
        assert response.status_code == 401

    def test_a_viewer_cannot_upload(self, upload_client):
        response = upload_client.post(
            "/api/documents",
            files={"file": ("invoice.pdf", PDF, "application/pdf")},
            headers={"Authorization": "Bearer viewer"},
        )
        assert response.status_code == 403

    def test_a_non_pdf_is_400_not_500(self, upload_client):
        response = upload_client.post(
            "/api/documents",
            files={"file": ("invoice.pdf", b"MZ\x90 exe", "application/pdf")},
            headers={"Authorization": "Bearer clerk"},
        )
        assert response.status_code == 400
        assert "not a PDF" in response.json()["detail"]

    def test_the_endpoint_takes_no_tenant_parameter(self):
        """Nowhere to put one means nothing to forget to validate."""
        import inspect  # noqa: PLC0415

        signature = inspect.signature(ingest.upload_document)
        assert "tenant_id" not in signature.parameters


# ---------------------------------------------------------------------------
# Hand-off to the worker
# ---------------------------------------------------------------------------


class TestEnqueueOnIngest:
    """Ingestion returns as soon as the document is durable; work is async."""

    def test_an_ingested_document_is_enqueued(
        self, wired, tmp_path, monkeypatch
    ):
        from invoice_processing.worker.queue import LocalQueue  # noqa: PLC0415

        queue = LocalQueue(tmp_path / "q")
        monkeypatch.setattr(service, "get_queue", lambda: queue)

        result = ingest_document(ctx(), PDF, filename="a.pdf")
        [message] = queue.pull()
        assert message.payload["document_id"] == result.document_id
        assert message.payload["tenant_id"] == "tenant-acme"

    def test_a_duplicate_is_not_re_enqueued(self, wired, tmp_path, monkeypatch):
        """Re-processing an invoice we already have is wasted money."""
        from invoice_processing.worker.queue import LocalQueue  # noqa: PLC0415

        queue = LocalQueue(tmp_path / "q")
        monkeypatch.setattr(service, "get_queue", lambda: queue)

        ingest_document(ctx(), PDF)
        ingest_document(ctx(), PDF)
        assert queue.depth() == 1

    def test_a_queue_failure_does_not_fail_the_upload(
        self, wired, monkeypatch, caplog
    ):
        """The bytes are safe; a stuck document is recoverable, a lost one isn't."""

        class BrokenQueue:
            def publish(self, payload, **attributes):
                raise RuntimeError("pubsub unavailable")

        monkeypatch.setattr(service, "get_queue", BrokenQueue)
        result = ingest_document(ctx(), PDF, filename="a.pdf")
        assert result.document_id
        assert result.queued is None

    def test_the_document_is_stored_before_it_is_enqueued(
        self, wired, tmp_path, monkeypatch
    ):
        """Publishing first would let a worker pull a document that isn't there."""
        blobs, documents, _ = wired
        seen = {}

        class SpyQueue:
            def publish(self, payload, **attributes):
                seen["stored"] = payload["storage_key"] in blobs.objects
                seen["registered"] = (
                    documents.find_by_sha(
                        payload["tenant_id"], payload["sha256"]
                    )
                    is not None
                )
                return "m-1"

        monkeypatch.setattr(service, "get_queue", SpyQueue)
        ingest_document(ctx(), PDF)
        assert seen == {"stored": True, "registered": True}


class TestProviderPayloads:
    """The endpoint named Postmark and SendGrid and could parse neither.

    It read the body as raw RFC-822. Postmark posts JSON and SendGrid posts
    multipart/form-data, so a real delivery parsed to no attachments at all
    and the endpoint answered 200 with an empty document list -- accepting the
    mail and ingesting nothing.
    """

    def _pdf(self):
        return b"%PDF-1.4 invoice bytes"

    def test_postmark_sender_and_reply_to(self):
        body = json.dumps(
            {
                "OriginalRecipient": "acme@invoices.example.com",
                "MessageID": "pm-2",
                "FromFull": {"Email": "billing@vendor.test", "Name": "Billing"},
                "ReplyTo": "pay-me@lookalike.test",
                "Attachments": [],
            }
        ).encode()
        parsed = ingest.parse_inbound(body, "application/json")
        assert parsed.sender == "billing@vendor.test"
        assert parsed.reply_to == "pay-me@lookalike.test"

    def test_sendgrid_sender(self):
        boundary = "xYzBoundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="to"\r\n\r\n'
            "acme@invoices.example.com\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="from"\r\n\r\n'
            "Vendor Billing <billing@vendor.test>\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        parsed = ingest.parse_inbound(
            body, f"multipart/form-data; boundary={boundary}"
        )
        assert parsed.sender == "billing@vendor.test"

    def test_raw_mime_sender_and_reply_to(self):
        message = EmailMessage()
        message["From"] = "Vendor <supplier@vendor.test>"
        message["Reply-To"] = "pay-me@lookalike.test"
        message["To"] = "acme@invoices.example.com"
        message.set_content("x")
        parsed = ingest.parse_inbound(message.as_bytes(), "message/rfc822")
        assert parsed.sender == "supplier@vendor.test"
        assert parsed.reply_to == "pay-me@lookalike.test"

    def test_postmark_json_with_base64_attachments(self):
        body = json.dumps(
            {
                "OriginalRecipient": "acme@invoices.example.com",
                "MessageID": "pm-1",
                "Attachments": [
                    {
                        "Name": "invoice.pdf",
                        "Content": base64.b64encode(self._pdf()).decode(),
                        "ContentType": "application/pdf",
                    },
                    {
                        "Name": "logo.png",
                        "Content": base64.b64encode(b"\x89PNG").decode(),
                        "ContentType": "image/png",
                    },
                ],
            }
        ).encode()

        parsed = ingest.parse_inbound(body, "application/json")

        assert parsed.recipient == "acme@invoices.example.com"
        assert parsed.message_id == "pm-1"
        assert [name for name, _ in parsed.attachments] == ["invoice.pdf"]
        assert parsed.attachments[0][1] == self._pdf()

    def test_postmark_raw_email_is_preferred_when_present(self):
        raw = _email_with_pdf("acme@invoices.example.com", self._pdf())
        body = json.dumps(
            {
                "OriginalRecipient": "acme@invoices.example.com",
                "RawEmail": raw.decode("utf-8", "replace"),
            }
        ).encode()

        parsed = ingest.parse_inbound(body, "application/json")

        assert parsed.recipient == "acme@invoices.example.com"
        assert len(parsed.attachments) == 1
        # The stored evidence is the message, not the provider's description.
        assert parsed.raw.startswith(b"From:") or b"MIME" in parsed.raw

    def test_sendgrid_multipart_with_files(self):
        boundary = "xYzBoundary"
        body = (
            (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="to"\r\n\r\n'
                "acme@invoices.example.com\r\n"
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="attachment1"; '
                'filename="invoice.pdf"\r\n'
                "Content-Type: application/pdf\r\n\r\n"
            ).encode()
            + self._pdf()
            + f"\r\n--{boundary}--\r\n".encode()
        )

        parsed = ingest.parse_inbound(
            body, f"multipart/form-data; boundary={boundary}"
        )

        assert parsed.recipient == "acme@invoices.example.com"
        assert [name for name, _ in parsed.attachments] == ["invoice.pdf"]

    def test_sendgrid_multipart_with_full_mime(self):
        boundary = "xYzBoundary"
        raw = _email_with_pdf("acme@invoices.example.com", self._pdf())
        body = (
            (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="email"\r\n\r\n'
            ).encode()
            + raw
            + f"\r\n--{boundary}--\r\n".encode()
        )

        parsed = ingest.parse_inbound(
            body, f"multipart/form-data; boundary={boundary}"
        )

        assert len(parsed.attachments) == 1

    def test_a_raw_mime_post_still_works(self):
        """What curl and every existing test send."""
        raw = _email_with_pdf("acme@invoices.example.com", self._pdf())

        parsed = ingest.parse_inbound(raw, "message/rfc822")

        assert parsed.recipient.endswith("acme@invoices.example.com")
        assert len(parsed.attachments) == 1

    def test_several_recipients_route_on_the_first(self):
        body = json.dumps(
            {"To": "acme@invoices.example.com, someone@else.test"}
        ).encode()

        parsed = ingest.parse_inbound(body, "application/json")

        assert parsed.recipient == "acme@invoices.example.com"

    def test_malformed_json_is_not_a_crash(self):
        parsed = ingest.parse_inbound(b"{not json", "application/json")

        assert parsed.attachments == []
        assert parsed.recipient == ""


class TestWebhookAuthenticationAProviderCanActuallyUse:
    """Neither provider can add a request header. Both can put credentials in
    the URL they post to, which arrive as HTTP Basic auth."""

    def test_basic_auth_password_is_accepted(self, monkeypatch):
        monkeypatch.setenv("INBOUND_WEBHOOK_SECRET", "s3cret")
        token = base64.b64encode(b"postmark:s3cret").decode()

        request = _request({"authorization": f"Basic {token}"})
        ingest.verify_webhook_secret(request)  # must not raise

    def test_the_header_still_works(self, monkeypatch):
        monkeypatch.setenv("INBOUND_WEBHOOK_SECRET", "s3cret")

        ingest.verify_webhook_secret(_request({"x-webhook-token": "s3cret"}))

    def test_a_wrong_password_is_refused(self, monkeypatch):
        monkeypatch.setenv("INBOUND_WEBHOOK_SECRET", "s3cret")
        token = base64.b64encode(b"postmark:wrong").decode()

        with pytest.raises(HTTPException) as exc:
            ingest.verify_webhook_secret(
                _request({"authorization": f"Basic {token}"})
            )
        assert exc.value.status_code == 401

    def test_garbage_credentials_are_refused(self, monkeypatch):
        monkeypatch.setenv("INBOUND_WEBHOOK_SECRET", "s3cret")

        with pytest.raises(HTTPException):
            ingest.verify_webhook_secret(
                _request({"authorization": "Basic not-base64!"})
            )

    def test_still_fails_closed_when_unconfigured(self, monkeypatch):
        monkeypatch.delenv("INBOUND_WEBHOOK_SECRET", raising=False)
        token = base64.b64encode(b"postmark:anything").decode()

        with pytest.raises(HTTPException) as exc:
            ingest.verify_webhook_secret(
                _request({"authorization": f"Basic {token}"})
            )
        assert exc.value.status_code == 503


def _request(headers: dict):
    class _Request:
        def __init__(self, headers):
            self.headers = headers

    return _Request(headers)


def _email_with_pdf(recipient: str, pdf: bytes) -> bytes:
    message = EmailMessage()
    message["From"] = "supplier@vendor.test"
    message["To"] = recipient
    message["Subject"] = "Invoice"
    message["Message-ID"] = "<mime-1@vendor.test>"
    message.set_content("Please find attached.")
    message.add_attachment(
        pdf, maintype="application", subtype="pdf", filename="invoice.pdf"
    )
    return message.as_bytes()
