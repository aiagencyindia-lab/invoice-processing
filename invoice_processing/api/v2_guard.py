"""Rate limits and idempotency keys for the public /v2 API.

One HTTP middleware for paths under `/v2`, kept in the database so both hold
across containers and restarts:

* **Rate limit:** every request from an authenticated caller (an API key or a
  user, by `Principal.user_id`) counts in a one-minute window. Over the
  tenant's `v2.rate_limit_per_minute` (default 120) it answers 429 with
  `Retry-After`; every answer carries the `X-RateLimit-*` headers.
* **Idempotency:** a POST sent with `Idempotency-Key` runs once. A retry with
  the same key and body gets the first answer back (`Idempotent-Replay:
  true`); the same key with another body is 422; a retry while the first is
  still running is 409. Kept 24 hours; a non-2xx answer frees the key.

An unauthenticated request passes straight to the route, which answers 401.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, select
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response

from ..db.models import ApiIdempotency, ApiRateWindow
from ..db.session import tenant_session
from . import auth

logger = logging.getLogger(__name__)

PREFIX = "/v2"
KEY_HEADER = "idempotency-key"
_KEY = re.compile(r"[\x21-\x7e]{1,128}")
KEEP = timedelta(hours=24)
MAX_STORED = 256 * 1024
WINDOW = 60


def _now() -> datetime:
    return datetime.now(UTC)


# ponytail: a per-process minute cache; settings change rarely, and a stale
# limit for up to a minute is harmless.
_LIMITS: dict[str, tuple[float, int]] = {}
LIMIT_TTL = 60.0


def limit_for(tenant_id: str) -> int:
    """The tenant's per-caller budget per minute (trust thresholds)."""
    import time  # noqa: PLC0415

    now = time.monotonic()
    cached = _LIMITS.get(tenant_id)
    if cached and cached[0] > now:
        return cached[1]
    from .. import tenant_settings  # noqa: PLC0415 - avoids an import cycle
    from ..trust import thresholds  # noqa: PLC0415

    merged = thresholds.merged(
        tenant_settings.effective(tenant_id).get("trust_thresholds")
    )
    value = int(merged["v2.rate_limit_per_minute"])
    _LIMITS[tenant_id] = (now + LIMIT_TTL, value)
    return value


def _insert(s, model):
    """An INSERT that understands ON CONFLICT on both dialects we run."""
    if s.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert  # noqa: PLC0415
    else:
        from sqlalchemy.dialects.sqlite import insert  # noqa: PLC0415
    return insert(model)


def _count(tenant_id: str, principal: str, start: datetime) -> int:
    """This request's number in its window: one atomic insert-or-increment,
    so two first requests of a minute both count and neither fails."""
    now = _now()
    with tenant_session(tenant_id) as s:
        stmt = _insert(s, ApiRateWindow).values(
            id=str(uuid4()),
            tenant_id=tenant_id,
            principal=principal,
            window_start=start,
            count=1,
            created_at=now,
            updated_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["tenant_id", "principal", "window_start"],
            set_={"count": ApiRateWindow.count + 1, "updated_at": now},
        ).returning(ApiRateWindow.count)
        return int(s.execute(stmt).scalar_one())


def _claim(tenant_id, principal, key, digest) -> tuple[str, dict | None]:
    """("run", None) when this request owns the key now; ("replay", row),
    ("conflict", None) or ("busy", None) otherwise. The claim is one atomic
    insert-or-nothing, so of two first requests exactly one runs."""
    now = _now()
    where = (
        ApiIdempotency.tenant_id == tenant_id,
        ApiIdempotency.principal == principal,
        ApiIdempotency.key == key,
    )
    with tenant_session(tenant_id) as s:
        s.execute(
            delete(ApiIdempotency).where(
                *where, ApiIdempotency.expires_at <= now
            )
        )
        stmt = (
            _insert(s, ApiIdempotency)
            .values(
                id=str(uuid4()),
                tenant_id=tenant_id,
                principal=principal,
                key=key,
                request_hash=digest,
                status="in_progress",
                expires_at=now + KEEP,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=["tenant_id", "principal", "key"]
            )
            .returning(ApiIdempotency.id)
        )
        if s.execute(stmt).scalar_one_or_none() is not None:
            return "run", None
        row = s.scalars(select(ApiIdempotency).where(*where)).one()
        if row.status != "done":
            return "busy", None  # the honest answer, whatever the body
        if row.request_hash != digest:
            return "conflict", None
        return "replay", {
            "status_code": row.status_code,
            "body": row.body,
            "content_type": row.content_type,
        }


