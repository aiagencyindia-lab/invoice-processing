# Signup tightening: verified email everywhere, no signup on a bound domain — design

Phase 3, SSO follow-up 1. Brainstormed with the user 2026-10-12; both
sections below were approved in turn.

## Goal

Close the two gaps PR #15 left open: no request is admitted on an address the
identity provider has not verified, and nobody can start a rival organisation
on a company domain that an admin has already bound to theirs.

Success: both refusals are enforced by the server, each new test fails on the
pre-change commit, every gate stays green, and a legitimate signup (verified
address, unbound domain) still works.

## Where we start

- `auth.principal_from_claims` refuses only `email_verified is False`; a token
  that omits the claim is admitted. (Firebase always sets it, so this is
  defence in depth, but the rule should say what it means.)
- `POST /api/signup` (gated by `ALLOW_SELF_SIGNUP`) checks neither email
  verification nor domain binding. The signup page checks `emailVerified` in
  the browser only.
- The hole this leaves: a password account at a bound domain (say `acme.com`)
  is refused at sign-in — only a `google.com` token can be linked through a
  binding — so `AppShell` sends it to `/signup`, where today it can create a
  second "Acme" organisation.
- Latent frontend bug the new server check would expose: `isEmailVerified()`
  reloads the Firebase user but keeps the cached ID token, which still says
  `email_verified: false` until it expires. A freshly verified person would be
  refused by the server.

## Decisions (user rulings)

1. **Scope: tighten now, Microsoft later.** Microsoft sign-in and the Entra
   `tid` check become their own sub-project once an Entra app registration
   exists to test against. (The Firebase ID token does not carry `tid`, and
   the Microsoft email is not trustworthy, so that work needs a second,
   Microsoft-signed token or a blocking function.)
2. Signup stays gated by `ALLOW_SELF_SIGNUP`. Public mailbox addresses
   (gmail.com, …) may still sign up a one-person organisation.
3. Not in scope: refusing signup when the address already has a users row in
   some other tenant without a claim yet. It needs a cross-tenant lookup;
   domain binding covers the realistic case. Add it if claim writes are seen
   failing in practice.

## Design

### 1. `principal_from_claims`

`claims.get("email_verified") is False` becomes `is not True`. Missing, `None`,
or the string `"true"` are all refused with the existing 403
"Email address is not verified." Every path that builds a principal from a
token goes through here (the claimed path and the "usual 403" fall-throughs in
`sso.resolve_principal`); the unclaimed SSO path already requires `is True`.

### 2. `POST /api/signup`

Order of checks, after the existing ones (`ALLOW_SELF_SIGNUP` off → 403;
token already has a tenant → 409) and before the organisation name check:

1. `claims.get("email_verified") is not True` → **403** "Verify your email
   address before creating an organisation."
2. Domain lookup: `services = auth._services()` — the same accessor sign-in
   uses. It returns `None` only when no database (or no users table) is
   configured, and raises the 503 `AuthError` when a configured database
   cannot be reached. With services present,
   `services.directory.domain(sso.domain_of(email))`:
   - raises → **503** via `sso._unavailable` (signup never proceeds without
     the check);
   - returns a binding (any provider) → **403** "Your organisation's domain is
     already connected to Ledgerly. Sign in with Google using this address
     and you'll join it." The tenant is not named.
   - returns `None` → continue.
   No services (no database) → skip the lookup; there are no bindings to
   consult. Same rule as sign-in.

403, not 409: the signup page treats 409 as "already a member, go to the
dashboard", which would loop this person between `/dashboard` and `/signup`.

The lookup is case-insensitive: `domain_of` lowercases and `directory.domain`
lowercases again; bindings are stored normalized.

### 3. Signup page (`frontend/src/app/signup/page.tsx`)

Call `refreshToken()` whenever verification is observed as done, before
showing the organisation step: in `settle` when `isEmailVerified()` is true,
and in the "I have verified" button handler. Nothing else changes; a 403
detail is already shown through `ApiError.friendly`.

## Testing

Each new test must fail on `main` (`ae53e0f`); prove it in a scratch worktree
of that commit with the new tests copied in, then remove the worktree.

- `tests/test_auth.py`: claims without `email_verified` → 403; with
  `"email_verified": "true"` (string) → 403; with `True` → admitted.
- `tests/test_signup_api.py`:
  - `email_verified: False` → 403, no tenant created;
  - claim missing → 403;
  - email on a domain bound to another tenant → 403 with the "Sign in with
    Google" text, no tenant created, no `tenant_self_provisioned` audit;
  - mixed-case email (`Ann@ACME.test`) on a bound `acme.test` → 403;
  - directory `domain()` raising → 503, no tenant created;
  - verified address on an unbound domain → 200 (existing happy path, now
    with the field present).
- Verifier fakes without `email_verified` get `"email_verified": True`
  (test data alignment only, no assertion relaxed): `test_exports.py`,
  `test_kpi_api.py`, `test_overrides.py`, `test_record_locks.py`,
  `test_rule_proposals.py`, `test_sheet_api.py`, and any other fake the
  tightened check turns red.
- Postgres parity: one signup test that seeds a binding in tenant A and
  signs up from that domain must run on the CI Postgres list
  (`.github/workflows/ci.yml`, "DB-backed tests against Postgres"), proving
  the signup path works on real Postgres and pg8000. (CI connects as a
  superuser, which bypasses RLS; the sentinel's RLS itself is already proven
  as a NOBYPASSRLS role by
  `test_sso_lookup_sentinel_reads_domains_and_nothing_else`.)
- Frontend: `npm run lint`, `npx tsc --noEmit`, e2e 34/34.

Gates: full pytest, ruff check + format, `benchmark/harness/gates.sh`, the
Postgres CI list locally, and a browser check that `/signup` still renders.

## Residual risk

- The real Firebase path (password signup → verification link → fresh token
  → `/api/signup`) cannot be exercised locally without Identity Platform. It
  joins the live-verify list alongside SSO.
- A domain bound *after* someone already self-signed up on it leaves their
  organisation in place; binding is first come. Unchanged from PR #15.
