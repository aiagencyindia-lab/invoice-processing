"""Start the API against a scratch data tree, for the end-to-end run.

The browser test drives the real service, which writes: it approves invoices,
records corrections, saves settings and stores uploads. Pointed at the normal
data directory it would do all of that to the developer's own invoices -- three
test suites have already done exactly that, which is why `INVOICE_DATA_DIR`
exists.

So this seeds a throwaway tree and execs uvicorn against it. Seeded, not empty:
the data directory holds tracked configuration as well as output, and an empty
one leaves the engine with no rules book.

The API runs on a scratch SQLite database inside that tree, as production runs
on Postgres. Without one, every database-only feature -- the approved-invoice
sheet, the two-person bank-change queue -- could only be tested for its "needs
a database" message, never for what it does.

A goods receipt queues a re-match of the invoices it may release, and only a
worker runs that. The real worker would also take every uploaded document and
call Vertex, so the API is started beside `--rematch-worker` instead: a second
process with the same environment that every 0.5 s moves `case.rematch`
messages out of the shared local queue into its own and handles them with the
real `Worker`. Document messages are never pulled, leased, acked or nacked --
they stay queued exactly as the upload left them. It stops with the API.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SOURCE = REPO / "invoice_processing" / "data"
GOLDEN = REPO / "tests" / "fixtures" / "golden" / "cases"

# Under frontend/ so `npx playwright test` owns its own workspace, and
# gitignored there.
SCRATCH = REPO / "frontend" / "e2e" / ".data"

TENANT = "e2e"

# The invoice held by step 2.3: line 1 is short one unit until a receipt.
PO_CASE = "case_004_po"
PO_NUMBER = "PO-E2E-1"
PO_VENDOR = "Bearing Test Supplies"


def environment(port: str) -> dict[str, str]:
    return {
        "INVOICE_DATA_DIR": str(SCRATCH),
        "DATABASE_URL": f"sqlite:///{SCRATCH / 'e2e.db'}",
        # The browser cannot sign in to Identity Platform unattended, and the
        # sign-in gate itself is covered by tests/test_frontend_reaches_the_flow.py.
        "AUTH_DISABLED": "1",
        "ENV": "local",
        "AUTH_DEV_TENANT": TENANT,
        # Never the real bucket, database or queue. Set empty rather than
        # removed: the product calls load_dotenv(), which refills absent ones.
        "GCS_BUCKET": "",
        "CLOUD_SQL_INSTANCE": "",
        "PUBSUB_TOPIC": "",
        "E2E_API_PORT": port,
    }


def seed_database() -> None:
    """Migrate the scratch database and load the golden cases into it.

    The cases are read from the copied fixture tree with the filesystem store
    and written through the database store, the same calls the worker makes for
    a processed document -- so the API serves them exactly as it would serve
    real ones.
    """
    # Imported here, after main() has set the environment: the product reads
    # its configuration at import, and must see the scratch database.
    sys.path.insert(0, str(REPO))
    from invoice_processing.adapters import FsCaseRepository  # noqa: PLC0415
    from invoice_processing.db.migrate import upgrade_to_head  # noqa: PLC0415
    from invoice_processing.db.models import Tenant, User  # noqa: PLC0415
    from invoice_processing.db.repositories import (  # noqa: PLC0415
        PgCaseRepository,
    )
    from invoice_processing.db.session import tenant_session  # noqa: PLC0415
    from invoice_processing.ports import RunContext  # noqa: PLC0415

    upgrade_to_head()
    with tenant_session(TENANT) as session:
        session.add(Tenant(id=TENANT, slug=TENANT, name="End-to-end"))
        session.flush()
        # Approval rules and delegations may only name real users of the
        # tenant, and the browser plays these people via X-Dev-Principal.
        for email, role in (
            ("dev@localhost", "admin"),
            ("alice@e2e.test", "approver"),
            ("bob@e2e.test", "approver"),
            # A delegate decides in the seat, so must be able to approve.
            ("pat@e2e.test", "approver"),
        ):
            session.add(User(tenant_id=TENANT, email=email, role=role))

    seed_purchase_order()

    source = FsCaseRepository(SCRATCH / "agent_output", tenant_id=TENANT)
    target = PgCaseRepository()
    for case_id in source.iter_case_ids(TENANT):
        ctx = RunContext(tenant_id=TENANT, case_id=case_id)
        artifacts = source.get_artifacts(ctx)
        for key, payload in artifacts.items():
            target.put_artifact(ctx, key, payload)
        target.project_case(ctx, artifacts)


def _golden_invoice() -> dict:
    path = GOLDEN / "case_005" / "02_extraction.json"
    return json.loads(path.read_text())["invoice"]


def seed_purchase_order() -> None:
    """The PO and the first, short receipt behind PO_CASE's held step 2.3."""
    from invoice_processing.db.repositories import (  # noqa: PLC0415
        PgPurchaseOrderRepository,
    )

    invoice = _golden_invoice()
    first, second = invoice["line_items"]
    repo = PgPurchaseOrderRepository()
    repo.upsert_orders(
        TENANT,
        [
            {
                "po_number": PO_NUMBER,
                "vendor_name": PO_VENDOR,
                "total_amount": invoice["invoice_total_inc_tax"],
                "currency": invoice["currency"],
                "lines": [
                    {
                        "line_number": 1,
                        "description": first["description"],
                        "qty_ordered": first["quantity"],
                        "unit_price": first["unit_price"],
                        "receipt_required": True,
                    },
                    {
                        "line_number": 2,
                        "description": second["description"],
                        "qty_ordered": second["quantity"],
                        "unit_price": second["unit_price"],
                        "receipt_required": False,
                    },
                ],
            }
        ],
        uploaded_by="seed",
    )
    repo.record_receipts(
        TENANT,
        [
            {
                "grn_number": "GRN-E2E-0",
                "po_number": PO_NUMBER,
                "received_on": "2026-10-01",
                "lines": [
                    {"line_number": 1, "qty_received": first["quantity"] - 1}
                ],
            }
        ],
        recorded_by="seed",
    )


