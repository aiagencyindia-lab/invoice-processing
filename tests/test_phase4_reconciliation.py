"""Phase 4 arithmetic, on invoices that are not the simplest possible shape.

Check 4.1 compared the stated total against `ex + tax`, so a discount, a
freight charge or a rounding line each looked like an error the supplier had
made. Check 4.2 compared the sum of the lines against the subtotal and stopped,
so a quantity changed without its total was invisible. Check 4.4 measured every
invoice against one rate, so a 12% line taxed at 18% and an 18% line taxed at
12% cancelled out perfectly.

All three of the new terms default to absent, which is why an invoice that
states none of them reconciles exactly as it did before.
"""

import pytest

from invoice_processing.shared_libraries.acting import general_invoice_agent
from invoice_processing.shared_libraries.acting.general_invoice_agent import (
    Phase4ValidatorAgent,
)

# 18% on 100,000, a 2,000 discount, 500 of freight, and 0.40 of rounding.
FULL = {
    "currency": "INR",
    "subtotal": 100000.00,
    "discount": 2000.00,
    "invoice_total_ex_tax": 98000.00,
    "other_charges": [
        {"description": "Freight", "amount": 500.00, "tax_rate": 0.18}
    ],
    "round_off": 0.40,
    "tax_amount": 17730.00,
    "invoice_total_inc_tax": 116230.40,
    "line_items": [
        {
            "description": "Bearings",
            "quantity": 20,
            "unit_price": 5000.00,
            "amount_ex_tax": 100000.00,
            "hsn_sac": "8482",
            "tax_rate": 0.18,
        }
    ],
}

LEGACY = {
    "currency": "AUD",
    "invoice_total_ex_tax": 1000.00,
    "tax_amount": 100.00,
    "invoice_total_inc_tax": 1100.00,
    "line_items": [
        {
            "description": "Service",
            "quantity": 10,
            "unit_price": 100.0,
            "amount_ex_tax": 1000.00,
        }
    ],
}


@pytest.fixture
def config(monkeypatch):
    def apply(**overrides):
        settings = general_invoice_agent.DEFAULT_CONFIG.copy()
        settings.update(overrides)
        monkeypatch.setitem(
            general_invoice_agent._config_store, "CONFIG", settings
        )

    return apply


def steps(tmp_path, invoice):
    result = Phase4ValidatorAgent().run(tmp_path, {"invoice": invoice}, {})
    return {v["step"]: v for v in result["validations"]}


class TestTotalVerification:
    def test_discount_freight_and_rounding_reconcile(self, tmp_path, config):
        config()
        assert steps(tmp_path, FULL)["4.1"]["passed"] is True

    def test_a_wrong_stated_total_is_reported(self, tmp_path, config):
        config()
        step = steps(tmp_path, {**FULL, "invoice_total_inc_tax": 115000.00})[
            "4.1"
        ]
        assert step["passed"] is False

    def test_it_never_rejects(self, tmp_path, config):
        """The one thing 4.1 must never do. It reports; 4.2 rejects."""
        config()
        result = Phase4ValidatorAgent().run(
            tmp_path, {"invoice": {**FULL, "invoice_total_inc_tax": 1.0}}, {}
        )
        step = next(v for v in result["validations"] if v["step"] == "4.1")
        assert step["informational"] is True
        assert "rejection_template" not in step
        assert result["decision"] == "ACCEPT"

    def test_the_simple_shape_is_unchanged(self, tmp_path, config):
        config()
        assert steps(tmp_path, LEGACY)["4.1"]["passed"] is True


class TestLineSum:
    def test_lines_that_do_not_reach_the_subtotal_fail(self, tmp_path, config):
        config()
        invoice = {
            **FULL,
            "line_items": [{**FULL["line_items"][0], "amount_ex_tax": 90000.0}],
        }
        assert steps(tmp_path, invoice)["4.2"]["passed"] is False

    def test_a_quantity_changed_without_its_total_is_caught(
        self, tmp_path, config
    ):
        """The sum still matches the subtotal. Only the line's own arithmetic
        gives it away."""
        config()
        invoice = {
            **FULL,
            "line_items": [{**FULL["line_items"][0], "quantity": 25}],
        }
        step = steps(tmp_path, invoice)["4.2"]
        assert step["passed"] is False
        assert "Bearings" in step["evidence"]

    def test_a_line_discount_is_part_of_the_line(self, tmp_path, config):
        config()
        invoice = {
            **FULL,
            "subtotal": 95000.0,
            "invoice_total_ex_tax": 93000.0,
            "line_items": [
                {
                    **FULL["line_items"][0],
                    "line_discount": 5000.0,
                    "amount_ex_tax": 95000.0,
                }
            ],
        }
        assert steps(tmp_path, invoice)["4.2"]["passed"] is True

    def test_a_line_without_a_quantity_is_not_second_guessed(
        self, tmp_path, config
    ):
        """A lump-sum line states an amount and nothing to derive it from."""
        config()
        invoice = {
            **FULL,
            "line_items": [
                {"description": "Retainer", "amount_ex_tax": 100000.00}
            ],
        }
        assert steps(tmp_path, invoice)["4.2"]["passed"] is True


