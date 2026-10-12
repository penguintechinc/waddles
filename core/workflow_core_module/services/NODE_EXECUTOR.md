# Node Executor Service

## Overview

The `NodeExecutor` service is responsible for executing individual workflow nodes. It handles all node types including triggers, conditions, actions, data operations, loops, and flow control. Each node type has specialized execution logic with comprehensive error handling, timeouts, and security sandboxing.

## Architecture

### Key Components

1. **NodeExecutor Class**: Main executor with async HTTP session management
2. **Node Type Handlers**: Specialized methods for each node type
3. **Condition Evaluator**: Evaluates condition rules with multiple operators
4. **Variable Replacement**: Template engine for {{variable}} substitution
5. **RestrictedPython Sandbox**: Secure Python code execution (5s timeout)

### Supported Node Types

```
Trigger Nodes (16 types)
├── TriggerCommand      - Command pattern matching
├── TriggerEvent        - Platform event handling
├── TriggerWebhook      - HTTP webhook receiver
└── TriggerSchedule     - Cron schedule trigger

Condition Nodes (3 types)
├── ConditionIf         - True/false branching
├── ConditionSwitch     - Multi-way routing
└── ConditionFilter     - Array filtering

Action Nodes (5 types)
├── ActionModule        - Call WaddleBot modules via router API
├── ActionWebhook       - HTTP requests to external services
├── ActionChatMessage   - Send platform messages
├── ActionBrowserSource - Update OBS overlays
└── ActionDelay         - Async sleep

Data Nodes (3 types)
├── DataTransform       - Python code execution (sandboxed)
├── DataVariableSet     - Set workflow variables
└── DataVariableGet     - Get workflow variables

Loop Nodes (3 types)
├── LoopForeach         - Array iteration
├── LoopWhile           - Condition-based looping
└── LoopBreak           - Break from loops

Flow Nodes (3 types)
├── FlowMerge           - Consolidate paths
├── FlowParallel        - Parallel execution
└── FlowEnd             - Workflow termination
```

## Usage

### Basic Usage

```python
from services.node_executor import NodeExecutor
from models.execution import ExecutionContext
from models.nodes import ConditionIfConfig, ConditionRule, OperatorType

# Create executor
executor = NodeExecutor()

# Create execution context
context = ExecutionContext(
    execution_id="exec-001",
    workflow_id="workflow-123",
    workflow_version="1.0.0",
    session_id="session-456",
    entity_id="community-789",
    user_id="user-abc",
    variables={"count": 5}
)

# Define a node
node = ConditionIfConfig(
    node_id="check_count",
    label="Check Count",
    position={"x": 100, "y": 200},
    condition=[
        ConditionRule(
            variable="count",
            operator=OperatorType.GREATER_THAN,
            value=3
        )
    ]
)

# Execute node
state = await executor.execute_node(node, context)

print(f"Status: {state.status}")
print(f"Output: {state.output_data}")
print(f"Logs: {state.logs}")
```

### Async Context Manager

```python
async with NodeExecutor() as executor:
    # HTTP session automatically managed
    state = await executor.execute_node(node, context)
```

### Custom Router URL

```python
executor = NodeExecutor(router_url="http://custom-router:8080")
```

## Node Execution Details

### Trigger Nodes

Trigger nodes are entry points and don't execute during workflow runtime - they only validate.

```python
# Trigger nodes pass through
node = TriggerCommandConfig(
    node_id="cmd_trigger",
    command_pattern="!help",
    platforms=["twitch", "discord"]
)
state = await executor.execute_node(node, context)
# Returns success with validation
```

### Condition Nodes

#### IF Condition

Evaluates condition rules (AND logic) and routes to true/false ports:

```python
node = ConditionIfConfig(
    node_id="age_check",
    label="Check Age",
    condition=[
        ConditionRule(
            variable="age",
            operator=OperatorType.GREATER_EQUAL,
            value=18
        )
    ],
    output_true_port="adult",
    output_false_port="minor"
)
```

**Supported Operators**:
- `EQUALS`, `NOT_EQUALS` - Basic equality
- `GREATER_THAN`, `LESS_THAN`, `GREATER_EQUAL`, `LESS_EQUAL` - Numeric comparison
- `CONTAINS`, `NOT_CONTAINS` - String/array containment
- `MATCHES_REGEX` - Regular expression matching
- `IN_LIST`, `NOT_IN_LIST` - List membership

