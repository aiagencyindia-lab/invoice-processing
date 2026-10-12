# Audit Integrity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The API can no longer rewrite or truncate the audit trail, refused access is recorded, and an admin can export the trail for a date range as CSV.

**Architecture:** Refusals are audited through the existing `SsoServices` wiring (a third `audit` member) so `auth.require_role` and `sso._refuse_inactive` reach the sink without an import cycle. `PgAuditSink.query` learns a date range and a keyset cursor; `api/system.py` serialises through one `_audit_dict` for the list, the CSV stream and the JSONL fallback. Postgres privileges move to a two-role bootstrap (owner migrates, app serves) proven by a Postgres-only test; `db/migrate.py` gains a CLI for the owner's release step.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic, Postgres 16 (pg8000) / SQLite, pytest; Next.js 16, React 19, Playwright.

**Spec:** `docs/superpowers/specs/2026-10-12-audit-integrity-design.md`

## Global Constraints

- Always `./.venv/bin/python`; run from `/Users/vineet/Downloads/ADK/invoice-processing`. Tests: `-q -p no:cacheprovider`.
- Audit failures are never swallowed: a sink error during a refusal → 503 via `sso._unavailable` (text contains "could not be reached"); the request is still refused.
- Refusals are audited only when `auth._services()` returns services with a non-None `audit`; no database → nothing recorded (`# ponytail:` comment).
- Event names: `access_denied`, `sign_in_refused`, `audit_exported`. `_ENTITY_TYPES` gains `"access": "user"`, `"sign_in": "user"`, `"audit": "audit_trail"`.
- `/api/audit` keeps keys `at, actor, action, entity_type, entity_id, payload, before`; adds `id, actor_type, request_id, ip`; response adds `next_cursor`. Cursor format `"<iso>|<id>"`; malformed → 422.
- `/api/audit/export` is `require_role("admin")`; `audit_exported` is emitted **before** streaming; CSV header exactly `id,occurred_at,actor,actor_type,action,entity_type,entity_id,request_id,ip,before,after`.
- App role: `NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB`; only `SELECT, INSERT` on `audit_events`. Never edit an applied migration.
- `DATABASE_URL`/`CLOUD_SQL_USER` stay the app credentials; the migrate CLI reads `MIGRATE_DATABASE_URL`/`MIGRATE_CLOUD_SQL_USER`.
- Every new route needs a cross-tenant probe and a `PROBED` entry in `tests/test_tenant_isolation_api.py`. New DB test files or node ids go on the CI Postgres list.
- Comments explain why; ruff check + format clean; commit subjects imperative and say why; trailer `Co-Authored-By: Claude <model that wrote it> <noreply@anthropic.com>`.

## Review Focus

1. `since` later than `until` must return an empty list, not an error or everything — test in Task 2.
2. A cursor from one tenant presented by another must yield nothing from the first tenant — covered by RLS plus the explicit `tenant_id` filter; probe in Task 3.
3. A refusal on a route whose template has a path parameter must record the template (`/api/invoices/{case_id}/actions`), not the concrete id — test in Task 1.
4. Export of a range with zero events must still return the header row with 200 — test in Task 3.
5. The dev-bypass deactivated path (`enforce_active`) must audit `sign_in_refused` too, not only the token path — test in Task 1.

---

### Task 1: Record refused access

**Files:**
- Modify: `invoice_processing/db/repositories.py:748-764` (`_ENTITY_TYPES`)
- Modify: `invoice_processing/sso.py` (`SsoServices`, `_refuse_inactive`, its three call sites at ~107, ~135, ~197)
- Modify: `invoice_processing/api/auth.py:340-358` (`require_role`)
- Modify: `invoice_processing/api/service.py:427-443` (`_sso_services`)
- Test: `tests/test_db_repositories.py` (~line 903), `tests/test_sso.py`, `tests/test_auth.py`

**Interfaces:**
- Consumes: `auth._services() -> sso.SsoServices | None`; `Principal.context() -> RunContext` (carries request id and client ip from `auth.current_request_id` / `auth.current_client_ip`); sinks expose `emit(ctx, event_type, payload, before=None)`.
- Produces: `sso.SsoServices(directory, claims, audit=None)`; `sso._refuse_inactive(row, tenant_id, email, services)`; `service._sso_services()` returns the audit sink as the third member.

- [ ] **Step 1: Failing tests — entity types.** In `tests/test_db_repositories.py::test_an_event_says_what_it_is_about` add:

```python
        assert entity_type_for("access_denied") == "user"
        assert entity_type_for("sign_in_refused") == "user"
        assert entity_type_for("audit_exported") == "audit_trail"
```

Run: `./.venv/bin/python -m pytest tests/test_db_repositories.py -q -p no:cacheprovider -k says_what_it_is_about` — Expected: FAIL (`None != 'user'`).

- [ ] **Step 2: Implement.** Add to `_ENTITY_TYPES`:

```python
    "access": "user",
    "sign_in": "user",
    "audit": "audit_trail",
```

Re-run Step 1 — Expected: PASS.

- [ ] **Step 3: Failing tests — sign_in_refused.** In `tests/test_sso.py` add after `FakeClaims`:

```python
class FakeAudit:
    def __init__(self, fail=False):
        self.events, self.fail = [], fail

    def emit(self, ctx, event_type, payload, before=None):
        if self.fail:
            raise RuntimeError("audit down")
        self.events.append((ctx.tenant_id, ctx.actor, event_type, payload))
```

Change the `services` helper to pass a third member:

```python
def services(**kw):
    return sso.SsoServices(
        kw.get("directory", FakeDirectory()),
        kw.get("claims", FakeClaims()),
        kw.get("audit", FakeAudit()),
    )
```

Add to `TestClaimedTokens`:

