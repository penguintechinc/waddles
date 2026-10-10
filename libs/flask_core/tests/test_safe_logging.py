"""Tests for flask_core.safe_logging (SECURITY: PII / secret in logs).

The load-bearing assertion in every test: a *sentinel* planted in the places a
real failure puts data (exception message, ``args``, URL query string, response
body, hostile attribute) never reaches captured log output -- not in the message,
not in ``extra``, and not in a rendered ``exc_info`` traceback footer.

Fail-first / mutation check (executed, see PR): replacing ``describe_exc``'s body
with ``return str(exc)`` turns ``test_message_and_args_never_reach_description``,
``test_log_exc_safe_never_logs_the_message`` and every ``TestHostileInput`` case
red; restoring it turns them green.
"""

from __future__ import annotations

import logging
import traceback
from types import SimpleNamespace

import httpx
import pytest
from cryptography.exceptions import InvalidTag

from flask_core.safe_logging import (
    SafeExcInfo,
    classify_exc,
    describe_exc,
    frames_only,
    log_exc_safe,
    url_host,
)

SENTINEL = "SENTINEL-s3cr3t-9f2b"

_LOGGER = logging.getLogger("test_safe_logging")


def _all_log_text(caplog: pytest.LogCaptureFixture) -> str:
    """Everything a handler could emit: formatted message, rendered traceback, `extra`."""
    formatter = logging.Formatter()
    chunks = [formatter.format(record) + repr(record.__dict__) for record in caplog.records]
    return "\n".join(chunks)


class _PgError(Exception):
    """Stand-in for a psycopg2 error: carries `pgcode` and a value-bearing message."""

    def __init__(self, message: str, pgcode: str | None) -> None:
        super().__init__(message)
        self.pgcode = pgcode


