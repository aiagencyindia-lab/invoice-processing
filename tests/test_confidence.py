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

import pytest

from invoice_processing import paths
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
    data = json.loads(
        (paths.AGENTIC_FLOW_OUT / case / "02_extraction.json").read_text()
    )
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


PAGES = ["Invoice Total: $1,069.32 dated 11/02/2026 ABN 51 824 753 556"]


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
            (paths.AGENTIC_FLOW_OUT / case / "02_extraction.json").read_text()
        )
        invoice = data.get("invoice", {})
        assert "_evidence" in invoice, "run the pipeline to populate evidence"
        assert "_routing" in invoice
        assert invoice["_evidence"]["_summary"]["has_text_layer"] is True

    def test_no_llm_call_is_involved(self):
        source = (paths.AGENT_PKG_DIR / "confidence.py").read_text()
        assert "generate_content" not in source
        assert "GenerativeModel" not in source
