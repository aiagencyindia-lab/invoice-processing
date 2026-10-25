# Wave 3 Scaffold Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land every shared file Wave 3's five parallel branches build on — migration, type registry, hold ledger + outbox, hold detail on the gate and its callers, API-key hook, graph/history/provider seams, worker sweep, benchmark hooks, isolation probes, frontend shell — with no tenant-visible behaviour change while every engine mode stays `off`.

**Architecture:** Additive. New modules (`trust/types.py`, `trust/holds.py`, `trust/webhooks/`, `trust/apikeys.py`, `trust/reality/`, `trust/score/`, five route stubs) plus small, tested edits at existing seams (`findings/store.py`, `evaluate.py`, `gate.py`, `approval_flow.py`, `service.py`, `auth.py`, `modes.py`, `tenant_settings.py`, `sync.py`, `lineage.py`, `mapping.py`, providers, `runner.py`). Holds are a ledger written inside the finding's own session; the gate keeps computing from findings.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic, pytest (SQLite; Postgres via `TEST_DATABASE_URL`); Next.js 16 / TypeScript for the shell. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-21-v2-wave3-scaffold-design.md` (program plan: `docs/superpowers/plans/2026-10-21-v2-wave3-program.md`).

## Global Constraints

- Every new tenant table: in `models.TENANT_TABLES`, in the migration's own `RLS_TABLES`, head pinned in `tests/test_restore_check.py`, revision in the `tests/test_gcs_and_migrations.py` param list.
- Every new route: a two-tenant probe; stubs are listed in `PROBED` by the scaffold, branch modules export their own `PROBED`.
- Money is `Decimal`; ids and raw bank numbers never appear in log `extra`; masked attrs in the graph; HTML only via `trust/html.py`.
- Default engine mode stays `off`; nothing a tenant sees changes until an operator opts in.
- Commit messages are plain sentences ending with the Co-Authored-By line the session reminder gives; stage explicit paths only; never stage `.impeccable/hook.cache.json`.
- Run `ruff check` + `ruff format --check` on `invoice_processing tests benchmark` before every commit.

## Review Focus

1. A finding re-detected by an engine in `enforcing` mode after its hold was lifted by `rejected` — `upsert` returns `"untouched"` for a decided finding, so no second hold row appears (Task 4 test).
2. `set_engine_mode` from `enforcing` back to `advisory` lifts every open hold of that engine and emits `hold.lifted` per row, not one event (Task 3 test).
3. Export after an override when a *new* hold-type finding appears: the id-set comparison refuses (Task 6 test); the old string override stored by a pre-Wave-3 workflow (`hold_overridden` is a `str`) is treated as no override (Task 6 test).
4. A request carrying both `X-API-Key` and a valid bearer: the bearer wins and the key never changes the tenant (Task 7 test).
5. History rows of the new kinds with a `po_number` that matches no PO get only `issued_by`, never a dangling `fulfils` (Task 8 test).

---

### Task 1: Migration, models, TENANT_TABLES

**Files:**
- Create: `invoice_processing/db/migrations/versions/a1b2c3d4e5f6_trust_wave3.py`
- Modify: `invoice_processing/db/models.py` (append five models before `TENANT_TABLES`; extend the tuple)
- Modify: `tests/test_restore_check.py:31`, `tests/test_gcs_and_migrations.py:478`
- Test: `tests/test_trust_schema.py`

**Interfaces:**
- Produces ORM classes `TrustHold`, `TrustEvidenceRequest`, `ApiKey`, `WebhookEndpoint`, `WebhookOutbox` (columns per the spec table; every one `Base, TenantMixin, TimestampMixin`, `id = String(36) default _uuid`); `ApiKey` has `UniqueConstraint("tenant_id", "key_prefix", name="uq_api_keys_prefix")`; `WebhookOutbox.attempts` default 0, `next_attempt_at` nullable=False default `_now`.

