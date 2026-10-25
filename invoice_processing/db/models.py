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
from datetime import UTC, date, datetime
from decimal import Decimal

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
    return datetime.now(UTC)


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
        UniqueConstraint("tenant_id", "uid", name="uq_user_uid"),
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
    # The Identity Platform account this row is linked to. Null until the
    # person has signed in (or an admin pasted it). Unique per tenant so two
    # rows can never claim one account.
    uid: Mapped[str | None] = mapped_column(String(128))
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


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


class SsoDomain(Base, TenantMixin):
    """An email domain whose company accounts may sign in to this tenant.

    `domain` is unique across every tenant: two organisations cannot both
    claim acme.com, and the lookup that happens before the caller's tenant is
    known must resolve to exactly one.
    """

    __tablename__ = "sso_domains"
    __table_args__ = (UniqueConstraint("domain", name="uq_sso_domain"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    domain: Mapped[str] = mapped_column(String(253), nullable=False)
    provider: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # google|microsoft
    default_role: Mapped[str] = mapped_column(String(32), nullable=False)
    # The customer's Entra tenant id, for the record (Microsoft bindings).
    idp_tenant_id: Mapped[str | None] = mapped_column(String(64))
    # The Identity Platform OIDC provider a Microsoft binding trusts
    # (`oidc.<name>`); its issuer is the customer's Entra tenant. Operator-set.
    idp_provider_id: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(320), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now
    )


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
    # The buyer's coding at ordering time. An invoice line paired to this PO
    # line inherits it ahead of the tenant's vendor/keyword rules. Only the
    # ERP upload sets it; a scanned PO leaves it null.
    gl_account: Mapped[str | None] = mapped_column(String(64))
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


# ---------------------------------------------------------------------------
# v2 Trust layer (Wave 0). Time-travel tables are never updated in place: a
# change closes the current row (valid_to) and inserts a new one, so "what did
# we know on date D" is a filter, not a reconstruction.
# ---------------------------------------------------------------------------


def _open(name: str, *cols: str, when: str = "valid_to IS NULL") -> Index:
    """One open row per key; closed rows may repeat the key."""
    return Index(
        name,
        *cols,
        unique=True,
        postgresql_where=text(when),
        sqlite_where=text(when),
    )


class GraphNode(Base, TenantMixin, TimestampMixin):
    """An artefact or party in the Evidence Graph (PRD FR-19.1)."""

    __tablename__ = "graph_nodes"
    __table_args__ = (
        _open("uq_graph_nodes_open", "tenant_id", "kind", "natural_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    natural_key: Mapped[str] = mapped_column(String(512), nullable=False)
    # Backed by an existing row: nothing is copied, only pointed at.
    ref_table: Mapped[str | None] = mapped_column(String(64))
    ref_id: Mapped[str | None] = mapped_column(String(36), index=True)
    attrs: Mapped[dict] = mapped_column(JSONType, default=dict)
    natural_keys: Mapped[dict] = mapped_column(JSONType, default=dict)
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GraphEdge(Base, TenantMixin, TimestampMixin):
    """A link between two nodes, with where it came from and how sure we are."""

    __tablename__ = "graph_edges"
    __table_args__ = (
        _open(
            "uq_graph_edges_open",
            "tenant_id",
            "src",
            "dst",
            "kind",
            "source_system",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    src: Mapped[str] = mapped_column(
        String(36), ForeignKey("graph_nodes.id"), nullable=False, index=True
    )
    dst: Mapped[str] = mapped_column(
        String(36), ForeignKey("graph_nodes.id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # document | system_record | human_assertion | model_inference
    provenance: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    attrs: Mapped[dict] = mapped_column(JSONType, default=dict)
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EntityLink(Base, TenantMixin, TimestampMixin):
    """Two nodes judged to be one entity. Never deletes a node; reversible."""

    __tablename__ = "entity_links"
    __table_args__ = (
        _open(
            "uq_entity_links_open",
            "tenant_id",
            "node_a",
            "node_b",
            when="reversed_at IS NULL",
        ),
    )

    AUTO, PROPOSED, ACCEPTED, REJECTED = (
        "auto",
        "proposed",
        "accepted",
        "rejected",
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    node_a: Mapped[str] = mapped_column(
        String(36), ForeignKey("graph_nodes.id"), nullable=False, index=True
    )
    node_b: Mapped[str] = mapped_column(
        String(36), ForeignKey("graph_nodes.id"), nullable=False, index=True
    )
    score: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    method: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PROPOSED
    )
    decided_by: Mapped[str | None] = mapped_column(String(320))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reversed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    reason: Mapped[str | None] = mapped_column(String(500))


class TrustHistoryImport(Base, TenantMixin, TimestampMixin):
    """One uploaded history file. Re-uploading the same bytes is a no-op."""

    __tablename__ = "trust_history_imports"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "file_sha256", name="uq_trust_history_imports_sha"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    source_name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    filename: Mapped[str | None] = mapped_column(String(255))
    mapping: Mapped[dict] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="complete"
    )
    rows_ok: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_bad: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors: Mapped[list] = mapped_column(JSONType, default=list)
    created_by: Mapped[str | None] = mapped_column(String(320))


class HistoryRecord(Base, TenantMixin, TimestampMixin):
    """One normalised row of imported history."""

    __tablename__ = "history_records"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "import_id",
            "row_number",
            name="uq_history_records_row",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    import_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("trust_history_imports.id"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(128), index=True)
    fields: Mapped[dict] = mapped_column(JSONType, default=dict)
    amount: Mapped[float | None] = mapped_column(Numeric(18, 4))
    currency: Mapped[str | None] = mapped_column(String(8))
    record_date: Mapped[date | None] = mapped_column(Date)
    vendor_key: Mapped[str | None] = mapped_column(String(512), index=True)
    doc_number: Mapped[str | None] = mapped_column(String(128), index=True)


class TrustFinding(Base, TenantMixin, TimestampMixin):
    """A quantified, cited claim that money is being lost (PRD FR-20.7)."""

    __tablename__ = "trust_findings"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "dedupe_key", name="uq_trust_findings_dedupe"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    transaction_node: Mapped[str] = mapped_column(
        String(36), ForeignKey("graph_nodes.id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="detected", index=True
    )
    impact: Mapped[float | None] = mapped_column(Numeric(18, 4))
    currency: Mapped[str | None] = mapped_column(String(8))
    confidence: Mapped[float | None] = mapped_column(Numeric(5, 4))
    owner_id: Mapped[str | None] = mapped_column(String(320))
    engine_version: Mapped[str | None] = mapped_column(String(32))
    rule_version: Mapped[str | None] = mapped_column(String(32))
    model_version: Mapped[str | None] = mapped_column(String(64))
    dedupe_key: Mapped[str] = mapped_column(String(256), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSONType, default=dict)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TrustFindingCitation(Base, TenantMixin):
    """Why we believe a finding: a graph node and an excerpt. At least one each."""

    __tablename__ = "trust_finding_citations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    finding_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("trust_findings.id"), nullable=False, index=True
    )
    node_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("graph_nodes.id"), nullable=False, index=True
    )
    excerpt: Mapped[str | None] = mapped_column(Text)


class TrustFindingEvent(Base, TenantMixin, TimestampMixin):
    """Every status change with the money at that stage: the recovery ledger."""

    __tablename__ = "trust_finding_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    finding_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("trust_findings.id"), nullable=False, index=True
    )
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[float | None] = mapped_column(Numeric(18, 4))
    actor: Mapped[str] = mapped_column(String(320), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500))


class TrustAudit(Base, TenantMixin, TimestampMixin):
    """A Trust Audit engagement: scope, contingency terms, approver (FR-29.1)."""

    __tablename__ = "trust_audits"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="draft"
    )
    period_from: Mapped[date | None] = mapped_column(Date)
    period_to: Mapped[date | None] = mapped_column(Date)
    # Vendor graph node ids; empty means every vendor.
    vendors: Mapped[list] = mapped_column(JSONType, default=list)
    categories: Mapped[list] = mapped_column(JSONType, default=list)
    # Decimal strings: recovered_pct, prevented_pct, cap, minimum.
    terms: Mapped[dict] = mapped_column(JSONType, default=dict)
    approver: Mapped[str | None] = mapped_column(String(320))
    created_by: Mapped[str | None] = mapped_column(String(320))


class TrustAuditRun(Base, TenantMixin, TimestampMixin):
    """One batch run of the engines over the tenant's history (FR-29.2)."""

    __tablename__ = "trust_audit_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    audit_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("trust_audits.id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued"
    )
    engines: Mapped[list] = mapped_column(JSONType, default=list)
    progress: Mapped[dict] = mapped_column(JSONType, default=dict)
    error: Mapped[str | None] = mapped_column(String(500))
    requested_by: Mapped[str | None] = mapped_column(String(320))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


class TrustAuditSample(Base, TenantMixin, TimestampMixin):
    """A random sample of findings a controller validates for precision."""

    __tablename__ = "trust_audit_samples"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    audit_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("trust_audits.id"), nullable=False, index=True
    )
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    seed: Mapped[str | None] = mapped_column(String(32))
    finding_ids: Mapped[list] = mapped_column(JSONType, default=list)
    # finding_id -> true | false | null (not yet validated)
    verdicts: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(320))


