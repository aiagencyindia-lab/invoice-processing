# Wave 5 branch A — Agents screen Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (native: one implementer, one fresh review at the end). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface the Wave 4 agent control plane: the `/agents` page, the propose-mandate dialog, two dashboard tiles and the shell block banner.

**Architecture:** Client pages on the existing skeleton (`usePoll` + `api()`, `can(me, role)`), existing CSS classes only, no new library. Every endpoint exists on `main` after the scaffold (#100). Tests are Playwright specs against the real e2e server (`scripts/e2e_api.py`), seeded through the API inside the spec.

**Tech Stack:** Next.js (app router, client components), TypeScript, Playwright.

**Spec:** `docs/superpowers/specs/2026-10-23-v2-wave5-screen-design.md` (with the program plan B3, B10, Part 2 row A, and the scaffold spec).

## Global Constraints

- Owns: `frontend/src/app/agents/page.tsx`, `frontend/src/components/trust/AgentMandateDialog.tsx` (new), `frontend/src/app/dashboard/page.tsx` (two tiles), `frontend/e2e/agents.spec.ts` (new). May touch: `AppShell.tsx` banner block only; `globals.css` append-only; `tests/test_frontend_reaches_the_flow.py` append-only; the agent types at the end of `lib/api.ts` only to correct a shape. No backend change.
- h1 "Agents"; page for approver+ ("Agents are for approvers and above."); write controls only for `can(me, "admin")`.
- Tiles `data-tile`: `blocks` "Agent blocks", `reviews` "Needs review", `agents` "Agents", `approved` "Approved this month". Hint "last 24 h" from `window_hours`.
- Money maps per currency joined with " · " via `money()` from `lib/trust`.
- Banner copy: `⚠ {blocked} agent action(s) blocked and {reviews} need review in the last {window_hours} h · Agents` (link to `/agents`), `role="status"`, after the paused banner, polled every 60 s only for approver+.
- Dashboard tiles "Agent blocks" / "Agent reviews", `href="/agents"`, hidden below approver.
- Playwright `workers: 1`; e2e count 47 → ≥ 49.

## Review Focus

- Self-approval: the proposer's Approve is disabled with "A different admin approves"; the server's 403 sentence still shows if it is reached (e2e: the button is disabled for the proposer; the API answer is asserted in the spec).
- An agent with no mandate, or an expired/unverified one, must not read as healthy (badge-warn "none" / "expired" / "unverified").
- A ledger with several currencies must not show "[object Object]" (joined text).
- A 409 on the log import (engine off) shows the server sentence and a link to Trust engines (e2e).
- A count request that fails (viewer, 403) hides the banner rather than showing an error (banner only polled for approver+).

## Deviation (test only)

The spec's e2e sends `PUT /api/vendors` with the approver key. That route needs admin, and an API key can be at most approver (`trust/apikeys.ROLES`), so the role check refuses it before the guard runs and no block is recorded. The spec instead sends `POST /api/vendors/{id}/bank-changes` (clerk+) with the key: `bank_change.propose` is outside the mandate (`vendor.create` only), so enforcing refuses it 403 and records an inline BLOCK. Nothing is written.

---

### Task 1: E2E spec (failing first)

**Files:** Create `frontend/e2e/agents.spec.ts`.

- [ ] Helpers `ADMIN`, `SECOND = "it@e2e-sso.test:admin"`, `goto`, `as`, `call(page, method, path, who, data?)`.
- [ ] `test.describe.serial` with a `beforeAll`-style first test seeding: create key `agents-e2e-bot` (approver), register agent `E2E bot` (owner `dev@localhost`, principal `{api_key_id}`), propose mandate (`vendor.create`, INR, caps `{}`, now → +30 d).
- [ ] Test "the proposer cannot approve its own mandate": `/agents` → open row → Approve button disabled with hint "A different admin approves"; API approve as ADMIN → 403 "A mandate needs a second person to approve it."; approve as SECOND → 200; reload → Agents row shows "v1", "verified"; panel lists the mandate.
- [ ] Test "a refused agent action raises the banner and the blocks tile": baseline count; `agents` → enforcing; `POST /api/vendors/{id}/bank-changes` with `X-API-Key` → 403; reload `/agents` → banner visible, `[data-tile='blocks']` reads baseline+1; dashboard tile "Agent blocks" too; `finally` mode → off.
- [ ] Test "the log import explains an engine that is off": upload a CSV → alert with "Agent control is off" and a link "Trust engines".
- [ ] Run `npm run build && npx playwright test e2e/agents.spec.ts` → FAIL (stub page).

### Task 2: Page, dialog, tiles, banner

**Files:** `agents/page.tsx`, `components/trust/AgentMandateDialog.tsx`, `dashboard/page.tsx`, `AppShell.tsx` banner block.

- [ ] `AgentMandateDialog({ agent, onClose, onDone })`: fields per spec §4; body `{scope:{actions, currency, counterparties, caps}, valid_from, valid_to, reason?}`; amounts sent as strings; errors via `ApiError.friendly`.
- [ ] Page per spec §1–6: tiles, Agents table with filter chips, Spend by owner, register dialog (exactly one principal shape), agent panel (mandates, ledger, actions), action dialogs (approve / revoke mandate / revoke agent with reason), log import.
- [ ] Dashboard: `usePoll<Me>("/api/me")`, count polled only for approver+, two tiles.
- [ ] AppShell: banner per Global Constraints.
- [ ] Run the spec → PASS; then lint, tsc, build, full e2e, the Python frontend test, ruff.
- [ ] Commit.
