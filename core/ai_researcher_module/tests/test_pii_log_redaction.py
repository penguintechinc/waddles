"""ai_researcher_module must never write exception text (bound DB values) to its logs.

# regression: SECURITY (PII in logs). Handlers and services logged raw exceptions --
# ``f"Failed to store message: {e}"`` (the firehose INSERT of message content, username
# and platform_user_id), ``"...%s", exc``, ``exc_info=True``, ``error=str(e)`` -- and
# echoed ``str(e)`` back in 500 responses. A DB driver exception's message embeds the
# BOUND VALUES of the failing statement (``DETAIL: Key (email)=(a@b.c) already exists``,
# inlined INSERT text, ...), so chat message content, usernames and platform ids reached
# the log stream. SearXNG's HTTP error also logged the request URL, whose query string is
# the user's research question.

Every behavioural test drives a driver-shaped error whose message AND diagnostic detail
embed `SENTINEL` (a known secret) through the real code path, then asserts the sentinel
is absent from everything the logging system emitted -- the rendered message, the
standard-formatter output (which would include a traceback / ``exc_info`` rendering) and
the AAA structured-formatter output (which renders extra fields like ``error=``) -- while
the operation, exception type and SQLSTATE stay present, i.e. the line stays actionable.

`TestStaticGuard` is the module-wide net: `flask_core.exc_log_audit` must find zero unsafe
log calls across the whole package (it found 120+ before the fix).

Mutation check (executed, not narrated -- see the PR description): reverting a site to its
pre-fix shape (``{e}`` / ``exc_info=True`` / the raw URL) turns the matching test red.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from flask_core.auth import create_jwt_token
from flask_core.exc_log_audit import audit_paths
from flask_core.logging_config import StructuredFormatter

os.environ.setdefault("SECRET_KEY", "test-secret-key-for-ai-researcher-module-tests")

import app as ai_researcher_app_module  # noqa: E402
from services.anomaly_detector import AnomalyDetector  # noqa: E402
from services.rate_limiter import RateLimiter  # noqa: E402
from services.research_service import ResearchService  # noqa: E402
from services.searxng_service import SearXNGService  # noqa: E402

from config import Config  # noqa: E402
from services import bot_detection  # noqa: E402

SENTINEL = "SENTINEL-s3cr3t-msg-9f2c1a7e"
PII_USER = "victim_username_77"

_DETAIL = f"Key (email)=({SENTINEL}) already exists."
_MESSAGE = (
    'duplicate key value violates unique constraint "users_email_key"\n'
    f"DETAIL:  {_DETAIL}\n"
    f'invalid input syntax for type uuid: "{PII_USER}"'
)
_SECRETS = (SENTINEL, PII_USER)

MODULE_ROOT = Path(__file__).resolve().parent.parent
SECRET = os.environ["SECRET_KEY"]


class FakePgError(Exception):
    """Driver-shaped error: duck-types psycopg2 (`pgcode` + `diag`), embeds the sentinel."""

    def __init__(self, message: str = _MESSAGE, pgcode: str = "23505") -> None:
        """Build the error with a sentinel-bearing message and psycopg2-style diagnostics."""
        super().__init__(message)
        self.pgcode = pgcode
        self.diag = SimpleNamespace(
            sqlstate=pgcode,
            constraint_name="users_email_key",
            table_name="users",
            column_name="email",
            message_detail=_DETAIL,
            message_primary=message,
        )


class LogCapture(logging.Handler):
    """Collects every record and renders it the ways a real sink would."""

    def __init__(self) -> None:
        """Create an empty capture that accepts every level."""
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        self._plain = logging.Formatter("%(levelname)s %(name)s %(message)s")
        self._structured = StructuredFormatter("pii-redaction-test", "1.0.0")

    def emit(self, record: logging.LogRecord) -> None:
        """Store the record."""
        self.records.append(record)

    def rendered(self) -> str:
        """Everything a sink could write: plain (incl. tracebacks) + AAA structured output."""
        chunks: list[str] = []
        for record in self.records:
            chunks.append(self._plain.format(record))
            chunks.append(self._structured.format(record))
        return "\n".join(chunks)

    def assert_clean(self) -> None:
        """Fail if any secret reached the log stream (and that something was logged at all)."""
        assert self.records, "nothing was logged -- the test did not exercise a logging path"
        text = self.rendered()
        for secret in _SECRETS:
            assert secret not in text, f"{secret!r} leaked into logs:\n{text}"


@pytest.fixture
def capture() -> Iterator[LogCapture]:
    """Capture std loggers (propagate to root) and the AAA loggers (propagate=False)."""
    handler = LogCapture()
    root = logging.getLogger()
    previous = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    aaa_loggers = [ai_researcher_app_module.logger.logger, bot_detection.logger.logger]
    previous_levels = [lg.level for lg in aaa_loggers]
    for lg in aaa_loggers:
        lg.setLevel(logging.DEBUG)
        lg.addHandler(handler)
    yield handler
    for lg, level in zip(aaa_loggers, previous_levels, strict=True):
        lg.removeHandler(handler)
        lg.setLevel(level)
    root.removeHandler(handler)
    root.setLevel(previous)


def _token() -> str:
    return create_jwt_token(
        user_id="1",
        username="alice",
        email="alice@example.com",
        roles=[],
        secret_key=SECRET,
        tenant="acme-corp",
    )


@pytest.fixture
def failing_dal(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Install a module-level DAL whose every `execute` raises the driver-shaped error."""
    dal = SimpleNamespace(execute=AsyncMock(side_effect=FakePgError()))
    monkeypatch.setattr(ai_researcher_app_module, "dal", dal)
    return dal


