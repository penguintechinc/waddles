"""Unit tests for the shared music-provider log-safety helpers (`providers._log_safety`)."""

import logging
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from providers._log_safety import (
    StripHttpxQueryFilter,
    describe_error,
    error_reason,
    install_httpx_log_guards,
    install_httpx_query_redaction,
    quiet_httpx_request_logs,
    safe_reason,
)

SECRET = "SENTINEL-helper-secret-d4a1"


@pytest.fixture
def httpx_logger() -> Iterator[logging.Logger]:
    """Yield the httpx logger with its level and filters restored afterwards."""
    logger = logging.getLogger("httpx")
    level, filters = logger.level, list(logger.filters)
    yield logger
    logger.setLevel(level)
    logger.filters[:] = filters


def _record(args: Any) -> logging.LogRecord:
    """Build a log record carrying `args`."""
    record = logging.LogRecord("httpx", logging.INFO, __file__, 1, "HTTP Request: %s", (), None)
    record.args = args
    return record


class TestStripHttpxQueryFilter:
    """The filter strips query strings from URL arguments and nothing else."""

    def test_httpx_url_argument_loses_its_query(self) -> None:
        """An httpx.URL arg keeps scheme/host/path but not the query."""
        record = _record((httpx.URL(f"https://api.test/p?oauth_token={SECRET}"), 200))
        assert StripHttpxQueryFilter().filter(record) is True
        args = record.args
        assert isinstance(args, tuple)
        assert SECRET not in repr(args)
        assert str(args[0]) == "https://api.test/p"
        assert args[1] == 200

    def test_string_url_argument_loses_its_query(self) -> None:
        """A str arg that looks like a URL is truncated at '?'."""
        record = _record((f"http://api.test/p?q={SECRET}",))
        StripHttpxQueryFilter().filter(record)
        assert record.args == ("http://api.test/p",)

    @pytest.mark.parametrize("scheme", ["http", "https"])
    def test_string_urls_of_both_schemes_are_stripped_at_the_first_question_mark(
        self, scheme: str
    ) -> None:
        """A second '?' inside the query must not re-expose the part after it."""
        record = _record((f"{scheme}://api.test/p?a=1?b={SECRET}",))
        StripHttpxQueryFilter().filter(record)
        assert record.args == (f"{scheme}://api.test/p",)

    def test_non_url_arguments_are_untouched(self) -> None:
        """Methods, versions and plain strings that are not URLs pass through."""
        record = _record(("GET", "what?ever", 7))
        StripHttpxQueryFilter().filter(record)
        assert record.args == ("GET", "what?ever", 7)

    def test_non_tuple_args_never_break_the_filter(self) -> None:
        """A record whose args are not a tuple is left alone and still passes."""
        record = _record(None)
        assert StripHttpxQueryFilter().filter(record) is True
        assert record.args is None


class TestInstall:
    """Installing the guards is idempotent and only ever raises the level."""

    def test_query_filter_is_installed_once(self, httpx_logger: logging.Logger) -> None:
        """Repeated installs add exactly one filter instance."""
        httpx_logger.filters[:] = []
        install_httpx_query_redaction()
        install_httpx_query_redaction()
        install_httpx_log_guards()
        assert sum(isinstance(f, StripHttpxQueryFilter) for f in httpx_logger.filters) == 1

    @pytest.mark.parametrize("start", [logging.NOTSET, logging.DEBUG, logging.INFO])
    def test_level_is_pinned_to_warning(self, httpx_logger: logging.Logger, start: int) -> None:
        """NOTSET/DEBUG/INFO are all raised to WARNING."""
        httpx_logger.setLevel(start)
        quiet_httpx_request_logs()
        assert httpx_logger.level == logging.WARNING

    @pytest.mark.parametrize("start", [logging.WARNING, logging.ERROR, logging.CRITICAL])
    def test_stricter_level_is_never_lowered(
        self, httpx_logger: logging.Logger, start: int
    ) -> None:
        """A deployment that set WARNING or stricter keeps its setting."""
        httpx_logger.setLevel(start)
        install_httpx_log_guards()
        assert httpx_logger.level == start


class TestSafeReason:
    """Only plain identifiers are accepted as a provider reason."""

    @pytest.mark.parametrize("value", ["invalid_grant", "NO_ACTIVE_DEVICE", "quotaExceeded", "a"])
    def test_identifiers_pass(self, value: str) -> None:
        """Typical OAuth / API enums are kept verbatim."""
        assert safe_reason(value) == value

    @pytest.mark.parametrize(
        "value",
        [f"bad {SECRET}", SECRET, "", "1abc", "a" * 65, "x\ny", None, 5, ["invalid_grant"]],
    )
    def test_everything_else_is_dropped(self, value: object) -> None:
        """Free text, hyphenated tokens, over-long and non-string values are rejected."""
        assert safe_reason(value) is None


