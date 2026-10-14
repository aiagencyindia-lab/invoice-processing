"""The Railway start script: both processes, and it stops when either does.

A container whose worker died but whose API still answers looks healthy to
the platform and processes nothing; exiting lets Railway restart it.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "start-railway.sh"


def run(worker: str, api: str) -> subprocess.Popen:
    return subprocess.Popen(
        ["sh", str(SCRIPT)],
        env={**os.environ, "WORKER_CMD": worker, "API_CMD": api},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def test_a_dead_worker_stops_the_container():
    proc = run("exit 3", "sleep 60")
    assert proc.wait(timeout=15) != 0


def test_a_dead_api_stops_the_container():
    proc = run("sleep 60", "exit 4")
    assert proc.wait(timeout=15) != 0


def test_sigterm_stops_both_and_is_a_clean_exit():
    """Railway sends SIGTERM on every redeploy; that is not a crash."""
    proc = run("sleep 60", "sleep 60")
    time.sleep(1)
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=15) == 0
