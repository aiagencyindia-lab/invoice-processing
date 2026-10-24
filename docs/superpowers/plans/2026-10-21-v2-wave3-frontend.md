# Wave 3 Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Wave 3 trust surfaces: the workbench hold banner and trust panel, evidence requests on a finding, the vendor trust profile, trust posture, engine readiness with promote/demote, and the integrations admin (API keys, webhooks).

**Architecture:** `"use client"` pages built from `usePoll` + `api()`, the same way as Wave 2 (`app/findings/**`, `components/trust/**`). Each new component lives in `components/trust/`. Every Wave 3 route is a 503 stub on main until its backend branch merges, so every view renders `ApiError.friendly` in an `.empty-state` and never throws.

**Tech Stack:** Next.js 16, React 19, TypeScript 6, Playwright. No new dependency, no chart library.

**Spec:** `docs/superpowers/specs/2026-10-21-v2-wave3-frontend-design.md`. Response shapes come from the score, reality, provenance and api specs in the same folder, plus the types in `frontend/src/lib/api.ts`.

## Global Constraints

- Owned files only: `app/invoices/page.tsx`, `app/findings/[id]/page.tsx`, `app/vendors/page.tsx`, `app/vendors/[id]/page.tsx`, `app/{trust-posture,trust-engines,integrations}/page.tsx`, `components/trust/{TrustPanel,HoldBanner,DimensionMeter,EvidenceRequestDialog,ReadinessTable,ApiKeysPanel,WebhooksPanel}.tsx`, `e2e/trust-wave3.spec.ts`, and this plan.
- Never edit `lib/api.ts`, `lib/trust.ts`, `AppShell.tsx`, `globals.css`, `golden-path.spec.ts`, `scripts/e2e_api.py`, or any Python. Response envelopes that `api.ts` does not type (`{requests}`, `{documents}`, `{keys}`, `{endpoints}`, `{deliveries}`) are declared locally and were proposed to the parent session for `api.ts`.
- Each view's `h1` equals its NAV label: "Trust posture", "Trust engines", "Integrations". The vendor page's `h1` is the vendor's name.
- Use only existing CSS classes. Visuals are Unicode glyphs, `<meter>`, `<progress>`, and CSS-width bars.
- `can(me, role)` is courtesy gating only: the server decides.

## Review Focus

1. A 503 or 404 from any Wave 3 route renders a sentence, never a crash. The e2e tests every view against main's stubs.
2. `detail.hold` is absent (an older API) or null: no banner, no crash. `hold?.held` is guarded.
3. `detail.trust_node` is null: no trust fetch (`usePoll(null)`), and the panel says "Not scored yet".
4. A dimension value of `null` renders "—" and the dimension's note, not an empty `<meter>` at 0.
5. An API key or webhook secret is shown once only. It lives in component state, is cleared on dismiss, and is never re-fetched.

---

### Task 1: Hold banner and trust panel on the workbench

**Files:** Create `components/trust/{HoldBanner,DimensionMeter,TrustPanel}.tsx`; Modify `app/invoices/page.tsx`.

**Interfaces:**
- `HoldBanner({ hold: HoldDetail | null | undefined, isAdmin: boolean })`: renders nothing unless `hold?.held` is true or `hold.waiting` is non-empty. A `role="alert"` `.panel` lists each hold (type, summary, `Link` to `/findings/{finding_id}`) and the waiting engines (`ENGINE_LABELS`). For an admin it adds the hint "An administrator can override at approval with a reason."
- `DimensionMeter({ name: Dimension, score: DimensionScore })`: label (`DIMENSION_LABELS`), `<meter min=0 max=1 low=.5 high=.8 optimum=1>`, the value as a percentage or "—", a `hold` badge, the note, and drivers as finding links.
- `TrustPanel({ node: string | null })`: `usePoll<TransactionTrust>(node ? /api/trust/transactions/{node}/trust : null)`. Renders six meters in `DIMENSIONS` order, the composite labelled "advisory", `score_version` and `computed_at`. Error → `.empty-state` with the error. Null node → "Not scored yet."
- Export 409: when `detail.hold?.held`, `say(detail.hold.message ?? caught.friendly)`; otherwise keep "Approve the invoice before exporting".

- [ ] Steps: build the components, wire them in after `<Findings>`, put the banner above the fields, run `npx tsc --noEmit`, commit.

### Task 2: Evidence requests on the finding detail

**Files:** Create `components/trust/EvidenceRequestDialog.tsx`; Modify `app/findings/[id]/page.tsx`.

