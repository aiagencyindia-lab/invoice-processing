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

---

## 2026-09-04 — P4: work authorization by reference

**Problem.** Check 1.4 asked whether a Work Authorization *form* was attached to
the submission (`waf_count > 0`, counted by the classifier over the PDFs in the
case folder). The worker materialises only `invoice.pdf`, so that count is
always zero and the check could never pass — with the setting on, 9 of the 14
invoices in the smoke run died there before phases 2–4 ran at all. And it is
the wrong question: most customers do not attach the form, the invoice quotes a
reference, and the authorisation lives in their records. All 48 of the
benchmark's 1.4 failures are reference faults (missing, unknown, another
supplier's, expired); none is a missing form.

**Root cause.** `WorkAuthorizationExtraction.reference_number` was extracted and
read by nothing, and there was no concept of an authorization master anywhere.

**Fix.** `wa_matching.py`, the same shape as `po_matching`: the customer loads
what they authorised (`GET`/`PUT /api/work-authorizations`), check 1.4 asks
whether the quoted reference exists, belongs to this supplier and is active, and
check 4.3 asks whether the invoice stays inside it — hours, rate, project and
period, each reported separately, because billing 200 hours against 40 and
billing 40 hours at twice the rate cost the same and are different
conversations with the supplier. An attached form still satisfies 1.4, and with
no master loaded both checks behave exactly as they did.

**Bug found while doing it.** `_determine_work_type` matched `"pm"` as a bare
substring, so "Develo**pm**ent" classified as PREVENTATIVE — which is on the
exemption list, so a software invoice exempted itself from the authorization
check by spelling. Now whole-word, and the two divergent copies of the function
are one.

**Also fixed.** Check 4.3 used to be the last thing phase 4 did; restructuring
it revealed that an early return would have skipped 4.4 entirely. There is now a
test that phase 4 always reports all four of its steps.

**Files.** `wa_matching.py` (new), `api/config_routes.py`,
`shared_libraries/acting/general_invoice_agent.py`, `paths.py`,
`tests/test_tenant_isolation_api.py` (+probe, +PROBED entry).

**Tests.** `tests/test_wa_matching.py` (22).

**Result.** 1534 passed. Reverting the supplier comparison fails two named
tests; reverting the whole-word fix fails
`test_development_is_not_preventative_maintenance`.

**Regression status.** Green.

---

## 2026-09-04 — P5, P6, P7: matching and arithmetic against real invoices

