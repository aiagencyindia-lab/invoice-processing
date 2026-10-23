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

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SOURCE = REPO / "invoice_processing" / "data"
GOLDEN = REPO / "tests" / "fixtures" / "golden" / "cases"

# Under frontend/ so `npx playwright test` owns its own workspace, and
# gitignored there.
SCRATCH = REPO / "frontend" / "e2e" / ".data"

TENANT = "e2e"

# Invoices held by step 2.3: line 1 is short one unit until a receipt.
# (case, PO, vendor, the short receipt already on file). The first is cleared
# by an imported GRN, the second by a confirmed scan, so neither test waits on
# the other.
HELD = (
    ("case_004_po", "PO-E2E-1", "Bearing Test Supplies", "GRN-E2E-0"),
    ("case_004_po2", "PO-E2E-2", "Scan Test Supplies", "GRN-E2E-20"),
)
# The scanned receipt for PO-E2E-2, its line 1 misread as 10 (1 arrived).
SCAN_PO = HELD[1][1]


def environment(port: str) -> dict[str, str]:
    return {
        "INVOICE_DATA_DIR": str(SCRATCH),
        "DATABASE_URL": f"sqlite:///{SCRATCH / 'e2e.db'}",
        # A fixed test key, so mandates can be approved and verified.
        "TRUST_MANDATE_KEY": "e2e-mandate-key",
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
    from invoice_processing import po_book  # noqa: PLC0415
    from invoice_processing.adapters import FsCaseRepository  # noqa: PLC0415
    from invoice_processing.db.migrate import upgrade_to_head  # noqa: PLC0415
    from invoice_processing.db.models import Tenant, User  # noqa: PLC0415
    from invoice_processing.db.repositories import (  # noqa: PLC0415
        PgCaseRepository,
        PgPurchaseOrderRepository,
        PgVendorRepository,
    )
    from invoice_processing.db.session import tenant_session  # noqa: PLC0415
    from invoice_processing.ports import RunContext  # noqa: PLC0415
    from invoice_processing.worker import runner  # noqa: PLC0415

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
            ("casey@e2e.test", "viewer"),
            # An admin may connect only their own sign-in domain.
            ("it@e2e-sso.test", "admin"),
        ):
            session.add(User(tenant_id=TENANT, email=email, role=role))

    for _, po_number, vendor, grn in HELD:
        seed_purchase_order(po_number, vendor, grn)
    seed_scans()

    source = FsCaseRepository(SCRATCH / "agent_output", tenant_id=TENANT)
    target = PgCaseRepository()
    for case_id in source.iter_case_ids(TENANT):
        ctx = RunContext(tenant_id=TENANT, case_id=case_id)
        artifacts = source.get_artifacts(ctx)
        for key, payload in artifacts.items():
            target.put_artifact(ctx, key, payload)
        target.project_case(ctx, artifacts)
        # The worker never runs in this stack, so code the lines here the
        # way it would: PO-line accounts first, then the tenant's rules.
        # The private worker function is used on purpose: the seed must code
        # lines exactly as the worker would (PO lines first, then rules), and a
        # hand-written artifact would drift from it.
        coded = runner._code_gl(
            ctx,
            target,
            artifacts,
            book=po_book.DbBook(PgPurchaseOrderRepository(), TENANT),
        )
        target.project_case(ctx, {**artifacts, **(coded or {})})
    # case_005's supplier in the vendor master, before seed_finding() syncs the
    # graph, so the vendor gets a node and /vendors/[id] has a trust profile.
    # The tax ID is the one the invoice prints, so check 3.2 still matches.
    PgVendorRepository().upsert_many(
        TENANT,
        [
            {
                "name": "QuickFix HVAC Repairs",
                "tax_id": "51 824 753 556",
                "kind": "service",
            }
        ],
    )
    seed_finding()


