"""Duplicate detection: the cheapest expensive mistake to prevent.

Step 3.1 used to report `"passed": True, "evidence": "Duplicate check not
implemented"` -- announcing success without running. Paying an invoice twice is
among the most common AP losses and needs no model to catch.

The tests below split into two halves that matter differently. Detection tests
prove real duplicates are caught. Discrimination tests prove legitimate repeat
billing is not: a monthly retainer at the same amount from the same vendor is
normal business, and a control that blocks it gets switched off.
"""

import json

import pytest
import yaml

from invoice_processing import paths
from invoice_processing.duplicates import (
    EXACT,
    FUZZY,
    NORMALIZED,
    InvoiceKey,
    find_duplicates,
    normalize_number,
    normalize_vendor,
    same_vendor,
    to_validation,
)
from invoice_processing.shared_libraries.acting import (
    general_invoice_agent as agent,
)
from invoice_processing.shared_libraries.acting import (
    general_invoice_agent as gia,
)

ORIGINAL = InvoiceKey(
    case_id="case_100",
    vendor_name="QuickFix HVAC Repairs Pty Ltd",
    vendor_tax_id="51 824 753 556",
    invoice_number="INV-00042",
    invoice_date="2026-02-11",
    total=1069.32,
)


def _variant(**overrides) -> InvoiceKey:
    base = {
        "case_id": "case_200",
        "vendor_name": ORIGINAL.vendor_name,
        "vendor_tax_id": ORIGINAL.vendor_tax_id,
        "invoice_number": ORIGINAL.invoice_number,
        "invoice_date": ORIGINAL.invoice_date,
        "total": ORIGINAL.total,
    }
    base.update(overrides)
    return InvoiceKey(**base)


class TestDetectsDuplicates:
    def test_exact_resubmission(self):
        matches = find_duplicates(_variant(), [ORIGINAL])
        assert matches and matches[0].match_type == EXACT
        assert matches[0].case_id == "case_100"

    @pytest.mark.parametrize(
        "number", ["inv-00042", "INV 42", "INV42", "inv_0042", "INV-42"]
    )
    def test_number_formatting_variants(self, number):
        """'INV-0042' and 'inv 42' are one invoice re-sent."""
        matches = find_duplicates(_variant(invoice_number=number), [ORIGINAL])
        assert matches, f"{number!r} not matched"
        assert matches[0].match_type in (EXACT, NORMALIZED)

    def test_identical_file_is_caught_regardless_of_extraction(self):
        """A byte-identical file does not depend on extraction being right."""
        prior = InvoiceKey(case_id="case_100", file_sha256="abc123")
        candidate = InvoiceKey(case_id="case_200", file_sha256="abc123")
        matches = find_duplicates(candidate, [prior])
        assert matches and matches[0].match_type == EXACT
        assert "Identical file" in matches[0].reason

    def test_resubmission_under_a_new_number(self):
        """Same vendor, same amount, days apart, different number."""
        matches = find_duplicates(
            _variant(invoice_number="INV-00099", invoice_date="2026-02-14"),
            [ORIGINAL],
        )
        assert matches and matches[0].match_type == FUZZY
        assert "possible resubmission" in matches[0].reason

    def test_amount_within_tolerance_still_matches(self):
        matches = find_duplicates(
            _variant(invoice_number="INV-00099", total=1069.32 * 1.002),
            [ORIGINAL],
        )
        assert matches and matches[0].match_type == FUZZY


class TestDoesNotOverReach:
    """Legitimate billing must pass, or the control gets disabled."""

    def test_different_vendor_same_number_is_not_a_duplicate(self):
        """Invoice numbers are only unique within a vendor."""
        other = _variant(
            vendor_name="Precision Tech Co", vendor_tax_id="64 560 747 633"
        )
        assert find_duplicates(other, [ORIGINAL]) == []

    def test_same_name_different_tax_id_is_a_different_entity(self):
        """A tax ID identifies an entity; a trading name does not."""
        other = _variant(
            vendor_tax_id="64 560 747 633", invoice_number="INV-00099"
        )
        assert find_duplicates(other, [ORIGINAL]) == []

    def test_recurring_monthly_invoice_is_not_flagged(self):
        """Same vendor and amount a month apart is a retainer, not a duplicate."""
        monthly = _variant(
            invoice_number="INV-00099", invoice_date="2026-03-11"
        )
        assert find_duplicates(monthly, [ORIGINAL]) == []

    def test_different_amount_is_not_flagged(self):
        different = _variant(invoice_number="INV-00099", total=2500.00)
        assert find_duplicates(different, [ORIGINAL]) == []

    def test_an_invoice_never_matches_itself(self):
        assert find_duplicates(ORIGINAL, [ORIGINAL]) == []

    def test_credit_note_prefix_is_not_the_invoice(self):
        """INV-42 and CN-42 are different documents."""
        assert normalize_number("INV-42") != normalize_number("CN-42")

    def test_missing_dates_do_not_produce_a_fuzzy_match(self):
        """Absent data must not manufacture a suspicion."""
        matches = find_duplicates(
            _variant(invoice_number="INV-00099", invoice_date=""),
            [InvoiceKey(**{**ORIGINAL.__dict__, "invoice_date": ""})],
        )
        assert matches == []


