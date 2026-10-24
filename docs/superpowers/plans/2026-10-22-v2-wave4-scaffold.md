# v2 Wave 4 Scaffold Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lay every shared seam of the agent control plane (schema, registries, store, policy/record/guard seams, route stubs, probe hand-over, benchmark slot) so four branches can build in parallel without touching shared files, with zero behaviour change while the `agents` engine is `off`.

**Architecture:** Three tenant tables behind FORCE RLS; a `trust/agents/` package whose `AgentStore` is the only data access; `policy.evaluate_action` composes rule tuples the branches fill; `record.record` is the one writer of ledger row → finding → webhook; the guard and twelve routes ship as stubs.

**Tech Stack:** Python 3.11, SQLAlchemy 2, Alembic, FastAPI 0.141, pytest; Next.js 16 (one label, one filter).

**Spec:** `docs/superpowers/specs/2026-10-22-v2-wave4-scaffold-design.md`, program plan `docs/superpowers/plans/2026-10-22-v2-wave4-program.md` (U1–U10, B1–B19).

## Global Constraints

- Migration revision `b2c3d4e5f6a7`, revises `a1b2c3d4e5f6`; `RLS_TABLES = ["agents", "agent_mandates", "agent_actions"]`.
- Every money value `Decimal`; `agent_actions.amount` `Numeric(18, 4)`; cap amounts are strings in `scope`.
- No ids, emails or secrets in log `extra`; HTML only via `trust/html.py`.
- Engine `agents` default `off`; with it off, no `agents` table is queried by any governed route.
- `ACTIONS = ("vendor.create", "vendor.patch", "bank_change.propose", "bank_change.decide", "invoice.approve", "invoice.reject", "invoice.export")`; `DECISIONS = ("ALLOW", "ALLOW-WITH-REVIEW", "BLOCK")`; `SOURCES = ("inline", "log", "dryrun")`.
- Thresholds: `agents.velocity_window_minutes 10`, `agents.velocity_max 20`, `agents.split_window_days 7`, `agents.split_min_count 2`, `agents.injection_min_hits 1`.
- Stage explicit paths only; never `.impeccable/hook.cache.json` or `3.sh`.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

**Deviation from the program plan (ponytail):** `benchmark/history/eval.py` already plugs in `measure(tenant_id, graph, findings, manifest, url)` modules via `_measure_modules()`; the scaffold adds `"agents"` to that tuple instead of a new `simulate` hook, and branch D writes `benchmark/history/agents.py` (not `faults/agents.py`). The program plan and simulation spec are updated in Task 6.

## Review Focus

1. A `dryrun` row must never count: `spent`, `count_since`, `creators_of` all exclude `source='dryrun'` — pinned in Task 3.
2. A BLOCKed approval spent nothing: `spent` excludes `decision='BLOCK'` — Task 3.
3. Mandate approve by its proposer (case-insensitive) is refused; approve of a non-latest version is refused; propose while one is pending is refused — Task 3.
4. `record` with an ALLOW writes no finding and no outbox row; a BLOCK that was not refused (shadow) writes the finding but no `agent.action.blocked` — Task 4.
5. A tenant with the engine off sees no change in the Trust engines page — Task 2 (filter) and the existing e2e.

---

### Task 1: Schema

**Files:**
- Create: `invoice_processing/db/migrations/versions/b2c3d4e5f6a7_trust_wave4.py`
- Modify: `invoice_processing/db/models.py` (three models after `WebhookOutbox`; `TENANT_TABLES` += three names)
- Test: `tests/test_trust_schema.py`, `tests/test_gcs_and_migrations.py` (param list), `tests/test_restore_check.py` (pin)

**Interfaces:**
- Produces: models `Agent`, `AgentMandate`, `AgentAction` with the columns of the program plan's S1 table; `agents.status` default `"active"`, `agent_mandates.status` default `"proposed"`, `agent_actions.source` default `"inline"`.

