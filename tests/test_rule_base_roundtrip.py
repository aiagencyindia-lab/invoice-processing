"""Rule base load/save must not lose data.

`load()` keeps only enabled rules in memory, and `save_rule_base()` used to
rebuild the file from that filtered list. A load/save cycle therefore deleted
every disabled rule permanently, and dropped any key a future schema version
might add. For a governed rule base -- where a suspended rule is an audit record
of a decision someone made -- silent deletion is a serious bug.
"""

import json

import pytest

from invoice_processing.shared_libraries.alf_engine import ALFEngine

DISABLED_RULE = {
    "id": "ALF-900",
    "name": "Suspended rule kept for audit",
    "description": "Disabled, but must survive a load/save cycle",
    "scope": "test_scope_disabled",
    "priority": 10,
    "enabled": False,
    "tags": ["suspended"],
    "conditions": [
        {"field": "decision_phase4", "operator": "equals", "value": "REJECT"}
    ],
    "actions": [
        {
            "type": "set_field",
            "target": "Invoice Processing.Invoice Status",
            "value": "Pending Payment",
        }
    ],
    "metadata": {"root_cause": "test"},
    "future_schema_key": {"added_by": "a later version"},
}

ENABLED_RULE = {
    "id": "ALF-901",
    "name": "Active rule",
    "description": "",
    "scope": "test_scope_active",
    "priority": 20,
    "enabled": True,
    "tags": [],
    "conditions": [
        {"field": "decision_phase4", "operator": "equals", "value": "REJECT"}
    ],
    "actions": [
        {
            "type": "set_field",
            "target": "Invoice Processing.Invoice Status",
            "value": "Rejected",
        }
    ],
    "metadata": {},
}


@pytest.fixture
def rule_base_path(tmp_path):
    doc = {
        "schema_version": "2.0.0",
        "metadata": {"name": "Round-trip fixture", "version": "2.0.0"},
        "rules": [DISABLED_RULE, ENABLED_RULE],
        "unknown_top_level_key": {"kept": True},
    }
    path = tmp_path / "rule_base.json"
    path.write_text(json.dumps(doc, indent=2))
    return path


def _rules_by_id(path) -> dict:
    doc = json.loads(path.read_text())
    return {r["id"]: r for r in doc["rules"]}


def test_disabled_rule_survives_round_trip(rule_base_path):
    """The regression: a suspended rule must not be deleted by a save."""
    engine = ALFEngine(rule_base_path)
    engine.save_rule_base(rule_base_path)

    rules = _rules_by_id(rule_base_path)
    assert "ALF-900" in rules, "disabled rule was silently deleted"
    assert rules["ALF-900"]["enabled"] is False


def test_unknown_keys_survive_round_trip(rule_base_path):
    """Forward compatibility: keys we don't understand must be preserved."""
    engine = ALFEngine(rule_base_path)
    engine.save_rule_base(rule_base_path)

    doc = json.loads(rule_base_path.read_text())
    assert doc.get("unknown_top_level_key") == {"kept": True}

    rules = _rules_by_id(rule_base_path)
    assert rules["ALF-900"].get("future_schema_key") == {
        "added_by": "a later version"
    }


def test_round_trip_is_idempotent(rule_base_path):
    """load(save(load(x))) == load(save(x)) -- no drift across cycles."""
    ALFEngine(rule_base_path).save_rule_base(rule_base_path)
    first = json.loads(rule_base_path.read_text())

    ALFEngine(rule_base_path).save_rule_base(rule_base_path)
    second = json.loads(rule_base_path.read_text())

    # last_saved is expected to change; nothing else should.
    first["metadata"].pop("last_saved", None)
    second["metadata"].pop("last_saved", None)
    assert first == second


def test_enabled_rule_still_round_trips(rule_base_path):
    engine = ALFEngine(rule_base_path)
    engine.save_rule_base(rule_base_path)

    rules = _rules_by_id(rule_base_path)
    assert rules["ALF-901"]["name"] == "Active rule"
    assert rules["ALF-901"]["enabled"] is True


def test_total_rules_counts_the_whole_document(rule_base_path):
    """Metadata must not report only the active subset."""
    ALFEngine(rule_base_path).save_rule_base(rule_base_path)
    doc = json.loads(rule_base_path.read_text())
    assert doc["metadata"]["total_rules"] == len(doc["rules"]) == 2


def test_only_enabled_rules_are_evaluated(rule_base_path):
    """Preserving disabled rules must not resurrect them at runtime."""
    engine = ALFEngine(rule_base_path)
    assert [r.id for r in engine.rules] == ["ALF-901"]
