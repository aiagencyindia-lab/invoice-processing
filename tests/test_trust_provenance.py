"""Provenance v1: PDF facts, the ledger and score, and tamper after approval."""

from datetime import datetime, timezone

from invoice_processing.trust.provenance import pdfmeta

WORDS = " ".join(f"word{i}" for i in range(40))


def make_pdf(
    *,
    producer: str | None = "Tally",
    creator: str | None = "TallyPrime",
    created: str | None = "D:20250101100000+05'30'",
    modified: str | None = "D:20250101100000+05'30'",
    pages: int = 1,
    size: tuple = (595.28, 841.89),
    font: str = "Helvetica",
    text: str = WORDS,
) -> bytes:
    """A small, valid PDF built by hand: no writer library is a dependency."""
    info = {
        "Producer": producer,
        "Creator": creator,
        "CreationDate": created,
        "ModDate": modified,
    }
    info_body = "".join(f"/{k} ({v})" for k, v in info.items() if v is not None)
    lines = [text[i : i + 60] for i in range(0, len(text), 60)] if text else []
    stream = (
        "BT /F1 10 Tf 12 TL 40 780 Td "
        + " ".join(f"({line}) Tj T*" for line in lines)
        + " ET"
    )
    n_fixed = 4  # catalog, pages, font, info
    page_ids = [n_fixed + 1 + 2 * i for i in range(pages)]
    objs = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: "<< /Type /Pages /Kids ["
        + " ".join(f"{p} 0 R" for p in page_ids)
        + f"] /Count {pages} >>",
        3: f"<< /Type /Font /Subtype /Type1 /BaseFont /{font} /FontDescriptor"
        f" << /Type /FontDescriptor /FontName /{font} /Flags 32"
        " /FontBBox [0 0 1000 1000] /ItalicAngle 0 /Ascent 800 /Descent -200"
        " /CapHeight 700 /StemV 80 >> >>",
        4: f"<< {info_body} >>",
    }
    for p in page_ids:
        objs[p] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {size[0]} {size[1]}]"
            f" /Resources << /Font << /F1 3 0 R >> >> /Contents {p + 1} 0 R >>"
        )
        objs[p + 1] = (
            f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"
        )
    out = b"%PDF-1.4\n"
    offsets = {}
    for num in sorted(objs):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n{objs[num]}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for num in sorted(objs):
        out += f"{offsets[num]:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R /Info 4 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return out


# -- PDF facts ---------------------------------------------------------------


def test_facts_read_metadata_pages_fonts_and_text():
    facts = pdfmeta.facts(make_pdf(pages=2))
    assert facts["producer"] == "Tally" and facts["creator"] == "TallyPrime"
    assert facts["creation_date"] == "2025-01-01T10:00:00+05:30"
    assert facts["mod_date"] == "2025-01-01T10:00:00+05:30"
    assert facts["page_count"] == 2
    assert facts["page_sizes"] == [[595, 842]]
    assert facts["fonts"] == ["Helvetica"]
    assert facts["has_text"] is True


def test_no_text_layer_is_reported():
    assert pdfmeta.facts(make_pdf(text=""))["has_text"] is False


def test_subset_prefix_is_stripped_from_font_names():
    assert pdfmeta.facts(make_pdf(font="ABCDEF+Arial"))["fonts"] == ["Arial"]


def test_pdf_date_parses_offsets_and_partial_forms():
    utc = timezone.utc
    assert pdfmeta.pdf_date("D:20250102") == datetime(2025, 1, 2, tzinfo=utc)
    assert pdfmeta.pdf_date("D:20250102030405Z") == datetime(
        2025, 1, 2, 3, 4, 5, tzinfo=utc
    )
    assert pdfmeta.pdf_date("20250102030405-02'00'").isoformat() == (
        "2025-01-02T03:04:05-02:00"
    )
    assert pdfmeta.pdf_date("yesterday") is None
    assert pdfmeta.pdf_date(None) is None
    assert pdfmeta.pdf_date("D:20251399") is None


def test_fingerprint_ignores_page_count_and_text_but_not_fonts_or_producer():
    base = pdfmeta.facts(make_pdf())
    same = pdfmeta.facts(make_pdf(pages=3, text="other words entirely"))
    assert pdfmeta.fingerprint(base) == pdfmeta.fingerprint(same)
    for changed in (
        make_pdf(font="Courier"),
        make_pdf(producer="iLovePDF"),
        make_pdf(size=(612, 792)),
    ):
        assert pdfmeta.fingerprint(pdfmeta.facts(changed)) != (
            pdfmeta.fingerprint(base)
        )


def test_non_pdf_bytes_give_none():
    assert pdfmeta.facts(b"\x89PNG\r\n\x1a\n...") is None
    assert pdfmeta.facts(b"%PDF-1.4 truncated garbage") is None
