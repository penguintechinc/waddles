"""
Node Executor Service
====================

Executes individual workflow nodes based on their type.
Handles all node types: triggers, conditions, actions, data, loops, and flow.
"""

import asyncio
import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field

import aiohttp

# Import RestrictedPython for safe code execution
try:
    from RestrictedPython import compile_restricted, safe_globals
    from RestrictedPython.Eval import default_guarded_getitem, default_guarded_getiter
    from RestrictedPython.Guards import guarded_iter_unpack_sequence
    RESTRICTED_PYTHON_AVAILABLE = True
except ImportError:
    RESTRICTED_PYTHON_AVAILABLE = False
    compile_restricted = None
    safe_globals = None
    default_guarded_getitem = None
    default_guarded_getiter = None
    guarded_iter_unpack_sequence = None

from models.nodes import (
    NodeType,
    WorkflowNode,
    OperatorType,
    ConditionRule,
    # Trigger nodes
    TriggerCommandConfig,
    TriggerEventConfig,
    TriggerWebhookConfig,
    TriggerScheduleConfig,
    # Condition nodes
    ConditionIfConfig,
    ConditionSwitchConfig,
    ConditionFilterConfig,
    # Action nodes
    ActionModuleConfig,
    ActionWebhookConfig,
    ActionChatMessageConfig,
    ActionBrowserSourceConfig,
    ActionDelayConfig,
    # Data nodes
    DataTransformConfig,
    DataVariableSetConfig,
    DataVariableGetConfig,
    # Loop nodes
    LoopForeachConfig,
    LoopWhileConfig,
    LoopBreakConfig,
    # Flow nodes
    FlowMergeConfig,
    FlowParallelConfig,
    FlowEndConfig,
)
from models.execution import (
    NodeExecutionState,
    NodeExecutionStatus,
    ExecutionContext,
    PortData,
)
from config import Config
from flask_core import get_logger

logger = get_logger(__name__)


class NodeExecutionError(Exception):
    """Raised when node execution fails"""
    def __init__(self, message: str, error_type: str = "execution"):
        super().__init__(message)
        self.error_type = error_type


@dataclass(slots=True)
class NodeExecutionResult:
    """
    Result of node execution.

    Attributes:
        success: Whether execution succeeded
        output_port: Which output port to use (for routing)
        output_data: Data to pass to next node(s)
        error: Error message if failed
        error_type: Type of error
        logs: Execution logs
    """
    success: bool
    output_port: Optional[str] = None
    output_data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    error_type: Optional[str] = None
    logs: List[str] = None

    def __post_init__(self):
        if self.logs is None:
            self.logs = []


