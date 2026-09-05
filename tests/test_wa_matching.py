"""Work authorization by reference, which is how customers actually work.

Check 1.4 asked whether a Work Authorization form was attached to the
submission. The worker materialises only the invoice, so the count it read was
always zero and the check could never pass -- and most customers do not attach
the form anyway: the invoice quotes a reference and the authorisation lives in
their records. In the benchmark corpus all 48 of the 1.4 failures are
reference-level faults, and none is a missing form.

The form-based path stays. A tenant who attaches them and has loaded no master
sees exactly today's behaviour, which is what makes this safe to ship.
"""

import json

import pytest

from invoice_processing import vendor_master, wa_matching
from invoice_processing.shared_libraries.acting import general_invoice_agent
from invoice_processing.shared_libraries.acting.general_invoice_agent import (
    Phase1ValidatorAgent,
    Phase4ValidatorAgent,
    determine_work_type,
)

VENDOR = "Cloudhaven Infra Services Pvt Ltd"
OTHER_VENDOR = "Nimbus Nine Software LLP"

WA = {
    "reference": "WA-A-2026-002",
    "vendor_name": VENDOR,
    "project": "Customer Portal v3",
    "approved_hours": 120,
    "hourly_rate": 2363.0,
    "period_start": "2026-05-01",
    "period_end": "2026-12-28",
    "status": "ACTIVE",
}


def authorizations(*records):
    return wa_matching.WorkAuthorizations(records=list(records))


def invoice(**overrides) -> dict:
    return {
        "vendor_name": VENDOR,
        "work_authorization_reference": "WA-A-2026-002",
        "project": "Customer Portal v3",
        "service_period_start": "2026-07-10",
        "service_period_end": "2026-08-09",
        "line_items": [
            {
                "description": "Managed cloud operations",
                "quantity": 42,
                "unit": "hour",
                "unit_price": 2363.0,
                "amount_ex_tax": 99246.0,
            }
        ],
        **overrides,
    }


class TestDoesTheAuthorizationExist:
    def test_a_live_authorization_for_this_supplier_matches(self):
        result = wa_matching.match_invoice_to_wa(invoice(), authorizations(WA))
        assert result.outcome == wa_matching.MATCHED

    def test_an_invoice_quoting_nothing_fails(self):
        result = wa_matching.match_invoice_to_wa(
            invoice(work_authorization_reference=None), authorizations(WA)
        )
        assert result.outcome == wa_matching.NO_REFERENCE

    def test_a_reference_nobody_issued_fails(self):
        result = wa_matching.match_invoice_to_wa(
            invoice(work_authorization_reference="WA-A-2026-965"),
            authorizations(WA),
        )
        assert result.outcome == wa_matching.NOT_FOUND

    def test_another_suppliers_authorization_fails(self):
        """The one that matters: a reference quoted by a supplier it was never
        issued to. The amount can look entirely reasonable."""
        result = wa_matching.match_invoice_to_wa(
            invoice(vendor_name=OTHER_VENDOR), authorizations(WA)
        )
        assert result.outcome == wa_matching.OTHER_VENDOR
        assert VENDOR in result.evidence

    def test_an_expired_authorization_fails(self):
        result = wa_matching.match_invoice_to_wa(
            invoice(), authorizations({**WA, "status": "EXPIRED"})
        )
        assert result.outcome == wa_matching.EXPIRED

    def test_punctuation_in_the_reference_is_not_a_mismatch(self):
        result = wa_matching.match_invoice_to_wa(
            invoice(work_authorization_reference="wa a 2026 002"),
            authorizations(WA),
        )
        assert result.outcome == wa_matching.MATCHED

    def test_no_master_is_reported_as_such(self):
        result = wa_matching.match_invoice_to_wa(invoice(), authorizations())
        assert result.outcome == wa_matching.NOT_CONFIGURED
        assert result.ok


