"""Leakage engine (FR-20): contract compliance, duplicates, statements, radar."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

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


# -- Task 4: contract rules ---------------------------------------------------


def edge(env, src, dst, kind, provenance="system_record"):
    _, graph, _ = env
    graph.upsert_edge(
        CTX,
        src=src,
        dst=dst,
        kind=kind,
        source_system="test",
        provenance=provenance,
        confidence=1.0,
        observed_at=AT,
        at=AT,
    )


def vendor(env, ref_id="ven-1", name="Acme Steel"):
    _, graph, _ = env
    return graph.upsert_node(
        CTX,
        kind="vendor",
        natural_key=f"vendors:{ref_id}",
        ref_table="vendors",
        ref_id=ref_id,
        attrs={"name": name},
        at=AT,
    )


def invoice(
    env,
    ven,
    number,
    day,
    amount,
    lines=(),
    *,
    live=True,
    terms=None,
    ref=None,
):
    _, graph, _ = env
    ref = ref or f"{ven[:6]}-{number}"
    table = "invoices" if live else "history_records"
    iid = graph.upsert_node(
        CTX,
        kind="invoice",
        natural_key=f"{table}:{ref}",
        ref_table=table,
        ref_id=ref,
        attrs={
            "invoice_number": number,
            "invoice_date": day,
            "amount": str(amount),
            "currency": "INR",
            "payment_terms": terms,
        },
        at=AT,
    )
    edge(env, iid, ven, "issued_by")
    for n, (desc, qty, price) in enumerate(lines, start=1):
        lid = graph.upsert_node(
            CTX,
            kind="invoice_line",
            natural_key=f"invoice_lines:{ref}:{n}",
            ref_table="invoice_lines",
            ref_id=f"{ref}:{n}",
            attrs={
                "line_number": n,
                "description": desc,
                "quantity": str(qty),
                "unit_price": str(price),
                "amount": str(Decimal(str(qty)) * Decimal(str(price))),
            },
            at=AT,
        )
        edge(env, lid, iid, "part_of")
    return iid


def clause(env, contract, kind, params, excerpt, conf=0.95, reviewed=True):
    _, graph, _ = env
    cid = graph.upsert_node(
        CTX,
        kind="contract_clause",
        natural_key=f"clause:{contract}:1:{kind}:{excerpt[:20]}",
        attrs={
            "type": kind,
            "params": params,
            "page": 1,
            "excerpt": excerpt,
            "confidence": conf,
            "reviewed": reviewed,
        },
        at=AT,
    )
    edge(env, cid, contract, "part_of", "model_inference")
    return cid


def steel_contract(
    env,
    ven,
    *,
    doc_id="doc-c",
    start="2025-01-01",
    end="2026-12-31",
    rate="1000",
    governs=True,
    **kw,
):
    cid = contract_node(env, doc_id=doc_id)
    if governs:
        edge(env, cid, ven, "governs", "human_assertion")
    clause(
        env,
        cid,
        "rate",
        {"price": rate},
        f"The rate for Steel Rod 12mm is INR {rate} per unit.",
        **kw,
    )
    clause(
        env,
        cid,
        "term",
        {"start": start, "end": end},
        f"This agreement runs from {start} to {end}.",
        **kw,
    )
    return cid


def found(env, code=None, **kw):
    specs = leakage.run(make_ectx(env, **kw))
    return [s for s in specs if code is None or s.type == code]


def test_rate_overcharge(env):
    ven = vendor(env)
    steel_contract(env, ven)
    inv = invoice(
        env, ven, "INV-1", "2025-03-01", 11000, [("Steel Rod 12mm", 10, 1100)]
    )
    [f] = found(env, "LEAK-RATE")
    assert f.impact == Decimal("1000.00") and f.currency == "INR"
    assert f.transaction_node == inv
    assert f.dedupe_key.startswith("LEAK-RATE:")
    assert f.engine_version == "leakage-1"
    assert f.details["needs_review"] is False
    assert f.confidence == pytest.approx(0.9 * 0.95)
    kinds = {env[1].node(T, c.node_id)["kind"] for c in f.citations}
    assert kinds == {"invoice", "invoice_line", "contract_clause", "contract"}


def test_rate_within_tolerance_is_clean(env):
    ven = vendor(env)
    steel_contract(env, ven)
    invoice(
        env, ven, "INV-1", "2025-03-01", 10040, [("Steel Rod 12mm", 10, 1004)]
    )
    assert found(env, "LEAK-RATE") == []


def test_unmatched_description_is_clean(env):
    ven = vendor(env)
    steel_contract(env, ven)
    invoice(
        env,
        ven,
        "INV-1",
        "2025-03-01",
        50000,
        [("Copper wire 4 core", 10, 5000)],
    )
    assert found(env, "LEAK-RATE") == []


def test_escalator_allows_escalated_price(env):
    ven = vendor(env)
    cid = steel_contract(env, ven)
    clause(env, cid, "escalator", {"pct": "5"}, "Prices escalate by 5% yearly.")
    invoice(
        env, ven, "INV-1", "2026-02-01", 10500, [("Steel Rod 12mm", 10, 1050)]
    )
    assert found(env, "LEAK-RATE") == [] and found(env, "LEAK-ESCALATOR") == []


def test_escalator_overcharge(env):
    ven = vendor(env)
    cid = steel_contract(env, ven)
    clause(env, cid, "escalator", {"pct": "5"}, "Prices escalate by 5% yearly.")
    invoice(
        env, ven, "INV-1", "2026-02-01", 11000, [("Steel Rod 12mm", 10, 1100)]
    )
    [f] = found(env, "LEAK-ESCALATOR")
    assert f.impact == Decimal("500.00")  # (1100 - 1050) x 10
    assert f.details["allowed_unit_price"] == "1050.00"


def test_tier_price_after_cumulative_qty(env):
    ven = vendor(env)
    cid = steel_contract(env, ven)
    clause(
        env,
        cid,
        "tier",
        {"qty": "100", "price": "900"},
        "Above 100 units in a contract year the price is INR 900.",
    )
    invoice(
        env, ven, "INV-1", "2025-02-01", 80000, [("Steel Rod 12mm", 80, 1000)]
    )
    second = invoice(
        env, ven, "INV-2", "2025-03-01", 50000, [("Steel Rod 12mm", 50, 1000)]
    )
    [f] = found(env, "LEAK-TIER")
    assert f.transaction_node == second
    assert f.impact == Decimal("5000.00")  # (1000 - 900) x 50
    assert found(env, "LEAK-RATE") == []


def test_rebate_without_credit(env):
    ven = vendor(env)
    cid = steel_contract(env, ven)
    clause(
        env,
        cid,
        "rebate",
        {"threshold": "100000", "pct": "2"},
        "A rebate of 2% applies once spend exceeds INR 1,00,000.",
    )
    invoice(env, ven, "INV-1", "2025-02-01", 60000)
    second = invoice(env, ven, "INV-2", "2025-03-01", 60000)
    [f] = found(env, "LEAK-REBATE")
    assert f.transaction_node == second
    assert f.impact == Decimal("2400.00")


def statement_line(env, ven, number, day, amount, ref=None):
    _, graph, _ = env
    sid = graph.upsert_node(
        CTX,
        kind="vendor_statement_line",
        natural_key=f"history_records:{ref or number}",
        ref_table="history_records",
        ref_id=ref or number,
        attrs={
            "doc_number": number,
            "invoice_date": day,
            "amount": str(amount),
        },
        at=AT,
    )
    edge(env, sid, ven, "issued_by")
    return sid


def test_rebate_with_credit_is_clean(env):
    ven = vendor(env)
    cid = steel_contract(env, ven)
    clause(
        env,
        cid,
        "rebate",
        {"threshold": "100000", "pct": "2"},
        "A rebate of 2% applies once spend exceeds INR 1,00,000.",
    )
    invoice(env, ven, "INV-1", "2025-02-01", 60000)
    invoice(env, ven, "INV-2", "2025-03-01", 60000)
    statement_line(env, ven, "CN-9", "2025-04-01", -2400)
    assert found(env, "LEAK-REBATE") == []


def test_invoice_outside_term(env):
    ven = vendor(env)
    steel_contract(env, ven, end="2025-06-30")
    inv = invoice(env, ven, "INV-1", "2025-08-01", 1000)
    [f] = found(env, "LEAK-TERM")
    assert f.transaction_node == inv and f.impact is None


def test_governed_by_prefers_covering_contract(env):
    _, graph, _ = env
    ven = vendor(env)
    old = steel_contract(
        env, ven, doc_id="doc-old", start="2024-01-01", end="2024-12-31"
    )
    new = steel_contract(env, ven, doc_id="doc-new")
    early = invoice(env, ven, "INV-1", "2024-06-01", 1000)
    late = invoice(env, ven, "INV-2", "2025-06-01", 1000)
    found(env)
    gov = {e["src"]: e for e in graph.edges(T, kind="governed_by")}
    assert gov[early]["dst"] == old and gov[late]["dst"] == new
    assert gov[late]["provenance"] == "model_inference"


def test_contract_vendor_from_attrs_when_no_governs_edge(env):
    _, graph, _ = env
    ven = vendor(env, ref_id="ven-9")
    cid = contract_node(env, vendor_id="ven-9")
    clause(
        env,
        cid,
        "rate",
        {"price": "1000"},
        "The rate for Steel Rod 12mm is INR 1000 per unit.",
    )
    invoice(
        env, ven, "INV-1", "2025-03-01", 11000, [("Steel Rod 12mm", 10, 1100)]
    )
    assert len(found(env, "LEAK-RATE")) == 1
    assert graph.edges(T, kind="governs") == []


def test_low_confidence_clause_is_ignored(env):
    ven = vendor(env)
    steel_contract(env, ven, conf=0.5, reviewed=False)
    invoice(
        env, ven, "INV-1", "2025-03-01", 11000, [("Steel Rod 12mm", 10, 1100)]
    )
    assert found(env, "LEAK-RATE") == []


def test_unreviewed_clause_marks_needs_review(env):
    ven = vendor(env)
    steel_contract(env, ven, conf=0.8, reviewed=False)
    invoice(
        env, ven, "INV-1", "2025-03-01", 11000, [("Steel Rod 12mm", 10, 1100)]
    )
    [f] = found(env, "LEAK-RATE")
    assert f.details["needs_review"] is True
    assert f.confidence == pytest.approx(0.9 * 0.8)


# -- Task 5: early-payment discount -------------------------------------------


def payment(env, ven, number, day, amount, settles=None, ref=None):
    _, graph, _ = env
    pid = graph.upsert_node(
        CTX,
        kind="payment",
        natural_key=f"history_records:{ref or 'pay-' + number + day}",
        ref_table="history_records",
        ref_id=ref or f"pay-{number}{day}",
        attrs={
            "doc_number": number,
            "invoice_date": day,
            "amount": str(amount),
        },
        at=AT,
    )
    edge(env, pid, ven, "issued_by")
    if settles:
        edge(env, pid, settles, "settles")
    return pid


def test_missed_discount_paid_early_in_full(env):
    ven = vendor(env)
    inv = invoice(env, ven, "INV-1", "2025-03-01", 50000, terms="2/10 net 30")
    pay = payment(env, ven, "INV-1", "2025-03-08", 50000, settles=inv)
    [f] = found(env, "LEAK-DISCOUNT")
    assert f.impact == Decimal("1000.00") and f.transaction_node == inv
    assert pay in {c.node_id for c in f.citations}


def test_discount_taken_is_clean(env):
    ven = vendor(env)
    inv = invoice(env, ven, "INV-1", "2025-03-01", 50000, terms="2/10 net 30")
    payment(env, ven, "INV-1", "2025-03-08", 49000, settles=inv)
    assert found(env, "LEAK-DISCOUNT") == []


def test_late_payment_is_clean(env):
    ven = vendor(env)
    inv = invoice(env, ven, "INV-1", "2025-03-01", 50000, terms="2/10 net 30")
    payment(env, ven, "INV-1", "2025-03-25", 50000, settles=inv)
    assert found(env, "LEAK-DISCOUNT") == []


def test_discount_terms_from_contract_clause(env):
    ven = vendor(env)
    cid = steel_contract(env, ven)
    clause(
        env,
        cid,
        "discount",
        {"pct": "1.5", "days": "15", "net": "45"},
        "A 1.5% discount applies if paid within 15 days, net 45.",
    )
    inv = invoice(env, ven, "INV-1", "2025-03-01", 40000)
    # No settles edge: matched by vendor and normalised number.
    payment(env, ven, "inv 1", "2025-03-10", 40000)
    [f] = found(env, "LEAK-DISCOUNT")
    assert f.impact == Decimal("600.00") and f.transaction_node == inv
    assert f.details["needs_review"] is False
