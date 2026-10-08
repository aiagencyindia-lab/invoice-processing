# Approval workflows — design

Phase 3 ("Parity") sub-project 2. Design approved section by section with the user on
2026-10-08 (superpowers brainstorming, architectural path). The implementation plan is written
from this document; nothing here is built before that plan exists.

## Context

Approval today is one action by anyone with the `approver` role (`service.py` `act`,
POST `/api/invoices/{case_id}/actions`). Nothing checks amount, vendor or entity; there is no
assignment, delegation or escalation; export and the sheet trust a single key,
`workflow.decision == "approve"`. The product plan's Phase 3 lists "approval workflows
(conditional routing, delegation, escalation, mobile)".

Facts the design builds on:
- Workflow state is a free-form JSON merge (`_record_workflow`); keys written today are
  `decision, decided_by, decided_at, reason, reason_category` plus export keys.
- `users` has email and role only; roles are also Identity Platform claims. No manager,
  limit, department or entity exists.
- No entity concept; step 1.2 matches the invoice's `customer_name` against
  `organization_names`. That match is the "entity".
- Uploader is `documents.source_ref` when `source == "upload"`; case id equals document id.
  Field edits are rows in the corrections store with an actor.
- No outbound notification mechanism exists. Escalation is in-app.
- The e2e stack runs on a database and can be several people (`X-Dev-Principal`).

