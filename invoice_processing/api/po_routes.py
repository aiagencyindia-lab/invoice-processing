"""Purchase orders with lines, and the goods received against them.

Both arrive as a file from the customer's ERP or stores system, as JSON or
CSV, and either the whole file is stored or none of it: every problem in the
file is named with its row, so one upload fixes them all. Receipts are
evidence -- immutable once accepted, a repeated GRN number is a no-op, and a
correction is a new GRN with negative quantities and a reason.

CSV is one row per line with the header columns repeated on each row.

Purchase orders::

    po_number,vendor_name,total_amount,currency,status,amount_invoiced,
    customer_name,valid_from,valid_to,amount_basis,line_number,item_code,
    description,qty_ordered,unit_price,uom,receipt_required,qty_invoiced,gl_account

Goods receipts::

    grn_number,po_number,received_on,reason,line_number,item_code,qty_received

`receipt_required` is true/false/yes/no/1/0, or empty to follow the vendor.
Rows are numbered as a spreadsheet shows them: the header is row 1.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from .. import po_matching
from ..trust.agents import guard
from . import auth
from .auth import Principal, require_role

logger = logging.getLogger(__name__)
router = APIRouter()

NEEDS_DB = "Purchase-order matching needs a database. Configure DATABASE_URL."
MAX_IMPORT_BYTES = 5 * 1024 * 1024
TOO_BIG = "Import is over 5 MB."

PO_HEADER = (
    "po_number", "vendor_name", "total_amount", "currency", "status",
    "amount_invoiced", "customer_name", "valid_from", "valid_to",
    "amount_basis",
)  # fmt: skip
PO_LINE = (
    "line_number", "item_code", "description", "qty_ordered", "unit_price",
    "uom", "receipt_required", "qty_invoiced", "gl_account",
)  # fmt: skip
GRN_HEADER = ("grn_number", "po_number", "received_on", "reason")
GRN_LINE = ("line_number", "item_code", "qty_received")


def _iso_date(value: str) -> str:
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            date.fromisoformat(value)
            return value
    except ValueError:
        pass
    raise ValueError("must be a date as YYYY-MM-DD")


def _po_number(value: str) -> str:
    if not po_matching.normalize_po_number(value):
        raise ValueError("must contain letters or digits")
    return value


def Text(n: int, min_length: int = 0):
    return Annotated[str, Field(min_length=min_length, max_length=n)]


# Numeric(18, 4) holds 14 integer digits; Integer is 32-bit. Beyond either
# Postgres refuses the insert, so the row is refused here, by name.
MAX_NUMBER = 99_999_999_999_999
MAX_LINE = 2**31 - 1
Amount = Annotated[float, Field(ge=0, le=MAX_NUMBER, allow_inf_nan=False)]
Quantity = Annotated[
    float, Field(ge=-MAX_NUMBER, le=MAX_NUMBER, allow_inf_nan=False)
]
IsoDate = Annotated[str, Field(max_length=10), AfterValidator(_iso_date)]
PoNumber = Annotated[str, Field(max_length=128), AfterValidator(_po_number)]
LineNumber = Annotated[int, Field(gt=0, le=MAX_LINE)]


class _Row(BaseModel):
    model_config = ConfigDict(
        extra="ignore", str_strip_whitespace=True, coerce_numbers_to_str=True
    )


class PoHeaderIn(_Row):
    po_number: PoNumber
    vendor_name: Text(512, 1)
    total_amount: Amount
    currency: Annotated[str, Field(pattern=r"^[A-Za-z]{3}$")] | None = None
    status: Text(32) | None = None
    amount_invoiced: Amount | None = None
    customer_name: Text(512) | None = None
    valid_from: IsoDate | None = None
    valid_to: IsoDate | None = None
    amount_basis: Literal["inc_tax", "ex_tax"] | None = None
    approved_quantity: Amount | None = None
    approved_unit_price: Amount | None = None
    hsn_sac: Text(16) | None = None


class PoLineIn(_Row):
    line_number: LineNumber
    item_code: Text(64) | None = None
    description: Text(8000) = ""
    qty_ordered: Amount
    unit_price: Amount
    uom: Text(16) | None = None
    receipt_required: bool | None = None
    qty_invoiced: Amount = 0
    # Blank cells never reach here: the CSV grouper drops empty cells and
    # str_strip_whitespace trims JSON, so "   " arrives as "" -- mapped to
    # None by the validator below rather than stored as an empty account.
    gl_account: Text(64) | None = None

    @field_validator("gl_account")
    @classmethod
    def _blank_is_none(cls, value):
        return value or None


class GrnHeaderIn(_Row):
    grn_number: Text(128, 1)
    po_number: PoNumber
    received_on: IsoDate
    reason: Text(500) | None = None


class GrnLineIn(_Row):
    line_number: LineNumber | None = None
    item_code: Text(64) | None = None
    qty_received: Quantity


_NUMBER = {"float_parsing", "float_type", "finite_number", "greater_than_equal"}
_WHOLE = {"int_parsing", "int_type", "int_from_float", "greater_than"}


def _say(err: dict) -> str:
    field = ".".join(str(p) for p in err["loc"]) or "entry"
    kind = err["type"]
    if kind == "missing":
        return f"{field} is required"
    if kind == "string_too_long":
        return f"{field} must be at most {err['ctx']['max_length']} characters"
    if kind == "string_too_short":
        return f"{field} must not be blank"
    if kind == "less_than_equal":
        if field == "line_number":
            return f"{field} must be a whole number from 1 to {MAX_LINE}"
        return f"{field} must be at most {MAX_NUMBER}"
    if kind == "greater_than_equal" and field == "qty_received":
        return f"{field} must be at least -{MAX_NUMBER}"
    if kind in _NUMBER:
        return f"{field} must be a number" + (
            "" if field == "qty_received" else " ≥ 0"
        )
    if kind in _WHOLE:
        return f"{field} must be a whole number above 0"
    if kind == "value_error":
        return f"{field} {err['ctx']['error']}"
    if kind == "string_pattern_mismatch" and field == "currency":
        return "currency must be a 3-letter code"
    return f"{field}: {err['msg']}"


def _check(model, data, row: int, problems: list[str], suffix: str = ""):
    """`data` as `model`, or None with every problem appended."""
    if not isinstance(data, dict):
        problems.append(f"row {row}: is not an object{suffix}")
        return None
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        problems.extend(f"row {row}: {_say(e)}{suffix}" for e in exc.errors())
        return None


def _row(record, index: int) -> int:
    row = record.get("_row") if isinstance(record, dict) else None
    return row if isinstance(row, int) else index + 1


def _lines(record) -> list:
    lines = record.get("lines", [])
    return lines if isinstance(lines, list) else [lines]


def _line_place(line, record_row: int, k: int) -> tuple[int, str]:
    """Where to point at a line: its own CSV row, or its PO's row + which."""
    if isinstance(line, dict) and isinstance(line.get("_row"), int):
        return line["_row"], ""
    which = line.get("line_number", k) if isinstance(line, dict) else k
    return record_row, f" (line {which})"


