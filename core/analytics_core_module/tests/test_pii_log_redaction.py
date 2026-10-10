"""PII-in-logs regression tests for the analytics core module.

# regression: every service / route / job in analytics-core logged
# ``f"Failed to ...: {e}"`` (and returned ``error_response(str(e), 500)``).
# The analytics queries bind community and user data, and a DB driver's
# exception message embeds those *bound values* (psycopg2 ``DETAIL: Key
# (...)=(...)``, invalid-input echoes), so community data reached the log
# stream and the HTTP response body.

Each test drives a driver-shaped error whose message AND diagnostic detail
embed `SENTINEL` and asserts it is absent from everything logged (structured
line, plain format, raw record dict) and from the HTTP response, while the
exception type and SQLSTATE stay present so the line remains actionable.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import inspect
import logging
import typing
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

import app as analytics_app
from config import Config
from jobs import bot_score_calculator
from services import health_service
from services.analytics_service import AnalyticsService
from services.bad_actor_service import BadActorService
from services.bot_score_service import BotScoreService
from services.funnel_service import FunnelService
from services.health_service import HealthService
from services.log_safety import INTERNAL_ERROR_MESSAGE, log_failure
from services.metrics_service import MetricsService
from services.polling_service import PollingService
from services.retention_service import RetentionService

if TYPE_CHECKING:
    from conftest import Capture

SENTINEL = "SENTINEL-s3cr3t-community-data-9f2c1a7e"
MODULE_ROOT = Path(__file__).resolve().parent.parent

SERVICE_CLASSES = (
    AnalyticsService,
    BadActorService,
    BotScoreService,
    FunnelService,
    HealthService,
    MetricsService,
    PollingService,
    RetentionService,
)


class FakePgError(Exception):
    """Driver-shaped error: duck-types psycopg2 (`pgcode` + `diag`), embeds the sentinel."""

    def __init__(self) -> None:
        """Embed the sentinel in the message and in the diagnostic detail."""
        super().__init__(
            'duplicate key value violates unique constraint "analytics_config_pkey"\n'
            f"DETAIL:  Key (community_id)=({SENTINEL}) already exists."
        )
        self.pgcode = "23505"
        self.diag = SimpleNamespace(
            sqlstate="23505",
            constraint_name="analytics_config_pkey",
            table_name="analytics_config",
            column_name="community_id",
            message_detail=f"Key (community_id)=({SENTINEL})",
        )


class FailingDal:
    """DAL double whose every operation raises a sentinel-bearing driver error."""

    def executesql(self, *args: Any, **kwargs: Any) -> Any:
        """Raise the driver-shaped error."""
        raise FakePgError()

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        """Raise the driver-shaped error."""
        raise FakePgError()

    def commit(self, *args: Any, **kwargs: Any) -> Any:
        """Raise the driver-shaped error."""
        raise FakePgError()


class PoisonMapping(dict[str, Any]):
    """A mapping whose every read raises a ``KeyError`` that echoes the sentinel."""

    def __getitem__(self, key: str) -> Any:
        """Raise a KeyError carrying the sentinel (like a missing user-supplied key)."""
        raise KeyError(f"{SENTINEL}:{key}")

    def get(self, key: str, default: Any = None) -> Any:
        """Raise a KeyError carrying the sentinel."""
        raise KeyError(f"{SENTINEL}:{key}")


def _job() -> Any:
    """Return the (unannotated) batch-job module as `Any`."""
    return bot_score_calculator


def _build(cls: Any, dal: Any, logger: Any) -> Any:
    """Construct a (legacy, unannotated) service class against `dal` and `logger`."""
    return cls(dal, logger)


_VALID_ENUMS: dict[str, Any] = {
    "bucket_size": "1d",
    "cohort_period": "week",
    "funnel_name": next(iter(FunnelService.FUNNEL_STEPS)),
}


def _synthesize(param: inspect.Parameter) -> Any:
    """Build a plausible argument for `param`; free-text strings carry the sentinel."""
    if param.name in _VALID_ENUMS:
        return _VALID_ENUMS[param.name]
    if param.default is not inspect.Parameter.empty:
        return param.default
    ann = param.annotation
    origin = typing.get_origin(ann) or ann
    if origin is int:
        return 1
    if origin is float:
        return 1.0
    if origin is bool:
        return False
    if origin is str:
        return SENTINEL
    if origin is datetime:
        return datetime(2026, 1, 1)
    if origin is date:
        return date(2026, 1, 1)
    if origin is list:
        return [PoisonMapping()]
    return PoisonMapping()


def _logging_methods() -> list[tuple[type, str]]:
    """Enumerate every service method with an ``except Exception as ...`` handler.

    Selected by the handler (not by the fix) so the sweep covers -- and fails on --
    pre-fix code as well.
    """
    found: list[tuple[type, str]] = []
    for cls in SERVICE_CLASSES:
        for name, member in inspect.getmembers(cls, inspect.isfunction):
            if name.startswith("__"):
                continue
            if "except Exception as" in inspect.getsource(member):
                found.append((cls, name))
    return found


LOGGING_METHODS = _logging_methods()


def test_logging_method_denominator() -> None:
    """The parametrized sweep below must actually cover the module (no empty sweep)."""
    assert len(LOGGING_METHODS) >= 40, f"only {len(LOGGING_METHODS)} logging methods found"


@pytest.mark.parametrize(
    ("cls", "method"), LOGGING_METHODS, ids=[f"{c.__name__}.{m}" for c, m in LOGGING_METHODS]
)
def test_service_failure_never_logs_bound_values(
    capture: Capture, monkeypatch: pytest.MonkeyPatch, cls: type, method: str
) -> None:
    """A failing query is logged as type + SQLSTATE only -- never the driver message."""

    async def _enabled(*args: Any, **kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(health_service, "feature_enabled", _enabled)
    service = _build(cls, FailingDal(), capture.logger)
    if method == "calculate_health_score":
        # premium + feature-enabled so the outer try block (not an early return) is exercised
        async def _premium(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"is_premium": True}

        monkeypatch.setattr(service, "_get_config", _premium)
    func = getattr(service, method)
    kwargs = {
        name: _synthesize(param) for name, param in inspect.signature(func).parameters.items()
    }

    with contextlib.suppress(Exception):  # services re-raise after logging; only the logs matter
        result = func(**kwargs)
        if asyncio.iscoroutine(result):
            asyncio.run(result)

    errors = capture.error_records()
    assert errors, f"{cls.__name__}.{method} emitted no ERROR record"
    rendered = capture.rendered()
    assert SENTINEL not in rendered
    assert any("error" in getattr(r, "additional", {}) for r in errors)


def test_db_failure_log_stays_actionable(capture: Capture) -> None:
    """Type, SQLSTATE and category survive so on-call can still diagnose the failure."""
    service = _build(AnalyticsService, FailingDal(), capture.logger)
    with pytest.raises(FakePgError):
        asyncio.run(service.get_config(42))

    rendered = capture.rendered()
    assert "Failed to get analytics config" in rendered
    assert "sqlstate=23505" in rendered
    assert "category=unique_violation" in rendered
    assert "community_id=42" in rendered
    assert SENTINEL not in rendered


def test_sanitized_traceback_emitted_at_debug_without_message(capture: Capture) -> None:
    """DEBUG carries the frame-only traceback (call path), never the exception text."""
    service = _build(MetricsService, FailingDal(), capture.logger)
    with pytest.raises(FakePgError):
        asyncio.run(service.get_timeseries(7, "messages"))

    debug_text = "\n".join(
        r.getMessage() + repr(getattr(r, "additional", {}))
        for r in capture.collector.records
        if r.levelno == logging.DEBUG
    )
    assert "sanitized traceback" in debug_text
    assert "get_timeseries" in debug_text
    assert SENTINEL not in debug_text


def test_log_failure_stdlib_logger_branch(caplog: pytest.LogCaptureFixture) -> None:
    """A plain `logging.Logger` gets the same value-free description plus safe fields."""
    log = logging.getLogger("analytics-core-test.stdlib")
    with caplog.at_level(logging.DEBUG, logger=log.name):
        log_failure(log, "Platform analytics request failed", FakePgError(), endpoint="summary")

    assert "endpoint=summary" in caplog.text
    assert "sqlstate=23505" in caplog.text
    assert SENTINEL not in caplog.text


def test_log_failure_warning_level(capture: Capture) -> None:
    """Degraded-but-serving paths (e.g. credential load fallback) log at WARNING, value-free."""
    log_failure(capture.logger, "Failed to load credentials", FakePgError(), level=logging.WARNING)

    warnings = [r for r in capture.collector.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert not capture.error_records()
    assert "sqlstate=23505" in capture.rendered()
    assert SENTINEL not in capture.rendered()


def test_config_credential_load_failure_is_redacted(
    capture: Capture, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Config's DB-credential fallback warning must not echo the driver message."""
    redis_less = Config

    class FailingRedis:
        """Redis double whose pubsub() raises the sentinel-bearing error."""

        def pubsub(self) -> Any:
            """Raise inside the listener thread body."""
            raise FakePgError()

    monkeypatch.setattr(redis_less, "REDIS_URL", "redis://unused")
    with caplog.at_level(logging.DEBUG):
        thread = redis_less.start_credential_listener(FailingRedis())
        assert thread is not None
        thread.join(timeout=5)

    assert "Credential listener error" in caplog.text
    assert "sqlstate=23505" in caplog.text
    assert SENTINEL not in caplog.text


