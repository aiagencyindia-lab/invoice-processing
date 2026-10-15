"""Leakage engine (FR-20): contract compliance, duplicates, statements, radar."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from invoice_processing.db import models
from invoice_processing.db.session import create_all, get_engine, tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust import leakage
from invoice_processing.trust.decide import FakeDecider
from invoice_processing.trust.engine import EngineContext
from invoice_processing.trust.findings import model as finding_model
from invoice_processing.trust.graph.links import LinkStore
from invoice_processing.trust.graph.store import GraphStore
from invoice_processing.trust.leakage import clauses

T = "tenant-leak"
CTX = RunContext(tenant_id=T, case_id="", run_id="r", actor="worker")
NOW = datetime(2025, 9, 1, tzinfo=timezone.utc)
AT = datetime(2025, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def env(scratch_db):
    url = scratch_db("leakage.db")
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
    with tenant_session(T, url) as s:
        s.add(models.Tenant(id=T, name="Leak", slug="leak", status="active"))
    return url, GraphStore(url), LinkStore(url)


def make_ectx(
    env,
    *,
    document=None,
    invoice_node=None,
    decider=None,
    blobs=None,
    settings=None,
    now=NOW,
):
    url, graph, links = env
    return EngineContext(
        ctx=CTX,
        mode="advisory",
        document=document,
        invoice_node=invoice_node,
        graph=graph,
        links=links,
        findings=None,
        providers=None,
        settings=settings or {},
        decider=decider or FakeDecider({}, fail=True),
        blobs=blobs,
        url=url,
        now=now,
    )


# -- Task 1: skeleton --------------------------------------------------------


def test_types_are_registered_to_leakage_and_none_hold():
    codes = {
        "LEAK-RATE",
        "LEAK-ESCALATOR",
        "LEAK-TIER",
        "LEAK-REBATE",
        "LEAK-DISCOUNT",
        "LEAK-TERM",
        "DUP-NEAR",
        "DUP-SPLIT",
        "DUP-CROSS",
        "STMT-UNAPPLIED-CREDIT",
        "STMT-DOUBLE-PAID",
        "STMT-DISPUTED",
        "RADAR-RENEWAL",
        "RADAR-ESCALATOR",
    }
    assert leakage.ENGINE_VERSION == "leakage-1"
    for code in codes:
        assert finding_model.TYPE_ENGINE[code] == "leakage"
        assert code not in finding_model.HOLD_TYPES


def test_empty_graph_finds_nothing(env):
    assert leakage.run(make_ectx(env)) == []


# -- Task 2: clause params by regex ------------------------------------------


def test_params_rate():
    assert clauses.params(
        "rate", "The rate for Steel Rod 12mm is INR 1,000.50 per unit."
    ) == {"price": "1000.50"}
    assert clauses.params("rate", "Price: ₹ 2,50,000 per unit") == {
        "price": "250000"
    }


def test_params_escalator():
    assert clauses.params(
        "escalator", "Prices escalate by 5% on each anniversary."
    ) == {"pct": "5"}


def test_params_tier():
    assert clauses.params(
        "tier", "Above 500 units in a contract year the price is Rs. 900."
    ) == {"qty": "500", "price": "900"}


def test_params_rebate():
    assert clauses.params(
        "rebate", "A rebate of 2.5% applies once spend exceeds INR 10,00,000."
    ) == {"threshold": "1000000", "pct": "2.5"}


def test_params_discount_terms():
    assert clauses.params("discount", "Payment terms 2/10 net 30.") == {
        "pct": "2",
        "days": "10",
        "net": "30",
    }
    assert clauses.params(
        "discount", "A 1.5% discount applies if paid within 15 days, net 45."
    ) == {"pct": "1.5", "days": "15", "net": "45"}


def test_params_term_dates():
    assert clauses.params(
        "term", "This agreement runs from 01/04/2025 to 31 March 2026."
    ) == {"start": "2025-04-01", "end": "2026-03-31"}
    assert clauses.params("term", "Effective 2025-01-01 until 2025-12-31.") == {
        "start": "2025-01-01",
        "end": "2025-12-31",
    }


def test_params_unparseable_is_none():
    assert clauses.params("rate", "The rate is agreed separately.") is None
    assert clauses.params("tier", "Tiered pricing applies at INR 5.") is None
    assert clauses.params("term", "Starts on 2025-01-01.") is None
    assert clauses.params("none", "INR 5") is None


def test_candidates_pick_money_percent_and_keywords():
    text = (
        "MASTER SUPPLY AGREEMENT\nThe parties agree as follows.\n"
        "The rate for Steel Rod is Rs. 1,000 per unit. "
        "Prices escalate by 5% yearly.\nGoverning law is Maharashtra."
    )
    assert clauses.candidates(text) == [
        "The rate for Steel Rod is Rs. 1,000 per unit.",
        "Prices escalate by 5% yearly.",
    ]


def test_bank_numbers_are_masked_in_excerpts():
    text = "Pay INR 500 to account 123456789012 at IFSC HDFC0001234."
    [cand] = clauses.candidates(text)
    assert "123456789012" not in cand and "…9012" in cand


# -- Task 3: clause extraction -----------------------------------------------

FILLER = (
    "This master supply agreement is made between the buyer and the supplier "
    "named in the schedule and records the commercial terms both accept."
)


def make_pdf(pages: list[list[str]]) -> bytes:
    """A text-layer PDF, one Helvetica line per string, no dependencies."""

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None, None]
    objs[2] = "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    kids = []
    for lines in pages:
        body = "".join(
            f"BT /F1 9 Tf 40 {780 - 14 * i} Td ({esc(line)}) Tj ET\n"
            for i, line in enumerate(lines)
        )
        objs.append(f"<< /Length {len(body)} >>\nstream\n{body}endstream")
        content = len(objs)
        objs.append(
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content} 0 R >>"
        )
        kids.append(f"{len(objs)} 0 R")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out = b"%PDF-1.4\n"
    offsets = []
    for n, obj in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n{obj}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return out


class Blobs:
    def __init__(self, data: dict[str, bytes]):
        self.data = data

    def get(self, key: str) -> bytes:
        return self.data[key]


CONTRACT_PAGE = [
    FILLER,
    "The rate for Steel Rod 12mm is INR 1,000 per unit.",
    "Prices escalate by 5% on each anniversary of the start date.",
    "This agreement runs from 2025-01-01 to 2026-12-31.",
    "Governing law is the law of Maharashtra.",
]
CONTRACT_ANSWERS = {
    "c0": "rate",
    "c0_score": 0.95,
    "c1": "escalator",
    "c1_score": 0.9,
    "c2": "term",
    "c2_score": 0.99,
}


def contract_node(env, doc_id="doc-c", vendor_id=None):
    _, graph, _ = env
    return graph.upsert_node(
        CTX,
        kind="contract",
        natural_key=f"documents:{doc_id}",
        ref_table="documents",
        ref_id=doc_id,
        attrs={"filename": "msa.pdf", "vendor_id": vendor_id},
        at=AT,
    )


def contract_doc(doc_id="doc-c"):
    return {"id": doc_id, "kind": "contract", "storage_key": f"k/{doc_id}"}


def extract(env, pages, answers, **kw):
    decider = FakeDecider(answers)
    ectx = make_ectx(
        env,
        document=contract_doc(),
        decider=decider,
        blobs=Blobs({"k/doc-c": make_pdf(pages)}),
        **kw,
    )
    return clauses.extract(ectx), decider


def test_contract_document_extracts_clauses_from_pdf(env):
    _, graph, _ = env
    cid = contract_node(env)
    ids, decider = extract(env, [CONTRACT_PAGE], CONTRACT_ANSWERS)
    assert len(ids) == 3 and len(decider.prompts) == 1
    nodes = {
        n["attrs"]["type"]: n for n in graph.nodes(T, kind="contract_clause")
    }
    rate = nodes["rate"]["attrs"]
    assert rate["params"] == {"price": "1000"}
    assert rate["page"] == 1 and rate["reviewed"] is False
    assert rate["confidence"] == 0.95
    assert "Steel Rod" in rate["excerpt"]
    assert nodes["term"]["attrs"]["params"] == {
        "start": "2025-01-01",
        "end": "2026-12-31",
    }
    assert nodes["rate"]["natural_key"].startswith(f"clause:{cid}:1:")
    edges = graph.edges(T, kind="part_of", dst=cid)
    assert len(edges) == 3
    assert {e["provenance"] for e in edges} == {"model_inference"}


def test_model_fallback_writes_no_clause(env):
    _, graph, _ = env
    contract_node(env)
    decider = FakeDecider({}, fail=True)
    ectx = make_ectx(
        env,
        document=contract_doc(),
        decider=decider,
        blobs=Blobs({"k/doc-c": make_pdf([CONTRACT_PAGE])}),
    )
    assert clauses.extract(ectx) == []
    assert graph.nodes(T, kind="contract_clause") == []


def test_max_pages_caps_decide_calls(env):
    contract_node(env)
    _, decider = extract(
        env,
        [CONTRACT_PAGE] * 3,
        CONTRACT_ANSWERS,
        settings={"trust_thresholds": {"leakage.max_pages": 2}},
    )
    assert len(decider.prompts) == 2


def test_reviewed_clause_is_not_overwritten(env):
    _, graph, _ = env
    contract_node(env)
    extract(env, [CONTRACT_PAGE], CONTRACT_ANSWERS)
    rate = next(
        n
        for n in graph.nodes(T, kind="contract_clause")
        if n["attrs"]["type"] == "rate"
    )
    graph.upsert_node(
        CTX,
        kind="contract_clause",
        natural_key=rate["natural_key"],
        attrs={**rate["attrs"], "reviewed": True, "confidence": 1.0},
    )
    extract(env, [CONTRACT_PAGE], {**CONTRACT_ANSWERS, "c0_score": 0.5})
    again = graph.open_node(T, "contract_clause", rate["natural_key"])
    assert again["attrs"]["reviewed"] is True
    assert again["attrs"]["confidence"] == 1.0


def test_model_never_supplies_params(env):
    _, graph, _ = env
    contract_node(env)
    extract(
        env,
        [CONTRACT_PAGE],
        {"c0": "rate", "c0_score": 0.9, "c0_params": {"price": "1"}},
    )
    [rate] = graph.nodes(T, kind="contract_clause")
    assert rate["attrs"]["params"] == {"price": "1000"}
