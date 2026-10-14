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
(database and documents) are kept.

- [ ] **Resume**, in this order: `Postgres` → Redeploy; `ledgerly` →
      "Deploy latest commit"; `app` → "Deploy latest commit". Not
      `railway redeploy` on `ledgerly`: its last listed deployment is an old
      failed build.
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