def seed_finding() -> None:
    """One cited LEAK-RATE finding on case_005, so the findings pages can be
    driven end to end without a worker or an engine run."""
    from decimal import Decimal  # noqa: PLC0415

    from sqlalchemy import select  # noqa: PLC0415

    from invoice_processing.db.models import Invoice  # noqa: PLC0415
    from invoice_processing.db.session import tenant_session  # noqa: PLC0415
    from invoice_processing.ports import RunContext  # noqa: PLC0415
    from invoice_processing.trust import leakage  # noqa: F401, PLC0415 - registers its types
    from invoice_processing.trust.decide import FakeDecider  # noqa: PLC0415
    from invoice_processing.trust.findings.model import (  # noqa: PLC0415
        Citation,
        FindingSpec,
    )
    from invoice_processing.trust.findings.store import FindingStore  # noqa: PLC0415
    from invoice_processing.trust.graph.links import LinkStore  # noqa: PLC0415
    from invoice_processing.trust.graph.store import GraphStore  # noqa: PLC0415
    from invoice_processing.trust.graph.sync import sync_tenant  # noqa: PLC0415

    ctx = RunContext(tenant_id=TENANT, case_id="case_005", actor="e2e-seed")
    graph, links = GraphStore(), LinkStore()
    sync_tenant(ctx, graph, links, decider=FakeDecider({}))
    with tenant_session(TENANT) as session:
        invoice_id = session.scalar(
            select(Invoice.id).where(
                Invoice.tenant_id == TENANT, Invoice.case_id == "case_005"
            )
        )
    node = graph.nodes_by_ref(TENANT, "invoices", [invoice_id]).get(invoice_id)
    if node is None:
        return
    FindingStore().upsert(
        ctx,
        FindingSpec(
            type="LEAK-RATE",
            transaction_node=node,
            dedupe_key=f"LEAK-RATE:invoices:{invoice_id}:1",
            impact=Decimal("1250.00"),
            currency="INR",
            confidence=0.86,
            engine_version="leakage-1",
            rule_version="contract-3.1",
            model_version="none",
            summary="Line 1 billed above the contract rate",
            details={
                "engine": "leakage",
                "hold": False,
                "mode": "advisory",
                "line": 1,
                "description": "Hex bolts",
                "quantity": "250",
                "unit_price": "55.00",
                "contract_rate": "50.00",
                "needs_review": True,
            },
            citations=(Citation(node, "Line 1: 250 x INR 55.00"),),
        ),
    )
    # Wave 3: a reality insight and a trust score, so the workbench's trust
    # panel and the finding pages render without a worker or an engine run.
    FindingStore().upsert(
        ctx,
        FindingSpec(
            type="REAL-NO-EVIDENCE",
            transaction_node=node,
            dedupe_key=f"REAL-NO-EVIDENCE:invoices:{invoice_id}",
            impact=None,
            currency="INR",
            confidence=0.6,
            engine_version="reality-0",
            rule_version="FR-23-v1",
            model_version="none",
            summary="No goods receipt or delivery corroborates the invoice",
            details={
                "engine": "reality",
                "hold": False,
                "mode": "advisory",
                "result": "NO_EVIDENCE",
                "kind": "goods",
            },
            citations=(Citation(node, "Invoice quantity 250"),),
        ),
    )
    graph.upsert_node(
        ctx,
        kind="trust_score",
        natural_key=f"trust_score:invoices:{invoice_id}",
        attrs=_seed_score(),
    )


def _seed_score() -> dict:
    """A trust_score node in the shape the score branch writes."""

    def dim(value, note, hold=False):
        return {"value": value, "hold": hold, "drivers": [], "note": note}

    return {
        "score_version": "score-1",
        "computed_at": "2026-10-21T09:00:00+00:00",
        "composite": 0.72,
        "dimensions": {
            "identity": dim(1.0, "Bank account verified."),
            "authorization": dim(0.5, "Approval pending."),
            "reality": dim(0.4, "No delivery evidence on record."),
            "price": dim(0.55, "Line 1 billed above the contract rate."),
            "authenticity": dim(0.9, "Known channel and template."),
            "consistency": dim(0.95, "Amount in line with history."),
        },
        "versions": {"engines": {"leakage": "leakage-1"}},
        "holds": [],
        "mode": {"leakage": "advisory", "reality": "advisory"},
    }


def _golden_invoice() -> dict:
    path = GOLDEN / "case_005" / "02_extraction.json"
    return json.loads(path.read_text())["invoice"]


