# Approval Workflows Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route every invoice a person must touch to the right approver(s) by vendor, entity and amount tier, with segregation of duties, dated delegation, SLA escalation and a phone-sized approvals page, while AUTO invoices stay touchless and export/sheet/learning keep reading the same workflow keys.

**Architecture:** Four tenant tables (`approver_groups`, `approval_rules`, `delegations`, `approval_steps`) plus `users.manager_email`, behind `PgApprovalRepository`. A pure `approvals.py` builds a sequential chain and answers "may this person decide this step". The worker routes after projection and runs an overdue scan; the existing actions endpoint decides the caller's current step and, on the last one, writes today's workflow keys. `approval_routes.py` exposes queue, chain and configuration. Without a database everything falls back to today's single-approver behaviour.

**Tech Stack:** Python 3.12 (`./.venv/bin/python`), FastAPI, SQLAlchemy 2 + Alembic (Postgres with RLS; SQLite locally/tests), pytest, ruff; Next.js 16 / React 19 / Playwright in `frontend/`.

**Spec:** `docs/superpowers/specs/2026-10-08-approval-workflows-design.md` (commit `88b3e93`). Read it first.

**Plan on disk (Task 0 copies this file there):** `docs/superpowers/plans/2026-10-08-approval-workflows.md`

## Global Constraints

- Always `./.venv/bin/python`. Tests: `./.venv/bin/python -m pytest <path> -q -p no:cacheprovider`. Never pipe pytest into `tail` when the exit code matters.
- ruff clean after every task: `./.venv/bin/python -m ruff check . && ./.venv/bin/python -m ruff format .`
- TDD: a test that fails without the change, for every behaviour.
- Comments explain *why*; deliberate simplifications marked `# ponytail: <ceiling + upgrade path>`.
- No endpoint takes `tenant_id`; every new route gets a probe + `PROBED` entry in `tests/test_tenant_isolation_api.py` (guard test fails otherwise). Roles via `require_role`; actors are `principal.email`.
- AUTO invoices with no matching amount tier get **no steps**. Chain completion writes exactly today's keys: `decision`, `decided_by`, `decided_at`, `reason`, `reason_category`.
- Nothing is ever auto-approved. Refusals are 403 with a sentence naming the rule; admin override needs a reason and is audited.
- DB fixtures follow `tests/test_db_repositories.py`: `Base.metadata.drop_all(engine); create_all(url)`; tenants added inside `tenant_session(tenant_id, url)`. New DB test files go on the CI Postgres job list in `.github/workflows/ci.yml` (~:157).
- Commit locally after each task, never push. Trailer: the model that wrote the commit (`Co-Authored-By: Claude <model> <noreply@anthropic.com>`).
- Production is down (closed billing). Local only; never touch GCP, Cloud SQL, GCS, Railway.

## File structure

| File | Responsibility |
|---|---|
| `invoice_processing/db/models.py` | `ApproverGroup`, `ApprovalRule`, `Delegation`, `ApprovalStep`; `User.manager_email`; `TENANT_TABLES` |
| `invoice_processing/db/migrations/versions/f2c7a19d8e43_approval_workflows.py` | tables + RLS + users column |
| `invoice_processing/db/repositories.py` | `PgApprovalRepository` + exceptions; `_ENTITY_TYPES` entries; `PgCorrectionStore.actors_for(case_id)` helper if absent |
| `invoice_processing/approvals.py` (new) | pure engine: `needs_person`, `build_chain`, `satisfaction`, `refusal` |
| `invoice_processing/tenant_settings.py` | `approval_sla_days` |
| `invoice_processing/api/service.py` | `get_approvals()` accessor; `act` decides a step; `edit_fields` re-routes; include router |
| `invoice_processing/api/approval_routes.py` (new) | mine, chain, groups/rules/delegations CRUD |
| `invoice_processing/api/admin_routes.py` | `PATCH /api/admin/users/{user_id}` (manager) |
| `invoice_processing/worker/runner.py` | route after projection; overdue scan in the loop |
| `frontend/src/lib/api.ts`, `components/AppShell.tsx`, `app/approvals/page.tsx` (new), `app/invoices/page.tsx`, `app/admin/page.tsx`, `app/admin/approvals/page.tsx` (new), `e2e/golden-path.spec.ts` | UI |
| tests: `tests/test_approvals.py`, `tests/test_approval_repository.py`, `tests/test_approval_routes.py` (new); `tests/test_tenant_isolation_api.py`, `tests/test_worker.py`, `tests/test_tenant_settings.py`, `tests/test_gcs_and_migrations.py` | |

---

## Task 0: Save this plan into the repo

- [ ] Copy this file to `docs/superpowers/plans/2026-10-08-approval-workflows.md` and commit: `Plan the approval-workflows implementation`.

---

## Task 1: Models, migration, setting

**Files:**
- Modify: `invoice_processing/db/models.py` (`User` ~:101-113; new models after `VendorBankChange`; `TENANT_TABLES`)
- Create: `invoice_processing/db/migrations/versions/f2c7a19d8e43_approval_workflows.py` (`down_revision = "e7a2c9d41b56"`; template: that file)
- Modify: `invoice_processing/tenant_settings.py` (`SETTABLE`, `NON_ENGINE_DEFAULTS`, `DESCRIPTIONS`, non-negative validation group)
- Test: `tests/test_approval_repository.py` (new, schema class), `tests/test_tenant_settings.py`

**Produces:** ORM `ApproverGroup(id, tenant_id, name, members: JSON list)`, `ApprovalRule(id, tenant_id, name, kind, match: JSON, steps: JSON, enabled)`, `Delegation(id, tenant_id, delegator_email, delegate_email, starts_on: Date, ends_on: Date, created_by)`, `ApprovalStep(id, tenant_id, case_id, position: int, source_rule_id, assignee_kind, assignee_value, status, due_at, decided_by, acting_for, override_reason, escalated_to, escalated_at, note)` with constants `PENDING/APPROVED/REJECTED/CANCELLED`; `User.manager_email: str | None`; setting `approval_sla_days` default 3.

