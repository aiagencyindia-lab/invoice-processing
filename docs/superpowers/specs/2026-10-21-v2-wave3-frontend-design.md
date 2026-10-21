# Wave 3 — Frontend surfaces (FR-30, PRD §8) design (`v2/w3-frontend`)

**Program plan:** `docs/superpowers/plans/2026-10-21-v2-wave3-program.md`. **Scaffold contracts:** `docs/superpowers/specs/2026-10-21-v2-wave3-scaffold-design.md` (types in `lib/api.ts`, NAV entries, placeholder pages, e2e seed). Builder's call B8 (one frontend branch; Settings keeps raw JSON). Conventions as Wave 2: `"use client"` pages with `usePoll` + `api()`, `ApiError.friendly`, `can(me, role)` courtesy gating, the existing CSS classes, Unicode glyphs, no chart library (`Tile.tsx`, `<meter>`, `<progress>`, CSS bars).

**Owns:** `frontend/src/app/invoices/page.tsx` (edit), `frontend/src/app/findings/[id]/page.tsx` (edit), `frontend/src/app/vendors/page.tsx` (link to detail), `frontend/src/app/vendors/[id]/page.tsx`, `frontend/src/app/trust-posture/page.tsx`, `frontend/src/app/trust-engines/page.tsx`, `frontend/src/app/integrations/page.tsx` (fill the placeholders), new `frontend/src/components/trust/{TrustPanel,HoldBanner,DimensionMeter,EvidenceRequestDialog,ReadinessTable,ApiKeysPanel,WebhooksPanel}.tsx`, `frontend/e2e/trust-wave3.spec.ts`, its plan doc. Must not touch `lib/api.ts`, `lib/trust.ts`, `AppShell.tsx`, `globals.css`, `golden-path.spec.ts`, any Python.

## Views

| Route | Data | UI |
|---|---|---|
| `/invoices` (workbench) | `detail.hold`, `detail.trust_node` → `usePoll(/api/trust/transactions/{node}/trust)` | `HoldBanner`: open holds (type, summary, link to `/findings/[id]`), engines still `waiting`, and for admins the "override at approval with a reason" hint; `TrustPanel`: six `DimensionMeter`s (`<meter>` 0–1, hold badge, drivers list), versions, composite labelled "advisory"; the export 409 now shows the hold message instead of "Approve the invoice before exporting" |
| `/findings/[id]` | the finding's dimension, `/api/trust/evidence-requests?…` | dimension chip; "Request evidence" (`EvidenceRequestDialog`: owner, `<input type="date">`, note) when the status allows; proof upload (multipart) on an open request; overdue badge |
| `/vendors` | `Vendor.node` | each row links to `/vendors/[id]` |
| `/vendors/[id]` | `/api/vendors` row, `/api/trust/vendors/{node}/trust`, `/api/vendors/{id}/documents` | profile tiles, findings by type, GST standing, KYC documents with authenticity scores + upload, network links, monthly composite as a CSS bar list |
| `/trust-posture` | `/api/trust/posture?since` | money tiles, by-type / by-vendor tables, pre- vs post-payment share (`<progress>`), provenance buckets, coverage meters; `since` chips like the KPI page |
| `/trust-engines` (admin) | `/api/trust/readiness` | `ReadinessTable` per engine (mode, since, evidence columns, recommendation) with a Promote / Demote dialog (reason required) → `POST /api/trust/engines/{engine}/mode` |
| `/integrations` (admin) | `/api/integrations/api-keys`, `/api/integrations/webhooks` | `ApiKeysPanel`: create (key shown once with copy), revoke; `WebhooksPanel`: endpoint form (https url, event checkboxes), test, deliveries list |

Every view keeps its `h1` equal to the NAV label (the scaffold added the labels to the e2e nav list and `VIEWS`).

## e2e (`frontend/e2e/trust-wave3.spec.ts`, against the scaffold seed)

The workbench shows the hold banner and six dimensions for `case_005` → finding detail "Request evidence" → `/vendors/[id]` loads with the profile → `/trust-posture` tiles → `/trust-engines` shows a recommendation; demote/promote is audited → `/integrations` creates and revokes a key. `tests/test_frontend_reaches_the_flow.py` (scaffold-owned) already asserts the pages exist; this branch adds nothing there.

**Done:** `npm run lint`, `npx tsc --noEmit`, `npm run build`, Playwright golden path + `trust.spec.ts` + `trust-wave3.spec.ts` green; the Python suite still passes (the reach test greps frontend source).
