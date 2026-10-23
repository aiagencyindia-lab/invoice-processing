"""Confidence must discriminate, not just agree.

Per-field confidence is what turns extraction into a touchless rate: fields
above the threshold post automatically, the rest route to a human. A signal that
says "high" for everything is worse than none, because it manufactures trust.

So the tests that matter here are the negative ones -- a hallucinated value, a
transposed total, a document with no text layer. The clean-invoice cases only
establish that the signal does not cry wolf.

The pipeline's three original `confidence` fields were LLM self-reports and dead
code. Self-reported confidence is uncalibrated, and this codebase has measured
the model disagreeing with itself ~40% of the time at temperature=0. These two
signals -- text-layer grounding and arithmetic consistency -- are deterministic
and free.
"""

import json
from pathlib import Path
from typing import ClassVar

import pytest
from golden import GOLDEN_CASES

from invoice_processing import confidence, paths
from invoice_processing.confidence import (
    HIGH,
    LOW,
    MEDIUM,
    arithmetic_checks,
    build_evidence,
    locate,
    routing_decision,
    score,
)

CASES = ("case_001", "case_002", "case_005")


def _invoice(case: str) -> dict:
    data = json.loads((GOLDEN_CASES / case / "02_extraction.json").read_text())
    return data.get("invoice", {})


def _pdf(case: str):
    return paths.EXEMPLARY_DIR / case / "invoice.pdf"


class TestGroundingOnRealInvoices:
    @pytest.mark.parametrize("case", CASES)
    def test_clean_invoice_routes_auto(self, case):
        """No false alarms: a correct extraction must not demand review."""
        evidence = build_evidence(_invoice(case), _pdf(case))
        assert routing_decision(evidence) == "AUTO", (
            f"{case} flagged for review: "
            f"{evidence['_summary']['fields_needing_review']}"
        )

    @pytest.mark.parametrize("case", CASES)
    def test_every_field_is_located_in_the_document(self, case):
        evidence = build_evidence(_invoice(case), _pdf(case))
        for name, field in evidence.items():
            if name == "_summary":
                continue
            assert field["grounded"] is True, (
                f"{case}.{name} not found in the PDF"
            )
            assert field["page"] == 1


class TestDetectsBadExtraction:
    """The tests that give the signal its value."""

    def test_hallucinated_value_is_caught(self):
        invoice = dict(
            _invoice("case_005"), vendor_name="Totally Fabricated Pty Ltd"
        )
        evidence = build_evidence(invoice, _pdf("case_005"))

        assert evidence["vendor_name"]["grounded"] is False
        assert routing_decision(evidence) == "REVIEW"
        assert "vendor_name" in evidence["_summary"]["fields_needing_review"]

    def test_transposed_total_is_caught(self):
        """1069.32 -> 1096.32: plausible, absent from the document, breaks maths."""
        invoice = dict(_invoice("case_005"), invoice_total_inc_tax=1096.32)
        evidence = build_evidence(invoice, _pdf("case_005"))

        assert routing_decision(evidence) == "REVIEW"
        assert evidence["_summary"]["arithmetic"]["totals_reconcile"] is False

    def test_wrong_invoice_number_is_caught(self):
        invoice = dict(_invoice("case_005"), invoice_number="INV-99999")
        evidence = build_evidence(invoice, _pdf("case_005"))
        assert evidence["invoice_number"]["grounded"] is False
        assert routing_decision(evidence) == "REVIEW"

    def test_only_the_bad_field_is_flagged(self):
        """Precision matters: flagging everything is as useless as flagging nothing."""
        invoice = dict(_invoice("case_005"), invoice_number="INV-99999")
        evidence = build_evidence(invoice, _pdf("case_005"))
        assert evidence["_summary"]["fields_needing_review"] == [
            "invoice_number"
        ]


