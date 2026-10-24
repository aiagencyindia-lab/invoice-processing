"""History arrives as files: whole or not at all, every bad row named."""

import io
from datetime import date
from decimal import Decimal

import openpyxl
import pytest
from fresh_schema import fresh_schema

from invoice_processing.db import models
from invoice_processing.db.session import tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust.decide import FakeDecider
from invoice_processing.trust.history import mapping, readers
from invoice_processing.trust.history.importer import (
    HistoryImporter,
    parse_amount,
    parse_date,
)

T = "tenant-acme"
CTX = RunContext(tenant_id=T, case_id="", run_id="r", actor="clerk@x.test")
CSV = (
    b"Bill No,Supplier,Amount,Date\n"
    b"INV-1,Acme,100.50,2026-01-05\n"
    b"INV-2,Acme,oops,2026-01-06\n"
    b"INV-3,Globex,200,06/01/2026\n"
)
MAP = {
    "Bill No": "doc_number",
    "Supplier": "vendor_key",
    "Amount": "amount",
    "Date": "record_date",
}


@pytest.fixture
def importer(scratch_db):
    url = scratch_db("history.db")
    fresh_schema(url)
    with tenant_session(T, url) as s:
        s.add(models.Tenant(id=T, name="Acme", slug="acme", status="active"))
    return HistoryImporter(url)


def load(importer, data=CSV, filename="ap.csv", source="tally", mapping=MAP):
    return importer.import_file(
        CTX,
        kind="ap_invoice",
        source_name=source,
        filename=filename,
        data=data,
        mapping=mapping,
    )


def test_csv_lands_whole_with_bad_rows_reported(importer):
    out = load(importer)
    assert (out.status, out.rows_ok, out.rows_bad) == ("partial", 2, 1)
    assert out.errors == [
        {"row": 3, "reason": "amount: 'oops' is not a number"}
    ]
    with tenant_session(T, importer.url) as s:
        rows = (
            s.query(models.HistoryRecord)
            .order_by(models.HistoryRecord.row_number)
            .all()
        )
        assert [(r.doc_number, r.amount, r.record_date) for r in rows] == [
            ("INV-1", Decimal("100.5000"), date(2026, 1, 5)),
            ("INV-3", Decimal("200.0000"), date(2026, 1, 6)),
        ]
        assert rows[0].fields == {
            "Bill No": "INV-1",
            "Supplier": "Acme",
            "Amount": "100.50",
            "Date": "2026-01-05",
        }


def test_same_sha256_is_noop_even_with_a_new_filename(importer):
    first = load(importer, filename="a.csv")
    again = load(importer, filename="b.csv")
    assert again.duplicate_of == first.import_id and again.rows_ok == 0
    with tenant_session(T, importer.url) as s:
        assert s.query(models.HistoryRecord).count() == 2
        assert s.query(models.TrustHistoryImport).count() == 1


def test_missing_required_field_refuses_the_whole_file(importer):
    with pytest.raises(ValueError, match="amount"):
        load(importer, mapping={"Bill No": "doc_number"})
    with tenant_session(T, importer.url) as s:
        assert s.query(models.TrustHistoryImport).count() == 0


def test_xlsx_reads_dates_and_numbers(importer):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Bill No", "Supplier", "Amount", "Date"])
    ws.append(["INV-9", "Acme", 100.5, date(2026, 1, 5)])
    buf = io.BytesIO()
    wb.save(buf)
    out = load(importer, data=buf.getvalue(), filename="a.xlsx", source="x")
    assert out.rows_ok == 1
    with tenant_session(T, importer.url) as s:
        r = s.query(models.HistoryRecord).one()
        assert (r.amount, r.record_date) == (
            Decimal("100.5000"),
            date(2026, 1, 5),
        )


def test_grouped_number_text_is_parsed_or_refused():
    assert parse_amount("1,00,000.50") == Decimal("100000.50")
    assert parse_amount("₹ 1,000") == Decimal("1000")
    assert parse_amount("(250.00)") == Decimal("-250.00")
    with pytest.raises(ValueError):
        parse_amount("ten")


def test_parse_date_accepts_indian_and_iso_forms():
    assert parse_date("06/01/2026") == date(2026, 1, 6)
    assert parse_date("06-01-2026") == date(2026, 1, 6)
    assert parse_date("2026-01-06T10:00:00") == date(2026, 1, 6)
    with pytest.raises(ValueError):
        parse_date("someday")


def test_read_table_refuses_unknown_extension():
    with pytest.raises(ValueError, match=r"\.csv or \.xlsx"):
        readers.read_table(b"x", "a.xls")