- [ ] **Step 1: Write the failing tests** in `tests/test_trust_schema.py`:

```python
WAVE4_TABLES = ("agents", "agent_mandates", "agent_actions")

def test_the_wave4_tables_are_tenant_tables(url):
    names = set(inspect(get_engine(url)).get_table_names())
    for name in WAVE4_TABLES:
        assert name in TENANT_TABLES and name in names, name

def test_an_agent_principal_is_unique_per_tenant_but_external_agents_repeat(url):
    # two rows with principal_id="api-key:1" → IntegrityError; two with None → fine

def test_a_mandate_version_is_unique_per_agent(url):
    # two AgentMandate(agent_id=a, version=1) → IntegrityError
```

Add `("b2c3d4e5f6a7", 3)` to the param list in `tests/test_gcs_and_migrations.py` and change the pin in `tests/test_restore_check.py` to `"b2c3d4e5f6a7"`.

- [ ] **Step 2: Run** `./.venv/bin/python -m pytest tests/test_trust_schema.py tests/test_restore_check.py tests/test_gcs_and_migrations.py -q -p no:cacheprovider` — Expected: FAIL (tables missing, head mismatch).
- [ ] **Step 3: Implement** the migration (copy `_id/_tenant/_stamps/_at/JSON` helpers from `a1b2c3d4e5f6_trust_wave3.py`; FK `agent_mandates.agent_id` and `agent_actions.agent_id` → `agents.id`; unique `uq_agents_principal (tenant_id, principal_id)`, `uq_agent_mandates_version (agent_id, version)`; index `ix_agent_actions_agent_time (tenant_id, agent_id, occurred_at)`; `vendor_key` and `subject_ref` indexed; RLS block as Wave 3) and the three models (`TenantMixin, TimestampMixin`, `_uuid` ids, `JSONType` for `scope`/`reasons`).
- [ ] **Step 4: Run** the same command — PASS. Then the round trip on Postgres: `docker exec pg-w1 psql -U postgres -c "CREATE DATABASE w4_mig"` and `DATABASE_URL=postgresql+pg8000://postgres:postgres@localhost:55433/w4_mig ./.venv/bin/python -m alembic upgrade head && … downgrade base` — both succeed.
- [ ] **Step 5: Commit** the four files.

### Task 2: Registries, settings, frontend

**Files:**
- Modify: `invoice_processing/trust/types.py`, `trust/modes.py`, `trust/score/readiness.py`, `trust/audit/report.py`, `trust/webhooks/__init__.py`, `trust/thresholds.py`
- Modify: `frontend/src/lib/trust.ts`, `frontend/src/app/trust-engines/page.tsx`
- Test: `tests/test_trust_findings.py`, `tests/test_trust_settings.py` (snapshot), `tests/test_trust_score_*.py` (readiness), `tests/test_trust_explain.py`

**Interfaces:**
- Produces: `types.TABLE` nine `AGENT-*` entries (`_t("agents", False, "authorization", …)`, codes and meanings from the program plan S2 table); `modes.ENGINES[-1] == "agents"`; `modes.BLOCKING_ENGINES = frozenset({"agents"})`; `webhooks.EVENTS` ends with `"agent.action.blocked"`; `thresholds.DEFAULTS` five `agents.*` keys.

- [ ] **Step 1: Write the failing tests:**

```python
# tests/test_trust_findings.py — replace test_reality_is_an_engine_that_can_hold
def test_agents_is_a_blocking_engine_that_never_holds():
    assert modes.ENGINES[-1] == "agents"
    assert "agents" not in modes.HOLD_ENGINES and "agents" in modes.BLOCKING_ENGINES
    assert "reality" in modes.HOLD_ENGINES and "usage" in modes.CAPABILITIES
    agent = [c for c, t in types.TABLE.items() if t.engine == "agents"]
    assert len(agent) == 9 and all(not types.TABLE[c].hold and types.TABLE[c].dimension == "authorization" for c in agent)

# readiness test module
def test_a_blocking_engine_is_not_insight_only():
    assert readiness.insight_only("agents") is False
    assert readiness.recommend("advisory", days=30, findings=50, decided=20, precision=0.95, engine="agents")["to"] == "enforcing"  # match recommend's real signature
```

