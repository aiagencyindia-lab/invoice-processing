"""Scanned purchase orders and goods receipts: extraction, grounding, payload."""

import pytest

from invoice_processing import confidence, supporting_docs
from invoice_processing.api import po_routes
from invoice_processing.supporting_docs import (
    GoodsReceiptExtraction,
    GrnLineExtraction,
    PoLineExtraction,
    PurchaseOrderExtraction,
)

PO_PAGE = (
    "PURCHASE ORDER PO-4471 Date 05/09/2026 Valid from 01/09/2026 to "
    "31/12/2026 Supplier Acme Steel Pvt Ltd GSTIN 24AAACA1234F1Z5 Bill to "
    "Ledgerly Foods Line Item Code Description Qty Unit Rate HSN "
    "1 ST-10 Steel rod 12mm 40 nos 250.00 7214 "
    "2 ST-20 Steel plate 6mm 10 nos 1,200.00 7208 "
    "Total INR 22,000.00 Authorised signatory for Ledgerly Foods purchasing"
)
GRN_PAGE = (
    "GOODS RECEIPT NOTE GRN-105 Received on 12/09/2026 Against PO PO-4471 "
    "Supplier Acme Steel Pvt Ltd Received by R. Mehta stores department "
    "Line Item Code Description Ordered Received Rejected Remarks "
    "1 ST-10 Steel rod 12mm 40 38 2 two bent "
    "2 ST-20 Steel plate 6mm 10 10 0 ok checked and signed at the gate"
)


def _po(**over):
    base = {
        "po_number": "PO-4471",
        "po_date": "2026-09-05",
        "valid_from": "2026-09-01",
        "valid_to": "2026-12-31",
        "vendor_name": "Acme Steel Pvt Ltd",
        "vendor_tax_id": "24AAACA1234F1Z5",
        "customer_name": "Ledgerly Foods",
        "currency": "INR",
        "total_amount": 22000.0,
        "amount_basis": None,
        "lines": [
            PoLineExtraction(
                line_number=1,
                item_code="ST-10",
                description="Steel rod 12mm",
                quantity=40,
                unit="nos",
                unit_price=250.0,
                hsn_sac="7214",
            ),
            PoLineExtraction(
                line_number=None,
                item_code="ST-20",
                description="Steel plate 6mm",
                quantity=10,
                unit="nos",
                unit_price=1200.0,
                hsn_sac="7208",
            ),
        ],
    }
    return PurchaseOrderExtraction(**{**base, **over})


def _grn(**over):
    base = {
        "grn_number": "GRN-105",
        "received_on": "2026-09-12",
        "po_number": "PO-4471",
        "vendor_name": "Acme Steel Pvt Ltd",
        "received_by": "R. Mehta",
        "lines": [
            GrnLineExtraction(
                line_number=1,
                item_code="ST-10",
                description="Steel rod 12mm",
                quantity_received=38,
                unit=None,
                remarks="two bent",
            ),
            GrnLineExtraction(
                line_number=2,
                item_code="ST-20",
                description="Steel plate 6mm",
                quantity_received=10,
                unit=None,
                remarks=None,
            ),
        ],
    }
    return GoodsReceiptExtraction(**{**base, **over})


def _fake(result, seen=None):
    def call(pdf_path, prompt, model, schema):
        if seen is not None:
            seen.update(prompt=prompt, schema=schema)
        return result, 0

    return call


@pytest.fixture
def page(monkeypatch):
    def use(text):
        monkeypatch.setattr(confidence, "extract_page_text", lambda p: [text])
        monkeypatch.setattr(confidence, "ocr_page_text", lambda p: [])

    return use


