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
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SOURCE = REPO / "invoice_processing" / "data"
GOLDEN = REPO / "tests" / "fixtures" / "golden" / "cases"

# Under frontend/ so `npx playwright test` owns its own workspace, and
# gitignored there.
SCRATCH = REPO / "frontend" / "e2e" / ".data"

TENANT = "e2e"


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

    source = FsCaseRepository(SCRATCH / "agent_output", tenant_id=TENANT)
    target = PgCaseRepository()
    for case_id in source.iter_case_ids(TENANT):
        ctx = RunContext(tenant_id=TENANT, case_id=case_id)
        artifacts = source.get_artifacts(ctx)
        for key, payload in artifacts.items():
            target.put_artifact(ctx, key, payload)
        target.project_case(ctx, artifacts)


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
    # Two more cases, so the approval-chain and rejection tests each have an
    # invoice nothing else in the run decides (a decided case is closed to
    # further approvals, and an edited one needs a second approver).
    for new, vendor in (
        ("case_003", "Chain Test Supplies"),
        ("case_004", "Returned Goods Supplies"),
    ):
        clone = SCRATCH / "agent_output" / new
        shutil.copytree(GOLDEN / "case_005", clone)
        for path in clone.glob("*.json"):
            path.write_text(
                path.read_text()
                .replace("case_005", new)
                .replace("QuickFix HVAC Repairs", vendor)
            )
    return SCRATCH


def main() -> int:
    seed()
    port = os.getenv("E2E_API_PORT", "8100")
    os.environ.update(environment(port))
    seed_database()
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


if __name__ == "__main__":
    raise SystemExit(main())
