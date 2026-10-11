"""Scanned purchase orders and goods receipts, confirmed by a person.

The worker parks what it read; nothing counts until a clerk accepts it here,
and accepting runs the corrected document through the same validation and
storage as an ERP import (`po_routes`). Every problem is named in one 400 and
nothing is stored. A PO from the ERP upload is never changed by a scan.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from itertools import zip_longest
from types import SimpleNamespace
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
)

from .. import po_matching, supporting_docs
from ..ports import ConcurrentWrite, StatusChanged, UploadOwned
from ..supporting_docs import GOODS_RECEIPT, PURCHASE_ORDER
from . import po_routes
from .auth import Principal, require_role

logger = logging.getLogger(__name__)
router = APIRouter()

NEEDS_DB = "Scanned-document review needs a database. Configure DATABASE_URL."
NO_LINES = (
    "No lines could be read from this purchase order; add them or reject it."
)
STATUSES = ("pending", "blocked", "accepted", "rejected")


def _keys(model) -> tuple[str, ...]:
    return tuple(k for k in model.model_fields if k != "lines")


# What a clerk may correct: the extraction's own fields, and for a receipt the
# PO line each line was received against.
_SHAPE = {
    PURCHASE_ORDER: (
        _keys(supporting_docs.PurchaseOrderExtraction),
        _keys(supporting_docs.PoLineExtraction),
    ),
    GOODS_RECEIPT: (
        # A correction (negative quantities) needs its reason, as on import.
        (*_keys(supporting_docs.GoodsReceiptExtraction), "reason"),
        _keys(supporting_docs.GrnLineExtraction),
    ),
}

Key = Annotated[str, Field(max_length=64)]
# Text as well as numbers: a misread "six" must reach the import validation
# and come back as a named problem, not as a schema error. Strict, so true is
# never read as 1; a boolean is let through only to be refused by name.
Value = (
    StrictBool
    | StrictInt
    | StrictFloat
    | Annotated[str, Field(max_length=8000)]
    | None
)
Record = Annotated[dict[Key, Value], Field(max_length=32)]


class AcceptIn(BaseModel):
    fields: Record = {}
    lines: Annotated[list[Record], Field(max_length=2000)] = []
    # The clerk picked the PO lines again after changing the PO number.
    lines_rechosen: bool = False


class RejectIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    reason: Annotated[str, Field(min_length=1, max_length=500)]


def _repos():
    from . import service  # noqa: PLC0415

    # The JSON fallback serves the worker in single-process development;
    # accepting writes the PO tables, so review needs the database.
    if not service._database_configured():
        raise HTTPException(503, NEEDS_DB)
    try:
        supporting = service.get_supporting_documents()
        orders = service.get_purchase_orders()
    except Exception:
        logger.exception("Scanned documents could not be reached")
        raise HTTPException(503, "Scanned documents are unavailable.") from None
    if supporting is None or orders is None:
        raise HTTPException(503, NEEDS_DB)
    return supporting, orders


def _public(row: dict) -> dict:
    return {
        **row,
        "document_url": f"/api/invoices/{row['document_id']}/document",
    }


def _found(supporting, principal: Principal, row_id: str) -> dict:
    row = supporting.get(principal.tenant_id, row_id)
    if row is None:
        raise HTTPException(404, "Unknown scanned document")
    return row


# A receipt blocked on a misread PO number would otherwise wait forever: the
# clerk corrects the number, and the store still refuses a PO not on file.
ACCEPTABLE = ("pending", "blocked")
REJECTABLE = ("pending", "blocked")


RACED = (
    "This document is being decided by someone else; reload to see the result."
)


def _not_decidable(status: str, blocked_reason: str | None) -> HTTPException:
    if status == "blocked":
        return HTTPException(
            409, blocked_reason or "Its purchase order is not on file."
        )
    return HTTPException(409, f"This document was already {status}.")


def _stored(orders, tenant_id: str, rows: list[dict]) -> set[str]:
    """Document ids of `rows` that something stored came from."""
    po_numbers = {
        r["document_id"]: ((r["extracted"] or {}).get("fields") or {}).get(
            "po_number"
        )
        for r in rows
        if r["kind"] == PURCHASE_ORDER
    }
    return orders.stored_documents(
        tenant_id, [r["document_id"] for r in rows], po_numbers
    )


def _stored_or_all(orders, tenant_id: str, rows: list[dict]) -> set[str]:
    """`_stored` for display: unknown reads as stored, never as a 500 and
    never as an orphan to reopen (the reopen route checks for itself)."""
    try:
        return _stored(orders, tenant_id, rows)
    except Exception:
        logger.exception("Stores of scanned documents not read")
        return {r["document_id"] for r in rows}


def _refusal(orders, principal: Principal, row: dict, status: str):
    """Why `row`, now `status`, cannot be decided. Accepted with nothing
    stored may be an accept still storing, so nobody is told it succeeded."""
    if status == "accepted":
        try:
            stored = row["document_id"] in _stored(
                orders, principal.tenant_id, [row]
            )
        except Exception:
            logger.exception("Store of %s not read", row["id"])
            stored = False
        if not stored:
            return HTTPException(409, RACED)
    return _not_decidable(status, row["blocked_reason"])


def _decidable(
    supporting, orders, principal: Principal, row_id: str, allowed
) -> dict:
    row = _found(supporting, principal, row_id)
    if row["status"] not in allowed:
        raise _refusal(orders, principal, row, row["status"])
    return row


def _mark(
    supporting, orders, principal: Principal, row: dict, allowed, **decision
) -> dict:
    """The decision, only if nobody decided this document meanwhile; returns
    the row with `previous`, the status and block the decision replaced."""
    try:
        return supporting.mark(
            principal.tenant_id,
            row["id"],
            reviewed_by=principal.email,
            expect=allowed,
            **decision,
        )
    except StatusChanged as exc:
        if exc.status in allowed:
            # Still decidable, yet the compare-and-set kept missing: the
            # row is changing under us, so nothing is claimed about it.
            raise HTTPException(409, RACED) from None
        raise _refusal(orders, principal, row, exc.status) from None


def _pairing(orders, principal: Principal, row: dict, po_number):
    """(PO `po_number` if on file, else None; the line of it each extracted
    receipt line pairs to)."""
    order = (
        orders.get_order(principal.tenant_id, str(po_number))
        if po_number
        else None
    )
    paired = None
    if order is not None and row["kind"] == GOODS_RECEIPT:
        paired = supporting_docs.pair_grn_lines(
            (row["extracted"] or {}).get("lines") or [], order["lines"]
        )
    return order, paired


def _refuse_stale_pairing(orders, principal, row, fields, lines) -> None:
    """A PO number changed from the scan's leaves its lines paired to the old
    PO; the store books by line number, so they would land on whatever line
    has that number on the new one. Refused unless they pair the new PO."""
    new = fields.get("po_number")
    new_key = po_matching.normalize_po_number(new) if new else None
    if new_key == row["po_number"]:
        return
    try:
        order = orders.get_order(principal.tenant_id, str(new)) if new else None
    except Exception:
        logger.exception("PO %s not read to check the pairing", new)
        raise HTTPException(
            503, "Purchase-order lines could not be read; try again."
        ) from None
    paired = (
        supporting_docs.pair_grn_lines(lines, order["lines"])
        if order is not None
        else [None] * len(lines)
    )
    if [ln.get("po_line_number") for ln in lines] != paired:
        if not new:
            raise HTTPException(
                400,
                "The PO number was cleared; enter the PO number and choose "
                "the PO lines again.",
            )
        was = ((row["extracted"] or {}).get("fields") or {}).get("po_number")
        raise HTTPException(
            400,
            f"The PO number changed from {was or row['po_number']} to {new}; "
            "choose the PO lines again.",
        )


def _refuse_booleans(body: AcceptIn) -> None:
    """true/false is never a value of a PO or receipt; named, not coerced."""
    places = [("", body.fields)] + [
        (f"line {i}: ", ln) for i, ln in enumerate(body.lines, start=1)
    ]
    for where, record in places:
        for key, value in record.items():
            if isinstance(value, bool):
                raise HTTPException(
                    400,
                    f"{where}{key} must be a number or text, not true or "
                    "false.",
                )


def _one_record(message: str) -> str:
    # The import names rows of a file; here there is only the one document.
    return re.sub(r"^row 1: ", "", message)


def _refused(problems: list[str]) -> HTTPException:
    return HTTPException(400, "; ".join(_one_record(p) for p in problems))


def _without_none(record: dict) -> dict:
    """Unread values left out, so validation says "required" and a GRN line
    with no PO line is named by its position rather than "line None"."""
    out = {k: v for k, v in record.items() if v is not None and k != "lines"}
    if "lines" in record:
        out["lines"] = [_without_none(ln) for ln in record["lines"]]
    return out


def _changed(before: dict, after: dict) -> dict:
    return {
        k: {"before": before.get(k), "after": after.get(k)}
        for k in after
        if before.get(k) != after.get(k)
    }


def _read_lines_changed(
    orders, principal, row, fields, lines, keys
) -> int | None:
    """Lines the clerk changed, the PO line they chose included -- against
    the pairing to the PO they confirmed, which may not be the scanned one.
    None when the pairing cannot be read (the audit then lacks this count)."""
    read = (row["extracted"] or {}).get("lines") or []
    if row["kind"] == GOODS_RECEIPT:
        try:
            _, paired = _pairing(
                orders, principal, row, fields.get("po_number")
            )
            read = [
                {**ln, "po_line_number": p}
                for ln, p in zip(
                    read, paired or [None] * len(read), strict=True
                )
            ]
        except Exception:
            logger.exception("Pairing of %s not read for its audit", row["id"])
            return None
    return _lines_changed(read, lines, keys)


def _lines_changed(before: list, after: list, keys) -> int:
    def view(ln):
        return None if ln is None else {k: ln.get(k) for k in keys}

    return sum(
        view(a) != view(b) for a, b in zip_longest(before or [], after or [])
    )


@router.get("/api/supporting-documents")
def list_supporting_documents(
    principal: Annotated[Principal, Depends(require_role("clerk"))],
    status: Annotated[str, Query(max_length=64)] = "pending,blocked",
    stored: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict:
    """`stored=false`: only acceptances that stored nothing and are past the
    still-storing window (orphans an admin can reopen), newest first; a store
    that cannot be read hides them all."""
    supporting, orders = _repos()
    wanted = tuple(s.strip() for s in status.split(",") if s.strip())
    if not wanted or set(wanted) - set(STATUSES):
        raise HTTPException(
            400, f"status must be some of {', '.join(STATUSES)}."
        )
    rows = supporting.list(
        principal.tenant_id,
        wanted,
        limit=limit,
        newest_first=stored is not None,
    )
    done = _stored_or_all(orders, principal.tenant_id, rows)
    listed = [{**_public(r), "stored": r["document_id"] in done} for r in rows]
    if stored is not None:
        listed = [
            r
            for r in listed
            if r["status"] == "accepted"
            and r["stored"] is stored
            # Not yet an orphan: it may still be storing.
            and (stored or not _still_storing(r))
        ]
    return {"documents": listed}


@router.get("/api/supporting-documents/{row_id}")
def read_supporting_document(
    row_id: str,
    principal: Annotated[Principal, Depends(require_role("clerk"))],
) -> dict:
    supporting, orders = _repos()
    row = _found(supporting, principal, row_id)
    order, paired = _pairing(orders, principal, row, row["po_number"])
    return {
        **_public(row),
        "po_on_file": order is not None,
        "paired": paired,
        "stored": row["document_id"]
        in _stored_or_all(orders, principal.tenant_id, [row]),
    }


def _store_order(orders, principal: Principal, row: dict, clean: list) -> dict:
    # The repository refuses an ERP-owned PO in the same transaction as the
    # write, so an upload landing meanwhile is never overwritten.
    try:
        return orders.upsert_orders(
            principal.tenant_id,
            clean,
            uploaded_by=principal.email,
            source="document",
            document_id=row["document_id"],
            replace=False,
        )
    except (UploadOwned, ConcurrentWrite) as exc:
        raise HTTPException(409, str(exc)) from None
    except ValueError as exc:
        raise _refused([str(exc)]) from None


def _store_receipt(orders, principal: Principal, row: dict, clean) -> dict:
    try:
        out = orders.record_receipts(
            principal.tenant_id,
            clean,
            recorded_by=principal.email,
            source="document",
            document_id=row["document_id"],
        )
    except ConcurrentWrite as exc:
        raise HTTPException(409, str(exc)) from None
    except ValueError as exc:
        raise _refused([str(exc)]) from None
    if out["skipped_duplicates"]:
        raise HTTPException(
            400, f"Goods receipt {clean[0]['grn_number']} is already recorded."
        )
    return out


@router.post("/api/supporting-documents/{row_id}/accept")
def accept_supporting_document(
    row_id: str,
    body: AcceptIn,
    response: Response,
    principal: Annotated[Principal, Depends(require_role("clerk"))],
) -> dict:
    """Confirm the corrected document into the match. Body: `{fields, lines}`
    in the extraction's shape; a receipt line carries `po_line_number`."""
    supporting, orders = _repos()
    row = _decidable(supporting, orders, principal, row_id, ACCEPTABLE)
    kind = row["kind"]
    field_keys, line_keys = _SHAPE[kind]
    before = row["extracted"] or {}
    fields = {
        k: body.fields.get(k, (before.get("fields") or {}).get(k))
        for k in field_keys
    }
    kept = (
        (*line_keys, "po_line_number") if kind == GOODS_RECEIPT else line_keys
    )
    _refuse_booleans(body)
    lines = [{k: ln[k] for k in kept if k in ln} for ln in body.lines]
    if kind == PURCHASE_ORDER and not lines:
        raise HTTPException(400, NO_LINES)
    if kind == GOODS_RECEIPT and not body.lines_rechosen:
        _refuse_stale_pairing(orders, principal, row, fields, lines)

    record = _without_none(
        supporting_docs.to_import_payload(kind, fields, lines)
    )
    validate, clean_up, number_key = (
        (po_routes.validate_orders, po_routes._clean_orders, "po_number")
        if kind == PURCHASE_ORDER
        else (
            po_routes.validate_receipts,
            po_routes._clean_receipts,
            "grn_number",
        )
    )
    problems = validate([record])
    if problems:
        raise _refused(problems)
    clean = clean_up([record])
    number = clean[0][number_key]
    accepted = {
        "id": row["id"],
        "document_id": row["document_id"],
        "kind": kind,
        "number": number,
        "changed": _changed(before.get("fields") or {}, fields),
        "lines_changed": _read_lines_changed(
            orders, principal, row, fields, lines, kept
        ),
    }

    # Claim the row first: a reject arriving while the PO or receipt is being
    # written is then refused, rather than contradicted by what was stored.
    claimed = _mark(
        supporting, orders, principal, row, ACCEPTABLE,
        status="accepted",
        extracted={**before, "fields": fields, "lines": lines},
    )  # fmt: skip
    previous = claimed["previous"]
    try:
        if kind == PURCHASE_ORDER:
            out = _store_order(orders, principal, row, clean)
            response.status_code = 201
        else:
            stored = _store_receipt(orders, principal, row, clean)
    except Exception:
        try:
            supporting.revert_to_pending(
                principal.tenant_id, row["id"], extracted=before,
                status=previous["status"],
                blocked_reason=previous["blocked_reason"],
            )  # fmt: skip
        except Exception:
            logger.exception("Scanned document %s left accepted", row["id"])
        raise
    # Stored is stored: a trail that could not be written is logged, not
    # turned into a failure the clerk would retry into a duplicate.
    _emit(principal, "supporting_document_accepted", accepted)
    if kind == PURCHASE_ORDER:
        po_routes.unblock_receipts(principal, [number])
        return out
    return {
        "recorded": len(stored["recorded"]),
        "rematch_queued": po_routes.queue_rematches(
            principal, stored["po_numbers"]
        ),
    }


