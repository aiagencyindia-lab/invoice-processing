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
  - [x] Ruff's `target-version` is `py311`, matching `requires-python`
        (every `timezone.utc` is now `UTC`).
  - [x] Webhook hosts that resolve to internal addresses are refused on
        save and skipped (a failed attempt) at every delivery
        (`trust/webhooks/target.py`).
  - [ ] DNS rebinding between that check and `requests`' own lookup still
        gets through: pin the checked address in a transport adapter before
        an attacker-run DNS server is in scope.
  - [x] `?as_of` on a transaction's trust reads its own score node
        (`GraphStore.open_node(..., as_of=)`), not every score node.
  - [ ] PRD §9 endpoints not built in Wave 3 and not otherwise listed:
        `POST /v2/bank-accounts/{id}/verify` and `POST /v2/evidence/sources`
        (registering a live connector, which waits on real connectors).
  - [ ] The `reality_coverage_*` benchmark gates are partly true by
        construction (the reality fault family plants invoice lines from its
        own evidence; a kind with nothing checked passes). Real coverage is
        the AC-23 partner gate below.
  - [ ] Evidence requests stay `open` when a finding is released by other
        evidence (e.g. a GRN import) rather than by its uploaded proof;
        cancel them on release if the open list gets noisy.
  - [x] Decided (2026-10-22): a clean run in any mode releases the
        engine's open findings, holds placed while enforcing included; mode
        decides whether a hold is placed, not what is found. Pinned in
        `tests/test_trust_engine.py`.
  - [ ] Deferred from Wave 3: webhook outbox retention sweep; Slack /
        WhatsApp / email notifications; public evidence-upload links;
        copy-move and ML forensics; live connectors (OAuth); rate limiting
        and idempotency keys on `/v2`; `/v2/agents*` (Wave 4).
  - [ ] Partner gates: AC-23 reviewer agreement on reality checks, ≥ 70 %
        of findings before payment, cost per transaction ≤ ₹6.
- [x] **Wave 4 (Agent control plane, built dark)** — program plan
      `docs/superpowers/plans/2026-10-22-v2-wave4-program.md`, specs
      `docs/superpowers/specs/2026-10-22-v2-wave4-*-design.md`. Engine
      `agents` is `off` by default; no screen.
  - [x] Scaffold (#83) · [x] Registry, mandates, ledger, routes (#86) ·
        [x] Behavioural detectors + explanations (#85) · [x] Structural
        rules + guard wiring (#87) · [x] Simulated fleet, `/v2/agents*`,
        docs (#90). Follow-ups: #84 (seam tests), #88 (CI test job 45 min),
        #89 (e2e spec files run serially: they share one e2e database).
  - [x] Wave close: `TestWave4StubsIsolation` removed (every agent route is
        probed in `tests/test_trust_agent_routes.py`); the `WAVE4_PENDING`
        explain skips went with the behaviour branch. AC-28 on `--seed 7`:
        65/65 structural blocked, 20/20 behavioural flagged, 0/97 false
        blocks, ~1 s. A 27-step live smoke passed (2026-10-22).
  - [x] A new API key may not take a registered agent's name (its derived
        email would act as the agent); refused in `apikeys.create`.
  - [x] Mandate signatures are enforced (2026-10-23): `mandate_active`
        blocks an agent whose mandate in force does not verify (unsigned,
        changed after signing, or `TRUST_MANDATE_KEY` unset) or was replaced
        by a later version by the action's time; the signature now covers
        the state (`status`, `revoked_*`) and every approve, supersede and
        revoke reseals the rows it changed.
  - [ ] Signatures made before 2026-10-23 sign the digest alone and no
        longer verify: re-approve (propose + approve a new version) any
        mandate approved earlier. No database holds one yet (the Wave 4
        tables were never deployed; production is on the v1 schema).
  - [ ] Approve and revoke seal in a second session after the state change
        commits. A revoke of the very version being approved, landing
        between the two, can leave it revoked with an "active" seal: it then
        never verifies (fails closed, silently). Lock the row while sealing
        if this ever matters.
  - [ ] Replaying an entire earlier mandate row (signature included) from a
        backup is undetectable from inside the database when no later
        version exists (e.g. un-revoking an agent's only mandate together
        with the agent row). Needs an anchor outside the database (e.g. the
        audit trail shipped off-box, or a signed revocation list).
  - [x] Spend counts what happened (2026-10-23): approve/reject and export
        stamp their ledger row `executed` or `failed` around the write
        (`agent_actions.outcome`, migration `c3d4e5f6a7b8`); caps and the
        ledger skip failed rows, unstamped rows (a crash, imported logs)
        still count, and the ledger counts each invoice's export once.
  - [ ] Velocity (`agents.velocity_max`) still counts failed attempts, by
        design: a burst of refused retries is itself the behaviour it flags.
        Two exports of one invoice with identical timestamps (imported logs
        only) pick an arbitrary "latest" amount.
  - [x] Injection is classified after the write (2026-10-23): the guard
        queues `trust.agent.injection` once the agent lock is released and
        the worker reads the text and asks the model (`agents/later.py`);
        a yes raises its own `AGENT-INJECTION` finding seconds later. An
        unreachable queue leaves that action unscanned (logged).
  - [ ] `PubSubQueue.publish` waits up to 30 s for the broker on the request
        path (every enqueue, this one included); a slow Pub/Sub still breaks
        the inline p95. Shorten it, or publish without waiting, if it shows.
  - [x] Imported log rows are judged at their time (2026-10-23): an agent
        is revoked only from its `revoked_at`.
  - [x] `/actions/evaluate` and `/actions/import` answer 409 while the
        engine is `off` and record nothing (2026-10-23).
  - [x] A person refused under U9 raises one finding per invoice and
        person; no ledger row, no `agent.action.blocked` (2026-10-23).
  - [x] A structural rule that raises is the guard's failure (503 in
        enforcing, logged no-op below, no finding); a behavioural detector
        that raises is skipped (2026-10-23).
  - [ ] A detector that keeps raising is now only the `agent_rule_failed`
        warning (error type); alert on it once logs reach a monitor.
  - [x] `replace_vendors` reads the vendor keys in one query (2026-10-23).
  - [ ] FR-28.1 "our own agents register automatically" is moot: no
        in-house agent performs a governed write today.
  - [ ] Deferred from Wave 4: an agents view / NAV / dashboard (release
        gate); live ERP-agent observers (SAP Joule, Oracle, Dynamics) beyond
        file import; external mandate-format adapters; notifications on
        blocks; governing PO creation and document upload; holds from
        log-based BLOCKs; mandate expiry sweep; `agent_actions` retention;
        OCR text for injection scanning; vendor-name key collisions for
        counterparties; `record()` is ordered, not atomic (a crash can leave
        a decision without its finding).
  - [ ] Partner gates (PRD §5): two design partners running agents in
        production and one adopted external mandate standard before release.
- [ ] `opentelemetry-api` 1.42.1 advisory (CVE-2026-114603 /
      GHSA-xcq7-p3pf-p88h) waived until **2026-12-12**: the fix (1.45.1) is
      blocked by google-adk's cap (≤ 1.42.1, still so in 2.12.0); the product
      API never parses inbound trace headers. Upgrade when google-adk allows.
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
