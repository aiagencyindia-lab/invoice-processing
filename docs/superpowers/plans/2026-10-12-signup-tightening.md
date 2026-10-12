# Signup Tightening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refuse any token whose email is not verified, and refuse self-signup on an email domain already bound to an organisation.

**Architecture:** One-line tightening in `auth.principal_from_claims`; two new checks in `api/signup.py::sign_up` that reuse sign-in's own services accessor (`auth._services()`) and domain lookup (`directory.domain`) so they fail closed the same way; one frontend fix so a freshly verified account presents a fresh token.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 (SQLite tests, Postgres/pg8000 in CI), pytest; Next.js 16 / React 19 / Firebase JS SDK.

**Spec:** `docs/superpowers/specs/2026-10-12-signup-tightening-design.md`

## Global Constraints

- Always `./.venv/bin/python`; run from `/Users/vineet/Downloads/ADK/invoice-processing`.
- Unverified refusal (auth): existing text `"Email address is not verified."`, 403.
- Signup unverified refusal: 403, exactly `"Verify your email address before creating an organisation."`
- Signup bound-domain refusal: 403, exactly `"Your organisation's domain is already connected to Ledgerly. Sign in with Google using this address and you'll join it."` — never names the tenant. Never 409 (the page treats 409 as "already a member").
- Directory unreachable during signup → 503 via `sso._unavailable` (its text contains "could not be reached"). No database configured (`auth._services()` returns `None`) → skip the domain check.
- A binding refuses signup whatever its `provider`.
- Comments explain *why*; ruff check + format clean.
- Commit subject imperative, says why; trailer `Co-Authored-By: Claude <model that wrote it> <noreply@anthropic.com>`.
- Never weaken an assertion to make a test pass; test fakes gain `"email_verified": True` only.

## Review Focus

1. A binding whose provider is `microsoft` must still refuse signup — test in Task 2.
2. `email_verified` as the string `"true"` must be refused by signup as well as by auth — tests in Tasks 1 and 2.
3. Mixed-case email (`Ann@ACME.test`) against a stored `acme.test` binding must refuse — test in Task 2.
4. A refused signup must leave no tenant and no `tenant_self_provisioned` audit line — asserted in Task 2's refusal tests.
5. An already-verified user landing on `/signup` with a token minted before verification must get a fresh token before the organisation step — Task 3 (`settle`), checked by review; there is no frontend unit-test runner.

---

### Task 1: Refuse a token unless `email_verified is True`

**Files:**
- Modify: `invoice_processing/api/auth.py` (`principal_from_claims`, the `email_verified` check ~line 181)
- Test: `tests/test_auth.py` (class `TestTenantComesFromTheToken`, next to `test_unverified_email_is_refused` ~line 132)
- Modify (test data only): verifier fakes that build claims without `email_verified`: `tests/test_exports.py`, `tests/test_kpi_api.py`, `tests/test_overrides.py`, `tests/test_record_locks.py`, `tests/test_rule_proposals.py`, `tests/test_sheet_api.py`, plus any other file the full suite turns red for this reason.

**Interfaces:**
- Consumes: nothing new.
- Produces: `principal_from_claims(claims)` raises `AuthError(403, "Email address is not verified.")` unless `claims["email_verified"] is True`.

- [ ] **Step 1: Write the failing tests** — add to `TestTenantComesFromTheToken` in `tests/test_auth.py`:

```python
    def test_a_token_without_email_verified_is_refused(self):
        """Firebase always sets it, so its absence means a token we do not
        understand -- refuse rather than assume."""
        claims = {k: v for k, v in ACME.items() if k != "email_verified"}
        with pytest.raises(AuthError) as caught:
            principal_from_claims(claims)
        assert caught.value.status_code == 403

    def test_email_verified_must_be_the_boolean_true(self):
        with pytest.raises(AuthError):
            principal_from_claims({**ACME, "email_verified": "true"})

    def test_a_verified_email_is_admitted(self):
        assert principal_from_claims(ACME).email == "clerk@acme.test"
```

