"""Trust Audit (Wave 2): ledger + fee, scope and sampling, report, store, batch."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from invoice_processing.db import models
from invoice_processing.db.session import create_all, get_engine, tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust.audit import ledger, sampling
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
