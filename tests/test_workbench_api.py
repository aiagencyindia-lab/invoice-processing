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

from invoice_processing import paths
from invoice_processing.adapters import FsCaseRepository
from invoice_processing.api import auth, service
from invoice_processing.api.service import app

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
    shutil.copytree(paths.AGENTIC_FLOW_OUT, cases_root)

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


class TestUI:
    def test_workbench_is_served(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "Invoice Review" in response.text

    def test_ui_is_keyboard_driven(self, client):
        """Throughput target is ~45s per exception; that needs keys, not clicks."""
        html = client.get("/").text
        for key in ("approve", "reject", "save"):
            assert key in html
        assert "keydown" in html


class TestUIReachesTheWholeFlow:
    """The end-to-end flow must be reachable by a reviewer, not just by curl.

    Upload and export endpoints existed and were tested for a full phase while
    the UI had no control for either, so the flow the product is sold on --
    upload, review, correct, approve, export -- could not be completed by a
    person. Endpoint tests cannot catch that; only asking what the page can
    reach can.
    """

    def _html(self, client):
        return client.get("/").text

    def test_every_step_of_the_flow_has_a_control(self, client):
        """A reachable endpoint is not a usable one.

        Checking only that the URL appears in the page would pass with the
        buttons deleted, since the fetch calls would still mention it. Each
        step needs something a person can actually click or press.
        """
        html = self._html(client)
        for step, control in (
            ("upload", 'onchange="upload()"'),
            ("correct", 'onclick="save()"'),
            ("approve", "onclick=\"act('approve')\""),
            ("reject", "onclick=\"act('reject')\""),
            ("export", 'onclick="exportCsv()"'),
        ):
            assert control in html, f"no control for the {step} step"

    def test_upload_accepts_only_pdfs(self, client):
        """The server checks magic bytes; the picker should not offer the rest."""
        assert 'accept="application/pdf"' in self._html(client)

    def test_the_new_actions_have_keyboard_shortcuts(self, client):
        """Throughput is the point; a mouse-only control is a slow control."""
        html = self._html(client)
        assert 'k === "e"' in html, "no shortcut for export"
        assert 'k === "u"' in html, "no shortcut for upload"

    def test_an_unapproved_export_is_explained_not_just_failed(self, client):
        """A reviewer needs to know approving is the next step, not that it broke."""
        html = self._html(client)
        assert "Approve the invoice before exporting" in html
        assert "409" in html

    def test_upload_does_not_claim_the_invoice_is_processed(self, client):
        """It is stored and queued; extraction happens later in the worker."""
        html = self._html(client)
        assert "queued for processing" in html
        assert "duplicate" in html, (
            "a re-sent document must be reported as such"
        )