@router.post("/api/supporting-documents/{row_id}/reject")
def reject_supporting_document(
    row_id: str,
    body: RejectIn,
    principal: Annotated[Principal, Depends(require_role("clerk"))],
) -> dict:
    supporting, orders = _repos()
    row = _decidable(supporting, orders, principal, row_id, REJECTABLE)
    _mark(
        supporting, orders, principal, row, REJECTABLE,
        status="rejected", note=body.reason,
    )  # fmt: skip
    po_routes._audit().emit(
        principal.context(),
        "supporting_document_rejected",
        {
            "id": row["id"],
            "document_id": row["document_id"],
            "kind": row["kind"],
            "reason": body.reason,
        },
    )
    return {"status": "rejected"}


SENT_TO_INVOICES = "Sent to the invoice queue"


@router.post("/api/supporting-documents/{row_id}/process-as-invoice")
def process_supporting_document_as_invoice(
    row_id: str,
    principal: Annotated[Principal, Depends(require_role("clerk"))],
) -> dict:
    """The classifier was wrong: this is an invoice. Closes the review and
    runs the document through the invoice pipeline, which is told not to
    divert it again."""
    from ..worker.runner import enqueue_document  # noqa: PLC0415
    from . import service  # noqa: PLC0415

    supporting, orders = _repos()
    row = _decidable(supporting, orders, principal, row_id, REJECTABLE)
    document_id = row["document_id"]
    try:
        documents = service.get_documents()
        document = documents.get(principal.tenant_id, document_id)
    except Exception:
        logger.exception("Document %s not read", document_id)
        raise HTTPException(
            503, "The document could not be read; try again."
        ) from None
    if not document:
        raise HTTPException(
            404,
            "This document's file record is missing; it cannot be sent to "
            "the invoice queue.",
        )
    _mark(
        supporting, orders, principal, row, REJECTABLE,
        status="rejected", note=SENT_TO_INVOICES,
    )  # fmt: skip
    # From here a failure is recovered by Retry on Documents, which forces a
    # document with this note through the invoice path (system.py).
    try:
        documents.set_kind(principal.tenant_id, document_id, "invoice")
        # As a retry does: a kept claim would make the worker skip it.
        service.get_claim_store().release(principal.tenant_id, document_id)
        message_id = enqueue_document(
            service.get_queue(),
            principal.context(document_id),
            SimpleNamespace(
                document_id=document_id,
                sha256=document.get("sha256", ""),
                storage_key=document.get("storage_key", ""),
            ),
            force_invoice=True,
        )
    except Exception:
        logger.exception("%s not queued as an invoice", document_id)
        raise HTTPException(
            503,
            "The document could not be queued; retry it from Documents.",
        ) from None
    _emit(
        principal,
        "supporting_document_sent_to_invoices",
        {"id": row["id"], "document_id": document_id, "kind": row["kind"]},
    )
    return {"status": "rejected", "queued": message_id}