class NodeExecutor:
    """
    Executes individual workflow nodes.

    Handles all node types with proper error handling, timeouts,
    and security sandboxing where appropriate.
    """

    def __init__(self, router_url: Optional[str] = None):
        """
        Initialize node executor.

        Args:
            router_url: URL of router API for action module calls
        """
        self.router_url = router_url or Config.ROUTER_URL
        self._http_session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        """Async context manager entry"""
        await self._ensure_http_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        await self.close()

    async def _ensure_http_session(self):
        """Ensure HTTP session exists"""
        if self._http_session is None or self._http_session.closed:
            timeout = aiohttp.ClientTimeout(total=60)
            self._http_session = aiohttp.ClientSession(timeout=timeout)

    async def close(self):
        """Close HTTP session"""
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
            self._http_session = None

    async def execute_node(
        self,
        node: WorkflowNode,
        context: ExecutionContext,
        input_data: Optional[Dict[str, Any]] = None
    ) -> NodeExecutionState:
        """
        Execute a workflow node.

        Args:
            node: Node configuration to execute
            context: Execution context with variables
            input_data: Input data from previous node(s)

        Returns:
            NodeExecutionState with execution results
        """
        state = NodeExecutionState(node_id=node.node_id)
        state.mark_started()

        # Store input data
        if input_data:
            for port_name, data in input_data.items():
                state.input_data[port_name] = PortData(port_name=port_name, data=data)

        try:
            # Route to appropriate executor based on node type
            result = await self._execute_by_type(node, context, input_data or {})

            if result.success:
                # Store output data
                if result.output_data:
                    for port_name, data in result.output_data.items():
                        state.set_output(port_name, data)

                # Store logs
                state.logs.extend(result.logs)

                state.mark_completed()
                logger.info(
                    f"Node {node.node_id} ({node.node_type.value}) completed successfully",
                    extra={
                        "workflow_id": context.workflow_id,
                        "execution_id": context.execution_id,
                        "node_id": node.node_id,
                    }
                )
            else:
                state.mark_failed(result.error or "Unknown error", result.error_type or "execution")
                logger.error(
                    f"Node {node.node_id} ({node.node_type.value}) failed: {result.error}",
                    extra={
                        "workflow_id": context.workflow_id,
                        "execution_id": context.execution_id,
                        "node_id": node.node_id,
                        "error_type": result.error_type,
                    }
                )

        except Exception as e:
            error_msg = f"Unexpected error executing node: {str(e)}"
            state.mark_failed(error_msg, "exception")
            logger.exception(
                f"Exception in node {node.node_id} ({node.node_type.value})",
                extra={
                    "workflow_id": context.workflow_id,
                    "execution_id": context.execution_id,
                    "node_id": node.node_id,
                }
            )

        return state

    async def _execute_by_type(
        self,
        node: WorkflowNode,
        context: ExecutionContext,
        input_data: Dict[str, Any]
    ) -> NodeExecutionResult:
        """Route execution to type-specific handler"""

        # Trigger nodes - validation only, not executed
        if isinstance(node, (TriggerCommandConfig, TriggerEventConfig,
                           TriggerWebhookConfig, TriggerScheduleConfig)):
            return await self._execute_trigger(node, context, input_data)

        # Condition nodes
        elif isinstance(node, ConditionIfConfig):
            return await self.execute_condition_if(node, context, input_data)
        elif isinstance(node, ConditionSwitchConfig):
            return await self.execute_condition_switch(node, context, input_data)
        elif isinstance(node, ConditionFilterConfig):
            return await self.execute_condition_filter(node, context, input_data)

        # Action nodes
        elif isinstance(node, ActionModuleConfig):
            return await self.execute_action_module(node, context, input_data)
        elif isinstance(node, ActionWebhookConfig):
            return await self.execute_action_webhook(node, context, input_data)
        elif isinstance(node, ActionChatMessageConfig):
            return await self.execute_action_chat_message(node, context, input_data)
        elif isinstance(node, ActionBrowserSourceConfig):
            return await self.execute_action_browser_source(node, context, input_data)
        elif isinstance(node, ActionDelayConfig):
            return await self.execute_action_delay(node, context, input_data)

        # Data nodes
        elif isinstance(node, DataTransformConfig):
            return await self.execute_data_transform(node, context, input_data)
        elif isinstance(node, DataVariableSetConfig):
            return await self.execute_data_variable_set(node, context, input_data)
        elif isinstance(node, DataVariableGetConfig):
            return await self.execute_data_variable_get(node, context, input_data)

        # Loop nodes
        elif isinstance(node, LoopForeachConfig):
            return await self.execute_loop_foreach(node, context, input_data)
        elif isinstance(node, LoopWhileConfig):
            return await self.execute_loop_while(node, context, input_data)
        elif isinstance(node, LoopBreakConfig):
            return await self.execute_loop_break(node, context, input_data)

        # Flow nodes
        elif isinstance(node, FlowMergeConfig):
            return await self.execute_flow_merge(node, context, input_data)
        elif isinstance(node, FlowParallelConfig):
            return await self.execute_flow_parallel(node, context, input_data)
        elif isinstance(node, FlowEndConfig):
            return await self.execute_flow_end(node, context, input_data)

        else:
            return NodeExecutionResult(
                success=False,
                error=f"Unknown node type: {type(node)}",
                error_type="validation"
            )

    # ========================================================================
    # TRIGGER NODE EXECUTORS (Validation only)
    # ========================================================================

    async def _execute_trigger(
        self,
        node: WorkflowNode,
        context: ExecutionContext,
        input_data: Dict[str, Any]
    ) -> NodeExecutionResult:
        """
        Trigger nodes are entry points - they don't execute during workflow.
        This just validates and passes through.
        """
        return NodeExecutionResult(
            success=True,
            output_port="output",
            output_data={"trigger_data": input_data},
            logs=[f"Trigger node {node.node_id} validated"]
        )

    # ========================================================================
    # CONDITION NODE EXECUTORS
    # ========================================================================

    async def execute_condition_if(
        self,
        node: ConditionIfConfig,
        context: ExecutionContext,
        input_data: Dict[str, Any]
    ) -> NodeExecutionResult:
        """
        Execute IF condition node.

        Evaluates condition rules and routes to true/false output port.
        """
        try:
            result = self._evaluate_condition_rules(node.condition, context)

            output_port = node.output_true_port if result else node.output_false_port

            return NodeExecutionResult(
                success=True,
                output_port=output_port,
                output_data={"condition_result": result},
                logs=[f"Condition evaluated to: {result}, routing to port: {output_port}"]
            )

        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=f"Condition evaluation error: {str(e)}",
                error_type="execution"
            )

    async def execute_condition_switch(
        self,
        node: ConditionSwitchConfig,
        context: ExecutionContext,
        input_data: Dict[str, Any]
    ) -> NodeExecutionResult:
        """
        Execute SWITCH condition node.

        Matches variable value against cases and routes to matching port.
        """
        try:
            variable_value = context.get_variable(node.variable)

            # Convert to string for comparison
            value_str = str(variable_value) if variable_value is not None else ""

            # Find matching case
            output_port = node.default_port
            matched_case = None

            for case_value, port_name in node.cases.items():
                if str(case_value) == value_str:
                    output_port = port_name
                    matched_case = case_value
                    break

            return NodeExecutionResult(
                success=True,
                output_port=output_port,
                output_data={
                    "switch_value": variable_value,
                    "matched_case": matched_case
                },
                logs=[
                    f"Switch variable '{node.variable}' = '{value_str}'",
                    f"Matched case: {matched_case}" if matched_case else "No match, using default",
                    f"Routing to port: {output_port}"
                ]
            )

        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=f"Switch evaluation error: {str(e)}",
                error_type="execution"
            )

    async def execute_condition_filter(
        self,
        node: ConditionFilterConfig,
        context: ExecutionContext,
        input_data: Dict[str, Any]
    ) -> NodeExecutionResult:
        """
        Execute FILTER condition node.

        Filters array items based on condition rules.
        """
        try:
            # Get input array
            input_array = context.get_variable(node.input_array)

            if not isinstance(input_array, list):
                return NodeExecutionResult(
                    success=False,
                    error=f"Variable '{node.input_array}' is not an array",
                    error_type="validation"
                )

            # Filter items
            filtered_items = []
            for item in input_array:
                # Create temporary context with item as variable
                temp_context = ExecutionContext(
                    execution_id=context.execution_id,
                    workflow_id=context.workflow_id,
                    workflow_version=context.workflow_version,
                    session_id=context.session_id,
                    entity_id=context.entity_id,
                    user_id=context.user_id,
                    variables={**context.variables, "item": item}
                )

                if self._evaluate_condition_rules(node.condition, temp_context):
                    filtered_items.append(item)

            # Store result
            context.set_variable(node.output_variable, filtered_items)

            return NodeExecutionResult(
                success=True,
                output_port="output",
                output_data={node.output_variable: filtered_items},
                logs=[
                    f"Filtered array '{node.input_array}'",
                    f"Input count: {len(input_array)}, Output count: {len(filtered_items)}"
                ]
            )

        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=f"Filter error: {str(e)}",
                error_type="execution"
            )

    def _evaluate_condition_rules(
        self,
        rules: List[ConditionRule],
        context: ExecutionContext
    ) -> bool:
        """
        Evaluate condition rules (AND logic between rules).

        Args:
            rules: List of condition rules
            context: Execution context with variables

        Returns:
            True if all rules pass, False otherwise
        """
        if not rules:
            return True

        for rule in rules:
            variable_value = context.get_variable(rule.variable)

            if not self._evaluate_operator(variable_value, rule.operator, rule.value):
                return False

        return True

    def _evaluate_operator(self, left: Any, operator: OperatorType, right: Any) -> bool:
        """Evaluate single operator comparison"""
        try:
            if operator == OperatorType.EQUALS:
                return left == right
            elif operator == OperatorType.NOT_EQUALS:
                return left != right
            elif operator == OperatorType.GREATER_THAN:
                return float(left) > float(right)
            elif operator == OperatorType.LESS_THAN:
                return float(left) < float(right)
            elif operator == OperatorType.GREATER_EQUAL:
                return float(left) >= float(right)
            elif operator == OperatorType.LESS_EQUAL:
                return float(left) <= float(right)
            elif operator == OperatorType.CONTAINS:
                return str(right) in str(left)
            elif operator == OperatorType.NOT_CONTAINS:
                return str(right) not in str(left)
            elif operator == OperatorType.MATCHES_REGEX:
                return bool(re.search(str(right), str(left)))
            elif operator == OperatorType.IN_LIST:
                return left in (right if isinstance(right, list) else [right])
            elif operator == OperatorType.NOT_IN_LIST:
                return left not in (right if isinstance(right, list) else [right])
            else:
                logger.warning(f"Unknown operator: {operator}")
                return False
        except Exception as e:
            logger.warning(f"Operator evaluation error: {e}")
            return False

    # ========================================================================
    # ACTION NODE EXECUTORS
    # ========================================================================

    async def execute_action_module(
        self,
        node: ActionModuleConfig,
        context: ExecutionContext,
        input_data: Dict[str, Any]
    ) -> NodeExecutionResult:
        """
        Execute ACTION_MODULE node.

        Calls Waddles action module via router API.
        """
        try:
            await self._ensure_http_session()

            # Build module input from variable mapping
            module_input = {}
            for workflow_var, module_param in node.input_mapping.items():
                value = context.get_variable(workflow_var)
                if value is not None:
                    module_input[module_param] = value

            # Build request payload
            payload = {
                "module_name": node.module_name,
                "module_version": node.module_version,
                "input": module_input,
                "entity_id": context.entity_id,
                "user_id": context.user_id,
                "session_id": context.session_id,
            }

            # Call router API
            url = f"{self.router_url}/api/v1/execute-module"
            timeout = aiohttp.ClientTimeout(total=node.timeout_seconds)

            async with self._http_session.post(
                url,
                json=payload,
                timeout=timeout
            ) as response:
                if response.status == 200:
                    result = await response.json()

                    # Map output to workflow variables
                    for module_output, workflow_var in node.output_mapping.items():
                        if module_output in result.get("output", {}):
                            context.set_variable(
                                workflow_var,
                                result["output"][module_output]
                            )

                    return NodeExecutionResult(
                        success=True,
                        output_port="success",
                        output_data=result.get("output", {}),
                        logs=[
                            f"Module '{node.module_name}' executed successfully",
                            f"Response: {result.get('message', 'OK')}"
                        ]
                    )
                else:
                    error_text = await response.text()
                    return NodeExecutionResult(
                        success=False,
                        error=f"Module execution failed: {response.status} - {error_text}",
                        error_type="api_error"
                    )

        except asyncio.TimeoutError:
            return NodeExecutionResult(
                success=False,
                error=f"Module execution timeout after {node.timeout_seconds}s",
                error_type="timeout"
            )
        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=f"Module execution error: {str(e)}",
                error_type="execution"
            )

    async def execute_action_webhook(
        self,
        node: ActionWebhookConfig,
        context: ExecutionContext,
        input_data: Dict[str, Any]
    ) -> NodeExecutionResult:
        """
        Execute ACTION_WEBHOOK node.

        Makes HTTP request to external webhook.
        """
        try:
            await self._ensure_http_session()

            # Replace variables in URL
            url = self._replace_variables(node.url, context)

            # Build headers
            headers = {
                key: self._replace_variables(value, context)
                for key, value in node.headers.items()
            }

            # Build body
            body = None
            if node.body_template:
                body_str = self._replace_variables(node.body_template, context)
                try:
                    body = json.loads(body_str)
                except json.JSONDecodeError:
                    body = body_str

            # Execute request with retries
            last_error = None
            for attempt in range(node.retry_count + 1):
                try:
                    timeout = aiohttp.ClientTimeout(total=node.timeout_seconds)

                    async with self._http_session.request(
                        method=node.method.value,
                        url=url,
                        headers=headers,
                        json=body if isinstance(body, dict) else None,
                        data=body if isinstance(body, str) else None,
                        timeout=timeout
                    ) as response:
                        response_text = await response.text()

                        if response.status < 400:
                            # Success
                            try:
                                response_data = json.loads(response_text)
                            except json.JSONDecodeError:
                                response_data = {"text": response_text}

                            # Store response if output variable specified
                            if node.output_variable:
                                context.set_variable(node.output_variable, response_data)

                            return NodeExecutionResult(
                                success=True,
                                output_port="success",
                                output_data={
                                    "status": response.status,
                                    "response": response_data
                                },
                                logs=[
                                    f"Webhook {node.method.value} {url}",
                                    f"Status: {response.status}",
                                    f"Attempt: {attempt + 1}/{node.retry_count + 1}"
                                ]
                            )
                        else:
                            last_error = f"HTTP {response.status}: {response_text}"

                except asyncio.TimeoutError:
                    last_error = f"Timeout after {node.timeout_seconds}s"
                except Exception as e:
                    last_error = str(e)

                # Wait before retry (exponential backoff)
                if attempt < node.retry_count:
                    await asyncio.sleep(2 ** attempt)

            # All retries failed
            return NodeExecutionResult(
                success=False,
                error=f"Webhook failed after {node.retry_count + 1} attempts: {last_error}",
                error_type="webhook_error"
            )

        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=f"Webhook error: {str(e)}",
                error_type="execution"
            )

    async def execute_action_chat_message(
        self,
        node: ActionChatMessageConfig,
        context: ExecutionContext,
        input_data: Dict[str, Any]
    ) -> NodeExecutionResult:
        """
        Execute ACTION_CHAT_MESSAGE node.

        Sends message to chat platform via router API.
        """
        try:
            await self._ensure_http_session()

            # Replace variables in message template
            message = self._replace_variables(node.message_template, context)

            # Build payload
            payload = {
                "message": message,
                "destination": node.destination,
                "entity_id": context.entity_id,
                "user_id": context.user_id,
                "platform": node.platform or context.platform,
                "reply_to_message": node.reply_to_message,
            }

            if node.channel_id:
                payload["channel_id"] = node.channel_id
            if node.user_id:
                payload["target_user_id"] = node.user_id

            # Call router API
            url = f"{self.router_url}/api/v1/send-message"

            async with self._http_session.post(url, json=payload) as response:
                if response.status == 200:
                    result = await response.json()

                    return NodeExecutionResult(
                        success=True,
                        output_port="success",
                        output_data={"message_id": result.get("message_id")},
                        logs=[f"Message sent: {message[:50]}..."]
                    )
                else:
                    error_text = await response.text()
                    return NodeExecutionResult(
                        success=False,
                        error=f"Message send failed: {response.status} - {error_text}",
                        error_type="api_error"
                    )

        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=f"Chat message error: {str(e)}",
                error_type="execution"
            )

    async def execute_action_browser_source(
        self,
        node: ActionBrowserSourceConfig,
        context: ExecutionContext,
        input_data: Dict[str, Any]
    ) -> NodeExecutionResult:
        """
        Execute ACTION_BROWSER_SOURCE node.

        Updates OBS browser source via browser_source_core_module API.
        """
        try:
            await self._ensure_http_session()

            # Replace variables in content template
            content = None
            if node.content_template:
                content = self._replace_variables(node.content_template, context)

            # Build payload
            payload = {
                "source_type": node.source_type,
                "action": node.action,
                "content": content,
                "duration": node.duration,
                "priority": node.priority,
                "entity_id": context.entity_id,
            }

            # Call browser source API
            browser_source_url = Config.ROUTER_URL.replace(
                "router-service:8000",
                "browser-source:8052"
            )
            url = f"{browser_source_url}/api/v1/update"

            async with self._http_session.post(url, json=payload) as response:
                if response.status == 200:
                    return NodeExecutionResult(
                        success=True,
                        output_port="success",
                        output_data={"source_updated": True},
                        logs=[f"Browser source '{node.source_type}' updated"]
                    )
                else:
                    error_text = await response.text()
                    return NodeExecutionResult(
                        success=False,
                        error=f"Browser source update failed: {error_text}",
                        error_type="api_error"
                    )

        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=f"Browser source error: {str(e)}",
                error_type="execution"
            )

    async def execute_action_delay(
        self,
        node: ActionDelayConfig,
        context: ExecutionContext,
        input_data: Dict[str, Any]
    ) -> NodeExecutionResult:
        """
        Execute ACTION_DELAY node.

        Pauses workflow execution for specified duration.
        """
        try:
            # Get delay duration
            delay_ms = node.delay_ms

            if node.delay_variable:
                delay_value = context.get_variable(node.delay_variable)
                if delay_value is not None:
                    delay_ms = int(delay_value)

            if delay_ms < 0:
                return NodeExecutionResult(
                    success=False,
                    error="Delay cannot be negative",
                    error_type="validation"
                )

            # Maximum delay of 5 minutes for safety
            max_delay_ms = 300000
            if delay_ms > max_delay_ms:
                delay_ms = max_delay_ms

            # Execute delay
            await asyncio.sleep(delay_ms / 1000.0)

            return NodeExecutionResult(
                success=True,
                output_port="output",
                output_data={"delayed_ms": delay_ms},
                logs=[f"Delayed for {delay_ms}ms"]
            )

        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=f"Delay error: {str(e)}",
                error_type="execution"
            )

    # ========================================================================
    # DATA NODE EXECUTORS
    # ========================================================================

    async def execute_data_transform(
        self,
        node: DataTransformConfig,
        context: ExecutionContext,
        input_data: Dict[str, Any]
    ) -> NodeExecutionResult:
        """
        Execute DATA_TRANSFORM node.

        Executes Python code in RestrictedPython sandbox with 5s timeout.
        """
        try:
            logs = []

            # Execute each transformation
            for output_var, expression in node.transformations.items():
                try:
                    # Execute with timeout
                    result = await asyncio.wait_for(
                        self._execute_restricted_python(expression, context),
                        timeout=5.0
                    )

                    # Store result
                    context.set_variable(output_var, result)
                    logs.append(f"Transformed '{output_var}' = {result}")

                except asyncio.TimeoutError:
                    return NodeExecutionResult(
                        success=False,
                        error=f"Transformation '{output_var}' timeout after 5s",
                        error_type="timeout"
                    )
                except Exception as e:
                    return NodeExecutionResult(
                        success=False,
                        error=f"Transformation '{output_var}' error: {str(e)}",
                        error_type="execution"
                    )

            return NodeExecutionResult(
                success=True,
                output_port="output",
                output_data={
                    var: context.get_variable(var)
                    for var in node.transformations.keys()
                },
                logs=logs
            )

        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=f"Transform error: {str(e)}",
                error_type="execution"
            )

    async def _execute_restricted_python(
        self,
        code: str,
        context: ExecutionContext
    ) -> Any:
        """
        Execute Python code in RestrictedPython sandbox.

        No file access, no network access, only safe builtins.
        """
        if not RESTRICTED_PYTHON_AVAILABLE:
            raise RuntimeError(
                "RestrictedPython not available. Install with: pip install RestrictedPython"
            )

        # Compile restricted code. This installed RestrictedPython version
        # (8.5) returns the compiled `types.CodeType` directly and raises
        # `SyntaxError` on compile failure -- it does NOT return a
        # `CompileResult(code, errors, ...)` wrapper. The previous
        # `byte_code.errors` / `byte_code.code` access assumed that older
        # wrapper shape, so this raised `AttributeError: 'code' object has
        # no attribute 'errors'` on every single call, silently caught by
        # execute_data_transform's except block -- DATA_TRANSFORM nodes
        # never actually executed in production. Undetected because this
        # file had 0% coverage on this path.
        try:
            byte_code = compile_restricted(
                code,
                filename='<workflow>',
                mode='eval'
            )
        except SyntaxError as e:
            raise ValueError(f"Compilation errors: {e}")

        # Build safe globals
        restricted_globals = {
            '__builtins__': safe_globals,
            '_getitem_': default_guarded_getitem,
            '_getiter_': default_guarded_getiter,
            '_iter_unpack_sequence_': guarded_iter_unpack_sequence,
            # Expose workflow variables
            **context.variables
        }

        # Execute in executor to avoid blocking
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            eval,
            byte_code,
            restricted_globals
        )

        return result

    async def execute_data_variable_set(
        self,
        node: DataVariableSetConfig,
        context: ExecutionContext,
        input_data: Dict[str, Any]
    ) -> NodeExecutionResult:
        """
        Execute DATA_VARIABLE_SET node.

        Sets workflow variables.
        """
        try:
            logs = []

            for var_name, value in node.variables.items():
                # Replace variables in value if it's a string
                if isinstance(value, str):
                    value = self._replace_variables(value, context)

                context.set_variable(var_name, value)
                logs.append(f"Set '{var_name}' = {value}")

            return NodeExecutionResult(
                success=True,
                output_port="output",
                output_data=dict(node.variables),
                logs=logs
            )

        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=f"Variable set error: {str(e)}",
                error_type="execution"
            )

    async def execute_data_variable_get(
        self,
        node: DataVariableGetConfig,
        context: ExecutionContext,
        input_data: Dict[str, Any]
    ) -> NodeExecutionResult:
        """
        Execute DATA_VARIABLE_GET node.

        Gets variable from context.
        """
        try:
            value = context.get_variable(node.variable_name, node.default_value)

            # Store in output variable if specified
            if node.output_variable:
                context.set_variable(node.output_variable, value)

            return NodeExecutionResult(
                success=True,
                output_port="output",
                output_data={node.output_variable or node.variable_name: value},
                logs=[f"Got '{node.variable_name}' = {value}"]
            )

        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=f"Variable get error: {str(e)}",
                error_type="execution"
            )

    # ========================================================================
    # LOOP NODE EXECUTORS
    # ========================================================================

    async def execute_loop_foreach(
        self,
        node: LoopForeachConfig,
        context: ExecutionContext,
        input_data: Dict[str, Any]
    ) -> NodeExecutionResult:
        """
        Execute LOOP_FOREACH node.

        Sets up loop iteration variables. Actual loop logic handled by engine.
        """
        try:
            # Get array to iterate
            array = context.get_variable(node.array_variable)

            if not isinstance(array, list):
                return NodeExecutionResult(
                    success=False,
                    error=f"Variable '{node.array_variable}' is not an array",
                    error_type="validation"
                )

            # Check max iterations
            if len(array) > node.max_iterations:
                return NodeExecutionResult(
                    success=False,
                    error=f"Array length {len(array)} exceeds max iterations {node.max_iterations}",
                    error_type="validation"
                )

            # Store loop metadata
            loop_state = {
                "array": array,
                "length": len(array),
                "index": 0,
                "item_variable": node.item_variable,
                "index_variable": node.index_variable,
            }

            return NodeExecutionResult(
                success=True,
                output_port="loop",
                output_data={"loop_state": loop_state},
                logs=[f"Starting foreach loop over {len(array)} items"]
            )

        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=f"Foreach error: {str(e)}",
                error_type="execution"
            )

    async def execute_loop_while(
        self,
        node: LoopWhileConfig,
        context: ExecutionContext,
        input_data: Dict[str, Any]
    ) -> NodeExecutionResult:
        """
        Execute LOOP_WHILE node.

        Checks while condition. Actual loop logic handled by engine.
        """
        try:
            # Evaluate condition
            condition_result = self._evaluate_condition_rules(node.condition, context)

            # Get current iteration count
            iteration = context.get_variable("_while_iteration", 0)

            if iteration >= node.max_iterations:
                return NodeExecutionResult(
                    success=False,
                    error=f"While loop exceeded max iterations: {node.max_iterations}",
                    error_type="validation"
                )

            # Update iteration
            context.set_variable("_while_iteration", iteration + 1)

            output_port = "loop" if condition_result else "exit"

            return NodeExecutionResult(
                success=True,
                output_port=output_port,
                output_data={
                    "condition_result": condition_result,
                    "iteration": iteration + 1
                },
                logs=[
                    f"While condition: {condition_result}",
                    f"Iteration: {iteration + 1}/{node.max_iterations}"
                ]
            )

        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=f"While loop error: {str(e)}",
                error_type="execution"
            )

    async def execute_loop_break(
        self,
        node: LoopBreakConfig,
        context: ExecutionContext,
        input_data: Dict[str, Any]
    ) -> NodeExecutionResult:
        """
        Execute LOOP_BREAK node.

        Breaks out of current loop.
        """
        try:
            # Check break condition if specified
            should_break = True
            if node.break_condition:
                should_break = self._evaluate_condition_rules(node.break_condition, context)

            if should_break:
                return NodeExecutionResult(
                    success=True,
                    output_port="break",
                    output_data={"loop_broken": True},
                    logs=["Loop break executed"]
                )
            else:
                return NodeExecutionResult(
                    success=True,
                    output_port="continue",
                    output_data={"loop_broken": False},
                    logs=["Loop break condition not met, continuing"]
                )

        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=f"Loop break error: {str(e)}",
                error_type="execution"
            )

    # ========================================================================
    # FLOW NODE EXECUTORS
    # ========================================================================

    async def execute_flow_merge(
        self,
        node: FlowMergeConfig,
        context: ExecutionContext,
        input_data: Dict[str, Any]
    ) -> NodeExecutionResult:
        """
        Execute FLOW_MERGE node.

        Consolidates multiple paths. Logic handled by engine.
        """
        return NodeExecutionResult(
            success=True,
            output_port="output",
            output_data=input_data,
            logs=["Flow merge executed"]
        )

    async def execute_flow_parallel(
        self,
        node: FlowParallelConfig,
        context: ExecutionContext,
        input_data: Dict[str, Any]
    ) -> NodeExecutionResult:
        """
        Execute FLOW_PARALLEL node.

        Initiates parallel execution. Logic handled by engine.
        """
        return NodeExecutionResult(
            success=True,
            output_port="parallel",
            output_data={
                "execution_type": node.execution_type,
                "merge_results": node.merge_results
            },
            logs=["Parallel execution initiated"]
        )

    async def execute_flow_end(
        self,
        node: FlowEndConfig,
        context: ExecutionContext,
        input_data: Dict[str, Any]
    ) -> NodeExecutionResult:
        """
        Execute FLOW_END node.

        Marks workflow completion.
        """
        return NodeExecutionResult(
            success=True,
            output_port=node.final_output_port,
            output_data=input_data,
            logs=["Workflow end reached"]
        )

    # ========================================================================
    # UTILITY METHODS
    # ========================================================================

    def _replace_variables(self, template: str, context: ExecutionContext) -> str:
        """
        Replace {{variable}} placeholders in template with values from context.

        Args:
            template: String with {{variable}} placeholders
            context: Execution context with variables

        Returns:
            String with variables replaced
        """
        if not template:
            return template

        def replace_match(match):
            var_name = match.group(1).strip()
            value = context.get_variable(var_name, "")
            return str(value)

        # Replace {{variable}} patterns
        result = re.sub(r'\{\{([^}]+)\}\}', replace_match, template)

        return result