**P5 — the purchase order is more than a number and a total.** The two-way
match compared a tax-inclusive invoice total against whatever amount the export
stated. The benchmark's purchase orders are stated net of tax, so every
correctly taxed invoice looked like 18% over-billing — the single most common
false rejection in the smoke run. `amount_basis` says which basis the amount is
on and the comparison follows it. Four more optional fields, each checked only
when the export carries it: `customer_name` (a purchase order raised by another
company in the group is not this company's commitment), `valid_from`/`valid_to`,
`approved_quantity` and `approved_unit_price` — twice the quantity at half the
price reconciles perfectly against a total and is not what was ordered.

**P6 — one blended rate cannot check a multi-rate invoice.** Check 4.4
multiplied the whole net total by a single rate looked up by currency, and
there was no INR entry, so every Indian invoice was measured against 10%. It
now takes the statutory rate per line from the tenant's `tax_rates_by_hsn`
table, falls back to the rate the invoice itself states, and only then to the
currency default — so a tenant with no table keeps exactly today's behaviour. An
invoice-level discount is apportioned across the lines before tax, which is how
the tax is actually computed; taxing the printed line values overstated it by
the whole discount. Freight and similar charges carry their own rate. And where
an invoice splits its tax into components, they must agree with where the supply
happened: charging one tax across a state line, or a split within one, is a
filing error the customer carries and a single total hides completely. New
`tax_rate_tolerance` (default 1.00), because 4.4 borrowed the two-cent balance
tolerance and per-line rounding exceeded it on almost every invoice.

**P7 — reconciliation that accounts for what invoices actually print.** 4.1
compared the stated total against `ex + tax`, so a discount, a freight line or a
rounding adjustment each read as an error the supplier had made. It is now
subtotal − discount + tax + other charges + round-off, every term defaulting to
zero. 4.2 gains the second arithmetic claim: each line must be the product it
says it is. A quantity changed without its total leaves the subtotal untouched
and was invisible to a sum.

**Files.** `po_matching.py`,
`shared_libraries/acting/general_invoice_agent.py`, `tenant_settings.py`,
`shared_libraries/invoice_master_data.yaml`.

**Tests.** `tests/test_phase4_reconciliation.py` (19),
`tests/test_po_matching.py` (+7).

**Result.** 1553 passed. Reverting the amount-basis fix, the discount
apportionment or the per-line arithmetic each fails named tests (6 in total).

**Regression status.** Green. Every new comparison is skipped when its field is
absent, so an invoice from the existing pilot tenant is matched and reconciled
exactly as before.

---

## 2026-09-05 — 88 baseline invoices were re-run: the worktree had no credentials

**What happened.** Scoring the finished baseline showed 138 documents failing
check 1.1. Reading their evidence rather than their count: 75 said *"Unable to
find your project. Please provide a project ID"*. The worktree pinned to the
pre-fix commit was created by `git worktree add`, and `.env` is gitignored — so
those invoices never reached Vertex at all. Their extraction failed for want of
a credential, and the benchmark would have recorded it as the application
failing to read an invoice.

**How it was caught.** By opening the evidence strings behind a suspicious
number. 138 extraction failures against a corpus of 250 real invoices is not a
plausible result, and the scorer reports evidence per row precisely so that a
number can be interrogated instead of believed.

**What was done.** `.env` copied into the worktree; the 75 credential failures
and the 13 errored documents removed from the raw records; their rows deleted
from each stack's database and their queue state cleared; all 88 re-run on the
same pinned pre-fix code. Checked first that no already-recorded duplicate had
its original in the re-run set, which would have corrupted the duplicate
ordering that check 3.1 depends on.

**Left in place.** Nothing about the application changed. This is a harness
defect and a note in the method, not a product finding.

---

## 2026-09-05 — Found during the AFTER run, deliberately not fixed mid-run

**Check 2.2 rejects multi-line invoices against per-item purchase orders.** The
ex-tax comparison from P5 works — the evidence quotes net totals, not gross —
but the comparison itself is against the wrong thing. This corpus's purchase
orders are per-item commitments: `approved_value` is one item's quantity times
its unit price. The invoices quoting them carry 3 to 14 lines. Comparing an
invoice total against a single item's approved value produces a rejection every
time, and the dataset expects all 19 of them to pass.

**Why it is not fixed in this run.** Changing the code now would leave the
AFTER run measuring two different applications, which is the one thing the
before-and-after comparison cannot survive. It is reported as measured and
carried into the remaining-issues list.

**The fix, when it is made.** A purchase order that states an approved quantity
and unit price is governing that item, and the quantity and price checks
already added are the right controls for it; the invoice-total comparison
should apply only where the order states a commitment for the whole order. That
is a change to `po_matching.match_invoice_to_po`, testable in isolation, and
its effect is measurable with `replay.py` over the AFTER extractions without
spending another run.

---

## 2026-09-05 — P8: what a purchase order commits to decides what is compared

**Problem.** Check 2.2 rejected 19 legitimate multi-line invoices as exceeding
their purchase order. The dataset expects every one to pass.

**Root cause.** An order stating an approved quantity and unit price commits to
*that item*. The invoices quoting them carry 3 to 14 lines the order says
nothing about, so measuring the whole invoice against one item's value rejects
every legitimate multi-line invoice. The ex-tax fix from P5 was working — the
evidence quotes net totals — but the comparison itself was against the wrong
quantity.

**Fix.** Where the order states line detail, the quantity and price checks are
the controls and they have already run; the invoice total is not compared
against it. Where the order states a value and no line detail, it commits to
the order as a whole and the invoice total is exactly the right comparison —
that is the case the check was written for, and it is unchanged.

**Files.** `po_matching.py`.

**Tests.** `tests/test_po_matching.py::TestWhatThePurchaseOrderCommitsTo` (3):
an order for one item does not cap the invoice; that item is still governed by
quantity and price; an order with no line detail still caps the invoice.

**Measured, not assumed.** Replayed over the 156 extractions of the abandoned
AFTER run: exactly 19 verdicts change, all of them 2.2 from FAIL to PASS, and
nothing else changes except the checks those invoices now go on to reach.

**Result.** 1574 passed. Reverting fails
`test_an_order_for_one_item_does_not_cap_the_invoice`.

**Also fixed.** The replay tool did not pass `document_type` through, so every
replayed non-invoice document passed a check the real run failed. Found by an
implausible replay result — five documents flipping 1.1 from fail to pass when
nothing in the change touched extraction.

**The AFTER run was restarted from scratch**, so all 300 invoices are measured
on one version of the application.

---

## 2026-09-05 — AFTER run complete, and two regressions it found

300 invoices on `2521ff9`, no errors, no credential failures. The numbers are in
`benchmark/comparison/report.md`. Two of the 24 regressed rows have causes worth
recording, because both are defects these changes introduced.

**Check 1.2 rejects an invoice over an OCR misread (4 invoices).** The customer
tax-ID comparison added in P3 is exact. On a scanned invoice the extractor read
`24AAECF9034MIZE` where the document says `24AAECF9034M1ZE` — a `1` read as an
`I`. The comparison then reports a registration "not one of this
organisation's", which is a rejection caused by our own reading rather than by
anything on the invoice. **The fix:** compare only when the extracted
identifier is checksum-valid. A misread identifier fails its own checksum, and
a checksum-valid one that differs really is somebody else's.

**Check 1.3 rejects invoices that state a tax-inclusive total (5 invoices).**
`require_tax_breakup` fails an invoice where no tax amount was extracted. Five
of these print "Amount payable: ₹X, inclusive of all applicable taxes" with a
supplier registration on the page: they are tax invoices, and the dataset
expects 1.3 to pass and 2.1 (not itemized) to be the check that catches them.
**The fix:** 1.3 should fail a document that is not a tax invoice — a bill of
supply, or one with no supplier registration — and leave "no itemised tax" to
2.1, whose job it is.

Both are reported as measured. Neither is fixed in this run.

---

## 2026-09-05 — P9: two regressions the AFTER run found, fixed

**1.2 — an invoice rejected because of our own OCR.** The customer tax-ID
comparison was exact, so when the extractor read `24AAECF9034MIZE` off a scan
where the document says `24AAECF9034M1ZE` — a `1` read as an `I` — the invoice
was rejected as addressed to another company. Four real invoices. The fix is the
checksum: an identifier we misread fails it, and one that passes and still
differs really is somebody else's, so the comparison now runs only on a
well-formed identifier and otherwise says the name alone was checked.

**1.3 — a tax-inclusive total is still a tax invoice.** `require_tax_breakup`
failed any invoice where no tax amount could be extracted. Five invoices print
"Amount payable, inclusive of all applicable taxes" over a supplier
registration: they are tax invoices, and what is wrong with them is that they
are not itemised — which is check 2.1's question, and 2.1 asks it. 1.3 now fails
a document that is not a tax invoice and nothing else.

**Files.** `shared_libraries/acting/general_invoice_agent.py`.

**Tests.** `test_an_unreadable_registration_does_not_reject_the_invoice`,
`test_a_tax_inclusive_total_is_still_a_tax_invoice`; two tests that encoded the
old behaviour were rewritten to the intended rule rather than deleted.

**Measured before re-running.** Replayed over the AFTER run's own extractions:
11 invoices flip 1.3 to pass, 5 flip 1.2 to pass, and 6 now reach check 2.1 and
fail there — the hand-off the 1.3 fix was made for.

**Result.** 1575 passed. Reverting the checksum gate fails
`test_an_unreadable_registration_does_not_reject_the_invoice`.

---

## 2026-09-05 — Final AFTER run, and what the last 15 regressions are

300 invoices on `6db9408`, no errors. Both P9 fixes held: check 1.2's false
positives went 26 → 0 and 1.3's 32 → 2. 226 rows fixed against 15 regressed.

The 15 have three causes, and only one of them is a validation defect:

**Eight are check 2.2 applying a goods rule to services.** Every one is a
service vendor, and every one reads "Invoice prices this at X; purchase order
approved Y". The quantity and unit-price checks added in P5 belong to goods: a
service invoice's rate is governed by its work authorization, which check 4.3
already tests, and the purchase order is a budget envelope rather than a price
list. The vendor master already records which suppliers supply goods —
`wa_matching` reads it for exactly this reason — so the fix is to gate the
quantity and price comparisons the same way. Two further regressions (3.1 and
3.2 on the same invoices) are these eight cascading.

**Two are the extractor inventing the customer.** `MFG-INV-002` and
`TECH-INV-024` are addressed to Lumora Brandcraft LLP; the extractor returned
the *receiving tenant's* own name and registration. Check 1.2 then correctly
passed an invoice that should have failed, because what it was given no longer
said what the document says. No validation change can fix this: a check cannot
catch what extraction has already rewritten. It belongs in the extraction
prompt, and it is the most serious single finding in the run.

**One is the deadline doing its job.** `SVC-INV-025` hit the 180-second Vertex
timeout, so its extraction failed and 1.1 and 1.3 report it. That is the trade
P0 makes — a failed invoice instead of a hung worker — and it cost exactly one
invoice out of 300.

---

## 2026-09-05 — P10: what a purchase order governs depends on what it buys

**Problem.** Eight of the 15 regressions in the final AFTER run were check 2.2
rejecting service invoices — "Invoice prices this at 2,363.00; purchase order
approved 2,815.00" — and every one was expected to pass. Two more (3.1 and 3.2
on the same invoices) were those eight cascading.

**Root cause.** The quantity and unit-price checks added in P5 are goods
controls. A service supplier's rate is set by its work authorization, which
step 4.3 already checks against the authorized rate and the authorized hours;
the purchase order is the budget the work is drawn against, and its unit price
is one line of an estimate rather than an agreed price list. Applying the goods
rule to services rejects an invoice for billing a rate its own authorization
allows — and 4.3 says so on the same invoice, which is the tell.

**Fix.** `match_invoice_to_po` takes the tenant's supplier list, the same way
`match_invoice_to_wa` already does, and skips the quantity and price
comparisons for a supplier the list marks as supplying services. Everything
else about the order is still checked for them: the supplier, the customer, the
currency, the status, the validity window. An unknown supplier keeps the
stricter reading — an invoice from someone not on the list is not the place to
relax a control.

**Files.** `po_matching.py`, `shared_libraries/acting/general_invoice_agent.py`.

**Tests.** `tests/test_po_matching.py::TestWhatTheOrderGoverns` (6), including
one that runs the same invoice against the same order twice and changes only
what the supplier list says the supplier sells.

**Measured before re-running.** Replayed over the AFTER run's own extractions:
21 invoices flip 2.2 from FAIL to PASS, and the checks they now reach for the
first time follow — 19 reach 3.1 and 3.3, 18 reach 3.2, 13 reach each of 4.1,
4.2 and 4.3. Six of those newly-reached checks fail, which is the point: they
are being asked for the first time.

**Result.** 1581 passed. Reverting the goods/service gate fails two named tests.

---

## 2026-09-05 — Every local run had been writing to the production bucket

**What happened.** Cleaning up after the production subset, an independent
listing of the bucket showed 273 objects under `t/bench-A/`, `t/bench-B/` and
`t/bench-C/` — capitalised, which no production run ever used — timestamped
across the baseline and every AFTER run. Local runs had been writing their
document PDFs into the production GCS bucket from the start.

