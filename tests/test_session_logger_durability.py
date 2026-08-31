"""SME session logs must survive a crash.

The session file is the audit record of who proposed and approved which
correction rule. It used to be written only when save() was called explicitly,
so an exception -- or an SME simply walking away -- discarded the whole session
including the rule-approval events. Events are now flushed as they occur.
"""

import json

import pytest

from invoice_processing.core import session_logger as session_logger_module
from invoice_processing.core.session_logger import SessionLogger


@pytest.fixture
def sessions_dir(tmp_path):
    """Point the logger at a temp dir; it reads a module-level constant."""
    original = session_logger_module.SESSIONS_DIR
    session_logger_module.SESSIONS_DIR = tmp_path
    try:
        yield tmp_path
    finally:
        session_logger_module.SESSIONS_DIR = original


def _only_session(sessions_dir) -> dict:
    files = list(sessions_dir.glob("*.json"))
    assert len(files) == 1, f"expected one session file, found {files}"
    return json.loads(files[0].read_text())


def test_events_persist_without_calling_save(sessions_dir):
    """The regression: a session abandoned before save() must not be lost."""
    logger = SessionLogger()
    logger.log_case_loaded("case_005", "Rejected at phase 4")
    logger.log_sme_feedback("Emergency repairs under $2,000 are exempt")

    # Deliberately no save() -- simulating a crash or abandoned session.
    session = _only_session(sessions_dir)
    assert [e["event_type"] for e in session["events"]] == [
        "case_loaded",
        "sme_feedback",
    ]
    assert session["events"][1]["data"]["feedback"] == (
        "Emergency repairs under $2,000 are exempt"
    )


def test_each_event_is_flushed_as_it_happens(sessions_dir):
    logger = SessionLogger()

    logger.log_case_loaded("case_001", "summary")
    assert len(_only_session(sessions_dir)["events"]) == 1

    logger.log_sme_feedback("more context")
    assert len(_only_session(sessions_dir)["events"]) == 2


def test_save_finalizes_the_session(sessions_dir):
    logger = SessionLogger()
    logger.log_case_loaded("case_002", "summary")

    assert not _only_session(sessions_dir)["ended_at"], (
        "an in-progress session should not be marked ended"
    )
    path = logger.save()

    assert path is not None
    session = _only_session(sessions_dir)
    assert session["ended_at"]
    assert len(session["events"]) == 1


def test_no_file_written_before_any_event(sessions_dir):
    """Starting a session shouldn't leave an empty artifact behind."""
    SessionLogger().start()
    assert list(sessions_dir.glob("*.json")) == []


def test_flush_failure_does_not_break_the_workflow(sessions_dir, capsys):
    """An unwritable audit trail must warn, not crash the SME's session."""
    logger = SessionLogger()
    session_logger_module.SESSIONS_DIR = sessions_dir / "not-a-dir"
    (sessions_dir / "not-a-dir").write_text("blocking file")

    logger.log_case_loaded("case_001", "summary")

    assert "could not persist session log" in capsys.readouterr().out
