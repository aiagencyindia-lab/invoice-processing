"""Two-way match: invoice against purchase order.

Step 2.2 reported `passed: True` while checking nothing, so an account that
demanded purchase orders received no purchase-order check at all.

The tests are weighted towards what a match is *for*. A PO check that only
confirms the number exists catches clerical noise; the ones that matter are the
vendor check, which catches an invoice quoting somebody else's commitment, and
the cumulative check, which catches over-billing split across several invoices
that each look reasonable alone.
"""

import json

import pytest

from invoice_processing import paths, po_matching
from invoice_processing.po_matching import (
    CURRENCY_MISMATCH,
    EXCEEDS_REMAINING,
    MATCHED,
    NO_PO_ON_INVOICE,
    OVER_BILLED,
    PO_CLOSED,
    PO_NOT_FOUND,
    VENDOR_MISMATCH,
    PurchaseOrder,
    match_invoice_to_po,
    normalize_po_number,
    normalize_vendor,
    to_validation,
    tolerance_for,
)
from invoice_processing.shared_libraries.acting import (
    general_invoice_agent as agent,
)


class Orders:
    """Stands in for the ERP."""

    def __init__(self, *orders):
        self.by_number = {
            normalize_po_number(o["po_number"]): o for o in orders
        }

    def find(self, po_number):
        return self.by_number.get(normalize_po_number(po_number))


def po(**overrides):
    return {
        "po_number": "PO-4471",
        "vendor_name": "Acme Pty Ltd",
        "total_amount": 10_000.0,
        "currency": "AUD",
        "status": "open",
        "amount_invoiced": 0.0,
        **overrides,
    }


def invoice(**overrides):
    return {
        "po_number": "PO-4471",
        "vendor_name": "Acme Pty Ltd",
        "invoice_total_inc_tax": 5_000.0,
        "currency": "AUD",
        **overrides,
    }


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


class TestNormalisation:
    @pytest.mark.parametrize(
        "value", ["PO-4471", "po 4471", "PO4471", " po-4471 ", "P.O. 4471"]
    )
    def test_the_same_po_written_five_ways(self, value):
        """Vendors type all of these; only a string comparison disagrees."""
        assert normalize_po_number(value) == normalize_po_number("PO4471")

    def test_different_purchase_orders_stay_different(self):
        assert normalize_po_number("PO-4471") != normalize_po_number("PO-4472")

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_absent_values(self, value):
        assert normalize_po_number(value) == ""

    def test_legal_suffixes_do_not_break_the_vendor_check(self):
        """The PO says 'Acme Pty Ltd'; the invoice letterhead says 'Acme'."""
        assert normalize_vendor("Acme Pty Ltd") == normalize_vendor("ACME")
        assert normalize_vendor("Globex Inc.") == normalize_vendor("Globex")

    def test_genuinely_different_vendors_stay_different(self):
        assert normalize_vendor("Acme Pty Ltd") != normalize_vendor(
            "Globex Inc"
        )


class TestTolerance:
    def test_the_larger_of_percentage_and_absolute_applies(self):
        assert tolerance_for(100.0) == 50.0  # absolute wins on a small PO
        assert (
            tolerance_for(100_000.0) == 2000.0
        )  # percentage wins on a large one

    def test_it_is_never_negative(self):
        assert tolerance_for(-500.0) >= 0


# ---------------------------------------------------------------------------
# The checks that carry the value
# ---------------------------------------------------------------------------


class TestVendorMismatch:
    """An invoice quoting another supplier's PO is the expensive failure."""

    def test_a_different_vendor_is_refused(self):
        result = match_invoice_to_po(
            invoice(vendor_name="Globex Inc"), Orders(po())
        )
        assert result.outcome == VENDOR_MISMATCH
        assert result.blocking

    def test_the_amount_can_look_perfectly_reasonable(self):
        """Every other check passes while this one fails; hence it runs first."""
        result = match_invoice_to_po(
            invoice(vendor_name="Globex Inc", invoice_total_inc_tax=100.0),
            Orders(po()),
        )
        assert result.outcome == VENDOR_MISMATCH

    def test_the_reason_names_both_parties(self):
        """A clerk must be able to see which PO belongs to whom."""
        result = match_invoice_to_po(
            invoice(vendor_name="Globex Inc"), Orders(po())
        )
        assert "Acme" in result.reason and "Globex" in result.reason

    def test_a_legal_suffix_difference_is_not_a_mismatch(self):
        result = match_invoice_to_po(
            invoice(vendor_name="ACME"), Orders(po(vendor_name="Acme Pty Ltd"))
        )
        assert result.outcome == MATCHED


