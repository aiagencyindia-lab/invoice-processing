# Wave 2 — Findings and Trust Audit frontend design

**Program plan:** `docs/superpowers/plans/2026-10-16-v2-wave2-program.md` (decisions, ownership, execution). **Contracts:** `docs/superpowers/specs/2026-10-14-v2-trust-layer-design.md` §3.1.

### C. Frontend (FR-30 part, §8) — `frontend/src/app/{findings,trust-audit,contracts}/**`

| Route | Data | Actions |
|---|---|---|
| `/findings` | `usePoll(/api/trust/findings?…)` + `/summary`; URL filters; hold toggle labelled "Payment Trust queue" | row → detail; quick "Triage" |
| `/findings/[id]` | detail, `/explanation`, pack on demand | `TransitionDialog` (reason required for rejected/written_off; amount prefilled with impact for recovered/prevented; 409 → "moved to X, reload"); tabs Why (sections + correct/useful rating), Evidence (`EvidenceViewer`: steps left, source right), Lineage (`.chain`, seven stages, badges), History (events); "Evidence pack" HTML/JSON |
| `/trust-audit` | `/api/trust/audits` | create form (`<input type="date">`, vendor multi-select from `/api/vendors` mapped to nodes, categories, terms, approver) |
| `/trust-audit/[id]` | audit; `usePoll(run, 3000)` while active | tabs Overview (scope, terms, progress bar, per-engine chips, Run batch, interim count → `/findings?since=`), Sampling (draw N, ✓/✗ rows, precision tile), Report (tables + `Tile`s, Print view, Download JSON), Ledger (rows, fee card, CSV/JSON), Contracts |
| `/contracts/[document_id]` | clauses + `objectUrl(file)` | type select, params inputs, Save (PATCH), iframe `#page=`, reviewed badge |

e2e `frontend/e2e/trust.spec.ts`: seeded `LEAK-RATE` row → Why shows what_happened + ≥1 evidence → triage → create engagement → Run batch shows queued → sample of 1 ✓ → precision 100% → Report renders "Executive summary" → Ledger has CSV. Pytest adds `test_findings_can_be_triaged`, `test_an_engagement_can_be_created`.
