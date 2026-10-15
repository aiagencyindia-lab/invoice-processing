# Wave 1 scaffold — implementation plan

> Executed inline (superpowers:executing-plans), TDD per task, ledger at
> `.superpowers/sdd/2026-10-15-v2-wave1-scaffold/progress.md`.

**Spec:** `docs/superpowers/specs/2026-10-15-v2-wave1-scaffold-design.md` (APIs, kinds,
thresholds, ownership are fixed there). **Program:** `2026-10-15-v2-wave1-program.md`.
**Branch:** `v2/w1-scaffold` from `v2/wave0-foundation`.

Global constraints are Wave 0's (see `2026-10-14-v2-wave0-foundation.md`), plus: no
engine logic in this branch; every new public name matches the scaffold spec exactly,
because four implementers code against it in parallel.

## Tasks

| # | Task | Files | Tests that must fail first |
|---|---|---|---|
| 1 | Finding-type registry carries engine and hold flag; `list()` filters by transaction node | `trust/findings/model.py`, `trust/findings/store.py` | `test_register_type_records_engine_and_hold`, `test_hold_types_lists_only_holds`, `test_list_filters_by_transaction_node` (in `tests/test_trust_findings.py`) |
| 2 | Thresholds: defaults module + one tenant setting | `trust/thresholds.py`, `tenant_settings.py` | `test_threshold_defaults_cover_every_wave1_name`, `test_trust_thresholds_rejects_unknown_name_and_bool`, `test_trust_thresholds_merges_over_defaults` (`tests/test_trust_settings.py`) |
| 3 | Engine runtime: `EngineContext`, convention discovery, mode stamping, failure contract, batch mode | `trust/engine.py`, `trust/evaluate.py` | `tests/test_trust_engine.py`: `test_missing_engine_package_is_skipped`, `test_off_engine_does_not_run`, `test_findings_are_stamped_with_mode_and_upserted`, `test_insight_engine_failure_is_logged_not_raised`, `test_hold_engine_failure_in_enforcing_writes_engine_failed_and_a_clean_run_retires_it`, `test_contract_document_runs_engines_without_an_invoice`, `test_batch_run_has_no_document`, `test_threshold_reads_tenant_override_then_default`, `test_transactions_is_memoised` |
| 4 | Hold gate + the two v1 call sites | `trust/gate.py`, `approval_flow.py`, `api/service.py` | `tests/test_trust_gate.py`: `test_no_graph_store_means_no_hold`, `test_open_hold_in_enforcing_holds`, `test_advisory_or_rejected_does_not_hold`, `test_final_approval_is_refused_while_held`, `test_admin_override_with_reason_passes`, `test_export_is_409_while_held` |
| 5 | Sync additions | `trust/graph/sync.py` | in `tests/test_trust_graph_sync.py`: `test_invoice_node_carries_document_and_tax_attrs`, `test_lines_carry_hsn_and_discount`, `test_vendor_carries_bank_verification`, `test_history_nodes_carry_every_mapped_field`, `test_statement_and_bank_lines_sync_after_payments` |
| 6 | Contract upload route | `api/trust_routes.py`, `tests/test_tenant_isolation_api.py` | `tests/test_trust_routes.py`: `test_contract_upload_stores_document_node_and_governs_edge`, `test_contract_upload_refuses_non_pdf_and_unknown_vendor`, `test_contracts_list_is_tenant_scoped`; isolation probe |
| 7 | Benchmark plug-ins | `benchmark/history/faults/__init__.py`, `generate.py`, `eval.py` | `tests/test_benchmark_history.py`: `test_fault_modules_are_discovered_and_inject_rows`, `test_eval_runs_engines_and_scores_per_type`, `test_type_without_a_module_is_not_gated`, existing Wave 0 gates still pass |
| 8 | CI list, `.gitignore`, PENDING | `.github/workflows/ci.yml`, `.gitignore`, `docs/PENDING.md` | none (config); full suite + Postgres list + eval at the end |

Each task: write the named tests, watch them fail, implement, run the task's test files
plus the suites that own the touched v1 files (`test_approval_flow*`, `test_export*`,
`test_tenant_settings`, `test_config_routes`, `test_trust_*`), commit.

## Verification
Full SQLite suite; Postgres CI list on a local `postgres:16`; `python -m benchmark.history.eval --seed 7`
with no fault modules present still passes Wave 0's two gates; ruff clean. Then a `fable`
whole-branch review, one fix pass, PR.
