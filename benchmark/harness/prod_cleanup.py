"""Remove what a production benchmark run left behind.

    railway run --service api -- ./.venv/bin/python -m benchmark.harness.prod_cleanup
    railway run --service api -- ./.venv/bin/python -m benchmark.harness.prod_cleanup --apply

Deletes the documents, invoices and stored objects belonging to the `bench-`
tenants, and nothing else. Three things it deliberately does not do:

* **It does not touch a tenant whose id does not start with `bench-`.** The
  prefix is checked before anything is read or deleted, because a cleanup
  script that can reach the pilot's data is a worse risk than the rows it
  removes.
* **It cannot delete audit events, and neither can anything else.** The trail
  is append-only in the database, not by convention: `audit_events` has SELECT
  and INSERT policies and no others, under FORCE ROW LEVEL SECURITY, so a
  DELETE affects zero rows and reports success. That is why the tenant rows
  themselves stay -- `audit_events` holds a foreign key to `tenants.id`, so
  removing a tenant would mean dropping that policy on a production database
  to tidy up an empty row. See `--remove-tenants`, which explains this and
  refuses.
* **It does not delete a prefix it was not told about.** Objects are namespaced
  `t/<tenant>/`, and the tenants it cleans are the `bench-` rows in the
  database. A local run that wrote to the bucket under a tenant id no longer in
  the database leaves objects this cannot see, which is exactly what happened
  once -- so `--prefix` exists to name such a prefix explicitly, and it still
  refuses anything outside the `bench-` namespace.

Defaults to a dry run. `--apply` is required to delete anything.
"""

from __future__ import annotations

import argparse
import os
import sys

PREFIX = "bench-"


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--apply",
        action="store_true",
        help="actually delete; without it nothing is written",
    )
    p.add_argument(
        "--prefix",
        action="append",
        default=[],
        help=(
            "also remove objects under t/<prefix>/, for a tenant that is no "
            "longer in the database; repeatable"
        ),
    )
    p.add_argument(
        "--remove-tenants",
        action="store_true",
        help="refuses, and explains why the tenant rows have to stay",
    )
    p.add_argument(
        "--rows-only",
        action="store_true",
        help="delete database rows and leave stored objects alone",
    )
    p.add_argument(
        "--tenant-prefix",
        default=PREFIX,
        help=f"only tenants whose id starts with this (must itself start {PREFIX!r})",
    )
    args = p.parse_args(argv)
    if args.remove_tenants:
        raise SystemExit(
            "A tenant row cannot be removed while its audit events exist, and "
            "its audit events cannot be removed at all: `audit_events` carries "
            "SELECT and INSERT policies and no others, under FORCE ROW LEVEL "
            "SECURITY, so a DELETE quietly affects nothing. Removing the row "
            "would mean dropping that policy on the production database, which "
            "is a real control traded away for three empty rows that cost "
            "nothing and make the next run a --seed-only away."
        )
    prefix = args.tenant_prefix
    if not prefix.startswith(PREFIX):
        raise SystemExit(
            f"Refusing to clean up outside the {PREFIX!r} namespace: {prefix!r}."
        )
    if not (os.getenv("CLOUD_SQL_INSTANCE") or os.getenv("DATABASE_URL")):
        raise SystemExit(
            "No database configured. Run this under `railway run --service api`."
        )

    from sqlalchemy import delete, func, select

    from invoice_processing.db.models import (
        Correction,
        Document,
        Finding,
        Invoice,
        InvoiceLine,
        PipelineRun,
        Tenant,
    )
    from invoice_processing.db.session import tenant_session

    with tenant_session("") as session:
        tenant_ids = [
            str(t.id)
            for t in session.scalars(select(Tenant)).all()
            if str(t.id).startswith(prefix)
        ]
    if not tenant_ids:
        print(f"No tenants starting with {prefix!r}. Nothing to do.")
        return 0

    bucket_name = os.getenv("GCS_BUCKET", "")
    total = 0
    # One statement per table, not one per row. Deleting ~900 rows a row at a
    # time over an IAM tunnel takes a quarter of an hour per tenant and commits
    # nothing until it finishes, so it is indistinguishable from a hang.
    # Children before parents. Deleting a row at a time let SQLAlchemy's
    # relationships work this order out; one statement per table does not, and
    # `invoice_lines` holds a foreign key into `invoices` that refuses.
    models = (
        ("invoice_lines", InvoiceLine),
        ("findings", Finding),
        ("corrections", Correction),
        ("pipeline_runs", PipelineRun),
        ("invoices", Invoice),
        ("documents", Document),
    )
    for tenant_id in tenant_ids:
        with tenant_session(tenant_id) as session:
            counts = {
                name: session.scalar(
                    select(func.count())
                    .select_from(model)
                    .where(model.tenant_id == tenant_id)
                )
                for name, model in models
            }
            keys = [
                d
                for (d,) in session.execute(
                    select(Document.storage_key).where(
                        Document.tenant_id == tenant_id
                    )
                )
            ]
            print(f"{tenant_id}: {counts}", flush=True)
            total += sum(counts.values())

            if args.apply:
                for name, model in models:
                    session.execute(
                        delete(model).where(model.tenant_id == tenant_id)
                    )
                    print(f"  deleted {counts[name]} {name}", flush=True)

        if args.apply and bucket_name and not args.rows_only:
            from google.cloud import storage

            client = storage.Client()
            bucket = client.bucket(bucket_name)
            print(f"  removing {len(keys)} stored objects", flush=True)
            for n, key in enumerate(keys, 1):
                bucket.blob(f"t/{tenant_id}/{key}").delete()
                if n % 25 == 0 or n == len(keys):
                    print(f"    {n}/{len(keys)}", flush=True)

    for extra in args.prefix:
        if not extra.startswith(PREFIX):
            raise SystemExit(
                f"Refusing to touch objects outside {PREFIX!r}: {extra!r}."
            )
        if not bucket_name:
            break
        from google.cloud import storage

        client = storage.Client()
        bucket = client.bucket(bucket_name)
        keys = [b.name for b in client.list_blobs(bucket, prefix=f"t/{extra}/")]
        print(f"t/{extra}/: {len(keys)} stored objects", flush=True)
        total += len(keys)
        if args.apply:
            for n, key in enumerate(keys, 1):
                bucket.blob(key).delete()
                if n % 20 == 0 or n == len(keys):
                    print(f"    {n}/{len(keys)}", flush=True)

    if not args.apply:
        print(
            f"\nDry run: {total} rows and objects would be removed. Re-run with --apply."
        )
    else:
        print(f"\nDeleted {total} rows. Audit events are kept, by design.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
