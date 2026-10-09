"""One real extraction run over a generated scan set, scored against its truth.

The fixture holds ground truth and `supporting_docs.extract` output captured
once with Gemini (see its "source"); the PDFs are not committed. This scores
the recorded run, so it never calls a model.
"""

import json
from pathlib import Path

from invoice_processing.supporting_docs import GOODS_RECEIPT, pair_grn_lines

FIXTURE = (
    Path(__file__).parent / "fixtures" / "supporting_docs" / "measured.json"
)
RECORDS = json.loads(FIXTURE.read_text())["records"]

# truth key -> extracted field key
HEADER = {
    GOODS_RECEIPT: {
        "grn_number": "grn_number",
        "po_number": "po_number",
        "received_on": "received_on",
    },
    "purchase_order": {
        "po_number": "po_number",
        "vendor_name": "vendor_name",
        "total": "total_amount",
    },
}
QTY = {GOODS_RECEIPT: "quantity_received", "purchase_order": "quantity"}


def _same(truth, got) -> bool:
    if isinstance(truth, float):
        return got is not None and abs(float(got) - truth) < 0.005
    return " ".join(str(got or "").split()).casefold() == truth.casefold()


def _flagged(rec, key, got) -> bool:
    # a blank is as visible to the reviewer as a listed field
    review = rec["extracted"]["_evidence"]["_summary"]["fields_needing_review"]
    return got is None or key in review


def score(records):
    header = {GOODS_RECEIPT: [0, 0], "purchase_order": [0, 0]}
    qty, pairing, flagged = [0, 0], [0, 0], [0, 0]
    wrong = []
    for rec in records:
        kind, truth, ext = rec["kind"], rec["truth"], rec["extracted"]
        checks = [
            (t, ext["fields"].get(e), truth[t], e)
            for t, e in HEADER[kind].items()
        ]
        lines = ext["lines"]
        for i, tl in enumerate(truth["lines"]):
            got = lines[i].get(QTY[kind]) if i < len(lines) else None
            checks.append(
                (f"lines[{i}]", got, tl[QTY[kind]], f"lines[{i}].{QTY[kind]}")
            )
        for name, got, want, key in checks:
            ok = _same(want, got)
            bucket = qty if name.startswith("lines[") else header[kind]
            bucket[0] += ok
            bucket[1] += 1
            if not ok:
                wrong.append((rec["file"], name))
                flagged[0] += _flagged(rec, key, got)
                flagged[1] += 1
        if kind == GOODS_RECEIPT:
            paired = pair_grn_lines(lines, truth["po_lines"])
            for i, tl in enumerate(truth["lines"]):
                pairing[0] += (
                    i < len(paired) and paired[i] == tl["po_line_number"]
                )
                pairing[1] += 1
    figures = tuple(
        tuple(x)
        for x in (
            header[GOODS_RECEIPT],
            header["purchase_order"],
            qty,
            pairing,
            flagged,
        )
    )
    return figures, sorted(wrong)


# (correct, total) for: GRN header fields, PO header fields, line quantities,
# GRN lines paired to the right PO line, wrong values flagged for review.
# ponytail: pinned to this one run of 26 documents, not a gate; turn these into
# floors once the scan set grows enough for a rate to mean something.
PINNED = ((56, 60), (17, 18), (34, 34), (28, 28), (3, 5))
# GRN-03/09/12/15 read the RECEIVED stamp's date (OCR grounds 03 and 09, so
# they go unflagged); SVC-INV-089 prints no total row and the total is blank.
PINNED_WRONG = [
    ("GRN-03.pdf", "received_on"),
    ("GRN-09.pdf", "received_on"),
    ("GRN-12.pdf", "received_on"),
    ("GRN-15.pdf", "received_on"),
    ("SVC-INV-089.pdf", "total"),
]


def test_fixture_is_the_full_scan_set():
    kinds = [r["kind"] for r in RECORDS]
    assert kinds.count(GOODS_RECEIPT) == 20
    assert kinds.count("purchase_order") == 6
    assert all("error" not in r["extracted"] for r in RECORDS)


def test_measured_figures_are_pinned():
    assert score(RECORDS) == (PINNED, PINNED_WRONG)
