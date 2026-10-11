"""GL auto-coding: which rule codes a line, and how the projection shows it."""

from typing import ClassVar

from invoice_processing import approval_flow, case_projection
from invoice_processing.gl_coding import code_lines, po_accounts_from_match

INVOICE = {"vendor_name": "Acme Supplies Pty Ltd"}
LINES = [
    {"line_number": 1, "description": "Printer PAPER A4"},
    {"line_number": 2, "description": "Courier delivery"},
]


def rule(id, gl, vendor=None, keyword=None, created="2026-01-01", enabled=True):
    return {
        "id": id,
        "vendor_name": vendor,
        "keyword": keyword,
        "gl_account": gl,
        "enabled": enabled,
        "created_by": "a@x.com",
        "created_at": created,
    }


def coded(artifact):
    return {
        line["line_number"]: (line["gl_account"], line["rule_id"])
        for line in artifact["lines"]
    }


def test_vendor_and_keyword_beats_keyword_beats_vendor():
    rules = [
        rule("v", "100", vendor="ACME SUPPLIES"),
        rule("k", "200", keyword="paper"),
        rule("vk", "300", vendor="acme supplies pty ltd", keyword="paper"),
    ]
    assert coded(code_lines(INVOICE, LINES, rules)) == {
        1: ("300", "vk"),
        2: ("100", "v"),
    }
    assert coded(code_lines(INVOICE, LINES, rules[:2]))[1] == ("200", "k")


def test_tie_goes_to_earliest_created_then_input_order():
    rules = [
        rule("late", "1", keyword="paper", created="2026-02-01"),
        rule("early", "2", keyword="paper", created="2026-01-01"),
        rule("early2", "3", keyword="paper", created="2026-01-01"),
    ]
    assert coded(code_lines(INVOICE, LINES, rules))[1] == ("2", "early")


def test_disabled_and_malformed_rules_are_skipped():
    rules = [
        rule("off", "1", keyword="paper", enabled=False),
        rule("blank", "2"),
        {"id": "nogl", "keyword": "paper", "gl_account": ""},
        "junk",
        {"keyword": "paper", "gl_account": "9"},
        rule("ok", "4", keyword="paper", created="2026-09-01"),
    ]
    assert coded(code_lines(INVOICE, LINES, rules))[1] == ("4", "ok")


def test_unmatched_line_is_uncoded():
    line = code_lines(INVOICE, LINES, [])["lines"][0]
    assert line == {
        "line_number": 1,
        "gl_account": None,
        "source": "none",
        "rule_id": None,
        "coded_by": None,
        "coded_at": None,
    }


def test_rule_line_shape():
    line = code_lines(INVOICE, LINES, [rule("k", "200", keyword="paper")])[
        "lines"
    ][0]
    assert line["source"] == "rule"
    assert line["coded_by"] is None and line["coded_at"] is None


def test_manual_lines_survive_a_rerun():
    manual = {
        "line_number": 1,
        "gl_account": "999",
        "source": "manual",
        "rule_id": None,
        "coded_by": "p@x.com",
        "coded_at": "2026-10-01T00:00:00+00:00",
    }
    previous = {
        "lines": [manual, {**manual, "line_number": 2, "source": "rule"}]
    }
    out = code_lines(INVOICE, LINES, [rule("k", "200", keyword="o")], previous)
    assert out["lines"][0] == manual
    assert out["lines"][1]["gl_account"] == "200"


def test_empty_vendor_never_matches_a_vendor_rule():
    rules = [rule("v", "100", vendor="Ltd")]
    for vendor in (None, "", "  "):
        assert (
            coded(code_lines({"vendor_name": vendor}, LINES, rules))[1][0]
            is None
        )


def test_no_lines():
    assert code_lines(INVOICE, [], [rule("k", "1", keyword="x")]) == {
        "lines": []
    }


class TestProjection:
    ARTIFACTS: ClassVar[dict] = {
        "transformer": {
            "line_items_mapped": [
                {"line_number": 1, "description": "a", "gl_account": "T1"},
                {"line_number": 2, "description": "b", "gl_account": "T2"},
            ]
        }
    }

    def test_coded_account_overrides_transformer(self):
        artifacts = {
            **self.ARTIFACTS,
            "gl_coding": {
                "lines": [
                    {"line_number": 1, "gl_account": "6" * 100},
                    {"line_number": 2, "gl_account": None},
                ]
            },
        }
        got = [line["gl_account"] for line in case_projection.lines(artifacts)]
        assert got == ["6" * 64, "T2"]

    def test_malformed_coding_is_ignored(self):
        for bad in ("x", {"lines": "x"}, {"lines": ["x", {"gl_account": "9"}]}):
            artifacts = {**self.ARTIFACTS, "gl_coding": bad}
            got = [
                line["gl_account"] for line in case_projection.lines(artifacts)
            ]
            assert got == ["T1", "T2"]

    def test_only_integer_line_numbers_code_a_line(self):
        bad = {
            "lines": [
                {"line_number": [1], "gl_account": "9"},
                {"line_number": True, "gl_account": "9"},
            ]
        }
        artifacts = {**self.ARTIFACTS, "gl_coding": bad}
        got = [line["gl_account"] for line in case_projection.lines(artifacts)]
        assert got == ["T1", "T2"]


