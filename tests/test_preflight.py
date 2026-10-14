"""Startup configuration checks.

Three settings fail open and silent: a deployment missing them serves happily
while losing documents, running without row-level security, or handing out an
unauthenticated admin. The failure is discovered weeks later by a customer
rather than in minutes by an alert, which is what makes silence the defect.

These tests pin two properties: the check names every one of those settings,
and it fails a deployed environment while merely warning locally.
"""

import pytest

from invoice_processing import paths
from invoice_processing.api import auth, preflight
from invoice_processing.api.preflight import (
    ConfigurationError,
    check_configuration,
    enforce,
    is_deployed,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (
        "GCS_BUCKET",
        "DATABASE_URL",
        "CLOUD_SQL_INSTANCE",
        "GOOGLE_CLOUD_PROJECT",
        "AUTH_DISABLED",
        "ENV",
        "STATE_DIR",
    ):
        monkeypatch.delenv(name, raising=False)


def configured(monkeypatch):
    monkeypatch.setenv("GCS_BUCKET", "b")
    monkeypatch.setenv("CLOUD_SQL_INSTANCE", "p:r:i")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")


class TestDetection:
    def test_a_fully_configured_environment_is_clean(self, monkeypatch):
        configured(monkeypatch)
        assert check_configuration() == []

    def test_missing_bucket_is_reported(self, monkeypatch):
        configured(monkeypatch)
        monkeypatch.delenv("GCS_BUCKET")
        [problem] = check_configuration()
        assert "GCS_BUCKET" in problem and "lost" in problem

    def test_missing_database_is_reported(self, monkeypatch):
        configured(monkeypatch)
        monkeypatch.delenv("CLOUD_SQL_INSTANCE")
        [problem] = check_configuration()
        assert "row-level security" in problem

    def test_missing_project_is_reported(self, monkeypatch):
        configured(monkeypatch)
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT")
        [problem] = check_configuration()
        assert "GOOGLE_CLOUD_PROJECT" in problem

    def test_disabled_auth_is_reported(self, monkeypatch):
        configured(monkeypatch)
        monkeypatch.setenv("AUTH_DISABLED", "1")
        [problem] = check_configuration()
        assert "unauthenticated admin" in problem

    def test_an_empty_environment_reports_everything(self):
        assert len(check_configuration()) == 3

    def test_either_database_variable_satisfies_the_check(self, monkeypatch):
        configured(monkeypatch)
        monkeypatch.delenv("CLOUD_SQL_INSTANCE")
        monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
        assert check_configuration() == []


class TestEnforcement:
    @pytest.mark.parametrize(
        "env", ["production", "prod", "staging", "PROD", "uat"]
    )
    def test_a_deployed_environment_refuses_to_start(self, env):
        with pytest.raises(ConfigurationError) as exc:
            enforce(env)
        assert "Refusing to start" in str(exc.value)

    @pytest.mark.parametrize("env", ["local", "dev", "test", "", "ci"])
    def test_a_non_deployed_environment_only_warns(self, env, caplog):
        enforce(env)  # must not raise
        assert any("Configuration" in r.message for r in caplog.records)

    def test_a_configured_deployment_starts(self, monkeypatch):
        configured(monkeypatch)
        enforce("production")

    def test_the_error_names_every_problem(self, monkeypatch):
        with pytest.raises(ConfigurationError) as exc:
            enforce("production")
        message = str(exc.value)
        assert "GCS_BUCKET" in message
        assert "row-level security" in message
        assert "GOOGLE_CLOUD_PROJECT" in message

    @pytest.mark.parametrize(
        ("env", "expected"),
        [
            ("production", True),
            ("staging", True),
            ("local", False),
            ("", False),
        ],
    )
    def test_is_deployed(self, env, expected):
        assert is_deployed(env) is expected


class TestDevBypassAllowList:
    """A deny-list has to anticipate every spelling of production."""

    @pytest.mark.parametrize("env", ["local", "dev", "development", "test"])
    def test_the_bypass_works_in_listed_local_environments(
        self, monkeypatch, env
    ):
        monkeypatch.setenv("AUTH_DISABLED", "1")
        monkeypatch.setenv("ENV", env)
        assert auth.dev_principal() is not None

    @pytest.mark.parametrize(
        "env",
        [
            "production",
            "prod",
            "prd",
            "live",
            "production-eu",
            "staging",
            "ci",
            "",
        ],
    )
    def test_the_bypass_refuses_everywhere_else(self, monkeypatch, env):
        """`prd`, `live` and an unset ENV all slipped through the deny-list."""
        monkeypatch.setenv("AUTH_DISABLED", "1")
        monkeypatch.setenv("ENV", env)
        assert auth.dev_principal() is None


class TestStartupHook:
    def test_the_app_runs_preflight_on_startup(self):
        source = (paths.AGENT_PKG_DIR / "api" / "service.py").read_text()
        assert "preflight" in source
        assert "lifespan_context" in source, "preflight is not wired to startup"


class TestStateDir:
    """A Railway volume stands in for a bucket; a missing one must not."""

    def _rest(self, monkeypatch):
        monkeypatch.setenv("CLOUD_SQL_INSTANCE", "p:r:i")
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")

    def test_a_writable_volume_satisfies_storage(self, monkeypatch, tmp_path):
        self._rest(monkeypatch)
        monkeypatch.setenv("STATE_DIR", str(tmp_path))
        assert check_configuration() == []

    def test_a_missing_volume_is_named(self, monkeypatch, tmp_path):
        self._rest(monkeypatch)
        monkeypatch.setenv("STATE_DIR", str(tmp_path / "nope"))
        [problem] = check_configuration()
        assert "does not exist" in problem and "volume" in problem

    def test_a_file_is_not_a_volume(self, monkeypatch, tmp_path):
        self._rest(monkeypatch)
        target = tmp_path / "f"
        target.write_text("x")
        monkeypatch.setenv("STATE_DIR", str(target))
        [problem] = check_configuration()
        assert "is not a directory" in problem

    def test_a_read_only_volume_is_named(self, monkeypatch, tmp_path):
        self._rest(monkeypatch)
        monkeypatch.setenv("STATE_DIR", str(tmp_path))
        monkeypatch.setattr(preflight.os, "access", lambda path, mode: False)
        [problem] = check_configuration()
        assert "not writable" in problem

    def test_neither_still_names_both_ways_out(self, monkeypatch):
        self._rest(monkeypatch)
        [problem] = check_configuration()
        assert "GCS_BUCKET" in problem and "STATE_DIR" in problem

    def test_a_deployed_service_refuses_a_missing_volume(
        self, monkeypatch, tmp_path
    ):
        self._rest(monkeypatch)
        monkeypatch.setenv("ENV", "production")
        monkeypatch.setenv("STATE_DIR", str(tmp_path / "nope"))
        with pytest.raises(ConfigurationError, match="volume"):
            enforce()

    def test_local_development_only_warns(self, monkeypatch, tmp_path, caplog):
        self._rest(monkeypatch)
        monkeypatch.setenv("STATE_DIR", str(tmp_path / "nope"))
        with caplog.at_level("WARNING"):
            enforce()
        assert "does not exist" in caplog.text
