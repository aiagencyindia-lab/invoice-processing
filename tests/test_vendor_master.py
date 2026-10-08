"""Step 3.2 asks two questions, and a checksum only answers one.

"Is this tax identifier well-formed?" is arithmetic. "Is it *this supplier's*?"
needs a list of suppliers, and it is the question that catches an invoice
carrying somebody else's registration -- structurally perfect, and not theirs.
In the benchmark corpus that is 18 of the 49 planted tax-id faults.

The master is optional on purpose. A tenant that has not loaded one must keep
today's behaviour exactly, or enabling this feature would reject every invoice
from every supplier as unknown.
"""

import json

import pytest

from invoice_processing import vendor_master
from invoice_processing.shared_libraries.acting import general_invoice_agent
from invoice_processing.shared_libraries.acting.general_invoice_agent import (
    Phase3ValidatorAgent,
)

ACME = {
    "name": "Cloudhaven Infra Services Pvt Ltd",
    "tax_id": "29ZOICE4828P1ZD",
}
OTHER = {"name": "Nimbus Nine Software LLP", "tax_id": "27UHVFN1303Z1Z9"}


def master(*records) -> vendor_master.VendorMaster:
    return vendor_master.VendorMaster(records=list(records))


def invoice(**overrides) -> dict:
    return {
        "vendor_name": ACME["name"],
        "vendor_tax_id": ACME["tax_id"],
        **overrides,
    }


class TestMatching:
    def test_the_registered_tax_id_matches(self):
        result = vendor_master.check_vendor_tax_id(invoice(), master(ACME))
        assert result.outcome == vendor_master.MATCHED
        assert result.matched

    def test_another_suppliers_tax_id_is_caught(self):
        """The fraud case: a well-formed identifier that is not theirs. No
        checksum can see this, which is the whole reason the master exists."""
        result = vendor_master.check_vendor_tax_id(
            invoice(vendor_tax_id=OTHER["tax_id"]), master(ACME, OTHER)
        )
        assert result.outcome == vendor_master.TAX_ID_MISMATCH
        assert not result.matched
        assert OTHER["tax_id"] in result.evidence
        assert ACME["tax_id"] in result.evidence

    def test_a_supplier_nobody_registered_is_not_silently_accepted(self):
        result = vendor_master.check_vendor_tax_id(invoice(), master(OTHER))
        assert result.outcome == vendor_master.UNKNOWN_VENDOR
        assert not result.matched

    def test_a_missing_tax_id_is_its_own_outcome(self):
        result = vendor_master.check_vendor_tax_id(
            invoice(vendor_tax_id=None), master(ACME)
        )
        assert result.outcome == vendor_master.NO_TAX_ID

    def test_formatting_differences_are_not_mismatches(self):
        """Suppliers print the same registration with spaces, dashes and in
        lower case. Rejecting an invoice over punctuation is a support ticket,
        not a control."""
        result = vendor_master.check_vendor_tax_id(
            invoice(vendor_tax_id="29 zoice 4828 p1zd"), master(ACME)
        )
        assert result.outcome == vendor_master.MATCHED

    def test_a_legal_suffix_does_not_make_a_supplier_unknown(self):
        result = vendor_master.check_vendor_tax_id(
            invoice(vendor_name="Cloudhaven Infra Services Private Limited"),
            master(ACME),
        )
        assert result.outcome == vendor_master.MATCHED

    def test_no_master_means_the_check_is_unchanged(self):
        result = vendor_master.check_vendor_tax_id(invoice(), master())
        assert result.outcome == vendor_master.NOT_CONFIGURED
        assert result.matched


class TestInThePipeline:
    @pytest.fixture
    def config(self, monkeypatch, tmp_path):
        def apply(**overrides):
            config = general_invoice_agent.DEFAULT_CONFIG.copy()
            config.update(overrides)
            monkeypatch.setitem(
                general_invoice_agent._config_store, "CONFIG", config
            )
            return config

        return apply

    def _step(self, tmp_path, invoice_data, checksum=None):
        extraction = {"invoice": dict(invoice_data)}
        if checksum is not None:
            extraction["invoice"]["_tax_id_validation"] = checksum
        result = Phase3ValidatorAgent(
            seen_invoices=lambda exclude_case_id="": []
        ).run(tmp_path, extraction, {})
        return next(
            (v for v in result["validations"] if v["step"] == "3.2"), None
        )

    def _write_master(self, tmp_path, *records):
        path = tmp_path / "vendors.json"
        path.write_text(
            json.dumps({"vendors": list(records)}), encoding="utf-8"
        )
        return str(path)

    def test_a_valid_checksum_still_fails_when_it_is_not_this_suppliers(
        self, tmp_path, config
    ):
        config(vendor_master_file=self._write_master(tmp_path, ACME, OTHER))
        step = self._step(
            tmp_path,
            invoice(vendor_tax_id=OTHER["tax_id"]),
            checksum={"valid": True, "reason": "Valid checksum"},
        )
        assert step["passed"] is False
        assert step["rejection_template"] == "Vendor tax ID invalid"
        assert step["outcome"] == vendor_master.TAX_ID_MISMATCH

    def test_a_registered_supplier_passes(self, tmp_path, config):
        config(vendor_master_file=self._write_master(tmp_path, ACME))
        step = self._step(
            tmp_path,
            invoice(),
            checksum={"valid": True, "reason": "Valid checksum"},
        )
        assert step["passed"] is True

    def test_without_a_master_the_checksum_alone_decides(
        self, tmp_path, config
    ):
        config(vendor_master_file=str(tmp_path / "absent.json"))
        step = self._step(
            tmp_path,
            invoice(),
            checksum={"valid": True, "reason": "Valid checksum"},
        )
        assert step["passed"] is True
        assert step["evidence"] == "Valid checksum"

    def test_a_failed_checksum_still_fails_with_a_master_loaded(
        self, tmp_path, config
    ):
        config(vendor_master_file=self._write_master(tmp_path, ACME))
        step = self._step(
            tmp_path,
            invoice(),
            checksum={"valid": False, "reason": "Invalid checksum"},
        )
        assert step["passed"] is False

    def test_a_supplier_nobody_registered_is_reported_even_without_a_checksum(
        self, tmp_path, config
    ):
        """Extraction found no tax ID to check, so there is no checksum result.
        The step used to emit nothing at all in that case -- an invoice from an
        unregistered supplier passed unexamined."""
        config(vendor_master_file=self._write_master(tmp_path, OTHER))
        step = self._step(tmp_path, invoice())
        assert step is not None
        assert step["passed"] is False
        assert step["outcome"] == vendor_master.UNKNOWN_VENDOR


class TestVendorRecordCarriesBankDetails:
    """The DB-backed master hands the pipeline the fields step 3.4 compares."""

    def test_from_dict_reads_bank_and_email_fields(self):
        v = vendor_master.Vendor.from_dict(
            {
                **ACME,
                "email_domain": "Cloudhaven.example",
                "bank_account_number": "901031925572",
                "bank_code": "KCBL0230056",
                "pending_change": True,
            }
        )
        assert v.email_domain == "cloudhaven.example"
        assert v.has_bank and v.pending_change

    def test_missing_fields_default_to_none(self):
        v = vendor_master.Vendor.from_dict(ACME)
        assert v.bank_account_number is None and not v.has_bank
