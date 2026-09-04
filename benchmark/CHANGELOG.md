# Benchmark changelog

Every change made because the benchmark found something. One entry per change,
newest last. A change with no root cause named is a change that should not have
been made.

Format: date · problem · root cause · fix · files · tests · result · regression.

---

## 2026-09-04 — Baseline established

**Problem.** The 14 validation checks had never been measured against anything
but three exemplary Australian cases.

**What was done.** Branched `benchmark/india-gst` from `260d7e9`; captured every
repo gate first (`benchmark/baseline/current-test-results.md`); vendored the
300-invoice synthetic dataset under `benchmark/dataset/` read-only (the 33 MB of
PDFs stay outside git); wrote the harness (`benchmark/harness/`) and the two
descriptive baselines (`architecture.md`, `current-validation-map.md`).

**Product changed.** Nothing. `pyproject.toml` gained a ruff exclusion for the
vendored dataset generator and a `PLC0415` exemption for the harness.

**Regression status.** All gates green (1434 tests, 21/21, 27/27, eval GATE
PASSED, 18 e2e).

### Configuration decision recorded here because it shapes every BEFORE number

`require_work_authorization` is **off** in the baseline profile. Check 1.4 can
only be satisfied by a Work Authorization *form inside the submission PDF*
(`waf_count`), and the worker materialises only `invoice.pdf`; this dataset
carries WA *reference numbers* instead. With the flag on, 9 of the 14 invoices
in the smoke run (`benchmark/runs/smoke15-require-wa-on/`) were rejected at 1.4
before phases 2–4 ran, which would have produced a baseline measuring one broken
check and nothing else. With it off, every other check is measured and 1.4's 48
expected failures are scored as false negatives — the honest reading: the
application cannot validate a work-authorization reference today. Both values
are reachable through the settings API, so this stays within "configured as an
administrator could configure it".

---

## 2026-09-04 — P0: a Vertex call that cannot hang

**Problem.** One benchmark document took 294 seconds against a ~60 second norm,
and the repo already records two 15–25 minute stalls.

**Root cause.** `model.generate_content` takes no timeout and the RPC beneath it
has none either, so a stalled request blocked its caller indefinitely. In the
worker that is worse than slow: the queue lease expires, the document is
redelivered, stalls again, and after three attempts an invoice nobody rejected
is dead-lettered. The critic had already hit this and had already fixed it
(`guarded_generate`) — in its own file, shared with nobody.

**Fix.** Moved the guard to `invoice_processing/llm_guard.py`; the critic
re-exports it so every existing caller and test keeps its import. The acting
pipeline's three Vertex calls now go through one `_generate` helper with
`VERTEX_TIMEOUT_SECONDS` (default 180). A timeout raises, so the classifier
falls back to keywords, extraction is marked failed, and the worker's existing
retry and dead-letter path does its job.

**Files.** `invoice_processing/llm_guard.py` (new),
`shared_libraries/investigation/investigate_agent_reconst.py`,
`shared_libraries/acting/general_invoice_agent.py`.

**Tests.** `tests/test_vertex_deadline.py` — a stalled call is abandoned; an
infrastructure error is retried; a malformed request is not; an AST sweep fails
if any future code calls `generate_content` outside `_generate`; the critic and
the pipeline share one guard object.

**Result.** 1464 passed. Reverting the routing fails
`test_no_pipeline_code_calls_vertex_directly`. The critic's own 13 tests pass
unchanged, which is what makes this a move rather than a rewrite.

**Regression status.** Green.

---

## 2026-09-04 — P1: a reference date step 3.3 can be held to

**Problem.** "Is this invoice dated in the future?" was answered against
`date.today()`, read at validation time, so the dataset's 48 future-dated
invoices stop being future-dated as the calendar advances and the benchmark
cannot be reproduced. Separately, the corpus prints seven date formats and
`parse_date` knew five — and an unparseable date makes step 3.3 emit no verdict
at all, so a future-dated invoice passes because nobody looked.

**Root cause.** No injection point for the clock; a hardcoded format list.

**Fix.** `_reference_date()` reads `INVOICE_REFERENCE_DATE` (ISO) and falls back
to today, including when the value is unreadable — a typo must not stop the
queue. An environment variable rather than a tenant setting: a customer who can
move the clock can accept invoices dated into next year. `_DATE_FORMATS` gains
`09-Aug-2026`, `09.08.2026`, `09 August 2026`, `09 Aug 2026`, `Aug 09, 2026`.

**Files.** `shared_libraries/acting/general_invoice_agent.py`.

**Tests.** `tests/test_reference_date.py` — the date comes from the environment;
unset means today; an unreadable value falls back rather than failing; the eight
printed formats parse; nonsense is still `None`.

**Result.** 1464 passed. Reverting to `date.today()` fails
`test_it_comes_from_the_environment`.

**Regression status.** Green.

---

## 2026-09-04 — P2: the tax-ID format is a customer's, and a checksum is half the check

