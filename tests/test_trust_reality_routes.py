"""Evidence-request routes for reality checks (FR-23.5, U4)."""

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from fresh_schema import fresh_schema

from invoice_processing.adapters import JsonlAuditSink, LocalBlobStore
from invoice_processing.api import auth, service
from invoice_processing.api.service import app
from invoice_processing.db import models
from invoice_processing.db.repositories import PgDocumentRepository
from invoice_processing.db.session import tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust.findings.model import Citation, FindingSpec
from invoice_processing.trust.findings.store import FindingStore
from invoice_processing.trust.graph.store import GraphStore

# Every path this module adds; test_tenant_isolation_api unions it into its
# coverage check, and TestRealityIsolation probes each one across tenants.
PROBED = {
    "/api/trust/findings/{finding_id}/evidence-request",
    "/api/trust/evidence-requests",
    "/api/trust/evidence-requests/{request_id}/proof",
    "/api/trust/evidence-requests/{request_id}/cancel",
}

ACME, GLOBEX = "tenant-acme", "tenant-globex"
PDF = b"%PDF-1.4 proof of delivery\n%%EOF"


class FakeVerifier:
    """Token `<tenant>:<role>`."""

    def verify(self, token):
        tenant, _, role = token.partition(":")
        if tenant not in ("acme", "globex") or role not in (
            "viewer",
            "clerk",
            "approver",
            "admin",
        ):
            raise auth.AuthError("Invalid token")
        return {
            "sub": f"u-{token}",
            "email": f"{role}@{tenant}.test",
            "email_verified": True,
            "tenant_id": f"tenant-{tenant}",
            "role": role,
        }


class FakeQueue:
    def __init__(self):
        self.published: list[dict] = []

    def publish(self, message, tenant_id=None):
        self.published.append(message)
        return "m"


@pytest.fixture
def env(tmp_path, monkeypatch, scratch_db):
    url = scratch_db("reality_routes.db")
    fresh_schema(url)
    for tid, slug in ((ACME, "acme"), (GLOBEX, "globex")):
        with tenant_session(tid, url) as s:
            s.add(models.Tenant(id=tid, name=slug, slug=slug, status="active"))
    graph, findings, queue = GraphStore(url), FindingStore(url), FakeQueue()
    blobs = LocalBlobStore(tmp_path / "b")
    monkeypatch.setattr(service, "_graph_store", graph)
    monkeypatch.setattr(service, "_finding_store", findings)
    monkeypatch.setattr(service, "_documents", PgDocumentRepository(url))
    monkeypatch.setattr(service, "get_blobs", lambda tenant_id: blobs)
    monkeypatch.setattr(service, "get_queue", lambda: queue)
    monkeypatch.setattr(
        service, "_audit", JsonlAuditSink(tmp_path / "audit.jsonl")
    )
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr(auth, "_verifier", FakeVerifier())
    monkeypatch.delenv("AUTH_DISABLED", raising=False)

    def finding(tid, status="detected"):
        ctx = RunContext(tenant_id=tid, case_id="", run_id="r", actor="engine")
        invoice = graph.upsert_node(
            ctx,
            kind="invoice",
            natural_key=f"invoices:{tid}",
            ref_table="invoices",
            ref_id="i1",
            attrs={"document_id": "doc-inv", "invoice_number": "INV-1"},
        )
        fid, _ = findings.upsert(
            ctx,
            FindingSpec(
                type="REAL-NO-EVIDENCE",
                transaction_node=invoice,
                dedupe_key=f"REAL-NO-EVIDENCE:{tid}",
                impact=None,
                currency="INR",
                confidence=0.5,
                engine_version="reality-1",
                rule_version="FR-23-v1",
                model_version="none",
                summary="Invoiced 100 units; no evidence on record.",
                citations=(Citation(invoice),),
            ),
        )
        if status != "detected":
            findings.transition(ctx, fid, "triaged", actor="t")
            findings.transition(ctx, fid, status, actor="t")
        return fid, invoice

    def client(token):
        return TestClient(app, headers={"Authorization": f"Bearer {token}"})

    return {
        "client": client,
        "finding": finding,
        "graph": graph,
        "findings": findings,
        "queue": queue,
        "blobs": blobs,
        "url": url,
    }


def ask(env, token, fid, **body):
    return env["client"](token).post(
        f"/api/trust/findings/{fid}/evidence-request",
        json={"owner": "ops@acme.test", "due_date": "2026-01-05", **body},
    )


def upload(env, token, rid, data=PDF):
    return env["client"](token).post(
        f"/api/trust/evidence-requests/{rid}/proof",
        files={"file": ("pod.pdf", data, "application/pdf")},
    )


def test_request_moves_the_finding_to_needs_vendor_input(env):
    fid, _ = env["finding"](ACME)
    r = ask(env, "acme:clerk", fid, note="POD please")
    assert r.status_code == 201, r.text
    row = r.json()
    assert row["status"] == "open" and row["owner"] == "ops@acme.test"
    assert row["requested_by"] == "clerk@acme.test"
    assert row["finding_status"] == "needs_vendor_input"
    assert env["findings"].get(ACME, fid)["status"] == "needs_vendor_input"
    # A second request on the same finding is allowed and leaves it there.
    assert ask(env, "acme:clerk", fid).status_code == 201


def test_request_needs_a_clerk_and_a_valid_body(env):
    fid, _ = env["finding"](ACME)
    assert ask(env, "acme:viewer", fid).status_code == 403
    assert ask(env, "acme:clerk", fid, owner="").status_code == 422
    assert ask(env, "acme:clerk", fid, owner="  ").status_code == 400
    assert env["findings"].get(ACME, fid)["status"] == "detected"
    assert ask(env, "acme:clerk", fid, due_date="soon").status_code == 422
    assert ask(env, "acme:clerk", "nope").status_code == 404


