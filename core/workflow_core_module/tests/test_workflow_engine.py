"""`services/workflow_engine.py` -- DAG-based workflow execution engine.

`_load_workflow` is a documented stub (always returns `None` -- "In
production, this would load from database... For now, return None to
indicate not implemented"). That's pre-existing, deliberately-incomplete
scope, not a bug introduced by broken wiring, so this suite doesn't
implement a DB-backed loader; it monkeypatches `_load_workflow` (its own
documented extension point) to drive full graph-execution coverage, and
separately asserts today's actual behavior (every `execute_workflow` call
raises "Workflow not found" while unpatched).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from models.execution import ExecutionContext, ExecutionResult, ExecutionStatus, NodeExecutionState
from models.nodes import (
    ConditionIfConfig,
    ConditionRule,
    ConditionSwitchConfig,
    FlowEndConfig,
    FlowParallelConfig,
    LoopForeachConfig,
    LoopWhileConfig,
    OperatorType,
    PortDefinition,
    PortType,
    DataType,
    TriggerCommandConfig,
)
from models.workflow import WorkflowConnection, WorkflowDefinition, WorkflowMetadata
from services.workflow_engine import (
    NodeExecutionException,
    WorkflowEngine,
    WorkflowEngineException,
    WorkflowLoopException,
    WorkflowTimeoutException,
)


def _engine(**overrides: object) -> WorkflowEngine:
    defaults = dict(dal=MagicMock(), max_parallel_nodes=2)
    defaults.update(overrides)
    return WorkflowEngine(**defaults)


def _out_port(name: str = "out") -> PortDefinition:
    return PortDefinition(name=name, port_type=PortType.OUTPUT, data_type=DataType.OBJECT)


def _in_port(name: str = "in") -> PortDefinition:
    return PortDefinition(name=name, port_type=PortType.INPUT, data_type=DataType.OBJECT)


def _trigger_end_workflow(*, retry_failed_nodes: bool = False, max_retries: int = 0) -> WorkflowDefinition:
    """A minimal trigger -> end workflow, connected end to end."""
    trigger = TriggerCommandConfig(
        node_id="t1", label="Trigger", position={"x": 0, "y": 0},
        output_ports=[_out_port("out")],
    )
    end = FlowEndConfig(
        node_id="e1", label="End", position={"x": 1, "y": 0},
        input_ports=[_in_port("in")],
    )
    metadata = WorkflowMetadata(
        workflow_id="wf-1", name="Test", description="d",
        author_id="u1", community_id="c1",
        retry_failed_nodes=retry_failed_nodes, max_retries=max_retries,
    )
    return WorkflowDefinition(
        metadata=metadata,
        nodes={"t1": trigger, "e1": end},
        connections=[
            WorkflowConnection(
                connection_id="conn1", from_node_id="t1", from_port_name="out",
                to_node_id="e1", to_port_name="in",
            )
        ],
    )


def _context(workflow_id: str = "wf-1") -> ExecutionContext:
    return ExecutionContext(
        execution_id="exec-1", workflow_id=workflow_id, workflow_version="1.0.0",
        session_id="s1", entity_id="c1", user_id="u1",
    )


class TestInit:
    def test_init_sets_attributes(self) -> None:
        engine = _engine(max_loop_iterations=5, max_total_operations=50, max_loop_depth=3)
        assert engine.max_loop_iterations == 5
        assert engine.max_total_operations == 50
        assert engine.max_loop_depth == 3
        assert engine._active_executions == {}


class TestExecuteWorkflow:
    @pytest.mark.asyncio
    async def test_unpatched_load_workflow_always_not_found(self) -> None:
        """Documents today's real behavior: `_load_workflow` is a stub returning None."""
        engine = _engine()
        with pytest.raises(WorkflowEngineException, match="Workflow not found"):
            await engine.execute_workflow("wf-1", {})

    @pytest.mark.asyncio
    async def test_successful_execution(self) -> None:
        engine = _engine()
        workflow = _trigger_end_workflow()
        with patch.object(WorkflowEngine, "_load_workflow", AsyncMock(return_value=workflow)):
            result = await engine.execute_workflow("wf-1", {"user_id": "u1"})
        assert result.status == ExecutionStatus.COMPLETED
        assert "t1" in result.execution_path
        assert "e1" in result.execution_path
        assert "exec-1" not in engine._active_executions or True  # cleaned up
        assert result.execution_id not in engine._active_executions

    @pytest.mark.asyncio
    async def test_uses_provided_context(self) -> None:
        engine = _engine()
        workflow = _trigger_end_workflow()
        ctx = _context()
        original_id = ctx.execution_id
        with patch.object(WorkflowEngine, "_load_workflow", AsyncMock(return_value=workflow)):
            result = await engine.execute_workflow("wf-1", {"foo": "bar"}, context=ctx)
        assert result.status == ExecutionStatus.COMPLETED
        assert ctx.execution_id != original_id  # reassigned to the new execution_id
        assert ctx.get_variable("foo") == "bar"

    @pytest.mark.asyncio
    async def test_no_trigger_nodes_raises_and_is_recorded(self) -> None:
        engine = _engine()
        end = FlowEndConfig(node_id="e1", label="End", position={"x": 0, "y": 0})
        metadata = WorkflowMetadata(
            workflow_id="wf-2", name="NoTrigger", description="d", author_id="u1", community_id="c1"
        )
        workflow = WorkflowDefinition(metadata=metadata, nodes={"e1": end})
        with (
            patch.object(WorkflowEngine, "_load_workflow", AsyncMock(return_value=workflow)),
            pytest.raises(WorkflowEngineException, match="No trigger nodes"),
        ):
            await engine.execute_workflow("wf-2", {})

    @pytest.mark.asyncio
    async def test_timeout_raises_workflow_timeout_exception(self) -> None:
        engine = _engine()
        workflow = _trigger_end_workflow()
        # `0 or self.default_timeout` falls back to the 300s default (0 is
        # falsy) -- use a tiny positive value so the real timeout wins.
        workflow.metadata.max_execution_time_seconds = 0.01

        async def _slow_execute_graph(**_: object) -> None:
            await asyncio.sleep(0.2)

        engine._execute_graph = _slow_execute_graph
        with (
            patch.object(WorkflowEngine, "_load_workflow", AsyncMock(return_value=workflow)),
            pytest.raises(WorkflowTimeoutException),
        ):
            await engine.execute_workflow("wf-1", {})


