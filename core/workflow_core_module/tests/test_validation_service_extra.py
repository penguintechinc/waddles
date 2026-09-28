"""`services/validation_service.py` -- additional coverage beyond `tests/test_validation_service.py`.

Targets the per-node-type config validators, connection validation, cycle
detection, security-pattern scanning, and the small standalone URL/cron
validators that the original moved-in example suite didn't exercise.
"""

from __future__ import annotations

from models.nodes import (
    ActionBrowserSourceConfig,
    ActionChatMessageConfig,
    ActionDelayConfig,
    ActionModuleConfig,
    ActionWebhookConfig,
    ConditionFilterConfig,
    ConditionIfConfig,
    ConditionRule,
    ConditionSwitchConfig,
    DataTransformConfig,
    DataType,
    DataVariableGetConfig,
    DataVariableSetConfig,
    FlowEndConfig,
    FlowMergeConfig,
    FlowParallelConfig,
    LoopBreakConfig,
    LoopForeachConfig,
    LoopWhileConfig,
    OperatorType,
    PortDefinition,
    PortType,
    TriggerCommandConfig,
    TriggerEventConfig,
    TriggerScheduleConfig,
    TriggerWebhookConfig,
)
from models.workflow import WorkflowConnection, WorkflowDefinition, WorkflowMetadata
from services.validation_service import ValidationResult, WorkflowValidationService


def _out(name: str = "out") -> PortDefinition:
    return PortDefinition(name=name, port_type=PortType.OUTPUT, data_type=DataType.OBJECT)


def _in(name: str = "in") -> PortDefinition:
    return PortDefinition(name=name, port_type=PortType.INPUT, data_type=DataType.OBJECT)


def _svc() -> WorkflowValidationService:
    return WorkflowValidationService()


def _result() -> ValidationResult:
    return ValidationResult(is_valid=True)


def _wf(nodes: dict, connections: list | None = None) -> WorkflowDefinition:
    metadata = WorkflowMetadata(
        workflow_id="wf-1", name="W", description="d", author_id="u1", community_id="c1"
    )
    return WorkflowDefinition(metadata=metadata, nodes=nodes, connections=connections or [])


class TestValidationResult:
    def test_to_dict_counts_node_errors(self) -> None:
        result = _result()
        result.add_node_error("n1", "bad")
        result.add_node_error("n1", "also bad")
        result.add_error("global bad")
        d = result.to_dict()
        assert d["error_count"] == 3
        assert d["is_valid"] is False


class TestNodeLabelAndPosition:
    def test_empty_label_is_node_error(self) -> None:
        svc, result = _svc(), _result()
        node = TriggerCommandConfig(
            node_id="t1", label="  ", position={"x": 0, "y": 0},
            command_pattern="!x", platforms=["twitch"],
        )
        svc._validate_node_config(node, result)
        assert "t1" in result.node_validation_errors

    def test_missing_position_key_is_node_error(self) -> None:
        svc, result = _svc(), _result()
        node = TriggerCommandConfig(
            node_id="t1", label="T", position={"x": 0},
            command_pattern="!x", platforms=["twitch"],
        )
        svc._validate_node_config(node, result)
        assert "t1" in result.node_validation_errors