class TestIsTheInvoiceInsideIt:
    def _check(self, inv, wa=None, **kwargs):
        return wa_matching.hours_check(
            inv,
            wa_matching.WorkAuthorization.from_dict(wa or WA),
            labour_keywords=("labour", "labor", "technician", "hours"),
            **kwargs,
        )

    def test_an_invoice_within_every_limit_passes(self):
        assert (
            self._check(invoice()).outcome == wa_matching.WITHIN_AUTHORIZATION
        )

    def test_more_hours_than_authorized_fails(self):
        inv = invoice()
        inv["line_items"][0]["quantity"] = 200
        result = self._check(inv)
        assert result.outcome == wa_matching.HOURS_EXCEEDED
        assert result.details["invoiced_hours"] == 200

    def test_a_higher_rate_than_authorized_fails(self):
        """Same money as over-billing hours, different conversation with the
        supplier -- so the evidence says which one it was."""
        inv = invoice()
        inv["line_items"][0]["unit_price"] = 3181.0
        result = self._check(inv)
        assert result.outcome == wa_matching.RATE_MISMATCH
        assert "3,181.00" in result.evidence

    def test_work_billed_to_another_project_fails(self):
        result = self._check(invoice(project="Cloud Cost Optimisation"))
        assert result.outcome == wa_matching.PROJECT_MISMATCH

    def test_work_done_outside_the_authorized_period_fails(self):
        result = self._check(
            invoice(
                service_period_start="2026-03-22",
                service_period_end="2026-04-12",
            )
        )
        assert result.outcome == wa_matching.OUTSIDE_PERIOD

    def test_hours_come_from_lines_billed_by_the_hour(self):
        inv = invoice()
        inv["line_items"].append(
            {"description": "Licence", "quantity": 500, "unit": "user"}
        )
        assert wa_matching.invoiced_hours(inv) == 42

    def test_a_lump_sum_line_is_not_hours(self):
        inv = invoice()
        inv["line_items"] = [
            {"description": "Retainer", "quantity": 1, "unit": "lot"}
        ]
        assert wa_matching.invoiced_hours(inv) == 0


class TestInThePipeline:
    @pytest.fixture
    def config(self, monkeypatch):
        def apply(**overrides):
            settings = general_invoice_agent.DEFAULT_CONFIG.copy()
            settings["require_work_authorization"] = True
            settings.update(overrides)
            monkeypatch.setitem(
                general_invoice_agent._config_store, "CONFIG", settings
            )

        return apply

    def _master(self, tmp_path, *records):
        path = tmp_path / "work_authorizations.json"
        path.write_text(
            json.dumps({"work_authorizations": list(records)}), encoding="utf-8"
        )
        return str(path)

    def _step(self, agent_cls, tmp_path, extraction, step_id):
        result = agent_cls().run(tmp_path, extraction)
        return next(
            (v for v in result["validations"] if v["step"] == step_id), None
        )

    def test_step_14_passes_on_a_valid_reference(self, tmp_path, config):
        config(work_authorization_file=self._master(tmp_path, WA))
        step = self._step(
            Phase1ValidatorAgent, tmp_path, {"invoice": invoice()}, "1.4"
        )
        assert step["passed"] is True
        assert step["outcome"] == wa_matching.MATCHED

    def test_step_14_fails_on_another_suppliers_authorization(
        self, tmp_path, config
    ):
        config(work_authorization_file=self._master(tmp_path, WA))
        step = self._step(
            Phase1ValidatorAgent,
            tmp_path,
            {"invoice": invoice(vendor_name=OTHER_VENDOR)},
            "1.4",
        )
        assert step["passed"] is False
        assert step["rejection_template"] == "Missing Work Authorization Form"

    def test_an_attached_form_still_satisfies_step_14(self, tmp_path, config):
        """A tenant who sends the paperwork must not be told it is missing
        because they have also started loading a master."""
        config(work_authorization_file=self._master(tmp_path, WA))
        step = self._step(
            Phase1ValidatorAgent,
            tmp_path,
            {
                "invoice": invoice(work_authorization_reference=None),
                "waf_count": 1,
            },
            "1.4",
        )
        assert step["passed"] is True

    def test_without_a_master_the_form_path_is_unchanged(
        self, tmp_path, config
    ):
        config(work_authorization_file=str(tmp_path / "absent.json"))
        step = self._step(
            Phase1ValidatorAgent,
            tmp_path,
            {"invoice": invoice(), "waf_count": 0},
            "1.4",
        )
        assert step["passed"] is False
        assert "WAF present: False" in step["evidence"]

    def test_step_43_checks_the_authorization_it_matched(
        self, tmp_path, config
    ):
        config(work_authorization_file=self._master(tmp_path, WA))
        inv = invoice()
        inv["line_items"][0]["quantity"] = 200
        result = Phase4ValidatorAgent().run(tmp_path, {"invoice": inv}, {})
        step = next(v for v in result["validations"] if v["step"] == "4.3")
        assert step["passed"] is False
        assert step["outcome"] == wa_matching.HOURS_EXCEEDED

    def test_step_44_still_runs_when_43_rejects(self, tmp_path, config):
        """4.3 used to be the last thing phase 4 did. It is not."""
        config(work_authorization_file=self._master(tmp_path, WA))
        inv = invoice()
        inv["line_items"][0]["quantity"] = 200
        result = Phase4ValidatorAgent().run(tmp_path, {"invoice": inv}, {})
        assert {v["step"] for v in result["validations"]} >= {
            "4.1",
            "4.2",
            "4.3",
            "4.4",
        }


