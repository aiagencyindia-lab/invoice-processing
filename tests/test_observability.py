"""One line per event, as JSON, with the ids that join it to the audit trail."""

import json
import logging
import sys

import pytest

from invoice_processing import observability as obs
from invoice_processing.api import auth
from invoice_processing.llm_guard import LlmUnavailable


@pytest.fixture(autouse=True)
def clean_root():
    root = logging.getLogger()
    before = list(root.handlers)
    yield
    for h in list(root.handlers):
        if h.name == obs.HANDLER_NAME:
            root.removeHandler(h)
    for h in before:
        if h not in root.handlers:
            root.addHandler(h)


def _record(msg="hello", *, args=(), exc_info=None, **extra):
    record = logging.LogRecord(
        "invoice_processing.x", logging.INFO, "f.py", 1, msg, args, exc_info
    )
    for k, v in extra.items():
        setattr(record, k, v)
    obs.ContextFilter("api").filter(record)
    return record


def _line(record):
    return json.loads(obs.JsonFormatter().format(record))


class TestTheLine:
    def test_carries_the_ids_from_the_context(self, monkeypatch):
        monkeypatch.setenv("ENV", "staging")
        t1 = auth.current_request_id.set("req-1")
        t2 = obs.current_tenant_id.set("acme")
        t3 = obs.current_document_id.set("doc-9")
        try:
            line = _line(_record("hi", method="GET"))
        finally:
            auth.current_request_id.reset(t1)
            obs.current_tenant_id.reset(t2)
            obs.current_document_id.reset(t3)
        assert line["msg"] == "hi" and line["level"] == "INFO"
        assert line["logger"] == "invoice_processing.x"
        assert (line["request_id"], line["tenant_id"], line["document_id"]) == (
            "req-1",
            "acme",
            "doc-9",
        )
        assert line["service"] == "api" and line["env"] == "staging"
        assert line["method"] == "GET"
        assert line["ts"].endswith("Z") or "+" in line["ts"]

    def test_ids_are_empty_outside_a_request(self):
        line = _line(_record())
        assert (line["request_id"], line["tenant_id"], line["document_id"]) == (
            "",
            "",
            "",
        )

    def test_an_exception_is_one_field(self):
        try:
            raise ValueError("boom")
        except ValueError:
            record = _record("failed", exc_info=sys.exc_info())
        line = _line(record)
        assert "ValueError: boom" in line["exc"]
        assert "\n" not in line["msg"]

    def test_extra_keys_cannot_break_the_line(self):
        """`extra` keys that shadow the line's own fields, or hold something
        json cannot encode, must not raise or hide the record."""
        record = _record("hi", exc="not a traceback", when=object())
        line = _line(record)
        assert line["msg"] == "hi"
        assert line["exc"] == "not a traceback"
        assert isinstance(line["when"], str)

    def test_a_bad_format_string_still_logs(self):
        record = _record("%s %s", args=("only-one",))
        line = _line(record)
        assert "only-one" in line["msg"]


class TestEveryRecord:
    def test_every_record_carries_the_ids_through_any_handler(self, caplog):
        """caplog's handler is not ours, and a child logger's record never
        passes the root logger's filters: the ids are stamped when the
        record is made, so every handler sees them."""
        token = auth.current_request_id.set("req-caplog")
        try:
            with caplog.at_level("INFO", logger="invoice_processing.child"):
                logging.getLogger("invoice_processing.child").info("x")
        finally:
            auth.current_request_id.reset(token)
        [record] = caplog.records
        assert record.request_id == "req-caplog"
        assert record.tenant_id == "" and record.document_id == ""


class TestConfigure:
    def test_json_when_deployed_text_otherwise(self, monkeypatch, capsys):
        monkeypatch.delenv("LOG_FORMAT", raising=False)
        monkeypatch.setenv("ENV", "production")
        obs.configure("api")
        logging.getLogger("invoice_processing.t").info("deployed")
        monkeypatch.setenv("ENV", "local")
        obs.configure("api")
        logging.getLogger("invoice_processing.t").info("local")
        out = capsys.readouterr().out.strip().splitlines()
        assert json.loads(out[0])["msg"] == "deployed"
        assert out[1].endswith("INFO invoice_processing.t [] local")

    def test_log_format_overrides_the_environment(self, monkeypatch, capsys):
        monkeypatch.setenv("ENV", "local")
        monkeypatch.setenv("LOG_FORMAT", "json")
        obs.configure("worker")
        logging.getLogger("invoice_processing.t").info("x")
        assert (
            json.loads(capsys.readouterr().out.strip())["service"] == "worker"
        )

    def test_configure_is_idempotent_and_keeps_other_handlers(
        self, monkeypatch
    ):
        root = logging.getLogger()
        other = logging.NullHandler()
        root.addHandler(other)
        try:
            obs.configure("api")
            obs.configure("api")
            ours = [h for h in root.handlers if h.name == obs.HANDLER_NAME]
            assert len(ours) == 1
            assert other in root.handlers
        finally:
            root.removeHandler(other)

    def test_uvicorn_access_log_is_off_and_errors_propagate(self, monkeypatch):
        obs.configure("api")
        assert logging.getLogger("uvicorn.access").disabled is True
        error = logging.getLogger("uvicorn.error")
        assert error.handlers == [] and error.propagate is True

    def test_level_comes_from_the_environment(self, monkeypatch, capsys):
        monkeypatch.setenv("LOG_LEVEL", "WARNING")
        monkeypatch.setenv("LOG_FORMAT", "json")
        obs.configure("api")
        logging.getLogger("invoice_processing.t").info("quiet")
        logging.getLogger("invoice_processing.t").warning("loud")
        out = capsys.readouterr().out
        assert "quiet" not in out and "loud" in out


