"""The half of the loop that was missing: a taught rule reaching a decision.

Rules were validated, impact-assessed, backed up and written to a file that no
deployed process read. The worker ran the pipeline and stored what it produced;
`ALFEngine` was reachable only from the ADK chat agent, which reads the
packaged sample folder and is not what the Docker image runs. An expert could
approve a rule and change nothing at all.

These tests pin the connection, in both modes: shadow records what a rule
would do, binding lets it do it, and neither may cost an invoice.
"""

from __future__ import annotations

import pytest

from invoice_processing import alf_stage, case_projection, root_agent
from invoice_processing.api import service
from invoice_processing.core.rule_writer import RuleWriterAgent
from invoice_processing.ports import RunContext
from invoice_processing.worker import runner

# A postprocessing document shaped the way the pipeline writes one.
REJECTED = {
    "Invoice Processing": {
        "Invoice Status": "Rejected",
        "Invoice Number": "INV-1",
    },
    "Outcome Message": {"Outcome Message": "Rejected: no work authorization"},
}

ARTIFACTS = {
    "extraction": {
        "invoice": {"vendor_name": "QuickFix HVAC", "waf_count": 0},
        "waf_count": 0,
    },
    "phase4": {
        "decision": "REJECT",
        "rejection_template": "Invoice does not match work authorization",
    },
    # The pipeline's own verdict, which is what everything asking "what was
    # decided" reads first -- so a correction that does not reach it is a
    # correction nothing downstream can see.
    "decision": {
        "final_decision": "REJECT",
        "decision_class": "REJECT",
        "rejection_template": "Invoice does not match work authorization",
    },
    "postprocessing": REJECTED,
}


def exemption_rule() -> dict:
    """The worked example, in the shape a rule base stores."""
    return {
        "schema_version": "2.0.0",
        "metadata": {},
        "rules": [
            {
                "id": "ALF-001",
                "name": "WAF exemption for low-value repairs",
                "description": "Policy exempts repairs under $2,000.",
                "scope": "WAF_EXEMPTION_REPAIR_LOW_VALUE",
                "priority": 50,
                "enabled": True,
                "conditions": [
                    {
                        "field": "decision_phase4",
                        "operator": "equals",
                        "value": "REJECT",
                    },
                    {
                        "field": "has_waf",
                        "operator": "is_false",
                        "value": True,
                    },
                ],
                "actions": [
                    {
                        "type": "set_field",
                        "target": "Invoice Processing.Invoice Status",
                        "value": "Pending Payment",
                    }
                ],
                "metadata": {},
            }
        ],
    }


class TestShadowMode:
    def test_it_records_what_the_rule_would_have_done(self):
        written = alf_stage.run(ARTIFACTS, exemption_rule(), binding=False)

        audit = written["alf"]
        assert audit["mode"] == "shadow"
        assert audit["would_change"] is True
        assert audit["changed"] is False
        assert audit["status_before"] == "Rejected"
        assert audit["status_after"] == "Pending Payment"
        assert [r["rule_id"] for r in audit["rules_applied"]] == ["ALF-001"]

    def test_the_decision_is_left_alone(self):
        """The whole point: evidence from real invoices, costing nothing."""
        written = alf_stage.run(ARTIFACTS, exemption_rule(), binding=False)

        assert "postprocessing" not in written
        assert ARTIFACTS["postprocessing"] == REJECTED


class TestBindingMode:
    def test_the_rule_changes_the_decision(self):
        written = alf_stage.run(ARTIFACTS, exemption_rule(), binding=True)

        assert alf_stage.status_of(written["postprocessing"]) == (
            "Pending Payment"
        )
        assert written["alf"]["changed"] is True

    def test_what_it_was_before_is_kept(self):
        """ "The rule changed it" is half a sentence without "from what"."""
        written = alf_stage.run(ARTIFACTS, exemption_rule(), binding=True)

        assert alf_stage.status_of(written["postprocessing_pre_alf"]) == (
            "Rejected"
        )
        # And the caller's own dict was not mutated underneath it.
        assert ARTIFACTS["postprocessing"] == REJECTED

    def test_the_decision_artifact_is_corrected_too(self):
        """Otherwise the correction lands where nobody reads it.

        A rule sets a field in the postprocessing document; `final_decision`
        keeps saying what the pipeline concluded before the rule ran. That key
        is what the projected column comes from -- the one the sheet and the
        review queue filter on -- and what `machine_verdict` reads to decide
        whether a reviewer later agreed with the machine. Found by running the
        stage end to end: the artifact said Pending Payment and the column
        still said REJECT.
        """
        written = alf_stage.run(ARTIFACTS, exemption_rule(), binding=True)

        assert written["decision"]["final_decision"] == "ACCEPT"
        assert written["decision"]["invoice_status"] == "Pending Payment"
        assert written["decision"]["corrected_by"] == ["ALF-001"]
        assert written["decision_pre_alf"]["final_decision"] == "REJECT"

        # And the column derived from it follows.
        assert (
            case_projection.invoice_columns({**ARTIFACTS, **written})[
                "decision"
            ]
            == "ACCEPT"
        )

    def test_a_rule_that_matches_nothing_changes_nothing(self):
        doc = exemption_rule()
        doc["rules"][0]["conditions"][0]["value"] = "ACCEPT"

        written = alf_stage.run(ARTIFACTS, doc, binding=True)

        assert "postprocessing" not in written
        assert written["alf"]["would_change"] is False


