"""
Node Executor Tests
===================

Comprehensive tests for NodeExecutor service.
"""

import asyncio
import pytest
from datetime import datetime
from typing import Dict, Any
from unittest.mock import AsyncMock, patch

from services.node_executor import NodeExecutor, NodeExecutionResult
from models.nodes import (
    ConditionIfConfig,
    ConditionSwitchConfig,
    ConditionFilterConfig,
    ConditionRule,
    OperatorType,
    ActionDelayConfig,
    DataVariableSetConfig,
    DataVariableGetConfig,
    LoopForeachConfig,
    FlowEndConfig,
    NodeType,
    PortDefinition,
    PortType,
    DataType,
)
from models.execution import ExecutionContext


@pytest.fixture
def executor():
    """Create node executor instance"""
    return NodeExecutor()


@pytest.fixture
def context():
    """Create execution context"""
    return ExecutionContext(
        execution_id="test-exec-001",
        workflow_id="test-workflow",
        workflow_version="1.0.0",
        session_id="test-session",
        entity_id="test-entity",
        user_id="user123",
        username="testuser",
        platform="twitch",
        variables={
            "test_var": "hello",
            "count": 5,
            "items": ["a", "b", "c"],
        }
    )


# ============================================================================
# CONDITION NODE TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_condition_if_true(executor, context):
    """Test IF condition evaluating to true"""
    node = ConditionIfConfig(
        node_id="cond1",
        label="Test If",
        position={"x": 0, "y": 0},
        condition=[
            ConditionRule(
                variable="count",
                operator=OperatorType.GREATER_THAN,
                value=3
            )
        ],
        output_true_port="true",
        output_false_port="false"
    )

    state = await executor.execute_node(node, context)

    assert state.status.value == "completed"
    assert state.get_output("condition_result") is True
    assert "true" in str(state.logs)


@pytest.mark.asyncio
async def test_condition_if_false(executor, context):
    """Test IF condition evaluating to false"""
    node = ConditionIfConfig(
        node_id="cond2",
        label="Test If False",
        position={"x": 0, "y": 0},
        condition=[
            ConditionRule(
                variable="count",
                operator=OperatorType.LESS_THAN,
                value=3
            )
        ]
    )

    state = await executor.execute_node(node, context)

    assert state.status.value == "completed"
    assert state.get_output("condition_result") is False


@pytest.mark.asyncio
async def test_condition_switch_match(executor, context):
    """Test SWITCH condition with match"""
    node = ConditionSwitchConfig(
        node_id="switch1",
        label="Test Switch",
        position={"x": 0, "y": 0},
        variable="count",
        cases={
            "5": "five",
            "10": "ten",
        },
        default_port="default"
    )

    state = await executor.execute_node(node, context)

    assert state.status.value == "completed"
    assert state.get_output("matched_case") == "5"


@pytest.mark.asyncio
async def test_condition_switch_no_match(executor, context):
    """Test SWITCH condition with no match"""
    node = ConditionSwitchConfig(
        node_id="switch2",
        label="Test Switch No Match",
        position={"x": 0, "y": 0},
        variable="count",
        cases={
            "7": "seven",
            "9": "nine",
        },
        default_port="default"
    )

    state = await executor.execute_node(node, context)

    assert state.status.value == "completed"
    assert state.get_output("matched_case") is None


@pytest.mark.asyncio
async def test_condition_filter(executor, context):
    """Test FILTER condition"""
    # Add array with numeric items
    context.set_variable("numbers", [1, 2, 3, 4, 5, 6])

    node = ConditionFilterConfig(
        node_id="filter1",
        label="Test Filter",
        position={"x": 0, "y": 0},
        input_array="numbers",
        condition=[
            ConditionRule(
                variable="item",
                operator=OperatorType.GREATER_THAN,
                value=3
            )
        ],
        output_variable="filtered"
    )

    state = await executor.execute_node(node, context)

    assert state.status.value == "completed"
    filtered = context.get_variable("filtered")
    assert filtered == [4, 5, 6]


# ============================================================================
# OPERATOR TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_operator_equals(executor, context):
    """Test EQUALS operator"""
    node = ConditionIfConfig(
        node_id="op1",
        label="Test Equals",
        position={"x": 0, "y": 0},
        condition=[
            ConditionRule(
                variable="test_var",
                operator=OperatorType.EQUALS,
                value="hello"
            )
        ]
    )

    state = await executor.execute_node(node, context)
    assert state.get_output("condition_result") is True


