# Controller approval-chain step Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An approval rule can name a `role:controller` step that only a Controller can decide.

**Architecture:** One predicate `approvals.fills(role, seat)` (exact for `controller`, the rank ladder otherwise) used by `satisfaction` for role steps and role escalations. Rule save refuses a controller step when the tenant has no active Controller; the admin user patch refuses removing the last active Controller while a rule or a pending step needs one.

**Tech Stack:** Python, FastAPI, SQLAlchemy, pytest.

**Spec:** design agreed in chat 2026-10-25 (PENDING "A real `role:controller` approval-chain step"); follows `docs/superpowers/specs/2026-10-24-controller-role-design.md`.

## Global Constraints

- A `role:controller` step is decided only by `role == "controller"`; approver and admin never fill it, not even after escalation (user's choice: overdue controller steps stay Controller-only; admins are still notified).
- No other step's behaviour changes: other role steps keep the ladder (a controller still fills `role:approver`).
- Uploader and one-person-per-step refusals unchanged.
- No frontend change (the rule editor's step value is free text).

## Review Focus

- Two admins demoting the last two Controllers at the same moment can leave none (check-then-write, no lock) — accepted ceiling, marked `ponytail:`; escalation still notifies admins.
- A controller step whose only Controller uploaded the invoice cannot be decided — same as any one-person seat today; not handled.
- A deactivated Controller does not count as the tenant's Controller (rule save and last-Controller check use active users only).
- An overdue controller step escalated to `role:admin` must not appear in the admin's queue as decidable.

---

### Task 1: Controller-only role steps (pure)

**Files:** Modify `invoice_processing/approvals.py`; Test `tests/test_approvals.py`.
**Produces:** `fills(role: str, seat: str) -> bool` in `approvals.py`.

- [ ] Failing tests: `test_only_a_controller_fills_a_controller_seat` (fills: controller→controller True; admin, approver → controller False; controller → approver True; admin → approver True; clerk → approver False); `test_a_controller_step_is_decided_by_a_controller_only` (satisfaction on `{"assignee_kind":"role","assignee_value":"controller"}`: controller → "has the controller role", admin/approver → None); `test_an_escalated_controller_step_stays_controller_only` (same step with `escalated_to="role:admin"`: admin → None).
- [ ] Run, expect FAIL; implement `fills`, use it in both role branches of `satisfaction`; run, expect PASS.

### Task 2: Rule save and the last Controller

**Files:** Modify `invoice_processing/api/approval_routes.py` (`_check_rule`), `invoice_processing/api/admin_routes.py` (`patch_user`), `invoice_processing/db/repositories.py` (`PgApprovalRepository.has_pending(tenant_id, kind, value) -> bool`), `README.md` (roles paragraph), `docs/PENDING.md` (tick); Test `tests/test_approval_routes.py`.

- [ ] Failing tests (replace `test_a_controller_role_step_is_refused`):
  - `test_a_controller_role_step_needs_an_active_controller`: no controller → 400 mentioning "controller"; inactive controller only → 400; active controller → 201.
  - `test_only_a_controller_decides_a_controller_step`: rule `[role:controller]` on the case; `admin:boss` and `approver:a` → 403/409 not decided; `controller:cora` → 200 and decision approve.
  - `test_the_last_controller_cannot_be_removed_while_a_rule_needs_one`: with that rule, admin PATCH cora `{"role":"approver"}` → 409; `{"is_active":false}` → 409; with a second active controller → 200.
  - `test_the_last_controller_cannot_be_removed_while_a_step_waits`: rule deleted after a pending `role:controller` step exists → still 409.
  - `test_the_last_controller_can_go_when_nothing_needs_one`: no rule, no step → 200.
- [ ] Run, expect FAIL; implement: `_check_rule` refuses with "There is no active controller, so nobody could approve a controller step." when no active user has role controller; `patch_user`, before writing, when `current` is an active controller and the change makes them not an active controller, counts other active controllers and, if none, refuses 409 "{email} is the only controller and a rule or a waiting step needs one; make someone else a controller first." when any live rule step is role:controller or `has_pending(t, "role", "controller")`. `ponytail:` comment names the concurrent-demotion ceiling.
- [ ] Run, expect PASS; README: "a `role:controller` chain step is decided only by a controller (never an admin, even when it is overdue)"; tick PENDING; full suite, ruff check + format (unpiped), Postgres suites on a fresh DB; commit.
