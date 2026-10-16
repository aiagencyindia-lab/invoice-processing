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
import math
import uuid
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import and_, delete, exists, func, or_, select, text, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from .. import approvals, gl_coding, po_matching
from ..ports import (
    ConcurrentWrite,
    LockHeld,
    RuleBaseConflict,
    RunContext,
    StatusChanged,
    UploadOwned,
)
from ..sheet_columns import (
    ATTENTION_FLAGS,
    BY_KEY,
    FLAG_STEPS,
    WORKFLOW_PATHS,
    is_sortable,
)
from .models import (
    ApprovalRule,
    ApprovalStep,
    ApproverGroup,
    AuditEvent,
    Correction,
    Delegation,
    Document,
    DocumentClaim,
    ExportJob,
    Finding,
    GlRule,
    GoodsReceipt,
    GrnLine,
    Invoice,
    InvoiceLine,
    PipelineRun,
    PoLine,
    PurchaseOrder,
    RecordLock,
    Rule,
    SheetView,
    SsoDomain,
    SupportingDocument,
    Tenant,
    User,
    Vendor,
    VendorBankChange,
)
from .session import tenant_session


def _iso(value: datetime | None) -> str | None:
    """A stored timestamp as ISO text that always carries its offset.

    Every timestamp is written in UTC, but SQLite hands it back naive, and a
    browser reads offset-less ISO as local time, so the UI shifted it by the
    viewer's offset. Postgres already returns it aware; this is a no-op there.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


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

    def update_artifact(self, ctx: RunContext, key: str, mutate) -> dict:
        """Read, mutate, and write one artifact in one locked transaction.

        Postgres locks the invoice row (FOR UPDATE), so a second writer waits
        and then reads this one's result. SQLite ignores FOR UPDATE; BEGIN
        IMMEDIATE takes its database write lock before the read instead.
        """
        with tenant_session(ctx.tenant_id, self.url) as session:
            if session.get_bind().dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            invoice = session.scalar(
                select(Invoice)
                .where(
                    Invoice.tenant_id == ctx.tenant_id,
                    Invoice.case_id == ctx.case_id,
                )
                .with_for_update()
            )
            if invoice is None:
                # ponytail: no row to lock yet; every caller updates a case
                # the worker already stored, so a racing create is not a path.
                invoice = Invoice(
                    tenant_id=ctx.tenant_id, case_id=ctx.case_id, fields={}
                )
                session.add(invoice)
            value = mutate((invoice.fields or {}).get(key))
            invoice.fields = {**(invoice.fields or {}), key: value}
            return value

    def update_artifacts(self, ctx: RunContext, mutate) -> dict | None:
        """`update_artifact` for several keys at once, deciding on the whole
        case: `mutate(artifacts)` returns the keys to write, or None to write
        nothing. One locked transaction, so a check inside it holds."""
        import copy  # noqa: PLC0415

        with tenant_session(ctx.tenant_id, self.url) as session:
            if session.get_bind().dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            invoice = session.scalar(
                select(Invoice)
                .where(
                    Invoice.tenant_id == ctx.tenant_id,
                    Invoice.case_id == ctx.case_id,
                )
                .with_for_update()
            )
            if invoice is None:
                return None  # no case, nothing to decide on or write
            # A copy: nested edits in place would be invisible to the JSON
            # column's change detection, and the write would not land.
            updates = mutate(copy.deepcopy(dict(invoice.fields or {})))
            if updates:
                invoice.fields = {**(invoice.fields or {}), **updates}
            return updates

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

            # The human decision time wins. A touchless invoice is decided
            # the moment it is projected, and keeps that time on retries so a
            # re-run does not move it. Nothing here ever writes None: the
            # worker re-projects from files that never hold the workflow
            # artifact, and clearing the column would erase a real decision.
            decided = case_projection.decided_at(artifacts)
            if decided is not None:
                invoice.decided_at = decided
            elif invoice.decided_at is None and case_projection.is_touchless(
                artifacts
            ):
                invoice.decided_at = datetime.now(timezone.utc)

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

            # One run row per invoice. Every approval, correction, GL coding
            # and rematch re-projects; appending on each made the same cost
            # count several times over. Replace only when there is a run to
            # replace it with: a projection without an audit_log must not
            # erase the cost already recorded.
            run = case_projection.run(artifacts)
            if run is not None:
                session.query(PipelineRun).filter(
                    PipelineRun.tenant_id == ctx.tenant_id,
                    PipelineRun.invoice_id == invoice.id,
                ).delete(synchronize_session=False)
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

    def held_by_receipt(self, tenant_id: str, po_number: str) -> list[str]:
        """Undecided cases quoting this PO that step 2.3 held."""
        from ..rematch import is_held_by_receipt  # noqa: PLC0415

        # ponytail: PO numbers normalise in Python, so every REVIEW row of the
        # tenant is read; a normalised po_number column if that grows large.
        with tenant_session(tenant_id, self.url) as session:
            rows = session.scalars(
                select(Invoice)
                .where(
                    Invoice.tenant_id == tenant_id,
                    Invoice.routing == "REVIEW",
                    Invoice.case_id.is_not(None),
                )
                .order_by(Invoice.case_id)
            ).all()
            return [
                row.case_id
                for row in rows
                if is_held_by_receipt(dict(row.fields or {}), po_number)
            ]


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

    # What the engine is allowed to act on. `Rule.status` defaults to
    # "proposed", so a load with no status filter would hand the evaluator
    # every draft anybody had ever started -- a rule nobody approved, changing
    # decisions on real invoices. That is a one-word bug with a payment
    # attached, so the filter lives here rather than in each caller.
    LIVE_STATUS = "approved"

    def load(self, tenant_id: str) -> tuple[dict, int]:
        """The rules this tenant has approved and left enabled."""
        with tenant_session(tenant_id, self.url) as session:
            rows = session.scalars(
                select(Rule)
                .where(
                    Rule.tenant_id == tenant_id,
                    Rule.status == self.LIVE_STATUS,
                    Rule.enabled.is_(True),
                )
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
            # Every rule, so an id already taken by a draft is updated rather
            # than inserted twice -- but the version is computed over the same
            # rules `load` returns, or the two could never agree and every save
            # would look like a conflict.
            live = [
                r
                for r in existing
                if r.status == self.LIVE_STATUS and r.enabled
            ]
            current = self._version(
                sorted(
                    (self._to_dict(r) for r in live),
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
                # Saving the rule base is how a rule goes live. A proposal
                # becomes one through the approval path instead, which is the
                # only reason `status` exists.
                rule.status = self.LIVE_STATUS

        return self._version(incoming)

    # --- proposals -------------------------------------------------------
    #
    # A proposal is a rule that is not live yet, so it lives in the same table
    # under a status the evaluator's `load` filter excludes. What it must NOT
    # go through is `load`/`save`: `load` returns only approved rules, so a
    # read-modify-write round-trip would carry none of the drafts, and `save`
    # would then be writing a document with every proposal missing. Approval
    # is a status flip on one row instead.

    def propose(
        self,
        tenant_id: str,
        *,
        case_id: str,
        author: str,
        note: str = "",
        reason_category: str = "",
    ) -> dict:
        """Record that a case is worth a rule. No rule has been written yet."""
        with tenant_session(tenant_id, self.url) as session:
            rule = Rule(
                tenant_id=tenant_id,
                rule_id=self.next_rule_id(tenant_id),
                name=f"Proposed from {case_id}",
                status="pending_draft",
                enabled=True,
                conditions=[],
                actions=[],
                author_id=author,
                rule_metadata={
                    "source_case_id": case_id,
                    "reason_category": reason_category,
                    "note": note,
                },
            )
            session.add(rule)
            session.flush()
            return self._proposal(rule)

    # Action types that call a model. A rule carrying one runs Gemini on every
    # invoice it matches, for as long as it is enabled -- minutes of latency,
    # a cost per invoice, and no guarantee of the same answer twice. That is a
    # different thing to approve than a rule that sets a field, so the
    # approver is told which one they are looking at.
    LLM_ACTIONS = ("llm_continue_processing", "llm_patch_fields")

    @classmethod
    def _proposal(cls, rule: Rule) -> dict:
        metadata = dict(rule.rule_metadata or {})
        llm_actions = sorted(
            {
                action.get("type")
                for action in (rule.actions or [])
                if action.get("type") in cls.LLM_ACTIONS
            }
        )
        return {
            "llm_actions": llm_actions,
            "rule_id": rule.rule_id,
            "name": rule.name,
            "description": rule.description,
            "status": rule.status,
            "enabled": rule.enabled,
            "conditions": list(rule.conditions or []),
            "actions": list(rule.actions or []),
            "scope": rule.scope,
            "priority": rule.priority,
            "author": rule.author_id,
            "approver": rule.approver_id,
            "source_case_id": metadata.get("source_case_id"),
            "reason_category": metadata.get("reason_category"),
            "note": metadata.get("note"),
            "impact": metadata.get("impact"),
            # The orchestrator reports success even when a rule still matches
            # cases it was never meant to. Carried separately so a reader
            # cannot mistake "it ran" for "it is safe".
            "has_collateral": bool(metadata.get("has_collateral")),
            "collateral_warning": metadata.get("collateral_warning"),
            "error": metadata.get("error"),
            "created_at": _iso(rule.created_at),
        }

    def proposals(self, tenant_id: str, status: str | None = None) -> list:
        """Rules that are not in force: drafts, failures, rejections.

        With no status this returns everything EXCEPT approved rules, because
        a proposal is a thing awaiting a decision and an approved rule has had
        one -- `load` is where the live rule base comes from.

        That is the right split for the page, and it is a trap for anything
        cleaning up: a script that disabled "every proposal this author made"
        silently skipped the rule it had just approved, and left it in force
        on a production tenant. If you want every row, ask for both.
        """
        with tenant_session(tenant_id, self.url) as session:
            statement = select(Rule).where(Rule.tenant_id == tenant_id)
            if status:
                statement = statement.where(Rule.status == status)
            else:
                statement = statement.where(
                    Rule.status != self.LIVE_STATUS,
                )
            rows = session.scalars(
                statement.order_by(Rule.created_at.desc())
            ).all()
            return [self._proposal(r) for r in rows]

    def proposal(self, tenant_id: str, rule_id: str) -> dict | None:
        with tenant_session(tenant_id, self.url) as session:
            rule = self._rule_row(session, tenant_id, rule_id)
            return self._proposal(rule) if rule else None

    @staticmethod
    def _rule_row(session, tenant_id: str, rule_id: str):
        return session.scalar(
            select(Rule).where(
                Rule.tenant_id == tenant_id, Rule.rule_id == rule_id
            )
        )

    def update_proposal(self, tenant_id: str, rule_id: str, **changes) -> dict:
        """Write a drafted rule, or its failure, onto the proposal."""
        metadata_changes = changes.pop("metadata", None)
        with tenant_session(tenant_id, self.url) as session:
            rule = self._rule_row(session, tenant_id, rule_id)
            if rule is None:
                return {}
            for field, value in changes.items():
                setattr(rule, field, value)
            if metadata_changes:
                rule.rule_metadata = {
                    **(rule.rule_metadata or {}),
                    **metadata_changes,
                }
            session.flush()
            return self._proposal(rule)

    def set_status(
        self, tenant_id: str, rule_id: str, status: str, *, approver: str
    ) -> dict:
        """Approve or reject one proposal.

        Deliberately not a `save`: approving is one row changing status, and
        routing it through the document would mean writing back a rule base
        that, by construction, contains no proposals.
        """
        with tenant_session(tenant_id, self.url) as session:
            rule = self._rule_row(session, tenant_id, rule_id)
            if rule is None:
                return {}
            rule.status = status
            rule.approver_id = approver
            session.flush()
            return self._proposal(rule)

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
                "source": document.source,
                "sender": document.sender,
                "reply_to": document.reply_to,
            }

    def get(self, tenant_id: str, document_id: str) -> dict | None:
        with tenant_session(tenant_id, self.url) as session:
            d = session.scalar(
                select(Document).where(
                    Document.tenant_id == tenant_id,
                    Document.id == document_id,
                )
            )
            if d is None:
                return None
            return {
                "id": d.id,
                "source": d.source,
                "source_ref": d.source_ref,
                "filename": d.filename,
                "sha256": d.sha256,
                "sender": d.sender,
                "reply_to": d.reply_to,
                "kind": d.kind,
                "storage_key": d.storage_key,
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
                    "kind": d.kind,
                    "created_at": _iso(d.created_at),
                }
                for d in rows
            ]

    def set_kind(self, tenant_id: str, document_id: str, kind) -> None:
        """invoice / purchase_order / goods_receipt; None = not classified yet."""
        with tenant_session(tenant_id, self.url) as session:
            d = session.scalar(
                select(Document).where(
                    Document.tenant_id == tenant_id,
                    Document.id == document_id,
                )
            )
            if d is not None:
                d.kind = kind

    def non_invoice_ids(self, tenant_id: str) -> set[str]:
        with tenant_session(tenant_id, self.url) as session:
            return set(
                session.scalars(
                    select(Document.id).where(
                        Document.tenant_id == tenant_id,
                        Document.kind.is_not(None),
                        Document.kind != "invoice",
                    )
                )
            )

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
    "vendor": "vendor",
    "approval": "approval",
    "delegation": "delegation",
    "gl_rule": "gl_rule",
    "goods_receipt": "goods_receipt",
    "supporting_document": "supporting_document",
    "supporting": "supporting_document",
    "sso_domain": "sso_domain",
    "access": "user",
    "sign_in": "user",
    "audit": "audit_trail",
    # v2 trust layer: trust_history_imported, trust_finding_*, trust_dead_lettered.
    "trust": "trust",
    "entity": "entity_link",
}


def entity_type_for(event_type: str, case_id: str = "") -> str | None:
    # Two-word subjects first: gl_rule_created is about a rule, while
    # gl_line_coded falls through to the invoice it names.
    head = event_type.rsplit("_", 1)[0]
    prefix = event_type.split("_", 1)[0]
    return (
        _ENTITY_TYPES.get(head)
        or _ENTITY_TYPES.get(prefix)
        or ("invoice" if case_id else None)
    )


def audit_event_row(
    ctx: RunContext,
    event_type: str,
    payload: dict,
    before: dict | None = None,
) -> AuditEvent:
    """The audit row for an event, for a caller that adds it to its own session."""
    return AuditEvent(
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
            session.add(audit_event_row(ctx, event_type, payload, before))

    def query(
        self,
        tenant_id: str,
        limit: int = 100,
        since: datetime | None = None,
        until: datetime | None = None,
        before: tuple[datetime, str] | None = None,
    ) -> list[AuditEvent]:
        stmt = select(AuditEvent).where(AuditEvent.tenant_id == tenant_id)
        if since is not None:
            stmt = stmt.where(AuditEvent.occurred_at >= since)
        if until is not None:
            stmt = stmt.where(AuditEvent.occurred_at <= until)
        if before is not None:
            # Keyset, not offset: an auditor paging a year of events must
            # never see a row twice or skip one because a new row landed.
            t, i = before
            stmt = stmt.where(
                or_(
                    AuditEvent.occurred_at < t,
                    and_(AuditEvent.occurred_at == t, AuditEvent.id < i),
                )
            )
        stmt = stmt.order_by(
            AuditEvent.occurred_at.desc(), AuditEvent.id.desc()
        ).limit(limit)
        with tenant_session(tenant_id, self.url) as session:
            return list(session.scalars(stmt).all())


# How long a lock lives without being renewed. Long enough for a reviewer to
# read an invoice and correct it, short enough that a closed tab is not a
# support ticket.
LOCK_TTL = timedelta(minutes=15)


class PgRecordLocks:
    """One editor per invoice, where every container can see who it is.

    The exclusion is the unique constraint, as with `PgClaimStore`: an insert
    either wins or does nothing, atomically, for however many people press
    Edit at the same moment. What differs is expiry -- a claim is finished by
    the worker that took it, and a person may simply walk away, so an
    unrenewed lock lapses and the next caller takes it over.
    """

    def __init__(self, url: str | None = None):
        self.url = url

    @staticmethod
    def _to_dict(lock: RecordLock) -> dict:
        expires = lock.expires_at
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return {
            "case_id": lock.case_id,
            "holder": lock.holder,
            "expires_at": _iso(expires),
        }

    @staticmethod
    def _row(session, tenant_id: str, case_id: str):
        return session.scalar(
            select(RecordLock).where(
                RecordLock.tenant_id == tenant_id,
                RecordLock.case_id == case_id,
            )
        )

    @staticmethod
    def _live(lock) -> bool:
        if lock is None:
            return False
        expires = lock.expires_at
        if expires is None:
            return False
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires > datetime.now(timezone.utc)

    def get(self, tenant_id: str, case_id: str) -> dict | None:
        """The live lock on this case, or None. A lapsed lock reads as absent."""
        with tenant_session(tenant_id, self.url) as session:
            lock = self._row(session, tenant_id, case_id)
            return self._to_dict(lock) if self._live(lock) else None

    def acquire(
        self, tenant_id: str, case_id: str, holder: str, ttl=LOCK_TTL
    ) -> tuple[bool, dict]:
        """Take or renew the lock. Returns (is_yours, lock).

        Renewing is the same call as taking, so the editor keeps its lock by
        doing what it would do anyway rather than through a second endpoint
        nobody remembers to call.
        """
        expires_at = datetime.now(timezone.utc) + ttl
        with tenant_session(tenant_id, self.url) as session:
            values = {
                "id": str(uuid.uuid4()),
                "tenant_id": tenant_id,
                "case_id": case_id,
                "holder": holder,
                "expires_at": expires_at,
            }
            insert = (
                postgresql.insert
                if session.get_bind().dialect.name == "postgresql"
                else sqlite.insert
            )
            result = session.execute(
                insert(RecordLock)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["tenant_id", "case_id"])
            )
            if result.rowcount:
                return True, {
                    "case_id": case_id,
                    "holder": holder,
                    "expires_at": expires_at.isoformat(),
                }

            # Somebody has a row. Ours to renew, theirs to respect, or lapsed
            # and free to take over.
            lock = self._row(session, tenant_id, case_id)
            if self._live(lock) and lock.holder != holder:
                return False, self._to_dict(lock)

            lock.holder = holder
            lock.expires_at = expires_at
            session.flush()
            return True, self._to_dict(lock)

    def release(
        self, tenant_id: str, case_id: str, holder: str, force: bool = False
    ) -> dict | None:
        """Give up the lock. Returns the lock that was broken, if any.

        `force` is for an admin breaking someone else's: without it a stuck
        lock outlives the person who left it, and with it unqualified the lock
        would mean nothing.
        """
        with tenant_session(tenant_id, self.url) as session:
            lock = self._row(session, tenant_id, case_id)
            if lock is None:
                return None
            if not force and self._live(lock) and lock.holder != holder:
                raise LockHeld(self._to_dict(lock))
            broken = self._to_dict(lock)
            session.delete(lock)
            return broken


class PendingChangeExists(Exception):
    """A vendor already has a bank change awaiting decision."""


class SelfApproval(Exception):
    """The proposer tried to approve their own bank change."""


class ChangeNotPending(Exception):
    """The change was already decided."""


BANK_FIELDS = ("account_name", "account_number", "code", "iban")


class PgVendorRepository:
    """Suppliers and the dual-control queue for their payee details.

    Bank columns on `vendors` are written only by `decide(approve)`. The
    bulk `upsert_many` is the customer's supplier export and deliberately
    cannot carry bank details: an export is one person's file.
    """

    def __init__(self, url: str | None = None):
        self.url = url

    # -- shapes ----------------------------------------------------------
    @staticmethod
    def _iso(value):
        return _iso(value)

    @staticmethod
    def _pending_id(session, tenant_id: str, vendor_id: str) -> str | None:
        return session.scalar(
            select(VendorBankChange.id).where(
                VendorBankChange.tenant_id == tenant_id,
                VendorBankChange.vendor_id == vendor_id,
                VendorBankChange.status == VendorBankChange.PENDING,
            )
        )

    # ponytail: one pending-change query per vendor (here and in
    # load_records); fine for a supplier list in the hundreds, group the
    # queries by vendor_id when lists reach the thousands.
    def _vendor_dict(self, session, vendor: Vendor) -> dict:
        pending = self._pending_id(session, vendor.tenant_id, vendor.id)
        return {
            "id": vendor.id,
            "name": vendor.name,
            "tax_id": vendor.tax_id,
            "kind": vendor.kind or "",
            "email_domain": vendor.email_domain,
            "bank_account_name": vendor.bank_account_name,
            "bank_account_number": vendor.bank_account_number,
            "bank_code": vendor.bank_code,
            "bank_iban": vendor.bank_iban,
            "bank_verified_at": self._iso(vendor.bank_verified_at),
            "bank_verified_by": vendor.bank_verified_by,
            "pending_change_id": pending,
        }

    def _change_dict(
        self, change: VendorBankChange, vendor_name: str = ""
    ) -> dict:
        out = {
            "id": change.id,
            "vendor_id": change.vendor_id,
            "vendor_name": vendor_name,
            "reason": change.reason,
            "source_case_id": change.source_case_id,
            "status": change.status,
            "proposed_by": change.proposed_by,
            "decided_by": change.decided_by,
            "decided_at": self._iso(change.decided_at),
            "decision_note": change.decision_note,
            "created_at": self._iso(change.created_at),
        }
        for f in BANK_FIELDS:
            out[f"proposed_{f}"] = getattr(change, f"proposed_{f}")
            out[f"previous_{f}"] = getattr(change, f"previous_{f}")
        return out

    def _vendor(self, session, tenant_id: str, vendor_id: str) -> Vendor:
        vendor = session.scalar(
            select(Vendor).where(
                Vendor.tenant_id == tenant_id, Vendor.id == vendor_id
            )
        )
        if vendor is None:
            raise KeyError(vendor_id)
        return vendor

    # -- vendors ---------------------------------------------------------
    def upsert_many(self, tenant_id: str, records: list[dict]) -> int:
        count = 0
        with tenant_session(tenant_id, self.url) as session:
            existing = {
                v.normalized_name: v
                for v in session.scalars(
                    select(Vendor).where(Vendor.tenant_id == tenant_id)
                )
            }
            for record in records:
                name = str(record.get("name") or "").strip()
                key = po_matching.normalize_vendor(name)
                if not key:
                    continue
                vendor = existing.get(key)
                if vendor is None:
                    vendor = Vendor(
                        tenant_id=tenant_id,
                        name=name,
                        normalized_name=key,
                        tax_id="",
                    )
                    session.add(vendor)
                    existing[key] = vendor
                vendor.name = name
                vendor.tax_id = str(record.get("tax_id") or "").strip()
                vendor.kind = str(record.get("kind") or "").lower()
                count += 1
            session.flush()
        return count

    def list(self, tenant_id: str) -> list[dict]:
        with tenant_session(tenant_id, self.url) as session:
            rows = session.scalars(
                select(Vendor)
                .where(Vendor.tenant_id == tenant_id)
                .order_by(Vendor.name)
            ).all()
            return [self._vendor_dict(session, v) for v in rows]

    def get(self, tenant_id: str, vendor_id: str) -> dict | None:
        with tenant_session(tenant_id, self.url) as session:
            try:
                return self._vendor_dict(
                    session, self._vendor(session, tenant_id, vendor_id)
                )
            except KeyError:
                return None

    def find_by_name(self, tenant_id: str, name: str) -> dict | None:
        key = po_matching.normalize_vendor(name)
        with tenant_session(tenant_id, self.url) as session:
            vendor = session.scalar(
                select(Vendor).where(
                    Vendor.tenant_id == tenant_id,
                    Vendor.normalized_name == key,
                )
            )
            return self._vendor_dict(session, vendor) if vendor else None

    def patch(
        self, tenant_id: str, vendor_id: str, *, email_domain: str | None
    ) -> dict:
        with tenant_session(tenant_id, self.url) as session:
            vendor = self._vendor(session, tenant_id, vendor_id)
            vendor.email_domain = (email_domain or "").strip().lower() or None
            session.flush()
            return self._vendor_dict(session, vendor)

    def load_records(self, tenant_id: str) -> list[dict]:
        """What the pipeline's VendorMaster is built from."""
        with tenant_session(tenant_id, self.url) as session:
            rows = session.scalars(
                select(Vendor).where(Vendor.tenant_id == tenant_id)
            ).all()
            out = []
            for v in rows:
                changes = session.scalars(
                    select(VendorBankChange)
                    .where(
                        VendorBankChange.tenant_id == tenant_id,
                        VendorBankChange.vendor_id == v.id,
                    )
                    .order_by(VendorBankChange.created_at.desc())
                ).all()
                # A first verification (nothing recorded before it) is not a
                # change of payee, so it must not trip the "recent change"
                # signal on a vendor nobody has ever had different details for.
                approved = [
                    c
                    for c in changes
                    if c.status == VendorBankChange.APPROVED
                    and any(getattr(c, f"previous_{f}") for f in BANK_FIELDS)
                ]
                out.append(
                    {
                        "vendor_id": v.id,
                        "name": v.name,
                        "tax_id": v.tax_id,
                        "kind": v.kind or "",
                        "email_domain": v.email_domain,
                        "bank_account_name": v.bank_account_name,
                        "bank_account_number": v.bank_account_number,
                        "bank_code": v.bank_code,
                        "bank_iban": v.bank_iban,
                        "bank_verified_at": self._iso(v.bank_verified_at),
                        "pending_change": any(
                            c.status == VendorBankChange.PENDING
                            for c in changes
                        ),
                        "last_bank_change_at": self._iso(approved[0].decided_at)
                        if approved
                        else None,
                    }
                )
            return out

    # -- bank changes ----------------------------------------------------
    def propose(
        self,
        tenant_id: str,
        vendor_id: str,
        *,
        proposed: dict,
        reason: str,
        proposed_by: str,
        source_case_id: str | None = None,
    ) -> dict:
        with tenant_session(tenant_id, self.url) as session:
            vendor = self._vendor(session, tenant_id, vendor_id)
            # Read now: after a failed flush the session cannot lazy-load.
            vendor_name = vendor.name
            if self._pending_id(session, tenant_id, vendor.id):
                raise PendingChangeExists(vendor_name)
            change = VendorBankChange(
                tenant_id=tenant_id,
                vendor_id=vendor.id,
                reason=reason,
                proposed_by=proposed_by,
                source_case_id=source_case_id,
                status=VendorBankChange.PENDING,
            )
            for f in BANK_FIELDS:
                setattr(change, f"proposed_{f}", proposed.get(f"bank_{f}"))
                setattr(change, f"previous_{f}", getattr(vendor, f"bank_{f}"))
            session.add(change)
            try:
                session.flush()
            except IntegrityError as exc:
                # Lost the race the pre-check cannot close: the partial unique
                # index on pending changes refused the second one.
                raise PendingChangeExists(vendor_name) from exc
            session.refresh(change)
            return self._change_dict(change, vendor.name)

    def list_changes(
        self, tenant_id: str, status: str | None = None
    ) -> list[dict]:
        with tenant_session(tenant_id, self.url) as session:
            query = (
                select(VendorBankChange, Vendor.name)
                .join(Vendor, Vendor.id == VendorBankChange.vendor_id)
                .where(VendorBankChange.tenant_id == tenant_id)
                .order_by(VendorBankChange.created_at.desc())
            )
            if status:
                query = query.where(VendorBankChange.status == status)
            return [self._change_dict(c, n) for c, n in session.execute(query)]

    def get_change(self, tenant_id: str, change_id: str) -> dict | None:
        with tenant_session(tenant_id, self.url) as session:
            row = session.execute(
                select(VendorBankChange, Vendor.name)
                .join(Vendor, Vendor.id == VendorBankChange.vendor_id)
                .where(
                    VendorBankChange.tenant_id == tenant_id,
                    VendorBankChange.id == change_id,
                )
            ).first()
            return self._change_dict(*row) if row else None

    def decide(
        self,
        tenant_id: str,
        change_id: str,
        *,
        action: str,
        decided_by: str,
        note: str | None,
    ) -> dict:
        if action not in ("approve", "reject"):
            raise ValueError(action)
        with tenant_session(tenant_id, self.url) as session:
            change = session.scalar(
                select(VendorBankChange)
                .where(
                    VendorBankChange.tenant_id == tenant_id,
                    VendorBankChange.id == change_id,
                )
                .with_for_update()
            )
            if change is None:
                raise KeyError(change_id)
            if change.status != VendorBankChange.PENDING:
                raise ChangeNotPending(change.status)
            if (
                change.proposed_by.strip().casefold()
                == decided_by.strip().casefold()
            ):
                raise SelfApproval(decided_by)
            vendor = self._vendor(session, tenant_id, change.vendor_id)
            now = datetime.now(timezone.utc)
            if action == "approve":
                for f in BANK_FIELDS:
                    setattr(
                        vendor, f"bank_{f}", getattr(change, f"proposed_{f}")
                    )
                vendor.bank_verified_at = now
                vendor.bank_verified_by = decided_by
                change.status = VendorBankChange.APPROVED
            else:
                change.status = VendorBankChange.REJECTED
            change.decided_by = decided_by
            change.decided_at = now
            change.decision_note = note
            session.flush()
            return self._change_dict(change, vendor.name)