def test_log_failure_never_reads_exception_message(capture: Capture) -> None:
    """Even a non-DB exception whose message is the sentinel logs type only."""
    log_failure(capture.logger, "Something failed", ValueError(SENTINEL), community_id=3)

    rendered = capture.rendered()
    assert "type=ValueError" in rendered
    assert SENTINEL not in rendered


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("get", "/api/v1/analytics/5/basic", {}),
        ("get", f"/api/v1/analytics/5/metrics?metric_type={SENTINEL}", {}),
        ("get", f"/api/v1/analytics/5/poll?since={SENTINEL}", {}),
        ("get", "/api/v1/analytics/5/config", {}),
        ("put", "/api/v1/analytics/5/config", {"json": {"note": SENTINEL}}),
        ("post", "/api/v1/internal/events", {"json": {"events": [{"user": SENTINEL}]}}),
        ("post", "/api/v1/internal/aggregate", {"json": {"community_id": 5, "force": True}}),
        ("get", "/api/v1/analytics/5/bot-score", {}),
        ("post", "/api/v1/analytics/5/bot-score/calculate", {}),
        ("get", "/api/v1/analytics/5/suspected-bots", {}),
        (
            "put",
            "/api/v1/analytics/5/suspected-bots/9/review",
            {"json": {"is_false_positive": True}},
        ),
    ],
)
def test_route_failure_logs_and_responds_without_bound_values(
    capture: Capture,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    kwargs: dict[str, Any],
) -> None:
    """Routes log type + SQLSTATE and return a static 500 body, never exception text."""
    dal = FailingDal()
    monkeypatch.setattr(analytics_app, "logger", capture.logger)
    monkeypatch.setattr(Config, "SERVICE_API_KEY", "test-key")
    for attr, cls in (
        ("analytics_service", AnalyticsService),
        ("metrics_service", MetricsService),
        ("polling_service", PollingService),
        ("bot_score_service", BotScoreService),
    ):
        monkeypatch.setattr(analytics_app, attr, _build(cls, dal, capture.logger))

    async def call() -> tuple[int, str]:
        client = analytics_app.app.test_client()
        response = await getattr(client, method)(
            path, headers={"X-Service-Key": "test-key"}, **kwargs
        )
        return response.status_code, (await response.get_data(as_text=True))

    status, body = asyncio.run(call())

    assert status == 500, body
    assert SENTINEL not in body
    assert INTERNAL_ERROR_MESSAGE in body
    assert capture.error_records()
    assert SENTINEL not in capture.rendered()


