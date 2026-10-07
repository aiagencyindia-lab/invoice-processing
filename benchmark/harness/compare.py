"""BEFORE vs AFTER, joined on test id.

    python -m benchmark.harness.compare baseline after

Writes benchmark/comparison/{before-vs-after.csv, flips.csv, report.md}. Reads
the two scored runs and nothing else: the ground truth, the test matrix and the
scoring rules are identical by construction, so every difference in this report
is the application.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter

from . import dataset

COMPARISON = dataset.REPO / "benchmark" / "comparison"


def load(run: str) -> dict:
    path = dataset.REPO / "benchmark" / "runs" / run / "results.json"
    if not path.exists():
        raise SystemExit(f"no scored run at {path}; run the scorer first")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _pct(v) -> str:
    return "-" if v is None else f"{v * 100:.1f}%"


def _delta(before, after) -> str:
    if before is None or after is None:
        return "-"
    return f"{(after - before) * 100:+.1f}pt"


def check_table(before: dict, after: dict, population: str) -> list[list]:
    rows = []
    for check_id, name in dataset.CHECKS:
        b = before["per_check"][population][check_id]
        a = after["per_check"][population][check_id]
        rows.append(
            [
                check_id,
                name,
                b["n"],
                a["n"],
                _pct(b["accuracy"]),
                _pct(a["accuracy"]),
                _delta(b["accuracy"], a["accuracy"]),
                _pct(b["recall"]),
                _pct(a["recall"]),
                _pct(b["precision"]),
                _pct(a["precision"]),
                f"{b['fp']}→{a['fp']}",
                f"{b['fn']}→{a['fn']}",
            ]
        )
    return rows


def flips(before: dict, after: dict) -> list[dict]:
    b_rows = {r["test_id"]: r for r in before["rows"]}
    out = []
    for r in after["rows"]:
        b = b_rows.get(r["test_id"])
        if b is None or b["passed"] == r["passed"]:
            continue
        if b["passed"] is True and r["passed"] is False:
            kind = "regressed"
        elif b["passed"] is False and r["passed"] is True:
            kind = "fixed"
        elif b["passed"] is False and r["passed"] is None:
            kind = "no-longer-scored"
        elif b["passed"] is None and r["passed"] is False:
            kind = "newly-failing"
        else:
            kind = "newly-scored"
        out.append(
            {
                "kind": kind,
                "test_id": r["test_id"],
                "invoice_id": r["invoice_id"],
                "company_id": r["company_id"],
                "check_id": r["check_id"],
                "expected_result": r["expected_result"],
                "before_result": b["actual_result"],
                "after_result": r["actual_result"],
                "before_evidence": b["validation_details"],
                "after_evidence": r["validation_details"],
            }
        )
    order = {
        "regressed": 0,
        "fixed": 1,
        "newly-failing": 2,
        "newly-scored": 3,
        "no-longer-scored": 4,
    }
    return sorted(
        out,
        key=lambda f: (order.get(f["kind"], 9), f["check_id"], f["invoice_id"]),
    )


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("before")
    p.add_argument("after")
    args = p.parse_args(argv)
    before, after = load(args.before), load(args.after)
    COMPARISON.mkdir(parents=True, exist_ok=True)

    header = [
        "check_id",
        "check_name",
        "n_before",
        "n_after",
        "accuracy_before",
        "accuracy_after",
        "accuracy_change",
        "recall_before",
        "recall_after",
        "precision_before",
        "precision_after",
        "false_positives",
        "false_negatives",
    ]
    cascade = check_table(before, after, "cascade_full")
    with open(
        COMPARISON / "before-vs-after.csv", "w", newline="", encoding="utf-8"
    ) as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(cascade)

    flipped = flips(before, after)
    with open(
        COMPARISON / "flips.csv", "w", newline="", encoding="utf-8"
    ) as fh:
        w = csv.DictWriter(
            fh, fieldnames=list(flipped[0].keys()) if flipped else ["kind"]
        )
        w.writeheader()
        w.writerows(flipped)

    kinds = Counter(f["kind"] for f in flipped)
    bd, ad = before["decision"], after["decision"]
    bx, ax = before["extraction"], after["extraction"]
    bt, at = before["timing"]["all"], after["timing"]["all"]

    lines = [
        f"# BEFORE (`{args.before}`) vs AFTER (`{args.after}`)",
        "",
        f"`{args.before}` at `{before['meta']['git_sha']}`, {before['meta']['records']} submissions; "
        f"`{args.after}` at `{after['meta']['git_sha']}`, {after['meta']['records']} submissions. "
        "Same documents, same order, same ground truth, same scoring code.",
        "",
        "## Per check (cascade-aware, full population)",
        "",
        "| Check | n before→after | Accuracy before | after | change | Recall b→a | Precision b→a | FP b→a | FN b→a |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in cascade:
        lines.append(
            f"| {r[0]} {r[1]} | {r[2]}→{r[3]} | {r[4]} | {r[5]} | {r[6]} | {r[7]}→{r[8]} | {r[9]}→{r[10]} | {r[11]} | {r[12]} |"
        )
    lines += [
        "",
        "A check whose `n` moved was measured on a different number of documents, "
        "usually because an earlier check stopped rejecting everything. Read the accuracy "
        "change together with `n`.",
        "",
        "## Decisions",
        "",
        "| Measure | Before | After | Change |",
        "|---|---|---|---|",
    ]
    for label, key in (
        ("touchless rate", "touchless_rate"),
        ("correct touchless rate", "correct_touchless_rate"),
        ("touchless accuracy", "touchless_accuracy"),
        ("decision accuracy", "decision_accuracy"),
        ("posted untouched (of clean)", "posted_untouched_rate"),
    ):
        # .get: a run scored before a metric existed has no value for it.
        lines.append(
            f"| {label} | {_pct(bd.get(key))} | {_pct(ad.get(key))} | {_delta(bd.get(key), ad.get(key))} |"
        )
    for label, key in (
        ("correctly auto-approved", "correctly_auto_approved"),
        ("incorrectly auto-approved", "incorrectly_auto_approved"),
        ("correctly rejected", "correctly_rejected"),
        ("incorrectly rejected", "incorrectly_rejected"),
        ("rejection reason exact", "reason_strict"),
        ("rejection reason another real fault", "reason_family"),
        ("rejection reason wrong", "reason_wrong"),
        ("errors / no decision", "errors_or_no_decision"),
        ("posted untouched", "posted_untouched"),
        ("posted untouched wrongly", "posted_untouched_wrongly"),
    ):
        b_v, a_v = bd["counts"].get(key, 0), ad["counts"].get(key, 0)
        lines.append(f"| {label} | {b_v} | {a_v} | {a_v - b_v:+d} |")

    lines += [
        "",
        "## Extraction",
        "",
        "| Group | Before | After | Change |",
        "|---|---|---|---|",
    ]
    for group in ("header", "totals", "lines", "tax"):
        key = f"{group}_accuracy"
        lines.append(
            f"| {group} | {_pct(bx.get(key))} | {_pct(ax.get(key))} | {_delta(bx.get(key), ax.get(key))} |"
        )
    lines.append(
        f"| documents scored | {bx['documents_scored']} | {ax['documents_scored']} | {ax['documents_scored'] - bx['documents_scored']:+d} |"
    )

    lines += [
        "",
        "## Cost and time",
        "",
        "| Measure | Before | After |",
        "|---|---|---|",
        f"| processing seconds, median | {bt['processing_time_seconds'].get('median', '-')} | {at['processing_time_seconds'].get('median', '-')} |",
        f"| LLM calls, mean | {bt['llm_calls'].get('mean', '-')} | {at['llm_calls'].get('mean', '-')} |",
        f"| cost USD, total | {bt['cost_usd'].get('sum', '-')} | {at['cost_usd'].get('sum', '-')} |",
        f"| errors | {bt['errors']} | {at['errors']} |",
        "",
        "## Flips",
        "",
        f"{len(flipped)} of {len(after['rows'])} scored rows changed verdict: "
        + ", ".join(f"{n} {k}" for k, n in kinds.most_common())
        + ". Every one is listed with its before and after evidence in `flips.csv`.",
        "",
    ]
    regressions = [f for f in flipped if f["kind"] == "regressed"]
    if regressions:
        lines += [
            "### Regressions",
            "",
            "| Test | Check | Expected | Before | After |",
            "|---|---|---|---|---|",
        ]
        for f in regressions[:50]:
            lines.append(
                f"| {f['invoice_id']} | {f['check_id']} | {f['expected_result']} | {f['before_result']} | {f['after_result']} |"
            )
        if len(regressions) > 50:
            lines.append(
                f"| … | and {len(regressions) - 50} more in flips.csv | | | |"
            )
        lines.append("")
    else:
        lines += ["No row that passed before fails now.", ""]

    (COMPARISON / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {COMPARISON / 'report.md'} ({len(flipped)} flips)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