#### SWITCH Condition

Routes based on variable value matching:

```python
node = ConditionSwitchConfig(
    node_id="tier_switch",
    variable="subscription_tier",
    cases={
        "1": "tier1_port",
        "2": "tier2_port",
        "3": "tier3_port"
    },
    default_port="no_sub"
)
```

#### FILTER Condition

Filters array items based on conditions:

```python
node = ConditionFilterConfig(
    node_id="filter_active",
    input_array="users",
    condition=[
        ConditionRule(
            variable="item.active",
            operator=OperatorType.EQUALS,
            value=True
        )
    ],
    output_variable="active_users"
)
```

### Action Nodes

#### Module Action

Calls WaddleBot action modules via router API:

```python
node = ActionModuleConfig(
    node_id="call_ai",
    module_name="ai_interaction",
    module_version="latest",
    input_mapping={
        "user_message": "message",
        "user_context": "context"
    },
    output_mapping={
        "ai_response": "response"
    },
    timeout_seconds=30
)
```

#### Webhook Action

Makes HTTP requests to external services:

```python
node = ActionWebhookConfig(
    node_id="notify_discord",
    url="https://discord.com/api/webhooks/{{webhook_id}}/{{token}}",
    method=HttpMethod.POST,
    headers={
        "Content-Type": "application/json"
    },
    body_template='{"content": "Alert: {{message}}"}',
    output_variable="webhook_response",
    retry_count=3,
    timeout_seconds=10
)
```

**Features**:
- Variable replacement in URL, headers, body
- Automatic retries with exponential backoff
- JSON/text body support
- Response capture

#### Chat Message Action

Sends messages to chat platforms:

```python
node = ActionChatMessageConfig(
    node_id="send_welcome",
    message_template="Welcome {{username}}! You are viewer #{{count}}",
    destination="current_channel",
    platform="twitch",
    reply_to_message=True
)
```

#### Browser Source Action

Updates OBS browser sources:

```python
node = ActionBrowserSourceConfig(
    node_id="show_alert",
    source_type="ticker",
    action="display",
    content_template="New follower: {{username}}!",
    duration=10,
    priority=5
)
```

#### Delay Action

Pauses workflow execution:

```python
node = ActionDelayConfig(
    node_id="wait_5s",
    delay_ms=5000
)

# Or with dynamic delay
node = ActionDelayConfig(
    node_id="wait_dynamic",
    delay_variable="delay_duration"
)
```

**Safety Limits**:
- Maximum delay: 300,000ms (5 minutes)
- Negative delays rejected

### Data Nodes

#### Transform Data

Executes Python code in RestrictedPython sandbox:

```python
node = DataTransformConfig(
    node_id="calculate",
    transformations={
        "total": "price * quantity",
        "tax": "total * 0.08",
        "final": "total + tax"
    },
    expression_language="python"
)
```

**Security**:
- No file access
- No network access
- 5-second timeout per transformation
- Only safe builtins available
- Access to workflow variables only

#### Set Variables

Sets workflow variables:

```python
node = DataVariableSetConfig(
    node_id="set_greeting",
    variables={
        "greeting": "Hello {{username}}!",
        "timestamp": "{{now}}",
        "status": "active"
    },
    scope=VariableScope.WORKFLOW
)
```

#### Get Variables

Retrieves variables from context:

```python
node = DataVariableGetConfig(
    node_id="get_user",
    variable_name="current_user",
    output_variable="user_data",
    default_value={"name": "Guest"}
)
```

### Loop Nodes

#### ForEach Loop

Iterates over array items:

```python
node = LoopForeachConfig(
    node_id="process_items",
    array_variable="items",
    item_variable="current_item",
    index_variable="current_index",
    max_iterations=10000
)
```

**Loop State Output**:
```python
{
    "array": [...],
    "length": 5,
    "index": 0,
    "item_variable": "current_item",
    "index_variable": "current_index"
}
```

#### While Loop

Loops while condition is true:

