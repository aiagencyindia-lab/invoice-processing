# SSO: domain-bound Google sign-in — design

Phase 3, sub-project 7. Brainstormed with the user 2026-10-12; every section
below was approved in turn.

## Goal

A design partner's staff sign in to Ledgerly with their company Google
Workspace account. Nobody pastes an account id, nobody keeps a Ledgerly
password, and an admin can promote or remove a colleague and have it take
effect on their next request.

## Where we start

Access is decided only by two custom claims on the Identity Platform token,
`tenant_id` and `role`; the users table is never read when a request is
authenticated. A user row has no link to its Identity Platform account.
`is_active` exists but is never checked. A first sign-in has no path into an
existing tenant except an admin pasting the person's uid, or self-signup,
which creates a new tenant.

## Decisions (user rulings)

1. **Purpose: design partners' staff sign in with their company identity.**
   No passwords in Ledgerly for them. Google Workspace now; Microsoft 365
   next.
2. **Provider model: bind a domain to a provider.** The operator configures
   the Google and Microsoft providers once in Identity Platform. A tenant
   admin registers their email domain and which provider it uses. No
   per-customer OIDC client registration.
3. **First sign-in from a bound domain auto-provisions** a user row with the
   domain's default role (viewer unless the admin chose clerk). An admin
   promotes them later.
4. **Authority at request time: tenant from the signed token; role and
   active status from the users row** when a database is configured. Claims
   keep carrying the role as the fallback for deployments without one. A
   deactivation or role change takes effect on the person's next request.
5. **Google now, Microsoft-ready.** The data model and the admin form accept
   `microsoft`, but there is no Microsoft sign-in button, and a token from
   the Microsoft provider on an unclaimed account is refused until the
   follow-up (which needs the operator's Entra app registration and a check
   of the Entra tenant id, because Microsoft tokens arrive with the email
   unverified).
6. **Approach: domain binding plus just-in-time provisioning in the
   backend.** No Identity Platform multi-tenancy, no per-tenant providers.

## 1. Data model

- New tenant table `sso_domains` (in `TENANT_TABLES`, under RLS, plus a read-only lookup policy: the pre-tenant domain lookup runs in a session whose tenant setting is the sentinel `sso-lookup`, which no real tenant can equal, and that policy permits SELECT on this one table only):
  `id`, `tenant_id`, `domain` (lower-cased; unique across all tenants, so
  two organisations cannot both claim `acme.com`), `provider` ∈
  {`google`, `microsoft`}, `default_role` ∈ {`viewer`, `clerk`},
  `idp_tenant_id` (nullable; the Entra tenant id for Microsoft, unused in
  this slice), `created_by`, `created_at`.
- `users` gains `uid` (String(128), nullable, unique within the tenant): the
  Identity Platform account the row is linked to. And `last_login_at`
  (timestamp with time zone, nullable). `is_active` starts being enforced.
- Public mailbox domains are refused by name when binding: `gmail.com`,
  `googlemail.com`, `outlook.com`, `hotmail.com`, `live.com`, `yahoo.com`,
  `icloud.com`, `proton.me`, `protonmail.com`.
- No DNS proof of ownership in this slice. A bound domain admits only
  Google-verified accounts of exactly that domain, and Google verifies only
  addresses it hosts, so a wrongly bound domain admits no one. Binding is
  admin-only and takes effect at once.
- Migration revises `b8c4d1e2f3a5`; `RLS_TABLES = ["sso_domains"]`; the RLS
  parametrize and the forced-policy list in `tests/test_gcs_and_migrations.py`
  are extended.

## 2. Request path (`api/auth.py`)

`current_principal` keeps its shape. After the token is verified, one
function `resolve_principal(claims, directory)` decides, in this order:

1. **Token has `tenant_id`.** The tenant is that claim, as today. The users
   row for (tenant, email) is read.
   - Row exists and `is_active` is false → 403 "This account has been
     deactivated. Ask an admin of <tenant name> to reactivate it."
   - Row exists → its `role` replaces the claim's role; `last_login_at` is
     touched at most once per 15 minutes (ponytail: avoids a write per
     request; sharpen if anyone needs minute-level last-seen).
   - No row → the claim's role stands (the admin CLI and dry-run paths keep
     working).
2. **Token has no `tenant_id`.** The email must be verified and the token's
   `firebase.sign_in_provider` must be `google.com`.
   - Microsoft provider → 403 "Microsoft sign-in is not enabled yet."
   - Other providers or unverified email → 403 as today.
   - The email's domain is looked up in `sso_domains`. No match → the
     existing 403 "not a member" (the signup page still offers to create an
     organisation).
   - Match → if a users row for (tenant, email) already exists (admin
     pre-created, or the claims have not refreshed yet), link its `uid` when
     missing and proceed as it; otherwise create the row (role =
     `default_role`, `uid` = token `sub`, `display_name` from the token's
     `name`), write claims `{tenant_id, role}` to that uid through the
     existing claims writer, emit `user_provisioned_by_sso`
     `{email, role, domain}`, and proceed as that principal. A claims-write
     failure is logged and the request proceeds on the row; the next request
     repairs the claims.