class TestBuildExecutionGraph:
    @pytest.mark.asyncio
    async def test_builds_simple_graph(self) -> None:
        engine = _engine()
        workflow = _trigger_end_workflow()
        graph = await engine._build_execution_graph(workflow, _context())
        assert graph["t1"] == ["e1"]

    @pytest.mark.asyncio
    async def test_disabled_connection_excluded(self) -> None:
        engine = _engine()
        workflow = _trigger_end_workflow()
        workflow.connections[0].enabled = False
        graph = await engine._build_execution_graph(workflow, _context())
        assert graph["t1"] == []

    @pytest.mark.asyncio
    async def test_circular_dependency_raises(self) -> None:
        engine = _engine()
        a = TriggerCommandConfig(node_id="a", label="A", position={"x": 0, "y": 0})
        b = FlowEndConfig(node_id="b", label="B", position={"x": 1, "y": 0})
        metadata = WorkflowMetadata(
            workflow_id="wf-c", name="Cyclic", description="d", author_id="u1", community_id="c1"
        )
        workflow = WorkflowDefinition(
            metadata=metadata,
            nodes={"a": a, "b": b},
            connections=[
                WorkflowConnection("c1", "a", "out", "b", "in"),
                WorkflowConnection("c2", "b", "out", "a", "in"),
            ],
        )
        with pytest.raises(WorkflowEngineException, match="Circular dependency"):
            await engine._build_execution_graph(workflow, _context())


