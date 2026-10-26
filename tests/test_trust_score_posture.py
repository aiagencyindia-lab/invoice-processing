"""Posture and the vendor trust profile: computed on read."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fresh_schema import fresh_schema

from invoice_processing.db import models
from invoice_processing.db.session import tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust import holds as holds_mod
from invoice_processing.trust.audit import ledger
from invoice_processing.trust.findings.model import Citation, FindingSpec
from invoice_processing.trust.findings.store import FindingStore
from invoice_processing.trust.graph.links import LinkStore
from invoice_processing.trust.graph.store import GraphStore
from invoice_processing.trust.score import posture, vendor

T = "tenant-acme"
CTX = RunContext(tenant_id=T, case_id="", run_id="r", actor="engine")
AT = datetime(2026, 10, 1, tzinfo=UTC)
GSTIN = "29ABCDE1234F1Z5"


def dims(composite, **values):
    return {
        "composite": composite,
        "computed_at": AT.isoformat(),
        "dimensions": {
            d: {"value": values.get(d)}
            for d in (
                "identity",
                "authorization",
                "reality",
                "price",
                "authenticity",
                "consistency",
            )
        },
    }


@pytest.fixture
def env(scratch_db):
    url = scratch_db("posture.db")
    fresh_schema(url)
    with tenant_session(T, url) as s:
        s.add(models.Tenant(id=T, name="Acme", slug="acme", status="active"))
    g, store = GraphStore(url), FindingStore(url)
    v = g.upsert_node(
        CTX,
        kind="vendor",
        natural_key="vendors:v1",
        attrs={"name": "Acme Steel"},
        natural_keys={"gstin": GSTIN, "pan": GSTIN[2:12], "name": "acme steel"},
    )
    twin = g.upsert_node(
        CTX,
        kind="vendor",
        natural_key="vendors:v2",
        attrs={"name": "Acme Steel Two", "gstin": "27AAACA9999A1Z1"},
        natural_keys={"pan": GSTIN[2:12], "name": "acme steel two"},
    )
    g.upsert_node(
        CTX,
        kind="vendor",
        natural_key="vendors:v3",
        attrs={"name": "Other"},
        natural_keys={"name": "other"},
    )
    invs = []
    for i, (date, comp) in enumerate(
        (("2026-08-10", 0.5), ("2026-09-02", 0.9), ("2026-09-20", 0.7))
    ):
        nid = g.upsert_node(
            CTX,
            kind="invoice",
            natural_key=f"invoices:i{i}",
            ref_table="invoices",
            ref_id=f"i{i}",
            attrs={"amount": "1000.00", "invoice_date": date},
        )
        g.upsert_edge(
            CTX,
            src=nid,
            dst=v,
            kind="issued_by",
            source_system="t",
            provenance="system_record",
            confidence=1.0,
            observed_at=AT,
        )
        g.upsert_node(
            CTX,
            kind="trust_score",
            natural_key=f"trust_score:invoices:i{i}",
            attrs=dims(comp, identity=comp, reality=None if i else 0.4),
        )
        invs.append(nid)
    ev = g.upsert_node(
        CTX,
        kind="provider_evidence",
        natural_key="gstin:abc",
        attrs={
            "capability": "gstin",
            "status": "ok",
            "observed_at": AT.isoformat(),
            "data": {"status": "active", "missed_periods": 3},
        },
    )
    bank = g.upsert_node(
        CTX,
        kind="provider_evidence",
        natural_key="bank_verification:abc",
        attrs={
            "capability": "bank_verification",
            "status": "ok",
            "observed_at": AT.isoformat(),
            "data": {"result": "unverified"},
        },
    )
    kyc = g.upsert_node(
        CTX,
        kind="document_version",
        natural_key="documents:k1:x",
        ref_table="documents",
        ref_id="k1",
        attrs={"kind": "kyc", "subkind": "pan", "score": 0.3},
    )
    g.upsert_edge(
        CTX,
        src=kyc,
        dst=v,
        kind="evidences",
        source_system="provenance",
        provenance="system_record",
        confidence=1.0,
        observed_at=AT,
    )
    for k, score in (("a", 0.95), ("b", 0.15)):
        g.upsert_node(
            CTX,
            kind="document_version",
            natural_key=f"documents:{k}:x",
            attrs={"score": score},
        )

    def finding(code, node, impact, cites=()):
        fid, _ = store.upsert(
            CTX,
            FindingSpec(
                type=code,
                transaction_node=node,
                dedupe_key=f"{code}:{node}",
                impact=Decimal(impact) if impact else None,
                currency="INR",
                confidence=1.0,
                engine_version="e-1",
                rule_version="r",
                model_version="none",
                summary="s",
                details={},
                citations=(Citation(node), *(Citation(c) for c in cites)),
            ),
        )
        return fid

    nonfiler = finding("GST-NONFILER", invs[0], None, cites=(ev,))
    unverified = finding("PAY-UNVERIFIED", invs[0], "1000.00", cites=(bank,))
    rate = finding("LEAK-RATE", invs[1], "120.00")
    dup = finding("DUP-NEAR", invs[2], "300.00")
    for fid, steps in (
        (
            rate,
            [
                ("triaged", None),
                ("accepted", None),
                ("claimed", None),
                ("recovered", Decimal("100.00")),
            ],
        ),
        (
            unverified,
            [
                ("triaged", None),
                ("accepted", None),
                ("prevented", Decimal("1000.00")),
            ],
        ),
        (dup, [("triaged", None), ("rejected", None)]),
    ):
        for to, amount in steps:
            store.transition(
                CTX, fid, to, actor="a@x", reason="r", amount=amount
            )
    with tenant_session(T, url) as s:
        row = s.get(models.TrustFinding, unverified)
        holds_mod.place(s, CTX, row)
        holds_mod.place(s, CTX, s.get(models.TrustFinding, nonfiler))
        holds_mod.lift(s, CTX, nonfiler, by="a@x", reason="r")
    return {
        "url": url,
        "graph": g,
        "findings": store,
        "vendor": v,
        "twin": twin,
        "links": LinkStore(url),
        "ids": {"nonfiler": nonfiler, "rate": rate, "dup": dup},
    }


def test_posture_money_reconciles_with_the_ledger(env):
    p = posture.summary(
        T, graph=env["graph"], findings=env["findings"], url=env["url"]
    )
    events = env["findings"].events(T)
    totals = ledger.totals(
        ledger.rows({e["finding_id"] for e in events}, events)
    )
    assert p["prevented"] == totals["prevented"] == "1000.00"
    assert p["recovered"] == totals["recovered"] == "100.00"
    assert p["written_off"] == "0.00"
    assert p["found"] == {"count": 4, "impact": "1420.00"}
    assert p["open_exposure"] == "0.00"  # nonfiler has no impact, rest decided
    assert p["by_type"]["LEAK-RATE"] == {"count": 1, "impact": "120.00"}
    assert p["by_vendor"][0]["name"] == "Acme Steel"
    assert p["by_vendor"][0]["count"] == 4
    assert p["pre_payment_share"] == round(1000 / 1100, 4)
    assert p["holds"] == {"open": 1, "placed": 2, "lifted": 1}
    assert p["provenance_distribution"] == {
        "0.0-0.2": 1,
        "0.2-0.4": 0,
        "0.4-0.6": 0,
        "0.6-0.8": 0,
        "0.8-1.0": 1,
    }
    assert p["verification_coverage"] == {
        "identity": 1.0,
        "reality": round(1 / 3, 4),
        "authenticity": 0.0,
    }


def test_posture_since_bounds_what_moved(env):
    later = datetime.now(UTC) + timedelta(days=1)
    p = posture.summary(
        T,
        graph=env["graph"],
        findings=env["findings"],
        url=env["url"],
        since=later,
    )
    assert p["found"]["count"] == 0 and p["prevented"] == "0.00"
    assert p["pre_payment_share"] is None
    assert p["holds"] == {"open": 1, "placed": 0, "lifted": 0}


def test_an_empty_tenant_has_a_posture(scratch_db):
    url = scratch_db("empty.db")
    fresh_schema(url)
    p = posture.summary(
        T, graph=GraphStore(url), findings=FindingStore(url), url=url
    )
    assert p["found"] == {"count": 0, "impact": "0.00"}
    assert p["verification_coverage"] == dict.fromkeys(
        ("identity", "reality", "authenticity")
    )


def test_vendor_profile(env):
    env["links"].propose_or_link(
        CTX, env["vendor"], env["twin"], 0.7, "name", reason="similar"
    )
    p = vendor.profile(
        T,
        env["vendor"],
        graph=env["graph"],
        findings=env["findings"],
        links=env["links"],
        url=env["url"],
    )
    assert p["vendor"]["name"] == "Acme Steel" and p["vendor"]["scored"] == 3
    assert p["dimensions"]["identity"] == 0.7
    assert p["dimensions"]["reality"] == 0.4
    assert p["dimensions"]["price"] is None
    assert p["findings_by_type"]["GST-NONFILER"] == {"open": 1, "decided": 0}
    assert p["findings_by_type"]["DUP-NEAR"] == {"open": 0, "decided": 1}
    assert p["gst"] == {
        "status": "active",
        "missed_periods": 3,
        "observed_at": AT.isoformat(),
    }
    assert p["bank_verified"] is False
    assert [k["subkind"] for k in p["kyc"]] == ["pan"]
    (twin,) = p["network"]
    assert twin["vendor"]["name"] == "Acme Steel Two"
    assert twin["vendor"]["tax_id"] == "27AAACA9999A1Z1"
    assert "pan" in twin["shared"]
    assert any(s.startswith("link:") for s in twin["shared"])
    assert p["timeline"] == [
        {"month": "2026-08", "composite": 0.5},
        {"month": "2026-09", "composite": 0.8},
    ]


def test_an_unknown_or_non_vendor_node_has_no_profile(env):
    kw = {"graph": env["graph"], "findings": env["findings"], "url": env["url"]}
    assert vendor.profile(T, "nope", **kw) is None
    invoice = env["graph"].nodes(T, kind="invoice")[0]["id"]
    assert vendor.profile(T, invoice, **kw) is None


def test_vendor_profile_reads_invoices_and_documents_in_bulk(env, monkeypatch):
    calls = []
    real = env["graph"].node
    monkeypatch.setattr(
        env["graph"], "node", lambda *a, **k: calls.append(a) or real(*a, **k)
    )
    p = vendor.profile(
        T,
        env["vendor"],
        graph=env["graph"],
        findings=env["findings"],
        url=env["url"],
    )
    assert p["vendor"]["invoices"] == 3 and len(p["kyc"]) == 1
    assert len(calls) == 1  # the vendor itself


def test_a_rejected_link_is_not_network_evidence(env):
    link = env["links"].propose_or_link(
        CTX, env["vendor"], env["twin"], 0.7, "name", reason="similar"
    )
    env["links"].decide(CTX, link["id"], accept=False, actor="a@x")
    p = vendor.profile(
        T,
        env["vendor"],
        graph=env["graph"],
        findings=env["findings"],
        links=env["links"],
        url=env["url"],
    )
    (twin,) = p["network"]
    assert twin["shared"] == ["pan"]