**Root cause.** `configure_environment` *deleted* `GCS_BUCKET` from the
environment before importing the product. `core.config` calls `load_dotenv()`
at import, and dotenv fills in any variable that is **absent** — so the bucket
name came straight back from the repo's `.env`. `DATABASE_URL` was unaffected
because it is *set* rather than deleted, and dotenv does not override a
variable that already has a value. The asymmetry between the two is the whole
bug, and it made every local run report a local blob directory while using the
production bucket.

**What it did and did not affect.** No production database rows: the databases
really were local SQLite. No benchmark result: blobs are content-addressed
storage the pipeline reads back by key, so every measurement stands. What it
did was put 273 synthetic PDFs in a customer's bucket, and make "nothing local
touches production" false when it was said.

**Fix.** The variables are set to empty rather than deleted; an empty value is
already present, so dotenv leaves it alone and every reader treats it as unset.
`tests/test_benchmark_scoring.py::TestALocalRunStaysLocal` pins both halves —
that the variables are emptied, and that `load_dotenv` against the repo's own
`.env` cannot refill them. Reverting to `pop` fails both.

**Also fixed.** `prod_cleanup` could only see prefixes belonging to tenants
still in the database, which is why it reported "0 objects to remove" while 273
sat in the bucket. `--prefix` names an orphaned one explicitly, and still
refuses anything outside `bench-`.

**Verified after.** The bucket holds 13 objects, all under the pilot's own
tenant. The pilot has its 13 documents and 13 invoices. The bench tenants hold
nothing.

---

## 2026-09-05 — Final AFTER run: the whole corpus on the whole fix set

300 invoices at `18843b3` — the first run carrying everything: the India-GST
pack, both P9 regression fixes, the P10 goods/service fix, and a harness that
keeps its documents local. No errors.

The P10 fix, which until now had only been replayed and spot-checked on 45
invoices, lands as the replay predicted: check 2.2 goes 73.1% → 95.7% with its
false positives 23 → 2. Three checks reach 100%: duplicate detection, future
date, and total verification.

**Regressions are 8, from 15, and all eight are one of three things.** Two
invoices (`MFG-INV-040`, `SVC-INV-042`) failed extraction and take 1.1, 1.2 and
1.3 down with them — six of the eight. Their evidence names the cause:
`503 recvmsg:Can't assign requested address` and `LLM call exceeded 180s and
was abandoned`. That is infrastructure and the deadline doing its job, not
validation. The remaining two are the extractor returning the receiving
tenant's own name and registration for an invoice addressed elsewhere
(`TECH-INV-024`, `TECH-INV-087`) — the hallucination first recorded on
2026-09-05, still the most serious finding in this work and still not
something a validation change can reach.

**Two checks are worth reading past their accuracy.** 4.2 (59.3%) and 4.4
(76.0%) carry 20 and 12 false positives; their precision is 13.0% and 7.7%.
Both are now reached by ten times as many invoices as before (5 → 54, 1 → 50),
so they are being asked real questions for the first time, and they are
answering a good share of them wrongly. That is the next thing to fix.

---

## 2026-09-05 — P11: the 4.2 and 4.4 false positives

**Problem.** Check 4.2 read 59.3% with 20 false positives, 4.4 76.0% with 12.
Both had just started being reached by ten times as many invoices, and were
answering a good share of them wrongly.

**Three root causes, each measured before it was fixed.**

*A charge counted twice (11 of 4.2's, 9 of 4.4's).* Freight, packing and travel
are printed outside the line table, and the extractor returns them in both
places — sometimes describing the charge only as "Other Charges", so the two
descriptions do not even match. On `MFG-INV-015` the expected tax overshot by
455.94, which is 2,533.00 of packing at 18% to the cent. The test is now
arithmetic rather than textual, because the arithmetic is what went wrong: a
charge is a duplicate when dropping it brings the line sum back to the printed
subtotal. Where the sums do not say so, both are kept — two things costing the
same are not the same thing.

*The taxable total is not to be trusted.* `invoice_total_ex_tax` is where an
extractor's arithmetic errors land; on the same invoice it came back holding
the packing charge as well. The tax base is now subtotal minus discount, which
the document prints and which cannot drift that way.

*The discount column is a percentage (5 of 4.2's, 1 of 4.4's).* Most layouts
print "10%", and the extractor returns 0.1 — read as ten rupees against a
90,420 line. `line_discount_pct` and `has_discount` are now extracted
separately, a value below one in the amount field is read as a rate, and a
discount that is shown but cannot be quantified lets the line pass. That last
is the tax-ID lesson again: an unreadable column is our problem, not the
supplier's.

*The tolerance is now proportional as well as fixed.* A rupee or two is the
right allowance on a small invoice and an impossible one on a two-million-rupee
tax figure, where legitimate per-line rounding and discount apportionment reach
several hundred.

**Measured on the AFTER run's own extractions, same invoices, same population:**

| | Before | After |
|---|---|---|
| 4.2 | FP 24, precision 11.1%, accuracy 59.0% | **FP 6, precision 25.0%, accuracy 86.9%** |
| 4.4 | FP 15, precision 28.6%, accuracy 75.4% | **FP 1, precision 80.0%, accuracy 95.1%** |

**What it costs, stated rather than buried.** Recall falls: 4.2 from 75% to 50%,
4.4 from 100% to 66.7%. Three real faults are no longer caught.

* `MFG-INV-039` (a wrong line discount) is lost only because this measurement
  replays extractions taken before `line_discount_pct` existed. A fresh run
  should recover it, and `test_a_wrong_line_discount_is_still_caught_when_it_can_be_read`
  pins that it is caught whenever the discount can be read.
* `TECH-INV-031` and `TECH-INV-067` are `TAX_ROUNDING` faults of 4.20 on
  250,751.58 — 0.0017%, where legitimate drift on this corpus reaches 0.075%.
  **The fault is smaller than the noise it would have to be told apart from**,
  so no tolerance separates them. Catching both with a fixed one-rupee band
  costs twelve wrongly rejected invoices for two discrepancies of four rupees,
  which is the wrong trade for a system that pays suppliers. Recorded in
  `test_a_rounding_error_below_our_own_precision_is_not_caught`.

**Result.** 1594 passed.

---

## 2026-09-05 — Final run: the calculation fixes measured on the whole corpus

300 invoices at `d3d07fc`, no errors. The first run where the extractor is
asked for `line_discount_pct` and `has_discount`, so the first that measures
the discount fix rather than replaying around it.

The calculation checks land where the replay said they would, and further:
4.2 goes 59.3% → 94.4% with its false positives 20 → 0, and 4.4 76.0% → 96.1%
with 12 → 1. Six checks now have no false positives at all (1.1, 1.2, 1.3, 2.1,
3.3, 4.1) and five reach 100%.

**Regressions are 3, from 8.** All three are the extractor rewriting the
document, and none can be reached by a validation change: on `MFG-INV-002` and
`TECH-INV-024` it returned the receiving tenant's own name and registration for
an invoice addressed elsewhere, and on `TECH-INV-087` it supplied a supplier
tax ID the document does not carry. Checks 1.2 and 1.3 then passed them
correctly, on evidence that was wrong before it arrived.

**The trade recorded in P11 held.** `MFG-INV-039`'s wrong line discount came
back, as predicted, once the discount could be read: 4.2's recall is 40% here
against 0% at baseline, and its remaining misses are the two `TAX_ROUNDING`
faults of four rupees in a quarter of a million, deliberately given up.

**What is left is extraction, not validation.** Line accuracy fell 1.5 points
and tax accuracy 1.4 while every validation check improved; the three
regressions and 4.4's single false positive are all the extractor. That is the
ceiling on the touchless rate now, and the next work.

---

## 2026-09-06 — The whole corpus on production infrastructure

