"""The drafting path, with only the model itself stubbed.

Everything else that tests drafting replaces the whole orchestrator, which is
how the discoverer came to be calling a method that had been deleted -- the
first time anyone found out was running it against real Vertex, where it threw
`AttributeError` before it reached the model at all.

So this exercises the real `RuleDiscovererAgent` and its real prompt building,
and fakes exactly one thing: the call to Gemini.
"""

from __future__ import annotations

import json

import pytest

from invoice_processing.core import rule_discoverer
from invoice_processing.core.case_loader import CaseData
from invoice_processing.core.rule_discoverer import RuleDiscovererAgent

DRAFTED = {
    "id": "ALF-007",
    "name": "WAF exemption for low-value repairs",
    "description": "Policy exempts repairs under $2,000.",
    "scope": "WAF_EXEMPTION",
    "priority": 50,
    "conditions": [
        {
            "field": "decision_phase4",
            "operator": "equals",
            "value": "REJECT",
            "description": "Phase 4 rejected it",
        }
    ],
    "actions": [
        {
            "type": "set_field",
            "target": "Invoice Processing.Invoice Status",
            "value": "Pending Payment",
            "description": "Accept it",
        }
    ],
}


@pytest.fixture
def model(monkeypatch):
    """The only thing faked. Records the prompt it was given."""
    seen = {}

    def generate(prompt: str):
        seen["prompt"] = prompt
        return json.dumps(DRAFTED), 120

    monkeypatch.setattr(rule_discoverer._LLMClient, "generate", generate)
    return seen


@pytest.fixture
def case():
    data = CaseData(case_id="case_005")
    data.artifacts = {
        "extraction": {"invoice": {"vendor_name": "QuickFix HVAC"}},
        "phase4": {
            "decision": "REJECT",
            "rejection_template": "Invoice does not match work authorization",
        },
    }
    data.context = {
        "decision_phase4": "REJECT",
        "invoice": {"vendor_name": "QuickFix HVAC"},
        "phase4": {
            "decision": "REJECT",
            "rejection_template": "Invoice does not match work authorization",
        },
    }
    data.postprocessing = {"Invoice Processing": {"Invoice Status": "Rejected"}}
    return data


class TestDraftingNeedsNothingFromDisk:
    def test_it_drafts_without_a_rule_file(self, case, model):
        """It used to read the rule base off disk to build its prompt. That
        file is nobody's rule base now -- rules are per tenant, in a
        repository -- and reaching for it threw before the model was called."""
        proposed = RuleDiscovererAgent().run(case, "policy exempts repairs")

        assert proposed.success, proposed.parse_errors
        assert proposed.rule_dict["conditions"]

    def test_the_rules_in_force_reach_the_prompt(self, case, model):
        """The model has to see what is already there: only one rule per scope
        fires, so drafting into an occupied scope writes a rule that will
        silently shadow another."""
        existing = [
            {
                "id": "ALF-001",
                "name": "An existing rule",
                "scope": "WAF_EXEMPTION",
                "conditions": [],
            }
        ]

        RuleDiscovererAgent().run(
            case, "policy exempts repairs", existing_rules=existing
        )

        assert "ALF-001" in model["prompt"]
        assert "WAF_EXEMPTION" in model["prompt"]

    def test_the_id_comes_from_whoever_owns_the_rule_base(self, case, model):
        """Not from a max+1 scan of a file, which is how two people teaching a
        rule at the same time both got ALF-004."""
        proposed = RuleDiscovererAgent().run(
            case, "policy exempts repairs", next_id="ALF-042"
        )

        assert proposed.rule_dict["id"] == "ALF-042"
