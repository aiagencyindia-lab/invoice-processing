"""Trust Audit (Wave 2): ledger + fee, scope and sampling, report, store, batch."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from invoice_processing.db import models
from invoice_processing.db.session import create_all, get_engine, tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust.audit import ledger, render, report, sampling
from invoice_processing.trust.audit.store import TrustAuditStore
from invoice_processing.trust.graph.store import GraphStore

# -- ledger + fee -------------------------------------------------------------

TERMS = {
    "recovered_pct": "25",
    "prevented_pct": "10",
    "cap": None,
    "minimum": None,
}


@pytest.mark.parametrize(
    ("terms", "recovered", "prevented", "expected"),
    [
        (
            TERMS,
            "1000",
            "500",
            ("250.00", "50.00", "300.00", "300.00", "300.00"),
        ),
        (TERMS, "0", "0", ("0.00", "0.00", "0.00", "0.00", "0.00")),
        # ROUND_HALF_UP at the half cent: 0.125 -> 0.13, 0.1 * 0.05 = 0.005 -> 0.01
        (TERMS, "0.5", "0.05", ("0.13", "0.01", "0.14", "0.14", "0.14")),
        (
            {**TERMS, "cap": "100"},
            "1000",
            "0",
            ("250.00", "0.00", "250.00", "100.00", "100.00"),
        ),
        (
            {**TERMS, "minimum": "400"},
            "1000",
            "0",
            ("250.00", "0.00", "250.00", "250.00", "400.00"),
        ),
        # cap then minimum: the minimum wins over the cap when it is higher
        (
            {**TERMS, "cap": "100", "minimum": "150"},
            "1000",
            "0",
            ("250.00", "0.00", "250.00", "100.00", "150.00"),
        ),
        (
            {"recovered_pct": "12.5", "prevented_pct": "0"},
            "10",
            "99",
            ("1.25", "0.00", "1.25", "1.25", "1.25"),
        ),
    ],
)
def test_fee_table(terms, recovered, prevented, expected):
    out = ledger.fee(terms, Decimal(recovered), Decimal(prevented))
    assert (
        out["fee_recovered"],
        out["fee_prevented"],
        out["gross"],
        out["capped"],
        out["due"],
    ) == expected
    assert all(isinstance(v, str) for v in out.values())


def _event(fid, to, amount, **kw):
    return {
        "finding_id": fid,
        "type": kw.get("type", "LEAK-RATE"),
        "to_status": to,
        "amount": amount,
        "actor": kw.get("actor", "a@x.test"),
        "at": kw.get("at", "2026-10-01T00:00:00+00:00"),
        "reason": kw.get("reason"),
    }


def test_ledger_rows_keep_only_scope_money_events_and_totals_add_up():
    events = [
        _event("f1", "detected", "100.0000"),
        _event("f1", "claimed", "100.0000"),
        _event("f1", "recovered", "80.5000", reason="partial"),
        _event("f2", "prevented", "20.0000"),
        _event("f3", "recovered", "999"),  # out of scope
        _event("f4", "written_off", None, reason="gone"),
    ]
    rows = ledger.rows({"f1": "Acme Steel", "f2": None, "f4": "Globex"}, events)
    assert [r["to_status"] for r in rows] == [
        "claimed",
        "recovered",
        "prevented",
        "written_off",
    ]
    assert rows[1] == {
        "finding_id": "f1",
        "type": "LEAK-RATE",
        "vendor": "Acme Steel",
        "to_status": "recovered",
        "amount": "80.50",
        "attested_by": "a@x.test",
        "at": "2026-10-01T00:00:00+00:00",
        "reason": "partial",
    }
    assert rows[3]["amount"] is None
    assert ledger.totals(rows) == {
        "recovered": "80.50",
        "prevented": "20.00",
        "written_off": "0.00",
        "claimed": "100.00",
    }


# -- scope + sampling ---------------------------------------------------------

T = "tenant-acme"
AT = datetime(2026, 10, 1, tzinfo=timezone.utc)
CTX = RunContext(tenant_id=T, case_id="", run_id="r", actor="engine")


@pytest.fixture
def graph_db(scratch_db):
    url = scratch_db("audit.db")
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
    with tenant_session(T, url) as s:
        s.add(models.Tenant(id=T, name="Acme", slug="acme", status="active"))
    graph = GraphStore(url)

    def node(kind, key, **attrs):
        return graph.upsert_node(CTX, kind=kind, natural_key=key, attrs=attrs)

    def edge(src, dst, kind):
        graph.upsert_edge(
            CTX,
            src=src,
            dst=dst,
            kind=kind,
            source_system="v1",
            provenance="system_record",
            confidence=1.0,
            observed_at=AT,
        )

    v1, v2 = (
        node("vendor", "vendors:v1", name="Acme Steel"),
        node("vendor", "vendors:v2", name="Globex"),
    )
    i1 = node("invoice", "invoices:i1", invoice_date="2026-03-10", case_id="c1")
    i2 = node("invoice", "invoices:i2", invoice_date="2026-07-01", case_id="c2")
    i3 = node("invoice", "invoices:i3", case_id="c3")  # undated
    edge(i1, v1, "issued_by")
    edge(i2, v2, "issued_by")
    edge(i3, v1, "issued_by")
    l1 = node(
        "invoice_line", "lines:1", description="Steel coils", gl_account="5100"
    )
    l2 = node(
        "invoice_line", "lines:2", description=None, gl_account="6200 Freight"
    )
    edge(l1, i1, "part_of")
    edge(l2, i2, "part_of")
    return {
        "url": url,
        "graph": graph,
        "v1": v1,
        "v2": v2,
        "i1": i1,
        "i2": i2,
        "i3": i3,
    }


def _finding(
    fid, node, impact="100", status="detected", type="LEAK-RATE", **kw
):
    return {
        "id": fid,
        "type": type,
        "transaction_node": node,
        "status": status,
        "impact": None if impact is None else f"{Decimal(impact):.4f}",
        "currency": kw.get("currency", "INR"),
        "summary": f"{type} on {fid}",
        "details": kw.get("details", {}),
        "citations": kw.get("citations", [{"node_id": node, "excerpt": "inv"}]),
    }


def _audit(**over):
    return {
        "id": "a1",
        "name": "FY26",
        "status": "draft",
        "period_from": None,
        "period_to": None,
        "vendors": [],
        "categories": [],
        "terms": TERMS,
        "approver": "cfo@x.test",
        **over,
    }


def test_scope_without_filters_keeps_everything(graph_db):
    fs = [
        _finding("f1", graph_db["i1"]),
        _finding("f2", graph_db["i2"]),
        _finding("f3", graph_db["i3"]),
    ]
    out = sampling.scope_findings(_audit(), fs, graph_db["graph"], tenant_id=T)
    assert [f["id"] for f in out] == ["f1", "f2", "f3"]


def test_scope_by_period_uses_the_transaction_date_and_drops_undated(graph_db):
    fs = [
        _finding("f1", graph_db["i1"]),
        _finding("f2", graph_db["i2"]),
        _finding("f3", graph_db["i3"]),
    ]
    audit = _audit(period_from="2026-01-01", period_to="2026-06-30")
    out = sampling.scope_findings(audit, fs, graph_db["graph"], tenant_id=T)
    assert [f["id"] for f in out] == ["f1"]
    audit = _audit(period_from=date(2026, 7, 1), period_to=None)
    out = sampling.scope_findings(audit, fs, graph_db["graph"], tenant_id=T)
    assert [f["id"] for f in out] == ["f2"]


def test_scope_by_vendor_node_and_category_keyword(graph_db):
    fs = [
        _finding("f1", graph_db["i1"]),
        _finding("f2", graph_db["i2"]),
        _finding("f3", graph_db["i3"]),
    ]
    g = graph_db["graph"]
    out = sampling.scope_findings(
        _audit(vendors=[graph_db["v2"]]), fs, g, tenant_id=T
    )
    assert [f["id"] for f in out] == ["f2"]
    out = sampling.scope_findings(
        _audit(categories=["freight"]), fs, g, tenant_id=T
    )
    assert [f["id"] for f in out] == ["f2"]
    out = sampling.scope_findings(
        _audit(categories=["5100", "nothing"]), fs, g, tenant_id=T
    )
    assert [f["id"] for f in out] == ["f1"]
    out = sampling.scope_findings(
        _audit(vendors=[graph_db["v1"]], categories=["steel"]),
        fs,
        g,
        tenant_id=T,
    )
    assert [f["id"] for f in out] == ["f1"]


def test_draw_is_deterministic_by_seed_and_refuses_oversize():
    ids = [f"f{i}" for i in range(10)]
    a = sampling.draw(ids, 4, "seed-1")
    assert a == sampling.draw(list(reversed(ids)), 4, "seed-1")
    assert len(set(a)) == 4 and set(a) <= set(ids)
    assert a != sampling.draw(ids, 4, "seed-2")
    with pytest.raises(ValueError):
        sampling.draw(ids, 11, "s")


def test_precision_counts_only_validated_verdicts():
    assert sampling.precision(
        {"a": True, "b": False, "c": None, "d": True}
    ) == {
        "validated": 3,
        "true": 2,
        "false": 1,
        "precision": 2 / 3,
    }
    assert sampling.precision({"a": None}) == {
        "validated": 0,
        "true": 0,
        "false": 0,
        "precision": None,
    }


# -- report -------------------------------------------------------------------


def _scoped(fid, node, vendor, **kw):
    f = _finding(fid, node, **kw)
    f["vendor"] = vendor
    return f


def test_assemble_reconciles_with_the_ledger_and_groups(graph_db):
    acme = {"node": graph_db["v1"], "name": "Acme Steel"}
    globex = {"node": graph_db["v2"], "name": "<Globex>"}
    scope = [
        _scoped("f1", graph_db["i1"], acme, impact="100", status="recovered"),
        _scoped(
            "f2",
            graph_db["i2"],
            globex,
            impact="50",
            status="accepted",
            type="PAY-BEC",
            details={"hold": True},
        ),
        _scoped(
            "f3",
            graph_db["i3"],
            acme,
            impact="30",
            status="detected",
            type="PAY-BEC",
            details={"hold": True},
        ),
        _scoped(
            "f4",
            graph_db["i1"],
            None,
            impact="7",
            status="rejected",
            type="GST-ITC-RISK",
        ),
        _scoped(
            "f5",
            graph_db["i1"],
            acme,
            impact="9",
            status="triaged",
            type="GST-ITC-RISK",
        ),
    ]
    events = [
        _event("f1", "claimed", "100.0000"),
        _event("f1", "recovered", "90.0000", actor="cfo@x.test"),
        _event("f2", "prevented", "50.0000", type="PAY-BEC"),
        _event("zz", "recovered", "1000"),
    ]
    out = report.assemble(
        _audit(),
        scope,
        events,
        graph=graph_db["graph"],
        run=None,
        now=AT,
        tenant_id=T,
    )
    assert out["report_version"] == 1 and out["generated_at"] == AT.isoformat()
    assert out["summary"] == {
        "findings": 5,
        "detected_impact": "196.00",
        "accepted_impact": "150.00",
        "recovered": "90.00",
        "prevented": "50.00",
        "written_off": "0.00",
        "at_risk_holds": "30.00",
        "currency": "INR",
    }
    assert out["ledger"]["totals"]["recovered"] == "90.00"
    assert out["ledger"]["fee"]["due"] == "27.50"  # 90*25% + 50*10%
    by_type = {r["type"]: r for r in out["by_type"]}
    assert by_type["PAY-BEC"] == {
        "type": "PAY-BEC",
        "engine": "payment",
        "count": 2,
        "impact": "80.00",
        "recovered": "0.00",
        "prevented": "50.00",
    }
    by_vendor = {r["vendor_node"]: r for r in out["by_vendor"]}
    assert by_vendor[graph_db["v1"]]["recovered"] == "90.00"
    assert (
        by_vendor[graph_db["v1"]]["count"] == 3
        and by_vendor[""]["vendor"] is None
    )
    assert (
        out["itc_exposure"]["count"] == 2
        and out["itc_exposure"]["impact"] == "16.00"
    )
    assert [p["engine"] for p in out["prevention"]] == [
        "leakage",
        "payment",
        "provenance",
        "gst_itc",
    ]
    pay = out["prevention"][1]
    assert pay["count"] == 2 and pay["impact"] == "80.00"
    assert pay["mode_suggested"] == "enforcing" and pay["mode_now"] == "off"
    assert "payment.bec_hold_signals" in pay["thresholds_hint"]
    assert "GST-ITC-RISK" in out["prevention"][3]["finding_types"]


def test_assemble_top_recoveries_are_capped_ordered_and_cited(graph_db):
    acme = {"node": graph_db["v1"], "name": "Acme Steel"}
    scope = [
        _scoped(f"f{i}", graph_db["i1"], acme, impact=str(i)) for i in range(25)
    ] + [
        _scoped(
            "gone", graph_db["i1"], acme, impact="999", status="written_off"
        )
    ]
    out = report.assemble(
        _audit(),
        scope,
        [],
        graph=graph_db["graph"],
        run=None,
        now=AT,
        tenant_id=T,
    )
    top = out["top_recoveries"]
    assert len(top) == 20 and [t["id"] for t in top][:3] == [
        "f24",
        "f23",
        "f22",
    ]
    assert top[0]["vendor"] == "Acme Steel" and top[0]["status"] == "detected"
    [cite] = top[0]["citations"]
    assert cite["kind"] == "invoice" and cite["excerpt"] == "inv"
    assert cite["source"] == {
        "document_url": "/api/invoices/c1/document",
        "page": None,
    }


def test_render_escapes_and_prints_every_section(graph_db):
    globex = {"node": graph_db["v2"], "name": "<Globex & Co>"}
    scope = [
        _scoped("f1", graph_db["i2"], globex, impact="5", summary="<b>x</b>")
    ]
    scope[0]["summary"] = "<script>alert(1)</script>"
    out = report.assemble(
        _audit(name="<FY26>"),
        scope,
        [],
        graph=graph_db["graph"],
        run=None,
        now=AT,
        tenant_id=T,
    )
    html = render.report_html(out)
    assert "<script>" not in html and "&lt;Globex &amp; Co&gt;" in html
    for title in (
        "Executive summary",
        "By type",
        "By vendor",
        "Top recoveries",
        "Prevention",
        "Recovery ledger",
    ):
        assert title in html
    assert "f1" not in html  # no ids on the printed page


# -- store --------------------------------------------------------------------


@pytest.fixture
def store(graph_db):
    return TrustAuditStore(graph_db["url"])


def test_store_creates_lists_and_updates_audits(store):
    a = store.create(
        CTX,
        name="FY26",
        period_from=date(2026, 1, 1),
        period_to=None,
        vendors=["v-1"],
        categories=["freight"],
        terms=TERMS,
        approver="cfo@x.test",
    )
    assert a["status"] == "draft" and a["created_by"] == "engine"
    assert a["period_from"] == "2026-01-01" and a["period_to"] is None
    assert a["terms"] == TERMS and a["created_at"] and a["updated_at"]
    assert store.get(T, a["id"]) == a
    assert store.get("tenant-globex", a["id"]) is None
    assert [x["id"] for x in store.list(T)] == [a["id"]]
    assert store.list("tenant-globex") == []
    b = store.update(T, a["id"], status="active", name="FY26 Q1")
    assert b["status"] == "active" and b["name"] == "FY26 Q1"
    assert store.update("tenant-globex", a["id"], name="x") is None


def test_store_runs_progress_and_finish(store):
    a = store.create(CTX, name="FY26", terms=TERMS)
    run = store.create_run(
        CTX, a["id"], engines=["leakage"], requested_by="cfo@x.test"
    )
    assert run["status"] == "queued" and run["audit_id"] == a["id"]
    assert run["progress"] == {
        "engines_done": 0,
        "engines_total": 0,
        "engine": None,
        "findings_so_far": 0,
        "per_engine": {},
    }
    assert store.get_run("tenant-globex", run["id"]) is None
    started = store.start(T, run["id"], now=AT)
    assert (
        started["status"] == "running"
        and started["started_at"] == AT.isoformat()
    )
    store.progress(T, run["id"])(
        {
            "engine": "leakage",
            "done": 1,
            "total": 2,
            "findings": 3,
            "report": {"mode": "advisory", "findings": 3},
        }
    )
    p = store.get_run(T, run["id"])["progress"]
    assert p == {
        "engines_done": 1,
        "engines_total": 2,
        "engine": "leakage",
        "findings_so_far": 3,
        "per_engine": {"leakage": {"mode": "advisory", "findings": 3}},
    }
    done = store.finish(
        T,
        run["id"],
        status="done",
        per_engine={
            "leakage": {"mode": "advisory", "findings": 3},
            "payment": "off",
        },
        now=AT,
    )
    assert done["status"] == "done" and done["finished_at"] == AT.isoformat()
    assert done["progress"]["per_engine"]["payment"] == "off"
    failed = store.finish(
        T, run["id"], status="failed", error="x" * 600, now=AT
    )
    assert failed["status"] == "failed" and len(failed["error"]) == 500
    assert [r["id"] for r in store.runs(T, a["id"])] == [run["id"]]


def test_store_blocking_run_abandons_stale_ones(store):
    a = store.create(CTX, name="FY26", terms=TERMS)
    fresh = store.create_run(CTX, a["id"], engines=[], requested_by="x", now=AT)
    assert (
        store.blocking_run(T, a["id"], now=AT + timedelta(minutes=29))["id"]
        == fresh["id"]
    )
    assert (
        store.blocking_run(T, a["id"], now=AT + timedelta(minutes=31)) is None
    )
    assert store.get_run(T, fresh["id"])["error"] == "abandoned"
    running = store.create_run(
        CTX, a["id"], engines=[], requested_by="x", now=AT
    )
    store.start(T, running["id"], now=AT)
    assert (
        store.blocking_run(T, a["id"], now=AT + timedelta(hours=1, minutes=59))
        is not None
    )
    assert (
        store.blocking_run(T, a["id"], now=AT + timedelta(hours=2, minutes=1))
        is None
    )
    assert store.get_run(T, running["id"])["status"] == "failed"
    store.finish(T, running["id"], status="done", now=AT)  # done never blocks
    assert store.blocking_run(T, a["id"], now=AT) is None


def test_store_samples_and_verdicts(store):
    a = store.create(CTX, name="FY26", terms=TERMS)
    s = store.create_sample(
        CTX, a["id"], size=2, seed="s", finding_ids=["f1", "f2"]
    )
    assert s["verdicts"] == {"f1": None, "f2": None} and s["precision"] is None
    out = store.set_verdicts(T, s["id"], {"f1": True})
    assert out["verdicts"] == {"f1": True, "f2": None}
    assert (out["validated"], out["true"], out["false"], out["precision"]) == (
        1,
        1,
        0,
        1.0,
    )
    assert store.get_sample("tenant-globex", s["id"]) is None
    assert [x["id"] for x in store.samples(T, a["id"])] == [s["id"]]
