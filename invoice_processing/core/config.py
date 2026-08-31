"""
Configuration for the Invoice Processing learning core.

All paths resolved relative to this file's location.
LLM config loaded from shared .env file.
"""

import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Directory layout
# ---------------------------------------------------------------------------
# Re-exported for the core modules that import these from here
# (case_loader, rule_writer, rule_discoverer, session_logger, tools).
from ..paths import (  # noqa: F401
    AGENT_PKG_DIR,
    AGENT_ROOT,
    AGENTIC_FLOW_OUT,
    ALF_OUT_DIR,
    DATA_DIR,
    ENV_PATH,
    RULE_BASE_PATH,
    RULES_BOOK_PATH,
    SESSIONS_DIR,
    SHARED_LIBS_DIR,
)

CORE_DIR = Path(__file__).resolve().parent  # invoice_processing/core/

# Legacy aliases (for backward compatibility within modules)
LEARNING_AGENT_DIR = AGENT_PKG_DIR
PROJECT_ROOT = AGENT_ROOT

# ---------------------------------------------------------------------------
# Ensure shared_libraries is importable
# ---------------------------------------------------------------------------

# shared_libraries modules import each other by bare name (e.g.
# `from master_data_loader import ...`), so that directory must be on the path.
# Phase 0.5 converts those to package imports and this hack goes away.
_shared_libs_str = str(SHARED_LIBS_DIR)
if _shared_libs_str not in sys.path:
    sys.path.insert(0, _shared_libs_str)

# ---------------------------------------------------------------------------
# .env loading
# ---------------------------------------------------------------------------

try:
    from dotenv import load_dotenv

    load_dotenv(ENV_PATH)
except ImportError:
    pass  # dotenv not installed; assume env vars are set externally

# ---------------------------------------------------------------------------
# LLM configuration (mirrors acting_agent / alf_engine.py)
# ---------------------------------------------------------------------------


def get_llm_project_id():
    """Return PROJECT_ID from env (deferred for Agent Engine compatibility).

    Checks multiple env var names to match the inference path pattern
    and falls back to google.auth.default() for Agent Engine containers.
    """
    project = (
        os.getenv("PROJECT_ID")
        or os.getenv("GOOGLE_CLOUD_PROJECT")
        or os.getenv("GOOGLE_CLOUD_PROJECT_ID")
        or os.getenv("GCP_PROJECT")
    )
    if not project:
        try:
            import google.auth  # noqa: PLC0415

            _, project = google.auth.default()
        except Exception:
            pass
    return project


def get_llm_location():
    """Return LOCATION from env (deferred for Agent Engine compatibility)."""
    return os.getenv("LOCATION") or os.getenv(
        "GOOGLE_CLOUD_REGION", "us-central1"
    )


def get_llm_model():
    """Return GEMINI_PRO_MODEL from env (deferred for Agent Engine compatibility)."""
    return os.getenv("GEMINI_PRO_MODEL", "gemini-2.5-pro")


def get_llm_call_delay():
    """Return API_CALL_DELAY_SECONDS from env (deferred for Agent Engine compatibility)."""
    return float(os.getenv("API_CALL_DELAY_SECONDS", "1.0"))


# ---------------------------------------------------------------------------
# Master data loader (optional — provides domain-agnostic configuration)
# ---------------------------------------------------------------------------

try:
    from ..shared_libraries.master_data_loader import load_master_data

    _master = load_master_data()
    _MASTER_DATA = _master
except Exception:
    _MASTER_DATA = None

# ---------------------------------------------------------------------------
# Artifact file mapping (agent output folder -> context key)
# Reads from master data if available, else uses hardcoded defaults.
# ---------------------------------------------------------------------------

if _MASTER_DATA and _MASTER_DATA.get_agent_file_map():
    _md_map = _MASTER_DATA.get_agent_file_map()
    ARTIFACT_MAP = {}
    for key, filename in _md_map.items():
        # Normalize master data keys to match what case_loader expects
        if key == "final_decision":
            ARTIFACT_MAP["decision"] = filename
        elif key == "transformation":
            ARTIFACT_MAP["transformer"] = filename
        else:
            ARTIFACT_MAP[key] = filename
else:
    ARTIFACT_MAP = {
        "classification": "01_classification.json",
        "extraction": "02_extraction.json",
        "phase1": "03_phase1_validation.json",
        "phase2": "04_phase2_validation.json",
        "phase3": "05_phase3_validation.json",
        "phase4": "06_phase4_validation.json",
        "transformer": "07_transformation.json",
        "decision": "08_decision.json",
        "audit_log": "09_audit_log.json",
        "postprocessing": "Postprocessing_Data.json",
    }

# ---------------------------------------------------------------------------
# Terminal formatting helpers
# ---------------------------------------------------------------------------


class Color:
    """ANSI color codes for terminal output."""

    BOLD = "\033[1m"
    DIM = "\033[2m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    MAGENTA = "\033[95m"
    BLUE = "\033[94m"
    RESET = "\033[0m"