```python
    def test_a_deactivated_user_is_recorded_as_refused(self):
        off = dict(ROW, is_active=False)
        audit = FakeAudit()
        with pytest.raises(AuthError):
            sso.resolve_principal(
                claimed(),
                services(directory=FakeDirectory([off]), audit=audit),
                now=NOW,
            )
        assert audit.events == [
            (
                ACME,
                "pat@acme.com",
                "sign_in_refused",
                {"email": "pat@acme.com", "user_id": "u-1", "reason": "deactivated"},
            )
        ]

    def test_a_refusal_that_cannot_be_recorded_is_a_503(self):
        """The refusal stands either way; what must not happen is a silent gap."""
        off = dict(ROW, is_active=False)
        with pytest.raises(AuthError) as exc:
            sso.resolve_principal(
                claimed(),
                services(directory=FakeDirectory([off]), audit=FakeAudit(fail=True)),
                now=NOW,
            )
        assert exc.value.status_code == 503

    def test_a_deactivated_dev_identity_is_recorded_too(self):
        """enforce_active is the dev-bypass path; it must not be the quiet one."""
        off = dict(ROW, is_active=False)
        audit = FakeAudit()
        dev = Principal(user_id="dev", email="pat@acme.com", tenant_id=ACME, role="admin")
        with pytest.raises(AuthError):
            sso.enforce_active(dev, services(directory=FakeDirectory([off]), audit=audit))
        assert [e[2] for e in audit.events] == ["sign_in_refused"]

    def test_without_a_sink_a_refusal_is_still_a_refusal(self):
        off = dict(ROW, is_active=False)
        with pytest.raises(AuthError) as exc:
            sso.resolve_principal(
                claimed(), services(directory=FakeDirectory([off]), audit=None), now=NOW
            )
        assert exc.value.status_code == 403
```

Run: `./.venv/bin/python -m pytest tests/test_sso.py -q -p no:cacheprovider` — Expected: the four new tests FAIL (`TypeError` on the 3-arg `SsoServices`, then assertion failures); everything else passes once the NamedTuple default exists.

- [ ] **Step 4: Implement in `sso.py`.**

```python
class SsoServices(NamedTuple):
    directory: Any  # PgUserDirectory-shaped
    claims: Any  # ClaimsWriter
    audit: Any = None  # AuditSink; None when there is no database


def _refuse_inactive(
    row: dict, tenant_id: str, email: str, services: SsoServices
) -> None:
    if row.get("is_active") is not False:
        return
    # ponytail: no database means no sink, and the refusal goes unrecorded;
    # the same rule as the rest of sign-in.
    if services.audit is not None:
        ctx = Principal(
            user_id=str(row.get("uid") or ""),
            email=email,
            tenant_id=tenant_id,
            role=str(row.get("role") or "viewer"),
        ).context()
        try:
            services.audit.emit(
                ctx,
                "sign_in_refused",
                {"email": email, "user_id": row["id"], "reason": "deactivated"},
            )
        except Exception as exc:  # a refusal nobody can see is the real failure
            raise _unavailable(exc) from exc
    raise AuthError(
        "This account has been deactivated. Ask an admin of "
        f"{row.get('tenant_name') or 'your organisation'} to reactivate it.",
        status_code=403,
    )
```

Update the three call sites: `enforce_active` → `_refuse_inactive(row, principal.tenant_id, principal.email, services)`; claimed path → `_refuse_inactive(row, base.tenant_id, base.email, services)`; domain path → `_refuse_inactive(row, tenant_id, email, services)`. In the domain path that call sits inside `try: ... except AuthError: raise; except Exception as exc: raise _unavailable(exc)`, so a 503 from the sink passes through as an `AuthError` already.

Run Step 3 — Expected: PASS.

- [ ] **Step 5: Failing tests — access_denied.** In `tests/test_auth.py`, the `app` fixture's test application has no middleware, so `request_id`/`ip` are empty there; the shape is what is tested. Add a route with a path parameter to the fixture:

```python
    @application.post("/invoices/{case_id}/approve")
    def approve_one(
        case_id: str,
        principal: Annotated[Principal, Depends(require_role("approver"))],
    ):
        return {"ok": True}
```

Add a new class (import `FakeAudit` from `tests.test_sso`):

```python
class TestRefusalsAreRecorded:
    @pytest.fixture
    def audit(self, monkeypatch):
        sink = FakeAudit()
        monkeypatch.setattr(
            auth,
            "_sso_services",
            lambda: sso.SsoServices(FakeDirectory(), FakeClaims(), sink),
        )
        return sink

    def test_a_role_refusal_names_the_route_and_the_roles(self, app, audit):
        r = app.post("/invoices/inv-7/approve", headers=bearer("acme-token"))
        assert r.status_code == 403
        assert audit.events == [
            (
                "tenant-acme",
                "clerk@acme.test",
                "access_denied",
                {
                    "path": "/invoices/{case_id}/approve",
                    "method": "POST",
                    "required_role": "approver",
                    "actual_role": "clerk",
                },
            )
        ]

    def test_a_refusal_that_cannot_be_recorded_is_a_503(self, app, monkeypatch):
        monkeypatch.setattr(
            auth,
            "_sso_services",
            lambda: sso.SsoServices(FakeDirectory(), FakeClaims(), FakeAudit(fail=True)),
        )
        assert app.post("/approve", headers=bearer("acme-token")).status_code == 503

    def test_without_services_a_refusal_is_a_plain_403(self, app):
        assert app.post("/approve", headers=bearer("acme-token")).status_code == 403

    def test_a_permitted_call_records_nothing(self, app, audit, monkeypatch):
        monkeypatch.setattr(
            auth, "_verifier", FakeVerifier({"boss": {**ACME, "role": "approver"}})
        )
        assert app.post("/approve", headers=bearer("boss")).status_code == 200
        assert audit.events == []
```

Run: `./.venv/bin/python -m pytest tests/test_auth.py -q -p no:cacheprovider -k TestRefusalsAreRecorded` — Expected: first two FAIL (empty events / 403 not 503), last two pass.

- [ ] **Step 6: Implement `require_role`.**

