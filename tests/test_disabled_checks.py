"""Switching a validation step off.

Turning off a check is a governance decision, not a preference: 3.1 is what
stands between a duplicate invoice and a duplicate payment, and duplicate
detection shipped switched off once already. So the tests here weigh most
heavily on what a disabled step is *recorded* as, and on what cannot be
switched off at all.
"""

import pytest

from invoice_processing import checks, tenant_settings
from invoice_processing.shared_libraries.acting import general_invoice_agent


class TestTheCatalogue:
    def test_it_lists_every_step_the_pipeline_runs(self):
        ids = checks.known_ids()
        assert {"1.1", "1.4", "2.2", "2.3", "3.1", "3.4", "4.3", "4.4"} <= ids
        assert len(ids) == 16

    def test_it_comes_from_the_rulebook_not_a_second_list(self):
        """A hand-kept copy drifts, and the screen then offers a switch for a
        step the engine no longer runs."""
        source = (
            __import__("pathlib")
            .Path("invoice_processing/checks.py")
            .read_text(encoding="utf-8")
        )
        assert "get_validation_phases" in source
        for hardcoded in ('"1.2"', '"3.1"', '"4.4"'):
            assert hardcoded not in source, hardcoded

    def test_extraction_success_is_mandatory(self):
        assert "1.1" in checks.MANDATORY
        entry = next(s for s in checks.catalogue() if s["id"] == "1.1")
        assert entry["mandatory"] is True


class TestValidation:
    def test_it_accepts_known_steps(self):
        cleaned = tenant_settings.validate({"disabled_checks": ["3.1", "2.2"]})
        assert cleaned["disabled_checks"] == ["2.2", "3.1"]

    def test_it_refuses_an_unknown_step(self):
        with pytest.raises(tenant_settings.SettingsError) as caught:
            tenant_settings.validate({"disabled_checks": ["9.9"]})
        assert "No such validation step" in str(caught.value)

    def test_it_refuses_to_switch_off_extraction(self):
        with pytest.raises(tenant_settings.SettingsError) as caught:
            tenant_settings.validate({"disabled_checks": ["1.1"]})
        assert "cannot be switched off" in str(caught.value)

    def test_mandatory_steps_are_ignored_even_if_stored(self):
        """Defence in depth: a value written before this rule existed, or by
        another path, must not disable extraction."""
        assert checks.disabled_from({"disabled_checks": ["1.1", "3.1"]}) == {
            "3.1"
        }


class TestASkippedStepIsNotAPassedStep:
    """The distinction is the whole point of an auditable decision."""

    def _settle(self, validations, disabled):
        original = general_invoice_agent._get_config()
        general_invoice_agent._config_store["CONFIG"] = {
            **original,
            "disabled_checks": disabled,
        }
        try:
            return general_invoice_agent._settle(validations)
        finally:
            general_invoice_agent._config_store["CONFIG"] = original

    def test_a_disabled_failing_step_stops_rejecting(self):
        validations = [
            {
                "step": "3.1",
                "passed": False,
                "evidence": "Duplicate of X",
                "rejection_template": "Invoice already processed",
            },
        ]
        failed = self._settle(validations, ["3.1"])
        assert failed == []

    def test_it_is_recorded_as_skipped_never_as_passed(self):
        validations = [
            {
                "step": "3.1",
                "passed": False,
                "evidence": "Duplicate of X",
                "rejection_template": "Invoice already processed",
            },
        ]
        self._settle(validations, ["3.1"])
        entry = validations[0]
        assert entry["skipped"] is True
        assert entry["passed"] is None, "a check nobody ran has no verdict"
        assert entry["rejection_template"] is None
        assert "Skipped" in entry["evidence"]
        # The original evidence survives, so the trail still says what the
        # check would have found.
        assert "Duplicate of X" in entry["evidence"]

    def test_an_enabled_step_still_rejects(self):
        validations = [
            {
                "step": "3.1",
                "passed": False,
                "evidence": "Duplicate",
                "rejection_template": "Invoice already processed",
            },
        ]
        assert len(self._settle(validations, [])) == 1

    def test_informational_steps_never_reject_in_any_phase(self):
        """Only one of the four phases used to exclude them, so an
        informational check could reject an invoice in the other three."""
        validations = [
            {
                "step": "4.1",
                "passed": False,
                "informational": True,
                "evidence": "Total differs",
            },
        ]
        assert self._settle(validations, []) == []
