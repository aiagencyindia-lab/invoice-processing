"""Start the API against a scratch data tree, for the end-to-end run.

The browser test drives the real service, which writes: it approves invoices,
records corrections, saves settings and stores uploads. Pointed at the normal
data directory it would do all of that to the developer's own invoices -- three
test suites have already done exactly that, which is why `INVOICE_DATA_DIR`
exists.

So this seeds a throwaway tree and execs uvicorn against it. Seeded, not empty:
the data directory holds tracked configuration as well as output, and an empty
one leaves the engine with no rules book.
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
    return SCRATCH


def main() -> int:
    seed()
    port = os.getenv("E2E_API_PORT", "8100")
    env = {
        **os.environ,
        "INVOICE_DATA_DIR": str(SCRATCH),
        # The browser cannot sign in to Identity Platform unattended, and the
        # sign-in gate itself is covered by tests/test_frontend_reaches_the_flow.py.
        "AUTH_DISABLED": "1",
        "ENV": "local",
        "AUTH_DEV_TENANT": "e2e",
        # Never the real bucket, database or queue.
        "GCS_BUCKET": "",
        "CLOUD_SQL_INSTANCE": "",
        "DATABASE_URL": "",
        "PUBSUB_TOPIC": "",
    }
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
        env=env,
    )


if __name__ == "__main__":
    raise SystemExit(main())