def test_previous_entries_without_an_integer_line_number_are_ignored():
    manual = {"source": "manual", "gl_account": "9"}
    previous = {
        "lines": [
            {**manual, "line_number": [1]},
            {**manual, "line_number": True},
        ]
    }
    assert coded(code_lines(INVOICE, LINES, [], previous)) == {
        1: (None, None),
        2: (None, None),
    }


def test_routing_ignores_a_malformed_coding():
    for bad in (
        "x",
        ["x"],
        {"lines": "x"},
        {"lines": ["x", {"gl_account": 1}]},
    ):
        invoice = approval_flow.routing_invoice({"gl_coding": bad})
        assert invoice["_gl_accounts"] == []


def test_a_po_line_account_beats_a_matching_rule():
    rules = [rule("k", "200", keyword="paper")]
    artifact = code_lines(INVOICE, LINES, rules, po_accounts={1: (7, "6100")})
    assert artifact["lines"][0] == {
        "line_number": 1,
        "gl_account": "6100",
        "source": "po",
        "rule_id": None,
        "po_line_number": 7,
        "coded_by": None,
        "coded_at": None,
    }
    # Line 2 has no PO account and no matching rule: uncoded as before.
    assert artifact["lines"][1]["source"] == "none"


def test_a_rule_still_codes_a_line_the_po_leaves_blank():
    rules = [rule("v", "100", vendor="Acme Supplies Pty Ltd")]
    artifact = code_lines(INVOICE, LINES, rules, po_accounts={1: (1, "6100")})
    assert coded(artifact) == {1: ("6100", None), 2: ("100", "v")}


def test_a_person_s_coding_beats_the_po():
    manual = {
        "line_number": 1,
        "gl_account": "5000",
        "source": "manual",
        "rule_id": None,
        "coded_by": "c@x.test",
        "coded_at": "2026-01-02T00:00:00",
    }
    artifact = code_lines(
        INVOICE,
        LINES,
        [],
        previous={"lines": [manual]},
        po_accounts={1: (1, "6100")},
    )
    assert artifact["lines"][0] == manual


THREE_WAY = [
    {"line_number": 1, "po_line_number": 1, "paired_by": "description"},
    {"line_number": 2, "po_line_number": 2, "paired_by": "item_code"},
]
BOOK = [
    {"line_number": 1, "gl_account": "6100"},
    {"line_number": 2, "gl_account": "  "},
]


def test_po_accounts_follow_the_pairing_and_skip_blank_accounts():
    assert po_accounts_from_match(THREE_WAY, BOOK, [1, 2]) == {1: (1, "6100")}


def test_a_book_row_with_a_bad_line_number_is_ignored():
    rows = [{"line_number": [1], "gl_account": "6100"}, BOOK[0]]
    assert po_accounts_from_match(THREE_WAY, rows, [1, 2]) == {1: (1, "6100")}


def test_bool_and_float_po_line_numbers_are_skipped():
    odd = [
        {"line_number": 1, "po_line_number": True},
        {"line_number": 2, "po_line_number": 1.0},
    ]
    assert po_accounts_from_match(odd, BOOK, [1, 2]) == {}


def test_two_invoice_lines_may_share_a_po_line():
    shared = [
        {"line_number": 1, "po_line_number": 1},
        {"line_number": 2, "po_line_number": 1},
    ]
    assert po_accounts_from_match(shared, BOOK, [1, 2]) == {
        1: (1, "6100"),
        2: (1, "6100"),
    }


def test_an_unpaired_line_falls_to_rules():
    unpaired = [{**THREE_WAY[0], "po_line_number": None, "paired_by": None}]
    assert po_accounts_from_match(unpaired, BOOK, [1]) == {}


def test_pairs_by_position_not_by_number():
    """The transformer may number lines 10, 20; step 2.3 numbers them 1, 2."""
    assert po_accounts_from_match(THREE_WAY, BOOK, [10, 20]) == {
        10: (1, "6100")
    }


def test_a_count_mismatch_yields_nothing():
    """Positions only line up when the counts do; otherwise do not guess."""
    assert po_accounts_from_match(THREE_WAY, BOOK, [1]) == {}
    assert po_accounts_from_match([], BOOK, [1, 2]) == {}


def test_malformed_pairing_rows_are_skipped():
    weird = [{"line_number": 1, "po_line_number": "1"}, "not a dict"]
    assert po_accounts_from_match(weird, BOOK, [1, 2]) == {}
