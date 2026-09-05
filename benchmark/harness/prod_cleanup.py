"""Remove what a production benchmark run left behind.

    railway run --service api -- ./.venv/bin/python -m benchmark.harness.prod_cleanup
    railway run --service api -- ./.venv/bin/python -m benchmark.harness.prod_cleanup --apply

Deletes the documents, invoices and stored objects belonging to the `bench-`
tenants, and nothing else. Three things it deliberately does not do:

* **It does not touch a tenant whose id does not start with `bench-`.** The
  prefix is checked before anything is read or deleted, because a cleanup
  script that can reach the pilot's data is a worse risk than the rows it
  removes.
* **It does not delete audit events.** The trail is append-only by
  construction: it records that these documents were ingested and processed,
  and that stays true after the documents are gone. Removing it would make the
  audit trail lie about its own history, which is the one thing it exists not
  to do.
* **It does not add a delete to the blob port.** Nothing in the product needs
  one, and a port grows a destructive method when a caller needs it rather
  than when a script would find it convenient. Objects are namespaced
  `t/<tenant>/`, so this removes them by prefix through the storage client.

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
        "--tenant-prefix",
        default=PREFIX,
        help=f"only tenants whose id starts with this (must itself start {PREFIX!r})",
    )
    args = p.parse_args(argv)
    prefix = args.tenant_prefix
    if not prefix.startswith(PREFIX):
        raise SystemExit(
            f"Refusing to clean up outside the {PREFIX!r} namespace: {prefix!r}."
        )
    if not (os.getenv("CLOUD_SQL_INSTANCE") or os.getenv("DATABASE_URL")):
        raise SystemExit(
            "No database configured. Run this under `railway run --service api`."
        )

    from sqlalchemy import select

    from invoice_processing.db.models import (
        Document,
        Finding,
        Invoice,
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
    for tenant_id in tenant_ids:
        with tenant_session(tenant_id) as session:
            documents = session.scalars(
                select(Document).where(Document.tenant_id == tenant_id)
            ).all()
            invoices = session.scalars(
                select(Invoice).where(Invoice.tenant_id == tenant_id)
            ).all()
            findings = session.scalars(
                select(Finding).where(Finding.tenant_id == tenant_id)
            ).all()
            runs = session.scalars(
                select(PipelineRun).where(PipelineRun.tenant_id == tenant_id)
            ).all()
            print(
                f"{tenant_id}: {len(documents)} documents, {len(invoices)} invoices, "
                f"{len(findings)} findings, {len(runs)} pipeline runs"
            )
            total += len(documents) + len(invoices) + len(findings) + len(runs)

            if args.apply:
                for row in (*findings, *runs, *invoices, *documents):
                    session.delete(row)

        if args.apply and bucket_name:
            from google.cloud import storage

            client = storage.Client()
            bucket = client.bucket(bucket_name)
            removed = 0
            for blob in client.list_blobs(bucket, prefix=f"t/{tenant_id}/"):
                blob.delete()
                removed += 1
            print(f"  {removed} stored objects removed from {bucket_name}")

    if not args.apply:
        print(f"\nDry run: {total} rows would be deleted. Re-run with --apply.")
    else:
        print(f"\nDeleted {total} rows. Audit events are kept, by design.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
