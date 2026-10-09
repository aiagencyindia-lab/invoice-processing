"""Step 2.3 in the pipeline: line-level three-way match holds, never rejects."""

import json

import pytest

from invoice_processing import po_book, po_matching
from invoice_processing.shared_libraries.acting import general_invoice_agent
from invoice_processing.shared_libraries.acting.general_invoice_agent import (
    Phase2ValidatorAgent,
)

VENDOR = {"name": "Acme Pty Ltd", "tax_id": "", "kind": "goods"}


def po(n, description, qty, price):
    return {
        "line_number": n,
        "description": description,
        "qty_ordered": qty,
        "unit_price": price,
    }


LINES = (po(1, "Bearing 6205", 10, 100),)

HEADER = {
    "po_number": "PO-1",
    "vendor_name": "Acme Pty Ltd",
    "total_amount": 1000,
    "currency": "AUD",
    "status": "open",
}


class FakeBook:
    def __init__(self, lines, received, raises=None):
        self.lines, self.received, self.raises = lines, received, raises

    def find(self, po_number):
        return HEADER if self._known(po_number) else None

    @staticmethod
    def _known(po_number):
        return po_matching.normalize_po_number(po_number) == (
            po_matching.normalize_po_number("PO-1")
        )

    def book(self, po_number):
        if self.raises:
            raise self.raises
        if not self._known(po_number):
            return None
        return {
            "po": HEADER,
            "lines": self.lines,
            "received": self.received,
            "billed": {},
            "grn_numbers": {},
        }


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    def run(
        received=None,
        po_number="PO-1",
        lines=LINES,
        book_raises=None,
        **overrides,
    ):
        book = FakeBook(
            list(lines) if lines else None, received or {}, book_raises
        )
        monkeypatch.setitem(
            general_invoice_agent._config_store,
            "CONFIG",
            {
                **general_invoice_agent.DEFAULT_CONFIG,
                "duplicate_check_enabled": False,
                "require_purchase_order": False,
                "purchase_order_book": book,
                "vendor_master_records": [VENDOR],
                **overrides,
            },
        )
        run.extraction = {
            "invoice": {
                "po_number": po_number,
                "vendor_name": "Acme Pty Ltd",
                "invoice_total_inc_tax": 500,
                "currency": "AUD",
                "line_items": [
                    {
                        "description": "Bearing 6205",
                        "quantity": 5,
                        "unit_price": 100,
                        "amount_ex_tax": 500,
                    }
                ],
                "_routing": "AUTO",
                "_evidence": {"_summary": {"fields_needing_review": []}},
            }
        }
        out = Phase2ValidatorAgent().run(
            tmp_path, run.extraction, {"decision": "CONTINUE"}
        )
        run.saved = tmp_path / "02_extraction.json"
        return out

    return run


def _step(out):
    return next(v for v in out["validations"] if v["step"] == "2.3")


def test_short_receipt_holds_for_review_and_never_rejects(pipeline):
    out = pipeline(received={1: 2})
    step = _step(out)
    assert out["decision"] == "CONTINUE"
    assert step["passed"] is False and step["informational"] is True
    assert step["signals"] == ["short_received"]
    assert step["routing_before"] == "AUTO"
    assert step["lines"] and step["lines"][0]["po_line_number"] == 1
    invoice = pipeline.extraction["invoice"]
    assert invoice["_routing"] == "REVIEW"
    assert (
        "receipt: short_received"
        in invoice["_evidence"]["_summary"]["fields_needing_review"]
    )
    # Re-saved for every reader that takes routing from the artifact.
    saved = json.loads(pipeline.saved.read_text())
    assert saved["invoice"]["_routing"] == "REVIEW"


def test_full_receipt_passes_untouched(pipeline):
    out = pipeline(received={1: 10})
    step = _step(out)
    assert step["passed"] is True and step["signals"] == []
    assert "routing_before" not in step
    assert pipeline.extraction["invoice"]["_routing"] == "AUTO"


def test_step_2_2_rejections_still_reject_and_skip_2_3(pipeline):
    out = pipeline(po_number="PO-NOPE")
    assert out["decision"] == "REJECT"
    assert not any(v["step"] == "2.3" for v in out["validations"])


def test_po_without_lines_is_header_only(pipeline):
    out = pipeline(lines=None)
    step = _step(out)
    assert step["passed"] is True and "header match only" in step["evidence"]


def test_book_failure_holds_not_passes(pipeline):
    out = pipeline(book_raises=RuntimeError("db down"))
    step = _step(out)
    assert step["passed"] is False and step["signals"] == ["unchecked"]
    assert "db down" in step["evidence"]
    assert out["decision"] == "CONTINUE"
    assert pipeline.extraction["invoice"]["_routing"] == "REVIEW"


def test_disabled_check_skips(pipeline):
    out = pipeline(received={1: 0}, disabled_checks=["2.3"])
    assert _step(out).get("skipped")
    assert pipeline.extraction["invoice"]["_routing"] == "AUTO"


def test_file_book_reads_optional_lines(tmp_path):
    path = tmp_path / "pos.json"
    path.write_text(
        json.dumps(
            [
                {**HEADER, "lines": [{**po(1, "Bearing", 10, 100), "qty_received": 4, "qty_invoiced": 1}]},
                {**HEADER, "po_number": "PO-2"},
            ]
        )
    )  # fmt: skip
    book = po_book.FileBook(path)
    got = book.book("po-1")
    assert got["received"] == {1: 4} and got["billed"] == {1: 1}
    assert got["po"]["po_number"] == "PO-1"
    assert book.book("PO-2") is None and book.book("PO-9") is None
    assert book.find("PO-2")["po_number"] == "PO-2"


def test_db_book_adds_grn_numbers_per_line():
    class Repo:
        def book(self, tenant_id, po_number):
            assert tenant_id == "t1"
            return {"po": HEADER, "lines": [], "received": {}, "billed": {}}

        def list_receipts(self, tenant_id, po_number=None):
            return [
                {"grn_number": "G1", "lines": [{"line_number": 1}]},
                {"grn_number": "G2", "lines": [{"line_number": 1}, {"line_number": 2}]},
            ]  # fmt: skip

    book = po_book.DbBook(Repo(), "t1")
    assert book.find("PO-1") == HEADER
    assert book.book("PO-1")["grn_numbers"] == {1: ["G1", "G2"], 2: ["G2"]}
