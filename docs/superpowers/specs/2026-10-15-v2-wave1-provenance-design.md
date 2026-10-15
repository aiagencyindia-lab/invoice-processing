# Provenance v1 engine (FR-22, part) — Wave 1 spec

Date: 2026-10-15. Parent: `2026-10-14-v2-trust-layer-design.md` §3.1; scaffold:
`2026-10-15-v2-wave1-scaffold-design.md`. Package `invoice_processing/trust/provenance/`,
`ENGINE_VERSION = "provenance-1"`. `PROV-TAMPER` is a hold type.

**Purpose.** A deterministic authenticity score per document, template
consistency against the vendor's history, tamper detection after approval,
and a provenance ledger.

**Inputs.** Document bytes → `pdfplumber` metadata (Producer, Creator,
CreationDate, ModDate), page count, page sizes, font names, text-layer presence
(`confidence.has_readable_text`); the vendor's earlier `document_version`
nodes; approval nodes; invoice attrs `document_sha256, decision,
invoice_number`. No providers.

**Ledger.** Node `document_version` (natural key `documents:{id}:{sha}`;
attrs `sha256, source, sender_domain, producer, creator, creation_date,
mod_date, page_count, has_text, fingerprint, channel, template, score,
components, actor`), edge `evidences` → invoice (`system_record`, 1.0).
Graph-only; no table (scaffold ruling 1).

**Score (deterministic).** `channel`: email from `vendor.email_domain` 1.0 /
api 0.8 / upload 0.6 / other 0.3. `template`: share of the vendor's prior
fingerprints that match; 1.0 while history is below `template_min_history`.
`text`: 1 or 0. `meta`: 1 when a producer is present and
`creation ≤ mod ≤ now`. `score = 0.4·channel + 0.3·template + 0.2·text +
0.1·meta`. Fingerprint = sha256 of rounded page size + sorted font set +
producer.

**Findings.**
- `PROV-SUSPECT` (advisory): score < `suspect_below`; impact None; cites the
  `document_version`.
- `PROV-TAMPER` (**hold**): another invoice with the same normalised number
  and vendor that was approved, with a different `document_sha256`; impact the
  invoice amount; cites both invoices and both versions.

**Thresholds.** `provenance.suspect_below 0.5, template_min_history 3`.

**Benchmark `faults/provenance.py`.** `seed_graph` plants approved history
invoices with a prior sha, then re-submits a changed one. Gate: `PROV-TAMPER`
precision = recall = 1.0.

**Non-goals (Wave 1).** KYC and delivery proofs, a forensic/ML ensemble,
image analysis, OCR (a missing text layer only lowers the score), non-PDF
documents (skipped and logged).