def validate_orders(records) -> list[str]:
    """Every problem in an upload of purchase orders, as "row N: ..."."""
    problems: list[str] = []
    first_row: dict[str, int] = {}
    for i, record in enumerate(records):
        row = _row(record, i)
        header = _check(PoHeaderIn, record, row, problems)
        if not isinstance(record, dict):
            continue
        problems.extend(record.get("_conflicts", []))
        if header is not None:
            key = po_matching.normalize_po_number(header.po_number)
            if key in first_row:
                problems.append(
                    f"row {row}: PO {header.po_number} appears twice in this "
                    f"upload (first at row {first_row[key]})"
                )
            first_row.setdefault(key, row)
        numbers: set[int] = set()
        for k, line in enumerate(_lines(record), start=1):
            at, suffix = _line_place(line, row, k)
            parsed = _check(PoLineIn, line, at, problems, suffix)
            if parsed is None:
                continue
            if parsed.line_number in numbers:
                problems.append(
                    f"row {at}: line_number {parsed.line_number} appears "
                    f"twice on this PO{suffix}"
                )
            numbers.add(parsed.line_number)
    return problems


def validate_receipts(records) -> list[str]:
    """Every problem in an upload of goods receipts, as "row N: ..."."""
    problems: list[str] = []
    for i, record in enumerate(records):
        row = _row(record, i)
        header = _check(GrnHeaderIn, record, row, problems)
        if not isinstance(record, dict):
            continue
        problems.extend(record.get("_conflicts", []))
        lines = _lines(record)
        if not lines:
            problems.append(f"row {row}: a goods receipt needs lines")
        for k, line in enumerate(lines, start=1):
            at, suffix = _line_place(line, row, k)
            parsed = _check(GrnLineIn, line, at, problems, suffix)
            if parsed is None:
                continue
            if parsed.line_number is None and not parsed.item_code:
                problems.append(
                    f"row {at}: needs a line_number or an item_code{suffix}"
                )
            if (
                parsed.qty_received < 0
                and header is not None
                and not header.reason
            ):
                problems.append(
                    f"row {at}: a negative qty_received needs a reason{suffix}"
                )
    return problems