def _finish(tenant_id, principal, key, status_code, body, content_type):
    """Store a 2xx answer for replay; free the key otherwise."""
    where = (
        ApiIdempotency.tenant_id == tenant_id,
        ApiIdempotency.principal == principal,
        ApiIdempotency.key == key,
    )
    with tenant_session(tenant_id) as s:
        parsed = None
        if 200 <= status_code < 300 and len(body) <= MAX_STORED:
            try:
                parsed = json.loads(body) if body else None
                keep = True
            except ValueError:
                keep = False
        else:
            keep = False
        if not keep:
            s.execute(delete(ApiIdempotency).where(*where))
            return
        row = s.scalars(select(ApiIdempotency).where(*where)).first()
        if row is not None:
            row.status = "done"
            row.status_code = status_code
            row.body = parsed
            row.content_type = content_type


def purge_expired(tenant_id: str) -> int:
    """Daily housekeeping (the worker's retention sweep): idempotency keys
    past their 24 hours and rate windows over a day old. Returns rows gone."""
    now = _now()
    with tenant_session(tenant_id) as s:
        keys = s.execute(
            delete(ApiIdempotency).where(
                ApiIdempotency.tenant_id == tenant_id,
                ApiIdempotency.expires_at <= now,
            )
        ).rowcount
        windows = s.execute(
            delete(ApiRateWindow).where(
                ApiRateWindow.tenant_id == tenant_id,
                ApiRateWindow.window_start < now - timedelta(days=1),
            )
        ).rowcount
    return int(keys or 0) + int(windows or 0)


def _resolve(request):
    try:
        return auth._resolve_principal(request)
    except auth.AuthError:
        return None


async def guard(request, call_next):
    if (
        not request.url.path.startswith(PREFIX + "/")
        and request.url.path != PREFIX
    ):
        return await call_next(request)
    principal = await run_in_threadpool(_resolve, request)
    if principal is None or not principal.user_id:
        return await call_next(request)  # the route answers 401
    # Resolved once: the route's own dependency reuses it.
    request.state.principal = principal
    tenant, who = principal.tenant_id, principal.user_id

    now = _now()
    start = now.replace(second=0, microsecond=0)
    reset = max(1, math.ceil(WINDOW - (now - start).total_seconds()))
    limit = await run_in_threadpool(limit_for, tenant)
    used = await run_in_threadpool(_count, tenant, who, start)
    headers = {
        "X-RateLimit-Limit": str(limit),
        "X-RateLimit-Remaining": str(max(0, limit - used)),
        "X-RateLimit-Reset": str(reset),
    }
    if used > limit:
        return JSONResponse(
            {"detail": "Too many requests; slow down and retry."},
            status_code=429,
            headers={**headers, "Retry-After": str(reset)},
        )

    key = request.headers.get(KEY_HEADER)
    if request.method != "POST" or key is None:
        response = await call_next(request)
        response.headers.update(headers)
        return response
    if not _KEY.fullmatch(key):
        return JSONResponse(
            {"detail": "Idempotency-Key must be 1 to 128 visible characters."},
            status_code=400,
            headers=headers,
        )
    digest = hashlib.sha256(await request.body()).hexdigest()
    outcome, stored = await run_in_threadpool(_claim, tenant, who, key, digest)
    if outcome == "conflict":
        return JSONResponse(
            {"detail": "This Idempotency-Key was used with another request."},
            status_code=422,
            headers=headers,
        )
    if outcome == "busy":
        return JSONResponse(
            {"detail": "A request with this Idempotency-Key is still running."},
            status_code=409,
            headers=headers,
        )
    if outcome == "replay":
        return Response(
            content=json.dumps(stored["body"]).encode()
            if stored["body"] is not None
            else b"",
            status_code=stored["status_code"],
            media_type=stored["content_type"] or "application/json",
            headers={**headers, "Idempotent-Replay": "true"},
        )
    try:
        response = await call_next(request)
        body = b"".join([chunk async for chunk in response.body_iterator])
    except BaseException:
        await run_in_threadpool(_finish, tenant, who, key, 500, b"", None)
        raise
    await run_in_threadpool(
        _finish,
        tenant,
        who,
        key,
        response.status_code,
        body,
        response.headers.get("content-type"),
    )
    out = Response(
        content=body,
        status_code=response.status_code,
        background=response.background,
        headers={
            k: v
            for k, v in response.headers.items()
            if k.lower() != "content-length"
        },
    )
    out.headers.update(headers)
    return out
