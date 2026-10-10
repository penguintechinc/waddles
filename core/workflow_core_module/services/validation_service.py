"""
Workflow Validation Service
=============================

Comprehensive validation for workflow definitions including:
- Graph structure validation (DAG detection, orphaned nodes, reachability)
- Node configuration validation (per-node type validation)
- Connection validation (port matching, type compatibility)
- Trigger configuration validation
- Complexity limits (max nodes, depth)
- Security validation (malicious code patterns)

Follows Waddles patterns with comprehensive logging and error handling.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple, Any
from collections import defaultdict, deque

from models.workflow import WorkflowDefinition, WorkflowConnection
from models.nodes import (
    WorkflowNode, NodeType, DataType, PortType,
    TriggerCommandConfig, TriggerEventConfig, TriggerWebhookConfig,
    TriggerScheduleConfig, ConditionIfConfig, ConditionSwitchConfig,
    ConditionFilterConfig, ActionModuleConfig, ActionWebhookConfig,
    ActionChatMessageConfig, ActionBrowserSourceConfig, ActionDelayConfig,
    DataTransformConfig, DataVariableSetConfig, DataVariableGetConfig,
    LoopForeachConfig, LoopWhileConfig, LoopBreakConfig,
    FlowMergeConfig, FlowParallelConfig, FlowEndConfig
)
from flask_core import describe_db_error


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ValidationResult:
    """Result of workflow validation"""
    is_valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    node_validation_errors: Dict[str, List[str]] = field(default_factory=dict)

    def add_error(self, message: str) -> None:
        """Add error message"""
        self.errors.append(message)
        self.is_valid = False

    def add_warning(self, message: str) -> None:
        """Add warning message"""
        self.warnings.append(message)

    def add_node_error(self, node_id: str, message: str) -> None:
        """Add error for a specific node"""
        if node_id not in self.node_validation_errors:
            self.node_validation_errors[node_id] = []
        self.node_validation_errors[node_id].append(message)
        self.is_valid = False

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization"""
        return {
            "is_valid": self.is_valid,
            "errors": self.errors,
            "warnings": self.warnings,
            "node_validation_errors": self.node_validation_errors,
            "error_count": len(self.errors) + sum(len(e) for e in self.node_validation_errors.values()),
            "warning_count": len(self.warnings),
        }