def _clean_orders(records) -> list[dict]:
    return [
        {
            **PoHeaderIn.model_validate(r).model_dump(exclude_none=True),
            "lines": [
                PoLineIn.model_validate(ln).model_dump(exclude_none=True)
                for ln in _lines(r)
            ],
        }
        for r in records
    ]


def _clean_receipts(records) -> list[dict]:
    return [
        {
            **GrnHeaderIn.model_validate(r).model_dump(exclude_none=True),
            "lines": [
                GrnLineIn.model_validate(ln).model_dump(exclude_none=True)
                for ln in _lines(r)
            ],
        }
        for r in records
    ]


def _group(text: str, key: str, header: tuple, line: tuple) -> list[dict]:
    """CSV rows -> one record per `key`, its rows' line columns as lines.

    A later row whose header columns disagree with the first is recorded as
    a conflict rather than silently losing to it.
    """
    records: dict[str, dict] = {}
    reader = csv.DictReader(io.StringIO(text))
    for raw in reader:
        row = reader.line_num
        cells = {
            (k or "").strip(): v.strip()
            for k, v in raw.items()
            if isinstance(v, str) and v.strip()
        }
        ident = cells.get(key, "")
        group = (
            po_matching.normalize_po_number(ident)
            if key == "po_number"
            else ident
        )
        head = {k: cells[k] for k in header if k in cells}
        record = records.get(group)
        if record is None:
            record = records[group] = {**head, "_row": row, "lines": []}
            record["_conflicts"] = []
        else:
            for k in header:
                if head.get(k) != record.get(k):
                    record["_conflicts"].append(
                        f"row {row}: {k} differs from row {record['_row']} "
                        f"for {ident or 'this record'}"
                    )
        body = {k: cells[k] for k in line if k in cells}
        if body:
            record["lines"].append({**body, "_row": row})
    return list(records.values())


def parse_po_csv(text: str) -> list[dict]:
    return _group(text, "po_number", PO_HEADER, PO_LINE)


def parse_grn_csv(text: str) -> list[dict]:
    return _group(text, "grn_number", GRN_HEADER, GRN_LINE)


def _public(item):
    if not isinstance(item, dict):
        return item
    out = {k: v for k, v in item.items() if not str(k).startswith("_")}
    if isinstance(out.get("lines"), list):
        out["lines"] = [_public(ln) for ln in out["lines"]]
    return out


def without_internal_keys(records: list) -> list:
    """JSON records minus `_row`/`_conflicts`: only the CSV parser sets them,
    and a caller's own values would be trusted as row numbers and errors."""
    return [_public(r) for r in records]