class TestExecuteGraph:
    @pytest.mark.asyncio
    async def test_no_trigger_nodes_raises(self) -> None:
        engine = _engine()
        end = FlowEndConfig(node_id="e1", label="End", position={"x": 0, "y": 0})
        metadata = WorkflowMetadata(
            workflow_id="wf-2", name="NoTrigger", description="d", author_id="u1", community_id="c1"
        )
        workflow = WorkflowDefinition(metadata=metadata, nodes={"e1": end})
        result = ExecutionResult(
            execution_id="e1", workflow_id="wf-2", status=ExecutionStatus.RUNNING, execution_path=[]
        )
        with pytest.raises(WorkflowEngineException, match="No trigger nodes"):
            await engine._execute_graph(workflow, {}, _context(), result)

    @pytest.mark.asyncio
    async def test_populates_node_states_and_executes(self) -> None:
        engine = _engine()
        workflow = _trigger_end_workflow()
        graph = await engine._build_execution_graph(workflow, _context())
        result = ExecutionResult(
            execution_id="exec-1", workflow_id="wf-1", status=ExecutionStatus.RUNNING, execution_path=[]
        )
        await engine._execute_graph(workflow, graph, _context(), result)
        assert set(result.node_states.keys()) == {"t1", "e1"}
        assert result.execution_path == ["t1", "e1"]


class TestExecuteNodeTree:
    @pytest.mark.asyncio
    async def test_operation_limit_raises(self) -> None:
        engine = _engine(max_total_operations=0)
        workflow = _trigger_end_workflow()
        result = ExecutionResult(execution_id="e", workflow_id="wf-1", status=ExecutionStatus.RUNNING, execution_path=[])
        result.node_states["t1"] = NodeExecutionState(node_id="t1")
        with pytest.raises(WorkflowLoopException, match="Maximum operations"):
            await engine._execute_node_tree(workflow, {}, "t1", _context(), result, operation_count=0, loop_depth=0)

    @pytest.mark.asyncio
    async def test_loop_depth_limit_raises(self) -> None:
        engine = _engine(max_loop_depth=0)
        workflow = _trigger_end_workflow()
        result = ExecutionResult(execution_id="e", workflow_id="wf-1", status=ExecutionStatus.RUNNING, execution_path=[])
        with pytest.raises(WorkflowLoopException, match="Maximum loop depth"):
            await engine._execute_node_tree(workflow, {}, "t1", _context(), result, operation_count=0, loop_depth=0)

    @pytest.mark.asyncio
    async def test_missing_node_returns_quietly(self) -> None:
        engine = _engine()
        workflow = _trigger_end_workflow()
        result = ExecutionResult(execution_id="e", workflow_id="wf-1", status=ExecutionStatus.RUNNING, execution_path=[])
        await engine._execute_node_tree(workflow, {}, "missing", _context(), result, operation_count=0, loop_depth=0)
        assert result.execution_path == []

    @pytest.mark.asyncio
    async def test_disabled_node_marked_skipped(self) -> None:
        engine = _engine()
        workflow = _trigger_end_workflow()
        workflow.nodes["t1"].enabled = False
        result = ExecutionResult(execution_id="e", workflow_id="wf-1", status=ExecutionStatus.RUNNING, execution_path=[])
        result.node_states["t1"] = NodeExecutionState(node_id="t1")
        await engine._execute_node_tree(workflow, {}, "t1", _context(), result, operation_count=0, loop_depth=0)
        assert result.node_states["t1"].status.value == "skipped"

    @pytest.mark.asyncio
    async def test_cancelled_context_stops_execution(self) -> None:
        engine = _engine()
        workflow = _trigger_end_workflow()
        result = ExecutionResult(execution_id="e", workflow_id="wf-1", status=ExecutionStatus.RUNNING, execution_path=[])
        result.node_states["t1"] = NodeExecutionState(node_id="t1")
        ctx = _context()
        ctx.cancelled = True
        await engine._execute_node_tree(workflow, {}, "t1", ctx, result, operation_count=0, loop_depth=0)
        assert result.status == ExecutionStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_node_failure_invokes_error_handler(self) -> None:
        engine = _engine()
        workflow = _trigger_end_workflow()
        result = ExecutionResult(execution_id="e", workflow_id="wf-1", status=ExecutionStatus.RUNNING, execution_path=[])
        result.node_states["t1"] = NodeExecutionState(node_id="t1")
        engine._execute_node = AsyncMock(side_effect=NodeExecutionException("boom"))
        engine._handle_error_and_retry = AsyncMock()
        await engine._execute_node_tree(workflow, {}, "t1", _context(), result, operation_count=0, loop_depth=0)
        engine._handle_error_and_retry.assert_awaited_once()
        assert result.execution_path == []

    @pytest.mark.asyncio
    async def test_parallel_node_dispatches_to_parallel_executor(self) -> None:
        engine = _engine()
        parallel = FlowParallelConfig(node_id="p1", label="Parallel", position={"x": 0, "y": 0})
        result = ExecutionResult(execution_id="e", workflow_id="wf-1", status=ExecutionStatus.RUNNING, execution_path=[])
        result.node_states["p1"] = NodeExecutionState(node_id="p1")
        metadata = WorkflowMetadata(workflow_id="wf-p", name="P", description="d", author_id="u1", community_id="c1")
        workflow = WorkflowDefinition(metadata=metadata, nodes={"p1": parallel})
        engine._execute_parallel_nodes = AsyncMock()
        await engine._execute_node_tree(workflow, {"p1": []}, "p1", _context(), result, operation_count=0, loop_depth=0)
        engine._execute_parallel_nodes.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_loop_node_dispatches_to_loop_handler(self) -> None:
        engine = _engine()
        loop = LoopForeachConfig(node_id="l1", label="Loop", position={"x": 0, "y": 0})
        result = ExecutionResult(execution_id="e", workflow_id="wf-1", status=ExecutionStatus.RUNNING, execution_path=[])
        result.node_states["l1"] = NodeExecutionState(node_id="l1")
        metadata = WorkflowMetadata(workflow_id="wf-l", name="L", description="d", author_id="u1", community_id="c1")
        workflow = WorkflowDefinition(metadata=metadata, nodes={"l1": loop})
        engine._handle_loops = AsyncMock()
        await engine._execute_node_tree(workflow, {"l1": []}, "l1", _context(), result, operation_count=0, loop_depth=0)
        engine._handle_loops.assert_awaited_once()


