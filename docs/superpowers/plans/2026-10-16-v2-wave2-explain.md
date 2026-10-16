# Wave 2 — Causal Why engine (FR-24): implementation plan

Spec: `docs/superpowers/specs/2026-10-16-v2-wave2-explain-design.md`. Branch `v2/w2-explain`.
Contract: the TypeScript shapes in `frontend/src/lib/api.ts` (Explanation, EvidencePack, RatingSummary), field for field.

## Tasks (TDD, one commit each)

| # | Task | Files | Tests |
|---|---|---|---|
| 1 | Model + `check()` | `trust/explain/model.py` | `test_trust_explain.py::test_check_rejects_uncited_step`, `::test_check_rejects_empty_evidence_and_blank_step` |
| 2 | Counterfactual table | `trust/explain/counterfactual.py` | `::test_counterfactual[<TYPE>]` (parametrized over `TYPES`), `::test_counterfactual_without_impact_has_a_note` |
| 3 | Templates + engine | `trust/explain/templates/{__init__,leakage,payment,provenance,gst_itc,generic}.py`, `trust/explain/engine.py`, `tests/fixtures/trust_explain/<TYPE>.json` | `::test_every_type_explains[<TYPE>]` (check passes, ≥1 step, evidence ⊆ citations), `::test_unknown_type_falls_back_to_generic`, `::test_template_that_cites_an_uncited_node_is_refused` |
| 4 | Evidence pack + HTML | `trust/explain/pack.py`, `trust/explain/render.py` | `::test_pack_sha_is_stable_and_excludes_itself`, `::test_pack_edges_only_between_cited_nodes`, `::test_pack_html_escapes_script` |
| 5 | Ratings | `trust/explain/ratings.py` | `::test_rating_upserts_per_rater_and_audits`, `::test_ratings_aggregate_by_type` |
| 6 | Routes | `api/trust_explain_routes.py` | `test_trust_explain_routes.py::test_explanation_*`, `::test_evidence_pack_*`, `::test_rating_*`, `::test_uncited_template_is_a_500`, `TestExplainIsolation::test_explain_routes_stay_with_their_tenant`; module exports `PROBED` |
| 7 | Benchmark measure | `benchmark/history/explain.py` | `python -m benchmark.history.eval --seed 7` green with `explanation_citation_coverage`, `counterfactual_impact_error` |

## Rulings where the spec is silent
- `Explanation.check(cited)` is a method taking the citation ids; the engine calls it before returning, and the route turns `Uncited` into 500 + `explain_uncited` log.
- A counterfactual's `delta` is always the finding's impact (what the engine priced); `payable_now` is the transaction's `amount` attr when present, else None.
- `caused_by` persons: only `decided_by` of an `approves` edge into the transaction node (uncited → `node_id: None`).
- `evidence_pack(..., findings=...)` re-reads the finding through the store only when the dict has no `citations` (list rows); route passes the `get` dict.
- `lineage_version` is `"lineage-1"` (the lineage module exports none).

## Review focus (riskiest untested inputs)
1. A finding whose cited node was deleted from the graph (evidence `kind: "missing"`, label still cited).
2. `details` keys absent or of the wrong type (every template reads defensively; fixtures pin the happy path only).
3. Transaction node without an `amount` attr (payable_now None, delta still impact).
4. A rating note carrying HTML (`render.py` escapes; JSON passes it through).
5. Citation order/duplicates: pack `nodes` dedupes by id while `evidence` keeps citation order.