class TestDescribeExc:
    """What `describe_exc` is allowed to report."""

    def test_message_and_args_never_reach_description(self) -> None:
        exc = ValueError(f"bad value {SENTINEL}", SENTINEL)
        text = describe_exc(exc)
        assert SENTINEL not in text
        assert text == "type=builtins.ValueError category=value_error"

    def test_sqlstate_unique_violation(self) -> None:
        text = describe_exc(_PgError(f"Key (email)=({SENTINEL}) already exists", "23505"))
        assert SENTINEL not in text
        assert "code=23505" in text
        assert "category=unique_violation" in text

    @pytest.mark.parametrize(
        ("sqlstate", "category"),
        [
            ("23502", "not_null_violation"),
            ("23503", "foreign_key_violation"),
            ("22P02", "invalid_text_representation"),
            ("23000", "integrity_constraint_violation"),  # class fallback
            ("08006", "connection_failure"),
            ("40P01", "deadlock_detected"),
            ("XX000", "internal_error"),
            ("0A000", "feature_not_supported"),
            ("ZZ999", "db_error"),  # well-formed but unknown -> generic label, not the code text
        ],
    )
    def test_sqlstate_categories(self, sqlstate: str, category: str) -> None:
        assert classify_exc(_PgError("m", sqlstate)).category == category

    @pytest.mark.parametrize(
        "pgcode", ["23505\n", "23505 ", "2350", "23505x", "23505;DROP", "", None, 23505, [SENTINEL]]
    )
    def test_malformed_sqlstate_attribute_is_dropped(self, pgcode: object) -> None:
        """Only a well-formed 5-char SQLSTATE is ever reported; anything else is ignored."""
        info = classify_exc(_PgError(f"m {SENTINEL}", pgcode))  # type: ignore[arg-type]
        assert info.code is None
        assert info.category == "unclassified"
        assert SENTINEL not in str(info)

    def test_db_classification_is_delegated_to_db_errors(self) -> None:
        """One owner for the SQLSTATE tables: labels match `describe_db_error`'s exactly."""
        from flask_core.db_errors import summarize_db_error

        exc = _PgError(f"Key (email)=({SENTINEL}) already exists", "23505")
        info = classify_exc(exc)
        assert info.category == summarize_db_error(exc).category == "unique_violation"
        assert info.code == summarize_db_error(exc).sqlstate == "23505"

    def test_http_status_from_response(self) -> None:
        request = httpx.Request("POST", f"https://id.example/token?client_secret={SENTINEL}")
        exc = httpx.HTTPStatusError(
            f"401 for {request.url}", request=request, response=httpx.Response(401, request=request)
        )
        assert SENTINEL in str(exc)  # precondition: the raw message really leaks
        text = describe_exc(exc)
        assert SENTINEL not in text
        assert text == "type=httpx.HTTPStatusError code=401 category=http_status"

    def test_http_status_out_of_range_is_dropped(self) -> None:
        exc = Exception("x")
        exc.response = SimpleNamespace(status_code=SENTINEL)  # type: ignore[attr-defined]  # noqa: E501 - deliberately hostile
        assert classify_exc(exc).code is None

    def test_botocore_style_error_code(self) -> None:
        exc = Exception(f"An error occurred ({SENTINEL})")
        exc.response = {"Error": {"Code": "NoSuchKey", "Message": SENTINEL}}  # type: ignore[attr-defined]  # noqa: E501
        info = classify_exc(exc)
        assert info.code == "NoSuchKey"
        assert SENTINEL not in str(info)

    def test_botocore_code_must_match_allowlist_pattern(self) -> None:
        exc = Exception("x")
        exc.response = {"Error": {"Code": f"has spaces {SENTINEL}"}}  # type: ignore[attr-defined]
        assert classify_exc(exc).code is None

    def test_network_categories(self) -> None:
        assert classify_exc(httpx.ConnectError(SENTINEL)).category == "network_connect"
        assert classify_exc(httpx.ReadTimeout(SENTINEL)).category == "network_timeout"
        assert classify_exc(TimeoutError(SENTINEL)).category == "network_timeout"

    def test_errno_is_reported_for_os_errors(self) -> None:
        info = classify_exc(ConnectionRefusedError(111, f"refused {SENTINEL}"))
        assert info.code == "111"
        assert info.category == "network_connect"
        assert SENTINEL not in str(info)

    def test_cause_type_and_category_come_from_the_chain(self) -> None:
        class WrapperError(Exception):
            pass

        try:
            try:
                raise InvalidTag()
            except InvalidTag as inner:
                raise WrapperError(f"decrypt failed {SENTINEL}") from inner
        except WrapperError as exc:
            info = classify_exc(exc)
        assert info.category == "crypto_auth_failed"
        assert info.cause_type == "cryptography.exceptions.InvalidTag"
        assert SENTINEL not in str(info)

    def test_sqlalchemy_style_orig_is_unwrapped(self) -> None:
        wrapper = Exception(f"(psycopg2.errors.UniqueViolation) {SENTINEL}")
        wrapper.orig = _PgError(f"dup {SENTINEL}", "23505")  # type: ignore[attr-defined]
        info = classify_exc(wrapper)
        assert (info.code, info.category) == ("23505", "unique_violation")
        assert SENTINEL not in str(info)

    def test_unknown_type_is_unclassified(self) -> None:
        class MysteryError(Exception):
            pass

        assert classify_exc(MysteryError(SENTINEL)).category == "unclassified"

    def test_suppressed_context_is_not_followed(self) -> None:
        try:
            try:
                raise InvalidTag()
            except InvalidTag:
                raise RuntimeError(SENTINEL) from None
        except RuntimeError as exc:
            info = classify_exc(exc)
        assert info.category == "unclassified"
        assert info.cause_type is None

    def test_db_error_in_suppressed_context_reports_type_and_code_only(self) -> None:
        """`db_errors` follows `__context__` even after `from None`; only type/code leak."""
        try:
            try:
                raise _PgError(f"inner {SENTINEL}", "23505")
            except _PgError:
                raise RuntimeError(SENTINEL) from None
        except RuntimeError as exc:
            text = describe_exc(exc)
        assert SENTINEL not in text
        assert "code=23505" in text

    def test_cyclic_chain_terminates(self) -> None:
        a, b = Exception("a"), Exception("b")
        a.__cause__, b.__cause__ = b, a
        assert classify_exc(a).exc_type == "builtins.Exception"

    def test_result_is_a_frozen_slotted_dataclass(self) -> None:
        info = classify_exc(ValueError("x"))
        assert isinstance(info, SafeExcInfo)
        assert not hasattr(info, "__dict__")
        with pytest.raises(AttributeError):
            info.category = "x"  # type: ignore[misc]


class TestHostileInput:
    """Exceptions that try to smuggle data through classification must fail closed."""

    def test_raising_property_does_not_break_logging(self) -> None:
        class BoomError(Exception):
            @property
            def response(self) -> object:
                raise RuntimeError(SENTINEL)

            @property
            def pgcode(self) -> object:
                raise RuntimeError(SENTINEL)

        text = describe_exc(BoomError(SENTINEL))
        assert SENTINEL not in text
        assert text.endswith("category=unclassified")

    def test_hostile_class_name_is_rejected(self) -> None:
        hostile = type(f"Bad\nname {SENTINEL}", (Exception,), {})
        text = describe_exc(hostile(SENTINEL))
        assert SENTINEL not in text
        assert "<unsafe-type-name>" in text
        assert "\n" not in text

    def test_local_class_qualname_is_normalised(self) -> None:
        class LocalError(Exception):
            pass

        assert "locals" in describe_exc(LocalError()) and "<" not in describe_exc(LocalError())

    def test_non_exception_argument_does_not_crash_or_leak(self) -> None:
        text = describe_exc(SENTINEL)  # type: ignore[arg-type]  # misuse: a str, not an exception
        assert SENTINEL not in text