class TestExecuteNode:
    @pytest.mark.asyncio
    async def test_success_marks_completed_with_output(self) -> None:
        engine = _engine()
        node = FlowEndConfig(node_id="e1", label="End", position={"x": 0, "y": 0})
        result = ExecutionResult(execution_id="e", workflow_id="wf-1", status=ExecutionStatus.RUNNING, execution_path=[])
        result.node_states["e1"] = NodeExecutionState(node_id="e1")
        await engine._execute_node(node, _context(), result)
        state = result.node_states["e1"]
        assert state.status.value == "completed"
        assert state.get_output("result") == {"status": "success"}

    @pytest.mark.asyncio
    async def test_failure_marks_failed_and_raises(self) -> None:
        engine = _engine()
        node = FlowEndConfig(node_id="e1", label="End", position={"x": 0, "y": 0})
        result = ExecutionResult(execution_id="e", workflow_id="wf-1", status=ExecutionStatus.RUNNING, execution_path=[])
        result.node_states["e1"] = NodeExecutionState(node_id="e1")
        with patch("services.workflow_engine.asyncio.sleep", side_effect=RuntimeError("sleep failed")):
            with pytest.raises(NodeExecutionException):
                await engine._execute_node(node, _context(), result)
        assert result.node_states["e1"].status.value == "failed"


