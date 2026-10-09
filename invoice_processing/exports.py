"""Writing approved invoices out as a file.

An export outlives the request that asked for it. Ten thousand invoices take
longer than a browser will wait, and "where did my download go" has to have an
answer, so each one is a row that can be polled and a file that can be fetched
again -- rather than a response that either arrives or is lost.

Three formats, chosen because they are what an accounting team actually
receives: CSV for anything, XLSX for a person, JSON for a system. Each writes
the same rows from the same query the grid is showing, so an export can never
disagree with the screen it was started from.

Nothing here changes how an invoice is approved. It reads the sheet.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime, timezone
from typing import Any

from . import sheet_columns

logger = logging.getLogger(__name__)

# Rows are fetched a page at a time so a large export never holds the whole
# result set in memory at once.
PAGE_SIZE = 500

# Above this, the request returns immediately and the work continues in the
# background. Below it, waiting is faster than polling.
INLINE_ROW_LIMIT = 2_000

FORMATS = ("csv", "xlsx", "json")

CONTENT_TYPES = {
    "csv": "text/csv",
    "json": "application/json",
    "xlsx": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ),
}


class ExportFailed(RuntimeError):
    """The export cannot be produced. Recorded on the job, not raised at a user."""


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


def formula_safe(text: str) -> str:
    """Text a spreadsheet will show, not run as a formula.

    Extraction text is untrusted: a vendor or description of
    "=HYPERLINK(...)" would otherwise execute when the file is opened. A
    numeric string (a credit's "-85.0") is left alone so it stays a number.
    """
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        try:
            float(text)
        except ValueError:
            return "'" + text
    return text


def _cell(value: Any) -> Any:
    """One value, as a spreadsheet should carry it.

    Lists become a comma-separated string because a cell holds one value; a
    quality-flag column is read, not computed on. Strings are made
    formula-safe; numbers pass through so totals can still be summed.
    """
    if isinstance(value, list):
        value = ", ".join(str(item) for item in value)
    if isinstance(value, str):
        return formula_safe(value)
    return value


def write_csv(columns: list[str], rows: list[dict], labels: dict) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([labels.get(key, key) for key in columns])
    for row in rows:
        writer.writerow(
            [
                "" if row.get(key) is None else _cell(row.get(key))
                for key in columns
            ]
        )
    # BOM: Excel opens a plain UTF-8 CSV as Latin-1 and turns a vendor called
    # "Café" into mojibake, which looks like the export corrupted the data.
    return buffer.getvalue().encode("utf-8-sig")


def write_json(columns: list[str], rows: list[dict], labels: dict) -> bytes:
    payload = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "columns": [
            {"key": key, "label": labels.get(key, key)} for key in columns
        ],
        "rows": [{key: row.get(key) for key in columns} for row in rows],
    }
    return json.dumps(payload, indent=2, default=str).encode("utf-8")


def write_xlsx(columns: list[str], rows: list[dict], labels: dict) -> bytes:
    from openpyxl import Workbook  # noqa: PLC0415
    from openpyxl.styles import Font  # noqa: PLC0415
    from openpyxl.utils import get_column_letter  # noqa: PLC0415

    book = Workbook()
    sheet = book.active
    sheet.title = "Approved invoices"

    sheet.append([labels.get(key, key) for key in columns])
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    # The header stays put while a hundred rows scroll under it, and the
    # filter arrows are what a finance user reaches for first.
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = (
        f"A1:{get_column_letter(len(columns))}{max(len(rows) + 1, 2)}"
    )

    money = {
        column.key
        for column in sheet_columns.COLUMNS
        if column.type in ("money", "number")
    }
    for row in rows:
        sheet.append([_cell(row.get(key)) for key in columns])

    for index, key in enumerate(columns, start=1):
        letter = get_column_letter(index)
        # Numbers as numbers, so a total can be summed in the spreadsheet
        # rather than re-typed. Dates stay strings: the source columns are
        # strings, and inventing a date type would assert a precision the
        # extraction never had.
        if key in money:
            for cell in sheet[letter][1:]:
                cell.number_format = "#,##0.00"
        width = (
            max(
                len(str(labels.get(key, key))),
                *(len(str(_cell(row.get(key)) or "")) for row in rows[:200]),
            )
            if rows
            else len(str(labels.get(key, key)))
        )
        sheet.column_dimensions[letter].width = min(max(width + 2, 10), 60)

    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


WRITERS = {"csv": write_csv, "json": write_json, "xlsx": write_xlsx}


# ---------------------------------------------------------------------------
# Running a job
# ---------------------------------------------------------------------------


def resolve_columns(requested: list[str] | None) -> list[str]:
    """Which columns the file carries.

    An unknown column is dropped rather than failing the export: a saved view
    written before a column was renamed should still produce a file.
    """
    if not requested:
        return [
            column.key for column in sheet_columns.COLUMNS if column.default
        ]
    known = [key for key in requested if key in sheet_columns.BY_KEY]
    return known or [
        column.key for column in sheet_columns.COLUMNS if column.default
    ]


def collect_rows(sheet, tenant_id: str, spec: dict) -> list[dict]:
    """Every row the export covers, a page at a time.

    `case_ids` wins over the filters: when a person has ticked rows, that
    selection is the request, and re-running the filter could return something
    else if an invoice was approved in between.
    """
    filters = dict(spec.get("filters") or {})
    selected = spec.get("case_ids") or []
    rows: list[dict] = []
    offset = 0
    while True:
        page, total = sheet.page(
            tenant_id,
            dict(filters, offset=offset),
            spec.get("sort") or "approved_at",
            spec.get("order") or "desc",
            PAGE_SIZE,
        )
        rows.extend(page)
        offset += PAGE_SIZE
        if not page or offset >= total:
            break

    if selected:
        wanted = set(selected)
        rows = [row for row in rows if row.get("case_id") in wanted]
    return rows


def run(job_id: str, tenant_id: str) -> dict:
    """Produce the file for one job and record what happened.

    Failures are written to the job rather than raised: the caller is either a
    background task with nobody to tell, or a request that has already been
    answered. A job that says why it failed is the only useful outcome.
    """
    from .api import service  # noqa: PLC0415
    from .db.repositories import PgExportJobs, SheetRepository  # noqa: PLC0415
    from .ports import RunContext  # noqa: PLC0415

    jobs = PgExportJobs()
    job = jobs.get(tenant_id, job_id)
    if job is None:
        raise ExportFailed(f"No export job {job_id}")

    jobs.update(tenant_id, job_id, status="running")
    try:
        spec = job["spec"] or {}
        columns = resolve_columns(spec.get("columns"))
        labels = {column.key: column.label for column in sheet_columns.COLUMNS}
        rows = collect_rows(SheetRepository(), tenant_id, spec)

        writer = WRITERS.get(job["format"])
        if writer is None:
            raise ExportFailed(f"Unknown format {job['format']}")
        payload = writer(columns, rows, labels)

        key = f"exports/{job_id}.{job['format']}"
        service.get_blobs(tenant_id).put(
            key, payload, content_type=CONTENT_TYPES[job["format"]]
        )
        finished = jobs.update(
            tenant_id,
            job_id,
            status="done",
            row_count=len(rows),
            storage_key=key,
            finished_at=datetime.now(timezone.utc),
        )
        service.get_audit().emit(
            RunContext(tenant_id=tenant_id, actor=job["requested_by"]),
            "export_completed",
            {"export": job_id, "format": job["format"], "rows": len(rows)},
        )
        return finished
    except Exception as exc:
        logger.exception("Export %s failed", job_id)
        jobs.update(
            tenant_id,
            job_id,
            status="failed",
            error=str(exc)[:2000],
            finished_at=datetime.now(timezone.utc),
        )
        try:
            service.get_audit().emit(
                RunContext(tenant_id=tenant_id, actor=job["requested_by"]),
                "export_failed",
                {"export": job_id, "error": str(exc)[:500]},
            )
        except Exception:
            # The export already failed; failing to record that must not
            # replace its error with a different one.
            logger.warning("Could not audit the failed export %s", job_id)
        return jobs.get(tenant_id, job_id) or {}
