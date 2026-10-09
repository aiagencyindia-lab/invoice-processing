"""Planted receipt faults over real extractions: PO lines are rebuilt from the
benchmark ground truth for the 19 Company B PO invoices (10 clean, 9 with one
planted fault; see "source" in the fixture). The assertions are the
score; `expected_signals` for an unplanted record is the observed result, which
records the known gaps rather than hiding them: MFG-INV-033 (two lines
extracted without a price or quantity) and MFG-INV-035 (no extracted lines)
both hold as unreadable_line."""

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
    got = {r["invoice_id"]: _match(r) for r in RECORDS}
    wrong = [
        r["invoice_id"]
        for r in RECORDS
        if sorted(got[r["invoice_id"]].signals) != r["expected_signals"]
    ]
    missed = [
        r["invoice_id"]
        for r in planted
        if r["fault"] not in got[r["invoice_id"]].signals
    ]
    lines = [x for r in clean for x in got[r["invoice_id"]].lines]
    paired = sum(x.paired_by == "description" for x in lines)
    clean_pass = sum(got[r["invoice_id"]].passed for r in clean)
    summary = (
        f"clean passed {clean_pass}/{len(clean)}; faults caught "
        f"{len(planted) - len(missed)}/{len(planted)}; split controls "
        f"{len(controls)}; description pairing {paired}/{len(lines)}"
    )
    assert not wrong, summary
    assert not missed, summary
    assert (clean_pass, len(clean), paired, len(lines)) == PINNED, summary


# ponytail: pinned to today's measurement so a pairing change shows as a diff;
# 10 clean records is a ceiling -- re-pin by hand, or make these floors, once
# the fixture grows.
# (clean passed, clean records, lines paired, clean lines)
PINNED = (8, 10, 104, 104)
