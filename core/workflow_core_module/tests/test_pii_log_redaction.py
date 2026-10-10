"""workflow_core_module must never write exception text (bound DB values) to its logs.

# regression: SECURITY (PII in logs). The module logged raw exceptions --
# ``f"...{str(e)}"``, ``"...%s", e``, ``exc_info=True``, ``logger.exception()`` and the
# three blueprints' ``@errorhandler(500)`` (``f"...{str(error)}", exc_info=True``). A DB
# driver exception's message embeds the BOUND VALUES of the failing statement
# (``DETAIL: Key (email)=(a@b.c) already exists``, inlined INSERT text, ...), so message
# content, usernames and platform ids reached the log stream.

Every behavioural test drives a driver-shaped error whose message AND diagnostic detail
embed `SENTINEL` (a known secret) through the real code path, then asserts the sentinel
is absent from everything the logging system emitted -- the rendered message, the
standard-formatter output (which would include a traceback / ``exc_info`` rendering) and
the AAA structured-formatter output (which renders extra fields like ``error=``) -- while
the operation, exception type and SQLSTATE stay present, i.e. the line stays actionable.

`TestStaticGuard` is the module-wide net: `flask_core.exc_log_audit` must find zero unsafe
log calls across the whole package (it found 268 before the fix).

Mutation check (executed, not narrated -- see the PR description): reverting a site to its
pre-fix shape (``{e}`` / ``exc_info=True``) turns the matching test red.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from controllers import execution_api, webhook_api, workflow_api
from controllers.error_logging import log_internal_error
from flask_core.exc_log_audit import audit_paths
from flask_core.logging_config import AAALogger, StructuredFormatter
from quart import Quart
from services.permission_service import PermissionService
from services.webhook_executor import RetryPolicy, WebhookExecutor
from services.workflow_engine import WorkflowEngineException, WorkflowTimeoutException
from services.workflow_service import WorkflowService, WorkflowServiceException
from werkzeug.exceptions import InternalServerError

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
    """Capture std-library log records (controllers use module loggers that propagate)."""
    handler = LogCapture()
    root = logging.getLogger()
    previous = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    yield handler
    root.removeHandler(handler)
    root.setLevel(previous)


@pytest.fixture
def aaa_logger(tmp_path: Path, capture: LogCapture) -> AAALogger:
    """The real production logger type (AAALogger, propagate=False) wired to `capture`."""
    aaa = AAALogger("pii-redaction-test", "1.0.0", log_level="DEBUG", log_dir=str(tmp_path))
    aaa.logger.addHandler(capture)
    return aaa


class TestInternalErrorHandlers:
    """The three blueprints' `@errorhandler(500)` log an error_id + type only."""

    @pytest.mark.parametrize(
        "module", [execution_api, webhook_api, workflow_api], ids=lambda m: m.__name__
    )
    @pytest.mark.parametrize("wrapped", [True, False], ids=["wrapped-original", "bare-exception"])
    async def test_500_handler_never_logs_exception_text(
        self, module: Any, wrapped: bool, capture: LogCapture
    ) -> None:
        """Handler runs inside the live exception (exc_info would render the sentinel)."""
        app = Quart(__name__)
        try:
            raise FakePgError()
        except FakePgError as exc:
            error: BaseException = InternalServerError(original_exception=exc) if wrapped else exc
            async with app.app_context():
                response, status = await module.internal_error(error)
        body = await response.get_data(as_text=True)

        assert status == 500
        capture.assert_clean()
        for secret in _SECRETS:
            assert secret not in body
        text = capture.rendered()
        assert "error_id=" in text
        assert "FakePgError" in text
        if wrapped:
            assert "sqlstate=23505" in text
        logged_id = re.search(r"error_id=([0-9a-f]{32})", text)
        assert logged_id is not None
        assert json.loads(body)["error"]["details"]["error_id"] == logged_id.group(1)

    async def test_log_internal_error_without_exception(self, capture: LogCapture) -> None:
        """A handler invoked without any exception still logs an id and a placeholder type."""
        first = log_internal_error(logging.getLogger("t"), None)
        second = log_internal_error(logging.getLogger("t"), None)
        assert first != second
        assert "type=unknown" in capture.rendered()


class TestErrorDecorators:
    """The per-blueprint error decorators log type-only for every exception branch."""

    @pytest.mark.parametrize(
        ("decorator", "exc"),
        [
            (execution_api.handle_execution_errors, FakePgError()),
            (execution_api.handle_execution_errors, WorkflowEngineException(f"x {SENTINEL}")),
            (execution_api.handle_execution_errors, WorkflowTimeoutException(f"x {SENTINEL}")),
            (execution_api.handle_execution_errors, PermissionError(f"x {SENTINEL}")),
            (webhook_api.handle_webhook_errors, FakePgError()),
            (workflow_api.handle_workflow_errors, FakePgError()),
        ],
        ids=[
            "execution-generic",
            "execution-engine",
            "execution-timeout",
            "execution-permission",
            "webhook-generic",
            "workflow-generic",
        ],
    )
    async def test_decorator_logs_type_only(
        self, decorator: Any, exc: BaseException, capture: LogCapture
    ) -> None:
        """The generic branch used `exc_info=True`; the others interpolated `str(e)`."""

        @decorator
        async def boom() -> None:
            raise exc

        app = Quart(__name__)
        async with app.app_context():
            await boom()
        capture.assert_clean()
        assert type(exc).__name__ in capture.rendered()


