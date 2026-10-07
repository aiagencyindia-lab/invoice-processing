"""Mechanical repairs to the extractor's output, and the prompt that prevents
the rest.

Three failures, found by diffing 300 extractions against what each document
actually prints:

* The extractor returned Greek capitals indistinguishable from Latin ones in
  invoice numbers -- six clean, text-layer invoices. A look-alike is a
  different character, so the number matched nothing and a resubmission would
  sail past the duplicate check.
* It repeated freight and packing as line items on 15 invoices, so every reader
  of the lines saw a line the document does not print.
* On the 46 submissions that bundle two or three invoices into one PDF, it took
  fields from more than one of them: the bill-to of the second invoice, the
  lines of the third. That is a prompt problem, and the prompt now says so.
"""

import inspect
import json

from invoice_processing import duplicates, extraction_cleanup
from invoice_processing.shared_libraries.acting import general_invoice_agent
from invoice_processing.shared_libraries.acting.general_invoice_agent import (
    ExtractorAgent,
    _charges_and_lines,
)

# Greek Tau, Rho, Alpha -- drawn exactly like the Latin T, P, A.
GREEK_TPA = "\u03a4\u03a1\u0391"


class TestLookAlikeCharacters:
    def test_a_greek_invoice_number_becomes_the_printed_latin_one(self):
        invoice = extraction_cleanup.clean(
            {"invoice_number": f"{GREEK_TPA}/26/0200"}
        )
        assert invoice["invoice_number"] == "TPA/26/0200"

    def test_every_identifier_is_folded(self):
        invoice = extraction_cleanup.clean(
            dict.fromkeys(
                extraction_cleanup.IDENTIFIER_FIELDS, f"{GREEK_TPA}-1"
            )
        )
        for field in extraction_cleanup.IDENTIFIER_FIELDS:
            assert invoice[field] == "TPA-1", field

    def test_prose_is_left_alone(self):
        """A description or a name can legitimately be written in Greek.
        Identifiers are compared character by character; prose is not."""
        text = f"{GREEK_TPA} consulting"
        invoice = extraction_cleanup.clean(
            {"vendor_name": text, "line_items": [{"description": text}]}
        )
        assert invoice["vendor_name"] == text
        assert invoice["line_items"][0]["description"] == text

    def test_the_repair_is_recorded(self):
        """Changing a value the model returned without saying so would make
        the extraction artifact claim something the model never read."""
        invoice = extraction_cleanup.clean({"invoice_number": f"{GREEK_TPA}/1"})
        assert "invoice_number" in invoice["_repairs"][0]

    def test_a_clean_invoice_records_nothing(self):
        invoice = extraction_cleanup.clean({"invoice_number": "TPA/26/0200"})
        assert "_repairs" not in invoice

    def test_a_folded_number_is_found_as_a_duplicate(self):
        """What the repair is for: a resubmission that came back in Greek
        letters must match the original that came back in Latin ones."""
        original = duplicates.InvoiceKey.from_invoice(
            "first",
            {"vendor_name": "Talkpoint PR", "invoice_number": "TPA/26/0200"},
        )
        resubmitted = extraction_cleanup.clean(
            {
                "vendor_name": "Talkpoint PR",
                "invoice_number": f"{GREEK_TPA}/26/0200",
            }
        )
        again = duplicates.InvoiceKey.from_invoice("second", resubmitted)
        assert duplicates.find_duplicates(again, [original])


CHARGE_REPEATED = {
    "subtotal": 100000.00,
    "invoice_total_ex_tax": 102500.00,
    "other_charges": [{"description": "Freight", "amount": 2500.00}],
    "line_items": [
        {"description": "Bearings", "amount_ex_tax": 100000.00},
        {"description": "Freight", "amount_ex_tax": 2500.00},
    ],
}


class TestAChargeRepeatedAsALine:
    INVOICE = CHARGE_REPEATED

    def test_the_repeated_line_is_removed(self):
        invoice = extraction_cleanup.clean(
            {**self.INVOICE, "line_items": list(self.INVOICE["line_items"])},
            charges_and_lines=_charges_and_lines,
        )
        assert [line["description"] for line in invoice["line_items"]] == [
            "Bearings"
        ]
        assert invoice["other_charges"] == self.INVOICE["other_charges"]
        assert "repeated an other charge" in invoice["_repairs"][0]

    def test_a_genuine_line_of_the_same_value_stays(self):
        """Two things costing the same are two things. Only a line whose
        removal brings the lines back to the printed subtotal is a repeat."""
        invoice = {
            **self.INVOICE,
            "subtotal": 102500.00,
            "line_items": [
                {"description": "Bearings", "amount_ex_tax": 100000.00},
                {"description": "Spare seals", "amount_ex_tax": 2500.00},
            ],
        }
        cleaned = extraction_cleanup.clean(
            invoice, charges_and_lines=_charges_and_lines
        )
        assert len(cleaned["line_items"]) == 2


# MFG-INV-042 as production extracted it: packing printed as table line 2 and
# again under "Other Charges", below a Sub Total that leaves it out -- and the
# extractor returned the table alone. Check 4.2 rejected a clean invoice.
CHARGE_ONLY_AS_A_LINE = {
    "subtotal": 27850.00,
    "invoice_total_ex_tax": 30059.00,
    "other_charges": [],
    "line_items": [
        {
            "description": "Taper roller bearing 30208",
            "amount_ex_tax": 27850.00,
        },
        {
            "description": "Packing charges",
            "amount_ex_tax": 2209.00,
            "tax_rate": 0.18,
        },
    ],
}


