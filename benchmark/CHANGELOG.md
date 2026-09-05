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
