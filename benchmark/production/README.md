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