class TestTaxRate:
    def test_two_rates_on_one_invoice(self, tmp_path, config):
        config(tax_rates_by_hsn={"8482": 0.18, "4911": 0.12})
        invoice = {
            **FULL,
            "subtotal": 20000.0,
            "discount": 0,
            "invoice_total_ex_tax": 20000.0,
            "other_charges": [],
            "round_off": 0,
            # 10,000 at 18% + 10,000 at 12% = 3,000
            "tax_amount": 3000.0,
            "invoice_total_inc_tax": 23000.0,
            "line_items": [
                {
                    "description": "Bearings",
                    "amount_ex_tax": 10000.0,
                    "hsn_sac": "8482",
                },
                {
                    "description": "Manuals",
                    "amount_ex_tax": 10000.0,
                    "hsn_sac": "4911",
                },
            ],
        }
        assert steps(tmp_path, invoice)["4.4"]["passed"] is True

    def test_rates_swapped_between_lines_is_caught(self, tmp_path, config):
        """A blended rate cannot see this: the totals cancel out exactly."""
        config(tax_rates_by_hsn={"8482": 0.18, "4911": 0.12})
        invoice = {
            **FULL,
            "subtotal": 20000.0,
            "discount": 0,
            "invoice_total_ex_tax": 20000.0,
            "other_charges": [],
            "round_off": 0,
            "tax_amount": 3000.0,
            "invoice_total_inc_tax": 23000.0,
            "line_items": [
                {
                    "description": "Bearings",
                    "amount_ex_tax": 5000.0,
                    "hsn_sac": "8482",
                },
                {
                    "description": "Manuals",
                    "amount_ex_tax": 15000.0,
                    "hsn_sac": "4911",
                },
            ],
        }
        step = steps(tmp_path, invoice)["4.4"]
        assert step["passed"] is False
        assert "per line" in step["evidence"]

    def test_tax_charged_on_an_exempt_line_is_caught(self, tmp_path, config):
        config(tax_rates_by_hsn={"8482": 0.18, "4901": 0.0})
        invoice = {
            **FULL,
            "subtotal": 15000.0,
            "discount": 0,
            "invoice_total_ex_tax": 15000.0,
            "other_charges": [],
            "round_off": 0,
            "tax_amount": 2700.0,  # 18% of everything, including the exempt line
            "invoice_total_inc_tax": 17700.0,
            "line_items": [
                {
                    "description": "Bearings",
                    "amount_ex_tax": 10000.0,
                    "hsn_sac": "8482",
                },
                {
                    "description": "Printed books",
                    "amount_ex_tax": 5000.0,
                    "hsn_sac": "4901",
                },
            ],
        }
        assert steps(tmp_path, invoice)["4.4"]["passed"] is False

    def test_freight_carries_its_own_rate(self, tmp_path, config):
        config(tax_rates_by_hsn={"8482": 0.18})
        assert steps(tmp_path, FULL)["4.4"]["passed"] is True

    def test_an_unclassified_invoice_uses_the_currency_rate(
        self, tmp_path, config
    ):
        """Nothing on the invoice says what was sold, so there is one rate to
        check against -- exactly as before."""
        config()
        step = steps(tmp_path, LEGACY)["4.4"]
        assert step["passed"] is True
        assert "10%" in step["evidence"]

    def test_component_tax_counts_toward_the_total(self, tmp_path, config):
        """An invoice that splits its tax and states no total charged tax."""
        config(tax_rates_by_hsn={"8482": 0.18})
        invoice = {
            **FULL,
            "tax_amount": None,
            "cgst_amount": 8865.00,
            "sgst_amount": 8865.00,
            "place_of_supply": "24-Gujarat",
            "vendor_tax_id": "24ITCCR9797D1ZQ",
        }
        assert steps(tmp_path, invoice)["4.4"]["passed"] is True


