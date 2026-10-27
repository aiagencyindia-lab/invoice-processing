"""Remove what a production benchmark run left behind.

On Railway everything runs inside the service, over SSH (the database
secrets are sealed, so `railway run` no longer receives them):

    # 1. Empty each bench tenant with the product's own purge (owner role):
    #    every row, audit events included, and its stored documents.
    STATE_DIR=<the run's stacks/<C>/state> \
      python -m invoice_processing.admin.provision suspend-tenant \
        --tenant bench-a --operator <you> --reason benchmark
    STATE_DIR=... python -m invoice_processing.admin.provision purge-tenant \
        --tenant bench-a --operator <you> --confirm bench-a \
        --skip-grace --reason benchmark
    # 2. Then drop the purged rows themselves (dry run without --apply).
    python -m benchmark.harness.prod_cleanup --remove-tenants --apply

The audit trail used to be undeletable by anything, which is why the tenant
rows were kept; the offboarding purge (2026-10-13, `audit_events_purge`) now
removes a tenant's trail as the owner role, so a purged bench tenant is an
empty tombstone and `--remove-tenants` deletes it. It refuses a bench tenant
that is not purged, because deleting its row would strand (or be refused by)
the rows still pointing at it. A sweep of the deployed worker that read the
tenant list just before the suspension can still write a row after the purge;
the DELETE then fails on its foreign key, and running `purge-tenant` again
(it accepts a purged tenant) clears it.

Without `--remove-tenants` this is the older, partial cleanup: one DELETE
per table for the v1 tables (documents, invoices and their children) and the
stored objects in GCS_BUCKET when one is set. It leaves the v2 trust tables
and audit events; use the purge above for a production run.

Two things it never does:

* **It does not touch a tenant whose id does not start with `bench-`.** The
  prefix is checked before anything is read or deleted, because a cleanup
  script that can reach the pilot's data is a worse risk than the rows it
  removes.
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
        help="drop the bench tenant rows, once each has been purged",
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
    prefix = args.tenant_prefix
    if not prefix.startswith(PREFIX):
        raise SystemExit(
            f"Refusing to clean up outside the {PREFIX!r} namespace: {prefix!r}."
        )
    if not (os.getenv("CLOUD_SQL_INSTANCE") or os.getenv("DATABASE_URL")):
        raise SystemExit(
            "No database configured. Run this inside the service "
            "(Railway: over SSH into invoice-processing)."
        )
    if args.remove_tenants:
        return remove_tenants(prefix, apply=args.apply)

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


def remove_tenants(prefix: str, *, apply: bool) -> int:
    """Delete the `prefix` tenant rows, every one of which must be purged."""
    from sqlalchemy import delete, select

    from invoice_processing import offboarding
    from invoice_processing.db.models import Tenant
    from invoice_processing.db.session import tenant_session

    try:
        # Before any engine exists, so every connection is the owner's.
        offboarding.require_owner_role()
    except offboarding.OffboardingError as exc:
        raise SystemExit(str(exc)) from None
    with tenant_session("") as session:
        found = {
            str(t.id): t.status
            for t in session.scalars(select(Tenant)).all()
            if str(t.id).startswith(prefix)
        }
    if not found:
        print(f"No tenants starting with {prefix!r}. Nothing to do.")
        return 0
    waiting = sorted(
        t for t, status in found.items() if status != offboarding.PURGED
    )
    if waiting:
        raise SystemExit(
            f"Not purged yet: {', '.join(waiting)}. Suspend and purge each with "
            "`python -m invoice_processing.admin.provision` first; nothing was "
            "removed."
        )
    for tenant_id in sorted(found):
        print(f"{tenant_id}: purged", flush=True)
        if apply:
            with tenant_session(tenant_id) as session:
                session.execute(delete(Tenant).where(Tenant.id == tenant_id))
            print(f"  removed {tenant_id}", flush=True)
    if not apply:
        print(
            f"\nDry run: {len(found)} tenant rows would be removed. Re-run with --apply."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
