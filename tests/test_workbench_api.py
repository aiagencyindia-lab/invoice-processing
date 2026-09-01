"""Review workbench API.

The workbench is where the touchless rate is earned or lost. Two behaviours
carry the value:

* **Flagged fields surface without being hunted for.** The queue orders by
  weakest confidence and the detail view puts low-confidence fields first. A
  reviewer's scarcest resource is attention; making them scan for the suspect
  field is how a 45-second review becomes a three-minute one.
* **Every edit is recorded as a correction, with the old value.** Corrections
  are what accuracy is measured from and what the learning loop mines for
  rules. An edit applied without being recorded is data thrown away.

Tests run against the real processed cases through the CaseRepository port, so
they exercise the same path production will.
"""

import json
import shutil

import pytest
from fastapi.testclient import TestClient
from golden import GOLDEN_CASES

from invoice_processing.adapters import (
    FsCaseRepository,
    JsonDocumentRepository,
    LocalBlobStore,
)
from invoice_processing.api import auth, service
from invoice_processing.api.service import app
from invoice_processing.ports import RunContext

TENANT = "tenant-acme"
ADMIN_CLAIMS = {
    "sub": "u-admin",
    "email": "sme@acme.com",
    "email_verified": True,
    "tenant_id": TENANT,
    "role": "admin",
}


class FakeVerifier:
    """Claims without a network round trip or a real Google key."""

    def __init__(self, claims_by_token):
        self.claims_by_token = claims_by_token

    def verify(self, token):
        if token not in self.claims_by_token:
            raise auth.AuthError("Invalid token")
        return self.claims_by_token[token]


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Fully isolated: a copy of the artifacts, and temp correction/audit logs.

    Editing endpoints write back to the case repository, so pointing the API at
    the real `agent_output` would let one test mutate the fixture data another
    test asserts on -- which is exactly what happened before this fixture
    existed: a test passed alone and failed in the suite.
    """
    cases_root = tmp_path / "agent_output"
    shutil.copytree(GOLDEN_CASES, cases_root)

    monkeypatch.setattr(
        service, "_cases", FsCaseRepository(cases_root, tenant_id=TENANT)
    )
    monkeypatch.setattr(
        service, "_corrections_path", tmp_path / "corrections.jsonl"
    )
    monkeypatch.setattr(
        service, "_audit", service.JsonlAuditSink(tmp_path / "audit.jsonl")
    )
    # Authenticated as an admin of TENANT. The dev bypass is explicitly off, so
    # these tests exercise the same token path production uses.
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    monkeypatch.setattr(auth, "_verifier", FakeVerifier({"tok": ADMIN_CLAIMS}))
    return TestClient(app, headers={"Authorization": "Bearer tok"})


def _field(client, case_id: str, name: str):
    """Current value of a field.

    Tests assert against what is there rather than a hardcoded seed value: the
    artifacts are regenerated output, so pinning a literal makes the test fail
    the next time the pipeline is re-run.
    """
    fields = client.get(f"/api/invoices/{case_id}").json()["fields"]
    return next(f for f in fields if f["name"] == name)["value"]


class TestQueue:
    def test_lists_processed_invoices(self, client):
        body = client.get("/api/invoices").json()
        assert body["counts"]["total"] >= 3
        assert {i["case_id"] for i in body["invoices"]} >= {
            "case_001",
            "case_002",
            "case_005",
        }

    def test_summary_carries_what_a_reviewer_triages_on(self, client):
        invoice = client.get("/api/invoices").json()["invoices"][0]
        for key in ("vendor", "invoice_number", "total", "routing", "decision"):
            assert key in invoice

    def test_review_items_sort_above_auto(self, client):
        """A chronological queue buries the invoices that need a human."""
        invoices = client.get("/api/invoices").json()["invoices"]
        routings = [i["routing"] for i in invoices]
        assert routings == sorted(routings, key=lambda r: r != "REVIEW")

    def test_a_document_still_being_extracted_is_not_an_invoice(self, client):
        """The worker records its step through the same store the artifacts
        use. Counting that record as an artifact listed a case with no vendor,
        no fields and no validation records -- a blank row in the workbench for
        a document that was still being read."""
        service.get_cases().put_artifact(
            RunContext(tenant_id=TENANT, case_id="still-extracting"),
            "progress",
            {"step": 2, "total": 9, "key": "extraction", "label": "x"},
        )

        listed = {
            i["case_id"] for i in client.get("/api/invoices").json()["invoices"]
        }
        assert "still-extracting" not in listed
        assert client.get("/api/invoices/still-extracting").status_code == 404

    def test_counts_are_consistent(self, client):
        body = client.get("/api/invoices").json()
        counts = body["counts"]
        assert counts["needs_review"] + counts["auto"] == counts["total"]


class TestDetail:
    def test_returns_fields_lines_and_findings(self, client):
        body = client.get("/api/invoices/case_005").json()
        assert body["fields"], "no fields returned"
        assert body["line_items"], "no line items returned"
        assert body["findings"], "no validation records returned"

    def test_low_confidence_fields_come_first(self, client):
        """The reviewer must not have to hunt for the flagged field."""
        fields = client.get("/api/invoices/case_005").json()["fields"]
        scored = [
            f["confidence"] for f in fields if f["confidence"] is not None
        ]
        assert scored == sorted(scored), "fields are not ordered by confidence"

    def test_unchecked_fields_are_marked_as_such(self, client):
        """A field with no verdict must not be presented as though it had one."""
        fields = client.get("/api/invoices/case_005").json()["fields"]
        unchecked = [f for f in fields if not f["checked"]]
        for field in unchecked:
            assert field["confidence"] is None, (
                f"{field['name']} was never checked but carries a confidence score"
            )

    def test_findings_explain_the_decision(self, client):
        """Explainability is the product's differentiator, so it must be served."""
        findings = client.get("/api/invoices/case_005").json()["findings"]
        assert all("step" in f and "evidence" in f for f in findings)

    def test_unknown_case_is_404(self, client):
        assert client.get("/api/invoices/nope").status_code == 404