def hold_for_receipt(case: Path) -> None:
    """Make a clone the invoice step 2.3 held against PO_NUMBER.

    The 2.3 entry is what the pipeline would have stored for the seeded PO
    and GRN-E2E-0: line 1 one unit short, line 2 a service needing no receipt.
    """
    path = case / "02_extraction.json"
    extraction = json.loads(path.read_text())
    invoice = extraction["invoice"]
    invoice["po_number"] = PO_NUMBER
    invoice["_routing"] = "REVIEW"
    invoice["_evidence"]["_summary"]["fields_needing_review"] = [
        "receipt: short_received"
    ]
    path.write_text(json.dumps(extraction, indent=2))

    first, second = invoice["line_items"]
    received = first["quantity"] - 1
    short = (
        f"Line 1 '{first['description']}': billed {first['quantity']:g}, "
        f"received {received:g} (GRN-E2E-0); 1 still to arrive."
    )
    path = case / "04_phase2_validation.json"
    phase2 = json.loads(path.read_text())
    phase2["validations"] += [
        {
            "step": "2.2",
            "rule": "Valid purchase order",
            "passed": True,
            "implemented": True,
            "outcome": "matched",
            "po_number": "poe2e1",
            "evidence": f"{PO_NUMBER} is open for {PO_VENDOR}.",
        },
        {
            "step": "2.3",
            "rule": "Invoice lines match PO lines and goods received",
            "passed": False,
            "informational": True,
            "evidence": short,
            "signals": ["short_received"],
            "lines": [
                {
                    "line_number": 1,
                    "description": first["description"],
                    "po_line_number": 1,
                    "paired_by": "description",
                    "quantity": first["quantity"],
                    "unit_price": first["unit_price"],
                    "billed": 0.0,
                    "received": received,
                    "signals": ["short_received"],
                    "evidence": short,
                },
                {
                    "line_number": 2,
                    "description": second["description"],
                    "po_line_number": 2,
                    "paired_by": "description",
                    "quantity": second["quantity"],
                    "unit_price": second["unit_price"],
                    "billed": 0.0,
                    "received": 0.0,
                    "signals": [],
                    "evidence": "",
                },
            ],
            "routing_before": "AUTO",
            "rejection_template": None,
        },
    ]
    path.write_text(json.dumps(phase2, indent=2))


