"""Provenance v2: forensic families, the v2 score, PROV-EDITED, KYC and proofs."""

import pytest

from benchmark.history.faults.provenance import make_pdf
from invoice_processing.trust.provenance import forensics, pdfmeta

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


# -- forensic families -------------------------------------------------------


def _facts(**kw):
    base = {
        "producer": "Tally",
        "creation_date": "2025-01-01T10:00:00+05:30",
        "mod_date": "2025-01-01T10:00:00+05:30",
        "incremental_updates": 0,
        "font_names": {"body": ["Helvetica"], "amounts": ["Helvetica"]},
    }
    return {**base, **kw}


@pytest.mark.parametrize(
    "producer,prior,value",
    [
        ("iLovePDF", [], 0.3),
        ("PDF-XChange Editor 9.5", [], 0.3),
        ("Canva", [], 0.3),
        ("Skia/PDF m120", [], 0.8),
        ("wkhtmltopdf 0.12.6", [], 0.8),
        ("Tally", [], 0.8),
        (None, [], 0.6),
        ("Some Unknown Writer", [], 0.6),
        ("Canva", ["canva "], 1.0),  # the vendor always uses it
        ("iLovePDF", ["Tally", None], 0.3),
        ("Tally", ["TALLY"], 1.0),
    ],
)
def test_producer_family(producer, prior, value):
    assert forensics.producer(_facts(producer=producer), prior) == value


@pytest.mark.parametrize(
    "kw,saves,value",
    [
        ({}, 0, 1.0),
        ({"incremental_updates": 1}, 1, 0.7),
        ({"incremental_updates": 2}, 2, 0.2),
        ({"incremental_updates": 5}, 5, 0.2),
        # a full rewrite on a later day counts as one save
        ({"mod_date": "2025-01-03T10:00:00+05:30"}, 1, 0.7),
        ({"mod_date": "2025-01-01T18:00:00+05:30"}, 0, 1.0),
        ({"mod_date": None}, 0, 1.0),
        ({"creation_date": None, "mod_date": "2025-03-01T00:00:00"}, 0, 1.0),
    ],
)
def test_edits_family(kw, saves, value):
    f = _facts(**kw)
    assert forensics.saves(f) == saves
    assert forensics.edits(f, 2) == value


def test_edits_threshold_is_the_tenants():
    assert forensics.edits(_facts(incremental_updates=2), 3) == 0.7


@pytest.mark.parametrize(
    "names,value",
    [
        ({"body": ["ABCDEF+Arial"], "amounts": ["GHIJKL+Arial"]}, 0.3),
        ({"body": ["ABCDEF+Arial"], "amounts": ["ABCDEF+Arial"]}, 1.0),
        # bold totals: another font, not another subset of the same one
        ({"body": ["ABCDEF+Arial"], "amounts": ["GHIJKL+Arial-Bold"]}, 1.0),
        ({"body": [], "amounts": ["ABCDEF+Arial", "GHIJKL+Arial"]}, 0.3),
        ({"body": ["Arial"], "amounts": ["Arial"]}, 1.0),
        ({"body": ["ABCDEF+Arial", "GHIJKL+Arial"], "amounts": []}, 1.0),
        (None, 1.0),  # a v1 facts dict
    ],
)
def test_fonts_family(names, value):
    assert forensics.fonts(_facts(font_names=names)) == value


def test_fonts_is_zero_without_a_text_layer():
    assert forensics.fonts(_facts(has_text=False)) == 0.0
