"""Postgres implementations of the storage ports.

These satisfy the same protocols as the filesystem adapters from Phase 0.5, so
the engines move from local files to a database without changing.

Every query filters on `tenant_id` explicitly, in addition to the row-level
security policies. That redundancy is the point: RLS catches a forgotten filter,
and the filter catches a misconfigured policy. Relying on either alone puts one
mistake between a customer and another customer's invoices.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator

from sqlalchemy import select

from ..ports import RuleBaseConflict, RunContext
from .models import (
    AuditEvent,
    Correction,
    Document,
    Finding,
    Invoice,
    InvoiceLine,
    PipelineRun,
    Rule,
    Tenant,
    User,
)
from .session import tenant_session


class PgCaseRepository:
    """Case artifacts, stored as the invoice row plus its pipeline run.

    Artifacts live in the invoice's `fields` JSON rather than as ten separate
    blobs: they are read together, always, and a single row keeps a case
    atomically consistent.
    """

    def __init__(self, url: str | None = None):
        self.url = url

    def get_artifacts(self, ctx: RunContext) -> dict[str, dict]:
        with tenant_session(ctx.tenant_id, self.url) as session:
            invoice = session.scalar(
                select(Invoice).where(
                    Invoice.tenant_id == ctx.tenant_id,
                    Invoice.case_id == ctx.case_id,
                )
            )
            return dict(invoice.fields or {}) if invoice else {}

    def put_artifact(self, ctx: RunContext, key: str, payload: dict) -> None:
        with tenant_session(ctx.tenant_id, self.url) as session:
            invoice = session.scalar(
                select(Invoice).where(
                    Invoice.tenant_id == ctx.tenant_id,
                    Invoice.case_id == ctx.case_id,
                )
            )
            if invoice is None:
                invoice = Invoice(
                    tenant_id=ctx.tenant_id, case_id=ctx.case_id, fields={}
                )
                session.add(invoice)
            # Reassign rather than mutate: SQLAlchemy does not track in-place
            # edits to a JSON column, so a mutation would silently not persist.
            invoice.fields = {**(invoice.fields or {}), key: payload}

    def project_case(self, ctx: RunContext, artifacts: dict) -> None:
        """Mirror a finished case into the columns and tables that answer
        questions.

        The artifacts stay the record; this is the queryable view of them.
        Without it every typed column on `invoices` was NULL in production --
        both documented indexes indexed nothing, the review queue had to be
        sorted in Python after loading every case, and `invoice_lines`,
        `findings` and `pipeline_runs` held no rows at all despite being
        designed, migrated and deployed.

        Idempotent: lines and findings are replaced, not appended, so a retry
        of the same case does not double them. A projection failure must never
        cost the artifacts, so the caller runs this after they are stored.
        """
        from .. import case_projection  # noqa: PLC0415

        columns = case_projection.invoice_columns(artifacts)
        with tenant_session(ctx.tenant_id, self.url) as session:
            invoice = session.scalar(
                select(Invoice).where(
                    Invoice.tenant_id == ctx.tenant_id,
                    Invoice.case_id == ctx.case_id,
                )
            )
            if invoice is None:
                invoice = Invoice(
                    tenant_id=ctx.tenant_id, case_id=ctx.case_id, fields={}
                )
                session.add(invoice)
                session.flush()

            for column, value in columns.items():
                setattr(invoice, column, value)

            # The case id *is* the document id everywhere in this system, so
            # the link needs no extra bookkeeping -- but it is only set when
            # that document really exists, because a dangling foreign key
            # fails the whole write.
            if invoice.document_id is None:
                document = session.scalar(
                    select(Document).where(
                        Document.tenant_id == ctx.tenant_id,
                        Document.id == ctx.case_id,
                    )
                )
                if document is not None:
                    invoice.document_id = document.id

            session.query(InvoiceLine).filter(
                InvoiceLine.tenant_id == ctx.tenant_id,
                InvoiceLine.invoice_id == invoice.id,
            ).delete(synchronize_session=False)
            for line in case_projection.lines(artifacts):
                session.add(
                    InvoiceLine(
                        tenant_id=ctx.tenant_id, invoice_id=invoice.id, **line
                    )
                )

            session.query(Finding).filter(
                Finding.tenant_id == ctx.tenant_id,
                Finding.invoice_id == invoice.id,
            ).delete(synchronize_session=False)
            for finding in case_projection.findings(artifacts):
                session.add(
                    Finding(
                        tenant_id=ctx.tenant_id,
                        invoice_id=invoice.id,
                        phase=finding["phase"],
                        step=finding["step"],
                        rule_ref=finding["rule"],
                        severity=case_projection.severity_of(finding),
                        passed=finding["passed"],
                        evidence=finding["evidence"],
                    )
                )

            run = case_projection.run(artifacts)
            if run is not None:
                session.add(
                    PipelineRun(
                        tenant_id=ctx.tenant_id,
                        invoice_id=invoice.id,
                        master_data_version=None,
                        **run,
                    )
                )

    def iter_case_ids(self, tenant_id: str) -> Iterator[str]:
        """Deterministic order -- impact assessment depends on it."""
        with tenant_session(tenant_id, self.url) as session:
            rows = session.scalars(
                select(Invoice.case_id)
                .where(
                    Invoice.tenant_id == tenant_id, Invoice.case_id.is_not(None)
                )
                .order_by(Invoice.case_id)
            ).all()
        return iter(rows)

    def get_many_artifacts(
        self, tenant_id: str, case_ids: list[str]
    ) -> dict[str, dict[str, dict]]:
        """One query, not N. Impact assessment scans every case."""
        if not case_ids:
            return {}
        with tenant_session(tenant_id, self.url) as session:
            rows = session.scalars(
                select(Invoice).where(
                    Invoice.tenant_id == tenant_id,
                    Invoice.case_id.in_(case_ids),
                )
            ).all()
            return {row.case_id: dict(row.fields or {}) for row in rows}


class PgRuleRepository:
    """The ALF rule base, per tenant, with optimistic concurrency.

    Version is a hash of the stored rules, so a write made by any path -- API,
    another session, a migration -- invalidates a stale writer's expectation.
    """

    def __init__(self, url: str | None = None):
        self.url = url

    @staticmethod
    def _version(rules: list[dict]) -> int:
        payload = json.dumps(rules, sort_keys=True, default=str)
        return int(hashlib.sha256(payload.encode()).hexdigest()[:12], 16)

    @staticmethod
    def _to_dict(rule: Rule) -> dict:
        return {
            "id": rule.rule_id,
            "name": rule.name,
            "description": rule.description,
            "scope": rule.scope,
            "priority": rule.priority,
            "enabled": rule.enabled,
            "conditions": rule.conditions,
            "actions": rule.actions,
            "metadata": rule.rule_metadata,
        }

    def load(self, tenant_id: str) -> tuple[dict, int]:
        with tenant_session(tenant_id, self.url) as session:
            rows = session.scalars(
                select(Rule)
                .where(Rule.tenant_id == tenant_id)
                .order_by(Rule.priority, Rule.rule_id)
            ).all()
            rules = [self._to_dict(r) for r in rows]
        return (
            {"schema_version": "2.0.0", "metadata": {}, "rules": rules},
            self._version(rules),
        )

    def save(self, tenant_id: str, doc: dict, *, expected_version: int) -> int:
        incoming = doc.get("rules", [])
        with tenant_session(tenant_id, self.url) as session:
            existing = session.scalars(
                select(Rule).where(Rule.tenant_id == tenant_id)
            ).all()
            current = self._version(
                sorted(
                    (self._to_dict(r) for r in existing),
                    key=lambda r: (r["priority"], r["id"]),
                )
            )
            if current != expected_version:
                raise RuleBaseConflict(
                    "The rule base changed since it was read "
                    f"(expected {expected_version}, found {current}). "
                    "Re-read and reapply rather than overwriting."
                )

            by_id = {r.rule_id: r for r in existing}
            for payload in incoming:
                rule = by_id.get(payload["id"])
                if rule is None:
                    rule = Rule(
                        tenant_id=tenant_id, rule_id=payload["id"], name=""
                    )
                    session.add(rule)
                rule.name = payload.get("name", "")
                rule.description = payload.get("description")
                rule.scope = payload.get("scope", "global")
                rule.priority = payload.get("priority", 100)
                rule.enabled = payload.get("enabled", True)
                rule.conditions = payload.get("conditions", [])
                rule.actions = payload.get("actions", [])
                rule.rule_metadata = payload.get("metadata", {})

        return self._version(incoming)

    def next_rule_id(self, tenant_id: str) -> str:
        """Allocated from stored rules; the version check makes racing visible."""
        with tenant_session(tenant_id, self.url) as session:
            ids = session.scalars(
                select(Rule.rule_id).where(Rule.tenant_id == tenant_id)
            ).all()
        highest = 0
        for rule_id in ids:
            if str(rule_id).startswith("ALF-"):
                try:
                    highest = max(highest, int(str(rule_id).split("-")[1]))
                except (IndexError, ValueError):
                    continue
        return f"ALF-{highest + 1:03d}"


class PgDocumentRepository:
    """Ingested documents, keyed by content.

    `find_by_sha` is what makes ingestion idempotent: a provider retry or a
    double-clicked upload finds the existing row instead of creating a second
    invoice to pay.
    """

    def __init__(self, url: str | None = None):
        self.url = url

    def find_by_sha(self, tenant_id: str, sha256: str) -> dict | None:
        with tenant_session(tenant_id, self.url) as session:
            document = session.scalar(
                select(Document).where(
                    Document.tenant_id == tenant_id,
                    Document.sha256 == sha256,
                )
            )
            if document is None:
                return None
            return {
                "id": document.id,
                "storage_key": document.storage_key,
                "filename": document.filename,
                "sha256": document.sha256,
            }

    def create(self, tenant_id: str, **fields) -> str:
        with tenant_session(tenant_id, self.url) as session:
            document = Document(tenant_id=tenant_id, **fields)
            session.add(document)
            session.flush()
            return document.id

    def list_documents(self, tenant_id: str, limit: int = 200) -> list[dict]:
        """Every ingested document for a tenant, newest first."""
        with tenant_session(tenant_id, self.url) as session:
            rows = session.scalars(
                select(Document)
                .where(Document.tenant_id == tenant_id)
                .order_by(Document.created_at.desc())
                .limit(limit)
            ).all()
            return [
                {
                    "id": d.id,
                    "storage_key": d.storage_key,
                    "sha256": d.sha256,
                    "filename": d.filename,
                    "byte_size": d.byte_size,
                    "source": d.source,
                    "source_ref": d.source_ref,
                    "created_at": d.created_at.isoformat()
                    if d.created_at
                    else None,
                }
                for d in rows
            ]

    def tenant_id_for_slug(self, slug: str) -> str | None:
        """Resolve an inbound-email local part to a tenant.

        Not tenant-scoped -- it is what *decides* the tenant. Kept to an exact
        match on an active tenant so a lookup can never widen into a scan that
        returns somebody else.
        """
        with tenant_session("", self.url) as session:
            tenant = session.scalar(
                select(Tenant).where(
                    Tenant.slug == slug, Tenant.status == "active"
                )
            )
            return tenant.id if tenant else None


class PgAuditSink:
    """Append-only audit trail. Durable on call."""

    def __init__(self, url: str | None = None):
        self.url = url

    def emit(self, ctx: RunContext, event_type: str, payload: dict) -> None:
        with tenant_session(ctx.tenant_id, self.url) as session:
            session.add(
                AuditEvent(
                    tenant_id=ctx.tenant_id,
                    actor=ctx.actor,
                    actor_type="user" if ctx.actor else "system",
                    action=event_type,
                    entity_type="invoice" if ctx.case_id else None,
                    entity_id=ctx.case_id or None,
                    after=payload,
                    request_id=ctx.run_id or None,
                )
            )

    def query(self, tenant_id: str, limit: int = 100) -> list[AuditEvent]:
        with tenant_session(tenant_id, self.url) as session:
            return list(
                session.scalars(
                    select(AuditEvent)
                    .where(AuditEvent.tenant_id == tenant_id)
                    .order_by(AuditEvent.occurred_at.desc())
                    .limit(limit)
                ).all()
            )


class PgCorrectionStore:
    """Corrections in Postgres, where they survive a deploy.

    They were appended to a file on the API container's local disk, so the
    "asset that compounds" was erased by every redeploy and split in half by a
    second replica. The audit trail recorded that fields were corrected and by
    whom; the values themselves -- the part a model could learn from -- were
    only ever in that file.
    """

    def __init__(self, url: str | None = None):
        self.url = url

    def record(self, ctx: RunContext, correction: dict) -> None:
        with tenant_session(ctx.tenant_id, self.url) as session:
            invoice = session.scalar(
                select(Invoice).where(
                    Invoice.tenant_id == ctx.tenant_id,
                    Invoice.case_id == ctx.case_id,
                )
            )
            if invoice is None:
                invoice = Invoice(
                    tenant_id=ctx.tenant_id, case_id=ctx.case_id, fields={}
                )
                session.add(invoice)
                session.flush()

            # The row references a user, and the actor arrives as an email --
            # so it is resolved here rather than stored twice. An actor with
            # no user row keeps the correction and loses only the attribution,
            # which the audit trail still holds.
            user_id = session.scalar(
                select(User.id).where(
                    User.tenant_id == ctx.tenant_id,
                    User.email == correction.get("actor"),
                )
            )
            session.add(
                Correction(
                    tenant_id=ctx.tenant_id,
                    invoice_id=invoice.id,
                    field_path=str(correction.get("field_path"))[:255],
                    old_value=_as_text(correction.get("old_value")),
                    new_value=_as_text(correction.get("new_value")),
                    user_id=user_id,
                    source=correction.get("source", "review"),
                )
            )

    def list(self, tenant_id: str, case_id: str | None = None) -> list[dict]:
        with tenant_session(tenant_id, self.url) as session:
            query = (
                select(Correction, Invoice.case_id, User.email)
                .join(Invoice, Correction.invoice_id == Invoice.id)
                .outerjoin(User, Correction.user_id == User.id)
                .where(Correction.tenant_id == tenant_id)
                .order_by(Correction.created_at.desc())
            )
            if case_id is not None:
                query = query.where(Invoice.case_id == case_id)
            return [
                {
                    "case_id": row_case_id,
                    "field_path": correction.field_path,
                    "old_value": correction.old_value,
                    "new_value": correction.new_value,
                    "actor": email,
                    "tenant_id": tenant_id,
                    "at": correction.created_at.isoformat()
                    if correction.created_at
                    else None,
                }
                for correction, row_case_id, email in session.execute(query)
            ]


def _as_text(value) -> str | None:
    """Corrections are compared and read by people, so they are stored as text.

    A field's type is whatever the model extracted -- a number, a date string,
    None. Storing the rendering keeps "what it was" answerable without knowing
    which of those it happened to be.
    """
    if value is None:
        return None
    return value if isinstance(value, str) else json.dumps(value, default=str)


def record_pipeline_run(ctx: RunContext, **fields) -> None:
    """Persist reproducibility metadata for one run."""
    with tenant_session(ctx.tenant_id) as session:
        session.add(PipelineRun(tenant_id=ctx.tenant_id, **fields))