```python
node = LoopWhileConfig(
    node_id="retry_loop",
    condition=[
        ConditionRule(
            variable="retry_count",
            operator=OperatorType.LESS_THAN,
            value=5
        )
    ],
    max_iterations=10000
)
```

**Safety**:
- Tracks iteration count in `_while_iteration` variable
- Enforces max_iterations limit
- Returns "loop" port to continue, "exit" port to break

#### Break Statement

Exits current loop:

```python
node = LoopBreakConfig(
    node_id="break_on_error",
    break_condition=[
        ConditionRule(
            variable="error_occurred",
            operator=OperatorType.EQUALS,
            value=True
        )
    ]
)
```

### Flow Nodes

#### Merge

Consolidates multiple execution paths:

```python
node = FlowMergeConfig(
    node_id="merge_results",
    input_ports_required=-1  # -1 = all inputs required
)
```

#### Parallel

Executes multiple paths simultaneously:

```python
node = FlowParallelConfig(
    node_id="parallel_actions",
    execution_type="parallel",
    merge_results=True,
    timeout_seconds=300
)
```

#### End

Marks workflow completion:

```python
node = FlowEndConfig(
    node_id="workflow_end",
    final_output_port="end"
)
```

## Variable Replacement

The executor supports `{{variable}}` template syntax:

```python
# In any string field
message = "Hello {{username}}, you have {{points}} points!"

# Results in
"Hello JohnDoe, you have 150 points!"
```

**Usage in**:
- Message templates
- Webhook URLs
- Webhook headers
- Webhook body
- Variable values
- Content templates

## Error Handling

### Node Execution Result

Every node execution returns a `NodeExecutionState`:

```python
state = await executor.execute_node(node, context)

# Check status
if state.status == NodeExecutionStatus.COMPLETED:
    print("Success!")
    print(f"Output: {state.output_data}")
elif state.status == NodeExecutionStatus.FAILED:
    print(f"Failed: {state.error}")
    print(f"Error type: {state.error_type}")

# Access logs
for log in state.logs:
    print(log)

# Get execution time
duration = state.get_execution_time_seconds()
```

### Error Types

- `validation` - Input validation failed
- `execution` - Runtime execution error
- `timeout` - Operation timeout
- `api_error` - External API call failed
- `webhook_error` - Webhook call failed
- `exception` - Unexpected exception

### Retry Logic

Action nodes with `retry_count` automatically retry on failure:

```python
node = ActionWebhookConfig(
    url="https://api.example.com/notify",
    retry_count=3,  # Total 4 attempts
    timeout_seconds=10
)
```

**Backoff**: Exponential backoff (2^attempt seconds)
- Attempt 1: Immediate
- Attempt 2: 2s delay
- Attempt 3: 4s delay
- Attempt 4: 8s delay

## Performance & Limits

### Timeouts

| Node Type | Default Timeout | Max Timeout |
|-----------|----------------|-------------|
| ActionModule | 30s | Configurable |
| ActionWebhook | 10s | Configurable |
| ActionDelay | N/A | 300s (5 min) |
| DataTransform | 5s | Fixed |
| HTTP Session | 60s | Fixed |

### Iteration Limits

| Node Type | Default Max | Purpose |
|-----------|-------------|---------|
| LoopForeach | 10,000 | Prevent infinite loops |
| LoopWhile | 10,000 | Prevent infinite loops |
| ConditionFilter | N/A | Bounded by array size |

### Memory Safety

- HTTP session pooling
- Async context manager cleanup
- No blocking operations
- RestrictedPython sandboxing

## Testing

Run comprehensive tests:

```bash
cd /home/penguin/code/WaddleBot/core/workflow_core_module
pytest services/test_node_executor.py -v
```

### Test Coverage

- ✓ All condition node types
- ✓ All operators (12 types)
- ✓ Action nodes (delay, variable ops)
- ✓ Data nodes (set, get, transform)
- ✓ Loop nodes (foreach, while, break)
- ✓ Flow nodes (merge, parallel, end)
- ✓ Variable replacement
- ✓ Error handling
- ✓ Validation
- ✓ Context manager
- ✓ Timeouts and limits

## Integration

### With Workflow Engine