class TestSentry:
    def test_no_test_reaches_the_real_sentry(self):
        """The developer's .env carries a real SENTRY_DSN, loaded into the
        environment at import. A test that starts a service (the worker's
        failed-migration test does) then sent its deliberate errors to the
        real project, and the run ended waiting on Sentry's flush."""
        import os  # noqa: PLC0415

        # Names only: a failure here must never print the DSN itself.
        set_names = {name for name, value in os.environ.items() if value}
        assert "SENTRY_DSN" not in set_names
        assert obs.init_sentry("worker") is False
        assert not obs.sentry_sdk.get_client().is_active()

    def test_off_without_a_dsn(self, monkeypatch):
        monkeypatch.delenv("SENTRY_DSN", raising=False)
        calls = []
        monkeypatch.setattr(
            obs.sentry_sdk, "init", lambda **kw: calls.append(kw)
        )
        assert obs.init_sentry("api") is False
        assert calls == []

    def test_a_malformed_dsn_runs_without_sentry(self, monkeypatch, caplog):
        """A pasted DSN with quotes or a missing scheme crash-looped both
        production processes at start-up. Monitoring is not a control: say so
        and keep serving. The warning must not echo the value (it is a
        credential)."""
        monkeypatch.setenv("SENTRY_DSN", "key@o1.ingest.sentry.io/1")
        with caplog.at_level(logging.WARNING):
            assert obs.init_sentry("api") is False
        assert not obs.sentry_sdk.get_client().is_active()
        assert "SENTRY_DSN" in caplog.text
        assert "ingest.sentry.io" not in caplog.text

    def test_on_with_a_dsn_and_the_safe_options(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DSN", "https://key@o1.ingest.sentry.io/1")
        monkeypatch.setenv("ENV", "staging")
        monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "abc1234")
        calls = []
        monkeypatch.setattr(
            obs.sentry_sdk, "init", lambda **kw: calls.append(kw)
        )
        assert obs.init_sentry("worker") is True
        [kw] = calls
        assert kw["dsn"].endswith("/1")
        assert kw["environment"] == "staging" and kw["release"] == "abc1234"
        assert kw["send_default_pii"] is False
        assert kw["max_request_body_size"] == "never"
        assert kw["traces_sample_rate"] == 0
        assert kw["before_send"] is obs.scrub
        names = {type(i).__name__ for i in kw["integrations"]}
        assert "LoggingIntegration" in names

    def test_scrub_drops_credentials_and_tags_the_ids(self, monkeypatch):
        t1 = auth.current_request_id.set("req-7")
        t2 = obs.current_tenant_id.set("acme")
        try:
            event = obs.scrub(
                {
                    "request": {
                        "headers": {
                            "Authorization": "Bearer x",
                            "Cookie": "a=b",
                            "X-Dev-Principal": "a@b:admin",
                            "User-Agent": "ua",
                        }
                    },
                    "tags": {"kept": "yes"},
                },
                {},
            )
        finally:
            auth.current_request_id.reset(t1)
            obs.current_tenant_id.reset(t2)
        assert event["request"]["headers"] == {"User-Agent": "ua"}
        assert event["tags"]["request_id"] == "req-7"
        assert event["tags"]["tenant_id"] == "acme"
        assert event["tags"]["kept"] == "yes"

    def test_an_outage_is_one_issue(self):
        hint = {"exc_info": (LlmUnavailable, LlmUnavailable("503"), None)}
        event = obs.scrub({"tags": {}}, hint)
        assert event["fingerprint"] == ["llm-unavailable"]


class TestReviewFixes:
    def test_extra_cannot_reuse_the_stamped_names(self):
        """The ids are on every record from birth, so the stdlib refuses an
        `extra=` that names them. Pinned so the first person to try finds
        out here, not in production."""
        with pytest.raises(KeyError, match="request_id"):
            # warning, not info: an unconfigured root is at WARNING and a
            # disabled level returns before the record is ever made.
            logging.getLogger("invoice_processing.t").warning(
                "x", extra={"request_id": "abc"}
            )

    def test_uvicorns_colour_copy_stays_out_of_the_line(self):
        line = _line(_record("Uvicorn running", color_message="\x1b[1mhi"))
        assert "color_message" not in line

    def test_a_bad_log_level_falls_back_to_info(self, monkeypatch, capsys):
        monkeypatch.setenv("LOG_LEVEL", "VERBOSE")
        monkeypatch.setenv("LOG_FORMAT", "json")
        obs.configure("api")
        logging.getLogger("invoice_processing.t").info("still here")
        out = capsys.readouterr().out
        assert "still here" in out and "LOG_LEVEL" in out

    def test_the_text_format_shows_the_request_id(self, monkeypatch, capsys):
        monkeypatch.setenv("LOG_FORMAT", "text")
        obs.configure("api")
        token = auth.current_request_id.set("req-text")
        try:
            logging.getLogger("invoice_processing.t").info("hi")
        finally:
            auth.current_request_id.reset(token)
        assert "req-text" in capsys.readouterr().out

    def test_scrub_handles_headers_as_pairs(self):
        event = obs.scrub(
            {"request": {"headers": [["Authorization", "x"], ["Accept", "y"]]}},
            {},
        )
        assert event["request"]["headers"] == [["Accept", "y"]]