def seed_purchase_order(po_number: str, vendor: str, grn: str) -> None:
    """The PO and the first, short receipt behind a held step 2.3."""
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
                "po_number": po_number,
                "vendor_name": vendor,
                "total_amount": invoice["invoice_total_inc_tax"],
                "currency": invoice["currency"],
                "lines": [
                    {
                        "line_number": 1,
                        "description": first["description"],
                        "qty_ordered": first["quantity"],
                        "unit_price": first["unit_price"],
                        "receipt_required": True,
                        "gl_account": "6100",
                    },
                    {
                        "line_number": 2,
                        "description": second["description"],
                        "qty_ordered": second["quantity"],
                        "unit_price": second["unit_price"],
                        "receipt_required": False,
                        "gl_account": "6200",
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
                "grn_number": grn,
                "po_number": po_number,
                "received_on": "2026-10-01",
                "lines": [
                    {"line_number": 1, "qty_received": first["quantity"] - 1}
                ],
            }
        ],
        recorded_by="seed",
    )


def _scan(
    grn: str, po_number: str | None, quantity: float, description: str
) -> dict:
    """What `supporting_docs.extract` stores for a one-line receipt whose
    quantity was read with low confidence."""
    evidence = {
        "grn_number": {
            "value": grn,
            "grounded": True,
            "page": 1,
            "confidence": 0.95,
        },
        "po_number": {
            "value": po_number,
            "grounded": True,
            "page": 1,
            "confidence": 0.95,
        },
        "lines[0].quantity_received": {
            "value": quantity,
            "grounded": False,
            "page": None,
            "confidence": 0.4,
        },
    }
    evidence["_summary"] = {
        "min_confidence": 0.4,
        "fields_needing_review": ["lines[0].quantity_received"],
        "has_text_layer": True,
    }
    return {
        "kind": "goods_receipt",
        "fields": {
            "grn_number": grn,
            "received_on": "2026-10-09",
            "po_number": po_number,
            "vendor_name": None,
            "received_by": None,
        },
        "lines": [
            {
                "line_number": 1,
                "item_code": None,
                "description": description,
                "quantity_received": quantity,
                "unit": None,
                "remarks": None,
            }
        ],
        "_evidence": evidence,
        "_repairs": [],
    }


# A scanned PO whose number was read with low confidence. Never accepted by
# the e2e (its accept request is intercepted), so it stays pending.
SCAN_ORDER = "PO-E2E-SCAN"


def _po_scan() -> dict:
    """What `supporting_docs.extract` stores for a one-line PO whose number
    was read with low confidence."""
    return {
        "kind": "purchase_order",
        "fields": {
            "po_number": SCAN_ORDER,
            "po_date": "2026-10-01",
            "valid_from": None,
            "valid_to": None,
            "vendor_name": "Scan Test Supplies",
            "vendor_tax_id": None,
            "customer_name": None,
            "currency": "INR",
            "total_amount": 1250.5,
            "amount_basis": None,
        },
        "lines": [
            {
                "line_number": 1,
                "item_code": None,
                "description": "Widgets",
                "quantity": 5,
                "unit": None,
                "unit_price": 250.1,
                "hsn_sac": None,
            }
        ],
        "_evidence": {
            "po_number": {
                "value": SCAN_ORDER,
                "grounded": False,
                "page": None,
                "confidence": 0.5,
            },
            "_summary": {
                "min_confidence": 0.5,
                "fields_needing_review": ["po_number"],
                "has_text_layer": True,
            },
        },
        "_repairs": [],
    }


