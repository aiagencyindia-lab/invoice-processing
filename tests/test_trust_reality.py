"""Reality checks (FR-23): is what was invoiced corroborated by deliveries,
hours or seats on record?"""

from decimal import Decimal

import pytest

from invoice_processing.trust.reality import bands, classify

D = Decimal


# -- bands ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("found", "present", "result"),
    [
        ("100", True, "CORROBORATED"),
        ("120", True, "CORROBORATED"),  # over-delivery corroborates
        ("98", True, "CORROBORATED"),  # within 2 %
        ("97", True, "PARTIAL"),
        ("94", True, "PARTIAL"),  # within 3 x 2 %
        ("93", True, "CONTRADICTED"),
        ("0", True, "CONTRADICTED"),  # evidence, but none counts
        ("0", False, "NO_EVIDENCE"),
    ],
)
def test_band(found, present, result):
    assert bands.band(D("100"), D(found), present, D("0.02")) == result


# -- classification ----------------------------------------------------------


def _line(qty="1", unit=None, description="Widget"):
    return {"attrs": {"quantity": qty, "unit": unit, "description": description}}


@pytest.mark.parametrize(
    ("attrs", "lines", "kind"),
    [
        ({"po_number": "PO-1"}, [], "goods"),
        ({}, [_line("5", "kg")], "goods"),
        ({}, [_line("5")], "goods"),
        ({"po_number": "PO-1"}, [_line("10", "hr")], "services"),
        ({"work_authorization_reference": "WA-1"}, [_line()], "services"),
        ({}, [_line("3", "md")], "services"),
        ({}, [_line("40", "seat", "User licence seats")], "subscription"),
        ({}, [_line("1", None, "Annual subscription")], "subscription"),
        ({}, [_line("1")], "other"),
        ({}, [], "other"),
    ],
)
def test_classify(attrs, lines, kind):
    assert classify.classify({"attrs": attrs}, lines) == kind


def test_hours_of_reads_days_as_eight_hours():
    assert classify.hours_of(_line("2", "day")["attrs"]) == D("16")
    assert classify.hours_of(_line("5", "Hrs")["attrs"]) == D("5")
    assert classify.hours_of(_line("5", "kg")["attrs"]) is None