def test_propose_uses_synonyms_then_decider_fallback():
    proposed = mapping.propose(
        "ap_invoice",
        ["Bill No", "Supplier", "Zorg"],
        decider=FakeDecider({}, fail=True),
    )
    assert proposed == {
        "Bill No": "doc_number",
        "Supplier": "vendor_key",
        "Zorg": "",
    }
    with_model = mapping.propose(
        "ap_invoice", ["Zorg"], decider=FakeDecider({"Zorg": "amount"})
    )
    assert with_model == {"Zorg": "amount"}


def test_saved_mapping_comes_from_the_last_import(importer):
    load(importer)
    assert importer.saved_mapping(T, "tally", "ap_invoice") == MAP
    assert importer.saved_mapping(T, "zoho", "ap_invoice") is None
    assert (
        importer.saved_mapping("tenant-globex", "tally", "ap_invoice") is None
    )


def test_a_file_whose_rows_all_fail_is_not_stored_and_can_be_retried(importer):
    bad = {
        "Bill No": "doc_number",
        "Supplier": "amount",
        "Amount": "vendor_key",
    }
    with pytest.raises(ValueError, match="row 2: amount"):
        load(importer, mapping=bad)
    with tenant_session(T, importer.url) as s:
        assert s.query(models.TrustHistoryImport).count() == 0
    assert load(importer).rows_ok == 2  # same bytes, corrected mapping


def test_blank_amount_is_null_where_amount_is_optional(importer):
    csv = b"PO,Supplier,Amount\nPO-1,Acme,\n"
    out = importer.import_file(
        CTX,
        kind="po",
        source_name="t",
        filename="po.csv",
        data=csv,
        mapping={
            "PO": "doc_number",
            "Supplier": "vendor_key",
            "Amount": "amount",
        },
    )
    assert out.rows_ok == 1
    with tenant_session(T, importer.url) as s:
        assert s.query(models.HistoryRecord).one().amount is None


def test_parse_amount_refuses_exponents_and_accepts_rupee_prefixes():
    assert parse_amount("Rs. 1,000") == Decimal("1000")
    assert parse_amount("INR 2,50,000.00") == Decimal("250000.00")
    with pytest.raises(ValueError):
        parse_amount("1e5")


# -- evidence kinds (Wave 3 reality checks) ------------------------------------


@pytest.mark.parametrize(
    ("kind", "headers", "row", "fields"),
    [
        (
            "ticket",
            "Issue Key,Supplier,Date,Status,Hours",
            "OPS-1,Acme,2026-01-05,done,6",
            {"doc_number", "vendor_key", "record_date", "status", "hours"},
        ),
        (
            "timesheet",
            "Supplier,Date,Hours,Rate,Approved By",
            "Acme,2026-01-05,8,1200,lead@x.test",
            {"vendor_key", "record_date", "hours", "rate", "approved_by"},
        ),
        (
            "shipment",
            "AWB,PO,Date,Qty,Status,Courier",
            "AWB-9,PO-1,2026-01-05,10,delivered,BlueDart",
            {
                "doc_number",
                "po_number",
                "record_date",
                "quantity",
                "status",
                "carrier",
            },
        ),
        (
            "seat_count",
            "Supplier,Date,Plan,Qty",
            "Acme,2026-01-31,pro,25",
            {"vendor_key", "record_date", "product", "quantity"},
        ),
    ],
)
def test_evidence_kinds_map_by_synonym_and_import(
    importer, kind, headers, row, fields
):
    from invoice_processing.trust.decide import FakeDecider  # noqa: PLC0415
    from invoice_processing.trust.history import mapping as mp  # noqa: PLC0415

    proposed = mp.propose(
        kind, headers.split(","), decider=FakeDecider({}, fail=True)
    )
    assert set(proposed.values()) >= fields, proposed
    out = importer.import_file(
        CTX,
        kind=kind,
        source_name="ops",
        filename=f"{kind}.csv",
        data=f"{headers}\n{row}\n".encode(),
        mapping={h: f for h, f in proposed.items() if f},
    )
    assert (out.status, out.rows_ok) == ("complete", 1)


def test_an_evidence_row_missing_its_required_fields_is_refused(importer):
    with pytest.raises(ValueError):
        importer.import_file(
            CTX,
            kind="timesheet",
            source_name="ops",
            filename="t.csv",
            data=b"Supplier,Date\nAcme,2026-01-05\n",
            mapping={"Supplier": "vendor_key", "Date": "record_date"},
        )