class TestAChargeReturnedOnlyAsALine:
    def _clean(self, **changes):
        invoice = json.loads(json.dumps({**CHARGE_ONLY_AS_A_LINE, **changes}))
        return extraction_cleanup.clean(
            invoice, charges_and_lines=_charges_and_lines
        )

    def test_the_line_the_subtotal_leaves_out_becomes_a_charge(self):
        invoice = self._clean()
        assert [line["description"] for line in invoice["line_items"]] == [
            "Taper roller bearing 30208"
        ]
        assert invoice["other_charges"] == [
            {
                "description": "Packing charges",
                "amount": 2209.00,
                "tax_rate": 0.18,
            }
        ]
        assert "printed subtotal leaves them out" in invoice["_repairs"][0]

    def test_a_wrong_subtotal_is_not_explained_away(self):
        """A line-sum fault: the subtotal is short by one line, but the taxable
        total was computed from it too. The lines do not reach the taxable
        total, so nothing moves and 4.2 still fails."""
        invoice = self._clean(invoice_total_ex_tax=27850.00)
        assert len(invoice["line_items"]) == 2
        assert "_repairs" not in invoice

    def test_two_lines_of_the_gap_are_ambiguous_and_left_alone(self):
        lines = CHARGE_ONLY_AS_A_LINE["line_items"]
        invoice = self._clean(
            subtotal=27850.00 - 2209.00,
            line_items=[
                {**lines[0], "amount_ex_tax": 27850.00 - 2209.00},
                {"description": "Seal kit", "amount_ex_tax": 2209.00},
                lines[1],
            ],
        )
        assert len(invoice["line_items"]) == 3


# MFG-INV-064 (manual verification MV-002): a clean invoice rejected at 3.2.
# Its text layer prints the vendor's GSTIN with the letter O; the extractor
# returned a zero, which fails the checksum.
PRINTED_PAGE = [
    "Precision Polymers TAX INVOICE GSTIN: 24OXMPF9713X1Z2 "
    "Invoice No. PPL/2026-27/0412 Bill To Ferrovane Industries "
    "GSTIN: 24AAECF9034M1ZE"
]


class TestAnIdentifierIsReadAsPrinted:
    def test_a_letter_read_as_a_digit_takes_the_printed_spelling(self):
        invoice = extraction_cleanup.clean(
            {"vendor_tax_id": "240XMPF9713X1Z2"}, pages=PRINTED_PAGE
        )
        assert invoice["vendor_tax_id"] == "24OXMPF9713X1Z2"
        assert "read as the page prints it" in invoice["_repairs"][0]

    def test_a_forged_id_printed_on_the_page_stays_forged(self):
        """Nothing is folded: a value the page prints is the page's word,
        however wrong it is -- check 3.2 exists to see that."""
        forged = ["GSTIN: 24ITCCR9797D1ZX"]
        assert (
            extraction_cleanup.read_as_printed("24ITCCR9797D1ZX", forged)
            == "24ITCCR9797D1ZX"
        )

    def test_a_scan_is_left_alone(self):
        assert (
            extraction_cleanup.read_as_printed("240XMPF9713X1Z2", [])
            == "240XMPF9713X1Z2"
        )

    def test_two_printed_candidates_are_ambiguous(self):
        pages = ["24OXMPF9713X1Z2 and 24OXMPF97I3X1Z2"]
        assert (
            extraction_cleanup.read_as_printed("240XMPF9713X1Z2", pages)
            == "240XMPF9713X1Z2"
        )

    def test_the_values_own_separators_are_kept(self):
        assert (
            extraction_cleanup.read_as_printed("PPL/2026-27/04I2", PRINTED_PAGE)
            == "PPL/2026-27/0412"
        )

    def test_the_pipeline_passes_the_text_layer(self):
        source = inspect.getsource(general_invoice_agent.ExtractorAgent.run)
        assert "pages=confidence.extract_page_text" in source


class TestTheExtractionPrompt:
    """The prompt is part of the extraction logic, and these are the
    sentences in it that were learned from measured failures."""

    def _prompt(self, monkeypatch):
        captured = {}

        def fake_call(pdf_path, prompt, model, schema):
            captured["prompt"] = prompt
            return schema(), 0

        monkeypatch.setattr(
            general_invoice_agent, "call_gemini_with_pdf", fake_call
        )
        ExtractorAgent()._extract_invoice("unused.pdf")
        return captured["prompt"]

    def test_it_extracts_one_invoice_from_a_file_that_holds_several(
        self, monkeypatch
    ):
        prompt = self._prompt(monkeypatch)
        assert "FIRST invoice only" in prompt
        assert "Never combine" in prompt

    def test_it_says_what_the_total_before_tax_includes(self, monkeypatch):
        prompt = self._prompt(monkeypatch)
        assert "INCLUDING other charges" in prompt

    def test_a_quotation_or_work_order_is_not_a_purchase_order(
        self, monkeypatch
    ):
        """MV-027, MV-035: "Quotation Ref. Q-4179" came back as po_number and
        the invoice was rejected for an unknown PO it never quoted. Re-extracting
        the six such invoices with this wording returned null for all six, and
        fifteen printed POs were still read."""
        prompt = self._prompt(monkeypatch)
        assert "PURCHASE ORDER number only" in prompt
        assert "quotation" in prompt.lower()
        assert "never fill it from another reference" in prompt
        assert (
            "work order reference)" not in prompt.split('"po_number"')[1][:80]
        )
