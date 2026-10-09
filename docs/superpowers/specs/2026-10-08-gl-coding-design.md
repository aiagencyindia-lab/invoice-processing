# Non-PO GL auto-coding — design (written after the build)

Phase 3, sub-project 3. Merged as PR #8 (`f6cf348`), follow-ups on
`fix/approvals-mine-and-utc`. The user asked for implementation to start within
five minutes, so brainstorming was skipped and the design went straight into
the plan (`docs/superpowers/plans/2026-10-08-gl-coding.md`). This file records
the design and the rulings that were made without a brainstorm, so the next
person does not have to reconstruct them from commits.

## What it does

- **Rules, per tenant** (`gl_rules`, RLS): match on vendor (normalised like PO
  matching), a casefolded keyword in the line description, or both, mapping to
  a GL account. Precedence vendor+keyword > keyword > vendor-only; the earliest
  rule wins a tie. Admins write, clerks read (`/admin/gl-rules`).
- **Coding** (pure `gl_coding.code_lines`): the worker codes every line after
  correction rules and before projection, into the `gl_coding` artifact;
  `case_projection.lines` fills `invoice_lines.gl_account` from it.
- **Corrections**: `PATCH /api/invoices/{id}/lines/{n}/gl` (clerk+) writes a
  `manual` line, audited `gl_line_coded`, re-projects and re-routes. Closed
  once the invoice is decided (409). An admin's "remember for this vendor"
  creates or updates that vendor's vendor-only rule.
- **Approvals**: rule kind `gl` (`gl_prefix`); chain order vendor → entity → gl
  → amount. A hand-coder counts as an editor, so cannot be the sole approver.
- **Exports**: Xero `AccountCode` per line; the per-invoice CSV is one row per
  line with that line's account; the Export centre has a `GL accounts` column.
  GL attaches to export lines only when extraction and projected lines pair
  exactly (count + description); otherwise none, audited `gl_export_unpaired`.

## Rulings

1. **Deterministic rules only, no LLM suggestion** — coding must be explainable
   like every other decision here — cost if wrong: low rule coverage leaves
   lines uncoded; add an LLM-suggested source once a design partner's coverage
   is measured.
2. **Informational: an uncoded line never holds or rejects** — the AUTO rate is
   what a pilot is judged on, and a tenant with no rules would otherwise send
   everything to review — cost if wrong: uncoded lines reach export; the
   Xero default account catches them.
3. **All lines are coded, PO or not** — PO-line inheritance needs GRN/3-way
   match — cost if wrong: PO invoices are coded by rules until then.
4. **`gl` approval rules shape the chain but do not pull a touchless invoice to
   a person** — same as vendor/entity rules — cost if wrong: GL-owner review is
   not mandatory; a recode that drops the GL step is audited
   (`gl_line_coded` + re-route), not blocked.
5. **A manual line survives every re-run; a decided invoice is never re-coded**
   — the account an approver signed off is the one exported.
6. **`remember` updates an existing vendor-only rule instead of adding one** —
   a second one would lose the tie to the older rule and never code anything.
7. **Artifact writes are atomic (`CaseRepository.update_artifact`)** — two
   people coding different lines, or a PATCH during a worker run, must both
   land. Postgres: `SELECT … FOR UPDATE`; SQLite: `BEGIN IMMEDIATE`; files:
   `flock`.
8. **Per-invoice CSV became one row per line** (the plan said "a column per
   line"; the first build joined accounts into one cell) — AP bill imports are
   per line, and a joined cell cannot say which line went where.

## Process deviations (from HANDOFF10 §4)

Tasks ran in parallel in separate git worktrees (the shared-checkout risk the
one-at-a-time rule guards against does not apply), with one whole-branch review
and one fix wave instead of a review per task, and no SDD ledger. An
independent audit afterwards ran 38 live API checks against a fresh DB-backed
stack (all pass) and found three low defects, fixed on the follow-up branch.
