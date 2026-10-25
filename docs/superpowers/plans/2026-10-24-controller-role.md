# Controller role Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `controller` role (ranked with approver; never admin) that alone approves or rejects agent registrations, which now start `proposed`.

**Architecture:** One more key in the shared `ROLE_RANK`; an exact-role dependency for the Controller-only power; `proposed` / `rejected` agent statuses enforced in `rules.mandate_active`; two Controller routes; the Agents page and admin role pickers learn the role.

**Tech Stack:** Python 3.11 FastAPI, SQLAlchemy 2, pytest; Next.js 16, Playwright.

**Spec:** `docs/superpowers/specs/2026-10-24-controller-role-design.md`.

## Global Constraints

- No migration (roles and agent statuses are free strings).
- API keys never hold `controller` (`apikeys.ROLES` unchanged).
- No existing approver or admin permission changes; the benchmark's fleet keeps active agents (`store.register` default `status="active"`).
- Ruff as CI: `uv run ruff check . && uv run ruff format --check .`

## Review Focus

- An admin calling approve → 403 (the anti-goal). Pinned in Task 2.
- An API key whose role field were somehow `controller` → 403 from `require_exact_role` (keys refused outright). Pinned in Task 1.
- A rejected agent's key registered again → 201. Pinned in Task 2.
- A proposed agent acting through the log import → BLOCK "not approved yet". Pinned in Task 2.
- A controller in a `role:approver` chain step can decide it. Pinned in Task 1.

---

### Task 1: The role

**Files:** `api/auth.py` (`ROLE_RANK += controller: 2`, `require_exact_role`), `approvals.py` (import `ROLE_RANK` from auth), `api/approval_routes.py` (`APPROVING` → rank check; refuse `role:controller` steps), `admin/provision.py` (manager rank check), `trust/provenance/__init__.py` (`ROLE_CHANNEL`), `api/admin_routes.py` (Literal + roles list); tests `tests/test_auth.py`, `tests/test_approval_routes.py`, `tests/test_admin_api.py`, `tests/test_provisioning.py`.

- [ ] Failing tests: rank (controller can approver, not admin); `require_exact_role("controller")` on a tiny route: controller 200; admin, approver 403 audited; an API-key principal with role controller 403; a controller decides a `role:approver` step; a controller can be a user step and a delegate; `role:controller` step → 400; provisioning accepts controller as a manager; admin can create/patch a controller, roles list includes it.
- [ ] Implement; run those files plus the whole approval/admin/auth suites; commit.

### Task 2: Agent registration approval

**Files:** `trust/agents/store.py` (`register(status=)`, `approve_registration`, `reject_registration`), `trust/agents/registry.py` (register → proposed; `approve`, `reject`), `trust/agents/rules.py` (`mandate_active`), `api/trust_agent_routes.py` (routes, list filter, propose message), `docs/API_V2.md`, `scripts/e2e_api.py` (seed `cora@e2e.test` controller); tests `tests/test_trust_agent_routes.py` (helpers approve; new tests; PROBED), `tests/test_trust_agents_rules.py`, `tests/test_trust_api_routes.py` if `/v2/agents` register asserts status.

- [ ] Failing tests per the spec's Tests bullet for agents.
- [ ] Implement; run agent + api-route + isolation suites; commit.

### Task 3: Frontend

**Files:** `frontend/src/lib/api.ts` (`ROLE_RANK`, `isController`), `frontend/src/app/admin/page.tsx` (fallback role lists), `frontend/src/app/agents/page.tsx` (badges, Approve/Reject), `frontend/e2e/agents.spec.ts`.

- [ ] e2e first: the agent shows "awaiting Controller", no Approve for the admin, Cora approves → active; existing flow continues.
- [ ] Implement; lint, tsc, build, e2e; commit.

### Task 4: Close-out

- [ ] `README.md` paragraph; full suite; Postgres trust + isolation + approvals; eval 56/56; fable review; one fix pass; PR; merge on the user's word.
