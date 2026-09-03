"""Tests for RuleWriterAgent -- validation and formatting.

Writing rules, allocating their ids and checking for conflicts left with the
chat agent: rules live per tenant in a repository now, approving one is a
status change on a row, and the scope-conflict warning moved to the approval
route where it can read the rules actually in force.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from invoice_processing.core.rule_writer import RuleWriterAgent

_EXPECTED_RULE_COUNT = 2


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_RULE_BASE = {
    "schema_version": "2.0.0",
    "metadata": {"total_rules": 1},
    "rules": [
        {
            "id": "ALF-001",
            "name": "Test Rule",
            "scope": "customer_entity_validation",
            "priority": 50,
            "conditions": [
                {"field": "decision", "operator": "equals", "value": "REJECT"}
            ],
            "actions": [{"type": "override_decision", "value": "ACCEPT"}],
        }
    ],
}

VALID_RULE = {
    "id": "ALF-002",
    "name": "New Rule",
    "scope": "calculation_validation",
    "priority": 60,
    "conditions": [
        {"field": "decision_phase4", "operator": "equals", "value": "REJECT"},
        {
            "field": "phase4.rejection_template",
            "operator": "contains",
            "value": "false positive",
        },
    ],
    "actions": [{"type": "override_decision", "value": "ACCEPT"}],
    "metadata": {"severity": "HIGH", "root_cause": "Test"},
}


def _make_agent_with_rule_base(rule_base_dict: dict) -> RuleWriterAgent:
    """Create a RuleWriterAgent with a temp rule base file."""
    agent = RuleWriterAgent()
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    )
    json.dump(rule_base_dict, tmp, indent=2)
    tmp.close()
    return agent, Path(tmp.name)


# ---------------------------------------------------------------------------
# validate_rule
# ---------------------------------------------------------------------------


class TestValidateRule:
    def setup_method(self):
        self.agent = RuleWriterAgent()

    def test_valid_rule_no_errors(self):
        errors = self.agent.validate_rule(VALID_RULE)
        assert errors == []

    def test_missing_id(self):
        rule = {k: v for k, v in VALID_RULE.items() if k != "id"}
        errors = self.agent.validate_rule(rule)
        assert any("'id'" in e for e in errors)

    def test_missing_name(self):
        rule = {k: v for k, v in VALID_RULE.items() if k != "name"}
        errors = self.agent.validate_rule(rule)
        assert any("'name'" in e for e in errors)

    def test_missing_conditions(self):
        rule = {k: v for k, v in VALID_RULE.items() if k != "conditions"}
        errors = self.agent.validate_rule(rule)
        assert any("'conditions'" in e for e in errors)

    def test_missing_actions(self):
        rule = {k: v for k, v in VALID_RULE.items() if k != "actions"}
        errors = self.agent.validate_rule(rule)
        assert any("'actions'" in e for e in errors)

    def test_unsupported_operator(self):
        rule = {
            **VALID_RULE,
            "conditions": [{"field": "x", "operator": "FAKE_OP", "value": "y"}],
        }
        errors = self.agent.validate_rule(rule)
        assert any("unsupported operator" in e for e in errors)

    def test_unsupported_action_type(self):
        rule = {
            **VALID_RULE,
            "actions": [{"type": "teleport_data"}],
        }
        errors = self.agent.validate_rule(rule)
        assert any("unsupported type" in e for e in errors)

    def test_condition_missing_field(self):
        rule = {
            **VALID_RULE,
            "conditions": [{"operator": "equals", "value": "x"}],
        }
        errors = self.agent.validate_rule(rule)
        assert any("missing 'field'" in e for e in errors)

    def test_condition_missing_operator(self):
        rule = {
            **VALID_RULE,
            "conditions": [{"field": "x", "value": "y"}],
        }
        errors = self.agent.validate_rule(rule)
        assert any("missing 'operator'" in e for e in errors)

    def test_action_missing_type(self):
        rule = {
            **VALID_RULE,
            "actions": [{"value": "something"}],
        }
        errors = self.agent.validate_rule(rule)
        assert any("missing 'type'" in e for e in errors)


# ---------------------------------------------------------------------------
# check_conflicts
# ---------------------------------------------------------------------------
# next_rule_id
# ---------------------------------------------------------------------------
# format_rule_display
# ---------------------------------------------------------------------------


class TestFormatRuleDisplay:
    def test_includes_key_fields(self):
        agent = RuleWriterAgent()
        output = agent.format_rule_display(VALID_RULE)
        assert "ALF-002" in output
        assert "New Rule" in output
        assert "calculation_validation" in output
        assert "60" in output
        assert "Conditions:" in output
        assert "Actions:" in output

    def test_includes_metadata(self):
        agent = RuleWriterAgent()
        output = agent.format_rule_display(VALID_RULE)
        assert "Severity: HIGH" in output
        assert "Root cause: Test" in output

    def test_handles_missing_metadata(self):
        rule = {k: v for k, v in VALID_RULE.items() if k != "metadata"}
        agent = RuleWriterAgent()
        output = agent.format_rule_display(rule)
        assert "Conditions:" in output  # still works


# ---------------------------------------------------------------------------
# run (write rule to file)