class TestDocument:
    def test_serves_the_source_pdf(self, client):
        response = client.get("/api/invoices/case_005/document")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert response.content.startswith(b"%PDF")

    def test_unknown_case_is_404(self, client):
        assert client.get("/api/invoices/nope/document").status_code == 404

    def test_serves_an_uploaded_pdf_from_the_blob_store(
        self, client, tmp_path, monkeypatch
    ):
        """Only the three bundled sample cases used to have a document.

        Every invoice a customer actually uploads has a case id that is not a
        sample directory, so the reviewer's document pane was blank for all of
        them -- a review screen with nothing to review against.
        """
        case_id = "8f14e45f-ea2b-4c9d-9d3f-2c0b6ad0e001"
        key = "documents/deadbeef.pdf"
        pdf = b"%PDF-1.4\n%uploaded\n"

        blobs = LocalBlobStore(tmp_path / "blobs")
        blobs.put(key, pdf, "application/pdf")
        monkeypatch.setattr(service, "get_blobs", lambda tenant_id: blobs)

        documents = JsonDocumentRepository(tmp_path / "documents.json")
        documents.create(
            TENANT,
            id=case_id,
            sha256="deadbeef",
            storage_key=key,
            filename="uploaded.pdf",
        )
        monkeypatch.setattr(service, "_documents", documents)

        # The case must exist for the tenant, or the endpoint refuses before it
        # ever reaches the blob store -- that guard is what stops a stranger
        # downloading a document by guessing an id.
        service.get_cases().put_artifact(
            RunContext(tenant_id=TENANT, case_id=case_id),
            "extraction",
            {"invoice": {"vendor_name": "Uploaded Pty Ltd"}},
        )

        response = client.get(f"/api/invoices/{case_id}/document")
        assert response.status_code == 200
        assert response.content == pdf


