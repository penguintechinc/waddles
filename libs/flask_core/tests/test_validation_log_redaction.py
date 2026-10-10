"""Validation-failure log redaction -- SECURITY (PII in logs).

# regression: the ``validate_json`` / ``validate_query`` / ``validate_form``
# decorators logged ``logger.error(f"Validation errors: {errors}")`` (non-strict
# mode) and ``f"... error={str(e)}"`` (any exception, including ones raised by
# the wrapped endpoint). ``errors`` carried each failing field's ``loc`` -- which
# includes client-chosen dict keys / extra-field names -- and Pydantic's ``msg``,
# which echoes the value for custom validators and parse errors. Every consuming
# service therefore wrote user-supplied values into its log stream.

Each test drives a request whose offending value / key / message embeds
`SENTINEL` (a known marker) and asserts it is absent from *everything* the
logging system emitted -- rendered message, formatted record, ``args`` and
``exc_info`` -- while the error count, declared field names and error types stay
present, so the log line remains actionable and the assertion cannot pass
vacuously (record and detail denominators are asserted on every test).
"""

from __future__ import annotations

import logging
import sys
import types
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_PKG_DIR = Path(__file__).resolve().parent.parent / "flask_core"
if "flask_core" not in sys.modules:
    _stub = types.ModuleType("flask_core")
    _stub.__path__ = [str(_PKG_DIR)]
    sys.modules["flask_core"] = _stub

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from quart import Quart

from flask_core.validation import (
    validate_data,
    validate_form,
    validate_json,
    validate_query,
)
from flask_core.validation_errors import describe_validation_errors

SENTINEL = "SENTINEL-pii-victim-9f2c1a7e"
LOGGER_NAME = "flask_core.validation"


class Address(BaseModel):
    """Nested model so nested locations are exercised."""

    model_config = ConfigDict(extra="forbid")

    zip_code: int


class Signup(BaseModel):
    """JSON body model hitting parse, length, custom-message, key and nesting errors."""

    model_config = ConfigDict(extra="forbid")

    age: int
    nickname: str = Field(min_length=40)
    email: str
    ident: uuid.UUID
    tags: list[int]
    meta: dict[str, int]
    address: Address

    @field_validator("email")
    @classmethod
    def _email_not_reserved(cls, value: str) -> str:
        """Echo the value in the error message -- the worst-case custom validator."""
        raise ValueError(f"{value} is not an allowed email")


class FlatSignup(BaseModel):
    """Flat model for query-string / form sources."""

    model_config = ConfigDict(extra="forbid")

    age: int
    nickname: str = Field(min_length=40)
    email: str

    @field_validator("email")
    @classmethod
    def _email_not_reserved(cls, value: str) -> str:
        """Echo the value in the error message -- the worst-case custom validator."""
        raise ValueError(f"{value} is not an allowed email")


class Passes(BaseModel):
    """Model that always validates, so the *endpoint* is what raises."""

    model_config = ConfigDict(extra="ignore")


class Guarded(BaseModel):
    """Model-level validator whose message echoes the payload (``loc == ()``)."""

    name: str

    @model_validator(mode="after")
    def _reject(self) -> Guarded:
        """Always fail, echoing a value."""
        raise ValueError(f"rejected {self.name}")


JSON_BODY: dict[str, Any] = {
    "age": SENTINEL,
    "nickname": SENTINEL,
    "email": SENTINEL,
    "ident": SENTINEL,
    "tags": ["1", SENTINEL],
    "meta": {SENTINEL: "x"},
    "address": {"zip_code": SENTINEL, SENTINEL: 1},
    SENTINEL: "extra-field-name-is-client-chosen",
}
FLAT_PARAMS: dict[str, str] = {
    "age": SENTINEL,
    "nickname": SENTINEL,
    "email": SENTINEL,
    SENTINEL: "x",
}


