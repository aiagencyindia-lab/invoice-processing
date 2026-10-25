"""The Railway project as code: `.railway/railway.ts`.

Pulled from the live project (`railway config pull --variables unmanaged`)
and applied only by hand (`railway config apply`). A rename in the repo that
the file still names would only show up as a failed production deploy, so
what it names is checked here.
"""

import importlib.util
import pathlib
import re

from fastapi.testclient import TestClient

from invoice_processing.api import service
from invoice_processing.api.service import app

ROOT = pathlib.Path(__file__).resolve().parents[1]
IAC = (ROOT / ".railway" / "railway.ts").read_text()


def setting(name: str) -> str:
    (value,) = re.findall(rf'\b{name}: "([^"]+)"', IAC)
    return value


def test_the_start_script_exists():
    command = setting("start").split()
    assert command[0] == "sh" and (ROOT / command[1]).is_file()


def test_the_pre_deploy_step_migrates():
    assert setting("preDeploy") == "python -m invoice_processing.db.migrate"
    assert importlib.util.find_spec("invoice_processing.db.migrate")


def test_the_health_check_is_a_public_route(monkeypatch):
    monkeypatch.setattr(service, "_database_configured", lambda: False)
    r = TestClient(app).get(setting("healthcheck"))
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_variables_stay_out_of_code():
    """Secrets live in Railway; the file must never carry or manage them."""
    assert "variables: { managed: false }" in IAC
    assert "postgres://" not in IAC and "postgresql://" not in IAC


def test_the_frontend_builds_from_its_own_directory():
    assert 'rootDirectory: "frontend"' in IAC
