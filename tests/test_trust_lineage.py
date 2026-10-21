"""Every gap in Contract -> PO -> GRN -> Invoice -> Approval -> Payment -> GL is named."""

from datetime import datetime, timezone

import pytest
from fresh_schema import fresh_schema

from invoice_processing.db import models
from invoice_processing.db.session import tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust.graph.lineage import CHAIN, lineage
from invoice_processing.trust.graph.store import GraphStore

T = "tenant-acme"
CTX = RunContext(tenant_id=T, case_id="", run_id="r", actor="t")
AT = datetime(2026, 10, 14, tzinfo=timezone.utc)


@pytest.fixture
def graph(scratch_db):
    url = scratch_db("lineage.db")
    fresh_schema(url)
    with tenant_session(T, url) as s:
        s.add(models.Tenant(id=T, name="Acme", slug="acme", status="active"))
    return GraphStore(url)


def edge(graph, src, dst, kind, confidence=1.0):
    graph.upsert_edge(
        CTX,
        src=src,
        dst=dst,
        kind=kind,
        source_system="v1",
        provenance="system_record",
        confidence=confidence,
        observed_at=AT,
    )


def build(graph, *, grn=True, same_vendor=True, grn_qty="10"):
    v1 = graph.upsert_node(CTX, kind="vendor", natural_key="v1")
    v2 = graph.upsert_node(CTX, kind="vendor", natural_key="v2")
    po = graph.upsert_node(
        CTX, kind="po", natural_key="po", attrs={"amount": "1000.0000"}
    )
    inv = graph.upsert_node(
        CTX,
        kind="invoice",
        natural_key="inv",
        attrs={"amount": "1000.0000", "quantity": "10"},
    )
    edge(graph, po, v1, "issued_by")
    edge(graph, inv, v1 if same_vendor else v2, "issued_by")
    edge(graph, inv, po, "references")
    if grn:
        g = graph.upsert_node(
            CTX, kind="grn", natural_key="grn", attrs={"qty_received": grn_qty}
        )
        edge(graph, g, po, "fulfils")
    ap = graph.upsert_node(
        CTX, kind="approval", natural_key="ap", attrs={"status": "approved"}
    )
    edge(graph, ap, inv, "approves")
    pay = graph.upsert_node(
        CTX, kind="payment", natural_key="pay", attrs={"amount": "1000.0000"}
    )
    edge(graph, pay, inv, "settles")
    return inv


def by_stage(links):
    return {link.stage: link.status for link in links}


def test_full_chain_is_found_except_contract_and_gl(graph):
    out = lineage(T, build(graph), graph=graph)
    assert [link.stage for link in out] == list(CHAIN)
    assert by_stage(out) == {
        "contract": "no_evidence",
        "po": "found",
        "grn": "found",
        "invoice": "found",
        "approval": "found",
        "payment": "found",
        "gl": "no_evidence",
    }
    contract = next(link for link in out if link.stage == "contract")
    assert contract.note == "No contract governs this invoice."


def contract_for(graph, inv, vendor):
    c = graph.upsert_node(CTX, kind="contract", natural_key="msa")
    edge(graph, c, vendor, "governs")
    edge(graph, inv, c, "governed_by")
    return c


def test_governed_invoice_finds_its_contract(graph):
    inv = build(graph)
    v1 = graph.open_node(T, "vendor", "v1")["id"]
    c = contract_for(graph, inv, v1)
    contract = lineage(T, inv, graph=graph)[0]
    assert (contract.status, contract.node_id) == ("found", c)


def test_contract_with_another_vendor_contradicts(graph):
    inv = build(graph)
    v2 = graph.open_node(T, "vendor", "v2")["id"]
    contract_for(graph, inv, v2)
    contract = lineage(T, inv, graph=graph)[0]
    assert contract.status == "contradicts"
    assert "different vendor" in contract.note


def test_missing_grn_is_no_evidence(graph):
    out = lineage(T, build(graph, grn=False), graph=graph)
    assert by_stage(out)["grn"] == "no_evidence"


def test_po_for_other_vendor_contradicts(graph):
    out = lineage(T, build(graph, same_vendor=False), graph=graph)
    po = next(link for link in out if link.stage == "po")
    assert po.status == "contradicts" and "vendor" in po.note


def test_short_receipt_contradicts(graph):
    out = lineage(T, build(graph, grn_qty="4"), graph=graph)
    grn = next(link for link in out if link.stage == "grn")
    assert grn.status == "contradicts" and "4" in grn.note


def test_unknown_node_is_an_error(graph):
    with pytest.raises(KeyError):
        lineage(T, "nope", graph=graph)


def test_quantities_come_from_lines_on_a_synced_graph(graph):
    inv = build(graph, grn=False)
    po = next(e["dst"] for e in graph.edges(T, src=inv, kind="references"))
    line = graph.upsert_node(
        CTX, kind="invoice_line", natural_key="il", attrs={"quantity": "10"}
    )
    edge(graph, line, inv, "part_of")
    g = graph.upsert_node(CTX, kind="grn", natural_key="g")
    edge(graph, g, po, "fulfils")
    gl = graph.upsert_node(
        CTX, kind="grn_line", natural_key="gl", attrs={"qty_received": "4"}
    )
    edge(graph, gl, g, "part_of")
    out = lineage(T, inv, graph=graph)
    grn = next(link for link in out if link.stage == "grn")
    assert grn.status == "contradicts" and "4" in grn.note and "10" in grn.note


def test_an_imported_receipt_counts_by_its_quantity(graph):
    """A history GRN carries `quantity`, not v1's `qty_received`."""
    inv = build(graph, grn=False)
    po = graph.open_node(T, "po", "po")["id"]
    line = graph.upsert_node(
        CTX, kind="invoice_line", natural_key="il", attrs={"quantity": "10"}
    )
    edge(graph, line, inv, "part_of")
    g = graph.upsert_node(
        CTX, kind="grn", natural_key="hgrn", attrs={"quantity": "4"}
    )
    edge(graph, g, po, "fulfils")
    [grn] = [x for x in lineage(T, inv, graph=graph) if x.stage == "grn"]
    assert grn.status == "contradicts" and "Received 4" in grn.note
