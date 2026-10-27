"""PII-in-logs regression tests for the shared ``libs/`` consumers.

# regression: calendar_sync, presence, platform_receiver, coordination_client and
# module_sdk logged ``f"...{exc}"`` / ``str(exc)`` / ``exc_info=True``. Those
# exception messages carry request URLs (calendar IDs are the owner's e-mail),
# bound SQL values, remote response bodies and payload fragments, so user data
# reached the log stream. Logs now carry the exception *type* (and HTTP status)
# only.

Two layers: (1) behavioural -- drive a failure whose message embeds `SENTINEL`
through the real code path and assert it is absent from every log record while
the type stays present; (2) a taint-aware AST guard over all of ``libs/`` that
fails if an except-bound exception (or a string derived from it) reaches a log /
print sink, so a new leak cannot slip in unnoticed.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from libs.calendar_sync.base import CalendarProviderBase, describe_error  # noqa: E402
from libs.calendar_sync.providers.google import GoogleCalendarProvider  # noqa: E402
from libs.calendar_sync.sync_engine import CalendarSyncEngine  # noqa: E402
from libs.module_sdk.adapters.webhook_adapter import WebhookAdapter  # noqa: E402
from libs.module_sdk.security.scoped_tokens import ScopedTokenService  # noqa: E402
from libs.platform_receiver.base import PlatformReceiverBase  # noqa: E402
from libs.presence.sync_engine import PresenceSyncEngine  # noqa: E402

SENTINEL = "SENTINEL-user-data-victim.person@example.invalid"
LIBS = REPO_ROOT / "libs"


def _rendered(caplog: pytest.LogCaptureFixture) -> str:
    """Everything logged: formatted line, message, raw args and the record dict."""
    formatter = logging.Formatter()
    return "\n".join(
        f"{formatter.format(r)}|{r.getMessage()}|{r.args!r}|{r.__dict__!r}" for r in caplog.records
    )


class SentinelError(Exception):
    """Generic exception whose message carries the sentinel (like a DB / parser error)."""

    def __init__(self) -> None:
        """Embed the sentinel in the message."""
        super().__init__(f"boom: Key (email)=({SENTINEL}) already exists")


def _status_error() -> httpx.HTTPStatusError:
    """An httpx status error whose str() is the request URL containing the sentinel."""
    request = httpx.Request("GET", f"https://www.googleapis.com/calendar/v3/calendars/{SENTINEL}")
    return httpx.HTTPStatusError(
        f"Client error '404 Not Found' for url '{request.url}'",
        request=request,
        response=httpx.Response(404, request=request),
    )


def test_describe_error_is_type_and_status_only() -> None:
    """`describe_error` never reads the message; HTTP status is added when present."""
    assert describe_error(SentinelError()) == "type=SentinelError"
    assert describe_error(_status_error()) == "type=HTTPStatusError status=404"
    assert SENTINEL not in describe_error(SentinelError())
    assert SENTINEL not in describe_error(_status_error())


class _Google(GoogleCalendarProvider):
    """Concrete provider for exercising the shared `_log_error` choke point."""


@pytest.mark.parametrize("exc_factory", [SentinelError, _status_error], ids=["generic", "httpx"])
def test_calendar_provider_log_error_is_redacted(
    caplog: pytest.LogCaptureFixture, exc_factory: Any
) -> None:
    """Every provider failure funnels through `_log_error`; it must log type/status only."""
    provider = _Google({"access_token": "x"})
    with caplog.at_level(logging.DEBUG):
        provider._log_error("list_calendars", exc_factory())

    text = _rendered(caplog)
    assert SENTINEL not in text
    assert "list_calendars failed" in text
    assert "type=" in text


def test_calendar_providers_route_failures_through_log_error() -> None:
    """The provider subclasses must not have private (leaky) logging beside `_log_error`."""
    assert issubclass(GoogleCalendarProvider, CalendarProviderBase)
    offenders = _guard_offenders(LIBS / "calendar_sync")
    assert offenders == {}, offenders


class _FailingDal:
    """DAL double whose execute() raises a sentinel-bearing error."""

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        """Raise the sentinel error."""
        raise SentinelError()


def test_sync_engine_db_failure_is_redacted(caplog: pytest.LogCaptureFixture) -> None:
    """Sync-map persistence failures log type only (bound values are in the message)."""
    engine = CalendarSyncEngine(_Google({"access_token": "x"}), _FailingDal())
    with caplog.at_level(logging.DEBUG):
        asyncio.run(engine._update_sync_map("u1", "cal", "wb1", "pv1", "tok"))
        assert asyncio.run(engine._get_stored_sync_token("u1", "cal")) is None

    text = _rendered(caplog)
    assert SENTINEL not in text
    assert "_update_sync_map failed: type=SentinelError" in text
    assert "_get_stored_sync_token failed: type=SentinelError" in text


class _RaisingProvider:
    """Presence provider double that raises a sentinel-bearing error on push."""

    async def push_presence(self, user_id: str, status: str) -> bool:
        """Raise the sentinel error."""
        raise SentinelError()


def test_presence_fan_out_failure_is_redacted(caplog: pytest.LogCaptureFixture) -> None:
    """Fan-out errors are logged and returned to the caller without exception text."""
    from libs.presence.schema import PUSH_CAPABLE_PLATFORMS

    target = sorted(PUSH_CAPABLE_PLATFORMS)[0]
    source = next(p for p in sorted(PUSH_CAPABLE_PLATFORMS) if p != target)
    no_store: Any = None  # _fan_out never touches the store
    engine = PresenceSyncEngine(state_store=no_store, providers={target: _RaisingProvider()})
    with caplog.at_level(logging.DEBUG):
        fanned, errors = asyncio.run(engine._fan_out("user-uuid", source, "online"))

    assert fanned == []
    assert errors == [f"Fan-out error to platform={target}: SentinelError"]
    assert SENTINEL not in _rendered(caplog)
    assert "error=SentinelError" in _rendered(caplog)


class _Receiver(PlatformReceiverBase):
    """Minimal concrete receiver."""

    PLATFORM = "testplat"

    async def start(self) -> None:
        """No-op."""

    async def stop(self) -> None:
        """No-op."""


def test_platform_dispatch_failure_and_username_not_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """dispatch() logs neither the exception text nor the raw username, nor returns the text."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused {SENTINEL}", request=request)

    receiver = _Receiver("http://router.test")
    receiver._http_session = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    event = {"message_type": "chat", "username": SENTINEL}
    with caplog.at_level(logging.DEBUG):
        result = asyncio.run(receiver.dispatch(event))

    assert result == {"success": False, "error": "Router dispatch failed: ConnectError"}
    assert SENTINEL not in _rendered(caplog)
    assert "Router dispatch failed: ConnectError" in _rendered(caplog)


