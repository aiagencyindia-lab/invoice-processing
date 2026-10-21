"""Provenance v2 signal families (FR-22.1): deterministic reads of a PDF's
facts, each a value in [0, 1] where 1 is "nothing unusual". No model."""

from __future__ import annotations

from datetime import datetime, timedelta

# Lower-case substrings of /Producer or /Creator. An editor rewrites a
# document someone else generated; a renderer generates one from data.
# ponytail: substring lists; add a name when a tenant's documents show one.
EDITORS = (
    "pdf-xchange",
    "ilovepdf",
    "sejda",
    "smallpdf",
    "canva",
    "foxit phantompdf",
    "foxit pdf editor",
    "nitro",
    "pdfescape",
    "pdffiller",
    "dochub",
    "soda pdf",
    "pdf candy",
)
RENDERERS = (
    "wkhtmltopdf",
    "chromium",
    "skia",
    "headless chrome",
    "reportlab",
    "prince",
    "itext",
    "tcpdf",
    "fpdf",
    "dompdf",
    "mpdf",
    "weasyprint",
    "pdfsharp",
    "aspose",
    "jasperreports",
    "crystal reports",
    "acrobat distiller",
    "adobe pdf library",
    "ghostscript",
    "quartz pdfcontext",
    "microsoft: print to pdf",
    "microsoft® word",
    "libreoffice",
    "openoffice",
    "tally",
    "zoho",
    "quickbooks",
    "sap ",
)
EDITOR, RENDERER, EMPTY, UNKNOWN, MATCHES_HISTORY = 0.3, 0.8, 0.6, 0.6, 1.0
# A full rewrite (no incremental section) on a later day is still a save.
RESAVE_GAP = timedelta(days=1)


def _norm(value) -> str:
    return " ".join(str(value or "").lower().split())


def producer(facts: dict, prior_producers) -> float:
    """The generator's fingerprint. Matching the vendor's own earlier
    documents wins: a vendor that always invoices from Canva is consistent;
    a renderer-made invoice re-saved in an editor is not."""
    name = _norm(facts.get("producer"))
    if not name:
        return EMPTY
    if name in {_norm(p) for p in prior_producers if p}:
        return MATCHES_HISTORY
    # An editor often names itself in /Creator over a generic library's
    # /Producer ("Canva" over "Skia/PDF").
    both = f"{name} {_norm(facts.get('creator'))}"
    if any(e in both for e in EDITORS):
        return EDITOR
    if any(r in both for r in RENDERERS):
        return RENDERER
    return UNKNOWN


def saves(facts: dict) -> int:
    """Incremental saves, or one when the file was rewritten whole on a
    later day than it was created."""
    count = facts.get("incremental_updates") or 0
    if count:
        return count
    created, modified = facts.get("creation_date"), facts.get("mod_date")
    if created and modified:
        gap = datetime.fromisoformat(modified) - datetime.fromisoformat(created)
        if gap >= RESAVE_GAP:
            return 1
    return 0


def edits(facts: dict, suspect_at: int) -> float:
    n = saves(facts)
    if n >= suspect_at:
        return 0.2
    return 0.7 if n else 1.0


def _base(name: str) -> tuple[str | None, str]:
    """("ABCDEF", "Arial") for a subset name, (None, name) otherwise."""
    if len(name) > 7 and name[6] == "+" and name[:6].isupper():
        return name[:6], name[7:]
    return None, name


def fonts(facts: dict) -> float:
    """Mixed subsets on amount lines: the same base font embedded under two
    subset prefixes, one of them on a line holding an amount, is what an
    editor leaves when it re-types digits. A bold totals font is another
    base font and does not count. Without a text layer there is nothing to
    read, which scores as v1's `text` does."""
    if facts.get("has_text") is False:
        return 0.0
    names = facts.get("font_names") or {}
    prefixes: dict[str, set] = {}
    for name in [*names.get("body", ()), *names.get("amounts", ())]:
        prefix, base = _base(name)
        if prefix:
            prefixes.setdefault(base, set()).add(prefix)
    for name in names.get("amounts", ()):
        prefix, base = _base(name)
        if prefix and len(prefixes[base]) > 1:
            return 0.3
    return 1.0
