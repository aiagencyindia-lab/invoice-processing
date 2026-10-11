"""Reads a finished case's artifacts into the shapes the database stores.

The pipeline's output is ten JSON artifacts. The relational model exists so a
decision can be *queried* -- "which invoices from this vendor are awaiting
review", "which step rejected this one", "what did that run cost" -- and none
of those questions can be asked of a JSON blob without loading every case.

This module is the one place that knows how to read an artifact set. Both the
API and the repository go through it, so a change to the pipeline's output
shape breaks one file rather than drifting silently between two readers.

Nothing here touches a database or raises on a malformed case: a projection is
a convenience over the artifacts, and the artifacts remain the record.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .gl_coding import valid_entries


# Money and confidence are Numeric columns. Floats are converted through their
# string form -- Decimal(0.1) is 0.1000000000000000055511151231257827, and a
# total that arrives a fraction of a cent off reconciles against nothing.
def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    # The transformer formats amounts for a person: "1,000.00". Passing that
    # to Decimal raises, and the column ended up NULL -- so on a live invoice
    # every line under a thousand stored correctly and the one line over it
    # did not, which is the kind of gap nobody notices until a total is wrong.
    for junk in (",", " ", "\u00a0", "$", "\u20ac", "\u00a3", "\u20b9"):
        text = text.replace(junk, "")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]  # accounting negatives
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError, TypeError):
        return None


def _text(value: Any, limit: int) -> str | None:
    """Trim to the column width rather than letting the insert fail.

    A vendor address longer than the column is a reason to store a shortened
    address, not a reason to lose the whole projection of the case.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit]


def extraction(artifacts: dict) -> dict:
    return (artifacts.get("extraction") or {}).get("invoice") or {}


# The machine and the person do not speak the same language: the pipeline says
# ACCEPT/REJECT/SET_ASIDE and a reviewer says approve/reject. Until now nothing
# in the system had ever put the two side by side, so a reviewer overruling a
# validation was indistinguishable from one agreeing with it -- and the single
# most useful thing a reviewer produces was discarded at the moment they
# produced it.
_ACCEPTED = {"ACCEPT", "ACCEPTED", "PENDING PAYMENT", "APPROVED"}
_REJECTED = {"REJECT", "REJECTED"}


def classify_verdict(text) -> str:
    """accept | reject | undecided, from whatever the pipeline wrote."""
    verdict = str(text or "").strip().upper()
    if verdict in _ACCEPTED:
        return "accept"
    if verdict in _REJECTED:
        return "reject"
    # SET_ASIDE, "To Verify", an unfinished pipeline: nothing to disagree with.
    return "undecided"


def machine_verdict(artifacts: dict) -> str:
    """What the pipeline decided: accept | reject | undecided.

    Same precedence `invoice_columns` uses -- the decision artifact first, the
    postprocessing status second -- so the two can never disagree about what
    the machine said.
    """
    decision = artifacts.get("decision") or {}
    postprocessing = (artifacts.get("postprocessing") or {}).get(
        "Invoice Processing"
    ) or {}
    return classify_verdict(
        decision.get("final_decision")
        # The flattened rule-evaluation context spells it this way. Both
        # are read here so one answer to "what did the machine decide"
        # serves the sheet, the disagreement column and the impact report.
        or decision.get("decision")
        or postprocessing.get("Invoice Status")
    )


def agreement(artifacts: dict) -> str | None:
    """Did the person agree with the machine? None until someone decides.

    `exemption` is the valuable one: the rulebook rejected an invoice a human
    then approved, which is precisely the case ALF rules exist for.
    `too_permissive` is the expensive one -- the machine would have paid it.
    """
    human = (artifacts.get("workflow") or {}).get("decision")
    if human not in ("approve", "reject"):
        return None

    verdict = machine_verdict(artifacts)
    if verdict == "reject":
        return "agree" if human == "reject" else "exemption"
    if verdict == "accept":
        return "agree" if human == "approve" else "too_permissive"
    # Routed to a human because the pipeline would not decide. Them deciding
    # it is the system working, not a disagreement with it.
    return "agree"


