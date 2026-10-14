"""CSV and XLSX to headers plus rows of strings. Nothing else is interpreted here."""

from __future__ import annotations

import csv
import io
from datetime import date, datetime


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def read_table(
    data: bytes, filename: str
) -> tuple[list[str], list[dict[str, str]]]:
    name = (filename or "").lower()
    if name.endswith(".csv"):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValueError("The file is not UTF-8 text.") from None
        reader = csv.DictReader(io.StringIO(text))
        headers = [
            h.strip() for h in (reader.fieldnames or []) if h and h.strip()
        ]
        rows = [{h: _cell(r.get(h)) for h in headers} for r in reader]
    elif name.endswith(".xlsx"):
        import openpyxl  # noqa: PLC0415 - heavy, and only for spreadsheets

        try:
            wb = openpyxl.load_workbook(
                io.BytesIO(data), read_only=True, data_only=True
            )
        except Exception:
            raise ValueError(
                "The file is not a readable .xlsx workbook."
            ) from None
        try:
            ws = wb.worksheets[0]
            lines = [list(r) for r in ws.iter_rows(values_only=True)]
        finally:
            wb.close()
        lines = [line for line in lines if any(_cell(c) for c in line)]
        if not lines:
            return [], []
        headers = [_cell(c) for c in lines[0]]
        rows = [
            {h: _cell(c) for h, c in zip(headers, line, strict=False) if h}
            for line in lines[1:]
        ]
        headers = [h for h in headers if h]
    else:
        raise ValueError("Upload a .csv or .xlsx file.")
    if not headers:
        raise ValueError("The file has no header row.")
    return headers, rows