@pytest.mark.parametrize(
    ("path", "endpoint"),
    [
        ("/api/v1/analytics/platform/summary", "get_platform_summary"),
        ("/api/v1/analytics/platform/reputation", "get_reputation_distribution"),
        ("/api/v1/analytics/platform/growth", "get_growth_trends"),
        ("/api/v1/analytics/platform/activity", "get_activity_breakdown"),
        ("/api/v1/analytics/platform/community-health", "get_community_health_summaries"),
        ("/api/v1/analytics/user/12/self", "get_user_self_stats"),
    ],
)
def test_blueprint_failure_is_logged_and_not_echoed(
    capture: Capture, monkeypatch: pytest.MonkeyPatch, path: str, endpoint: str
) -> None:
    """Blueprint handlers previously logged nothing and echoed ``str(e)`` to the caller."""
    from blueprints import platform_bp, user_bp
    from services.platform_stats_service import PlatformStatsService
    from services.user_stats_service import UserStatsService

    dal = FailingDal()
    monkeypatch.setattr(Config, "SERVICE_API_KEY", "test-key")
    monkeypatch.setattr(
        platform_bp, "platform_stats_service", _build(PlatformStatsService, dal, capture.logger)
    )
    monkeypatch.setattr(
        user_bp, "user_stats_service", _build(UserStatsService, dal, capture.logger)
    )
    # the blueprints log through logging.getLogger("waddlebot.<module>") -- the very logger
    # object the AAALogger fixture configured, so `capture` already sees their records

    async def call() -> tuple[int, str]:
        client = analytics_app.app.test_client()
        response = await client.get(path, headers={"X-Service-Key": "test-key"})
        return response.status_code, (await response.get_data(as_text=True))

    status, body = asyncio.run(call())

    assert status == 500, body
    assert SENTINEL not in body
    assert INTERNAL_ERROR_MESSAGE in body
    rendered = capture.rendered()
    assert f"endpoint={endpoint}" in rendered
    assert "sqlstate=23505" in rendered
    assert SENTINEL not in rendered