def seed_scans() -> None:
    """Scanned documents as the worker leaves them, each decided by its own
    test: receipts pending against SCAN_PO, blocked on a PO not on file,
    with no PO number read, and really an invoice; a PO with a flagged
    number; and an acceptance that stored nothing (a crash between the
    claim and the store), old enough for an admin to reopen.

    Each has a stored PDF and its `supporting_extraction` artifact, so the
    review drawer's document pane serves bytes rather than a 404.
    """
    from sqlalchemy import update  # noqa: PLC0415

    from invoice_processing.api import service  # noqa: PLC0415
    from invoice_processing.db.models import SupportingDocument  # noqa: PLC0415
    from invoice_processing.db.repositories import (  # noqa: PLC0415
        PgCaseRepository,
        PgDocumentRepository,
        PgSupportingDocumentRepository,
    )
    from invoice_processing.db.session import tenant_session  # noqa: PLC0415
    from invoice_processing.ports import RunContext  # noqa: PLC0415

    description = _golden_invoice()["line_items"][0]["description"]
    documents = PgDocumentRepository()
    supporting = PgSupportingDocumentRepository()
    blobs = service.get_blobs(TENANT)

    def store(name: str, pdf: str, extracted: dict, blocked=None) -> dict:
        # Marked with its name so every scan is its own document.
        data = (
            REPO / "invoice_processing" / "exemplary_data" / pdf / "invoice.pdf"
        ).read_bytes() + f"%{name}\n".encode()
        sha = hashlib.sha256(data).hexdigest()
        key = f"documents/{sha}.pdf"
        blobs.put(key, data, "application/pdf")
        document_id = documents.create(
            TENANT,
            storage_key=key,
            sha256=sha,
            filename=f"{name.lower()}-scan.pdf",
            mime_type="application/pdf",
            byte_size=len(data),
        )
        documents.set_kind(TENANT, document_id, extracted["kind"])
        row = supporting.upsert_from_extraction(
            TENANT,
            document_id=document_id,
            kind=extracted["kind"],
            extracted=extracted,
            po_number=extracted["fields"]["po_number"],
            blocked_reason=blocked,
            extraction_error=None,
        )
        PgCaseRepository().put_artifact(
            RunContext(tenant_id=TENANT, case_id=document_id),
            "supporting_extraction",
            extracted,
        )
        return row

    for grn, po_number, pdf, blocked in (
        ("GRN-E2E-21", SCAN_PO, "case_005", None),
        (
            "GRN-E2E-22",
            "PO-NOT-YET",
            "case_001",
            "Purchase order PO-NOT-YET is not on file",
        ),
        ("GRN-E2E-23", None, "case_002", None),
        ("GRN-E2E-24", None, "case_002", None),
    ):
        store(grn, pdf, _scan(grn, po_number, 10, description), blocked)
    store(SCAN_ORDER, "case_002", _po_scan())

    orphan = store(
        "GRN-E2E-25", "case_002", _scan("GRN-E2E-25", None, 1, description)
    )
    supporting.mark(
        TENANT,
        orphan["id"],
        status="accepted",
        reviewed_by="seed",
        expect=("pending",),
    )
    # Past the window in which an acceptance may still be storing.
    with tenant_session(TENANT) as session:
        session.execute(
            update(SupportingDocument)
            .where(SupportingDocument.id == orphan["id"])
            .values(reviewed_at=datetime.now(UTC) - timedelta(hours=1))
        )


def hold_for_receipt(case: Path, po_number: str, vendor: str, grn: str) -> None:
    """Make a clone the invoice step 2.3 held against `po_number`.

    The 2.3 entry is what the pipeline would have stored for the seeded PO
    and `grn`: line 1 one unit short, line 2 a service needing no receipt.
    """
    path = case / "02_extraction.json"
    extraction = json.loads(path.read_text())
    invoice = extraction["invoice"]
    invoice["po_number"] = po_number
    invoice["_routing"] = "REVIEW"
    invoice["_evidence"]["_summary"]["fields_needing_review"] = [
        "receipt: short_received"
    ]
    path.write_text(json.dumps(extraction, indent=2))

    first, second = invoice["line_items"]
    received = first["quantity"] - 1
    short = (
        f"Line 1 '{first['description']}': billed {first['quantity']:g}, "
        f"received {received:g} ({grn}); 1 still to arrive."
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
            "po_number": po_number.replace("-", "").lower(),
            "evidence": f"{po_number} is open for {vendor}.",
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
    # case_004_gl and the held cases sort before case_005, which stays the
    # queue's last row.
    for new, vendor in (
        ("case_003", "Chain Test Supplies"),
        ("case_004", "Returned Goods Supplies"),
        ("case_004_gl", "Ledger Test Supplies"),
        *((case, vendor) for case, _, vendor, _ in HELD),
    ):
        clone = SCRATCH / "agent_output" / new
        shutil.copytree(GOLDEN / "case_005", clone)
        for path in clone.glob("*.json"):
            path.write_text(
                path.read_text()
                .replace("case_005", new)
                .replace("QuickFix HVAC Repairs", vendor)
            )
    for case, po_number, vendor, grn in HELD:
        hold_for_receipt(
            SCRATCH / "agent_output" / case, po_number, vendor, grn
        )
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