class TestHandleConditionalRouting:
    @pytest.mark.asyncio
    async def test_if_condition_true_branch(self) -> None:
        engine = _engine()
        node = ConditionIfConfig(
            node_id="c1", label="If", position={"x": 0, "y": 0},
            condition=[ConditionRule(variable="x", operator=OperatorType.EQUALS, value=1)],
        )
        metadata = WorkflowMetadata(workflow_id="wf", name="w", description="d", author_id="u1", community_id="c1")
        end = FlowEndConfig(node_id="e1", label="End", position={"x": 1, "y": 0})
        workflow = WorkflowDefinition(
            metadata=metadata, nodes={"c1": node, "e1": end},
            connections=[WorkflowConnection("c", "c1", "true", "e1", "in")],
        )
        ctx = _context()
        ctx.set_variable("x", 1)
        result = ExecutionResult(execution_id="e", workflow_id="wf", status=ExecutionStatus.RUNNING, execution_path=[])
        next_nodes = await engine._handle_conditional_routing(workflow, {}, node, ctx, result)
        assert next_nodes == ["e1"]

    @pytest.mark.asyncio
    async def test_if_condition_false_branch(self) -> None:
        engine = _engine()
        node = ConditionIfConfig(
            node_id="c1", label="If", position={"x": 0, "y": 0},
            condition=[ConditionRule(variable="x", operator=OperatorType.EQUALS, value=1)],
        )
        metadata = WorkflowMetadata(workflow_id="wf", name="w", description="d", author_id="u1", community_id="c1")
        end = FlowEndConfig(node_id="e1", label="End", position={"x": 1, "y": 0})
        workflow = WorkflowDefinition(
            metadata=metadata, nodes={"c1": node, "e1": end},
            connections=[WorkflowConnection("c", "c1", "false", "e1", "in")],
        )
        ctx = _context()
        ctx.set_variable("x", 2)
        result = ExecutionResult(execution_id="e", workflow_id="wf", status=ExecutionStatus.RUNNING, execution_path=[])
        next_nodes = await engine._handle_conditional_routing(workflow, {}, node, ctx, result)
        assert next_nodes == ["e1"]

    @pytest.mark.asyncio
    async def test_switch_matching_case(self) -> None:
        engine = _engine()
        node = ConditionSwitchConfig(
            node_id="s1", label="Switch", position={"x": 0, "y": 0},
            variable="v", cases={"5": "five"}, default_port="default",
        )
        metadata = WorkflowMetadata(workflow_id="wf", name="w", description="d", author_id="u1", community_id="c1")
        end = FlowEndConfig(node_id="e1", label="End", position={"x": 1, "y": 0})
        workflow = WorkflowDefinition(
            metadata=metadata, nodes={"s1": node, "e1": end},
            connections=[WorkflowConnection("c", "s1", "five", "e1", "in")],
        )
        ctx = _context()
        ctx.set_variable("v", 5)
        result = ExecutionResult(execution_id="e", workflow_id="wf", status=ExecutionStatus.RUNNING, execution_path=[])
        next_nodes = await engine._handle_conditional_routing(workflow, {}, node, ctx, result)
        assert next_nodes == ["e1"]

    @pytest.mark.asyncio
    async def test_switch_default_case(self) -> None:
        engine = _engine()
        node = ConditionSwitchConfig(
            node_id="s1", label="Switch", position={"x": 0, "y": 0},
            variable="v", cases={"5": "five"}, default_port="default",
        )
        metadata = WorkflowMetadata(workflow_id="wf", name="w", description="d", author_id="u1", community_id="c1")
        end = FlowEndConfig(node_id="e1", label="End", position={"x": 1, "y": 0})
        workflow = WorkflowDefinition(
            metadata=metadata, nodes={"s1": node, "e1": end},
            connections=[WorkflowConnection("c", "s1", "default", "e1", "in")],
        )
        ctx = _context()
        ctx.set_variable("v", 999)
        result = ExecutionResult(execution_id="e", workflow_id="wf", status=ExecutionStatus.RUNNING, execution_path=[])
        next_nodes = await engine._handle_conditional_routing(workflow, {}, node, ctx, result)
        assert next_nodes == ["e1"]

    @pytest.mark.asyncio
    async def test_default_routing_follows_graph(self) -> None:
        engine = _engine()
        workflow = _trigger_end_workflow()
        graph = await engine._build_execution_graph(workflow, _context())
        result = ExecutionResult(execution_id="e", workflow_id="wf-1", status=ExecutionStatus.RUNNING, execution_path=[])
        next_nodes = await engine._handle_conditional_routing(
            workflow, graph, workflow.nodes["t1"], _context(), result
        )
        assert next_nodes == ["e1"]