def test_job_gather_failure_logs_type_only(
    capture: Capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The batch job logs per-community failures (a returned exception) without its message."""
    monkeypatch.setattr(bot_score_calculator, "logger", capture.logger)
    # The job's completion call `logger.audit("msg", action=...)` collides with
    # AAALogger.audit's positional `action` (pre-existing, unrelated bug); no-op it so
    # the per-community failure path under test is what the assertions observe.
    monkeypatch.setattr(capture.logger, "audit", lambda *args, **kwargs: None)

    class OneCommunityDal(FailingDal):
        """Lists one active community, then fails every per-community query."""

        def executesql(self, query: str, *args: Any, **kwargs: Any) -> Any:
            """Return one community row for the listing query; raise otherwise."""
            if "FROM communities" in query:
                return [(5,)]
            raise FakePgError()

    monkeypatch.setattr(bot_score_calculator, "init_database", lambda _url: OneCommunityDal())

    summary = asyncio.run(_job().calculate_all_communities())

    assert summary["errors"] == 1
    rendered = capture.rendered()
    assert "Failed to calculate score for community" in rendered
    assert "community_id=5" in rendered
    assert SENTINEL not in rendered


def _exception_leaks(source: str) -> list[int]:
    """Return line numbers where an except-bound exception is interpolated into output.

    Flags f-strings / ``%`` formatting / ``str()`` / ``repr()`` / bare passing of the
    caught name (or an ``isinstance(x, Exception)`` result) inside a logger, ``print`` or
    ``error_response`` call. Passing the exception to ``log_failure`` is the sanctioned way.
    """
    tree = ast.parse(source)
    leaks: list[int] = []
    sinks = {
        "error",
        "warning",
        "info",
        "debug",
        "critical",
        "exception",
        "log",
        "print",
        "error_response",
        "jsonify",
        "system",
        "audit",
    }
    for handler in (n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler) and n.name):
        for call in (n for n in ast.walk(handler) if isinstance(n, ast.Call)):
            name = getattr(call.func, "attr", getattr(call.func, "id", ""))
            if name not in sinks:
                continue
            sanctioned = {
                id(arg)
                for inner in ast.walk(call)
                if isinstance(inner, ast.Call)
                and getattr(inner.func, "id", "") in {"log_failure", "describe_db_error"}
                for arg in ast.walk(inner)
            }
            for node in ast.walk(call):
                if (
                    isinstance(node, ast.Name)
                    and node.id == handler.name
                    and id(node) not in sanctioned
                ):
                    leaks.append(call.lineno)
                    break
    return leaks


def test_no_exception_interpolation_left_in_module() -> None:
    """Static guard: no except-bound exception reaches a log/print/response sink unsanitized."""
    files = [
        p for p in MODULE_ROOT.rglob("*.py") if "tests" not in p.parts and p.name != "log_safety.py"
    ]
    assert len(files) >= 15, f"scanned only {len(files)} files"
    offenders = {
        str(p.relative_to(MODULE_ROOT)): lines
        for p in files
        if (lines := _exception_leaks(p.read_bytes().decode()))
    }
    assert not offenders, f"exception interpolated into output: {offenders}"


def test_static_guard_detects_the_original_bug() -> None:
    """The guard itself must fail on the pre-fix shapes (a gate that cannot fail is no gate)."""
    bad = (
        "try:\n    x()\nexcept Exception as e:\n"
        "    logger.error(f'Failed: {e}', community_id=1)\n"
        "    return error_response(str(e), 500)\n"
    )
    assert _exception_leaks(bad) == [4, 5]
    good = "try:\n    x()\nexcept Exception as e:\n    log_failure(logger, 'Failed', e)\n"
    assert _exception_leaks(good) == []