async def _read_upload(request: Request, field: str, parse_csv) -> list:
    """The records in a JSON body or an uploaded .csv/.json file; 413 over
    the cap, 400 when it cannot be read."""
    declared = request.headers.get("content-length", "")
    slack = 64 * 1024  # multipart boundaries around a file at the cap
    if declared.isdigit() and int(declared) > MAX_IMPORT_BYTES + slack:
        raise HTTPException(413, TOO_BIG)
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise HTTPException(400, "Attach the file as 'file'.")
        data = await upload.read(MAX_IMPORT_BYTES + 1)
        name = (upload.filename or "").lower()
        is_csv = name.endswith(".csv")
        if not (is_csv or name.endswith(".json")):
            raise HTTPException(400, "Upload a .csv or .json file.")
    else:
        data, is_csv = await request.body(), False
    if len(data) > MAX_IMPORT_BYTES:
        raise HTTPException(413, TOO_BIG)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "The file is not UTF-8 text.") from None
    if is_csv:
        records = parse_csv(text)
    else:
        try:
            body = json.loads(text)
        except ValueError:
            raise HTTPException(400, "The file is not valid JSON.") from None
        records = body.get(field) if isinstance(body, dict) else body
        if not isinstance(records, list):
            raise HTTPException(400, f"{field} must be a list.")
        records = without_internal_keys(records)
    if not records:
        raise HTTPException(
            400, f"The upload has no {field.replace('_', ' ')}."
        )
    return records


def _repo():
    from . import service  # noqa: PLC0415

    try:
        repo = service.get_purchase_orders()
    except Exception:
        logger.exception("Purchase orders could not be reached")
        raise HTTPException(503, "Purchase orders are unavailable.") from None
    if repo is None:
        raise HTTPException(503, NEEDS_DB)
    return repo


def _refused(exc: ValueError, records) -> HTTPException:
    """The repository's refusal, its "row N" pointed at the file's row."""

    def place(m):
        i = int(m[1]) - 1
        return f"row {_row(records[i], i) if 0 <= i < len(records) else m[1]}:"

    return HTTPException(400, re.sub(r"^row (\d+):", place, str(exc)))


def store_orders(repo, principal: Principal, records, *, replace=False) -> dict:
    """Validate the whole upload, ask the agent guard about each new PO,
    then upsert it in one transaction."""
    problems = validate_orders(records)
    if problems:
        raise HTTPException(400, "; ".join(problems))
    clean = _clean_orders(records)
    # Every create is checked before any is written (FR-28.2).
    decision = guard.check(
        principal, lambda: guard.po_action(principal.context(), clean, repo)
    )
    try:
        with guard.settling(principal, decision) as settle:
            out = repo.upsert_orders(
                principal.tenant_id,
                clean,
                uploaded_by=principal.email,
                replace=replace,
            )
            settle.written()
            return out
    except ValueError as exc:
        raise _refused(exc, records) from None


def queue_rematches(principal: Principal, po_numbers: list[str]) -> int:
    """Held invoices quoting these POs, queued to be matched again.

    The receipts are already stored, so a failure here is logged and audited
    rather than refused; the invoices simply stay held.
    """
    from ..worker.runner import enqueue_rematch  # noqa: PLC0415
    from . import service  # noqa: PLC0415

    queued, failed, error = 0, 0, None
    try:
        cases, queue = service.get_cases(), service.get_queue()
        for po_number in po_numbers:
            for case_id in cases.held_by_receipt(
                principal.tenant_id, po_number
            ):
                # One stalled publish must not leave the rest unqueued.
                try:
                    enqueue_rematch(
                        queue,
                        principal.tenant_id,
                        case_id,
                        po_number,
                        request_id=auth.current_request_id.get(""),
                    )
                    queued += 1
                except Exception as exc:
                    failed, error = failed + 1, exc
                    logger.exception("A re-match job could not be queued")
    except Exception as exc:
        error = exc
        logger.exception("Re-match jobs could not be queued")
    if error is not None:
        _audit().emit(
            principal.context(),
            "three_way_rematch_enqueue_failed",
            {
                "po_numbers": po_numbers,
                "queued": queued,
                "failed": failed,
                "error": str(error),
            },
        )
    return queued


def _audit():
    from . import service  # noqa: PLC0415

    return service.get_audit()


