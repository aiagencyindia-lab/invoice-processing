"""The validation steps, and which of them a tenant has switched off.

The catalogue is read from the rulebook rather than repeated here: a second
list of checks would drift from the one the engine runs, and a settings screen
that offers a switch for a check that no longer exists is worse than no screen.

Switching a check off is a governance decision, so two rules hold everywhere
below:

* A disabled step is recorded as **skipped**, never as passed. A skipped check
  has no verdict, and writing one down would make the audit trail assert
  something nobody verified -- which is the opposite of what this system is for.
* Step 1.1 cannot be switched off. It reports whether an invoice could be
  extracted at all; with it off there is nothing for the other checks to read,
  so the switch would not mean "skip this check" but "decide on nothing".
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Extraction success. Without it the remaining steps have no document to read.
MANDATORY = frozenset({"1.1"})


def catalogue() -> list[dict]:
    """Every step the pipeline runs, in order, as the rulebook defines them."""
    try:
        from .shared_libraries.master_data_loader import (  # noqa: PLC0415
            load_master_data,
        )

        phases = load_master_data().get_validation_phases()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Could not read the validation pipeline: %s", exc)
        phases = []

    steps: list[dict] = []
    for phase in phases or []:
        for step in phase.get("steps", []) or []:
            steps.append(
                {
                    "id": str(step.get("id", "")),
                    "name": step.get("name", ""),
                    "phase": phase.get("id", ""),
                    "rejection": step.get("rejection_template"),
                    "informational": bool(step.get("informational_only")),
                    "mandatory": str(step.get("id", "")) in MANDATORY,
                }
            )
    return [s for s in steps if s["id"]]


def known_ids() -> set[str]:
    return {s["id"] for s in catalogue()}


def disabled_from(config: dict) -> set[str]:
    """Which steps this run must skip. Mandatory ones are never included."""
    raw = config.get("disabled_checks") or []
    return {str(i) for i in raw} - set(MANDATORY)
