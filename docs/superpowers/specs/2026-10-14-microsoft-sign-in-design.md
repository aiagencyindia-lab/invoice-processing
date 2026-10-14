# Microsoft sign-in — design

Brainstormed with the user 2026-10-14; the purpose, the approach and three
design sections were approved in turn. Follows the SSO slice (PR #15), which
left Microsoft "ready but off".

## Why now

Purpose (user): readiness and the SOC 2 story. No customer is waiting, so
small and safe beats self-serve. Google sign-in was verified live on Identity
Platform (project `rasayan`) on 2026-10-14 and the CSP is enforced; Microsoft
is the last sign-in path the product promises.

## Where we start

Measured 2026-10-14 on `main` (`9f6f6f4`):

- `sso.resolve_principal` refuses any `microsoft.com` token without a tenant
  claim: 403 "Microsoft sign-in is not enabled yet."
- `sso_domains` has `provider` (`google` | `microsoft`) and `idp_tenant_id`
  (nullable, "the Entra tenant id, for the Microsoft follow-up", unused).
- The admin form (`SsoDomainCreate`) accepts `provider: microsoft`; such a
  binding admits nobody. An admin may bind only their own sign-in domain
  (`admin_routes.py:424`).
- `SignIn.tsx` renders a disabled "Sign in with Microsoft" button when
  `NEXT_PUBLIC_SSO_MICROSOFT=1`.
- `rasayan` (shared with the user's Rasayan web app) has the generic
  `microsoft.com` provider on (Entra app `33351e5d-2b01-4ce4-801e-0e732adaca26`),
  plus Google; `localhost` is authorized.
- Alembic head `d1a2b3c4e5f6`.

**The problem.** A domain binding must answer "is this person from Acme's
Microsoft 365?". A Firebase token from the generic `microsoft.com` provider
names neither the Entra tenant nor a trustworthy email domain: in a
multi-tenant Entra app anyone in any tenant signs in, and their own tenant
decides what their email says.

## Decisions

1. **Purpose: readiness.** Smallest safe design; an operator step per customer
   is acceptable.
2. **Approach: one Identity Platform OIDC provider per customer Entra
   tenant.** Its issuer is that tenant
   (`https://login.microsoftonline.com/<tid>/v2.0`), so Identity Platform
   refuses anyone else and the token's `firebase.sign_in_provider` names it
   (`oidc.<name>`). Rejected: verifying Microsoft's id_token ourselves (a
   second token verifier to own), and a blocking function (Cloud Functions
   infrastructure).

## Design

### 1. Trust model and data

A token without a tenant claim joins a tenant through a **Microsoft binding
only if both** hold:

- its `sign_in_provider` equals the binding's `idp_provider_id`; and
- its email's domain equals the binding's `domain`.

The provider is the trust anchor: a user of another Entra tenant can be given
`ceo@acme.com`, but signs in through that tenant's provider, which does not
match Acme's binding.

- **Operator-only.** Providers are created by the operator, one per customer,
  so only the operator sets a binding's provider:
  `python -m invoice_processing.admin.provision bind-microsoft --tenant <slug>
  --domain acme.com --provider oidc.acme --entra-tenant <tid> --role clerk`.
  It validates the domain with `sso.normalize_domain` (no public mailboxes),
  requires the provider id to match `^oidc\.[a-z0-9-]{1,40}$`, requires an
  active tenant, refuses a domain already bound, and writes an
  `sso_domain_bound` audit row in the tenant (payload: domain, provider,
  provider id, Entra tenant id, role, operator).
- **The admin form no longer offers Microsoft.** `SsoDomainCreate.provider`
  becomes `Literal["google"]`; the frontend select shows Google only. An
  existing Microsoft binding is listed read-only ("Microsoft 365 — set up by
  Ledgerly support") and can still be disconnected by the tenant admin.
- **Data.** Migration after `d1a2b3c4e5f6` adds `sso_domains.idp_provider_id`
  `String(64)` nullable. `idp_tenant_id` keeps the Entra tenant id (record and
  display). `provider` stays `microsoft`.
- **Email verification.** Entra sends no `email_verified`, so Firebase may
  mark OIDC email unverified. On this path the provider match replaces it: the
  customer's own Entra tenant vouches for its users. Google keeps requiring
  `email_verified is True`.

### 2. Sign-in flow

- **Lookup.** New public route `GET /api/sso/microsoft?email=<address>` →
  `200 {"provider_id": "oidc.acme"}` for a Microsoft binding of the address's
  domain; otherwise `404` "Microsoft sign-in isn't set up for <domain>. Ask
  your administrator." (also for Google bindings, unbound and public-mailbox
  domains, and malformed addresses). `503` "needs a database" when there is
  none. It reveals only whether a domain uses Ledgerly through Microsoft;
  recorded in `SECURITY.md` with the missing rate limit.
- **Frontend.** With `NEXT_PUBLIC_SSO_MICROSOFT=1`, "Sign in with Microsoft"
  opens a work-email field; submit calls the lookup, then
  `signInWithPopup(new OAuthProvider(provider_id))` with `login_hint` set to
  the address, falling back to redirect exactly like Google
  (`POPUP_UNAVAILABLE`). The lookup's 404 sentence is shown as the problem.
- **Backend** (`sso.resolve_principal`, no tenant claim):
  - `microsoft.com` → still 403 (unchanged sentence).
  - `oidc.*` → `bound = directory.domain(domain_of(email))`; unless
    `bound.provider == "microsoft"` and `bound.idp_provider_id == provider`,
    403 "This Microsoft account's organisation isn't connected to
    Ledgerly." On a match, the same provisioning as the Google path.
  - The Google and OIDC paths share one helper, so provisioning, uid
    linking, the `user_provisioned_by_sso` audit row, claims and the closed-
    tenant check exist once.
  - Email is required on both; `email_verified` only on Google.
- **Unchanged.** Tokens with a tenant claim: the users row decides role and
  active status; suspension applies on every path.
- **Errors.** Directory failure → 503 (as today); refusals → 403 sentences;
  the frontend shows the server's sentence.

### 3. Tests

TDD; each new test proved failing first. DB tests on the CI Postgres list.

- `sso`: an `oidc.*` token with a matching binding provisions, links and
  writes claims; a different provider id with the right domain is refused
  (the attacker case); a Microsoft binding never admits a Google token and a
  Google binding never admits an OIDC token; `microsoft.com` still refused;
  an unverified OIDC email is admitted only through the matching provider;
  directory failure → 503; a suspended tenant's binding provisions no one.
- Repository + migration: `idp_provider_id` round-trips through
  `PgUserDirectory.domain`; upgrade and downgrade on SQLite and Postgres.
- `bind-microsoft`: creates the binding and its audit row; refuses a
  malformed provider id, a bound domain, a public mailbox domain, a closed
  tenant.
- Lookup route: Microsoft binding → provider id; Google / unbound / public
  mailbox / malformed → 404 sentence; listed in
  `tests/test_tenant_isolation_api.py` as a public route.
- Admin route: refuses `provider: microsoft`; the listing still returns a
  Microsoft binding (with provider id) and it can be deleted.
- Frontend: e2e covers the email-first button's 404 sentence; Python
  contract test that the Microsoft button calls the lookup.

### 4. Live test (user drives)

On `rasayan`:

- User (console): Identity Platform → Add provider → OpenID Connect, id e.g.
  `oidc.entra-test`, issuer `https://login.microsoftonline.com/<tid>/v2.0`,
  client id `33351e5d-…` and its secret, code flow. In Entra, the app's
  redirect URIs include `https://rasayan.firebaseapp.com/__/auth/handler`.
- Me: scratch database, a test tenant, `bind-microsoft` for the user's Entra
  domain; local stack with real token checks (as on 2026-10-14).
- Check: a Microsoft account from that tenant joins (claims, audit, pages);
  an account from another tenant is refused if one is available; no CSP
  violation under enforcement. Clean-up as before.

### 5. Documents

- `SECURITY.md` sign-in section: provider-as-trust rule, operator-only
  Microsoft binding, the lookup endpoint's disclosure and missing rate limit.
- `deployment/MICROSOFT_SIGN_IN.md`: operator runbook — create the OIDC
  provider, `bind-microsoft`, verify.

## Out of scope

Self-serve Microsoft binding by tenant admins; SAML; the shared
`microsoft.com` provider; rate limiting on the lookup endpoint; a custom
auth domain (the consent screen names `rasayan.firebaseapp.com`).
