"""One context builder, one field registry.

The rule-evaluation context used to be built twice: once by the live ALF engine
and once by the impact assessor, from two hand-maintained copies of the same
logic. They had already drifted on `has_waf` -- the engine read raw extraction
evidence while the loader overrode it from the phase 1 artifact -- so the impact
report an SME approved a rule against could differ from what the rule actually
did in production. That is precisely the guarantee the product sells.

These tests assert the two paths cannot diverge again, that the field list shown
to the rule-writing LLM is generated rather than hand-written, and that a rule
referencing a nonexistent field is rejected instead of silently never firing.
"""

import pytest

from invoice_processing import context
from invoice_processing.context import (
    FIELDS,
    UnknownFieldPath,
    build_case_context,
    describe_fields,
    validate_field_path,
)
from invoice_processing.core.case_loader import CaseLoaderAgent
from invoice_processing.core.prompts import RULE_DISCOVERY_SYSTEM_PROMPT
from invoice_processing.core.rule_writer import RuleWriterAgent
from invoice_processing.shared_libraries.alf_engine import ActingAgentAdapter

ARTIFACTS = {
    "extraction": {
        "waf_count": 0,
        "invoice": {
            "vendor_name": "QuickFix",
            "invoice_total_inc_tax": 1069.32,
        },
        "work_authorization": {},
    },
    "phase1": {"decision": "CONTINUE"},
    "phase2": {"decision": "CONTINUE"},
    "phase3": {"decision": "CONTINUE"},
    "phase4": {"decision": "REJECT", "rejection_template": "no authorization"},
    "transformer": {"line_items_mapped": [{"item_code": "LABOUR"}]},
}


class TestSingleBuilder:
    def test_engine_and_loader_share_one_implementation(self):
        """Both call sites must resolve to the canonical builder."""
        ctx = dict(ARTIFACTS)
        ActingAgentAdapter._promote_convenience_fields(ctx)
        assert ctx == build_case_context(ARTIFACTS)

    def test_loader_delegates_to_canonical_builder(self):
        loader_ctx = CaseLoaderAgent()._build_context(ARTIFACTS, None)
        assert loader_ctx == build_case_context(ARTIFACTS)

    def test_loader_does_not_override_has_waf(self):
        """The exact divergence, on artifacts that would trigger it.

        The old loader overrode has_waf whenever phase 1 carried the key. A test
        whose fixture omits that key cannot detect the override coming back, so
        this one supplies it deliberately.
        """
        artifacts = {
            "extraction": {"waf_count": 0, "invoice": {}},
            "phase1": {"decision": "CONTINUE", "has_waf": True},
        }
        loader_ctx = CaseLoaderAgent()._build_context(artifacts, None)

        assert loader_ctx == build_case_context(artifacts)
        assert loader_ctx["has_waf"] is False, (
            "the loader is overriding has_waf from phase 1 again"
        )
        assert loader_ctx["waf_present_phase1"] is True

    def test_builder_is_pure(self):
        """No I/O, and the caller's artifacts are not mutated."""
        before = {k: dict(v) for k, v in ARTIFACTS.items()}
        build_case_context(ARTIFACTS)
        assert ARTIFACTS == before


class TestHasWafIsUnambiguous:
    """The divergence that motivated this work."""

    def test_has_waf_is_raw_extraction_evidence(self):
        ctx = build_case_context({"extraction": {"waf_count": 2}})
        assert ctx["has_waf"] is True
        assert ctx["waf_count"] == 2

    def test_phase1_does_not_silently_override_has_waf(self):
        """The old loader let phase 1 win. Both views are now distinct fields."""
        ctx = build_case_context(
            {
                "extraction": {"waf_count": 0},
                "phase1": {"decision": "CONTINUE", "has_waf": True},
            }
        )
        assert ctx["has_waf"] is False, "raw evidence must win for has_waf"
        assert ctx["waf_present_phase1"] is True, "phase 1 view stays available"

    def test_both_waf_views_are_registered_fields(self):
        paths = {spec.path for spec in FIELDS}
        assert {"has_waf", "waf_present_phase1"} <= paths


class TestFieldRegistry:
    def test_registry_matches_what_the_builder_produces(self):
        """A registered top-level field must actually be set by the builder."""
        ctx = build_case_context(ARTIFACTS)
        for spec in FIELDS:
            if "." in spec.path or spec.path in {"decision"}:
                continue  # nested paths and postprocessing-only fields
            assert spec.path in ctx, (
                f"{spec.path} is advertised to rule authors but the builder "
                f"never sets it"
            )

    def test_description_is_generated_not_prose(self):
        rendered = describe_fields()
        for spec in FIELDS:
            assert spec.path in rendered

    def test_prompt_uses_the_generated_block(self):
        """The rule-writing LLM must see generated text, not a stale copy."""
        assert "waf_present_phase1" in RULE_DISCOVERY_SYSTEM_PROMPT, (
            "prompt is not rendering the field registry"
        )
        assert describe_fields() in RULE_DISCOVERY_SYSTEM_PROMPT


class TestFieldPathValidation:
    def test_known_path_accepted(self):
        validate_field_path("decision_phase4", "equals")

    def test_dotted_path_under_known_artifact_accepted(self):
        """Artifacts carry more fields than are worth registering."""
        validate_field_path("phase4.some_unlisted_field", "contains")

    def test_typo_rejected_with_a_suggestion(self):
        with pytest.raises(UnknownFieldPath) as exc:
            validate_field_path("decision_phase_4", "equals")
        assert "never fire" in str(exc.value)
        assert "decision_phase4" in str(exc.value)

    def test_operator_must_suit_the_field_type(self):
        with pytest.raises(UnknownFieldPath) as exc:
            validate_field_path("invoice.line_items", "equals")
        assert "any_item_contains" in str(exc.value)

    @pytest.mark.parametrize("bad", ["", "..", ".leading", "trailing.", None])
    def test_malformed_paths_rejected(self, bad):
        with pytest.raises(UnknownFieldPath):
            validate_field_path(bad)


class TestRuleWriterRejectsUnfireableRules:
    def test_rule_with_unknown_field_fails_validation(self):
        rule = {
            "id": "ALF-999",
            "name": "Typo",
            "conditions": [
                {
                    "field": "decision_phase_4",
                    "operator": "equals",
                    "value": "REJECT",
                }
            ],
            "actions": [{"type": "set_field", "target": "x", "value": "y"}],
        }
        errors = RuleWriterAgent().validate_rule(rule)
        assert any("never fire" in e for e in errors)

    def test_valid_rule_still_passes(self):
        rule = {
            "id": "ALF-999",
            "name": "Fine",
            "conditions": [
                {
                    "field": "decision_phase4",
                    "operator": "equals",
                    "value": "REJECT",
                }
            ],
            "actions": [{"type": "set_field", "target": "x", "value": "y"}],
        }
        assert RuleWriterAgent().validate_rule(rule) == []

    def test_the_shipped_rule_base_validates(self):
        """ALF-001 must not be rejected by the new check."""
        writer = RuleWriterAgent()
        for rule in writer.get_existing_rules():
            assert writer.validate_rule(rule) == [], (
                f"shipped rule {rule.get('id')} fails validation"
            )


def test_no_second_context_builder_remains():
    """Guard against a future hand-rolled copy reappearing."""
    assert not hasattr(CaseLoaderAgent, "_flatten_phase_decisions"), (
        "the duplicate context builder is back"
    )
    assert context.promote_convenience_fields.__module__ == (
        "invoice_processing.context"
    )
