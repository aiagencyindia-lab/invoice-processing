# How an invoice travels through this system

Read before the benchmark numbers: what follows is the path each of the 300
submissions takes, with the file and line where each step happens. Nothing here
is inferred from documentation — it is read from the code at `260d7e9`.

```
UPLOAD ─► EXTRACTION ─► VALIDATION ─► DECISION ─► REVIEW ─► DATABASE ─► METRICS
```

## UPLOAD

`POST /api/documents` (`invoice_processing/api/ingest.py:236`, role `clerk`) or,
for the benchmark, `ingest.ingest_document(ctx, bytes, ...)` (`ingest.py:131`)
called in-process — the same function the route calls, with an explicit
`RunContext`. There is no other way in.

1. `check_acceptable` — non-empty, ≤ 25 MB, must begin `%PDF` (`ingest.py:99`).
2. SHA-256 of the bytes; `documents.find_by_sha(tenant, sha)` (`ingest.py:159`).
   **A byte-identical re-upload is refused here, before the pipeline runs**, and
   returns `IngestResult(duplicate=True)`. 27 of the dataset's 51 duplicates are
   byte-identical, so their duplicate detection happens at this seam, not at
   check 3.1.
3. Blob stored content-addressed at `documents/<sha>.pdf`; tenant prefix added
   by the store (`LocalBlobStore` locally, `GcsBlobStore` when `GCS_BUCKET` is
   set — `api/service.py:451`).
4. A `documents` row (UUID; that UUID becomes the case id), an audit event
   `document_ingested`, and last a queue message `document.ingested`
   (`worker/runner.py:779`). Publish failure is logged, not raised: a stuck
   document is recoverable, a lost one is not.

## EXTRACTION

`Worker.handle` claims the document (`runner.py:585`) and calls
`process_document` (`runner.py:192`), which materialises **only `invoice.pdf`**
into a temp case folder (`runner.py:212` — no WAF, no sidecar), loads the
tenant's settings into the engine's module-global config
(`tenant_settings.apply_to_pipeline`, `:267`), and runs the 9-agent pipeline
(`shared_libraries/acting/general_invoice_agent.py::process_invoice`, `:2340`).

| Agent | What it does | LLM |
|---|---|---|
| 1 Classifier `:691` | PDF → text (`extract_pdf_with_fallback:463`, Gemini OCR then pdfplumber), then one Flash call returning `{has_invoice, has_work_authorization, invoice_count}` (`DocumentContent:308`). **It knows two document types.** A receipt or proforma with an invoice-shaped header is classified as an invoice. | 2 |
| 2 Extractor `:838` | One Gemini **Pro** call with the PDF and `InvoiceExtraction` (`:340`) as the schema. Also computes the tax-ID checksum into `invoice["_tax_id_validation"]` (`:849`) and per-field grounding/`_routing` (`confidence.py`). | 1 (+1 if a WAF was found) |
| 3–6 Phase validators | Deterministic Python. See the validation map. | 0 |
| 7 Transformer `:1730` | Line-item mapping; may call Flash for item codes unless the cache covers them. | 0–1 |
| 8 Output generator | `08_decision.json` + `Postprocessing_Data.json`. | 0 |
| 9 Audit logger `:2087` | `09_audit_log.json` with `processing_time_seconds`, `total_llm_calls`, tokens, cost. | 0 |

Roughly 4 Gemini calls and ~60 s per invoice. Every call is `temperature=0`
with `seed=42` (`:124`), and every call has **no timeout** (`:455/565/608`) —
the observed 15–25 minute hangs have no upper bound in code.

## VALIDATION and DECISION

Four phases, each writing `0[3-6]_phaseN_validation.json`. **The pipeline stops
at the first failing phase** (`_run_pipeline:2235` → `_handle_phase_rejection`
`:2159`), and only the first failing step of that phase reaches
`08_decision.json` (`:1191`). So a rejected invoice has no verdict for most
checks — the benchmark scores that as `NOT_RUN`, never as a pass.

`_settle` (`:1034`) is the single definition of "failed": `passed is False and
not informational`. It also marks steps the tenant has switched off as
`skipped` with `passed: None` — a check nobody ran has no verdict.

Terminal states are `ACCEPT`, `REJECT` and `ERROR`. `SET_ASIDE` / "To Verify"
exists in the status map but this pipeline never produces it. Confidence
routing (`confidence.py:357`) yields `AUTO`/`REVIEW` into `invoice["_routing"]`
and is **advisory only** — no phase reads it.

## REVIEW

`08_decision.json` and the projected columns feed `/invoices`; a reviewer
approves or rejects through `act()` (`api/service.py`), which records the human
decision and its agreement with the machine (`case_projection.agreement`).
Nothing in this benchmark exercises the human half — every decision measured
here is the pipeline's own.

## DATABASE

`process_document` copies each artifact through the `CaseRepository` port
(`runner.py:260`), applies the tenant's ALF rules (`_apply_rules:163`, shadow by
default, never fails an invoice), then projects into the relational view
(`case_projection.project_case`) — `invoices`, `findings`, `pipeline_runs`.
Locally the repository is Postgres-on-SQLite; artifacts live in `invoices.fields`.

The duplicate corpus for check 3.1 is read back out of exactly this projection
(`seen_invoices_for`, `runner.py:129`) — so a case that failed to project is
invisible to every later invoice's duplicate check.

## METRICS

`09_audit_log.json` per case, mirrored into `pipeline_runs` (`db/models.py:266`)
with `llm_calls`, `cost_usd`, `duration_ms`. The benchmark reads the artifacts
through the repository, which is the same seam the workbench reads.