class TestTriggerValidators:
    def test_trigger_command_valid(self) -> None:
        svc, result = _svc(), _result()
        node = TriggerCommandConfig(
            node_id="t1", label="T", position={"x": 0, "y": 0},
            command_pattern="!help", platforms=["twitch"], cooldown_seconds=5, user_cooldown_seconds=5,
        )
        svc._validate_trigger_command(node, result)
        assert result.is_valid is True

    def test_trigger_command_invalid(self) -> None:
        svc, result = _svc(), _result()
        node = TriggerCommandConfig(
            node_id="t1", label="T", position={"x": 0, "y": 0},
            command_pattern="", platforms=[], cooldown_seconds=-1, user_cooldown_seconds=-1,
        )
        svc._validate_trigger_command(node, result)
        assert len(result.node_validation_errors["t1"]) == 4

    def test_trigger_event_invalid(self) -> None:
        svc, result = _svc(), _result()
        node = TriggerEventConfig(node_id="t1", label="T", position={"x": 0, "y": 0})
        svc._validate_trigger_event(node, result)
        assert len(result.node_validation_errors["t1"]) == 2

    def test_trigger_webhook_valid(self) -> None:
        svc, result = _svc(), _result()
        node = TriggerWebhookConfig(
            node_id="t1", label="T", position={"x": 0, "y": 0}, webhook_path="/hook",
        )
        svc._validate_trigger_webhook(node, result)
        assert result.is_valid is True

    def test_trigger_webhook_missing_leading_slash(self) -> None:
        svc, result = _svc(), _result()
        node = TriggerWebhookConfig(
            node_id="t1", label="T", position={"x": 0, "y": 0}, webhook_path="hook",
        )
        svc._validate_trigger_webhook(node, result)
        assert "t1" in result.node_validation_errors

    def test_trigger_webhook_empty_path(self) -> None:
        svc, result = _svc(), _result()
        node = TriggerWebhookConfig(node_id="t1", label="T", position={"x": 0, "y": 0}, webhook_path="")
        svc._validate_trigger_webhook(node, result)
        assert "t1" in result.node_validation_errors

    def test_trigger_schedule_empty_cron(self) -> None:
        svc, result = _svc(), _result()
        node = TriggerScheduleConfig(node_id="t1", label="T", position={"x": 0, "y": 0}, cron_expression="")
        svc._validate_trigger_schedule(node, result)
        assert "t1" in result.node_validation_errors

    def test_trigger_schedule_invalid_cron(self) -> None:
        svc, result = _svc(), _result()
        node = TriggerScheduleConfig(
            node_id="t1", label="T", position={"x": 0, "y": 0}, cron_expression="not a cron",
        )
        svc._validate_trigger_schedule(node, result)
        assert "t1" in result.node_validation_errors

    def test_trigger_schedule_valid_cron(self) -> None:
        svc, result = _svc(), _result()
        node = TriggerScheduleConfig(
            node_id="t1", label="T", position={"x": 0, "y": 0}, cron_expression="0 12 * * *",
        )
        svc._validate_trigger_schedule(node, result)
        assert result.is_valid is True

    def test_validate_trigger_config_warns_no_output_ports(self) -> None:
        svc, result = _svc(), _result()
        node = TriggerCommandConfig(node_id="t1", label="T", position={"x": 0, "y": 0})
        svc._validate_trigger_config(node, result)
        assert result.warnings


class TestConditionValidators:
    def test_condition_if_no_rules(self) -> None:
        svc, result = _svc(), _result()
        node = ConditionIfConfig(node_id="c1", label="C", position={"x": 0, "y": 0})
        svc._validate_condition_if(node, result)
        assert "c1" in result.node_validation_errors

    def test_condition_if_empty_rule_variable(self) -> None:
        svc, result = _svc(), _result()
        node = ConditionIfConfig(
            node_id="c1", label="C", position={"x": 0, "y": 0},
            condition=[ConditionRule(variable="", operator=OperatorType.EQUALS, value=1)],
        )
        svc._validate_condition_if(node, result)
        assert "c1" in result.node_validation_errors

    def test_condition_switch_invalid(self) -> None:
        svc, result = _svc(), _result()
        node = ConditionSwitchConfig(node_id="s1", label="S", position={"x": 0, "y": 0}, variable="")
        svc._validate_condition_switch(node, result)
        assert len(result.node_validation_errors["s1"]) == 2

    def test_condition_filter_invalid(self) -> None:
        svc, result = _svc(), _result()
        # output_variable defaults to "filtered_items" (non-empty), so only
        # the missing input_array + missing condition errors apply here.
        node = ConditionFilterConfig(node_id="f1", label="F", position={"x": 0, "y": 0})
        svc._validate_condition_filter(node, result)
        assert len(result.node_validation_errors["f1"]) == 2

    def test_condition_filter_empty_output_variable(self) -> None:
        svc, result = _svc(), _result()
        node = ConditionFilterConfig(
            node_id="f1", label="F", position={"x": 0, "y": 0}, output_variable="",
        )
        svc._validate_condition_filter(node, result)
        assert len(result.node_validation_errors["f1"]) == 3


