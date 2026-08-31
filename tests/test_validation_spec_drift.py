"""Tests for validation steps whose code disagreed with their own spec.

Two classes of bug are covered here:

1. Step 4.1 was declared `informational_only: true` in invoice_master_data.yaml
   and documented as informational in the rules book, but the code let it fail
   the phase -- producing a REJECT whose `rejection_template` was None, i.e. a
   rejection with no stated reason.
2. Steps 2.2 and 3.1 are not implemented, but reported `"passed": True` with no
   marking. A control that reports success without running is worse than a
   missing one, because a reviewer will believe it.
"""

import pytest

from invoice_processing.shared_libraries.acting import general_invoice_agent
from invoice_processing.shared_libraries.acting.general_invoice_agent import (
    Phase2ValidatorAgent,
    Phase3ValidatorAgent,
    Phase4ValidatorAgent,
)


def set_config(**overrides):
    """Override pipeline config for a test.

    There is no injection point: config lives in the module-level mutable
    `_config_store` global. Phase 0.2 replaces this with an EngineSettings
    object passed through RunContext, at which point this helper disappears.
    """
    config = general_invoice_agent.DEFAULT_CONFIG.copy()
    config.update(overrides)
    general_invoice_agent._config_store["CONFIG"] = config


def reset_config():
    general_invoice_agent._config_store["CONFIG"] = (
        general_invoice_agent.DEFAULT_CONFIG.copy()
    )


def step(result: dict, step_id: str) -> dict:
    return next(v for v in result["validations"] if v["step"] == step_id)


def find_step(result: dict, step_id: str) -> dict | None:
    return next(
        (v for v in result["validations"] if v["step"] == step_id), None
    )


# total_inc deliberately disagrees with ex + tax by $100.
BROKEN_TOTALS = {
    "invoice_total_ex_tax": 100.00,
    "invoice_total_inc_tax": 210.00,
    "tax_amount": 10.00,
    "currency": "AUD",
    "line_items": [
        {"description": "Service", "amount_ex_tax": 100.00, "quantity": 1}
    ],
}


class TestStep41IsInformational:
    """A broken total must not produce a reason-less rejection."""

    def test_step_41_records_the_failure(self, tmp_path):
        result = Phase4ValidatorAgent().run(
            tmp_path, {"invoice": BROKEN_TOTALS}, {"decision": "CONTINUE"}
        )
        s41 = step(result, "4.1")
        assert s41["passed"] is False, "the arithmetic really is broken"
        assert s41["informational"] is True

    def test_step_41_alone_does_not_reject(self, tmp_path):
        """The regression: informational failures must not drive the decision."""
        result = Phase4ValidatorAgent().run(
            tmp_path, {"invoice": BROKEN_TOTALS}, {"decision": "CONTINUE"}
        )
        assert result["decision"] == "ACCEPT"

    def test_no_reason_less_rejection(self, tmp_path):
        """Any REJECT must carry a template explaining why."""
        result = Phase4ValidatorAgent().run(
            tmp_path, {"invoice": BROKEN_TOTALS}, {"decision": "CONTINUE"}
        )
        if result["decision"] == "REJECT":
            assert result["rejection_template"], (
                "rejected without a stated reason"
            )

    def test_real_failures_still_reject(self, tmp_path):
        """Guard against over-correcting: 4.2 must still be able to reject."""
        invoice = {
            "invoice_total_ex_tax": 100.00,
            "invoice_total_inc_tax": 110.00,
            "tax_amount": 10.00,
            "currency": "AUD",
            # Line sum of 5.00 is nowhere near the 100.00 subtotal.
            "line_items": [
                {"description": "Service", "amount_ex_tax": 5.00, "quantity": 1}
            ],
        }
        result = Phase4ValidatorAgent().run(
            tmp_path, {"invoice": invoice}, {"decision": "CONTINUE"}
        )
        assert result["decision"] == "REJECT"
        assert (
            result["rejection_template"] == "Invoice amounts do not reconcile"
        )


class TestUnimplementedChecksAreMarked:
    """Enabling an unimplemented control must not look like a clean pass."""

    @pytest.fixture(autouse=True)
    def _restore_config(self):
        yield
        reset_config()

    def test_po_check_is_flagged_when_enabled(self, tmp_path):
        set_config(require_purchase_order=True)
        result = Phase2ValidatorAgent().run(
            tmp_path,
            {"invoice": {"line_items": [{"description": "x"}]}},
            {"decision": "CONTINUE"},
        )
        s22 = find_step(result, "2.2")
        assert s22 is not None, "step 2.2 should appear when the flag is on"
        assert s22["implemented"] is False
        assert s22["status"] == "NOT_IMPLEMENTED"
        assert "NOT IMPLEMENTED" in s22["evidence"]

    def test_duplicate_check_is_now_implemented(self, tmp_path):
        """Step 3.1 was a stub reporting success. It now really runs.

        Kept here as the record that the stub is gone; the detection logic
        itself is covered by test_duplicate_detection.py.
        """
        set_config(duplicate_check_enabled=True)
        result = Phase3ValidatorAgent().run(
            tmp_path,
            {"invoice": {"invoice_date": "2026-01-01"}},
            {"decision": "CONTINUE"},
        )
        s31 = find_step(result, "3.1")
        assert s31 is not None, "step 3.1 should appear when the flag is on"
        assert s31["implemented"] is True
        assert s31.get("status") != "NOT_IMPLEMENTED"
        assert "NOT IMPLEMENTED" not in s31["evidence"]

    def test_implemented_checks_are_not_falsely_flagged(self, tmp_path):
        """Real checks must not carry the not-implemented marker."""
        result = Phase4ValidatorAgent().run(
            tmp_path,
            {
                "invoice": {
                    "invoice_total_ex_tax": 100.00,
                    "invoice_total_inc_tax": 110.00,
                    "tax_amount": 10.00,
                    "currency": "AUD",
                    "line_items": [
                        {"description": "Service", "amount_ex_tax": 100.00}
                    ],
                }
            },
            {"decision": "CONTINUE"},
        )
        for validation in result["validations"]:
            assert validation.get("implemented") is not False, (
                f"step {validation['step']} wrongly marked unimplemented"
            )
