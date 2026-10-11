"""Multi-tenant schema for the pilot.

Scope is deliberately the subset the pilot needs -- tenants, users, documents,
invoices, lines, findings, pipeline runs, rules, corrections and audit events.
Purchase orders, goods receipts, payments and tax reconciliation tables come
with the features that use them; inventing them now would be schema nobody
validates against real data.

Two rules hold everywhere:

* **Every tenant-owned row carries `tenant_id`**, and every query filters on it.
  Postgres row-level security enforces the same thing underneath (see the
  migration). That is deliberate belt-and-braces: an application bug still
  cannot read across tenants, and a misconfigured policy still cannot either.
* **Money is `Numeric`, never float.** A float total is a rounding error waiting
  to be argued about with a supplier.

`corrections` is the table that compounds: every reviewer edit is the training
signal for accuracy measurement and the raw material for ALF rules.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

# Postgres gets JSONB; SQLite (tests) falls back to JSON.
try:
    from sqlalchemy.dialects.postgresql import JSONB

    JSONType = JSON().with_variant(JSONB, "postgresql")
except ImportError:  # pragma: no cover
    JSONType = JSON


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class TenantMixin:
    """Every tenant-owned table carries this. RLS keys off it."""

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id"), nullable=False, index=True
    )


# ---------------------------------------------------------------------------
# Tenancy and identity
# ---------------------------------------------------------------------------


class Tenant(Base, TimestampMixin):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Immutable after creation: data residency is a promise, not a setting.
    region: Mapped[str] = mapped_column(
        String(32), nullable=False, default="au"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active"
    )
    settings: Mapped[dict] = mapped_column(JSONType, default=dict)


class User(Base, TenantMixin, TimestampMixin):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_user_email"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(
        String(32), nullable=False, default="clerk"
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Where an overdue approval step escalates to; unset falls back to an admin.
    manager_email: Mapped[str | None] = mapped_column(String(320))


# ---------------------------------------------------------------------------
# Documents and invoices
# ---------------------------------------------------------------------------


class Document(Base, TenantMixin, TimestampMixin):
    """A file as received. Immutable evidence."""

    __tablename__ = "documents"
    __table_args__ = (
        # File-level duplicate detection: the same bytes twice is never
        # re-processed, and the check does not depend on extraction.
        Index("ix_documents_tenant_sha", "tenant_id", "sha256"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    filename: Mapped[str | None] = mapped_column(String(512))
    mime_type: Mapped[str | None] = mapped_column(String(128))
    byte_size: Mapped[int | None] = mapped_column(Integer)
    page_count: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(
        String(32), default="upload"
    )  # upload|email|api
    source_ref: Mapped[str | None] = mapped_column(
        String(1024)
    )  # .eml key, message id
    # Who the email said it came from, for the sender-domain BEC signal. Both
    # kept: Reply-To is where a reply would go, which is what a fraudster
    # redirects; From is what the reviewer sees.
    sender: Mapped[str | None] = mapped_column(String(320))
    reply_to: Mapped[str | None] = mapped_column(String(320))
    malware_status: Mapped[str] = mapped_column(String(32), default="pending")
    # NULL is an invoice; purchase_order / goods_receipt are scanned supporting
    # documents (see SupportingDocument).
    kind: Mapped[str | None] = mapped_column(String(16))

    invoices: Mapped[list[Invoice]] = relationship(back_populates="document")


class Invoice(Base, TenantMixin, TimestampMixin):
    __tablename__ = "invoices"
    __table_args__ = (
        # Duplicate detection's primary index. Not unique: a genuine duplicate
        # must be storable and *flagged*, not rejected by the database, or the
        # reviewer never sees what happened.
        Index(
            "ix_invoices_dupe", "tenant_id", "vendor_tax_id", "invoice_number"
        ),
        Index("ix_invoices_status", "tenant_id", "status"),
        # "Everything a reviewer overruled" is the learning loop's one query.
        Index("ix_invoices_agreement", "tenant_id", "decision_agreement"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    document_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("documents.id")
    )
    case_id: Mapped[str | None] = mapped_column(String(128), index=True)

    invoice_number: Mapped[str | None] = mapped_column(String(128))
    invoice_date: Mapped[str | None] = mapped_column(String(32))
    due_date: Mapped[str | None] = mapped_column(String(32))
    po_number: Mapped[str | None] = mapped_column(String(128))
    payment_terms: Mapped[str | None] = mapped_column(String(255))
    currency: Mapped[str | None] = mapped_column(String(8))

    vendor_name: Mapped[str | None] = mapped_column(String(512))
    vendor_tax_id: Mapped[str | None] = mapped_column(String(64))
    vendor_address: Mapped[str | None] = mapped_column(Text)

    # Money: fixed precision, never float.
    total_ex_tax: Mapped[float | None] = mapped_column(Numeric(18, 4))
    tax_amount: Mapped[float | None] = mapped_column(Numeric(18, 4))
    total_inc_tax: Mapped[float | None] = mapped_column(Numeric(18, 4))

    # Bank details as printed on the invoice. Never the vendor's authoritative
    # account -- a mismatch against the vendor master is the fraud signal.
    bank_account_name: Mapped[str | None] = mapped_column(String(255))
    bank_account_number: Mapped[str | None] = mapped_column(String(64))
    bank_code: Mapped[str | None] = mapped_column(String(64))

    status: Mapped[str] = mapped_column(
        String(32), default="received", nullable=False
    )
    decision: Mapped[str | None] = mapped_column(String(32))
    decision_reason: Mapped[str | None] = mapped_column(Text)

    # What the person decided, and whether that agreed with the machine.
    # Columns rather than a JSON path because "show me everything a reviewer
    # overruled" is the question the learning loop is built on, and it has to
    # be answerable in one indexed query rather than a scan of every row.
    #   agreement: agreed | overruled_reject | overruled_accept | unknown
    human_decision: Mapped[str | None] = mapped_column(String(16))
    decision_agreement: Mapped[str | None] = mapped_column(String(24))
    decision_category: Mapped[str | None] = mapped_column(String(32))
    routing: Mapped[str | None] = mapped_column(String(16))  # AUTO | REVIEW
    min_confidence: Mapped[float | None] = mapped_column(Numeric(4, 3))

    # Per-field values with evidence, as produced by the confidence module.
    fields: Mapped[dict] = mapped_column(JSONType, default=dict)

    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the invoice stopped needing anyone: a person's decision time from
    # the workflow artifact, or the projection time for a touchless invoice.
    # Null while someone still has to act.
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    external_id: Mapped[str | None] = mapped_column(String(128))  # ERP bill id

    document: Mapped[Document | None] = relationship(back_populates="invoices")
    lines: Mapped[list[InvoiceLine]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan"
    )
    findings: Mapped[list[Finding]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan"
    )


class InvoiceLine(Base, TenantMixin, TimestampMixin):
    __tablename__ = "invoice_lines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    invoice_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("invoices.id"), nullable=False, index=True
    )
    line_number: Mapped[int | None] = mapped_column(Integer)
    description: Mapped[str | None] = mapped_column(Text)
    item_code: Mapped[str | None] = mapped_column(String(64))
    quantity: Mapped[float | None] = mapped_column(Numeric(18, 4))
    unit_price: Mapped[float | None] = mapped_column(Numeric(18, 4))
    amount_ex_tax: Mapped[float | None] = mapped_column(Numeric(18, 4))
    tax_amount: Mapped[float | None] = mapped_column(Numeric(18, 4))
    tax_code: Mapped[str | None] = mapped_column(String(32))
    gl_account: Mapped[str | None] = mapped_column(String(64))

    invoice: Mapped[Invoice] = relationship(back_populates="lines")


class Finding(Base, TenantMixin, TimestampMixin):
    """One validation outcome. This is the explainability record."""

    __tablename__ = "findings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    invoice_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("invoices.id"), nullable=False, index=True
    )
    phase: Mapped[str | None] = mapped_column(String(32))
    step: Mapped[str | None] = mapped_column(String(16))
    rule_ref: Mapped[str | None] = mapped_column(
        String(128)
    )  # rules book clause
    severity: Mapped[str | None] = mapped_column(String(16))
    passed: Mapped[bool | None] = mapped_column(Boolean)
    evidence: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="open")
    override_reason: Mapped[str | None] = mapped_column(Text)
    overridden_by: Mapped[str | None] = mapped_column(String(36))

    invoice: Mapped[Invoice] = relationship(back_populates="findings")


class PipelineRun(Base, TenantMixin, TimestampMixin):
    """Everything needed to re-execute and defend a decision."""

    __tablename__ = "pipeline_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    invoice_id: Mapped[str | None] = mapped_column(String(36), index=True)
    pipeline_version: Mapped[str | None] = mapped_column(String(32))
    model_ids: Mapped[dict] = mapped_column(JSONType, default=dict)
    master_data_version: Mapped[str | None] = mapped_column(String(32))
    rules_book_version: Mapped[str | None] = mapped_column(String(32))
    rule_base_version: Mapped[str | None] = mapped_column(String(64))
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    critic_verdict: Mapped[str | None] = mapped_column(String(32))


# ---------------------------------------------------------------------------
# Learning
# ---------------------------------------------------------------------------


class Rule(Base, TenantMixin, TimestampMixin):
    """An ALF correction rule. Per-tenant, versioned, SoD-enforced."""

    __tablename__ = "rules"
    __table_args__ = (
        UniqueConstraint("tenant_id", "rule_id", name="uq_rule_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    rule_id: Mapped[str] = mapped_column(String(32), nullable=False)  # ALF-001
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(String(128), default="global")
    priority: Mapped[int] = mapped_column(Integer, default=100)
    status: Mapped[str] = mapped_column(String(32), default="proposed")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    conditions: Mapped[list] = mapped_column(JSONType, default=list)
    actions: Mapped[list] = mapped_column(JSONType, default=list)
    rule_metadata: Mapped[dict] = mapped_column(JSONType, default=dict)

    # Segregation of duties: the author must not be the approver.
    author_id: Mapped[str | None] = mapped_column(String(36))
    approver_id: Mapped[str | None] = mapped_column(String(36))
    shadow_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


class Correction(Base, TenantMixin, TimestampMixin):
    """A reviewer's edit. The asset that compounds.

    Every correction is both an accuracy measurement and a candidate rule.
    """

    __tablename__ = "corrections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    invoice_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("invoices.id"), nullable=False, index=True
    )
    field_path: Mapped[str] = mapped_column(String(255), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[str | None] = mapped_column(String(36))
    source: Mapped[str] = mapped_column(String(32), default="review")


class Vendor(Base, TenantMixin, TimestampMixin):
    """A supplier, with the bank details the tenant has verified for it.

    The bank fields are written by exactly one path: approving a
    `VendorBankChange`. The bulk import never touches them, so the only way a
    payee account enters the system is through two people.
    """

    __tablename__ = "vendors"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "normalized_name", name="uq_vendors_name"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(512), nullable=False)
    tax_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default="")
    email_domain: Mapped[str | None] = mapped_column(String(255))
    bank_account_name: Mapped[str | None] = mapped_column(String(255))
    bank_account_number: Mapped[str | None] = mapped_column(String(64))
    bank_code: Mapped[str | None] = mapped_column(String(64))
    bank_iban: Mapped[str | None] = mapped_column(String(64))
    bank_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    bank_verified_by: Mapped[str | None] = mapped_column(String(320))


class VendorBankChange(Base, TenantMixin, TimestampMixin):
    """A proposed change to a vendor's payee details, awaiting a second person.

    Segregation of duties is enforced in the repository (`decided_by` must
    differ from `proposed_by`), not merely recorded as it is for rules.
    """

    __tablename__ = "vendor_bank_changes"
    # The database, not a Python pre-check, is what stops two containers both
    # proposing a change for one vendor.
    __table_args__ = (
        Index(
            "uq_vendor_bank_changes_pending",
            "vendor_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
    )

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    vendor_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("vendors.id"), nullable=False, index=True
    )
    proposed_account_name: Mapped[str | None] = mapped_column(String(255))
    proposed_account_number: Mapped[str | None] = mapped_column(String(64))
    proposed_code: Mapped[str | None] = mapped_column(String(64))
    proposed_iban: Mapped[str | None] = mapped_column(String(64))
    previous_account_name: Mapped[str | None] = mapped_column(String(255))
    previous_account_number: Mapped[str | None] = mapped_column(String(64))
    previous_code: Mapped[str | None] = mapped_column(String(64))
    previous_iban: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    source_case_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default=PENDING, index=True)
    proposed_by: Mapped[str] = mapped_column(String(320), nullable=False)
    decided_by: Mapped[str | None] = mapped_column(String(320))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)


class AuditEvent(Base, TenantMixin):
    """Append-only. No update or delete grant exists for this table."""

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_tenant_time", "tenant_id", "occurred_at"),
        Index("ix_audit_entity", "tenant_id", "entity_type", "entity_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    actor: Mapped[str | None] = mapped_column(String(320))
    actor_type: Mapped[str] = mapped_column(String(32), default="user")
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(64))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    before: Mapped[dict | None] = mapped_column(JSONType)
    after: Mapped[dict | None] = mapped_column(JSONType)
    request_id: Mapped[str | None] = mapped_column(String(64))
    ip: Mapped[str | None] = mapped_column(String(64))


# Tables carrying tenant-owned data. The migration enables RLS on each.
class DocumentClaim(Base, TenantMixin, TimestampMixin):
    """Who is processing a document, where every worker can see it.

    The claim was a file created with `O_EXCL` under the data directory. That
    is a correct mutual exclusion on one machine and none at all across two:
    each container has its own disk, so two workers could both win the same
    claim and run the same extraction side by side -- which is exactly the
    duplicate the whole idempotency design exists to prevent.

    The unique constraint is the lock. `INSERT ... ON CONFLICT DO NOTHING`
    either inserts a row or does not, atomically, for every process at once.
    """

    __tablename__ = "document_claims"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "document_id", name="uq_document_claims_document"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    document_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # running | done | failed. The worker reads this to tell a redelivery of
    # finished work from work someone else is doing.
    state: Mapped[str] = mapped_column(
        String(16), default="running", nullable=False
    )
    worker_id: Mapped[str | None] = mapped_column(String(128))


class WorkerHeartbeat(Base):
    """Liveness, where every container can see it.

    This was a file under the data directory, which works with one process and
    not at all with two: the worker wrote it to its own disk and the API, in a
    separate container, reported that no worker had ever run while invoices
    were being processed a few seconds earlier.

    Deliberately *not* a tenant table. A worker serves every tenant, so scoping
    its liveness to one would make the banner wrong for all the others, and
    row-level security would hide the row from the very query that needs it.
    """

    __tablename__ = "worker_heartbeats"

    # One row per worker process, so two workers do not overwrite each other
    # and the newest beat still answers "is anything alive".
    worker_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    pid: Mapped[int | None] = mapped_column(Integer)


class SheetView(Base, TenantMixin, TimestampMixin):
    """A saved set of filters, sort and columns for the approved-invoice sheet.

    Per user, not per tenant: two people reviewing the same queue want
    different lenses on it, and a shared-by-default view is one person's
    preference imposed on everyone. `shared` opts a view into being visible to
    the whole tenant, which is a deliberate act and needs approver rank.

    The definition is a JSON blob rather than columns because it is UI state --
    which filters, which columns, how wide. Giving it a schema here would mean
    a migration every time the grid gains a control.
    """

    __tablename__ = "sheet_views"
    __table_args__ = (
        Index("ix_sheet_views_tenant_owner", "tenant_id", "owner"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # The email of whoever created it, matching how the audit trail and the
    # corrections store name an actor.
    owner: Mapped[str] = mapped_column(String(320), nullable=False)
    shared: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    definition: Mapped[dict] = mapped_column(JSONType, default=dict)


class ExportJob(Base, TenantMixin, TimestampMixin):
    """One request to write approved invoices out as a file.

    A row rather than a background task alone, because the user has to be able
    to come back to it: an export of ten thousand invoices outlives the request
    that asked for it, and "where did my download go" must have an answer.
    """

    __tablename__ = "export_jobs"
    __table_args__ = (
        Index("ix_export_jobs_tenant_time", "tenant_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    requested_by: Mapped[str | None] = mapped_column(String(320))
    fmt: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # csv|xlsx|json
    # The filters, columns and row selection the export was asked for, kept so
    # a finished file can say what it contains.
    spec: Mapped[dict] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(
        String(16), default="queued", nullable=False
    )  # queued|running|done|failed
    row_count: Mapped[int | None] = mapped_column(Integer)
    storage_key: Mapped[str | None] = mapped_column(String(1024))
    error: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


class RecordLock(Base, TenantMixin, TimestampMixin):
    """Who is editing one invoice, so two people cannot fix it at once.

    Second person wins under last-write-wins: two reviewers correcting the same
    invoice each read the extraction artifact, each write it back whole, and
    the one who saves second silently discards the other's corrections. The
    corrections store still holds both, which makes the trail say two people
    fixed a field and the invoice show only one of them.

    Same shape as `DocumentClaim`: the unique constraint is the lock, so
    `INSERT ... ON CONFLICT DO NOTHING` picks one winner for every process at
    once. Unlike a claim it carries an expiry -- a reviewer who closes the tab
    must not strand the record, and there is nobody to notice that they did.
    """

    __tablename__ = "record_locks"
    __table_args__ = (
        UniqueConstraint("tenant_id", "case_id", name="uq_record_locks_case"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    case_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # The authenticated caller's email, not a self-reported name: this appears
    # in a refusal shown to someone else.
    holder: Mapped[str] = mapped_column(String(320), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ApproverGroup(Base, TenantMixin, TimestampMixin):
    """A named set of people any one of whom satisfies a step."""

    __tablename__ = "approver_groups"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_approver_groups_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # ponytail: a JSON list of emails, filtered in Python. A tenant has a
    # handful of groups; a members table when "which groups is X in" needs SQL.
    members: Mapped[list] = mapped_column(JSONType, default=list)


class ApprovalRule(Base, TenantMixin, TimestampMixin):
    """Who must approve, keyed by vendor, entity or amount tier."""

    __tablename__ = "approval_rules"

    VENDOR, ENTITY, AMOUNT = "vendor", "entity", "amount"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    match: Mapped[dict] = mapped_column(JSONType, default=dict)
    steps: Mapped[list] = mapped_column(JSONType, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Delegation(Base, TenantMixin, TimestampMixin):
    """Out of office: the delegate may decide anything routed to the delegator
    while the range is active."""

    __tablename__ = "delegations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    delegator_email: Mapped[str] = mapped_column(
        String(320), nullable=False, index=True
    )
    delegate_email: Mapped[str] = mapped_column(
        String(320), nullable=False, index=True
    )
    starts_on: Mapped[date] = mapped_column(Date, nullable=False)
    ends_on: Mapped[date] = mapped_column(Date, nullable=False)
    created_by: Mapped[str] = mapped_column(String(320), nullable=False)


class ApprovalStep(Base, TenantMixin, TimestampMixin):
    """One link of an invoice's approval chain. The lowest pending position is
    the current step; nothing is ever decided out of order."""

    __tablename__ = "approval_steps"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "case_id",
            "position",
            name="uq_approval_steps_position",
        ),
        Index("ix_approval_steps_pending", "tenant_id", "status", "due_at"),
    )

    PENDING, APPROVED, REJECTED, CANCELLED = (
        "pending",
        "approved",
        "rejected",
        "cancelled",
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    case_id: Mapped[str] = mapped_column(
        String(128), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    source_rule_id: Mapped[str | None] = mapped_column(String(36))
    assignee_kind: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # user|group|role
    assignee_value: Mapped[str] = mapped_column(String(320), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PENDING
    )
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(String(320))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acting_for: Mapped[str | None] = mapped_column(String(320))
    override_reason: Mapped[str | None] = mapped_column(Text)
    # "role:admin" or an email
    escalated_to: Mapped[str | None] = mapped_column(String(320))
    escalated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    note: Mapped[str | None] = mapped_column(Text)


class GlRule(Base, TenantMixin, TimestampMixin):
    """Codes an invoice line to a GL account by vendor and/or keyword."""

    __tablename__ = "gl_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    vendor_name: Mapped[str | None] = mapped_column(String(255))
    keyword: Mapped[str | None] = mapped_column(String(200))
    gl_account: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String(320), nullable=False)


class PurchaseOrder(Base, TenantMixin, TimestampMixin):
    """A purchase order as uploaded from the ERP; lines in PoLine.

    po_number is stored normalised (po_matching.normalize_po_number) so the
    match and the upsert agree on identity; po_number_display is what the
    ERP printed, for people.
    """

    __tablename__ = "purchase_orders"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "po_number", name="uq_purchase_orders_number"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    po_number: Mapped[str] = mapped_column(String(128), nullable=False)
    po_number_display: Mapped[str] = mapped_column(String(128), nullable=False)
    vendor_name: Mapped[str] = mapped_column(String(512), nullable=False)
    total_amount: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="AUD"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="open"
    )
    amount_invoiced: Mapped[float] = mapped_column(
        Numeric(18, 4), nullable=False, default=0
    )
    customer_name: Mapped[str | None] = mapped_column(String(512))
    valid_from: Mapped[str | None] = mapped_column(String(10))
    valid_to: Mapped[str | None] = mapped_column(String(10))
    amount_basis: Mapped[str] = mapped_column(
        String(8), nullable=False, default="inc_tax"
    )
    approved_quantity: Mapped[float | None] = mapped_column(Numeric(18, 4))
    approved_unit_price: Mapped[float | None] = mapped_column(Numeric(18, 4))
    hsn_sac: Mapped[str | None] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="upload"
    )
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    uploaded_by: Mapped[str] = mapped_column(String(320), nullable=False)
    document_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("documents.id")
    )

    lines: Mapped[list[PoLine]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="PoLine.line_number",
    )


class PoLine(Base, TenantMixin):
    __tablename__ = "po_lines"
    __table_args__ = (
        UniqueConstraint(
            "purchase_order_id", "line_number", name="uq_po_lines_number"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    purchase_order_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("purchase_orders.id"), nullable=False, index=True
    )
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    item_code: Mapped[str | None] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    qty_ordered: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    unit_price: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    uom: Mapped[str | None] = mapped_column(String(16))
    receipt_required: Mapped[bool | None] = mapped_column(Boolean)
    qty_invoiced_opening: Mapped[float] = mapped_column(
        Numeric(18, 4), nullable=False, default=0
    )

    order: Mapped[PurchaseOrder] = relationship(back_populates="lines")


class GoodsReceipt(Base, TenantMixin):
    """Evidence that goods arrived. Immutable: a correction is a new receipt
    with negative quantities and a reason."""

    __tablename__ = "goods_receipts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "grn_number", name="uq_goods_receipts_number"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    grn_number: Mapped[str] = mapped_column(String(128), nullable=False)
    purchase_order_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("purchase_orders.id"), nullable=False, index=True
    )
    received_on: Mapped[str] = mapped_column(String(10), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500))
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="upload"
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    recorded_by: Mapped[str] = mapped_column(String(320), nullable=False)
    document_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("documents.id")
    )

    lines: Mapped[list[GrnLine]] = relationship(cascade="all, delete-orphan")


class GrnLine(Base, TenantMixin):
    __tablename__ = "grn_lines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    goods_receipt_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("goods_receipts.id"), nullable=False, index=True
    )
    po_line_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("po_lines.id"), nullable=False, index=True
    )
    qty_received: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)


class SupportingDocument(Base, TenantMixin, TimestampMixin):
    """A scanned PO or GRN awaiting a person's confirmation. Nothing here
    feeds the match until status is accepted and a real order/receipt exists."""

    __tablename__ = "supporting_documents"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "document_id", name="uq_supporting_documents_document"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("documents.id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # purchase_order | goods_receipt
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )  # pending | blocked | accepted | rejected
    extracted: Mapped[dict] = mapped_column(
        JSONType, nullable=False, default=dict
    )
    po_number: Mapped[str | None] = mapped_column(
        String(128), index=True
    )  # normalised
    blocked_reason: Mapped[str | None] = mapped_column(String(500))
    extraction_error: Mapped[str | None] = mapped_column(String(500))
    reviewed_by: Mapped[str | None] = mapped_column(String(320))
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    note: Mapped[str | None] = mapped_column(String(500))


TENANT_TABLES = (
    "users",
    "record_locks",
    "document_claims",
    "documents",
    "invoices",
    "invoice_lines",
    "findings",
    "pipeline_runs",
    "rules",
    "corrections",
    "audit_events",
    "sheet_views",
    "export_jobs",
    "vendors",
    "vendor_bank_changes",
    "approver_groups",
    "approval_rules",
    "delegations",
    "approval_steps",
    "gl_rules",
    "purchase_orders",
    "po_lines",
    "goods_receipts",
    "grn_lines",
    "supporting_documents",
)
