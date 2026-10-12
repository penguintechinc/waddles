"""Tests for flask_core.logging_config -- structured AAA formatter, handlers, per-event helpers.

Logging is exercised end to end: a real AAALogger writes to a real rotating file under
``tmp_path`` and the file contents are asserted (not just that a method was called).
"""

import logging
from logging.handlers import RotatingFileHandler, SysLogHandler

import pytest

from flask_core.logging_config import (
    AAALogger,
    StructuredFormatter,
    get_logger,
    setup_aaa_logging,
)


@pytest.fixture
def aaa(tmp_path):
    lg = AAALogger("modx", "1.2.3", log_level="DEBUG", log_dir=str(tmp_path))
    yield lg
    for h in list(lg.logger.handlers):
        h.close()
        lg.logger.removeHandler(h)


def read_log(tmp_path) -> str:
    return (tmp_path / "modx.log").read_text()


class TestStructuredFormatter:
    def _record(self, msg="hello", **extra):
        rec = logging.LogRecord("n", logging.WARNING, __file__, 1, msg, (), None)
        for k, v in extra.items():
            setattr(rec, k, v)
        return rec

    def test_minimal_record(self):
        out = StructuredFormatter("mod", "9").format(self._record())
        assert "WARNING mod:9 GENERAL hello" in out
        assert "community=" not in out and "time=" not in out

    def test_all_context_fields_rendered_in_order(self):
        rec = self._record(event_type="AUDIT", community="c1", user="u1", action="ban",
                           result="SUCCESS", execution_time=12, additional={"k": "v", "n": 3})
        out = StructuredFormatter("mod", "9").format(rec)
        tail = out.split("AUDIT ", 1)[1]
        assert tail == "community=c1 user=u1 action=ban result=SUCCESS time=12ms hello k=v n=3"

    def test_zero_execution_time_is_still_shown(self):
        out = StructuredFormatter("m", "1").format(self._record(execution_time=0))
        assert "time=0ms" in out

    def test_non_dict_additional_ignored(self):
        out = StructuredFormatter("m", "1").format(self._record(additional="oops"))
        assert out.endswith("hello")

    def test_message_args_are_interpolated(self):
        rec = logging.LogRecord("n", logging.INFO, __file__, 1, "x=%s", ("42",), None)
        assert StructuredFormatter("m", "1").format(rec).endswith("x=42")


class TestAAALoggerSetup:
    def test_handlers_and_level(self, aaa, tmp_path):
        kinds = {type(h) for h in aaa.logger.handlers}
        assert logging.StreamHandler in kinds and RotatingFileHandler in kinds
        assert aaa.logger.level == logging.DEBUG and aaa.logger.propagate is False
        assert aaa.logger.name == "waddlebot.modx"
        assert (tmp_path / "modx.log").exists()

    def test_reinit_does_not_stack_handlers(self, tmp_path):
        a = AAALogger("dup", "1", log_dir=str(tmp_path))
        b = AAALogger("dup", "1", log_dir=str(tmp_path))
        try:
            assert len(b.logger.handlers) == 2 and a.logger is b.logger
        finally:
            for h in list(b.logger.handlers):
                h.close()
                b.logger.removeHandler(h)

    def test_invalid_level_raises(self, tmp_path):
        with pytest.raises(AttributeError):
            AAALogger("bad", "1", log_level="LOUD", log_dir=str(tmp_path))

    def test_uncreatable_log_dir_degrades_to_console_only(self, tmp_path):
        blocker = tmp_path / "file"
        blocker.write_text("not a dir")
        lg = AAALogger("nodir", "1", log_dir=str(blocker / "sub"))
        try:
            assert [type(h) for h in lg.logger.handlers] == [logging.StreamHandler]
        finally:
            for h in list(lg.logger.handlers):
                h.close()
                lg.logger.removeHandler(h)

    def test_syslog_handler_attached_when_enabled(self, tmp_path):
        lg = AAALogger("sys", "1", log_dir=str(tmp_path), enable_syslog=True,
                       syslog_host="127.0.0.1", syslog_port=51400, syslog_facility="local3")
        try:
            sys_handlers = [h for h in lg.logger.handlers if isinstance(h, SysLogHandler)]
            assert len(sys_handlers) == 1
            assert lg.syslog_facility == SysLogHandler.LOG_LOCAL3
        finally:
            for h in list(lg.logger.handlers):
                h.close()
                lg.logger.removeHandler(h)

    def test_syslog_setup_failure_is_contained(self, tmp_path, monkeypatch):
        import flask_core.logging_config as lc

        def boom(*a, **k):
            raise OSError("no syslog")

        monkeypatch.setattr(lc, "SysLogHandler", type("S", (), {"__init__": boom, "LOG_LOCAL0": 16}))
        lg = AAALogger("sysfail", "1", log_dir=str(tmp_path), enable_syslog=True)
        try:
            assert len(lg.logger.handlers) == 2  # console + file only
        finally:
            for h in list(lg.logger.handlers):
                h.close()
                lg.logger.removeHandler(h)