300 invoices through production Cloud SQL and GCS on the deployed code
(`f49b71d`), no errors, on the `bench-` tenants. The pilot's queue was not
involved: the harness keeps processing local so the documents are handled by
the code under test rather than by the deployed worker.

**Against the frozen baseline it reproduces the local result.** Decision
accuracy 85.0% → 97.7%, touchless 0% → 14.7%, wrongly rejected 45 → 4, and
check 4.3 reaches 100%. Every check lands within a point or two of the local
run.

**Against the local run of the same code, 296 of 300 decisions are identical**
and 291 of 300 rejection reasons. None of the four differences is the
infrastructure:

* `MFG-INV-020` and `MFG-INV-064` are the *same* GSTIN read two ways — `24O`
  against `240`, a letter O against a digit zero — and the two runs disagree in
  opposite directions on the two invoices. The vendor-master comparison is
  doing its job in both; what differs is what it was given.
* `MFG-INV-046` drifted further: a different supplier's tax ID entirely, and a
  taxable total 57,799 out.
* `MFG-INV-042` decided differently with **no extraction drift at all** — the
  one difference this run cannot attribute to the model's reading, and the only
  one worth investigating as a possible order-dependence in duplicate
  detection.

**Extraction differs on 49 of 300 documents** between two runs of identical
code on identical files. That is the finding this exercise keeps returning: the
ceiling on every number here is what the extractor reads, not what the checks
decide.

Cleanup follows; the bench tenants' rows and objects are removed and their
audit events kept.

---

## 2026-09-06 — Cleanup, and why the first two attempts did nothing

The production corpus is removed: 3,575 rows and 273 stored objects across the
three `bench-` tenants, verified afterwards by querying the database and the
bucket directly rather than trusting the script's own report. The pilot is
untouched — 13 documents, 13 invoices, 23 invoice lines, 106 findings.

Two failures on the way, both worth recording.

**Deleting a row at a time is indistinguishable from a hang.** The first run sat
for twenty minutes with three seconds of CPU and no output, and I reported it as
"slow but progressing". It was not: nothing had committed, because ~900 ORM
deletes per tenant over an IAM tunnel take a quarter of an hour and the
transaction commits only at the end. Timing a single delete — 0.9 seconds —
is what settled it. One statement per table replaced them.

**A bulk delete does not cascade.** The batched version then failed on
`invoice_lines_invoice_id_fkey`: deleting a row at a time let SQLAlchemy's
relationships work the order out, and one statement per table does not. Children
are now deleted before parents, and `invoice_lines` and `corrections` are named
explicitly rather than assumed absent — `bench-b` alone held 436 invoice lines
that the earlier version never counted.

The claim I made in between — that it was working and only invisible — was
wrong, and the check that disproved it was the same one that has caught every
other mistake here: read the state, not the report.

---

## 2026-09-06 — The bench tenant rows stay, and the database says why

Asked to remove the three `bench-` tenants as well, the answer turned out to be
no, for a reason worth keeping.

`audit_events` carries a foreign key to `tenants.id`, so a tenant row cannot go
while its events exist. And its events cannot go at all: the table has SELECT
and INSERT policies and no others, under FORCE ROW LEVEL SECURITY, so a DELETE
matches zero rows and reports success. The append-only trail is enforced by
Postgres, not by the convention this script had been observing — `db/rls.py`
says so in its docstring, and the database proved it by quietly deleting
nothing.

Removing the rows would mean dropping that policy on a production database to
tidy up three empty ones. `--remove-tenants` now refuses and explains, which is
more useful than the foreign-key traceback it produced first.

**Final production state:** the pilot has its 13 documents, 13 invoices and 64
audit events; the bench tenants hold no documents, invoices, lines, findings or
runs, and keep 659 audit events between them recording what the benchmark did;
the bucket holds 13 objects, all the pilot's.

A silent delete of zero rows is exactly the failure this codebase keeps
warning about, and it took reading `rls.py` rather than the error to see it.

---

## 2026-09-24 — Extraction: what the model gets wrong, and what the scorer did

Diffing every extraction in the AFTER run against what each document prints
sorted the "extraction errors" into five causes. Two were not extraction at all.

**Fixed in the product:**

* **Fields mixed across invoices.** 46 submissions bundle two or three invoices
  into one PDF, and the extractor took fields from more than one: `MFG-INV-002`
  prints Lumora Brandcraft as the bill-to on page 1 and Ferrovane on page 2's
  second invoice, and came back as Ferrovane. This was the "hallucinated
  customer" of the previous entries. It is also why check 2.1 caught so few
  un-itemized invoices: a first invoice with no line table borrowed the second
  one's lines. The prompt now says to extract the first invoice only and never
  to combine fields across invoices.
* **Look-alike characters in identifiers.** Six clean, text-layer invoices came
  back with Greek capitals indistinguishable from Latin ones — Tau, Rho, Alpha
  for a printed `TPA`. A look-alike is a different character, so the number
  matches nothing and a resubmission passes the duplicate check.
  `extraction_cleanup.py` folds them in identifier fields only; prose is left
  alone, and every repair is recorded on the extraction as `_repairs`.
* **A charge repeated as a line item.** 15 invoices. The reconciliation checks
  already tolerated it; the workbench and the exports did not. Removed once, at
  extraction, using the same arithmetic test the checks use.

**Fixed in the scorer, which had been grading the wrong thing:**

* **The taxable total.** The prompt did not say whether "total excluding tax"
  includes freight; the extractor mostly assumed it did, and the scorer compared
  against the dataset's `taxable_amount`, which does not. 19 of the 28
  clean-document "errors" were exactly taxable amount plus other charges. The
  prompt now defines it (before tax, after discount, including other charges)
  and the scorer compares against that. **Both runs were re-scored from their
  raw records**: the field reads 94.0% at baseline and 87.6% after, where both
  had read about 77%. That movement is the measurement becoming accurate, not
  the extractor improving.

**Not fixed, deliberately:** misreads on image-only scans, such as `O` read as
`0` in a GSTIN. A structural repair would also "repair" the planted faults,
such as `8` changed to `B`, that check 3.2 exists to catch. On a scan with no
text layer there is no way to tell a misread from a forgery, and pretending
otherwise weakens the control.

**Tests:** `tests/test_extraction_cleanup.py` (10). Reverting the fixes fails 5.

## 2026-09-24 — A model that is down decided 300 invoices

**Problem.** The first run at `85776e0` finished in 158 s, not ~1.5 h, exited 0,
and scored 85.0% decision accuracy with 0% touchless: every invoice rejected as
"Document is not a valid Tax Invoice". Billing had been switched off on the GCP
project; Vertex answered every call with a 403. The same project backs
production, where the Cloud SQL instance was suspended too.

**Root cause.** Two, one in each place.

- *Product.* Every Vertex call is wrapped in a document-level fallback --
  pdfplumber for text, a keyword classifier for the document type, an empty
  extraction for the invoice. Each caught `Exception`, so a refusal that had
  nothing to do with the document was handled as an unreadable document, and
  the keyword guess was then validated and rejected. In production that is a
  false rejection reason sent to a real supplier.
- *Harness.* `run.py` checked only that the stack processes exited 0. Nothing
  asked whether the model had answered once.

**Fix.** `llm_guard.guarded_generate` raises `LlmUnavailable` for an account
refusal (billing, 401/403, API not enabled) without retrying, and for a service
error that outlives its retries. A timeout stays `LlmCallTimeout` -- one slow
scan is the document's doing and keeps its fallback. The five fallbacks in the
acting pipeline re-raise `LlmUnavailable`, so `process_invoice` returns ERROR
and the worker retries the document instead of deciding it. `run.py` now makes
one real call through the same guard before starting any stack (exit 2, nothing
run), and exits 1 after scoring if any invoice has no decision.

**Files.** `invoice_processing/llm_guard.py`,
`invoice_processing/shared_libraries/acting/general_invoice_agent.py`,
`benchmark/harness/run.py`.