def test_a_purchase_order_maps_header_and_lines(page):
    page(PO_PAGE)
    seen = {}
    out = supporting_docs.extract(
        "po.pdf", "purchase_order", call=_fake(_po(), seen)
    )
    assert seen["schema"] is PurchaseOrderExtraction
    assert seen["prompt"] == supporting_docs.PO_PROMPT
    assert out["kind"] == "purchase_order"
    assert out["fields"]["po_number"] == "PO-4471"
    assert out["fields"]["total_amount"] == 22000.0
    assert "lines" not in out["fields"]
    assert [ln["line_number"] for ln in out["lines"]] == [1, 2]
    assert out["lines"][1]["unit_price"] == 1200.0
    assert out["_repairs"] == []


def test_a_goods_receipt_maps_header_and_lines(page):
    page(GRN_PAGE)
    seen = {}
    out = supporting_docs.extract(
        "g.pdf", "goods_receipt", call=_fake(_grn(), seen)
    )
    assert seen["schema"] is GoodsReceiptExtraction
    assert seen["prompt"] == supporting_docs.GRN_PROMPT
    assert out["fields"]["grn_number"] == "GRN-105"
    assert out["fields"]["po_number"] == "PO-4471"
    assert [ln["quantity_received"] for ln in out["lines"]] == [38, 10]


def test_unreadable_fields_stay_null(page):
    page(GRN_PAGE)
    out = supporting_docs.extract(
        "g.pdf",
        "goods_receipt",
        call=_fake(_grn(received_on=None, received_by=None)),
    )
    assert out["fields"]["received_on"] is None
    assert out["fields"]["received_by"] is None
    assert "received_on" not in out["_evidence"]


def test_the_prompts_say_transcribe_and_which_quantity():
    for prompt in (supporting_docs.PO_PROMPT, supporting_docs.GRN_PROMPT):
        assert "do not compute" in prompt
        assert "null" in prompt
    assert "accepted/received" in supporting_docs.GRN_PROMPT
    assert "not the ordered or rejected" in supporting_docs.GRN_PROMPT


def test_identifiers_are_read_as_the_page_prints_them(page):
    page(GRN_PAGE)
    # Cyrillic ER and O in the PO number; O for 0 in the GRN number and item code
    grn = _grn(
        grn_number="GRN-1O5",
        po_number="\N{CYRILLIC CAPITAL LETTER ER}\N{CYRILLIC CAPITAL LETTER O}-4471",
    )
    grn.lines[0].item_code = "ST-1O"
    out = supporting_docs.extract("g.pdf", "goods_receipt", call=_fake(grn))
    assert out["fields"]["grn_number"] == "GRN-105"
    assert out["fields"]["po_number"] == "PO-4471"
    assert out["lines"][0]["item_code"] == "ST-10"
    assert len(out["_repairs"]) == 3


def test_a_quantity_not_on_the_page_is_ungrounded_and_low(page):
    page(GRN_PAGE)
    grn = _grn()
    grn.lines[0].quantity_received = 37
    out = supporting_docs.extract("g.pdf", "goods_receipt", call=_fake(grn))
    ev = out["_evidence"]
    assert ev["lines[0].quantity_received"]["grounded"] is False
    assert ev["lines[0].quantity_received"]["confidence"] == confidence.LOW
    assert ev["lines[1].quantity_received"]["confidence"] == confidence.HIGH
    assert ev["grn_number"]["grounded"] is True
    summary = ev["_summary"]
    assert summary["has_text_layer"] is True
    assert summary["min_confidence"] == confidence.LOW
    assert summary["fields_needing_review"] == ["lines[0].quantity_received"]


def test_a_scan_with_no_text_cannot_be_grounded(page):
    page("")
    out = supporting_docs.extract("g.pdf", "goods_receipt", call=_fake(_grn()))
    ev = out["_evidence"]
    assert ev["grn_number"]["grounded"] is None
    assert ev["_summary"]["has_text_layer"] is False
    assert ev["_summary"]["min_confidence"] < confidence.HIGH


def test_a_failed_call_raises(page):
    page(GRN_PAGE)

    def broken(*a):
        raise RuntimeError("model unavailable")

    with pytest.raises(RuntimeError, match="model unavailable"):
        supporting_docs.extract("g.pdf", "goods_receipt", call=broken)