# A page, not a fragment: grounding only runs where there is enough text to
# check a value against, so a one-line fixture would be testing the wrong path.
PAGES = [
    "QuickFix HVAC Repairs Pty Ltd\n"
    "12 Boundary Road, Richmond VIC 3121\n"
    "ABN 51 824 753 556\n"
    "TAX INVOICE\n"
    "Invoice Number: INV-00005\n"
    "Invoice Total: $1,069.32 dated 11/02/2026\n"
    "Description Qty Unit Price Amount\n"
    "Technician labour 3.00 110.00 330.00\n"
    "Air filter replacement 2.00 45.00 90.00\n"
    "Subtotal 972.11 GST 97.21 Total 1,069.32\n"
    "Account Name: QuickFix HVAC\n"
    "BSB 062-000 Account 12345678"
]


class TestNormalization:
    """Without these, every correct number gets flagged and the signal is noise."""

    def test_number_matches_formatted_amount(self):
        assert locate(1069.32, PAGES)[0] is True

    def test_iso_date_matches_displayed_date(self):
        assert locate("2026-02-11", PAGES)[0] is True

    def test_spaced_tax_id_matches(self):
        assert locate("51824753556", PAGES)[0] is True

    def test_absent_value_is_not_matched(self):
        assert locate("9999.99", PAGES)[0] is False


class TestNoTextLayer:
    """A scanned image is not evidence that a value is wrong."""

    def test_missing_text_layer_returns_none_not_false(self):
        assert locate("anything", [])[0] is None
        assert locate("anything", ["", "   "])[0] is None

    def test_scanned_document_routes_to_review(self):
        invoice = _invoice("case_005")
        evidence = build_evidence(invoice, None)
        assert evidence["_summary"]["has_text_layer"] is False
        assert routing_decision(evidence) == "REVIEW", (
            "an ungroundable document must not auto-post"
        )

    def test_scoring_caps_at_medium_without_grounding(self):
        assert score(None, True) == MEDIUM
        assert score(None, False) == LOW


class TestArithmetic:
    def test_totals_reconcile(self):
        checks = arithmetic_checks(
            {
                "invoice_total_ex_tax": 972.11,
                "tax_amount": 97.21,
                "invoice_total_inc_tax": 1069.32,
            }
        )
        assert checks["totals_reconcile"] is True

    def test_totals_that_do_not_reconcile(self):
        checks = arithmetic_checks(
            {
                "invoice_total_ex_tax": 100.0,
                "tax_amount": 10.0,
                "invoice_total_inc_tax": 200.0,
            }
        )
        assert checks["totals_reconcile"] is False

    def test_line_maths(self):
        checks = arithmetic_checks(
            {
                "line_items": [
                    {
                        "quantity": 3,
                        "unit_price": 219.87,
                        "amount_ex_tax": 659.61,
                    },
                    {
                        "quantity": 2.5,
                        "unit_price": 125.0,
                        "amount_ex_tax": 312.50,
                    },
                ]
            }
        )
        assert checks["line_maths_consistent"] is True

    def test_line_maths_catches_a_wrong_extension(self):
        checks = arithmetic_checks(
            {
                "line_items": [
                    {"quantity": 3, "unit_price": 100.0, "amount_ex_tax": 250.0}
                ]
            }
        )
        assert checks["line_maths_consistent"] is False

    def test_missing_fields_are_skipped_not_failed(self):
        """Absence of data must not be reported as a failed check."""
        assert arithmetic_checks({}) == {}


class TestScoring:
    def test_grounded_and_consistent_is_high(self):
        assert score(True, True) == HIGH

    def test_grounded_with_no_arithmetic_opinion_is_high(self):
        assert score(True, None) == HIGH

    def test_one_signal_only_is_medium(self):
        assert score(True, False) == MEDIUM
        assert score(False, True) == MEDIUM

    def test_neither_signal_is_low(self):
        assert score(False, False) == LOW


class TestPipelineIntegration:
    @pytest.mark.parametrize("case", CASES)
    def test_evidence_is_written_into_the_extraction_artifact(self, case):
        """The pipeline must persist evidence for the reviewer UI."""
        data = json.loads(
            (GOLDEN_CASES / case / "02_extraction.json").read_text()
        )
        invoice = data.get("invoice", {})
        assert "_evidence" in invoice, "run the pipeline to populate evidence"
        assert "_routing" in invoice
        assert invoice["_evidence"]["_summary"]["has_text_layer"] is True

    def test_no_llm_call_is_involved(self):
        source = (paths.AGENT_PKG_DIR / "confidence.py").read_text()
        assert "generate_content" not in source
        assert "GenerativeModel" not in source