Decisions taken with the user: routing applies only to invoices a person must touch (AUTO
stays touchless unless an amount tier matches); rules match on vendor, entity, amount tier
(GL deferred to the GL auto-coding sub-project); out-of-office delegation with a date range;
SLA escalation to the assignee's manager else any admin, nothing auto-approved; a phone-sized
`/approvals` page in the web app; targets are named groups and individual users, with role
fallback; multiple matching rules union in the fixed order vendor → entity → amount; no rule
means any approver (today's behaviour); segregation: uploader refused, field-editor cannot be
sole approver, each step by a different person, admin override with a recorded reason;
storage approach A (dedicated tables).

## Section 1 — Data model

One Alembic migration (template: the vendors migration `e7a2c9d41b56`; RLS via
`enable_rls_statements`, skipped on non-Postgres). All new tables in `TENANT_TABLES`.

| Table | Columns | Rules |
|---|---|---|
| `approver_groups` | id, tenant_id, name, members (JSON list of emails), created_at, updated_at | unique (tenant_id, name). Members as a JSON list — `# ponytail:` a tenant has a handful of groups; membership is filtered in Python; a members table if groups grow. Members must be tenant users. |
| `approval_rules` | id, tenant_id, name, kind (`vendor`/`entity`/`amount`), match (JSON), steps (JSON list of `{kind: user/group/role, value}`), enabled, created_at, updated_at | match is `{vendor_name}` / `{organization_name}` / `{min_amount, currency?}`. A `user` value must be a tenant user; a `group` value must exist. |
| `delegations` | id, tenant_id, delegator_email, delegate_email, starts_on, ends_on, created_by, created_at, updated_at | delegator ≠ delegate; ends_on ≥ starts_on; active when today ∈ [starts_on, ends_on]. |
| `approval_steps` | id, tenant_id, case_id, position, source_rule_id (nullable), assignee_kind, assignee_value, status (`pending`/`approved`/`rejected`/`cancelled`), due_at, decided_by, acting_for, override_reason, escalated_to, escalated_at, note, created_at, updated_at | unique (tenant_id, case_id, position). Steps run in position order; the lowest `pending` is current. |
| `users` | + `manager_email` (nullable) | escalation target; must be a tenant user; set by admins. |

Unchanged: the existing workflow keys. Completing the last step writes `decision="approve"`,
`decided_by`, `decided_at` (and reason/category) exactly as today so export, the sheet and the
learning loop see no change. A rejection at any step writes `decision="reject"` and cancels
the remaining steps.

Setting: `approval_sla_days` (int, default 3, calendar days) in `tenant_settings.SETTABLE`
with description; business-day calendars deferred.

## Section 2 — Routing engine

Pure module `invoice_processing/approvals.py` (pattern: `bank_check.py`): inputs in, steps
out, no DB access.

**When it runs:** in the worker right after `project_case`; and from the fields endpoint when
total, vendor name or customer name changed. Re-routing replaces only pending steps; decided
steps stay.

**Needs a person:** `_routing == "REVIEW"`, or any enabled amount rule's `min_amount` ≤
`total_inc_tax` (restricted to the rule's currency when it names one). Otherwise no steps,
and the invoice stays touchless.

**Chain, in fixed order:**
1. Vendor rules whose `vendor_name` normalises (`po_matching.normalize_vendor`) to the
   invoice's vendor name — all matching, in rule creation order.
2. Entity rules whose `organization_name` matches the invoice's `customer_name` the way step
   1.2 matches it.
3. The single amount tier: the enabled amount rule with the highest `min_amount` ≤ total.
4. No match → one step `{role: approver}`.

Each rule contributes its steps in order. A duplicate assignee (same kind and value) collapses
to its first position.

**Due date:** `now + approval_sla_days` set when a step becomes current, not at chain build.

**Satisfying a step:** the caller is the named user; or a member of the named group; or holds
the named role (`Principal.can`); or is an active delegate of someone who does
(`acting_for` recorded); or is the step's `escalated_to` (user or role). Admins may satisfy any
step by override with a reason (`override_reason` recorded; audit `approval_overridden`).

**Segregation, at decision time:**
- The uploader (`documents.source_ref`, source `upload`) is refused at every step.
- A field editor (any correction actor on this case) cannot be the sole approver: if they
  approve the only step, a further `{role: approver}` step is appended.
- The decider of an earlier step cannot decide the next one.
- Refusals are 403 with a sentence naming the rule; an admin may override with a reason.

**Escalation:** the worker's poll loop runs an overdue scan each pass. A pending step past
`due_at` and not yet escalated gets `escalated_to` = the assignee's `manager_email` (for a
user assignee), else `role:admin`; `escalated_at` set; audit `approval_escalated`. The original
assignee can still decide it. Nothing is auto-approved.

## Section 3 — API

New router `invoice_processing/api/approval_routes.py`, conventions as `vendor_routes.py`.
Every route gets a probe and a `PROBED` entry in `tests/test_tenant_isolation_api.py`.

| Route | Role | Behaviour |
|---|---|---|
| `GET /api/approvals/mine` | viewer | Current pending steps the caller can decide (assignee, group member, role holder, active delegate, escalation target), each with the invoice summary, step position, due date, overdue flag, and `why` ("member of Finance", "delegate for x", "escalated from y"). |
| `GET /api/invoices/{case_id}/approvals` | viewer | Full chain for one invoice. |
| `POST /api/invoices/{case_id}/actions` | approver | Same contract plus optional `override_reason` (admin only). Approve decides the caller's current step; last step → existing workflow keys written, `invoice_approved` emitted. Reject → current step rejected, rest cancelled, `decision="reject"`, `invoice_rejected`. 403 when the step is not the caller's or a segregation rule applies; 409 when no step is pending (already decided). |
| `GET/POST/PATCH/DELETE /api/approvals/groups[/{id}]` | clerk read, admin write | Group CRUD; members validated against tenant users. |
| `GET/POST/PATCH/DELETE /api/approvals/rules[/{id}]` | clerk read, admin write | Rule CRUD; user/group targets validated. |
| `GET/POST/DELETE /api/approvals/delegations[/{id}]` | approver (own), admin (anyone) | Self-delegation 400; bad range 400. |
| `PATCH /api/admin/users/{user_id}` | admin | `manager_email`, validated as a tenant user. |

Fields endpoint (`PATCH /api/invoices/{case_id}/fields`): re-runs routing when total, vendor
name or customer name changed; response includes `approvals_rerouted: bool`; audit
`approval_rerouted`.

Audit events: `approval_step_decided`, `approval_chain_completed`, `approval_overridden`,
`approval_escalated`, `approval_rerouted`, `approval_group_created/updated/deleted`,
`approval_rule_created/updated/deleted`, `delegation_created/deleted`. Existing
`invoice_approved`/`invoice_rejected` still fire at completion. Add `"approval": "approval"`,
`"delegation": "delegation"` to `_ENTITY_TYPES`.

Without a database: approval routes → 503 (same message as vendor routes); the actions
endpoint keeps today's single-approver behaviour (file-backed mode and e2e-less local use).

## Section 4 — Frontend

- **`/approvals`** (nav `min: "viewer"`, between Invoices and Approved invoices): single
  column of cards, phone-sized; each card: vendor, invoice number, total, step and `why`, due
  date with overdue badge, PDF link, large Approve/Reject (reuse the decision dialog; reject
  needs a note). Approvers also manage their own out-of-office delegation here.
- **Workbench** (`invoices/page.tsx`): an "Approval" panel under findings listing the chain
  (assignee, status, decided by/when, due). Approve/Reject disabled with visible reason when
  the current step is not mine or a segregation rule applies; admins get Override (asks for a
  reason).
- **Admin**: user table gains Manager with inline edit; new `/admin/approvals` page with
  three panels (groups, rules, delegations), list + inline form each.
- Types in `lib/api.ts`: `ApprovalStep`, `MyApproval`, `ApproverGroup`, `ApprovalRule`,
  `Delegation`.
- Browser tests (DB-backed e2e, `X-Dev-Principal`): two-tier amount rule end to end (A decides
  step 1, A refused on step 2, B completes, sheet shows approved); uploader refused; delegate
  sees the delegator's step on `/approvals`.

## Section 5 — Testing, measurement, rollout

Tests (each failing without its change):
- `tests/test_approvals.py` (pure engine): needs-a-person; fixed-order chain; highest tier
  only; duplicate collapse; role fallback; three segregation rules; delegate/escalation
  satisfaction; re-route keeps decided steps.
- `tests/test_approval_repository.py`: step state machine; one current step; completion writes
  existing workflow keys; overdue scan; delegation window.
- `tests/test_approval_routes.py`: every route and role; 403 unsatisfied; override recorded;
  fields re-route; 503 without DB.
- `tests/test_tenant_isolation_api.py`: probes for every new route.
- `tests/test_worker.py`: routing after projection; AUTO below every tier → no steps.
- Migration round-trip; new DB test files on the CI Postgres job.

Measurement: with no rules, replay over `after-ecb9bbd` keeps 40/45 clean posted untouched
and 1 posted wrongly; with the benchmark profile's high-value rule, report (not tune) how
many invoices now need a person, in `benchmark/CHANGELOG.md`.

Rollout: migration + repositories → engine → actions endpoint + completion → worker routing +
overdue scan → configuration routes + probes → frontend → measurement + docs.

## Out of scope

Notifications of any channel; email approve links; business-day calendars; GL-based routing;
parallel steps; per-user approval limits; changing a decided step.

## Verification (end to end, once implemented)

```bash
cd /Users/vineet/Downloads/ADK/invoice-processing
./.venv/bin/python -m pytest tests/ -q -p no:cacheprovider
./.venv/bin/python -m ruff check . && ./.venv/bin/python -m ruff format --check .
bash benchmark/harness/gates.sh
./.venv/bin/python -m benchmark.harness.replay --run after-ecb9bbd --profile benchmark/harness/profiles/after.json --out replay-approvals
```
Manual stack: configure a two-tier rule and a group, upload an invoice over the tier, decide
as two people via the workbench and `/approvals` on a phone-width viewport, see the chain in
the audit trail.
