"""Tests for Phase 4 Step 4.4 (Tax Rate Verification).

Step 4.1 only checks that total = subtotal + tax, which still holds when the
tax was computed at the wrong rate. Step 4.4 is what catches the wrong rate.
"""

import json
from pathlib import Path

import pytest

from invoice_processing.shared_libraries.acting.general_invoice_agent import (
    Phase4ValidatorAgent,
)
from tests.test_validation_spec_drift import reset_config, set_config

AGENT_DIR = Path(__file__).resolve().parent.parent


def run_phase4(tmp_path, invoice: dict) -> dict:
    """Run the Phase 4 validator over a bare invoice and return its output."""
    agent = Phase4ValidatorAgent()
    return agent.run(tmp_path, {"invoice": invoice}, {"decision": "CONTINUE"})


def step(result: dict, step_id: str) -> dict:
    return next(v for v in result["validations"] if v["step"] == step_id)


def invoice(total_ex: float, tax: float, currency: str = "AUD") -> dict:
    return {
        "invoice_total_ex_tax": total_ex,
        "invoice_total_inc_tax": round(total_ex + tax, 2),
        "tax_amount": tax,
        "currency": currency,
        "line_items": [
            {"description": "Service", "amount_ex_tax": total_ex, "quantity": 1}
        ],
    }


class TestInvoicesStatingNoTax:
    """An invoice with no tax amount must be decided, not crash the pipeline.

    `.get("tax_amount", 0)` never fired its default: the extractor writes the
    key with a null when it cannot find the figure. The addition then raised
    TypeError, the worker retried three times and dead-lettered a perfectly
    readable invoice with no decision and no explanation.
    """

    def test_a_missing_tax_amount_does_not_crash(self, tmp_path):
        no_tax = invoice(52250.00, 0.00)
        no_tax["tax_amount"] = None

        result = run_phase4(tmp_path, no_tax)

        assert step(result, "4.1")["passed"] is True

    def test_the_assumption_is_recorded_not_hidden(self, tmp_path):
        """A check that supplies a number it never read has to say so."""
        no_tax = invoice(52250.00, 0.00)
        no_tax["tax_amount"] = None

        result = run_phase4(tmp_path, no_tax)

        assert "no tax amount stated" in step(result, "4.1")["evidence"]

    def test_a_stated_zero_reads_as_stated(self, tmp_path):
        result = run_phase4(tmp_path, invoice(52250.00, 0.00))
        assert "no tax amount stated" not in step(result, "4.1")["evidence"]

    def test_missing_totals_do_not_crash_either(self, tmp_path):
        blank = invoice(100.00, 10.00)
        blank["invoice_total_inc_tax"] = None
        blank["invoice_total_ex_tax"] = None

        result = run_phase4(tmp_path, blank)

        assert step(result, "4.1")["passed"] is not None


class TestTaxRateVerification:
    def test_correct_gst_passes(self, tmp_path):
        result = run_phase4(tmp_path, invoice(505.00, 50.50))
        assert step(result, "4.4")["passed"] is True
        assert result["decision"] == "ACCEPT"

    def test_wrong_rate_is_rejected(self, tmp_path):
        # case_001: $95.70 stated on a $638.00 base is 15%, not the 10% claimed.
        result = run_phase4(tmp_path, invoice(638.00, 95.70))
        assert step(result, "4.4")["passed"] is False
        assert result["decision"] == "REJECT"
        assert result["rejection_template"] == "Invoice tax amount incorrect"

    def test_step_4_1_alone_would_miss_it(self, tmp_path):
        """The planted error is self-consistent, so 4.1 cannot catch it."""
        result = run_phase4(tmp_path, invoice(638.00, 95.70))
        assert step(result, "4.1")["passed"] is True

    def test_rounding_stays_within_tolerance(self, tmp_path):
        # case_005: 972.11 * 0.10 = 97.211, which rounds to the stated 97.21.
        result = run_phase4(tmp_path, invoice(972.11, 97.21))
        assert step(result, "4.4")["passed"] is True

    @pytest.mark.parametrize(
        ("currency", "tax", "expected_pass"),
        [
            ("NZD", 15.00, True),  # NZD is configured at 15%
            ("NZD", 10.00, False),
            ("USD", 0.00, True),  # USD is configured at 0%
            ("XYZ", 10.00, True),  # unknown currency falls back to 10%
        ],
    )
    def test_rate_follows_currency(
        self, tmp_path, currency, tax, expected_pass
    ):
        result = run_phase4(tmp_path, invoice(100.00, tax, currency))
        assert step(result, "4.4")["passed"] is expected_pass

    def test_missing_currency_does_not_crash(self, tmp_path):
        inv = invoice(100.00, 10.00)
        inv["currency"] = None
        assert step(run_phase4(tmp_path, inv), "4.4")["passed"] is True


def test_waf_rejection_still_takes_precedence(tmp_path):
    """4.3 is appended before 4.4, so a WAF failure is reported first.

    Step 4.3 is gated on `require_work_authorization`, so the flag must be on
    for it to run at all -- see test_step_43_gating.py.
    """
    inv = invoice(1000.00, 500.00)  # also fails 4.4
    inv["line_items"] = [
        {
            "description": "Technician labour",
            "amount_ex_tax": 1000.00,
            "quantity": 8,
        }
    ]
    set_config(require_work_authorization=True)
    try:
        result = run_phase4(tmp_path, inv)
    finally:
        reset_config()
    assert result["decision"] == "REJECT"
    assert (
        result["rejection_template"]
        == "Invoice does not match work authorization"
    )


def test_ground_truth_line_tax_matches_its_own_header():
    """case_001's fixture was internally inconsistent; guard the correction."""
    gt_dir = AGENT_DIR / "invoice_processing" / "exemplary_data"
    for case in ("case_001", "case_002", "case_005"):
        gt = json.loads(
            (gt_dir / case / "Postprocessing_Data.json").read_text()
        )
        lines = json.loads(gt["Line Items"])
        line_tax = round(
            sum(float(str(x["tax"]).replace(",", "")) for x in lines), 2
        )
        header_tax = float(
            str(gt["Invoice Details"]["Tax Amount"]).replace(",", "")
        )
        assert line_tax == pytest.approx(header_tax, abs=0.02), case