class TestTheFraudRelevantFieldsAreChecked:
    """Payment redirection changes an account number. An account number that
    is not in the document it was read from is the cheapest strong signal
    there is -- and these were the only extracted fields the grounder skipped,
    so they reached the workbench marked "not corroborated"."""

    def test_bank_details_are_grounded(self):
        """The list itself: without the field here the value is never looked
        for, and the workbench renders it as unchecked."""
        for field in (
            "bank_account_name",
            "bank_account_number",
            "bank_code",
            "bank_iban",
        ):
            assert field in confidence.GROUNDED_FIELDS, field

    def test_an_account_number_absent_from_the_page_is_flagged(self):
        page = PAGES
        grounded, _ = confidence.locate("87654321", page)
        assert grounded is False
        grounded, _ = confidence.locate("12345678", page)
        assert grounded is True

    def test_payment_terms_are_left_alone_deliberately(self):
        """The extractor paraphrases them, so grounding flags correct values."""
        assert "payment_terms" not in confidence.GROUNDED_FIELDS


class TestTaxBreakdownIsReconciled:
    """Extracted, stored, and until now never checked against anything."""

    def _invoice(self, breakdown, tax):
        return {
            "invoice_total_ex_tax": 1000.0,
            "tax_amount": tax,
            "invoice_total_inc_tax": 1000.0 + tax,
            "tax_breakdown": breakdown,
        }

    def test_rates_that_add_up_pass(self):
        checks = confidence.arithmetic_checks(
            self._invoice(
                [{"tax_amount": 60.0}, {"tax_amount": 40.0}], tax=100.0
            )
        )
        assert checks["tax_breakdown_sums_to_tax"] is True

    def test_rates_that_do_not_add_up_fail(self):
        checks = confidence.arithmetic_checks(
            self._invoice(
                [{"tax_amount": 60.0}, {"tax_amount": 25.0}], tax=100.0
            )
        )
        assert checks["tax_breakdown_sums_to_tax"] is False

    def test_no_breakdown_is_not_a_failure(self):
        """Most invoices state one rate and no breakdown. Absence is not an
        error, and reporting it as one would flag every ordinary invoice."""
        checks = confidence.arithmetic_checks(self._invoice([], tax=100.0))
        assert "tax_breakdown_sums_to_tax" not in checks

    def test_a_partial_breakdown_is_not_judged(self):
        checks = confidence.arithmetic_checks(
            self._invoice(
                [{"tax_amount": 60.0}, {"tax_amount": None}], tax=100.0
            )
        )
        assert "tax_breakdown_sums_to_tax" not in checks


class TestAPageThatIsReallyAnImage:
    """Found in production: an invoice saved from a browser as a PDF.

    The page was one image. Its only text was what the browser printed around
    it -- a timestamp, the source URL, "1/1" -- so "does this PDF have text?"
    answered yes, and all ten extracted fields were reported as *not found in
    the document*. That reads as an accusation that the extractor invented
    them, when in fact nothing could be checked at all.
    """

    BROWSER_PRINT: ClassVar[list[str]] = [
        "02/09/2026, 15:05 1000_Invoice_Generator_Template_b16c74bbb6.webp "
        "(1000x1418)\n"
        "https://assets.refrens.com/1000_Invoice_Generator_Template.webp 1/1"
    ]

    def test_a_print_header_is_not_a_text_layer(self):
        assert confidence.has_readable_text(self.BROWSER_PRINT) is False

    def test_a_real_page_is(self):
        assert confidence.has_readable_text(PAGES) is True

    def test_fields_read_as_unknown_not_as_missing(self):
        """`None` means "could not check"; `False` means "checked, not there".
        Only one of those is true here, and they lead a reviewer to different
        conclusions."""
        grounded, page = confidence.locate("Foobar Labs", self.BROWSER_PRINT)

        assert grounded is None
        assert page is None

    def test_the_summary_says_there_is_no_text_layer(self, tmp_path):
        evidence = confidence.build_evidence(
            {"invoice_number": "003", "vendor_name": "Foobar Labs"}, None
        )
        assert evidence["_summary"]["has_text_layer"] is False

    def test_such_a_document_still_routes_to_review(self):
        """Unknown is not permission to auto-approve."""
        assert confidence.score(None, True) < confidence.HIGH
        assert confidence.score(None, None) < confidence.HIGH


