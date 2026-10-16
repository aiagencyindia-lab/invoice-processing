"""Trust Audit routes (Wave 2): engagements, runs, samples, report, ledger,
each probed as two tenants."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from test_tenant_isolation_api import ACME, GLOBEX, FakeVerifier

from invoice_processing.adapters import JsonlAuditSink
from invoice_processing.api import auth, service
from invoice_processing.api.service import app
from invoice_processing.db import models
from invoice_processing.db.session import create_all, get_engine, tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.tenant_settings import JsonTenantSettings
from invoice_processing.trust.findings.model import Citation, FindingSpec
from invoice_processing.trust.findings.store import FindingStore
from invoice_processing.trust.graph.store import GraphStore
from invoice_processing.worker.queue import LocalQueue

# Every path this module adds; the isolation coverage test unions it.
PROBED = {
    "/api/trust/audits",
    "/api/trust/audits/{audit_id}",
    "/api/trust/audits/{audit_id}/runs",
    "/api/trust/audits/{audit_id}/runs/{run_id}",
    "/api/trust/audits/{audit_id}/samples",
    "/api/trust/audits/{audit_id}/samples/{sample_id}",
    "/api/trust/audits/{audit_id}/report",
    "/api/trust/audits/{audit_id}/ledger",
}

AT = datetime(2026, 10, 1, tzinfo=timezone.utc)
BODY = {
    "name": "FY26 leakage",
    "period_from": "2026-01-01",
    "period_to": "2026-12-31",
    "vendors": [],
    "categories": [],
    "terms": {
        "recovered_pct": "25",
        "prevented_pct": "10",
        "cap": None,
        "minimum": None,
    },
    "approver": "cfo@acme.test",
}


@pytest.fixture
def env(tmp_path, monkeypatch, scratch_db):
    url = scratch_db("audit_routes.db")
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
    for tid, slug in ((ACME, "acme"), (GLOBEX, "globex")):
        with tenant_session(tid, url) as s:
            s.add(models.Tenant(id=tid, name=slug, slug=slug, status="active"))
    graph, findings = GraphStore(url), FindingStore(url)
    monkeypatch.setattr(service, "_graph_store", graph)
    monkeypatch.setattr(service, "_finding_store", findings)
    monkeypatch.setattr(
        service, "_tenant_settings", JsonTenantSettings(tmp_path / "ts.json")
    )
    monkeypatch.setattr(
        service, "_audit", JsonlAuditSink(tmp_path / "audit.jsonl")
    )
    queue = LocalQueue(tmp_path / "queue")
    monkeypatch.setattr(service, "get_queue", lambda: queue)
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr(auth, "_verifier", FakeVerifier())
    monkeypatch.delenv("AUTH_DISABLED", raising=False)

    out = {"url": url, "graph": graph, "findings": findings, "queue": queue}
    for tid, slug, vendor_name in (
        (ACME, "acme", "Acme Steel"),
        (GLOBEX, "globex", "=Globex()"),
    ):
        ctx = RunContext(tenant_id=tid, case_id="", run_id="r", actor="engine")
        vendor = graph.upsert_node(
            ctx,
            kind="vendor",
            natural_key="vendors:v",
            attrs={"name": vendor_name},
        )
        invoice = graph.upsert_node(
            ctx,
            kind="invoice",
            natural_key="invoices:i",
            ref_table="invoices",
            ref_id=f"i-{slug}",
            attrs={"case_id": f"case-{slug}", "invoice_date": "2026-03-01"},
        )
        graph.upsert_edge(
            ctx,
            src=invoice,
            dst=vendor,
            kind="issued_by",
            source_system="v1",
            provenance="system_record",
            confidence=1.0,
            observed_at=AT,
        )
        fid, _ = findings.upsert(
            ctx,
            FindingSpec(
                type="LEAK-RATE",
                transaction_node=invoice,
                dedupe_key="LEAK-RATE:i",
                impact=Decimal("100"),
                currency="INR",
                confidence=0.9,
                engine_version="e",
                rule_version="r",
                model_version="none",
                summary=f"{slug}'s leak",
                details={"engine": "leakage"},
                citations=(Citation(invoice, "the invoice"),),
            ),
        )
        out[slug] = {"vendor": vendor, "invoice": invoice, "finding": fid}

    def client(slug="acme", role="approver"):
        return TestClient(
            app, headers={"Authorization": f"Bearer {slug}-{role}"}
        )

    out["client"] = client
    return out


def _create(env, slug="acme", **over):
    r = env["client"](slug).post("/api/trust/audits", json={**BODY, **over})
    assert r.status_code == 201, r.text
    return r.json()


# -- engagements --------------------------------------------------------------


def test_create_get_list_and_patch_an_engagement(env):
    a = _create(env)
    assert a["status"] == "draft" and a["created_by"] == "approver@acme.test"
    assert (
        a["period_from"] == "2026-01-01" and a["terms"]["recovered_pct"] == "25"
    )
    viewer = env["client"]("acme", "viewer")
    assert [x["id"] for x in viewer.get("/api/trust/audits").json()] == [
        a["id"]
    ]
    detail = viewer.get(f"/api/trust/audits/{a['id']}").json()
    assert (
        detail["runs"] == []
        and detail["samples"] == []
        and detail["name"] == a["name"]
    )
    r = env["client"]().patch(
        f"/api/trust/audits/{a['id']}",
        json={
            "status": "closed",
            "terms": {
                "recovered_pct": "30",
                "prevented_pct": "5",
                "cap": "1000",
                "minimum": None,
            },
        },
    )
    assert r.status_code == 200 and r.json()["status"] == "closed"
    assert r.json()["terms"] == {
        "recovered_pct": "30",
        "prevented_pct": "5",
        "cap": "1000",
        "minimum": None,
    }


def test_engagement_validation(env):
    c = env["client"]()
    assert (
        c.post("/api/trust/audits", json={**BODY, "name": "  "}).status_code
        == 400
    )
    assert (
        c.post(
            "/api/trust/audits",
            json={
                **BODY,
                "period_from": "2026-12-31",
                "period_to": "2026-01-01",
            },
        ).status_code
        == 400
    )
    assert (
        c.post(
            "/api/trust/audits",
            json={**BODY, "terms": {"recovered_pct": "abc"}},
        ).status_code
        == 400
    )
    assert (
        c.post(
            "/api/trust/audits", json={**BODY, "terms": {"recovered_pct": "-1"}}
        ).status_code
        == 400
    )
    a = _create(env, terms=None)
    assert a["terms"] == {
        "recovered_pct": "25",
        "prevented_pct": "10",
        "cap": None,
        "minimum": None,
    }
    assert (
        c.patch(
            f"/api/trust/audits/{a['id']}", json={"status": "bogus"}
        ).status_code
        == 400
    )
    assert (
        env["client"]("acme", "clerk")
        .post("/api/trust/audits", json=BODY)
        .status_code
        == 403
    )
    assert (
        env["client"]("acme", "viewer")
        .patch(f"/api/trust/audits/{a['id']}", json={"name": "x"})
        .status_code
        == 403
    )


# -- runs ---------------------------------------------------------------------


def test_run_is_queued_once_and_enqueued(env):
    a = _create(env)
    c = env["client"]()
    r = c.post(
        f"/api/trust/audits/{a['id']}/runs", json={"engines": ["leakage"]}
    )
    assert r.status_code == 202, r.text
    run = r.json()
    assert run["status"] == "queued" and run["engines"] == ["leakage"]
    assert run["requested_by"] == "approver@acme.test"
    [msg] = env["queue"].pull(5)
    assert (
        msg.payload["type"] == "trust.audit.run"
        and msg.payload["run_id"] == run["id"]
    )
    assert msg.payload["tenant_id"] == ACME
    assert c.get(f"/api/trust/audits/{a['id']}").json()["status"] == "active"
    again = c.post(f"/api/trust/audits/{a['id']}/runs", json={})
    assert (
        again.status_code == 409
        and again.json()["detail"]["run_id"] == run["id"]
    )
    assert (
        c.post(
            f"/api/trust/audits/{a['id']}/runs", json={"engines": ["nope"]}
        ).status_code
        == 400
    )
    got = (
        env["client"]("acme", "viewer")
        .get(f"/api/trust/audits/{a['id']}/runs/{run['id']}")
        .json()
    )
    assert got["id"] == run["id"] and got["progress"]["engines_total"] == 0
    assert c.get(f"/api/trust/audits/{a['id']}/runs/nope").status_code == 404
    assert (
        env["client"]("acme", "clerk")
        .post(f"/api/trust/audits/{a['id']}/runs", json={})
        .status_code
        == 403
    )


def test_a_stale_run_is_abandoned_and_a_new_one_accepted(env, monkeypatch):
    from invoice_processing.trust.audit import store as audit_store  # noqa: PLC0415

    a = _create(env)
    c = env["client"]()
    first = c.post(f"/api/trust/audits/{a['id']}/runs", json={}).json()
    monkeypatch.setattr(
        audit_store,
        "_now",
        lambda: datetime.now(timezone.utc) + audit_store.QUEUED_FOR * 2,
    )
    r = c.post(f"/api/trust/audits/{a['id']}/runs", json={})
    assert r.status_code == 202 and r.json()["id"] != first["id"]
    old = c.get(f"/api/trust/audits/{a['id']}/runs/{first['id']}").json()
    assert old["status"] == "failed" and old["error"] == "abandoned"


def test_a_run_is_queued_even_when_the_queue_is_down(env, monkeypatch):
    def down():
        raise RuntimeError("no queue")

    monkeypatch.setattr(service, "get_queue", down)
    a = _create(env)
    r = env["client"]().post(f"/api/trust/audits/{a['id']}/runs", json={})
    assert r.status_code == 202 and r.json()["status"] == "queued"


# -- samples ------------------------------------------------------------------


def test_sample_draws_from_the_scope_and_takes_verdicts(env):
    a = _create(env)
    c, clerk = env["client"](), env["client"]("acme", "clerk")
    assert (
        c.post(
            f"/api/trust/audits/{a['id']}/samples", json={"size": 2}
        ).status_code
        == 400
    )
    assert (
        c.post(
            f"/api/trust/audits/{a['id']}/samples", json={"size": 0}
        ).status_code
        == 400
    )
    r = c.post(
        f"/api/trust/audits/{a['id']}/samples", json={"size": 1, "seed": "s1"}
    )
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["finding_ids"] == [env["acme"]["finding"]] and s["seed"] == "s1"
    assert s["precision"] is None and s["validated"] == 0
    path = f"/api/trust/audits/{a['id']}/samples/{s['id']}"
    bad = clerk.patch(path, json={"verdicts": {"nope": True}})
    assert bad.status_code == 400
    r = clerk.patch(path, json={"verdicts": {env["acme"]["finding"]: True}})
    assert (
        r.status_code == 200
        and r.json()["precision"] == 1.0
        and r.json()["true"] == 1
    )
    assert (
        env["client"]("acme", "viewer")
        .patch(path, json={"verdicts": {}})
        .status_code
        == 403
    )
    [listed] = c.get(f"/api/trust/audits/{a['id']}").json()["samples"]
    assert listed["validated"] == 1
    # an out-of-scope engagement has nothing to sample
    empty = _create(env, vendors=["no-such-node"])
    assert (
        c.post(
            f"/api/trust/audits/{empty['id']}/samples", json={"size": 1}
        ).status_code
        == 400
    )


# -- report + ledger ----------------------------------------------------------


def _recover(env, fid):
    c = env["client"]()
    for status in ("triaged", "accepted", "claimed", "recovered"):
        assert (
            c.post(
                f"/api/trust/findings/{fid}/transition",
                json={"to_status": status},
            ).status_code
            == 200
        )


def test_report_json_and_html(env):
    a = _create(env)
    _recover(env, env["acme"]["finding"])
    v = env["client"]("acme", "viewer")
    r = v.get(f"/api/trust/audits/{a['id']}/report")
    assert r.status_code == 200
    rep = r.json()
    assert (
        rep["report_version"] == 1
        and rep["audit"]["id"] == a["id"]
        and rep["run"] is None
    )
    assert rep["summary"] == {
        "findings": 1,
        "detected_impact": "100.00",
        "accepted_impact": "100.00",
        "recovered": "100.00",
        "prevented": "0.00",
        "written_off": "0.00",
        "at_risk_holds": "0.00",
        "currency": "INR",
    }
    assert rep["ledger"]["fee"]["due"] == "25.00"
    assert rep["by_vendor"][0]["vendor"] == "Acme Steel"
    assert (
        rep["top_recoveries"][0]["citations"][0]["source"]["document_url"]
        == "/api/invoices/case-acme/document"
    )
    assert rep["prevention"][0]["mode_now"] == "off"
    h = v.get(f"/api/trust/audits/{a['id']}/report?format=html")
    assert h.status_code == 200 and h.headers["content-type"].startswith(
        "text/html"
    )
    assert "Executive summary" in h.text and "Acme Steel" in h.text
    assert (
        v.get(f"/api/trust/audits/{a['id']}/report?format=pdf").status_code
        == 400
    )


def test_ledger_json_and_formula_safe_csv(env):
    a = _create(env)
    _recover(env, env["acme"]["finding"])
    v = env["client"]("acme", "viewer")
    led = v.get(f"/api/trust/audits/{a['id']}/ledger").json()
    assert [r["to_status"] for r in led["rows"]] == ["claimed", "recovered"]
    assert (
        led["rows"][1]["attested_by"] == "approver@acme.test"
        and led["rows"][1]["amount"] == "100.00"
    )
    assert (
        led["totals"]["recovered"] == "100.00"
        and led["fee"]["fee_recovered"] == "25.00"
    )
    r = v.get(f"/api/trust/audits/{a['id']}/ledger?format=csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith(
        "text/csv"
    )
    assert "attachment" in r.headers["content-disposition"]
    text = r.content.decode("utf-8-sig")
    assert (
        text.splitlines()[0]
        == "finding_id,type,vendor,to_status,amount,attested_by,at,reason"
    )
    assert "Acme Steel" in text and "recovered,100.00" in text
    assert (
        v.get(f"/api/trust/audits/{a['id']}/ledger?format=xlsx").status_code
        == 400
    )


# -- isolation ----------------------------------------------------------------


def test_engagements_runs_samples_report_and_ledger_stay_with_their_tenant(env):
    a = _create(env)
    acme, globex = env["client"](), env["client"]("globex")
    run = acme.post(f"/api/trust/audits/{a['id']}/runs", json={}).json()
    sample = acme.post(
        f"/api/trust/audits/{a['id']}/samples", json={"size": 1}
    ).json()
    assert globex.get("/api/trust/audits").json() == []
    base = f"/api/trust/audits/{a['id']}"
    assert globex.get(base).status_code == 404
    assert globex.patch(base, json={"name": "stolen"}).status_code == 404
    assert globex.post(f"{base}/runs", json={}).status_code == 404
    assert globex.get(f"{base}/runs/{run['id']}").status_code == 404
    assert globex.post(f"{base}/samples", json={"size": 1}).status_code == 404
    assert (
        globex.patch(
            f"{base}/samples/{sample['id']}", json={"verdicts": {}}
        ).status_code
        == 404
    )
    assert globex.get(f"{base}/report").status_code == 404
    assert globex.get(f"{base}/ledger?format=csv").status_code == 404
    assert acme.get(base).json()["name"] == BODY["name"]
    # Globex's own engagement with the same scope sees only Globex's finding,
    # and its CSV carries no Acme vendor.
    g = _create(env, "globex")
    _recover(env, env["acme"]["finding"])
    for status in ("triaged", "accepted", "claimed", "recovered"):
        globex.post(
            f"/api/trust/findings/{env['globex']['finding']}/transition",
            json={"to_status": status},
        )
    rep = globex.get(f"/api/trust/audits/{g['id']}/report").json()
    assert (
        rep["summary"]["findings"] == 1
        and rep["by_vendor"][0]["vendor"] == "=Globex()"
    )
    csv_text = globex.get(
        f"/api/trust/audits/{g['id']}/ledger?format=csv"
    ).content.decode("utf-8-sig")
    assert "Acme" not in csv_text and "'=Globex()" in csv_text
    s = globex.post(
        f"/api/trust/audits/{g['id']}/samples", json={"size": 1}
    ).json()
    assert s["finding_ids"] == [env["globex"]["finding"]]
    assert globex.get(
        f"/api/trust/audits/{a['id']}/samples/{sample['id']}"
    ).status_code in (404, 405)