class TestActionValidators:
    def test_action_module_invalid(self) -> None:
        svc, result = _svc(), _result()
        node = ActionModuleConfig(
            node_id="a1", label="A", position={"x": 0, "y": 0}, module_name="", timeout_seconds=0,
        )
        svc._validate_action_module(node, result)
        assert len(result.node_validation_errors["a1"]) == 2

    def test_action_webhook_empty_url(self) -> None:
        svc, result = _svc(), _result()
        node = ActionWebhookConfig(
            node_id="w1", label="W", position={"x": 0, "y": 0}, url="",
            timeout_seconds=0, retry_count=-1,
        )
        svc._validate_action_webhook(node, result)
        assert len(result.node_validation_errors["w1"]) == 3

    def test_action_webhook_invalid_url_format(self) -> None:
        svc, result = _svc(), _result()
        node = ActionWebhookConfig(
            node_id="w1", label="W", position={"x": 0, "y": 0}, url="not-a-url",
        )
        svc._validate_action_webhook(node, result)
        assert "w1" in result.node_validation_errors

    def test_action_webhook_valid_url(self) -> None:
        svc, result = _svc(), _result()
        node = ActionWebhookConfig(
            node_id="w1", label="W", position={"x": 0, "y": 0}, url="https://example.com/hook",
        )
        svc._validate_action_webhook(node, result)
        assert result.is_valid is True

    def test_action_chat_message_requires_channel(self) -> None:
        svc, result = _svc(), _result()
        node = ActionChatMessageConfig(
            node_id="m1", label="M", position={"x": 0, "y": 0}, message_template="hi",
            destination="specified_channel",
        )
        svc._validate_action_chat_message(node, result)
        assert "m1" in result.node_validation_errors

    def test_action_chat_message_requires_user(self) -> None:
        svc, result = _svc(), _result()
        node = ActionChatMessageConfig(
            node_id="m1", label="M", position={"x": 0, "y": 0}, message_template="hi",
            destination="user_pm",
        )
        svc._validate_action_chat_message(node, result)
        assert "m1" in result.node_validation_errors

    def test_action_chat_message_empty_template(self) -> None:
        svc, result = _svc(), _result()
        node = ActionChatMessageConfig(node_id="m1", label="M", position={"x": 0, "y": 0}, message_template="")
        svc._validate_action_chat_message(node, result)
        assert "m1" in result.node_validation_errors

    def test_action_browser_source_invalid(self) -> None:
        svc, result = _svc(), _result()
        node = ActionBrowserSourceConfig(
            node_id="b1", label="B", position={"x": 0, "y": 0},
            source_type="bogus", action="bogus", duration=0,
        )
        svc._validate_action_browser_source(node, result)
        assert len(result.node_validation_errors["b1"]) == 3

    def test_action_delay_negative(self) -> None:
        svc, result = _svc(), _result()
        node = ActionDelayConfig(node_id="d1", label="D", position={"x": 0, "y": 0}, delay_ms=-1)
        svc._validate_action_delay(node, result)
        assert "d1" in result.node_validation_errors

    def test_action_delay_zero_warns(self) -> None:
        svc, result = _svc(), _result()
        node = ActionDelayConfig(node_id="d1", label="D", position={"x": 0, "y": 0}, delay_ms=0)
        svc._validate_action_delay(node, result)
        assert result.warnings


class TestDataValidators:
    def test_data_transform_invalid(self) -> None:
        svc, result = _svc(), _result()
        node = DataTransformConfig(
            node_id="d1", label="D", position={"x": 0, "y": 0}, expression_language="ruby",
        )
        svc._validate_data_transform(node, result)
        assert len(result.node_validation_errors["d1"]) == 2

    def test_data_variable_set_empty(self) -> None:
        svc, result = _svc(), _result()
        node = DataVariableSetConfig(node_id="s1", label="S", position={"x": 0, "y": 0})
        svc._validate_data_variable_set(node, result)
        assert "s1" in result.node_validation_errors

    def test_data_variable_get_invalid(self) -> None:
        svc, result = _svc(), _result()
        node = DataVariableGetConfig(node_id="g1", label="G", position={"x": 0, "y": 0})
        svc._validate_data_variable_get(node, result)
        assert len(result.node_validation_errors["g1"]) == 2