# What an Indian GST invoice prints, from the benchmark corpus (MFG-INV-042):
# the month as a word, CGST and SGST with no tax total, a round-off line.
GST_PAGES = [
    "Apex Industrial Components Pvt Ltd TAX INVOICE GSTIN: 24ITCCR9797D1ZQ "
    "Invoice No. AIC/2026-27/0175 Invoice Date 13-May-2026 Due Date "
    "28-May-2026 Taper roller bearing 50 nos 557.00 2% 27,293.00 Sub Total "
    "27,293.00 CGST 2,456.37 SGST 2,456.37 Round Off 0.26 GRAND TOTAL "
    "32,206.00 Amount in words: Rupees Thirty Two Thousand Two Hundred Six"
]


class TestWhatGstInvoicesPrint:
    """Four shapes the benchmark's clean invoices print and the module flagged.

    Each one sent a correct invoice to review: 34 of 38 accepted invoices were
    routed to a human, and only 4 posted untouched.
    """

    def test_a_month_written_as_a_word_is_found(self):
        assert locate("2026-05-13", GST_PAGES)[0] is True
        assert locate("2026-05-28", GST_PAGES)[0] is True

    def test_a_tax_total_is_grounded_through_its_printed_components(self):
        invoice = {
            "tax_amount": 4912.74,
            "cgst_amount": 2456.37,
            "sgst_amount": 2456.37,
        }
        evidence = confidence.build_evidence(invoice, None)
        assert evidence["tax_amount"]["grounded"] is None  # no pages given
        assert confidence._locate_tax_by_components(
            invoice, 4912.74, GST_PAGES
        ) == (True, 1)

    def test_components_that_do_not_add_up_ground_nothing(self):
        invoice = {"cgst_amount": 2456.37, "sgst_amount": 2456.37}
        assert confidence._locate_tax_by_components(
            invoice, 5000.00, GST_PAGES
        ) == (False, None)

    def test_a_line_discount_is_part_of_the_line_maths(self):
        checks = arithmetic_checks(
            {
                "line_items": [
                    {
                        "quantity": 50,
                        "unit_price": 557.0,
                        "line_discount_pct": 0.02,
                        "amount_ex_tax": 27293.0,
                    }
                ]
            }
        )
        assert checks["line_maths_consistent"] is True

    def test_the_round_off_is_part_of_the_total(self):
        checks = arithmetic_checks(
            {
                "invoice_total_ex_tax": 27293.00,
                "tax_amount": 4912.74,
                "round_off": 0.26,
                "invoice_total_inc_tax": 32206.00,
            }
        )
        assert checks["totals_reconcile"] is True


class TestArithmeticThatContradictsItselfIsReviewed:
    """Line checks were computed and never used for routing: an invoice whose
    lines disagree with its own subtotal posted untouched if every field
    happened to be grounded."""

    def test_lines_that_overshoot_the_subtotal_route_to_review(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            confidence, "extract_page_text", lambda _p: GST_PAGES
        )
        invoice = {
            "invoice_number": "AIC/2026-27/0175",
            "invoice_total_ex_tax": 27293.00,
            "line_items": [
                {"amount_ex_tax": 27293.00},
                {"amount_ex_tax": 500.0},
            ],
        }
        evidence = build_evidence(invoice, Path("printed.pdf"))
        # Every field is on the page; only the arithmetic objects.
        assert all(
            evidence[name]["confidence"] == HIGH
            for name in ("invoice_number", "invoice_total_ex_tax")
        )
        assert evidence["_summary"]["fields_needing_review"] == [
            "arithmetic: lines_sum_to_subtotal"
        ]
        assert routing_decision(evidence) == "REVIEW"

    def test_consistent_lines_still_post(self, monkeypatch):
        monkeypatch.setattr(
            confidence, "extract_page_text", lambda _p: GST_PAGES
        )
        invoice = {
            "invoice_number": "AIC/2026-27/0175",
            "invoice_total_ex_tax": 27293.00,
            "line_items": [{"amount_ex_tax": 27293.00}],
        }
        assert (
            routing_decision(build_evidence(invoice, Path("p.pdf"))) == "AUTO"
        )


