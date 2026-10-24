# Controller role and agent registration approval (G2)

**Why:** PRD v2.0 §4 says "agent registration requires Controller approval". Today one admin registers an agent and it is active at once (Wave 5 surfaced this as G2). The user chose (2026-10-24) a **real Controller role** that is a **separate finance role**: it ranks with approver for everything that exists, and it alone approves agent registrations — admins cannot stand in for it. Separation of duties: IT admins register agents, a finance Controller signs them off, neither can do both.

**Goals:** a `controller` user role; agents registered as `proposed` until a Controller approves; nothing an unapproved agent attempts succeeds. **Non-goals:** Controller approval of mandates or bank changes (mandates stay two-admin); `role:controller` approval-chain steps; controller API keys; SSO auto-provisioning as controller. **Anti-goals:** an admin, an API key or an approver able to approve a registration; any existing approver or admin permission changing.

## The role

- `controller` joins `ROLE_RANK` at rank **2** (same as `approver`) in `invoice_processing/api/auth.py`. `invoice_processing/approvals.py` stops keeping its own copy and imports `ROLE_RANK` from `auth`. `frontend/src/lib/api.ts` `ROLE_RANK` gains `controller: 2`. So `require_role("approver")` / `can("approver")` pass for a controller and `require_role("admin")` does not; a controller satisfies `role:approver` chain steps and never takes a `role:admin` escalation.
- The exclusive power is an exact-role check, never the ladder: `auth.require_exact_role(role)` — a dependency that passes only when `principal.role == role` and the principal is not an API key (`principal.user_id` does not start with `api-key:`), and audits a refusal the way `require_role` does (`access_denied` with `required_role`/`actual_role`). Used as `Controller = Annotated[Principal, Depends(require_exact_role("controller"))]`.
- API keys never hold it: `trust/apikeys.ROLES` stays `("viewer", "clerk", "approver")` (Wave 4 U8).
- SSO domain `default_role` stays viewer/clerk (no auto-provisioned controller). Roles are free strings in `users.role` — no migration.

## Places that must learn the role

- `api/admin_routes.py`: `UserPatch.role` Literal adds `"controller"`; the `roles` list returned to the UI adds it. `UserCreate` already validates through `provision.validate_role` (auto).
- Hard-coded "can approve" lists become rank checks (`ROLE_RANK.get(r, -1) >= ROLE_RANK["approver"]`): `api/approval_routes.py` `APPROVING` (user steps and delegates) and `admin/provision.py` manager check.
- `approval_routes` role-step validation refuses `role:controller` with "A controller step is not supported; name the controller as a user step." (the ladder would let approvers and admins satisfy it — my call; a real controller step is a follow-up if wanted).
- `trust/provenance/__init__.py` `ROLE_CHANNEL`: `controller` scores like `approver`.
- Frontend: role pickers in `frontend/src/app/admin/page.tsx` (fallback lists) offer Controller; `api.ts` adds `isController(me)` (`me.role === "controller"`).
- `README.md`: one paragraph on the role and what only it can do.

## Agent registration approval

- `store.register(..., status="active")` gains a `status` parameter (default unchanged, so the benchmark's simulated fleet and direct-store tests keep active agents). `registry.register` passes `status="proposed"`: every registration through `POST /api/trust/agents` and `POST /v2/agents` starts **proposed**.
- `POST /api/trust/agents/{agent_id}/approve` (Controller): proposed → `active`, recorded in the audit event `agent_approved {name, owner}` (no new column, no migration). Anything but proposed → 409 "Only a proposed agent can be approved." Approval by the person who registered it (`registered_by` casefold-equal to the caller's email) → 403 — a belt-and-braces check; one person holds one role, but the row says who registered (my call).
- `POST /api/trust/agents/{agent_id}/reject` (Controller, body `{reason}` required): proposed → status **`rejected`** (a distinct value, so the UI and list tell it from a revoked agent), `revoked_at`/`revoked_by`/`revoke_reason` stamped, and **`principal_id` and `principal_email` cleared** so the API key or user can be registered again (the agent never acted; `agents` has a unique `(tenant_id, principal_id)`) — my call. Audit `agent_rejected {name, reason}`. Anything but proposed → 409. Admins keep the existing revoke route for active agents.
- Every path an agent acts through (inline guard, log import, `/v2/agents/actions` intake, evaluate) runs `rules.RULES`, so the proposed/rejected block in `mandate_active` covers them all. A `rejected` agent is blocked like a revoked one ("{name} was rejected."). Existing agents: none exist in any database (Wave 4/5 tables never deployed), so no backfill.
- While proposed: `propose_mandate` refuses (it already refuses any status but active; message becomes "Only an active agent takes a mandate."); `rules.mandate_active` blocks with "{name} is not approved yet." before the no-mandate message; `GET /api/trust/agents?status=` accepts `proposed` and `rejected`. The provisioning CLI (`--role choices=sorted(ROLE_RANK)`) gains `controller` automatically — intended.
- Both routes are in `tests/test_trust_agent_routes.PROBED` with two-tenant probes (an other tenant's controller gets 404). Not added to `/v2` (a person's act, as mandate approval is).
- `docs/API_V2.md` Agents section: a registered agent starts `proposed`; a Controller approves it on `/api`.

## Frontend

- Agents page: a `proposed` status badge (`badge-warn`, "awaiting Controller") and a `rejected` badge (`badge-neutral`); for a controller, **Approve** and **Reject** buttons on proposed rows (Reject opens the existing reason-dialog pattern); for everyone else a hint "A Controller approves new agents." The Agents NAV entry stays `min: "approver"`, so controllers see it.
- `scripts/e2e_api.py` seeds a controller user `cora@e2e.test` (role `controller`).
- `frontend/e2e/agents.spec.ts`: after register, the agent shows "awaiting Controller"; as the admin there is no Approve button; as `cora@e2e.test:controller` Approve → active; the existing mandate steps follow.

## Tests

- `tests/test_auth.py`: rank — controller passes approver, fails admin; `require_exact_role("controller")` passes a controller, refuses an admin, an approver and an API key (audited).
- `tests/test_approval_routes.py` / provisioning tests: a controller can be a user step, a delegate and a manager; a `role:controller` step is refused; a controller satisfies a `role:approver` step.
- `tests/test_admin_api.py`: an admin can set and create a controller; the roles list includes it.
- `tests/test_trust_agent_routes.py`: register → proposed; approve by a controller → active and audited; approve by an admin / approver / API key → 403; approve twice → 409; reject with reason → `rejected`, principal cleared, and the same API key registers again; approval by the registrant → 403; a rejected agent's evaluate → BLOCK "was rejected"; mandate proposal on a proposed agent → 409; an evaluate on a proposed agent → BLOCK "not approved yet"; list `?status=proposed`; two-tenant probes. Existing route-test helpers approve the agent (as a controller) right after registering.
- Fakes that whitelist role tokens accept `controller` where a test needs it.
- Gates: full suite, ruff, Postgres trust + isolation, eval `--seed 7` 56/56 (fleet unaffected), frontend lint/tsc/build, e2e.

## Left to the builder

Copy of the hint texts; whether Approve/Reject sit in the row or the agent panel (row, unless it crowds the table).