class TestCumulativeOverBilling:
    """Five invoices under the total can still take the sum past it."""

    def test_an_invoice_within_the_remaining_balance_matches(self):
        result = match_invoice_to_po(
            invoice(invoice_total_inc_tax=3_000.0),
            Orders(po(amount_invoiced=6_000.0)),
        )
        assert result.outcome == MATCHED

    def test_an_invoice_beyond_the_remaining_balance_is_refused(self):
        """Under the PO total, but the sum is not."""
        result = match_invoice_to_po(
            invoice(invoice_total_inc_tax=5_000.0),
            Orders(po(amount_invoiced=6_000.0)),
        )
        assert result.outcome == EXCEEDS_REMAINING
        assert result.blocking

    def test_a_per_invoice_check_alone_would_have_passed_it(self):
        """The regression guard: 5,000 is well under the 10,000 PO."""
        order = PurchaseOrder.from_dict(po(amount_invoiced=6_000.0))
        assert 5_000.0 < order.total_amount
        assert 5_000.0 > order.remaining

    def test_the_reason_shows_the_arithmetic(self):
        result = match_invoice_to_po(
            invoice(invoice_total_inc_tax=5_000.0),
            Orders(po(amount_invoiced=6_000.0)),
        )
        for figure in ("5,000", "4,000", "10,000", "6,000"):
            assert figure in result.reason, result.reason


class TestAmounts:
    def test_billing_under_the_po_is_normal(self):
        """Partial delivery and staged work are not mismatches."""
        result = match_invoice_to_po(
            invoice(invoice_total_inc_tax=1_000.0), Orders(po())
        )
        assert result.outcome == MATCHED

    def test_over_billing_beyond_tolerance_is_refused(self):
        result = match_invoice_to_po(
            invoice(invoice_total_inc_tax=11_000.0), Orders(po())
        )
        assert result.outcome == OVER_BILLED
        assert result.variance == 1000.0

    def test_over_billing_within_tolerance_passes(self):
        """Rounding and freight routinely put an invoice slightly over."""
        result = match_invoice_to_po(
            invoice(invoice_total_inc_tax=10_150.0), Orders(po())
        )
        assert result.outcome == MATCHED

    def test_the_tolerance_is_configurable(self):
        strict = match_invoice_to_po(
            invoice(invoice_total_inc_tax=10_150.0),
            Orders(po()),
            tolerance_pct=0.0,
            tolerance_abs=10.0,
        )
        assert strict.outcome == OVER_BILLED

    def test_an_unreadable_total_does_not_fabricate_a_verdict(self):
        result = match_invoice_to_po(
            invoice(invoice_total_inc_tax=None), Orders(po())
        )
        assert result.outcome == MATCHED
        assert "not readable" in result.reason


class TestPresenceAndStatus:
    def test_a_missing_po_fails_when_one_is_required(self):
        result = match_invoice_to_po(
            invoice(po_number=None), Orders(po()), required=True
        )
        assert result.outcome == NO_PO_ON_INVOICE
        assert result.blocking

    def test_a_missing_po_passes_when_none_is_required(self):
        """A missing PO is only a failure where a PO was expected."""
        result = match_invoice_to_po(
            invoice(po_number=None), Orders(po()), required=False
        )
        assert result.outcome == MATCHED

    def test_an_unknown_po_is_refused(self):
        result = match_invoice_to_po(invoice(po_number="PO-9999"), Orders(po()))
        assert result.outcome == PO_NOT_FOUND

    def test_an_empty_po_source_does_not_silently_pass(self):
        """A check that can see no POs must not report success."""
        result = match_invoice_to_po(invoice(), Orders())
        assert result.outcome == PO_NOT_FOUND
        assert result.blocking

    @pytest.mark.parametrize("status", ["closed", "cancelled", "on_hold"])
    def test_a_closed_po_cannot_be_invoiced(self, status):
        result = match_invoice_to_po(invoice(), Orders(po(status=status)))
        assert result.outcome == PO_CLOSED

    @pytest.mark.parametrize(
        "status",
        ["open", "approved", "partially_invoiced", "partially_received"],
    )
    def test_open_statuses_are_accepted(self, status):
        result = match_invoice_to_po(invoice(), Orders(po(status=status)))
        assert result.outcome == MATCHED

    def test_a_currency_mismatch_is_refused(self):
        result = match_invoice_to_po(
            invoice(currency="USD"), Orders(po(currency="AUD"))
        )
        assert result.outcome == CURRENCY_MISMATCH


# ---------------------------------------------------------------------------
# What the pipeline records
# ---------------------------------------------------------------------------