**Tests.** `tests/test_llm_unavailable.py`: the guard's four outcomes; the real
pipeline on `case_001` with a billing 403 returns ERROR after one call and
writes no decision (it returned REJECT before the fix); a failed preflight
starts no stack. The live preflight against the suspended project fails in ~5 s
with the 403.

**Result.** No benchmark numbers: the run is discarded, and a valid one waits
on billing.

## 2026-10-07 — MFG-INV-042 was extraction drift after all, and 4.2 now absorbs it

**Problem.** The production run rejected the clean invoice `MFG-INV-042` at
4.2 (line sum 30,059 against a subtotal of 27,850) where the local run accepted
it. The handoff recorded it as the one difference with "no extraction drift at
all" and suspected order-dependence in duplicate detection.

**Root cause.** Neither. Check 3.1 passed identically in both runs. The
document prints packing (2,209) as line 2 of the item table *and* as "Other
Charges" below a Sub Total that leaves it out. Both runs extracted both table
lines; only the local run also returned the charge in `other_charges`, which is
what lets `_charges_and_lines` recognise the repeat. The earlier comparison
covered header fields, not `other_charges`, so it reported no drift.

**Fix.** `_charges_and_lines` -- the one definition 4.1, 4.2, 4.4 and the
extraction cleanup share -- now also handles a charge that arrives only as a
line: when the lines equal the printed taxable total and exactly one line is
the amount the printed subtotal leaves out, that line is moved to the charges.
Two printed figures must agree, so a wrong subtotal (whose taxable total was
computed from it) still fails 4.2.

**Files.** `invoice_processing/shared_libraries/acting/general_invoice_agent.py`,
`invoice_processing/extraction_cleanup.py`.

**Tests.** `tests/test_extraction_cleanup.py::TestAChargeReturnedOnlyAsALine`:
the MFG-INV-042 shape is repaired (fails without the fix); a short subtotal is
not explained away; two candidate lines are left alone.

**Result (replay, both stored runs, 600 extractions).** The rule fires on 14
extractions, every one a real charge -- freight, transit insurance, packing,
travel. One verdict changes: `MFG-INV-042` 4.1 and 4.2 False -> True on the
production extractions. No planted line-sum fault stops failing. The rest are
rejected earlier for their planted faults. `SVC-INV-025` loses an accidental
4.2 failure: its planted line-discount fault was never extracted, and it was
failing on the travel line instead. 4.2 is not reached there.

## 2026-10-07 — The touchless gap: what the target measures, and four false alarms

**Problem.** Touchless was 15.3% against a 40% target.

**What the number is.** Touchless is ACCEPT / 300, and the corpus holds 45
clean invoices of 300 -- so 15.0% is its ceiling, and 15.3% is already there
(four of the 46 accepts are wrong accepts). The 40% target cannot be met on
this corpus, by construction. The real gap sits inside the accepts: **42 of 46
were routed to a human** (`accepted but routed to review`). Only 4 posted with
no one touching them.

**Root cause.** Of 38 accepted invoices whose artifacts the run kept, 11 are
scans with no text layer (review is the honest answer there). The rest were
flagged by `confidence.py` checking a GST invoice against an Australian
template, and disagreeing with the checks that had just passed it:

- Dates printed with the month as a word (`13-May-2026`) matched none of the
  numeric renderings, so `invoice_date` and `due_date` were "not on the page".
- A GST invoice prints CGST and SGST and often no tax total, so `tax_amount`
  was never found.
- `line_maths_consistent` ignored line discounts; 4.2 counts them.
- `totals_reconcile` ignored the round-off line; 4.1 counts it.

And one real hole, found by measuring the fix: `lines_sum_to_subtotal` and
`line_maths_consistent` were computed and never used for routing. A wrongly
accepted invoice (`MFG-INV-039`, lines 116,806 over the subtotal) had been sent
to review only by the date false alarm; fixing the dates alone posted it.
*[Corrected below, 2026-10-07: the 116,806 is MFG-INV-039's printed invoice
discount, not an overshoot. The rule stands; this example was wrong.]*

**Fix.** Month-name renderings for dates; a tax total grounded when its printed
components are found and add up to it; line maths uses 4.2's `_line_discount`,
moved into `confidence.line_discount` so there is one definition; totals count
the round-off; any failed arithmetic check caps the invoice at MEDIUM and names
itself in `fields_needing_review`, so it routes to review.

**Files.** `invoice_processing/confidence.py`,
`invoice_processing/shared_libraries/acting/general_invoice_agent.py`.

**Tests.** `tests/test_confidence.py`: `TestWhatGstInvoicesPrint` (five, each
failing before) and `TestArithmeticThatContradictsItselfIsReviewed` (a
self-contradicting invoice with every field grounded routes to review; a
consistent one still posts).

**Result (evidence recomputed over stored extractions and the PDFs).**

| | Before | After |
|---|---|---|
| Clean accepts posted untouched, local run | 4 of 36 | **16 of 36** |
| Clean accepts posted untouched, production run | 4 of 36 | **16 of 36** |
| Wrong accepts posted untouched | 0 | **0** |

Still routed to review on clean invoices: 11 scans, ~4 where the lines include
a charge the confidence check does not net out, a few `vendor_address`
renderings, single fields elsewhere. Touchless as the report defines it does
not move -- it counts decisions, not routing -- so the report needs a
"posted untouched" line for this to show.

## 2026-10-07 — MV-002: an identifier is read as the page prints it

**Problem.** `MFG-INV-064`, a clean invoice, was rejected at 3.2: "Invalid
check character ... Invoice states 240XMPF9713X1Z2". Found by the document
review of the manual verification matrix (MV-002).

**Root cause.** The page's text layer prints `24OXMPF9713X1Z2` -- the letter
O -- which matches the vendor master and passes its checksum. The extractor
returned a zero. `85776e0` left O/0 alone on purpose, because folding one into
the other would also turn a forged tax ID into a valid one; that holds for a
blind fold, but not for a page that prints the answer.

**Fix.** `extraction_cleanup.read_as_printed`: when an identifier is not on the
page and exactly one string on the page differs from it only by O/0 or I/1, the
printed string is taken, with the value's own separators kept. Nothing is
folded, so a forged ID printed on the page is taken as printed and 3.2 still
sees it. Scans (no text layer) and ambiguous pages are left alone. The
extractor passes the document's text layer into `clean`.

**Tests.** `TestAnIdentifierIsReadAsPrinted` in `tests/test_extraction_cleanup.py`:
the MV-002 shape, a printed forgery left alone, a scan left alone, two
candidates left alone, separators kept, and the pipeline passing the text
layer. All six fail without the fix.

**Result (both stored runs, 600 extractions).** Six identifiers change, all
vendor GSTINs (O/0 four times, I/1 twice), and all six now match what the
dataset says is printed; none moves away from it. Two decisions change, both
toward the dataset: `MFG-INV-064` is no longer rejected for its own misread,
and `MFG-INV-020` passes 3.2 on to the 4.2 fault it actually carries. The
other four are rejected earlier for real faults.

## 2026-10-07 — MV-018: a lump sum is not a breakdown

**Problem.** `TECH-INV-025` (manual verification MV-018) was accepted. Its only
line is "Professional services, 998513, 1 lot, 2,81,160.00" -- the dataset's
NOT_ITEMIZED_LUMPSUM fault.

**Root cause.** Check 2.1 passed any invoice with at least one line. A lump sum
is one line. So is a document with no table at all: the extractor writes one
line from the prose ("Charges for the period as per engagement letter") with
no quantity and no price.

**Fix.** `_itemized`: a breakdown needs at least one line with a quantity and a
unit price, and a single line of one lot (or `ls`, `job`, `lump sum`) is not
one. Every planted lump sum in both stored runs is exactly that shape; no
clean invoice is. Lines are counted after `_charges_and_lines`, so a charge
returned as a line does not make a lump sum look itemized.

