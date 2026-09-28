"""`services/grpc_handler.py` -- JWT verification + workflow trigger dispatch.

Covers the full `WorkflowServiceServicer.TriggerWorkflow` path: JWT
verification (valid/expired/bad-signature/wrong-alg/`none`-alg/missing
claims), user_id/permission checks, trigger-data parsing, and the
execute-workflow handoff. `algorithms=["HS256"]` in `jwt.decode` is an
explicit allowlist -- PyJWT rejects any other declared `alg` (including
`none`), which is what the wrong-alg/`none`-alg tests assert against.

Regression coverage: `execute_workflow` is called with `trigger_data`
only (no `context=` kwarg) -- passing a plain dict there used to raise
`AttributeError: 'dict' object has no attribute 'execution_id'` inside
`WorkflowEngine.execute_workflow` (which unconditionally does
`context.execution_id = ...` when `context` is not None), silently
turning every gRPC-triggered execution into an opaque INTERNAL abort.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import grpc
import jwt
import pytest

from services.grpc_handler import WorkflowGrpcService, WorkflowServiceServicer

SECRET_KEY = "test-secret-key-for-workflow-core-module-tests"


class _Aborted(grpc.aio.AbortError):
    """Raised by the fake context's `.abort()` to mimic grpc.aio's AbortError.

    Real `ServicerContext.abort()` never returns -- it raises
    `grpc.aio.AbortError` to unwind the coroutine. Subclassing the real
    exception (rather than a plain `Exception`) matters here: `grpc_handler.py`
    has a dedicated `except grpc.aio.AbortError: raise` clause specifically so
    a deliberate abort isn't re-caught by the generic `except Exception`
    below and turned into a second, wrong-status abort call -- a plain
    `Exception` subclass would silently bypass that clause and hide a
    regression in it.
    """


def _make_context() -> MagicMock:
    context = MagicMock()
    context.abort = AsyncMock(side_effect=_Aborted)
    return context


def _make_request(
    *,
    token: str,
    workflow_id: str = "wf-1",
    trigger_source: str = "manual",
    trigger_data: str = "{}",
    session_id: str = "sess-1",
    entity_id: str = "community-1",
    user_id: int = 42,
    platform: str = "discord",
) -> SimpleNamespace:
    """A duck-typed stand-in for the generated `TriggerWorkflowRequest`.

    `TriggerWorkflow` only ever reads attributes off `request` -- it never
    needs protobuf's actual message machinery, so a `SimpleNamespace` is
    sufficient and keeps these tests decoupled from proto codegen.
    """
    return SimpleNamespace(
        token=token,
        workflow_id=workflow_id,
        trigger_source=trigger_source,
        trigger_data=trigger_data,
        session_id=session_id,
        entity_id=entity_id,
        user_id=user_id,
        platform=platform,
    )


def _make_token(
    payload: dict[str, Any], *, secret: str = SECRET_KEY, algorithm: str = "HS256"
) -> str:
    return jwt.encode(payload, secret, algorithm=algorithm)


def _make_none_alg_token(payload: dict[str, Any]) -> str:
    """Hand-craft an unsigned `alg: none` JWT (attacker-forged shape)."""

    def _b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    header = _b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    body = _b64(json.dumps(payload).encode())
    return f"{header}.{body}."


@pytest.fixture
def handler() -> tuple[WorkflowServiceServicer, MagicMock, MagicMock, MagicMock]:
    workflow_engine = MagicMock()
    workflow_engine.execute_workflow = AsyncMock(
        return_value=SimpleNamespace(execution_id="exec-123")
    )
    workflow_service = MagicMock()
    permission_service = MagicMock()
    permission_service.check_permission = AsyncMock(return_value=True)
    logger = MagicMock()

    servicer = WorkflowServiceServicer(
        workflow_engine=workflow_engine,
        workflow_service=workflow_service,
        permission_service=permission_service,
        secret_key=SECRET_KEY,
        logger_instance=logger,
    )
    return servicer, workflow_engine, permission_service, logger


class TestValidToken:
    """Happy path: valid token, permitted user, successful execution."""

    @pytest.mark.asyncio
    async def test_valid_token_triggers_execution_and_returns_success(self, handler):
        servicer, workflow_engine, permission_service, logger = handler
        token = _make_token({"user_id": 42, "community_id": 7})
        request = _make_request(token=token, trigger_data=json.dumps({"foo": "bar"}))
        context = _make_context()

        response = await servicer.TriggerWorkflow(request, context)

        assert response.success is True
        assert "exec-123" in response.message
        context.abort.assert_not_awaited()

        # Regression: no `context=` kwarg, and no plain dict passed as
        # positional context either -- see module docstring.
        workflow_engine.execute_workflow.assert_awaited_once()
        _, kwargs = workflow_engine.execute_workflow.await_args
        assert kwargs["workflow_id"] == "wf-1"
        assert "context" not in kwargs
        assert kwargs["trigger_data"]["foo"] == "bar"
        assert kwargs["trigger_data"]["user_id"] == 42
        assert kwargs["trigger_data"]["entity_id"] == "community-1"
        assert kwargs["trigger_data"]["session_id"] == "sess-1"
        assert kwargs["trigger_data"]["platform"] == "discord"
        assert kwargs["trigger_data"]["grpc_initiated"] is True

        permission_service.check_permission.assert_awaited_once_with(
            workflow_id="wf-1",
            user_id=42,
            permission_type="can_execute",
            community_id=7,
        )
        logger.audit.assert_called_once()


class TestExpiredToken:
    @pytest.mark.asyncio
    async def test_expired_token_aborts_unauthenticated(self, handler):
        servicer, workflow_engine, _, _ = handler
        expired = _make_token(
            {"user_id": 42, "exp": datetime.now(UTC) - timedelta(hours=1)}
        )
        request = _make_request(token=expired)
        context = _make_context()

        with pytest.raises(_Aborted):
            await servicer.TriggerWorkflow(request, context)

        context.abort.assert_awaited_once()
        code, message = context.abort.await_args.args
        assert code == grpc.StatusCode.UNAUTHENTICATED
        assert "expired" in message.lower()
        workflow_engine.execute_workflow.assert_not_awaited()


class TestBadSignature:
    @pytest.mark.asyncio
    async def test_wrong_secret_aborts_unauthenticated(self, handler):
        servicer, workflow_engine, _, _ = handler
        forged = _make_token({"user_id": 42}, secret="not-the-real-secret")
        request = _make_request(token=forged)
        context = _make_context()

        with pytest.raises(_Aborted):
            await servicer.TriggerWorkflow(request, context)

        context.abort.assert_awaited_once()
        code, message = context.abort.await_args.args
        assert code == grpc.StatusCode.UNAUTHENTICATED
        assert "jwt verification failed" in message.lower()
        workflow_engine.execute_workflow.assert_not_awaited()


class TestWrongAlgorithm:
    @pytest.mark.asyncio
    async def test_wrong_algorithm_rejected(self, handler):
        """A token signed HS512 must be rejected by the HS256-only allowlist."""
        servicer, workflow_engine, _, _ = handler
        token = _make_token({"user_id": 42}, algorithm="HS512")
        request = _make_request(token=token)
        context = _make_context()

        with pytest.raises(_Aborted):
            await servicer.TriggerWorkflow(request, context)

        context.abort.assert_awaited_once()
        code, _message = context.abort.await_args.args
        assert code == grpc.StatusCode.UNAUTHENTICATED
        workflow_engine.execute_workflow.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_none_algorithm_forgery_rejected(self, handler):
        """Attacker-forged unsigned `alg: none` token must never validate."""
        servicer, workflow_engine, _, _ = handler
        token = _make_none_alg_token({"user_id": 42})
        request = _make_request(token=token)
        context = _make_context()

        with pytest.raises(_Aborted):
            await servicer.TriggerWorkflow(request, context)

        context.abort.assert_awaited_once()
        code, _message = context.abort.await_args.args
        assert code == grpc.StatusCode.UNAUTHENTICATED
        workflow_engine.execute_workflow.assert_not_awaited()


class TestMissingClaims:
    @pytest.mark.asyncio
    async def test_missing_user_id_claim_aborts_unauthenticated(self, handler):
        servicer, workflow_engine, _, logger = handler
        token = _make_token({"community_id": 7})  # no user_id claim
        request = _make_request(token=token)
        context = _make_context()

        with pytest.raises(_Aborted):
            await servicer.TriggerWorkflow(request, context)

        context.abort.assert_awaited_once()
        code, message = context.abort.await_args.args
        assert code == grpc.StatusCode.UNAUTHENTICATED
        assert "missing user_id" in message.lower()
        logger.warning.assert_called_once()
        workflow_engine.execute_workflow.assert_not_awaited()


class TestUserIdMismatch:
    @pytest.mark.asyncio
    async def test_request_user_id_must_match_token_user_id(self, handler):
        servicer, workflow_engine, _, logger = handler
        token = _make_token({"user_id": 999})
        request = _make_request(token=token, user_id=42)
        context = _make_context()

        with pytest.raises(_Aborted):
            await servicer.TriggerWorkflow(request, context)

        context.abort.assert_awaited_once_with(
            grpc.StatusCode.PERMISSION_DENIED, "User ID mismatch"
        )
        logger.authz.assert_called_once()
        workflow_engine.execute_workflow.assert_not_awaited()


class TestPermissionDenied:
    @pytest.mark.asyncio
    async def test_cannot_execute_permission_denied(self, handler):
        servicer, workflow_engine, permission_service, logger = handler
        permission_service.check_permission = AsyncMock(return_value=False)
        token = _make_token({"user_id": 42})
        request = _make_request(token=token)
        context = _make_context()

        with pytest.raises(_Aborted):
            await servicer.TriggerWorkflow(request, context)

        context.abort.assert_awaited_once_with(
            grpc.StatusCode.PERMISSION_DENIED,
            "Permission denied: cannot execute workflow",
        )
        logger.authz.assert_called_once()
        workflow_engine.execute_workflow.assert_not_awaited()


class TestInvalidTriggerData:
    @pytest.mark.asyncio
    async def test_malformed_json_aborts_invalid_argument(self, handler):
        servicer, workflow_engine, _, logger = handler
        token = _make_token({"user_id": 42})
        request = _make_request(token=token, trigger_data="{not-json")
        context = _make_context()

        with pytest.raises(_Aborted):
            await servicer.TriggerWorkflow(request, context)

        context.abort.assert_awaited_once()
        code, message = context.abort.await_args.args
        assert code == grpc.StatusCode.INVALID_ARGUMENT
        assert "invalid trigger_data json" in message.lower()
        logger.error.assert_called_once()
        workflow_engine.execute_workflow.assert_not_awaited()


class TestExecutionFailure:
    @pytest.mark.asyncio
    async def test_engine_exception_aborts_internal(self, handler):
        servicer, workflow_engine, _, logger = handler
        workflow_engine.execute_workflow = AsyncMock(side_effect=RuntimeError("boom"))
        token = _make_token({"user_id": 42})
        request = _make_request(token=token)
        context = _make_context()

        with pytest.raises(_Aborted):
            await servicer.TriggerWorkflow(request, context)

        context.abort.assert_awaited_once()
        code, message = context.abort.await_args.args
        assert code == grpc.StatusCode.INTERNAL
        assert "boom" in message
        logger.error.assert_called_once()


class TestWorkflowGrpcService:
    """The concrete gRPC-registered wrapper just delegates to the handler."""

    @pytest.mark.asyncio
    async def test_delegates_to_handler(self):
        handler_mock = MagicMock()
        handler_mock.TriggerWorkflow = AsyncMock(return_value="the-response")
        service = WorkflowGrpcService(handler_mock)
        request = object()
        context = object()

        result = await service.TriggerWorkflow(request, context)

        assert result == "the-response"
        handler_mock.TriggerWorkflow.assert_awaited_once_with(request, context)