class StepNotPending(Exception):
    """The step is decided already, or an earlier step is still waiting."""


class UnknownGroup(KeyError):
    pass


class UnknownRule(KeyError):
    pass


class InvalidDelegation(ValueError):
    pass


class PgApprovalRepository:
    """Approval chains, groups, rules and delegations for a tenant.

    The API and the worker are separate containers, so "what is waiting for
    me" and "what is overdue" have to be queries both can run.
    """

    def __init__(self, url: str | None = None):
        self.url = url

    # -- shapes ----------------------------------------------------------
    @staticmethod
    def _iso(value):
        return _iso(value)

    def _group_dict(self, g: ApproverGroup) -> dict:
        return {
            "id": g.id,
            "name": g.name,
            "members": list(g.members or []),
            "created_at": self._iso(g.created_at),
        }

    def _rule_dict(self, r: ApprovalRule) -> dict:
        return {
            "id": r.id,
            "name": r.name,
            "kind": r.kind,
            "match": dict(r.match or {}),
            "steps": list(r.steps or []),
            "enabled": bool(r.enabled),
            "created_at": self._iso(r.created_at),
        }

    def _delegation_dict(self, d: Delegation) -> dict:
        return {
            "id": d.id,
            "delegator_email": d.delegator_email,
            "delegate_email": d.delegate_email,
            "starts_on": d.starts_on.isoformat(),
            "ends_on": d.ends_on.isoformat(),
            "created_by": d.created_by,
            "created_at": self._iso(d.created_at),
        }

    def _step_dict(self, s: ApprovalStep) -> dict:
        return {
            "id": s.id,
            "case_id": s.case_id,
            "position": s.position,
            "source_rule_id": s.source_rule_id,
            "assignee_kind": s.assignee_kind,
            "assignee_value": s.assignee_value,
            "status": s.status,
            "due_at": self._iso(s.due_at),
            "decided_by": s.decided_by,
            "decided_at": self._iso(s.decided_at),
            "acting_for": s.acting_for,
            "override_reason": s.override_reason,
            "escalated_to": s.escalated_to,
            "escalated_at": self._iso(s.escalated_at),
            "note": s.note,
            "updated_at": self._iso(s.updated_at),
        }

    @staticmethod
    def _one(session, model, tenant_id: str, row_id: str, missing):
        row = session.scalar(
            select(model).where(
                model.tenant_id == tenant_id, model.id == row_id
            )
        )
        if row is None:
            raise missing(row_id)
        return row

    # -- groups ----------------------------------------------------------
    def list_groups(self, tenant_id: str) -> list[dict]:
        with tenant_session(tenant_id, self.url) as session:
            rows = session.scalars(
                select(ApproverGroup)
                .where(ApproverGroup.tenant_id == tenant_id)
                .order_by(ApproverGroup.name)
            )
            return [self._group_dict(g) for g in rows]

    def groups_map(self, tenant_id: str) -> dict[str, list[str]]:
        return {g["name"]: g["members"] for g in self.list_groups(tenant_id)}

    def create_group(
        self, tenant_id: str, name: str, members: list[str]
    ) -> dict:
        with tenant_session(tenant_id, self.url) as session:
            group = ApproverGroup(
                tenant_id=tenant_id, name=name, members=list(members)
            )
            session.add(group)
            session.flush()
            return self._group_dict(group)

    def update_group(
        self, tenant_id: str, group_id: str, *, name=None, members=None
    ) -> dict:
        with tenant_session(tenant_id, self.url) as session:
            group = self._one(
                session, ApproverGroup, tenant_id, group_id, UnknownGroup
            )
            if name is not None:
                group.name = name
            if members is not None:
                group.members = list(members)
            session.flush()
            return self._group_dict(group)

    def delete_group(self, tenant_id: str, group_id: str) -> None:
        with tenant_session(tenant_id, self.url) as session:
            session.delete(
                self._one(
                    session, ApproverGroup, tenant_id, group_id, UnknownGroup
                )
            )

    # -- rules -----------------------------------------------------------
    def list_rules(self, tenant_id: str) -> list[dict]:
        with tenant_session(tenant_id, self.url) as session:
            rows = session.scalars(
                select(ApprovalRule)
                .where(ApprovalRule.tenant_id == tenant_id)
                .order_by(ApprovalRule.created_at, ApprovalRule.id)
            )
            return [self._rule_dict(r) for r in rows]

    def create_rule(
        self, tenant_id: str, *, name, kind, match, steps, enabled=True
    ) -> dict:
        with tenant_session(tenant_id, self.url) as session:
            rule = ApprovalRule(
                tenant_id=tenant_id,
                name=name,
                kind=kind,
                match=match,
                steps=steps,
                enabled=enabled,
            )
            session.add(rule)
            session.flush()
            return self._rule_dict(rule)

    def update_rule(self, tenant_id: str, rule_id: str, **fields) -> dict:
        with tenant_session(tenant_id, self.url) as session:
            rule = self._one(
                session, ApprovalRule, tenant_id, rule_id, UnknownRule
            )
            for key in ("name", "kind", "match", "steps", "enabled"):
                if key in fields:
                    setattr(rule, key, fields[key])
            session.flush()
            return self._rule_dict(rule)

    def delete_rule(self, tenant_id: str, rule_id: str) -> None:
        with tenant_session(tenant_id, self.url) as session:
            session.delete(
                self._one(
                    session, ApprovalRule, tenant_id, rule_id, UnknownRule
                )
            )

    # -- delegations -----------------------------------------------------
    def list_delegations(self, tenant_id: str, *, email=None) -> list[dict]:
        with tenant_session(tenant_id, self.url) as session:
            query = select(Delegation).where(Delegation.tenant_id == tenant_id)
            if email:
                who = email.strip().casefold()
                query = query.where(
                    or_(
                        Delegation.delegator_email == who,
                        Delegation.delegate_email == who,
                    )
                )
            rows = session.scalars(query.order_by(Delegation.starts_on))
            return [self._delegation_dict(d) for d in rows]

    def create_delegation(
        self, tenant_id, *, delegator, delegate, starts_on, ends_on, created_by
    ) -> dict:
        # Emails are stored folded so matching is a plain equality.
        delegator, delegate = (
            delegator.strip().casefold(),
            delegate.strip().casefold(),
        )
        if delegator == delegate:
            raise InvalidDelegation("You cannot delegate to yourself.")
        if ends_on < starts_on:
            raise InvalidDelegation("The end date is before the start date.")
        with tenant_session(tenant_id, self.url) as session:
            row = Delegation(
                tenant_id=tenant_id,
                delegator_email=delegator,
                delegate_email=delegate,
                starts_on=starts_on,
                ends_on=ends_on,
                created_by=created_by,
            )
            session.add(row)
            session.flush()
            return self._delegation_dict(row)

    def delete_delegation(self, tenant_id: str, delegation_id: str) -> None:
        with tenant_session(tenant_id, self.url) as session:
            row = session.scalar(
                select(Delegation).where(
                    Delegation.tenant_id == tenant_id,
                    Delegation.id == delegation_id,
                )
            )
            if row is not None:
                session.delete(row)

    def active_delegators_of(
        self, tenant_id: str, email: str, today: date
    ) -> list[str]:
        with tenant_session(tenant_id, self.url) as session:
            return list(
                session.scalars(
                    select(Delegation.delegator_email).where(
                        Delegation.tenant_id == tenant_id,
                        Delegation.delegate_email == email.strip().casefold(),
                        Delegation.starts_on <= today,
                        Delegation.ends_on >= today,
                    )
                )
            )

    # -- steps -----------------------------------------------------------
    def _steps(
        self, session, tenant_id: str, case_id: str, *, lock: bool = False
    ) -> list[ApprovalStep]:
        query = (
            select(ApprovalStep)
            .where(
                ApprovalStep.tenant_id == tenant_id,
                ApprovalStep.case_id == case_id,
            )
            .order_by(ApprovalStep.position)
        )
        if lock:
            # Postgres re-reads a row after waiting on a concurrent decide, so
            # we see its new status. A no-op on SQLite (see `decide`).
            query = query.with_for_update()
        return list(session.scalars(query))

    def chain(self, tenant_id: str, case_id: str) -> list[dict]:
        with tenant_session(tenant_id, self.url) as session:
            return [
                self._step_dict(s)
                for s in self._steps(session, tenant_id, case_id)
            ]

    def current(self, tenant_id: str, case_id: str) -> dict | None:
        for step in self.chain(tenant_id, case_id):
            if step["status"] == ApprovalStep.PENDING:
                return step
        return None

    def _add_steps(self, session, tenant_id, case_id, specs, *, sla_days, now):
        rows = self._steps(session, tenant_id, case_id, lock=True)
        position = max((s.position for s in rows), default=0)
        has_pending = any(s.status == ApprovalStep.PENDING for s in rows)
        for spec in specs:
            position += 1
            session.add(
                ApprovalStep(
                    tenant_id=tenant_id,
                    case_id=case_id,
                    position=position,
                    source_rule_id=spec.source_rule_id,
                    assignee_kind=spec.assignee.kind,
                    assignee_value=spec.assignee.value,
                    status=ApprovalStep.PENDING,
                    # Only the step that is up next runs a clock.
                    due_at=None
                    if has_pending
                    else now + timedelta(days=sla_days),
                )
            )
            has_pending = True
        session.flush()

    def replace_pending(
        self, tenant_id, case_id, specs, *, sla_days, now
    ) -> list[dict]:
        """Re-route: drop what is still waiting, keep what people decided."""
        with tenant_session(tenant_id, self.url) as session:
            self._steps(session, tenant_id, case_id, lock=True)
            # One guarded statement, never per-row deletes of rows read
            # earlier: a decision that landed meanwhile must survive.
            session.execute(
                delete(ApprovalStep).where(
                    ApprovalStep.tenant_id == tenant_id,
                    ApprovalStep.case_id == case_id,
                    ApprovalStep.status == ApprovalStep.PENDING,
                )
            )
            self._add_steps(
                session, tenant_id, case_id, specs, sla_days=sla_days, now=now
            )
            return [
                self._step_dict(s)
                for s in self._steps(session, tenant_id, case_id)
            ]

    def append_step(self, tenant_id, case_id, spec, *, sla_days, now) -> dict:
        with tenant_session(tenant_id, self.url) as session:
            self._add_steps(
                session, tenant_id, case_id, [spec], sla_days=sla_days, now=now
            )
            return self._step_dict(self._steps(session, tenant_id, case_id)[-1])

    def decide(
        self,
        tenant_id: str,
        step_id: str,
        *,
        approve: bool,
        decided_by: str,
        acting_for=None,
        override_reason=None,
        note=None,
        sla_days: int,
        now: datetime,
    ) -> tuple[dict, bool]:
        # ponytail: FOR UPDATE is a no-op on SQLite, so the double-decision
        # guard (the status re-check after the lock wait) holds on Postgres
        # only; dev/test SQLite serialises writers with its file lock.
        with tenant_session(tenant_id, self.url) as session:
            step = session.scalar(
                select(ApprovalStep)
                .where(
                    ApprovalStep.tenant_id == tenant_id,
                    ApprovalStep.id == step_id,
                )
                .with_for_update()
            )
            if step is None or step.status != ApprovalStep.PENDING:
                raise StepNotPending(step_id)
            pending = [
                s
                for s in self._steps(session, tenant_id, step.case_id)
                if s.status == ApprovalStep.PENDING
            ]
            if pending[0].id != step.id:
                raise StepNotPending(step_id)
            step.status = (
                ApprovalStep.APPROVED if approve else ApprovalStep.REJECTED
            )
            step.decided_by = decided_by
            step.decided_at = now
            step.acting_for = acting_for
            step.override_reason = override_reason
            step.note = note
            later = pending[1:]
            if approve and later:
                later[0].due_at = now + timedelta(days=sla_days)
            if not approve:
                for s in later:
                    s.status = ApprovalStep.CANCELLED
            session.flush()
            return self._step_dict(step), not approve or not later

    def mine(
        self, tenant_id, *, email, role, groups_map, delegators_of
    ) -> list[dict]:
        # ponytail: loads every pending step for the tenant and groups in
        # Python; fine for a queue in the hundreds, push "lowest pending per
        # case" into SQL (window function) when queues reach the thousands.
        # Callers resolve delegations (for their own date) and pass the
        # delegators in.
        with tenant_session(tenant_id, self.url) as session:
            rows = session.scalars(
                select(ApprovalStep)
                .where(
                    ApprovalStep.tenant_id == tenant_id,
                    ApprovalStep.status == ApprovalStep.PENDING,
                )
                .order_by(ApprovalStep.case_id, ApprovalStep.position)
            )
            heads: dict[str, ApprovalStep] = {}
            for s in rows:
                heads.setdefault(s.case_id, s)
            out = []
            # Most urgent first: soonest due, steps with no clock last.
            ordered = sorted(
                heads.values(),
                key=lambda s: (
                    s.due_at is None,
                    s.due_at.replace(tzinfo=None) if s.due_at else None,
                    s.created_at.replace(tzinfo=None),
                ),
            )
            for s in ordered:
                d = self._step_dict(s)
                why = approvals.satisfaction(
                    email,
                    role,
                    d,
                    groups=groups_map,
                    delegators_of=delegators_of,
                )
                if why:
                    out.append({**d, "why": why})
            return out

    def overdue(self, tenant_id: str, now: datetime) -> list[dict]:
        with tenant_session(tenant_id, self.url) as session:
            rows = session.scalars(
                select(ApprovalStep)
                .where(
                    ApprovalStep.tenant_id == tenant_id,
                    ApprovalStep.status == ApprovalStep.PENDING,
                    ApprovalStep.due_at.is_not(None),
                    ApprovalStep.due_at < now,
                    ApprovalStep.escalated_at.is_(None),
                )
                .order_by(ApprovalStep.due_at)
            )
            return [self._step_dict(s) for s in rows]

    def escalate(self, tenant_id: str, step_id: str, *, to: str, now) -> dict:
        with tenant_session(tenant_id, self.url) as session:
            step = session.scalar(
                select(ApprovalStep)
                .where(
                    ApprovalStep.tenant_id == tenant_id,
                    ApprovalStep.id == step_id,
                )
                .with_for_update()
            )
            # Escalated already counts as not pending for this purpose: two
            # workers scanning at once must not both notify, and the second
            # must not overwrite where the first sent it.
            if (
                step is None
                or step.status != ApprovalStep.PENDING
                or step.escalated_at is not None
            ):
                raise StepNotPending(step_id)
            step.escalated_to = to
            step.escalated_at = now
            session.flush()
            return self._step_dict(step)

    def tenant_ids(self) -> list[str]:
        """Every active tenant id. The one deliberately cross-tenant read
        here, like `PgDocumentRepository.tenant_id_for_slug`: the SLA sweep
        has to visit each tenant to find its overdue steps, and `tenants` has
        no RLS. A suspended tenant's approvals stop escalating."""
        with tenant_session("", self.url) as session:
            return list(
                session.scalars(
                    select(Tenant.id).where(Tenant.status == "active")
                )
            )

    def manager_of(self, tenant_id: str, email: str) -> str | None:
        with tenant_session(tenant_id, self.url) as session:
            return session.scalar(
                select(User.manager_email).where(
                    User.tenant_id == tenant_id,
                    func.lower(User.email) == email.strip().casefold(),
                )
            )


