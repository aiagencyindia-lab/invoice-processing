# Feature check, 2026-10-27

Is every feature working, on `main` at `2eb12c2`? What was checked, how, and
what it found. Local only; production has not been checked this way.

## How

1. **Invoice pipeline, real model.** The 300-invoice benchmark on the fixed
   harness (`benchmark/runs/after-2026-10-27`, see `benchmark/CHANGELOG.md`).
2. **Trust engines, synthetic.** `python -m benchmark.history.eval --seed 7`:
   all 56 gates pass.
3. **Trust engines, real invoices.** Every engine is `off` by default, so the
   benchmark only built the graph. A copy of each benchmark stack was run
   with all six engines in `shadow`, one `evaluate` per document (91 per
   company, under 10 s each, no model calls), and the findings were matched
   to the dataset's faults.
4. **Every page, real data.** The app (API, worker, dev frontend) against the
   shadowed company-A copy; each of the 26 pages opened and read, one
   finding opened end to end.
5. **Automated.** ~4,450 tests and 51 browser end-to-end tests pass on every
   PR (model answers faked).

## Results

| Area | How checked | Result |
|---|---|---|
| Extraction, the 14 checks, accept/reject, reasons | 1 | 98.3%, same five known misses as pre-v2 |
| Exact duplicates at ingest | 1 | 24 caught |
| Purchase orders sent to a person | 1 | 6 of 6 |
| Graph sync (v2) | 1, 3 | built on every document; no failures |
| Leakage: near-duplicates | 3 | 24 flagged, all on a real duplicate pair (15 on the copy, 9 on its original because the run came after ingest), 0 false |
| Reality: price jump, volume spike | 2, 3 | gates pass; on this dataset spikes are noise (synthetic quantities vary 5–15×, e.g. "100 against the usual 6"), 60 volume flags, 27 on clean invoices |
| Provenance | 2, 3 | gates pass; 42 flags, mostly non-invoices and multi-document PDFs, 7 on clean invoices; the dataset has no file tampering to find |
| GST / ITC | 2, 3 | gates pass; with no GST registry connected, 228 of 273 invoices are `GST-UNVERIFIED` |
| Payment trust | 2, 3 | gates pass; with no bank master, `PAY-UNVERIFIED` on 267 of 273 invoices |
| Agent control | 2, 5 | gates pass; no agents in the dataset |
| Trust engines page | 4 | shows "would have held" (87 payment, 78 GST of 91) and keeps every engine at "stay" — the warning before enforcing works |
| Finding detail: why, evidence, impact, next action, rating | 4 | correct on a near-duplicate |
| Dashboard, KPIs, invoices, approvals, documents, POs, approved sheet, vendors, activity, rules, settings, agents, integrations, trust audit, trust posture, admin, approval setup, GL rules | 4, 5 | every page loads with real data, no errors |

## Found

All three fixed 2026-10-27: #134, #135, #136.

1. **Two touchless rates.** The dashboard says 54% (47 of 87); the KPI page
   says 17% (15 of 87). `/api/stats` counts every invoice routed `AUTO`,
   including the ones the rules rejected; the KPI page counts only accepted
   ones (`case_projection.is_touchless`). Same label, two numbers, on the
   figure a pilot is judged on. The KPI definition matches the benchmark.
2. **"Holds open" counts holds that were never placed.** The findings page
   shows 165 holds open while trust posture shows 0: `findings_summary`
   counts open findings of hold types in any mode, so shadow findings read as
   holds.
3. **One vendor, several rows.** A misread or faulted GSTIN makes a separate
   vendor identity (company A: Kestrel ×3, Cloudhaven, Nimbus, Quantar,
   Redwood ×2), and the findings, posture and audit filters list the same
   name more than once with nothing to tell them apart.

Not bugs, but worth knowing before a pilot moves an engine past shadow:
payment and GST hold nearly everything until a bank master and a GST
registry are connected, and reality's spike rule needs real history.

## Not checked

- Production, any of the above.
- Clicking through every action by hand (approve, reject, export, imports,
  API keys, webhooks, agents). These have end-to-end tests with faked model
  answers, not a real-model walkthrough.
- Email ingest, SSO, Xero.
