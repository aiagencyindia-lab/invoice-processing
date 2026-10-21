"""Provenance v2: forensic families, the v2 score, PROV-EDITED, KYC and proofs."""

import pytest

from benchmark.history.faults.provenance import make_pdf
from invoice_processing.trust.provenance import pdfmeta

# -- PDF facts: incremental saves and fonts ----------------------------------


def test_a_fresh_file_has_no_incremental_updates():
    assert pdfmeta.facts(make_pdf())["incremental_updates"] == 0


def test_each_incremental_save_is_counted_and_the_last_info_wins():
    data = make_pdf(saves=["D:20250105", "D:20250210"])
    facts = pdfmeta.facts(data)
    assert facts["incremental_updates"] == 2
    assert facts["mod_date"] == "2025-02-10T00:00:00+00:00"
    assert facts["has_text"] is True


@pytest.mark.parametrize(
    "data,count",
    [
        (b"%PDF-1.4\nstartxref\n9\n%%EOF\n", 0),
        (b"%PDF-1.4\nstartxref\n9\n%%EOF\nstartxref\n20\n%%EOF\n", 1),
        # a linearised file carries a first-page startxref of its own
        (
            b"%PDF-1.4\n1 0 obj << /Linearized 1 >> endobj\n"
            b"startxref\n0\n%%EOF\nstartxref\n20\n%%EOF\n",
            0,
        ),
        (b"%PDF-1.4\nno trailer at all", 0),
    ],
)
def test_incremental_updates_from_bytes(data, count):
    assert pdfmeta.incremental_updates(data) == count


def test_font_names_split_amount_lines_from_the_body():
    facts = pdfmeta.facts(
        make_pdf(
            font="ABCDEF+Helvetica",
            amount_line="Total 1,180.00",
            amount_font="GHIJKL+Helvetica",
        )
    )
    assert facts["font_names"] == {
        "body": ["ABCDEF+Helvetica"],
        "amounts": ["GHIJKL+Helvetica"],
    }
    # the template fingerprint still sees one base font
    assert facts["fonts"] == ["Helvetica"]


def test_an_amount_line_in_the_body_font_is_not_mixed():
    facts = pdfmeta.facts(
        make_pdf(font="ABCDEF+Helvetica", amount_line="Total 1,180.00")
    )
    assert facts["font_names"]["amounts"] == ["ABCDEF+Helvetica"]