@pytest.mark.asyncio
async def test_operator_contains(executor, context):
    """Test CONTAINS operator"""
    node = ConditionIfConfig(
        node_id="op2",
        label="Test Contains",
        position={"x": 0, "y": 0},
        condition=[
            ConditionRule(
                variable="test_var",
                operator=OperatorType.CONTAINS,
                value="ell"
            )
        ]
    )

    state = await executor.execute_node(node, context)
    assert state.get_output("condition_result") is True


@pytest.mark.asyncio
async def test_operator_regex(executor, context):
    """Test MATCHES_REGEX operator"""
    node = ConditionIfConfig(
        node_id="op3",
        label="Test Regex",
        position={"x": 0, "y": 0},
        condition=[
            ConditionRule(
                variable="test_var",
                operator=OperatorType.MATCHES_REGEX,
                value=r"^h.*o$"
            )
        ]
    )

    state = await executor.execute_node(node, context)
    assert state.get_output("condition_result") is True


@pytest.mark.asyncio
async def test_operator_in_list(executor, context):
    """Test IN_LIST operator"""
    node = ConditionIfConfig(
        node_id="op4",
        label="Test In List",
        position={"x": 0, "y": 0},
        condition=[
            ConditionRule(
                variable="test_var",
                operator=OperatorType.IN_LIST,
                value=["hello", "world"]
            )
        ]
    )

    state = await executor.execute_node(node, context)
    assert state.get_output("condition_result") is True


# ============================================================================
# ACTION NODE TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_action_delay(executor, context):
    """Test ACTION_DELAY node"""
    node = ActionDelayConfig(
        node_id="delay1",
        label="Test Delay",
        position={"x": 0, "y": 0},
        delay_ms=100  # 100ms
    )

    start = datetime.utcnow()
    state = await executor.execute_node(node, context)
    elapsed = (datetime.utcnow() - start).total_seconds()

    assert state.status.value == "completed"
    assert elapsed >= 0.1  # At least 100ms
    assert state.get_output("delayed_ms") == 100


@pytest.mark.asyncio
async def test_action_delay_variable(executor, context):
    """Test ACTION_DELAY with variable"""
    context.set_variable("delay_time", 50)

    node = ActionDelayConfig(
        node_id="delay2",
        label="Test Delay Variable",
        position={"x": 0, "y": 0},
        delay_ms=0,
        delay_variable="delay_time"
    )

    state = await executor.execute_node(node, context)

    assert state.status.value == "completed"
    assert state.get_output("delayed_ms") == 50


