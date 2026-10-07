"""Re-run the four validation phases over extractions already on disk.

    python -m benchmark.harness.replay --run baseline --profile after [--company A]
        [--out NAME]

A development tool, not a result. It answers "what would today's validation
code decide about yesterday's extractions?" in seconds and with no model calls,
which is what makes it usable between fixes. It cannot see fields that were
never extracted, so it never replaces the AFTER run -- the report it prints
says so.

Duplicate detection is replayed in submission order against the corpus built up
so far, the same way the worker feeds it. Today's extraction cleanup and
confidence routing are applied first, from the document's own text layer, so
the replay is today's code end to end after the model call.

`--out NAME` writes the replayed records as benchmark/runs/NAME, which
`score` reads like any other run: the way to put a number on a validation fix
without spending a model run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path

from . import dataset, stack_main


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--run", required=True, help="run whose extractions to replay"
    )
    p.add_argument(
        "--profile", required=True, help="settings profile to replay under"
    )
    p.add_argument("--company", default=",".join(dataset.COMPANIES))
    p.add_argument("--out", help="write the replayed records as this run")
    args = p.parse_args(argv)

    run_dir = dataset.REPO / "benchmark" / "runs" / args.run
    workspace = Path(tempfile.mkdtemp(prefix="replay-"))
    stack_main.configure_environment(workspace)
    os.environ["DATABASE_URL"] = (
        ""  # nothing is stored; artifacts come from the jsonl
    )

    from invoice_processing import confidence, extraction_cleanup
    from invoice_processing.shared_libraries.acting import (
        general_invoice_agent as agent,
    )

    truth = dataset.ground_truth()
    out_dir = (
        dataset.REPO / "benchmark" / "runs" / args.out if args.out else None
    )
    if out_dir:
        (out_dir / "raw").mkdir(parents=True, exist_ok=True)

    from . import collect

    changed: Counter = Counter()
    for company in args.company.split(","):
        raw = run_dir / "raw" / f"{company}.jsonl"
        if not raw.exists():
            print(f"no {raw}")
            continue
        profile = stack_main.load_profile(args.profile, company)
        masters = profile["masters"]
        config = {**agent.DEFAULT_CONFIG, **profile["settings"]}
        po_file = workspace / "purchase_orders.json"
        po_file.write_text(
            json.dumps(
                {
                    "purchase_orders": dataset.po_records(
                        company, masters["purchase_orders"]
                    )
                }
            ),
            encoding="utf-8",
        )
        config["purchase_order_file"] = str(po_file)
        for key, filename, records in (
            (
                "vendor_master_file",
                "vendors.json",
                dataset.vendor_records(company),
            ),
            (
                "work_authorization_file",
                "work_authorizations.json",
                dataset.wa_records(company),
            ),
        ):
            flag = "vendors" if "vendor" in key else "work_authorizations"
            if masters.get(flag):
                path = workspace / filename
                path.write_text(json.dumps(records), encoding="utf-8")
                config[key] = str(path)
        agent._config_store["CONFIG"] = config

        records = [
            json.loads(line)
            for line in raw.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        records.sort(key=lambda r: r["submission_seq"])
        corpus: list = []
        replayed_records: list[dict] = []

        for rec in records:
            if rec["ingest_duplicate"] or not rec.get("extracted"):
                replayed_records.append(rec)
                continue
            pdf = dataset.DATASET / truth[rec["invoice_id"]]["document_file"]
            pages = confidence.extract_page_text(pdf)
            invoice = extraction_cleanup.clean(
                dict(rec["extracted"]),
                charges_and_lines=agent._charges_and_lines,
                pages=pages,
            )
            evidence = confidence.build_evidence(invoice, pdf)
            invoice["_routing"] = confidence.routing_decision(evidence)
            extraction = {
                "invoice": invoice,
                "work_authorization": rec.get("work_authorization"),
                "invoice_count": (rec["classification"] or {}).get(
                    "invoice_count"
                )
                or 1,
                "waf_count": (rec["classification"] or {}).get("waf_count")
                or 0,
                "extraction_failed": (rec["classification"] or {}).get(
                    "extraction_failed"
                ),
                # Check 1.1 reads this. Leaving it out made every replayed
                # non-invoice document pass a check the real run failed.
                "document_type": (rec["classification"] or {}).get(
                    "document_type"
                ),
            }
            folder = workspace / "cases" / rec["invoice_id"]
            folder.mkdir(parents=True, exist_ok=True)

            phase1 = agent.Phase1ValidatorAgent().run(folder, extraction)
            artifacts = {"phase1": phase1}
            if phase1["decision"] != "REJECT":
                phase2 = agent.Phase2ValidatorAgent().run(
                    folder, extraction, phase1
                )
                artifacts["phase2"] = phase2
                if phase2["decision"] != "REJECT":
                    phase3 = agent.Phase3ValidatorAgent(
                        seen_invoices=lambda exclude_case_id="", corpus=corpus: (
                            list(corpus)
                        )
                    ).run(folder, extraction, phase2)
                    artifacts["phase3"] = phase3
                    if phase3["decision"] != "REJECT":
                        artifacts["phase4"] = agent.Phase4ValidatorAgent().run(
                            folder, extraction, phase3
                        )

            from invoice_processing.duplicates import InvoiceKey

            corpus.append(InvoiceKey.from_invoice(rec["invoice_id"], invoice))

            replayed = collect.findings(artifacts)
            rejected = next(
                (
                    (number, artifacts[f"phase{number}"])
                    for number in (1, 2, 3, 4)
                    if (artifacts.get(f"phase{number}") or {}).get("decision")
                    == "REJECT"
                ),
                None,
            )
            replayed_records.append(
                {
                    **rec,
                    "extracted": {
                        k: v for k, v in invoice.items() if k != "_evidence"
                    },
                    "findings": replayed,
                    "final_decision": "REJECT" if rejected else "ACCEPT",
                    "rejection_template": rejected[1].get("rejection_template")
                    if rejected
                    else None,
                    "rejection_phase": rejected[0] if rejected else None,
                    "first_failing_step": next(
                        (
                            f["step"]
                            for f in replayed
                            if f["passed"] is False and not f["informational"]
                        ),
                        None,
                    ),
                }
            )
            before = {f["step"]: f["passed"] for f in rec["findings"]}
            after = {f["step"]: f["passed"] for f in replayed}
            for step in sorted(set(before) | set(after)):
                if before.get(step, "absent") != after.get(step, "absent"):
                    changed[
                        f"{step}: {before.get(step, 'absent')} -> {after.get(step, 'absent')}"
                    ] += 1

        if out_dir:
            (out_dir / "raw" / f"{company}.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in replayed_records),
                encoding="utf-8",
            )

    print(f"\nReplayed {args.run} under profile {args.profile}.")
    if not changed:
        print("No verdict changed.")
    for label, count in changed.most_common():
        print(f"  {count:4d}  {label}")
    print(
        "\nThis is today's code after the model call, over the fields the "
        "recorded run extracted. "
        "Anything that depends on a newly extracted field will not appear here; "
        "only a full run can measure that."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