```python
from services.node_executor import NodeExecutor
from services.workflow_engine import WorkflowEngine

# Engine uses executor for all nodes
engine = WorkflowEngine()
executor = NodeExecutor()

# Engine calls executor.execute_node() for each node
async def execute_workflow(workflow, trigger_data):
    async with executor:
        for node in workflow.nodes:
            state = await executor.execute_node(node, context)
            # Handle state, routing, etc.
```

### With Router API

Executor automatically calls router API for:
- `ActionModule` nodes
- `ActionChatMessage` nodes

Configure router URL:
```python
# Via constructor
executor = NodeExecutor(router_url="http://router:8000")

# Via config
from config import Config
Config.ROUTER_URL = "http://custom-router:8080"
```

## Dependencies

```
aiohttp>=3.9.1          # Async HTTP client
RestrictedPython==7.0   # Safe Python execution
```

## Logging

All node executions are logged with comprehensive AAA logging:

```python
# Success logs
logger.info(
    f"Node {node_id} ({node_type}) completed successfully",
    extra={
        "workflow_id": context.workflow_id,
        "execution_id": context.execution_id,
        "node_id": node_id,
    }
)

# Error logs
logger.error(
    f"Node {node_id} ({node_type}) failed: {error}",
    extra={
        "workflow_id": context.workflow_id,
        "execution_id": context.execution_id,
        "node_id": node_id,
        "error_type": error_type,
    }
)
```

## Security Considerations

### RestrictedPython Sandbox

Data transform nodes execute in a restricted environment:

**Allowed**:
- Basic Python operations (+, -, *, /, %, etc.)
- Variable access from workflow context
- Safe builtins (len, str, int, float, etc.)

**Blocked**:
- File I/O (open, read, write)
- Network access (socket, urllib, requests)
- System calls (os, sys, subprocess)
- Import statements
- Dangerous builtins (eval, exec, compile)

### Webhook Security

External webhook calls:
- Support custom headers for authentication
- Variable replacement for dynamic URLs
- Configurable timeouts
- Retry with exponential backoff
- Response capture

### API Calls

Module and chat message actions:
- Use internal router API
- Session-based authentication
- Entity/user context passed
- Timeout enforcement

## Best Practices

1. **Always use async context manager** for proper cleanup
   ```python
   async with NodeExecutor() as executor:
       state = await executor.execute_node(node, context)
   ```

2. **Set appropriate timeouts** for external calls
   ```python
   node.timeout_seconds = 30  # Based on expected response time
   ```

3. **Use retries for unreliable services**
   ```python
   node.retry_count = 3  # For flaky external APIs
   ```

4. **Validate node inputs** before execution
   ```python
   is_valid, errors = workflow.validate()
   if not is_valid:
       handle_errors(errors)
   ```

5. **Monitor execution metrics**
   ```python
   duration = state.get_execution_time_seconds()
   if duration > threshold:
       log_slow_node(node_id, duration)
   ```

6. **Handle errors gracefully**
   ```python
   if state.status == NodeExecutionStatus.FAILED:
       # Log, retry, or route to error handler
       await handle_node_error(state)
   ```

## Files

- `/home/penguin/code/WaddleBot/core/workflow_core_module/services/node_executor.py` (1,264 lines)
- `/home/penguin/code/WaddleBot/core/workflow_core_module/services/test_node_executor.py` (560 lines)
- `/home/penguin/code/WaddleBot/core/workflow_core_module/services/NODE_EXECUTOR.md` (This file)

## See Also

- `models/nodes.py` - Node type definitions
- `models/execution.py` - Execution state models
- `services/workflow_engine.py` - Workflow orchestration
- `docs/event-processing.md` - Event processing overview

## Security: SSRF Egress Guard

`ACTION_WEBHOOK` fetches an author-controllable URL and reflects the response,
so it is routed through `services/egress_guard.py` using a dedicated
`_webhook_session` (the internal `_http_session` is unguarded by design).

- http/https only, no URL credentials, every resolved address must be public
  (RFC1918/loopback/link-local incl. 169.254.169.254/unique-local/CGNAT/IPv6
  equivalents blocked)
- DNS-rebinding safe: pre-flight check + connect-time `GuardedResolver` pinning
- Redirects are refused, not followed
- Blocked => `NodeExecutionResult(error_type="egress_blocked")`, never retried
- Tests: `tests/test_egress_guard.py`