- [ ] **Step 1: Write the failing tests** in `tests/test_trust_schema.py` (append; follow the file's existing Wave 2 test style):

```python
WAVE3_TABLES = ("trust_holds", "trust_evidence_requests", "api_keys", "webhook_endpoints", "webhook_outbox")

def test_wave3_tables_exist_and_are_tenant_tables(scratch_db):
    url = scratch_db("w3.db"); fresh_schema(url)
    names = set(inspect(get_engine(url)).get_table_names())
    assert set(WAVE3_TABLES) <= names
    assert set(WAVE3_TABLES) <= set(models.TENANT_TABLES)

def test_api_key_prefix_is_unique_per_tenant(scratch_db):  # two rows same prefix same tenant -> IntegrityError
```
Add `("a1b2c3d4e5f6", 5)` to the param list in `tests/test_gcs_and_migrations.py`; change the pinned head in `tests/test_restore_check.py` to `"a1b2c3d4e5f6"`.

- [ ] **Step 2: Run** `./.venv/bin/python -m pytest tests/test_trust_schema.py tests/test_restore_check.py tests/test_gcs_and_migrations.py -q -p no:cacheprovider` → FAIL (missing tables / head mismatch / no such revision).

- [ ] **Step 3: Implement.** Copy the Wave 2 migration's header and helpers (`_id`, `_tenant`, `_stamps`, `JSON`, the dialect guard, `enable_rls_statements(RLS_TABLES)`); `revision = "a1b2c3d4e5f6"`, `down_revision = "f4a5b6c7d8e9"`; `RLS_TABLES` = the five names; `downgrade` drops in reverse. Add the five models and extend `TENANT_TABLES` in the same order as `RLS_TABLES`.

- [ ] **Step 4: Run** the three test files → PASS. Also `DATABASE_URL=postgresql+pg8000://postgres:postgres@localhost:55433/w3mig ./.venv/bin/python -m alembic upgrade head && … downgrade base` when `pg-w1` is up (record the result in the ledger if Docker is down).

- [ ] **Step 5: Commit** `invoice_processing/db/models.py`, the migration, the three test files.

---

### Task 2: Type registry (`trust/types.py`), dimensions, modes constants, thresholds

**Files:**
- Create: `invoice_processing/trust/types.py`
- Modify: `invoice_processing/trust/findings/model.py` (`register_type` gains `dimension`; registers `types.TABLE` at import), `invoice_processing/trust/modes.py` (`ENGINES`, `HOLD_ENGINES`, `CAPABILITIES`), `invoice_processing/trust/thresholds.py` (12 new defaults), `invoice_processing/trust/audit/report.py:19` (PREVENTION row for reality)
- Test: `tests/test_trust_findings.py` (registry agreement), `tests/test_trust_settings.py` (snapshot → `WAVE1_THRESHOLDS` renamed `THRESHOLDS` with the 12 new names)

**Interfaces:**
- Produces `types.Type(engine: str, hold: bool, dimension: str | None, description: str)` (frozen dataclass), `types.TABLE: dict[str, Type]`, `types.DIMENSIONS`; `finding_model.TYPE_DIMENSION: dict[str, str | None]`; `register_type(code, description, *, engine="bench", hold=False, dimension=None)`.
- `TABLE` holds every code of Waves 1–2 (read them from `trust/{leakage,payment,provenance,gst_itc}/__init__.py` `register_type` calls) plus `TRUST-ENGINE-FAILED` and the eight Wave 3 codes in the spec, with dimensions: PAY-UNVERIFIED/NAME-MISMATCH/BEC/BEC-SIGNAL → identity; PAY-ANOMALY → consistency; PROV-* → authenticity; GST-* → identity; LEAK-RATE/ESCALATOR/TIER/REBATE/DISCOUNT/TERM → price; DUP-*/STMT-*/RADAR-* → consistency; TRUST-ENGINE-FAILED → None.

- [ ] **Step 1: Write the failing tests**

```python
def test_every_engine_registration_matches_the_static_table():
    import invoice_processing.trust.leakage, …payment, …provenance, …gst_itc  # noqa
    for code, t in types.TABLE.items():
        assert fm.TYPE_ENGINE[code] == t.engine and (code in fm.HOLD_TYPES) == t.hold
    assert set(fm.TYPES) - {"BENCH-IMPORT"} == set(types.TABLE)

def test_the_api_process_knows_hold_types_without_engines():
    # fresh interpreter: python -c "from invoice_processing.trust.findings import model as m; print('PAY-BEC' in m.HOLD_TYPES)" -> True (use subprocess)

def test_reality_is_an_engine_that_can_hold():
    assert "reality" in modes.ENGINES and "reality" in modes.HOLD_ENGINES and "usage" in modes.CAPABILITIES
```
Snapshot test: `THRESHOLDS` dict = old 23 + the 12 spec values.

- [ ] **Step 2: Run** `pytest tests/test_trust_findings.py tests/test_trust_settings.py -q` → FAIL.
- [ ] **Step 3: Implement** `types.py`; in `findings/model.py` after `register_type` is defined: `for code, t in TABLE.items(): register_type(code, t.description, engine=t.engine, hold=t.hold, dimension=t.dimension)` (move the existing `TRUST-ENGINE-FAILED` registration into `TABLE`); engines' own `register_type` calls stay (idempotent). Update `modes.py`, `thresholds.py`, `report.py` PREVENTION (`("reality", "enforcing", "reality.", ("REAL-CONTRADICTED", "REAL-PARTIAL", "REAL-NO-EVIDENCE"))` following the tuple's existing shape), `tenant_settings.DESCRIPTIONS["trust_engine_modes"]` to name reality.
- [ ] **Step 4: Run** `pytest tests/test_trust_findings.py tests/test_trust_settings.py tests/test_trust_audit.py tests/test_trust_routes.py -q` → PASS.
- [ ] **Step 5: Commit.**

