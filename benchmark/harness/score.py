"""Score one run against the frozen ground truth.

    python -m benchmark.harness.score benchmark/runs/baseline --profile benchmark/harness/profiles/baseline.json

Produces results.csv (one row per invoice x check, 4,200 rows), results.json
(rows plus every aggregate), extraction.csv and report.md in the run directory.

Two rules keep the numbers honest:

* A check the pipeline never reached is NOT_RUN, never PASS. The pipeline
  stops at the first failing phase, so most checks on a rejected invoice have
  no verdict; the dataset's own README says how to score that
  ("cascade-aware": an expected failure counts only where it is the first
  failing check, an expected pass only where the check was reached).
* Every population states its denominator. A percentage without one is how a
  benchmark starts lying.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import dataset, normalize

POSITIVE = {"FAIL", "INFORMATIONAL_DISCREPANCY"}
REACHED = {"PASS", "FAIL", "INFORMATIONAL_MATCH", "INFORMATIONAL_DISCREPANCY"}

# The dataset's 13 rejection strings keyed by check; the app's own specific
# reasons (purchase-order outcomes) are mapped onto them, never retyped.
DATASET_REASON = {
    "1.1": "Document is not a valid Tax Invoice",
    "1.2": "Invoice addressed to different company",
    "1.3": "Document is not a Tax Invoice",
    "1.4": "Missing Work Authorization Form",
    "1.5": "Multiple invoices in submission",
    "2.1": "Invoice charges not itemized",
    "2.2": "Invalid purchase order",
    "3.1": "Invoice already processed",
    "3.2": "Vendor tax ID invalid",
    "3.3": "Invoice is future dated",
    "4.2": "Invoice amounts do not reconcile",
    "4.3": "Invoice does not match work authorization",
    "4.4": "Invoice tax amount incorrect",
}


def app_reason_map() -> dict[str, str]:
    from invoice_processing import po_matching  # light import, no Vertex

    out = {v: v for v in DATASET_REASON.values()}
    for template in po_matching._REJECTION_TEMPLATES.values():
        out[template] = DATASET_REASON["2.2"]
    return out


# What each check reads, so a failed row shows the values behind it.
FIELDS_FOR_CHECK = {
    "1.1": ("invoice_number", "extraction_failed", "document_type"),
    "1.2": ("customer_name", "customer_tax_id"),
    "1.3": ("vendor_tax_id", "invoice_date", "vendor_name", "document_type"),
    "1.4": ("waf_count", "work_authorization_reference", "vendor_name"),
    "1.5": ("invoice_count",),
    "2.1": ("line_item_count",),
    "2.2": (
        "po_number",
        "invoice_total_inc_tax",
        "invoice_total_ex_tax",
        "vendor_name",
    ),
    "3.1": (
        "vendor_name",
        "invoice_number",
        "invoice_date",
        "invoice_total_inc_tax",
    ),
    "3.2": ("vendor_tax_id", "_tax_id_validation", "vendor_name"),
    "3.3": ("invoice_date",),
    "4.1": (
        "invoice_total_inc_tax",
        "invoice_total_ex_tax",
        "tax_amount",
        "subtotal",
        "discount",
        "round_off",
    ),
    "4.2": ("invoice_total_ex_tax", "line_item_count", "subtotal"),
    "4.3": ("work_authorization_reference", "line_item_count"),
    "4.4": (
        "tax_amount",
        "invoice_total_ex_tax",
        "currency",
        "cgst_amount",
        "sgst_amount",
        "igst_amount",
    ),
}


def norm_expected(value: str) -> str:
    return re.sub(
        r"_+", "_", re.sub(r"[^A-Z]", "_", str(value or "").upper())
    ).strip("_")


# ---------------------------------------------------------------------------
# Per document -> per check
# ---------------------------------------------------------------------------


def load_records(run_dir: Path) -> dict[str, dict]:
    records = {}
    for path in sorted((run_dir / "raw").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                records[rec["invoice_id"]] = rec
    return records


def actual_for(rec: dict) -> dict[str, dict]:
    """check_id -> {result, evidence, template, outcome}."""
    out = {
        cid: {
            "result": "NOT_RUN",
            "evidence": None,
            "template": None,
            "outcome": None,
        }
        for cid, _ in dataset.CHECKS
    }
    if rec["ingest_duplicate"]:
        out["3.1"] = {
            "result": "FAIL",
            "evidence": "Byte-identical file already received; refused at ingest (sha256)",
            "template": "Invoice already processed",
            "outcome": "file_hash",
        }
        return out
    for f in rec["findings"]:
        step = f["step"]
        if step not in out:
            continue
        if f["skipped"] or f["passed"] is None:
            result = "SKIPPED"
        elif step == "4.1":
            result = (
                "INFORMATIONAL_MATCH"
                if f["passed"]
                else "INFORMATIONAL_DISCREPANCY"
            )
        else:
            result = "PASS" if f["passed"] else "FAIL"
        out[step] = {
            "result": result,
            "evidence": f["evidence"],
            "template": f["rejection_template"],
            "outcome": f["outcome"],
        }
    return out


def extracted_values(rec: dict, check_id: str) -> dict:
    inv = rec.get("extracted") or {}
    cls = rec.get("classification") or {}
    pool = {
        **inv,
        "line_item_count": len(inv.get("line_items") or []),
        "waf_count": cls.get("waf_count"),
        "invoice_count": cls.get("invoice_count"),
        "document_type": cls.get("document_type") or inv.get("document_type"),
        "extraction_failed": cls.get("extraction_failed"),
    }
    return {k: pool.get(k) for k in FIELDS_FOR_CHECK[check_id] if k in pool}


def build_rows(records: dict[str, dict]) -> list[dict]:
    matrix = dataset.matrix()
    rows = []
    for invoice_id, rec in sorted(
        records.items(),
        key=lambda kv: (kv[1]["company_id"], kv[1]["submission_seq"]),
    ):
        actual = actual_for(rec)
        app_first = rec.get("first_failing_step")
        for check_id, _name in dataset.CHECKS:
            m = matrix[(invoice_id, check_id)]
            expected = norm_expected(m["expected_result"])
            a = actual[check_id]
            result = a["result"]
            reached = result in REACHED
            first = m["is_first_failing_check"] == "True"
            passed: bool | None
            note = ""
            if expected == "N_A":
                if result in POSITIVE:
                    passed, note = (
                        False,
                        "spurious failure: check not applicable to this document",
                    )
                else:
                    passed, note = None, "not applicable"
            elif reached:
                passed = result == expected
                if not passed:
                    note = f"expected {expected}, got {result}"
            elif expected == "FAIL" and first:
                passed = False
                note = (
                    f"never reached: pipeline stopped at {app_first}"
                    if app_first
                    else "never reached: pipeline accepted the invoice"
                )
            else:
                passed = None
                note = (
                    "not reached: pipeline stopped earlier"
                    if app_first
                    else ("skipped" if result == "SKIPPED" else "not reached")
                )
                if expected == "FAIL":
                    note = "cascaded: an earlier expected failure hides this one by design"

            if result in POSITIVE:
                actual_action = "REJECT" if check_id != "4.1" else "FLAG"
                if check_id == "3.1" and rec["ingest_duplicate"]:
                    actual_action = "REJECT"
            elif result in ("PASS", "INFORMATIONAL_MATCH"):
                actual_action = "PASS"
            else:
                actual_action = result

            rows.append(
                {
                    "test_id": f"{invoice_id}:{check_id}",
                    "invoice_id": invoice_id,
                    "company_id": rec["company_id"],
                    "check_id": check_id,
                    "check_name": dataset.CHECK_NAME[check_id],
                    "expected_result": expected,
                    "actual_result": result,
                    "expected_action": m["expected_system_action"],
                    "actual_action": actual_action,
                    "passed": passed,
                    "reached": reached,
                    "is_first_failing_check": first,
                    "in_balanced_population": m["in_balanced_population"]
                    == "True",
                    "error_message": note if passed is not True else "",
                    "fault_code": m["fault_code"],
                    "extracted_values": extracted_values(rec, check_id),
                    "validation_details": a["evidence"],
                    "processing_time": rec.get("processing_time"),
                    "llm_calls": rec.get("llm_calls"),
                    "cost": rec.get("cost_usd"),
                }
            )
    return rows


# ---------------------------------------------------------------------------
# Aggregates
# ---------------------------------------------------------------------------


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def check_metrics(
    rows: list[dict], population: str, balanced: bool
) -> dict[str, dict]:
    """population: 'cascade' (dataset README rule) or 'all' (every reached row)."""
    out = {}
    for check_id, name in dataset.CHECKS:
        tp = tn = fp = fn = 0
        n_not_run = n_spurious = n_reached = 0
        for r in rows:
            if r["check_id"] != check_id or (
                balanced and not r["in_balanced_population"]
            ):
                continue
            exp, act = r["expected_result"], r["actual_result"]
            if r["reached"]:
                n_reached += 1
            if exp == "N_A":
                if act in POSITIVE:
                    n_spurious += 1
                continue
            if population == "cascade":
                include = (exp == "FAIL" and r["is_first_failing_check"]) or (
                    exp not in POSITIVE and r["reached"]
                )
                if exp == "INFORMATIONAL_DISCREPANCY":
                    include = r["reached"]
            else:
                include = r["reached"]
            if not include:
                continue
            if not r["reached"]:
                n_not_run += 1
            positive, predicted = exp in POSITIVE, act in POSITIVE
            if positive and predicted:
                tp += 1
            elif positive:
                fn += 1
            elif predicted:
                fp += 1
            else:
                tn += 1
        n = tp + tn + fp + fn
        out[check_id] = {
            "name": name,
            "n": n,
            "tp": tp,
            "tn": tn,
            "fp": fp,
            "fn": fn,
            "accuracy": _rate(tp + tn, n),
            "precision": _rate(tp, tp + fp),
            "recall": _rate(tp, tp + fn),
            "fpr": _rate(fp, fp + tn),
            "fnr": _rate(fn, fn + tp),
            "reached": n_reached,
            "not_run_counted_as_fn": n_not_run,
            "spurious_failures_on_na": n_spurious,
        }
    return out


def decision_metrics(records: dict[str, dict]) -> dict:
    master = dataset.master()
    reason_map = app_reason_map()
    c = Counter()
    review = 0
    first_failing = Counter()
    for invoice_id, rec in records.items():
        expected = master[invoice_id]["expected_final_decision"]
        actual = rec["final_decision"]
        c["total"] += 1
        c[f"expected_{expected}"] += 1
        c[f"actual_{actual}"] += 1
        routing = (rec.get("extracted") or {}).get("_routing")
        if actual == "ACCEPT":
            c["auto_approved"] += 1
            c[
                "correctly_auto_approved"
                if expected == "ACCEPT"
                else "incorrectly_auto_approved"
            ] += 1
            if routing == "REVIEW":
                review += 1
        elif actual == "REJECT":
            c["rejected"] += 1
            if expected == "REJECT":
                c["correctly_rejected"] += 1
                app_reason = reason_map.get(
                    rec.get("rejection_template") or "", None
                )
                expected_reason = master[invoice_id][
                    "expected_rejection_reason"
                ]
                gt_faults = set(
                    (master[invoice_id]["fault_checks"] or "").split("|")
                ) - {""}
                if app_reason == expected_reason:
                    c["reason_strict"] += 1
                elif app_reason in {DATASET_REASON.get(f) for f in gt_faults}:
                    c["reason_family"] += 1
                else:
                    c["reason_wrong"] += 1
            else:
                c["incorrectly_rejected"] += 1
        else:
            c["errors_or_no_decision"] += 1
        first_failing[
            (
                master[invoice_id]["first_failing_check"] or "none",
                rec.get("first_failing_step") or "none",
            )
        ] += 1
    total = c["total"]
    return {
        "counts": dict(c),
        "touchless_rate": _rate(c["auto_approved"], total),
        "correct_touchless_rate": _rate(c["correctly_auto_approved"], total),
        "touchless_accuracy": _rate(
            c["correctly_auto_approved"], c["auto_approved"]
        ),
        "decision_accuracy": _rate(
            c["correctly_auto_approved"] + c["correctly_rejected"], total
        ),
        "accepted_but_routed_to_review": review,
        "first_failing_confusion": {
            f"{e}->{a}": n for (e, a), n in sorted(first_failing.items())
        },
    }


HEADER_FIELDS = (
    ("invoice_number", "invoice_number", "ident"),
    ("invoice_date", "invoice_date", "date"),
    ("due_date", "due_date", "date"),
    ("vendor_tax_id", "vendor_gstin", "ident"),
    ("customer_name", "customer_name", "fold"),
    ("po_number", "po_number", "ident"),
    ("work_authorization_reference", "work_authorization_id", "ident"),
    ("customer_tax_id", "customer_gstin", "ident"),
)
TOTAL_FIELDS = (
    # Everything before tax: the taxable amount after discount, plus other
    # charges such as freight. The scorer first compared this against
    # `taxable_amount` alone, which leaves freight out -- so an extractor that
    # read "total before tax" correctly was marked wrong on every invoice that
    # carried a freight line: 19 of the 28 clean-document "errors".
    ("invoice_total_ex_tax", "_total_before_tax", "amount"),
    ("tax_amount", "tax_amount", "amount"),
    ("invoice_total_inc_tax", "stated_total", "amount"),
    ("cgst_amount", "cgst", "amount"),
    ("sgst_amount", "sgst", "amount"),
    ("igst_amount", "igst", "amount"),
)


def _match(kind: str, got, want) -> bool | None:
    if kind == "amount":
        return normalize.amounts_equal(got, want)
    if want in (None, "") and got in (None, ""):
        return True
    if want in (None, "") or got in (None, ""):
        return False
    if kind == "ident":
        return normalize.ident(got) == normalize.ident(want)
    if kind == "date":
        return normalize.iso_date(got) == normalize.iso_date(want)
    if kind == "fold":
        g, w = normalize.fold(got), normalize.fold(want)
        return g == w or w in g
    return got == want


def extraction_metrics(records: dict[str, dict]) -> tuple[dict, list[dict]]:
    per_field: dict[str, Counter] = defaultdict(Counter)
    per_doc = []
    for invoice_id, rec in records.items():
        gt = rec and dataset.ground_truth()[invoice_id]
        if gt["record_kind"] == "NONDOC" or rec["ingest_duplicate"]:
            continue
        inv = rec.get("extracted") or {}
        if not inv or (rec.get("classification") or {}).get(
            "extraction_failed"
        ):
            per_doc.append(
                {
                    "invoice_id": invoice_id,
                    "company_id": rec["company_id"],
                    "extracted": False,
                }
            )
            continue
        sub = dict(gt["as_submitted"])
        sub["_total_before_tax"] = str(
            (normalize.amount(sub.get("taxable_amount")) or 0)
            + (normalize.amount(sub.get("other_charges_total")) or 0)
        )
        doc = {
            "invoice_id": invoice_id,
            "company_id": rec["company_id"],
            "extracted": True,
        }
        groups = {"header": [], "totals": [], "lines": []}
        for app_key, gt_key, kind in HEADER_FIELDS:
            if app_key not in inv:
                continue  # field not in this version's schema
            ok = _match(kind, inv.get(app_key), sub.get(gt_key))
            per_field[app_key][ok] += 1
            doc[app_key] = ok
            groups["header"].append(ok)
        for app_key, gt_key, kind in TOTAL_FIELDS:
            if app_key not in inv:
                continue
            ok = _match(kind, inv.get(app_key), sub.get(gt_key))
            per_field[app_key][ok] += 1
            doc[app_key] = ok
            groups["totals"].append(ok)
        got_lines = inv.get("line_items") or []
        want_lines = sub.get("lines") or []
        count_ok = len(got_lines) == len(want_lines)
        per_field["line_count"][count_ok] += 1
        doc["line_count"] = count_ok
        groups["lines"].append(count_ok)
        for g, w in zip(got_lines, want_lines, strict=False):
            for app_key, gt_key, kind in (
                ("description", "description", "fold"),
                ("quantity", "quantity", "amount"),
                ("unit_price", "unit_price", "amount"),
                ("amount_ex_tax", "line_total", "amount"),
                ("hsn_sac", "hsn_sac", "ident"),
            ):
                if app_key not in g:
                    continue
                ok = _match(kind, g.get(app_key), w.get(gt_key))
                per_field[f"line.{app_key}"][ok] += 1
                groups["lines"].append(ok)
        for name, results in groups.items():
            scored = [r for r in results if r is not None]
            doc[f"{name}_accuracy"] = _rate(
                sum(1 for r in scored if r), len(scored)
            )
        header_and_totals = [
            doc.get(k) for k, _, _ in HEADER_FIELDS + TOTAL_FIELDS if k in doc
        ]
        doc["header_and_totals_correct"] = all(
            r is not False for r in header_and_totals
        )
        per_doc.append(doc)

    summary = {
        "documents_scored": sum(1 for d in per_doc if d["extracted"]),
        "documents_not_extracted": sum(
            1 for d in per_doc if not d["extracted"]
        ),
        "per_field": {
            k: {
                "matched": c[True],
                "mismatched": c[False],
                "missing": c[None],
                "rate": _rate(c[True], c[True] + c[False]),
            }
            for k, c in sorted(per_field.items())
        },
    }
    for group in ("header", "totals", "lines"):
        vals = [
            d[f"{group}_accuracy"]
            for d in per_doc
            if d.get(f"{group}_accuracy") is not None
        ]
        summary[f"{group}_accuracy"] = (
            round(statistics.mean(vals), 4) if vals else None
        )
    tax_fields = [
        c
        for k, c in per_field.items()
        if k in ("tax_amount", "cgst_amount", "sgst_amount", "igst_amount")
    ]
    summary["tax_accuracy"] = _rate(
        sum(c[True] for c in tax_fields),
        sum(c[True] + c[False] for c in tax_fields),
    )
    summary["all_header_and_totals_correct"] = sum(
        1 for d in per_doc if d.get("header_and_totals_correct")
    )
    return summary, per_doc


def timing_metrics(records: dict[str, dict]) -> dict:
    def stats(values):
        values = [v for v in values if isinstance(v, (int, float))]
        if not values:
            return {"n": 0}
        values.sort()
        return {
            "n": len(values),
            "mean": round(statistics.mean(values), 3),
            "median": round(statistics.median(values), 3),
            "p95": round(
                values[min(len(values) - 1, int(len(values) * 0.95))], 3
            ),
            "sum": round(sum(values), 3),
        }

    out = {}
    groups = {"all": list(records.values())}
    for rec in records.values():
        groups.setdefault(rec["company_id"], []).append(rec)
    for name, recs in groups.items():
        processed = [r for r in recs if not r["ingest_duplicate"]]
        out[name] = {
            "processing_time_seconds": stats(
                [r.get("processing_time") for r in processed]
            ),
            "wall_seconds": stats([r.get("wall_seconds") for r in recs]),
            "llm_calls": stats([r.get("llm_calls") for r in processed]),
            "cost_usd": stats([r.get("cost_usd") for r in processed]),
            "errors": sum(
                1 for r in recs if r["final_decision"] in ("ERROR", "NONE")
            ),
        }
    out["not_measured"] = [
        "OCR cost (folded into Gemini cost)",
        "infrastructure cost",
        "human review time",
    ]
    return out


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


def _pct(v) -> str:
    return "-" if v is None else f"{v * 100:.1f}%"


def _check_table(metrics: dict, title: str) -> list[str]:
    lines = [
        f"### {title}",
        "",
        "| Check | n | TP | TN | FP | FN | Acc | Prec | Recall | FPR | FNR | not run→FN | spurious on N/A |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for cid, m in metrics.items():
        lines.append(
            f"| {cid} {m['name']} | {m['n']} | {m['tp']} | {m['tn']} | {m['fp']} | {m['fn']} | {_pct(m['accuracy'])} | {_pct(m['precision'])} | {_pct(m['recall'])} | {_pct(m['fpr'])} | {_pct(m['fnr'])} | {m['not_run_counted_as_fn']} | {m['spurious_failures_on_na']} |"
        )
    return [*lines, ""]


def report_md(
    run: str,
    meta: dict,
    per_check: dict,
    decision: dict,
    extraction: dict,
    timing: dict,
    records: dict,
) -> str:
    c = decision["counts"]
    lines = [
        f"# Benchmark run `{run}`",
        "",
        f"Generated {meta['generated_at']} at `{meta['git_sha']}` with profile `{meta['profile']}`; {meta['records']} of 300 submissions recorded.",
        "",
    ]
    lines += ["## Decisions", "", "| Measure | Value |", "|---|---|"]
    for k in (
        "total",
        "expected_ACCEPT",
        "expected_REJECT",
        "auto_approved",
        "correctly_auto_approved",
        "incorrectly_auto_approved",
        "rejected",
        "correctly_rejected",
        "incorrectly_rejected",
        "errors_or_no_decision",
        "reason_strict",
        "reason_family",
        "reason_wrong",
    ):
        lines.append(f"| {k} | {c.get(k, 0)} |")
    lines += [
        f"| touchless rate (ACCEPT / {c['total']}) | {_pct(decision['touchless_rate'])} |",
        f"| correct touchless rate (correct ACCEPT / {c['total']}) | {_pct(decision['correct_touchless_rate'])} |",
        f"| touchless accuracy (correct ACCEPT / ACCEPT) | {_pct(decision['touchless_accuracy'])} |",
        f"| decision accuracy | {_pct(decision['decision_accuracy'])} |",
        f"| accepted but routed to review | {decision['accepted_but_routed_to_review']} |",
        "",
        "Reason match is scored on correctly rejected invoices: `strict` = the app's reason is the dataset's first-failing reason; `family` = it names another fault the dataset planted on that invoice; `wrong` = neither.",
        "",
    ]
    lines += [
        "## Per check",
        "",
        "Positive class = FAIL (for 4.1, DISCREPANCY). `cascade` follows the dataset README: an expected FAIL counts only where it is the first failing check (a NOT_RUN there is a false negative); an expected PASS counts only where the check was reached. `all findings` scores every check the pipeline actually reached. `balanced` restricts to the dataset's balanced population.",
        "",
    ]
    lines += _check_table(
        per_check["cascade_full"], "Cascade-aware, full population"
    )
    lines += _check_table(
        per_check["cascade_balanced"], "Cascade-aware, balanced population"
    )
    lines += _check_table(
        per_check["all_full"], "All findings, full population"
    )
    lines += [
        "## First failing check: expected → actual",
        "",
        "| expected → actual | n |",
        "|---|---|",
    ]
    for k, n in sorted(
        decision["first_failing_confusion"].items(), key=lambda kv: -kv[1]
    ):
        lines.append(f"| {k} | {n} |")
    lines += [
        "",
        "## Extraction (vs as-submitted values)",
        "",
        f"Scored on {extraction['documents_scored']} extracted invoice documents ({extraction['documents_not_extracted']} not extracted; non-invoice documents and ingest-level duplicates excluded).",
        "",
        "| Group | Accuracy |",
        "|---|---|",
    ]
    for g in ("header", "totals", "lines", "tax"):
        lines.append(f"| {g} | {_pct(extraction.get(f'{g}_accuracy'))} |")
    lines += [
        f"| all header+totals fields correct | {extraction['all_header_and_totals_correct']} docs |",
        "",
        "| Field | matched | mismatched | missing | rate |",
        "|---|---|---|---|---|",
    ]
    for k, v in extraction["per_field"].items():
        lines.append(
            f"| {k} | {v['matched']} | {v['mismatched']} | {v['missing']} | {_pct(v['rate'])} |"
        )
    lines += [
        "",
        "## Timing and cost",
        "",
        "| Group | docs | processing s (mean/median/p95) | wall s mean | LLM calls mean | cost USD sum | errors |",
        "|---|---|---|---|---|---|---|",
    ]
    for name in ("all", "A", "B", "C"):
        t = timing.get(name)
        if not t:
            continue
        proc, wall, calls, cost = (
            t["processing_time_seconds"],
            t["wall_seconds"],
            t["llm_calls"],
            t["cost_usd"],
        )
        lines.append(
            f"| {name} | {wall.get('n', 0)} | {proc.get('mean', '-')}/{proc.get('median', '-')}/{proc.get('p95', '-')} | {wall.get('mean', '-')} | {calls.get('mean', '-')} | {cost.get('sum', '-')} | {t['errors']} |"
        )
    lines += ["", f"Not measured: {', '.join(timing['not_measured'])}.", ""]
    errors = [
        r for r in records.values() if r["final_decision"] in ("ERROR", "NONE")
    ]
    if errors:
        lines += ["## Errors", ""]
        for r in errors:
            lines.append(
                f"- `{r['invoice_id']}`: {(r.get('error_message') or r.get('outcome') or '')[:300]}"
            )
        lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("run_dir")
    p.add_argument("--profile", default="")
    args = p.parse_args(argv)
    run_dir = Path(args.run_dir).resolve()
    records = load_records(run_dir)
    if not records:
        print(f"no records under {run_dir / 'raw'}")
        return 1
    rows = build_rows(records)
    per_check = {
        "cascade_full": check_metrics(rows, "cascade", False),
        "cascade_balanced": check_metrics(rows, "cascade", True),
        "all_full": check_metrics(rows, "all", False),
        "all_balanced": check_metrics(rows, "all", True),
    }
    decision = decision_metrics(records)
    extraction, per_doc = extraction_metrics(records)
    timing = timing_metrics(records)
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=dataset.REPO, text=True
        ).strip()
    except Exception:
        sha = "unknown"
    meta = {
        "run": run_dir.name,
        "profile": args.profile,
        "git_sha": sha,
        "generated_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "records": len(records),
        "reference_date": "2026-09-03",
    }

    with open(run_dir / "results.csv", "w", newline="", encoding="utf-8") as fh:
        cols = [
            "test_id",
            "invoice_id",
            "company_id",
            "check_id",
            "check_name",
            "expected_result",
            "actual_result",
            "expected_action",
            "actual_action",
            "passed",
            "reached",
            "is_first_failing_check",
            "in_balanced_population",
            "error_message",
            "fault_code",
            "extracted_values",
            "validation_details",
            "processing_time",
            "llm_calls",
            "cost",
        ]
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            row = dict(r)
            row["extracted_values"] = json.dumps(
                r["extracted_values"], default=str
            )
            row["passed"] = "" if r["passed"] is None else r["passed"]
            w.writerow(row)
    with open(
        run_dir / "extraction.csv", "w", newline="", encoding="utf-8"
    ) as fh:
        keys = sorted(
            {k for d in per_doc for k in d},
            key=lambda k: (
                k not in ("invoice_id", "company_id", "extracted"),
                k,
            ),
        )
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(per_doc)
    with open(run_dir / "results.json", "w", encoding="utf-8") as fh:
        json.dump(
            {
                "meta": meta,
                "per_check": per_check,
                "decision": decision,
                "extraction": extraction,
                "timing": timing,
                "documents": {
                    k: {
                        kk: vv
                        for kk, vv in v.items()
                        if kk not in ("findings", "extracted")
                    }
                    for k, v in records.items()
                },
                "rows": rows,
            },
            fh,
            indent=1,
            default=str,
        )
    (run_dir / "report.md").write_text(
        report_md(
            run_dir.name, meta, per_check, decision, extraction, timing, records
        ),
        encoding="utf-8",
    )
    print(f"scored {len(records)} documents -> {run_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