class TestNormalization:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("INV-0042", "INV42"),
            ("inv 42", "INV42"),
            ("INV42", "INV42"),
            ("0042", "42"),
            ("", ""),
        ],
    )
    def test_number(self, raw, expected):
        assert normalize_number(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("QuickFix HVAC Repairs Pty Ltd", "QUICKFIX HVAC REPAIRS"),
            ("QuickFix HVAC Repairs", "QUICKFIX HVAC REPAIRS"),
            ("Acme Inc", "ACME"),
        ],
    )
    def test_vendor(self, raw, expected):
        assert normalize_vendor(raw) == expected

    def test_tax_id_beats_name(self):
        a = InvoiceKey(
            "a", vendor_name="Different Name", vendor_tax_id="51824753556"
        )
        b = InvoiceKey(
            "b", vendor_name="Another Name", vendor_tax_id="51 824 753 556"
        )
        assert same_vendor(a, b) is True

    def test_name_used_when_no_tax_id(self):
        a = InvoiceKey("a", vendor_name="QuickFix HVAC Repairs Pty Ltd")
        b = InvoiceKey("b", vendor_name="QuickFix HVAC Repairs")
        assert same_vendor(a, b) is True


class TestValidationEntry:
    def test_clean_invoice_passes(self):
        entry = to_validation([])
        assert entry["passed"] is True
        assert entry["implemented"] is True

    def test_exact_match_blocks_with_a_reason(self):
        matches = find_duplicates(_variant(), [ORIGINAL])
        entry = to_validation(matches)
        assert entry["passed"] is False
        assert entry["rejection_template"] == "Invoice already processed"
        assert "case_100" in entry["matched_cases"]

    def test_fuzzy_match_warns_without_blocking(self):
        """A suspicion must not reject on its own."""
        matches = find_duplicates(
            _variant(invoice_number="INV-00099", invoice_date="2026-02-14"),
            [ORIGINAL],
        )
        entry = to_validation(matches)
        assert entry["passed"] is True, "a fuzzy match alone must not reject"
        assert entry["rejection_template"] is None
        assert "review recommended" in entry["evidence"]


class TestPipelineIntegration:
    def test_step_31_runs_against_real_cases(self, tmp_path):
        original = gia._config_store["CONFIG"]
        gia._config_store["CONFIG"] = {
            **original,
            "duplicate_check_enabled": True,
        }
        try:
            result = gia.Phase3ValidatorAgent().run(
                tmp_path,
                {
                    "invoice": {
                        "invoice_date": "2026-01-01",
                        "vendor_name": "Nobody",
                    }
                },
                {"decision": "CONTINUE"},
            )
        finally:
            gia._config_store["CONFIG"] = original

        entry = next(v for v in result["validations"] if v["step"] == "3.1")
        assert entry["implemented"] is True
        assert entry["passed"] is True, "an unseen vendor is not a duplicate"

    def test_reprocessing_a_known_case_is_detected(self, tmp_path):
        """The real scenario: the same invoice submitted twice."""
        invoice = json.loads(
            (
                paths.AGENTIC_FLOW_OUT / "case_005" / "02_extraction.json"
            ).read_text()
        )["invoice"]

        original = gia._config_store["CONFIG"]
        gia._config_store["CONFIG"] = {
            **original,
            "duplicate_check_enabled": True,
        }
        try:
            # tmp_path stands in for a *new* case carrying case_005's content.
            result = gia.Phase3ValidatorAgent().run(
                tmp_path, {"invoice": invoice}, {"decision": "CONTINUE"}
            )
        finally:
            gia._config_store["CONFIG"] = original

        entry = next(v for v in result["validations"] if v["step"] == "3.1")
        assert entry["passed"] is False, "resubmitted invoice was not caught"
        assert "case_005" in entry["matched_cases"]


