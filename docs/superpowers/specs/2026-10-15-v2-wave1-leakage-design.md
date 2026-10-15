# Leakage engine (FR-20) — Wave 1 spec

Date: 2026-10-15. Parent: `2026-10-14-v2-trust-layer-design.md` §3.1; scaffold:
`2026-10-15-v2-wave1-scaffold-design.md`. Package `invoice_processing/trust/leakage/`,
`ENGINE_VERSION = "leakage-1"`. No hold types.

**Purpose.** Contract-vs-invoice compliance, duplicates beyond v1,
supplier-statement reconciliation, missed early-payment discounts, and a
renewal/escalator radar. Every finding cites the clause, line, invoice,
payment or statement nodes it rests on; ₹ impact is deterministic arithmetic.

**Inputs.** Contract documents (clause extraction when
`ectx.document["kind"] == "contract"`); `invoice`, `invoice_line`, `vendor`,
`payment`, `vendor_statement_line`, `contract`, `contract_clause` nodes;
`links.canonical_map`; `ectx.transactions()`. Contract PDF bytes via
`ectx.blobs.get(storage_key)` → `confidence.extract_page_text` and
`confidence.locate` for page citations. No providers.

**Clause extraction.** Deterministic candidate spans per page (regex: `%`,
₹/INR amounts, dates, "per unit / tier / rebate / escalat / renew / notice /
net N"). One `decide()` call per page classifies its candidates (`choice` ∈
rate|escalator|tier|rebate|discount|term|none plus a `score`); `params` are
parsed from the excerpt by regex, never by the model. Node `contract_clause`
(natural key `clause:{contract}:{page}:{sha(excerpt)[:12]}`; attrs `type,
params, page, excerpt, confidence, reviewed=False`), edge `part_of` → contract
(`model_inference`). Invoice → contract: edge `governed_by` to the vendor's
contract whose term covers the invoice date, else the latest. Cap
`leakage.max_pages`. A fallback answer is `none` (insight engine: skip, log).

**Findings.**
- `LEAK-RATE`: unit price > rate×(1+`rate_tolerance_pct`) on a matched item
  (token overlap ≥ `desc_match`); impact `(unit_price − rate) × qty`.
- `LEAK-ESCALATOR`: allowed = base×(1+esc)^years; impact as above.
- `LEAK-TIER`: cumulative qty in the period ≥ tier; impact
  `(unit_price − tier_price) × qty`.
- `LEAK-REBATE`: rolling spend ≥ threshold with no statement credit ≥ rebate;
  impact `spend × pct`.
- `LEAK-DISCOUNT`: `payment_terms` like `2/10 net 30` parsed deterministically;
  a payment on/before day d for the full amount; impact `amount × pct`.
- `LEAK-TERM`: invoice dated outside the contract term; impact None.
- `DUP-NEAR`: same canonical vendor, `normalize_number` equal or edit distance
  ≤ 2 with equal amount, within `dup_window_days`; impact the later amount.
- `DUP-SPLIT`: ≥ 2 invoices within `split_window_days`, each below
  `high_value_threshold`, summing to ≥ it; impact None.
- `DUP-CROSS`: same number + amount on two vendor nodes with one canonical;
  impact the amount.
- `STMT-UNAPPLIED-CREDIT` (statement credit with no matching payment; impact
  |amount|), `STMT-DOUBLE-PAID` (two payments settle one invoice, or statement
  payments exceed the invoice; impact the excess), `STMT-DISPUTED` (statement
  line unknown to us; impact None).
- `RADAR-RENEWAL`, `RADAR-ESCALATOR`: a term/escalator date within
  `renewal_notice_days`; impact None; dedupe per contract and month.

Finding confidence = rule confidence × clause confidence; an unreviewed clause
marks `details.needs_review`. Reuses `duplicates.normalize_number`,
`normalize_vendor`, `same_vendor`.

**Thresholds.** `leakage.desc_match 0.6, dup_window_days 7,
split_window_days 3, rate_tolerance_pct 0.5, clause_min_confidence 0.7,
renewal_notice_days 60, max_pages 40`.

**Benchmark `faults/leakage.py`.** Rate overcharge, near-duplicate, split,
unapplied credit, missed discount; `seed_graph` plants clause nodes from
`contract_clauses.json`. Gates: precision ≥ 0.9, recall ≥ 0.8,
impact_error ≤ 0.02.

**Non-goals (Wave 1).** `LEAK-SLA`, claim packs, a clause editor UI, scanned
contracts (OCR), FX, PO-based tiers.