- [ ] **Step 1: Failing tests**

```python
# tests/test_approval_repository.py
"""Approval chains, groups, rules and delegations live in the database: the
API and the worker are separate containers, and "what is waiting for me" and
"what is overdue" are queries, not scans of every case."""

import pytest
from sqlalchemy import inspect

from invoice_processing.db import models
from invoice_processing.db.session import create_all


class TestSchema:
    def test_tables_are_tenant_tables(self):
        for name in ("approver_groups", "approval_rules", "delegations", "approval_steps"):
            assert name in models.TENANT_TABLES

    def test_create_all_builds_them(self, scratch_db):
        inspector = inspect(create_all(scratch_db("schema.db")))
        for name in ("approver_groups", "approval_rules", "delegations", "approval_steps"):
            assert inspector.has_table(name)
        assert "manager_email" in {c["name"] for c in inspector.get_columns("users")}
        step_cols = {c["name"] for c in inspector.get_columns("approval_steps")}
        assert {"case_id", "position", "assignee_kind", "assignee_value", "status", "due_at",
                "decided_by", "acting_for", "override_reason", "escalated_to", "escalated_at"} <= step_cols
```

In `tests/test_tenant_settings.py`:

```python
def test_approval_sla_days_is_settable_and_defaults_to_3():
    assert tenant_settings.validate({"approval_sla_days": 5}) == {"approval_sla_days": 5}
    assert tenant_settings.effective("nobody", store={})["approval_sla_days"] == 3

def test_approval_sla_days_cannot_be_negative():
    with pytest.raises(tenant_settings.SettingsError):
        tenant_settings.validate({"approval_sla_days": -1})
```

(Match how that file builds `store` for `effective`; copy its existing bank_change_recent_days tests.)

- [ ] **Step 2: Run → FAIL** (`KeyError`/`AssertionError`).

- [ ] **Step 3: Models.** Add `manager_email: Mapped[str | None] = mapped_column(String(320))` to `User` with a comment (escalation target). Then:

```python
class ApproverGroup(Base, TenantMixin, TimestampMixin):
    """A named set of people any one of whom satisfies a step."""

    __tablename__ = "approver_groups"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_approver_groups_name"),)

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
    delegator_email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    delegate_email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    starts_on: Mapped[date] = mapped_column(Date, nullable=False)
    ends_on: Mapped[date] = mapped_column(Date, nullable=False)
    created_by: Mapped[str] = mapped_column(String(320), nullable=False)


class ApprovalStep(Base, TenantMixin, TimestampMixin):
    """One link of an invoice's approval chain. The lowest pending position is
    the current step; nothing is ever decided out of order."""

    __tablename__ = "approval_steps"
    __table_args__ = (
        UniqueConstraint("tenant_id", "case_id", "position", name="uq_approval_steps_position"),
        Index("ix_approval_steps_pending", "tenant_id", "status", "due_at"),
    )

    PENDING, APPROVED, REJECTED, CANCELLED = "pending", "approved", "rejected", "cancelled"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    source_rule_id: Mapped[str | None] = mapped_column(String(36))
    assignee_kind: Mapped[str] = mapped_column(String(16), nullable=False)  # user|group|role
    assignee_value: Mapped[str] = mapped_column(String(320), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=PENDING)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(String(320))
    acting_for: Mapped[str | None] = mapped_column(String(320))
    override_reason: Mapped[str | None] = mapped_column(Text)
    escalated_to: Mapped[str | None] = mapped_column(String(320))
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)
```

Add `Date` and `date` to the imports if missing. Add the four tables to `TENANT_TABLES`.

- [ ] **Step 4: Migration** `f2c7a19d8e43`, revises `e7a2c9d41b56`. `RLS_TABLES = ["approver_groups", "approval_rules", "delegations", "approval_steps"]`. Create the four tables with every column above (`nullable=False` where the model says so; `created_at`/`updated_at` `nullable=False, server_default=sa.func.now()`), the unique constraints and the `ix_approval_steps_pending` index, then `op.add_column("users", sa.Column("manager_email", sa.String(320), nullable=True))`. Postgres-only: `enable_rls_statements(RLS_TABLES)`. Downgrade drops the column and the four tables. Then run an autogenerate compare on a scratch SQLite DB (upgrade head vs `Base.metadata`) and put the output in the report: no diffs for the new tables or `users`.

- [ ] **Step 5: Setting.** `"approval_sla_days": (int, float)` in `SETTABLE` with a why-comment; default `3` in `NON_ENGINE_DEFAULTS`; `DESCRIPTIONS` text: "Days an approval step may wait before it is escalated to the approver's manager, or an admin."; add to the non-negative validation group.

- [ ] **Step 6:** `pytest tests/test_approval_repository.py tests/test_tenant_settings.py tests/test_gcs_and_migrations.py tests/test_disabled_checks.py` → PASS (the migration round-trip and "every TENANT_TABLES entry exists" tests included). Full suite. ruff. Commit: `Give approval chains, groups, rules and delegations tables the worker can read`.

---

## Task 2: The pure engine `approvals.py`

**Files:**
- Create: `invoice_processing/approvals.py`
- Test: `tests/test_approvals.py` (new)

**Produces:**

```python
@dataclass(frozen=True)
class Assignee:
    kind: str   # "user" | "group" | "role"
    value: str  # email | group name | role name

@dataclass(frozen=True)
class StepSpec:
    assignee: Assignee
    source_rule_id: str | None

ROLE_FALLBACK = StepSpec(Assignee("role", "approver"), None)

def needs_person(invoice: dict, rules: list[dict]) -> bool
def build_chain(invoice: dict, rules: list[dict], *, organization_names: list[str]) -> list[StepSpec]
def satisfaction(email: str, role: str, step: dict, *, groups: dict[str, list[str]], delegators_of: list[str]) -> str | None
def refusal(email: str, *, uploader: str | None, previous_deciders: list[str]) -> str | None
def editor_needs_second(email: str, editors: list[str], is_only_pending: bool) -> bool
def match_entity(customer_name, organization_name) -> bool   # same folding as step 1.2
```

