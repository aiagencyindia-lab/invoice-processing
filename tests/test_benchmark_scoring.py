"""The benchmark's scoring rules, pinned.

A benchmark that scores itself generously is worse than no benchmark, and the
generous mistakes are all in one place: what to do with a check the pipeline
never reached. These tests fix that answer.

They use the real dataset expectations, so they also fail if the vendored
dataset is edited -- which it must not be.
"""

from __future__ import annotations

import json
import os

import pytest
from dotenv import load_dotenv

from benchmark.harness import dataset, run, score, stack_main
from invoice_processing import po_matching


def record(invoice_id: str, **overrides) -> dict:
    """A collected record with no findings, ready to be pointed at a check."""
    base = {
        "invoice_id": invoice_id,
        "company_id": dataset.master()[invoice_id]["company_id"],
        "submission_seq": int(dataset.master()[invoice_id]["submission_seq"]),
        "ingest_duplicate": False,
        "final_decision": "ACCEPT",
        "rejection_template": None,
        "first_failing_step": None,
        "findings": [],
        "classification": {},
        "extracted": {},
        "processing_time": 1.0,
        "llm_calls": 3,
        "cost_usd": 0.01,
        "wall_seconds": 1.0,
    }
    return {**base, **overrides}


def finding(step: str, passed: bool | None, **extra) -> dict:
    return {
        "step": step,
        "passed": passed,
        "informational": extra.pop("informational", False),
        "skipped": extra.pop("skipped", False),
        "evidence": extra.pop("evidence", ""),
        "rejection_template": extra.pop("rejection_template", None),
        "outcome": extra.pop("outcome", None),
    }


def first_failing(check_id: str) -> str:
    """An invoice the dataset expects to fail first at this check."""
    for invoice_id, row in dataset.master().items():
        if row["first_failing_check"] == check_id:
            return invoice_id
    raise AssertionError(f"no invoice fails first at {check_id}")


def row_for(rows: list[dict], invoice_id: str, check_id: str) -> dict:
    return next(
        r
        for r in rows
        if r["invoice_id"] == invoice_id and r["check_id"] == check_id
    )


class TestAbsentIsNotAPass:
    def test_a_check_the_pipeline_never_reached_is_not_run(self):
        invoice_id = first_failing("3.3")
        rows = score.build_rows({invoice_id: record(invoice_id)})
        assert row_for(rows, invoice_id, "3.3")["actual_result"] == "NOT_RUN"

    def test_an_expected_failure_never_reached_is_a_false_negative(self):
        invoice_id = first_failing("3.3")
        rows = score.build_rows({invoice_id: record(invoice_id)})
        metrics = score.check_metrics(rows, "cascade", balanced=False)["3.3"]
        assert (metrics["fn"], metrics["tp"]) == (1, 0)
        assert row_for(rows, invoice_id, "3.3")["passed"] is False

    def test_a_hidden_later_failure_is_excluded_rather_than_scored(self):
        """The dataset stops at the first failure and so does the pipeline.

        Scoring a check the document never legitimately reached would punish
        the application for a rejection it got right.
        """
        invoice_id = next(
            i
            for i, row in dataset.master().items()
            if row["first_failing_check"] == "1.2"
            and dataset.matrix()[(i, "4.4")]["expected_result"] == "FAIL"
        )
        rows = score.build_rows(
            {
                invoice_id: record(
                    invoice_id,
                    final_decision="REJECT",
                    first_failing_step="1.2",
                    findings=[finding("1.2", False, rejection_template="x")],
                )
            }
        )
        assert row_for(rows, invoice_id, "4.4")["passed"] is None
        assert (
            score.check_metrics(rows, "cascade", balanced=False)["4.4"]["n"]
            == 0
        )


class TestSkippedAndInformational:
    def test_a_disabled_check_is_skipped_not_passed(self):
        invoice_id = first_failing("3.3")
        rows = score.build_rows(
            {
                invoice_id: record(
                    invoice_id, findings=[finding("3.3", None, skipped=True)]
                )
            }
        )
        assert row_for(rows, invoice_id, "3.3")["actual_result"] == "SKIPPED"

    @pytest.mark.parametrize(
        ("passed", "expected"),
        [(True, "INFORMATIONAL_MATCH"), (False, "INFORMATIONAL_DISCREPANCY")],
    )
    def test_check_41_never_reads_as_pass_or_fail(self, passed, expected):
        invoice_id = first_failing("3.3")
        rows = score.build_rows(
            {
                invoice_id: record(
                    invoice_id,
                    findings=[finding("4.1", passed, informational=True)],
                )
            }
        )
        row = row_for(rows, invoice_id, "4.1")
        assert row["actual_result"] == expected
        assert row["actual_action"] in ("PASS", "FLAG")


class TestNotApplicable:
    def test_failing_a_check_that_does_not_apply_is_counted_separately(self):
        """1.4 on a goods vendor: the dataset says N/A, so a rejection there is
        neither a true nor a false positive -- it is a spurious failure, and
        hiding it inside accuracy would flatter the application."""
        invoice_id = next(
            i
            for i, _ in dataset.matrix()
            if dataset.matrix()[(i, "1.4")]["expected_result"] == "N_A"
        )
        rows = score.build_rows(
            {
                invoice_id: record(
                    invoice_id,
                    findings=[
                        finding("1.4", False, rejection_template="Missing")
                    ],
                )
            }
        )
        assert row_for(rows, invoice_id, "1.4")["passed"] is False
        metrics = score.check_metrics(rows, "cascade", balanced=False)["1.4"]
        assert metrics["spurious_failures_on_na"] == 1
        assert metrics["n"] == 0


