"""The API service's Railway deploy settings, kept in git.

Railway reads `deployment/railway.api.json` for the `invoice-processing`
service (its config-file path is set to it) and the file overrides the
dashboard. A rename in the repo that the file still points at would only
show up as a failed production deploy, so it is checked here.
"""

import importlib.util
import json
import pathlib

from fastapi.testclient import TestClient

from invoice_processing.api import service
from invoice_processing.api.service import app

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "deployment" / "railway.api.json").read_text())
DEPLOY = CONFIG["deploy"]


def test_the_start_script_exists():
    command = DEPLOY["startCommand"].split()
    assert command[0] == "sh" and (ROOT / command[1]).is_file()


def test_the_pre_deploy_step_migrates():
    (command,) = DEPLOY["preDeployCommand"]
    assert command == "python -m invoice_processing.db.migrate"
    assert importlib.util.find_spec("invoice_processing.db.migrate")


def test_the_health_check_is_a_public_route(monkeypatch):
    monkeypatch.setattr(service, "_database_configured", lambda: False)
    r = TestClient(app).get(DEPLOY["healthcheckPath"])
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_it_holds_only_deploy_settings():
    """No builder: the image keeps coming from the repo's Dockerfile."""
    assert set(CONFIG) == {"$schema", "deploy"}
    assert DEPLOY["drainingSeconds"] == 60
    assert DEPLOY["restartPolicyType"] == "ON_FAILURE"
