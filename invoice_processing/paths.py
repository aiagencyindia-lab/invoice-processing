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

from pathlib import Path

# invoice_processing/ -- the Python package itself.
AGENT_PKG_DIR = Path(__file__).resolve().parent

# The repo root: the directory containing the package, pyproject.toml and .env.
AGENT_ROOT = AGENT_PKG_DIR.parent

# Package subdirectories.
SHARED_LIBS_DIR = AGENT_PKG_DIR / "shared_libraries"
EXEMPLARY_DIR = AGENT_PKG_DIR / "exemplary_data"

# Runtime data. Everything the pipeline writes lives under DATA_DIR.
DATA_DIR = AGENT_PKG_DIR / "data"
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
MASTER_DATA_PATH = SHARED_LIBS_DIR / "invoice_master_data.yaml"

# Environment file, loaded by core.config on import.
ENV_PATH = AGENT_ROOT / ".env"
