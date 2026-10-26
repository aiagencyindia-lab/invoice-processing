# Pending work

Everything paused or unfinished. Keep this list current: tick nothing off
until it is done and verified, and move finished items to "Done" with the
date. Last updated 2026-10-25.

## Production (Railway) — LIVE

Project `ledgerly-new` (`9bd2d348-41a3-412a-9484-20a7e1077b75`) on the
**aixaitools** Railway account, workspace "My Projects", environment
`production` (`ddaa420c-639d-4942-943f-3ea87f38b489`). Services: `Postgres`
(volume `postgres-volume`), `invoice-processing` (API + worker, volume
`ledgerly-volume` at `/data`), `app` (frontend,
`app-production-d37f.up.railway.app`, the only public domain). Google
project `rasayan` for Gemini and sign-in (service account `ledgerly-prod`).
The old paused `ledgerly` project (`b2683d9c…`) no longer exists.

**Every merge to `main` deploys to production**: the GitHub source is
connected to `invoice-processing` and `app`. The pre-deploy step migrates
(checked 2026-10-25: `main` 0eacd9d running, schema at `d4e5f6a7b8c9`); the
processes run with `RUN_MIGRATIONS` off.

- [x] **Restore check** against live production (2026-10-25): **PASS**
      (schema version, forced RLS, every policy, the app role's limits).
      The database holds no tenants yet. Run inside the service over SSH
      (key `vineet-mac` registered on aixaitools):
      ```bash
      railway ssh -p 9bd2d348-41a3-412a-9484-20a7e1077b75 -e production -s invoice-processing -- sh -c 'cd /app && ./.venv/bin/python -m invoice_processing.db.restore_check --restored "$MIGRATE_DATABASE_URL"'
      ```
      Run it again against a restored copy once backups exist.
- [x] **Sealed** `DATABASE_URL`, `MIGRATE_DATABASE_URL`,
      `GOOGLE_CREDENTIALS_JSON` on `invoice-processing` (2026-10-26, in the
      dashboard). Nobody can read them back; changing one means pasting a
      new value. `railway run` no longer receives them (use `railway ssh`
      for the restore check, which runs inside the service).
- [ ] **Backups**: none exist (checked 2026-10-25). Volume backup
      schedules need a **Railway Pro** workspace; point-in-time recovery
      for `Postgres` also needs a bucket. Decide on Pro, then: daily +
      weekly on `postgres-volume` and `ledgerly-volume`
      (`volumeInstanceBackupScheduleUpdate`), and record the retention.
- [x] **No public database endpoint** (2026-10-25): `Postgres` has no TCP
      proxy and no domain.
- [ ] **Docs PR**: backup retention and the smoke-test result in
      `SECURITY.md` / `deployment/BACKUP_RESTORE.md`; the project ids in
      `deployment/README.md`.
- [x] **Deploy settings into git** (2026-10-26): `.railway/railway.ts`
      (Railway IaC, variables unmanaged) describes the live project;
      `railway config plan` showed no changes. Applied by hand with
      `railway config apply`, never on push (`deployment/README.md`).
      Railway refuses `railway.json` config files now.

Gotchas found on the first deploy:
- A Postgres added with `railway add --database postgres` has **no TCP
  proxy and no `DATABASE_PUBLIC_URL`**; bootstrap from a laptop needs one.
- Postgres says "password authentication failed" for a role that does not
  exist; its own log says which.
- A malformed `SENTRY_DSN` used to crash both processes (fixed in #49:
  now a warning).
- `railway domain` with no subcommand *creates* a public domain when the
  service has none; list them with `railway domain list -s <service>`.

## Spend cleanup (operator)

- [x] Old Railway project `invoice-processing` (`b3a3bee6-…`, aixaitools;
      crashed API on the closed GCP project) deleted 2026-10-26; Railway
      removes it on 2026-10-28. Production (`ledgerly-new`) untouched.
- [x] The shramiksmart workspace items (unused `ledgerly` `94573ffd-…`,
      `lovely-consideration`, `responsible-integrity`): done by the operator
      (2026-10-26).

## Microsoft sign-in

Branch `phase3/microsoft-sign-in`: built and reviewed; spec
`docs/superpowers/specs/2026-10-14-microsoft-sign-in-design.md`.

- [x] `main` merged in (2026-10-25); the provider-id migration revises
      `d4e5f6a7b8c9`. Second review: no cross-tenant or privilege defect;
      an unfinished Microsoft binding answers "not set up", and a token's
      Entra `tid` must match the binding's when both are present.
- [ ] Live test. The operator provides the Entra tenant id and email
      domain, and creates OIDC provider `oidc.entra-test` on `rasayan`
      (issuer `https://login.microsoftonline.com/<tid>/v2.0`, code flow,
      Entra optional `email` claim, redirect
      `https://rasayan.firebaseapp.com/__/auth/handler`).
- [ ] PR, review, merge.
- [ ] After the live test: require the `tid` check (today it applies only
      when Firebase passes `firebase.sign_in_attributes.tid`), once the
      token shows where it lands.
- [x] The public provider lookup (`POST /api/sso/microsoft`) is rate
      limited (2026-10-25): 20 a minute per caller address, 600 overall,
      429 with `Retry-After`. In-process (one container); the caller key is
      the forwarded address, which a client can set, so the overall cap is
      the real bound.

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
  - [x] API keys carry the tenant (2026-10-26): new keys are
        `ik_{tenant_slug}_{prefix}_{secret}`, so a request reads one tenant.
        Keys made before keep resolving by asking each active tenant.
  - [x] Ruff's `target-version` is `py311`, matching `requires-python`
        (every `timezone.utc` is now `UTC`).
  - [x] Webhook hosts that resolve to internal addresses are refused on
        save and skipped (a failed attempt) at every delivery
        (`trust/webhooks/target.py`).
  - [x] DNS rebinding (2026-10-25): deliveries go out through
        `target.post`, which resolves once, refuses any inward answer and
        connects to the checked address (TLS server name and certificate
        still checked against the real host; no redirects). Proxies
        (`HTTPS_PROXY`) are not honoured on that path.
  - [x] `?as_of` on a transaction's trust reads its own score node
        (`GraphStore.open_node(..., as_of=)`), not every score node.
  - [ ] PRD §9 endpoints not built in Wave 3 and not otherwise listed:
        `POST /v2/bank-accounts/{id}/verify` and `POST /v2/evidence/sources`
        (registering a live connector, which waits on real connectors).
  - [ ] The `reality_coverage_*` benchmark gates are partly true by
        construction (the reality fault family plants invoice lines from its
        own evidence; a kind with nothing checked passes). Real coverage is
        the AC-23 partner gate below.
  - [x] Evidence requests close with their finding (2026-10-26): when a
        finding leaves `needs_vendor_input` (released by other evidence or
        decided by a person), its open requests are cancelled in the same
        transaction; fulfilled ones are kept.
  - [x] Decided (2026-10-22): a clean run in any mode releases the
        engine's open findings, holds placed while enforcing included; mode
        decides whether a hold is placed, not what is found. Pinned in
        `tests/test_trust_engine.py`.
  - [x] Webhook outbox retention (2026-10-26): the worker's daily sweep
        drops delivered or dead rows older than 90 days
        (`outbox.purge_finished`); rows still owed a delivery stay.
  - [ ] Deferred from Wave 3: Slack /
        WhatsApp / email notifications; public evidence-upload links;
        copy-move and ML forensics; live connectors (OAuth). (Rate limits
        and idempotency keys on `/v2` landed 2026-10-24; `/v2/agents*` in
        Wave 4.)
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
  - [x] Approve and revoke seal in a second session after the state change
        commits; the reseal now locks the row (2026-10-26), so a revoke
        landing in between is signed as revoked, not overwritten with the
        "active" seal (proven on Postgres with two connections).
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
        queues `trust.agent.injection` once the governed write happened and
        the worker reads the text and asks the model (`agents/later.py`);
        a yes raises its own `AGENT-INJECTION` finding seconds later. An
        unreachable queue leaves that action unscanned (logged).
  - [x] `PubSubQueue.publish` waits at most 5 s for the broker (2026-10-25,
        was 30 s); it still waits for the message id, so a failed publish
        stays an error in the request's log and the document stays
        republishable.
  - [x] PO re-matches (`po_routes.py`) and deferred injection scans
        (`guard.py`) catch a failed publish per item (2026-10-25): one
        stalled publish no longer skips the rest of the batch; two failures
        in a row stop the batch (a dead broker costs two timeouts, not one
        per item).
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
  - [x] Deferred from Wave 4 → **Wave 5** (program plan
        `docs/superpowers/plans/2026-10-23-v2-wave5-program.md`):
    - [x] Scaffold (`v2/w5-scaffold`): `record()` is one transaction (the
          ledger row, finding and event commit together, in the guard's lock
          session; plain `FOR UPDATE`); `po.create` / `document.upload` kinds;
          `Action.vendor_id`; banner count route; agents list carries the
          current mandate; a refused item in a bulk request stamps the
          earlier rows failed; `/v2/agents/actions` stub; Agents nav + stub
          page (#100).
    - [x] A screen (#101): agents view, mandates, spend by owner, dashboard
          tiles, the block banner (notifications on blocks = in-app +
          webhook).
    - [x] B governed (#103): PO creation and document upload governed;
          holds from log-based BLOCKs on invoices.
    - [x] C intake (#104): `POST /v2/agents/actions` (the generic seam for
          ERP observers; no vendor-specific adapter), counterparties by
          vendor id, the mandate scope documented (in place of external
          mandate-format adapters, which wait for a standard).
    - [x] D hygiene (#102): `agent_actions` retention sweep; OCR text for
          the worker's injection scan.
    - [x] Wave close (2026-10-24): stub-isolation entry removed; an
          18-step live smoke passed on the seeded e2e server (key agent →
          name + id counterparties → two-admin approval, verified →
          enforcing → PO import refused, nothing stored → intake log BLOCK
          holds the invoice → banner count → finding decided, hold lifted →
          ledger shows both).
  - [ ] Wave 5 known limits: an agent hold bites only while agent control
        is enforcing (the hold row stays open and bites again on return;
        only a person's decision lifts it); OCR uses tesseract, which the
        `Dockerfile` installs (a host without it gets no OCR signal); an approver key may log rows
        for any named agent and `/v2` has a per-caller rate limit and
        idempotency keys (2026-10-24); velocity counts every row, so a large PO import
        can raise a velocity review; the log import and intake look up the
        vendor and invoice node per row (fine at 5000 rows, revisit if
        feeds grow).
    - [x] Mandate expiry sweep: not needed. Expiry is computed on every
          read, and rewriting `status` would break the mandate signature.
  - [x] (G1) FR-28.4 "drift from mandate" (2026-10-24), read as drift from
        the agent's own past: an approval or export over 3 times the largest
        amount it handled in that currency over 90 days (at least 10 clean
        past actions) raises an `AGENT-DRIFT` review. Thresholds per tenant.
        Not built: a "first-time kind of action" signal (it flags ordinary
        approve-then-export work; the mandate already scopes the kinds).
  - [ ] Drift limits: it is not a limit — an agent can ratchet up 3 times
        per step and each step becomes its new normal, so the per-action cap
        stays the control; an amount drift flagged never becomes baseline,
        even after a person accepts the finding (a lasting step-up keeps
        flagging until the cap or the factor changes). The baseline is one
        count/max query over the whole window (2026-10-25; was the newest
        500 rows).
  - [x] (G2) PRD "agent registration requires Controller approval"
        (2026-10-24): a `controller` role (ranks with approver, never
        admin) alone approves or rejects a registration; agents start
        `proposed` and are blocked until then. A `role:controller`
        approval-chain step (2026-10-25) is decided only by a controller,
        never an approver or admin, even overdue (admins are notified); a
        rule needs an active controller, and the last one cannot be demoted
        or deactivated while a rule or waiting step needs one (unlocked
        check: two simultaneous demotions can still leave none).
  - [ ] Partner gates (PRD §5): two design partners running agents in
        production and one adopted external mandate standard before release.
- [x] `opentelemetry-api` advisory (CVE-2026-114603 / GHSA-xcq7-p3pf-p88h)
      fixed 2026-10-26: a `[tool.uv] override-dependencies` entry installs
      opentelemetry 1.45.1 past google-adk 2.13.0's `<=1.44` cap; the waiver
      is gone. Drop the override once google-adk allows >=1.45.1.
- [x] Remove the Postgres TCP proxy before the trust routes go live: the
      live project has none (checked 2026-10-25).

## Known gaps

- One container on Railway (scale up, not out); documents on one volume.
  Upgrade path: S3 adapter + shared queue + separate worker.
- `GET /healthz` (2026-10-25) answers 200 when the database answers, 503
  otherwise, with no sign-in. It is Railway's Healthcheck Path on
  `invoice-processing` (set 2026-10-25): a deploy whose database does not
  answer is never switched to.
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
