import pytest

from invoice_processing.three_way import (
    NOT_RECEIVED,
    OVER_ORDERED,
    PRICE_OVER_PO,
    SHORT_RECEIVED,
    UNPAIRED_LINE,
    UNREADABLE_LINE,
    match,
    pair,
)


def inv(desc, q, p, **kw):
    return {"description": desc, "quantity": q, "unit_price": p, **kw}


def po(n, desc, q, p, **kw):
    return {
        "line_number": n,
        "description": desc,
        "qty_ordered": q,
        "unit_price": p,
        **kw,
    }


def test_pairs_by_item_code_then_line_number_then_description():
    inv_lines = [
        inv("whatever", 1, 1, item_code="b-6205"),
        inv("x", 1, 1, po_line_number=3),
        inv("Deep groove ball bearing 6205 2RS", 1, 1),
    ]
    po_lines = [
        po(1, "Ball bearing 6205-2RS", 10, 100, item_code="B6205"),
        po(2, "Deep groove ball bearing 6205-2RS", 10, 100),
        po(3, "Grease", 1, 1),
    ]
    assert pair(inv_lines, po_lines) == [
        (1, "item_code"),
        (3, "line_number"),
        (2, "description"),
    ]


def test_near_tie_description_is_unpaired():
    po_lines = [
        po(1, "Hex bolt M8 zinc", 1, 1),
        po(2, "Hex bolt M10 zinc", 1, 1),
    ]
    assert pair([inv("Hex bolt zinc", 1, 1)], po_lines) == [(None, None)]


def test_a_po_line_is_claimed_once():
    po_lines = [po(1, "Bearing 6205", 1, 1)]
    assert pair(
        [inv("Bearing 6205", 1, 1), inv("Bearing 6205", 1, 1)], po_lines
    )[1] == (None, None)


def test_short_receipt_holds_with_grn_evidence():
    r = match(
        [inv("Bearing 6205", 100, 100)],
        [po(1, "Bearing 6205", 100, 100)],
        {1: 60},
        {1: 0},
        vendor_kind="goods",
    )
    assert (
        r.signals == (SHORT_RECEIVED,)
        and "billed 100, received 60" in r.lines[0].evidence
        and not r.passed
    )
    r = match(
        [inv("Bearing 6205", 100, 100)],
        [po(1, "Bearing 6205", 100, 100)],
        {1: 60},
        {},
        vendor_kind="goods",
        grn_numbers={1: ["GRN-7"]},
    )
    assert "received 60 (GRN-7)" in r.evidence[0]


def test_not_received_vs_split_delivery_passes():
    assert match(
        [inv("B", 10, 1)], [po(1, "B", 10, 1)], {}, {}, vendor_kind="goods"
    ).signals == (NOT_RECEIVED,)
    assert match(
        [inv("B", 4, 1)],
        [po(1, "B", 10, 1)],
        {1: 10},
        {1: 6},
        vendor_kind="goods",
    ).passed


def test_billed_plus_this_invoice_over_received_or_ordered():
    assert match(
        [inv("B", 5, 1)],
        [po(1, "B", 10, 1)],
        {1: 10},
        {1: 6},
        vendor_kind="goods",
    ).signals == (OVER_ORDERED,)
    assert match(
        [inv("B", 5, 1)],
        [po(1, "B", 20, 1)],
        {1: 10},
        {1: 6},
        vendor_kind="goods",
    ).signals == (SHORT_RECEIVED,)


def test_receipt_required_fallback_and_unknown_vendor_holds():
    assert match(
        [inv("Freight", 1, 50)],
        [po(1, "Freight", 1, 50, receipt_required=False)],
        {},
        {},
        vendor_kind="goods",
    ).passed
    assert match(
        [inv("Hours", 8, 50)],
        [po(1, "Hours", 8, 50)],
        {},
        {},
        vendor_kind="service",
    ).passed
    assert match(
        [inv("B", 1, 1)], [po(1, "B", 1, 1)], {}, {}, vendor_kind=None
    ).signals == (NOT_RECEIVED,)


def test_price_tolerance_is_pct_or_abs_whichever_larger_and_under_passes():
    def m(p, po_p):
        return match(
            [inv("B", 1, p)],
            [po(1, "B", 1, po_p, receipt_required=False)],
            {},
            {},
            vendor_kind="goods",
        )

    assert m(149, 100).passed
    assert m(151, 100).signals == (PRICE_OVER_PO,)
    assert m(10_300, 10_000).signals == (PRICE_OVER_PO,)
    assert m(80, 100).passed