3. **No database configured** → step 2 is skipped and claims are the only
   authority, as today. The dev bypass applies only the active check: a deactivated dev identity is refused, but the role named in the header stands (the e2e identities depend on it).

The database reads sit behind a small `UserDirectory` port
(`PgUserDirectory`; `None` without a database), so `tests/test_auth.py` can
fake it without a database.

## 3. Admin API

All `require_role("admin")`, tenant from the principal, each with a
cross-tenant probe and a `PROBED` entry:

- `GET /api/admin/sso-domains` → `{"domains": [...]}`.
- `POST /api/admin/sso-domains` `{domain, provider, default_role}` → 201.
  409 "That domain is already connected to another organisation." with no
  hint of which. 422 for a public mailbox domain, a malformed domain, or a
  bad provider or role. Audit `sso_domain_added`.
- `DELETE /api/admin/sso-domains/{id}` → 204. Existing users keep their rows.
  Audit `sso_domain_removed`.
- `PATCH /api/admin/users/{user_id}` extended with `role` and `is_active`.
  Refuses self-demotion and self-deactivation with 409. Audit
  `user_role_changed` (before/after) and `user_deactivated` /
  `user_reactivated`. Rewrites the claims when they are live.
- `GET /api/admin/users` now returns `uid`, `is_active` and `last_login_at`,
  so the admin page stops calling every linked user "not linked".
- `repositories._ENTITY_TYPES` gains `sso_domain`.

## 4. Frontend

- Admin page: a "Single sign-on" panel listing bound domains, with add
  (domain, provider, default role) and remove (confirm first); on each user a
  role selector and a Deactivate / Reactivate control. The uid paste box
  stays for the rare manual link.
- Login page: the Google button is unchanged. The shell treats a 403 whose
  detail mentions deactivation as a message to show, not a redirect to
  signup; any other 403 still goes to signup. After a successful `/api/me`
  while the Firebase token still lacks `tenant_id`, the shell calls
  `refreshToken()` once so the new claims land.
- "Sign in with Microsoft" renders only when `NEXT_PUBLIC_SSO_MICROSOFT=1`
  (default off): the hook for the follow-up, unused in this slice.

## 5. Errors (fail closed)

- No database → no provisioning; claims only.
- A domain lookup that errors refuses the request (503, "needs a database")
  rather than provisioning.
- Provider mismatch, unverified email, deactivated row → refuse.
- Claims write failure after the row is created → proceed on the row, log;
  repaired on the next request.

## 6. Testing (TDD; each test proven to fail without its change)

- `tests/test_auth.py`: every branch of the resolution order against a fake
  directory: deactivated → 403; row role overrides claim; no row → claim
  role; unclaimed + Google + bound → provisioned principal with the default
  role and claims written; unclaimed + Google + bound + existing row → linked,
  not duplicated; unclaimed + Microsoft → 403; unclaimed + unbound → not a
  member; no directory → claims only; `last_login_at` throttle.
- `tests/test_admin_api.py`: sso-domain list/add/remove; public domain and
  malformed domain 422; a domain held by another tenant → 409 with no name;
  role change and deactivation with audit events and `DryRunClaims`
  rewrites; self-demotion and self-deactivation 409; users list carries
  `uid`, `is_active`, `last_login_at`.
- Repository tests (SQLite locally, on the CI Postgres list) for
  `PgUserDirectory` and the sso-domain repository, including the global
  uniqueness of `domain`.
- `tests/test_gcs_and_migrations.py`: migration round trip; RLS parametrize
  row; forced-policy list.
- `tests/test_tenant_isolation_api.py`: probes for the three new routes; the
  module in the no-tenant-in-request test.
- e2e (dev principal): add a domain in the admin panel, change a user's role,
  deactivate a user and see that user refused through `X-Dev-Principal`.
- Live Google sign-in is verified only if the user enables Identity Platform
  on `rasayan` and sets the four frontend values; otherwise the spec records
  that the Google path is covered by faked-token tests and not live-verified.

## Later (out of scope)

Microsoft button and Entra tenant-id verification; per-tenant OIDC or SAML;
DNS proof of domain ownership; SCIM provisioning; sign-in audit events;
session revocation on deactivation (the token stays valid for its hour, but
every request is refused from the next one on).
