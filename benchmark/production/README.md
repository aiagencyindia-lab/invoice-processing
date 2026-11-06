# The same 300 invoices, on production infrastructure

`benchmark/after/` is the canonical AFTER run: local SQLite, local blob
directories, three parallel stacks, reproducible by anyone with the repo and a
Vertex project. This directory holds the same 300 documents run through
**production Cloud SQL and the production GCS bucket** on the deployed code
(`f49b71d`), to answer a question the local run cannot: does any of this behave
differently when the stores are real?

It does not.

| | Local (`after`) | Production (`prod300`) |
|---|---|---|
| Decision accuracy | 97.7% | 97.7% |
| Touchless rate | 15.3% | 14.7% |
| Wrongly rejected | 3 | 4 |
| Errors | 0 | 0 |
| Median seconds per invoice | 51 | ~190 |

**296 of 300 decisions and 291 of 300 rejection reasons are identical**, and
none of the four differences is the infrastructure:

* `MFG-INV-020` and `MFG-INV-064` are the same GSTIN read two ways — `24O`
  against `240`, a letter against a digit — with the two runs disagreeing in
  opposite directions on two invoices from the same supplier.
* `MFG-INV-046` drifted further: a different supplier's tax ID entirely, and a
  taxable total 57,799 out.
* `MFG-INV-042` differs with **no extraction drift at all**. It is the only one
  of the four that cannot be attributed to the model's reading, and the only
  one worth investigating.

Extraction differs on **49 of 300 documents** between two runs of identical code
over identical files. That, not the storage layer and not the validation logic,
is the ceiling on every number in this benchmark.

## How it was run, and what it left behind

Three tenants named `bench-a`, `bench-b`, `bench-c` so that anyone reading the
production database can see at a glance which rows are synthetic. Processing
stayed in local workers — `--production` drops `PUBSUB_TOPIC` — so the deployed
worker never saw these documents and the pilot's queue kept draining normally.

```bash
railway run --service api -- ./.venv/bin/python -u -m benchmark.harness.stack_main \
  --company A --out benchmark/runs/prod300 \
  --profile benchmark/harness/profiles/after.json --production --fresh
```

Afterwards `benchmark/harness/prod_cleanup.py --apply` removed the documents,
invoices, findings, pipeline runs and stored objects. Audit events are kept:
they record that these documents were ingested and processed, which stays true
after the documents are gone.

The run above was on GCP (Cloud SQL and a GCS bucket). The steps below are
how a production run is done on Railway, where production now lives.

## Railway

Production is one container running the API and the worker
(`invoice-processing`, project `ledgerly-new`), Postgres, and a volume at
`/data`. The database secrets are sealed, so `railway run` does not receive
them: the harness runs **inside the service, over SSH**, as the deployed image
and with its own credentials. Nothing may merge to `main` while it runs: a
merge redeploys and kills the processes (a run resumes without `--fresh`, but
its tenants must then stay until it has).

`--production` uses the service's database and keeps everything else to the
run: `STATE_DIR` is pointed at `<out>/stacks/<C>/state`, so the queue, stored
documents and master data are the run's own and never the deployed worker's
`/data`. The harness's worker heartbeats are rows in the shared database, so
while it runs the health check sees a live worker even if the deployed one
died; check the deployed worker's own row (its pid) rather than the newest.

```bash
SSH="ssh -F <ssh_config from railway ssh> ledgerly-api"
# The PDFs are not in git or the image (33 MB).
tar czf - -C <dataset dir> documents | $SSH 'tar xzf - -C /app/benchmark/dataset'
# One process per company, detached so a dropped connection does not end it.
$SSH 'cd /app && mkdir -p /tmp/bench && for C in A B C; do
  GOOGLE_APPLICATION_CREDENTIALS=/tmp/gcp-credentials.json nohup setsid \
  ./.venv/bin/python -u -m benchmark.harness.stack_main --company $C \
    --out /tmp/bench/runs/prod --profile benchmark/harness/profiles/after.json \
    --production --fresh > /tmp/bench/prod-$C.log 2>&1 < /dev/null & done'
# Pull it back and score it locally.
$SSH 'tar czf - -C /tmp/bench/runs prod' | tar xzf - -C benchmark/runs/
python -m benchmark.harness.score benchmark/runs/prod --profile benchmark/harness/profiles/after.json
```

Afterwards the bench tenants are removed completely, with the product's own
purge and then `prod_cleanup --remove-tenants` (see its docstring), and
`/tmp/bench` and the copied PDFs are deleted. Production is left holding only
its real tenants.

### Result, 2026-11-05 (`e4c22e3`, Gemini 3.x)

The first run on Railway, and the first on the models production now uses:
`gemini-3.5-flash` and `gemini-3.1-pro-preview` at `LOCATION=global`, project
`ledgerly-513708`. Report: [`railway-2026-11-05/report.md`](railway-2026-11-05/report.md).
Compared with the same code and models run locally the day before
(`g3-2026-11-04`) and with the local Gemini 2.5 run (`after-2026-10-27`):

| | Railway, 3.x | Local, 3.x | Local, 2.5 |
|---|---|---|---|
| Decision accuracy | 98.3% | 98.3% | 98.3% |
| Correct / wrong accepts, wrong rejects | 43 / 3, 2 | 43 / 3, 2 | 43 / 3, 2 |
| Routed to a clerk (purchase orders) | 6, all correct | 6, all correct | 6, all correct |
| Accepted but routed to review | 6 | 5 | 5 |
| Rejection reason exact / family / wrong | 219 / 21 / 6 | 219 / 21 / 6 | 219 / 22 / 5 |
| Extraction: header / totals / lines / tax | 99.3 / 99.1 / 91.7 / 99.8% | 99.2 / 99.6 / 91.5 / 100% | 97.8 / 98.9 / 91.5 / 99.8% |
| Documents not extracted | 1 | 2 | 0 |
| Median / p95 processing | 57.7 s / 133.5 s | 67.9 s / 138.1 s | 48.3 s / 102.8 s |
| Errors or no decision | 0 | 0 | 0 |

**Four decisions differ from the local 3.x run, and none is the infrastructure.**
`TECH-INV-052` (clean) was accepted on Railway and wrongly rejected locally
("Vendor tax ID invalid"); `MFG-INV-045` (clean) went the other way, wrongly
rejected on Railway ("Missing Work Authorization Form"). `TECH-INV-006` and
`MFG-INV-089` were correctly rejected by both, for different reasons. The
totals are identical.

The ~$1.05 the report shows prices every call as `gemini-2.5-flash` and,
like every cost figure before 2026-11-06, leaves out Gemini 3's thinking
tokens, which Vertex bills at the output rate. A first repricing from the
audit logs ($0.02 an invoice) inherited that omission and was wrong. The
November Vertex bill showed output at ~92% of spend.

Measured on the 14-invoice smoke subset on 2026-11-06, thinking included
(`thinking-*-2026-11-06` runs): **$0.0825 (₹7.9) an invoice at the models'
default thinking**, which is what this run used. With the defaults since then
(Flash `low`, Pro `medium`) it is **$0.055 (₹5.3)**, with the same decisions
and extraction; see `benchmark/CHANGELOG.md`. Both figures leave out an
unrecorded PDF-to-text call: counted per call, the new defaults cost $0.078,
and $0.069 (₹6.6) once digital PDFs are read locally (same changelog).

Cleanup left production with its one real tenant (`aixai`): each bench tenant's
purge deleted ~2,000 rows, audit events included, and 91 stored documents;
`/data` was unchanged and the deployed worker kept running throughout.
