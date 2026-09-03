"""
Function tools for the Invoice Processing agent.

Inference only. The learning tools lived here -- load a case, draft a rule,
assess its impact, write it -- and they are gone because the product has them
now: a reviewer overrules a decision, records it as policy, and approves the
rule that comes back on a screen showing what else it would change.

What is left is what the ADK agent is still good for: running the pipeline
over a local sample case while working on the pipeline.
"""

import sys

from ..paths import AGENT_ROOT, EXEMPLARY_DIR, SHARED_LIBS_DIR

# shared_libraries modules import each other by bare name; see core.config.
sys.path.insert(0, str(AGENT_ROOT))
sys.path.insert(0, str(SHARED_LIBS_DIR))


def list_inference_cases() -> list[str]:
    """List all case IDs available for inference processing.

    Scans the exemplary_data/ directory for case folders that contain
    at least one PDF file (the input for the acting agent).

    Returns:
        Sorted list of case IDs (e.g., ["case_001", "case_002", ...]).
    """
    if not EXEMPLARY_DIR.exists():
        return []

    cases = []
    for folder in sorted(EXEMPLARY_DIR.iterdir()):
        if folder.is_dir() and any(folder.glob("*.pdf")):
            cases.append(folder.name)
    return cases


# ===========================================================================
# LEARNING TOOLS
# ===========================================================================
