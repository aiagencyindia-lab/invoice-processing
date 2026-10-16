# Wave 2 Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use checkbox syntax.

**Goal:** The findings queue, finding detail (Why / Evidence / Lineage / History + transitions), the Trust Audit workspace and the clause editor, coded against the scaffold's `api.ts` types.
**Architecture:** Five `"use client"` pages over `usePoll` + `api()`; four small components under `components/trust/`; every non-scaffold route (`/api/trust/audits*`, `.../explanation`, `.../evidence-pack`, `.../rating`) degrades to an `.empty-state` carrying the error text, because those routes land on sibling branches.
**Tech stack:** Next.js 16 app router, plain CSS from `globals.css`, Playwright.
**Spec:** `docs/superpowers/specs/2026-10-16-v2-wave2-frontend-design.md`.

## Global constraints
- Ownership: only `frontend/src/app/{findings,trust-audit,contracts}/**`, new files in `frontend/src/components/trust/`, `frontend/e2e/trust.spec.ts`, this plan. No edits to `api.ts`, `trust.ts`, `AppShell.tsx`, `globals.css`, `EvidenceViewer.tsx`, `golden-path.spec.ts`, Python.
- Role gating mirrors `_role_for`: `MONEY_STATUSES` → `can(me, "approver")`, else `can(me, "clerk")`. Clause editing → approver.
- No new npm dependencies, no Tailwind, Unicode glyphs only.

## Review focus (riskiest untested inputs)
1. A 409 on transition whose `detail` is `{current, message}`: the dialog must read `detail.current`, say "moved to X", and reload — not render `[object Object]`.
2. `amount` typed with a comma or blank on a `LEDGER_STATUSES` transition: blank must send no `amount` (the server prefills from impact); anything else is sent verbatim and the server's 400 is shown.
3. A finding with `lineage: []` (node missing) must still render the seven stage cells, all "no evidence".
4. The audit run poll (`usePoll(run, 3000)`) must stop when the run is `done`/`failed`, and a 404 from the audit routes must not loop error toasts.
5. The clause editor PATCH may return a *new* `node_id` (re-versioned clause); the row must re-key on it, or a second Save would hit the stale id.

## Tasks
- [ ] **F1** Plan committed.
- [ ] **F2** `components/trust/{FindingRow,TransitionDialog,LineageChain,RatingButtons}.tsx`.
- [ ] **F3** `app/findings/page.tsx`: filters in the URL (`status|open|type|engine|vendor|hold|since|until`), summary tiles, table of `FindingRow`, quick Triage, "Payment Trust queue" toggle.
- [ ] **F4** `app/findings/[id]/page.tsx`: header + transitions, tabs Why / Evidence / Lineage / History, evidence pack HTML/JSON buttons.
- [ ] **F5** `app/trust-audit/page.tsx` (list + create form) and `app/trust-audit/[id]/page.tsx` (Overview / Sampling / Report / Ledger / Contracts).
- [ ] **F6** `app/contracts/[document_id]/page.tsx`.
- [ ] **F7** `e2e/trust.spec.ts`: `the findings queue lists the seeded finding`, `a finding can be read, cited and triaged`, `the explanation degrades without the Why engine`, `the trust audit page offers the engagement form`.
- [ ] **F8** `npm run lint && npx tsc --noEmit && npm run build && npm run test:e2e`; push.