class TestEnabledByDefault:
    """It shipped switched off, so a pilot ran with no duplicate check at all.

    A duplicate invoice is a duplicate payment -- the most expensive routine
    error in accounts payable -- so the default matters as much as the logic.
    These tests pin the default and prove the check actually fires through the
    real Phase 3 validator, not just in isolation.
    """

    def test_the_effective_default_is_on(self):
        """The layered value the pipeline actually reads.

        Master data is layered over the hardcoded fallback, so asserting the
        fallback alone would pass even with the YAML switched off -- which is
        the authoritative source and the one that would really disable the
        check.
        """

        assert agent._get_config()["duplicate_check_enabled"] is True
        assert agent.DEFAULT_CONFIG["duplicate_check_enabled"] is True

    def test_the_master_data_default_is_on(self):
        data = yaml.safe_load(
            (
                paths.AGENT_PKG_DIR
                / "shared_libraries"
                / "invoice_master_data.yaml"
            ).read_text(encoding="utf-8")
        )
        config = data["config"] if "config" in data else data
        found = _find_key(config, "duplicate_check_enabled")
        assert found is True, f"master data has {found!r}"

    def test_an_exact_duplicate_blocks_the_step(self, tmp_path, monkeypatch):
        """End to end through the validator the pipeline actually calls."""

        invoice = {
            "vendor_name": "Acme Pty Ltd",
            "invoice_number": "INV-9001",
            "invoice_total_inc_tax": 1100.0,
            "invoice_date": "2026-02-01",
        }
        prior = tmp_path / "case_prior"
        prior.mkdir()
        (prior / "02_extraction.json").write_text(
            json.dumps({"invoice": invoice})
        )
        monkeypatch.setattr(agent, "OUTPUT_BASE_DIR", tmp_path)

        candidate = InvoiceKey.from_invoice("case_new", invoice)
        matches = find_duplicates(
            candidate, agent._load_seen_invoices(exclude_case_id="case_new")
        )
        entry = to_validation(matches)

        assert entry["passed"] is False, (
            "an exact duplicate was allowed through"
        )
        assert entry["rejection_template"] == "Invoice already processed"
        assert "case_prior" in entry["matched_cases"]

    def test_a_fuzzy_match_warns_without_blocking(self, tmp_path, monkeypatch):
        """Retainers and subscriptions bill the same amount every month."""

        prior = tmp_path / "case_prior"
        prior.mkdir()
        (prior / "02_extraction.json").write_text(
            json.dumps(
                {
                    "invoice": {
                        "vendor_name": "Acme Pty Ltd",
                        "invoice_number": "RET-0001",
                        "invoice_total_inc_tax": 1100.0,
                        "invoice_date": "2026-02-01",
                    }
                }
            )
        )
        monkeypatch.setattr(agent, "OUTPUT_BASE_DIR", tmp_path)

        candidate = InvoiceKey.from_invoice(
            "case_new",
            {
                "vendor_name": "Acme Pty Ltd",
                "invoice_number": "RET-0002",  # a different invoice
                "invoice_total_inc_tax": 1100.0,
                "invoice_date": "2026-02-03",
            },
        )
        entry = to_validation(
            find_duplicates(candidate, agent._load_seen_invoices("case_new"))
        )
        assert entry["passed"] is True, "legitimate repeat billing was blocked"

    def test_a_distinct_invoice_is_not_flagged(self, tmp_path, monkeypatch):

        prior = tmp_path / "case_prior"
        prior.mkdir()
        (prior / "02_extraction.json").write_text(
            json.dumps(
                {
                    "invoice": {
                        "vendor_name": "Globex Inc",
                        "invoice_number": "G-1",
                        "invoice_total_inc_tax": 42.0,
                        "invoice_date": "2026-01-01",
                    }
                }
            )
        )
        monkeypatch.setattr(agent, "OUTPUT_BASE_DIR", tmp_path)

        candidate = InvoiceKey.from_invoice(
            "case_new",
            {
                "vendor_name": "Acme Pty Ltd",
                "invoice_number": "INV-9001",
                "invoice_total_inc_tax": 1100.0,
                "invoice_date": "2026-02-01",
            },
        )
        entry = to_validation(
            find_duplicates(candidate, agent._load_seen_invoices("case_new"))
        )
        assert entry["passed"] is True
        assert entry["evidence"] == "No prior invoice matched"


def _find_key(node, key):
    """Locate a config key anywhere in the master-data tree."""
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for value in node.values():
            found = _find_key(value, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_key(item, key)
            if found is not None:
                return found
    return None