def parse_timestamp(value) -> datetime | None:
    """An ISO timestamp as an aware datetime, or None.

    Tolerant on purpose: the workflow artifact was written by several
    versions of this code, some with a `Z`, some naive. Garbage is None, not
    an exception -- a KPI must never stop a projection.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def decided_at(artifacts: dict) -> datetime | None:
    """When a person decided this invoice; None if nobody has."""
    workflow = artifacts.get("workflow") or {}
    if workflow.get("decision") not in ("approve", "reject"):
        return None
    return parse_timestamp(workflow.get("decided_at"))


def is_touchless(artifacts: dict) -> bool:
    """Nothing asked for a person: confident extraction, the rulebook
    accepted, and receiving did not hold it."""
    if extraction(artifacts).get("_routing") != "AUTO":
        return False
    if machine_verdict(artifacts) != "accept":
        return False
    return not any(
        f["step"] == "2.3" and f["passed"] is False for f in findings(artifacts)
    )


def invoice_columns(artifacts: dict) -> dict:
    """The typed columns of one invoice row.

    Every one of these was NULL in production: the artifacts were written to
    the `fields` JSON and nothing else, so `ix_invoices_dupe` indexed nothing
    but nulls and the review queue had to be sorted in Python after loading
    every case in the tenant.
    """
    invoice = extraction(artifacts)
    evidence = (invoice.get("_evidence") or {}).get("_summary") or {}
    decision = artifacts.get("decision") or {}
    postprocessing = (artifacts.get("postprocessing") or {}).get(
        "Invoice Processing"
    ) or {}

    verdict = decision.get("final_decision") or postprocessing.get(
        "Invoice Status"
    )

    return {
        "invoice_number": _text(invoice.get("invoice_number"), 128),
        "invoice_date": _text(invoice.get("invoice_date"), 32),
        "due_date": _text(invoice.get("due_date"), 32),
        "po_number": _text(invoice.get("po_number"), 128),
        "payment_terms": _text(invoice.get("payment_terms"), 255),
        "currency": _text(invoice.get("currency"), 8),
        "vendor_name": _text(invoice.get("vendor_name"), 512),
        "vendor_tax_id": _text(invoice.get("vendor_tax_id"), 64),
        "vendor_address": _text(invoice.get("vendor_address"), 4000),
        "total_ex_tax": _decimal(invoice.get("invoice_total_ex_tax")),
        "tax_amount": _decimal(invoice.get("tax_amount")),
        "total_inc_tax": _decimal(invoice.get("invoice_total_inc_tax")),
        "bank_account_name": _text(invoice.get("bank_account_name"), 255),
        "bank_account_number": _text(invoice.get("bank_account_number"), 64),
        "bank_code": _text(invoice.get("bank_code"), 64),
        "decision": _text(verdict, 32),
        "decision_reason": _text(
            decision.get("rejection_template")
            or decision.get("rejection_reason"),
            4000,
        ),
        "routing": _text(invoice.get("_routing"), 16),
        "min_confidence": _decimal(evidence.get("min_confidence")),
        # "processed" means the pipeline reached a decision, not that anyone
        # approved it -- approval lives in the workflow artifact and the audit
        # trail. Without this every row stayed "received" forever.
        "status": "processed" if verdict else "received",
        **_human_columns(artifacts),
    }


def _human_columns(artifacts: dict) -> dict:
    """The reviewer's decision, or nothing at all.

    Omitted rather than nulled when no one has decided, because the worker
    reprojects from the pipeline's own output files -- which never contain the
    workflow artifact -- and a dict of Nones would wipe a recorded decision
    every time its document was retried.
    """
    workflow = artifacts.get("workflow") or {}
    if workflow.get("decision") not in ("approve", "reject"):
        return {}
    return {
        "human_decision": _text(workflow.get("decision"), 16),
        "decision_agreement": _text(agreement(artifacts), 24),
        "decision_category": _text(workflow.get("reason_category"), 32),
    }


def lines(artifacts: dict) -> list[dict]:
    """Line items, preferring the transformer's mapping.

    The transformer adds the line number and item code the extractor does not
    produce; its amounts are strings, so everything goes through `_decimal`.
    Falling back to the raw extraction means a case that was rejected before
    the transformer ran still has its lines.
    """
    return _with_gl_coding(_lines(artifacts), artifacts.get("gl_coding"))


def _with_gl_coding(rows: list[dict], coding) -> list[dict]:
    """A coded account (rule or person) wins over the transformer's guess.

    A malformed artifact is ignored rather than losing the projection.
    """
    coded = {
        entry["line_number"]: _text(entry.get("gl_account"), 64)
        for entry in valid_entries(coding)
    }
    for row in rows:
        row["gl_account"] = coded.get(row["line_number"]) or row["gl_account"]
    return rows


def _lines(artifacts: dict) -> list[dict]:
    mapped = (artifacts.get("transformer") or {}).get("line_items_mapped")
    if mapped:
        return [
            {
                "line_number": item.get("line_number") or index,
                "description": _text(item.get("description"), 8000),
                "item_code": _text(item.get("item_code"), 64),
                "quantity": _decimal(item.get("quantity")),
                "unit_price": _decimal(item.get("unit_cost")),
                "amount_ex_tax": _decimal(item.get("line_cost")),
                "tax_amount": _decimal(item.get("tax")),
                "tax_code": _text(item.get("tax_code"), 32),
                "gl_account": _text(item.get("gl_account"), 64),
            }
            for index, item in enumerate(mapped, start=1)
        ]

    return [
        {
            "line_number": index,
            "description": _text(item.get("description"), 8000),
            "item_code": _text(item.get("item_code"), 64),
            "quantity": _decimal(item.get("quantity")),
            "unit_price": _decimal(item.get("unit_price")),
            "amount_ex_tax": _decimal(item.get("amount_ex_tax")),
            "tax_amount": _decimal(item.get("tax_amount")),
            "tax_code": _text(item.get("tax_code"), 32),
            "gl_account": None,
        }
        for index, item in enumerate(
            extraction(artifacts).get("line_items") or [], start=1
        )
    ]


def findings(artifacts: dict) -> list[dict]:
    """Every validation step, in the order the phases ran.

    This is the explainability record: the reason a decision can be defended
    rather than asserted. Passes are included -- "which checks ran and passed"
    is half of what an auditor asks.
    """
    out: list[dict] = []
    for phase in ("phase1", "phase2", "phase3", "phase4"):
        for validation in (artifacts.get(phase) or {}).get("validations") or []:
            out.append(
                {
                    "phase": phase,
                    "step": validation.get("step"),
                    "rule": validation.get("rule"),
                    "passed": validation.get("passed"),
                    "informational": validation.get("informational", False),
                    "implemented": validation.get("implemented", True),
                    "evidence": validation.get("evidence"),
                    "rejection_template": validation.get("rejection_template"),
                }
            )
    out.extend(alf_findings(artifacts))
    return out


def alf_findings(artifacts: dict) -> list[dict]:
    """The rules that fired, as validation records like any other.

    A corrected decision that cannot say which rule corrected it is exactly
    the opaque automation this product exists not to be. Rendering them as
    findings means the drawer, the workbench and the export explain a rule
    without a line of new frontend code.
    """
    audit = artifacts.get("alf") or {}
    out: list[dict] = []
    for applied in audit.get("rules_applied") or []:
        conditions = ", ".join(
            f"{c.get('field')} {c.get('operator')} {c.get('expected')!r}"
            for c in applied.get("conditions_evaluated") or []
        )
        shadow = audit.get("mode") == "shadow"
        out.append(
            {
                "phase": "alf",
                "step": applied.get("rule_id"),
                "rule": applied.get("rule_name") or applied.get("rule_id"),
                # A rule firing is not a check failing. `passed` is what the
                # UI colours on, and a correction is not a problem.
                "passed": True,
                "informational": shadow,
                "implemented": True,
                "evidence": (
                    f"{'Would correct' if shadow else 'Corrected'} "
                    f"{audit.get('status_before')} -> "
                    f"{audit.get('status_after')}. Fired on: {conditions}"
                ),
                "rejection_template": None,
            }
        )
    return out


def severity_of(finding: dict) -> str:
    """How much a failed step matters.

    A step that can reject carries a rejection template; one marked
    informational never rejects however it lands. Recording that distinction
    is what stops a reviewer treating step 4.1 like a blocking failure.
    """
    if finding.get("informational"):
        return "info"
    if finding.get("passed") is None:
        return "skipped"
    if finding.get("passed"):
        return "pass"
    return "blocking" if finding.get("rejection_template") else "advisory"


def run(artifacts: dict) -> dict | None:
    """What the run cost and how it was configured.

    The audit-log artifact already records this; nothing was reading it. It is
    also the only per-case measurement of LLM calls that exists, which is the
    number the critic's cost target is supposed to be judged against.
    """
    audit = artifacts.get("audit_log") or {}
    summary = audit.get("processing_summary") or {}
    if not summary:
        return None
    duration = summary.get("processing_time_seconds")
    return {
        "pipeline_version": _text(audit.get("version"), 32),
        "model_ids": summary.get("models") or {},
        "llm_calls": int(summary.get("total_llm_calls") or 0),
        "cost_usd": _decimal(summary.get("total_cost_usd")),
        "duration_ms": int(duration * 1000) if duration else None,
        "critic_verdict": _text(summary.get("critic_verdict"), 32),
    }