# An accept claims the row, then stores: until it has had time to finish, an
# accepted row with nothing stored may be one still storing, not an orphan.
# ponytail: a time window, not a lock; the store usually takes under a second.
STILL_STORING = timedelta(minutes=5)


def _still_storing(row: dict) -> bool:
    reviewed = row["reviewed_at"]
    return bool(reviewed) and (
        datetime.now(timezone.utc) - _aware(datetime.fromisoformat(reviewed))
        < STILL_STORING
    )


@router.post("/api/supporting-documents/{row_id}/reopen")
def reopen_supporting_document(
    row_id: str,
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> dict:
    """Put an acceptance that stored nothing (a crash between the claim and
    the store) back to pending, so it can be confirmed again."""
    supporting, orders = _repos()
    row = _found(supporting, principal, row_id)
    if row["status"] != "accepted":
        raise HTTPException(
            409,
            "Only an accepted document can be reopened; this one is "
            f"{row['status']}.",
        )
    if _still_storing(row):
        raise HTTPException(
            409,
            "This document was accepted moments ago and may still be "
            "storing; try again in a few minutes.",
        )
    try:
        stored = row["document_id"] in _stored(
            orders, principal.tenant_id, [row]
        )
    except Exception:
        logger.exception("Store of %s not read", row["id"])
        raise HTTPException(
            503, "Purchase orders could not be read; try again."
        ) from None
    if stored:
        raise HTTPException(
            409,
            "Something was stored from this document; it cannot be reopened.",
        )
    try:
        # Only the acceptance read above: one made since is left alone.
        supporting.revert_to_pending(
            principal.tenant_id, row["id"], extracted=row["extracted"],
            reviewed_at=row["reviewed_at"],
        )  # fmt: skip
    except StatusChanged as exc:
        if exc.status == "accepted":
            raise HTTPException(409, RACED) from None
        raise _not_decidable(exc.status, row["blocked_reason"]) from None
    _emit(
        principal,
        "supporting_document_reopened",
        {
            "id": row["id"],
            "document_id": row["document_id"],
            "kind": row["kind"],
        },
    )
    return {"status": "pending"}


def _aware(at: datetime) -> datetime:
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def _emit(principal: Principal, event: str, payload: dict) -> None:
    try:
        po_routes._audit().emit(principal.context(), event, payload)
    except Exception:
        logger.exception("%s for %s not audited", event, payload.get("id"))