class TestErrorReason:
    """error_reason() reads only enum-shaped fields and never raises."""

    def test_oauth_error_string(self) -> None:
        """``{"error": "invalid_grant"}`` -> invalid_grant."""
        response = httpx.Response(400, json={"error": "invalid_grant", "error_description": SECRET})
        assert error_reason(response) == "invalid_grant"

    def test_nested_reason(self) -> None:
        """``{"error": {"reason": ...}}`` -> the nested enum, never the message."""
        response = httpx.Response(
            403, json={"error": {"status": 403, "message": SECRET, "reason": "PREMIUM_REQUIRED"}}
        )
        assert error_reason(response) == "PREMIUM_REQUIRED"

    @pytest.mark.parametrize(
        "body",
        [
            {"error": {"message": SECRET}},
            {"error": f"free text {SECRET}"},
            {"error": 5},
            {"errors": [{"error_message": SECRET}]},
            [SECRET],
            "string-body",
            {},
        ],
    )
    def test_no_usable_reason(self, body: Any) -> None:
        """Bodies without an identifier-shaped reason yield None."""
        assert error_reason(httpx.Response(400, json=body)) is None

    def test_non_json_body(self, caplog: pytest.LogCaptureFixture) -> None:
        """A non-JSON body yields None and a DEBUG line carrying only the exception type."""
        response = httpx.Response(502, text=f"<html>{SECRET}</html>")
        with caplog.at_level(logging.DEBUG, logger="providers._log_safety"):
            assert error_reason(response) is None
        assert [r.getMessage() for r in caplog.records] == [
            "Provider error body is not JSON: JSONDecodeError"
        ]

    def test_dropped_reason_is_logged_once_at_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        """A present-but-unsafe reason is dropped with a static DEBUG line (no value)."""
        response = httpx.Response(400, json={"error": f"bad {SECRET}"})
        with caplog.at_level(logging.DEBUG, logger="providers._log_safety"):
            assert error_reason(response) is None
        assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
            (logging.DEBUG, "Provider error reason is not a plain identifier; dropped")
        ]

    @pytest.mark.parametrize("body", [{"error": "invalid_grant"}, {}, {"error": {"status": 1}}])
    def test_no_drop_line_for_a_valid_or_absent_reason(
        self, caplog: pytest.LogCaptureFixture, body: Any
    ) -> None:
        """Nothing is logged when the reason is valid or simply absent."""
        with caplog.at_level(logging.DEBUG, logger="providers._log_safety"):
            error_reason(httpx.Response(400, json=body))
        assert caplog.records == []


class _OwnError(Exception):
    """Stand-in for a provider's own exception type."""


class TestDescribeError:
    """describe_error() never renders an exception message it does not own."""

    def test_unknown_exception_is_type_only(self) -> None:
        """A foreign exception contributes its type name and nothing else."""
        assert describe_error(RuntimeError(SECRET)) == "RuntimeError"

    def test_transport_error_is_type_only(self) -> None:
        """httpx transport errors embed URLs in their text -- type only."""
        request = httpx.Request("GET", f"https://api.test/?oauth_token={SECRET}")
        assert describe_error(httpx.ConnectError(SECRET, request=request)) == "ConnectError"

    def test_status_error_reports_status_and_reason(self) -> None:
        """HTTPStatusError -> status plus the validated reason, no URL."""
        request = httpx.Request("GET", f"https://api.test/?oauth_token={SECRET}")
        response = httpx.Response(400, request=request, json={"error": "invalid_grant"})
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            text = describe_error(exc)
        assert text == "HTTPStatusError status=400 reason=invalid_grant"
        assert SECRET not in text

    def test_status_error_without_reason(self) -> None:
        """No usable reason -> the field is omitted rather than guessed."""
        request = httpx.Request("GET", "https://api.test/")
        try:
            httpx.Response(500, request=request, text=SECRET).raise_for_status()
        except httpx.HTTPStatusError as exc:
            assert describe_error(exc) == "HTTPStatusError status=500"

    def test_own_error_detail_is_carried_only_when_declared(self) -> None:
        """Declared provider errors keep their (trusted) text; undeclared ones do not."""
        exc = _OwnError("static failure")
        assert describe_error(exc, own_errors=(_OwnError,)) == "_OwnError detail=static failure"
        assert describe_error(exc) == "_OwnError"

    def test_own_error_detail_is_sanitised_and_capped(self) -> None:
        """Control characters become '?', and the detail is length-capped (log forging)."""
        exc = _OwnError("line1\nFAKE-LOG-LINE\r\x00" + "x" * 500)
        text = describe_error(exc, own_errors=(_OwnError,))
        assert "\n" not in text and "\r" not in text and "\x00" not in text
        assert "line1?FAKE-LOG-LINE??" in text
        assert len(text) < 260
