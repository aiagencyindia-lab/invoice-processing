"""Print-styled HTML for the report and the evidence pack.

One assembler feeds a JSON export and this page; the browser's print dialog
makes the PDF. Every value passes through `html.escape`: these pages render
vendor names, clause excerpts and email fragments.
"""

from __future__ import annotations

from html import escape

_CSS = """
body { font: 13px/1.45 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
       color: #111; margin: 32px auto; max-width: 960px; padding: 0 16px; }
h1 { font-size: 22px; margin: 0 0 4px; } h2 { font-size: 16px; margin: 28px 0 8px;
     border-bottom: 1px solid #ddd; padding-bottom: 4px; page-break-after: avoid; }
table { border-collapse: collapse; width: 100%; margin: 8px 0 16px; page-break-inside: auto; }
th, td { text-align: left; padding: 4px 8px; border-bottom: 1px solid #eee; vertical-align: top; }
th { font-weight: 600; background: #f6f6f6; } tr { page-break-inside: avoid; }
dl.kv { display: grid; grid-template-columns: max-content 1fr; gap: 2px 16px; margin: 8px 0; }
dl.kv dt { color: #666; } dl.kv dd { margin: 0; }
.muted { color: #666; } .toolbar { float: right; }
@media print { .toolbar { display: none; } body { margin: 0; max-width: none; } }
"""


def page(title: str, body: str) -> str:
    """A complete page; `body` is HTML already built by the helpers here."""
    return (
        '<!doctype html>\n<html><head><meta charset="utf-8">'
        f"<title>{escape(title)}</title><style>{_CSS}</style></head><body>"
        '<div class="toolbar"><button onclick="window.print()">Print</button></div>'
        f"<h1>{escape(title)}</h1>{body}</body></html>"
    )


def table(columns: list[str], rows: list[list]) -> str:
    head = "".join(f"<th>{escape(str(c))}</th>" for c in columns)
    body = "".join(
        "<tr>" + "".join(f"<td>{escape(_text(v))}</td>" for v in row) + "</tr>"
        for row in rows
    )
    return f"<table><tr>{head}</tr>{body}</table>"


def kv(pairs: list[tuple[str, object]]) -> str:
    items = "".join(
        f"<dt>{escape(str(k))}</dt><dd>{escape(_text(v))}</dd>"
        for k, v in pairs
    )
    return f'<dl class="kv">{items}</dl>'


def section(title: str, body: str) -> str:
    return f"<h2>{escape(title)}</h2>{body}"


def _text(value) -> str:
    return "" if value is None else str(value)
