"""Re-match an invoice step 2.3 held once a goods receipt arrives.

No LLM call and no pipeline run: the stored extraction is matched again with
the pipeline's own step-2.3 code (`three_way_entry`), and only step 2.3 and
the `receipt:` review entries are rewritten. The hold clears only when the
match passes and nothing else -- a low-confidence field, an arithmetic or
bank flag -- still needs review. A decided case is never touched.
"""

from __future__ import annotations

from . import po_matching

RECEIPT = "receipt: "


def three_way_validation(phase2) -> dict | None:
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
    entry = three_way_validation(artifacts.get("phase2"))
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
        if (artifacts.get("workflow") or {}).get("decision"):
            return {"skipped": "decided"}
        invoice = (artifacts.get("extraction") or {}).get("invoice") or {}
        po_number = invoice.get("po_number")
        # Matched outside the lock: the book is another table, and holding
        # the case row while reading it would serialise every receipt.
        entry = agent.three_way_entry(invoice, book, config, vendors)
        out: dict = {}

        def rewrite(current: dict) -> dict | None:
            # Checked again under the lock: a decided case is never touched.
            if (current.get("workflow") or {}).get("decision"):
                return None
            phase2 = current.get("phase2") or {}
            validations = list(phase2.get("validations") or [])
            old = three_way_validation(phase2) or {}
            new = dict(entry)
            # Carried forward even on a pass, so the routing the invoice had
            # before 2.3 first held it is never lost to a later re-match.
            if "routing_before" in old:
                new["routing_before"] = old["routing_before"]
            elif not new["passed"]:
                new["routing_before"] = "AUTO"
            i = next(
                (
                    i
                    for i, v in enumerate(validations)
                    if v.get("step") == "2.3"
                ),
                None,
            )
            if i is None:
                validations.append(new)
            else:
                validations[i] = new

            extraction = current.get("extraction") or {}
            inv = extraction.setdefault("invoice", {})
            summary = inv.setdefault("_evidence", {}).setdefault("_summary", {})
            needing = [
                f
                for f in summary.get("fields_needing_review") or []
                if not str(f).startswith(RECEIPT)
            ]
            # Only this run lifting a 2.3 hold restores routing: a repeat on
            # an already-passing entry leaves it as it is.
            cleared = (
                old.get("passed") is False and new["passed"] and not needing
            )
            if cleared:
                inv["_routing"] = old.get("routing_before") or "AUTO"
            elif not new["passed"]:
                needing += [f"{RECEIPT}{s}" for s in new["signals"]]
                inv["_routing"] = "REVIEW"
            summary["fields_needing_review"] = needing
            out.update(
                before=list(old.get("signals") or []),
                after=list(new["signals"]),
                cleared=cleared,
            )
            return {
                "phase2": {**phase2, "validations": validations},
                "extraction": extraction,
            }

        if cases.update_artifacts(ctx, rewrite) is None:
            return {"skipped": "decided"}
        fresh = cases.get_artifacts(ctx)
        cases.project_case(ctx, fresh)
        service.reroute(ctx, fresh, ["receipt"])
        audit.emit(ctx, "three_way_rematched", {"po_number": po_number, **out})
        return out
    except Exception as exc:
        audit.emit(
            ctx,
            "three_way_rematch_failed",
            {"po_number": po_number, "error": str(exc)},
        )
        raise