class TestWorkTypeKeywords:
    def test_development_is_not_preventative_maintenance(self):
        """ "pm" was matched as a bare substring, so "Development" classified as
        preventative maintenance -- and preventative work is on the exempt
        list, so a software invoice exempted itself from the check by
        spelling."""
        assert (
            determine_work_type(
                {
                    "line_items": [
                        {"description": "Software development services"}
                    ]
                }
            )
            == "REPAIRS"
        )

    def test_real_preventative_maintenance_still_matches(self):
        for description in ("PM inspection", "Preventative maintenance visit"):
            assert (
                determine_work_type(
                    {"line_items": [{"description": description}]}
                )
                == "PREVENTATIVE"
            )


class TestSuppliersOfGoods:
    """A supplier of goods has no work to authorise. Demanding an
    authorization from them rejects every delivery they ever invoice -- 153 of
    the benchmark's 300 submissions are goods invoices, and the dataset marks
    check 1.4 as not applicable to every one of them."""

    def _vendors(self, kind):
        return vendor_master.VendorMaster(
            records=[
                {"name": VENDOR, "tax_id": "29ZOICE4828P1ZD", "kind": kind}
            ]
        )

    def test_a_goods_supplier_is_not_asked_for_one(self):
        result = wa_matching.match_invoice_to_wa(
            invoice(work_authorization_reference=None),
            authorizations(WA),
            vendors=self._vendors("goods"),
        )
        assert result.outcome == wa_matching.NOT_APPLICABLE
        assert result.ok

    def test_a_service_supplier_still_is(self):
        result = wa_matching.match_invoice_to_wa(
            invoice(work_authorization_reference=None),
            authorizations(WA),
            vendors=self._vendors("service"),
        )
        assert result.outcome == wa_matching.NO_REFERENCE

    def test_an_unknown_supplier_is_still_asked(self):
        """Silence is not permission: a supplier nobody has classified is
        treated as one whose work needs authorising."""
        result = wa_matching.match_invoice_to_wa(
            invoice(work_authorization_reference=None),
            authorizations(WA),
            vendors=vendor_master.VendorMaster(
                records=[{"name": OTHER_VENDOR, "tax_id": "27UHVFN1303Z1Z9"}]
            ),
        )
        assert result.outcome == wa_matching.NO_REFERENCE

    def test_the_pipeline_records_no_verdict_for_a_goods_supplier(
        self, tmp_path, monkeypatch
    ):
        """Not a pass -- no entry at all. A verdict on a question that does not
        arise is noise in the audit trail."""
        settings = general_invoice_agent.DEFAULT_CONFIG.copy()
        settings["require_work_authorization"] = True
        path = tmp_path / "wa.json"
        path.write_text(
            json.dumps({"work_authorizations": [WA]}), encoding="utf-8"
        )
        vendors = tmp_path / "vendors.json"
        vendors.write_text(
            json.dumps(
                {
                    "vendors": [
                        {
                            "name": VENDOR,
                            "tax_id": "29ZOICE4828P1ZD",
                            "kind": "goods",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        settings["work_authorization_file"] = str(path)
        settings["vendor_master_file"] = str(vendors)
        monkeypatch.setitem(
            general_invoice_agent._config_store, "CONFIG", settings
        )

        result = Phase1ValidatorAgent().run(
            tmp_path, {"invoice": invoice(work_authorization_reference=None)}
        )
        assert not [v for v in result["validations"] if v["step"] == "1.4"]