def seed() -> Path:
    """A fresh scratch tree: tracked config copied in, output from the fixture."""
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    SCRATCH.mkdir(parents=True)

    # Configuration the engine needs to start at all.
    for name in (
        "reconstructed_rules_book.md",
        "rule_base.json",
        "rule_discovery_cache.json",
    ):
        source = SOURCE / name
        if source.exists():
            shutil.copy2(source, SCRATCH / name)

    # The three frozen cases stand in for "invoices that have been processed".
    # Extraction itself needs Vertex and stays out of this run.
    shutil.copytree(GOLDEN, SCRATCH / "agent_output")
    # More cases, so the approval-chain, rejection and GL-coding tests each
    # have an invoice nothing else in the run decides (a decided case is closed
    # to further approvals, and an edited one needs a second approver).
    # case_004_gl and case_004_po sort before case_005, which stays the
    # queue's last row.
    for new, vendor in (
        ("case_003", "Chain Test Supplies"),
        ("case_004", "Returned Goods Supplies"),
        ("case_004_gl", "Ledger Test Supplies"),
        (PO_CASE, PO_VENDOR),
    ):
        clone = SCRATCH / "agent_output" / new
        shutil.copytree(GOLDEN / "case_005", clone)
        for path in clone.glob("*.json"):
            path.write_text(
                path.read_text()
                .replace("case_005", new)
                .replace("QuickFix HVAC Repairs", vendor)
            )
    hold_for_receipt(SCRATCH / "agent_output" / PO_CASE)
    return SCRATCH


def drain_rematches() -> None:
    """Handle `case.rematch` messages forever; leave every other one queued.

    Filtered before pulling: a pull would lease document messages too, and
    releasing them again would still count a delivery attempt against them.
    """
    sys.path.insert(0, str(REPO))
    from invoice_processing.api import service  # noqa: PLC0415
    from invoice_processing.worker.queue import LocalQueue  # noqa: PLC0415
    from invoice_processing.worker.runner import (  # noqa: PLC0415
        REMATCH,
        Worker,
    )

    shared = service.get_queue()
    own = LocalQueue(SCRATCH / "rematch_queue")
    worker = Worker(
        own,
        claims=service.get_claim_store(worker_id="e2e-rematch"),
        audit=service.get_audit(),
    )
    # ponytail: re-reads every ready message each tick; fine for an e2e run's
    # handful, a typed subscription if this ever drains a real queue.
    while True:
        for path in sorted(shared.ready.glob("*.json")):
            try:
                record = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue  # gone, or not ours to judge
            if (record.get("payload") or {}).get("type") == REMATCH:
                path.rename(own.ready / path.name)
        worker.run_once()
        time.sleep(0.5)


def main() -> int:
    if "--rematch-worker" in sys.argv:
        drain_rematches()
        return 0
    seed()
    port = os.getenv("E2E_API_PORT", "8100")
    os.environ.update(environment(port))
    seed_database()
    drain = subprocess.Popen(
        [sys.executable, __file__, "--rematch-worker"],
        cwd=REPO,
        env=dict(os.environ),
    )
    try:
        return subprocess.call(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "invoice_processing.api.service:app",
                "--port",
                port,
            ],
            cwd=REPO,
            env=dict(os.environ),
        )
    finally:
        drain.terminate()
        drain.wait()


if __name__ == "__main__":
    raise SystemExit(main())