Add `import invoice_processing.trust.agents` to `test_every_engine_registration_agrees_with_the_static_table` (Task 3 creates it; this test goes green there). Update the settings snapshot with the five keys. In `tests/test_trust_explain.py` add `WAVE4_PENDING = {c for c, e in finding_model.TYPE_ENGINE.items() if e == "agents"}` and `pytest.skip` in the fixture/counterfactual/explains tests when `code in WAVE4_PENDING` (branch C removes it).

- [ ] **Step 2: Run** `pytest tests/test_trust_findings.py tests/test_trust_settings.py tests/test_trust_explain.py tests/test_trust_score_routes.py -q` — FAIL.
- [ ] **Step 3: Implement.** `readiness.insight_only` returns False for `BLOCKING_ENGINES`; the advisory→enforcing "Stay: enforcing adds nothing…" branch does not apply to them. `report.PREVENTION` += `("agents", "enforcing", "agents.", (nine codes))`. Frontend: `ENGINE_LABELS.agents = "Agent control"`; in `TrustEnginesPage` pass `data.engines.filter((e) => e.mode !== "off" || e.findings > 0)` (U6).
- [ ] **Step 4: Run** the tests — PASS (except the registration-agreement test, which needs Task 3); `cd frontend && npm run lint && npx tsc --noEmit`.
- [ ] **Step 5: Commit.**

### Task 3: Package and `AgentStore`

**Files:**
- Create: `invoice_processing/trust/agents/__init__.py`, `invoice_processing/trust/agents/store.py`
- Test: `tests/test_trust_agents_store.py`

