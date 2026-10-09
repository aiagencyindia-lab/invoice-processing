"""Re-match an invoice step 2.3 held once a goods receipt arrives.

No LLM call and no pipeline run: the stored extraction is matched again with
the pipeline's own step-2.3 code (`three_way_entry`), and only step 2.3 and
the `receipt:` review entries are rewritten. The hold clears only when the
match passes and nothing else -- a low-confidence field, an arithmetic or
bank flag -- still needs review. A decided case is never touched.
"""

from __future__ import annotations

import copy

from . import po_matching

RECEIPT = "receipt: "


def _three_way(phase2) -> dict | None:
    for entry in (phase2 or {}).get("validations") or []:
        if entry.get("step") == "2.3":
            return entry
    return None


def is_held_by_receipt(artifacts: dict, po_number) -> bool:
    """Undecided, quoting this PO, and step 2.3 did not pass."""
    if (artifacts.get("workflow") or {}).get("decision"):
        return False
    invoice = (artifacts.get("extraction") or {}).get("invoice") or {}
    key = po_matching.normalize_po_number(po_number)
    if (
        not key
        or po_matching.normalize_po_number(invoice.get("po_number")) != key
    ):
        return False
    entry = _three_way(artifacts.get("phase2"))
    return entry is not None and entry.get("passed") is False


def rematch_case(ctx, cases, book, *, vendors, config: dict, audit) -> dict:
    """Match the stored invoice again; {"before", "after", "cleared"}.

    Raises after auditing `three_way_rematch_failed`, so the worker retries;
    the case stays held meanwhile.
    """
    from .api import service  # noqa: PLC0415
    from .shared_libraries.acting import (  # noqa: PLC0415
        general_invoice_agent as agent,
    )

    po_number = None
    try:
        artifacts = cases.get_artifacts(ctx)
        # ponytail: checked once, not under the row lock; a decision landing
        # mid-rematch is caught by the approval-time re-match.
        if (artifacts.get("workflow") or {}).get("decision"):
            return {"skipped": "decided"}
        invoice = (artifacts.get("extraction") or {}).get("invoice") or {}
        po_number = invoice.get("po_number")
        entry = agent.three_way_entry(invoice, book, config, vendors)
        old: dict = {}

        # Deep copies: a nested edit in place is invisible to the JSON
        # column's change detection, and the write would silently not land.
        def rewrite_phase2(current):
            current = copy.deepcopy(current or {})
            validations = list(current.get("validations") or [])
            for i, v in enumerate(validations):
                if v.get("step") == "2.3":
                    old.update(v)
                    validations[i] = entry
                    break
            else:
                validations.append(entry)
            if not entry["passed"]:
                entry["routing_before"] = old.get("routing_before", "AUTO")
            current["validations"] = validations
            return current

        cases.update_artifact(ctx, "phase2", rewrite_phase2)
        cleared = False

        def rewrite_extraction(current):
            nonlocal cleared
            current = copy.deepcopy(current or {})
            inv = current.setdefault("invoice", {})
            summary = inv.setdefault("_evidence", {}).setdefault("_summary", {})
            needing = [
                f
                for f in summary.get("fields_needing_review") or []
                if not str(f).startswith(RECEIPT)
            ]
            if entry["passed"]:
                if not needing:
                    inv["_routing"] = old.get("routing_before") or "AUTO"
                    cleared = True
            else:
                needing += [f"{RECEIPT}{s}" for s in entry["signals"]]
                inv["_routing"] = "REVIEW"
            summary["fields_needing_review"] = needing
            return current

        cases.update_artifact(ctx, "extraction", rewrite_extraction)
        fresh = cases.get_artifacts(ctx)
        cases.project_case(ctx, fresh)
        service.reroute(ctx, fresh, ["receipt"])
        out = {
            "before": list(old.get("signals") or []),
            "after": list(entry["signals"]),
            "cleared": cleared,
        }
        audit.emit(ctx, "three_way_rematched", {"po_number": po_number, **out})
        return out
    except Exception as exc:
        audit.emit(
            ctx,
            "three_way_rematch_failed",
            {"po_number": po_number, "error": str(exc)},
        )
        raise