def unblock_receipts(principal: Principal, po_numbers) -> list[str]:
    """Scanned receipts that were waiting for these POs, now confirmable.

    The POs are already stored, so a failure here is logged and audited
    rather than refused; the receipts simply stay blocked.
    """
    from . import service  # noqa: PLC0415

    wanted = {po_matching.normalize_po_number(p) for p in po_numbers if p}
    ids: list[str] = []
    try:
        supporting = service.get_supporting_documents()
        if supporting is None:
            return ids
        # One read of what is blocked, so a 1,000-PO import is not 1,000 queries.
        waiting = {
            r["po_number"]
            # Uncapped: a blocked receipt past any page size must still go.
            for r in supporting.list(
                principal.tenant_id, ("blocked",), limit=None
            )
        }
        for po in sorted(wanted & waiting):
            for row_id in supporting.unblock(principal.tenant_id, po):
                ids.append(row_id)
                _audit().emit(
                    principal.context(),
                    "supporting_document_unblocked",
                    {"id": row_id, "po_number": po},
                )
    except Exception as exc:
        logger.exception("Blocked scanned receipts could not be released")
        _audit().emit(
            principal.context(),
            "supporting_document_unblock_failed",
            {"po_numbers": sorted(wanted), "error": str(exc)},
        )
    return ids


def _import_orders(principal: Principal, records: list) -> dict:
    out = store_orders(_repo(), principal, records)
    _audit().emit(principal.context(), "purchase_orders_imported", out)
    unblock_receipts(principal, [r.get("po_number") for r in records])
    return out


def _import_receipts(principal: Principal, records: list) -> dict:
    repo = _repo()
    problems = validate_receipts(records)
    if problems:
        raise HTTPException(400, "; ".join(problems))
    clean = _clean_receipts(records)
    try:
        out = repo.record_receipts(
            principal.tenant_id, clean, recorded_by=principal.email
        )
    except ValueError as exc:
        raise _refused(exc, records) from None
    po_of = {r["grn_number"]: r["po_number"] for r in clean}
    for grn in out["recorded"]:
        _audit().emit(
            principal.context(),
            "goods_receipt_recorded",
            {"grn_number": grn, "po_number": po_of[grn]},
        )
    return {
        "recorded": len(out["recorded"]),
        "skipped_duplicates": len(out["skipped_duplicates"]),
        "rematch_queued": queue_rematches(principal, out["po_numbers"]),
    }


@router.post("/api/purchase-orders/import", status_code=201)
async def import_purchase_orders(
    request: Request,
    principal: Annotated[Principal, Depends(require_role("clerk"))],
) -> dict:
    """Upsert purchase orders by number: header updated, lines replaced,
    opening billed quantities reset. POs absent from the file are untouched.
    Body: `{"purchase_orders": [{..., "lines": [...]}]}`, or a .csv/.json
    file as `file` (CSV columns in the module docstring)."""
    _repo()  # 503 before reading a body nobody can store
    records = await _read_upload(request, "purchase_orders", parse_po_csv)
    return await run_in_threadpool(_import_orders, principal, records)


@router.get("/api/purchase-orders/{po_number}")
def read_purchase_order(
    po_number: str,
    principal: Annotated[Principal, Depends(require_role("clerk"))],
) -> dict:
    order = _repo().get_order(principal.tenant_id, po_number)
    if order is None:
        raise HTTPException(404, "Unknown purchase order")
    return order


@router.post("/api/goods-receipts/import")
async def import_goods_receipts(
    request: Request,
    principal: Annotated[Principal, Depends(require_role("clerk"))],
) -> dict:
    """Record goods receipts. Body: `{"goods_receipts": [{grn_number,
    po_number, received_on, reason?, lines: [{line_number | item_code,
    qty_received}]}]}`, or a .csv/.json file as `file`."""
    _repo()
    records = await _read_upload(request, "goods_receipts", parse_grn_csv)
    return await run_in_threadpool(_import_receipts, principal, records)


@router.get("/api/goods-receipts")
def list_goods_receipts(
    principal: Annotated[Principal, Depends(require_role("clerk"))],
    po_number: Annotated[str | None, Query(max_length=128)] = None,
) -> dict:
    return {
        "goods_receipts": _repo().list_receipts(principal.tenant_id, po_number)
    }