class TestValidationEntry:
    def test_a_match_passes_and_carries_no_rejection(self):
        entry = to_validation(match_invoice_to_po(invoice(), Orders(po())))
        assert entry["passed"] is True
        assert entry["step"] == "2.2"
        assert entry["implemented"] is True
        assert "rejection_template" not in entry

    @pytest.mark.parametrize(
        ("outcome_invoice", "orders", "expected"),
        [
            ({"po_number": None}, [po()], "Purchase order number required"),
            ({"po_number": "PO-1"}, [po()], "Purchase order not recognised"),
            (
                {"vendor_name": "Globex Inc"},
                [po()],
                "Purchase order belongs to a different supplier",
            ),
            (
                {"invoice_total_inc_tax": 50_000.0},
                [po()],
                "Invoice exceeds purchase order value",
            ),
        ],
    )
    def test_each_failure_tells_the_vendor_what_to_fix(
        self, outcome_invoice, orders, expected
    ):
        """'PO mismatch' sends them back to ask which part."""
        entry = to_validation(
            match_invoice_to_po(invoice(**outcome_invoice), Orders(*orders))
        )
        assert entry["passed"] is False
        assert entry["rejection_template"] == expected

    def test_the_variance_is_recorded_for_review(self):
        entry = to_validation(
            match_invoice_to_po(
                invoice(invoice_total_inc_tax=11_000.0), Orders(po())
            )
        )
        assert entry["variance"] == 1000.0
        assert entry["tolerance"] == 200.0

    def test_no_entry_claims_to_be_unimplemented(self):
        entry = to_validation(match_invoice_to_po(invoice(), Orders(po())))
        assert entry.get("status") != "NOT_IMPLEMENTED"
        assert "NOT IMPLEMENTED" not in entry["evidence"]


# ---------------------------------------------------------------------------
# The PO source
# ---------------------------------------------------------------------------


class TestPurchaseOrderFile:
    @staticmethod
    def _source(tmp_path, payload):

        path = tmp_path / "purchase_orders.json"
        if payload is not None:
            path.write_text(json.dumps(payload))
        return agent._PurchaseOrderFile(path)

    def test_reads_a_list(self, tmp_path):
        source = self._source(tmp_path, [po()])
        assert source.find("PO-4471")["vendor_name"] == "Acme Pty Ltd"

    def test_reads_a_wrapped_object(self, tmp_path):
        source = self._source(tmp_path, {"purchase_orders": [po()]})
        assert source.find("PO-4471") is not None

    def test_lookup_is_normalised(self, tmp_path):
        source = self._source(tmp_path, [po(po_number="PO-4471")])
        assert source.find("po 4471") is not None

    def test_a_missing_file_finds_nothing_rather_than_raising(self, tmp_path):
        assert self._source(tmp_path, None).find("PO-4471") is None

    def test_malformed_json_does_not_crash_the_pipeline(self, tmp_path):
        path = tmp_path / "purchase_orders.json"
        path.write_text("{not json")

        assert agent._PurchaseOrderFile(path).find("PO-4471") is None

    def test_an_unreadable_source_fails_closed(self, tmp_path):
        """No POs known must surface as 'not found', never as a pass."""
        source = self._source(tmp_path, None)
        result = match_invoice_to_po(invoice(), source)
        assert result.outcome == PO_NOT_FOUND
        assert result.blocking


class TestPipelineWiring:
    """An engine nothing calls is the defect this codebase already had once."""

    def test_step_22_calls_the_matcher(self):

        source = (
            paths.AGENT_PKG_DIR
            / "shared_libraries"
            / "acting"
            / "general_invoice_agent.py"
        ).read_text(encoding="utf-8")
        assert "po_matching.match_invoice_to_po" in source
        assert "NOT IMPLEMENTED - no purchase order check" not in source

    def test_it_runs_when_a_po_is_quoted_even_if_not_required(self):
        """An invoice naming a PO asserts a commitment worth checking."""

        source = (
            paths.AGENT_PKG_DIR
            / "shared_libraries"
            / "acting"
            / "general_invoice_agent.py"
        ).read_text(encoding="utf-8")
        assert 'require_purchase_order") or invoice.get("po_number")' in source

    def test_the_matcher_satisfies_the_source_protocol(self):
        assert isinstance(Orders(po()), po_matching.PurchaseOrders)


class TestVendorNormalisationIsWordwise:
    """Legal suffixes used to be stripped as bare substrings anywhere in the
    name, which is invisible while both sides are mangled the same way and
    dangerous the moment a vendor master is indexed by the result."""

    def test_a_suffix_inside_a_word_survives(self):
        assert po_matching.normalize_vendor("Coca Cola Company") == "COCA COLA"
        assert (
            po_matching.normalize_vendor("Quantar DevOps Consulting Pvt Ltd")
            == "QUANTAR DEVOPS CONSULTING PVT"
        )

    def test_two_different_suppliers_do_not_collapse_together(self):
        """The failure that matters: one supplier's invoice checked against
        another's purchase order or tax ID."""
        assert po_matching.normalize_vendor(
            "Corp Solutions"
        ) != po_matching.normalize_vendor("Solutions")

    def test_the_same_company_written_two_ways_still_matches(self):
        assert po_matching.normalize_vendor(
            "Cloudhaven Infra Services Pvt Ltd"
        ) == po_matching.normalize_vendor(
            "Cloudhaven Infra Services Private Limited"
        )

    def test_a_private_limited_is_not_its_llp_namesake(self):
        """Different legal entities, and after suffix-stripping the legal form
        is the only thing left telling them apart."""
        assert po_matching.normalize_vendor(
            "Lumora Brandcraft Pvt Ltd"
        ) != po_matching.normalize_vendor("Lumora Brandcraft LLP")