class TestRoutes:
    """HTTP routes: logs AND 500 bodies carry no exception text."""

    async def test_firehose_store_failure_logs_no_bound_values(
        self, failing_dal: SimpleNamespace, capture: LogCapture
    ) -> None:
        """The message INSERT (content/username/platform_user_id) failing must not leak them."""
        client = ai_researcher_app_module.app.test_client()
        response = await client.post(
            "/api/v1/researcher/messages/firehose",
            json={
                "community_id": 1,
                "platform": "twitch",
                "platform_user_id": "12345",
                "username": PII_USER,
                "message": f"my secret is {SENTINEL}",
            },
            headers={"X-Service-Key": Config.SERVICE_API_KEY},
        )
        assert response.status_code == 200
        assert (await response.get_json())["data"]["processed"] == 0
        capture.assert_clean()
        text = capture.rendered()
        assert "Failed to store message" in text
        assert "FakePgError" in text
        assert "sqlstate=23505" in text

    @pytest.mark.parametrize(
        "path",
        ["/api/v1/researcher/context/1", "/api/v1/admin/1/ai-researcher/config"],
        ids=["context", "config"],
    )
    async def test_500_route_neither_logs_nor_returns_exception_text(
        self, path: str, failing_dal: SimpleNamespace, capture: LogCapture
    ) -> None:
        """`logger.error(f"...{e}")` and `error_response(f"...{str(e)}")` both leaked."""
        client = ai_researcher_app_module.app.test_client()
        response = await client.get(path, headers={"Authorization": f"Bearer {_token()}"})
        body = await response.get_data(as_text=True)
        assert response.status_code == 500
        for secret in _SECRETS:
            assert secret not in body
        assert "type=" not in body  # the response is a static message, not even the type
        capture.assert_clean()
        assert "FakePgError" in capture.rendered()


class _BoomRedis:
    """Redis stand-in: every awaited command raises an error that embeds the sentinel."""

    def __getattr__(self, name: str) -> Any:
        async def boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError(f"redis {name} failed for key {SENTINEL}")

        return boom