class TrustExplanationRating(Base, TenantMixin, TimestampMixin):
    """A reviewer's verdict on an explanation (FR-24.5): one per rater."""

    __tablename__ = "trust_explanation_ratings"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "finding_id",
            "rater",
            name="uq_trust_explanation_ratings_rater",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    finding_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("trust_findings.id"), nullable=False, index=True
    )
    rater: Mapped[str] = mapped_column(String(320), nullable=False)
    correct: Mapped[bool] = mapped_column(Boolean, nullable=False)
    useful: Mapped[bool] = mapped_column(Boolean, nullable=False)
    note: Mapped[str | None] = mapped_column(String(500))


class TrustHold(Base, TenantMixin, TimestampMixin):
    """A payment hold placed or lifted: the ledger behind hold.placed and
    hold.lifted. The gate still decides from the open findings; this records
    when each hold began and ended."""

    __tablename__ = "trust_holds"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    finding_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("trust_findings.id"), nullable=False, index=True
    )
    transaction_node: Mapped[str] = mapped_column(
        String(36), nullable=False, index=True
    )
    engine: Mapped[str] = mapped_column(String(32), nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    placed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    placed_by: Mapped[str | None] = mapped_column(String(320))
    lifted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lifted_by: Mapped[str | None] = mapped_column(String(320))
    lift_reason: Mapped[str | None] = mapped_column(String(500))


class TrustEvidenceRequest(Base, TenantMixin, TimestampMixin):
    """Proof asked of a vendor or an internal owner for a finding (FR-23.5)."""

    __tablename__ = "trust_evidence_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    finding_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("trust_findings.id"), nullable=False, index=True
    )
    owner: Mapped[str] = mapped_column(String(320), nullable=False)
    due_date: Mapped[date | None] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(String(500))
    # open | fulfilled | cancelled
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="open"
    )
    proof_document_id: Mapped[str | None] = mapped_column(String(36))
    requested_by: Mapped[str | None] = mapped_column(String(320))
    fulfilled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