- Dimension chip: the score endpoint gives no per-finding dimension, so the chip reads the finding's own `details.dimension` when present and otherwise derives the dimension from the type through a local map of the Wave 3 table (`REAL-PRICE-JUMP` → price, `REAL-VOLUME-SPIKE` → consistency, `REAL-*` → reality, `PROV-*` → authenticity, `PAY-*`/`GST-*` → identity, `LEAK-*` → price).
- `EvidenceRequestDialog({ finding, onCancel, onDone })`: fields owner (email), due date (`<input type="date">`), note (≤ 500). POST `/api/trust/findings/{id}/evidence-request`. Errors go to `role="alert"`.
- The "Request evidence" button shows when the finding is in `detected`, `triaged` or `needs_vendor_input` and `can(me, "clerk")`.
- Requests: `usePoll<{requests: EvidenceRequest[]}>(/api/trust/evidence-requests?finding_id={id})`, then filtered client-side by `finding_id` as well. Each request shows owner, due date, an `overdue` badge and its status; an open one has a proof `<input type="file">` that POSTs multipart `file` to `/api/trust/evidence-requests/{rid}/proof`. A 503 shows "Evidence requests are not available yet." quietly.

### Task 3: Vendor detail and list link

**Files:** Modify `app/vendors/page.tsx` (the name cell links to `/vendors/{id}`); fill `app/vendors/[id]/page.tsx`.

- Find the row in `/api/vendors` by `id`, then fetch `usePoll<VendorTrustProfile>(row.node ? /api/trust/vendors/{node}/trust : null)` and `usePoll<{documents}>(/api/vendors/{id}/documents)`.
- Profile tiles (`.tiles` / `.tile`): tax ID, kind, bank status, GST standing. Then the six dimension means as meters, findings by type (table), KYC documents with scores plus an upload (subkind select: gst_certificate, pan, udyam, cancelled_cheque, abn), network links, and the monthly composite as CSS bars.
- `h1` = vendor name. Unknown id → "No such vendor."

### Task 4: Trust posture

**File:** `app/trust-posture/page.tsx`. `since` chips (30 / 90 / 365 days / all time) in the URL as `?days=`. `since` = an ISO date computed client-side. Money tiles: found, prevented, recovered, written off, open exposure. Then the by-type and by-vendor tables, pre-payment share as a `<progress>`, provenance buckets as CSS bars, the three coverage meters, and hold counts.

### Task 5: Trust engines

**Files:** `components/trust/ReadinessTable.tsx`, `app/trust-engines/page.tsx`. An admin-only courtesy (`can(me,"admin")`). `ReadinessTable({ engines, onChange })` has one row per engine: engine, mode badge, since, days, findings, would-have-held, held, open holds, precision, failures, recommendation, then Promote and Demote buttons. Promote is the next mode in `off → shadow → advisory → enforcing`; Demote is the previous one. A dialog asks for a reason (required) and POSTs `/api/trust/engines/{engine}/mode {mode, reason}`; the response row replaces that engine's row through a reload.

### Task 6: Integrations

**Files:** `components/trust/{ApiKeysPanel,WebhooksPanel}.tsx`, `app/integrations/page.tsx`.
- `ApiKeysPanel`: list `{keys}`; a create form (name, role clerk|approver|viewer) → shows `key` once in a `<code>` with a Copy button and a "Done" that clears it; Revoke (confirm) → DELETE.
- `WebhooksPanel`: list `{endpoints}`; a create form (an `https://` URL validated client-side, event checkboxes from `finding.created, finding.decided, hold.placed, hold.lifted`, description) → the secret shown once; per endpoint, Test (POST `/test`) and Deliveries (expands a table from `/deliveries`).

### Task 7: e2e `trust-wave3.spec.ts`

Against main's seed and stubs. Every assertion that needs a backend route is gated on a direct probe (`page.request.get(...)` returning 200), with `test.skip(!ready, "…route not built yet")`, so the same spec turns real after the rebase. The probes must be made with the dev-principal header the test uses.
1. The workbench on `case_005`: no hold banner with every mode off; trust panel → six meters (gated on the transaction trust route).
2. Finding `REAL-NO-EVIDENCE` → "Request evidence" → fill the dialog → the request is listed (gated on evidence requests).
3. `/vendors` → click "QuickFix HVAC Repairs" → `h1` is the name (always); profile tiles (gated).
4. `/trust-posture`: `h1` (always); tiles (gated).
5. `/trust-engines` as admin: `h1` (always); a recommendation, then demote/promote reality with a reason (gated).
6. `/integrations` as admin: create a key, the key is shown once, revoke it (gated).

### Task 8: Verify, review, PR

`npm ci && npm run lint && npx tsc --noEmit && npm run build && npm run test:e2e`; `uv sync --frozen && ./.venv/bin/python -m pytest -q -p no:cacheprovider`. Then a fable whole-branch review, one fix pass, the rulings in `.superpowers/sdd/2026-10-21-v2-wave3-frontend/progress.md`, push, and the PR.
