"""Apply a tenant's correction rules to a finished case.

This is the half of the learning loop that was missing. The rules an expert
teaches were validated, impact-assessed and written to a file, and no deployed
process ever read them: the worker ran the pipeline and stored what it
produced, and `ALFEngine` was reachable only from the ADK chat agent, which
reads the packaged sample folder and is not what the Docker image runs. So a
rule could be approved and change nothing.

Deliberately a pure function over dictionaries -- no queue, no database, no
temp directory -- for the same reason `case_projection` is: this decides what
a customer gets paid, and it should be testable by handing it two dicts.

Two modes, because arming rules on a live ledger sight unseen is not something
to do on a Tuesday:

* **shadow** (the default) evaluates every rule and records what each *would*
  have changed. The decision is untouched. This is the evidence for whether
  the rules are any good, gathered on real invoices before they can cost
  anything.
* **binding** applies them, and the corrected decision is what the sheet, the
  queue and the export all see.

Either way the `alf` artifact is the explanation: which rule fired, on which
conditions, and what the decision was before it did.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

logger = logging.getLogger(__name__)

# The artifact the pipeline writes its final answer into, and the one the
# projector reads a decision out of.
OUTPUT_ARTIFACT = "postprocessing"
# The same answer before any rule touched it. Without this a revised decision
# cannot be described, let alone undone: "the rule changed it" is only half a
# sentence without "from what".
PRE_ALF_ARTIFACT = "postprocessing_pre_alf"
AUDIT_ARTIFACT = "alf"


def status_of(output: dict | None) -> str | None:
    """The invoice status inside a postprocessing document, if it has one."""
    return ((output or {}).get("Invoice Processing") or {}).get(
        "Invoice Status"
    )


def run(
    artifacts: dict,
    rules_doc: dict | None,
    *,
    binding: bool = False,
    rule_base_version: int | None = None,
) -> dict[str, Any]:
    """Evaluate a tenant's rules against one finished case.

    Returns the artifacts to store -- an empty dict when there is nothing to
    say, so a tenant with no rules pays nothing for this beyond one function
    call and writes no artifact claiming an evaluation happened.
    """
    from .context import build_case_context  # noqa: PLC0415
    from .shared_libraries.alf_engine import ALFEngine  # noqa: PLC0415

    rules = (rules_doc or {}).get("rules") or []
    if not rules:
        return {}

    provisional = artifacts.get(OUTPUT_ARTIFACT) or {}
    if not provisional:
        # Nothing to correct. A case that never reached an output has bigger
        # problems than a missing exemption.
        return {}

    engine = ALFEngine()
    engine.load_document(rules_doc)

    # The same builder the impact assessor uses. That is the whole reason an
    # impact report can be believed: the corpus a rule was measured against
    # and the case it runs on are described in identical terms.
    context = build_case_context(artifacts)
    revised, audit_log = engine.evaluate(copy.deepcopy(provisional), context)

    before = status_of(provisional)
    after = status_of(revised)
    audit = {
        **audit_log,
        "mode": "binding" if binding else "shadow",
        "rule_base_version": rule_base_version,
        "status_before": before,
        "status_after": after,
        # Said plainly, because "shadow" and "no rule matched" look identical
        # in a decision that did not change.
        "changed": bool(binding and after != before),
        "would_change": bool(after != before),
    }

    written: dict[str, Any] = {AUDIT_ARTIFACT: audit}
    if binding and revised != provisional:
        written[PRE_ALF_ARTIFACT] = provisional
        written[OUTPUT_ARTIFACT] = revised
    return written
