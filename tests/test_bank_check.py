"""Step 3.4. Payment redirection works by changing an account number, so the
bank details printed on an invoice are compared with the ones two people
verified for the vendor. Every signal holds the invoice for review; none
rejects, because a rejection letter tells a fraudster the control exists."""

from datetime import date

from invoice_processing import bank_check
from invoice_processing.vendor_master import Vendor

TODAY = date(2026, 9, 3)
VERIFIED = Vendor(
    name="Precision Weld Fabricators",
    tax_id="27AAAAA0000A1Z5",
    email_domain="precisionweld.example",
    bank_account_name="Precision Weld Fabricators",
    bank_account_number="181386752041",
    bank_code="SCBL0477815",
    bank_verified_at="2026-01-10T00:00:00+00:00",
    last_bank_change_at="2026-01-10T00:00:00+00:00",
)
UNVERIFIED = Vendor(name="Precision Weld Fabricators", tax_id="27AAAAA0000A1Z5")


def invoice(**overrides):
    base = {
        "vendor_name": "Precision Weld Fabricators",
        "bank_account_name": "Precision Weld Fabricators",
        "bank_account_number": "181386752041",
        "bank_code": "SCBL0477815",
    }
    return {**base, **overrides}


def check(inv, vendor=VERIFIED, sender=None, recent_days=30):
    return bank_check.compare(
        inv,
        vendor,
        sender=sender,
        reference_date=TODAY,
        recent_days=recent_days,
    )


class TestExactMatchIsSilent:
    def test_match_passes_with_evidence(self):
        result = check(invoice())
        assert result.passed and result.compared
        assert result.evidence == ["Bank details match vendor record"]

    def test_spaces_in_account_number_do_not_matter(self):
        assert check(invoice(bank_account_number="1813 8675 2041")).passed


class TestNothingToCompare:
    def test_no_bank_details_printed(self):
        result = check(
            invoice(
                bank_account_number=None, bank_code=None, bank_account_name=None
            )
        )
        assert not result.compared and result.passed

    def test_unknown_vendor_is_3_2s_problem(self):
        assert not check(invoice(), vendor=None).compared

    def test_unverified_vendor_and_no_details_printed(self):
        result = check(
            invoice(
                bank_account_number=None, bank_code=None, bank_account_name=None
            ),
            vendor=UNVERIFIED,
        )
        assert not result.compared


class TestMismatch:
    def test_changed_account_number(self):
        result = check(invoice(bank_account_number="476661095239"))
        assert result.signals == [bank_check.BANK_MISMATCH]
        assert "…5239" in result.evidence[0] and "…2041" in result.evidence[0]

    def test_changed_code(self):
        assert check(invoice(bank_code="HDFC0001234")).signals == [
            bank_check.BANK_MISMATCH
        ]

    def test_no_o_zero_folding(self):
        """MFG-INV-011: OCR read the IFSC's zero as O. A hold is the correct
        outcome -- a changed code is exactly what fraud changes."""
        assert check(invoice(bank_code="SCBLO477815")).signals == [
            bank_check.BANK_MISMATCH
        ]


class TestUnverified:
    def test_first_seen_bank_details(self):
        result = check(invoice(), vendor=UNVERIFIED)
        assert result.signals == [bank_check.BANK_UNVERIFIED]
        assert "No verified bank details" in result.evidence[0]


class TestPayeeName:
    def test_third_party_payee(self):
        result = check(invoice(bank_account_name="R K Traders"))
        assert bank_check.PAYEE_NAME_DIFFERS in result.signals

    def test_legal_suffix_differences_are_not_a_signal(self):
        assert check(
            invoice(bank_account_name="Precision Weld Fabricators Ltd")
        ).passed


class TestRecentChange:
    def test_pending_change(self):
        vendor = Vendor(**{**VERIFIED.__dict__, "pending_change": True})
        assert (
            bank_check.RECENT_BANK_CHANGE
            in check(invoice(), vendor=vendor).signals
        )

    def test_approved_within_window(self):
        vendor = Vendor(
            **{
                **VERIFIED.__dict__,
                "last_bank_change_at": "2026-08-20T10:00:00+00:00",
            }
        )
        assert (
            bank_check.RECENT_BANK_CHANGE
            in check(invoice(), vendor=vendor).signals
        )

    def test_approved_outside_window(self):
        vendor = Vendor(
            **{
                **VERIFIED.__dict__,
                "last_bank_change_at": "2026-07-01T10:00:00+00:00",
            }
        )
        assert check(invoice(), vendor=vendor).passed


class TestSenderDomain:
    def test_lookalike_domain(self):
        result = check(
            invoice(), sender="Billing <billing@precisionweld-invoices.example>"
        )
        assert result.signals == [bank_check.SENDER_DOMAIN_MISMATCH]
        assert "precisionweld.example" in result.evidence[0]

    def test_matching_domain_any_case(self):
        assert check(invoice(), sender="BILLING@PrecisionWeld.example").passed

    def test_no_vendor_domain_means_no_signal(self):
        vendor = Vendor(**{**VERIFIED.__dict__, "email_domain": None})
        assert check(invoice(), vendor=vendor, sender="x@free.example").passed

    def test_upload_has_no_sender(self):
        assert check(invoice(), sender=None).passed


class TestOrdering:
    def test_most_severe_first(self):
        vendor = Vendor(**{**VERIFIED.__dict__, "pending_change": True})
        result = check(
            invoice(bank_account_number="1", bank_account_name="X"),
            vendor=vendor,
            sender="a@b.example",
        )
        assert result.signals == [
            bank_check.BANK_MISMATCH,
            bank_check.PAYEE_NAME_DIFFERS,
            bank_check.RECENT_BANK_CHANGE,
            bank_check.SENDER_DOMAIN_MISMATCH,
        ]
