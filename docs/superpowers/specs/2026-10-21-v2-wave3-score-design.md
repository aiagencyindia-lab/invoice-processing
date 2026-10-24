# Wave 3 — Trust Score, vendor profile, posture, readiness (FR-25, U2) design (`v2/w3-score`)

**Program plan:** `docs/superpowers/plans/2026-10-21-v2-wave3-program.md`. **Scaffold contracts:** `docs/superpowers/specs/2026-10-21-v2-wave3-scaffold-design.md`. User decisions: U2 (operator-driven progression), U7 (score everywhere). Builder's calls B3 (score is a graph node), B4 (dimensions drive holds, composite never does).

**Owns:** `invoice_processing/trust/score/**` (`__init__` compute, `dimensions.py`, `vendor.py`, `posture.py`, `readiness.py`), `invoice_processing/api/trust_score_routes.py` (fills the stub), `benchmark/history/score.py`, `tests/test_trust_score*.py`, its plan doc. Nothing else.

## `compute(ectx, report) -> None`

Called by `evaluate()` after the engine loop (scaffold). For a per-document run: the invoice node; for a batch run: every invoice node. Reads the node's open findings (`ectx.findings.list(transaction_node=…)`), `TYPE_DIMENSION`, the `reality_check` node, the latest `document_version` node evidencing the invoice, the `approval` nodes (`approves` edges) and the workflow's `hold_overridden`. Writes a `trust_score` node (natural key `trust_score:{invoice natural_key}`; re-versioned on change by `upsert_node`):

```
{score_version: "score-1", computed_at,
 composite: float | null,
 dimensions: {identity: {value, hold, drivers: [finding ids], note}, authorization: {...},
              reality: {...}, price: {...}, authenticity: {...}, consistency: {...}},
 versions: {engines: {engine: engine_version seen}, provenance, reality, rules: {...}},
 holds: [finding ids], mode: {engine: mode}}
```

**Dimension rules** (`dimensions.py`, pure, table-driven):
- `identity` = 1 − max severity of open identity findings: `PAY-UNVERIFIED` / `PAY-NAME-MISMATCH` / `PAY-BEC` 1.0; `GST-INACTIVE` / `GST-UNVERIFIED` 1.0; GST insights 0.4; none → 1.0.
- `authorization` = chain complete with ≥ 1 distinct `decided_by` and no override 1.0; overridden 0.7; pending 0.5; rejected 0.0. No finding drives it (the v1 approval chain is the hold).
- `reality` = CORROBORATED 1.0 / PARTIAL 0.6 / NO_EVIDENCE 0.4 / CONTRADICTED 0.0 / no `reality_check` node → null.
- `price` = 1 − min(1, Σ open `LEAK-RATE|ESCALATOR|TIER|DISCOUNT|REBATE` impact ÷ invoice amount); no governing contract and an open `REAL-PRICE-JUMP` → 0.3; no contract and no history → null ("no baseline").
- `authenticity` = `document_version.score`; open `PROV-TAMPER` → 0.0; no node → null.
- `consistency` = 1 − 0.5 × |`PAY-ANOMALY` signals| (`first_time_vendor` → 0.5); open `REAL-VOLUME-SPIKE` → min(value, 0.4).
- `hold` per dimension = any open hold-type finding in it whose engine is `enforcing`. `composite` = mean of the non-null values; advisory.

## Routes (`trust_score_routes.py`)

- `GET /api/trust/transactions/{node_id}/trust?as_of` (viewer) → the score node attrs + `hold` (from `gate.payment_hold_detail`) + per-dimension finding rows.
- `GET /api/trust/vendors/{node_id}/trust` (viewer) → `{vendor, dimensions (means over the vendor's last 50 score nodes), findings_by_type {open, decided}, gst {status, missed_periods, observed_at} from the latest gstin provider_evidence, bank_verified from the latest bank_verification evidence, kyc: [document_version rows], network: [{vendor node, shared keys}] from natural_keys collisions + entity_links, timeline: [{month, composite}]}`.
- `GET /api/trust/posture?since` (viewer) → `{found, prevented, recovered, written_off, open_exposure, by_type, by_vendor, pre_payment_share, provenance_distribution (score buckets), verification_coverage {identity, reality, authenticity}, holds {open, placed, lifted}}` from `FindingStore.summary/events/count`, `trust_holds`, graph nodes. Money as Decimal strings; reconciles with the Wave 2 ledger totals.
- `GET /api/trust/readiness` (admin) → per engine `{mode, since, days_in_mode, findings, would_have_held, held, open_holds, triage {decided, accepted, rejected, precision}, failures, recommendation}`.
- `POST /api/trust/engines/{engine}/mode {mode, reason}` (admin) → `modes.set_engine_mode`, returns the readiness row. Never automatic.

**Readiness formula** (`readiness.py`, pure): `would_have_held` = hold-type findings with `details.mode ∈ {shadow, advisory}` since `since`; `held` = `trust_holds` placed since; `precision` = accepted / (accepted + rejected) over events with actor ≠ `system`; `failures` = `TRUST-ENGINE-FAILED` for the engine since. Recommendation: `off → shadow` always; `shadow → advisory` when `days ≥ readiness.min_days ∧ findings ≥ readiness.min_findings`; `advisory → enforcing` when `days ≥ min_days ∧ decided ≥ readiness.min_decided ∧ precision ≥ readiness.min_precision ∧ failures == 0`; insight-only engines: "enforcing adds nothing"; else `stay` naming the unmet condition.

## Benchmark

`benchmark/history/score.py::measure()` → `score_coverage == 1.0` (every evaluated invoice has a `trust_score` node) and `dimension_hold_consistency == 1.0` (every open hold-type finding's dimension value ≤ 0.5).

## Tests

Dimension table-driven incl. nulls; node versioning on re-run (same attrs → same id); readiness fixtures (precision math, every recommendation branch); posture reconciles with the ledger; routes with `PROBED` (`tests/test_trust_score_routes.py`); mode route audited and reconciles holds (outbox rows appear).

**Done:** gates green; ruff; suite on both dialects; probes.