class PgGlRuleRepository:
    """GL coding rules for a tenant. Storage only: the API validates."""

    FIELDS = frozenset({"vendor_name", "keyword", "gl_account", "enabled"})

    def __init__(self, url: str | None = None):
        self.url = url

    @staticmethod
    def _dict(r: GlRule) -> dict:
        return {
            "id": r.id,
            "vendor_name": r.vendor_name,
            "keyword": r.keyword,
            "gl_account": r.gl_account,
            "enabled": bool(r.enabled),
            "created_by": r.created_by,
            "created_at": _iso(r.created_at),
        }

    @staticmethod
    def _row(session, tenant_id: str, rule_id: str) -> GlRule | None:
        # Explicit tenant filter: SQLite has no RLS to fall back on.
        return session.scalar(
            select(GlRule).where(
                GlRule.tenant_id == tenant_id, GlRule.id == rule_id
            )
        )

    def list_rules(self, tenant_id: str) -> list[dict]:
        with tenant_session(tenant_id, self.url) as session:
            rows = session.scalars(
                select(GlRule)
                .where(GlRule.tenant_id == tenant_id)
                .order_by(GlRule.created_at, GlRule.id)
            )
            return [self._dict(r) for r in rows]

    def get(self, tenant_id: str, rule_id: str) -> dict | None:
        with tenant_session(tenant_id, self.url) as session:
            row = self._row(session, tenant_id, rule_id)
            return self._dict(row) if row else None

    def create(
        self,
        tenant_id: str,
        *,
        vendor_name,
        keyword,
        gl_account,
        created_by,
        enabled=True,
    ) -> dict:
        with tenant_session(tenant_id, self.url) as session:
            rule = GlRule(
                tenant_id=tenant_id,
                vendor_name=vendor_name,
                keyword=keyword,
                gl_account=gl_account,
                created_by=created_by,
                enabled=enabled,
            )
            session.add(rule)
            session.flush()
            return self._dict(rule)

    def update(self, tenant_id: str, rule_id: str, **fields) -> dict | None:
        unknown = set(fields) - self.FIELDS
        if unknown:
            raise TypeError(f"not editable: {sorted(unknown)}")
        with tenant_session(tenant_id, self.url) as session:
            row = self._row(session, tenant_id, rule_id)
            if row is None:
                return None
            for key, value in fields.items():
                setattr(row, key, value)
            session.flush()
            return self._dict(row)

    def delete(self, tenant_id: str, rule_id: str) -> bool:
        with tenant_session(tenant_id, self.url) as session:
            row = self._row(session, tenant_id, rule_id)
            if row is None:
                return False
            session.delete(row)
            return True