def test_unreadable_quantity_is_held_not_zero():
    r = match(
        [inv("B", None, 100)],
        [po(1, "B", 1, 100)],
        {1: 1},
        {},
        vendor_kind="goods",
    )
    assert r.signals == (UNREADABLE_LINE,)


def test_unpaired_line_holds_and_names_the_line():
    r = match(
        [inv("Mystery surcharge", 1, 1)],
        [po(1, "Bearing", 1, 1)],
        {1: 1},
        {},
        vendor_kind="goods",
    )
    assert (
        r.signals == (UNPAIRED_LINE,)
        and "line 1 'Mystery surcharge' matched no PO line" in r.evidence[0]
    )


def test_qty_tolerance_and_to_dict_roundtrip():
    r = match(
        [inv("B", 10, 1)],
        [po(1, "B", 10, 1)],
        {1: 9.5},
        {},
        vendor_kind="goods",
        qty_tol=1,
    )
    d = r.lines[0].to_dict()
    assert (
        r.passed
        and d["po_line_number"] == 1
        and d["quantity"] == 10
        and d["signals"] == []
    )


def test_line_can_carry_quantity_and_price_signals():
    r = match(
        [inv("B", 1, 500)], [po(1, "B", 1, 100)], {}, {}, vendor_kind="goods"
    )
    assert r.signals == (NOT_RECEIVED, PRICE_OVER_PO) and len(r.evidence) == 1


def test_non_finite_numbers_hold_as_unreadable_never_pass():
    free = {"receipt_required": False}
    cases = [
        ([inv("B", "nan", 1)], [po(1, "B", 1, 1, **free)], {}, {}),
        ([inv("B", 1, "inf")], [po(1, "B", 1, 1, **free)], {}, {}),
        ([inv("B", 1, "-Infinity")], [po(1, "B", 1, 1, **free)], {}, {}),
        ([inv("B", 1, 1)], [po(1, "B", 1, "nan", **free)], {}, {}),
        ([inv("B", 1, 1)], [po(1, "B", "nan", 1, **free)], {}, {}),
        ([inv("B", 1, 1)], [po(1, "B", 1, 1)], {1: float("nan")}, {}),
        ([inv("B", 1, 1)], [po(1, "B", 1, 1)], {1: 1}, {1: float("inf")}),
    ]
    for inv_lines, po_lines, rec, bil in cases:
        r = match(inv_lines, po_lines, rec, bil, vendor_kind="goods")
        assert r.signals == (UNREADABLE_LINE,), (inv_lines, po_lines, rec, bil)


def test_identical_wording_ties_break_by_price_then_line_number():
    po_lines = [
        po(n, "Hex bolt M8", 9, pr) for n, pr in ((1, 100), (2, 120), (3, 100))
    ]
    invs = [inv("Hex bolt M8", 1, pr) for pr in (120, 100, 100)]
    assert [n for n, _ in pair(invs, po_lines)] == [2, 1, 3]


def test_different_wording_near_ties_stay_unpaired_even_beside_identical_ones():
    po_lines = [
        po(1, "Hex bolt M8 zinc", 1, 1),
        po(2, "Hex bolt M8 zinc", 1, 1),
        po(3, "Hex bolt M10 zinc", 1, 1),
    ]
    assert pair([inv("Hex bolt zinc", 1, 1)], po_lines) == [(None, None)]


def test_invoice_with_no_lines_against_a_po_with_lines_holds():
    r = match([], [po(1, "Grease", 1, 1)], {1: 1}, {1: 0}, vendor_kind="goods")
    assert r.signals == (UNREADABLE_LINE,)
    assert r.evidence == ("Invoice has no line items to match.",)


@pytest.mark.parametrize("kw", ["qty_tol", "price_tol_pct", "price_tol_abs"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_a_non_finite_tolerance_is_refused_not_silently_passed(kw, bad):
    # NaN makes every "over tolerance" comparison false, so the line passes.
    with pytest.raises(ValueError, match=kw):
        match(
            [inv("B", 5, 999)],
            [po(1, "B", 1, 1)],
            {1: 0},
            {1: 0},
            vendor_kind="goods",
            **{kw: bad},
        )


@pytest.mark.parametrize("n", [True, [1], "1", 1.0])
def test_only_a_whole_number_po_line_number_pairs_by_line_number(n):
    assert pair([inv("x", 1, 1, po_line_number=n)], [po(1, "B", 1, 1)]) == [
        (None, None)
    ]