**Tests.** `tests/test_itemization.py`: a lot of professional services, a
prose line, and no lines fail; one priced line of goods, hours at a rate, and a
lot among other lines pass.

**Result (both stored runs).** The 179 invoices expected to pass 2.1 all still
pass. Planted itemization faults caught: 26 -> 35 of 44 locally, 27 -> 36 of
44 in production. Still missed: the "total only" layout, where the extractor
invents a plausible priced line that no structural test can tell from a real
one.

## 2026-10-07 — MV-027, MV-035: a quotation is not a purchase order

**Problem.** Two invoices (MV-027, MV-035) were rejected for "Purchase order
Q-4179 not found" -- right decision, wrong reason. Neither prints a PO; "Q-4179"
is the printed "Quotation Ref.". The real faults (a GSTIN, a line total) went
unreported.

**Root cause.** The extraction prompt described `po_number` as "purchase order
/ work order reference" and named nothing it is not. Over both stored runs the
extractor filled it from a quotation reference nine times (six local, three
production) and from the work authorization reference once. Both MV rows are
scans, so no text-layer check could have caught it; the fix had to be the
prompt.

**Fix.** `po_number` is the buyer's purchase order number only: a reference
the invoice labels as a PO. A quotation, quote, estimate or proposal is the
supplier's offer; a work order belongs in `work_authorization_reference`; with
no PO printed the field is null. `require_purchase_order` is off in the
benchmark profile, so a null PO skips 2.2 and the invoice reaches its real
fault.

**Tests.** `TestTheExtractionPrompt::test_a_quotation_or_work_order_is_not_a_purchase_order`.

**Result (targeted re-extraction, 27 invoices, about $0.35).** The six
invoices that had a quotation reference as their PO all came back null; 15 of
15 invoices with a printed PO still read it, scans included; 5 of 5 with a
work authorization and no PO still have none. Extraction is not deterministic,
so this is a sample, not a measurement -- the next full run is.

## 2026-10-07 — The PO match says what it compared

**Problem.** The document review of the manual verification matrix found 2.2
evidence that was false on three invoices it accepted correctly: "Matched
purchase order ZC-PO-260018 on quantity and unit price" where the order is
120 h at 4,557 and the invoice 288 h at 4,659 (MV-001), and the same sentence
on MV-003 and on MV-020, which has no line items at all.

**Root cause.** `match_invoice_to_po` skips the quantity and price comparison
for a listed service supplier, on purpose -- the work authorization prices a
service, step 4.3 -- and has nothing to compare when the invoice states no
quantity or price. Both paths ended at the same message, written for goods
that had been compared.

**Fix.** The match records which of quantity and unit price it compared, and
says so: "on quantity and unit price" when it compared them; "as an open order
for this supplier ... neither quantity nor price was compared" for a service,
naming step 4.3; "by reference only" when the invoice gives nothing to compare.
No outcome changes.

**Tests.** `tests/test_po_matching.py::TestTheMatchSaysWhatItCompared` (the
service and no-lines cases fail without the fix; the goods case is the
control).

## 2026-10-07 — Measured: the run at `9f8c923`, and today's code replayed over it

