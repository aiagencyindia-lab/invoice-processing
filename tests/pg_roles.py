"""A throwaway Postgres database built the way production is.

bootstrap.sql §1 as a superuser, migrations as the owner role, §2 again as
the superuser -- the order deployment/README.md gives -- with two fresh
NOSUPERUSER NOBYPASSRLS roles. CI's own user is a superuser and bypasses
RLS, so a claim about policies has to be proven as these roles.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

REPO = Path(__file__).resolve().parent.parent

pg_only = pytest.mark.skipif(
    not (os.getenv("TEST_DATABASE_URL") or "").startswith("postgresql"),
    reason="roles, grants and policies exist only on Postgres",
)


def _bootstrap_parts(owner: str, app: str) -> tuple[list[str], list[str]]:
    sql = (REPO / "invoice_processing" / "db" / "bootstrap.sql").read_text()
    # psql variables, literals before identifiers.
    for var, value in (
        (":'owner_pw'", "'pw'"),
        (":'app_pw'", "'pw'"),
        (":'owner'", f"'{owner}'"),
        (":'app'", f"'{app}'"),
        (':"owner"', owner),
        (':"app"', app),
    ):
        sql = sql.replace(var, value)
    part1, part2 = sql.split("\n-- §2")  # the marker line, as sed finds it

    def statements(part):
        code = "\n".join(
            line
            for line in part.splitlines()
            if not line.strip().startswith("--")
        )
        return [
            s
            for s in code.split(";")
            if s.strip() and "has_table_privilege" not in s
        ]

    return statements(part1), statements(part2)


@contextmanager
def bootstrapped_database():
    server = make_url(os.environ["TEST_DATABASE_URL"])
    suffix = uuid.uuid4().hex[:12]
    name = f"roles_{suffix}"
    owner, app = f"owner_{suffix}", f"app_{suffix}"
    part1, part2 = _bootstrap_parts(owner, app)
    admin = create_engine(server, isolation_level="AUTOCOMMIT")
    url = server.set(database=name)
    owner_url = url.set(username=owner, password="pw")

    def alembic(command: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "alembic", *command.split()],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
            env={
                **os.environ,
                "DATABASE_URL": owner_url.render_as_string(hide_password=False),
            },
        )

    try:
        with admin.connect() as c:
            c.execute(text(f"CREATE DATABASE {name}"))
        su = create_engine(url, isolation_level="AUTOCOMMIT")
        try:
            with su.connect() as c:
                for s in part1:
                    c.execute(text(s))
            up = alembic("upgrade head")
            assert up.returncode == 0, up.stderr[-2000:]
            with su.connect() as c:
                for s in part2:
                    c.execute(text(s))
        finally:
            su.dispose()
        yield SimpleNamespace(
            url=url,
            owner_url=owner_url,
            app_url=url.set(username=app, password="pw"),
            owner=owner,
            app=app,
            alembic=alembic,
        )
    finally:
        with admin.connect() as c:
            c.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
            c.execute(text(f"DROP ROLE IF EXISTS {app}"))
            c.execute(text(f"DROP ROLE IF EXISTS {owner}"))
        admin.dispose()