class WorkflowValidationService:
    """Service for comprehensive workflow validation"""

    # Configuration limits
    MAX_NODES = 100
    MAX_DEPTH = 10
    MAX_LOOP_ITERATIONS = 10000

    # Malicious code patterns
    MALICIOUS_PATTERNS = [
        r"__import__",
        r"eval\(",
        r"exec\(",
        r"compile\(",
        r"os\.system",
        r"subprocess\.",
        r"open\(",
        r"input\(",
        r"globals\(\)",
        r"locals\(\)",
        r"vars\(",
        r"__code__",
        r"__dict__",
        r"__builtins__",
    ]

    def __init__(self):
        """Initialize validation service"""
        logger.info("WorkflowValidationService initialized")

    def validate_workflow(
        self,
        workflow_def: WorkflowDefinition
    ) -> ValidationResult:
        """
        Main workflow validation entry point.

        Performs comprehensive validation of entire workflow definition
        including structure, nodes, connections, and security.

        Args:
            workflow_def: Workflow definition to validate

        Returns:
            ValidationResult with is_valid, errors, and warnings
        """
        result = ValidationResult(is_valid=True)

        try:
            logger.info(
                f"Starting workflow validation: {workflow_def.metadata.workflow_id}"
            )

            # Step 1: Basic checks
            if not workflow_def.nodes:
                result.add_error("Workflow must contain at least one node")
                return result

            # Step 2: Check complexity limits
            self._validate_complexity_limits(workflow_def, result)
            if not result.is_valid:
                return result

            # Step 3: Validate graph structure
            self._validate_graph_structure(workflow_def, result)

            # Step 4: Validate each node's configuration
            self._validate_all_node_configs(workflow_def, result)

            # Step 5: Validate connections
            self._validate_connections(workflow_def, result)

            # Step 6: Validate trigger configuration
            trigger_nodes = workflow_def.get_trigger_nodes()
            if not trigger_nodes:
                # A workflow with no trigger node can never be invoked, so
                # this must reject the workflow (is_valid=False) -- but the
                # informational "Workflow has no trigger nodes" message
                # stays a warning (tests/test_validation_service.py asserts
                # it's present in result.warnings specifically). Previously
                # this was add_warning() only, leaving is_valid=True for an
                # unexecutable workflow; undetected because this file had
                # no test coverage.
                result.add_warning("Workflow has no trigger nodes")
                result.add_error(
                    "Workflow is not executable: at least one trigger node is required"
                )
            else:
                for trigger_id in trigger_nodes:
                    trigger_node = workflow_def.get_node(trigger_id)
                    if trigger_node:
                        self._validate_trigger_config(trigger_node, result)

            # Step 7: Security validation
            self._validate_security(workflow_def, result)

            logger.info(
                f"Workflow validation completed: "
                f"is_valid={result.is_valid}, "
                f"errors={len(result.errors)}, "
                f"warnings={len(result.warnings)}"
            )

        except Exception as e:
            logger.error(f"Unexpected error during workflow validation: {describe_db_error(e)}")
            result.add_error(f"Validation error: {describe_db_error(e)}")

        return result

    def _validate_complexity_limits(
        self,
        workflow_def: WorkflowDefinition,
        result: ValidationResult
    ) -> None:
        """Validate workflow complexity limits"""
        node_count = len(workflow_def.nodes)

        # Check max nodes
        if node_count > self.MAX_NODES:
            result.add_error(
                f"Workflow exceeds maximum node count: "
                f"{node_count} > {self.MAX_NODES}"
            )

        # Check max depth
        max_depth = self._calculate_max_depth(workflow_def)
        if max_depth > self.MAX_DEPTH:
            result.add_warning(
                f"Workflow depth ({max_depth}) exceeds recommended maximum ({self.MAX_DEPTH}). "
                f"This may impact performance."
            )

    def _validate_graph_structure(
        self,
        workflow_def: WorkflowDefinition,
        result: ValidationResult
    ) -> None:
        """
        Validate graph structure: DAG check (with loop exceptions),
        reachability, and orphaned nodes.
        """
        # Check for cycles (allowing loop nodes)
        cycles = self._detect_cycles(workflow_def)
        if cycles:
            for cycle_path in cycles:
                result.add_error(
                    f"Cycle detected in workflow: {' -> '.join(cycle_path)}"
                )

        # Check for unreachable nodes
        unreachable = self._find_unreachable_nodes(workflow_def)
        if unreachable:
            result.add_warning(
                f"Unreachable nodes found (orphaned): {', '.join(unreachable)}"
            )

        # Check connectivity from triggers to end nodes
        trigger_nodes = workflow_def.get_trigger_nodes()
        end_nodes = set(workflow_def.get_end_nodes())

        if trigger_nodes and end_nodes:
            for trigger_id in trigger_nodes:
                reachable_from_trigger = self._find_reachable_nodes(
                    workflow_def, trigger_id
                )
                reachable_ends = reachable_from_trigger & end_nodes
                if not reachable_ends:
                    result.add_warning(
                        f"Trigger node {trigger_id} cannot reach any end nodes"
                    )

    def _validate_all_node_configs(
        self,
        workflow_def: WorkflowDefinition,
        result: ValidationResult
    ) -> None:
        """Validate configuration of all nodes"""
        for node_id, node in workflow_def.nodes.items():
            self._validate_node_config(node, result)

    def _validate_node_config(
        self,
        node: WorkflowNode,
        result: ValidationResult
    ) -> None:
        """
        Validate individual node configuration based on node type.

        Args:
            node: Node to validate
            result: ValidationResult to add errors to
        """
        node_id = node.node_id

        # Common validation
        if not node.label or not node.label.strip():
            result.add_node_error(node_id, "Node label cannot be empty")

        if "x" not in node.position or "y" not in node.position:
            result.add_node_error(
                node_id, "Node position must have x and y coordinates"
            )

        # Type-specific validation
        if isinstance(node, TriggerCommandConfig):
            self._validate_trigger_command(node, result)
        elif isinstance(node, TriggerEventConfig):
            self._validate_trigger_event(node, result)
        elif isinstance(node, TriggerWebhookConfig):
            self._validate_trigger_webhook(node, result)
        elif isinstance(node, TriggerScheduleConfig):
            self._validate_trigger_schedule(node, result)
        elif isinstance(node, ConditionIfConfig):
            self._validate_condition_if(node, result)
        elif isinstance(node, ConditionSwitchConfig):
            self._validate_condition_switch(node, result)
        elif isinstance(node, ConditionFilterConfig):
            self._validate_condition_filter(node, result)
        elif isinstance(node, ActionModuleConfig):
            self._validate_action_module(node, result)
        elif isinstance(node, ActionWebhookConfig):
            self._validate_action_webhook(node, result)
        elif isinstance(node, ActionChatMessageConfig):
            self._validate_action_chat_message(node, result)
        elif isinstance(node, ActionBrowserSourceConfig):
            self._validate_action_browser_source(node, result)
        elif isinstance(node, ActionDelayConfig):
            self._validate_action_delay(node, result)
        elif isinstance(node, DataTransformConfig):
            self._validate_data_transform(node, result)
        elif isinstance(node, DataVariableSetConfig):
            self._validate_data_variable_set(node, result)
        elif isinstance(node, DataVariableGetConfig):
            self._validate_data_variable_get(node, result)
        elif isinstance(node, LoopForeachConfig):
            self._validate_loop_foreach(node, result)
        elif isinstance(node, LoopWhileConfig):
            self._validate_loop_while(node, result)
        elif isinstance(node, LoopBreakConfig):
            self._validate_loop_break(node, result)
        elif isinstance(node, FlowMergeConfig):
            self._validate_flow_merge(node, result)
        elif isinstance(node, FlowParallelConfig):
            self._validate_flow_parallel(node, result)
        elif isinstance(node, FlowEndConfig):
            self._validate_flow_end(node, result)

    def _validate_trigger_command(
        self,
        node: TriggerCommandConfig,
        result: ValidationResult
    ) -> None:
        """Validate trigger command configuration"""
        if not node.command_pattern or not node.command_pattern.strip():
            result.add_node_error(node.node_id, "Command pattern cannot be empty")
        if not node.platforms:
            result.add_node_error(
                node.node_id, "At least one platform must be specified"
            )
        if node.cooldown_seconds < 0:
            result.add_node_error(
                node.node_id, "Cooldown cannot be negative"
            )
        if node.user_cooldown_seconds < 0:
            result.add_node_error(
                node.node_id, "User cooldown cannot be negative"
            )

    def _validate_trigger_event(
        self,
        node: TriggerEventConfig,
        result: ValidationResult
    ) -> None:
        """Validate trigger event configuration"""
        if not node.event_type or not node.event_type.strip():
            result.add_node_error(node.node_id, "Event type cannot be empty")
        if not node.platforms:
            result.add_node_error(
                node.node_id, "At least one platform must be specified"
            )

    def _validate_trigger_webhook(
        self,
        node: TriggerWebhookConfig,
        result: ValidationResult
    ) -> None:
        """Validate webhook trigger configuration"""
        if not node.webhook_path or not node.webhook_path.strip():
            result.add_node_error(node.node_id, "Webhook path cannot be empty")
        if not node.webhook_path.startswith("/"):
            result.add_node_error(
                node.node_id, "Webhook path must start with /"
            )

    def _validate_trigger_schedule(
        self,
        node: TriggerScheduleConfig,
        result: ValidationResult
    ) -> None:
        """Validate schedule trigger configuration"""
        if not node.cron_expression or not node.cron_expression.strip():
            result.add_node_error(
                node.node_id, "Cron expression cannot be empty"
            )
        else:
            if not self._is_valid_cron(node.cron_expression):
                result.add_node_error(
                    node.node_id,
                    f"Invalid cron expression: {node.cron_expression}"
                )

    def _validate_condition_if(
        self,
        node: ConditionIfConfig,
        result: ValidationResult
    ) -> None:
        """Validate condition if configuration"""
        if not node.condition:
            result.add_node_error(
                node.node_id, "At least one condition rule must be specified"
            )
        else:
            for rule in node.condition:
                if not rule.variable or not rule.variable.strip():
                    result.add_node_error(
                        node.node_id, "Condition variable cannot be empty"
                    )

    def _validate_condition_switch(
        self,
        node: ConditionSwitchConfig,
        result: ValidationResult
    ) -> None:
        """Validate condition switch configuration"""
        if not node.variable or not node.variable.strip():
            result.add_node_error(
                node.node_id, "Switch variable cannot be empty"
            )
        if not node.cases:
            result.add_node_error(
                node.node_id, "At least one case must be specified"
            )

    def _validate_condition_filter(
        self,
        node: ConditionFilterConfig,
        result: ValidationResult
    ) -> None:
        """Validate condition filter configuration"""
        if not node.input_array or not node.input_array.strip():
            result.add_node_error(
                node.node_id, "Input array variable cannot be empty"
            )
        if not node.condition:
            result.add_node_error(
                node.node_id, "At least one filter condition must be specified"
            )
        if not node.output_variable or not node.output_variable.strip():
            result.add_node_error(
                node.node_id, "Output variable cannot be empty"
            )

    def _validate_action_module(
        self,
        node: ActionModuleConfig,
        result: ValidationResult
    ) -> None:
        """Validate action module configuration"""
        if not node.module_name or not node.module_name.strip():
            result.add_node_error(
                node.node_id, "Module name cannot be empty"
            )
        if node.timeout_seconds <= 0:
            result.add_node_error(
                node.node_id, "Timeout must be greater than 0"
            )

    def _validate_action_webhook(
        self,
        node: ActionWebhookConfig,
        result: ValidationResult
    ) -> None:
        """Validate webhook action configuration"""
        if not node.url or not node.url.strip():
            result.add_node_error(node.node_id, "Webhook URL cannot be empty")
        else:
            if not self._is_valid_url(node.url):
                result.add_node_error(
                    node.node_id, f"Invalid URL: {node.url}"
                )
        if node.timeout_seconds <= 0:
            result.add_node_error(
                node.node_id, "Timeout must be greater than 0"
            )
        if node.retry_count < 0:
            result.add_node_error(
                node.node_id, "Retry count cannot be negative"
            )

    def _validate_action_chat_message(
        self,
        node: ActionChatMessageConfig,
        result: ValidationResult
    ) -> None:
        """Validate chat message action configuration"""
        if not node.message_template or not node.message_template.strip():
            result.add_node_error(
                node.node_id, "Message template cannot be empty"
            )
        if node.destination == "specified_channel" and not node.channel_id:
            result.add_node_error(
                node.node_id,
                "Channel ID required when destination is specified_channel"
            )
        if node.destination == "user_pm" and not node.user_id:
            result.add_node_error(
                node.node_id,
                "User ID required when destination is user_pm"
            )

    def _validate_action_browser_source(
        self,
        node: ActionBrowserSourceConfig,
        result: ValidationResult
    ) -> None:
        """Validate browser source action configuration"""
        valid_source_types = ["ticker", "media", "general"]
        if node.source_type not in valid_source_types:
            result.add_node_error(
                node.node_id,
                f"Invalid source type: {node.source_type}. "
                f"Must be one of: {', '.join(valid_source_types)}"
            )
        valid_actions = ["display", "update", "clear"]
        if node.action not in valid_actions:
            result.add_node_error(
                node.node_id,
                f"Invalid action: {node.action}. "
                f"Must be one of: {', '.join(valid_actions)}"
            )
        if node.duration <= 0:
            result.add_node_error(
                node.node_id, "Duration must be greater than 0"
            )

    def _validate_action_delay(
        self,
        node: ActionDelayConfig,
        result: ValidationResult
    ) -> None:
        """Validate delay action configuration"""
        if node.delay_ms < 0:
            result.add_node_error(
                node.node_id, "Delay cannot be negative"
            )
        if node.delay_ms == 0 and not node.delay_variable:
            result.add_warning(
                f"Delay node {node.node_id} has no delay (0ms) and no delay variable"
            )

    def _validate_data_transform(
        self,
        node: DataTransformConfig,
        result: ValidationResult
    ) -> None:
        """Validate data transform configuration"""
        if not node.transformations:
            result.add_node_error(
                node.node_id, "At least one transformation must be specified"
            )
        valid_languages = ["jq", "python", "js"]
        if node.expression_language not in valid_languages:
            result.add_node_error(
                node.node_id,
                f"Invalid expression language: {node.expression_language}"
            )

    def _validate_data_variable_set(
        self,
        node: DataVariableSetConfig,
        result: ValidationResult
    ) -> None:
        """Validate variable set configuration"""
        if not node.variables:
            result.add_node_error(
                node.node_id, "At least one variable must be specified"
            )

    def _validate_data_variable_get(
        self,
        node: DataVariableGetConfig,
        result: ValidationResult
    ) -> None:
        """Validate variable get configuration"""
        if not node.variable_name or not node.variable_name.strip():
            result.add_node_error(
                node.node_id, "Variable name cannot be empty"
            )
        if not node.output_variable or not node.output_variable.strip():
            result.add_node_error(
                node.node_id, "Output variable cannot be empty"
            )

    def _validate_loop_foreach(
        self,
        node: LoopForeachConfig,
        result: ValidationResult
    ) -> None:
        """Validate foreach loop configuration"""
        if not node.array_variable or not node.array_variable.strip():
            result.add_node_error(
                node.node_id, "Array variable cannot be empty"
            )
        if node.max_iterations <= 0 or node.max_iterations > self.MAX_LOOP_ITERATIONS:
            result.add_node_error(
                node.node_id,
                f"Max iterations must be between 1 and {self.MAX_LOOP_ITERATIONS}"
            )

    def _validate_loop_while(
        self,
        node: LoopWhileConfig,
        result: ValidationResult
    ) -> None:
        """Validate while loop configuration"""
        if not node.condition:
            result.add_node_error(
                node.node_id, "While loop must have a condition"
            )
        if node.max_iterations <= 0 or node.max_iterations > self.MAX_LOOP_ITERATIONS:
            result.add_node_error(
                node.node_id,
                f"Max iterations must be between 1 and {self.MAX_LOOP_ITERATIONS}"
            )

    def _validate_loop_break(
        self,
        node: LoopBreakConfig,
        result: ValidationResult
    ) -> None:
        """Validate break statement configuration"""
        # Break is valid with or without condition
        pass

    def _validate_flow_merge(
        self,
        node: FlowMergeConfig,
        result: ValidationResult
    ) -> None:
        """Validate merge node configuration"""
        if node.input_ports_required < -1 or node.input_ports_required == 0:
            result.add_node_error(
                node.node_id,
                "Input ports required must be -1 (all) or >= 1"
            )

    def _validate_flow_parallel(
        self,
        node: FlowParallelConfig,
        result: ValidationResult
    ) -> None:
        """Validate parallel node configuration"""
        valid_types = ["parallel", "any_first", "all_fastest"]
        if node.execution_type not in valid_types:
            result.add_node_error(
                node.node_id,
                f"Invalid execution type: {node.execution_type}"
            )
        if node.timeout_seconds <= 0:
            result.add_node_error(
                node.node_id, "Timeout must be greater than 0"
            )

    def _validate_flow_end(
        self,
        node: FlowEndConfig,
        result: ValidationResult
    ) -> None:
        """Validate end node configuration"""
        if not node.final_output_port or not node.final_output_port.strip():
            result.add_node_error(
                node.node_id, "Final output port cannot be empty"
            )

    def _validate_connections(
        self,
        workflow_def: WorkflowDefinition,
        result: ValidationResult
    ) -> None:
        """
        Validate all connections in workflow.

        Checks for:
        - Valid source and target nodes
        - Valid port names
        - Type compatibility
        - Conditional output validity
        """
        for conn in workflow_def.connections:
            self._validate_single_connection(workflow_def, conn, result)

    def _validate_single_connection(
        self,
        workflow_def: WorkflowDefinition,
        conn: WorkflowConnection,
        result: ValidationResult
    ) -> None:
        """Validate a single connection"""
        # Check nodes exist
        from_node = workflow_def.get_node(conn.from_node_id)
        to_node = workflow_def.get_node(conn.to_node_id)

        if not from_node:
            result.add_error(
                f"Connection {conn.connection_id}: "
                f"Source node not found: {conn.from_node_id}"
            )
            return

        if not to_node:
            result.add_error(
                f"Connection {conn.connection_id}: "
                f"Target node not found: {conn.to_node_id}"
            )
            return

        # Check ports exist
        from_port_names = {p.name for p in from_node.output_ports}
        to_port_names = {p.name for p in to_node.input_ports}

        if conn.from_port_name not in from_port_names:
            result.add_error(
                f"Connection {conn.connection_id}: "
                f"Output port not found: {from_node.node_id}.{conn.from_port_name}"
            )

        if conn.to_port_name not in to_port_names:
            result.add_error(
                f"Connection {conn.connection_id}: "
                f"Input port not found: {to_node.node_id}.{conn.to_port_name}"
            )
            return

        # Check type compatibility
        from_port = next(
            (p for p in from_node.output_ports if p.name == conn.from_port_name),
            None
        )
        to_port = next(
            (p for p in to_node.input_ports if p.name == conn.to_port_name),
            None
        )

        if from_port and to_port:
            if not self._are_types_compatible(from_port.data_type, to_port.data_type):
                result.add_warning(
                    f"Connection {conn.connection_id}: "
                    f"Type mismatch: {from_port.data_type.value} -> {to_port.data_type.value}"
                )

        # Validate conditional output if present
        if conn.conditional:
            self._validate_conditional_expression(
                conn.conditional,
                conn.connection_id,
                result
            )

    def _validate_trigger_config(
        self,
        node: WorkflowNode,
        result: ValidationResult
    ) -> None:
        """Validate trigger node configuration"""
        # Trigger nodes should have output ports
        if not node.output_ports:
            result.add_warning(
                f"Trigger node {node.node_id} has no output ports"
            )

    def _validate_security(
        self,
        workflow_def: WorkflowDefinition,
        result: ValidationResult
    ) -> None:
        """
        Validate security: check for malicious code patterns.

        Scans data transformation and custom code for dangerous patterns.
        """
        for node_id, node in workflow_def.nodes.items():
            # Check data transform expressions
            if isinstance(node, DataTransformConfig):
                for var_name, expression in node.transformations.items():
                    if self._has_malicious_patterns(expression):
                        result.add_node_error(
                            node_id,
                            f"Potential security issue in transformation '{var_name}': "
                            f"contains dangerous patterns"
                        )

            # Check condition expressions
            if isinstance(node, (ConditionIfConfig, ConditionFilterConfig)):
                if hasattr(node, "condition") and node.condition:
                    for rule in node.condition:
                        if isinstance(rule.value, str):
                            if self._has_malicious_patterns(rule.value):
                                result.add_node_error(
                                    node_id,
                                    f"Potential security issue in condition: "
                                    f"contains dangerous patterns"
                                )

            # Check webhook body templates
            if isinstance(node, ActionWebhookConfig) and node.body_template:
                if self._has_malicious_patterns(node.body_template):
                    result.add_node_error(
                        node_id,
                        f"Potential security issue in webhook body: "
                        f"contains dangerous patterns"
                    )

    def _detect_cycles(
        self,
        workflow_def: WorkflowDefinition
    ) -> List[List[str]]:
        """
        Detect cycles in workflow graph.

        Allows cycles only within loop nodes (foreach, while).
        Returns list of cycle paths if found.
        """
        cycles = []
        visited = set()
        rec_stack = set()
        path = []

        def dfs(node_id: str) -> bool:
            visited.add(node_id)
            rec_stack.add(node_id)
            path.append(node_id)

            for next_id in workflow_def.get_next_nodes(node_id):
                if next_id not in visited:
                    if dfs(next_id):
                        return True
                elif next_id in rec_stack:
                    # Cycle detected
                    cycle_start = path.index(next_id)
                    cycle_path = path[cycle_start:] + [next_id]

                    # Check if cycle is within loop construct
                    if not self._is_loop_cycle(workflow_def, cycle_path):
                        cycles.append(cycle_path)

            path.pop()
            rec_stack.remove(node_id)
            return False

        # Start DFS from all unvisited nodes
        for node_id in workflow_def.nodes:
            if node_id not in visited:
                dfs(node_id)

        return cycles

    def _is_loop_cycle(
        self,
        workflow_def: WorkflowDefinition,
        cycle_path: List[str]
    ) -> bool:
        """Check if cycle is within a loop construct"""
        # A valid loop cycle should contain a loop node
        loop_types = (LoopForeachConfig, LoopWhileConfig)
        for node_id in cycle_path:
            node = workflow_def.get_node(node_id)
            if node and isinstance(node, loop_types):
                return True
        return False

    def _find_unreachable_nodes(
        self,
        workflow_def: WorkflowDefinition
    ) -> List[str]:
        """Find nodes that are unreachable from any trigger"""
        trigger_nodes = workflow_def.get_trigger_nodes()
        if not trigger_nodes:
            return list(workflow_def.nodes.keys())

        reachable = set()
        for trigger_id in trigger_nodes:
            reachable.update(self._find_reachable_nodes(workflow_def, trigger_id))

        unreachable = set(workflow_def.nodes.keys()) - reachable
        return list(unreachable)

    def _find_reachable_nodes(
        self,
        workflow_def: WorkflowDefinition,
        start_node_id: str
    ) -> Set[str]:
        """BFS to find all reachable nodes from a starting node"""
        reachable = set()
        queue = deque([start_node_id])
        visited = set()

        while queue:
            node_id = queue.popleft()
            if node_id in visited:
                continue

            visited.add(node_id)
            reachable.add(node_id)

            for next_id in workflow_def.get_next_nodes(node_id):
                if next_id not in visited:
                    queue.append(next_id)

        return reachable

    def _calculate_max_depth(
        self,
        workflow_def: WorkflowDefinition
    ) -> int:
        """Calculate maximum depth of workflow graph"""
        trigger_nodes = workflow_def.get_trigger_nodes()
        if not trigger_nodes:
            return 0

        max_depth = 0
        for trigger_id in trigger_nodes:
            depth = self._calculate_node_depth(workflow_def, trigger_id, set())
            max_depth = max(max_depth, depth)

        return max_depth

    def _calculate_node_depth(
        self,
        workflow_def: WorkflowDefinition,
        node_id: str,
        visited: Set[str],
        depth: int = 0
    ) -> int:
        """Recursively calculate depth to deepest node"""
        if node_id in visited:
            return depth  # Avoid infinite recursion

        visited.add(node_id)
        next_nodes = workflow_def.get_next_nodes(node_id)

        if not next_nodes:
            return depth

        max_child_depth = 0
        for next_id in next_nodes:
            child_depth = self._calculate_node_depth(
                workflow_def, next_id, visited.copy(), depth + 1
            )
            max_child_depth = max(max_child_depth, child_depth)

        return max_child_depth

    def _are_types_compatible(
        self,
        from_type: DataType,
        to_type: DataType
    ) -> bool:
        """Check if two data types are compatible"""
        # Any type is compatible with itself
        if from_type == to_type:
            return True

        # ANY type is compatible with everything
        if from_type == DataType.ANY or to_type == DataType.ANY:
            return True

        # OBJECT and ARRAY are loosely compatible
        if from_type in (DataType.OBJECT, DataType.ARRAY) and \
           to_type in (DataType.OBJECT, DataType.ARRAY):
            return True

        return False

    def _validate_conditional_expression(
        self,
        expression: str,
        connection_id: str,
        result: ValidationResult
    ) -> None:
        """Validate conditional expression syntax"""
        if not expression or not expression.strip():
            result.add_warning(
                f"Connection {connection_id}: Empty conditional expression"
            )

    def _has_malicious_patterns(self, code: str) -> bool:
        """Check if code contains malicious patterns"""
        if not isinstance(code, str):
            return False

        for pattern in self.MALICIOUS_PATTERNS:
            if re.search(pattern, code, re.IGNORECASE):
                return True

        return False

    @staticmethod
    def _is_valid_url(url: str) -> bool:
        """Validate URL format"""
        url_pattern = r"^https?://[^\s/$.?#].[^\s]*$"
        return bool(re.match(url_pattern, url, re.IGNORECASE))

    @staticmethod
    def _is_valid_cron(cron: str) -> bool:
        """
        Basic cron validation.
        Checks for 5 or 6 fields (standard or with seconds).
        """
        parts = cron.strip().split()
        if len(parts) not in (5, 6):
            return False

        # Basic validation: each part should contain digits, comma, dash, slash, or asterisk
        for part in parts:
            if not re.match(r"^[0-9*,/-]+$", part):
                return False

        return True
