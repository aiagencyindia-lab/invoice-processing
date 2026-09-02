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
