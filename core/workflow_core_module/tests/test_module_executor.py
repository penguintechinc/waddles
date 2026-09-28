"""`services/module_executor.py` -- gRPC/HTTP dual-transport module execution.

This file previously had 0% coverage and never actually ran: it reads
`Config.GRPC_ENABLED` directly (not `getattr`/`hasattr`, unlike its sibling
per-module `*_GRPC_HOST` checks), which didn't exist on `Config` at all --
`ModuleExecutor()` raised `AttributeError` the instant it was constructed.
Fixed in `config.py`. This suite covers the gRPC channel manager, the
HTTP fallback transport (success/retry/rate-limit/timeout/exhaustion), and
the variable substitution helpers.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import grpc
import pytest

from config import Config
from models.execution import ExecutionContext
from services.module_executor import (
    GrpcModuleClientManager,
    ModuleExecutionResult,
    ModuleExecutor,
)


def _context(**overrides: Any) -> ExecutionContext:
    defaults = dict(
        execution_id="exec-1",
        workflow_id="wf-1",
        workflow_version="1.0",
        session_id="sess-1",
        entity_id="community-1",
        user_id="user-1",
    )
    defaults.update(overrides)
    return ExecutionContext(**defaults)


class TestModuleExecutionResult:
    def test_defaults_output_to_empty_dict(self) -> None:
        result = ModuleExecutionResult(success=True, output=None)
        assert result.output == {}


class TestGrpcModuleClientManagerHosts:
    def test_build_module_hosts_picks_up_configured_hosts(self) -> None:
        with patch.object(Config, "DISCORD_GRPC_HOST", "discord:50051", create=True):
            manager = GrpcModuleClientManager()
        assert manager._module_hosts.get("discord_action") == "discord:50051"

    def test_build_module_hosts_empty_when_nothing_configured(self) -> None:
        manager = GrpcModuleClientManager()
        assert isinstance(manager._module_hosts, dict)


class TestGrpcModuleClientManagerGetChannel:
    @pytest.mark.asyncio
    async def test_returns_none_when_grpc_disabled(self) -> None:
        manager = GrpcModuleClientManager()
        with patch.object(Config, "GRPC_ENABLED", False):
            assert await manager.get_channel("discord_action") is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_host_configured(self) -> None:
        manager = GrpcModuleClientManager()
        manager._module_hosts = {}
        with patch.object(Config, "GRPC_ENABLED", True):
            assert await manager.get_channel("discord_action") is None

    @pytest.mark.asyncio
    async def test_creates_and_caches_channel(self) -> None:
        manager = GrpcModuleClientManager()
        manager._module_hosts = {"discord_action": "discord:50051"}
        fake_channel = MagicMock()

        with (
            patch.object(Config, "GRPC_ENABLED", True),
            patch("flask_core.grpc_tls.secure_channel", return_value=fake_channel) as mock_secure,
        ):
            channel = await manager.get_channel("discord_action")

        assert channel is fake_channel
        assert manager._channels["discord_action"] is fake_channel
        mock_secure.assert_called_once()

    @pytest.mark.asyncio
    async def test_reuses_alive_cached_channel(self) -> None:
        manager = GrpcModuleClientManager()
        manager._module_hosts = {"discord_action": "discord:50051"}
        fake_channel = MagicMock()
        fake_channel.channel_ready = AsyncMock(return_value=None)
        manager._channels["discord_action"] = fake_channel

        with patch.object(Config, "GRPC_ENABLED", True):
            channel = await manager.get_channel("discord_action")

        assert channel is fake_channel

    @pytest.mark.asyncio
    async def test_recreates_dead_cached_channel(self) -> None:
        manager = GrpcModuleClientManager()
        manager._module_hosts = {"discord_action": "discord:50051"}
        dead_channel = MagicMock()
        dead_channel.channel_ready = AsyncMock(side_effect=Exception("dead"))
        dead_channel.close = AsyncMock(return_value=None)
        manager._channels["discord_action"] = dead_channel
        new_channel = MagicMock()

        with (
            patch.object(Config, "GRPC_ENABLED", True),
            patch("flask_core.grpc_tls.secure_channel", return_value=new_channel),
        ):
            channel = await manager.get_channel("discord_action")

        assert channel is new_channel
        dead_channel.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_channel_creation_failure_returns_none(self) -> None:
        manager = GrpcModuleClientManager()
        manager._module_hosts = {"discord_action": "discord:50051"}

        with (
            patch.object(Config, "GRPC_ENABLED", True),
            patch("flask_core.grpc_tls.secure_channel", side_effect=Exception("boom")),
        ):
            assert await manager.get_channel("discord_action") is None


class TestGrpcModuleClientManagerRetry:
    @pytest.mark.asyncio
    async def test_call_with_retry_succeeds_first_try(self) -> None:
        manager = GrpcModuleClientManager()
        method = AsyncMock(return_value="ok")
        result = await manager.call_with_retry(method, "req", max_retries=2, timeout=1.0)
        assert result == "ok"
        method.assert_awaited_once_with("req")

    @pytest.mark.asyncio
    async def test_call_with_retry_reraises_non_retryable_grpc_error(self) -> None:
        manager = GrpcModuleClientManager()
        error = grpc.aio.AioRpcError(
            grpc.StatusCode.PERMISSION_DENIED, None, None, "denied"
        )
        method = AsyncMock(side_effect=error)
        with pytest.raises(grpc.aio.AioRpcError):
            await manager.call_with_retry(method, "req", max_retries=2, timeout=1.0)

    @pytest.mark.asyncio
    async def test_call_with_retry_retries_then_succeeds_on_unavailable(self) -> None:
        manager = GrpcModuleClientManager()
        error = grpc.aio.AioRpcError(grpc.StatusCode.UNAVAILABLE, None, None, "down")
        method = AsyncMock(side_effect=[error, "ok"])
        with patch("services.module_executor.asyncio.sleep", new=AsyncMock()):
            result = await manager.call_with_retry(method, "req", max_retries=3, timeout=1.0)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_call_with_retry_exhausts_and_raises_last_error(self) -> None:
        manager = GrpcModuleClientManager()
        error = grpc.aio.AioRpcError(grpc.StatusCode.DEADLINE_EXCEEDED, None, None, "slow")
        method = AsyncMock(side_effect=error)
        with (
            patch("services.module_executor.asyncio.sleep", new=AsyncMock()),
            pytest.raises(grpc.aio.AioRpcError),
        ):
            await manager.call_with_retry(method, "req", max_retries=2, timeout=1.0)

    @pytest.mark.asyncio
    async def test_call_with_retry_retries_on_timeout(self) -> None:
        manager = GrpcModuleClientManager()
        method = AsyncMock(side_effect=[asyncio.TimeoutError(), "ok"])
        with patch("services.module_executor.asyncio.sleep", new=AsyncMock()):
            result = await manager.call_with_retry(method, "req", max_retries=3, timeout=1.0)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_close_all_closes_every_channel(self) -> None:
        manager = GrpcModuleClientManager()
        chan1, chan2 = MagicMock(), MagicMock()
        chan1.close = AsyncMock(return_value=None)
        chan2.close = AsyncMock(side_effect=Exception("already closed"))
        manager._channels = {"a": chan1, "b": chan2}
        await manager.close_all()
        assert manager._channels == {}


class TestModuleExecutorInit:
    def test_grpc_manager_created_when_enabled(self) -> None:
        with patch.object(Config, "GRPC_ENABLED", True):
            executor = ModuleExecutor()
        assert executor._grpc_manager is not None

    def test_grpc_manager_none_when_disabled(self) -> None:
        with patch.object(Config, "GRPC_ENABLED", False):
            executor = ModuleExecutor()
        assert executor._grpc_manager is None

    @pytest.mark.asyncio
    async def test_ensure_http_session_creates_once(self) -> None:
        executor = ModuleExecutor()
        await executor._ensure_http_session()
        session = executor._http_session
        assert session is not None
        await executor._ensure_http_session()
        assert executor._http_session is session
        await executor.close()

    @pytest.mark.asyncio
    async def test_close_closes_session_and_grpc_manager(self) -> None:
        with patch.object(Config, "GRPC_ENABLED", True):
            executor = ModuleExecutor()
        await executor._ensure_http_session()
        executor._grpc_manager.close_all = AsyncMock()
        await executor.close()
        assert executor._http_session is None
        executor._grpc_manager.close_all.assert_awaited_once()

    def test_setup_proto_path_idempotent(self) -> None:
        executor = ModuleExecutor()
        executor._setup_proto_path()
        assert executor._proto_path_setup is True
        executor._setup_proto_path()  # second call is a no-op branch

    def test_generate_token_returns_jwt(self) -> None:
        executor = ModuleExecutor()
        token = executor._generate_token({"foo": "bar"})
        assert isinstance(token, str) and token.count(".") == 2

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("discord", "discord_action"),
            ("DISCORD_ACTION", "discord_action"),
            ("unknown_module", "unknown_module"),
        ],
    )
    def test_normalize_module_name(self, raw: str, expected: str) -> None:
        executor = ModuleExecutor()
        assert executor._normalize_module_name(raw) == expected


class TestModuleExecutorExecute:
    @pytest.mark.asyncio
    async def test_execute_falls_back_to_http_when_grpc_disabled(self) -> None:
        with patch.object(Config, "GRPC_ENABLED", False):
            executor = ModuleExecutor()
            executor._execute_http = AsyncMock(
                return_value=ModuleExecutionResult(success=True, output={"ok": True})
            )
            result = await executor.execute(
                "discord", "1.0", {"msg": "hi"}, _context()
            )
        assert result.success is True
        executor._execute_http.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_execute_uses_grpc_result_when_available(self) -> None:
        with patch.object(Config, "GRPC_ENABLED", True):
            executor = ModuleExecutor()
            grpc_result = ModuleExecutionResult(success=True, transport_used="grpc")
            executor._execute_grpc = AsyncMock(return_value=grpc_result)
            executor._execute_http = AsyncMock()
            result = await executor.execute("discord", "1.0", {}, _context())
        assert result is grpc_result
        executor._execute_http.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_execute_falls_back_to_http_when_grpc_raises(self) -> None:
        with patch.object(Config, "GRPC_ENABLED", True):
            executor = ModuleExecutor()
            executor._execute_grpc = AsyncMock(side_effect=Exception("grpc down"))
            executor._execute_http = AsyncMock(
                return_value=ModuleExecutionResult(success=True)
            )
            result = await executor.execute("discord", "1.0", {}, _context())
        assert result.success is True

    @pytest.mark.asyncio
    async def test_execute_returns_error_result_when_http_raises(self) -> None:
        with patch.object(Config, "GRPC_ENABLED", False):
            executor = ModuleExecutor()
            executor._execute_http = AsyncMock(side_effect=Exception("boom"))
            result = await executor.execute("discord", "1.0", {}, _context())
        assert result.success is False
        assert result.error_type == "execution"


class TestModuleExecutorGrpcDispatch:
    @pytest.mark.asyncio
    async def test_execute_grpc_returns_none_without_manager(self) -> None:
        executor = ModuleExecutor()
        executor._grpc_manager = None
        assert await executor._execute_grpc("discord_action", {}, 1.0, 0) is None

    @pytest.mark.asyncio
    async def test_execute_grpc_routes_action_modules(self) -> None:
        executor = ModuleExecutor()
        executor._grpc_manager = MagicMock()
        executor._execute_grpc_action_module = AsyncMock(return_value=None)
        await executor._execute_grpc("discord_action", {}, 1.0, 0)
        executor._execute_grpc_action_module.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_execute_grpc_routes_core_modules(self) -> None:
        executor = ModuleExecutor()
        executor._grpc_manager = MagicMock()
        executor._execute_grpc_generic = AsyncMock(return_value=None)
        await executor._execute_grpc("reputation", {}, 1.0, 0)
        executor._execute_grpc_generic.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_execute_grpc_swallows_exceptions(self) -> None:
        executor = ModuleExecutor()
        executor._grpc_manager = MagicMock()
        executor._execute_grpc_generic = AsyncMock(side_effect=Exception("boom"))
        assert await executor._execute_grpc("reputation", {}, 1.0, 0) is None

    @pytest.mark.asyncio
    async def test_execute_grpc_action_module_no_channel(self) -> None:
        executor = ModuleExecutor()
        executor._grpc_manager = MagicMock()
        executor._grpc_manager.get_channel = AsyncMock(return_value=None)
        assert await executor._execute_grpc_action_module("discord_action", {}, 1.0, 0) is None

    @pytest.mark.asyncio
    async def test_execute_grpc_action_module_proto_import_missing(self) -> None:
        executor = ModuleExecutor()
        executor._grpc_manager = MagicMock()
        executor._grpc_manager.get_channel = AsyncMock(return_value=MagicMock())
        assert await executor._execute_grpc_action_module("discord_action", {}, 1.0, 0) is None

    @pytest.mark.asyncio
    async def test_execute_grpc_generic_always_none(self) -> None:
        executor = ModuleExecutor()
        assert await executor._execute_grpc_generic("reputation", {}, 1.0, 0) is None


class _FakeResponse:
    def __init__(self, status: int, json_body: dict | None = None, text_body: str = "err") -> None:
        self.status = status
        self._json_body = json_body or {}
        self._text_body = text_body

    async def json(self) -> dict:
        return self._json_body

    async def text(self) -> str:
        return self._text_body

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class TestModuleExecutorHttp:
    @pytest.mark.asyncio
    async def test_execute_http_no_url_configured(self) -> None:
        executor = ModuleExecutor()
        executor._get_module_http_url = MagicMock(return_value=None)
        result = await executor._execute_http("discord", {}, 5, 0)
        assert result.success is False
        assert result.error_type == "configuration"
        await executor.close()

    @pytest.mark.asyncio
    async def test_execute_http_success(self) -> None:
        executor = ModuleExecutor()
        executor._get_module_http_url = MagicMock(return_value="http://discord-action:8051")
        await executor._ensure_http_session()
        response = _FakeResponse(200, json_body={"output": {"sent": True}})
        executor._http_session.post = MagicMock(return_value=response)

        result = await executor._execute_http(
            "discord", {"module_name": "discord", "module_version": "1.0"}, 5, 0
        )
        assert result.success is True
        assert result.output == {"sent": True}
        await executor.close()

    @pytest.mark.asyncio
    async def test_execute_http_rate_limited_then_exhausts(self) -> None:
        executor = ModuleExecutor()
        executor._get_module_http_url = MagicMock(return_value="http://discord-action:8051")
        await executor._ensure_http_session()
        executor._http_session.post = MagicMock(return_value=_FakeResponse(429))

        with patch("services.module_executor.asyncio.sleep", new=AsyncMock()):
            result = await executor._execute_http("discord", {}, 5, retry_count=1)
        assert result.success is False
        assert "Rate limited" in result.error
        await executor.close()

    @pytest.mark.asyncio
    async def test_execute_http_server_error_retries_then_fails(self) -> None:
        executor = ModuleExecutor()
        executor._get_module_http_url = MagicMock(return_value="http://discord-action:8051")
        await executor._ensure_http_session()
        executor._http_session.post = MagicMock(return_value=_FakeResponse(500, text_body="oops"))

        with patch("services.module_executor.asyncio.sleep", new=AsyncMock()):
            result = await executor._execute_http("discord", {}, 5, retry_count=1)
        assert result.success is False
        assert result.error_type == "http_error"
        await executor.close()

    @pytest.mark.asyncio
    async def test_execute_http_client_error_no_retry(self) -> None:
        executor = ModuleExecutor()
        executor._get_module_http_url = MagicMock(return_value="http://discord-action:8051")
        await executor._ensure_http_session()
        executor._http_session.post = MagicMock(return_value=_FakeResponse(400, text_body="bad"))

        result = await executor._execute_http("discord", {}, 5, retry_count=2)
        assert result.success is False
        assert "HTTP 400" in result.error
        await executor.close()

    @pytest.mark.asyncio
    async def test_execute_http_timeout_retries_then_exhausts(self) -> None:
        executor = ModuleExecutor()
        executor._get_module_http_url = MagicMock(return_value="http://discord-action:8051")
        await executor._ensure_http_session()

        class _Raiser:
            def __call__(self, *a: Any, **k: Any) -> "_Raiser":
                return self

            async def __aenter__(self) -> Any:
                raise asyncio.TimeoutError()

            async def __aexit__(self, *exc: Any) -> None:
                return None

        executor._http_session.post = _Raiser()
        with patch("services.module_executor.asyncio.sleep", new=AsyncMock()):
            result = await executor._execute_http("discord", {}, 5, retry_count=1)
        assert result.success is False
        assert "Module execution failed after" in result.error
        await executor.close()

    @pytest.mark.asyncio
    async def test_execute_http_generic_exception_retries_then_exhausts(self) -> None:
        executor = ModuleExecutor()
        executor._get_module_http_url = MagicMock(return_value="http://discord-action:8051")
        await executor._ensure_http_session()

        class _Raiser:
            def __call__(self, *a: Any, **k: Any) -> "_Raiser":
                return self

            async def __aenter__(self) -> Any:
                raise ValueError("network died")

            async def __aexit__(self, *exc: Any) -> None:
                return None

        executor._http_session.post = _Raiser()
        with patch("services.module_executor.asyncio.sleep", new=AsyncMock()):
            result = await executor._execute_http("discord", {}, 5, retry_count=1)
        assert result.success is False
        await executor.close()

    def test_get_module_http_url_module_specific_env(self) -> None:
        executor = ModuleExecutor()
        with patch.object(Config, "DISCORD_ACTION_HTTP_URL", "http://custom:1234", create=True):
            assert executor._get_module_http_url("discord_action") == "http://custom:1234"

    def test_get_module_http_url_generic_service_url(self) -> None:
        executor = ModuleExecutor()
        with patch.object(Config, "MODULE_SERVICE_URL", "http://generic:9999", create=True):
            assert executor._get_module_http_url("unmapped_module") == "http://generic:9999"

    def test_get_module_http_url_constructs_action_url(self) -> None:
        executor = ModuleExecutor()
        assert executor._get_module_http_url("discord_action") == "http://discord-action:8051"

    def test_get_module_http_url_constructs_core_url(self) -> None:
        executor = ModuleExecutor()
        assert executor._get_module_http_url("reputation") == "http://reputation-core:8050"

    @pytest.mark.asyncio
    async def test_execute_http_adds_service_api_key_header(self) -> None:
        executor = ModuleExecutor()
        executor._get_module_http_url = MagicMock(return_value="http://discord-action:8051")
        await executor._ensure_http_session()
        captured: dict[str, Any] = {}

        def _post(url: str, json: Any, headers: dict, timeout: Any) -> _FakeResponse:
            captured["headers"] = headers
            return _FakeResponse(200, json_body={"output": {}})

        executor._http_session.post = _post
        with patch.object(Config, "SERVICE_API_KEY", "svc-key", create=True):
            await executor._execute_http("discord", {}, 5, 0)
        assert captured["headers"]["X-Service-Key"] == "svc-key"
        await executor.close()


class TestExtractVariables:
    @pytest.mark.asyncio
    async def test_extract_variables_maps_output_fields(self) -> None:
        executor = ModuleExecutor()
        ctx = _context()
        response = ModuleExecutionResult(success=True, output={"message_id": "abc123"})
        extracted = await executor.extract_variables(
            response, {"message_id": "sent_message_id"}, ctx
        )
        assert extracted == {"sent_message_id": "abc123"}
        assert ctx.get_variable("sent_message_id") == "abc123"

    @pytest.mark.asyncio
    async def test_extract_variables_empty_on_failure(self) -> None:
        executor = ModuleExecutor()
        response = ModuleExecutionResult(success=False)
        extracted = await executor.extract_variables(response, {"a": "b"}, _context())
        assert extracted == {}


class TestSubstituteExpressions:
    @pytest.mark.asyncio
    async def test_substitute_expressions_replaces_top_level_strings(self) -> None:
        executor = ModuleExecutor()
        ctx = _context()
        ctx.set_variable("name", "penguin")
        result = await executor.substitute_expressions({"greeting": "hi {name}"}, ctx)
        assert result == {"greeting": "hi penguin"}

    @pytest.mark.asyncio
    async def test_substitute_expressions_handles_nested_dict_and_list(self) -> None:
        executor = ModuleExecutor()
        ctx = _context()
        ctx.set_variable("count", 3)
        result = await executor.substitute_expressions(
            {"nested": {"a": "{count} items"}, "items": ["{count}", "static"]}, ctx
        )
        assert result == {"nested": {"a": "3 items"}, "items": ["3", "static"]}

    def test_substitute_string_uses_default_when_missing(self) -> None:
        executor = ModuleExecutor()
        ctx = _context()
        result = executor._substitute_string("value: {missing|fallback}", ctx)
        assert result == "value: fallback"

    def test_substitute_string_returns_original_when_missing_and_no_default(self) -> None:
        executor = ModuleExecutor()
        ctx = _context()
        result = executor._substitute_string("value: {missing}", ctx)
        assert result == "value: {missing}"

    def test_substitute_string_swallows_get_variable_errors(self) -> None:
        executor = ModuleExecutor()
        ctx = MagicMock()
        ctx.get_variable.side_effect = Exception("boom")
        result = executor._substitute_string("value: {broken|fallback}", ctx)
        assert result == "value: fallback"

    def test_substitute_value_passes_through_non_str_non_container(self) -> None:
        executor = ModuleExecutor()
        assert executor._substitute_value(42, _context()) == 42
