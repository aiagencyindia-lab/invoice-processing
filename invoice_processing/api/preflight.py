"""Startup configuration check.

Three settings fail *open and silent* if they are dropped from a deployment,
and each produces a system that reports itself healthy while quietly not doing
its job:

* `GCS_BUCKET` unset -- documents are written to container-local disk and
  vanish on the next restart. Nothing logs; the upload returns 200.
* Neither `DATABASE_URL` nor `CLOUD_SQL_INSTANCE` -- the engine falls back to
  in-memory SQLite. Row-level security does not exist there, so tenant
  isolation rests entirely on the repositories remembering to filter, and the
  audit trail silently becomes a local file.
* `GOOGLE_CLOUD_PROJECT` unset -- token verification has no audience to check.
  That one already fails closed, and is included so a deploy learns about all
  three at once rather than one redeploy at a time.

Silence is the problem in each case. A misconfigured deployment that refuses to
start is an outage someone fixes in minutes; one that serves happily and loses
documents is discovered weeks later by a customer.

The check is deliberately scoped by environment: local development legitimately
runs with none of this, so it only *warns* there and only *fails* where the
deployment claims to be real.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Environments where missing infrastructure is a deployment fault, not a
# developer running the app on a laptop.
DEPLOYED_ENVIRONMENTS = {"production", "prod", "staging", "stage", "uat"}


class ConfigurationError(RuntimeError):
    """The process must not serve traffic in this configuration."""


def is_deployed(env: str | None = None) -> bool:
    return (env if env is not None else os.getenv("ENV", "")).lower() in (
        DEPLOYED_ENVIRONMENTS
    )


def check_configuration(env: str | None = None) -> list[str]:
    """Return one message per setting that would fail silently. Empty is good."""
    from ..gcp_credentials import materialise_credentials  # noqa: PLC0415

    # Same reason as the worker: the bridge cannot live only in the
    # image's entrypoint, because a start command replaces it.
    materialise_credentials()

    problems = []

    if not os.getenv("GCS_BUCKET"):
        problems.append(
            "GCS_BUCKET is not set: documents will be written to local disk and "
            "lost when the container restarts."
        )

    if not (os.getenv("DATABASE_URL") or os.getenv("CLOUD_SQL_INSTANCE")):
        problems.append(
            "Neither DATABASE_URL nor CLOUD_SQL_INSTANCE is set: the app will "
            "use in-memory SQLite, which has no row-level security, and the "
            "audit trail will be a local file."
        )

    if not os.getenv("GOOGLE_CLOUD_PROJECT"):
        problems.append(
            "GOOGLE_CLOUD_PROJECT is not set: token verification has no "
            "audience to check against."
        )

    if os.getenv("AUTH_DISABLED", "").lower() in ("1", "true", "yes"):
        problems.append(
            "AUTH_DISABLED is set: every request would run as an "
            "unauthenticated admin."
        )

    return problems


def enforce(env: str | None = None) -> None:
    """Refuse to serve when a deployed environment is misconfigured.

    Warns rather than raises outside a deployed environment, so local
    development and the test suite are unaffected.
    """
    problems = check_configuration(env)
    if not problems:
        return

    if is_deployed(env):
        joined = "\n  - ".join(problems)
        raise ConfigurationError(
            f"Refusing to start in ENV={os.getenv('ENV')!r}:\n  - {joined}"
        )

    for problem in problems:
        logger.warning("Configuration: %s", problem)