**Interfaces:**
- Consumes: Task 1 models; `db.repositories.SelfApproval`; `GraphStore.upsert_node`.
- Produces (all on `AgentStore(url: str | None = None, graph=None)`; dicts mirror the row columns plus `id`; datetimes tz-aware):
  - `register(ctx, *, name, owner, principal_id=None, principal_email=None, kind, description=None) -> dict` (upserts graph node `kind="agent"`, `natural_key=f"agents:{id}"`, `ref_table="agents"`, attrs `{name, owner, kind, status}` when `graph` set; stores `graph_node`)
  - `get(tenant_id, agent_id) -> dict | None`; `by_principal(tenant_id, principal_id) -> dict | None`; `by_emails(tenant_id, emails) -> dict[str, dict]` (casefolded keys); `by_owner(tenant_id, owner) -> list[dict]`; `list(tenant_id, status=None) -> list[dict]`
  - `revoke(ctx, agent_id, reason) -> dict | None` (also revokes the active mandate, reason `"agent revoked"`; re-versions the graph node)
  - `canonical(row: dict) -> bytes`; `digest(row: dict) -> str` (module functions; fields `tenant_id, agent_id, version, scope, valid_from, valid_to, proposed_by, approved_by, approved_at, prev_digest`, `json.dumps(sort_keys=True, separators=(",", ":"), default=iso)`, sha256 hex)
  - `propose_mandate(ctx, agent_id, *, scope, valid_from, valid_to, reason=None) -> dict` (409-style `MandateConflict` when a `proposed` version exists; version = max+1; `prev_digest` = latest version's `digest`; `digest` computed with approver fields null)
  - `approve_mandate(ctx, mandate_id, *, signature: str | None) -> dict` (`SelfApproval` when `ctx.actor.casefold() == proposed_by.casefold()`; `MandateConflict` when not `proposed` or not the latest version; sets `approved_by/at`, recomputes `digest`, stores `signature`; prior `active` → `revoked`, reason `"superseded"`)
  - `revoke_mandate(ctx, mandate_id, reason) -> dict | None`; `mandates(tenant_id, agent_id) -> list[dict]` (version order)
  - `current_mandate(tenant_id, agent_id, now) -> dict | None` (`active` and `valid_from <= now < valid_to`)
  - `lock_agent(s, tenant_id, agent_id) -> None` (`select(Agent).with_for_update()`)
  - `record_action(s, ctx, **cols) -> str` (inserts in the given session, returns id)
  - `actions(tenant_id, *, agent_id=None, vendor_key=None, action=None, since=None, until=None, decisions=None, sources=None, limit=500) -> list[dict]` (newest first)
  - `spent(tenant_id, agent_id, *, since, until, currency, vendor_key=None, category=None) -> Decimal` — sum of `amount`, `action='invoice.approve'`, `decision != 'BLOCK'`, `source != 'dryrun'`, `since <= occurred_at < until`, matching currency
  - `count_since(tenant_id, agent_id, *, since, until) -> int` — non-dryrun rows in the window
  - `creators_of(tenant_id, vendor_key) -> list[dict]` — agents with a non-BLOCK, non-dryrun `vendor.create` for the key
  - `MandateConflict(Exception)` in `store.py`.
- `__init__.py`: `ENGINE_VERSION = "agents-1"`, `ACTIONS`, `DECISIONS`, `SOURCES`, `run(ectx) -> list` returning `[]`, and `register_type(code, t.description, engine="agents", hold=False, dimension=t.dimension)` for each `TABLE` entry with `engine == "agents"`.

- [ ] **Step 1: Write the failing tests** (`tests/test_trust_agents_store.py`, SQLite via `scratch_db` + `fresh_schema`, one tenant row):

```python
def test_register_and_look_up_by_principal_email_and_owner(store): ...
def test_a_mandate_cannot_be_approved_by_its_proposer(store):  # "Admin@x" proposes, "admin@x" approves → SelfApproval
def test_propose_while_one_is_pending_conflicts(store): ...
def test_only_the_latest_version_can_be_approved(store): ...
def test_approve_supersedes_the_active_version_and_chains_digests(store):
    # v2.prev_digest == v1.digest; v1.status == "revoked"; digest(v2) == v2["digest"]
def test_current_mandate_respects_the_validity_window(store):  # now == valid_to → None
def test_spent_counts_only_non_blocked_non_dryrun_approvals_in_currency(store):
    # ALLOW approve 100 INR, BLOCK approve 50 INR, dryrun approve 70 INR, export 100 INR, approve 10 USD → spent(INR) == Decimal("100")
def test_count_since_and_creators_of_ignore_dryrun(store): ...
def test_revoking_an_agent_revokes_its_active_mandate(store): ...
```

- [ ] **Step 2: Run** `pytest tests/test_trust_agents_store.py -q` — FAIL (no module).
- [ ] **Step 3: Implement** `__init__.py` and `store.py` per the interfaces. Normalise SQLite naive datetimes with `.replace(tzinfo=UTC)`.
- [ ] **Step 4: Run** the store tests and `pytest tests/test_trust_findings.py -q` — PASS.
- [ ] **Step 5: Commit.**

### Task 4: Policy seam, `record`, guard stub

**Files:**
- Create: `invoice_processing/trust/agents/{policy,record,rules,behaviour,guard}.py`
- Create: `invoice_processing/explain/templates/agents.py` (docstring only) + import line in `explain/templates/__init__.py` if that module imports family templates explicitly
- Test: `tests/test_trust_agents_store.py` (record tests), `tests/test_trust_agents_policy.py`

**Interfaces:**
- Consumes: Task 3 store; `FindingStore.upsert(ctx, spec, *, hold=False)`; `FindingSpec`/`Citation` (`trust/findings/model.py`); `webhooks.outbox.emit(s, tenant_id, event, data)`; `GraphStore.upsert_edge`.
- Produces:
  - `policy.Action`, `policy.Reason`, `policy.Decision` frozen dataclasses with the fields of the program plan S4 (Action also `currency`, `occurred_at`; `Decision.action_id`/`agent_id` default `None`).
  - `policy.evaluate_action(ctx, action, *, agent, mandate, store, settings, now, mode="advisory", decider=None) -> Decision` — runs every rule in `rules.RULES + behaviour.RULES`; a rule raising → `Reason("rule-failed", "AGENT-OUT-OF-MANDATE", "BLOCK" if mode == "enforcing" else "REVIEW", "A check could not run.")`; decision BLOCK > ALLOW-WITH-REVIEW > ALLOW; `explanation = " ".join(r.message for r in reasons)` passed through `html` escaping.
  - `rules.RULES: tuple = ()`, `behaviour.RULES: tuple = ()`.
  - `record.record(ctx, decision, action, *, agent, mandate, store, findings, graph, mode, source, refused, now) -> str` — ordered (B13): (1) `store.record_action` in its own session; (2) if `decision.decision != "ALLOW"` and `source != "dryrun"`: `findings.upsert(ctx, FindingSpec(type=first reason's type, transaction_node=action.transaction_node or agent["graph_node"], dedupe_key=f"agent_action:{id}", citations=[agent node] + [subject node if known], details={engine: "agents", mode, source, action: action.kind, agent_id, decision, reasons: [...], rule_version}))`; (3) one session: stamp `finding_id`; if `refused` emit `agent.action.blocked` with payload `{agent_id, agent_name, owner, action, subject_ref, decision, reasons, explanation, action_id, finding_id}`; (4) ALLOW/REVIEW inline with a known subject node → `graph.upsert_edge(agent node → subject, kind="acted_on")`. Returns the action id.
  - `guard.check(principal, action, *, settings_store=None, now=None, store=None) -> Decision | None` returning `None` (docstring names branch B).

- [ ] **Step 1: Write the failing tests:**

```python
def test_no_rules_allows(store): evaluate_action(...).decision == "ALLOW"
def test_block_beats_review_and_every_rule_runs(monkeypatch): # patch RULES with a REVIEW, a BLOCK, a None → BLOCK, two reasons, explanation has both messages
def test_a_raising_rule_blocks_only_in_enforcing(monkeypatch): ...
def test_the_explanation_is_escaped(monkeypatch): "<b>" → "&lt;b&gt;"
def test_record_allow_writes_only_the_ledger_row(env): no finding, no outbox row
def test_record_shadow_block_raises_a_finding_but_no_blocked_event(env): refused=False
def test_record_refused_block_emits_agent_action_blocked(env): one outbox row for a "*" endpoint, payload has no amount fields beyond the action's
def test_record_dryrun_never_raises_a_finding(env): ...
def test_record_without_an_invoice_node_cites_the_agent_node(env): finding.transaction_node == agent["graph_node"]
```

- [ ] **Step 2: Run** `pytest tests/test_trust_agents_policy.py tests/test_trust_agents_store.py -q` — FAIL.
- [ ] **Step 3: Implement** per the interfaces.
- [ ] **Step 4: Run** — PASS. Also `pytest tests/test_trust_explain.py -q` (the empty template module imports cleanly).
- [ ] **Step 5: Commit.**

### Task 5: Route stubs, service wiring, stub-probe hand-over

**Files:**
- Create: `invoice_processing/api/trust_agent_routes.py`
- Modify: `invoice_processing/api/service.py` (`get_agent_store()` beside `get_hold_store`, `app.include_router(trust_agent_router)`)
- Modify: `tests/test_tenant_isolation_api.py`

**Interfaces:**
- Produces: `router = APIRouter()` with twelve handlers raising `HTTPException(503, "Agent control is not built yet.")`, names and roles from the registry spec: `register_agent` POST `/api/trust/agents`; `list_agents` GET `/api/trust/agents`; `tenant_ledger` GET `/api/trust/agents/ledger`; `import_actions` POST `/api/trust/agents/actions/import`; `get_agent` GET `/api/trust/agents/{agent_id}`; `revoke_agent` POST `…/{agent_id}/revoke`; `propose_mandate` POST and `list_mandates` GET `…/{agent_id}/mandates`; `approve_mandate` / `revoke_mandate` POST `…/{agent_id}/mandates/{mandate_id}/approve|revoke`; `evaluate_action` POST `…/{agent_id}/actions/evaluate`; `get_ledger` GET `…/{agent_id}/ledger`. Static paths (`/ledger`, `/actions/import`) are declared before `/{agent_id}`. Each takes the principal dependency of its role so the 503 comes after auth. `service.get_agent_store() -> AgentStore | None` (None without the `agents` table, as `get_hold_store`).

- [ ] **Step 1: Write the failing tests** in `tests/test_tenant_isolation_api.py`: restore from 4f836b3 a `TestWave4StubsIsolation` class with `STUBS` (twelve `(method, template, role)`), `test_a_stub_answers_no_tenant` (skip when `template in branch_probed()`; both tenants get 503), `TestCoverage._probed` adding the stub templates, `test_a_built_stub_hands_over_to_its_branch_probe` (fake `tests.test_trust_agent_routes` module with `PROBED = {"/api/trust/agents"}`), `test_a_stub_is_covered_only_while_it_is_a_stub`; `EXTRA_PROBE_MODULES += ("tests.test_trust_agent_routes",)`; route-module tuple += `trust_agent_routes`.
- [ ] **Step 2: Run** `pytest tests/test_tenant_isolation_api.py -q` — FAIL.
- [ ] **Step 3: Implement** the router and service wiring.
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit.**

### Task 6: Benchmark slot, docs

**Files:**
- Modify: `benchmark/history/eval.py` (`_measure_modules` tuple `("reality", "score", "agents")`, docstring)
- Modify: `docs/PENDING.md` (Wave 4 section: scaffold ticked when merged; branches A–D open; deferrals from the program plan's non-goals and B12/B13)
- Modify: `docs/superpowers/plans/2026-10-22-v2-wave4-program.md`, `docs/superpowers/specs/2026-10-22-v2-wave4-simulation-design.md` (D owns `benchmark/history/agents.py` with `measure(tenant_id, graph, findings, manifest, url) -> dict`, not `faults/agents.py`/`simulate`)
- Test: `tests/test_benchmark_history.py`

- [ ] **Step 1: Write the failing test:** `test_an_agents_measure_module_is_picked_up_when_present` — monkeypatch `sys.modules["benchmark.history.agents"]` with a fake `measure` returning `{"n": 1, "gates": {"agents_x": True}}`; assert `"agents" in eval._measure_modules()`.
- [ ] **Step 2: Run** — FAIL. **Step 3:** add `"agents"`. **Step 4:** PASS, and `python -m benchmark.history.eval --seed 7` still 52/52.
- [ ] **Step 5: Commit.**

### Task 7: Verify, review, PR

- [ ] `./.venv/bin/python -m ruff check invoice_processing tests benchmark scripts && ./.venv/bin/python -m ruff format --check invoice_processing tests benchmark`
- [ ] SQLite full suite: passes, skips only the existing 15 plus the `WAVE4_PENDING` explain skips.
- [ ] Postgres: fresh DB `w4_scaffold`, `tests/test_trust_*.py` pass; alembic up/down round trip.
- [ ] `python -m benchmark.history.eval --seed 7`: 52/52.
- [ ] Frontend: `npm run lint && npx tsc --noEmit && npm run build && npm run test:e2e` (47 pass).
- [ ] Fable review of the branch diff; one fix pass (each fix test-first); ledger in `.superpowers/sdd/2026-10-22-v2-wave4-program/progress.md`.
- [ ] Push via the `aiagencyindia-lab` credential, open the PR, switch `gh` back; merge only on the user's "merge".
