"""Planted receipt faults over real extractions: PO lines are rebuilt from the
benchmark ground truth for the 19 clean Company B PO invoices, then one fault
is planted per record (see "source" in the fixture). The assertions are the
score; `expected_signals` for an unplanted record is the observed result, which
records the known pairing gap (repeated descriptions are left unpaired by
design) rather than hiding it."""

import json
from pathlib import Path

from invoice_processing import three_way

FIXTURE = Path(__file__).parent / "fixtures" / "three_way" / "planted.json"
RECORDS = json.loads(FIXTURE.read_text(encoding="utf-8"))["records"]


def _match(rec: dict) -> three_way.ThreeWay:
    return three_way.match(
        rec["extracted"]["line_items"],
        rec["po"]["lines"],
        {int(k): v for k, v in rec["received"].items()},
        {int(k): v for k, v in rec["billed"].items()},
        vendor_kind=rec["vendor_kind"],
    )


def test_planted_faults_are_caught_and_nothing_else_is_added():
    clean = [r for r in RECORDS if r["fault"] is None]
    planted = [r for r in RECORDS if r["fault"] not in (None, "split_delivery")]
    controls = [r for r in RECORDS if r["fault"] == "split_delivery"]
    wrong = [
        r["invoice_id"]
        for r in RECORDS
        if sorted(_match(r).signals) != r["expected_signals"]
    ]
    missed = [
        r["invoice_id"] for r in planted if r["fault"] not in _match(r).signals
    ]
    lines = [x for r in clean for x in _match(r).lines]
    paired = sum(x.paired_by is not None for x in lines)
    clean_pass = sum(_match(r).passed for r in clean)
    summary = (
        f"clean passed {clean_pass}/{len(clean)}; faults caught "
        f"{len(planted) - len(missed)}/{len(planted)}; split controls "
        f"{len(controls)}; description pairing {paired}/{len(lines)}"
    )
    assert not wrong, summary
    assert not missed, summary
    assert (clean_pass, len(clean), paired, len(lines)) == PINNED, summary


# ponytail: pinned to today's measurement so a pairing change shows as a diff
# (clean passed, clean records, lines paired, clean lines)
PINNED = (1, 10, 5, 104)
