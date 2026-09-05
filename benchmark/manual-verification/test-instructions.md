# Checking the result by hand

Everything in `benchmark/comparison/` was produced by a script. This is how you
verify it yourself, with the same application, through the browser — no
terminal work beyond starting the stack.

Budget about 90 minutes for the full 45-invoice sample, or 20 minutes for the
five rows marked **core** in `test-matrix.csv`.

## 1. Start a stack

One command per company. The stack is a real API, a real worker and the real
frontend, pointed at a scratch database seeded with that company's settings,
purchase orders, vendors and work authorizations — the same configuration the
AFTER run used.

```bash
cd /Users/vineet/Downloads/ADK/invoice-processing
./.venv/bin/python -m benchmark.harness.stack_main --company A \
  --out benchmark/runs/manual --profile benchmark/harness/profiles/after.json --seed-only
```

That prints the stack root, e.g. `benchmark/runs/manual/stacks/A`. Use it below
as `$ROOT`.

```bash
ROOT=benchmark/runs/manual/stacks/A

# API
DATABASE_URL="sqlite:///$PWD/$ROOT/app.db" INVOICE_DATA_DIR="$PWD/$ROOT/data" \
  INVOICE_REFERENCE_DATE=2026-09-03 AUTH_DISABLED=1 ENV=local AUTH_DEV_TENANT=bench-a \
  CLOUD_SQL_INSTANCE= GCS_BUCKET= PUBSUB_TOPIC= \
  ./.venv/bin/python -u -m uvicorn invoice_processing.api.service:app --port 8000
```

```bash
# worker, in a second terminal, from the same directory
ROOT=benchmark/runs/manual/stacks/A
DATABASE_URL="sqlite:///$PWD/$ROOT/app.db" INVOICE_DATA_DIR="$PWD/$ROOT/data" \
  INVOICE_REFERENCE_DATE=2026-09-03 CLOUD_SQL_INSTANCE= GCS_BUCKET= PUBSUB_TOPIC= \
  ./.venv/bin/python -u -m invoice_processing.worker
```

```bash
# frontend, in a third terminal
cd frontend && API_ORIGIN=http://127.0.0.1:8000 npm run dev
```

Open http://localhost:3000. The header shows a dev-mode chip: authentication is
disabled and every request runs as an administrator of `bench-A`.

**`INVOICE_REFERENCE_DATE=2026-09-03` matters.** The dataset's future-dated
invoices are future-dated relative to that day. Without it, check 3.3 measures
today's date and five invoices that should be rejected are accepted.

For company B or C, repeat with `--company B` / `--company C` and the matching
`AUTH_DEV_TENANT=bench-b` / `bench-c`.

## 2. Run one test

Take a row from `test-matrix.csv`.

1. **Upload.** Documents → Upload, and choose the file named in *File to
   upload*, e.g. `benchmark/dataset/documents/company_A/TECH-INV-047.pdf`.
   (The dataset's own filename for it is in *Shown to the reviewer as*; the
   application never sees that name, so either is fine.)
2. **Watch it move.** Documents shows `Queued → Processing → Done`. About a
   minute; image-only scans take longer.
3. **Open it.** Invoices → the new row. Read three things:
   - the **decision** — accepted or rejected;
   - the **reason**, if rejected;
   - the **findings** list, which names every check that ran and what it saw.
4. **Compare.** *Expected behavior* and *Expected rejection* are what the
   dataset says should happen. *Before result* and *After result* are what the
   two benchmark runs recorded.
5. **Record.** Write `agree` or `disagree` in **Manual result**, and anything
   you noticed in **Notes**. That column is empty on purpose — it is the one
   column no script wrote.

## 3. What counts as agreement

- The **decision** matches *Expected behavior*. This is the one that matters.
- For a rejection, the **reason** matches *Expected rejection*. A rejection for
  a different real fault on the same invoice is a partial agreement — note it
  rather than calling it a failure; the benchmark counts these separately as
  "another real fault".
- Check 4.1 never rejects. On its rows, look for a discrepancy *noted* in the
  findings, with the invoice still accepted or rejected on other grounds.

## 4. Duplicates need their original first

The three duplicate rows (`3.1`) only mean something after the invoice they
duplicate has been processed. `test-matrix.csv` names it in *Notes*. Upload the
original, wait for Done, then upload the duplicate.

Two mechanisms can catch it, and both are correct:

- an identical file is refused at upload, before any processing — the document
  never appears as a new invoice;
- a re-rendered copy is processed and rejected at check 3.1 with
  "Invoice already processed".

## 5. If you disagree with the benchmark

Say so in Notes and stop there — do not adjust anything. A disagreement between
a person and this benchmark is the most useful output either can produce, and
it is worth more unedited. `benchmark/baseline/current-validation-map.md`
explains what each check actually reads, which is usually enough to tell a bad
expectation from a bad implementation.