class TestServices:
    """Service layer: log lines and caller-facing `error` fields stay value-free."""

    async def test_rate_limiter_redis_failures(self, capture: LogCapture) -> None:
        """`Redis ... failed: {e}` (decrement/delete/get_usage) must not log the message."""
        limiter = RateLimiter(redis_client=_BoomRedis())
        assert await limiter._decrement_redis("k") == 0
        assert await limiter._delete_redis("k") is False
        usage = await limiter.get_usage(1, "u1")
        capture.assert_clean()
        assert SENTINEL not in repr(usage)
        assert "RuntimeError" in capture.rendered()

    @pytest.mark.parametrize("method", ["_get_from_cache", "_save_to_cache"])
    async def test_research_service_cache_errors(self, method: str, capture: LogCapture) -> None:
        """Cache get/save errors (Redis) are logged by type only."""
        service = ResearchService(None, None, None, None, _BoomRedis())
        if method == "_get_from_cache":
            await service._get_from_cache("k")
        else:
            await service._save_to_cache("k", {"v": 1}, 60)
        capture.assert_clean()
        assert "RuntimeError" in capture.rendered()

    async def test_research_service_ai_error_is_value_free(self, capture: LogCapture) -> None:
        """`'error': str(e)` flowed back to the caller; `exc_info=True` rendered the text."""
        provider = SimpleNamespace(generate=AsyncMock(side_effect=FakePgError()))
        service = ResearchService(provider, None, None, None, None)
        result = await service._generate_ai_response("sys", "user", 1, "u1", "research")
        assert result["success"] is False
        assert SENTINEL not in repr(result)
        capture.assert_clean()

    async def test_anomaly_detector_failure(self, capture: LogCapture) -> None:
        """Inner and outer failure paths: logs and `AnomalyResult.error` stay value-free."""
        dal = SimpleNamespace(execute=AsyncMock(side_effect=FakePgError()))
        detector = AnomalyDetector(dal)
        assert await detector.get_recent_anomalies(1) == []
        detector._detect_activity_spikes = AsyncMock(side_effect=FakePgError())  # type: ignore[method-assign]  # noqa: SLF001
        result = await detector.detect_anomalies(1, check_types=["activity"])
        assert result.success is False
        assert SENTINEL not in repr(result.to_dict())
        capture.assert_clean()
        assert "sqlstate=23505" in capture.rendered()

    async def test_bot_detection_structured_error_field(self, capture: LogCapture) -> None:
        """The AAA logger renders `error=` kwargs -- it must carry the description only."""
        db = SimpleNamespace(execute=AsyncMock(side_effect=FakePgError()))
        service = bot_detection.BotDetectionService(db)
        with pytest.raises(FakePgError):
            await service.get_at_risk_users(1)
        capture.assert_clean()
        assert "error=type=" in capture.rendered()

    @pytest.mark.parametrize(
        "behavior",
        ["http-500", "connect-error"],
    )
    async def test_searxng_logs_neither_query_nor_exception_text(
        self, behavior: str, capture: LogCapture
    ) -> None:
        """The request URL embeds `q=<user's question>`; the error text embeds values."""

        def handler(request: httpx.Request) -> httpx.Response:
            if behavior == "connect-error":
                raise httpx.ConnectError(f"cannot reach {request.url}")
            return httpx.Response(500, request=request)

        service = SearXNGService("http://searxng.invalid:8080")
        service._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: SLF001
        result = await service.search(f"how to {SENTINEL}")
        assert result.results == []
        capture.assert_clean()
        text = capture.rendered()
        assert "SearXNG" in text
        if behavior == "http-500":
            assert "status=500" in text
        else:
            assert "ConnectError" in text


class TestStaticGuard:
    """Module-wide net: no log call may interpolate / render an exception."""

    def test_no_unsafe_exception_logging_anywhere_in_the_module(self) -> None:
        """Zero findings, with a real denominator (a mis-pointed scan must not pass)."""
        report = audit_paths([MODULE_ROOT])
        assert report.files_examined >= 15, report.files_examined
        assert report.log_calls_examined >= 100, report.log_calls_examined
        assert not report.findings, "\n".join(f.render() for f in report.findings)
