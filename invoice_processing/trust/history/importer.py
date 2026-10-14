"""One file, one transaction: it lands whole (minus named bad rows) or not at all."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select

from ...db.models import HistoryRecord, TrustHistoryImport
from ...db.repositories import audit_event_row
from ...db.session import tenant_session
from ...ports import RunContext
from . import mapping as mp
from .readers import read_table

_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d/%m/%y",
    "%Y/%m/%d",
    "%d %b %Y",
    "%d-%b-%Y",
)
_MAX_LEN = {"vendor_key": 512, "currency": 8}


@dataclass(frozen=True)
class ImportResult:
    import_id: str
    status: str
    rows_ok: int
    rows_bad: int
    errors: list[dict]
    duplicate_of: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def parse_amount(text: str) -> Decimal:
    raw = str(text).strip()
    negative = raw.startswith("(") and raw.endswith(")")
    # Drops currency symbols and grouping commas, Indian or Western.
    cleaned = re.sub(r"[^\d.\-]", "", raw)
    if not _NUMBER.match(cleaned):
        raise ValueError(f"{raw!r} is not a number")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(f"{raw!r} is not a number") from None
    return -value if negative else value


def parse_date(text: str) -> date:
    raw = str(text).strip()
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"{raw!r} is not a date")


def _normalise(fields: dict[str, str]) -> dict:
    """Typed columns from the mapped fields; raises ValueError naming the field."""
    out: dict = {}
    if "amount" in fields:
        try:
            out["amount"] = parse_amount(fields["amount"])
        except ValueError as exc:
            raise ValueError(f"amount: {exc}") from None
    if fields.get("record_date"):
        try:
            out["record_date"] = parse_date(fields["record_date"])
        except ValueError as exc:
            raise ValueError(f"record_date: {exc}") from None
    for key in ("external_id", "doc_number", "vendor_key", "currency"):
        if fields.get(key):
            out[key] = fields[key].strip()[: _MAX_LEN.get(key, 128)]
    return out


def _check_required(kind: str, typed: dict) -> None:
    for field in mp.REQUIRED[kind]:
        if field == "amount":
            if "amount" not in typed:
                raise ValueError("amount: missing")
        elif not typed.get(field):
            raise ValueError(f"{field}: missing")


class HistoryImporter:
    def __init__(self, url: str | None = None):
        self.url = url

    def import_file(
        self,
        ctx: RunContext,
        *,
        kind: str,
        source_name: str,
        filename: str,
        data: bytes,
        mapping: dict[str, str],
    ) -> ImportResult:
        headers, rows = read_table(data, filename)
        problems = mp.validate(kind, mapping, headers)
        if problems:
            raise ValueError("; ".join(problems))
        sha = hashlib.sha256(data).hexdigest()
        with tenant_session(ctx.tenant_id, self.url) as s:
            existing = s.scalars(
                select(TrustHistoryImport).where(
                    TrustHistoryImport.tenant_id == ctx.tenant_id,
                    TrustHistoryImport.file_sha256 == sha,
                )
            ).first()
            if existing is not None:
                return ImportResult(
                    existing.id,
                    existing.status,
                    0,
                    0,
                    [],
                    duplicate_of=existing.id,
                )

            run = TrustHistoryImport(
                tenant_id=ctx.tenant_id,
                source_name=source_name,
                kind=kind,
                file_sha256=sha,
                filename=filename,
                mapping=dict(mapping),
                created_by=ctx.actor or None,
            )
            s.add(run)
            s.flush()
            errors: list[dict] = []
            ok = 0
            for index, raw in enumerate(rows, start=2):  # the header is row 1
                mapped = {
                    field: raw.get(header, "")
                    for header, field in mapping.items()
                    if field
                }
                try:
                    typed = _normalise(mapped)
                    _check_required(kind, typed)
                except ValueError as exc:
                    errors.append({"row": index, "reason": str(exc)})
                    continue
                s.add(
                    HistoryRecord(
                        tenant_id=ctx.tenant_id,
                        import_id=run.id,
                        kind=kind,
                        row_number=index,
                        external_id=typed.get("external_id"),
                        fields=raw,
                        amount=typed.get("amount"),
                        currency=typed.get("currency"),
                        record_date=typed.get("record_date"),
                        vendor_key=typed.get("vendor_key"),
                        doc_number=typed.get("doc_number"),
                    )
                )
                ok += 1
            run.rows_ok, run.rows_bad, run.errors = ok, len(errors), errors
            run.status = "partial" if errors else "complete"
            s.add(
                audit_event_row(
                    ctx,
                    "trust_history_imported",
                    {
                        "import_id": run.id,
                        "kind": kind,
                        "source_name": source_name,
                        "rows_ok": ok,
                        "rows_bad": len(errors),
                    },
                )
            )
            return ImportResult(run.id, run.status, ok, len(errors), errors)

    def saved_mapping(
        self, tenant_id: str, source_name: str, kind: str
    ) -> dict | None:
        with tenant_session(tenant_id, self.url) as s:
            row = s.scalars(
                select(TrustHistoryImport)
                .where(
                    TrustHistoryImport.tenant_id == tenant_id,
                    TrustHistoryImport.source_name == source_name,
                    TrustHistoryImport.kind == kind,
                )
                .order_by(TrustHistoryImport.created_at.desc())
            ).first()
            return dict(row.mapping) if row else None

    def list_imports(self, tenant_id: str) -> list[dict]:
        with tenant_session(tenant_id, self.url) as s:
            rows = s.scalars(
                select(TrustHistoryImport)
                .where(TrustHistoryImport.tenant_id == tenant_id)
                .order_by(TrustHistoryImport.created_at.desc())
            )
            return [
                {
                    "id": r.id,
                    "source_name": r.source_name,
                    "kind": r.kind,
                    "filename": r.filename,
                    "status": r.status,
                    "rows_ok": r.rows_ok,
                    "rows_bad": r.rows_bad,
                    "errors": list(r.errors or []),
                    "created_at": r.created_at.isoformat()
                    if r.created_at
                    else None,
                    "created_by": r.created_by,
                }
                for r in rows
            ]
