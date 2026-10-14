# Offboarding a tenant

Policy: SECURITY.md, "Data lifecycle". Every command below runs from the
repo (or a release container) with the production environment.

1. **Confirm the request** in writing from an admin of the tenant, and that
   they have exported what they need (Activity → Export for the audit
   trail; the sheet and invoice exports).
2. **Suspend.** Sign-in, the API, email intake, the worker and escalations
   refuse the tenant from the next request.

       python -m invoice_processing.admin.provision suspend-tenant \
           --tenant <slug> --operator <you> --reason "<ticket / reason>"

   A mistake inside 30 days is undone with `reactivate-tenant --tenant
   <slug> --operator <you>`. Jobs dead-lettered meanwhile ("tenant
   suspended") can then be replayed.
3. **Wait 30 days**, or skip the wait only with the customer's written
   request for immediate deletion (`--skip-grace --reason "<the request>"`).
4. **Stop the worker** for the purge, without stopping the API: on
   Railway the two share the `ledgerly` service, so set
   `WORKER_CMD=sleep infinity` on it and redeploy (the API keeps serving;
   remove the variable and redeploy afterwards). A job already running for
   the tenant would write its rows after the purge commits; the purge
   refuses while any of the tenant's jobs is in flight, and stopping the
   worker closes the moment before a job claims.
   **Purge as the owner role.** `MIGRATE_DATABASE_URL` must be set, as for
   the release step; the command refuses the app role.

       python -m invoice_processing.admin.provision purge-tenant \
           --tenant <slug> --operator <you> --confirm <slug>

   It deletes the documents, then every row in one transaction, and checks
   nothing is left before marking the tenant purged. On any error the
   tenant stays suspended; fix the cause and run it again (a re-run still
   counts the documents the first run deleted). A purged tenant can be
   purged again the same way, for anything a restore or a late write left
   under it; the record keeps the first purge and adds the re-run under
   `repurges`. Start the worker again afterwards.

   Jobs dead-lettered for the tenant ("tenant suspended" / "tenant
   purged") hold ids only. Once it is purged, delete them from the
   dead-letter queue (`queue/dead/` locally, the DLQ subscription on
   Pub/Sub); nothing else removes them.
5. **File the record.** The command's last line is one JSON object. Add it
   to the offboarding register (the private document outside the
   database). After a restore from a backup older than the purge, that
   register is the only list of tenants to purge again
   (deployment/BACKUP_RESTORE.md).
6. **Delete the Identity Platform users** of that tenant in the Google
   Cloud console (Identity Platform → Users, filter by the tenant's email
   domain). Their tokens are already refused; this removes the accounts.
   Add the date and the number of accounts deleted to the register entry:
   it is the evidence that this step happened.
