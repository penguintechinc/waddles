"""`services/node_executor.py` -- additional coverage beyond `tests/test_node_executor.py`.

Covers action-module/webhook/chat/browser-source HTTP executors, the
RestrictedPython data-transform sandbox, loop-while/break, flow-merge/
parallel, operator edge cases, and the top-level `execute_node` exception
handler -- paths the original moved-in suite didn't reach.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from models.execution import ExecutionContext
from models.nodes import (
    ActionBrowserSourceConfig,
    ActionChatMessageConfig,
    ActionModuleConfig,
    ActionWebhookConfig,
    ConditionRule,
    DataTransformConfig,
    DataVariableGetConfig,
    FlowMergeConfig,
    FlowParallelConfig,
    HttpMethod,
    LoopBreakConfig,
    LoopWhileConfig,
    OperatorType,
    TriggerCommandConfig,
)
from services.node_executor import NodeExecutionResult, NodeExecutor


def _context(**overrides: Any) -> ExecutionContext:
    defaults = dict(
        execution_id="exec-1", workflow_id="wf-1", workflow_version="1.0",
        session_id="sess-1", entity_id="community-1", user_id="user-1",
    )
    defaults.update(overrides)
    return ExecutionContext(**defaults)


class _FakeResponse:
    def __init__(self, status: int, json_body: Any = None, text_body: str = "err") -> None:
        self.status = status
        self._json_body = json_body if json_body is not None else {}
        self._text_body = text_body

    async def json(self) -> Any:
        return self._json_body

    async def text(self) -> str:
        return self._text_body

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class TestExecuteNodeTopLevel:
    @pytest.mark.asyncio
    async def test_execute_node_exception_marks_failed(self) -> None:
        executor = NodeExecutor()
        node = TriggerCommandConfig(node_id="t1", label="T", position={"x": 0, "y": 0})
        with patch.object(
            NodeExecutor, "_execute_by_type", AsyncMock(side_effect=RuntimeError("boom"))
        ):
            state = await executor.execute_node(node, _context())
        assert state.status.value == "failed"
        assert state.error_type == "exception"

    @pytest.mark.asyncio
    async def test_execute_node_stores_input_data(self) -> None:
        executor = NodeExecutor()
        node = TriggerCommandConfig(node_id="t1", label="T", position={"x": 0, "y": 0})
        state = await executor.execute_node(node, _context(), input_data={"in": "value"})
        assert "in" in state.input_data

    @pytest.mark.asyncio
    async def test_unknown_node_type_returns_validation_error(self) -> None:
        executor = NodeExecutor()
        fake_node = SimpleNamespace(node_id="x1", node_type=SimpleNamespace(value="fake_type"))
        state = await executor.execute_node(fake_node, _context())
        assert state.status.value == "failed"
        assert state.error_type == "validation"

    @pytest.mark.asyncio
    async def test_context_manager_ensures_and_closes_session(self) -> None:
        async with NodeExecutor() as executor:
            assert executor._http_session is not None
        # __aexit__ closed it
        assert executor._http_session is None

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self) -> None:
        executor = NodeExecutor()
        await executor.close()  # never opened -- no-op
        await executor._ensure_http_session()
        await executor.close()
        await executor.close()  # already closed -- no-op


class TestExecuteActionModule:
    @pytest.mark.asyncio
    async def test_success_maps_output(self) -> None:
        executor = NodeExecutor()
        node = ActionModuleConfig(
            node_id="m1", label="M", position={"x": 0, "y": 0},
            module_name="discord", input_mapping={"msg": "message"},
            output_mapping={"sent_id": "message_id"},
        )
        ctx = _context()
        ctx.set_variable("msg", "hello")
        await executor._ensure_http_session()
        executor._http_session.post = MagicMock(
            return_value=_FakeResponse(200, json_body={"output": {"sent_id": "abc"}, "message": "OK"})
        )
        result = await executor.execute_action_module(node, ctx, {})
        assert result.success is True
        assert ctx.get_variable("message_id") == "abc"
        await executor.close()

    @pytest.mark.asyncio
    async def test_non_200_returns_api_error(self) -> None:
        executor = NodeExecutor()
        node = ActionModuleConfig(node_id="m1", label="M", position={"x": 0, "y": 0})
        await executor._ensure_http_session()
        executor._http_session.post = MagicMock(return_value=_FakeResponse(500, text_body="oops"))
        result = await executor.execute_action_module(node, _context(), {})
        assert result.success is False
        assert result.error_type == "api_error"
        await executor.close()

    @pytest.mark.asyncio
    async def test_timeout_returns_timeout_error(self) -> None:
        executor = NodeExecutor()
        node = ActionModuleConfig(node_id="m1", label="M", position={"x": 0, "y": 0})
        await executor._ensure_http_session()

        class _Raiser:
            def __call__(self, *a: Any, **k: Any) -> "_Raiser":
                return self

            async def __aenter__(self) -> Any:
                raise asyncio.TimeoutError()

            async def __aexit__(self, *exc: Any) -> None:
                return None

        executor._http_session.post = _Raiser()
        result = await executor.execute_action_module(node, _context(), {})
        assert result.error_type == "timeout"
        await executor.close()

    @pytest.mark.asyncio
    async def test_generic_exception_returns_execution_error(self) -> None:
        executor = NodeExecutor()
        node = ActionModuleConfig(node_id="m1", label="M", position={"x": 0, "y": 0})
        await executor._ensure_http_session()
        executor._http_session.post = MagicMock(side_effect=ValueError("boom"))
        result = await executor.execute_action_module(node, _context(), {})
        assert result.error_type == "execution"
        await executor.close()


class TestExecuteActionWebhook:
    @pytest.mark.asyncio
    async def test_success_with_json_body_template(self) -> None:
        executor = NodeExecutor()
        node = ActionWebhookConfig(
            node_id="w1", label="W", position={"x": 0, "y": 0},
            url="https://example.com/{{path}}", method=HttpMethod.POST,
            headers={"X-Token": "{{token}}"}, body_template='{"a": "{{val}}"}',
            output_variable="resp",
        )
        ctx = _context()
        ctx.set_variable("path", "hook")
        ctx.set_variable("token", "abc")
        ctx.set_variable("val", "1")
        await executor._ensure_http_session()
        executor._http_session.request = MagicMock(
            return_value=_FakeResponse(200, text_body='{"ok": true}')
        )
        result = await executor.execute_action_webhook(node, ctx, {})
        assert result.success is True
        assert ctx.get_variable("resp") == {"ok": True}
        await executor.close()

    @pytest.mark.asyncio
    async def test_non_json_response_falls_back_to_text(self) -> None:
        executor = NodeExecutor()
        node = ActionWebhookConfig(
            node_id="w1", label="W", position={"x": 0, "y": 0}, url="https://x",
        )
        await executor._ensure_http_session()
        executor._http_session.request = MagicMock(
            return_value=_FakeResponse(200, text_body="not json")
        )
        result = await executor.execute_action_webhook(node, _context(), {})
        assert result.output_data["response"] == {"text": "not json"}
        await executor.close()

    @pytest.mark.asyncio
    async def test_body_template_invalid_json_sent_as_string(self) -> None:
        executor = NodeExecutor()
        node = ActionWebhookConfig(
            node_id="w1", label="W", position={"x": 0, "y": 0}, url="https://x",
            body_template="not { json",
        )
        await executor._ensure_http_session()
        captured: dict[str, Any] = {}

        def _request(**kwargs: Any) -> _FakeResponse:
            captured.update(kwargs)
            return _FakeResponse(200, text_body="ok")

        executor._http_session.request = _request
        await executor.execute_action_webhook(node, _context(), {})
        assert captured["data"] == "not { json"
        await executor.close()

    @pytest.mark.asyncio
    async def test_retries_then_fails(self) -> None:
        executor = NodeExecutor()
        node = ActionWebhookConfig(
            node_id="w1", label="W", position={"x": 0, "y": 0}, url="https://x", retry_count=1,
        )
        await executor._ensure_http_session()
        executor._http_session.request = MagicMock(return_value=_FakeResponse(500, text_body="err"))
        with patch("services.node_executor.asyncio.sleep", new=AsyncMock()):
            result = await executor.execute_action_webhook(node, _context(), {})
        assert result.success is False
        assert result.error_type == "webhook_error"
        await executor.close()

    @pytest.mark.asyncio
    async def test_timeout_then_exhausts(self) -> None:
        executor = NodeExecutor()
        node = ActionWebhookConfig(
            node_id="w1", label="W", position={"x": 0, "y": 0}, url="https://x", retry_count=0,
        )
        await executor._ensure_http_session()

        class _Raiser:
            def __call__(self, **k: Any) -> "_Raiser":
                return self

            async def __aenter__(self) -> Any:
                raise asyncio.TimeoutError()

            async def __aexit__(self, *exc: Any) -> None:
                return None

        executor._http_session.request = _Raiser()
        result = await executor.execute_action_webhook(node, _context(), {})
        assert "Timeout" in result.error
        await executor.close()

    @pytest.mark.asyncio
    async def test_outer_exception_returns_execution_error(self) -> None:
        executor = NodeExecutor()
        node = ActionWebhookConfig(node_id="w1", label="W", position={"x": 0, "y": 0}, url="https://x")
        executor._replace_variables = MagicMock(side_effect=ValueError("boom"))
        result = await executor.execute_action_webhook(node, _context(), {})
        assert result.error_type == "execution"


class TestExecuteActionChatMessage:
    @pytest.mark.asyncio
    async def test_success(self) -> None:
        executor = NodeExecutor()
        node = ActionChatMessageConfig(
            node_id="c1", label="C", position={"x": 0, "y": 0},
            message_template="Hi {{name}}", channel_id="ch1", user_id="u1",
        )
        ctx = _context()
        ctx.set_variable("name", "penguin")
        await executor._ensure_http_session()
        executor._http_session.post = MagicMock(
            return_value=_FakeResponse(200, json_body={"message_id": "m1"})
        )
        result = await executor.execute_action_chat_message(node, ctx, {})
        assert result.success is True
        assert result.output_data["message_id"] == "m1"
        await executor.close()

    @pytest.mark.asyncio
    async def test_failure(self) -> None:
        executor = NodeExecutor()
        node = ActionChatMessageConfig(node_id="c1", label="C", position={"x": 0, "y": 0})
        await executor._ensure_http_session()
        executor._http_session.post = MagicMock(return_value=_FakeResponse(500, text_body="err"))
        result = await executor.execute_action_chat_message(node, _context(), {})
        assert result.success is False
        assert result.error_type == "api_error"
        await executor.close()

    @pytest.mark.asyncio
    async def test_exception(self) -> None:
        executor = NodeExecutor()
        node = ActionChatMessageConfig(node_id="c1", label="C", position={"x": 0, "y": 0})
        await executor._ensure_http_session()
        executor._http_session.post = MagicMock(side_effect=ValueError("boom"))
        result = await executor.execute_action_chat_message(node, _context(), {})
        assert result.error_type == "execution"
        await executor.close()


class TestExecuteActionBrowserSource:
    @pytest.mark.asyncio
    async def test_success(self) -> None:
        executor = NodeExecutor()
        node = ActionBrowserSourceConfig(
            node_id="b1", label="B", position={"x": 0, "y": 0},
            content_template="Score: {{score}}",
        )
        ctx = _context()
        ctx.set_variable("score", 10)
        await executor._ensure_http_session()
        executor._http_session.post = MagicMock(return_value=_FakeResponse(200))
        result = await executor.execute_action_browser_source(node, ctx, {})
        assert result.success is True
        await executor.close()

    @pytest.mark.asyncio
    async def test_failure(self) -> None:
        executor = NodeExecutor()
        node = ActionBrowserSourceConfig(node_id="b1", label="B", position={"x": 0, "y": 0})
        await executor._ensure_http_session()
        executor._http_session.post = MagicMock(return_value=_FakeResponse(400, text_body="bad"))
        result = await executor.execute_action_browser_source(node, _context(), {})
        assert result.success is False
        await executor.close()

    @pytest.mark.asyncio
    async def test_exception(self) -> None:
        executor = NodeExecutor()
        node = ActionBrowserSourceConfig(node_id="b1", label="B", position={"x": 0, "y": 0})
        await executor._ensure_http_session()
        executor._http_session.post = MagicMock(side_effect=ValueError("boom"))
        result = await executor.execute_action_browser_source(node, _context(), {})
        assert result.error_type == "execution"
        await executor.close()


class TestExecuteDataTransform:
    @pytest.mark.asyncio
    async def test_success(self) -> None:
        executor = NodeExecutor()
        node = DataTransformConfig(
            node_id="d1", label="D", position={"x": 0, "y": 0},
            transformations={"doubled": "x * 2"},
        )
        ctx = _context()
        ctx.set_variable("x", 5)
        result = await executor.execute_data_transform(node, ctx, {})
        assert result.success is True
        assert ctx.get_variable("doubled") == 10

    @pytest.mark.asyncio
    async def test_restricted_python_unavailable(self) -> None:
        executor = NodeExecutor()
        node = DataTransformConfig(
            node_id="d1", label="D", position={"x": 0, "y": 0},
            transformations={"x": "1 + 1"},
        )
        with patch("services.node_executor.RESTRICTED_PYTHON_AVAILABLE", False):
            result = await executor.execute_data_transform(node, _context(), {})
        assert result.success is False
        assert result.error_type == "execution"

    @pytest.mark.asyncio
    async def test_compile_error(self) -> None:
        executor = NodeExecutor()
        node = DataTransformConfig(
            node_id="d1", label="D", position={"x": 0, "y": 0},
            transformations={"x": "this is not valid python(("},
        )
        result = await executor.execute_data_transform(node, _context(), {})
        assert result.success is False

    @pytest.mark.asyncio
    async def test_timeout(self) -> None:
        executor = NodeExecutor()
        node = DataTransformConfig(
            node_id="d1", label="D", position={"x": 0, "y": 0},
            transformations={"x": "1 + 1"},
        )
        with patch.object(
            NodeExecutor, "_execute_restricted_python",
            AsyncMock(side_effect=asyncio.TimeoutError()),
        ):
            result = await executor.execute_data_transform(node, _context(), {})
        assert result.error_type == "timeout"


class TestExecuteDataVariableGet:
    @pytest.mark.asyncio
    async def test_uses_variable_name_as_output_key_when_no_output_variable(self) -> None:
        executor = NodeExecutor()
        node = DataVariableGetConfig(
            node_id="g1", label="G", position={"x": 0, "y": 0}, variable_name="x",
        )
        ctx = _context()
        ctx.set_variable("x", 42)
        result = await executor.execute_data_variable_get(node, ctx, {})
        assert result.output_data == {"x": 42}


class TestEvaluateOperator:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "operator,left,right,expected",
        [
            (OperatorType.GREATER_EQUAL, 5, 5, True),
            (OperatorType.GREATER_EQUAL, 4, 5, False),
            (OperatorType.LESS_EQUAL, 5, 5, True),
            (OperatorType.LESS_EQUAL, 6, 5, False),
            (OperatorType.NOT_CONTAINS, "hello", "xyz", True),
            (OperatorType.NOT_CONTAINS, "hello", "ell", False),
            (OperatorType.MATCHES_REGEX, "hello", r"^h.*o$", True),
            (OperatorType.IN_LIST, 2, [1, 2, 3], True),
            (OperatorType.IN_LIST, "a", "a", True),
            (OperatorType.NOT_IN_LIST, 5, [1, 2, 3], True),
        ],
    )
    async def test_operators(self, operator, left, right, expected) -> None:
        executor = NodeExecutor()
        node = None
        result = executor._evaluate_operator(left, operator, right)
        assert result is expected

    def test_unknown_operator_returns_false(self) -> None:
        executor = NodeExecutor()
        result = executor._evaluate_operator(1, "not_a_real_operator", 1)
        assert result is False

    def test_operator_exception_returns_false(self) -> None:
        executor = NodeExecutor()
        result = executor._evaluate_operator("abc", OperatorType.GREATER_THAN, "xyz")
        assert result is False

    def test_condition_rules_short_circuit_on_first_failure(self) -> None:
        executor = NodeExecutor()
        ctx = _context()
        ctx.set_variable("a", 1)
        rules = [ConditionRule(variable="a", operator=OperatorType.EQUALS, value=999)]
        assert executor._evaluate_condition_rules(rules, ctx) is False


class TestExecuteLoopWhile:
    @pytest.mark.asyncio
    async def test_routes_to_loop_when_condition_true(self) -> None:
        executor = NodeExecutor()
        node = LoopWhileConfig(
            node_id="l1", label="L", position={"x": 0, "y": 0},
            condition=[ConditionRule(variable="x", operator=OperatorType.LESS_THAN, value=10)],
            max_iterations=5,
        )
        ctx = _context()
        ctx.set_variable("x", 1)
        result = await executor.execute_loop_while(node, ctx, {})
        assert result.output_port == "loop"

    @pytest.mark.asyncio
    async def test_routes_to_exit_when_condition_false(self) -> None:
        executor = NodeExecutor()
        node = LoopWhileConfig(
            node_id="l1", label="L", position={"x": 0, "y": 0},
            condition=[ConditionRule(variable="x", operator=OperatorType.LESS_THAN, value=0)],
            max_iterations=5,
        )
        ctx = _context()
        ctx.set_variable("x", 1)
        result = await executor.execute_loop_while(node, ctx, {})
        assert result.output_port == "exit"

    @pytest.mark.asyncio
    async def test_max_iterations_exceeded(self) -> None:
        executor = NodeExecutor()
        node = LoopWhileConfig(node_id="l1", label="L", position={"x": 0, "y": 0}, max_iterations=2)
        ctx = _context()
        ctx.set_variable("_while_iteration", 2)
        result = await executor.execute_loop_while(node, ctx, {})
        assert result.success is False
        assert result.error_type == "validation"


class TestExecuteLoopBreak:
    @pytest.mark.asyncio
    async def test_break_without_condition(self) -> None:
        executor = NodeExecutor()
        node = LoopBreakConfig(node_id="b1", label="B", position={"x": 0, "y": 0})
        result = await executor.execute_loop_break(node, _context(), {})
        assert result.output_port == "break"

    @pytest.mark.asyncio
    async def test_break_condition_true(self) -> None:
        executor = NodeExecutor()
        node = LoopBreakConfig(
            node_id="b1", label="B", position={"x": 0, "y": 0},
            break_condition=[ConditionRule(variable="x", operator=OperatorType.EQUALS, value=1)],
        )
        ctx = _context()
        ctx.set_variable("x", 1)
        result = await executor.execute_loop_break(node, ctx, {})
        assert result.output_port == "break"

    @pytest.mark.asyncio
    async def test_break_condition_false_continues(self) -> None:
        executor = NodeExecutor()
        node = LoopBreakConfig(
            node_id="b1", label="B", position={"x": 0, "y": 0},
            break_condition=[ConditionRule(variable="x", operator=OperatorType.EQUALS, value=1)],
        )
        ctx = _context()
        ctx.set_variable("x", 2)
        result = await executor.execute_loop_break(node, ctx, {})
        assert result.output_port == "continue"


class TestFlowNodes:
    @pytest.mark.asyncio
    async def test_flow_merge_passes_through_input(self) -> None:
        executor = NodeExecutor()
        node = FlowMergeConfig(node_id="m1", label="M", position={"x": 0, "y": 0})
        result = await executor.execute_flow_merge(node, _context(), {"a": 1})
        assert result.output_data == {"a": 1}

    @pytest.mark.asyncio
    async def test_flow_parallel_reports_config(self) -> None:
        executor = NodeExecutor()
        node = FlowParallelConfig(node_id="p1", label="P", position={"x": 0, "y": 0})
        result = await executor.execute_flow_parallel(node, _context(), {})
        assert result.output_port == "parallel"


class TestReplaceVariables:
    def test_empty_template_returns_as_is(self) -> None:
        executor = NodeExecutor()
        assert executor._replace_variables("", _context()) == ""

    def test_missing_variable_replaced_with_empty_string(self) -> None:
        executor = NodeExecutor()
        result = executor._replace_variables("Value: {{missing}}", _context())
        assert result == "Value: "
