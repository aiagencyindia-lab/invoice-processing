"""One company, one isolated stack, one process.

    python -m benchmark.harness.stack_main --company A --out benchmark/runs/baseline \
        --profile benchmark/harness/profiles/baseline.json [--subset-file ids.txt] [--fresh] [--seed-only]

Own SQLite database, own data directory, own worker -- the same code that runs
in production, minus the network. Every document goes through the real
ingestion path and the real worker; nothing is written to the database except
by the product. Environment is fixed *before* `invoice_processing` is imported
because its paths and store resolvers bind at import time.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

from . import dataset

REPO = dataset.REPO


def parse_args(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--company", required=True, choices=dataset.COMPANIES)
    p.add_argument(
        "--out",
        required=True,
        help="run directory, e.g. benchmark/runs/baseline",
    )
    p.add_argument("--profile", required=True)
    p.add_argument(
        "--subset-file", help="invoice ids, one per line; others are skipped"
    )
    p.add_argument(
        "--fresh", action="store_true", help="wipe this company's stack first"
    )
    p.add_argument(
        "--seed-only", action="store_true", help="bootstrap the tenant and stop"
    )
    return p.parse_args(argv)


def configure_environment(root: Path) -> None:
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    os.environ["DATABASE_URL"] = f"sqlite:///{root / 'app.db'}"
    os.environ["INVOICE_DATA_DIR"] = str(data)
    for key in (
        "CLOUD_SQL_INSTANCE",
        "GCS_BUCKET",
        "PUBSUB_TOPIC",
        "PUBSUB_SUBSCRIPTION",
    ):
        os.environ.pop(key, None)
    # The dataset's expectations are computed relative to this date.
    os.environ.setdefault("INVOICE_REFERENCE_DATE", "2026-09-03")
    # Configuration the engine needs to start; never item_code_cache.json,
    # which would warm across runs and make LLM counts incomparable.
    source = REPO / "invoice_processing" / "data"
    for name in (
        "reconstructed_rules_book.md",
        "rule_base.json",
        "rule_discovery_cache.json",
    ):
        if (source / name).exists() and not (data / name).exists():
            shutil.copy2(source / name, data / name)


def load_profile(path: str, company: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        profile = json.load(fh)
    settings = dict(profile["settings"])
    settings["organization_names"] = [dataset.company(company)["legal_name"]]
    for key, value in list(settings.items()):
        if value == "@hsn_rates":
            settings[key] = dataset.hsn_rates()
        elif value == "@company_gstin":
            settings[key] = [dataset.company(company)["gstin"]]
    return {"settings": settings, "masters": profile["masters"]}


def main(argv=None) -> int:
    args = parse_args(argv)
    company = args.company
    out = Path(args.out).resolve()
    root = out / "stacks" / company
    if args.fresh and root.exists():
        shutil.rmtree(root)
    configure_environment(root)

    # --- everything below may import the product ---------------------------
    from invoice_processing import paths, tenant_settings
    from invoice_processing.api import ingest, service
    from invoice_processing.db.migrate import upgrade_to_head
    from invoice_processing.db.models import Tenant
    from invoice_processing.db.session import tenant_session
    from invoice_processing.ports import RunContext
    from invoice_processing.worker.runner import (
        AttemptLog,
        Worker,
        _worker_id,
    )

    from . import collect

    tenant_id = f"bench-{company}"
    profile = load_profile(args.profile, company)

    upgrade_to_head()
    with tenant_session(tenant_id) as session:
        if session.get(Tenant, tenant_id) is None:
            session.add(
                Tenant(
                    id=tenant_id,
                    slug=tenant_id,
                    name=dataset.company(company)["legal_name"],
                )
            )

    # Settings go through the same validator PUT /api/settings uses, so a
    # profile cannot set what an administrator could not.
    cleaned = tenant_settings.validate(profile["settings"])
    service.get_tenant_settings().put(tenant_id, cleaned)

    def write_master(path: Path, key: str, records: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({key: records}, indent=2), encoding="utf-8")

    masters = profile["masters"]
    write_master(
        Path(paths.purchase_order_file(tenant_id)),
        "purchase_orders",
        dataset.po_records(company, masters["purchase_orders"]),
    )
    if masters.get("vendors"):
        write_master(
            Path(paths.vendor_master_file(tenant_id)),
            "vendors",
            dataset.vendor_records(company),
        )
    if masters.get("work_authorizations"):
        write_master(
            Path(paths.work_authorization_file(tenant_id)),
            "work_authorizations",
            dataset.wa_records(company),
        )
    if args.seed_only:
        print(f"seeded {tenant_id} at {root}")
        return 0

    raw_dir = out / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / f"{company}.jsonl"
    done: set[str] = set()
    if raw_path.exists():
        for line in raw_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                done.add(json.loads(line)["invoice_id"])

    subset = None
    if args.subset_file:
        subset = {
            line.strip()
            for line in Path(args.subset_file)
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        }

    cases = service.get_cases()
    documents = service.get_documents()
    claims = service.get_claim_store(worker_id=_worker_id())
    worker = Worker(
        queue=service.get_queue(),
        claims=claims,
        audit=service.get_audit(),
        attempts=AttemptLog(cases),
    )

    todo = [
        s
        for s in dataset.submissions(company)
        if s.invoice_id not in done
        and (subset is None or s.invoice_id in subset)
    ]
    print(
        f"[{company}] {len(todo)} to process, {len(done)} already done",
        flush=True,
    )

    for index, sub in enumerate(todo, 1):
        data = sub.pdf_path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        # A crash mid-document leaves a `running` claim the worker would skip.
        existing = documents.find_by_sha(tenant_id, sha)
        if (
            existing is not None
            and claims.state(tenant_id, existing["id"]) == "running"
        ):
            claims.release(tenant_id, existing["id"])

        started = time.time()
        ctx = RunContext(tenant_id=tenant_id, actor="benchmark")
        result = ingest.ingest_document(
            ctx, data, filename=sub.filename, source="upload"
        )
        # A document registered by an interrupted run looks exactly like a
        # duplicate to ingestion -- same bytes, same hash, already on file --
        # and recording it as one would credit the application with catching a
        # duplicate it never saw. Only a document that actually produced
        # artifacts was really processed before.
        processed_before = bool(
            cases.get_artifacts(
                RunContext(tenant_id=tenant_id, case_id=result.document_id)
            )
        )
        interrupted = result.duplicate and not processed_before
        if interrupted:
            from invoice_processing.worker.runner import enqueue_document

            enqueue_document(service.get_queue(), ctx, result)

        outcome = "ingest-duplicate"
        if not result.duplicate or interrupted:
            outcome = "retry"
            while outcome == "retry":
                outcomes = worker.run_once(max_messages=1)
                outcome = outcomes[0] if outcomes else "no-message"
        wall = time.time() - started

        artifacts = cases.get_artifacts(
            RunContext(tenant_id=tenant_id, case_id=result.document_id)
        )
        error_file = paths.AGENTIC_FLOW_OUT / result.document_id / "error.txt"
        error_text = (
            error_file.read_text(encoding="utf-8")[:2000]
            if error_file.exists()
            else None
        )
        rec = collect.record(
            sub,
            result.document_id,
            artifacts,
            ingest_duplicate=result.duplicate and not interrupted,
            wall_seconds=wall,
            outcome=outcome,
            error_text=error_text,
        )
        with open(raw_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
        print(
            f"[{company}] {index}/{len(todo)} {sub.invoice_id} -> {rec['final_decision']}"
            f" {rec['rejection_template'] or ''} ({wall:.0f}s, {outcome})",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