Rule dicts: `{id, name, kind, match, steps: [{kind, value}], enabled, created_at}`; `step` dicts for `satisfaction`: `{assignee_kind, assignee_value, escalated_to}`. Emails compared with `.strip().casefold()`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_approvals.py
"""Who must approve an invoice, and whether this person may. Pure: inputs in,
answer out, so the rules can be read and tested without a database."""

from invoice_processing import approvals as ap

ACME = {"vendor_name": "Cloudhaven Infra Services Pvt Ltd", "customer_name": "Zentrix Cloudworks Private Limited",
        "invoice_total_inc_tax": 120000.0, "currency": "INR", "_routing": "AUTO"}
ORGS = ["Zentrix Cloudworks"]


def rule(kind, match, steps, rid="r1", enabled=True, created_at="2026-01-01"):
    return {"id": rid, "name": rid, "kind": kind, "match": match, "steps": steps, "enabled": enabled, "created_at": created_at}

FIN = {"kind": "group", "value": "Finance"}
CFO = {"kind": "user", "value": "cfo@a.test"}
IT = {"kind": "user", "value": "it@a.test"}


class TestNeedsPerson:
    def test_review_routing_always_needs_a_person(self):
        assert ap.needs_person({**ACME, "_routing": "REVIEW"}, [])

    def test_auto_below_every_tier_is_touchless(self):
        assert not ap.needs_person(ACME, [rule("amount", {"min_amount": 500000}, [FIN])])

    def test_auto_at_or_above_a_tier_needs_a_person(self):
        assert ap.needs_person(ACME, [rule("amount", {"min_amount": 120000}, [FIN])])

    def test_a_tier_in_another_currency_does_not_count(self):
        assert not ap.needs_person(ACME, [rule("amount", {"min_amount": 1, "currency": "AUD"}, [FIN])])

    def test_disabled_rules_do_not_count(self):
        assert not ap.needs_person(ACME, [rule("amount", {"min_amount": 1}, [FIN], enabled=False)])


class TestChain:
    def test_no_rule_means_any_approver(self):
        assert ap.build_chain({**ACME, "_routing": "REVIEW"}, [], organization_names=ORGS) == [ap.ROLE_FALLBACK]

    def test_fixed_order_vendor_entity_amount(self):
        rules = [
            rule("amount", {"min_amount": 100000}, [FIN, CFO], rid="amt"),
            rule("entity", {"organization_name": "Zentrix Cloudworks"}, [{"kind": "group", "value": "Zentrix approvers"}], rid="ent"),
            rule("vendor", {"vendor_name": "Cloudhaven Infra Services Private Limited"}, [IT], rid="ven"),
        ]
        chain = ap.build_chain(ACME, rules, organization_names=ORGS)
        assert [(s.assignee.value, s.source_rule_id) for s in chain] == [
            ("it@a.test", "ven"), ("Zentrix approvers", "ent"), ("Finance", "amt"), ("cfo@a.test", "amt")]

    def test_only_the_highest_matching_tier_applies(self):
        rules = [rule("amount", {"min_amount": 50000}, [FIN], rid="low"),
                 rule("amount", {"min_amount": 100000}, [CFO], rid="high"),
                 rule("amount", {"min_amount": 500000}, [IT], rid="top")]
        assert [s.source_rule_id for s in ap.build_chain(ACME, rules, organization_names=ORGS)] == ["high"]

    def test_duplicate_assignee_collapses_to_first_position(self):
        rules = [rule("vendor", {"vendor_name": ACME["vendor_name"]}, [CFO], rid="ven"),
                 rule("amount", {"min_amount": 1}, [FIN, CFO], rid="amt")]
        assert [s.assignee.value for s in ap.build_chain(ACME, rules, organization_names=ORGS)] == ["cfo@a.test", "Finance"]

    def test_entity_match_folds_like_step_1_2(self):
        rules = [rule("entity", {"organization_name": "ZENTRIX  CLOUD-WORKS"}, [FIN])]
        assert ap.build_chain(ACME, rules, organization_names=ORGS)[0].assignee.value == "Finance"

    def test_review_invoice_with_no_match_gets_role_fallback_only(self):
        assert ap.build_chain({**ACME, "_routing": "REVIEW"}, [rule("vendor", {"vendor_name": "Other"}, [IT])], organization_names=ORGS) == [ap.ROLE_FALLBACK]


class TestSatisfaction:
    GROUPS = {"Finance": ["fin1@a.test", "Fin2@A.test"]}

    def test_named_user(self):
        assert ap.satisfaction("cfo@a.test", "approver", {"assignee_kind": "user", "assignee_value": "CFO@a.test", "escalated_to": None}, groups=self.GROUPS, delegators_of=[]) == "assigned to you"

    def test_group_member_case_insensitive(self):
        assert ap.satisfaction("fin2@a.test", "clerk", {"assignee_kind": "group", "assignee_value": "Finance", "escalated_to": None}, groups=self.GROUPS, delegators_of=[]) == "member of Finance"

    def test_role_holder(self):
        step = {"assignee_kind": "role", "assignee_value": "approver", "escalated_to": None}
        assert ap.satisfaction("x@a.test", "admin", step, groups={}, delegators_of=[]) == "has the approver role"
        assert ap.satisfaction("x@a.test", "clerk", step, groups={}, delegators_of=[]) is None

    def test_active_delegate_of_the_assignee(self):
        step = {"assignee_kind": "user", "assignee_value": "cfo@a.test", "escalated_to": None}
        assert ap.satisfaction("pa@a.test", "clerk", step, groups={}, delegators_of=["cfo@a.test"]) == "delegate for cfo@a.test"

    def test_escalation_target_user_and_role(self):
        step = {"assignee_kind": "user", "assignee_value": "cfo@a.test", "escalated_to": "boss@a.test"}
        assert ap.satisfaction("boss@a.test", "clerk", step, groups={}, delegators_of=[]) == "escalated to you"
        step = {**step, "escalated_to": "role:admin"}
        assert ap.satisfaction("root@a.test", "admin", step, groups={}, delegators_of=[]) == "escalated to admins"

    def test_nobody_else(self):
        assert ap.satisfaction("x@a.test", "approver", {"assignee_kind": "user", "assignee_value": "cfo@a.test", "escalated_to": None}, groups={}, delegators_of=[]) is None