class TestWhatStillSentCleanInvoicesToReview:
    """After 7934744, 11 clean text-layer invoices still went to a human.
    Each of these failed on a correct value."""

    def test_a_zero_padded_day_after_the_month_name(self):
        assert locate(
            "2026-04-01", [GST_PAGES[0] + " Invoice Date Apr 01, 2026"]
        )[0]

    def test_an_address_split_by_a_logo_on_the_same_row(self):
        """The page read "Ludhiana 668868, BF" / "Punjab": the logo's letters
        sat between the two lines of the address."""
        pages = [
            "Bharat Fasteners Corporation TAX INVOICE "
            "369, Industrial Area 22, Ludhiana 668868, BF Punjab "
            + "GSTIN 03NSQPM1376V1ZF "
            * 10
        ]
        grounded, _ = confidence._locate_by_segments(
            "369, Industrial Area 22, Ludhiana 668868, Punjab", pages
        )
        assert grounded is True
        assert confidence._locate_by_segments(
            "369, Industrial Area 22, Amritsar 668868, Punjab", pages
        ) == (False, None)

    def test_a_taxable_total_grounded_through_subtotal_and_charges(self):
        invoice = {
            "subtotal": 27850.00,
            "other_charges": [{"amount": 2209.00}],
        }
        page = [GST_PAGES[0] + " Sub Total 27,850.00 Other Charges 2,209.00"]
        assert confidence._locate_taxable_by_parts(invoice, 30059.00, page) == (
            True,
            1,
        )
        assert confidence._locate_taxable_by_parts(invoice, 31000.00, page) == (
            False,
            None,
        )

    def test_lines_are_compared_with_the_printed_subtotal_not_the_taxable(
        self,
    ):
        """MFG-INV-039: the lines equal the printed subtotal; the taxable
        total is 116,806 less because of an invoice discount. Comparing lines
        with the taxable total called that a contradiction."""
        checks = arithmetic_checks(
            {
                "subtotal": 2336120.05,
                "discount": 116806.00,
                "invoice_total_ex_tax": 2219314.05,
                "line_items": [
                    {"amount_ex_tax": 2000000.00},
                    {"amount_ex_tax": 336120.05},
                ],
            }
        )
        assert checks["lines_sum_to_subtotal"] is True

    def test_a_charge_in_the_taxable_total_is_netted_out(self):
        checks = arithmetic_checks(
            {
                "subtotal": 27850.00,
                "invoice_total_ex_tax": 30059.00,
                "other_charges": [{"amount": 2209.00}],
                "line_items": [{"amount_ex_tax": 27850.00}],
            }
        )
        assert checks["lines_sum_to_subtotal"] is True


class TestAValueMustStandOnItsOwn:
    """A substring search confirmed values the page never states: the 0.00
    placeholder inside 10.00, and a truncated GSTIN inside the printed one.
    Measured over 11,567 extracted fields in three stored runs, the token
    boundary flipped only values like these."""

    PAGE: ClassVar[list[str]] = [
        GST_PAGES[0]
        + " Discount 10.00 GSTIN: 24YVBFK3734K1ZQ IFSC DTBK0594444"
        + " Grand Total 13,41,792.00 ABN 51 824 753 556"
    ]

    def test_a_number_inside_a_longer_number_is_not_found(self):
        assert locate(0.0, self.PAGE)[0] is False

    def test_a_truncated_identifier_is_not_found(self):
        assert locate("24YVBFK3734K", self.PAGE)[0] is False
        assert locate("DTBK059444", self.PAGE)[0] is False

    def test_separators_still_do_not_matter(self):
        assert locate(1341792.0, self.PAGE)[0] is True  # Indian grouping
        assert locate("51824753556", self.PAGE)[0] is True  # spaced
        assert locate("24YVBFK3734K1ZQ", self.PAGE)[0] is True


