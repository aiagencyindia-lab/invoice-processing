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
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import aliased

from ..ports import RuleBaseConflict, RunContext
from ..sheet_columns import (
    BY_KEY,
    FLAG_STEPS,
    WORKFLOW_PATHS,
    is_sortable,
)
from .models import (
    AuditEvent,
    Correction,
    Document,
    DocumentClaim,
    Finding,
    Invoice,
    InvoiceLine,
    PipelineRun,
    Rule,
    SheetView,
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
        from ..context import validate_rule_base  # noqa: PLC0415

        validate_rule_base(doc)
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


# What an event is about, from what it is called. `entity_type` was set to
# "invoice" whenever a case id happened to be present and left null otherwise,
# so 33 of 44 rows in production said nothing about what they described.
_ENTITY_TYPES = {
    "document": "document",
    "invoice": "invoice",
    "fields": "invoice",
    "email": "document",
    "tenant": "tenant",
    "settings": "settings",
    "purchase": "purchase_order",
}


def entity_type_for(event_type: str, case_id: str = "") -> str | None:
    prefix = event_type.split("_", 1)[0]
    return _ENTITY_TYPES.get(prefix) or ("invoice" if case_id else None)


class PgAuditSink:
    """Append-only audit trail. Durable on call."""

    def __init__(self, url: str | None = None):
        self.url = url

    def emit(
        self,
        ctx: RunContext,
        event_type: str,
        payload: dict,
        before: dict | None = None,
    ) -> None:
        with tenant_session(ctx.tenant_id, self.url) as session:
            session.add(
                AuditEvent(
                    tenant_id=ctx.tenant_id,
                    actor=ctx.actor,
                    actor_type="user" if ctx.actor else "system",
                    action=event_type,
                    entity_type=entity_type_for(event_type, ctx.case_id),
                    entity_id=ctx.case_id or None,
                    before=before,
                    after=payload,
                    request_id=ctx.run_id or None,
                    ip=(ctx.settings or {}).get("ip") or None,
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


class PgClaimStore:
    """Which documents are being processed, visible to every container.

    Interchangeable with the filesystem `ClaimStore`: same four methods, same
    meanings. The difference is where the exclusion happens. `O_EXCL` on a
    local file excludes the other processes on that machine and nobody else,
    so two workers on two containers could both win the same claim and run the
    same extraction side by side -- the duplicate the idempotency design
    exists to prevent. It also meant the API could not see any claim at all,
    which is why a document being extracted right now reported itself as
    "received, but not queued".

    `INSERT ... ON CONFLICT DO NOTHING` is the same idea done where both can
    see it: one row is inserted or none is, atomically, however many processes
    ask at once.
    """

    def __init__(self, url: str | None = None, worker_id: str = ""):
        self.url = url
        self.worker_id = worker_id

    def claim(self, tenant_id: str, document_id: str) -> bool:
        with tenant_session(tenant_id, self.url) as session:
            values = {
                "id": str(uuid.uuid4()),
                "tenant_id": tenant_id,
                "document_id": document_id,
                "state": "running",
                "worker_id": self.worker_id or None,
            }
            if session.get_bind().dialect.name == "postgresql":
                statement = postgresql.insert(DocumentClaim).values(**values)
            else:
                statement = sqlite.insert(DocumentClaim).values(**values)
            result = session.execute(
                statement.on_conflict_do_nothing(
                    index_elements=["tenant_id", "document_id"]
                )
            )
            return bool(result.rowcount)

    def complete(
        self, tenant_id: str, document_id: str, result: str = "done"
    ) -> None:
        with tenant_session(tenant_id, self.url) as session:
            claim = self._row(session, tenant_id, document_id)
            if claim is None:
                session.add(
                    DocumentClaim(
                        tenant_id=tenant_id,
                        document_id=document_id,
                        state=result,
                        worker_id=self.worker_id or None,
                    )
                )
            else:
                claim.state = result

    def release(self, tenant_id: str, document_id: str) -> None:
        """Drop the claim so a retry can pick the work up again."""
        with tenant_session(tenant_id, self.url) as session:
            claim = self._row(session, tenant_id, document_id)
            if claim is not None:
                session.delete(claim)

    def state(self, tenant_id: str, document_id: str) -> str | None:
        with tenant_session(tenant_id, self.url) as session:
            claim = self._row(session, tenant_id, document_id)
            return claim.state if claim else None

    @staticmethod
    def _row(session, tenant_id: str, document_id: str):
        return session.scalar(
            select(DocumentClaim).where(
                DocumentClaim.tenant_id == tenant_id,
                DocumentClaim.document_id == document_id,
            )
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


# ---------------------------------------------------------------------------
# Approved-invoice sheet
# ---------------------------------------------------------------------------


def _workflow_value(key: str):
    """A workflow key as a SQL expression.

    Approval is written by the existing flow into the case's workflow artifact,
    which lives inside `invoices.fields`. Reading it through SQLAlchemy's JSON
    path keeps one query portable: it compiles to `->>` on Postgres, where an
    expression index covers it, and to `json_extract` on SQLite, where the test
    suite runs. Nothing needed to be written differently for this to work.
    """
    return Invoice.fields["workflow"][key].as_string()


class SheetRepository:
    """Queries over approved invoices: filter, sort, paginate, aggregate.

    The only reader of invoices by anything other than case id. Everything it
    returns was produced by the pipeline or by a person approving an invoice;
    this class writes nothing.
    """

    def __init__(self, url: str | None = None):
        self.url = url

    # -- filters ----------------------------------------------------------

    def _base(self, tenant_id: str, filters: dict):

        conditions = [Invoice.tenant_id == tenant_id]

        status = (filters.get("status") or "approved").lower()
        decision = _workflow_value("decision")
        if status == "approved":
            conditions.append(decision == "approve")
        elif status == "rejected":
            conditions.append(decision == "reject")
        elif status == "pending":
            # Processed by the pipeline, awaiting a person.
            conditions.append(decision.is_(None))
        # "all" adds nothing.

        if value := filters.get("q"):
            like = f"%{value.lower()}%"
            conditions.append(
                or_(
                    func.lower(Invoice.vendor_name).like(like),
                    func.lower(Invoice.invoice_number).like(like),
                    func.lower(Invoice.po_number).like(like),
                    func.lower(Invoice.vendor_tax_id).like(like),
                    func.lower(_workflow_value("external_id")).like(like),
                )
            )
        if value := filters.get("vendor"):
            conditions.append(Invoice.vendor_name == value)
        if value := filters.get("currency"):
            conditions.append(Invoice.currency == value)
        # `invoice_date` is a String(32) holding ISO dates, so a lexical
        # comparison is a chronological one. Named as a ceiling, not assumed.
        if value := filters.get("date_from"):
            conditions.append(Invoice.invoice_date >= value)
        if value := filters.get("date_to"):
            conditions.append(Invoice.invoice_date <= value)
        if value := filters.get("approved_from"):
            conditions.append(_workflow_value("decided_at") >= value)
        if value := filters.get("approved_to"):
            conditions.append(_workflow_value("decided_at") <= value)
        if (value := filters.get("min_total")) is not None:
            conditions.append(Invoice.total_inc_tax >= value)
        if (value := filters.get("max_total")) is not None:
            conditions.append(Invoice.total_inc_tax <= value)

        for field, column in (
            ("po", Invoice.po_number),
            ("tax", Invoice.tax_amount),
        ):
            value = filters.get(field)
            if value == "missing":
                conditions.append(column.is_(None))
            elif value == "present":
                conditions.append(column.is_not(None))
            elif value:
                conditions.append(column == value)

        for flag in filters.get("quality") or []:
            condition = self._flag_condition(flag, tenant_id, filters)
            if condition is not None:
                conditions.append(condition)

        return select(Invoice).where(and_(*conditions))

    def _flag_condition(self, flag: str, tenant_id: str, filters: dict):
        """The predicate behind one quality flag, or None if unknown."""

        if flag in FLAG_STEPS:
            return exists(
                select(Finding.id).where(
                    Finding.tenant_id == tenant_id,
                    Finding.invoice_id == Invoice.id,
                    Finding.step == FLAG_STEPS[flag],
                    Finding.passed.is_(False),
                )
            )
        if flag == "duplicate_number":
            other = aliased(Invoice)
            return exists(
                select(other.id).where(
                    other.tenant_id == tenant_id,
                    other.id != Invoice.id,
                    other.invoice_number == Invoice.invoice_number,
                    other.vendor_tax_id == Invoice.vendor_tax_id,
                    Invoice.invoice_number.is_not(None),
                )
            )
        if flag == "edited":
            return exists(
                select(Correction.id).where(
                    Correction.tenant_id == tenant_id,
                    Correction.invoice_id == Invoice.id,
                )
            )
        if flag == "high_value":
            threshold = filters.get("high_value_threshold") or 0
            if not threshold:
                # No threshold configured: the flag matches nothing rather
                # than everything, which is the safe way to be wrong.
                return Invoice.id.is_(None)
            return Invoice.total_inc_tax >= threshold
        if flag == "low_confidence":
            return and_(
                Invoice.min_confidence.is_not(None), Invoice.min_confidence < 1
            )
        if flag == "no_text_layer":
            return (
                Invoice.fields["extraction"]["invoice"]["_evidence"][
                    "_summary"
                ]["has_text_layer"].as_string()
                == "false"
            )
        missing = {
            "missing_po": Invoice.po_number,
            "missing_tax": Invoice.tax_amount,
            "missing_tax_id": Invoice.vendor_tax_id,
            "missing_due_date": Invoice.due_date,
        }
        if flag in missing:
            return missing[flag].is_(None)
        return None

    # -- reads ------------------------------------------------------------

    def page(
        self, tenant_id: str, filters: dict, sort: str, order: str, limit: int
    ) -> tuple[list[dict], int]:
        """One page of rows, plus the total the filters match.

        Offset paging rather than keyset: the grid needs a row count and page
        numbers, the corpus is thousands rather than millions, and a cursor
        that cannot jump to page nine would be a worse grid for a saving no
        one can measure yet.
        """

        statement = self._base(tenant_id, filters)
        offset = max(0, int(filters.get("offset") or 0))

        if not is_sortable(sort):
            sort = "approved_at"
        column = BY_KEY[sort]
        if column.source == "workflow":
            expression = _workflow_value(WORKFLOW_PATHS[sort])
        else:
            expression = getattr(Invoice, sort)
        direction = expression.desc() if order == "desc" else expression.asc()

        with tenant_session(tenant_id, self.url) as session:
            total = session.scalar(
                select(func.count()).select_from(statement.subquery())
            )
            rows = session.scalars(
                statement.order_by(direction, Invoice.id)
                .limit(limit)
                .offset(offset)
            ).all()
            return [
                self._row(session, tenant_id, row, filters) for row in rows
            ], (total or 0)

    def _row(self, session, tenant_id: str, invoice, filters: dict) -> dict:

        workflow = (invoice.fields or {}).get("workflow") or {}
        row: dict = {}
        for key, column in BY_KEY.items():
            if column.source == "column":
                value = getattr(invoice, key, None)
                row[key] = float(value) if isinstance(value, Decimal) else value
            elif column.source == "workflow":
                row[key] = workflow.get(WORKFLOW_PATHS[key])

        row["corrected_fields"] = (
            session.scalar(
                select(func.count(Correction.id)).where(
                    Correction.tenant_id == tenant_id,
                    Correction.invoice_id == invoice.id,
                )
            )
            or 0
        )
        row["quality_flags"] = self._flags_for(
            session, tenant_id, invoice, row, filters
        )
        return row

    def _flags_for(
        self, session, tenant_id: str, invoice, row: dict, filters: dict
    ) -> list[str]:
        """Which quality flags this row carries.

        Computed per row from data already loaded plus one findings read, so a
        page costs a fixed number of queries rather than one per flag.
        """

        failed = set(
            session.scalars(
                select(Finding.step).where(
                    Finding.tenant_id == tenant_id,
                    Finding.invoice_id == invoice.id,
                    Finding.passed.is_(False),
                )
            ).all()
        )
        flags = [flag for flag, step in FLAG_STEPS.items() if step in failed]

        if invoice.po_number is None:
            flags.append("missing_po")
        if invoice.tax_amount is None:
            flags.append("missing_tax")
        if invoice.vendor_tax_id is None:
            flags.append("missing_tax_id")
        if invoice.due_date is None:
            flags.append("missing_due_date")
        if (
            invoice.min_confidence is not None
            and float(invoice.min_confidence) < 1
        ):
            flags.append("low_confidence")

        summary = (
            ((invoice.fields or {}).get("extraction") or {}).get("invoice")
            or {}
        ).get("_evidence") or {}
        if (summary.get("_summary") or {}).get("has_text_layer") is False:
            flags.append("no_text_layer")

        threshold = filters.get("high_value_threshold") or 0
        if threshold and invoice.total_inc_tax is not None:
            if float(invoice.total_inc_tax) >= float(threshold):
                flags.append("high_value")

        if row.get("corrected_fields"):
            flags.append("edited")

        if invoice.invoice_number and self._has_duplicate(
            session, tenant_id, invoice
        ):
            flags.append("duplicate_number")

        return sorted(flags)

    @staticmethod
    def _has_duplicate(session, tenant_id: str, invoice) -> bool:

        return bool(
            session.scalar(
                select(Invoice.id)
                .where(
                    Invoice.tenant_id == tenant_id,
                    Invoice.id != invoice.id,
                    Invoice.invoice_number == invoice.invoice_number,
                    Invoice.vendor_tax_id == invoice.vendor_tax_id,
                )
                .limit(1)
            )
        )

    def ids(self, tenant_id: str, filters: dict) -> list[str]:
        """Every case id the filters match, for an export."""
        with tenant_session(tenant_id, self.url) as session:
            return [
                row.case_id
                for row in session.scalars(
                    self._base(tenant_id, filters).order_by(Invoice.id)
                ).all()
            ]

    def rows_for(
        self, tenant_id: str, filters: dict, limit: int, offset: int
    ) -> list[dict]:
        """A slice for the export writer to stream."""
        page_filters = dict(filters, offset=offset)
        rows, _ = self.page(
            tenant_id, page_filters, "approved_at", "desc", limit
        )
        return rows

    def summary(self, tenant_id: str, filters: dict) -> dict:
        """The tiles above the sheet, from the same filters as the grid."""

        statement = self._base(tenant_id, filters).subquery()
        today = datetime.now(timezone.utc).date().isoformat()
        month = today[:7]
        decided_at = statement.c.fields["workflow"]["decided_at"].as_string()

        with tenant_session(tenant_id, self.url) as session:
            total = session.scalar(select(func.count()).select_from(statement))
            by_currency = [
                {
                    "currency": currency or "—",
                    "count": count,
                    "total": float(amount or 0),
                    "tax": float(tax or 0),
                }
                for currency, count, amount, tax in session.execute(
                    select(
                        statement.c.currency,
                        func.count(),
                        func.sum(statement.c.total_inc_tax),
                        func.sum(statement.c.tax_amount),
                    ).group_by(statement.c.currency)
                )
            ]
            vendors = session.scalar(
                select(func.count(func.distinct(statement.c.vendor_name)))
            )
            approved_today = session.scalar(
                select(func.count())
                .select_from(statement)
                .where(decided_at.like(f"{today}%"))
            )
            approved_month = session.scalar(
                select(func.count())
                .select_from(statement)
                .where(decided_at.like(f"{month}%"))
            )

        return {
            "total": total or 0,
            "by_currency": sorted(by_currency, key=lambda c: c["currency"]),
            "vendors": vendors or 0,
            "approved_today": approved_today or 0,
            "approved_this_month": approved_month or 0,
        }

    def facets(self, tenant_id: str, filters: dict) -> dict:
        """Distinct vendors and currencies, for the filter dropdowns."""

        statement = self._base(
            tenant_id, dict(filters, vendor=None, currency=None)
        )
        subquery = statement.subquery()
        with tenant_session(tenant_id, self.url) as session:
            vendors = session.scalars(
                select(func.distinct(subquery.c.vendor_name))
                .where(subquery.c.vendor_name.is_not(None))
                .order_by(subquery.c.vendor_name)
                .limit(500)
            ).all()
            currencies = session.scalars(
                select(func.distinct(subquery.c.currency))
                .where(subquery.c.currency.is_not(None))
                .order_by(subquery.c.currency)
                .limit(50)
            ).all()
        return {"vendors": list(vendors), "currencies": list(currencies)}


class PgSheetViews:
    """Saved filter/column sets for the sheet, per user.

    Private by default: two reviewers want different lenses on the same queue,
    and a view everyone sees is one person's preference imposed on the tenant.
    `shared` is the deliberate opt-in.
    """

    def __init__(self, url: str | None = None):
        self.url = url

    @staticmethod
    def _to_dict(view: SheetView) -> dict:
        return {
            "id": view.id,
            "name": view.name,
            "owner": view.owner,
            "shared": bool(view.shared),
            "definition": dict(view.definition or {}),
            "created_at": view.created_at.isoformat()
            if view.created_at
            else None,
            "updated_at": view.updated_at.isoformat()
            if view.updated_at
            else None,
        }

    def list(self, tenant_id: str, owner: str) -> list[dict]:
        with tenant_session(tenant_id, self.url) as session:
            rows = session.scalars(
                select(SheetView)
                .where(
                    SheetView.tenant_id == tenant_id,
                    or_(SheetView.owner == owner, SheetView.shared.is_(True)),
                )
                .order_by(SheetView.name)
            ).all()
            return [self._to_dict(row) for row in rows]

    def get(self, tenant_id: str, view_id: str) -> dict | None:
        with tenant_session(tenant_id, self.url) as session:
            view = session.scalar(
                select(SheetView).where(
                    SheetView.tenant_id == tenant_id, SheetView.id == view_id
                )
            )
            return self._to_dict(view) if view else None

    def create(
        self,
        tenant_id: str,
        *,
        name: str,
        owner: str,
        shared: bool,
        definition: dict,
    ) -> dict:
        with tenant_session(tenant_id, self.url) as session:
            view = SheetView(
                tenant_id=tenant_id,
                name=name,
                owner=owner,
                shared=shared,
                definition=definition,
            )
            session.add(view)
            session.flush()
            return self._to_dict(view)

    def update(self, tenant_id: str, view_id: str, **changes) -> dict:
        with tenant_session(tenant_id, self.url) as session:
            view = session.scalar(
                select(SheetView).where(
                    SheetView.tenant_id == tenant_id, SheetView.id == view_id
                )
            )
            for field, value in changes.items():
                setattr(view, field, value)
            session.flush()
            return self._to_dict(view)

    def delete(self, tenant_id: str, view_id: str) -> None:
        with tenant_session(tenant_id, self.url) as session:
            view = session.scalar(
                select(SheetView).where(
                    SheetView.tenant_id == tenant_id, SheetView.id == view_id
                )
            )
            if view is not None:
                session.delete(view)
