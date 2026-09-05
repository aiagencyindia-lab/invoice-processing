"""Tax ID validation as a registry, so a country is a plugin not a fork.

`validate_tax_id` was `if format_type == "ABN"` with a comment promising other
formats, and the ABN checksum had drifted into three separate implementations
(acting pipeline, critic checks, and this registry). Adding a country meant
editing the acting pipeline.

Supporting a country is now a registration. These tests cover the registry
mechanics, both shipped validators, and the single most important safety
property: an unrecognised format must report that nothing was checked, not
"valid".
"""

import json
from pathlib import Path

import pytest

from invoice_processing import tax_ids
from invoice_processing.shared_libraries.acting.general_invoice_agent import (
    DEFAULT_CONFIG,
    validate_abn_checksum,
    validate_tax_id,
)
from invoice_processing.shared_libraries.investigation.checks import (
    abn_checksum_valid,
)

# Real ABNs from the sample invoices.
VALID_ABN = "51 824 753 556"
INVALID_ABN = "64 560 747 633"


class TestRegistry:
    def test_ships_more_than_one_validator(self):
        """A registry with a single implementation would be pointless."""
        assert len(tax_ids.supported_formats()) >= 3
        assert {"ABN", "GSTIN", "NONE"} <= set(tax_ids.supported_formats())

    def test_lookup_is_case_insensitive(self):
        assert tax_ids.validate(VALID_ABN, "abn") == tax_ids.validate(
            VALID_ABN, "ABN"
        )

    def test_unknown_format_reports_that_nothing_was_checked(self):
        """The safety property: a config typo must not silently disable a control."""
        valid, explanation = tax_ids.validate("whatever", "VAT")
        assert valid is True, "must not reject ids it cannot check"
        assert "NOT checked" in explanation
        assert "ABN" in explanation, "should list what is supported"

    def test_disabled_is_distinct_from_unknown(self):
        _, disabled = tax_ids.validate("x", "NONE")
        _, unknown = tax_ids.validate("x", "VAT")
        assert disabled != unknown

    def test_registering_a_new_format_needs_no_pipeline_change(self):
        """The point of the registry."""

        @tax_ids.register("TESTFMT")
        def _validate(value):
            return value == "GOOD", "checked"

        try:
            assert tax_ids.validate("GOOD", "TESTFMT") == (True, "checked")
            assert tax_ids.validate("BAD", "TESTFMT") == (False, "checked")
            # The acting pipeline picks it up without modification.
            assert validate_tax_id("GOOD", "TESTFMT")[0] is True
        finally:
            tax_ids._REGISTRY.pop("TESTFMT", None)


class TestAbn:
    def test_valid(self):
        assert tax_ids.validate_abn(VALID_ABN) == (True, "Valid checksum")

    def test_invalid_reports_the_residue(self):
        """Evidence an AP reviewer can act on, not just 'invalid'."""
        valid, explanation = tax_ids.validate_abn(INVALID_ABN)
        assert valid is False
        assert "mod 89 = 50" in explanation

    def test_formatting_is_ignored(self):
        assert tax_ids.validate_abn("51824753556")[0] is True
        assert tax_ids.validate_abn("51-824-753-556")[0] is True

    def test_wrong_length(self):
        assert tax_ids.validate_abn("5182475355")[0] is False

    def test_non_numeric(self):
        assert tax_ids.validate_abn("5182475355X")[0] is False