class TestLoopValidators:
    def test_loop_foreach_invalid(self) -> None:
        svc, result = _svc(), _result()
        node = LoopForeachConfig(
            node_id="l1", label="L", position={"x": 0, "y": 0}, array_variable="", max_iterations=0,
        )
        svc._validate_loop_foreach(node, result)
        assert len(result.node_validation_errors["l1"]) == 2

    def test_loop_foreach_exceeds_max(self) -> None:
        svc, result = _svc(), _result()
        node = LoopForeachConfig(
            node_id="l1", label="L", position={"x": 0, "y": 0}, array_variable="x",
            max_iterations=999999,
        )
        svc._validate_loop_foreach(node, result)
        assert "l1" in result.node_validation_errors

    def test_loop_while_invalid(self) -> None:
        svc, result = _svc(), _result()
        node = LoopWhileConfig(node_id="l1", label="L", position={"x": 0, "y": 0}, max_iterations=0)
        svc._validate_loop_while(node, result)
        assert len(result.node_validation_errors["l1"]) == 2

    def test_loop_break_always_valid(self) -> None:
        svc, result = _svc(), _result()
        node = LoopBreakConfig(node_id="b1", label="B", position={"x": 0, "y": 0})
        svc._validate_loop_break(node, result)
        assert result.is_valid is True


class TestFlowValidators:
    def test_flow_merge_invalid(self) -> None:
        svc, result = _svc(), _result()
        node = FlowMergeConfig(node_id="m1", label="M", position={"x": 0, "y": 0}, input_ports_required=0)
        svc._validate_flow_merge(node, result)
        assert "m1" in result.node_validation_errors

    def test_flow_merge_valid_all(self) -> None:
        svc, result = _svc(), _result()
        node = FlowMergeConfig(node_id="m1", label="M", position={"x": 0, "y": 0}, input_ports_required=-1)
        svc._validate_flow_merge(node, result)
        assert result.is_valid is True

    def test_flow_parallel_invalid(self) -> None:
        svc, result = _svc(), _result()
        node = FlowParallelConfig(
            node_id="p1", label="P", position={"x": 0, "y": 0},
            execution_type="bogus", timeout_seconds=0,
        )
        svc._validate_flow_parallel(node, result)
        assert len(result.node_validation_errors["p1"]) == 2

    def test_flow_end_empty_port(self) -> None:
        svc, result = _svc(), _result()
        node = FlowEndConfig(node_id="e1", label="E", position={"x": 0, "y": 0}, final_output_port="")
        svc._validate_flow_end(node, result)
        assert "e1" in result.node_validation_errors


class TestConnectionValidation:
    def test_source_node_not_found(self) -> None:
        svc, result = _svc(), _result()
        end = FlowEndConfig(node_id="e1", label="E", position={"x": 0, "y": 0}, input_ports=[_in()])
        wf = _wf({"e1": end}, [WorkflowConnection("c1", "missing", "out", "e1", "in")])
        svc._validate_single_connection(wf, wf.connections[0], result)
        assert not result.is_valid

    def test_target_node_not_found(self) -> None:
        svc, result = _svc(), _result()
        trigger = TriggerCommandConfig(node_id="t1", label="T", position={"x": 0, "y": 0}, output_ports=[_out()])
        wf = _wf({"t1": trigger}, [WorkflowConnection("c1", "t1", "out", "missing", "in")])
        svc._validate_single_connection(wf, wf.connections[0], result)
        assert not result.is_valid

    def test_output_port_not_found(self) -> None:
        svc, result = _svc(), _result()
        trigger = TriggerCommandConfig(node_id="t1", label="T", position={"x": 0, "y": 0}, output_ports=[_out("real")])
        end = FlowEndConfig(node_id="e1", label="E", position={"x": 0, "y": 0}, input_ports=[_in()])
        wf = _wf({"t1": trigger, "e1": end}, [WorkflowConnection("c1", "t1", "bogus", "e1", "in")])
        svc._validate_single_connection(wf, wf.connections[0], result)
        assert not result.is_valid

    def test_input_port_not_found(self) -> None:
        svc, result = _svc(), _result()
        trigger = TriggerCommandConfig(node_id="t1", label="T", position={"x": 0, "y": 0}, output_ports=[_out()])
        end = FlowEndConfig(node_id="e1", label="E", position={"x": 0, "y": 0}, input_ports=[_in("real")])
        wf = _wf({"t1": trigger, "e1": end}, [WorkflowConnection("c1", "t1", "out", "e1", "bogus")])
        svc._validate_single_connection(wf, wf.connections[0], result)
        assert not result.is_valid

    def test_type_mismatch_warns(self) -> None:
        svc, result = _svc(), _result()
        out_port = PortDefinition(name="out", port_type=PortType.OUTPUT, data_type=DataType.STRING)
        in_port = PortDefinition(name="in", port_type=PortType.INPUT, data_type=DataType.NUMBER)
        trigger = TriggerCommandConfig(node_id="t1", label="T", position={"x": 0, "y": 0}, output_ports=[out_port])
        end = FlowEndConfig(node_id="e1", label="E", position={"x": 0, "y": 0}, input_ports=[in_port])
        wf = _wf({"t1": trigger, "e1": end}, [WorkflowConnection("c1", "t1", "out", "e1", "in")])
        svc._validate_single_connection(wf, wf.connections[0], result)
        assert result.warnings

    def test_conditional_expression_empty_warns(self) -> None:
        svc, result = _svc(), _result()
        trigger = TriggerCommandConfig(node_id="t1", label="T", position={"x": 0, "y": 0}, output_ports=[_out()])
        end = FlowEndConfig(node_id="e1", label="E", position={"x": 0, "y": 0}, input_ports=[_in()])
        wf = _wf(
            {"t1": trigger, "e1": end},
            [WorkflowConnection("c1", "t1", "out", "e1", "in", conditional="  ")],
        )
        svc._validate_single_connection(wf, wf.connections[0], result)
        assert result.warnings


