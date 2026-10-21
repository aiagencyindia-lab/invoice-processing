# Wave 3 — Scaffold design (`v2/w3-scaffold`)

**Program plan:** `docs/superpowers/plans/2026-10-21-v2-wave3-program.md` (decisions U1–U7, builder's calls B1–B9, ownership, execution). **Contracts:** `docs/superpowers/specs/2026-10-14-v2-trust-layer-design.md` §3.1.

The scaffold owns every shared file and lands first. It adds no behaviour a tenant sees (every engine mode stays `off` by default; every new route is a 503 stub until its branch fills it) except the hold ledger, the outbox rows and the richer hold detail on `GET /api/invoices/{case_id}`.

## Data contracts the branches build on

**`trust_holds` row** `{id, tenant_id, finding_id, transaction_node, engine, type, placed_at, placed_by, lifted_at, lifted_by, lift_reason}`. Open = `lifted_at IS NULL`; at most one open row per finding (`HoldStore.place` is a no-op on an open row).

**Webhook payload** (one outbox row per subscribed active endpoint): `{"event", "tenant", "occurred_at", "data"}`. `data` for `hold.placed`/`hold.lifted`: `{finding_id, type, engine, transaction_node, summary, impact, currency}` (plus `lifted_by`, `reason` on lift); for `finding.created`/`finding.decided`: the finding store dict without citation excerpts; `ping`: `{}`.

**`payment_hold_detail()`** → `{"held": bool, "message": str | None, "holds": [{finding_id, type, engine, summary}], "waiting": [engine, ...], "transaction_node": str | None}`. `payment_hold()` keeps its signature and returns `detail["message"]`.

**`Decision.hold_overridden`** → `{"message": str, "finding_ids": [..]} | None`; stored on the workflow artifact; export passes when `set(detail.holds ids) <= set(hold_overridden.finding_ids)` and `waiting == []`.

**`GET /api/invoices/{case_id}`** gains `hold` (the detail above; `held: false` without a graph store) and `trust_node` (invoice graph node id or `None`).

**`trust/types.py`**: `Type(engine, hold, dimension, description)`; `TABLE: dict[code, Type]` for every code of Waves 1–3; `DIMENSIONS = ("identity", "authorization", "reality", "price", "authenticity", "consistency")`. `findings/model.py` registers `TABLE` at import; `register_type(..., dimension=None)` fills `TYPE_DIMENSION`; a test asserts each engine's own `register_type` calls agree with `TABLE`.

**Wave 3 codes in `TABLE`** (engine, hold, dimension): `REAL-CONTRADICTED` (reality, hold, reality) · `REAL-PARTIAL` (reality, —, reality) · `REAL-NO-EVIDENCE` (reality, —, reality) · `REAL-PRICE-JUMP` (reality, —, price) · `REAL-VOLUME-SPIKE` (reality, —, consistency) · `REAL-SHUTDOWN` (reality, —, reality) · `PROV-KYC-SUSPECT` (provenance, —, authenticity) · `PROV-EDITED` (provenance, —, authenticity).

**Modes**: `ENGINES = ("leakage","payment","provenance","gst_itc","reality")`; `HOLD_ENGINES` gains `reality`; `CAPABILITIES` gains `usage`. `mode_since(tenant_id, engine, store=None) -> datetime | None` reads the settings key `trust_engine_mode_since` (written only by `set_engine_mode`, not in `SETTABLE`). `set_engine_mode(ctx, engine, mode, *, store=None, audit=None, now=None, reason="") -> dict` validates, writes both keys, audits `trust_engine_mode_changed {engine, from, to, reason}`, and calls `holds.reconcile(ctx, engine, enforcing=(mode == "enforcing"))`.

**Settings**: `trust_shutdown_periods: list` of `[from, to]` ISO-date pairs (validated, default `[]`).

**Thresholds added** (`thresholds.DEFAULTS`): `reality.qty_tolerance_pct 0.02`, `reality.hours_tolerance_pct 0.10`, `reality.seats_tolerance_pct 0.05`, `reality.evidence_window_days 45`, `reality.price_jump_zscore 3.0`, `reality.volume_zscore 3.0`, `reality.min_history 5`, `provenance.edits_suspect 2`, `readiness.min_days 14`, `readiness.min_findings 20`, `readiness.min_decided 10`, `readiness.min_precision 0.8`.

**Graph**: invoice nodes also carry `unit` (per line), `service_period_start`, `service_period_end`, `work_authorization_reference`, `project` (from `Invoice.fields["extraction"]`). History kinds `ticket`, `timesheet`, `shipment`, `seat_count` project to nodes of the same kind with `issued_by` → vendor and, when `po_number` matches a PO, `fulfils` → po (confidence 0.9). `lineage` GRN stage reads `qty_received` then `quantity`.

**History kinds** (`mapping.FIELDS` / `REQUIRED`): ticket `(external_id, vendor_key, doc_number, record_date, status, hours, project, po_number)` req `(vendor_key, doc_number)`; timesheet `(external_id, vendor_key, record_date, hours, rate, project, po_number, approved_by)` req `(vendor_key, record_date, hours)`; shipment `(external_id, doc_number, po_number, vendor_key, record_date, quantity, status, carrier)` req `(doc_number, record_date)`; seat_count `(external_id, vendor_key, record_date, product, quantity)` req `(vendor_key, record_date, quantity)`.

**Providers**: `Usage.seats(vendor, period) -> dict`; `METHOD["usage"] = "seats"`; the fake answers like every other capability.

**`evaluate()`**: after the engine loop, when at least one engine ran, `score.compute(ectx, report)` inside try/except (a failure is logged, never fails the run; the scaffold's `compute` is a no-op). Holds: after each `upsert` in `enforcing` mode for a type in `HOLD_TYPES`, `HoldStore.place` (emits `hold.placed`).

**`FindingStore`**: `upsert` emits `finding.created` when the result is `"created"`; `transition` emits `finding.decided` when `to_status in DECIDED`, and when `to_status == "rejected"` lifts the open hold (emits `hold.lifted`). Emission and hold writes share the finding's session.

**Auth**: `api_key_token(request)` reads `X-API-Key`, else a bearer starting `ik_`; `set_api_key_resolver(fn)`; `_resolve_principal` tries the resolver first — `None` falls through to the token path, an `AuthError` is a 401. The scaffold's `trust/apikeys.resolve` returns `None`.

**Route stubs** (fixed function names; every stub returns 503 `"Not built yet"`): `api/trust_score_routes.py` `transaction_trust(node_id)`, `vendor_trust(node_id)`, `posture(since)`, `readiness()`, `set_mode(engine, body)`; `api/trust_reality_routes.py` `create_evidence_request(finding_id, body)`, `list_evidence_requests(status, overdue)`, `fulfil_evidence_request(request_id, file)`; `api/trust_provenance_routes.py` `upload_vendor_document(vendor_id, …)`, `score_document(document_id)`; `api/integration_routes.py` (`router` only); `api/v2_routes.py` (`APIRouter(prefix="/v2")`, empty).

**Worker**: `WEBHOOK_SCAN_SECONDS = 60`; `_deliver_webhooks_if_due()` next to `_escalate_if_due`, importing `trust.webhooks.deliver.due(now, post=None, limit=50)` (scaffold: returns 0).

**Frontend types** (`lib/api.ts`): `TrustHold`, `HoldDetail`, `TrustScore {score_version, computed_at, composite, dimensions: Record<Dimension, {value: number | null, hold: boolean, drivers: string[], note: string}>, versions, holds, mode}`, `VendorTrustProfile`, `TrustPosture`, `Readiness`, `EvidenceRequest`, `ApiKey`, `ApiKeyCreated`, `WebhookEndpoint`, `WebhookDelivery`; `InvoiceDetail.hold`, `InvoiceDetail.trust_node`; `Workflow.hold_overridden`. NAV: Trust posture (`/trust-posture`), Trust engines (`/trust-engines`, admin), Integrations (`/integrations`, admin); placeholder pages with the `h1`; `vendors/[id]` placeholder.

**e2e seed** (`scripts/e2e_api.py`): one `REAL-NO-EVIDENCE` finding and one `trust_score` node (the shape above, all six dimensions present) for `case_005`, so the frontend branch's e2e runs without a worker. No `trust_holds` row: the banner reads the gate, which holds only with an enforcing engine; the frontend e2e sets a mode itself to drive it.

## Deliverables S1–S9

See the program plan Part 1. Tests per deliverable: schema/migration (`test_trust_schema.py`, `test_gcs_and_migrations.py`, `test_restore_check.py`), types registry agreement, `set_engine_mode` (writes, audits, reconciles), `HoldStore` (place idempotent, lift, reconcile both directions), outbox emission from store and evaluate, `payment_hold_detail` shapes (held / waiting / none), export id-set comparison (old string override still rejected), auth resolver precedence, sync projections and new kinds, lineage GRN key, thresholds snapshot, worker sweep, benchmark baseline unchanged, isolation probes for the five stub paths, frontend reach test, Playwright golden path (nav list).

## Builder's calls recorded here

- `trust_holds` is a ledger, not the gate's truth (B1).
- Outbox delivery is a sweep, not a queue job (B2).
- Scores and reality results are graph nodes (B3).
- One static type table importable without engines (B5).
- `trust_engine_mode_since` is a settings key the admin cannot write directly.