class TestIngestLevelDuplicates:
    def test_a_file_refused_at_ingest_counts_as_duplicate_detection(self):
        """27 of the 51 duplicates never reach check 3.1: the SHA-256 check in
        ingestion refuses them first. That is real detection at a different
        seam, and it is recorded as such rather than as a missed duplicate."""
        invoice_id = next(
            i
            for i, row in dataset.master().items()
            if row["is_duplicate"] == "True"
        )
        rows = score.build_rows(
            {
                invoice_id: record(
                    invoice_id,
                    ingest_duplicate=True,
                    final_decision="REJECT",
                    rejection_template="Invoice already processed",
                    first_failing_step="3.1",
                )
            }
        )
        row = row_for(rows, invoice_id, "3.1")
        assert row["actual_result"] == "FAIL"
        assert "ingest" in row["validation_details"]


class TestReasonMapping:
    def test_the_purchase_order_reasons_map_to_the_datasets_wording(self):
        """The matcher tells a vendor exactly which part of the PO was wrong.
        The dataset has one string for all of them, so the mapping is read from
        the product rather than retyped here."""
        mapping = score.app_reason_map()
        for template in po_matching._REJECTION_TEMPLATES.values():
            assert mapping[template] == "Invalid purchase order"

    def test_every_dataset_reason_maps_to_itself(self):
        mapping = score.app_reason_map()
        for reason in score.DATASET_REASON.values():
            assert mapping[reason] == reason


class TestPostedUntouched:
    """ACCEPT is not touchless: 42 of 46 accepts in the first after-run were
    routed to a clerk. Posted untouched is ACCEPT *and* routed AUTO, scored
    against the clean invoices, with wrong ones counted on their own."""

    def _metrics(self):
        def rec(decision, routing):
            return {
                "final_decision": decision,
                "extracted": {"_routing": routing},
            }

        return score.decision_metrics(
            {
                "TECH-INV-049": rec("ACCEPT", "AUTO"),  # clean
                "TECH-INV-045": rec("ACCEPT", "REVIEW"),  # clean
                "TECH-INV-061": rec("ACCEPT", "AUTO"),  # faulty
            }
        )

    def test_only_an_auto_routed_clean_accept_counts(self):
        metrics = self._metrics()
        assert metrics["counts"]["posted_untouched"] == 1
        assert metrics["posted_untouched_rate"] == 0.5
        assert metrics["accepted_but_routed_to_review"] == 1

    def test_a_faulty_invoice_posted_untouched_is_counted_apart(self):
        assert self._metrics()["counts"]["posted_untouched_wrongly"] == 1


class TestTheMatrixIsIntact:
    def test_three_hundred_invoices_times_fourteen_checks(self):
        assert len(dataset.matrix()) == 4200
        assert len(dataset.ground_truth()) == 300

    def test_the_expected_vocabulary_is_the_one_the_scorer_knows(self):
        seen = {
            score.norm_expected(row["expected_result"])
            for row in dataset.matrix().values()
        }
        assert seen == {
            "PASS",
            "FAIL",
            "N_A",
            "INFORMATIONAL_MATCH",
            "INFORMATIONAL_DISCREPANCY",
        }


class TestALocalRunStaysLocal:
    """A local benchmark run must not reach production storage.

    It did. `configure_environment` deleted GCS_BUCKET from the environment,
    and `core.config` calls load_dotenv() at import, which fills in any
    variable that is absent -- so the bucket name came straight back from the
    repo's .env and 273 synthetic documents were written to the production
    bucket while the run reported using a local directory. DATABASE_URL was
    unaffected because it is set rather than deleted, and dotenv does not
    override a variable that already has a value.
    """

    @pytest.fixture(autouse=True)
    def _isolated_environment(self):
        """`configure_environment` writes to os.environ by design -- it is
        preparing a process to run in.

        The whole environment is snapshotted and restored, not just the keys
        this test names: monkeypatch only undoes what monkeypatch did, and the
        variable that leaked into the rest of the suite was one the function
        under test set for itself.
        """
        before = dict(os.environ)
        try:
            yield
        finally:
            os.environ.clear()
            os.environ.update(before)

    def test_the_infrastructure_variables_are_emptied_not_deleted(
        self, tmp_path
    ):
        os.environ["GCS_BUCKET"] = "a-real-bucket"
        stack_main.configure_environment(tmp_path)
        for key in (
            "GCS_BUCKET",
            "CLOUD_SQL_INSTANCE",
            "PUBSUB_TOPIC",
            "PUBSUB_SUBSCRIPTION",
        ):
            assert os.environ.get(key) == "", (
                f"{key} is {os.environ.get(key)!r}; an absent variable is "
                "refilled by load_dotenv at import"
            )

    def test_dotenv_cannot_refill_them(self, tmp_path):
        """The property that actually matters, asserted against dotenv itself."""
        stack_main.configure_environment(tmp_path)
        load_dotenv(dataset.REPO / ".env")
        assert not os.getenv("GCS_BUCKET")


class TestACleanRunExitsCleanly:
    """The scorer counts with a Counter, and a Counter writes no key it never
    incremented: a run where every invoice was decided has no
    errors_or_no_decision at all. That is a clean run, not a broken one."""

    def _write(self, tmp_path, counts):
        (tmp_path / "results.json").write_text(
            json.dumps({"decision": {"counts": counts}})
        )
        return tmp_path

    def test_no_undecided_key_means_none_undecided(self, tmp_path):
        out = self._write(tmp_path, {"total": 14, "rejected": 10})
        assert run.undecided(out) == 0

    def test_undecided_invoices_are_still_counted(self, tmp_path):
        out = self._write(tmp_path, {"total": 14, "errors_or_no_decision": 2})
        assert run.undecided(out) == 2