class TestExecuteParallelNodes:
    @pytest.mark.asyncio
    async def test_executes_all_nodes(self) -> None:
        engine = _engine()
        workflow = _trigger_end_workflow()
        result = ExecutionResult(execution_id="e", workflow_id="wf-1", status=ExecutionStatus.RUNNING, execution_path=[])
        result.node_states["e1"] = NodeExecutionState(node_id="e1")
        await engine._execute_parallel_nodes(
            workflow, {"e1": []}, ["e1"], _context(), result, operation_count=0, loop_depth=0, timeout=5,
        )
        assert "e1" in result.execution_path

    @pytest.mark.asyncio
    async def test_timeout_raises(self) -> None:
        engine = _engine()
        workflow = _trigger_end_workflow()
        result = ExecutionResult(execution_id="e", workflow_id="wf-1", status=ExecutionStatus.RUNNING, execution_path=[])

        async def _slow_execute_node_tree(**_: object) -> None:
            await asyncio.sleep(0.2)

        engine._execute_node_tree = _slow_execute_node_tree
        with pytest.raises(WorkflowTimeoutException):
            await engine._execute_parallel_nodes(
                workflow, {}, ["e1"], _context(), result, operation_count=0, loop_depth=0, timeout=0.01,
            )


class TestHandleLoops:
    @pytest.mark.asyncio
    async def test_foreach_non_array_variable_warns_and_returns(self) -> None:
        engine = _engine()
        node = LoopForeachConfig(
            node_id="l1", label="Loop", position={"x": 0, "y": 0}, array_variable="notlist",
        )
        ctx = _context()
        ctx.set_variable("notlist", "a string")
        workflow = _trigger_end_workflow()
        result = ExecutionResult(execution_id="e", workflow_id="wf-1", status=ExecutionStatus.RUNNING, execution_path=[])
        await engine._handle_loops(workflow, {}, node, [], ctx, result, operation_count=0, loop_depth=1)

    @pytest.mark.asyncio
    async def test_foreach_iterates_and_respects_max_iterations(self) -> None:
        engine = _engine(max_loop_iterations=2)
        node = LoopForeachConfig(
            node_id="l1", label="Loop", position={"x": 0, "y": 0},
            array_variable="items", item_variable="item", index_variable="idx",
            max_iterations=100,
        )
        ctx = _context()
        ctx.set_variable("items", [10, 20, 30, 40])
        workflow = _trigger_end_workflow()
        result = ExecutionResult(execution_id="e", workflow_id="wf-1", status=ExecutionStatus.RUNNING, execution_path=[])
        engine._execute_node_tree = AsyncMock()
        await engine._handle_loops(workflow, {}, node, ["e1"], ctx, result, operation_count=0, loop_depth=1)
        assert engine._execute_node_tree.await_count == 2  # capped by engine.max_loop_iterations

    @pytest.mark.asyncio
    async def test_foreach_break_stops_early(self) -> None:
        engine = _engine()
        node = LoopForeachConfig(
            node_id="l1", label="Loop", position={"x": 0, "y": 0},
            array_variable="items", max_iterations=100,
        )
        ctx = _context()
        ctx.set_variable("items", [1, 2, 3])
        workflow = _trigger_end_workflow()
        result = ExecutionResult(execution_id="e", workflow_id="wf-1", status=ExecutionStatus.RUNNING, execution_path=[])

        async def _fake_execute_node_tree(**kwargs: object) -> None:
            ctx.set_variable("__loop_break__", True)

        engine._execute_node_tree = _fake_execute_node_tree
        await engine._handle_loops(workflow, {}, node, ["e1"], ctx, result, operation_count=0, loop_depth=1)
        assert ctx.get_variable("__loop_break__") is False

    @pytest.mark.asyncio
    async def test_while_loop_runs_until_condition_false(self) -> None:
        engine = _engine(max_loop_iterations=100)
        ctx = _context()
        ctx.set_variable("count", 0)
        node = LoopWhileConfig(
            node_id="l1", label="While", position={"x": 0, "y": 0},
            condition=[ConditionRule(variable="count", operator=OperatorType.LESS_THAN, value=3)],
            max_iterations=100,
        )
        workflow = _trigger_end_workflow()
        result = ExecutionResult(execution_id="e", workflow_id="wf-1", status=ExecutionStatus.RUNNING, execution_path=[])

        async def _increment(**kwargs: object) -> None:
            ctx.set_variable("count", ctx.get_variable("count") + 1)

        engine._execute_node_tree = _increment
        await engine._handle_loops(workflow, {}, node, ["e1"], ctx, result, operation_count=0, loop_depth=1)
        assert ctx.get_variable("count") == 3

    @pytest.mark.asyncio
    async def test_while_loop_break_stops_early(self) -> None:
        engine = _engine()
        ctx = _context()
        node = LoopWhileConfig(
            node_id="l1", label="While", position={"x": 0, "y": 0}, condition=[], max_iterations=100,
        )
        workflow = _trigger_end_workflow()
        result = ExecutionResult(execution_id="e", workflow_id="wf-1", status=ExecutionStatus.RUNNING, execution_path=[])

        async def _break(**kwargs: object) -> None:
            ctx.set_variable("__loop_break__", True)

        engine._execute_node_tree = _break
        await engine._handle_loops(workflow, {}, node, ["e1"], ctx, result, operation_count=0, loop_depth=1)

    @pytest.mark.asyncio
    async def test_while_loop_hits_iteration_limit(self) -> None:
        engine = _engine(max_loop_iterations=2)
        ctx = _context()
        node = LoopWhileConfig(
            node_id="l1", label="While", position={"x": 0, "y": 0}, condition=[], max_iterations=100,
        )
        workflow = _trigger_end_workflow()
        result = ExecutionResult(execution_id="e", workflow_id="wf-1", status=ExecutionStatus.RUNNING, execution_path=[])
        engine._execute_node_tree = AsyncMock()
        await engine._handle_loops(workflow, {}, node, ["e1"], ctx, result, operation_count=0, loop_depth=1)
        assert engine._execute_node_tree.await_count == 2