def test_an_unknown_kind_is_refused():
    with pytest.raises(ValueError):
        supporting_docs.extract("x.pdf", "invoice", call=_fake(_grn()))


def test_grn_lines_pair_by_code_then_description_never_by_their_own_number():
    po_lines = [
        {
            "line_number": 1,
            "item_code": "ST-10",
            "description": "Steel rod 12mm",
        },
        {"line_number": 2, "item_code": None, "description": "Steel plate 6mm"},
        {"line_number": 3, "item_code": None, "description": "Bolts M8 zinc"},
    ]
    lines = [
        {"line_number": 9, "item_code": None, "description": "bolts m8 zinc"},
        {"line_number": 7, "item_code": "ST 10", "description": "rod"},
        {"line_number": 2, "item_code": None, "description": "anything"},
        {"line_number": 8, "item_code": None, "description": "paint"},
    ]
    assert supporting_docs.pair_grn_lines(lines, po_lines) == [3, 1, None, None]


def test_a_renumbered_partial_receipt_pairs_by_description_not_its_numbering():
    po_lines = [
        {
            "line_number": 1,
            "item_code": None,
            "description": "27-inch IPS monitor",
        },
        {
            "line_number": 2,
            "item_code": None,
            "description": "Business laptop 14-inch",
        },
        {
            "line_number": 3,
            "item_code": None,
            "description": "USB-C docking station",
        },
    ]
    lines = [
        {
            "line_number": 1,
            "item_code": None,
            "description": "27-inch IPS monitor",
        },
        {
            "line_number": 2,
            "item_code": None,
            "description": "USB-C docking station",
        },
    ]
    assert supporting_docs.pair_grn_lines(lines, po_lines) == [1, 3]


def test_a_purchase_order_payload_imports_cleanly(page):
    page(PO_PAGE)
    out = supporting_docs.extract("po.pdf", "purchase_order", call=_fake(_po()))
    payload = supporting_docs.to_import_payload(
        "purchase_order", out["fields"], out["lines"]
    )
    assert po_routes.validate_orders([payload]) == []
    assert payload["status"] == "open"
    assert payload["lines"][1] == {
        "line_number": 2,
        "item_code": "ST-20",
        "description": "Steel plate 6mm",
        "qty_ordered": 10,
        "unit_price": 1200.0,
        "uom": "nos",
        "receipt_required": None,
    }
    po_routes._clean_orders([payload])


def test_a_goods_receipt_payload_imports_cleanly(page):
    page(GRN_PAGE)
    out = supporting_docs.extract("g.pdf", "goods_receipt", call=_fake(_grn()))
    lines = [
        {**ln, "po_line_number": n}
        for ln, n in zip(out["lines"], [1, 2], strict=True)
    ]
    payload = supporting_docs.to_import_payload(
        "goods_receipt", out["fields"], lines
    )
    assert po_routes.validate_receipts([payload]) == []
    assert payload == {
        "grn_number": "GRN-105",
        "po_number": "PO-4471",
        "received_on": "2026-09-12",
        "reason": None,
        "lines": [
            {"line_number": 1, "item_code": "ST-10", "qty_received": 38},
            {"line_number": 2, "item_code": "ST-20", "qty_received": 10},
        ],
    }
    po_routes._clean_receipts([payload])


def test_an_unpaired_grn_line_without_a_code_does_not_import():
    payload = supporting_docs.to_import_payload(
        "goods_receipt",
        {
            "grn_number": "GRN-1",
            "po_number": "PO-1",
            "received_on": "2026-09-12",
        },
        [{"line_number": 1, "item_code": None, "quantity_received": 3}],
    )
    assert payload["lines"][0]["line_number"] is None
    assert any(
        "needs a line_number or an item_code" in p
        for p in po_routes.validate_receipts([payload])
    )