**Problem.** Two things, found together. Every tenant validated supplier tax IDs
as Australian ABNs, because `tax_id_format` lives in master data and is the same
for everyone — so all 300 Indian GSTINs in the corpus were rejected for being
15 characters long, which is the right verdict for the 49 planted faults and
the wrong one for the other 251. And a checksum cannot answer the question that
catches fraud: an invoice quoting *another supplier's* registration is
arithmetically perfect. 18 of the 49 tax-ID faults are exactly that.

**Root cause.** A per-product setting where a per-customer one belonged, and a
control that stopped at arithmetic.

**Fix.**
- `tax_id_format` joins the settings allow-list, validated against the
  registered validators — an unknown format is refused rather than silently
  leaving tax IDs unchecked.
- `vendor_master.py`: an optional per-tenant supplier list (`GET`/`PUT
  /api/vendors`, admin to write). Step 3.2 now reports the checksum *and*
  whether the identifier is the one that supplier is registered under. With no
  master loaded the check is byte-for-byte what it was, because a tenant who
  has not listed their suppliers must not have every invoice rejected as coming
  from an unknown one.
- The GSTIN validator's `UNVERIFIED — DO NOT ENABLE` warning is removed: it now
  has a 49-identifier corpus (`tests/fixtures/gstin_corpus.json`) and accepts
  all 33 well-formed GSTINs while rejecting all 16 corrupted ones.
- `master_files.py`: one reader for tenant reference data, because the
  purchase-order loader had already been written twice and the copies disagreed.

**Bug found while doing it.** `po_matching.normalize_vendor` stripped legal
suffixes as bare substrings anywhere in the name: "Coca Cola Company" became
"CA LA MPANY" and "Consulting" became "NSULTING". Invisible while both sides
were mangled identically — and not invisible at all once a vendor master is
indexed by that key, where two different suppliers collapsing to one string
means one supplier's invoice is checked against another's tax ID. Now
whole-word and suffix-only, with "Pvt"/"Private" kept as one token so an Indian
private limited is not confused with its LLP namesake.

**Files.** `tenant_settings.py`, `paths.py`, `master_files.py` (new),
`vendor_master.py` (new), `po_matching.py`, `api/config_routes.py`,
`shared_libraries/acting/general_invoice_agent.py`, `tax_ids.py`.

**Tests.** `tests/test_vendor_master.py` (12), `test_tax_id_registry.py`
(+3 corpus tests), `test_po_matching.py` (+4 normalisation tests),
`test_tenant_isolation_api.py` (+1 cross-tenant probe, which the route-coverage
guard demanded before the suite would pass).

**Result.** 1484 passed. Reverting the tax-ID comparison fails
`test_another_suppliers_tax_id_is_caught`; reverting the normalisation fix
fails four named tests including two that predate this change.

**Regression status.** Green.

---

## 2026-09-04 — P3: what kind of document is this?

**Problem.** The classifier answered one question — "does this look like an
invoice?" — so a receipt, a quotation, a proforma, a credit note, a purchase
order or a delivery challan with an invoice-shaped header extracted cleanly,
passed check 1.1, and went on to be validated as a payable. That is 50 of the
benchmark's 300 submissions. And check 1.3 could not tell a Bill of Supply from
a tax invoice, because it tested only that three fields were present.

**Root cause.** `DocumentContent` had `has_invoice: bool` and nothing else. The
keyword fallback was worse: anything containing the word "invoice" was one.

**Fix.**
- `document_type` on the classifier's schema, with eleven named kinds and a
  prompt that says the document's own title is the strongest evidence. It flows
  into the classification artifact and the extraction artifact.
- Check 1.1 fails a document that is not payable, naming the kind in its
  evidence. Check 1.3 fails a non-tax invoice or one stating no tax at all,
  gated by a new `require_tax_breakup` setting — suppliers in plenty of places
  legitimately bill without tax.
- Check 1.2 gains `organization_tax_ids`: where the customer has stated their
  own registrations and the invoice states who it is billed to by number, they
  must agree. A name can be typed; a registration is issued.
- The keyword fallback reads the title, most specific first ("proforma invoice"
  before "invoice"), and counts only payable kinds as invoices.
- The extraction schema gains what the rest of the pack needs and nothing more:
  `document_type`, `customer_tax_id`, `work_authorization_reference`, `project`,
  service period, `place_of_supply`, `subtotal`, `discount`, `other_charges`,
  `round_off`, `cgst/sgst/igst_amount`; per line `unit`, `hsn_sac`, `tax_rate`,
  `line_discount`. Every one optional and defaulting to None, so an invoice
  that does not print them reaches the checks exactly as before. The prompt
  says null means "the invoice does not state it" — a computed subtotal would
  hide the discrepancy a later check exists to find.

**Files.** `shared_libraries/acting/general_invoice_agent.py`,
`tenant_settings.py`, `shared_libraries/invoice_master_data.yaml`.

**Tests.** `tests/test_document_taxonomy.py` (27): each non-payable kind fails
1.1; a Bill of Supply fails 1.3 and not 1.1; an untyped document behaves as
before; component tax counts as tax; a foreign registration fails 1.2; the
fallback reads titles.

**Result.** 1511 passed. Reverting either taxonomy branch fails 8 named tests.

**Regression status.** Green. Nothing here fires on an invoice that does not
carry the new fields, which is what keeps the existing pilot tenant unchanged.