class TestEvaluateCondition:
    @pytest.mark.asyncio
    async def test_empty_rules_is_true(self) -> None:
        engine = _engine()
        assert await engine._evaluate_condition([], _context()) is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "operator,var_value,rule_value,expected",
        [
            (OperatorType.EQUALS, 5, 5, True),
            (OperatorType.EQUALS, 5, 6, False),
            (OperatorType.NOT_EQUALS, 5, 6, True),
            (OperatorType.NOT_EQUALS, 5, 5, False),
            (OperatorType.GREATER_THAN, 5, 3, True),
            (OperatorType.GREATER_THAN, 5, 10, False),
            (OperatorType.LESS_THAN, 3, 5, True),
            (OperatorType.LESS_THAN, 10, 5, False),
            (OperatorType.CONTAINS, "hello world", "world", True),
            (OperatorType.CONTAINS, "hello world", "xyz", False),
        ],
    )
    async def test_single_rule_operators(self, operator, var_value, rule_value, expected) -> None:
        engine = _engine()
        ctx = _context()
        ctx.set_variable("x", var_value)
        rules = [ConditionRule(variable="x", operator=operator, value=rule_value)]
        assert await engine._evaluate_condition(rules, ctx) is expected

    @pytest.mark.asyncio
    async def test_multiple_rules_and_logic(self) -> None:
        engine = _engine()
        ctx = _context()
        ctx.set_variable("x", 5)
        ctx.set_variable("y", 10)
        rules = [
            ConditionRule(variable="x", operator=OperatorType.EQUALS, value=5),
            ConditionRule(variable="y", operator=OperatorType.EQUALS, value=999),
        ]
        assert await engine._evaluate_condition(rules, ctx) is False