AMBIGUOUS = object()  # an item code shared by several lines of one PO


class PgPurchaseOrderRepository:
    """Purchase orders with lines, and the goods receipts against them.

    Storage plus the received/billed arithmetic; the API validates shapes.
    Every public method is one transaction, so a refused batch stores nothing.
    """

    HEADER = (
        "vendor_name", "total_amount", "currency", "status", "amount_invoiced",
        "customer_name", "valid_from", "valid_to", "amount_basis",
        "approved_quantity", "approved_unit_price", "hsn_sac",
    )  # fmt: skip

    def __init__(self, url: str | None = None):
        self.url = url

    @staticmethod
    def _f(value) -> float | None:
        return None if value is None else float(value)

    @classmethod
    def _header_dict(cls, o: PurchaseOrder) -> dict:
        return {
            "id": o.id,
            "po_number": o.po_number,
            "po_number_display": o.po_number_display,
            "vendor_name": o.vendor_name,
            "total_amount": cls._f(o.total_amount),
            "currency": o.currency,
            "status": o.status,
            "amount_invoiced": cls._f(o.amount_invoiced),
            "customer_name": o.customer_name,
            "valid_from": o.valid_from,
            "valid_to": o.valid_to,
            "amount_basis": o.amount_basis,
            "approved_quantity": cls._f(o.approved_quantity),
            "approved_unit_price": cls._f(o.approved_unit_price),
            "hsn_sac": o.hsn_sac,
            "source": o.source,
            "document_id": o.document_id,
            "uploaded_at": _iso(o.uploaded_at),
            "uploaded_by": o.uploaded_by,
        }

    @classmethod
    def _line_dict(cls, line: PoLine) -> dict:
        return {
            "line_number": line.line_number,
            "item_code": line.item_code,
            "description": line.description,
            "qty_ordered": cls._f(line.qty_ordered),
            "unit_price": cls._f(line.unit_price),
            "uom": line.uom,
            "receipt_required": line.receipt_required,
            "gl_account": line.gl_account,
        }

    @staticmethod
    def _order(
        session, tenant_id: str, po_number, *, lock: bool = False
    ) -> PurchaseOrder | None:
        stmt = select(PurchaseOrder).where(
            PurchaseOrder.tenant_id == tenant_id,
            PurchaseOrder.po_number
            == po_matching.normalize_po_number(po_number),
        )
        return session.scalar(stmt.with_for_update() if lock else stmt)

    @staticmethod
    def _received(session, tenant_id: str, order_id: str) -> dict[int, float]:
        rows = session.execute(
            select(
                PoLine.line_number,
                func.coalesce(func.sum(GrnLine.qty_received), 0),
            )
            .outerjoin(GrnLine, GrnLine.po_line_id == PoLine.id)
            .where(
                PoLine.tenant_id == tenant_id,
                PoLine.purchase_order_id == order_id,
            )
            .group_by(PoLine.line_number)
            .order_by(PoLine.line_number)
        )
        return {n: float(q) for n, q in rows}

    @staticmethod
    def _approved_since(
        session, tenant_id: str, po_number: str, since: datetime
    ) -> dict[int, float]:
        """Step-2.3 quantities per PO line on invoices approved after `since`."""
        # ponytail: scans this tenant's approved PO invoices on every match;
        # index/materialise if a PO gathers thousands.
        since = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
        billed: dict[int, float] = {}
        # The record, not the projected columns: a projection that failed
        # after an approval must not drop that approval from billed.
        rows = session.scalars(
            select(Invoice).where(
                Invoice.tenant_id == tenant_id,
                _workflow_value("decision") == "approve",
            )
        )
        for inv in rows:
            stored = ((inv.fields or {}).get("extraction") or {}).get(
                "invoice"
            ) or {}
            if (
                po_matching.normalize_po_number(stored.get("po_number"))
                != po_number
            ):
                continue
            fields = inv.fields or {}
            try:
                decided = datetime.fromisoformat(
                    fields["workflow"]["decided_at"]
                )
            except (KeyError, TypeError, ValueError):
                continue  # no readable decision time: not counted as billed
            if decided.tzinfo is None:
                decided = decided.replace(tzinfo=timezone.utc)
            if decided <= since:
                continue
            for v in (fields.get("phase2") or {}).get("validations") or []:
                if v.get("step") != "2.3":
                    continue
                for ln in v.get("lines") or []:
                    n, q = ln.get("po_line_number"), ln.get("quantity")
                    if n is not None and q is not None:
                        billed[int(n)] = billed.get(int(n), 0.0) + float(q)
        return billed

    def _billed(
        self, session, tenant_id: str, order: PurchaseOrder
    ) -> dict[int, float]:
        since = self._approved_since(
            session, tenant_id, order.po_number, order.uploaded_at
        )
        return {
            ln.line_number: float(ln.qty_invoiced_opening)
            + since.get(ln.line_number, 0.0)
            for ln in order.lines
        }

    def upsert_orders(self, tenant_id: str, orders: list[dict], **kw) -> dict:
        try:
            return self._upsert_orders(tenant_id, orders, **kw)
        except IntegrityError:
            # Both read "not on file" and both inserted; the loser stored nothing.
            raise ConcurrentWrite("purchase order") from None

    def _upsert_orders(
        self, tenant_id: str, orders: list[dict], *, uploaded_by: str,
        source: str = "upload", now: datetime | None = None,
        replace: bool = False, document_id: str | None = None,
    ) -> dict:  # fmt: skip
        """Upsert by PO number. `replace` also deletes the tenant's uploaded
        POs absent from `orders`, in the same transaction -- refused for a PO
        with goods received, since receipts are evidence and are never
        deleted. A scan-confirmed PO is not the ERP's to drop."""
        now = now or datetime.now(timezone.utc)
        created = updated = lines = 0
        seen: set[str] = set()
        with tenant_session(tenant_id, self.url) as session:
            # The owner check and the write share this transaction: Postgres
            # locks the PO row, SQLite takes its write lock before the read,
            # so an upload cannot land between them (ERP upload wins).
            if session.get_bind().dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            for i, data in enumerate(orders):
                key = po_matching.normalize_po_number(data["po_number"])
                if key in seen:
                    raise ValueError(
                        f"row {i + 1}: PO {data['po_number']} appears twice in this upload"
                    )
                seen.add(key)
                order = self._order(session, tenant_id, key, lock=True)
                if (
                    source == "document"
                    and order is not None
                    and order.source == "upload"
                ):
                    raise UploadOwned(data["po_number"])
                if order is None:
                    order = PurchaseOrder(tenant_id=tenant_id, po_number=key)
                    session.add(order)
                    created += 1
                else:
                    updated += 1
                order.po_number_display = data["po_number"]
                for f in self.HEADER:
                    if f in data:
                        setattr(order, f, data[f])
                # uploaded_at is the billed counting point and keeps its first
                # value: a re-upload must never drop approvals made since from
                # billed. The opening still resets from this upload, so an
                # opening that already includes them over-counts -- a hold,
                # never a silent pass.
                order.source, order.uploaded_by = source, uploaded_by
                order.document_id = document_id
                order.uploaded_at = order.uploaded_at or now
                session.flush()
                # Lines update in place by number: the receipts reference them.
                existing = {ln.line_number: ln for ln in order.lines}
                incoming = {ln["line_number"]: ln for ln in data["lines"]}
                for n, ln in existing.items():
                    if n in incoming:
                        continue
                    if session.scalar(
                        select(GrnLine.id).where(
                            GrnLine.tenant_id == tenant_id,
                            GrnLine.po_line_id == ln.id,
                        ).limit(1)
                    ):  # fmt: skip
                        raise ValueError(
                            f"row {i + 1}: PO {data['po_number']} line {n} "
                            "has goods received and cannot be removed"
                        )
                    order.lines.remove(ln)
                for n, ln in incoming.items():
                    row = existing.get(n)
                    if row is None:
                        row = PoLine(tenant_id=tenant_id, line_number=n)
                        order.lines.append(row)
                    row.item_code = ln.get("item_code")
                    row.description = ln.get("description") or ""
                    row.qty_ordered = ln["qty_ordered"]
                    row.unit_price = ln["unit_price"]
                    row.uom = ln.get("uom")
                    row.receipt_required = ln.get("receipt_required")
                    row.qty_invoiced_opening = ln.get("qty_invoiced") or 0
                    row.gl_account = ln.get("gl_account")
                lines += len(incoming)
                session.flush()
            if not replace:
                return {"created": created, "updated": updated, "lines": lines}
            deleted = 0
            for gone in session.scalars(
                select(PurchaseOrder).where(
                    PurchaseOrder.tenant_id == tenant_id,
                    PurchaseOrder.source == "upload",
                    PurchaseOrder.po_number.not_in(seen),
                )
            ).all():
                if session.scalar(
                    select(GoodsReceipt.id).where(
                        GoodsReceipt.tenant_id == tenant_id,
                        GoodsReceipt.purchase_order_id == gone.id,
                    ).limit(1)
                ):  # fmt: skip
                    raise ValueError(
                        f"PO {gone.po_number_display} has goods received "
                        "and cannot be removed"
                    )
                session.delete(gone)
                deleted += 1
        return {
            "created": created, "updated": updated, "lines": lines,
            "deleted": deleted,
        }  # fmt: skip

    @staticmethod
    def _item_key(code) -> str:
        return "".join(ch for ch in str(code).casefold() if ch.isalnum())

    def record_receipts(self, tenant_id: str, receipts: list[dict], **kw):
        try:
            return self._record_receipts(tenant_id, receipts, **kw)
        except IntegrityError:
            raise ConcurrentWrite("goods receipt") from None

    def _record_receipts(
        self, tenant_id: str, receipts: list[dict], *, recorded_by: str,
        source: str = "upload", now: datetime | None = None,
        document_id: str | None = None,
    ) -> dict:  # fmt: skip
        now = now or datetime.now(timezone.utc)
        recorded: list[str] = []
        skipped: list[str] = []
        po_numbers: list[str] = []
        with tenant_session(tenant_id, self.url) as session:
            for i, rc in enumerate(receipts):
                grn = rc["grn_number"]
                if grn in recorded or session.scalar(
                    select(GoodsReceipt.id).where(
                        GoodsReceipt.tenant_id == tenant_id,
                        GoodsReceipt.grn_number == grn,
                    )
                ):
                    skipped.append(grn)
                    continue
                order = self._order(session, tenant_id, rc["po_number"])
                if order is None:
                    raise ValueError(
                        f"row {i + 1}: purchase order {rc['po_number']!r} is not on file"
                    )
                # Lines resolve within this PO only: the schema does not tie
                # a grn_line's po_line to the receipt's PO, so this does.
                by_number = {ln.line_number: ln for ln in order.lines}
                by_code: dict = {}
                for ln in order.lines:
                    if ln.item_code:
                        key = self._item_key(ln.item_code)
                        # Two lines, one code: which one is a guess, so ask.
                        by_code[key] = AMBIGUOUS if key in by_code else ln
                receipt = GoodsReceipt(
                    tenant_id=tenant_id,
                    grn_number=grn,
                    purchase_order_id=order.id,
                    received_on=rc["received_on"],
                    reason=rc.get("reason"),
                    source=source,
                    document_id=document_id,
                    recorded_at=now,
                    recorded_by=recorded_by,
                )
                for item in rc["lines"]:
                    ref = item.get("line_number")
                    if ref is not None:
                        line = by_number.get(int(ref))
                    else:
                        ref = item.get("item_code")
                        line = by_code.get(self._item_key(ref))
                        if line is AMBIGUOUS:
                            raise ValueError(
                                f"row {i + 1}: item code {ref} matches several "
                                f"lines of PO {rc['po_number']}; give line_number"
                            )
                    if line is None:
                        raise ValueError(
                            f"row {i + 1}: PO {rc['po_number']} has no line {ref}"
                        )
                    if float(item["qty_received"]) < 0 and not rc.get("reason"):
                        raise ValueError(
                            f"row {i + 1}: a negative quantity needs a reason"
                        )
                    receipt.lines.append(
                        GrnLine(
                            tenant_id=tenant_id,
                            po_line_id=line.id,
                            qty_received=item["qty_received"],
                        )
                    )
                session.add(receipt)
                session.flush()
                recorded.append(grn)
                if order.po_number not in po_numbers:
                    po_numbers.append(order.po_number)
        return {
            "recorded": recorded,
            "skipped_duplicates": skipped,
            "po_numbers": po_numbers,
        }

    def list_orders(self, tenant_id: str) -> list[dict]:
        with tenant_session(tenant_id, self.url) as session:
            out = []
            for o in session.scalars(
                select(PurchaseOrder)
                .where(PurchaseOrder.tenant_id == tenant_id)
                .order_by(PurchaseOrder.po_number)
            ):
                out.append(
                    {
                        **self._header_dict(o),
                        "line_count": len(o.lines),
                        "received_total": sum(
                            self._received(session, tenant_id, o.id).values()
                        ),
                        "billed_total": sum(
                            self._billed(session, tenant_id, o).values()
                        ),
                    }
                )
            return out

    def has_orders(self, tenant_id: str) -> bool:
        with tenant_session(tenant_id, self.url) as session:
            return (
                session.scalar(
                    select(PurchaseOrder.id)
                    .where(PurchaseOrder.tenant_id == tenant_id)
                    .limit(1)
                )
                is not None
            )

    def _receipts(
        self, session, tenant_id: str, order: PurchaseOrder | None = None
    ):
        stmt = (
            select(GoodsReceipt, PurchaseOrder.po_number_display)
            .join(PurchaseOrder, PurchaseOrder.id == GoodsReceipt.purchase_order_id)
            .where(GoodsReceipt.tenant_id == tenant_id)
            .order_by(
                GoodsReceipt.received_on,
                GoodsReceipt.recorded_at,
                GoodsReceipt.grn_number,
            )
        )  # fmt: skip
        if order is not None:
            stmt = stmt.where(GoodsReceipt.purchase_order_id == order.id)
        lines = {
            ln.id: ln
            for ln in session.scalars(
                select(PoLine).where(PoLine.tenant_id == tenant_id)
            )
        }
        return [
            {
                "grn_number": g.grn_number,
                "po_number": display,
                "received_on": g.received_on,
                "reason": g.reason,
                "source": g.source,
                "document_id": g.document_id,
                "recorded_at": _iso(g.recorded_at),
                "recorded_by": g.recorded_by,
                "lines": [
                    {
                        "line_number": lines[gl.po_line_id].line_number,
                        "item_code": lines[gl.po_line_id].item_code,
                        "qty_received": float(gl.qty_received),
                    }
                    for gl in g.lines
                ],
            }
            for g, display in session.execute(stmt)
        ]

    def list_receipts(self, tenant_id: str, po_number=None) -> list[dict]:
        with tenant_session(tenant_id, self.url) as session:
            order = None
            if po_number is not None:
                order = self._order(session, tenant_id, po_number)
                if order is None:
                    return []
            return self._receipts(session, tenant_id, order)

    def get_order(self, tenant_id: str, po_number) -> dict | None:
        with tenant_session(tenant_id, self.url) as session:
            o = self._order(session, tenant_id, po_number)
            if o is None:
                return None
            received = self._received(session, tenant_id, o.id)
            billed = self._billed(session, tenant_id, o)
            return {
                **self._header_dict(o),
                "lines": [
                    {
                        **self._line_dict(ln),
                        "received": received.get(ln.line_number, 0.0),
                        "billed": billed[ln.line_number],
                        "remaining": float(ln.qty_ordered)
                        - billed[ln.line_number],
                    }
                    for ln in o.lines
                ],
                "receipts": self._receipts(session, tenant_id, o),
            }

    def po_owned_by_upload(self, tenant_id: str, po_number) -> bool:
        """True when an uploaded sheet owns this PO, so a scan must not touch it."""
        with tenant_session(tenant_id, self.url) as session:
            o = self._order(session, tenant_id, po_number)
            return o is not None and o.source == "upload"

    def stored_documents(
        self, tenant_id: str, document_ids, po_numbers: dict | None = None
    ) -> set[str]:
        """The documents something stored came from: a PO or receipt that
        carries the document's id, or -- for a scanned PO, `po_numbers` maps
        its document to the number it confirmed -- that PO being on file,
        since a later rescan or ERP upload takes the PO's link over."""
        ids = list(document_ids)
        if not ids:
            return set()
        with tenant_session(tenant_id, self.url) as session:
            found = set()
            for model in (PurchaseOrder, GoodsReceipt):
                found |= set(
                    session.scalars(
                        select(model.document_id).where(
                            model.tenant_id == tenant_id,
                            model.document_id.in_(ids),
                        )
                    )
                )
            keys = {
                doc: po_matching.normalize_po_number(n)
                for doc, n in (po_numbers or {}).items()
                if n and doc in ids
            }
            on_file = set(
                session.scalars(
                    select(PurchaseOrder.po_number).where(
                        PurchaseOrder.tenant_id == tenant_id,
                        PurchaseOrder.po_number.in_(set(keys.values())),
                    )
                )
            ) if keys else set()  # fmt: skip
            found |= {doc for doc, key in keys.items() if key in on_file}
            return found

    def book(self, tenant_id: str, po_number) -> dict | None:
        with tenant_session(tenant_id, self.url) as session:
            o = self._order(session, tenant_id, po_number)
            if o is None:
                return None
            return {
                "po": self._header_dict(o),
                "lines": [self._line_dict(ln) for ln in o.lines],
                "received": self._received(session, tenant_id, o.id),
                "billed": self._billed(session, tenant_id, o),
                "uploaded_at": _iso(o.uploaded_at),
            }


