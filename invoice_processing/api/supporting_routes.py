"""Scanned purchase orders and goods receipts, confirmed by a person.

The worker parks what it read; nothing counts until a clerk accepts it here,
and accepting runs the corrected document through the same validation and
storage as an ERP import (`po_routes`). Every problem is named in one 400 and
nothing is stored. A PO from the ERP upload is never changed by a scan.
"""

from __future__ import annotations

import logging
import re
from itertools import zip_longest
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

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
        _keys(supporting_docs.GoodsReceiptExtraction),
        _keys(supporting_docs.GrnLineExtraction),
    ),
}

Key = Annotated[str, Field(max_length=64)]
# Text as well as numbers: a misread "six" must reach the import validation
# and come back as a named problem, not as a schema error.
Value = int | float | Annotated[str, Field(max_length=8000)] | None
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


def _not_decidable(status: str, blocked_reason: str | None) -> HTTPException:
    if status == "blocked":
        return HTTPException(
            409, blocked_reason or "Its purchase order is not on file."
        )
    return HTTPException(409, f"This document was already {status}.")


def _decidable(supporting, principal: Principal, row_id: str, allowed) -> dict:
    row = _found(supporting, principal, row_id)
    if row["status"] not in allowed:
        raise _not_decidable(row["status"], row["blocked_reason"])
    return row


def _mark(supporting, principal: Principal, row: dict, allowed, **decision):
    """The decision, only if nobody decided this document meanwhile."""
    try:
        supporting.mark(
            principal.tenant_id,
            row["id"],
            reviewed_by=principal.email,
            expect=allowed,
            **decision,
        )
    except StatusChanged as exc:
        raise _not_decidable(exc.status, row["blocked_reason"]) from None


def _pairing(orders, principal: Principal, row: dict):
    """(PO on file or None, the PO line each extracted receipt line pairs to)."""
    order = (
        orders.get_order(principal.tenant_id, row["po_number"])
        if row["po_number"]
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
    order = orders.get_order(principal.tenant_id, str(new)) if new else None
    paired = (
        supporting_docs.pair_grn_lines(lines, order["lines"])
        if order is not None
        else [None] * len(lines)
    )
    if [ln.get("po_line_number") for ln in lines] != paired:
        was = ((row["extracted"] or {}).get("fields") or {}).get("po_number")
        raise HTTPException(
            400,
            f"The PO number changed from {was or row['po_number']} to {new}; "
            "choose the PO lines again.",
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


def _read_lines_changed(orders, principal, row, lines, keys) -> int | None:
    """Lines the clerk changed, the PO line they chose included; None when
    the pairing cannot be read (the audit then lacks only this count)."""
    read = (row["extracted"] or {}).get("lines") or []
    if row["kind"] == GOODS_RECEIPT:
        try:
            _, paired = _pairing(orders, principal, row)
        except Exception:
            logger.exception("Pairing of %s not read for its audit", row["id"])
            return None
        read = [
            {**ln, "po_line_number": p}
            for ln, p in zip(read, paired or [None] * len(read), strict=True)
        ]
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
) -> dict:
    supporting, _ = _repos()
    wanted = tuple(s.strip() for s in status.split(",") if s.strip())
    if not wanted or set(wanted) - set(STATUSES):
        raise HTTPException(
            400, f"status must be some of {', '.join(STATUSES)}."
        )
    rows = supporting.list(principal.tenant_id, wanted)
    return {"documents": [_public(r) for r in rows]}


@router.get("/api/supporting-documents/{row_id}")
def read_supporting_document(
    row_id: str,
    principal: Annotated[Principal, Depends(require_role("clerk"))],
) -> dict:
    supporting, orders = _repos()
    row = _found(supporting, principal, row_id)
    order, paired = _pairing(orders, principal, row)
    return {**_public(row), "po_on_file": order is not None, "paired": paired}


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
    row = _decidable(supporting, principal, row_id, ACCEPTABLE)
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
            orders, principal, row, lines, kept
        ),
    }

    # Claim the row first: a reject arriving while the PO or receipt is being
    # written is then refused, rather than contradicted by what was stored.
    _mark(
        supporting, principal, row, ACCEPTABLE,
        status="accepted",
        extracted={**before, "fields": fields, "lines": lines},
    )  # fmt: skip
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
                status=row["status"],
            )  # fmt: skip
        except Exception:
            logger.exception("Scanned document %s left accepted", row["id"])
        raise
    po_routes._audit().emit(
        principal.context(), "supporting_document_accepted", accepted
    )
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
    supporting, _ = _repos()
    row = _decidable(supporting, principal, row_id, REJECTABLE)
    _mark(
        supporting, principal, row, REJECTABLE,
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
