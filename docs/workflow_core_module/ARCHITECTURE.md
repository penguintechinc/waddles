# Workflow Core Module - Architecture

## Overview

The Workflow Core Module is a sophisticated workflow engine that enables visual workflow creation, validation, and execution with support for complex control flow, loops, conditionals, and integrations with external systems.

**Module Version:** 1.0.0
**Technology Stack:** Python 3.13, Quart (async Flask), gRPC, PyDAL, APScheduler, RestrictedPython

---

## Table of Contents

1. [System Architecture](#system-architecture)
2. [Component Overview](#component-overview)
3. [Workflow Engine](#workflow-engine)
4. [Node Types & Execution](#node-types--execution)
5. [Execution Flow](#execution-flow)
6. [Data Models](#data-models)
7. [Service Layer](#service-layer)
8. [Database Schema](#database-schema)
9. [Security Model](#security-model)
10. [Integration Points](#integration-points)

---

## System Architecture

### High-Level Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     API Layer (Quart)                       │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐     │
│  │ Workflow API │  │Execution API │  │ Webhook API  │     │
│  └──────────────┘  └──────────────┘  └──────────────┘     │
└─────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────────────────────────────────────┐
│                      Service Layer                          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐     │
│  │   Workflow   │  │   Workflow   │  │  Permission  │     │
│  │   Service    │  │    Engine    │  │   Service    │     │
│  └──────────────┘  └──────────────┘  └──────────────┘     │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐     │
│  │  Validation  │  │   Schedule   │  │   License    │     │
│  │   Service    │  │   Service    │  │   Service    │     │
│  └──────────────┘  └──────────────┘  └──────────────┘     │
└─────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────────────────────────────────────┐
│                    Execution Layer                          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐     │
│  │     Node     │  │   Module     │  │   Webhook    │     │
│  │   Executor   │  │   Executor   │  │   Executor   │     │
│  └──────────────┘  └──────────────┘  └──────────────┘     │
│  ┌──────────────┐  ┌──────────────┐                       │
│  │  Expression  │  │    DAG       │                       │
│  │    Engine    │  │  Execution   │                       │
│  └──────────────┘  └──────────────┘                       │
└─────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────────────────────────────────────┐
│                    Data Layer                               │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐     │
│  │  PostgreSQL  │  │    Redis     │  │  Router API  │     │
│  │   (PyDAL)    │  │   (Cache)    │  │ (External)   │     │
│  └──────────────┘  └──────────────┘  └──────────────┘     │
└─────────────────────────────────────────────────────────────┘
```

### Request Flow

#### Workflow Creation Flow

```
User → REST API → WorkflowService → ValidationService → Database
                       ↓                     ↓
                 LicenseService      PermissionService
```

#### Workflow Execution Flow

```
Trigger → REST/gRPC API → WorkflowEngine → NodeExecutor → Router/External
              ↓                  ↓              ↓
      PermissionService   DAG Execution   Expression Engine
              ↓                  ↓              ↓
          Database         State Tracking   Variable Evaluation
```

---

## Component Overview

### Core Components

| Component | Responsibility | Technology |
|-----------|---------------|------------|
| **app.py** | Application entry point, server initialization | Quart, Hypercorn |
| **WorkflowService** | Workflow CRUD operations, lifecycle management | Async Python |
| **WorkflowEngine** | Workflow execution, DAG traversal, state management | Async Python, ThreadPoolExecutor |
| **NodeExecutor** | Individual node execution logic | Async Python, RestrictedPython |
| **ValidationService** | Workflow structure validation | Python |
| **PermissionService** | Authorization and access control | Async Python |
| **LicenseService** | License validation | Async Python, Redis |
| **ScheduleService** | Scheduled workflow execution | APScheduler |
| **ExpressionEngine** | Variable interpolation, expression evaluation | RestrictedPython |

### Supporting Components

| Component | Responsibility |
|-----------|---------------|
| **GrpcHandler** | gRPC service implementation |
| **ModuleExecutor** | Execute module action nodes via Router |
| **WebhookExecutor** | Execute webhook action nodes |
| **WorkflowValidationService** | Deep validation of workflow definitions |

---

## Workflow Engine

### Workflow Execution Engine Architecture

The WorkflowEngine is the core of the module, responsible for orchestrating workflow execution.

#### Key Features

1. **DAG-Based Execution**
   - Builds directed acyclic graph from workflow definition
   - Topological sort for correct execution order
   - Detects and prevents cycles

2. **Parallel Execution**
   - ThreadPoolExecutor for concurrent node execution
   - Configurable parallelism limits
   - Proper synchronization and state management

3. **Loop Protection**
   - Maximum iterations per loop
   - Maximum total operations per workflow
   - Maximum loop nesting depth

4. **Error Handling**
   - Exponential backoff retry logic
   - Per-node error handling
   - Workflow-level timeout management

5. **State Persistence**
   - Incremental state saves to database
   - Crash recovery support
   - Execution history tracking

#### Execution Algorithm

```python
# Simplified execution flow
async def execute_workflow(workflow_id, trigger_data):
    # 1. Load workflow definition
    workflow = await load_workflow(workflow_id)

    # 2. Create execution context
    context = ExecutionContext(
        execution_id=generate_id(),
        workflow_id=workflow_id,
        variables=workflow.global_variables,
        metadata=trigger_data
    )

    # 3. Build execution graph (DAG)
    graph = build_execution_graph(workflow)

    # 4. Find trigger nodes
    trigger_nodes = get_trigger_nodes(workflow)

    # 5. Execute nodes in topological order
    for node_id in topological_sort(graph):
        # Check if node should execute
        if not should_execute_node(node_id, context):
            continue

        # Execute node
        result = await execute_node(node_id, context)

        # Update context with node outputs
        context.update(result)

        # Handle control flow (conditionals, loops)
        next_nodes = determine_next_nodes(result, graph)

        # Check loop limits
        if loop_count > MAX_LOOP_ITERATIONS:
            raise LoopLimitExceeded()

    # 6. Persist final state
    await save_execution_result(context)

    return context
```

### DAG Construction

The engine builds a directed acyclic graph from workflow connections:

```python
# Example workflow connections
connections = [
    Connection(from="node1", to="node2"),
    Connection(from="node2", to="node3"),
    Connection(from="node2", to="node4"),
    Connection(from="node3", to="node5"),
    Connection(from="node4", to="node5")
]

# Resulting DAG
graph = {
    "node1": ["node2"],
    "node2": ["node3", "node4"],
    "node3": ["node5"],
    "node4": ["node5"],
    "node5": []
}

# Topological sort order
execution_order = ["node1", "node2", "node3", "node4", "node5"]
# Note: node3 and node4 can execute in parallel
```

### State Tracking

The engine tracks comprehensive execution state:

```python
@dataclass
class ExecutionResult:
    execution_id: str
    workflow_id: str
    status: ExecutionStatus
    start_time: datetime
    end_time: Optional[datetime]
    execution_path: List[str]  # Ordered list of executed nodes
    node_states: Dict[str, NodeExecutionState]
    final_variables: Dict[str, Any]
    final_output: Any
    error_message: Optional[str]
    error_node_id: Optional[str]
    execution_time_seconds: float
```

---

## Node Types & Execution

### Node Type Categories

#### 1. Trigger Nodes

Entry points for workflow execution.

| Type | Description | Configuration |
|------|-------------|---------------|
| `trigger_command` | Triggered by chat commands | `command_name`, `aliases` |
| `trigger_event` | Triggered by system events | `event_type`, `filters` |
| `trigger_webhook` | Triggered by HTTP webhooks | `webhook_config` |
| `trigger_schedule` | Triggered by cron schedule | `cron_expression`, `timezone` |

#### 2. Condition Nodes

Control flow based on conditions.

| Type | Description | Outputs |
|------|-------------|---------|
| `condition_if` | If/else branching | `true`, `false` |
| `condition_switch` | Multi-way branching | `case_1`, `case_2`, ..., `default` |
| `condition_filter` | Filter data based on rules | `pass`, `fail` |

#### 3. Action Nodes

Execute operations.

| Type | Description | Side Effects |
|------|-------------|-------------|
| `action_module` | Execute module via Router | Calls external module |
| `action_webhook` | HTTP request to external API | HTTP request |
| `action_chat_message` | Send chat message | Message sent |
| `action_browser_source` | Update browser source | WebSocket message |
| `action_delay` | Wait for duration | Sleep/delay |

#### 4. Data Nodes

Manipulate variables and data.

| Type | Description | Purpose |
|------|-------------|---------|
| `data_transform` | Transform data with code | ETL operations |
| `data_variable_set` | Set variable value | State management |
| `data_variable_get` | Get variable value | Read state |

#### 5. Loop Nodes

Iteration control.

| Type | Description | Behavior |
|------|-------------|----------|
| `loop_foreach` | Iterate over collection | Loop over array/list |
| `loop_while` | Loop while condition true | Conditional iteration |
| `loop_break` | Break out of loop | Exit loop early |

#### 6. Flow Nodes

Flow control and synchronization.

| Type | Description | Behavior |
|------|-------------|----------|
| `flow_merge` | Merge multiple paths | Wait for all inputs |
| `flow_parallel` | Execute paths in parallel | Fork execution |
| `flow_end` | Workflow termination | End execution |

### Node Execution Model

Each node follows a consistent execution lifecycle:

```python
class NodeExecutionState:
    """Tracks state of a single node execution"""
    node_id: str
    status: NodeExecutionStatus  # pending, running, completed, failed
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    input_data: Dict[str, PortData]  # Input port values
    output_data: Dict[str, PortData]  # Output port values
    logs: List[str]  # Execution logs
    error: Optional[str]  # Error message
    error_type: Optional[str]  # Error category
    retry_count: int  # Number of retries attempted
```

### Example: Condition If Node Execution

```python
async def execute_condition_if(node: ConditionIfConfig, context: ExecutionContext):
    """
    Execute an if/else condition node.

    Evaluates condition and routes to 'true' or 'false' output port.
    """
    # Get condition configuration
    condition = node.config.condition

    # Evaluate condition (supports operators, variables)
    result = await evaluate_expression(condition, context.variables)

    # Determine output port
    output_port = "true" if result else "false"

    # Log result
    logs = [f"Condition evaluated to: {result}"]

    return NodeExecutionResult(
        success=True,
        output_port=output_port,
        output_data={"result": result},
        logs=logs
    )
```

### Example: Loop Foreach Node Execution

```python
async def execute_loop_foreach(node: LoopForeachConfig, context: ExecutionContext):
    """
    Execute a foreach loop node.

    Iterates over collection and executes loop body for each item.
    """
    # Get collection to iterate
    collection_var = node.config.collection_variable
    collection = context.get_variable(collection_var)

    if not isinstance(collection, (list, tuple)):
        raise NodeExecutionError(f"Variable '{collection_var}' is not a collection")

    results = []
    iteration_count = 0

    # Iterate over collection
    for index, item in enumerate(collection):
        # Check loop limit
        if iteration_count >= MAX_LOOP_ITERATIONS:
            raise LoopLimitExceeded()

        # Set loop variables
        context.set_variable(node.config.item_variable, item)
        context.set_variable(node.config.index_variable, index)

        # Execute loop body (child nodes)
        result = await execute_loop_body(node, context)
        results.append(result)

        iteration_count += 1

        # Check for break
        if context.get_variable("__break__"):
            break

    return NodeExecutionResult(
        success=True,
        output_port="output",
        output_data={"results": results},
        logs=[f"Completed {iteration_count} iterations"]
    )
```

---

## Execution Flow

### Complete Workflow Execution Sequence

```
1. Trigger Received
   ├─ Webhook POST, Command, Event, or Schedule
   └─ Trigger data extracted

2. Permission Check
   ├─ Load workflow definition
   ├─ Check user permissions (can_execute)
   └─ Validate license (if required)

3. Context Creation
   ├─ Generate execution_id
   ├─ Initialize variables with global_variables
   ├─ Add trigger data to context
   └─ Create ExecutionResult tracker

4. DAG Construction
   ├─ Build graph from connections
   ├─ Detect cycles
   ├─ Perform topological sort
   └─ Identify trigger nodes

5. Execution Loop
   For each node in topological order:
   ├─ Check if node should execute
   │  ├─ Check dependencies satisfied
   │  ├─ Check conditional routing
   │  └─ Check loop conditions
   │
   ├─ Execute Node
   │  ├─ Load node configuration
   │  ├─ Prepare input data from ports
   │  ├─ Execute node-specific logic
   │  ├─ Handle errors and retries
   │  └─ Collect output data
   │
   ├─ Update State
   │  ├─ Record in execution_path
   │  ├─ Update node_states
   │  ├─ Update context variables
   │  └─ Persist state to database
   │
   └─ Determine Next Nodes
      ├─ Follow output port connections
      ├─ Handle conditional branching
      └─ Queue next nodes for execution

6. Completion
   ├─ Mark execution as completed/failed
   ├─ Calculate execution metrics
   ├─ Persist final state
   └─ Return ExecutionResult
```

### Parallel Execution

Nodes without dependencies can execute in parallel:

```python
# Example: Parallel execution
workflow = {
    "node1": ["node2", "node3", "node4"],  # node2, node3, node4 have no interdependencies
    "node2": ["node5"],
    "node3": ["node5"],
    "node4": ["node5"],
    "node5": []
}

# Execution plan:
# Phase 1: node1 (sequential)
# Phase 2: node2, node3, node4 (parallel - up to MAX_PARALLEL_NODES)
# Phase 3: node5 (sequential - waits for all inputs)
```

### Error Handling & Retry

```python
async def execute_node_with_retry(node, context):
    """Execute node with exponential backoff retry"""
    retry_count = 0
    max_retries = workflow.metadata.max_retries

    while retry_count <= max_retries:
        try:
            result = await execute_node(node, context)
            return result
        except RetryableError as e:
            retry_count += 1
            if retry_count > max_retries:
                raise NodeExecutionError(f"Max retries exceeded: {e}")

            # Exponential backoff
            delay = 2 ** retry_count
            logger.warning(f"Node failed, retrying in {delay}s (attempt {retry_count}/{max_retries})")
            await asyncio.sleep(delay)
        except Exception as e:
            # Non-retryable error
            raise NodeExecutionError(f"Node execution failed: {e}")
```

---

## Data Models

### Core Models

#### WorkflowDefinition

```python
@dataclass
class WorkflowDefinition:
    """Complete workflow definition"""
    metadata: WorkflowMetadata
    nodes: Dict[str, WorkflowNode]
    connections: List[WorkflowConnection]
    global_variables: Dict[str, Any]
```

#### WorkflowMetadata

```python
@dataclass
class WorkflowMetadata:
    """Workflow metadata and settings"""
    workflow_id: str
    name: str
    description: str
    author_id: str
    community_id: str
    version: str
    status: WorkflowStatus  # draft, active, paused
    enabled: bool

    # Execution settings
    max_execution_time_seconds: int
    max_parallel_executions: int
    timeout_on_error: bool
    retry_failed_nodes: bool
    max_retries: int
```

#### WorkflowConnection

```python
@dataclass
class WorkflowConnection:
    """Connection between nodes"""
    connection_id: str
    from_node_id: str
    from_port_name: str
    to_node_id: str
    to_port_name: str
    enabled: bool
    conditional: Optional[str]  # Optional condition expression
```

#### ExecutionContext

```python
@dataclass
class ExecutionContext:
    """Runtime execution context"""
    execution_id: str
    workflow_id: str
    workflow_version: str
    session_id: str
    entity_id: str
    user_id: str
    variables: Dict[str, Any]  # Runtime variables
    metadata: Dict[str, Any]   # Trigger metadata
    start_time: datetime
    execution_path: List[str]  # Executed node IDs
```

### Node Model Hierarchy

```
BaseNodeConfig (abstract)
├─ TriggerCommandConfig
├─ TriggerEventConfig
├─ TriggerWebhookConfig
├─ TriggerScheduleConfig
├─ ConditionIfConfig
├─ ConditionSwitchConfig
├─ ConditionFilterConfig
├─ ActionModuleConfig
├─ ActionWebhookConfig
├─ ActionChatMessageConfig
├─ ActionBrowserSourceConfig
├─ ActionDelayConfig
├─ DataTransformConfig
├─ DataVariableSetConfig
├─ DataVariableGetConfig
├─ LoopForeachConfig
├─ LoopWhileConfig
├─ LoopBreakConfig
├─ FlowMergeConfig
├─ FlowParallelConfig
└─ FlowEndConfig
```

---

## Service Layer

### WorkflowService

Manages workflow CRUD operations and lifecycle.

**Key Methods:**
```python
class WorkflowService:
    async def create_workflow(workflow_data, community_id, entity_id, user_id, license_key)
    async def get_workflow(workflow_id, user_id, community_id)
    async def update_workflow(workflow_id, updates, user_id, community_id)
    async def delete_workflow(workflow_id, user_id, community_id)
    async def list_workflows(entity_id, user_id, filters, page, per_page)
    async def publish_workflow(workflow_id, user_id, community_id)
    async def validate_workflow(workflow_id)
```

### ValidationService

Validates workflow structure and configuration.

**Validation Rules:**
- At least one trigger node
- At least one end node
- All connections reference valid nodes
- All connections reference valid ports
- No circular dependencies (DAG requirement)
- Node-specific configuration validation

**Example:**
```python
class WorkflowValidationService:
    def validate(self, workflow: WorkflowDefinition) -> ValidationResult:
        errors = []
        warnings = []

        # Check for trigger nodes
        if not workflow.get_trigger_nodes():
            errors.append("Workflow must have at least one trigger node")

        # Validate connections
        for conn in workflow.connections:
            if conn.from_node_id not in workflow.nodes:
                errors.append(f"Connection references non-existent node: {conn.from_node_id}")

        # Check for cycles
        if has_cycle(workflow):
            errors.append("Workflow contains circular dependencies")

        return ValidationResult(
            is_valid=len(errors) == 0,
            errors=errors,
            warnings=warnings
        )
```

### PermissionService

Manages workflow permissions and access control.

**Permission Types:**
- `can_view` - View workflow definition
- `can_edit` - Modify workflow
- `can_delete` - Delete/archive workflow
- `can_execute` - Trigger execution

**Permission Resolution:**
1. Check if user is workflow owner (author_id)
2. Check direct user permissions
3. Check role-based permissions
4. Check community-level permissions

### LicenseService

Validates licenses for workflow features.

**Features Validated:**
- `workflows` - Basic workflow creation (limit: workflows_max)
- `advanced_workflows` - Advanced node types
- `workflow_schedules` - Scheduled executions

---

## Database Schema

### Key Tables

#### workflows

Primary workflow storage.

```sql
CREATE TABLE workflows (
    workflow_id UUID PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    entity_id INTEGER NOT NULL,
    community_id INTEGER NOT NULL,
    author_id INTEGER NOT NULL,
    status VARCHAR(50) DEFAULT 'draft',
    version VARCHAR(20) DEFAULT '1.0.0',
    nodes JSONB NOT NULL,
    connections JSONB NOT NULL,
    global_variables JSONB DEFAULT '{}',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    published_at TIMESTAMP,
    enabled BOOLEAN DEFAULT true,

    INDEX idx_entity_id (entity_id),
    INDEX idx_community_id (community_id),
    INDEX idx_status (status)
);
```

#### workflow_executions

Execution history and state.

```sql
CREATE TABLE workflow_executions (
    execution_id UUID PRIMARY KEY,
    workflow_id UUID NOT NULL REFERENCES workflows(workflow_id),
    status VARCHAR(50) NOT NULL,
    start_time TIMESTAMP NOT NULL,
    end_time TIMESTAMP,
    execution_time_seconds DECIMAL(10, 3),
    nodes_executed INTEGER DEFAULT 0,
    execution_path JSONB DEFAULT '[]',
    node_states JSONB DEFAULT '{}',
    final_variables JSONB DEFAULT '{}',
    final_output JSONB,
    error_message TEXT,
    error_node_id VARCHAR(255),
    created_at TIMESTAMP DEFAULT NOW(),

    INDEX idx_workflow_id (workflow_id),
    INDEX idx_status (status),
    INDEX idx_start_time (start_time DESC)
);
```

---

## Security Model

### Authentication

- JWT token validation
- Service-to-service API keys
- Webhook HMAC signatures

### Authorization

- Role-Based Access Control (RBAC)
- Permission checks before all operations
- Community-level isolation

### Sandboxing

- RestrictedPython for data transformation nodes
- Expression engine (`services/expression_engine.py`): AST allowlist walker, **never
  `eval`/`exec`/`compile`**. Allowed: literals, arithmetic/boolean/comparison
  operators, variable names, dict-key attribute access, list/dict/tuple subscripts,
  public built-in functions, and a fixed string-method set. Refused: any name or
  attribute starting with `_` (dunders), `getattr`-style object traversal
  (`().__class__.__bases__[0].__subclasses__()`), lambdas/comprehensions, keyword/starred
  calls, slices, oversized `**`/`*` results, expressions over 4096 chars. Webhook
  `$(...)` templates use the same approach (`SafeExpressionEvaluator`).
- No access to system modules or file I/O

### SSRF Egress Guard (ACTION_WEBHOOK)

`services/egress_guard.py` protects the webhook node, which fetches an
author-controllable URL and reflects the response:

- http/https only; URL userinfo refused
- Host is resolved and **every** address must be globally routable — RFC1918,
  loopback, link-local (incl. 169.254.169.254), CGNAT, unique-local (fc00::/7),
  fe80::/10, multicast/reserved/unspecified, and IPv4 embedded in IPv6
  (mapped, NAT64, 6to4, Teredo) are blocked
- DNS-rebinding safe: pre-flight check plus a connect-time `GuardedResolver` whose
  validated answer *is* the connect target (no second lookup between check and use)
- Redirects are not followed (a redirect hop could target a blocked address)
- Blocked targets fail loudly: `NodeExecutionResult(error_type="egress_blocked")`,
  never retried, never silently fetched
- The guarded session is separate from the internal-service session (router etc.
  live on private addresses and are intentionally unguarded)

---

## Integration Points

### Router Module

Execute module actions:

```
POST /api/v1/modules/execute
{
  "module_name": "music_module",
  "action": "play_song",
  "params": {...}
}
```

### License Server

Validate licenses:

```
POST /api/v1/licenses/validate
{
  "community_id": 123,
  "feature": "workflows"
}
```

### External Webhooks

HTTP requests to external APIs:

```
POST https://external-api.com/webhook
Headers:
  Content-Type: application/json
  X-Signature: hmac-sha256-signature
Body: {...}
```