def _workflow_service(logger: AAALogger) -> tuple[WorkflowService, MagicMock]:
    """A WorkflowService over a fake sync DAL, with the production AAALogger injected."""
    dal = MagicMock()
    license_service = MagicMock()
    license_service.validate_workflow_creation = AsyncMock()
    permission_service = MagicMock()
    permission_service.check_permission = AsyncMock(return_value=True)
    permission_service.list_workflows_for_user = AsyncMock(return_value=["wf-1"])
    svc = WorkflowService(
        dal=dal,
        license_service=license_service,
        permission_service=permission_service,
        validation_service=MagicMock(),
        logger_instance=logger,
    )
    return svc, dal


class TestServices:
    """Service-layer wrappers log type-only and re-raise without driver text."""

    @pytest.mark.parametrize(
        "call",
        [
            lambda s: s.create_workflow({"name": "x"}, 1, 2, 3),
            lambda s: s.get_workflow("wf-1", user_id=1),
            lambda s: s.update_workflow("wf-1", {"name": "x"}, user_id=1),
            lambda s: s.delete_workflow("wf-1", user_id=1),
            lambda s: s.list_workflows(entity_id=1, user_id=1),
        ],
        ids=["create", "get", "update", "delete", "list"],
    )
    async def test_workflow_service_db_failure(
        self, call: Any, aaa_logger: AAALogger, capture: LogCapture
    ) -> None:
        """Logs AND the re-raised WorkflowServiceException carry no driver text."""
        svc, dal = _workflow_service(aaa_logger)
        dal.executesql.side_effect = FakePgError()
        with pytest.raises(WorkflowServiceException) as raised:
            await call(svc)
        capture.assert_clean()
        for secret in _SECRETS:
            assert secret not in str(raised.value)
            assert secret not in raised.value.message
        assert "FakePgError" in capture.rendered()
        assert "sqlstate=23505" in capture.rendered()

    async def test_workflow_service_keeps_authored_messages(self, aaa_logger: AAALogger) -> None:
        """The module's own authored exception text still reaches the client (not erased)."""
        svc, _ = _workflow_service(aaa_logger)
        with pytest.raises(WorkflowServiceException, match="No valid fields"):
            await svc.update_workflow("wf-1", {}, user_id=1)

    async def test_permission_service_grant_failure(
        self, aaa_logger: AAALogger, capture: LogCapture
    ) -> None:
        """`error_msg` is logged AND returned in GrantResult -- both stay value-free."""
        dal = MagicMock()
        dal.executesql.side_effect = FakePgError()
        svc = PermissionService(dal=dal, logger=aaa_logger)
        result = await svc.grant_permission("wf-1", "user", 1, {"can_view": True})
        assert result.success is False
        capture.assert_clean()
        for secret in _SECRETS:
            assert secret not in (result.error or "")
            assert secret not in result.message

    async def test_permission_service_check_failure(
        self, aaa_logger: AAALogger, capture: LogCapture
    ) -> None:
        """A failing permission lookup is logged without the driver message."""
        dal = MagicMock()
        dal.executesql.side_effect = FakePgError()
        svc = PermissionService(dal=dal, logger=aaa_logger)
        await svc.check_permission("wf-1", 1, "can_view")
        capture.assert_clean()

    async def test_webhook_executor_request_error(self, capture: LogCapture) -> None:
        """An httpx error whose text embeds a value is logged by type only on retry."""
        error = httpx.ConnectError(f"cannot reach host for {SENTINEL}")
        responses: list[Any] = [error, error]

        class _Client:
            def __init__(self, **kwargs: Any) -> None:
                self._responses = responses

            async def __aenter__(self) -> _Client:
                return self

            async def __aexit__(self, *exc: Any) -> None:
                return None

            async def request(self, *args: Any, **kwargs: Any) -> httpx.Response:
                raise self._responses.pop(0)

        executor = WebhookExecutor(retry_policy=RetryPolicy(max_retries=1, initial_delay=0.001))
        with patch("services.webhook_executor.httpx.AsyncClient", _Client):
            result = await executor.execute(url="https://example.invalid/hook", method="POST")
        assert result["success"] is False
        capture.assert_clean()
        assert "ConnectError" in capture.rendered()


class TestHttpxUrlLogging:
    """httpx logs the full request URL at INFO -- webhook URLs carry tokens in the query."""

    async def test_webhook_url_query_is_not_logged(self, capture: LogCapture) -> None:
        """A successful call must not leave `?token=<secret>` in the log stream."""
        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
        executor = WebhookExecutor(retry_policy=RetryPolicy(max_retries=0))
        with patch(
            "services.webhook_executor.httpx.AsyncClient",
            lambda **kwargs: real_client(transport=transport, **kwargs),
        ):
            result = await executor.execute(
                url=f"https://example.invalid/hook?token={SENTINEL}", method="GET"
            )
        assert result["success"] is True
        assert SENTINEL not in capture.rendered()


class TestStaticGuard:
    """Module-wide net: no log call may interpolate / render an exception."""

    def test_no_unsafe_exception_logging_anywhere_in_the_module(self) -> None:
        """Zero findings, with a real denominator (a mis-pointed scan must not pass)."""
        report = audit_paths([MODULE_ROOT])
        assert report.files_examined >= 20, report.files_examined
        assert report.log_calls_examined >= 100, report.log_calls_examined
        assert not report.findings, "\n".join(f.render() for f in report.findings)