class PgSupportingDocumentRepository:
    """Scanned POs and receipts waiting for a person; see SupportingDocument."""

    def __init__(self, url: str | None = None):
        self.url = url

    @staticmethod
    def _dict(row: SupportingDocument, filename: str | None) -> dict:
        return {
            "id": row.id,
            "document_id": row.document_id,
            "kind": row.kind,
            "status": row.status,
            "extracted": row.extracted or {},
            "po_number": row.po_number,
            "blocked_reason": row.blocked_reason,
            "extraction_error": row.extraction_error,
            "reviewed_by": row.reviewed_by,
            "reviewed_at": _iso(row.reviewed_at),
            "note": row.note,
            "created_at": _iso(row.created_at),
            "filename": filename,
        }

    @staticmethod
    def _select(tenant_id: str):
        return (
            select(SupportingDocument, Document.filename)
            .join(
                Document,
                and_(
                    Document.id == SupportingDocument.document_id,
                    Document.tenant_id == tenant_id,
                ),
            )
            .where(SupportingDocument.tenant_id == tenant_id)
            .order_by(SupportingDocument.created_at, SupportingDocument.id)
        )

    def _one(self, session, tenant_id: str, id: str):
        return session.execute(
            self._select(tenant_id).where(SupportingDocument.id == id)
        ).first()

    @staticmethod
    def _existing(session, tenant_id: str, document_id: str):
        return session.scalar(
            select(SupportingDocument).where(
                SupportingDocument.tenant_id == tenant_id,
                SupportingDocument.document_id == document_id,
            )
        )

    def upsert_from_extraction(self, tenant_id: str, **kw) -> dict | None:
        """Create the row, or overwrite it while pending/blocked. A decided
        row is left alone and None is returned."""
        try:
            return self._upsert(tenant_id, **kw)
        except IntegrityError:
            # Another worker inserted it after this one looked: the row now
            # exists, so the same overwrite rule applies to it.
            return self._upsert(tenant_id, **kw)

    def _upsert(
        self, tenant_id: str, *, document_id: str, kind: str, extracted: dict,
        po_number: str | None, blocked_reason: str | None,
        extraction_error: str | None,
    ) -> dict | None:  # fmt: skip
        po = po_matching.normalize_po_number(po_number) if po_number else None
        with tenant_session(tenant_id, self.url) as session:
            row = self._existing(session, tenant_id, document_id)
            if row is None:
                row = SupportingDocument(
                    tenant_id=tenant_id, document_id=document_id
                )
                session.add(row)
            elif row.status not in ("pending", "blocked"):
                return None
            row.kind, row.extracted, row.po_number = kind, extracted, po
            row.blocked_reason = blocked_reason
            row.extraction_error = extraction_error
            row.status = "blocked" if blocked_reason else "pending"
            session.flush()
            return self._dict(*self._one(session, tenant_id, row.id))

    def list(
        self,
        tenant_id: str,
        statuses: tuple[str, ...],
        limit: int | None = 200,
        newest_first: bool = False,
    ) -> list[dict]:
        """Oldest first, so the longest-waiting scan is never cut off; the
        newest first for a search of recent acceptances. `limit=None` reads
        every row (an internal sweep, never a page)."""
        query = self._select(tenant_id).where(
            SupportingDocument.status.in_(statuses)
        )
        if newest_first:
            query = query.order_by(None).order_by(
                SupportingDocument.created_at.desc(),
                SupportingDocument.id.desc(),
            )
        with tenant_session(tenant_id, self.url) as session:
            rows = session.execute(query.limit(limit)).all()
            return [self._dict(r, f) for r, f in rows]

    def for_documents(self, tenant_id: str, document_ids) -> list[dict]:
        """The review rows of these documents (any status)."""
        if not document_ids:
            return []
        with tenant_session(tenant_id, self.url) as session:
            rows = session.execute(
                self._select(tenant_id).where(
                    SupportingDocument.document_id.in_(list(document_ids))
                )
            ).all()
            return [self._dict(r, f) for r, f in rows]

    def get(self, tenant_id: str, id: str) -> dict | None:
        with tenant_session(tenant_id, self.url) as session:
            found = self._one(session, tenant_id, id)
            return self._dict(*found) if found else None

    def mark(
        self, tenant_id: str, id: str, *, status: str, reviewed_by: str,
        expect: tuple[str, ...], note: str | None = None,
        extracted: dict | None = None,
    ) -> dict:  # fmt: skip
        """Record the decision, only while the row is still in one of the
        `expect` statuses (a conditional UPDATE, so two reviewers cannot both
        decide); otherwise StatusChanged. Accepting clears the block. The
        result's `previous` is the status and block the decision replaced,
        so an undo restores exactly that."""
        values = {
            "status": status,
            "reviewed_by": reviewed_by,
            "note": note,
            "reviewed_at": datetime.now(timezone.utc),
        }
        if extracted is not None:
            values["extracted"] = extracted  # the clerk's corrected version
        if status == "accepted":
            values["blocked_reason"] = None
        with tenant_session(tenant_id, self.url) as session:
            # Compare-and-set on what was read: the UPDATE matches only if
            # status and block are still those, so `previous` is exact.
            for _ in range(3):
                found = self._one(session, tenant_id, id)
                if found is None:
                    raise KeyError(id)
                current = found[0]
                was = {
                    "status": current.status,
                    "blocked_reason": current.blocked_reason,
                }
                if was["status"] not in expect:
                    raise StatusChanged(was["status"])
                session.expire(current)
                changed = session.execute(
                    update(SupportingDocument)
                    .where(
                        SupportingDocument.tenant_id == tenant_id,
                        SupportingDocument.id == id,
                        SupportingDocument.status == was["status"],
                        SupportingDocument.blocked_reason.is_not_distinct_from(
                            was["blocked_reason"]
                        ),
                    )
                    .values(**values)
                    .execution_options(synchronize_session=False)
                ).rowcount
                if changed:
                    found = self._one(session, tenant_id, id)
                    return {**self._dict(*found), "previous": was}
            raise StatusChanged(self._one(session, tenant_id, id)[0].status)

    def revert_to_pending(
        self, tenant_id: str, id: str, *, extracted: dict,
        status: str = "pending", blocked_reason: str | None = None,
        reviewed_at: str | None = None,
    ) -> dict:  # fmt: skip
        """Undo an acceptance: back to `status` and `blocked_reason` (what the
        acceptance replaced), with `extracted` and no reviewer. Only from
        accepted, and with `reviewed_at` only from the acceptance made then,
        never a later one (StatusChanged)."""
        with tenant_session(tenant_id, self.url) as session:
            match = [
                SupportingDocument.tenant_id == tenant_id,
                SupportingDocument.id == id,
                SupportingDocument.status == "accepted",
            ]
            if reviewed_at is not None:
                found = self._one(session, tenant_id, id)
                if found is None:
                    raise KeyError(id)
                if _iso(found[0].reviewed_at) != reviewed_at:
                    raise StatusChanged(found[0].status)
                # The UPDATE re-checks it, so a re-acceptance landing after
                # this read still makes it miss.
                match.append(
                    SupportingDocument.reviewed_at == found[0].reviewed_at
                )
                session.expire(found[0])
            changed = session.execute(
                update(SupportingDocument)
                .where(*match)
                .values(
                    status=status,
                    blocked_reason=blocked_reason,
                    extracted=extracted,
                    reviewed_by=None,
                    reviewed_at=None,
                    note=None,
                )
                .execution_options(synchronize_session=False)
            ).rowcount
            found = self._one(session, tenant_id, id)
            if found is None:
                raise KeyError(id)
            if not changed:
                raise StatusChanged(found[0].status)
            return self._dict(*found)

    def unblock(self, tenant_id: str, po_number) -> list[str]:
        """Blocked rows waiting on this PO become pending; returns their ids."""
        po = po_matching.normalize_po_number(po_number)
        with tenant_session(tenant_id, self.url) as session:
            rows = session.scalars(
                select(SupportingDocument).where(
                    SupportingDocument.tenant_id == tenant_id,
                    SupportingDocument.status == "blocked",
                    SupportingDocument.po_number == po,
                )
            ).all()
            for r in rows:
                r.status, r.blocked_reason = "pending", None
            return [r.id for r in rows]


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
                    "at": _iso(correction.created_at),
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
# KPIs
# ---------------------------------------------------------------------------