class TestSecurityValidation:
    def test_malicious_transform_flagged(self) -> None:
        svc, result = _svc(), _result()
        node = DataTransformConfig(
            node_id="d1", label="D", position={"x": 0, "y": 0},
            transformations={"x": "__import__('os').system('ls')"},
        )
        wf = _wf({"d1": node})
        svc._validate_security(wf, result)
        assert "d1" in result.node_validation_errors

    def test_malicious_condition_value_flagged(self) -> None:
        svc, result = _svc(), _result()
        node = ConditionIfConfig(
            node_id="c1", label="C", position={"x": 0, "y": 0},
            condition=[ConditionRule(variable="x", operator=OperatorType.EQUALS, value="eval(1)")],
        )
        wf = _wf({"c1": node})
        svc._validate_security(wf, result)
        assert "c1" in result.node_validation_errors

    def test_malicious_webhook_body_flagged(self) -> None:
        svc, result = _svc(), _result()
        node = ActionWebhookConfig(
            node_id="w1", label="W", position={"x": 0, "y": 0}, url="https://x",
            body_template="exec('bad')",
        )
        wf = _wf({"w1": node})
        svc._validate_security(wf, result)
        assert "w1" in result.node_validation_errors

    def test_clean_code_not_flagged(self) -> None:
        svc, result = _svc(), _result()
        node = DataTransformConfig(
            node_id="d1", label="D", position={"x": 0, "y": 0}, transformations={"x": "1 + 1"},
        )
        wf = _wf({"d1": node})
        svc._validate_security(wf, result)
        assert result.is_valid is True

    def test_has_malicious_patterns_non_string_returns_false(self) -> None:
        svc = _svc()
        assert svc._has_malicious_patterns(123) is False