# A scan, as tesseract reads it: the GSTIN's trailing letter O and the IFSC's
# fixed zero both come back as the other character.
OCR_PAGES = [
    "Kestrel Cyber Assurance Pvt Ltd TAX INVOICE GSTIN: 29PECCX8526R1Z0 "
    "Invoice No. KCA-2608-376 Invoice Date 12-08-2026 IFSC: KCBLO706240 "
    "A/c No.: 165340523820 Grand Total 15,83,315.00 " + "Sub Total " * 20
]


class TestAScanIsReadByOcr:
    """11 of 45 clean benchmark invoices are scans, and every one went to
    review: there was no text to ground anything against."""

    def _evidence(self, monkeypatch, invoice):
        monkeypatch.setattr(confidence, "extract_page_text", lambda _p: [])
        monkeypatch.setattr(confidence, "ocr_page_text", lambda _p: OCR_PAGES)
        return build_evidence(invoice, Path("scan.pdf"))

    def test_fields_are_grounded_against_the_ocr_text(self, monkeypatch):
        evidence = self._evidence(
            monkeypatch,
            {
                "invoice_number": "KCA-2608-376",
                "invoice_total_inc_tax": 1583315.0,
            },
        )
        assert evidence["_summary"]["read_by"] == "ocr"
        assert evidence["_summary"]["has_text_layer"] is False
        assert evidence["invoice_number"]["grounded"] is True

    def test_the_tax_id_tolerates_ocr_reading_o_as_zero(self, monkeypatch):
        """Safe for the GSTIN only: its checksum and the vendor master catch a
        swapped character on their own (check 3.2)."""
        evidence = self._evidence(
            monkeypatch, {"vendor_tax_id": "29PECCX8526R1ZO"}
        )
        assert evidence["vendor_tax_id"]["grounded"] is True

    def test_an_invoice_number_does_not(self, monkeypatch):
        """Duplicate detection does not fold O into 0, so a tolerated misread
        here could let a resubmission post untouched."""
        evidence = self._evidence(
            monkeypatch, {"invoice_number": "KCA-26O8-376"}
        )
        assert evidence["invoice_number"]["grounded"] is False

    def test_an_ifsc_tolerates_o_only_in_its_fixed_zero(self, monkeypatch):
        assert (
            self._evidence(monkeypatch, {"bank_code": "KCBL0706240"})[
                "bank_code"
            ]["grounded"]
            is True
        )
        assert (
            self._evidence(monkeypatch, {"bank_code": "KCBL0706249"})[
                "bank_code"
            ]["grounded"]
            is False
        )

    def test_no_tesseract_means_no_ocr_and_review(self, monkeypatch):
        monkeypatch.setattr(confidence.shutil, "which", lambda _name: None)
        assert confidence.ocr_page_text(Path("scan.pdf")) == []

    @pytest.mark.skipif(
        not confidence.shutil.which("tesseract"),
        reason="tesseract not installed",
    )
    def test_tesseract_reads_a_real_page(self):
        pages = confidence.ocr_page_text(_pdf("case_005"))
        assert locate("INV-00005", pages)[0] is True
        assert locate("51824753556", pages)[0] is True


def test_ocr_reads_bytes_like_a_path_and_stops_early(tmp_path, monkeypatch):
    """The worker's injection scan hands OCR the blob's bytes (Wave 5 B8),
    capped in pages and in time; tesseract is faked, pypdfium2 is real."""
    import subprocess  # noqa: PLC0415
    import time  # noqa: PLC0415

    from test_trust_leakage import make_pdf  # noqa: PLC0415

    calls = []

    def run(argv, *, input, **kw):
        calls.append(len(input))
        return subprocess.CompletedProcess(argv, 0, stdout=b"page")

    monkeypatch.setattr(confidence.shutil, "which", lambda _n: "/bin/tess")
    monkeypatch.setattr(confidence.subprocess, "run", run)
    data = make_pdf([["one"], ["two"], ["three"]])
    path = tmp_path / "scan.pdf"
    path.write_bytes(data)
    assert confidence.ocr_page_text(data) == confidence.ocr_page_text(path)
    assert confidence.ocr_page_text(data) == ["page"] * 3
    assert confidence.ocr_page_text(data, max_pages=2) == ["page"] * 2
    calls.clear()
    assert confidence.ocr_page_text(data, deadline=time.monotonic() - 1) == []
    assert calls == []