```python
def require_role(required: str):
    """Dependency factory enforcing a minimum role.

    Returns 403, not 404: the caller is authenticated and the resource exists;
    hiding that would make a permissions problem look like a bug. The refusal
    is written to the audit trail when there is one: an auditor's first
    question about access control is whether denials leave a trace.
    """

    def dependency(
        request: Request,
        principal: Annotated[Principal, Depends(current_principal)],
    ) -> Principal:
        if principal.can(required):
            return principal
        services = _services()
        if services is not None and services.audit is not None:
            route = request.scope.get("route")
            try:
                services.audit.emit(
                    principal.context(),
                    "access_denied",
                    {
                        # The template, so one line covers every invoice.
                        "path": getattr(route, "path", request.url.path),
                        "method": request.method,
                        "required_role": required,
                        "actual_role": principal.role,
                    },
                )
            except Exception as exc:
                from .. import sso  # noqa: PLC0415

                raise sso._unavailable(exc) from exc
        raise AuthError(
            f"Role '{principal.role}' cannot perform this action; "
            f"'{required}' or higher is required.",
            status_code=403,
        )

    return dependency
```

- [ ] **Step 7: Wire the real sink.** In `service._sso_services()` the last line becomes `return sso.SsoServices(_user_directory, get_claims_writer(), get_audit())`. Check `tests/conftest.py resolve_adapters_afresh` already resets `_audit` (it does: `"_audit"` is in the list).

- [ ] **Step 8: Run, lint, commit.**

Run: `./.venv/bin/python -m pytest tests/test_auth.py tests/test_sso.py tests/test_db_repositories.py tests/test_admin_api.py tests/test_tenant_isolation_api.py -q -p no:cacheprovider` — Expected: all pass. Then the full suite — Expected: all pass, 3 skipped. (If a route test somewhere asserts a 403 against a sink that raises, it will now see 503; read it and align the fixture, never the resolver.) `ruff check . && ruff format --check .` clean.

```bash
git add invoice_processing/db/repositories.py invoice_processing/sso.py invoice_processing/api/auth.py invoice_processing/api/service.py tests/test_db_repositories.py tests/test_sso.py tests/test_auth.py
git commit -m "Record a refused role or a deactivated sign-in in the audit trail

Co-Authored-By: Claude <model> <noreply@anthropic.com>"
```

---

### Task 2: Date range, cursor and full fields on `/api/audit`

**Files:**
- Modify: `invoice_processing/db/repositories.py:816-826` (`PgAuditSink.query`)
- Modify: `invoice_processing/api/system.py:508-585` (`list_audit`, new `_audit_dict`, `_parse_cursor`, `_read_jsonl_audit`)
- Test: `tests/test_system_api.py` (`TestAudit`, ~577-640), `tests/test_db_repositories.py`

**Interfaces:**
- Consumes: `JsonlAuditSink` records (`timestamp, tenant_id, case_id, run_id, actor, event_type, before, payload`); `AuditEvent` ORM columns.
- Produces: `PgAuditSink.query(tenant_id, limit=100, since=None, until=None, before=None) -> list[AuditEvent]`; `system._audit_dict(e) -> dict`; `system._parse_cursor(cursor) -> tuple[datetime, str]`; `system._read_jsonl_audit(tenant_id, limit, since=None, until=None) -> list[dict]`.

- [ ] **Step 1: Failing repository tests.** In `tests/test_db_repositories.py` (class holding `test_a_document_event_is_not_labelled_an_invoice`, which shows the `PgAuditSink(db)` + `RunContext` pattern) add:

```python
    def test_query_narrows_to_a_window_and_pages_by_cursor(self, db):
        from datetime import datetime, timedelta, timezone  # noqa: PLC0415

        from invoice_processing.db.models import AuditEvent  # noqa: PLC0415

        t0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
        with tenant_session(ACME, db) as s:
            for i in range(5):
                s.add(AuditEvent(id=f"e{i}", tenant_id=ACME, occurred_at=t0 + timedelta(hours=i), action="x"))
        sink = PgAuditSink(db)
        window = sink.query(ACME, since=t0 + timedelta(hours=1), until=t0 + timedelta(hours=3))
        assert [e.id for e in window] == ["e3", "e2", "e1"]
        first = sink.query(ACME, limit=2)
        rest = sink.query(ACME, limit=10, before=(first[-1].occurred_at, first[-1].id))
        assert [e.id for e in first] == ["e4", "e3"]
        assert [e.id for e in rest] == ["e2", "e1", "e0"]
        assert sink.query(ACME, since=t0 + timedelta(hours=4), until=t0) == []
```

Run: `./.venv/bin/python -m pytest tests/test_db_repositories.py -q -p no:cacheprovider -k window_and_pages` — Expected: FAIL (`unexpected keyword argument 'since'`).

- [ ] **Step 2: Implement `query`.**

```python
    def query(
        self,
        tenant_id: str,
        limit: int = 100,
        since: datetime | None = None,
        until: datetime | None = None,
        before: tuple[datetime, str] | None = None,
    ) -> list[AuditEvent]:
        stmt = select(AuditEvent).where(AuditEvent.tenant_id == tenant_id)
        if since is not None:
            stmt = stmt.where(AuditEvent.occurred_at >= since)
        if until is not None:
            stmt = stmt.where(AuditEvent.occurred_at <= until)
        if before is not None:
            # Keyset, not offset: an auditor paging a year of events must
            # never see a row twice or skip one because a new row landed.
            t, i = before
            stmt = stmt.where(
                or_(
                    AuditEvent.occurred_at < t,
                    and_(AuditEvent.occurred_at == t, AuditEvent.id < i),
                )
            )
        stmt = stmt.order_by(
            AuditEvent.occurred_at.desc(), AuditEvent.id.desc()
        ).limit(limit)
        with tenant_session(tenant_id, self.url) as session:
            return list(session.scalars(stmt).all())
```