class TestCycleDetection:
    def test_non_loop_cycle_is_error(self) -> None:
        svc, result = _svc(), _result()
        a = ActionDelayConfig(node_id="a", label="A", position={"x": 0, "y": 0}, output_ports=[_out()], input_ports=[_in()])
        b = ActionDelayConfig(node_id="b", label="B", position={"x": 1, "y": 0}, output_ports=[_out()], input_ports=[_in()])
        wf = _wf(
            {"a": a, "b": b},
            [
                WorkflowConnection("c1", "a", "out", "b", "in"),
                WorkflowConnection("c2", "b", "out", "a", "in"),
            ],
        )
        svc._validate_graph_structure(wf, result)
        assert not result.is_valid

    def test_loop_cycle_is_allowed(self) -> None:
        svc, result = _svc(), _result()
        loop = LoopForeachConfig(node_id="l", label="L", position={"x": 0, "y": 0}, output_ports=[_out()], input_ports=[_in()])
        body = ActionDelayConfig(node_id="b", label="B", position={"x": 1, "y": 0}, output_ports=[_out()], input_ports=[_in()])
        wf = _wf(
            {"l": loop, "b": body},
            [
                WorkflowConnection("c1", "l", "out", "b", "in"),
                WorkflowConnection("c2", "b", "out", "l", "in"),
            ],
        )
        cycles = svc._detect_cycles(wf)
        assert cycles == []

    def test_trigger_cannot_reach_end_warns(self) -> None:
        svc, result = _svc(), _result()
        trigger = TriggerCommandConfig(node_id="t", label="T", position={"x": 0, "y": 0}, output_ports=[_out()])
        end = FlowEndConfig(node_id="e", label="E", position={"x": 1, "y": 0}, input_ports=[_in()])
        wf = _wf({"t": trigger, "e": end})  # no connection between them
        svc._validate_graph_structure(wf, result)
        assert any("cannot reach any end nodes" in w for w in result.warnings)


class TestComplexityLimits:
    def test_exceeds_max_nodes(self) -> None:
        svc, result = _svc(), _result()
        nodes = {
            f"n{i}": ActionDelayConfig(node_id=f"n{i}", label=f"N{i}", position={"x": i, "y": 0})
            for i in range(WorkflowValidationService.MAX_NODES + 1)
        }
        wf = _wf(nodes)
        svc._validate_complexity_limits(wf, result)
        assert not result.is_valid

    def test_deep_chain_warns(self) -> None:
        svc, result = _svc(), _result()
        depth = WorkflowValidationService.MAX_DEPTH + 2
        nodes = {}
        connections = []
        for i in range(depth):
            nodes[f"n{i}"] = ActionDelayConfig(
                node_id=f"n{i}", label=f"N{i}", position={"x": i, "y": 0},
                output_ports=[_out()], input_ports=[_in()],
            )
        for i in range(depth - 1):
            connections.append(WorkflowConnection(f"c{i}", f"n{i}", "out", f"n{i+1}", "in"))
        nodes["t"] = TriggerCommandConfig(node_id="t", label="T", position={"x": -1, "y": 0}, output_ports=[_out()])
        connections.insert(0, WorkflowConnection("c_t", "t", "out", "n0", "in"))
        wf = _wf(nodes, connections)
        svc._validate_complexity_limits(wf, result)
        assert result.warnings


class TestStandaloneValidators:
    def test_is_valid_url(self) -> None:
        assert WorkflowValidationService._is_valid_url("https://example.com") is True
        assert WorkflowValidationService._is_valid_url("not a url") is False

    def test_is_valid_cron_wrong_field_count(self) -> None:
        assert WorkflowValidationService._is_valid_cron("* * *") is False

    def test_is_valid_cron_bad_characters(self) -> None:
        assert WorkflowValidationService._is_valid_cron("a b c d e") is False

    def test_is_valid_cron_six_fields(self) -> None:
        assert WorkflowValidationService._is_valid_cron("*/5 * * * * *") is True

    def test_are_types_compatible_any(self) -> None:
        svc = _svc()
        assert svc._are_types_compatible(DataType.ANY, DataType.STRING) is True

    def test_are_types_compatible_object_array(self) -> None:
        svc = _svc()
        assert svc._are_types_compatible(DataType.OBJECT, DataType.ARRAY) is True

    def test_are_types_incompatible(self) -> None:
        svc = _svc()
        assert svc._are_types_compatible(DataType.STRING, DataType.NUMBER) is False


class TestValidateWorkflowTopLevel:
    def test_unexpected_exception_is_caught(self) -> None:
        svc, result = _svc(), _result()
        node = TriggerCommandConfig(node_id="t", label="T", position={"x": 0, "y": 0})
        wf = _wf({"t": node})
        # Force an internal exception during validation to hit the outer except.
        svc._validate_complexity_limits = lambda *a, **k: (_ for _ in ()).throw(ValueError("boom"))
        outcome = svc.validate_workflow(wf)
        assert outcome.is_valid is False
        assert any("Validation error" in e for e in outcome.errors)
