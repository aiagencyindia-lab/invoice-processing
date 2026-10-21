"""v1 rows and imported history become one graph; engines read one shape."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fresh_schema import fresh_schema

from invoice_processing.db import models
from invoice_processing.db.session import tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust.decide import FakeDecider
from invoice_processing.trust.graph import resolve
from invoice_processing.trust.graph import sync as sync_module
from invoice_processing.trust.graph.links import LinkStore
from invoice_processing.trust.graph.store import GraphStore
from invoice_processing.trust.graph.sync import sync_tenant
from invoice_processing.trust.graph.transactions import transactions
from invoice_processing.trust.history.importer import HistoryImporter

T = "tenant-acme"
CTX = RunContext(tenant_id=T, case_id="", run_id="r", actor="sync")
NOW = datetime(2026, 10, 14, tzinfo=timezone.utc)
GSTIN = "27AAPFU0939F1ZV"


@pytest.fixture
def env(scratch_db):
    url = scratch_db("sync.db")
    fresh_schema(url)
    with tenant_session(T, url) as s:
        s.add(models.Tenant(id=T, name="Acme", slug="acme", status="active"))
        s.flush()
        s.add(
            models.Document(
                tenant_id=T,
                id="doc-1",
                sha256="a" * 64,
                storage_key="documents/a.pdf",
                filename="a.pdf",
            )
        )
        s.add(
            models.Vendor(
                tenant_id=T,
                id="ven-1",
                name="Acme Steel Pvt Ltd",
                normalized_name="acme steel",
                tax_id=GSTIN,
            )
        )
        s.add(
            models.PurchaseOrder(
                tenant_id=T,
                id="po-1",
                po_number="PO1",
                po_number_display="PO-1",
                vendor_name="Acme Steel Pvt Ltd",
                total_amount=Decimal("1000"),
                currency="INR",
                status="open",
                uploaded_by="clerk@x.test",
            )
        )
        s.flush()
        s.add(
            models.PoLine(
                tenant_id=T,
                id="pol-1",
                purchase_order_id="po-1",
                line_number=1,
                description="bolts",
                qty_ordered=Decimal("10"),
                unit_price=Decimal("100"),
            )
        )
        s.add(
            models.GoodsReceipt(
                tenant_id=T,
                id="grn-1",
                grn_number="GRN1",
                purchase_order_id="po-1",
                received_on="2026-01-04",
                recorded_by="clerk@x.test",
            )
        )
        s.flush()
        s.add(
            models.GrnLine(
                tenant_id=T,
                id="grnl-1",
                goods_receipt_id="grn-1",
                po_line_id="pol-1",
                qty_received=Decimal("10"),
            )
        )
        s.add(
            models.Invoice(
                tenant_id=T,
                id="inv-1",
                document_id="doc-1",
                case_id="case-1",
                invoice_number="INV-7",
                invoice_date="2026-01-05",
                po_number="PO-1",
                currency="INR",
                vendor_name="ACME STEEL",
                vendor_tax_id=GSTIN,
                total_inc_tax=Decimal("1000"),
                status="done",
            )
        )
    return url, GraphStore(url), LinkStore(url)


def sync(env, **kw):
    url, graph, links = env
    kw.setdefault("decider", FakeDecider({}))
    return sync_tenant(CTX, graph, links, url=url, **kw)


def test_sync_twice_creates_no_new_rows(env):
    _, graph, _ = env
    first = sync(env)
    assert first["invoice"] == 1 and first["vendor"] == 1
    assert first["po_line"] == 1 and first["grn"] == 1
    before = (len(graph.nodes(T)), len(graph.edges(T)))
    sync(env)
    assert (len(graph.nodes(T)), len(graph.edges(T))) == before


def test_invoice_links_to_vendor_po_and_grn(env):
    _, graph, _ = env
    sync(env)
    inv = graph.nodes_by_ref(T, "invoices", ["inv-1"])["inv-1"]
    ven = graph.nodes_by_ref(T, "vendors", ["ven-1"])["ven-1"]
    po = graph.nodes_by_ref(T, "purchase_orders", ["po-1"])["po-1"]
    grn = graph.nodes_by_ref(T, "goods_receipts", ["grn-1"])["grn-1"]
    assert [e["dst"] for e in graph.edges(T, src=inv, kind="issued_by")] == [
        ven
    ]
    assert [e["dst"] for e in graph.edges(T, src=inv, kind="references")] == [
        po
    ]
    assert [e["dst"] for e in graph.edges(T, src=grn, kind="fulfils")] == [po]
    issued = graph.edges(T, src=inv, kind="issued_by")[0]
    assert issued["provenance"] == "system_record"


def test_history_and_live_invoices_share_one_transaction_shape(env):
    url, graph, _ = env
    csv = b"Bill No,Supplier,Amount,Date\nINV-OLD,Acme Steel Pvt Ltd,500,2025-03-01\n"
    HistoryImporter(url).import_file(
        CTX,
        kind="ap_invoice",
        source_name="tally",
        filename="a.csv",
        data=csv,
        mapping={
            "Bill No": "doc_number",
            "Supplier": "vendor_key",
            "Amount": "amount",
            "Date": "record_date",
        },
    )
    sync(env)
    txns = sorted(transactions(T, graph=graph), key=lambda t: t.doc_number)
    assert [(t.doc_number, t.source, t.amount, t.date) for t in txns] == [
        ("INV-7", "live", Decimal("1000.0000"), date(2026, 1, 5)),
        ("INV-OLD", "history", Decimal("500.0000"), date(2025, 3, 1)),
    ]
    # The same vendor, by name, through resolution.
    assert txns[0].vendor_key == txns[1].vendor_key
    sources = {e["source_system"] for e in graph.edges(T, kind="issued_by")}
    assert sources == {"v1", "history:tally"}


def test_incremental_sync_touches_only_that_invoice(env):
    _, graph, _ = env
    out = sync(env, document_id="doc-1")
    assert out["invoice"] == 1 and out.get("po", 0) == 0
    assert graph.nodes_by_ref(T, "purchase_orders", ["po-1"]) == {}


def test_as_of_excludes_rows_closed_before_date(env):
    url, graph, _ = env
    sync(env, at=NOW)
    later = NOW + timedelta(days=1)
    with tenant_session(T, url) as s:
        s.get(models.Invoice, "inv-1").total_inc_tax = Decimal("1200")
    sync(env, at=later)
    assert transactions(T, graph=graph)[0].amount == Decimal("1200.0000")
    assert transactions(T, graph=graph, as_of=NOW)[0].amount == Decimal(
        "1000.0000"
    )


def test_invoice_with_a_conflicting_gstin_does_not_match_the_master_by_name(
    env,
):
    url, graph, _ = env
    with tenant_session(T, url) as s:
        s.add(
            models.Invoice(
                tenant_id=T,
                id="inv-2",
                document_id="doc-1",
                case_id="case-2",
                invoice_number="INV-8",
                currency="INR",
                vendor_name="Acme Steel Pvt Ltd",
                vendor_tax_id="29AABCU9603R1ZX",
                total_inc_tax=Decimal("10"),
                status="done",
            )
        )
    sync(env)
    inv = graph.nodes_by_ref(T, "invoices", ["inv-2"])["inv-2"]
    master = graph.nodes_by_ref(T, "vendors", ["ven-1"])["ven-1"]
    [edge] = graph.edges(T, src=inv, kind="issued_by")
    assert edge["dst"] != master
    assert graph.node(T, edge["dst"])["ref_table"] is None  # synthesised


def test_reversioning_a_node_closes_its_stale_edges(env):
    url, graph, _ = env
    sync(env, at=NOW)
    with tenant_session(T, url) as s:
        s.get(models.Invoice, "inv-1").total_inc_tax = Decimal("1200")
    sync(env, at=NOW + timedelta(days=1))
    inv = graph.nodes_by_ref(T, "invoices", ["inv-1"])["inv-1"]
    open_issued = [
        e for e in graph.edges(T, kind="issued_by") if e["src"] != inv
    ]
    live_invoice_edges = [
        e
        for e in open_issued
        if graph.node(T, e["src"])["ref_table"] == "invoices"
    ]
    assert live_invoice_edges == []
    assert len(graph.edges(T, src=inv, kind="issued_by")) == 1


def test_incremental_sync_references_a_po_known_only_from_history(env):
    url, graph, _ = env
    HistoryImporter(url).import_file(
        CTX,
        kind="po",
        source_name="tally",
        filename="po.csv",
        data=b"PO,Supplier\nPO-77,Acme Steel Pvt Ltd\n",
        mapping={"PO": "doc_number", "Supplier": "vendor_key"},
    )
    sync(env)
    with tenant_session(T, url) as s:
        s.get(models.Invoice, "inv-1").po_number = "PO-77"
    sync(env, document_id="doc-1")
    inv = graph.nodes_by_ref(T, "invoices", ["inv-1"])["inv-1"]
    [ref] = graph.edges(T, src=inv, kind="references")
    assert graph.node(T, ref["dst"])["attrs"]["doc_number"] == "PO-77"


def test_invoice_node_carries_document_and_tax_attrs(env):
    url, graph, _ = env
    with tenant_session(T, url) as s:
        doc = s.get(models.Document, "doc-1")
        doc.source, doc.source_ref = "email", "email/x.eml"
        doc.sender, doc.reply_to = "billing@acme.in", "pay@acrne.in"
        inv = s.get(models.Invoice, "inv-1")
        inv.total_ex_tax, inv.tax_amount = Decimal("900"), None
        inv.payment_terms, inv.due_date = "2/10 net 30", "04/02/2026"
        inv.fields = {
            "extraction": {
                "invoice": {
                    "cgst_amount": "45",
                    "sgst_amount": 45.0,
                    "place_of_supply": "27-Maharashtra",
                    "customer_tax_id": "27AAPFU0939F1ZV",
                    "discount": "5",
                }
            }
        }
    sync(env)
    node = graph.node(T, graph.nodes_by_ref(T, "invoices", ["inv-1"])["inv-1"])
    a = node["attrs"]
    assert (a["document_id"], a["document_sha256"]) == ("doc-1", "a" * 64)
    assert (a["document_source"], a["document_source_ref"]) == (
        "email",
        "email/x.eml",
    )
    # Domains, not addresses: the engines need the domain and the graph
    # keeps no personal email.
    assert (a["document_sender_domain"], a["document_reply_to_domain"]) == (
        "acme.in",
        "acrne.in",
    )
    assert "document_sender" not in a and "document_reply_to" not in a
    assert (a["payment_terms"], a["due_date"]) == ("2/10 net 30", "2026-02-04")
    assert (a["total_ex_tax"], a["tax_amount"]) == ("900.0000", "90.0000")
    assert a["place_of_supply"] == "27-Maharashtra"
    assert (
        a["customer_tax_id"] == "27AAPFU0939F1ZV" and a["discount"] == "5.0000"
    )


def test_tax_amount_falls_back_to_the_totals(env):
    url, graph, _ = env
    with tenant_session(T, url) as s:
        inv = s.get(models.Invoice, "inv-1")
        inv.total_ex_tax, inv.tax_amount, inv.fields = Decimal("900"), None, {}
    sync(env)
    node = graph.node(T, graph.nodes_by_ref(T, "invoices", ["inv-1"])["inv-1"])
    assert node["attrs"]["tax_amount"] == "100.0000"


def test_lines_carry_hsn_and_discount(env):
    url, graph, _ = env
    with tenant_session(T, url) as s:
        s.add(
            models.InvoiceLine(
                tenant_id=T,
                id="il-1",
                invoice_id="inv-1",
                line_number=1,
                quantity=Decimal("10"),
                unit_price=Decimal("100"),
            )
        )
        s.get(models.Invoice, "inv-1").fields = {
            "extraction": {
                "invoice": {
                    "line_items": [{"hsn_sac": "7318", "line_discount": "2"}]
                }
            }
        }
    sync(env)
    line = graph.node(
        T, graph.nodes_by_ref(T, "invoice_lines", ["il-1"])["il-1"]
    )
    assert line["attrs"]["hsn_sac"] == "7318"
    assert line["attrs"]["line_discount"] == "2.0000"


def test_vendor_carries_bank_verification(env):
    url, graph, _ = env
    with tenant_session(T, url) as s:
        v = s.get(models.Vendor, "ven-1")
        v.bank_verified_at = NOW
        v.bank_account_name = "Acme Steel Pvt Ltd"
    sync(env)
    node = graph.node(T, graph.nodes_by_ref(T, "vendors", ["ven-1"])["ven-1"])
    assert node["attrs"]["bank_verified_at"] == NOW.isoformat()
    assert node["attrs"]["bank_account_name"] == "Acme Steel Pvt Ltd"


def test_history_nodes_carry_every_mapped_field(env):
    url, graph, _ = env
    HistoryImporter(url).import_file(
        CTX,
        kind="bank_line",
        source_name="hdfc",
        filename="bank.csv",
        data=b"Date,Amount,Narration,UTR\n2026-01-09,-1000,ACME STEEL,UTR123\n",
        mapping={
            "Date": "record_date",
            "Amount": "amount",
            "Narration": "counterparty",
            "UTR": "bank_reference",
        },
    )
    sync(env)
    [node] = graph.nodes(T, kind="bank_line")
    assert node["attrs"]["counterparty"] == "ACME STEEL"
    assert node["attrs"]["bank_reference"] == "UTR123"


def test_statement_and_bank_lines_sync_after_payments():
    order = sync_module._HISTORY_ORDER
    assert (
        order["payment"] < order["bank_line"] < order["vendor_statement_line"]
    )


def test_imported_vendor_carries_its_bank_account_as_a_key_not_a_number(env):
    url, graph, _ = env
    HistoryImporter(url).import_file(
        CTX,
        kind="vendor",
        source_name="tally",
        filename="v.csv",
        data=b"Name,Account,IFSC\nNova Fab,50100012345678,HDFC0000001\n",
        mapping={
            "Name": "vendor_key",
            "Account": "bank_account",
            "IFSC": "bank_code",
        },
    )
    sync(env)
    [node] = [
        n
        for n in graph.nodes(T, kind="vendor")
        if n["attrs"]["name"] == "Nova Fab"
    ]
    expected = resolve.natural_keys(
        {"bank_account": "50100012345678", "bank_code": "HDFC0000001"}
    )
    assert node["natural_keys"]["bank_account"] == expected["bank_account"]
    assert node["attrs"]["bank_account"] == "…5678"
    assert "50100012345678" not in str(node)


def test_imported_invoice_carries_its_tax_amount(env):
    url, graph, _ = env
    HistoryImporter(url).import_file(
        CTX,
        kind="ap_invoice",
        source_name="tally",
        filename="a.csv",
        data=b"Bill No,Supplier,Amount,Tax,Date\nINV-T,Acme Steel,1180,180,2025-03-01\n",
        mapping={
            "Bill No": "doc_number",
            "Supplier": "vendor_key",
            "Amount": "amount",
            "Tax": "tax_amount",
            "Date": "record_date",
        },
    )
    sync(env)
    [node] = [
        n
        for n in graph.nodes(T, kind="invoice")
        if n["attrs"].get("doc_number") == "INV-T"
    ]
    assert Decimal(node["attrs"]["tax_amount"]) == Decimal("180")


# -- Wave 3 seams --------------------------------------------------------------


def test_invoice_nodes_carry_the_service_fields_and_line_units(env):
    url, graph, _ = env
    with tenant_session(T, url) as s:
        s.add(
            models.InvoiceLine(
                tenant_id=T,
                id="il-1",
                invoice_id="inv-1",
                line_number=1,
                quantity=Decimal("40"),
                unit_price=Decimal("25"),
            )
        )
        s.get(models.Invoice, "inv-1").fields = {
            "extraction": {
                "invoice": {
                    "service_period_start": "2026-01-01",
                    "service_period_end": "2026-01-31",
                    "work_authorization_reference": "WA-9",
                    "project": "Atlas",
                    "line_items": [{"unit": "hour"}],
                }
            }
        }
    sync(env)
    inv = graph.node(T, graph.nodes_by_ref(T, "invoices", ["inv-1"])["inv-1"])
    assert {
        k: inv["attrs"][k]
        for k in (
            "service_period_start",
            "service_period_end",
            "work_authorization_reference",
            "project",
        )
    } == {
        "service_period_start": "2026-01-01",
        "service_period_end": "2026-01-31",
        "work_authorization_reference": "WA-9",
        "project": "Atlas",
    }
    line = graph.node(
        T, graph.nodes_by_ref(T, "invoice_lines", ["il-1"])["il-1"]
    )
    assert line["attrs"]["unit"] == "hour"


def _import(url, kind, csv: bytes, mapping: dict):
    HistoryImporter(url).import_file(
        CTX,
        kind=kind,
        source_name="3pl",
        filename=f"{kind}.csv",
        data=csv,
        mapping=mapping,
    )


def test_a_shipment_against_a_known_po_fulfils_it(env):
    url, graph, _ = env
    _import(
        url,
        "shipment",
        b"AWB,PO,Supplier,Date,Qty,Status\nAWB1,PO-1,Acme Steel,2026-01-03,10,delivered\n",
        {
            "AWB": "doc_number",
            "PO": "po_number",
            "Supplier": "vendor_key",
            "Date": "record_date",
            "Qty": "quantity",
            "Status": "status",
        },
    )
    sync(env)
    [ship] = graph.nodes(T, kind="shipment")
    assert ship["attrs"]["quantity"] == "10" and ship["attrs"]["status"] == (
        "delivered"
    )
    kinds = {e["kind"] for e in graph.edges(T, src=ship["id"])}
    assert kinds == {"fulfils", "issued_by"}


def test_a_timesheet_naming_no_known_po_only_names_its_vendor(env):
    url, graph, _ = env
    _import(
        url,
        "timesheet",
        b"Vendor,Date,Hours,PO\nAcme Steel,2026-01-03,8,PO-404\n",
        {
            "Vendor": "vendor_key",
            "Date": "record_date",
            "Hours": "hours",
            "PO": "po_number",
        },
    )
    sync(env)
    [sheet] = graph.nodes(T, kind="timesheet")
    assert {e["kind"] for e in graph.edges(T, src=sheet["id"])} == {"issued_by"}