class TestTaxComponents:
    def _invoice(self, **overrides):
        return {
            **FULL,
            "tax_amount": 17730.00,
            "vendor_tax_id": "24ITCCR9797D1ZQ",  # a supplier in state 24
            **overrides,
        }

    def test_one_tax_across_a_state_line_is_right(self, tmp_path, config):
        config(tax_rates_by_hsn={"8482": 0.18})
        invoice = self._invoice(
            place_of_supply="29-Karnataka", igst_amount=17730.00
        )
        assert steps(tmp_path, invoice)["4.4"]["passed"] is True

    def test_one_tax_within_a_state_is_wrong(self, tmp_path, config):
        """The amount is right to the rupee, and the tax is the wrong kind --
        a filing error the customer carries, and one a single total hides."""
        config(tax_rates_by_hsn={"8482": 0.18})
        invoice = self._invoice(
            place_of_supply="24-Gujarat", igst_amount=17730.00
        )
        step = steps(tmp_path, invoice)["4.4"]
        assert step["passed"] is False
        assert "IGST" in step["evidence"]

    def test_a_split_across_a_state_line_is_wrong(self, tmp_path, config):
        config(tax_rates_by_hsn={"8482": 0.18})
        invoice = self._invoice(
            place_of_supply="29-Karnataka",
            cgst_amount=8865.00,
            sgst_amount=8865.00,
        )
        assert steps(tmp_path, invoice)["4.4"]["passed"] is False

    def test_unequal_halves_within_a_state_are_wrong(self, tmp_path, config):
        config(tax_rates_by_hsn={"8482": 0.18})
        invoice = self._invoice(
            place_of_supply="24-Gujarat",
            cgst_amount=10000.00,
            sgst_amount=7730.00,
        )
        assert steps(tmp_path, invoice)["4.4"]["passed"] is False

    def test_an_invoice_that_states_no_components_is_not_judged_on_them(
        self, tmp_path, config
    ):
        config(tax_rates_by_hsn={"8482": 0.18})
        assert steps(tmp_path, FULL)["4.4"]["passed"] is True


CHARGE_TWICE = {
    "currency": "INR",
    "subtotal": 100000.00,
    "invoice_total_ex_tax": 100000.00,
    "tax_amount": 18450.00,  # 18% of 100,000 plus 18% of the 2,500 charge
    "invoice_total_inc_tax": 120950.00,
    "other_charges": [
        {
            "description": "Freight & forwarding",
            "amount": 2500.00,
            "tax_rate": 0.18,
        }
    ],
    "line_items": [
        {
            "description": "Bearings",
            "quantity": 20,
            "unit_price": 5000.00,
            "amount_ex_tax": 100000.00,
            "hsn_sac": "8482",
        },
        # The same charge, returned again as a line.
        {"description": "Freight & forwarding", "amount_ex_tax": 2500.00},
    ],
}


class TestAChargeReadTwice:
    """Freight and packing are printed outside the line table, and the
    extractor returns them in both places often enough that it is the single
    largest source of false rejections in the corpus: 11 of check 4.2's 20 and
    9 of check 4.4's 12. On one invoice the tax overshot by 455.94, which is
    2,533.00 of packing at 18% to the cent."""

    INVOICE = CHARGE_TWICE

    def test_the_lines_still_reconcile(self, tmp_path, config):
        config()
        assert steps(tmp_path, self.INVOICE)["4.2"]["passed"] is True

    def test_the_tax_is_not_charged_twice(self, tmp_path, config):
        config(tax_rates_by_hsn={"8482": 0.18})
        assert steps(tmp_path, self.INVOICE)["4.4"]["passed"] is True

    def test_a_genuine_line_of_equal_value_is_not_swallowed(
        self, tmp_path, config
    ):
        """Two things costing the same are not the same thing."""
        config()
        invoice = {
            **self.INVOICE,
            "subtotal": 102500.00,
            "invoice_total_ex_tax": 102500.00,
            "line_items": [
                self.INVOICE["line_items"][0],
                {"description": "Spare seals", "amount_ex_tax": 2500.00},
            ],
        }
        assert steps(tmp_path, invoice)["4.2"]["passed"] is True