def _emitted(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Every textual rendering of every captured record (message, formatted, args, dict)."""
    texts: list[str] = []
    for record in caplog.records:
        texts.append(record.getMessage())
        texts.append(caplog.handler.format(record))
        texts.append(repr(record.args))
        texts.append(repr(vars(record)))
    return texts


def _assert_no_sentinel(caplog: pytest.LogCaptureFixture) -> None:
    """Assert records exist (non-vacuous) and none of their renderings contain the sentinel."""
    from_validation = [r for r in caplog.records if r.name == LOGGER_NAME]
    assert from_validation, (
        "no validation log record captured -- test would pass vacuously"
    )
    for text in _emitted(caplog):
        assert SENTINEL not in text, f"validation log leaked client value: {text!r}"
    for record in caplog.records:
        assert record.exc_info is None
        assert record.exc_text is None


def _lines(
    caplog: pytest.LogCaptureFixture, contains: str, level: int | None = None
) -> list[str]:
    """Return rendered messages from the validation logger containing `contains`.

    Restricted to records of exactly `level` when given (the DEBUG traceback record
    reuses the ERROR line's operation label).
    """
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == LOGGER_NAME
        and contains in r.getMessage()
        and (level is None or r.levelno == level)
    ]


async def _ok(data: Any) -> dict[str, str]:
    """Endpoint that succeeds (reached only if validation passes / is non-strict)."""
    return {"ok": "yes"}


def _mount(
    kind: str,
    model: type[BaseModel],
    handler: Callable[[Any], Awaitable[Any]],
    strict: bool = True,
) -> Quart:
    """Build a Quart app whose one route wraps `handler` in the `kind` validation decorator.

    Applied in call form (not ``@`` syntax) because the decorators are untyped, and
    ``@`` would make the typed handler untyped under ``mypy --strict``.
    """
    app = Quart(__name__)
    if kind == "json":
        app.post("/r")(validate_json(model, strict=strict)(handler))
    elif kind == "query":
        app.get("/r")(validate_query(model, strict=strict)(handler))
    else:
        app.post("/r")(validate_form(model, strict=strict)(handler))
    return app


def _app(kind: str, strict: bool) -> Quart:
    """Build the standard sentinel-test app for `kind` (always-succeeding endpoint)."""
    model = Signup if kind == "json" else FlatSignup
    return _mount(kind, model, _ok, strict)


async def _send(app: Quart, kind: str) -> Any:
    """Send the sentinel-bearing request for `kind` and return the response."""
    client = app.test_client()
    if kind == "json":
        return await client.post("/r", json=JSON_BODY)
    if kind == "query":
        return await client.get("/r", query_string=FLAT_PARAMS)
    return await client.post("/r", form=FLAT_PARAMS)


class TestDecoratorValidationFailureLogs:
    """No decorator, in any mode, may write client-supplied values to the log."""

    @pytest.mark.parametrize("strict", [True, False], ids=["strict", "non_strict"])
    @pytest.mark.parametrize("kind", ["json", "query", "form"])
    async def test_sentinel_never_logged(
        self, caplog: pytest.LogCaptureFixture, kind: str, strict: bool
    ) -> None:
        """Sentinel as value, dict key, extra-field name and validator message never reaches logs."""
        caplog.set_level(logging.DEBUG)
        resp = await _send(_app(kind, strict), kind)

        # Non-vacuous: the request really failed validation / was passed through.
        assert resp.status_code == (400 if strict else 200)
        _assert_no_sentinel(caplog)
        # The 400 body still reports failures to the (same) client -- unchanged behavior.
        if strict:
            assert "errors" in await resp.get_json()

    @pytest.mark.parametrize("strict", [True, False], ids=["strict", "non_strict"])
    async def test_json_log_stays_actionable(
        self, caplog: pytest.LogCaptureFixture, strict: bool
    ) -> None:
        """The log still names count, declared field locations and error types."""
        caplog.set_level(logging.DEBUG)
        await _send(_app("json", strict), "json")

        (warning,) = _lines(caplog, "AUTHZ validation_failed")
        assert "model=Signup" in warning
        assert "errors=9 " in warning
        for detail in (
            "age:int_parsing",
            "nickname:string_too_short",
            "email:value_error",
            "ident:uuid_parsing",
            "tags[]:int_parsing",
            "meta.<key>:int_parsing",
            "address.zip_code:int_parsing",
            "address.<key>:extra_forbidden",
            "<key>:extra_forbidden",
        ):
            assert detail in warning, detail
        if not strict:
            (error_line,) = _lines(caplog, "Validation errors (non-strict")
            assert "errors=9 " in error_line
            assert "age:int_parsing" in error_line

    @pytest.mark.parametrize("kind", ["query", "form"])
    async def test_flat_sources_log_locations_and_types(
        self, caplog: pytest.LogCaptureFixture, kind: str
    ) -> None:
        """Query/form failures log declared fields + types; the extra key is masked."""
        caplog.set_level(logging.DEBUG)
        await _send(_app(kind, strict=True), kind)

        (warning,) = _lines(caplog, "AUTHZ validation_failed")
        assert "model=FlatSignup" in warning
        assert "errors=4 " in warning
        for detail in (
            "age:int_parsing",
            "nickname:string_too_short",
            "email:value_error",
            "<key>:extra_forbidden",
        ):
            assert detail in warning, detail


class _FakePgError(Exception):
    """Driver-shaped error (duck-types psycopg2's ``pgcode``/``diag``) embedding the sentinel."""

    def __init__(self) -> None:
        super().__init__(f"Key (email)=({SENTINEL}) already exists.")
        self.pgcode = "23505"
        self.diag = SimpleNamespace(
            sqlstate="23505",
            constraint_name="users_email_key",
            table_name="users",
            column_name="email",
        )


def _chained() -> BaseException:
    """An exception whose *cause* (not its own message) carries the sentinel."""
    try:
        try:
            raise ValueError(f"inner {SENTINEL}")
        except ValueError as inner:
            raise RuntimeError("outer failure") from inner
    except RuntimeError as outer:
        return outer


class TestHandlerExceptionLogs:
    """Exceptions raised inside the wrapped endpoint are logged type-only."""

    @pytest.mark.parametrize(
        ("raiser", "expected_type"),
        [
            (lambda: ValueError(f"cannot process {SENTINEL}"), "type=ValueError"),
            (lambda: KeyError(SENTINEL), "type=KeyError"),
            (_FakePgError, "type=test_validation_log_redaction._FakePgError"),
            (_chained, "type=RuntimeError"),
        ],
        ids=["value_error", "key_error", "driver_error", "chained_cause"],
    )
    @pytest.mark.parametrize("kind", ["json", "query", "form"])
    async def test_handler_exception_not_echoed(
        self,
        caplog: pytest.LogCaptureFixture,
        kind: str,
        raiser: Any,
        expected_type: str,
    ) -> None:
        """`except Exception` logs type/SQLSTATE only -- never `str(e)` or its cause chain."""
        caplog.set_level(logging.DEBUG)

        async def boom(data: Any) -> dict[str, str]:
            raise raiser()

        app = _mount(kind, Passes, boom)
        client = app.test_client()
        if kind == "json":
            resp = await client.post("/r", json={"x": 1})
        elif kind == "query":
            resp = await client.get("/r", query_string={"x": "1"})
        else:
            resp = await client.post("/r", form={"x": "1"})

        assert resp.status_code == 400
        _assert_no_sentinel(caplog)
        (line,) = _lines(caplog, "ERROR validation_exception", logging.ERROR)
        assert "endpoint=boom" in line
        assert expected_type in line
        if expected_type.endswith("_FakePgError"):
            assert "sqlstate=23505" in line
            assert "category=unique_violation" in line

    async def test_debug_traceback_is_frames_only(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The DEBUG diagnostic keeps the call path but drops exception text (and its causes)."""
        caplog.set_level(logging.DEBUG)

        async def boom(data: Any) -> dict[str, str]:
            raise _chained()

        await _mount("json", Passes, boom).test_client().post("/r", json={})

        _assert_no_sentinel(caplog)
        debug = [
            r.getMessage()
            for r in caplog.records
            if r.name == LOGGER_NAME
            and r.levelno == logging.DEBUG
            and "sanitized traceback" in r.getMessage()
        ]
        assert len(debug) == 1
        assert "Traceback (most recent call last):" in debug[0]
        assert "File " in debug[0]


class TestDescribeValidationErrors:
    """Unit coverage of the value-free renderer."""

    @staticmethod
    def _failure(model: type[BaseModel], data: dict[str, Any]) -> ValidationError:
        """Return the ValidationError for `data` (asserting it really fails)."""
        with pytest.raises(ValidationError) as excinfo:
            model.model_validate(data)
        return excinfo.value

    def test_output_is_exact_and_value_free(self) -> None:
        """Exact output for a known failure; input never present."""
        exc = self._failure(Signup, JSON_BODY)
        # Pydantic itself *does* carry the value -- proving the sentinel is in play.
        assert SENTINEL in repr(exc.errors())
        out = describe_validation_errors(exc, Signup)
        assert SENTINEL not in out
        assert out == (
            "errors=9 fields=age:int_parsing,nickname:string_too_short,email:value_error,"
            "ident:uuid_parsing,tags[]:int_parsing,meta.<key>:int_parsing,"
            "address.zip_code:int_parsing,address.<key>:extra_forbidden,<key>:extra_forbidden"
        )

    def test_without_model_every_named_segment_is_masked(self) -> None:
        """No model => nothing can be proven developer-declared, so all names are masked."""
        exc = self._failure(FlatSignup, {"age": "x", "nickname": "y", "email": "z"})
        out = describe_validation_errors(exc)
        assert (
            out
            == "errors=3 fields=<key>:int_parsing,<key>:string_too_short,<key>:value_error"
        )

    def test_model_level_failure_has_root_location(self) -> None:
        """A model validator (empty ``loc``) renders as ``<root>`` and its message is dropped."""
        exc = self._failure(Guarded, {"name": SENTINEL})
        assert SENTINEL in repr(exc.errors())
        out = describe_validation_errors(exc, Guarded)
        assert out == "errors=1 fields=<root>:value_error"

    def test_details_are_capped(self) -> None:
        """More than 20 failures render the first 20 plus an overflow count."""

        class Wide(BaseModel):
            """Model rejecting every extra key."""

            model_config = ConfigDict(extra="forbid")

        exc = self._failure(Wide, {f"{SENTINEL}-{i}": i for i in range(25)})
        out = describe_validation_errors(exc, Wide)
        assert out.startswith("errors=25 fields=")
        assert out.endswith(" +5 more")
        assert out.count("<key>:extra_forbidden") == 20
        assert SENTINEL not in out

    def test_aliases_are_declared_names(self) -> None:
        """Declared aliases are developer strings and stay readable."""

        class Aliased(BaseModel):
            """Model with an alias."""

            model_config = ConfigDict(populate_by_name=True)

            display_name: int = Field(alias="displayName")

        exc = self._failure(Aliased, {"displayName": SENTINEL})
        assert (
            describe_validation_errors(exc, Aliased)
            == "errors=1 fields=displayName:int_parsing"
        )

    def test_validate_data_helper_does_not_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """`validate_data` returns the errors to the caller and emits no log record at all."""
        caplog.set_level(logging.DEBUG)
        ok, result, errors = validate_data(
            FlatSignup, {"age": SENTINEL, "nickname": "n", "email": "e"}
        )
        assert ok is False
        assert result is None
        assert len(errors) == 3
        assert not [r for r in caplog.records if r.name == LOGGER_NAME]
