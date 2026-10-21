# Pending work

Everything paused or unfinished. Keep this list current: tick nothing off
until it is done and verified, and move finished items to "Done" with the
date. Last updated 2026-10-14.

## Production (Railway) — PAUSED

Project `ledgerly` (`b2683d9c-cc99-44fc-a2c4-d271487f1181`) on the
**aixaitools** Railway account, workspace "My Projects", environment
`production`. Services: `Postgres`, `ledgerly` (API + worker, volume
`ledgerly-volume` at `/data`), `app` (frontend,
`app-production-79d5e.up.railway.app`). Google project `rasayan` for Gemini
and sign-in (service account `ledgerly-prod`).

Paused 2026-10-14 with `railway down` on all three services. The volumes
(database and documents) are kept. The GitHub source is **disconnected**
from `ledgerly` and `app` while paused: every push to `main` auto-deploys
them, which silently un-paused production once (after #50).

- [ ] **Resume**, in this order: `Postgres` → Redeploy, wait until active;
      then reconnect the repo, which deploys `main`:
      ```bash
      railway service source connect --repo aiagencyindia-lab/invoice-processing --branch main -p b2683d9c-cc99-44fc-a2c4-d271487f1181 -e production -s ledgerly
      railway service source connect --repo aiagencyindia-lab/invoice-processing --branch main -p b2683d9c-cc99-44fc-a2c4-d271487f1181 -e production -s app
      ```
      Check `app` still builds from root directory `frontend` (Settings →
      Source). Not `railway redeploy` on `ledgerly`: its last listed
      deployment is an old failed build.
- [ ] **Restore check** against live production, *before* sealing (sealed
      variables are not given to `railway run`). Expect PASS:
      ```bash
      railway run -p b2683d9c-cc99-44fc-a2c4-d271487f1181 -e production -s ledgerly -- sh -c './.venv/bin/python -m invoice_processing.db.restore_check --restored "$(printf %s "$MIGRATE_DATABASE_URL" | sed "s#@[^/]*/#@<tcp-proxy-host:port>/#")"'
      ```
- [ ] **Seal** `DATABASE_URL`, `MIGRATE_DATABASE_URL`,
      `GOOGLE_CREDENTIALS_JSON` on `ledgerly` (operator).
- [ ] **Backups**: `Postgres` → point-in-time recovery + daily;
      `ledgerly-volume` → daily schedule (operator). Record the retention.
- [ ] **Remove the Postgres TCP proxy** (public database endpoint, added
      only for bootstrap).
- [ ] **Docs PR**: backup retention and the smoke-test result in
      `SECURITY.md` / `deployment/BACKUP_RESTORE.md`; the move to the
      aixaitools account and the project ids in `deployment/README.md`.
- [ ] **Deploy settings into git**: start command, pre-deploy migrate and
      60 s draining were applied with `railway config apply` from a file
      outside the repo. Commit a `.railway/railway.ts` (variables
      unmanaged) so they are reviewed and reproducible.

Gotchas found on the first deploy:
- A Postgres added with `railway add --database postgres` has **no TCP
  proxy and no `DATABASE_PUBLIC_URL`**; bootstrap from a laptop needs one.
- Postgres says "password authentication failed" for a role that does not
  exist; its own log says which.
- A malformed `SENTRY_DSN` used to crash both processes (fixed in #49:
  now a warning).

## Spend cleanup (operator)

- [ ] Delete the old Railway project `invoice-processing`
      (`b3a3bee6-b209-49a8-9332-c9d496ed59bc`, aixaitools): crashed API,
      wired to the closed GCP project `project-cb9934aa-…`.
- [ ] Delete the unused `ledgerly` (`94573ffd-…`) in the **shramiksmart**
      workspace: an empty Postgres plus an undeployed staged patch. Check
      the workspace before deleting — the production `ledgerly` has the
      same name.
- [ ] Decide on `lovely-consideration` and `responsible-integrity`
      (shramiksmart workspace, unknown purpose).

## Microsoft sign-in

Branch `phase3/microsoft-sign-in` (no PR): built and reviewed; spec
`docs/superpowers/specs/2026-10-14-microsoft-sign-in-design.md`.

- [ ] Rebase onto `main` (expect conflicts in docs/`SECURITY.md` and the
      `restore_check` head test).
- [ ] Live test. The operator provides the Entra tenant id and email
      domain, and creates OIDC provider `oidc.entra-test` on `rasayan`
      (issuer `https://login.microsoftonline.com/<tid>/v2.0`, code flow,
      Entra optional `email` claim, redirect
      `https://rasayan.firebaseapp.com/__/auth/handler`).
- [ ] PR, review, merge.

## v2 Trust layer

Spec: `docs/superpowers/specs/2026-10-14-v2-trust-layer-design.md`.
Wave 0 plan: `docs/superpowers/plans/2026-10-14-v2-wave0-foundation.md`.

- [x] Wave 0 foundation: history import, Evidence Graph, findings v2,
      `decide()`, provider adapters, benchmark v2 harness (branch
      `v2/wave0-foundation`).
- [ ] AC-19 contract-clause coverage (≥ 60%): measured in CI by
      `benchmark.history.eval` against a fixed contract corpus with a keyword
      stand-in for the model (a pipeline regression gate). Measure the real
      model with `--live-model`; the partner gate needs real top-50-vendor
      contracts.
- [x] **Wave 1** — program plan `docs/superpowers/plans/2026-10-15-v2-wave1-program.md`;
      specs `docs/superpowers/specs/2026-10-15-v2-wave1-*.md`. Merged: scaffold
      (#53), shared fixes (#54), Provenance (#55), GST/ITC (#56), Payment Trust
      (#57), Leakage (#58), CI pool and timeouts (#59), follow-ups (this PR).
  - [ ] Operator note: an engine with hold types in `enforcing` mode, on the
        fake providers, holds every invoice (no real verifier answers
        "verified"). Keep modes `off` or `advisory` until a real provider is set.
        Also in `enforcing`, an invoice is held until its hold engines have
        checked it, a few seconds after processing.
  - [ ] Partner gates: AC-19 clause coverage on real contracts, AC-20 ≥ 0.5% of
        spend, AC-21 at a design partner.
  - [ ] Deferred review minors, per engine PR description (#55–#58).
  - [ ] Per-document evaluations read the whole tenant graph (ponytail notes
        name the ~50k ceiling); index by key before a tenant passes it.
  - [ ] Leakage findings are never auto-released by a clean run (only engines
        that can hold are); decide the policy for insight findings.
  - [ ] A contract or invoice node re-version closes its `part_of` /
        `evidences` edges until the next evaluation of that document.
- [x] **Wave 2 (Surfaces)** — program plan
      `docs/superpowers/plans/2026-10-16-v2-wave2-program.md`; specs
      `docs/superpowers/specs/2026-10-16-v2-wave2-*.md`.
  - [x] Scaffold (`v2/w2-scaffold`): Wave 2 tables, findings/lineage/clause
        routes, `trust.audit.run` job, frontend shell, benchmark hook.
  - [x] Trust Audit backend (`v2/w2-audit`, #62) · [x] Why engine
        (`v2/w2-explain`, #63) · [x] Findings + workspace frontend
        (`v2/w2-frontend`, #64). Merged 2026-10-16; `main` d770be9.
  - [ ] Deferred from the PRD's Wave 2 row: partner mode (v1 has no partner
        console), mobile surface, Slack/WhatsApp/email notifications, trust
        strip (Wave 3's score), `/v2` public API + webhooks (Wave 3),
        server-side PDF (the browser prints the HTML), evidence region
        highlight (page-level only), clause-edit impact assessment.
  - [ ] Engagement scope is a filter, not a FK: overlapping engagements
        count a recovery twice. Add `audit_id` to finding events if it bites.
  - [ ] `categories` scoping is keyword-weak (no category master in v1).
- [x] **Wave 3 (Always-on)** — program plan
      `docs/superpowers/plans/2026-10-21-v2-wave3-program.md`; specs
      `docs/superpowers/specs/2026-10-21-v2-wave3-*-design.md`. Merged
      2026-10-21/22: scaffold (#70), dependency floor (#71), stub-probe
      hand-over (#77), reality (#78), provenance v2 (#76), trust score
      (#75), API keys + `/v2` + webhooks (#74), frontend (#73), close-out
      (this PR).
  - [x] Scaffold (`v2/w3-scaffold`): hold ledger + webhook outbox, gate
        detail (override by finding id), static type registry, mode writer
        with readiness clock, API-key hook, evidence history kinds, route
        stubs, worker sweep, benchmark hooks, frontend shell.
  - [x] Reality checks (`v2/w3-reality`) · [x] Provenance v2
        (`v2/w3-provenance`) · [x] Trust Score (`v2/w3-score`) · [x] API keys,
        `/v2`, webhooks (`v2/w3-api`) · [x] Frontend (`v2/w3-frontend`).
  - [x] Wave close: every Wave 3 code has its explain fixture and template
        (the `WAVE3_PENDING` skips are gone), and the stub-isolation class is
        gone (each built route is probed in its branch's own test module).
  - [x] The API key resolver refuses keys of a tenant that is not `active`
        (`suspended`/`purged`), tested in `tests/test_trust_api_keys.py`.
  - [ ] An API key request costs one query per active tenant (FORCE RLS on
        `api_keys` blocks a cross-tenant prefix lookup). Put the tenant in
        the key (`ik_{tenant_slug}_{prefix}_{secret}`) or add a lookup
        policy before tenants number in the hundreds.
  - [ ] Ruff's `target-version` stays `py310` below `requires-python >=3.11`;
        moving it to `py311` rewrites every `timezone.utc` to `UTC` (~128
        sites): a change of its own.
  - [ ] Deferred from Wave 3: webhook outbox retention sweep; Slack /
        WhatsApp / email notifications; public evidence-upload links;
        copy-move and ML forensics; live connectors (OAuth); rate limiting
        and idempotency keys on `/v2`; `/v2/agents*` (Wave 4).
  - [ ] Partner gates: AC-23 reviewer agreement on reality checks, ≥ 70 %
        of findings before payment, cost per transaction ≤ ₹6.
- [ ] Wave 4 per the spec §2.
- [ ] Remove the Postgres TCP proxy before the trust routes go live (it was
      added for bootstrap only).

## Known gaps

- One container on Railway (scale up, not out); documents on one volume.
  Upgrade path: S3 adapter + shared queue + separate worker.
- No unauthenticated health route; deploys are gated by the start script
  exiting.
- No rate limit on the Microsoft provider lookup (branch).
- Logs: thread-pool lines carry blank ids; a client disconnect logs at
  ERROR; `AUTH_DISABLED` warns per request locally.
- Benchmark: known wrong MV-035, TECH-INV-030/031; dataset defects
  MFG-INV-020, MFG-INV-039, SVC-INV-019.
- `rasayan` still lists `localhost` as an authorized sign-in domain and is
  shared with the Rasayan web app.

## Later

- ERP connectors (wait for a design partner's ERP).
- Custom domain and auth domain.
- GitHub: branch protection on `main`; Dependabot security updates.

## Done

- 2026-10-14 — Production first deploy on Railway; smoke test passed
  (Google sign-up, one PDF processed to a decision in ~46 s).
- 2026-10-14 — #48 sign-up token-refresh loop; #49 run without Sentry on
  a malformed DSN.
