"""What a rule is measured against, before somebody lets it change payments.

The blast-radius report is the safety claim this product is sold on, and it
was computed against `data/agent_output/` -- directories on whichever machine
happened to be running. In a deployed worker that folder is empty, so the
report would have said "no collateral" truthfully and meaninglessly, having
looked at nothing at all.

These tests pin the two properties that make the number worth trusting: it
comes from the tenant's own cases, and it says how much of them it read.
"""

from __future__ import annotations

import pytest

from invoice_processing.core.impact_assessor import (
    DEFAULT_SAMPLE_SIZE,
    ImpactAssessorAgent,
)
from invoice_processing.ports import RunContext
from invoice_processing.tenant_cases import TenantCaseLoader

CONDITIONS = [
    {"field": "decision_phase4", "operator": "equals", "value": "REJECT"}
]


def case(verdict: str = "REJECT") -> dict:
    return {
        "phase4": {"decision": verdict},
        "decision": {"final_decision": verdict},
        "extraction": {"invoice": {"vendor_name": "QuickFix"}},
    }


class CountingCases:
    """A case repository that remembers how it was asked."""

    def __init__(self, cases: dict[str, dict]):
        self.cases = cases
        self.batch_calls = 0
        self.single_calls = 0

    def iter_case_ids(self, tenant_id):
        return list(self.cases)

    def get_many_artifacts(self, tenant_id, case_ids):
        self.batch_calls += 1
        return {cid: self.cases[cid] for cid in case_ids if cid in self.cases}

    def get_artifacts(self, ctx: RunContext):
        self.single_calls += 1
        return self.cases.get(ctx.case_id, {})


@pytest.fixture
def corpus():
    cases = {"case_target": case("REJECT")}
    for n in range(4):
        cases[f"case_{n}"] = case("ACCEPT")
    return CountingCases(cases)


class TestTheCorpusIsTheTenants:
    def test_a_scanned_po_or_receipt_is_not_a_case(self, corpus):
        """Its case holds no invoice; a rule measured over it would count
        documents the engine never evaluates."""

        class Documents:
            def non_invoice_ids(self, tenant_id):
                assert tenant_id == "tenant-acme"
                return {"case_1", "case_3"}

        loader = TenantCaseLoader(corpus, "tenant-acme", Documents())
        assert loader.list_cases() == ["case_0", "case_2", "case_target"]

    def test_cases_come_from_the_case_store(self, corpus):
        """Not from a folder on somebody's laptop, which in a deployed worker
        is empty -- and an impact report over no cases finds no collateral."""
        loader = TenantCaseLoader(corpus, "tenant-acme")

        report = ImpactAssessorAgent(loader=loader).run(
            CONDITIONS, "case_target"
        )

        assert report.total_cases == 5
        assert report.target_matched is True
        assert report.collateral_matches == []

    def test_collateral_in_the_tenants_own_ledger_is_found(self, corpus):
        corpus.cases["case_2"] = case("REJECT")
        loader = TenantCaseLoader(corpus, "tenant-acme")

        report = ImpactAssessorAgent(loader=loader).run(
            CONDITIONS, "case_target"
        )

        assert [m.case_id for m in report.collateral_matches] == ["case_2"]

    def test_the_whole_corpus_is_read_in_one_query(self, corpus):
        """One round trip, not one per case. Against a database three hundred
        milliseconds away, a query per case is the difference between a report
        somebody waits for and one they abandon -- and a safety number nobody
        waits for is a safety number nobody reads."""
        loader = TenantCaseLoader(corpus, "tenant-acme")

        ImpactAssessorAgent(loader=loader).run(CONDITIONS, "case_target")

        assert corpus.batch_calls == 1
        assert corpus.single_calls == 0


class TestTheReportStatesItsOwnDenominator:
    def test_a_full_scan_says_so(self, corpus):
        loader = TenantCaseLoader(corpus, "tenant-acme")

        report = ImpactAssessorAgent(loader=loader).run(
            CONDITIONS, "case_target"
        )

        assert report.sampled is False
        assert "Measured against all 5 cases" in report.summary

    def test_a_sample_names_what_it_missed(self, corpus):
        loader = TenantCaseLoader(corpus, "tenant-acme")

        report = ImpactAssessorAgent(loader=loader).run(
            CONDITIONS, "case_target", sample_size=3
        )

        assert report.sampled is True
        assert "Measured against 3 of 5 cases" in report.summary
        assert "2 cases were not checked" in report.summary

    def test_a_sampled_count_of_collateral_is_a_lower_bound(self, corpus):
        """Found live: 9 matching invoices in a corpus of 260, of which a
        200-case sample found 8. Printed flat, that reads as all of them --
        and the number an approver is trusting is the one that says how many
        other invoices a rule will re-decide."""
        for n in range(4):
            corpus.cases[f"case_{n}"] = case("REJECT")
        loader = TenantCaseLoader(corpus, "tenant-acme")

        report = ImpactAssessorAgent(loader=loader).run(
            CONDITIONS, "case_target", sample_size=3
        )

        assert "at least" in report.summary

    def test_a_full_scan_states_the_count_plainly(self, corpus):
        corpus.cases["case_2"] = case("REJECT")
        loader = TenantCaseLoader(corpus, "tenant-acme")

        report = ImpactAssessorAgent(loader=loader).run(
            CONDITIONS, "case_target"
        )

        assert "Collateral matches (1)" in report.summary
        assert "at least" not in report.summary

    def test_two_hundred_cases_are_read_before_sampling_starts(self):
        """Ten was the chat's sample. Ten of forty thousand is not a safety
        guarantee; it is theatre with a progress bar."""
        assert DEFAULT_SAMPLE_SIZE == 200


class TestOneVocabulary:
    def test_a_collateral_case_reports_the_decision_the_rest_of_the_app_shows(
        self, corpus
    ):
        """It used to read a key the context builder never produces, and
        default to ACCEPT -- so every collateral case displayed as accepted
        whatever it actually was."""
        corpus.cases["case_2"] = {
            "decision": {"final_decision": "REJECT"},
            "extraction": {},
        }
        loader = TenantCaseLoader(corpus, "tenant-acme")

        report = ImpactAssessorAgent(loader=loader).run(
            [
                {
                    "field": "decision.final_decision",
                    "operator": "equals",
                    "value": "REJECT",
                }
            ],
            "case_target",
        )

        collateral = {m.case_id: m.decision for m in report.collateral_matches}
        assert collateral["case_2"] == "REJECT"
