"""Tier 0 critic checks must be arithmetic, not opinion.

The investigation layer's "Layer 1" was documented as deterministic but made six
gemini-2.5-pro calls per case. Those calls were close to worthless: every prompt
is prefixed with INVESTIGATOR_GUIDELINES, which tells the model to "mark as
COMPLIANT (valid=true)" when in doubt, to give "MAXIMUM BENEFIT OF THE DOUBT",
and to return compliant below 90% confidence -- and each validator's exception
handler also returns valid=True, so a failed call looks like a passing one.

ABN checksums and labour-hour comparisons are arithmetic. Computing them is
free, instant, reproducible, and yields a better audit record than a narrative,
because it cites the exact values compared.
"""

import pytest

from invoice_processing.shared_libraries.investigation import checks
from invoice_processing.shared_libraries.investigation.checks import (
    abn_checksum_valid,
    check_abn_matching,
    check_waf_hours,
    hamming_distance,
    normalize_tax_id,
)

# case_001's ABN, which genuinely fails the mod-89 checksum, and case_005's,
# which passes. Verified independently against the ABN algorithm.
INVALID_ABN = "64 560 747 633"
VALID_ABN = "51 824 753 556"


class TestAbnChecksum:
    def test_valid_abn(self):
        assert abn_checksum_valid(VALID_ABN) is True

    def test_invalid_abn(self):
        assert abn_checksum_valid(INVALID_ABN) is False

    def test_formatting_is_ignored(self):
        assert abn_checksum_valid("51824753556") is True
        assert abn_checksum_valid("51-824-753-556") is True

    @pytest.mark.parametrize(
        "value", ["", None, "123", "not-an-abn", "5182475355X"]
    )
    def test_uncheckable_returns_none_not_false(self, value):
        """None means 'no opinion'. Reporting False would invent a violation."""
        assert abn_checksum_valid(value) is None

    def test_is_deterministic(self):
        assert len({abn_checksum_valid(VALID_ABN) for _ in range(20)}) == 1


class TestHammingDistance:
    def test_identical(self):
        assert hamming_distance(VALID_ABN, "51824753556") == 0

    def test_single_digit_difference(self):
        assert hamming_distance("51824753556", "51824753557") == 1

    def test_incomparable_returns_none(self):
        assert hamming_distance("123", "") is None
        assert hamming_distance("123", "12345") is None


class TestAbnMatching:
    def test_failing_checksum_expects_rejection(self):
        result = check_abn_matching(
            INVALID_ABN, "", {"valid": False}, "AUD", "REJECT"
        )
        assert result["checksum_passed"] is False
        assert result["expected_decision"] == "REJECT"
        assert result["valid"] is True, (
            "the agent agreed, so nothing is violated"
        )

    def test_passing_checksum_expects_acceptance(self):
        result = check_abn_matching(
            VALID_ABN, "", {"valid": True}, "AUD", "Continue"
        )
        assert result["checksum_passed"] is True
        assert result["expected_decision"] == "ACCEPT"
        assert result["valid"] is True

    def test_recorded_verdict_contradicting_the_checksum_is_a_violation(self):
        """The check the LLM path could not reliably make."""
        result = check_abn_matching(
            VALID_ABN, "", {"valid": False}, "AUD", "REJECT"
        )
        assert result["valid"] is False
        assert result["severity"] == "HIGH"
        assert "disagrees" in result["reasoning"]

    def test_single_digit_reference_mismatch_is_set_aside(self):
        """One digit out is more likely OCR than a different entity."""
        result = check_abn_matching(
            "51824753556", "51824753557", {"valid": True}, "AUD", "Continue"
        )
        assert result["hamming_distance"] == 1
        assert result["ocr_error_suspected"] is True
        assert result["expected_decision"] == "SET_ASIDE"

    def test_multi_digit_mismatch_is_rejection(self):
        result = check_abn_matching(
            "51824753556", "99999999999", {"valid": True}, "AUD", "Continue"
        )
        assert result["expected_decision"] == "REJECT"

    def test_missing_tax_id_is_not_reported_as_a_violation(self):
        result = check_abn_matching("", "", {}, "AUD", "Continue")
        assert result["valid"] is True
        assert result["checksum_validated"] is False

    def test_reasoning_cites_the_values_compared(self):
        """A deterministic check should explain itself better, not worse."""
        result = check_abn_matching(INVALID_ABN, "", {}, "AUD", "REJECT")
        assert "mod-89" in result["reasoning"]
        assert result["confidence"] == 1.0
        assert result["deterministic"] is True

    def test_is_deterministic(self):
        results = {
            check_abn_matching(VALID_ABN, "", {}, "AUD", "Continue")[
                "expected_decision"
            ]
            for _ in range(20)
        }
        assert results == {"ACCEPT"}


class TestWafHours:
    def test_exempt_work_type_skips(self):
        result = check_waf_hours("PREVENTATIVE", 5.0, 0, "Continue")
        assert result["work_type_exempt"] is True
        assert result["expected_decision"] == "Skip"

    def test_hours_within_authorization_continue(self):
        result = check_waf_hours("REPAIRS", 3.0, 4.0, "Continue")
        assert result["hours_within_tolerance"] is True
        assert result["expected_decision"] == "Continue"

    def test_hours_exceeding_authorization_reject(self):
        result = check_waf_hours("REPAIRS", 8.0, 4.0, "Reject")
        assert result["hours_within_tolerance"] is False
        assert result["expected_decision"] == "Reject"

    def test_tolerance_is_applied(self):
        """4.4h against 4h authorized is inside the 0.5h tolerance."""
        result = check_waf_hours("REPAIRS", 4.4, 4.0, "Continue")
        assert result["hours_within_tolerance"] is True

    def test_labour_billed_with_no_authorization_rejects(self):
        """case_005's shape."""
        result = check_waf_hours(
            "REPAIRS", 2.5, 0, "Reject", agent_step_passed=False
        )
        assert result["expected_decision"] == "Reject"
        assert result["valid"] is True, "the agent agreed with the arithmetic"

    def test_agent_disagreeing_with_arithmetic_is_a_violation(self):
        """The agent passed a step the numbers say should fail."""
        result = check_waf_hours(
            "REPAIRS", 8.0, 4.0, "Continue", agent_step_passed=True
        )
        assert result["valid"] is False
        assert result["violated_rules"] == ["Step 4.3"]

    def test_missing_step_result_is_not_a_violation(self):
        """Absence of evidence must not be reported as evidence of a fault."""
        result = check_waf_hours(
            "REPAIRS", 8.0, 4.0, "Reject", agent_step_passed=None
        )
        assert result["valid"] is True

    def test_custom_tolerance_is_honoured(self):
        result = check_waf_hours(
            "REPAIRS", 6.0, 4.0, "Continue", tolerance_info={"tolerance": 3.0}
        )
        assert result["hours_within_tolerance"] is True

    def test_is_deterministic(self):
        results = {
            check_waf_hours("REPAIRS", 2.5, 0, "Reject")["expected_decision"]
            for _ in range(20)
        }
        assert results == {"Reject"}


def test_normalize_tax_id():
    assert normalize_tax_id("51 824 753 556") == "51824753556"
    assert normalize_tax_id(None) == ""


def test_checks_make_no_llm_calls():
    """Tier 0 must be free. Importing must not construct a model."""
    source = checks.__file__
    text = open(source).read()
    assert "generate_content" not in text
    assert "GenerativeModel" not in text