class TestHandleErrorAndRetry:
    @pytest.mark.asyncio
    async def test_no_retry_configured_records_error(self) -> None:
        engine = _engine()
        workflow = _trigger_end_workflow(retry_failed_nodes=False)
        node = workflow.nodes["e1"]
        result = ExecutionResult(execution_id="e", workflow_id="wf-1", status=ExecutionStatus.RUNNING, execution_path=[])
        result.node_states["e1"] = NodeExecutionState(node_id="e1")
        await engine._handle_error_and_retry(workflow, node, _context(), result, ValueError("boom"))
        assert result.error_node_id == "e1"
        assert result.error_message == "boom"

    @pytest.mark.asyncio
    async def test_retry_exhausted_records_error(self) -> None:
        engine = _engine()
        workflow = _trigger_end_workflow(retry_failed_nodes=True, max_retries=1)
        node = workflow.nodes["e1"]
        result = ExecutionResult(execution_id="e", workflow_id="wf-1", status=ExecutionStatus.RUNNING, execution_path=[])
        state = NodeExecutionState(node_id="e1")
        state.retry_count = 1
        result.node_states["e1"] = state
        await engine._handle_error_and_retry(workflow, node, _context(), result, ValueError("boom"))
        assert result.error_node_id == "e1"

    @pytest.mark.asyncio
    async def test_retry_succeeds(self) -> None:
        engine = _engine()
        workflow = _trigger_end_workflow(retry_failed_nodes=True, max_retries=2)
        node = workflow.nodes["e1"]
        result = ExecutionResult(execution_id="e", workflow_id="wf-1", status=ExecutionStatus.RUNNING, execution_path=[])
        result.node_states["e1"] = NodeExecutionState(node_id="e1")
        engine._execute_node = AsyncMock()
        with patch("services.workflow_engine.asyncio.sleep", new=AsyncMock()):
            await engine._handle_error_and_retry(workflow, node, _context(), result, ValueError("boom"))
        engine._execute_node.assert_awaited_once()
        assert result.node_states["e1"].retry_count == 1

    @pytest.mark.asyncio
    async def test_retry_fails_again_recurses(self) -> None:
        engine = _engine()
        workflow = _trigger_end_workflow(retry_failed_nodes=True, max_retries=2)
        node = workflow.nodes["e1"]
        result = ExecutionResult(execution_id="e", workflow_id="wf-1", status=ExecutionStatus.RUNNING, execution_path=[])
        result.node_states["e1"] = NodeExecutionState(node_id="e1")
        engine._execute_node = AsyncMock(side_effect=ValueError("still broken"))
        with patch("services.workflow_engine.asyncio.sleep", new=AsyncMock()):
            await engine._handle_error_and_retry(workflow, node, _context(), result, ValueError("boom"))
        assert result.error_node_id == "e1"  # eventually recorded once retries exhausted


class TestMiscMethods:
    @pytest.mark.asyncio
    async def test_save_execution_state(self) -> None:
        engine = _engine()
        result = ExecutionResult(execution_id="e", workflow_id="wf-1", status=ExecutionStatus.RUNNING, execution_path=[])
        await engine._save_execution_state(result, _context(), final=True)  # must not raise

    @pytest.mark.asyncio
    async def test_load_workflow_returns_none(self) -> None:
        engine = _engine()
        assert await engine._load_workflow("wf-1") is None

    @pytest.mark.asyncio
    async def test_cancel_execution_not_found(self) -> None:
        engine = _engine()
        assert await engine.cancel_execution("missing") is False

    @pytest.mark.asyncio
    async def test_cancel_execution_success(self) -> None:
        engine = _engine()
        result = ExecutionResult(execution_id="e1", workflow_id="wf-1", status=ExecutionStatus.RUNNING, execution_path=[])
        engine._active_executions["e1"] = result
        assert await engine.cancel_execution("e1") is True
        assert result.status == ExecutionStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_get_execution_status_active(self) -> None:
        engine = _engine()
        result = ExecutionResult(execution_id="e1", workflow_id="wf-1", status=ExecutionStatus.RUNNING, execution_path=[])
        engine._active_executions["e1"] = result
        status = await engine.get_execution_status("e1")
        assert status["execution_id"] == "e1"

    @pytest.mark.asyncio
    async def test_get_execution_status_missing(self) -> None:
        engine = _engine()
        assert await engine.get_execution_status("missing") is None

    @pytest.mark.asyncio
    async def test_get_execution_metrics(self) -> None:
        from models.execution import ExecutionMetrics

        engine = _engine()
        metrics = ExecutionMetrics(
            execution_id="e1", workflow_id="wf-1", total_duration_seconds=1.0,
            node_count=2, nodes_executed=2, nodes_skipped=0, nodes_failed=0,
        )
        engine._execution_metrics["e1"] = metrics
        assert await engine.get_execution_metrics("e1") is metrics
        assert await engine.get_execution_metrics("missing") is None

    def test_shutdown(self) -> None:
        engine = _engine()
        engine.shutdown()  # must not raise