---

### Task 3: Mode changes with a clock, audit and reconcile (`modes.set_engine_mode`), settings keys

**Files:**
- Modify: `invoice_processing/trust/modes.py`, `invoice_processing/tenant_settings.py` (`SETTABLE["trust_shutdown_periods"] = list`, default `[]`, `validate` branch, `DESCRIPTIONS`), `invoice_processing/api/config_routes.py:122-152`
- Test: `tests/test_trust_settings.py`, `tests/test_trust_gate.py` (reconcile needs holds — written in Task 4; here `reconcile` is called through a monkeypatched `holds.reconcile`)

**Interfaces:**
- Produces `modes.mode_since(tenant_id, engine, store=None) -> datetime | None` (reads `store.get(tenant_id).get("trust_engine_mode_since", {}).get(engine)`, ISO → aware datetime).
- Produces `modes.set_engine_mode(ctx, engine, mode, *, store=None, audit=None, now=None, reason="") -> dict` returning `{"engine", "from", "to", "since"}`; raises `ValueError` on an unknown engine/mode; writes `trust_engine_modes` (merged) and `trust_engine_mode_since[engine] = now.isoformat()` via `store.put`; `audit.emit(ctx, "trust_engine_mode_changed", {"engine", "from", "to", "reason"})` when audit is given; when `from != to` and either side is `enforcing`, calls `holds.reconcile(ctx, engine, enforcing=(to == "enforcing"), url=None)`.
- `config_routes.update_settings`: if `"trust_engine_modes" in cleaned`, pop it and call `set_engine_mode` per changed engine (store = `service.get_tenant_settings()`, audit = `service.get_audit()`), then `store.put` the rest; the response is unchanged.