class TestSegregation:
    def test_uploader_is_refused(self):
        assert "uploaded" in ap.refusal("clerk@a.test", uploader="Clerk@a.test", previous_deciders=[])

    def test_previous_decider_is_refused(self):
        assert "earlier step" in ap.refusal("a@a.test", uploader=None, previous_deciders=["a@a.test"])

    def test_otherwise_allowed(self):
        assert ap.refusal("b@a.test", uploader="a@a.test", previous_deciders=["a@a.test"]) is None

    def test_editor_as_sole_approver_needs_a_second(self):
        assert ap.editor_needs_second("ed@a.test", ["ed@a.test"], is_only_pending=True)
        assert not ap.editor_needs_second("ed@a.test", ["ed@a.test"], is_only_pending=False)
        assert not ap.editor_needs_second("other@a.test", ["ed@a.test"], is_only_pending=True)
```

- [ ] **Step 2: Run → FAIL** (`ModuleNotFoundError`).

- [ ] **Step 3: Implement**

```python
"""Who must approve an invoice, and whether this person may.

Pure on purpose, like bank_check: the routing rules are the kind of thing an
auditor asks to see, and a function that takes rules in and gives a chain out
can be read, tested and explained without a database.

Only invoices a person must touch get a chain. The pipeline's AUTO routing
is the touchless rate a pilot is judged on; an amount tier is the one thing
that can pull an AUTO invoice back to a person.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import po_matching

ROLE_RANK = {"viewer": 0, "clerk": 1, "approver": 2, "admin": 3}
KIND_ORDER = ("vendor", "entity", "amount")  # the fixed order rules contribute in


@dataclass(frozen=True)
class Assignee:
    kind: str
    value: str


@dataclass(frozen=True)
class StepSpec:
    assignee: Assignee
    source_rule_id: str | None


ROLE_FALLBACK = StepSpec(Assignee("role", "approver"), None)


def _email(value) -> str:
    return str(value or "").strip().casefold()


def _fold(value) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def match_entity(customer_name, organization_name) -> bool:
    """The test step 1.2 makes: letters and digits only, substring."""
    org = _fold(organization_name)
    return bool(org) and org in _fold(customer_name)


def _amount_tier(invoice: dict, rules: list[dict]) -> dict | None:
    total = invoice.get("invoice_total_inc_tax")
    if total is None:
        return None
    currency = str(invoice.get("currency") or "").upper()
    best = None
    for r in rules:
        if r["kind"] != "amount" or not r.get("enabled", True):
            continue
        wanted = str(r["match"].get("currency") or "").upper()
        if wanted and wanted != currency:
            continue
        floor = float(r["match"].get("min_amount") or 0)
        if float(total) >= floor and (best is None or floor > float(best["match"].get("min_amount") or 0)):
            best = r
    return best


def needs_person(invoice: dict, rules: list[dict]) -> bool:
    return invoice.get("_routing") == "REVIEW" or _amount_tier(invoice, rules) is not None


def build_chain(invoice: dict, rules: list[dict], *, organization_names: list[str]) -> list[StepSpec]:
    vendor_key = po_matching.normalize_vendor(invoice.get("vendor_name"))
    ordered = sorted((r for r in rules if r.get("enabled", True)), key=lambda r: str(r.get("created_at") or ""))
    matched: list[dict] = []
    matched += [r for r in ordered if r["kind"] == "vendor"
                and po_matching.normalize_vendor(r["match"].get("vendor_name")) == vendor_key and vendor_key]
    matched += [r for r in ordered if r["kind"] == "entity"
                and match_entity(invoice.get("customer_name"), r["match"].get("organization_name"))]
    tier = _amount_tier(invoice, rules)
    if tier:
        matched.append(tier)

    chain: list[StepSpec] = []
    seen: set[tuple[str, str]] = set()
    for r in matched:
        for s in r.get("steps") or []:
            a = Assignee(str(s["kind"]), str(s["value"]))
            key = (a.kind, a.value.casefold())
            if key in seen:
                continue  # one person approves once, at their first position
            seen.add(key)
            chain.append(StepSpec(a, r["id"]))
    return chain or [ROLE_FALLBACK]


def satisfaction(email: str, role: str, step: dict, *, groups: dict[str, list[str]], delegators_of: list[str]) -> str | None:
    """Why this person may decide this step, or None."""
    me = _email(email)
    kind, value = step["assignee_kind"], step["assignee_value"]
    mine = {me, *(_email(d) for d in delegators_of)}

    def direct(who: str) -> str | None:
        if kind == "user" and _email(value) == who:
            return "assigned to you" if who == me else f"delegate for {value}"
        if kind == "group" and who in {_email(m) for m in groups.get(value, [])}:
            return f"member of {value}" if who == me else f"delegate for a member of {value}"
        return None

    for who in mine:
        why = direct(who)
        if why:
            return why
    if kind == "role" and ROLE_RANK.get(role, -1) >= ROLE_RANK.get(value, 99):
        return f"has the {value} role"
    target = step.get("escalated_to")
    if target:
        if target.startswith("role:"):
            if ROLE_RANK.get(role, -1) >= ROLE_RANK.get(target[5:], 99):
                return f"escalated to {target[5:]}s"
        elif _email(target) == me:
            return "escalated to you"
    return None


def refusal(email: str, *, uploader: str | None, previous_deciders: list[str]) -> str | None:
    me = _email(email)
    if uploader and _email(uploader) == me:
        return "The person who uploaded an invoice cannot approve it."
    if me in {_email(d) for d in previous_deciders}:
        return "You decided an earlier step; each step needs a different person."
    return None


def editor_needs_second(email: str, editors: list[str], is_only_pending: bool) -> bool:
    """Someone who corrected the fields cannot be the sole approver."""
    return is_only_pending and _email(email) in {_email(e) for e in editors}
```

- [ ] **Step 4:** tests PASS; ruff; commit `Decide who must approve an invoice, and whether this person may`.

---

## Task 3: `PgApprovalRepository`

**Files:**
- Modify: `invoice_processing/db/repositories.py` (new class after `PgVendorRepository`; exceptions; `_ENTITY_TYPES` += `"approval": "approval", "delegation": "delegation"`)
- Modify: `invoice_processing/api/service.py` (`_approvals = None`; `get_approvals()` via `_table_exists("approval_steps", "approval routing")`)
- Modify: `tests/conftest.py` (reset `_approvals`)
- Modify: `.github/workflows/ci.yml` (add `tests/test_approval_repository.py`)
- Test: `tests/test_approval_repository.py`

**Produces** `PgApprovalRepository(url=None)`:
- groups: `list_groups(t)`, `create_group(t, name, members) -> dict`, `update_group(t, id, *, name=None, members=None)`, `delete_group(t, id)`; `groups_map(t) -> dict[name, members]`
- rules: `list_rules(t)`, `create_rule(t, *, name, kind, match, steps, enabled=True)`, `update_rule(t, id, **fields)`, `delete_rule(t, id)`
- delegations: `list_delegations(t, *, email=None)`, `create_delegation(t, *, delegator, delegate, starts_on, ends_on, created_by)`, `delete_delegation(t, id)`, `active_delegators_of(t, email, today) -> list[str]`
- steps: `chain(t, case_id) -> list[dict]`, `current(t, case_id) -> dict | None`, `replace_pending(t, case_id, specs: list[StepSpec], *, sla_days, now) -> list[dict]` (deletes pending, keeps decided, appends new positions after the highest decided, sets `due_at` on the first pending only), `append_step(t, case_id, spec, *, sla_days, now)`, `decide(t, step_id, *, approve: bool, decided_by, acting_for=None, override_reason=None, note=None, sla_days, now) -> tuple[dict, bool completed]` (approve → next pending gets `due_at`; reject → remaining pending cancelled; raises `StepNotPending`), `mine(t, *, email, role, groups_map, delegators_of, today) -> list[dict]` (current pending steps the engine's `satisfaction` accepts, each with `why`), `overdue(t, now) -> list[dict]`, `escalate(t, step_id, *, to, now)`, `tenants_with_pending() -> list[str]` (via `tenant_session("")` over `Tenant` join, or list tenants then count; pick what RLS allows and document it).
- Exceptions: `StepNotPending`, `UnknownGroup`, `UnknownRule`, `InvalidDelegation`.

- [ ] **Step 1: Failing tests** — one class per concern in `tests/test_approval_repository.py` using a `repo` fixture like `tests/test_vendor_repository.py` (drop_all/create_all, tenants `tenant-a`/`tenant-b` via per-tenant sessions): group CRUD + tenant scoping; rule CRUD; delegation window (`active_delegators_of` on the start day, end day, day after); `replace_pending` keeps decided steps and renumbers; `decide` approve advances and sets the next `due_at`, reject cancels the rest, deciding a non-pending raises; `mine` returns a group member's step with `why == "member of Finance"` and not another tenant's; `overdue` returns only pending past-due unescalated; `escalate` sets fields. Write the real assertions (ids, statuses, positions, `due_at` present/absent).

- [ ] **Step 2: FAIL. Step 3: implement** following `PgVendorRepository`'s shape (`tenant_session`, explicit `tenant_id` filters on every query, `_to_dict` helpers with ISO timestamps, `with_for_update()` in `decide`). `mine` loads current pending steps for the tenant (one query: pending steps whose position is the minimum pending per case — do it in Python over pending rows grouped by case; `# ponytail:` fine for a tenant's queue), then filters with `approvals.satisfaction`.

- [ ] **Step 4:** service accessor + conftest + CI list; tests PASS; full suite; ruff; commit `Store approval chains, groups, rules and delegations per tenant`.

---

## Task 4: `approval_flow.py` and the actions endpoint

**Files:**
- Create: `invoice_processing/approval_flow.py` (DB-aware glue used by the API and the worker)
- Modify: `invoice_processing/api/service.py` (`ActionRequest` ~:516; `act` ~:1005-1075)
- Test: `tests/test_approval_routes.py` (new; the actions part), `tests/test_export_path.py` (one test: completion still exports)

**Produces** in `approval_flow.py`:

```python
NEEDS_DB = "Approval routing needs a database. Configure DATABASE_URL."

def route(ctx: RunContext, artifacts: dict, *, repo, now: datetime) -> list[dict]
    # rules = repo.list_rules(t); org names + sla from tenant_settings.effective(t);
    # invoice = artifacts["extraction"]["invoice"]; if not approvals.needs_person → repo.replace_pending(t, case, [], ...) and return [];
    # else repo.replace_pending(t, case, approvals.build_chain(...), sla_days=..., now=now)

def uploader_of(ctx, documents) -> str | None      # list_documents scan: id == case_id and source == "upload" → source_ref  (# ponytail: linear scan, same as _storage_key_for_case)
def editors_of(ctx, corrections) -> list[str]        # distinct non-None actors from corrections.list(t, case_id)

@dataclass
class Decision:
    step: dict; completed: bool; why: str; overridden: bool; appended_second: bool

class Refused(Exception): ...   # .detail sentence

def decide(ctx, principal, artifacts, *, repo, documents, corrections, approve: bool, note, override_reason, now) -> Decision
    # current = repo.current(t, case); if None: chain = route(...) (an invoice processed before routing existed, or routed touchless but a person is acting anyway → build_chain is never empty); current = repo.current(...)
    # why = approvals.satisfaction(email, role, current, groups=repo.groups_map(t), delegators_of=repo.active_delegators_of(t, email, now.date()))
    # block = approvals.refusal(email, uploader=uploader_of(...), previous_deciders=[s["decided_by"] for s in chain if s["status"]=="approved"])
    # if (why is None or block) and not (principal.can("admin") and override_reason): raise Refused(block or "This step is not assigned to you.")
    # acting_for = the delegator when why startswith "delegate for"
    # step, completed = repo.decide(...)
    # if approve and completed and approvals.editor_needs_second(email, editors_of(...), is_only_pending=True): repo.append_step(role approver); completed = False; appended_second = True
```

`act` changes (keep every existing validation, lock check and 404):

```python
    repo = get_approvals()
    previous = artifacts.get(WORKFLOW_ARTIFACT) or {}
    if repo is not None:
        if previous.get("decision"):
            raise HTTPException(status_code=409, detail="This invoice was already decided.")
        try:
            outcome = approval_flow.decide(ctx, principal, artifacts, repo=repo, documents=get_documents(),
                corrections=get_corrections(), approve=request.action == "approve", note=request.reason,
                override_reason=request.override_reason, now=datetime.now(timezone.utc))
        except approval_flow.Refused as exc:
            raise HTTPException(status_code=403, detail=exc.detail) from None
        get_audit().emit(ctx, "approval_overridden" if outcome.overridden else "approval_step_decided",
            {"step": outcome.step["position"], "assignee": f"{outcome.step['assignee_kind']}:{outcome.step['assignee_value']}",
             "action": request.action, "why": outcome.why, "acting_for": outcome.step.get("acting_for"),
             "override_reason": request.override_reason})
        if not outcome.completed:
            return {"case_id": case_id, "action": request.action, "completed": False,
                    "step": outcome.step["position"], "next": repo.current(principal.tenant_id, case_id)}
        get_audit().emit(ctx, "approval_chain_completed", {"action": request.action})
    # ... existing _record_workflow / _reproject / agreement / rule proposal / invoice_approved|rejected unchanged,
    # returning {..., "completed": True}
```

`ActionRequest` gains `override_reason: str | None = Field(default=None, max_length=2000)`; a non-admin sending it gets 403 "Only an admin can override an approval rule."

Without a database (`repo is None`) the endpoint behaves exactly as today.

- [ ] **Step 1: Failing tests** in `tests/test_approval_routes.py`. Fixture `env` modelled on `tests/test_vendor_routes.py` (DB via `scratch_db`, drop_all/create_all, tenant `tenant-acme`, `FakeVerifier` with `role:who` tokens, `service._approvals = PgApprovalRepository(url)`, `DATABASE_URL` set) **plus** the case store from `tests/test_export_path.py` (copy GOLDEN_CASES, `FsCaseRepository`, delete each case's workflow). Also monkeypatch `service._documents` with the `JsonDocumentRepository` and insert a document row whose id is `case_005`, `source="upload"`, `source_ref="uploader@x.test"`, and `service._corrections` with `JsonlCorrectionStore`. Tests:
  - no rules: an approver approves `case_005` → 200 `completed: True`, workflow `decision == "approve"`, `invoice_approved` audited (today's behaviour preserved).
  - a two-tier amount rule (steps `[user a@x.test, user b@x.test]`): `a` approves → `completed: False`, `step: 1`, `next` names `b`; `a` on step 2 → 403 "earlier step"; `b` approves → `completed: True`, workflow keys written.
  - the uploader (`uploader@x.test` as approver) → 403 "uploaded".
  - an editor who PATCHed fields and is the only approver → after approving, a second `role:approver` step exists and `completed: False`.
  - a clerk sending `override_reason` → 403; an admin with `override_reason` passes a refusal, audit `approval_overridden` carries the reason.
  - reject at step 1 cancels step 2 and writes `decision == "reject"`.
  - a decided invoice → 409.
  - in `tests/test_export_path.py`: with the approvals repo wired and no rules, approve then export still 200 (the completion keys are what export reads).

- [ ] **Step 2: FAIL. Step 3: implement. Step 4:** PASS, full suite, ruff. Commit `Decide one step of an invoice's approval chain, and finish it only on the last`.

---

## Task 5: Worker routing and the overdue scan

**Files:**
- Modify: `invoice_processing/worker/runner.py` (after `project_case` ~:303; `Worker.__init__`/`run_forever` ~:571-676)
- Modify: `invoice_processing/approval_flow.py` (`escalate_overdue(repo, provisioner_or_users, *, now) -> int`)
- Modify: `invoice_processing/db/repositories.py` (`PgApprovalRepository.manager_of(t, email) -> str | None` via `User.manager_email`; `tenant_ids() -> list[str]` via `tenant_session("")` over `Tenant`, documented as the one deliberately cross-tenant read, same as `tenant_id_for_slug`)
- Test: `tests/test_worker.py`, `tests/test_approval_repository.py`

- [ ] **Step 1: Failing tests**
  - `tests/test_worker.py`: with `service.get_approvals` monkeypatched to a repo on a scratch DB (tenant row added) and `process_invoice` stubbed to write an extraction whose invoice has `_routing: "REVIEW"`, `process_document` leaves one pending `role:approver` step for the case; with `_routing: "AUTO"` and no rules, zero steps. (Use the `Sink`/`Blobs` stubs from `test_the_email_sender_reaches_the_pipeline_config`; `project_case` on the stub is a no-op, routing must run regardless of projection success.)
  - `Worker.run_forever` with an injected `sleep` that raises `StopIteration` after one loop calls `escalate_overdue` once (monkeypatch `approval_flow.escalate_overdue` to count calls).
  - repository: `escalate_overdue` on a pending step past due assigns `escalated_to` = the assignee's `manager_email` when the assignee is a user with a manager, else `"role:admin"`; a group assignee → `"role:admin"`; audit `approval_escalated` emitted once per step (not again next scan).

- [ ] **Step 2: FAIL. Step 3: implement.**
  - In `process_document`, after the projection `try/except`:
    ```python
        # Who must approve, decided now that the columns exist. Separate from
        # projection on purpose: a projection failure must not leave an invoice
        # unrouted, and an unrouted invoice is one nobody is asked to approve.
        try:
            repo = service.get_approvals()
            if repo is not None:
                approval_flow.route(ctx, artifacts, repo=repo, now=datetime.now(timezone.utc))
        except Exception as exc:
            logger.warning("Case %s not routed for approval: %s", case_id, exc)
    ```
  - `Worker.run_forever`: before `if not outcomes:` call `self._escalate_if_due()` which runs `approval_flow.escalate_overdue(...)` at most every `ESCALATION_SCAN_SECONDS = 60` (`# ponytail: a poll-loop timer; a scheduler when there is one`), swallowing and logging exceptions.
  - `escalate_overdue`: for each tenant id, for each overdue step: target = `repo.manager_of(t, assignee_value)` if `assignee_kind == "user"` and a manager exists, else `"role:admin"`; `repo.escalate(...)`; `audit.emit(RunContext(tenant_id=t, case_id=step["case_id"], actor="worker"), "approval_escalated", {...})`; return count.

- [ ] **Step 4:** PASS, full suite, ruff. Commit `Route each processed invoice to its approvers, and escalate the ones nobody decided`.

---

## Task 6: Approval routes, admin manager, field re-routing, isolation probes

**Files:**
- Create: `invoice_processing/api/approval_routes.py`
- Modify: `invoice_processing/api/service.py` (include router; `edit_fields` ~:782-842 re-route)
- Modify: `invoice_processing/api/admin_routes.py` (`PATCH /api/admin/users/{user_id}`), `invoice_processing/admin/provision.py` (`set_manager(tenant_id, user_id, manager_email)`; `list_users` returns `manager_email`)
- Modify: `tests/test_tenant_isolation_api.py` (`PROBED`, module tuple, probes), `tests/test_approval_routes.py`, `.github/workflows/ci.yml` (add `tests/test_approval_routes.py`)

Routes (bodies are Pydantic models with `Field(max_length=...)`: names 128, emails 320, notes 2000):

| Route | Role | Notes |
|---|---|---|
| `GET /api/approvals/mine` | viewer | `{"items": [...], "count"}`; each item: `case_id, vendor, invoice_number, total, currency, position, due_at, overdue, why, assignee`; uses `repo.mine(...)` then `service._summarize` per case (`get_cases().get_many_artifacts` if present). |
| `GET /api/invoices/{case_id}/approvals` | viewer | `{"steps": repo.chain(...), "current": position or None}`; 404 unknown case. |
| `GET/POST /api/approvals/groups`, `PATCH/DELETE /api/approvals/groups/{group_id}` | clerk / admin | members validated against `_provisioner().list_users(tenant)` emails (400 naming the unknown one). Audit `approval_group_created/updated/deleted`. |
| `GET/POST /api/approvals/rules`, `PATCH/DELETE /api/approvals/rules/{rule_id}` | clerk / admin | `kind ∈ {vendor, entity, amount}`; match keys per kind; `steps` non-empty, each `kind ∈ {user, group, role}`, user → tenant user, group → existing group, role → ROLE_RANK. Audit `approval_rule_*`. |
| `GET/POST /api/approvals/delegations`, `DELETE /api/approvals/delegations/{delegation_id}` | approver / admin | approver may create/delete only where `delegator == principal.email`; admin anyone. 400 self-delegation, bad range, unknown delegate. Audit `delegation_created/deleted`. |
| `PATCH /api/admin/users/{user_id}` | admin | body `{manager_email: str | None}`; must be a tenant user and not the user themself. Audit `user_updated` with before. |

All approval routes 503 `NEEDS_DB` when `get_approvals()` is None. Declare literal paths before parameterised ones.

`edit_fields`: after `_reproject`, if `get_approvals()` and any edited field in `{"invoice_total_inc_tax", "vendor_name", "customer_name", "currency"}`: `approval_flow.route(...)`, audit `approval_rerouted`, and add `"approvals_rerouted": True` to the response (else `False`).

- [ ] **Step 1: Failing tests** in `tests/test_approval_routes.py`: every route's role gate; group/rule/delegation validation errors; `mine` for a group member (`why == "member of Finance"`) and for a delegate within range and not outside it; chain endpoint; `edit_fields` on the total re-routes to a higher tier and reports `approvals_rerouted: True`; 503 without DB parametrised over all routes; admin manager patch and its refusals.
  In `tests/test_tenant_isolation_api.py`: a `approvals_db` fixture (like `vendor_db`); probes: GLOBEX cannot read ACME's chain (404), `mine` is empty for GLOBEX after ACME has pending steps, GLOBEX admin cannot PATCH/DELETE ACME's group/rule/delegation by id (404), GLOBEX admin cannot set a manager on an ACME user (404); add `approval_routes` to the module tuple; `PROBED` entries for every new path.

- [ ] **Step 2: FAIL. Step 3: implement. Step 4:** `pytest tests/test_approval_routes.py tests/test_tenant_isolation_api.py tests/test_admin* -q`, full suite, ruff. Commit `Expose the approval queue, chains, groups, rules and delegations`.

---

## Task 7: Frontend

**Files:**
- Modify: `frontend/src/lib/api.ts` (types), `frontend/src/components/AppShell.tsx` (NAV: `{ href: "/approvals", label: "Approvals", glyph: "✓", min: "viewer" }` after Invoices), `frontend/src/app/invoices/page.tsx`, `frontend/src/app/admin/page.tsx`
- Create: `frontend/src/app/approvals/page.tsx`, `frontend/src/app/admin/approvals/page.tsx`
- Modify: `frontend/src/app/globals.css` (one `.card-list` rule set: single column, `gap: 12px`; `.btn-big { padding: 14px 18px; font-size: 15px; width: 100%; }`), `frontend/e2e/golden-path.spec.ts`

Types:

```ts
export type ApprovalStep = { id: string; position: number; assignee_kind: "user"|"group"|"role"; assignee_value: string;
  status: "pending"|"approved"|"rejected"|"cancelled"; due_at: string|null; decided_by: string|null; acting_for: string|null;
  override_reason: string|null; escalated_to: string|null; escalated_at: string|null; note: string|null; source_rule_id: string|null };
export type ApprovalChain = { steps: ApprovalStep[]; current: number|null };
export type MyApproval = { case_id: string; vendor: string|null; invoice_number: string|null; total: number|null; currency: string|null;
  position: number; due_at: string|null; overdue: boolean; why: string; assignee: string };
export type ApproverGroup = { id: string; name: string; members: string[] };
export type ApprovalRule = { id: string; name: string; kind: "vendor"|"entity"|"amount"; match: Record<string, unknown>;
  steps: { kind: "user"|"group"|"role"; value: string }[]; enabled: boolean };
export type Delegation = { id: string; delegator_email: string; delegate_email: string; starts_on: string; ends_on: string; created_by: string };
```

- **`/approvals`**: `usePoll<{items: MyApproval[]}>("/api/approvals/mine", 15000)`; `.card-list` of `.panel` cards; each: vendor + invoice number (link `/invoices?case=`), total, `why`, due date with `badge-error` "overdue" when `overdue`, step "n of m" is not available from `mine` so show "step n"; buttons `.btn-primary.btn-big` Approve (opens `DecisionDialog`, reused by extracting it to `components/DecisionDialog.tsx`) and `.btn-secondary.btn-big` Reject (dialog, note required). On success: notice from the response: `completed ? "Approved" : "Step n approved; next: <assignee>"`; reload. Empty state: "Nothing is waiting for you." Below: "Out of office" panel for approver+: list own delegations with Delete, form delegate email + two `<input type="date">`, POST. When the API returns 503: the muted "needs a database" line, as the vendors page does.
- **Workbench**: new `<ApprovalPanel chain={...} me={me} onOverride={...}/>` under findings, polling `/api/invoices/{id}/approvals`; rows per step with badge per status, assignee, `decided_by (as delegate for X)`, due; the current step highlighted. Approve/Reject buttons: keep enabled (the server is the authority) but when the chain exists and the current step is not mine (`why` unknown client-side → derive from `/api/approvals/mine` containing this case), show the sentence "Step n is assigned to <assignee>" beside them; a 403 from the server shows the detail in the notice (already how `act` reports errors). Admin: an "Override…" `.btn-chip` that `window.prompt`s for a reason and posts `override_reason`. After a non-final approval, `say("Step n approved; waiting on <next>")` and stay on the invoice (don't advance the queue).
- **Admin**: users table gets a Manager column: `<input>` + Save calling `PATCH /api/admin/users/{id}`; nav link "Approval setup" to `/admin/approvals`.
- **`/admin/approvals`**: three panels. Groups: list (name, members joined), inline form (name, members textarea one email per line), Delete. Rules: list (name, kind, match summary, steps as "user:x → group:y"), inline form: name, kind select, match inputs that switch with kind (vendor name / organisation name / min amount + currency), steps as an ordered list with add/remove rows (kind select + value input), enabled checkbox; Delete. Delegations: list + form (delegator, delegate, dates) for admins.
- **e2e** (DB-backed, `as()` helper): (1) admin creates a two-step amount rule `[user alice, user bob]` with `min_amount 1` via `/admin/approvals`; `alice@e2e.test:approver` opens `/approvals`, sees case_005 (total 1069.32 AUD > 1), approves; reloads and no longer sees it; as alice on the workbench the panel says step 2 is assigned to bob; `bob@e2e.test:approver` approves from `/approvals`; `/sheet` lists the vendor. (2) The uploader refusal is covered by the API test in Task 4 and has no e2e: the e2e stack runs no worker, so an uploaded PDF never becomes a case to approve. Record that in the spec file in Task 8. (3) Delegation: alice creates a delegation to `pat@e2e.test` for today; `pat@e2e.test:clerk` sees alice's step on `/approvals` with "delegate for alice@e2e.test".
  Run `cd frontend && npm run lint && npx tsc --noEmit && npm run test:e2e`.

- [ ] Commit `Show each person what is waiting for them, and admins how approvals route`.

---

## Task 8: Measurement, CHANGELOG, docs

- [ ] Replay with no rules: `./.venv/bin/python -m benchmark.harness.replay --run after-ecb9bbd --profile benchmark/harness/profiles/after.json --out replay-approvals` then score; confirm 40/45 posted untouched and 1 posted wrongly are unchanged (routing is not in the replay path, so this is a guard that nothing in the shared code moved them).
- [ ] Count, with a scratchpad script over `benchmark/runs/after-ecb9bbd/raw/*.jsonl`, how many ACCEPT+AUTO invoices have `invoice_total_inc_tax >= 500000` (the tier a benchmark tenant might set) and report it in `benchmark/CHANGELOG.md` as "would now need a person under a 5-lakh tier", not tuned.
- [ ] README: a short "Approval workflows" section (who is routed, the fixed order, segregation, delegation, escalation, the routes, 503 without a database). Spec file: add the uploader-e2e note from Task 7.
- [ ] Commit `Measure and document approval routing`.

---

## Task 9: Gates and stop

- [ ] `bash benchmark/harness/gates.sh` → 9/9 PASS. Report commits, test counts, deviations. Do not push; the user decides.

---

## Self-review against the spec

- §1 tables/column/setting → Task 1 ✔. §2 engine (needs_person, order, tier, collapse, due at current, satisfaction incl. delegate/escalation, three segregation rules, override, escalation to manager/admin) → Tasks 2, 4, 5 ✔. §3 routes, actions contract (+override_reason, 403/409), fields re-route, audit names, entity types, 503 → Tasks 3, 4, 6 ✔. §4 pages, panel, admin, types, e2e → Task 7 ✔ (uploader e2e replaced by an API test; recorded). §5 tests per layer, measurement, rollout order → Tasks 1–8 in order ✔. Out of scope untouched.
- Names consistent across tasks: `approvals.Assignee/StepSpec/ROLE_FALLBACK/needs_person/build_chain/satisfaction/refusal/editor_needs_second`; `PgApprovalRepository.{list_groups, create_group, update_group, delete_group, groups_map, list_rules, create_rule, update_rule, delete_rule, list_delegations, create_delegation, delete_delegation, active_delegators_of, chain, current, replace_pending, append_step, decide, mine, overdue, escalate, manager_of, tenant_ids}`; `approval_flow.{route, decide, Decision, Refused, uploader_of, editors_of, escalate_overdue, NEEDS_DB}`; `service.get_approvals()`; audit names as in §3.
