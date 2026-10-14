# Backup and restore

Targets (SECURITY.md): database RPO minutes (point-in-time recovery),
documents RPO up to 24 hours (daily volume backups), RTO 4 hours, a drill
every 6 months and after any production restore.

## What is backed up

Production runs on Railway (deployment/README.md).

- **Postgres point-in-time recovery** (the database): enable it during
  setup (Railway → `Postgres` → Backups → Point-in-time recovery). It
  archives the write-ahead log to a Railway bucket (storage is billed) and
  restores to any moment in its window into a new Postgres service.
- **Postgres volume backups**: a daily schedule as a second line, plus a
  manual backup before any risky change (a migration, a purge, a bulk
  import). Check: Railway → `Postgres` → Volume → Backups.
- **`ledgerly` `/data` volume** (uploaded documents and the local job
  queue): the same daily schedule. Documents are written once and never
  modified, so a backup up to a day old loses at most that day's uploads.

Retention at the time of writing (check when configuring): daily backups
6 days, weekly 27, monthly 89; point-in-time recovery about four weeks.

## Restoring the database

1. **Before anything else, take a manual backup** of the current volume, so
   the restore itself can be undone.
2. Point in time: Railway → `Postgres` → Backups → restore to the moment
   before the incident, into a new Postgres service, then point
   `DATABASE_URL` and `MIGRATE_DATABASE_URL` at it. Or a volume backup:
   `Postgres` → Volume → Backups → Restore (Railway redeploys the service
   onto the restored volume).
3. Run `db/bootstrap.sql` §2 as `postgres` (the command is in
   deployment/README.md). A restore carries the tables; re-running §2 is
   what guarantees the app role's grants and the audit REVOKE.
4. Check it, as the owner role:

       python -m invoice_processing.db.restore_check \
           --restored "<owner url>"

   Exit 0 and `PASS`, or stop and read each `FAIL` line. Note the
   "newest audit event" (the effective restore point) and every
   "not active" tenant.
5. **Re-purge.** A backup taken before a purge brings that tenant back as
   `suspended`, because its record lives in the same database. For every
   entry in the offboarding register whose `purged_at` is after the
   restore point, run `purge-tenant --tenant <slug> --operator <you>
   --confirm <slug> --skip-grace --reason "re-purge after restore"`
   (deployment/OFFBOARDING.md). A tenant listed as `purged` that still
   shows rows in the counts is purged again the same way.
6. Redeploy `ledgerly` so it reconnects.

## Restoring both

The database and the documents are backed up separately. Restore the
database first, run `restore_check`, and use its "newest audit event" as
the point to reconcile against; then restore the `/data` backup closest to
it. Documents newer than the database have no rows (re-upload them);
queued jobs for documents the database already processed are skipped by
the worker's claims.

## Restoring documents

Railway → `ledgerly` → Volume → Backups → Restore, after a manual backup
of the current volume. A purge's deleted documents come back with an older
backup too: re-run the purge for those tenants (step 5) after restoring.

## Local drill

Proves the procedure and the check without production. `pg_dump`,
`pg_restore` and `psql` run inside the Postgres containers (the dev
machine has none). From the repo root, in zsh:

    docker run -d --rm --name drill-src -e POSTGRES_PASSWORD=postgres \
        -e POSTGRES_DB=ledgerly -p 55433:5432 postgres:16
    docker run -d --rm --name drill-dst -e POSTGRES_PASSWORD=postgres \
        -e POSTGRES_DB=ledgerly -p 55434:5432 postgres:16
    sleep 5  # both servers accept connections
    V=(-v ON_ERROR_STOP=1 -v owner=ledgerly_owner -v app=ledgerly_app \
       -v owner_pw=ownerpw -v app_pw=apppw)
    export CLOUD_SQL_INSTANCE="" GCS_BUCKET="" PUBSUB_TOPIC=""
    SRC=localhost:55433/ledgerly DST=localhost:55434/ledgerly

    # Source, built the way production is: §1, migrate as owner, §2.
    sed -n '1,/^-- §2/p' invoice_processing/db/bootstrap.sql |
        docker exec -i drill-src psql -q -U postgres -d ledgerly "${V[@]}" -f -
    MIGRATE_DATABASE_URL=postgresql+pg8000://ledgerly_owner:ownerpw@$SRC \
        ./.venv/bin/python -m invoice_processing.db.migrate
    sed -n '/^-- §2/,$p' invoice_processing/db/bootstrap.sql |
        docker exec -i drill-src psql -q -U postgres -d ledgerly "${V[@]}" -f -

    # Seed it through the app role.
    export DATABASE_URL=postgresql+pg8000://ledgerly_app:apppw@$SRC
    P=(./.venv/bin/python -m invoice_processing.admin.provision)
    $P create-tenant --slug drill-a --name "Drill A"
    $P create-tenant --slug drill-b --name "Drill B"
    $P add-user --tenant drill-a --email ap@drill-a.test --role clerk
    $P add-user --tenant drill-b --email ap@drill-b.test --role admin
    $P suspend-tenant --tenant drill-b --operator <you> --reason drill
    unset DATABASE_URL

    # Dump, restore into a fresh server with only the roles, then §2.
    docker exec drill-src pg_dump -U postgres -Fc ledgerly -f /tmp/l.dump
    docker cp drill-src:/tmp/l.dump /tmp/l.dump
    docker cp /tmp/l.dump drill-dst:/tmp/l.dump
    sed -n '1,/^-- §2/p' invoice_processing/db/bootstrap.sql |
        docker exec -i drill-dst psql -q -U postgres -d ledgerly "${V[@]}" -f -
    docker exec drill-dst pg_restore -U postgres -d ledgerly /tmp/l.dump
    sed -n '/^-- §2/,$p' invoice_processing/db/bootstrap.sql |
        docker exec -i drill-dst psql -q -U postgres -d ledgerly "${V[@]}" -f -

    ./.venv/bin/python -m invoice_processing.db.restore_check \
        --restored postgresql+pg8000://ledgerly_owner:ownerpw@$DST \
        --source postgresql+pg8000://ledgerly_owner:ownerpw@$SRC
    docker stop drill-src drill-dst; rm /tmp/l.dump

Expected: exit 0, `PASS`, `not active: drill-b: suspended`, and row
counts for both tenants (tenants' users, drill-b's suspension event).

## Drill log

| Date | Where | Restore point | Result | By |
|---|---|---|---|---|
| 2026-10-14 | Local drill (Postgres 16 containers, `pg_dump -Fc` / `pg_restore`), head `d1a2b3c4e5f6` | 2026-10-13 18:38 UTC (newest audit event) | PASS: schema head, forced RLS and policies on all 26 tables, app role NOBYPASSRLS without UPDATE/DELETE/TRUNCATE on `audit_events`, row counts equal to the source for both tenants, suspended tenant listed | Claude Code, for vineetyad |