class TestCostsNothingWhenThereIsNothingToDo:
    def test_a_tenant_with_no_rules_writes_no_artifact(self):
        """Every tenant starts here, and none of them should pay for it."""
        assert alf_stage.run(ARTIFACTS, {"rules": []}) == {}
        assert alf_stage.run(ARTIFACTS, None) == {}

    def test_a_case_with_no_output_is_left_alone(self):
        assert alf_stage.run({"extraction": {}}, exemption_rule()) == {}


class TestTheWorkerPath:
    """`process_document` is where the loop is closed."""

    @pytest.fixture
    def worker_bits(self, monkeypatch, tmp_path):
        stored: dict[str, dict] = {}

        class Cases:
            def put_artifact(self, ctx, key, payload):
                stored[key] = payload

        class Rules:
            doc = exemption_rule()

            def load(self, tenant_id):
                return self.doc, 7

        monkeypatch.setattr(service, "get_rules", Rules)
        return Cases(), stored

    def test_rules_are_loaded_for_this_tenant_and_stored(self, worker_bits):
        cases, stored = worker_bits
        ctx = RunContext(tenant_id="t", case_id="case_005")

        written = runner._apply_rules(ctx, cases, ARTIFACTS, binding=True)

        assert stored["alf"]["rule_base_version"] == 7
        assert alf_stage.status_of(stored["postprocessing"]) == (
            "Pending Payment"
        )
        assert written["alf"]["changed"] is True

    def test_a_broken_rule_base_does_not_cost_the_invoice(
        self, monkeypatch, worker_bits
    ):
        """A malformed rule is an operator's problem. An un-corrected invoice
        is still a processed invoice; refusing to file it would turn a bad
        rule into an accounts-payable outage."""
        cases, _ = worker_bits

        class Exploding:
            def load(self, tenant_id):
                raise RuntimeError("rule base is not JSON")

        monkeypatch.setattr(service, "get_rules", Exploding)

        written = runner._apply_rules(
            RunContext(tenant_id="t", case_id="case_005"),
            cases,
            ARTIFACTS,
            binding=True,
        )

        assert "rule base is not JSON" in written["alf"]["error"]
        assert "postprocessing" not in written


class TestTheCorrectionExplainsItself:
    """A decision changed by a rule that cannot say which rule changed it is
    the opaque automation this product exists not to be."""

    def test_a_fired_rule_reads_as_a_finding(self):
        written = alf_stage.run(ARTIFACTS, exemption_rule(), binding=True)
        finding = case_projection.alf_findings({**ARTIFACTS, **written})[0]

        assert finding["step"] == "ALF-001"
        assert "Corrected Rejected -> Pending Payment" in finding["evidence"]
        assert "decision_phase4 equals" in finding["evidence"]
        assert finding["passed"] is True

    def test_a_shadow_rule_says_it_only_would_have(self):
        written = alf_stage.run(ARTIFACTS, exemption_rule(), binding=False)
        finding = case_projection.alf_findings({**ARTIFACTS, **written})[0]

        assert "Would correct" in finding["evidence"]
        assert finding["informational"] is True


class TestTheChatIsRetired:
    """Teaching happens in the product now.

    The chat agent could do it for one person with a terminal, wrote to a file
    nothing read, and had no idea which tenant it was talking about. Leaving
    the tools bound would leave a second way to write rules that skips the
    approval, the blast radius and the tenant boundary.
    """

    def test_the_agent_keeps_only_its_inference_tools(self):
        names = {getattr(t, "__name__", str(t)) for t in root_agent.tools}
        assert names == {"list_inference_cases", "run_inference"}

    def test_nothing_writes_a_rule_base_to_disk_any_more(self):
        """The unlocked read-modify-write is gone: two people teaching a rule
        at once each allocated the same id and the second write dropped the
        first rule."""
        for gone in ("run", "delete_rule", "next_rule_id", "load_rule_base"):
            assert not hasattr(RuleWriterAgent, gone), (
                f"RuleWriterAgent.{gone} writes rules outside the repository"
            )