# Why an invoice needed a person, first match wins. "other" exists so the
# buckets always add up to the exceptions; a non-zero "other" is a data
# problem to look at, not a category to design around.
BUCKETS = (
    "rejected_by_rules",
    "three_way_hold",
    "low_confidence",
    "approval_required",
    "manual_decision",
    "other",
)
KPI_CASES_LIMIT = 500


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile; None of nothing."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p * len(ordered)))
    return round(float(ordered[rank - 1]), 2)


def exception_bucket(
    decision: str | None,
    held: bool,
    routing: str | None,
    chain: bool,
    human_decision: str | None,
) -> str:
    from .. import case_projection  # noqa: PLC0415

    if case_projection.classify_verdict(decision) == "reject":
        return "rejected_by_rules"
    if held:
        return "three_way_hold"
    if routing == "REVIEW":
        return "low_confidence"
    if chain:
        return "approval_required"
    if human_decision is not None:
        return "manual_decision"
    return "other"


def _aware(value: datetime | None) -> datetime | None:
    # SQLite hands back naive datetimes for timezone-aware columns.
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 3) if denominator else None


def _hours(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 3600


class KpiRepository:
    """The operating numbers an AP manager is judged on, over one window.

    Reads typed columns only -- never the artifact JSON -- so a month of
    invoices costs one indexed select. One row per invoice comes back and the
    arithmetic happens here; SQLite has no percentile function and the
    drill-down wants the same rows anyway.
    """

    # ponytail: one row per invoice in the window; aggregate in SQL if a
    # window ever exceeds ~100k invoices.

    def __init__(self, url: str | None = None):
        self.url = url

    def _rows(self, session, tenant_id: str, since: datetime | None) -> list:
        received = func.coalesce(Document.created_at, Invoice.created_at)
        held = exists(
            select(Finding.id).where(
                Finding.tenant_id == tenant_id,
                Finding.invoice_id == Invoice.id,
                Finding.step == "2.3",
                Finding.passed.is_(False),
            )
        )
        chain = exists(
            select(ApprovalStep.id).where(
                ApprovalStep.tenant_id == tenant_id,
                ApprovalStep.case_id == Invoice.case_id,
            )
        )
        # The latest run, not the sum: databases projected before the
        # one-row-per-invoice fix still hold duplicates of the same cost.
        cost = (
            select(PipelineRun.cost_usd)
            .where(
                PipelineRun.tenant_id == tenant_id,
                PipelineRun.invoice_id == Invoice.id,
            )
            .order_by(PipelineRun.created_at.desc(), PipelineRun.id.desc())
            .limit(1)
            .scalar_subquery()
        )
        statement = (
            select(
                Invoice.case_id,
                received.label("received"),
                Invoice.decided_at,
                Invoice.human_decision,
                Invoice.decision,
                Invoice.routing,
                Invoice.po_number,
                held.label("held"),
                chain.label("chain"),
                cost.label("cost_usd"),
            )
            .select_from(Invoice)
            .outerjoin(
                Document,
                and_(
                    Document.tenant_id == tenant_id,
                    Document.id == Invoice.case_id,
                ),
            )
            .where(
                Invoice.tenant_id == tenant_id,
                # A scanned PO or receipt is never an invoice (same test as
                # the sheet).
                or_(Document.kind.is_(None), Document.kind == "invoice"),
            )
            .order_by(received.desc())
        )
        if since is not None:
            statement = statement.where(received >= since)
        return session.execute(statement).all()

    @staticmethod
    def _decided(row) -> bool:
        # The worker projects a touchless invoice as decided before it routes
        # the approval chain; a chain nobody has finished means it is not.
        return row.decided_at is not None and (
            row.human_decision is not None or not row.chain
        )

    def summary(
        self,
        tenant_id: str,
        since: datetime | None,
        now: datetime | None = None,
    ) -> dict:
        now = now or datetime.now(timezone.utc)
        with tenant_session(tenant_id, self.url) as session:
            rows = self._rows(session, tenant_id, since)

        decided = [r for r in rows if self._decided(r)]
        touchless = [r for r in decided if r.human_decision is None]
        touchless_ids = {r.case_id for r in touchless}

        buckets = dict.fromkeys(BUCKETS, 0)
        for r in rows:
            if r.case_id not in touchless_ids:
                buckets[
                    exception_bucket(
                        r.decision, r.held, r.routing, r.chain, r.human_decision
                    )
                ] += 1

        cycle: list[float] = []
        skipped = 0
        for r in decided:
            start, end = _aware(r.received), _aware(r.decided_at)
            hours = _hours(start, end) if start and end else None
            if hours is None or hours < 0:
                skipped += (
                    1  # fail closed: a bad clock never shortens the median
                )
            else:
                cycle.append(hours)

        open_rows = [r for r in rows if not self._decided(r)]
        oldest = min(
            (_aware(r.received) for r in open_rows if r.received), default=None
        )

        po_rows = [r for r in rows if r.po_number]
        # A hold is a three-way-match outcome: only PO-backed rows can have
        # one, so the rate can never exceed 1.
        held = [r for r in po_rows if r.held]

        costs = [float(r.cost_usd) for r in rows if r.cost_usd is not None]

        return {
            "received": len(rows),
            "decided": len(decided),
            "touchless": len(touchless),
            "touchless_rate": _rate(len(touchless), len(rows)),
            "exception_rate": _rate(len(rows) - len(touchless), len(rows)),
            "exceptions": buckets,
            "cycle_hours": {
                "median": percentile(cycle, 0.5),
                "p90": percentile(cycle, 0.9),
                "skipped": skipped,
            },
            "open": {
                "count": len(open_rows),
                "oldest_hours": round(_hours(oldest, now), 1)
                if oldest
                else None,
            },
            "three_way": {
                "po_invoices": len(po_rows),
                "held": len(held),
                "hold_rate": _rate(len(held), len(po_rows)),
            },
            "ai_cost": {
                "per_invoice_usd": round(sum(costs) / len(costs), 4)
                if costs
                else None,
                "total_usd": round(sum(costs), 4),
            },
        }

    def cases(
        self,
        tenant_id: str,
        since: datetime | None,
        reason: str,
    ) -> dict:
        """The invoices behind one bucket, newest received first."""
        with tenant_session(tenant_id, self.url) as session:
            rows = self._rows(session, tenant_id, since)
        matching = [
            r.case_id
            for r in rows
            if not (self._decided(r) and r.human_decision is None)
            and exception_bucket(
                r.decision, r.held, r.routing, r.chain, r.human_decision
            )
            == reason
        ]
        return {
            "case_ids": matching[:KPI_CASES_LIMIT],
            "truncated": len(matching) > KPI_CASES_LIMIT,
        }


# ---------------------------------------------------------------------------
# Sign-in: who is who, and which domains may sign in where
# ---------------------------------------------------------------------------


class DomainTaken(Exception):
    """This domain is already bound, by this tenant or another."""


def _user_dict(
    u: User, tenant_name: str | None, tenant_status: str | None = None
) -> dict:
    return {
        "id": u.id,
        "email": u.email,
        "role": u.role,
        "is_active": bool(u.is_active),
        "uid": u.uid,
        "display_name": u.display_name,
        "last_login_at": u.last_login_at,
        "tenant_name": tenant_name,
        "tenant_status": tenant_status,
    }


class PgUserDirectory:
    """The users rows as the request path reads them.

    Small on purpose: `find` is on every authenticated request, so it is one
    statement -- the row and its tenant's name and status together; the writes happen
    once per sign-in at most.
    """

    def __init__(self, url: str | None = None):
        self.url = url

    @staticmethod
    def _tenant_name(session, tenant_id: str) -> str | None:
        return session.scalar(select(Tenant.name).where(Tenant.id == tenant_id))

    def find(self, tenant_id: str, email: str) -> dict | None:
        """The row for this address, compared case-insensitively.

        Rows stored before emails were lowercased can differ only in case;
        the oldest wins, so the answer never flips between requests.
        """
        wanted = (email or "").strip().lower()
        with tenant_session(tenant_id, self.url) as session:
            found = session.execute(
                select(User, Tenant.name, Tenant.status)
                .outerjoin(Tenant, Tenant.id == User.tenant_id)
                .where(
                    User.tenant_id == tenant_id,
                    func.lower(User.email) == wanted,
                )
                .order_by(User.created_at, User.id)
                .limit(1)
            ).first()
            if found is None:
                return None
            return _user_dict(found[0], found[1], found[2])

    def tenant_status(self, tenant_id: str) -> str | None:
        """The tenant's status, for a caller with no users row to join it
        through. None when there is no tenants row at all."""
        with tenant_session(tenant_id, self.url) as session:
            return session.scalar(
                select(Tenant.status).where(Tenant.id == tenant_id)
            )

    def get(self, tenant_id: str, user_id: str) -> dict | None:
        with tenant_session(tenant_id, self.url) as session:
            row = session.scalar(
                select(User).where(
                    User.tenant_id == tenant_id, User.id == user_id
                )
            )
            return None if row is None else _user_dict(row, None)

    def _row(self, session, tenant_id: str, user_id: str) -> User:
        row = session.scalar(
            select(User).where(User.tenant_id == tenant_id, User.id == user_id)
        )
        if row is None:
            raise LookupError(user_id)
        return row

    def link_uid(self, tenant_id: str, user_id: str, uid: str) -> None:
        with tenant_session(tenant_id, self.url) as session:
            self._row(session, tenant_id, user_id).uid = uid

    def touch_login(self, tenant_id: str, user_id: str, now: datetime) -> None:
        with tenant_session(tenant_id, self.url) as session:
            self._row(session, tenant_id, user_id).last_login_at = now

    def provision(
        self,
        tenant_id: str,
        email: str,
        role: str,
        uid: str | None,
        display_name: str | None,
        *,
        audit: tuple[RunContext, str, dict] | None = None,
        now: datetime | None = None,
    ) -> dict:
        """Create the row; with `audit`, its event commits or rolls back with it.

        The event's payload gains the new row's `user_id`, known only here.
        """
        with tenant_session(tenant_id, self.url) as session:
            row = User(
                tenant_id=tenant_id,
                email=email.strip().lower(),
                role=role,
                uid=uid,
                display_name=display_name,
                last_login_at=now,
            )
            session.add(row)
            session.flush()
            if audit is not None:
                ctx, event_type, payload = audit
                session.add(
                    audit_event_row(
                        ctx, event_type, {**payload, "user_id": row.id}
                    )
                )
            return _user_dict(row, self._tenant_name(session, tenant_id))

    def set_role(
        self,
        tenant_id: str,
        user_id: str,
        role: str,
        *,
        audit: tuple[RunContext, str, dict, dict | None] | None = None,
    ) -> dict:
        """Change the role; with `audit`, its event commits or rolls back
        with it. The event's `before` gains the role this transaction
        replaced, whatever the caller read earlier."""
        with tenant_session(tenant_id, self.url) as session:
            row = self._row(session, tenant_id, user_id)
            previous, row.role = row.role, role
            session.flush()
            if audit is not None:
                ctx, event_type, payload, before = audit
                session.add(
                    audit_event_row(
                        ctx,
                        event_type,
                        payload,
                        {**(before or {}), "role": previous},
                    )
                )
            return {**_user_dict(row, None), "previous_role": previous}

    def set_active(
        self,
        tenant_id: str,
        user_id: str,
        active: bool,
        *,
        audit: tuple[RunContext, str, dict, dict | None] | None = None,
    ) -> dict:
        """Change the flag; with `audit`, its event commits or rolls back
        with it."""
        with tenant_session(tenant_id, self.url) as session:
            row = self._row(session, tenant_id, user_id)
            previous, row.is_active = bool(row.is_active), active
            session.flush()
            if audit is not None:
                ctx, event_type, payload, before = audit
                session.add(audit_event_row(ctx, event_type, payload, before))
            return {**_user_dict(row, None), "previous_active": previous}

    def domain(self, domain: str) -> dict | None:
        """Which tenant owns this domain. Runs before a tenant is known, so
        under the lookup sentinel; see rls.SSO_LOOKUP_TENANT."""
        from .rls import SSO_LOOKUP_TENANT  # noqa: PLC0415

        wanted = (domain or "").strip().lower()
        with tenant_session(SSO_LOOKUP_TENANT, self.url) as session:
            row = session.scalar(
                select(SsoDomain).where(SsoDomain.domain == wanted)
            )
            if row is None:
                return None
            return {
                "id": row.id,
                "tenant_id": row.tenant_id,
                "tenant_name": self._tenant_name(session, row.tenant_id),
                "tenant_status": session.scalar(
                    select(Tenant.status).where(Tenant.id == row.tenant_id)
                ),
                "domain": row.domain,
                "provider": row.provider,
                "default_role": row.default_role,
            }


class PgSsoDomainRepository:
    """A tenant's own domain bindings. The API validates; this stores."""

    def __init__(self, url: str | None = None):
        self.url = url

    @staticmethod
    def _dict(d: SsoDomain) -> dict:
        return {
            "id": d.id,
            "domain": d.domain,
            "provider": d.provider,
            "default_role": d.default_role,
            "created_by": d.created_by,
            "created_at": _iso(d.created_at),
        }

    def list(self, tenant_id: str) -> list[dict]:
        with tenant_session(tenant_id, self.url) as session:
            rows = session.scalars(
                select(SsoDomain)
                .where(SsoDomain.tenant_id == tenant_id)
                .order_by(SsoDomain.domain)
            )
            return [self._dict(d) for d in rows]

    def add(
        self,
        tenant_id: str,
        *,
        domain: str,
        provider: str,
        default_role: str,
        created_by: str,
    ) -> dict:
        try:
            with tenant_session(tenant_id, self.url) as session:
                row = SsoDomain(
                    tenant_id=tenant_id,
                    domain=domain.strip().lower(),
                    provider=provider,
                    default_role=default_role,
                    created_by=created_by,
                )
                session.add(row)
                session.flush()
                return self._dict(row)
        except IntegrityError as exc:
            # The unique constraint is the arbiter; a check-then-insert would
            # let two admins race for the same domain.
            # Matched on the message: pg8000 (production) carries no `.diag`;
            # it names the constraint, SQLite names the column.
            text = str(exc.orig)
            if "uq_sso_domain" in text or "sso_domains.domain" in text:
                raise DomainTaken(domain) from exc
            raise

    def remove(self, tenant_id: str, domain_id: str) -> bool:
        with tenant_session(tenant_id, self.url) as session:
            row = session.scalar(
                select(SsoDomain).where(
                    SsoDomain.tenant_id == tenant_id, SsoDomain.id == domain_id
                )
            )
            if row is None:
                return False
            session.delete(row)
            return True


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


def _as_number(value) -> Decimal | None:
    """An amount from a filter, as a number the database can compare.

    Returns None for anything unparseable so a typo in a saved view narrows
    nothing rather than failing the whole page.
    """
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError, TypeError):
        return None


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

        conditions = [
            Invoice.tenant_id == tenant_id,
            # A scanned PO or receipt's case holds an extraction awaiting
            # confirmation, never an invoice.
            ~exists(
                select(Document.id).where(
                    Document.tenant_id == tenant_id,
                    Document.id == Invoice.case_id,
                    Document.kind.is_not(None),
                    Document.kind != "invoice",
                )
            ),
        ]

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
        # Coerced, because an amount arrives from a query string as text and
        # Postgres refuses `numeric >= character varying` outright. SQLite
        # casts it quietly, so this only ever failed against the real
        # database -- a filter no test could catch on the test dialect.
        if (value := _as_number(filters.get("min_total"))) is not None:
            conditions.append(Invoice.total_inc_tax >= value)
        if (value := _as_number(filters.get("max_total"))) is not None:
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

        if flag == "needs_attention":
            # The one flag that is a question rather than a property: any of
            # these, where two named flags would mean both of them. Asked for
            # by the built-in view of the same name, and by the tile that
            # counts it -- the two have to agree or the tile lies about what
            # clicking through will show.
            return self._any_flag(ATTENTION_FLAGS, tenant_id, filters)
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
        if flag == "overruled":
            # Either direction: an exemption the rulebook is missing, or a
            # check it is missing. Both are the rulebook being wrong.
            return Invoice.decision_agreement.in_(
                ("exemption", "too_permissive")
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
        that cannot jump to page nine would be a worse grid for a saving
        nobody can measure yet.

        The page costs a fixed number of queries -- not one per row. Findings,
        edit counts and duplicate checks are each fetched for the whole page in
        one statement, because a query per row is invisible against SQLite and
        costs a round trip each against a database three hundred milliseconds
        away. That is the same mistake the document list and the review queue
        both had to have taken out of them.
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
            invoices = session.scalars(
                statement.order_by(direction, Invoice.id)
                .limit(limit)
                .offset(offset)
            ).all()
            if not invoices:
                return [], total or 0

            ids = [row.id for row in invoices]
            failed = self._failed_steps(session, tenant_id, ids)
            edits = self._edit_counts(session, tenant_id, ids)
            duplicates = self._duplicate_ids(session, tenant_id, invoices)

            return [
                self._row(
                    invoice,
                    filters,
                    failed_steps=failed.get(invoice.id, set()),
                    edits=edits.get(invoice.id, 0),
                    duplicate=invoice.id in duplicates,
                )
                for invoice in invoices
            ], (total or 0)

    @staticmethod
    def _failed_steps(
        session, tenant_id: str, ids: list[str]
    ) -> dict[str, set]:
        """Which validation steps failed, for every invoice on the page."""
        found: dict[str, set] = {}
        for invoice_id, step in session.execute(
            select(Finding.invoice_id, Finding.step).where(
                Finding.tenant_id == tenant_id,
                Finding.invoice_id.in_(ids),
                Finding.passed.is_(False),
            )
        ):
            found.setdefault(invoice_id, set()).add(step)
        return found

    @staticmethod
    def _edit_counts(session, tenant_id: str, ids: list[str]) -> dict[str, int]:
        return dict(
            session.execute(
                select(Correction.invoice_id, func.count(Correction.id))
                .where(
                    Correction.tenant_id == tenant_id,
                    Correction.invoice_id.in_(ids),
                )
                .group_by(Correction.invoice_id)
            ).all()
        )

    @staticmethod
    def _duplicate_ids(session, tenant_id: str, invoices) -> set[str]:
        """Which of these invoices share a number with another one.

        Compared against the whole tenant, not just the page: an invoice is a
        duplicate of one filtered off the screen just as much as of one beside
        it.
        """
        keys = {
            (row.invoice_number, row.vendor_tax_id)
            for row in invoices
            if row.invoice_number
        }
        if not keys:
            return set()

        numbers = {number for number, _ in keys}
        seen: dict[tuple, list[str]] = {}
        for invoice_id, number, tax_id in session.execute(
            select(
                Invoice.id, Invoice.invoice_number, Invoice.vendor_tax_id
            ).where(
                Invoice.tenant_id == tenant_id,
                Invoice.invoice_number.in_(numbers),
            )
        ):
            seen.setdefault((number, tax_id), []).append(invoice_id)

        duplicated = set()
        for row in invoices:
            others = seen.get((row.invoice_number, row.vendor_tax_id), [])
            if len(others) > 1:
                duplicated.add(row.id)
        return duplicated

    def _row(
        self,
        invoice,
        filters: dict,
        *,
        failed_steps: set,
        edits: int,
        duplicate: bool,
    ) -> dict:
        workflow = (invoice.fields or {}).get("workflow") or {}
        row: dict = {}
        for key, column in BY_KEY.items():
            if column.source == "column":
                value = getattr(invoice, key, None)
                row[key] = float(value) if isinstance(value, Decimal) else value
            elif column.source == "workflow":
                row[key] = workflow.get(WORKFLOW_PATHS[key])

        row["corrected_fields"] = edits
        accounts = [
            e.get("gl_account")
            for e in sorted(
                gl_coding.valid_entries(
                    (invoice.fields or {}).get("gl_coding")
                ),
                key=lambda e: e["line_number"],
            )
        ]
        row["gl_accounts"] = list(
            dict.fromkeys(a for a in accounts if isinstance(a, str) and a)
        )
        row["quality_flags"] = self._flags_for(
            invoice, row, filters, failed_steps, duplicate
        )
        return row

    @staticmethod
    def _flags_for(
        invoice, row: dict, filters: dict, failed_steps: set, duplicate: bool
    ) -> list[str]:
        """Which quality flags this row carries, from data already in hand."""
        flags = [
            flag for flag, step in FLAG_STEPS.items() if step in failed_steps
        ]

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

        evidence = (
            ((invoice.fields or {}).get("extraction") or {}).get("invoice")
            or {}
        ).get("_evidence") or {}
        if (evidence.get("_summary") or {}).get("has_text_layer") is False:
            flags.append("no_text_layer")

        threshold = filters.get("high_value_threshold") or 0
        if threshold and invoice.total_inc_tax is not None:
            if float(invoice.total_inc_tax) >= float(threshold):
                flags.append("high_value")

        if row.get("corrected_fields"):
            flags.append("edited")
        if invoice.decision_agreement in ("exemption", "too_permissive"):
            flags.append("overruled")
        if duplicate:
            flags.append("duplicate_number")

        return sorted(flags)

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

            # Counted here rather than from the page: a reader who sees
            # "3 duplicates" above a page of 100 rows needs it to mean three in
            # the whole filtered set, not three among the hundred on screen.
            duplicates = self._count_flagged(
                session, tenant_id, filters, ["duplicate_number"]
            )
            needs_attention = self._count_flagged(
                session, tenant_id, filters, ATTENTION_FLAGS
            )

        return {
            "total": total or 0,
            "by_currency": sorted(by_currency, key=lambda c: c["currency"]),
            "vendors": vendors or 0,
            "approved_today": approved_today or 0,
            "approved_this_month": approved_month or 0,
            "duplicates": duplicates,
            "needs_attention": needs_attention,
        }

    def _any_flag(self, flags, tenant_id: str, filters: dict):
        """One predicate matching a row that carries any of these flags."""
        conditions = [
            condition
            for flag in flags
            if (condition := self._flag_condition(flag, tenant_id, filters))
            is not None
        ]
        return or_(*conditions) if conditions else None

    def _count_flagged(
        self, session, tenant_id: str, filters: dict, flags
    ) -> int:
        """How many filtered rows carry any one of these quality flags.

        `or_`, not the `and_` the `quality` filter uses: a filter narrows to
        rows that are all of these things, a count asks how many are any of
        them, and "needs attention" is a question of the second kind.
        """
        condition = self._any_flag(flags, tenant_id, filters)
        if condition is None:
            return 0
        statement = self._base(tenant_id, filters).where(condition)
        return (
            session.scalar(
                select(func.count()).select_from(statement.subquery())
            )
            or 0
        )

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
            "created_at": _iso(view.created_at),
            "updated_at": _iso(view.updated_at),
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


# A job that started before this much time has passed and is still not
# finished has lost its runner. Fifteen minutes is well past any export the
# in-process runner can honestly still be working on.
STALE_EXPORT_AFTER = timedelta(minutes=15)


def _export_is_stale(job) -> bool:
    if job.status not in ("queued", "running") or job.created_at is None:
        return False
    started = job.created_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - started > STALE_EXPORT_AFTER


class PgExportJobs:
    """Export jobs: what was asked for, how it went, where the file is."""

    def __init__(self, url: str | None = None):
        self.url = url

    @staticmethod
    def _to_dict(job: ExportJob) -> dict:
        return {
            "id": job.id,
            "requested_by": job.requested_by,
            # `format` in the API, `fmt` on the row -- `format` is a builtin
            # and a SQLAlchemy attribute name worth not shadowing.
            "format": job.fmt,
            "spec": dict(job.spec or {}),
            "status": job.status,
            "row_count": job.row_count,
            "storage_key": job.storage_key,
            "error": job.error,
            "created_at": _iso(job.created_at),
            # In-process jobs die with the API, and a job whose process is
            # gone stays `running` for ever with nobody left to finish it.
            # Rather than a sweeper, say so on the way out: anything still
            # unfinished after the window is stale, and stale offers a retry.
            "stale": _export_is_stale(job),
            "finished_at": _iso(job.finished_at),
        }

    def create(
        self, tenant_id: str, *, requested_by: str, fmt: str, spec: dict
    ) -> dict:
        with tenant_session(tenant_id, self.url) as session:
            job = ExportJob(
                tenant_id=tenant_id,
                requested_by=requested_by,
                fmt=fmt,
                spec=spec,
                status="queued",
            )
            session.add(job)
            session.flush()
            return self._to_dict(job)

    def get(self, tenant_id: str, job_id: str) -> dict | None:
        with tenant_session(tenant_id, self.url) as session:
            job = session.scalar(
                select(ExportJob).where(
                    ExportJob.tenant_id == tenant_id, ExportJob.id == job_id
                )
            )
            return self._to_dict(job) if job else None

    def list(self, tenant_id: str, limit: int = 25) -> list[dict]:
        with tenant_session(tenant_id, self.url) as session:
            rows = session.scalars(
                select(ExportJob)
                .where(ExportJob.tenant_id == tenant_id)
                .order_by(ExportJob.created_at.desc())
                .limit(limit)
            ).all()
            return [self._to_dict(row) for row in rows]

    def update(self, tenant_id: str, job_id: str, **changes) -> dict:
        with tenant_session(tenant_id, self.url) as session:
            job = session.scalar(
                select(ExportJob).where(
                    ExportJob.tenant_id == tenant_id, ExportJob.id == job_id
                )
            )
            if job is None:
                return {}
            for field, value in changes.items():
                setattr(job, field, value)
            session.flush()
            return self._to_dict(job)