- [ ] **Step 1: Failing tests** (`tests/test_trust_settings.py`, with the file's `Store` stub extended with `put` that merges):

```python
def test_set_engine_mode_stamps_the_clock_and_audits():
    store = Store({}); audit = Audit()
    out = modes.set_engine_mode(CTX, "payment", "shadow", store=store, audit=audit, now=T0, reason="pilot")
    assert out == {"engine": "payment", "from": "off", "to": "shadow", "since": T0.isoformat()}
    assert modes.mode_since("t", "payment", store) == T0 and audit.events[-1][1] == "trust_engine_mode_changed"

def test_set_engine_mode_reconciles_only_across_enforcing(monkeypatch):  # off->shadow: no call; advisory->enforcing: enforcing=True; enforcing->advisory: enforcing=False

def test_shutdown_periods_validate():  # [["2026-01-01","2026-01-07"]] ok; ["x"] -> SettingsError; to < from -> SettingsError
def test_put_settings_route_detours_modes_through_set_engine_mode()  # in tests/test_trust_routes.py or the config route tests: PUT {"trust_engine_modes": {"payment": "shadow"}} -> audit has trust_engine_mode_changed and mode_since set
```
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** `pytest tests/test_trust_settings.py tests/test_trust_routes.py tests/test_config*.py -q` → PASS. **Step 5: Commit.**

---

### Task 4: Hold ledger + outbox emit side (`trust/holds.py`, `trust/webhooks/`), store and evaluate wiring

**Files:**
- Create: `invoice_processing/trust/holds.py`, `invoice_processing/trust/webhooks/__init__.py`, `invoice_processing/trust/webhooks/outbox.py`, `invoice_processing/trust/webhooks/deliver.py` (stub)
- Modify: `invoice_processing/trust/findings/store.py` (`upsert(..., *, hold=False)`, `transition` lifts on `rejected`, both emit), `invoice_processing/trust/evaluate.py:312-324` (pass `hold=(mode == "enforcing" and spec.type in HOLD_TYPES)`; the `_engine_failed` upsert passes `hold=True`), `invoice_processing/api/service.py` (`get_hold_store()` accessor next to `get_finding_store`)
- Test: `tests/test_trust_findings.py` (holds + outbox), `tests/test_trust_engine.py` (evaluate places a hold only in enforcing)

**Interfaces:**
- `webhooks.EVENTS = ("finding.created", "finding.decided", "hold.placed", "hold.lifted", "ping")`.
- `outbox.emit(s, tenant_id: str, event: str, data: dict, *, now: datetime | None = None) -> int`: for each `WebhookEndpoint` of the tenant with `active` and (`event in events or "*" in events`) add a `WebhookOutbox(endpoint_id, event, payload={"event", "tenant", "occurred_at", "data"}, attempts=0, next_attempt_at=now)`; returns rows added. Raises `ValueError` on an unknown event.
- `holds.place(s, ctx, *, finding_id, transaction_node, engine, type, summary, impact, currency, by="evaluate", now=None) -> bool` (False when an open row exists; True → row + `emit(hold.placed)`).
- `holds.lift(s, ctx, finding_id, *, by, reason, now=None) -> bool` (False when no open row; True → `lifted_*` + `emit(hold.lifted)`).
- `holds.open_for(tenant_id, transaction_node, *, url=None) -> list[dict]` rows `{id, finding_id, type, engine, placed_at}`.
- `holds.reconcile(ctx, engine, *, enforcing: bool, url=None, findings=None) -> dict` `{placed, lifted}`: enforcing → place for every open (status ∉ {rejected} and not decided-money) hold-type finding with `details.engine == engine`; not enforcing → lift every open row of that engine with reason `"engine left enforcing"`.
- `FindingStore.upsert(ctx, spec, *, hold: bool = False) -> tuple[str, str]`: on `"created"` → `emit(finding.created, finding dict)`; when `hold` and result ≠ `"untouched"` → `holds.place(...)` in the same session.
- `FindingStore.transition(...)`: after the event row, `to_status in DECIDED` → `emit(finding.decided, {...finding, "to_status", "reason"})`; `to_status == "rejected"` → `holds.lift(s, ctx, finding_id, by=actor, reason=reason)`.
- `deliver.due(now, *, post=None, limit=50) -> int` returns 0 (the api branch fills it).

- [ ] **Step 1: Failing tests** (`tests/test_trust_findings.py`, reuse its store/graph fixture; add an endpoint row with `events=["*"]`):

```python
def test_upsert_with_hold_places_once_and_emits():  # two upserts -> one trust_holds row, one hold.placed outbox row, one finding.created
def test_reject_lifts_and_emits():  # transition detected->triaged->rejected -> lifted_at set, hold.lifted + finding.decided rows
def test_a_redetected_rejected_finding_gets_no_new_hold():  # Review Focus 1
def test_reconcile_both_directions():  # Review Focus 2: two open hold-type findings -> enforcing=True places 2; enforcing=False lifts 2, two hold.lifted rows
def test_outbox_filters_by_endpoint_subscription():  # endpoint events=["finding.created"] gets no hold.placed
```
`tests/test_trust_engine.py`: `test_enforcing_places_a_hold_and_advisory_does_not()` using the existing `run(env, …, modes)` helper and a RUNNER returning one hold-type spec (`T-PAYHOLD` registered as in test_trust_gate).

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** `pytest tests/test_trust_findings.py tests/test_trust_engine.py tests/test_trust_gate.py tests/test_trust_findings_routes.py -q` → PASS. **Step 5: Commit.**

---

### Task 5: `gate.payment_hold_detail()`

**Files:**
- Modify: `invoice_processing/trust/gate.py`
- Test: `tests/test_trust_gate.py`

**Interfaces:**
- Produces `payment_hold_detail(tenant_id, case_id, *, url=None, settings_store=None) -> dict` with keys `held: bool`, `message: str | None`, `holds: list[{finding_id, type, engine, summary}]`, `waiting: list[str]`, `transaction_node: str | None`. `payment_hold()` returns `payment_hold_detail(...)["message"]`. The message text is unchanged (`_message`).

- [ ] **Step 1: Failing tests** (extend the file's `hold()` helper with `detail()`): `test_detail_lists_holding_findings_with_ids()`, `test_detail_waiting_when_unchecked()` (`waiting == ["payment"]`, `holds == []`, `held`), `test_detail_none_when_no_engine_enforcing()` (`held is False`, `message is None`), and every existing test unchanged.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** by refactoring the current body into `payment_hold_detail`. **Step 4: Run** `pytest tests/test_trust_gate.py -q` → PASS. **Step 5: Commit.**

---

### Task 6: Hold detail at approval, export and `GET /api/invoices/{case_id}`

**Files:**
- Modify: `invoice_processing/approval_flow.py` (`payment_hold(ctx) -> dict | None` returns the detail when `held` else None; `Decision.hold_overridden: dict | None`; `decide()` raises `PaymentHold(detail["message"])` and stores `{"message", "finding_ids"}`), `invoice_processing/api/service.py` (actions L1670-1689: same shape, audit payload `{"hold": message, "finding_ids", "override_reason"}`; export L2013-2021: `_hold_overridden_covers(state.get("hold_overridden"), detail) -> bool` = `isinstance(stored, dict) and not detail["waiting"] and {h["finding_id"] for h in detail["holds"]} <= set(stored["finding_ids"])`; `get_invoice` adds `"hold": approval_flow.payment_hold(ctx)` and `"trust_node"` via `get_graph_store().nodes_by_ref(tenant, "invoices", [invoice_id])` when a graph store and `Invoice` row exist, else `None`)
- Test: `tests/test_approval_routes.py:1485-1506`, `tests/test_export_path.py:519-585` (the existing `gate.payment_hold` monkeypatches become `gate.payment_hold_detail` returning the dict), `tests/test_trust_gate.py` for `_hold_overridden_covers`

- [ ] **Step 1: Failing tests:** `test_export_refuses_when_a_new_finding_holds_after_override()` (override with finding ids `{a}`, detail now `{a, b}` → 409), `test_a_legacy_string_override_no_longer_passes()` (Review Focus 3), `test_get_invoice_carries_hold_and_trust_node()` (with the gate monkeypatched → `body["hold"]["held"] is True`; without a graph store → `None`).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** `pytest tests/test_approval_routes.py tests/test_export_path.py tests/test_trust_gate.py tests/test_invoice*.py -q` → PASS. **Step 5: Commit.**

---

### Task 7: API-key resolver hook in `auth.py`

**Files:**
- Modify: `invoice_processing/api/auth.py`, `invoice_processing/api/service.py` (register `auth.set_api_key_resolver(lambda token: apikeys.resolve(token))` next to `set_sso_services`)
- Create: `invoice_processing/trust/apikeys.py` (`PREFIX = "ik_"`, `def resolve(token: str) -> Principal | None: return None`)
- Test: `tests/test_auth.py` (or the module that tests `require_role`; grep `set_verifier` to find it)

**Interfaces:**
- `auth.API_KEY_HEADER = "X-API-Key"`; `auth.api_key_token(request) -> str | None` (header value, else the bearer when it starts with `ik_`); `auth.set_api_key_resolver(fn: Callable[[str], Principal | None] | None)`; `_resolve_principal`: `token = api_key_token(request)`; if token and a resolver is set → `p = resolver(token)`; `p` → return it; else continue with the bearer path (a bearer that is an `ik_` token and resolves to None → `AuthError("Invalid API key.")`).

- [ ] **Step 1: Failing tests:** `test_api_key_header_reaches_the_resolver()` (resolver returns a Principal → `/api/me` 200 with its tenant), `test_a_bearer_wins_over_a_key()` (Review Focus 4: both headers, bearer tenant returned, resolver never called), `test_an_unknown_key_is_401()`.
- [ ] **Step 2–5:** FAIL → implement → `pytest tests/test_auth*.py tests/test_tenant_isolation_api.py -q` PASS → commit.

---

### Task 8: Graph, history, provider seams

**Files:**
- Modify: `invoice_processing/trust/graph/sync.py` (`_sync_v1_invoices` attrs += `service_period_start`, `service_period_end`, `work_authorization_reference`, `project` from `extracted`; line attrs += `unit` from `ext`; `_HISTORY_ORDER` += ticket 7, timesheet 8, shipment 9, seat_count 10; edge rule `elif kind in ("shipment", "ticket", "timesheet") and po_key in po_nodes: _edge(..., "fulfils", source, at, 0.9)`), `invoice_processing/trust/graph/lineage.py:117` (`quantity(node_id, line_kind, attrs: tuple[str, ...])` → first non-None attr; GRN call passes `("qty_received", "quantity")`), `invoice_processing/trust/history/mapping.py` (KINDS/FIELDS/REQUIRED/_SYNONYMS per the spec), `invoice_processing/trust/providers/base.py` (`class Usage(Protocol): def seats(self, vendor: str, period: str) -> dict`, `METHOD["usage"] = "seats"`), `invoice_processing/trust/providers/fakes.py` (`seats = _answer`)
- Test: `tests/test_trust_graph_sync.py`, `tests/test_trust_lineage.py`, `tests/test_trust_history_import.py`, `tests/test_trust_providers.py`

- [ ] **Step 1: Failing tests:** `test_invoice_nodes_carry_service_fields_and_line_units()`; `test_new_history_kinds_project_with_fulfils_when_the_po_exists()` and `test_new_history_kinds_without_a_po_get_only_issued_by()` (Review Focus 5); `test_history_grn_quantity_counts_in_lineage()` (grn node with `quantity` only → stage `found`/`contradicts` by number, not 0); `test_new_kinds_validate_required_fields()`; `test_fake_answers_usage()`.
- [ ] **Step 2–5:** FAIL → implement → `pytest tests/test_trust_graph_sync.py tests/test_trust_lineage.py tests/test_trust_history_import.py tests/test_trust_providers.py tests/test_trust_routes.py -q` PASS → commit.

---

### Task 9: Engine/score skeletons, route stubs, router mounts, evaluate score hook

**Files:**
- Create: `invoice_processing/trust/reality/__init__.py` (`ENGINE = "reality"`, `ENGINE_VERSION = "reality-0"`, `run(ectx) -> list: return []`, the six `register_type` calls matching `types.TABLE`), `invoice_processing/trust/score/__init__.py` (`SCORE_VERSION = "score-1"`, `def compute(ectx, report: dict) -> None: return None`), `invoice_processing/trust/explain/templates/reality.py` (module docstring only), `invoice_processing/api/trust_score_routes.py`, `invoice_processing/api/trust_reality_routes.py`, `invoice_processing/api/trust_provenance_routes.py`, `invoice_processing/api/integration_routes.py`, `invoice_processing/api/v2_routes.py`
- Modify: `invoice_processing/trust/explain/templates/__init__.py` (import `reality` where the other template modules are imported), `invoice_processing/trust/evaluate.py` (after the loop: `if any(isinstance(r, dict) for r in report.values()): try: from .score import compute; compute(base, report) except Exception: logger.warning("trust_score_failed", extra={"error": type(exc).__name__})`), `invoice_processing/api/service.py` (import + `include_router` for the five)
- Test: `tests/test_trust_engine.py` (score hook called with the report; a raising `compute` does not fail the run), `tests/test_tenant_isolation_api.py` (PROBED += the stub paths; `EXTRA_PROBE_MODULES` += the four branch modules; route-module tuple += the five modules), `tests/test_trust_routes.py` (each stub returns 503 for a viewer of either tenant)

Stub routes (every handler `Depends(require_role(...))` as the spec's roles; body `raise HTTPException(503, "Not built yet")`): `GET /api/trust/transactions/{node_id}/trust` viewer, `GET /api/trust/vendors/{node_id}/trust` viewer, `GET /api/trust/posture` viewer, `GET /api/trust/readiness` admin, `POST /api/trust/engines/{engine}/mode` admin; `POST /api/trust/findings/{finding_id}/evidence-request` clerk, `GET /api/trust/evidence-requests` viewer, `POST /api/trust/evidence-requests/{request_id}/proof` clerk; `POST /api/vendors/{vendor_id}/documents` clerk, `POST /api/trust/documents/{document_id}/authenticity` viewer. `integration_routes.router` and `v2_routes.router = APIRouter(prefix="/v2")` are empty.

- [ ] **Step 1–5:** failing tests → FAIL → implement → `pytest tests/test_trust_engine.py tests/test_tenant_isolation_api.py tests/test_trust_routes.py -q` PASS → commit.

---

### Task 10: Worker webhook sweep

**Files:**
- Modify: `invoice_processing/worker/runner.py` (`WEBHOOK_SCAN_SECONDS = 60`; `self._last_webhook_scan = None` in `__init__`; `_deliver_webhooks_if_due()` mirroring `_escalate_if_due`, calling `due(datetime.now(timezone.utc))` from `..trust.webhooks.deliver` inside try/except with `logger.exception("Webhook delivery sweep failed; continuing.")`; called from `run_forever` after `_escalate_if_due`)
- Test: `tests/test_trust_worker.py`

- [ ] **Step 1: Failing test:** `test_webhook_sweep_runs_once_per_interval(monkeypatch)` — monkeypatch `deliver.due` with a counter; two calls to `_deliver_webhooks_if_due()` at the same clock → 1 call; advance the clock 61 s → 2.
- [ ] **Step 2–5:** FAIL → implement → `pytest tests/test_trust_worker.py -q` PASS → commit.

---

### Task 11: Benchmark hooks and e2e seed

**Files:**
- Modify: `benchmark/history/generate.py` (`KINDS` += the four; `HEADERS` = the spec's FIELDS; the generator writes an empty CSV with headers for each new kind), `benchmark/history/eval.py` (`_reality_module()`, `_score_module()` like `_explain_module`; when present, `out["reality"] = reality.measure(tenant_id, graph, findings, manifest, url)` / `out["score"] = …`, gates merged into `out["gates"]`), `tests/test_benchmark_history.py` (`wave0_only` also sets `_reality_module`/`_score_module` to `lambda: None`), `scripts/e2e_api.py` (`seed_finding()` also upserts a `REAL-NO-EVIDENCE` finding `dedupe_key=f"REAL-NO-EVIDENCE:invoices:{invoice_id}"` citing the invoice node with `details={"mode": "advisory", "engine": "reality", "hold": False, "result": "NO_EVIDENCE"}`, a `trust_score` node `natural_key=f"trust_score:invoices:{invoice_id}"` with the spec's shape (six dimensions, `composite 0.72`, `reality.value 0.4`), and a `trust_holds` row for the `LEAK-RATE` finding via `holds.place` inside a `tenant_session` — importing `invoice_processing.trust.reality` so its types register)
- Test: `tests/test_benchmark_history.py` (baseline tests unchanged and green; `test_eval_merges_optional_wave3_gates(monkeypatch)` with fake modules returning `{"gates": {"x": True}}`)

- [ ] **Step 1–5:** failing test → FAIL → implement → `pytest tests/test_benchmark_history.py -q` PASS; `./.venv/bin/python -m benchmark.history.eval --seed 7` still 37/37 → commit.

---

### Task 12: Frontend shell, docs, PENDING

**Files:**
- Modify: `frontend/src/lib/api.ts` (types per the spec; `InvoiceDetail.hold: HoldDetail | null; trust_node: string | null`; `Workflow.hold_overridden?: {message: string; finding_ids: string[]} | null`), `frontend/src/lib/trust.ts` (`ENGINE_LABELS.reality = "Reality"`, `DIMENSION_LABELS`, `REALITY_LABELS`), `frontend/src/components/AppShell.tsx` (three NAV entries after Trust audit), `frontend/e2e/golden-path.spec.ts:217` (labels `"Trust posture"`, `"Trust engines"`, `"Integrations"`), `tests/test_frontend_reaches_the_flow.py:24` (`"trust-posture", "trust-engines", "integrations"`), `docs/PENDING.md` (Wave 3 bullets: program plan path, scaffold ticked when merged, five branches unchecked, deferrals: outbox retention, notifications, public upload links, copy-move, live connectors, rate limiting/idempotency, partner gates)
- Create: `frontend/src/app/trust-posture/page.tsx`, `frontend/src/app/trust-engines/page.tsx`, `frontend/src/app/integrations/page.tsx`, `frontend/src/app/vendors/[id]/page.tsx` — each `"use client"`, an `<h1>` equal to the NAV label (vendor page: `Vendor`), one `<p className="muted">` line saying the branch fills it.

- [ ] **Step 1: Run** `./.venv/bin/python -m pytest tests/test_frontend_reaches_the_flow.py -q` → FAIL (pages missing). **Step 2: Implement.** **Step 3: Run** that test, `cd frontend && npm run lint && npx tsc --noEmit && npm run build && npm run test:e2e` → PASS (41 + 3 nav clicks). **Step 4: Commit.**

---

### Task 13: Whole-branch verification

- [ ] `./.venv/bin/python -m ruff check invoice_processing tests benchmark && ./.venv/bin/python -m ruff format --check invoice_processing tests benchmark`
- [ ] `./.venv/bin/python -m pytest -q -p no:cacheprovider` → all pass.
- [ ] Postgres: `env -u DATABASE_URL TEST_DATABASE_URL=postgresql+pg8000://postgres:postgres@localhost:55433/trust_w3_scaffold ./.venv/bin/python -m pytest tests/test_trust_*.py -q -p no:cacheprovider` (fresh database; if Docker is down, say so in the PR).
- [ ] `./.venv/bin/python -m benchmark.history.eval --seed 7` → 37/37.
- [ ] Frontend: lint, tsc, build, Playwright.
- [ ] Fable review of the whole branch → one fix pass (each fix test-first) → PR.