def test_a_decided_finding_cannot_be_asked_about(env):
    fid, _ = env["finding"](ACME, status="accepted")
    r = ask(env, "acme:clerk", fid)
    assert r.status_code == 409 and r.json()["detail"]["current"] == "accepted"


def test_list_filters_and_overdue_on_read(env):
    fid, _ = env["finding"](ACME)
    past = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    future = (date.today() + timedelta(days=30)).isoformat()
    late = ask(env, "acme:clerk", fid, due_date=past).json()
    ask(env, "acme:clerk", fid, due_date=future)
    viewer = env["client"]("acme:viewer")
    rows = viewer.get("/api/trust/evidence-requests").json()["requests"]
    assert len(rows) == 2
    over = viewer.get("/api/trust/evidence-requests?overdue=true").json()
    assert [r["id"] for r in over["requests"]] == [late["id"]]
    assert over["requests"][0]["overdue"] is True
    done = viewer.get("/api/trust/evidence-requests?status=fulfilled").json()
    assert done["requests"] == []
    bad = viewer.get("/api/trust/evidence-requests?status=lost")
    assert bad.status_code == 400


def test_list_filters_by_finding(env):
    fid, _ = env["finding"](ACME)
    rid = ask(env, "acme:clerk", fid).json()["id"]
    viewer = env["client"]("acme:viewer")
    mine = viewer.get(f"/api/trust/evidence-requests?finding_id={fid}").json()
    assert [r["id"] for r in mine["requests"]] == [rid]
    assert set(mine["requests"][0]) == {
        "id", "finding_id", "owner", "due_date", "note", "status",
        "proof_document_id", "requested_by", "fulfilled_at", "overdue",
        "created_at",
    }  # fmt: skip
    other = viewer.get("/api/trust/evidence-requests?finding_id=nope").json()
    assert other["requests"] == []


def test_proof_is_stored_linked_and_re_evaluated(env):
    fid, invoice = env["finding"](ACME)
    rid = ask(env, "acme:clerk", fid).json()["id"]
    assert upload(env, "acme:viewer", rid).status_code == 403
    r = upload(env, "acme:clerk", rid)
    assert r.status_code == 200, r.text
    row = r.json()
    assert row["status"] == "fulfilled" and row["proof_document_id"]
    doc = service.get_documents().get(ACME, row["proof_document_id"])
    assert doc["kind"] == "proof"
    assert env["blobs"].get(doc["storage_key"]) == PDF
    [node] = env["graph"].nodes(ACME, kind="document_version")
    assert node["attrs"]["kind"] == "proof"
    [edge] = env["graph"].edges(ACME, src=node["id"], kind="evidences")
    assert edge["dst"] == invoice and edge["provenance"] == "human_assertion"
    jobs = [(m["type"], m["document_id"]) for m in env["queue"].published]
    assert jobs == [
        ("trust.evaluate", row["proof_document_id"]),
        ("trust.evaluate", "doc-inv"),
    ]
    # Fulfilled is final.
    assert upload(env, "acme:clerk", rid).status_code == 409


def test_proof_must_be_a_pdf_and_not_bytes_of_another_document(env):
    fid, _ = env["finding"](ACME)
    rid = ask(env, "acme:clerk", fid).json()["id"]
    assert upload(env, "acme:clerk", rid, b"hello").status_code == 400
    with tenant_session(ACME, env["url"]) as s:
        import hashlib  # noqa: PLC0415

        s.add(
            models.Document(
                tenant_id=ACME,
                sha256=hashlib.sha256(PDF).hexdigest(),
                storage_key="k",
                kind="invoice",
            )
        )
    assert upload(env, "acme:clerk", rid).status_code == 409
    no_file = env["client"]("acme:clerk").post(
        f"/api/trust/evidence-requests/{rid}/proof", data={"x": "1"}
    )
    assert no_file.status_code == 400


def test_cancel_once(env):
    fid, _ = env["finding"](ACME)
    rid = ask(env, "acme:clerk", fid).json()["id"]
    url = f"/api/trust/evidence-requests/{rid}/cancel"
    assert env["client"]("acme:viewer").post(url).status_code == 403
    r = env["client"]("acme:clerk").post(url)
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert env["client"]("acme:clerk").post(url).status_code == 409
    assert upload(env, "acme:clerk", rid).status_code == 409
    gone = env["client"]("acme:clerk").post(
        "/api/trust/evidence-requests/nope/cancel"
    )
    assert gone.status_code == 404


class TestRealityIsolation:
    def test_requests_stay_with_their_tenant(self, env):
        fid, _ = env["finding"](ACME)
        rid = ask(env, "acme:clerk", fid).json()["id"]
        globex = "globex:clerk"
        # Globex cannot ask about, list, prove or cancel Acme's.
        assert ask(env, globex, fid).status_code == 404
        listed = env["client"](globex).get("/api/trust/evidence-requests")
        assert listed.status_code == 200 and listed.json()["requests"] == []
        assert upload(env, globex, rid).status_code == 404
        cancel = env["client"](globex).post(
            f"/api/trust/evidence-requests/{rid}/cancel"
        )
        assert cancel.status_code == 404
        # And none of it touched Acme's row.
        mine = env["client"]("acme:viewer").get("/api/trust/evidence-requests")
        [row] = mine.json()["requests"]
        assert row["status"] == "open" and row["proof_document_id"] is None
        assert env["findings"].get(GLOBEX, fid) is None
        assert env["queue"].published == []
