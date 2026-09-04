"""The package a person uses to check the machine's answer by hand.

    python -m benchmark.harness.manual_sample baseline after

Writes benchmark/manual-verification/. The Manual result column is left empty
on purpose: filling it in is the whole point, and a pre-filled verdict would
make the sheet a record of what the benchmark already believed.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys

from . import dataset

OUT = dataset.REPO / "benchmark" / "manual-verification"

# One scenario per row of the sample: the fifteen things a person should see
# with their own eyes. `None` means a fully clean invoice.
SCENARIOS: list[tuple[str, str | None]] = [
    ("clean invoice, nothing wrong", None),
    ("not an invoice at all", "1.1"),
    ("addressed to a different company", "1.2"),
    ("not a tax invoice", "1.3"),
    ("work authorization missing or invalid", "1.4"),
    ("several invoices in one file", "1.5"),
    ("charges not itemized", "2.1"),
    ("purchase order wrong", "2.2"),
    ("already processed", "3.1"),
    ("vendor tax id invalid", "3.2"),
    ("dated in the future", "3.3"),
    ("stated total disagrees with its parts", "4.1"),
    ("line amounts do not reconcile", "4.2"),
    ("hours or rate outside the authorization", "4.3"),
    ("tax amount incorrect", "4.4"),
]


def load_rows(run: str):
    path = dataset.REPO / "benchmark" / "runs" / run / "results.json"
    if not path.exists():
        raise SystemExit(f"no scored run at {path}")
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return {r["test_id"]: r for r in data["rows"]}, data["documents"]


def pick(scenario_check: str | None) -> list[str]:
    """One invoice per company for this scenario, chosen by ground truth only."""
    chosen: list[str] = []
    for company in dataset.COMPANIES:
        for row in dataset.master().values():
            if row["company_id"] != company:
                continue
            first = row["first_failing_check"] or None
            if scenario_check == "4.1":
                # 4.1 never rejects, so it is never a first failing check:
                # pick an invoice whose stated total is deliberately wrong.
                if "4.1" not in (row["fault_checks"] or "").split("|"):
                    continue
            elif first != scenario_check:
                continue
            if row["invoice_id"] not in chosen:
                chosen.append(row["invoice_id"])
                break
    return chosen


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("before")
    p.add_argument("after")
    args = p.parse_args(argv)
    before_rows, before_docs = load_rows(args.before)
    after_rows, after_docs = load_rows(args.after)
    OUT.mkdir(parents=True, exist_ok=True)

    matrix_rows = []
    test_no = 0
    for scenario, check_id in SCENARIOS:
        for invoice_id in pick(check_id):
            test_no += 1
            m = dataset.master()[invoice_id]
            first = m["first_failing_check"] or ""
            b_doc = before_docs.get(invoice_id, {})
            a_doc = after_docs.get(invoice_id, {})
            scored_check = check_id or "1.1"
            b_row = before_rows.get(f"{invoice_id}:{scored_check}", {})
            a_row = after_rows.get(f"{invoice_id}:{scored_check}", {})
            before_seen = (
                f"{b_doc.get('final_decision', 'n/a')} / "
                f"{b_doc.get('rejection_template') or ''}"
            ).strip(" /")
            after_seen = (
                f"{a_doc.get('final_decision', 'n/a')} / "
                f"{a_doc.get('rejection_template') or ''}"
            ).strip(" /")
            matrix_rows.append(
                {
                    "Test ID": f"MV-{test_no:03d}",
                    "Invoice ID": invoice_id,
                    "Company": dataset.company(m["company_id"])["legal_name"],
                    "Check": (
                        f"{check_id} {dataset.CHECK_NAME[check_id]}"
                        if check_id
                        else "all checks pass"
                    ),
                    "Scenario": scenario,
                    "File to upload": m["document_file"],
                    "Shown to the reviewer as": m["submission_filename"],
                    "Expected behavior": m["expected_final_decision"],
                    "Expected rejection": m["expected_rejection_reason"] or "",
                    "Expected first failing check": first,
                    "Before result": before_seen,
                    "Before check verdict": b_row.get("actual_result", "n/a"),
                    "After result": after_seen,
                    "After check verdict": a_row.get("actual_result", "n/a"),
                    "Manual result": "",
                    "Notes": "",
                }
            )

    with open(OUT / "test-matrix.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(matrix_rows[0].keys()))
        writer.writeheader()
        writer.writerows(matrix_rows)

    sampled = {r["Invoice ID"] for r in matrix_rows}
    with open(
        OUT / "ground-truth.csv", "w", newline="", encoding="utf-8"
    ) as fh:
        cols = [
            "invoice_id",
            "company_id",
            "check_id",
            "check_name",
            "expected_result",
            "expected_system_action",
            "expected_rejection_reason",
            "fault_code",
            "is_first_failing_check",
        ]
        writer = csv.DictWriter(fh, fieldnames=cols)
        writer.writeheader()
        for (invoice_id, _check_id), row in sorted(dataset.matrix().items()):
            if invoice_id in sampled:
                writer.writerow({c: row.get(c, "") for c in cols})

    for run, name in (
        (args.before, "before-results.csv"),
        (args.after, "after-results.csv"),
    ):
        source = dataset.REPO / "benchmark" / "runs" / run / "results.csv"
        (OUT / name).write_text(
            source.read_text(encoding="utf-8"), encoding="utf-8"
        )

    comparison = (
        dataset.REPO / "benchmark" / "comparison" / "before-vs-after.csv"
    )
    if comparison.exists():
        (OUT / "before-vs-after.csv").write_text(
            comparison.read_text(encoding="utf-8"), encoding="utf-8"
        )

    print(f"wrote {len(matrix_rows)} manual tests to {OUT / 'test-matrix.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
