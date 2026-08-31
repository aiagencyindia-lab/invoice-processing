"""Step 4.3 (WAF hours) must honour the `require_work_authorization` gate.

Both invoice_master_data.yaml (`enabled_when_config: require_work_authorization`)
and rules book Step 4.3 declare this step conditional, but the code ran it
unconditionally. An organization that does not require work authorization still
had invoices rejected for lacking one -- the code was stricter than either spec.

Consequence of the fix: with the default config (`require_work_authorization`
is False), an invoice billing labour with no WAF is no longer rejected at 4.3.
"""

import pytest

from invoice_processing.shared_libraries.acting.general_invoice_agent import (
    Phase4ValidatorAgent,
)
from tests.test_validation_spec_drift import reset_config, set_config

# Mirrors case_005: labour billed, no work authorization attached, and the
# arithmetic is otherwise clean so only 4.3 could reject it.
LABOUR_NO_WAF = {
    "invoice_total_ex_tax": 972.11,
    "invoice_total_inc_tax": 1069.32,
    "tax_amount": 97.21,
    "currency": "AUD",
    "line_items": [
        {
            "description": "Gasket and seal kit",
            "amount_ex_tax": 659.61,
            "quantity": 3,
        },
        {
            "description": "Technician labour - standard rate",
            "amount_ex_tax": 312.50,
            "quantity": 2.5,
        },
    ],
}


@pytest.fixture(autouse=True)
def _restore_config():
    yield
    reset_config()


def run_phase4(tmp_path, invoice: dict) -> dict:
    return Phase4ValidatorAgent().run(
        tmp_path, {"invoice": invoice}, {"decision": "CONTINUE"}
    )


def step_43(result: dict) -> dict | None:
    return next((v for v in result["validations"] if v["step"] == "4.3"), None)


def test_step_43_skipped_when_authorization_not_required(tmp_path):
    """The fix: no WAF requirement means no WAF rejection."""
    result = run_phase4(tmp_path, LABOUR_NO_WAF)

    assert step_43(result) is None, "4.3 must not run when the gate is off"
    assert result["decision"] == "ACCEPT"
    assert result["rejection_template"] is None


def test_step_43_rejects_when_authorization_required(tmp_path):
    """With the gate on, unauthorized labour is still caught."""
    set_config(require_work_authorization=True)
    result = run_phase4(tmp_path, LABOUR_NO_WAF)

    s43 = step_43(result)
    assert s43 is not None, "4.3 must run when the gate is on"
    assert s43["passed"] is False
    assert result["decision"] == "REJECT"
    assert result["rejection_template"] == (
        "Invoice does not match work authorization"
    )


def test_hours_within_authorization_passes(tmp_path):
    """A WAF covering the billed hours passes regardless of the gate."""
    set_config(require_work_authorization=True)
    result = Phase4ValidatorAgent().run(
        tmp_path,
        {
            "invoice": LABOUR_NO_WAF,
            "work_authorization": {"authorized_hours": 4.0},
        },
        {"decision": "CONTINUE"},
    )

    s43 = step_43(result)
    assert s43 is not None
    assert s43["passed"] is True
    assert result["decision"] == "ACCEPT"


def test_hours_exceeding_authorization_rejects(tmp_path):
    set_config(require_work_authorization=True)
    result = Phase4ValidatorAgent().run(
        tmp_path,
        {
            "invoice": LABOUR_NO_WAF,
            "work_authorization": {"authorized_hours": 1.0},
        },
        {"decision": "CONTINUE"},
    )

    assert step_43(result)["passed"] is False
    assert result["decision"] == "REJECT"


def test_gate_does_not_suppress_other_phase4_checks(tmp_path):
    """Guard against over-correcting: 4.4 must still fire with the gate off."""
    wrong_tax = dict(LABOUR_NO_WAF, tax_amount=145.82)  # 15%, not 10%
    result = run_phase4(tmp_path, wrong_tax)

    assert result["decision"] == "REJECT"
    assert result["rejection_template"] == "Invoice tax amount incorrect"