def test_platform_dispatch_success_debug_log_has_no_username(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The DEBUG 'Dispatched ...' line used to carry the raw username."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True})

    receiver = _Receiver("http://router.test")
    receiver._http_session = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with caplog.at_level(logging.DEBUG):
        asyncio.run(receiver.dispatch({"message_type": "chat", "username": SENTINEL}))

    text = _rendered(caplog)
    assert SENTINEL not in text
    assert "Dispatched chat" in text


def test_scoped_token_failures_are_redacted(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Token generate/validate failures log type only."""
    import jwt

    service = ScopedTokenService("k" * 40)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise SentinelError()

    monkeypatch.setattr(jwt, "encode", boom)
    monkeypatch.setattr(jwt, "decode", boom)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(SentinelError):
            service.generate_token("community-1", "mod", ["chat:read"])
        assert service.validate_token("anything") is None

    text = _rendered(caplog)
    assert SENTINEL not in text
    assert "Failed to generate token: SentinelError" in text
    assert "Unexpected token validation error: SentinelError" in text


def test_scoped_token_db_failure_is_redacted(caplog: pytest.LogCaptureFixture) -> None:
    """DB-backed scope writes log type only (SQL bound values are in the driver message)."""
    from types import SimpleNamespace

    class RaisingDal:
        """DAL double whose select raises the sentinel-bearing driver error."""

        module_scopes = SimpleNamespace(community_id=1, module_name=2, scope=3)

        async def select_async(self, query: Any) -> Any:
            """Raise the sentinel error."""
            raise SentinelError()

    service = ScopedTokenService("k" * 40, dal=RaisingDal())
    with caplog.at_level(logging.DEBUG):
        granted = asyncio.run(service.grant_scope_async("community-1", "mod", "chat:read", "u1"))

    assert granted is False
    text = _rendered(caplog)
    assert SENTINEL not in text
    assert "Failed to grant scope: SentinelError" in text


def test_webhook_adapter_failure_never_logs_exception_text(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The adapter returns the error to its caller but logs only the exception type."""
    from libs.module_sdk.base import ExecuteRequest

    async def post(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> httpx.Response:
        raise httpx.ConnectError(f"refused {SENTINEL}")

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    adapter = WebhookAdapter(
        webhook_url="https://hooks.example.test/x", secret_key="s" * 40, module_name="mod"
    )
    request = ExecuteRequest(
        command="ping",
        args=[SENTINEL],
        user_id="u1",
        entity_id="e1",
        community_id="c1",
        session_id="s1",
        platform="twitch",
    )
    with caplog.at_level(logging.DEBUG):
        response = adapter.execute(request)

    assert response.success is False
    assert SENTINEL not in _rendered(caplog)
    assert "ConnectError" in _rendered(caplog)


SINKS = {
    "error", "warning", "warn", "info", "debug", "critical", "exception", "log", "print",
    "error_response", "jsonify", "system", "audit",
}  # fmt: skip
SAFE_CALLS = {"type", "describe_error", "describe_db_error", "log_failure"}
SKIP_PARTS = {"flask_core", "tests", "__pycache__"}

# Known, documented leftovers -- NOT fixed by this change. Each entry must still be
# flagged (a stale exemption fails the guard so it gets removed when fixed).
EXEMPT: dict[str, str] = {
    "botConfig.py": "v1 legacy copy (imports WaddlebotLibs.*, not deployed)",
    "botDBC.py": "v1 legacy copy (imports WaddlebotLibs.*, not deployed)",
    "botMatterbridgeHelpers.py": "v1 legacy copy (imports WaddlebotLibs.*, not deployed)",
    "module_sdk/security/example_usage.py": "demo script printing its own validation errors",
    "waddle_transports/waddle_transports/community_credentials.py": (
        "credential domain; owned by the separate credential PII-logs change"
    ),
}


def _sanctioned(node: ast.AST) -> set[int]:
    """Return ids of nodes nested in a SAFE_CALLS call (e.g. ``type(exc).__name__``)."""
    out: set[int] = set()
    for inner in ast.walk(node):
        if isinstance(inner, ast.Call) and getattr(inner.func, "id", "") in SAFE_CALLS:
            out |= {id(x) for x in ast.walk(inner)}
    return out


def _names(node: ast.AST, skip: set[int]) -> set[str]:
    """Loaded names in `node` outside sanctioned calls."""
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and id(n) not in skip}


def _leaks(source: str) -> list[int]:
    """Line numbers where an except-bound exception (or a string derived from it) is logged."""
    tree = ast.parse(source)
    lines: list[int] = []
    for handler in (n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler) and n.name):
        tainted = {handler.name}
        for stmt in ast.walk(handler):
            if (
                isinstance(stmt, ast.Assign)
                and _names(stmt.value, _sanctioned(stmt.value)) & tainted
            ):
                tainted |= {t.id for t in stmt.targets if isinstance(t, ast.Name)}
        for call in (n for n in ast.walk(handler) if isinstance(n, ast.Call)):
            name = getattr(call.func, "attr", getattr(call.func, "id", ""))
            if name not in SINKS:
                continue
            exc_info = any(
                kw.arg == "exc_info"
                and not (isinstance(kw.value, ast.Constant) and kw.value.value is False)
                for kw in call.keywords
            )
            if exc_info or _names(call, _sanctioned(call)) & tainted:
                lines.append(call.lineno)
    return sorted(set(lines))


def _guard_offenders(root: Path) -> dict[str, list[int]]:
    """Map relative path -> leak lines for every non-exempt file under `root`."""
    found: dict[str, list[int]] = {}
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(LIBS).as_posix()
        if SKIP_PARTS & set(path.relative_to(LIBS).parts) or rel in EXEMPT:
            continue
        if leaks := _leaks(path.read_bytes().decode()):
            found[rel] = leaks
    return found


def test_no_exception_text_reaches_logs_anywhere_in_libs() -> None:
    """Static guard over all of libs/ (flask_core has its own suite): no tainted log sinks."""
    scanned = [p for p in LIBS.rglob("*.py") if not SKIP_PARTS & set(p.relative_to(LIBS).parts)]
    assert len(scanned) >= 60, f"scanned only {len(scanned)} files -- wrong root?"
    assert _guard_offenders(LIBS) == {}


@pytest.mark.parametrize("rel", sorted(EXEMPT))
def test_exemptions_are_still_needed(rel: str) -> None:
    """A fixed file must be removed from EXEMPT (keeps the exemption list honest)."""
    assert _leaks((LIBS / rel).read_bytes().decode()), f"{rel} is clean now -- drop its exemption"


def test_guard_detects_the_original_bug_shapes() -> None:
    """The guard itself must fail on the pre-fix shapes (a gate that cannot fail is no gate)."""
    direct = "try:\n    x()\nexcept Exception as e:\n    logger.error(f'bad: {e}')\n"
    via_var = (
        "try:\n    x()\nexcept Exception as e:\n"
        "    error_msg = f'bad: {str(e)}'\n    logger.error(error_msg)\n"
    )
    exc_info = "try:\n    x()\nexcept Exception:\n    pass\n"
    exc_info_true = (
        "try:\n    x()\nexcept Exception as e:\n    logger.error('bad', exc_info=True)\n"
    )
    safe = "try:\n    x()\nexcept Exception as e:\n    logger.error('bad: %s', type(e).__name__)\n"
    assert _leaks(direct) == [4]
    assert _leaks(via_var) == [5]
    assert _leaks(exc_info_true) == [4]
    assert _leaks(exc_info) == []
    assert _leaks(safe) == []