@pytest.mark.asyncio
async def test_action_delay_max_limit(executor, context):
    """Test ACTION_DELAY enforces max limit.

    Patches asyncio.sleep so this assertion doesn't actually block the
    suite for the real 300s (5 minute) cap being asserted.
    """
    node = ActionDelayConfig(
        node_id="delay3",
        label="Test Delay Max",
        position={"x": 0, "y": 0},
        delay_ms=999999999  # Way too long
    )

    with patch("services.node_executor.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        state = await executor.execute_node(node, context)

    assert state.status.value == "completed"
    # Should be capped at 300000ms (5 minutes)
    assert state.get_output("delayed_ms") == 300000
    mock_sleep.assert_awaited_once_with(300.0)


# ============================================================================
# DATA NODE TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_data_variable_set(executor, context):
    """Test DATA_VARIABLE_SET node"""
    node = DataVariableSetConfig(
        node_id="set1",
        label="Test Set",
        position={"x": 0, "y": 0},
        variables={
            "new_var": "test_value",
            "new_count": 42,
        }
    )

    state = await executor.execute_node(node, context)

    assert state.status.value == "completed"
    assert context.get_variable("new_var") == "test_value"
    assert context.get_variable("new_count") == 42


@pytest.mark.asyncio
async def test_data_variable_set_with_template(executor, context):
    """Test DATA_VARIABLE_SET with variable replacement"""
    node = DataVariableSetConfig(
        node_id="set2",
        label="Test Set Template",
        position={"x": 0, "y": 0},
        variables={
            "greeting": "Hello {{test_var}}!",
        }
    )

    state = await executor.execute_node(node, context)

    assert state.status.value == "completed"
    assert context.get_variable("greeting") == "Hello hello!"


@pytest.mark.asyncio
async def test_data_variable_get(executor, context):
    """Test DATA_VARIABLE_GET node"""
    node = DataVariableGetConfig(
        node_id="get1",
        label="Test Get",
        position={"x": 0, "y": 0},
        variable_name="test_var",
        output_variable="retrieved_var"
    )

    state = await executor.execute_node(node, context)

    assert state.status.value == "completed"
    assert context.get_variable("retrieved_var") == "hello"


@pytest.mark.asyncio
async def test_data_variable_get_default(executor, context):
    """Test DATA_VARIABLE_GET with default value"""
    node = DataVariableGetConfig(
        node_id="get2",
        label="Test Get Default",
        position={"x": 0, "y": 0},
        variable_name="nonexistent",
        output_variable="result",
        default_value="default_value"
    )

    state = await executor.execute_node(node, context)

    assert state.status.value == "completed"
    assert context.get_variable("result") == "default_value"


# ============================================================================
# LOOP NODE TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_loop_foreach(executor, context):
    """Test LOOP_FOREACH node"""
    node = LoopForeachConfig(
        node_id="loop1",
        label="Test Foreach",
        position={"x": 0, "y": 0},
        array_variable="items",
        item_variable="current_item",
        index_variable="current_index"
    )

    state = await executor.execute_node(node, context)

    assert state.status.value == "completed"
    loop_state = state.get_output("loop_state")
    assert loop_state["array"] == ["a", "b", "c"]
    assert loop_state["length"] == 3


@pytest.mark.asyncio
async def test_loop_foreach_max_iterations(executor, context):
    """Test LOOP_FOREACH enforces max iterations"""
    # Create large array
    context.set_variable("large_array", list(range(20000)))

    node = LoopForeachConfig(
        node_id="loop2",
        label="Test Foreach Max",
        position={"x": 0, "y": 0},
        array_variable="large_array",
        max_iterations=10000
    )

    state = await executor.execute_node(node, context)

    assert state.status.value == "failed"
    assert "exceeds max iterations" in state.error


# ============================================================================
# FLOW NODE TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_flow_end(executor, context):
    """Test FLOW_END node"""
    node = FlowEndConfig(
        node_id="end1",
        label="Test End",
        position={"x": 0, "y": 0},
        final_output_port="end"
    )

    state = await executor.execute_node(node, context)

    assert state.status.value == "completed"
    assert "Workflow end reached" in str(state.logs)


# ============================================================================
# VARIABLE REPLACEMENT TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_variable_replacement_basic(executor, context):
    """Test basic variable replacement"""
    result = executor._replace_variables("Hello {{test_var}}!", context)
    assert result == "Hello hello!"


@pytest.mark.asyncio
async def test_variable_replacement_multiple(executor, context):
    """Test multiple variable replacements"""
    result = executor._replace_variables(
        "{{test_var}} world, count is {{count}}",
        context
    )
    assert result == "hello world, count is 5"


@pytest.mark.asyncio
async def test_variable_replacement_missing(executor, context):
    """Test variable replacement with missing variable"""
    result = executor._replace_variables("Value: {{missing}}", context)
    assert result == "Value: "


# ============================================================================
# ERROR HANDLING TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_condition_filter_not_array(executor, context):
    """Test FILTER with non-array variable"""
    context.set_variable("not_array", "string value")

    node = ConditionFilterConfig(
        node_id="filter_err",
        label="Test Filter Error",
        position={"x": 0, "y": 0},
        input_array="not_array",
        condition=[],
        output_variable="filtered"
    )

    state = await executor.execute_node(node, context)

    assert state.status.value == "failed"
    assert "is not an array" in state.error


@pytest.mark.asyncio
async def test_action_delay_negative(executor, context):
    """Test ACTION_DELAY with negative value"""
    node = ActionDelayConfig(
        node_id="delay_err",
        label="Test Delay Negative",
        position={"x": 0, "y": 0},
        delay_ms=-100
    )

    state = await executor.execute_node(node, context)

    assert state.status.value == "failed"
    assert "cannot be negative" in state.error


# ============================================================================
# ASYNC CONTEXT MANAGER TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_context_manager():
    """Test NodeExecutor as async context manager"""
    async with NodeExecutor() as executor:
        context = ExecutionContext(
            execution_id="test",
            workflow_id="test",
            workflow_version="1.0",
            session_id="test",
            entity_id="test",
            user_id="test"
        )

        node = FlowEndConfig(
            node_id="end",
            label="End",
            position={"x": 0, "y": 0}
        )

        state = await executor.execute_node(node, context)
        assert state.status.value == "completed"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