class TestCorrections:
    """The asset that compounds."""

    def test_edit_records_old_and_new_values(self, client, tmp_path):
        before = _field(client, "case_002", "vendor_name")
        response = client.patch(
            "/api/invoices/case_002/fields",
            json={
                "edits": [
                    {"field": "vendor_name", "value": "Precision Tech Co Ltd"}
                ]
            },
        )
        body = response.json()
        assert body["count"] == 1

        correction = body["corrections"][0]
        assert correction["field_path"] == "vendor_name"
        assert correction["old_value"] == before
        assert correction["new_value"] == "Precision Tech Co Ltd"
        assert correction["actor"] == "sme@acme.com"

    def test_correction_is_persisted(self, client, tmp_path):
        client.patch(
            "/api/invoices/case_002/fields",
            json={"edits": [{"field": "payment_terms", "value": "Net 999"}]},
        )
        lines = (
            (tmp_path / "corrections.jsonl").read_text().strip().splitlines()
        )
        assert len(lines) == 1
        assert json.loads(lines[0])["new_value"] == "Net 999"

    def test_a_no_op_edit_is_not_a_correction(self, client):
        """Re-saving an unchanged field must not pollute accuracy measurement."""
        current = client.get("/api/invoices/case_002").json()
        vendor = next(
            f for f in current["fields"] if f["name"] == "vendor_name"
        )

        body = client.patch(
            "/api/invoices/case_002/fields",
            json={
                "edits": [{"field": "vendor_name", "value": vendor["value"]}]
            },
        ).json()
        assert body["count"] == 0

    def test_edit_emits_an_audit_event(self, client, tmp_path):
        client.patch(
            "/api/invoices/case_002/fields",
            json={"edits": [{"field": "currency", "value": "NZD"}]},
        )
        events = [
            json.loads(line)
            for line in (tmp_path / "audit.jsonl")
            .read_text()
            .strip()
            .splitlines()
        ]
        assert events[-1]["event_type"] == "fields_corrected"
        assert events[-1]["actor"] == "sme@acme.com"

    def test_unknown_case_is_404(self, client):
        response = client.patch(
            "/api/invoices/nope/fields",
            json={"edits": [{"field": "x", "value": "y"}]},
        )
        assert response.status_code == 404


class TestActions:
    def test_approve(self, client):
        body = client.post(
            "/api/invoices/case_002/actions", json={"action": "approve"}
        ).json()
        assert body["action"] == "approve"

    def test_rejection_requires_a_reason(self, client):
        """A rejection with no reason cannot be actioned or explained."""
        response = client.post(
            "/api/invoices/case_002/actions", json={"action": "reject"}
        )
        assert response.status_code == 400
        assert "reason" in response.json()["detail"].lower()

    def test_rejection_with_a_reason_is_accepted(self, client):
        response = client.post(
            "/api/invoices/case_002/actions",
            json={"action": "reject", "reason": "Duplicate of INV-00001"},
        )
        assert response.status_code == 200

    def test_unknown_action_is_rejected(self, client):
        response = client.post(
            "/api/invoices/case_002/actions", json={"action": "delete"}
        )
        assert response.status_code == 400


class TestStats:
    def test_reports_the_pilot_numbers(self, client):
        body = client.get("/api/stats").json()
        for key in (
            "total",
            "needs_review",
            "auto",
            "touchless_rate",
            "corrections",
        ):
            assert key in body

    def test_touchless_rate_matches_the_counts(self, client):
        body = client.get("/api/stats").json()
        if body["total"]:
            assert body["touchless_rate"] == round(
                body["auto"] / body["total"], 3
            )


class TestRoot:
    """`/` used to serve the single-file workbench. It points at the app now.

    The claims the old page made are still tested, against the source of the
    app that replaced it -- see tests/test_frontend_reaches_the_flow.py.
    """

    def test_points_at_the_application(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "Open the application" in response.text

    def test_says_how_to_start_the_app_when_it_is_not_running(self, client):
        """A dead link is a dead end; the next step has to be on the page."""
        assert "npm run dev" in client.get("/").text