class TestLogExcSafe:
    """`log_exc_safe` emits type/code/category + caller fields, never the message."""

    def test_log_exc_safe_never_logs_the_message(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG)
        try:
            raise _PgError(f"Key (email)=({SENTINEL}) already exists", "23505")
        except _PgError as exc:
            log_exc_safe(_LOGGER, logging.ERROR, "insert failed", exc, op="insert")
        text = _all_log_text(caplog)
        assert SENTINEL not in text
        assert "insert failed type=" in text
        assert "code=23505" in text and "category=unique_violation" in text
        assert "op=insert" in text

    def test_debug_frames_record_has_location_but_no_exception_text(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        try:
            raise ValueError(SENTINEL)
        except ValueError as exc:
            log_exc_safe(_LOGGER, logging.WARNING, "boom", exc)
        debug = [r for r in caplog.records if r.levelno == logging.DEBUG]
        assert len(debug) == 1
        assert (
            "test_debug_frames_record_has_location_but_no_exception_text" in debug[0].getMessage()
        )
        assert SENTINEL not in _all_log_text(caplog)

    def test_no_debug_record_when_debug_disabled(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO)
        log_exc_safe(_LOGGER, logging.ERROR, "boom", ValueError("x"))
        assert [r.levelno for r in caplog.records] == [logging.ERROR]

    def test_disabled_level_is_a_no_op(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.CRITICAL)
        log_exc_safe(_LOGGER, logging.INFO, "boom", ValueError("x"))
        assert caplog.records == []

    def test_exc_info_is_never_attached(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG)
        try:
            raise ValueError(SENTINEL)
        except ValueError as exc:
            log_exc_safe(_LOGGER, logging.ERROR, "boom", exc)
        assert all(record.exc_info is None for record in caplog.records)

    def test_field_values_are_single_line_ascii_and_capped(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO)
        log_exc_safe(
            _LOGGER, logging.ERROR, "boom", ValueError("x"), who="a\nb\x1b[31m" + "z" * 500
        )
        message = caplog.records[0].getMessage()
        assert "\n" not in message and "\x1b" not in message
        assert len(message) < 400

    def test_unprintable_field_value_does_not_break_logging(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        class Bad:
            def __str__(self) -> str:
                raise RuntimeError(SENTINEL)

        caplog.set_level(logging.INFO)
        log_exc_safe(_LOGGER, logging.ERROR, "boom", ValueError("x"), thing=Bad())
        assert "thing=<unprintable>" in caplog.records[0].getMessage()
        assert SENTINEL not in _all_log_text(caplog)

    def test_log_record_points_at_the_caller(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO)
        log_exc_safe(_LOGGER, logging.ERROR, "boom", ValueError("x"))
        assert caplog.records[0].funcName == "test_log_record_points_at_the_caller"


class TestFramesOnly:
    """`frames_only` keeps where-it-failed without the exception footer."""

    def test_frames_without_message(self) -> None:
        def inner() -> None:
            raise ValueError(SENTINEL)

        try:
            inner()
        except ValueError as exc:
            rendered = frames_only(exc)
            full = "".join(traceback.format_exception(exc))
        assert SENTINEL in full  # precondition: the stdlib rendering leaks
        assert SENTINEL not in rendered
        assert rendered.startswith("test_safe_logging.py:")
        assert "inner" in rendered and " <- " in rendered

    def test_exception_without_traceback(self) -> None:
        assert frames_only(ValueError("x")) == "<no-frames>"

    def test_frame_count_is_capped(self) -> None:
        def recurse(depth: int) -> None:
            if depth == 0:
                raise ValueError(SENTINEL)
            recurse(depth - 1)

        try:
            recurse(50)
        except ValueError as exc:
            assert len(frames_only(exc).split(" <- ")) == 12


class TestUrlHost:
    """`url_host` drops everything a secret can ride in."""

    @pytest.mark.parametrize(
        ("url", "host"),
        [
            (f"https://api.example.com/p?token={SENTINEL}", "api.example.com"),
            (f"https://user:{SENTINEL}@Api.Example.com:8443/p#f", "api.example.com"),
            (f"https://hooks.example.com/services/{SENTINEL}", "hooks.example.com"),
            ("http://[::1]:8080/x", "::1"),
            ("http://10.0.0.5/x?a=b", "10.0.0.5"),
        ],
    )
    def test_host_only(self, url: str, host: str) -> None:
        result = url_host(url)
        assert result == host
        assert SENTINEL not in result

    @pytest.mark.parametrize(
        "value", ["not a url", "", "/relative/path?x=1", None, 42, f"http://bad host {SENTINEL}/"]
    )
    def test_unparseable_is_a_constant(self, value: object) -> None:
        assert url_host(value) == "<invalid-url>"

    def test_invalid_ipv6_does_not_raise(self) -> None:
        assert url_host("http://[::1/x") == "<invalid-url>"