def test_by_default_it_calls_the_pdf_extractor(page, monkeypatch):
    from invoice_processing.shared_libraries.acting import (  # noqa: PLC0415
        general_invoice_agent,
    )

    page(GRN_PAGE)
    monkeypatch.setattr(
        general_invoice_agent, "call_gemini_with_pdf", _fake(_grn())
    )
    out = supporting_docs.extract("g.pdf", "goods_receipt")
    assert out["fields"]["grn_number"] == "GRN-105"


def test_a_goods_receipt_payload_carries_its_correction_reason():
    payload = supporting_docs.to_import_payload(
        "goods_receipt",
        {
            "grn_number": "GRN-2",
            "po_number": "PO-1",
            "received_on": "2026-09-12",
            "reason": "returned damaged",
        },
        [{"po_line_number": 1, "quantity_received": -2}],
    )
    assert payload["reason"] == "returned damaged"
    assert po_routes.validate_receipts([payload]) == []


def test_an_ocr_page_grounds_an_identifier_but_never_repairs_it(monkeypatch):
    """OCR confuses O and 0 exactly as the extractor does, so its reading of
    "GRN-1O5" confirms "GRN-105" without correcting it to the misread."""
    monkeypatch.setattr(confidence, "extract_page_text", lambda p: [""])
    monkeypatch.setattr(
        confidence,
        "ocr_page_text",
        lambda p: [GRN_PAGE.replace("GRN-105", "GRN-1O5")],
    )
    out = supporting_docs.extract("g.pdf", "goods_receipt", call=_fake(_grn()))
    assert out["fields"]["grn_number"] == "GRN-105"
    assert out["_repairs"] == []
    ev = out["_evidence"]
    assert ev["grn_number"]["grounded"] is True
    assert ev["_summary"]["has_text_layer"] is False


def test_the_po_prompt_asks_for_the_currency_code():
    # Not grounded (see _NOT_GROUNDED): so it must not be asked "as printed".
    assert '"currency": "string or null (ISO 4217 code' in (
        supporting_docs.PO_PROMPT
    )
    assert "INR" in supporting_docs.PO_PROMPT


@pytest.mark.parametrize("printed", [0, 7])
def test_a_po_line_number_is_imported_as_given(printed):
    payload = supporting_docs.to_import_payload(
        "purchase_order",
        {"po_number": "PO-1"},
        [{"line_number": printed, "description": "rod", "quantity": 1}],
    )
    assert payload["lines"][0]["line_number"] == printed


STAMPED_GRN_PAGE = GRN_PAGE + " RECEIVED 04/09/2026 Accounts Dept."


def test_a_second_date_on_the_page_puts_the_receipt_date_in_review(page):
    """A RECEIVED stamp carries its own date, and the extractor has read it
    as the receipt date; the page grounds either reading."""
    page(STAMPED_GRN_PAGE)
    out = supporting_docs.extract("g.pdf", "goods_receipt", call=_fake(_grn()))
    ev = out["_evidence"]
    assert ev["received_on"]["grounded"] is True
    assert ev["received_on"]["confidence"] < confidence.HIGH
    assert "received_on" in ev["_summary"]["fields_needing_review"]


@pytest.mark.parametrize(
    "extra",
    ["", " Printed 12-Sep-2026", " Printed 2026-09-12", " Printed 12.09.2026"],
)
def test_one_date_printed_twice_is_not_a_second_date(page, extra):
    page(GRN_PAGE + extra)
    out = supporting_docs.extract("g.pdf", "goods_receipt", call=_fake(_grn()))
    review = out["_evidence"]["_summary"]["fields_needing_review"]
    assert "received_on" not in review


@pytest.mark.parametrize(
    "extra",
    [
        " Stamp 02.09.2026",
        " Stamp 5 Sep 2026",
        " Stamp Sep 5, 2026",
        " Stamp 2026-09-05",
        " Stamp 09/25/2026",
    ],
)
def test_every_date_format_counts_as_a_second_date(page, extra):
    page(GRN_PAGE + extra)
    out = supporting_docs.extract("g.pdf", "goods_receipt", call=_fake(_grn()))
    review = out["_evidence"]["_summary"]["fields_needing_review"]
    assert "received_on" in review
