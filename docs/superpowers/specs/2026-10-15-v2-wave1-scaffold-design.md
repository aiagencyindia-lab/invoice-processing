# Wave 1 scaffold — the shared extension points for four parallel engines

Date: 2026-10-15. Parent: `2026-10-14-v2-trust-layer-design.md` §2 (Wave 1), §3.1.
Program plan: `docs/superpowers/plans/2026-10-15-v2-wave1-program.md`.

The four Wave 1 engines (Leakage, Payment Trust, Provenance v1, GST/ITC v1)
are built at the same time in four worktrees. That is safe only if no two
branches edit the same file, so every shared change lands first, here.

## Rulings

1. **No Wave 1 migration.** Clauses, provenance records and verification
   evidence are graph nodes; a hold is an open finding of a hold type. The
   citation trigger only accepts node ids, so anything citable is a node
   anyway, and nodes get time-travel, RLS and masking for free.
2. **Engines register by convention.** `invoice_processing/trust/<name>/`
   exposes `run(ectx) -> list[FindingSpec]` for every name in
   `modes.ENGINES`; a missing package is skipped and logged.
3. **Mode semantics are generic** and live in `evaluate.py`: `shadow` writes
   findings with `details.mode="shadow"`; `advisory` writes and surfaces;
   `enforcing` additionally lets hold-type findings gate approval and export.
4. **One setting `trust_thresholds`**, every name declared in
   `trust/thresholds.py`.
5. **One hold gate** `trust/gate.py: payment_hold(tenant_id, case_id)`, called
   from `approval_flow.decide` (chain-completing step, beside `over_billed`)
   and `service.export_invoice`. Admin override with a reason. Fail-closed for
   hold types in `enforcing`; fail-open when nothing was evaluated yet (the
   approval-before-evaluation race is a Wave 2 item).
6. **Contracts arrive through a trust route**, not the PO/GRN flow.
7. **Engine failure**: insight engines log and continue; an engine that owns
   hold types, in `enforcing`, writes `TRUST-ENGINE-FAILED` (hold) and a later
   clean run retires it.
8. **Benchmark fault families are plug-ins** under `benchmark/history/faults/`.

## Node and edge kinds added in Wave 1

| Kind | Owner | Notes |
|---|---|---|
| node `contract` (ref `documents`) | scaffold route | `attrs`: filename, sha256, vendor_id |
| node `contract_clause` | leakage | `type, params, page, excerpt, confidence, reviewed` |
| node `document_version` (ref `documents`) | provenance | the provenance ledger |
| edge `governs` contract → vendor | scaffold route, else the next full sync | `human_assertion` (the uploader named the vendor) |
| edge `part_of` clause → contract | leakage | `model_inference` |
| edge `governed_by` invoice → contract | leakage | `model_inference` |
| edge `evidences` document_version → invoice | provenance | `system_record` |

## Runtime API every engine consumes

```python
@dataclass
class EngineContext:
    ctx: RunContext; mode: str; document: dict | None; invoice_node: str | None
    graph: GraphStore; links: LinkStore; findings: FindingStore; providers: Registry
    settings: dict; decider; blobs; url: str | None; now: datetime
    def threshold(self, name: str) -> float
    def transactions(self, as_of=None) -> list[Transaction]   # memoised per evaluate()
```
`run(ectx)` returns `FindingSpec`s; `evaluate()` stamps `details["mode"]` and
upserts them. `document` is `None` for a batch (tenant-wide) run.
`register_type(code, description, *, engine, hold=False)` declares a type;
`HOLD_TYPES` and `TYPE_ENGINE` feed the gate.

## Thresholds (defaults)

`leakage.desc_match 0.6, dup_window_days 7, split_window_days 3,
rate_tolerance_pct 0.5, clause_min_confidence 0.7, renewal_notice_days 60,
max_pages 40` · `payment.amount_zscore 3.0, min_history 5,
cadence_ratio_min 0.25, name_match_min 0.8, bec_hold_signals 2,
lookalike_ratio 0.8, bank_change_recent_days 30` · `provenance.suspect_below 0.5, template_min_history 3`
· `gst.nonfiler_missed_periods 2, new_vendor_days 90, first_invoice_multiple 3,
risk_inactive 1.0, risk_nonfiler 0.5, risk_shared 0.3, risk_new 0.2`.

## Sync additions (the only Wave 1 edit to the graph builder)

Invoice attrs += `document_id, document_sha256, document_source,
document_source_ref, document_sender_domain, document_reply_to_domain (domains only;
engines read full addresses from `ectx.document`), payment_terms,
due_date, total_ex_tax, tax_amount, place_of_supply, customer_tax_id,
discount`; line attrs += `hsn_sac, line_discount`; vendor attrs +=
`bank_verified_at, bank_account_name`; history nodes carry every mapped field
of their kind; `bank_line` and `vendor_statement_line` are ordered after
payments.

## File ownership for the engine branches

| Branch | MAY touch | MUST NOT touch |
|---|---|---|
| `v2/w1-<engine>` | `invoice_processing/trust/<engine>/**`, `tests/test_trust_<engine>*.py`, `tests/fixtures/trust_<engine>/**`, `benchmark/history/faults/<engine>.py`, `docs/superpowers/plans/2026-10-15-v2-wave1-<engine>.md` | everything else: `modes.py, evaluate.py, engine.py, gate.py, thresholds.py, tenant_settings.py, models.py, migrations/**, ci.yml, PENDING.md, graph/*, findings/*, providers/*, trust_routes.py, generate.py, eval.py, approval_flow.py, service.py, duplicates.py, confidence.py, frontend/**` |

A helper an engine needs from a shared module is written inside the engine
package; a post-wave cleanup promotes it. A needed sync/graph change is a
scaffold follow-up on `main`, then a rebase.

## Benchmark plug-in API

`benchmark/history/faults/<engine>.py` exports `inject(rng, rows, faults, *,
start, months) -> None`, `GATES: dict[type, {"precision", "recall",
"impact_error"}]`, optional `seed_graph(ctx, graph, data_dir)` and
`provider_scripts`. `eval.py` runs every engine in `advisory` with fake
providers and scores per finding type; a type with no module is not gated.