- [ ] **Step 2: Run to verify the first two fail**

Run: `./.venv/bin/python -m pytest tests/test_auth.py -q -p no:cacheprovider -k "email_verified or verified_email"`
Expected: 2 failed (`DID NOT RAISE`), 1 passed.

- [ ] **Step 3: Implement** — in `principal_from_claims` replace

```python
    if claims.get("email_verified") is False:
```

with

```python
    if claims.get("email_verified") is not True:
```

and extend the comment above the raise with one line: `# Absent or non-boolean counts as unverified: we only trust what Google asserted.`

- [ ] **Step 4: Run the full suite, align fakes**

Run: `./.venv/bin/python -m pytest tests/ -q -p no:cacheprovider`
For every failure caused by a fake verifier whose claims lack `email_verified`, add `"email_verified": True` to the claims that fake returns (e.g. in `tests/test_kpi_api.py` the `CLAIMS` dict entries). Change nothing else. Re-run until: all pass, 3 skipped.

- [ ] **Step 5: Lint**

Run: `./.venv/bin/python -m ruff check . && ./.venv/bin/python -m ruff format --check .`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add invoice_processing/api/auth.py tests/
git commit -m "Refuse a token that does not say its email is verified

Co-Authored-By: Claude <model> <noreply@anthropic.com>"
```

---

### Task 2: Signup refuses unverified email and bound domains

**Files:**
- Modify: `invoice_processing/api/signup.py` (`sign_up`, after the "already belongs" 409 and before the organisation-name check)
- Modify: `tests/test_signup_api.py` (FakeVerifier, `env` fixture, new tests)
- Modify: `.github/workflows/ci.yml` (add `tests/test_signup_api.py` to the "DB-backed tests against Postgres" list)

**Interfaces:**
- Consumes: `auth._services() -> sso.SsoServices | None` (raises 503 `AuthError` when a configured DB cannot be asked); `services.directory.domain(domain: str) -> dict | None`; `sso.domain_of(email) -> str`; `sso._unavailable(exc) -> AuthError` (503); `PgSsoDomainRepository(url).add(tenant_id, *, domain, provider, default_role, created_by)`; `FakeDirectory(fail=True)` from `tests/test_sso.py`.
- Produces: `POST /api/signup` → 403 unverified / 403 bound domain / 503 directory down, as in Global Constraints.

- [ ] **Step 1: Make the fixture dialect-agnostic and the fake able to vary verification**

In `tests/test_signup_api.py`, replace `FakeVerifier` and `env` with:

```python
class FakeVerifier:
    """Token format: `<email>|<uid>|<tenant or blank>|<verified>`.

    `verified` is optional: blank means True, `no` False, `absent` omits the
    claim, `str` sends the string "true".
    """

    def verify(self, token):
        email, uid, tenant, verified = (token.split("|") + ["", "", ""])[:4]
        claims = {"sub": uid, "user_id": uid, "email": email}
        if verified == "":
            claims["email_verified"] = True
        elif verified == "no":
            claims["email_verified"] = False
        elif verified == "str":
            claims["email_verified"] = "true"
        if tenant:
            claims["tenant_id"] = tenant
            claims["role"] = "admin"
        return claims


@pytest.fixture
def env(scratch_db, tmp_path, monkeypatch):
    url = scratch_db("signup.db")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("CLOUD_SQL_INSTANCE", raising=False)
    monkeypatch.setenv("ALLOW_SELF_SIGNUP", "1")
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    engine = get_engine(url)
    Base.metadata.drop_all(engine)
    create_all(url)

    monkeypatch.setattr(auth, "_verifier", FakeVerifier())
    monkeypatch.setattr(
        service, "_audit", JsonlAuditSink(tmp_path / "audit.jsonl")
    )
    writer = DryRunClaims()
    monkeypatch.setattr(service, "get_claims_writer", lambda: writer)
    return {"url": url, "tmp": tmp_path}