Add `and_, or_` to the `sqlalchemy` import and `datetime` to the typing imports if absent. SQLite stores tz-aware datetimes naive: compare with naive UTC in the test if the assertion fails on SQLite only (treat naive as UTC, per the repo's convention). Re-run Step 1 — Expected: PASS.

- [ ] **Step 3: Failing API tests.** In `tests/test_system_api.py::TestAudit` update the exact-shape test and add:

```python
    def test_an_empty_trail_is_not_an_error(self, env):
        assert env["client"]().get("/api/audit").json() == {
            "events": [],
            "count": 0,
            "next_cursor": None,
        }

    def test_events_carry_the_request_id_and_the_ip_keys(self, env):
        doc_id = add_document(env, sha="au5")
        env["client"]("approver").post(f"/api/documents/{doc_id}/retry")
        event = env["client"]().get("/api/audit").json()["events"][0]
        assert event["request_id"]
        assert set(event) >= {"id", "actor_type", "request_id", "ip"}

    def test_a_window_excludes_what_is_outside_it(self, env):
        doc_id = add_document(env, sha="au6")
        env["client"]("approver").post(f"/api/documents/{doc_id}/retry")
        past = "2000-01-01T00:00:00Z"
        assert env["client"]().get(f"/api/audit?until={past}").json()["count"] == 0
        assert env["client"]().get(f"/api/audit?since={past}").json()["count"] >= 1
        assert env["client"]().get(f"/api/audit?since=2099-01-01T00:00:00Z&until={past}").json()["count"] == 0

    def test_a_malformed_cursor_is_refused(self, env):
        assert env["client"]().get("/api/audit?cursor=nonsense").status_code == 422
```

Run: `./.venv/bin/python -m pytest tests/test_system_api.py -q -p no:cacheprovider -k TestAudit` — Expected: the four FAIL.

- [ ] **Step 4: Implement in `system.py`.**

```python
def _parse_cursor(cursor: str) -> tuple[datetime, str]:
    stamp, sep, event_id = cursor.rpartition("|")
    try:
        if not sep or not event_id:
            raise ValueError
        return datetime.fromisoformat(stamp), event_id
    except ValueError:
        raise HTTPException(
            status_code=422, detail="cursor must be the next_cursor of a previous page."
        ) from None


def _audit_dict(e) -> dict:
    """One shape for the list, the export and the local fallback."""
    return {
        "id": e.id,
        "at": e.occurred_at.isoformat() if e.occurred_at else None,
        "actor": e.actor,
        "actor_type": e.actor_type,
        "action": e.action,
        "entity_type": e.entity_type,
        "entity_id": e.entity_id,
        "payload": e.after,
        "before": e.before,
        "request_id": e.request_id,
        "ip": e.ip,
    }


@router.get("/api/audit")
def list_audit(
    principal: Annotated[Principal, Depends(current_principal)],
    limit: int = 100,
    since: datetime | None = None,
    until: datetime | None = None,
    cursor: str | None = None,
) -> dict:
    """Tenant-scoped activity trail. ... (keep the existing docstring)"""
    from . import service  # noqa: PLC0415

    before = _parse_cursor(cursor) if cursor else None
    sink = service.get_audit()
    limit = max(1, min(limit, 500))

    rows = _safe(
        lambda: sink.query(principal.tenant_id, limit=limit, since=since, until=until, before=before),
        default=None,
    )
    if rows is None:
        events = _read_jsonl_audit(principal.tenant_id, limit, since, until)
    else:
        events = [_audit_dict(e) for e in rows]
    next_cursor = (
        f"{events[-1]['at']}|{events[-1]['id']}"
        if rows is not None and len(events) == limit
        else None
    )
    return {"events": events, "count": len(events), "next_cursor": next_cursor}
```

`_read_jsonl_audit(tenant_id, limit, since=None, until=None)`: parse `record["timestamp"]` with `datetime.fromisoformat`; skip the record when `since`/`until` exclude it (naive timestamps are UTC: `.replace(tzinfo=timezone.utc)` when `tzinfo is None`); each dict gains `"id": None, "actor_type": "user" if record.get("actor") else "system", "request_id": record.get("run_id") or None, "ip": None`. The JSONL path pages by slice only (`# ponytail: the local file is one developer's trail`). Import `datetime, timezone` at the top of `system.py`. Query parameters named `since`/`until` arrive as ISO strings; FastAPI parses them into aware datetimes when they carry `Z` or an offset.

Re-run Step 3 — Expected: PASS. Then `./.venv/bin/python -m pytest tests/test_system_api.py tests/test_tenant_isolation_api.py -q -p no:cacheprovider` — Expected: pass.

- [ ] **Step 5: Lint and commit.** `ruff format invoice_processing/api/system.py invoice_processing/db/repositories.py tests/test_system_api.py tests/test_db_repositories.py`, `ruff check .` clean.

```bash
git add invoice_processing/db/repositories.py invoice_processing/api/system.py tests/test_system_api.py tests/test_db_repositories.py
git commit -m "Let the audit trail be read by date range and cursor, with every stored field

Co-Authored-By: Claude <model> <noreply@anthropic.com>"
```

---

### Task 3: CSV export of the audit trail

**Files:**
- Modify: `invoice_processing/api/system.py` (new `export_audit` after `list_audit`)
- Test: `tests/test_system_api.py`, `tests/test_tenant_isolation_api.py` (`PROBED` ~1278, a probe near `test_audit_does_not_cross_tenants` ~243)

**Interfaces:**
- Consumes: `PgAuditSink.query(..., before=)`, `_audit_dict`, `_read_jsonl_audit` from Task 2; `service.get_audit().emit`.
- Produces: `GET /api/audit/export?since=&until=` → `text/csv` stream.

- [ ] **Step 1: Failing tests.** In `tests/test_system_api.py::TestAudit`:

```python
    def test_an_admin_can_export_a_window_as_csv(self, env):
        doc_id = add_document(env, sha="au7")
        env["client"]("approver").post(f"/api/documents/{doc_id}/retry")
        r = env["client"]("admin").get(
            "/api/audit/export?since=2000-01-01T00:00:00Z&until=2099-01-01T00:00:00Z"
        )
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        assert r.headers["content-disposition"].startswith('attachment; filename="audit-')
        lines = r.text.splitlines()
        assert lines[0] == "id,occurred_at,actor,actor_type,action,entity_type,entity_id,request_id,ip,before,after"
        assert any("document_retried" in line for line in lines[1:])
        actions = [e["action"] for e in env["client"]().get("/api/audit").json()["events"]]
        assert "audit_exported" in actions

    def test_an_empty_window_still_has_a_header(self, env):
        r = env["client"]("admin").get(
            "/api/audit/export?since=2000-01-01T00:00:00Z&until=2000-01-02T00:00:00Z"
        )
        assert r.status_code == 200
        assert r.text.splitlines() == [
            "id,occurred_at,actor,actor_type,action,entity_type,entity_id,request_id,ip,before,after"
        ]

    def test_only_an_admin_can_export(self, env):
        for role in ("viewer", "clerk", "approver"):
            r = env["client"](role).get(
                "/api/audit/export?since=2000-01-01T00:00:00Z&until=2099-01-01T00:00:00Z"
            )
            assert r.status_code == 403, role
```

In `tests/test_tenant_isolation_api.py` add `"/api/audit/export",` to `PROBED` and, after `test_audit_does_not_cross_tenants`:

```python
    def test_audit_export_does_not_cross_tenants(self, clients):
        acme, globex = clients("acme"), clients("globex")
        acme.post("/api/invoices/case_002/actions", json={"action": "approve"})
        r = globex.get(
            "/api/audit/export?since=2000-01-01T00:00:00Z&until=2099-01-01T00:00:00Z"
        )
        assert r.status_code == 200
        assert len(r.text.splitlines()) == 1  # header only
```

Run both files with `-k "export"` — Expected: 404s / coverage failure (`/api/audit/export` not probed) → FAIL.

- [ ] **Step 2: Implement.**

```python
_CSV_COLUMNS = (
    "id", "occurred_at", "actor", "actor_type", "action", "entity_type",
    "entity_id", "request_id", "ip", "before", "after",
)


@router.get("/api/audit/export")
def export_audit(
    principal: Annotated[Principal, Depends(require_role("admin"))],
    since: datetime,
    until: datetime,
):
    """The trail for a window, as the file an auditor asks for.

    The export is itself an event, written before the first byte goes out:
    a dropped connection must still leave the record that someone pulled it.
    """
    import csv  # noqa: PLC0415
    import io  # noqa: PLC0415

    from fastapi.responses import StreamingResponse  # noqa: PLC0415

    from . import service  # noqa: PLC0415

    sink = service.get_audit()
    sink.emit(
        principal.context(),
        "audit_exported",
        {"since": since.isoformat(), "until": until.isoformat()},
    )

    def rows():
        before = None
        while True:
            page = _safe(
                lambda: sink.query(principal.tenant_id, limit=1000, since=since, until=until, before=before),
                default=None,
            )
            if page is None:  # local JSONL sink: one slice, no paging
                yield from _read_jsonl_audit(principal.tenant_id, 100_000, since, until)
                return
            for e in page:
                yield _audit_dict(e)
            if len(page) < 1000:
                return
            before = (page[-1].occurred_at, page[-1].id)

    def stream():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(_CSV_COLUMNS)
        yield buffer.getvalue()
        for event in rows():
            buffer.seek(0)
            buffer.truncate()
            writer.writerow(
                [
                    event["id"], event["at"], event["actor"], event["actor_type"],
                    event["action"], event["entity_type"], event["entity_id"],
                    event["request_id"], event["ip"],
                    json.dumps(event["before"], default=str) if event["before"] is not None else "",
                    json.dumps(event["payload"], default=str) if event["payload"] is not None else "",
                ]
            )
            yield buffer.getvalue()

    name = f"audit-{since.date().isoformat()}-{until.date().isoformat()}.csv"
    return StreamingResponse(
        stream(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
```

Re-run Step 1 — Expected: PASS. Run `tests/test_tenant_isolation_api.py` whole — Expected: pass (`test_every_route_is_covered` now satisfied).

- [ ] **Step 3: Lint, full suite, commit.** Full suite — Expected: all pass, 3 skipped. `ruff` clean.

```bash
git add invoice_processing/api/system.py tests/test_system_api.py tests/test_tenant_isolation_api.py
git commit -m "Export the audit trail for a date range as CSV, admin only and audited

Co-Authored-By: Claude <model> <noreply@anthropic.com>"
```

---

### Task 4: Activity page — date range and Export CSV

**Files:**
- Modify: `frontend/src/lib/api.ts:218-228` (`AuditEvent`), new `downloadFile` after `objectUrl` (~591)
- Modify: `frontend/src/app/activity/page.tsx`
- Test: `frontend/e2e/golden-path.spec.ts`

**Interfaces:**
- Consumes: `GET /api/audit?since=&until=&limit=`; `GET /api/audit/export?since=&until=`; `usePoll<T>(path, intervalMs)`; `Me` from `/api/me`.
- Produces: `downloadFile(path: string, filename: string): Promise<void>`.

- [ ] **Step 1: Write the e2e test** (append to `golden-path.spec.ts`; the dev principal is admin):

```ts
test("an admin can narrow the activity trail and export it", async ({ page }) => {
  await goto(page, "/activity");
  await page.getByLabel("Since").fill("2000-01-01T00:00");
  await expect(page.getByRole("button", { name: "Export CSV" })).toBeVisible();
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("button", { name: "Export CSV" }).click(),
  ]);
  expect(download.suggestedFilename()).toMatch(/^audit-.*\.csv$/);
});
```

Run: `cd frontend && npm run test:e2e -- -g "narrow the activity trail"` — Expected: FAIL (no "Since" field).

- [ ] **Step 2: `api.ts`.** Extend the type:

```ts
export type AuditEvent = {
  id: string | null;
  at: string | null;
  actor: string | null;
  actor_type: string | null;
  action: string | null;
  entity_type: string | null;
  entity_id: string | null;
  payload: Record<string, unknown> | null;
  before: Record<string, unknown> | null;
  request_id: string | null;
  ip: string | null;
};
```

Add after `objectUrl`:

```ts
/** Fetch a file with the bearer token and hand it to the browser as a
 *  download. A plain <a href> cannot carry the Authorization header. */
export async function downloadFile(path: string, filename: string): Promise<void> {
  const url = await objectUrl(path);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}
```

- [ ] **Step 3: `activity/page.tsx`.** Add state and the query:

```tsx
const [since, setSince] = useState("");
const [until, setUntil] = useState("");
const { data: me } = usePoll<Me>("/api/me");

const params = new URLSearchParams({ limit: String(limit) });
if (since) params.set("since", new Date(since).toISOString());
if (until) params.set("until", new Date(until).toISOString());
const { data, error } = usePoll<{ events: AuditEvent[]; count: number }>(
  `/api/audit?${params}`,
  POLL_MS,
);
```

In the filter row add, before the limit select:

```tsx
<input type="datetime-local" className="input-field" style={{ width: "auto" }}
  aria-label="Since" value={since} onChange={(e) => setSince(e.target.value)} />
<input type="datetime-local" className="input-field" style={{ width: "auto" }}
  aria-label="Until" value={until} onChange={(e) => setUntil(e.target.value)} />
{me?.role === "admin" ? (
  <button type="button" className="btn-secondary" onClick={exportCsv}>
    Export CSV
  </button>
) : null}
```

with

```tsx
const exportCsv = async () => {
  // The server requires both bounds; default to "everything so far".
  const from = since ? new Date(since) : new Date(0);
  const to = until ? new Date(until) : new Date();
  const q = new URLSearchParams({ since: from.toISOString(), until: to.toISOString() });
  const name = `audit-${from.toISOString().slice(0, 10)}-${to.toISOString().slice(0, 10)}.csv`;
  try {
    await downloadFile(`/api/audit/export?${q}`, name);
  } catch (caught) {
    setProblem(caught instanceof ApiError ? caught.friendly : String(caught));
  }
};
```

and a `problem` state rendered as `<p role="alert" style={{ color: "var(--error)", fontSize: 12.5 }}>{problem}</p>` under the filter row. Import `Me`, `ApiError`, `downloadFile` from `@/lib/api`.

- [ ] **Step 4: Verify.** `cd frontend && npm run lint && npx tsc --noEmit && npm run test:e2e` — Expected: lint and tsc clean; e2e 36 passed (35 + 1).

- [ ] **Step 5: Commit.**

```bash
git add frontend/src/lib/api.ts frontend/src/app/activity/page.tsx frontend/e2e/golden-path.spec.ts
git commit -m "Let an admin narrow the activity trail by date and export it as CSV

Co-Authored-By: Claude <model> <noreply@anthropic.com>"
```

---

### Task 5: `python -m invoice_processing.db.migrate` for the owner's release step

**Files:**
- Modify: `invoice_processing/db/migrate.py` (new `main`, `__main__` guard; module docstring's last paragraph)
- Modify: `.env.example:46-63`, `deployment/README.md` (new "Release" section at the top)
- Test: `tests/test_gcs_and_migrations.py::TestMigrationsRunAtStartup`

**Interfaces:**
- Produces: `migrate.main(argv=None) -> int`.

- [ ] **Step 1: Failing tests.** Add to `TestMigrationsRunAtStartup`:

```python
    def test_the_release_command_migrates_as_the_owner(self, tmp_path, monkeypatch, capsys):
        """The app role cannot run DDL, so a release step runs it as the
        owner. MIGRATE_DATABASE_URL names that connection."""
        owner = f"sqlite:///{tmp_path / 'owner.db'}"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'app.db'}")
        monkeypatch.setenv("MIGRATE_DATABASE_URL", owner)
        monkeypatch.setenv("RUN_MIGRATIONS", "0")
        monkeypatch.setattr(session, "_engine", None)

        assert migrate.main() == 0

        assert os.environ["DATABASE_URL"] == owner
        assert migrate.current_revision() is not None
        assert migrate.current_revision() in capsys.readouterr().out

    def test_the_release_command_fails_loudly(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MIGRATE_DATABASE_URL", "postgresql+pg8000://nobody:x@127.0.0.1:1/none")
        monkeypatch.setattr(session, "_engine", None)
        assert migrate.main() == 1
```

Run: `./.venv/bin/python -m pytest tests/test_gcs_and_migrations.py -q -p no:cacheprovider -k release_command` — Expected: FAIL (`no attribute 'main'`).

- [ ] **Step 2: Implement.**

```python
def main(argv: list[str] | None = None) -> int:
    """`python -m invoice_processing.db.migrate`: the release step.

    The API and worker connect as a role that owns nothing and cannot run
    DDL (see db/bootstrap.sql). Migrations run here, as the owner, before a
    deploy serves traffic. MIGRATE_* name the owner's connection so the
    app's own settings stay the least-privilege ones.
    """
    for source, target in (
        ("MIGRATE_DATABASE_URL", "DATABASE_URL"),
        ("MIGRATE_CLOUD_SQL_USER", "CLOUD_SQL_USER"),
    ):
        if os.getenv(source):
            os.environ[target] = os.environ[source]
    os.environ["RUN_MIGRATIONS"] = "1"
    logging.basicConfig(level=logging.INFO)
    try:
        print(upgrade_to_head())
    except Exception:
        logger.exception("Migration failed; nothing is serving a schema it could not reach.")
        return 1
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
```

Replace the docstring's last paragraph ("Set `RUN_MIGRATIONS=0` ...") with: "Deployed environments set `RUN_MIGRATIONS=0` and run `python -m invoice_processing.db.migrate` as the owner role in a release step (Railway: the pre-deploy command); the app's connection has no DDL rights."

Re-run Step 1 — Expected: PASS. Check `tests/test_gcs_and_migrations.py` whole still passes (the fresh `session._engine` reset matters: `get_engine` caches by URL, so the test's `monkeypatch.setattr(session, "_engine", None)` is what makes `main` pick up the new URL).

- [ ] **Step 3: Docs.** `.env.example`, after the Cloud SQL block:

```
# --- Release step (migrations) --------------------------------------------
# The app connects as a role with no DDL rights (db/bootstrap.sql). Deployed
# environments set RUN_MIGRATIONS=0 and run, as the owner role, before each
# deploy (Railway: pre-deploy command):
#   python -m invoice_processing.db.migrate
# MIGRATE_DATABASE_URL=postgresql+pg8000://ledgerly_owner:...@host/db
# MIGRATE_CLOUD_SQL_USER=sa-migrate@project.iam
# RUN_MIGRATIONS=0
```

`deployment/README.md`: insert at the top a "## Release (Railway + Postgres)" section with the four steps: (1) `psql ... -f invoice_processing/db/bootstrap.sql` §1 as `postgres` with `-v owner=... -v app=... -v owner_pw=... -v app_pw=...`; (2) `MIGRATE_DATABASE_URL=... python -m invoice_processing.db.migrate`; (3) bootstrap §2; (4) the VERIFY query; and the Railway variables (`DATABASE_URL` = app role, `MIGRATE_DATABASE_URL` = owner role, `RUN_MIGRATIONS=0`, pre-deploy command). Keep the existing Agent Runtime content below it.

- [ ] **Step 4: Lint, commit.**

```bash
git add invoice_processing/db/migrate.py tests/test_gcs_and_migrations.py .env.example deployment/README.md
git commit -m "Give the release step a migrate command that runs as the owner role

Co-Authored-By: Claude <model> <noreply@anthropic.com>"
```

---

### Task 6: Two-role bootstrap, proven on Postgres

**Files:**
- Rewrite: `invoice_processing/db/bootstrap.sql`
- Modify: `.github/workflows/ci.yml:179` (append the new node id)
- Test: `tests/test_gcs_and_migrations.py::TestMigration` (new test after `test_sso_lookup_sentinel_reads_domains_and_nothing_else`, ~line 800)

**Interfaces:**
- Consumes: the pattern at `tests/test_gcs_and_migrations.py:676-805` (scratch database, `CREATE ROLE ... NOBYPASSRLS`, `alembic upgrade head` subprocess with `DATABASE_URL`, `DBAPIError`, teardown `DROP DATABASE ... WITH (FORCE)` + `DROP ROLE`).
- Produces: `bootstrap.sql` with markers `-- §1` and `-- §2` so the test can split it.

- [ ] **Step 1: Write the failing Postgres test.**

```python
    @pytest.mark.skipif(
        not (os.getenv("TEST_DATABASE_URL") or "").startswith("postgresql"),
        reason="roles and grants exist only on Postgres",
    )
    def test_app_role_cannot_rewrite_the_audit_trail(self):
        """The API runs as a role that owns nothing. Proven as that role:
        it can append to and read the trail, and nothing else about it."""
        import uuid  # noqa: PLC0415

        from sqlalchemy import create_engine, text  # noqa: PLC0415
        from sqlalchemy.engine import make_url  # noqa: PLC0415
        from sqlalchemy.exc import DBAPIError  # noqa: PLC0415

        server = make_url(os.environ["TEST_DATABASE_URL"])
        suffix = uuid.uuid4().hex[:12]
        name = f"roles_{suffix}"
        owner, app_role = f"owner_{suffix}", f"app_{suffix}"
        tenant = "22222222-2222-2222-2222-222222222222"
        sql = (REPO / "invoice_processing" / "db" / "bootstrap.sql").read_text()
        for var, value in (
            (':"owner"', owner), (':"app"', app_role),
            (":'owner_pw'", "'pw'"), (":'app_pw'", "'pw'"),
        ):
            sql = sql.replace(var, value)
        part1, part2 = sql.split("-- §2")
        statements = lambda s: [x for x in s.split(";") if x.strip() and not x.strip().startswith("--") and "has_table_privilege" not in x]  # noqa: E731

        admin = create_engine(server, isolation_level="AUTOCOMMIT")
        url = server.set(database=name)
        engines = []
        try:
            with admin.connect() as c:
                c.execute(text(f"CREATE DATABASE {name}"))
            su = create_engine(url, isolation_level="AUTOCOMMIT")
            engines.append(su)
            with su.connect() as c:
                for s in statements(part1):
                    c.execute(text(s))
            up = subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=REPO, capture_output=True, text=True, timeout=180, check=False,
                env={**os.environ, "DATABASE_URL": url.set(username=owner, password="pw").render_as_string(hide_password=False)},
            )
            assert up.returncode == 0, up.stderr[-2000:]
            with su.connect() as c:
                for s in statements(part2):
                    c.execute(text(s))
                c.execute(text(
                    "insert into tenants (id, slug, name, region, status, settings, created_at, updated_at) "
                    f"values ('{tenant}', 'acme', 'Acme', 'au', 'active', '{{}}', now(), now())"
                ))
                assert c.execute(text(f"select has_table_privilege('{app_role}', 'audit_events', 'UPDATE')")).scalar() is False
                assert c.execute(text(f"select has_table_privilege('{app_role}', 'audit_events', 'INSERT')")).scalar() is True

            app = create_engine(url.set(username=app_role, password="pw"))
            engines.append(app)
            scope = text("SELECT set_config('app.tenant_id', :t, true)")
            with app.connect() as c:
                with c.begin():
                    c.execute(scope, {"t": tenant})
                    c.execute(text(
                        "insert into audit_events (id, tenant_id, occurred_at, actor_type, action) "
                        f"values ('e1', '{tenant}', now(), 'user', 'x')"
                    ))
                    assert c.execute(text("select count(*) from audit_events")).scalar() == 1
                    assert c.execute(text("update tenants set name = 'Acme Ltd'")).rowcount == 1
                for forbidden in (
                    "update audit_events set action = 'y'",
                    "delete from audit_events",
                    "truncate audit_events",
                    "alter table audit_events disable row level security",
                    "alter table audit_events no force row level security",
                    "drop table audit_events",
                    "alter table users add column evil int",
                    "create table evil (id int)",
                ):
                    with pytest.raises(DBAPIError), c.begin():
                        c.execute(scope, {"t": tenant})
                        c.execute(text(forbidden))
        finally:
            for engine in engines:
                engine.dispose()
            with admin.connect() as c:
                c.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
                c.execute(text(f"DROP ROLE IF EXISTS {app_role}"))
                c.execute(text(f"DROP ROLE IF EXISTS {owner}"))
            admin.dispose()
```

Start Postgres and run it:

```bash
docker run -d --rm --name pg-x -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=t -p 55432:5432 postgres:16
```

Wait for `docker exec pg-x pg_isready -U postgres`, then
`TEST_DATABASE_URL=postgresql+pg8000://postgres:postgres@localhost:55432/t ./.venv/bin/python -m pytest tests/test_gcs_and_migrations.py -q -p no:cacheprovider -k app_role_cannot` — Expected: FAIL (the current bootstrap has no `§2` marker / no `:"owner"`).

- [ ] **Step 2: Rewrite `bootstrap.sql`.**

```sql
-- One-time Postgres bootstrap: two roles.
--
-- The application connects as :"app", which owns nothing, cannot run DDL,
-- and can only append to and read the audit trail. Migrations run as
-- :"owner" in a release step (`python -m invoice_processing.db.migrate`).
-- An owner can TRUNCATE, ALTER or DROP any table it owns, policies included,
-- so the role serving traffic must not be the owner: that is the whole point.
--
-- Run as the built-in `postgres` user, connected to the application database.
-- Plain SQL with psql variables. Plain Postgres (Railway, Docker):
--   psql -v owner=ledgerly_owner -v app=ledgerly_app \
--        -v owner_pw='...' -v app_pw='...' -f bootstrap.sql
-- Cloud SQL IAM: create both users first with
--   gcloud sql users create <sa>@<project>.iam --type=CLOUD_IAM_SERVICE_ACCOUNT
-- delete the two CREATE ROLE lines, and pass the IAM names as owner and app.
--
-- Order: §1, then migrate as :"owner", then §2 (safe to re-run any time).
-- §2 must run after the first migrate because default privileges apply to
-- tables created later, and audit_events already exists by then.

-- §1 -- before the first migrate ------------------------------------------

CREATE ROLE :"owner" LOGIN PASSWORD :'owner_pw'
    NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB;
CREATE ROLE :"app" LOGIN PASSWORD :'app_pw'
    NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO :"owner";
GRANT USAGE ON SCHEMA public TO :"app";

-- Every table a future migration creates is granted to the app automatically.
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"app";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO :"app";

-- §2 -- after migrate; idempotent ------------------------------------------

-- A database migrated under the old single IAM user: hand its tables to the
-- owner first, or later migrations cannot alter them. Uncomment to use.
-- DO $$ DECLARE t text; BEGIN
--   FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' LOOP
--     EXECUTE format('ALTER TABLE %I OWNER TO %I', t, :'owner');
--   END LOOP;
-- END $$;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO :"app";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO :"app";

-- Last, so no grant above can hand it back: the trail is append-only.
REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
    ON audit_events FROM :"app";

-- VERIFY -- expected: false, true, then false/false for both roles.
SELECT has_table_privilege(:'app', 'audit_events', 'UPDATE') AS app_can_update_audit,
       has_table_privilege(:'app', 'audit_events', 'INSERT') AS app_can_insert_audit;
SELECT rolname, rolbypassrls, rolsuper FROM pg_roles
 WHERE rolname IN (:'owner', :'app');
```

Note for the test: the `DO $$ ... $$` block is commented, so splitting on `;` is safe; the VERIFY `SELECT ... has_table_privilege` statements are skipped by the test's filter and the second VERIFY query runs harmlessly. If the split trips on the `-- VERIFY` comment lines, strip comment-only lines before splitting.

Re-run Step 1 — Expected: PASS.

- [ ] **Step 3: CI list.** In `.github/workflows/ci.yml` after line 179's `...test_sso_lookup_sentinel_reads_domains_and_nothing_else"` add a continuation line:

```
                        "tests/test_gcs_and_migrations.py::TestMigration::test_app_role_cannot_rewrite_the_audit_trail" -q
```

(moving the `-q` to the new last line).

- [ ] **Step 4: Full Postgres list locally and round trip**, with the container still up (zsh):

```bash
files=(${(f)"$(awk '/DB-backed tests against Postgres/{f=1} f&&/tests\//{for(i=1;i<=NF;i++) if($i ~ /tests\//){gsub(/[\\"]/,"",$i); print $i}} f&&/-q$/{exit}' .github/workflows/ci.yml)"})
TEST_DATABASE_URL=postgresql+pg8000://postgres:postgres@localhost:55432/t ./.venv/bin/python -m pytest "${files[@]}" -q -p no:cacheprovider
DATABASE_URL=postgresql+pg8000://postgres:postgres@localhost:55432/t ./.venv/bin/python -m alembic upgrade head && DATABASE_URL=postgresql+pg8000://postgres:postgres@localhost:55432/t ./.venv/bin/python -m alembic downgrade base
docker stop pg-x
```

Expected: all pass; round trip exits 0.

- [ ] **Step 5: Commit.**

```bash
git add invoice_processing/db/bootstrap.sql tests/test_gcs_and_migrations.py .github/workflows/ci.yml
git commit -m "Serve as a role that cannot rewrite the audit trail; migrate as the owner

Co-Authored-By: Claude <model> <noreply@anthropic.com>"
```

---

### Finishing checks (controller)

- Full suite, ruff, `bash benchmark/harness/gates.sh` (9 gates), frontend lint/tsc/e2e (36).
- Pre-feature proof: in a scratch worktree of `main` (`72c6a9b`), copy in `tests/test_auth.py`, `tests/test_sso.py`, `tests/test_system_api.py`, `tests/test_db_repositories.py`, `tests/test_tenant_isolation_api.py`, `tests/test_gcs_and_migrations.py` and run them: the new tests fail. `git worktree remove --force` + `git worktree prune` afterwards.
- Browser: temporary `check-frontend-3200` launch config → `/activity`: Since narrows the list; Export CSV downloads `audit-….csv` with the header; remove the config.
- Manual stack (`manual-a-api-worker`, dev bypass): `curl -H 'X-Dev-Principal: clerk@bench.test:clerk' -X POST localhost:8000/api/invoices/<case>/actions -d '{"action":"approve"}'` → 403, and `access_denied` shows on `/activity`.
