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
4. **Purge as the owner role.** `MIGRATE_DATABASE_URL` (or
   `MIGRATE_CLOUD_SQL_USER`) must be set, as for the release step; the
   command refuses the app role.

       python -m invoice_processing.admin.provision purge-tenant \
           --tenant <slug> --operator <you> --confirm <slug>

   It deletes the documents, then every row in one transaction, and checks
   nothing is left before marking the tenant purged. On any error the
   tenant stays suspended; fix the cause and run it again.
5. **File the record.** The command's last line is one JSON object. Add it
   to the offboarding register (the private document outside the
   database). After a restore from a backup older than the purge, that
   register is the only list of tenants to purge again
   (deployment/BACKUP_RESTORE.md).
6. **Delete the Identity Platform users** of that tenant in the Google
   Cloud console (Identity Platform → Users, filter by the tenant's email
   domain). Their tokens are already refused; this removes the accounts.