```

Update imports: `from invoice_processing.db.session import create_all, get_engine, tenant_session`; add `from sqlalchemy import select`, `from sqlalchemy.orm import Session`, `from invoice_processing import sso`, `from invoice_processing.db.models import Base, Tenant`, `from invoice_processing.db.repositories import PgSsoDomainRepository`, `from tests.test_sso import FakeDirectory`.

Run: `./.venv/bin/python -m pytest tests/test_signup_api.py -q -p no:cacheprovider`
Expected: all existing tests pass (fixture refactor only).

- [ ] **Step 2: Write the failing tests** — add helpers and a class:

```python
def bind(url, domain, provider="google"):
    """Bind `domain` to another organisation, as its admin would."""
    with tenant_session("t-held", url) as session:
        session.add(Tenant(id="t-held", slug="held", name="Held Co"))
    PgSsoDomainRepository(url).add(
        "t-held",
        domain=domain,
        provider=provider,
        default_role="viewer",
        created_by=f"admin@{domain}",
    )


def slugs(url):
    with Session(get_engine(url)) as session:
        return set(session.scalars(select(Tenant.slug)))


def audited(env):
    path = env["tmp"] / "audit.jsonl"
    return path.exists() and "tenant_self_provisioned" in path.read_text()


class TestItRefusesUnprovenIdentity:
    @pytest.mark.parametrize("verified", ["no", "absent", "str"])
    def test_an_unverified_address(self, env, verified):
        """Anyone who can type an address could otherwise found an
        organisation in its name."""
        response = client(f"a@b.test|uid-1||{verified}").post(
            "/api/signup", json={"organisation": "Acme"}
        )
        assert response.status_code == 403
        assert response.json()["detail"] == (
            "Verify your email address before creating an organisation."
        )
        assert "acme" not in slugs(env["url"])

    @pytest.mark.parametrize("provider", ["google", "microsoft"])
    def test_an_address_on_a_bound_domain(self, env, provider):
        """The domain belongs to an organisation already; a rival one on it
        would split that company's invoices across two tenants."""
        bind(env["url"], "acme.test", provider)
        response = client("ann@acme.test|uid-1").post(
            "/api/signup", json={"organisation": "Acme"}
        )
        assert response.status_code == 403
        detail = response.json()["detail"]
        assert "Sign in with Google" in detail
        assert "Held Co" not in detail
        assert slugs(env["url"]) == {"held"}
        assert not audited(env)

    def test_the_bound_domain_check_ignores_case(self, env):
        bind(env["url"], "acme.test")
        response = client("Ann@ACME.test|uid-1").post(
            "/api/signup", json={"organisation": "Acme"}
        )
        assert response.status_code == 403

    def test_an_unreachable_directory_refuses(self, env, monkeypatch):
        """No lookup, no signup: the check fails closed like sign-in."""
        monkeypatch.setattr(
            auth,
            "_sso_services",
            lambda: sso.SsoServices(FakeDirectory(fail=True), None),
        )
        response = client("ann@acme.test|uid-1").post(
            "/api/signup", json={"organisation": "Acme"}
        )
        assert response.status_code == 503
        assert "acme" not in slugs(env["url"])

    def test_an_unbound_domain_still_signs_up(self, env):
        bind(env["url"], "other.test")
        response = client("ann@acme.test|uid-1").post(
            "/api/signup", json={"organisation": "Acme"}
        )
        assert response.status_code == 200
        assert "acme" in slugs(env["url"])
```

- [ ] **Step 3: Run to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_signup_api.py -q -p no:cacheprovider -k TestItRefusesUnprovenIdentity`
Expected: the unverified, bound-domain, case and unreachable tests FAIL (200 instead of 403/503); `test_an_unbound_domain_still_signs_up` passes.

- [ ] **Step 4: Implement** — in `sign_up`, directly after the "already belongs" 409 block:

```python
    # The verified address is the identity everything below is keyed on; a
    # token that does not vouch for it cannot found an organisation.
    if claims.get("email_verified") is not True:
        raise HTTPException(
            status_code=403,
            detail="Verify your email address before creating an organisation.",
        )

    # A bound domain already has an organisation; a second one on it would
    # split a company across tenants. Same accessor and lookup as sign-in,
    # so an unreachable database refuses (503) instead of skipping the check.
    # 403, not 409: the page reads 409 as "already a member".
    from .. import sso  # noqa: PLC0415
    from .auth import _services  # noqa: PLC0415

    services = _services()
    if services is not None:
        try:
            bound = services.directory.domain(sso.domain_of(str(claims["email"])))
        except Exception as exc:
            raise sso._unavailable(exc) from exc
        if bound is not None:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Your organisation's domain is already connected to "
                    "Ledgerly. Sign in with Google using this address and "
                    "you'll join it."
                ),
            )
```

Also update the module docstring's last paragraph with one sentence: the address must be verified and its domain unbound.

- [ ] **Step 5: Run signup and full suite**

Run: `./.venv/bin/python -m pytest tests/test_signup_api.py -q -p no:cacheprovider` → all pass.
Run: `./.venv/bin/python -m pytest tests/ -q -p no:cacheprovider` → all pass, 3 skipped.
Run: `./.venv/bin/python -m ruff format invoice_processing/api/signup.py tests/test_signup_api.py`, then `./.venv/bin/python -m ruff check . && ./.venv/bin/python -m ruff format --check .` → clean.

- [ ] **Step 6: Add to the CI Postgres list** — in `.github/workflows/ci.yml`, step "DB-backed tests against Postgres", add a line `tests/test_signup_api.py \` after `tests/test_tenant_isolation_api.py \`.

- [ ] **Step 7: Run on Postgres locally**

```bash
docker run -d --rm --name pg-signup -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=t -p 55432:5432 postgres:16
```
Wait until `docker exec pg-signup pg_isready` succeeds, then:
```bash
TEST_DATABASE_URL=postgresql+pg8000://postgres:postgres@localhost:55432/t ./.venv/bin/python -m pytest tests/test_signup_api.py -q -p no:cacheprovider
```
Expected: all pass. Then `docker stop pg-signup`. (Never touch container `bsi-platform-db-1`.)

- [ ] **Step 8: Commit**

```bash
git add invoice_processing/api/signup.py tests/test_signup_api.py .github/workflows/ci.yml
git commit -m "Refuse signup on an unverified address or an already-bound domain

Co-Authored-By: Claude <model> <noreply@anthropic.com>"
```

---

### Task 3: Present a fresh token once the email is verified

**Files:**
- Modify: `frontend/src/app/signup/page.tsx` (`settle` callback ~line 33; "I have verified" button handler ~line 125)

**Interfaces:**
- Consumes: `refreshToken(): Promise<void>` and `isEmailVerified(): Promise<boolean>` from `@/lib/auth` (both exist; `refreshToken` is already imported).
- Produces: nothing new.

- [ ] **Step 1: `settle`** — replace

```tsx
    setStep((await isEmailVerified()) ? "organisation" : "verify");
```

with

```tsx
    if (await isEmailVerified()) {
      // reload() updates the user, not the ID token: the cached token still
      // says email_verified=false and the server would refuse signup.
      await refreshToken();
      setStep("organisation");
    } else {
      setStep("verify");
    }
```

- [ ] **Step 2: The "I have verified" button** — replace its `onClick` body with:

```tsx
              onClick={async () => {
                setProblem(null);
                if (await isEmailVerified()) {
                  await refreshToken();
                  setStep("organisation");
                } else {
                  setProblem("Not verified yet — open the link, then try again.");
                }
              }}
```

- [ ] **Step 3: Lint, type-check, e2e**

```bash
cd frontend && npm run lint && npx tsc --noEmit && npm run test:e2e
```
Expected: lint and tsc clean; e2e 34 passed.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/app/signup/page.tsx
git commit -m "Refresh the token after verification so signup is not refused

Co-Authored-By: Claude <model> <noreply@anthropic.com>"
```
