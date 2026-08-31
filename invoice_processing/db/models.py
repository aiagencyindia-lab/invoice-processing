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
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
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
    malware_status: Mapped[str] = mapped_column(String(32), default="pending")

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
    routing: Mapped[str | None] = mapped_column(String(16))  # AUTO | REVIEW
    min_confidence: Mapped[float | None] = mapped_column(Numeric(4, 3))

    # Per-field values with evidence, as produced by the confidence module.
    fields: Mapped[dict] = mapped_column(JSONType, default=dict)

    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
TENANT_TABLES = (
    "users",
    "documents",
    "invoices",
    "invoice_lines",
    "findings",
    "pipeline_runs",
    "rules",
    "corrections",
    "audit_events",
)