class TestADiscountColumn:
    """The discount column is a percentage on most layouts. Reading "10%" as
    ten rupees rejected five real invoices for arithmetic the supplier got
    right."""

    def _invoice(self, **line):
        return {
            "currency": "INR",
            "subtotal": 81378.00,
            "invoice_total_ex_tax": 81378.00,
            "tax_amount": 14648.04,
            "invoice_total_inc_tax": 96026.04,
            "line_items": [
                {
                    "description": "Masterbatch black",
                    "quantity": 20,
                    "unit_price": 4521.00,  # 90,420 less 10% is 81,378
                    "amount_ex_tax": 81378.00,
                    "hsn_sac": "3204",
                    **line,
                }
            ],
        }

    def test_a_percentage_in_its_own_field(self, tmp_path, config):
        config()
        assert (
            steps(tmp_path, self._invoice(line_discount_pct=0.10))["4.2"][
                "passed"
            ]
            is True
        )

    def test_a_percentage_returned_as_an_amount(self, tmp_path, config):
        """What the extractor actually does: "10%" comes back as 0.1. A
        discount of ten paise is not a reading worth defending."""
        config()
        assert (
            steps(tmp_path, self._invoice(line_discount=0.10))["4.2"]["passed"]
            is True
        )

    def test_a_real_amount_still_works(self, tmp_path, config):
        config()
        assert (
            steps(tmp_path, self._invoice(line_discount=9042.00))["4.2"][
                "passed"
            ]
            is True
        )

    def test_a_discount_we_can_see_but_not_read_does_not_reject(
        self, tmp_path, config
    ):
        config()
        step = steps(tmp_path, self._invoice(has_discount=True))["4.2"]
        assert step["passed"] is True

    def test_a_line_with_no_discount_at_all_is_still_checked(
        self, tmp_path, config
    ):
        """The fault this check exists for: a quantity changed without its
        total, on a line that shows no discount to explain it."""
        config()
        invoice = self._invoice()
        invoice["line_items"][0]["quantity"] = 25
        assert steps(tmp_path, invoice)["4.2"]["passed"] is False


class TestWhatTheToleranceGivesUp:
    """The tax tolerance is now the larger of a fixed amount and a share of
    the tax due, and that choice loses something. It is recorded here rather
    than discovered later.
    """

    def test_a_wrong_line_discount_is_still_caught_when_it_can_be_read(
        self, tmp_path, config
    ):
        """The fault the percentage handling must not swallow: a discount
        applied wrongly. Readable, so it is checked."""
        config()
        invoice = {
            "currency": "INR",
            "subtotal": 85000.00,
            "invoice_total_ex_tax": 85000.00,
            "tax_amount": 15300.00,
            "invoice_total_inc_tax": 100300.00,
            "line_items": [
                {
                    "description": "Masterbatch",
                    "quantity": 20,
                    "unit_price": 4521.00,
                    "line_discount_pct": 0.10,  # 90,420 less 10% is 81,378
                    "amount_ex_tax": 85000.00,  # and the invoice says 85,000
                    "hsn_sac": "3204",
                }
            ],
        }
        assert steps(tmp_path, invoice)["4.2"]["passed"] is False

    def test_a_rounding_error_below_our_own_precision_is_not_caught(
        self, tmp_path, config
    ):
        """4.20 on 250,751.58 is 0.0017%. Legitimate per-line rounding and
        discount apportionment on this corpus reach 0.075%, so this fault is
        smaller than the noise it would have to be told apart from -- no
        tolerance separates them. Catching it with a fixed one-rupee band costs
        twelve wrongly rejected invoices for two discrepancies of four rupees,
        which is the wrong trade for a system that pays suppliers.
        """
        config(tax_rates_by_hsn={"8482": 0.18})
        invoice = {
            "currency": "INR",
            "subtotal": 1393041.00,
            "invoice_total_ex_tax": 1393041.00,
            "tax_amount": 250751.58,  # 4.20 above the statutory 250,747.38
            "invoice_total_inc_tax": 1643792.58,
            "line_items": [
                {
                    "description": "Bearings",
                    "amount_ex_tax": 1393041.00,
                    "hsn_sac": "8482",
                }
            ],
        }
        assert steps(tmp_path, invoice)["4.4"]["passed"] is True

    def test_a_wrong_rate_is_still_far_outside_the_tolerance(
        self, tmp_path, config
    ):
        """What the check is actually for: 12% charged where 18% is due is a
        third of the tax, not a fifth of a percent."""
        config(tax_rates_by_hsn={"8482": 0.18})
        invoice = {
            "currency": "INR",
            "subtotal": 1393041.00,
            "invoice_total_ex_tax": 1393041.00,
            "tax_amount": 167164.92,
            "invoice_total_inc_tax": 1560205.92,
            "line_items": [
                {
                    "description": "Bearings",
                    "amount_ex_tax": 1393041.00,
                    "hsn_sac": "8482",
                }
            ],
        }
        assert steps(tmp_path, invoice)["4.4"]["passed"] is False