**The run.** 300 invoices, local, against GCP project `rasayan` (the old
project's billing account is closed; see the 2026-09-24 outage entry). 87
minutes, preflight passed, every invoice decided. It measures `9f8c923`: the
bundled-invoice prompt fix and the outage guard.

**Replay can now score.** `replay` applies today's extraction cleanup and
confidence routing (from the document's own text layer) before the four
validation phases, and `--out NAME` writes the replayed records as a run the
scorer reads. Checked for faithfulness against this run before trusting it:
291 of 300 decisions and reasons came out exactly as recorded, and the other
nine are the 2.1 itemization fix, each moving to the dataset's expected
reason.

| | `after` (`d3d07fc`) | run at `9f8c923` | today (`bca701a`) replayed on it |
|---|---|---|---|
| Decision accuracy | 97.7% | 98.0% | **98.3%** |
| Wrong accepts | 4 | 3 | **2** |
| Wrong rejects | 3 | 3 | 3 |
| Rejection reason exact | 214 | 218 | **227** |
| Rejection reason wrong | 7 | 10 | **6** |
| Accepted but routed to review | 42 | 41 | **25** |
| Posted untouched (of 45 clean) | 4 (8.9%) | 4 (8.9%) | **19 (42.2%)** |
| Posted untouched wrongly | 0 | 0 | **0** |

Not in the replayed column: `1a1f705` (a quotation is not a PO) is a prompt
change and only a full run can measure it.

## 2026-10-07 — The last text-layer false alarms, and a correction

**Problem.** With today's code replayed over the run at `9f8c923`, 25 accepted
clean invoices still went to review: 11 scans and 11 text-layer invoices (the
rest of the 25 are wrong accepts and duplicates).

**Root cause.** Four more places where `confidence.py` disagreed with the page
or with the checks:

- `Apr 01, 2026` -- month name, then a zero-padded day -- was not a rendering.
- The vendor address was "not on the page" because a logo block sits on the
  same text row: the text layer reads `Ludhiana 668868, BF` / `Punjab`.
- `lines_sum_to_subtotal` compared the lines with the *taxable total*, which
  includes charges and is net of invoice discount. 4.2 compares with the
  printed subtotal and nets charges out.
- The taxable total is not printed on GST layouts that show Sub Total and
  Other Charges separately.

**Fix.** The zero-padded rendering; an address grounded segment by segment, by
text rather than `locate` (which reads "Amritsar 668868" as the number 668868 --
caught by the test); `charges_and_lines` moved into `confidence.py` and shared
with 4.1/4.2/4.4 and cleanup, so the line-sum check uses the printed subtotal
with charges netted, exactly as 4.2 does; a taxable total grounded when the
subtotal, every charge and any discount are printed and add up to it.

**Correction to the 2026-10-07 touchless entry.** It said MFG-INV-039 had
"lines 116,806 over the subtotal". 116,806 is its printed invoice discount:
the old check compared the lines (2,336,120.05) with the taxable total after
discount (2,219,314.05). Against the printed subtotal they agree exactly. The
rule that a failed arithmetic check routes to review stands; that example did
not support it. With the comparison fixed, MFG-INV-039 posts untouched -- and
it is the invoice whose planted fault is not printed (manual verification
MV-036), so the scorer's one "posted untouched wrongly" is the dataset's error,
not a missed fault.

**Tests.** `TestWhatStillSentCleanInvoicesToReview` in
`tests/test_confidence.py` (all five fail without the fix).

**Result (today's code replayed over the run at `9f8c923`).**

| | before this change | after |
|---|---|---|
| Accepted but routed to review | 25 | **12** |
| Posted untouched (of 45 clean) | 19 (42.2%) | **31 (68.9%)** |
| Posted untouched wrongly | 0 | 1 -- MFG-INV-039, the dataset error |
| Decision accuracy | 98.3% | 98.3% |

Every clean invoice with a text layer now posts untouched; the 11 left in
review are all scans, and only OCR grounding moves them.

## 2026-10-07 — Grounding requires a value to stand on its own

**Problem.** Prototyping OCR grounding for scans (tesseract, local only, not
in the product) asked whether grounding ever confirms a value the extractor
got wrong. It did -- but through the matcher, not OCR: a 0.00 placeholder total
was "found" inside 10.00, and the truncated GSTIN `24YVBFK3734K` inside the
printed `24YVBFK3734K1ZQ`.

**Root cause.** `confidence.locate` was a substring search, on the page as
written and again with every separator removed. A value matched anywhere,
including inside a longer number or identifier.

**Fix.** One pattern per rendering: the value's letters and digits with up to
three non-alphanumeric characters between them (so `51 824 753 556`,
`13,41,792.00` and `Pvt. Ltd` still match), bounded so no letter or digit
touches either end and no digit run continues it through a `.` or `,`.

**Tests.** `TestAValueMustStandOnItsOwn` in `tests/test_confidence.py` (the two
boundary cases fail without the fix; the separator case is the control).

**Result.** Over 11,567 extracted fields in three stored runs, every change
was grounded -> not grounded, and on text-layer documents every one was a
false confirmation: 0.0 placeholder totals on non-documents and a truncated
bank code. Replayed over the run at `9f8c923`, routing is unchanged -- 31 of
45 clean invoices posted untouched. The OCR side (not in the product) loses
groundings where tesseract runs tokens together or reads 0 as O; that is
for the OCR work, which will need O/0 and I/1 tolerance on identifiers.

## 2026-10-07 — Scans are grounded by OCR

**Problem.** Every clean scan went to review: 11 of the 45 clean invoices, the
whole of what was left after the text-layer fixes. With no text layer there was
nothing to ground a field against.

**Fix.** `confidence.ocr_page_text`: when a PDF has no readable text layer, its
pages are rendered at 300 dpi with pypdfium2 (already installed with
pdfplumber) and read by tesseract through `subprocess` -- no new Python
dependency. Grounding then runs against that text. Two tolerances, each where
something else catches a misuse:

- the vendor GSTIN may differ by O/0 or I/1 (tesseract reads the trailing O as
  a zero) -- its checksum and the vendor master catch a swapped character on
  their own (3.2);
- an IFSC may have O in its fifth position, which is zero by definition, so
  that tolerance cannot hide a changed bank code.

Invoice and PO numbers get none: duplicate detection does not fold O into 0,
and a prototype that tolerated them confirmed 24 wrong identifiers across three
runs. OCR text is never used to repair a value, only to ground it. No
tesseract, or a failure, leaves the page empty and the invoice in review --
today's behaviour. The Dockerfile installs `tesseract-ocr`.

**Tests.** `TestAScanIsReadByOcr` in `tests/test_confidence.py`: OCR text is
used and reported (`read_by: "ocr"`); the GSTIN tolerance applies and the
invoice-number one does not; the IFSC rule accepts only the fifth position; no
tesseract means no OCR; and a real tesseract read of `case_005`, skipped where
tesseract is absent.

**Result (replayed, ~4 minutes for 85 scans, median 1.8 s per document).**

| | run at `9f8c923` | run at `ecb9bbd` |
|---|---|---|
| Posted untouched (of 45 clean), before | 31 | 18 at the run's own code |
| **Posted untouched, today** | **39 (86.7%)** | **39 (86.7%)** |
| Posted untouched wrongly | 1 -- MFG-INV-039, the dataset error | 1 -- the same |
| Decision accuracy | 98.3% | 97.7% |

The 0.6-point accuracy gap between the two runs is extraction variance, each
case checked: two scanned GSTINs misread worse, a scan classified unreadable,
and `currency: "Rs."` on TECH-INV-037, which the PO check rejected as a
currency mismatch -- a real gap (currency is not normalised), noted for
its own fix.

**The prompt fix `1a1f705`, measured by the run at `ecb9bbd`:** `po_number`
extraction 220 of 220, up from 98.7%.

## 2026-10-07 — A currency is compared as an ISO code

**Problem.** In the run at `ecb9bbd`, four invoices were rejected for
"Purchase order currency mismatch": TECH-INV-037, which is clean, and three
whose real faults the mismatch hid.

**Root cause.** The extractor transcribes what is printed, and Indian invoices
print "Rs." or "₹": 74 extractions across the stored runs carry `Rs.`, `Rs` or
`₹`. The PO match compares the upper-cased string with the PO's `INR`. Tax
rates by currency, exports, the Xero connector (`CurrencyCode`) and dashboard
totals read the same field.

**Fix.** `extraction_cleanup.normalize_currency`, applied in `clean` before
anything reads or stores the extraction: printed rupee forms become `INR`,
`A$`/`US$`/`€`/`£` their codes, a valid code is upper-cased, and a bare `$`
(AUD or USD) or anything unrecognised is left for a reviewer. The repair is
recorded on the extraction.

**Tests.** `TestACurrencyAsPrinted` in `tests/test_extraction_cleanup.py`.

**Result (today's code replayed over the run at `ecb9bbd`).** Exactly the four
invoices change: TECH-INV-037 is accepted and posts untouched; TECH-INV-071 is
rejected for exceeding the PO quantity, SVC-INV-067 for its work
authorization and SVC-INV-084 as a duplicate -- each its planted fault. Decision
accuracy 97.7% -> 98.0%, wrong rejects 4 -> 3, exact reasons 222 -> 224, posted
untouched 39 -> 40 of 45.

## 2026-10-08 — Step 3.4: bank details against the vendor master

**Problem.** The extractor has read the payee's account, bank code and IBAN
since Phase 1 and the grounder checks they appear on the page, but nothing
asked whether they are the *vendor's*. A changed account number is how a
payment is redirected.

**Root cause.** No comparison of the extracted bank fields against any record
existed, and the vendor master carried no bank columns to compare with.

**Fix.** Step 3.4 (`bank_check`) compares the invoice with the vendor's
verified details and raises up to five signals -- `bank_mismatch`,
`bank_unverified`, `payee_name_differs`, `recent_bank_change`,
`sender_domain_mismatch` -- each of which holds the invoice for REVIEW and none
of which rejects. The benchmark's `vendor_records` now carries the dataset's
account, IFSC and billing domain (verified 2025-01-01, so the recent-change
signal measures invoices, not the seeding). Benchmark documents are uploads, so
`sender_domain_mismatch` cannot fire here.

**Tests.** `tests/test_benchmark_dataset.py`; `tests/test_bank_check_planted.py`
plants a changed account, then a changed IFSC, in the master against a real
stored extraction (skips if the gitignored run is absent).

**Result (today's code replayed over the run at `ecb9bbd`, `replay-bank-check`).**

Inside the pipeline 3.4 only runs for an invoice that reached phase 3: 76
passed, 1 held (TECH-INV-052), 223 never reached it, having been rejected at an
earlier step. The dataset planted no bank faults, so the measure that matters is
the same check run standalone over all 300 stored extractions:

| 3.4 over all 300 extractions | count |
|---|---|
| passed | 241 |
| held | 6 (2 `bank_mismatch`, 4 `payee_name_differs`) |
| not compared (no extraction, or vendor not in the master) | 53 |

Counting account and code alone gives 245 / 2 / 53; four more are held because
the payee name printed on the invoice differs from the vendor name
(`payee_name_differs`), which an account-and-code count does not see.

- MFG-INV-011 (faulty): IFSC printed `DTBKO117671`, master `DTBK0117671`.
- MFG-INV-003 (faulty): account printed with a stray leading digit, `1 974810581534`.
  Both invoices are faulty for other planted faults; the bank values here are
  OCR near-misses, not planted bank faults. They are held on purpose; no O/0
  folding was added.
- SVC-INV-045, SVC-INV-028 (faulty) and TECH-INV-070 (faulty): payee misread
  by one letter ("Metia", "PIR", "Lt").
- **TECH-INV-052 (clean): payee `Kestrel Cyber Assurance Pvt Lt` against vendor
  `Kestrel Cyber Assurance Pvt Ltd`.** A measured cost: a clean invoice held for a
  one-character truncation. It is also rejected at 3.2 (GSTIN misread), so no
  decision changes today.

Safety and cost, before (`replay-currency-on-ecb9bbd`) -> after:
posted untouched 40 of 45 clean -> 40 of 45; posted untouched wrongly 1
(MFG-INV-039, the dataset error) -> 1; decision accuracy 98.0% -> 98.0%; zero
flips. Step 3.4 changes no benchmark decision because every held invoice was
already rejected at an earlier step; its value is on faults the dataset did not
plant, which the planted tests cover.

**Declined.** O/0 folding on bank codes or accounts. A prototype that tolerated
O/0 on identifiers confirmed 24 wrong values across three runs, and a changed
bank code is exactly what this check exists to catch; OCR noise belongs in
review.

---

## 2026-10-08 — Approval workflows: no benchmark decision moves

**Problem.** Approval routing sits between a decision and its sign-off and
shares code with the pipeline, so it had to be shown not to move any benchmark
number.

**Root cause.** None; this is a guard, not a fix. Routing is not in the replay
path (the replay re-runs the pipeline over stored extractions and never creates
approval steps), so the replay can only prove the shared code did not change.

**Fix.** None to the pipeline. Approval chains are built after the pipeline
decides, only for invoices a person must touch (REVIEW, or a total at or above an
amount tier). An AUTO invoice below every tier gets no steps.

**Result (today's code replayed over the run at `ecb9bbd`, `replay-approvals`).**
Against `replay-currency-on-ecb9bbd`: posted untouched 40 of 45 clean -> 40 of
45; posted untouched wrongly 1 -> 1; decision accuracy 98.0% -> 98.0%; zero
flips.

**What the feature would change, reported and not tuned.** The benchmark tenant
has no tiers, so nothing changes there. Had it set a 5-lakh tier (total >=
500,000), 9 of the 18 invoices the run accepted and routed AUTO would now need a
person: MFG-INV-049, MFG-INV-056, SVC-INV-009, TECH-INV-043, TECH-INV-018,
TECH-INV-048, TECH-INV-001, TECH-INV-016, TECH-INV-054. That is the intended
cost of a tier, not a regression, and the threshold is the tenant's to choose.

---

## 2026-10-08 — GL coding: no benchmark decision moves

**Problem.** Non-PO GL coding adds a stage after extraction and a column to the
export, so it had to be shown not to move any benchmark number.

**Root cause.** None; this is a guard, not a fix. GL coding is informational:
it never feeds a validation check or a decision, and it reaches approval
routing only through `gl` approval rules. The benchmark tenant has no GL rules
of either kind, so every line stays uncoded and no chain changes.

**Result (today's code replayed over the run at `ecb9bbd`, `replay-gl-coding`).**
Against `replay-approvals`: posted untouched 40 of 45 clean -> 40 of 45; posted
untouched wrongly 1 -> 1; decision accuracy 98.0% -> 98.0%; the scored report is
identical line for line.

**Regression status.** All 9 gates green (2037 tests, 21/21, 27/27, eval gate,
lint, tsc, 22 e2e).

---

## 2026-10-09 — Three-way match: no benchmark decision moves; planted set 6/6

**Problem.** Step 2.3 (invoice lines against PO lines and goods received) needed
two measurements: that it never moves a benchmark decision, and that it catches
receipt faults the benchmark never planted.

**Root cause.** None for the guard. The benchmark tenant has no purchase-order
book, so the check has nothing to match against and stays out of the replay.

**Result, guard (today's code replayed over the run at `ecb9bbd`,
`replay-three-way`).** Against `replay-approvals`: posted untouched 40 of 45
clean -> 40 of 45; posted untouched wrongly 1 -> 1; decision accuracy 98.0% ->
98.0%; `results.csv` byte-identical.

**Result, planted set (`tests/fixtures/three_way/planted.json`).** The 19
Company B PO invoices the run accepted, with PO lines rebuilt from ground truth
and every line fully received. Six faults planted (short received x2, not
received, price over PO, over ordered, unpaired line): caught 6/6, and no
planted record raised a signal beyond its fault. Three split-delivery controls
(invoice quantity halved, receipt matches) add no signal. Of the 10 unplanted
records, 8 pass clean.

**Pairing fix found by the first measurement.** The first run paired only 5 of
104 lines by description (1 of 10 clean records passed). Company B invoices
repeat one item on several PO lines at different prices, the descriptions
matched exactly, and identical candidates tied, so the margin rule left every
such line unpaired. Description pairing now breaks a tie between PO lines with
identical wording by the closest unit price within tolerance, then the lowest
PO line number; different wording within 0.2 of the best still stays unpaired.
Now 104 of 104 invoice lines pair on the unplanted records.

**Remaining gaps, reported and not tuned.** MFG-INV-033: two lines were
extracted without a price or quantity, so it holds as `unreadable_line`.
MFG-INV-035: the extraction has no line items; a match over zero invoice lines
used to pass and now holds as `unreadable_line` ("Invoice has no line items to
match.").

**Files.** `tests/fixtures/three_way/planted.json`,
`tests/test_three_way_planted.py`.

**Regression status.** Full suite 2167 passed, 1 skipped; ruff check and format
clean. Description pairing is counted as lines paired by description: 104 of
104 on the unplanted records (MFG-INV-035 has no lines to count).

---

## 2026-10-09 — Scanned GRN/PO extraction: first measurement

**Problem.** Scanned purchase orders and goods receipt notes are now read by
`supporting_docs.extract` for a clerk to confirm, and nothing had measured how
often that reading is right, or whether a wrong value reaches the clerk
flagged.

**Root cause.** None; this is the first measurement, not a fix. No gate.

**What was done.** `gen_render.render_grn` renders a goods receipt note (GRN
No., date, PO No., supplier; #, Item, HSN/SAC, Ordered, Received, Unit;
"Received by") from `po_master.csv` rows, scanned with the existing
`degrade()`. `FONTDIR` now honours `DEJAVU_FONTDIR` so the generator runs off
Linux. A scratch script (parameters recorded in the fixture's `source`) built
20 GRNs — 7 clean, 7 degraded, 6 adversarial (RECEIVED stamp carrying a
different date, watermark, handwritten note), the adversarial ones multi-line,
about 40% of lines received short, two receiving PO lines 1 and 3 only — plus
the dataset's 6 PURCHASE_ORDER documents. One real Gemini 2.5 Pro run per PDF
(26 calls, no retries needed) is committed as
`tests/fixtures/supporting_docs/measured.json`; the PDFs are not.

**Result, scan set (`tests/test_supporting_docs_measured.py`, pinned).**

| measure | correct |
|---|---|
| GRN header (grn_number, po_number, received_on) | 56 / 60 |
| PO header (po_number, vendor_name, total) | 17 / 18 |
| line quantities (received on GRNs, ordered on POs) | 34 / 34 |
| GRN lines paired to the right PO line | 27 / 28 |
| wrong values flagged for review (or left blank) | 3 / 5 |

Every wrong value, with its reason:

- GRN-03, GRN-09 (adversarial) `received_on`: the model took the RECEIVED
  stamp's date (2026-09-02, 2026-09-04) over the printed GRN date. The stamp
  date is on the page, so OCR grounds it and it reaches the clerk **unflagged**.
- GRN-12, GRN-15 (adversarial) `received_on`: the same stamp misread; OCR did
  not find the stamp date, so both were flagged.
- SVC-INV-089 (PO, text layer) `total`: the page prints only the line amount,
  no total row; the model left the total blank as the prompt asks, and truth
  is `po_master.approved_value`. Layout, not a misread.

Pairing miss: GRN-06 receives PO lines 1 and 3 and prints them as GRN #1 and
#2; `pair_grn_lines` passes the GRN's own number as the PO line number, so #2
pairs to PO line 2 (a different item) before description matching runs. A
partial receipt that renumbers its lines pairs wrongly; reported, not tuned.

**Result, guard (today's code replayed over the run at `ecb9bbd`,
`replay-scanned-docs`).** Against `replay-approvals`: `results.csv`
byte-identical. The 6 PURCHASE_ORDER records stay REJECT in the replay: the
replay calls the phase agents from recorded classifications and never reaches
`process_invoice`, where the SUPPORTING branch lives, so it cannot show the
REJECT -> SUPPORTING change. The harness was not changed.

**Files.** `benchmark/dataset/generator/gen_render.py`,
`tests/fixtures/supporting_docs/measured.json`,
`tests/test_supporting_docs_measured.py`.

**Regression status.** Full suite 2286 passed, 1 skipped; ruff check and
format clean.
