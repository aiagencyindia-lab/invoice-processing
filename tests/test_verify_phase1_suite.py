"""The acceptance sweep's "test suite passes" check (scripts/verify_phase1.py).

CI's Tests step runs the whole suite on the same commit just before the
sweep; running it again inside a 900 s timeout doubled the job and timed out
on a slow runner once the suite passed 15 minutes.
"""

import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_phase1.py"


def _sweep():
    spec = importlib.util.spec_from_file_location("verify_phase1", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_suite_check_skips_when_ci_already_ran_the_suite(monkeypatch):
    sweep = _sweep()
    monkeypatch.setenv("SUITE_ALREADY_RAN", "1")

    def never(*a, **k):
        raise AssertionError("the suite must not run twice")

    monkeypatch.setattr(subprocess, "run", never)
    with pytest.raises(sweep.Skip, match="Tests step"):
        sweep.the_suite_passes()


def test_the_suite_check_still_runs_by_default(monkeypatch):
    sweep = _sweep()
    monkeypatch.delenv("SUITE_ALREADY_RAN", raising=False)
    ran = []

    def fake(cmd, **kw):
        ran.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="1 passed\n")

    monkeypatch.setattr(subprocess, "run", fake)
    assert sweep.the_suite_passes() == "1 passed"
    assert ran and ran[0][-2:] == ["tests/", "-q"]
