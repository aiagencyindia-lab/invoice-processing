"""One record per submission, from the artifacts the pipeline stored.

Reads through the case repository -- the same seam the workbench and the sheet
read -- so what the benchmark scores is what a reviewer would see.
"""

from __future__ import annotations

from .dataset import Submission

PHASES = ("phase1", "phase2", "phase3", "phase4")


def findings(artifacts: dict) -> list[dict]:
    out = []
    for phase in PHASES:
        for entry in (artifacts.get(phase) or {}).get("validations") or []:
            out.append(
                {
                    "step": entry.get("step"),
                    "passed": entry.get("passed"),
                    "informational": bool(entry.get("informational")),
                    "skipped": bool(entry.get("skipped")),
                    "evidence": entry.get("evidence"),
                    "rejection_template": entry.get("rejection_template"),
                    "outcome": entry.get("outcome"),
                }
            )
    return out


def record(
    sub: Submission,
    document_id: str,
    artifacts: dict,
    *,
    ingest_duplicate: bool,
    wall_seconds: float,
    outcome: str,
    error_text: str | None = None,
) -> dict:
    extraction = artifacts.get("extraction") or {}
    invoice = dict(extraction.get("invoice") or {})
    invoice.pop("_evidence", None)  # bulky, per-field grounding; not scored
    decision = artifacts.get("decision") or {}
    summary = (artifacts.get("audit_log") or {}).get("processing_summary") or {}
    attempts = artifacts.get("attempts") or {}
    found = findings(artifacts)

    final = decision.get("final_decision")
    if ingest_duplicate:
        final = "REJECT"
    elif not final and artifacts.get("supporting_extraction") is not None:
        # A lone PO or receipt the worker routed to a clerk to confirm
        # (worker/runner.process_supporting): decided, just not an invoice.
        final = "SUPPORTING"
    elif not final:
        final = (
            "ERROR" if (attempts.get("last_error") or error_text) else "NONE"
        )

    first_failing = next(
        (
            f["step"]
            for f in found
            if f["passed"] is False and not f["informational"]
        ),
        None,
    )
    return {
        "invoice_id": sub.invoice_id,
        "company_id": sub.company_id,
        "vendor_id": sub.vendor_id,
        "document_id": document_id,
        "submission_seq": sub.seq,
        "filename": sub.filename,
        "sha256": sub.sha256,
        "ingest_duplicate": ingest_duplicate,
        "outcome": outcome,
        "final_decision": final,
        "rejection_template": (
            "Invoice already processed"
            if ingest_duplicate
            else decision.get("rejection_template")
        ),
        "rejection_phase": decision.get("rejection_phase"),
        "first_failing_step": "3.1" if ingest_duplicate else first_failing,
        "findings": found,
        "classification": {
            "invoice_count": extraction.get("invoice_count"),
            "waf_count": extraction.get("waf_count"),
            "document_type": (
                (artifacts.get("classification") or {}).get("summary") or {}
            ).get("document_type"),
            "extraction_failed": extraction.get("extraction_failed"),
        },
        "extracted": invoice,
        "work_authorization": extraction.get("work_authorization"),
        "processing_time": summary.get("processing_time_seconds"),
        "llm_calls": summary.get("total_llm_calls"),
        "total_tokens": summary.get("total_tokens"),
        "cost_usd": summary.get("total_cost_usd"),
        "error_message": attempts.get("last_error") or error_text,
        "attempts": attempts.get("count"),
        "wall_seconds": round(wall_seconds, 3),
    }
