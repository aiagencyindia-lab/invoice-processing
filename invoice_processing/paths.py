"""Single source of truth for filesystem paths.

Six modules previously computed these constants independently, and four of them
derived the repo root as ``AGENT_PKG_DIR.parent.parent.parent`` -- correct only
while the agent lived three directories deep inside the adk-samples repo. Once
the agent moved, that expression resolved *above* the repo, so ``.env`` was
never found and every dotenv load silently did nothing.

Centralizing them means a future move breaks one line, not six, and the
duplicated roots can no longer drift apart.

Phase 0.5 replaces this module with settings injected through a RunContext, at
which point path resolution stops being global state. Until then this at least
guarantees every module agrees on where things are.
"""

import os
from pathlib import Path

# invoice_processing/ -- the Python package itself.
AGENT_PKG_DIR = Path(__file__).resolve().parent

# The repo root: the directory containing the package, pyproject.toml and .env.
AGENT_ROOT = AGENT_PKG_DIR.parent

# Package subdirectories.
SHARED_LIBS_DIR = AGENT_PKG_DIR / "shared_libraries"
EXEMPLARY_DIR = AGENT_PKG_DIR / "exemplary_data"

# Runtime data. Everything the pipeline writes lives under DATA_DIR.
#
# `INVOICE_DATA_DIR` relocates the whole tree. It exists so a test harness can
# run the real service against a scratch copy instead of the developer's own
# invoices -- three separate test suites have written into live data because
# there was no way to point them elsewhere. It is equally the hook for a
# mounted volume in a container.
#
# The directory holds tracked configuration as well as output (the rules book,
# the rule base), so a relocated tree must be seeded from this one rather than
# created empty.
DATA_DIR = Path(os.getenv("INVOICE_DATA_DIR") or (AGENT_PKG_DIR / "data"))


def state_dir() -> Path:
    """Where durable runtime state lives: uploaded documents, the local job
    queue and the worker heartbeat.

    `STATE_DIR` -- a mounted volume in production (Railway: `/data`) -- when
    set; otherwise `DATA_DIR`, as before. Tracked files (the rules book, the
    rule base) stay under `DATA_DIR`, in the image, so a fresh volume does
    not hide them. Read at call time, like the callers' `DATA_DIR`.
    """
    raw = os.getenv("STATE_DIR", "").strip()
    return Path(raw) if raw else DATA_DIR


AGENTIC_FLOW_OUT = DATA_DIR / "agent_output"
ALF_OUT_DIR = DATA_DIR / "alf_output"
INVESTIGATION_OUT_DIR = DATA_DIR / "investigation_output"
EVAL_RESULTS_DIR = DATA_DIR / "eval_results"
SESSIONS_DIR = DATA_DIR / "learning_sessions"

# Configuration files.
RULE_BASE_PATH = DATA_DIR / "rule_base.json"
RULES_BOOK_PATH = DATA_DIR / "reconstructed_rules_book.md"
RULE_DISCOVERY_CACHE_PATH = DATA_DIR / "rule_discovery_cache.json"
ITEM_CODE_CACHE_PATH = DATA_DIR / "item_code_cache.json"

# Open purchase orders, for the Step 2.2 two-way match. A pilot drops an ERP
# export here; the connector replaces the file without the matching logic
# changing. Absent means no POs are known, which surfaces as "not found" rather
# than as a silent pass.
# Legacy shared location. Kept so an existing single-tenant setup keeps
# working, but every new write goes to the per-tenant path below.
PURCHASE_ORDER_FILE = DATA_DIR / "purchase_orders.json"
PURCHASE_ORDER_DIR = DATA_DIR / "purchase_orders"

# Reference data a tenant loads so the checks have something to check against.
# Same shape as purchase orders and same reason for being per-tenant: these
# name a customer's suppliers, their tax registrations and what work they have
# authorised, and one shared file would disclose all three between tenants.
VENDOR_MASTER_DIR = DATA_DIR / "vendors"
WORK_AUTHORIZATION_DIR = DATA_DIR / "work_authorizations"


def tenant_file(directory: Path, tenant_id: str) -> Path:
    """`<directory>/<tenant>.json`, with the tenant id made filename-safe.

    A tenant id reaches this from a signed token, but it still ends up in a
    path, and a path built from an identifier without sanitising it is how a
    directory traversal starts.
    """
    safe = "".join(
        c if c.isalnum() or c in "-_" else "_" for c in str(tenant_id or "")
    )
    return directory / f"{safe or 'default'}.json"


def _durable(directory: Path, name: str) -> Path:
    """A reference-data directory: under `STATE_DIR` when set (a volume;
    these files are written at run time and must survive a deploy), else
    `directory`, as before."""
    raw = os.getenv("STATE_DIR", "").strip()
    return Path(raw) / name if raw else directory


def vendor_master_file(tenant_id: str) -> Path:
    """This tenant's vendors: names and tax registrations.

    Step 3.2 needs it to answer the half of the question a checksum cannot:
    this identifier is well-formed, but is it *this supplier's*?
    """
    return tenant_file(_durable(VENDOR_MASTER_DIR, "vendors"), tenant_id)


def work_authorization_file(tenant_id: str) -> Path:
    """This tenant's work authorizations: who may bill what, at what rate."""
    return tenant_file(
        _durable(WORK_AUTHORIZATION_DIR, "work_authorizations"), tenant_id
    )


def purchase_order_dir() -> Path:
    return _durable(PURCHASE_ORDER_DIR, "purchase_orders")


def purchase_order_file(tenant_id: str):
    """The purchase-order source for one tenant.

    Purchase orders name suppliers and commitment amounts, so a single shared
    file across tenants is a disclosure: one customer would see another's
    supplier list, and could overwrite it. Scoped per tenant from the start.

    Falls back to the legacy shared file when a tenant has none of its own, so
    an existing local setup keeps matching without being migrated.
    """
    scoped = tenant_file(purchase_order_dir(), tenant_id)
    if scoped.exists() or not PURCHASE_ORDER_FILE.exists():
        return scoped
    return PURCHASE_ORDER_FILE


MASTER_DATA_PATH = SHARED_LIBS_DIR / "invoice_master_data.yaml"

# Environment file, loaded by core.config on import.
ENV_PATH = AGENT_ROOT / ".env"
