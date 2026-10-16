"""The only writer of trust_audits, trust_audit_runs and trust_audit_samples.

Every read and write goes through `tenant_session`, and every row is looked
up by (tenant_id, id): a guessed id from another tenant is a None, never a row.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ...db.models import TrustAudit, TrustAuditRun, TrustAuditSample
from ...db.session import tenant_session
from ...ports import RunContext
from .sampling import precision

logger = logging.getLogger(__name__)

AUDIT_STATUSES = ("draft", "active", "closed")
RUN_STATUSES = ("queued", "running", "done", "failed")
# A run the worker never picked up, or never finished, stops blocking the
# engagement after this long (review item M6): it is marked failed instead.
QUEUED_FOR = timedelta(minutes=30)
RUNNING_FOR = timedelta(hours=2)
EMPTY_PROGRESS = {
    "engines_done": 0,
    "engines_total": 0,
    "engine": None,
    "findings_so_far": 0,
    "per_engine": {},
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)  # SQLite drops the zone
    return value


def _iso(value) -> str | None:
    """A datetime or a date as ISO text."""
    return None if value is None else _aware(value).isoformat()


def _audit_dict(a: TrustAudit) -> dict:
    return {
        "id": a.id,
        "name": a.name,
        "status": a.status,
        "period_from": _iso(a.period_from),
        "period_to": _iso(a.period_to),
        "vendors": list(a.vendors or []),
        "categories": list(a.categories or []),
        "terms": dict(a.terms or {}),
        "approver": a.approver,
        "created_by": a.created_by,
        "created_at": _iso(a.created_at),
        "updated_at": _iso(a.updated_at),
    }


def _run_dict(r: TrustAuditRun) -> dict:
    return {
        "id": r.id,
        "audit_id": r.audit_id,
        "status": r.status,
        "engines": list(r.engines or []),
        "progress": {**EMPTY_PROGRESS, **(r.progress or {})},
        "error": r.error,
        "requested_by": r.requested_by,
        "started_at": _iso(r.started_at),
        "finished_at": _iso(r.finished_at),
        "created_at": _iso(r.created_at),
    }


def _sample_dict(s: TrustAuditSample) -> dict:
    verdicts = dict(s.verdicts or {})
    return {
        "id": s.id,
        "audit_id": s.audit_id,
        "size": s.size,
        "seed": s.seed,
        "finding_ids": list(s.finding_ids or []),
        "verdicts": verdicts,
        "created_by": s.created_by,
        "created_at": _iso(s.created_at),
        **precision(verdicts),
    }


class TrustAuditStore:
    def __init__(self, url: str | None = None):
        self.url = url

    # -- audits ------------------------------------------------------------

    def create(self, ctx: RunContext, **fields) -> dict:
        with tenant_session(ctx.tenant_id, self.url) as s:
            row = TrustAudit(
                tenant_id=ctx.tenant_id, created_by=ctx.actor, **fields
            )
            s.add(row)
            s.flush()
            return _audit_dict(row)

    def _audit(self, s, tenant_id: str, audit_id: str) -> TrustAudit | None:
        return s.scalars(
            select(TrustAudit).where(
                TrustAudit.tenant_id == tenant_id, TrustAudit.id == audit_id
            )
        ).first()

    def get(self, tenant_id: str, audit_id: str) -> dict | None:
        with tenant_session(tenant_id, self.url) as s:
            row = self._audit(s, tenant_id, audit_id)
            return _audit_dict(row) if row else None

    def list(self, tenant_id: str) -> list[dict]:
        with tenant_session(tenant_id, self.url) as s:
            q = (
                select(TrustAudit)
                .where(TrustAudit.tenant_id == tenant_id)
                .order_by(TrustAudit.created_at.desc(), TrustAudit.id)
            )
            return [_audit_dict(a) for a in s.scalars(q)]

    def update(self, tenant_id: str, audit_id: str, **fields) -> dict | None:
        with tenant_session(tenant_id, self.url) as s:
            row = self._audit(s, tenant_id, audit_id)
            if row is None:
                return None
            for k, v in fields.items():
                setattr(row, k, v)
            row.updated_at = _now()
            s.flush()
            return _audit_dict(row)

    # -- runs --------------------------------------------------------------

    def create_run(
        self,
        ctx: RunContext,
        audit_id: str,
        *,
        engines: list[str],
        requested_by: str | None,
        now: datetime | None = None,
    ) -> dict:
        with tenant_session(ctx.tenant_id, self.url) as s:
            row = TrustAuditRun(
                tenant_id=ctx.tenant_id,
                audit_id=audit_id,
                status="queued",
                engines=list(engines),
                progress=dict(EMPTY_PROGRESS),
                requested_by=requested_by,
                **({"created_at": now} if now else {}),
            )
            s.add(row)
            s.flush()
            return _run_dict(row)

    def _run(self, s, tenant_id: str, run_id: str) -> TrustAuditRun | None:
        return s.scalars(
            select(TrustAuditRun).where(
                TrustAuditRun.tenant_id == tenant_id, TrustAuditRun.id == run_id
            )
        ).first()

    def get_run(self, tenant_id: str, run_id: str) -> dict | None:
        with tenant_session(tenant_id, self.url) as s:
            row = self._run(s, tenant_id, run_id)
            return _run_dict(row) if row else None

    def runs(self, tenant_id: str, audit_id: str) -> list[dict]:
        with tenant_session(tenant_id, self.url) as s:
            q = (
                select(TrustAuditRun)
                .where(
                    TrustAuditRun.tenant_id == tenant_id,
                    TrustAuditRun.audit_id == audit_id,
                )
                .order_by(TrustAuditRun.created_at.desc(), TrustAuditRun.id)
            )
            return [_run_dict(r) for r in s.scalars(q)]

    def blocking_run(
        self, tenant_id: str, audit_id: str, *, now: datetime | None = None
    ) -> dict | None:
        """The queued or running run that stops a new one, if any. A run
        past its deadline is marked failed ("abandoned") and blocks nothing."""
        now = now or _now()
        with tenant_session(tenant_id, self.url) as s:
            q = select(TrustAuditRun).where(
                TrustAuditRun.tenant_id == tenant_id,
                TrustAuditRun.audit_id == audit_id,
                TrustAuditRun.status.in_(("queued", "running")),
            )
            blocking = None
            for row in s.scalars(q):
                since = (
                    _aware(row.started_at)
                    if row.status == "running"
                    else _aware(row.created_at)
                ) or now
                limit = RUNNING_FOR if row.status == "running" else QUEUED_FOR
                if now - since > limit:
                    logger.warning(
                        "trust_audit_run_abandoned", extra={"was": row.status}
                    )
                    row.status, row.error, row.finished_at = (
                        "failed",
                        "abandoned",
                        now,
                    )
                else:
                    blocking = _run_dict(row)
            s.flush()
            return blocking

    def start(
        self, tenant_id: str, run_id: str, *, now: datetime | None = None
    ) -> dict | None:
        """queued|failed -> running; running stays running (a re-run); done
        returns None so the caller does nothing."""
        with tenant_session(tenant_id, self.url) as s:
            row = self._run(s, tenant_id, run_id)
            if row is None or row.status == "done":
                return None
            row.status = "running"
            row.error = None
            row.started_at = now or _now()
            row.finished_at = None
            row.progress = dict(EMPTY_PROGRESS)
            s.flush()
            return _run_dict(row)

    def progress(self, tenant_id: str, run_id: str):
        """An `on_progress` callback for `evaluate`; it never raises."""

        def record(p: dict) -> None:
            try:
                with tenant_session(tenant_id, self.url) as s:
                    row = self._run(s, tenant_id, run_id)
                    if row is None:
                        return
                    per_engine = dict(
                        (row.progress or {}).get("per_engine") or {}
                    )
                    per_engine[p["engine"]] = p["report"]
                    row.progress = {
                        "engines_done": p["done"],
                        "engines_total": p["total"],
                        "engine": p["engine"],
                        "findings_so_far": p["findings"],
                        "per_engine": per_engine,
                    }
            except Exception:
                logger.warning("trust_audit_progress_lost", exc_info=True)

        return record

    def finish(
        self,
        tenant_id: str,
        run_id: str,
        *,
        status: str,
        per_engine: dict | None = None,
        error: str | None = None,
        now: datetime | None = None,
    ) -> dict | None:
        with tenant_session(tenant_id, self.url) as s:
            row = self._run(s, tenant_id, run_id)
            if row is None:
                return None
            row.status = status
            row.error = error[:500] if error else None
            row.finished_at = now or _now()
            if per_engine is not None:
                row.progress = {
                    **EMPTY_PROGRESS,
                    **(row.progress or {}),
                    "per_engine": per_engine,
                }
            s.flush()
            return _run_dict(row)

    # -- samples -----------------------------------------------------------

    def create_sample(
        self,
        ctx: RunContext,
        audit_id: str,
        *,
        size: int,
        seed: str | None,
        finding_ids: list[str],
    ) -> dict:
        with tenant_session(ctx.tenant_id, self.url) as s:
            row = TrustAuditSample(
                tenant_id=ctx.tenant_id,
                audit_id=audit_id,
                size=size,
                seed=seed,
                finding_ids=list(finding_ids),
                verdicts=dict.fromkeys(finding_ids),
                created_by=ctx.actor,
            )
            s.add(row)
            s.flush()
            return _sample_dict(row)

    def _sample(self, s, tenant_id: str, sample_id: str):
        return s.scalars(
            select(TrustAuditSample).where(
                TrustAuditSample.tenant_id == tenant_id,
                TrustAuditSample.id == sample_id,
            )
        ).first()

    def get_sample(self, tenant_id: str, sample_id: str) -> dict | None:
        with tenant_session(tenant_id, self.url) as s:
            row = self._sample(s, tenant_id, sample_id)
            return _sample_dict(row) if row else None

    def samples(self, tenant_id: str, audit_id: str) -> list[dict]:
        with tenant_session(tenant_id, self.url) as s:
            q = (
                select(TrustAuditSample)
                .where(
                    TrustAuditSample.tenant_id == tenant_id,
                    TrustAuditSample.audit_id == audit_id,
                )
                .order_by(
                    TrustAuditSample.created_at.desc(), TrustAuditSample.id
                )
            )
            return [_sample_dict(r) for r in s.scalars(q)]

    def set_verdicts(
        self, tenant_id: str, sample_id: str, verdicts: dict
    ) -> dict | None:
        """Merge verdicts for findings in the sample; the route has already
        refused any other id."""
        with tenant_session(tenant_id, self.url) as s:
            row = self._sample(s, tenant_id, sample_id)
            if row is None:
                return None
            row.verdicts = {**(row.verdicts or {}), **verdicts}
            row.updated_at = _now()
            s.flush()
            return _sample_dict(row)