class TestAAAEvents:
    def test_auth_authz_audit_lines(self, aaa, tmp_path):
        aaa.auth("login", "u1", "SUCCESS", ip="1.2.3.4")
        aaa.authz("read", "u1", "c9", "DENIED")
        aaa.audit("ban", "u2", "c9", "SUCCESS", detail="spam")
        text = read_log(tmp_path)
        assert "AUTH user=u1 action=login result=SUCCESS Authentication: login - SUCCESS ip=1.2.3.4" in text
        assert "AUTHZ community=c9 user=u1 action=read result=DENIED Authorization: read - DENIED" in text
        assert "AUDIT community=c9 user=u2 action=ban result=SUCCESS Audit: ban - SUCCESS detail=spam" in text

    def test_audit_requires_all_four_positionals(self, aaa):
        with pytest.raises(TypeError):
            aaa.audit("only-action")  # type: ignore[call-arg]

    @pytest.mark.parametrize("method,level_name", [
        ("debug", "DEBUG"), ("info", "INFO"), ("warning", "WARNING"),
        ("error", "ERROR"), ("critical", "CRITICAL"), ("system", "INFO"),
    ])
    def test_level_helpers(self, aaa, tmp_path, method, level_name):
        getattr(aaa, method)(f"msg-{method}", user="u1")
        text = read_log(tmp_path)
        assert f"{level_name} modx:1.2.3" in text and f"msg-{method}" in text
        assert "user=u1" in text

    def test_performance(self, aaa, tmp_path):
        aaa.performance("render", 125, route="/x")
        text = read_log(tmp_path)
        assert "PERFORMANCE action=render time=125ms Performance: render completed in 125ms route=/x" in text

    def test_level_filtering(self, tmp_path):
        lg = AAALogger("quiet", "1", log_level="ERROR", log_dir=str(tmp_path))
        try:
            lg.info("hidden")
            lg.error("shown")
        finally:
            for h in list(lg.logger.handlers):
                h.flush()
        text = (tmp_path / "quiet.log").read_text()
        assert "shown" in text and "hidden" not in text
        for h in list(lg.logger.handlers):
            h.close()
            lg.logger.removeHandler(h)

    def test_build_extra_splits_known_and_additional(self, aaa):
        extra = aaa._build_extra("X", user="u", custom=1, other="o")
        assert extra["event_type"] == "X" and extra["user"] == "u"
        assert extra["additional"] == {"custom": 1, "other": "o"}
        assert "additional" not in aaa._build_extra("X", user="u")


class TestFactories:
    def test_get_logger_namespacing(self):
        assert get_logger("abc").name == "waddlebot.abc"

    def test_setup_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "warning")
        monkeypatch.setenv("LOG_DIR", str(tmp_path))
        monkeypatch.setenv("ENABLE_SYSLOG", "false")
        lg = setup_aaa_logging("envmod", "2")
        try:
            assert lg.log_level == logging.WARNING and lg.log_dir == str(tmp_path)
            assert lg.enable_syslog is False and (lg.syslog_host, lg.syslog_port) == ("localhost", 514)
        finally:
            for h in list(lg.logger.handlers):
                h.close()
                lg.logger.removeHandler(h)

    def test_setup_explicit_args_beat_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "ERROR")
        monkeypatch.setenv("ENABLE_SYSLOG", "false")
        lg = setup_aaa_logging("explicit", "2", log_level="DEBUG", log_dir=str(tmp_path),
                               enable_syslog=True, syslog_host="127.0.0.1", syslog_port=51401,
                               syslog_facility="LOCAL1")
        try:
            assert lg.log_level == logging.DEBUG and lg.enable_syslog is True
            assert lg.syslog_port == 51401 and lg.syslog_facility == SysLogHandler.LOG_LOCAL1
        finally:
            for h in list(lg.logger.handlers):
                h.close()
                lg.logger.removeHandler(h)

    def test_setup_enable_syslog_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ENABLE_SYSLOG", "TRUE")
        monkeypatch.setenv("SYSLOG_HOST", "127.0.0.1")
        monkeypatch.setenv("SYSLOG_PORT", "51402")
        lg = setup_aaa_logging("sysenv", "1", log_dir=str(tmp_path))
        try:
            assert lg.enable_syslog is True and lg.syslog_port == 51402
        finally:
            for h in list(lg.logger.handlers):
                h.close()
                lg.logger.removeHandler(h)
