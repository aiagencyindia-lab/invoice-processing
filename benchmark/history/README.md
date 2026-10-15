# Benchmark v2: tenant history

`generate.py` writes a synthetic company's AP history as the files a
customer would export (`vendor.csv`, `po.csv`, `grn.csv`, `ap_invoice.csv`,
`payment.csv`) plus `manifest.json`: the links the graph is expected to
make and, from Wave 1 on, the injected faults with their expected ₹ impact.
It is deterministic for a seed and uses nothing but the standard library.

`eval.py` imports those files through the real history importer, builds the
Evidence Graph, and measures the AC-19 coverage against the manifest:

| Gate | Floor |
|---|---|
| `invoice_vendor_coverage` (invoices linked to a vendor) | 0.95 |
| `po_backed_coverage` (PO-backed invoices linked to a PO with a receipt) | 0.80 |

`contract_clause_coverage` is recorded as `null` with
`not_measurable_until: wave1`, because clause extraction arrives with the
Leakage engine.

```bash
./.venv/bin/python -m benchmark.history.eval --seed 7
```

Writes `benchmark/runs/history/<timestamp>.json` (git-ignored) and exits 1
on a failed gate. `--data <dir>` scores an existing generated directory;
`--db <url>` runs against Postgres instead of a temporary SQLite file.
