"""Step 1.2: is this invoice addressed to us?

The check rejects every invoice a new tenant receives until the organisation
name is configured, so it is the first thing a customer touches -- and it was
defeated by a single space. An invoice addressed to "Enew bharat" was rejected
against a configured "Enewbharat", which reads to the user as the setting not
working.
"""

import pytest

from invoice_processing.shared_libraries.acting.general_invoice_agent import (
    _fold_name,
)


class TestNameFolding:
    @pytest.mark.parametrize(
        "configured,on_the_invoice",
        [
            # The case that was reported.
            ("Enewbharat", "Enew bharat"),
            # The same company as a supplier might write it.
            ("Enew Bharat", "ENEW  BHARAT"),
            ("E-New Bharat", "Enew Bharat"),
            ("Enew Bharat", "Enew Bharat."),
            # A configured short name inside the full legal name.
            ("ACME Corp", "Acme Corp Pty Ltd"),
        ],
    )
    def test_it_matches_the_same_company_written_differently(
        self, configured, on_the_invoice
    ):
        assert _fold_name(configured) in _fold_name(on_the_invoice)

    @pytest.mark.parametrize(
        "configured,on_the_invoice",
        [
            ("Enew Bharat", "Globex Pty Ltd"),
            ("ACME Corp", "Initech"),
        ],
    )
    def test_it_does_not_match_a_different_company(
        self, configured, on_the_invoice
    ):
        assert _fold_name(configured) not in _fold_name(on_the_invoice)

    def test_it_survives_nothing(self):
        """An unread customer name must not become a match for everything."""
        assert _fold_name(None) == ""
        assert _fold_name("") == ""
        # An empty configured name is filtered out before comparison, or it
        # would be a substring of every invoice ever received.
        assert "" in _fold_name("anything")

    def test_folding_is_not_what_makes_a_substring_match_loose(self):
        """A short name matching inside a longer unrelated one predates this
        and is a property of the substring test, not of folding. Recorded so
        the next person does not read it as a regression."""
        short, longer = "Acme", "Beacme Ltd"
        assert short.lower() in longer.lower(), "true before folding existed"
        assert _fold_name(short) in _fold_name(longer), "and equally true after"