class ApiKey(Base, TenantMixin, TimestampMixin):
    """A machine credential for the /v2 API. Only a hash of the secret is
    kept; the prefix finds the row."""

    __tablename__ = "api_keys"
    __table_args__ = (
        UniqueConstraint("tenant_id", "key_prefix", name="uq_api_keys_prefix"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    key_prefix: Mapped[str] = mapped_column(
        String(12), nullable=False, index=True
    )
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, default="viewer"
    )
    created_by: Mapped[str | None] = mapped_column(String(320))
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WebhookEndpoint(Base, TenantMixin, TimestampMixin):
    """Where a tenant wants trust events sent, and which ones."""

    __tablename__ = "webhook_endpoints"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    # Kept in the clear: it signs every delivery. Masked in every response.
    secret: Mapped[str] = mapped_column(String(128), nullable=False)
    events: Mapped[list] = mapped_column(JSONType, default=list)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    description: Mapped[str | None] = mapped_column(String(256))
    created_by: Mapped[str | None] = mapped_column(String(320))


class WebhookOutbox(Base, TenantMixin, TimestampMixin):
    """One event for one endpoint, written with the change that caused it and
    delivered by the worker's sweep."""

    __tablename__ = "webhook_outbox"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    endpoint_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("webhook_endpoints.id"),
        nullable=False,
        index=True,
    )
    event: Mapped[str] = mapped_column(String(48), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, index=True
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_status: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(String(500))
    dead_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Agent(Base, TenantMixin, TimestampMixin):
    """A machine actor a tenant has registered for agent control (FR-28.1).
    `principal_id` is the Principal's `user_id` it acts as; an external agent
    (seen only through imported logs) has none."""

    __tablename__ = "agents"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "principal_id", name="uq_agents_principal"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    owner: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    principal_id: Mapped[str | None] = mapped_column(String(64))
    principal_email: Mapped[str | None] = mapped_column(String(320), index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active"
    )
    registered_by: Mapped[str | None] = mapped_column(String(320))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[str | None] = mapped_column(String(320))
    revoke_reason: Mapped[str | None] = mapped_column(String(500))
    graph_node: Mapped[str | None] = mapped_column(String(36))


class AgentMandate(Base, TenantMixin, TimestampMixin):
    """What a person authorised an agent to do, when, and for how much
    (FR-28.5). Versioned; each version carries the previous one's digest."""

    __tablename__ = "agent_mandates"
    __table_args__ = (
        UniqueConstraint(
            "agent_id", "version", name="uq_agent_mandates_version"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    scope: Mapped[dict] = mapped_column(JSONType, default=dict)
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    valid_to: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="proposed"
    )
    proposed_by: Mapped[str] = mapped_column(String(320), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(320))
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    revoked_by: Mapped[str | None] = mapped_column(String(320))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(String(500))
    prev_digest: Mapped[str | None] = mapped_column(String(64))
    digest: Mapped[str | None] = mapped_column(String(64))
    signature: Mapped[str | None] = mapped_column(String(64))


class AgentAction(Base, TenantMixin, TimestampMixin):
    """One evaluated agent action and the decision on it: the decision
    ledger, written before the governed write (or from an imported log)."""

    __tablename__ = "agent_actions"
    __table_args__ = (
        Index(
            "ix_agent_actions_agent_time",
            "tenant_id",
            "agent_id",
            "occurred_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id"), nullable=False, index=True
    )
    mandate_id: Mapped[str | None] = mapped_column(String(36))
    mandate_version: Mapped[int | None] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_table: Mapped[str | None] = mapped_column(String(32))
    subject_ref: Mapped[str | None] = mapped_column(String(128), index=True)
    transaction_node: Mapped[str | None] = mapped_column(String(36))
    vendor_key: Mapped[str | None] = mapped_column(String(128), index=True)
    category: Mapped[str | None] = mapped_column(String(64))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    currency: Mapped[str | None] = mapped_column(String(8))
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    reasons: Mapped[list] = mapped_column(JSONType, default=list)
    explanation: Mapped[str | None] = mapped_column(String(1000))
    mode: Mapped[str | None] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(
        String(8), nullable=False, default="inline"
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finding_id: Mapped[str | None] = mapped_column(String(36))
    request_id: Mapped[str | None] = mapped_column(String(64))
    # executed | failed, stamped by the call site after its write; unstamped
    # (a crash, an imported log) still counts toward spend.
    outcome: Mapped[str | None] = mapped_column(String(16))


class ApiRateWindow(Base, TenantMixin, TimestampMixin):
    """How many `/v2` requests one caller made in one minute (rate limit)."""

    __tablename__ = "api_rate_windows"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "principal", "window_start", name="uq_api_rate_window"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    principal: Mapped[str] = mapped_column(String(128), nullable=False)
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ApiIdempotency(Base, TenantMixin, TimestampMixin):
    """A `/v2` POST sent with an `Idempotency-Key`: its body's hash and, once
    it finished, the answer a retry gets back. Kept 24 hours."""

    __tablename__ = "api_idempotency"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "principal", "key", name="uq_api_idempotency_key"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    principal: Mapped[str] = mapped_column(String(128), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="in_progress"
    )
    status_code: Mapped[int | None] = mapped_column(Integer)
    body: Mapped[dict | list | None] = mapped_column(JSONType)
    content_type: Mapped[str | None] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


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
    "sso_domains",
    "graph_nodes",
    "graph_edges",
    "entity_links",
    "trust_history_imports",
    "history_records",
    "trust_findings",
    "trust_finding_citations",
    "trust_finding_events",
    "trust_audits",
    "trust_audit_runs",
    "trust_audit_samples",
    "trust_explanation_ratings",
    "trust_holds",
    "trust_evidence_requests",
    "api_keys",
    "webhook_endpoints",
    "webhook_outbox",
    "agents",
    "agent_mandates",
    "agent_actions",
    "api_rate_windows",
    "api_idempotency",
)
