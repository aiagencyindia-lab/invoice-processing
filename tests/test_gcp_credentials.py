"""Bridging a credential held as a variable to the file Google wants.

The bridge used to live in the image's shell entrypoint. A platform that lets
a service set its own start command replaces the entrypoint with it, so the
worker -- which needs a custom command -- ran with no credentials at all and
failed on the first Google client it built. The API kept working, because it
uses the image's default command. That is why this is Python on the path every
entry point takes, and why it is tested.
"""

import json
import os
import pathlib
import stat

import pytest

from invoice_processing.gcp_credentials import (
    CredentialError,
    materialise_credentials,
)

# A shape that satisfies the checks, without being a credential.
_KEY = json.dumps(
    {"type": "service_account", "project_id": "p", "private_key": "not-a-key"}
)


@pytest.fixture(autouse=True)
def a_clean_environment(monkeypatch):
    """`materialise_credentials` sets GOOGLE_APPLICATION_CREDENTIALS in this
    process, so without clearing it between tests one test's file makes the
    next take the "already configured" path and assert nothing."""
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.delenv("GOOGLE_CREDENTIALS_JSON", raising=False)
    monkeypatch.delenv("GOOGLE_CREDENTIALS_PATH", raising=False)


class TestItBridges:
    def test_writes_the_variable_to_a_file_and_points_google_at_it(
        self, tmp_path, monkeypatch
    ):
        payload = _KEY
        monkeypatch.setenv("GOOGLE_CREDENTIALS_JSON", payload)
        target = tmp_path / "key.json"
        written = materialise_credentials(target)

        assert written == str(target)
        assert target.read_text() == payload
        assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == str(target)

    def test_the_file_is_not_world_readable(self, tmp_path, monkeypatch):
        """It holds a private key."""
        monkeypatch.setenv("GOOGLE_CREDENTIALS_JSON", _KEY)
        target = tmp_path / "key.json"
        materialise_credentials(target)
        mode = stat.S_IMODE(target.stat().st_mode)
        assert not mode & stat.S_IRGRP
        assert not mode & stat.S_IROTH

    def test_creates_the_directory(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GOOGLE_CREDENTIALS_JSON", _KEY)
        target = tmp_path / "nested" / "deeper" / "key.json"
        materialise_credentials(target)
        assert target.exists()


class TestItLeavesWellAlone:
    def test_does_nothing_when_the_variable_is_unset(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("GOOGLE_CREDENTIALS_JSON", raising=False)
        target = tmp_path / "key.json"
        assert materialise_credentials(target) is None
        assert not target.exists()

    def test_treats_an_empty_variable_as_unset(self, tmp_path, monkeypatch):
        """`FOO=` is how a deployment blanks a setting; it is not a credential."""
        monkeypatch.setenv("GOOGLE_CREDENTIALS_JSON", "   ")
        target = tmp_path / "key.json"
        assert materialise_credentials(target) is None
        assert not target.exists()

    def test_a_mounted_key_file_outranks_the_variable(
        self, tmp_path, monkeypatch
    ):
        """A deployment that went to the trouble of mounting one means it."""
        mounted = tmp_path / "mounted.json"
        mounted.write_text("{}")
        monkeypatch.setenv("GOOGLE_CREDENTIALS_JSON", _KEY)
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(mounted))

        target = tmp_path / "key.json"
        assert materialise_credentials(target) == str(mounted)
        assert not target.exists()

    def test_a_stale_pointer_does_not_win(self, tmp_path, monkeypatch):
        """A path that no longer exists is a leftover, not a configuration."""
        monkeypatch.setenv("GOOGLE_CREDENTIALS_JSON", _KEY)
        monkeypatch.setenv(
            "GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "gone.json")
        )
        target = tmp_path / "key.json"
        assert materialise_credentials(target) == str(target)


class TestEveryEntryPointBridges:
    """The failure was one entry point missing it, so assert both have it."""

    def test_the_worker_bridges_before_it_starts(self):
        source = pathlib.Path(
            "invoice_processing/worker/__main__.py"
        ).read_text(encoding="utf-8")
        assert "materialise_credentials()" in source

    def test_the_api_bridges_at_startup(self):
        source = pathlib.Path("invoice_processing/api/preflight.py").read_text(
            encoding="utf-8"
        )
        assert "materialise_credentials()" in source


class TestItRefusesRubbish:
    """A credential that is present and unusable must say so itself.

    Handed a placeholder, google-auth fails three frames deep with "is not a
    valid json file" -- naming the file that was just written rather than the
    variable that was wrong. That is exactly how a placeholder sat unnoticed
    on a deployed worker.
    """

    def test_a_placeholder_is_refused_by_name(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GOOGLE_CREDENTIALS_JSON", "<paste your key here>")
        with pytest.raises(CredentialError) as caught:
            materialise_credentials(tmp_path / "key.json")
        message = str(caught.value)
        assert "GOOGLE_CREDENTIALS_JSON" in message
        assert "not valid JSON" in message
        # The length and first character are what identify a truncated paste.
        assert "21 characters" in message

    def test_json_that_is_not_a_key_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GOOGLE_CREDENTIALS_JSON", '{"type": "whatever"}')
        with pytest.raises(CredentialError) as caught:
            materialise_credentials(tmp_path / "key.json")
        assert "private_key" in str(caught.value)

    def test_nothing_is_written_when_it_is_refused(self, tmp_path, monkeypatch):
        """A half-written file would be found by the next start and trusted."""
        monkeypatch.setenv("GOOGLE_CREDENTIALS_JSON", "nonsense")
        target = tmp_path / "key.json"
        with pytest.raises(CredentialError):
            materialise_credentials(target)
        assert not target.exists()
