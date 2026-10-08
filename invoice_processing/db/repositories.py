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
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import and_, delete, exists, func, or_, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from .. import approvals, po_matching
from ..ports import LockHeld, RuleBaseConflict, RunContext
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
    Invoice,
    InvoiceLine,
    PipelineRun,
    RecordLock,
    Rule,
    SheetView,
    Tenant,
    User,
    Vendor,
    VendorBankChange,
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
            "created_at": rule.created_at.isoformat()
            if rule.created_at
            else None,
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
    "vendor": "vendor",
    "approval": "approval",
    "delegation": "delegation",
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
            "expires_at": expires.isoformat() if expires else None,
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
        return value.isoformat() if value else None

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
        return value.isoformat() if value else None

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
            if step is None or step.status != ApprovalStep.PENDING:
                raise StepNotPending(step_id)
            step.escalated_to = to
            step.escalated_at = now
            session.flush()
            return self._step_dict(step)

    def tenant_ids(self) -> list[str]:
        """Every tenant id. The one deliberately cross-tenant read here, like
        `PgDocumentRepository.tenant_id_for_slug`: the SLA sweep has to visit
        each tenant to find its overdue steps, and `tenants` has no RLS."""
        with tenant_session("", self.url) as session:
            return list(session.scalars(select(Tenant.id)))

    def manager_of(self, tenant_id: str, email: str) -> str | None:
        with tenant_session(tenant_id, self.url) as session:
            return session.scalar(
                select(User.manager_email).where(
                    User.tenant_id == tenant_id,
                    func.lower(User.email) == email.strip().casefold(),
                )
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
            "created_at": job.created_at.isoformat()
            if job.created_at
            else None,
            # In-process jobs die with the API, and a job whose process is
            # gone stays `running` for ever with nobody left to finish it.
            # Rather than a sweeper, say so on the way out: anything still
            # unfinished after the window is stale, and stale offers a retry.
            "stale": _export_is_stale(job),
            "finished_at": (
                job.finished_at.isoformat() if job.finished_at else None
            ),
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
