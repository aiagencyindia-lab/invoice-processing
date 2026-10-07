"""Check 2.1: are the charges actually broken down?

It counted lines, so a lump sum passed as one line (manual verification
MV-018: "Professional services, 1 lot, 2,81,160", accepted), and so did a
document with no table, which the extractor turns into one line of prose with
no quantity and no price.
"""

from invoice_processing.shared_libraries.acting.general_invoice_agent import (
    _itemized,
)


def _line(**fields):
    return {"amount_ex_tax": 1000.0, **fields}


class TestWhatIsNotABreakdown:
    def test_one_lot_of_professional_services(self):
        itemized, evidence = _itemized(
            {
                "line_items": [
                    _line(
                        description="Professional services",
                        quantity=1,
                        unit="lot",
                        unit_price=281160.0,
                    )
                ]
            }
        )
        assert not itemized
        assert "lump-sum" in evidence

    def test_a_line_written_from_prose_with_no_quantity_or_rate(self):
        itemized, _ = _itemized(
            {
                "line_items": [
                    _line(
                        description="Charges for the period as per engagement letter"
                    )
                ]
            }
        )
        assert not itemized

    def test_no_lines_at_all(self):
        assert _itemized({"line_items": []})[0] is False


class TestWhatIs:
    def test_one_priced_line_of_counted_goods(self):
        """MFG-INV-042: 50 bearings at 557 is one line and a breakdown."""
        assert _itemized(
            {"line_items": [_line(quantity=50, unit="nos", unit_price=557.0)]}
        )[0]

    def test_hours_at_a_rate(self):
        assert _itemized(
            {
                "line_items": [
                    _line(quantity=288, unit="hour", unit_price=4659.0)
                ]
            }
        )[0]

    def test_one_lot_among_other_lines(self):
        """A lot is only a lump sum when it is the whole invoice."""
        assert _itemized(
            {
                "line_items": [
                    _line(quantity=50, unit="nos", unit_price=557.0),
                    _line(quantity=1, unit="lot", unit_price=1200.0),
                ]
            }
        )[0]