class TestGstin:
    """India's GSTIN: 15 chars, state code, PAN, entity, 'Z', check character."""

    def _with_valid_checksum(self, first14: str) -> str:
        alphabet = tax_ids._GSTIN_ALPHABET
        total = 0
        for index, char in enumerate(first14):
            product = alphabet.index(char) * (1 if index % 2 == 0 else 2)
            total += product // 36 + product % 36
        return first14 + alphabet[(36 - (total % 36)) % 36]

    def test_matches_the_canonical_published_example(self):
        """External validation, not self-consistency.

        27AAPFU0939F1ZV is the example that appears throughout GST
        documentation. This is the ONLY independent confirmation the checksum
        has; see the UNVERIFIED warning on validate_gstin. Everything else in
        this class tests self-consistency or structure, which cannot detect a
        systematically wrong algorithm.
        """
        assert tax_ids.validate_gstin("27AAPFU0939F1ZV") == (
            True,
            "Valid checksum",
        )

    def test_accepts_a_correctly_checksummed_gstin(self):
        gstin = self._with_valid_checksum("27AAPFU0939F1Z")
        assert tax_ids.validate_gstin(gstin) == (True, "Valid checksum")

    def test_rejects_a_wrong_check_character(self):
        gstin = self._with_valid_checksum("27AAPFU0939F1Z")
        alphabet = tax_ids._GSTIN_ALPHABET
        wrong = gstin[:14] + alphabet[(alphabet.index(gstin[14]) + 1) % 36]
        valid, explanation = tax_ids.validate_gstin(wrong)
        assert valid is False
        assert "check character" in explanation

    def test_rejects_wrong_length(self):
        assert tax_ids.validate_gstin("27AAPFU0939F1Z")[0] is False

    @pytest.mark.parametrize("state", ["00", "39", "99"])
    def test_rejects_invalid_state_code(self, state):
        gstin = self._with_valid_checksum(state + "AAPFU0939F1Z")
        valid, explanation = tax_ids.validate_gstin(gstin)
        assert valid is False
        assert "state code" in explanation

    def test_requires_z_at_position_14(self):
        gstin = self._with_valid_checksum("27AAPFU0939F1X")
        valid, explanation = tax_ids.validate_gstin(gstin)
        assert valid is False
        assert "'Z'" in explanation

    def test_rejects_characters_outside_the_alphabet(self):
        assert tax_ids.validate_gstin("27AAPFU0939F1Z@")[0] is False


class TestSingleImplementation:
    """Three copies of the ABN checksum existed. There must now be one."""

    @pytest.mark.parametrize("value", [VALID_ABN, INVALID_ABN, "51824753556"])
    def test_acting_pipeline_agrees_with_the_registry(self, value):
        assert validate_abn_checksum(value) == tax_ids.validate_abn(value)

    @pytest.mark.parametrize("value", [VALID_ABN, INVALID_ABN])
    def test_critic_agrees_with_the_registry(self, value):
        assert abn_checksum_valid(value) == tax_ids.validate_abn(value)[0]

    def test_critic_still_distinguishes_uncheckable_from_invalid(self):
        """None means 'no opinion'; the registry's False means 'wrong'."""
        assert abn_checksum_valid("") is None
        assert tax_ids.validate_abn("")[0] is False


class TestConfigDriven:
    def test_master_data_selects_the_format(self):
        """`tax_id_format` in the YAML picks the validator."""
        configured = DEFAULT_CONFIG["tax_id_format"]
        assert configured in tax_ids.supported_formats()


class TestGstinAgainstACorpus:
    """The GSTIN checksum carried an UNVERIFIED warning: it had been confirmed
    against one published example, which is not enough to reject a supplier's
    invoice on. This is the corpus that removed the warning."""

    @staticmethod
    def _corpus():
        path = Path(__file__).parent / "fixtures" / "gstin_corpus.json"
        return json.loads(path.read_text(encoding="utf-8"))["identifiers"]

    def test_it_accepts_every_well_formed_identifier(self):
        wrong = [
            entry["gstin"]
            for entry in self._corpus()
            if entry["valid"]
            and not tax_ids.validate(entry["gstin"], "GSTIN")[0]
        ]
        assert not wrong, f"valid GSTINs rejected: {wrong}"

    def test_it_rejects_every_corrupted_one(self):
        """A checksum that accepts a flipped check character is decoration."""
        missed = [
            entry["gstin"]
            for entry in self._corpus()
            if not entry["valid"]
            and tax_ids.validate(entry["gstin"], "GSTIN")[0]
        ]
        assert not missed, f"corrupted GSTINs accepted: {missed}"

    def test_the_corpus_is_big_enough_to_mean_something(self):
        corpus = self._corpus()
        assert sum(1 for e in corpus if e["valid"]) >= 30
        assert sum(1 for e in corpus if not e["valid"]) >= 15
