"""
Workflow Execution API Controller
==================================

REST API for workflow execution tracking with:
- Execution triggering (with permission checks)
- Real-time execution status queries
- Execution logs and node state inspection
- Dry-run testing (test mode)
- Performance metrics
- Execution cancellation

Endpoints:
- POST   /api/v1/workflows/:id/execute      - Trigger workflow execution
- GET    /api/v1/workflows/executions/:execId - Get execution details
- POST   /api/v1/workflows/executions/:execId/cancel - Cancel running execution
- GET    /api/v1/workflows/:id/executions   - List executions for workflow (paginated)
- POST   /api/v1/workflows/:id/test        - Dry-run test with detailed trace
"""

import logging
from quart import Blueprint, request, current_app
from functools import wraps
from typing import Callable
from datetime import datetime

from flask_core import (
    success_response,
    error_response,
    async_endpoint,
    auth_required,
    tenant_middleware,
    get_tenant_context,
    require_community_admin,
    require_community_member,
    CommunityAccessError,
    describe_db_error,
)
from services.workflow_engine import (
    WorkflowEngine,
    WorkflowEngineException,
    WorkflowTimeoutException,
    NodeExecutionException,
)
from services.permission_service import PermissionService
from services.community_scope import (
    WorkflowCommunityNotFoundError,
    resolve_execution_community_id,
    resolve_workflow_community_id,
)
from models.execution import ExecutionStatus, ExecutionContext
from controllers.error_logging import log_internal_error


logger = logging.getLogger(__name__)


# Create blueprint
execution_api = Blueprint('execution_api', __name__, url_prefix='/api/v1/workflows')


def get_workflow_service():
    """Get workflow service from app context."""
    return current_app.config.get('workflow_service')


def get_workflow_engine() -> WorkflowEngine:
    """Get workflow engine from app context."""
    return current_app.config.get('workflow_engine')


def get_permission_service() -> PermissionService:
    """Get permission service from app context."""
    return current_app.config.get('permission_service')


def get_dal():
    """Get database access layer from app context."""
    return current_app.config.get('dal')


async def _authorize_workflow_community(
    workflow_id: str, user_id: int, *, admin_required: bool
) -> int:
    """Resolve a workflow's REAL `community_id` and require caller membership/admin of it.

    SECURITY (A01, BOLA/IDOR -- deferred from gh security PR #260): mirrors
    `controllers/workflow_api.py::_authorize_community` -- never trusts a
    client-supplied `community_id` for authorization. See that module's
    docstring for the full rationale.
    """
    # `workflow_service.dal` (Postgres-only raw SQL, `workflows` table) and
    # `get_dal()` (pydal query objects, `communities`/`community_members`/
    # `tenants` tables) are the SAME underlying pydal `DAL` in production
    # (see `workflow_core_module/app.py`'s `dal`/`async_dal` split) --
    # fetched separately so this stays correct even where they legitimately
    # differ (e.g. tests).
    workflow_dal = get_workflow_service().dal
    shared_dal = get_dal()
    async_dal = current_app.config.get('async_dal')
    community_id = await resolve_workflow_community_id(workflow_dal, workflow_id)
    ctx = get_tenant_context(request)
    check = require_community_admin if admin_required else require_community_member
    await check(async_dal, shared_dal, request, ctx, community_id=community_id, user_id=user_id)
    return community_id


async def _authorize_execution_community(
    execution_id: str, user_id: int, *, admin_required: bool
) -> int:
    """Resolve an execution's owning workflow's REAL `community_id` and require caller access.

    SECURITY (A01): `get_execution_details` and `cancel_execution` had NO
    permission check at all beyond `auth_required` -- any authenticated
    user (any tenant/community) could view or cancel any execution by UUID.
    """
    workflow_dal = get_workflow_service().dal
    shared_dal = get_dal()
    async_dal = current_app.config.get('async_dal')
    community_id = await resolve_execution_community_id(workflow_dal, execution_id)
    ctx = get_tenant_context(request)
    check = require_community_admin if admin_required else require_community_member
    await check(async_dal, shared_dal, request, ctx, community_id=community_id, user_id=user_id)
    return community_id


def handle_execution_errors(f: Callable) -> Callable:
    """
    Decorator to handle execution-specific exceptions.

    Converts execution exceptions to appropriate HTTP responses.
    """
    @wraps(f)
    async def decorated_function(*args, **kwargs):
        try:
            return await f(*args, **kwargs)
        except WorkflowTimeoutException as e:
            logger.warning(
                f"Workflow execution timeout: {describe_db_error(e)}",
                extra={
                    "event_type": "ERROR",
                    "action": f.__name__,
                    "result": "TIMEOUT"
                }
            )
            return error_response(
                message=str(e),
                status_code=504,
                error_code="EXECUTION_TIMEOUT"
            )
        except WorkflowEngineException as e:
            logger.error(
                f"Workflow engine error: {describe_db_error(e)}",
                extra={
                    "event_type": "ERROR",
                    "action": f.__name__,
                    "result": "FAILURE"
                }
            )
            return error_response(
                message=str(e),
                status_code=400,
                error_code="EXECUTION_ERROR"
            )
        except ValueError as e:
            # Client errors (bad input)
            return error_response(
                message=str(e),
                status_code=400,
                error_code="BAD_REQUEST"
            )
        except WorkflowCommunityNotFoundError as e:
            return error_response(
                message=str(e),
                status_code=404,
                error_code="WORKFLOW_NOT_FOUND"
            )
        except CommunityAccessError as e:
            # SECURITY (A01): caller isn't a member/admin of the workflow's
            # or execution's REAL community.
            logger.warning(
                f"Community access denied: {e.message}",
                extra={
                    "event_type": "AUTHZ",
                    "action": f.__name__,
                    "result": "FORBIDDEN"
                }
            )
            return error_response(
                message=e.message,
                status_code=403,
                error_code="COMMUNITY_ACCESS_DENIED"
            )
        except PermissionError as e:
            logger.warning(
                f"Permission denied: {describe_db_error(e)}",
                extra={
                    "event_type": "AUTHZ",
                    "action": f.__name__,
                    "result": "DENIED"
                }
            )
            return error_response(
                message=str(e),
                status_code=403,
                error_code="PERMISSION_DENIED"
            )
        except Exception as e:
            logger.error(
                f"Unexpected error in {f.__name__}: {describe_db_error(e)}",
                extra={
                    "event_type": "ERROR",
                    "action": f.__name__,
                    "result": "FAILURE"
                }
            )
            return error_response(
                message="Internal server error",
                status_code=500,
                error_code="INTERNAL_ERROR"
            )

    return decorated_function


# ============================================================================
# POST /api/v1/workflows/:id/execute - Trigger workflow execution
# ============================================================================

@execution_api.route('/<workflow_id>/execute', methods=['POST'])
@async_endpoint
@tenant_middleware
@auth_required
@handle_execution_errors
async def execute_workflow(workflow_id: str):
    """
    Trigger workflow execution with permission check.

    Path Parameters:
        workflow_id: Workflow UUID

    Request Body:
    {
        "community_id": 123,
        "variables": {
            "param1": "value1",
            "param2": "value2"
        },
        "metadata": {
            "trigger_source": "api",
            "session_id": "sess_123"
        }
    }

    Returns:
        202: Execution started (execution_id in response)
        400: Invalid input or workflow not found
        401: Unauthorized
        403: Permission denied
        504: Workflow timeout
        500: Server error

    Response:
    {
        "success": true,
        "data": {
            "execution_id": "exec_uuid",
            "workflow_id": "workflow_uuid",
            "status": "running",
            "start_time": "2025-12-09T12:00:00Z"
        }
    }
    """
    user_info = request.current_user
    user_id = int(user_info['user_id'])

    data = await request.get_json() or {}

    # SECURITY (A01, BOLA/IDOR -- deferred from gh security PR #260): the
    # previous `community_id = int(data['community_id'])` was entirely
    # client-supplied and passed straight into `check_permission` as
    # advisory context -- a caller could pass a `community_id` they hold no
    # membership in and still trigger execution if any other permission
    # branch happened to pass. Resolves the workflow's REAL `community_id`
    # from the database and requires at least active membership of it
    # first; `check_permission`'s fine-grained `can_execute` check below
    # still gates the actual execute action.
    community_id = await _authorize_workflow_community(
        workflow_id, user_id, admin_required=False
    )

    # Get services
    workflow_service = get_workflow_service()
    permission_service = get_permission_service()
    workflow_engine = get_workflow_engine()

    # Check permission to execute
    has_permission = await permission_service.check_permission(
        workflow_id=workflow_id,
        user_id=user_id,
        permission_type="can_execute",
        community_id=community_id
    )

    if not has_permission:
        logger.warning(
            "Permission denied for workflow execution",
            extra={
                "event_type": "AUTHZ",
                "workflow_id": workflow_id,
                "user": str(user_id),
                "community": str(community_id),
                "action": "execute_workflow",
                "result": "DENIED"
            }
        )
        raise PermissionError("You do not have permission to execute this workflow")

    # Load workflow definition
    workflow = await workflow_service.get_workflow(
        workflow_id=workflow_id,
        user_id=user_id,
        community_id=community_id
    )

    if not workflow:
        raise ValueError(f"Workflow not found: {workflow_id}")

    # Build execution context from request
    variables = data.get('variables', {})
    metadata = data.get('metadata', {})

    trigger_data = {
        'workflow_id': workflow_id,
        'community_id': community_id,
        'user_id': user_id,
        'entity_id': workflow.get('entity_id', ''),
        'session_id': metadata.get('session_id'),
        'username': user_info.get('username'),
        'platform': metadata.get('trigger_source', 'api'),
        **variables
    }

    # Execute workflow
    try:
        result = await workflow_engine.execute_workflow(
            workflow_id=workflow_id,
            trigger_data=trigger_data
        )

        logger.info(
            "Workflow execution started",
            extra={
                "event_type": "AUDIT",
                "workflow_id": workflow_id,
                "execution_id": result.execution_id,
                "user": str(user_id),
                "community": str(community_id),
                "action": "execute_workflow",
                "result": "SUCCESS"
            }
        )

        return success_response(
            data={
                "execution_id": result.execution_id,
                "workflow_id": result.workflow_id,
                "status": result.status.value,
                "start_time": result.start_time.isoformat(),
                "execution_path": result.execution_path
            },
            status_code=202,
            message="Workflow execution started"
        )

    except Exception as e:
        logger.error(
            f"Failed to execute workflow: {describe_db_error(e)}",
            extra={
                "event_type": "ERROR",
                "workflow_id": workflow_id,
                "user": str(user_id),
                "community": str(community_id),
                "action": "execute_workflow",
                "result": "FAILURE"
            }
        )
        raise


# ============================================================================
# GET /api/v1/workflows/executions/:execId - Get execution details
# ============================================================================

@execution_api.route('/executions/<execution_id>', methods=['GET'])
@async_endpoint
@tenant_middleware
@auth_required
@handle_execution_errors
async def get_execution_details(execution_id: str):
    """
    Get execution details with full state information.

    Path Parameters:
        execution_id: Execution UUID

    Query Parameters:
        include_logs: Include node execution logs (true/false)
        include_metrics: Include performance metrics (true/false)

    Returns:
        200: Execution details
        401: Unauthorized
        404: Execution not found
        500: Server error

    Response:
    {
        "success": true,
        "data": {
            "execution_id": "exec_uuid",
            "workflow_id": "workflow_uuid",
            "status": "running|completed|failed|cancelled",
            "start_time": "2025-12-09T12:00:00Z",
            "end_time": "2025-12-09T12:05:00Z",
            "execution_time_seconds": 300,
            "execution_path": ["node1", "node2", "node3"],
            "node_states": {
                "node1": {
                    "node_id": "node1",
                    "status": "completed",
                    "started_at": "2025-12-09T12:00:00Z",
                    "completed_at": "2025-12-09T12:00:10Z",
                    "input_data": {},
                    "output_data": {},
                    "logs": [],
                    "error": null
                },
                ...
            },
            "final_variables": {},
            "final_output": {},
            "error_message": null,
            "error_node_id": null
        }
    }
    """
    user_info = request.current_user
    user_id = int(user_info['user_id'])

    # SECURITY (A01): this route had NO permission check at all beyond
    # `auth_required` -- any authenticated user (any tenant/community)
    # could view any execution's full state (including node input/output
    # data) by UUID. Requires membership of the execution's owning
    # workflow's REAL community.
    await _authorize_execution_community(execution_id, user_id, admin_required=False)

    # Get query parameters
    include_logs = request.args.get('include_logs', 'true').lower() == 'true'
    include_metrics = request.args.get('include_metrics', 'false').lower() == 'true'

    # Get workflow engine
    workflow_engine = get_workflow_engine()

    # Get execution status
    execution_summary = await workflow_engine.get_execution_status(execution_id)

    if not execution_summary:
        logger.warning(
            "Execution not found",
            extra={
                "event_type": "ERROR",
                "execution_id": execution_id,
                "user": str(user_id),
                "action": "get_execution_details",
                "result": "NOT_FOUND"
            }
        )
        raise ValueError(f"Execution not found: {execution_id}")

    # Build response data
    response_data = execution_summary.copy()

    # Add metrics if requested
    if include_metrics:
        metrics = await workflow_engine.get_execution_metrics(execution_id)
        if metrics:
            response_data['metrics'] = metrics.to_dict()

    logger.info(
        "Retrieved execution details",
        extra={
            "event_type": "AUDIT",
            "execution_id": execution_id,
            "user": str(user_id),
            "action": "get_execution_details",
            "result": "SUCCESS"
        }
    )

    return success_response(data=response_data)


# ============================================================================
# POST /api/v1/workflows/executions/:execId/cancel - Cancel running execution
# ============================================================================

@execution_api.route('/executions/<execution_id>/cancel', methods=['POST'])
@async_endpoint
@tenant_middleware
@auth_required
@handle_execution_errors
async def cancel_execution(execution_id: str):
    """
    Cancel a running workflow execution.

    Path Parameters:
        execution_id: Execution UUID

    Returns:
        200: Execution cancelled
        401: Unauthorized
        404: Execution not found
        400: Execution cannot be cancelled (not running)
        500: Server error

    Response:
    {
        "success": true,
        "data": {
            "execution_id": "exec_uuid",
            "status": "cancelled",
            "cancelled_at": "2025-12-09T12:00:00Z"
        }
    }
    """
    user_info = request.current_user
    user_id = int(user_info['user_id'])

    # SECURITY (A01): this route had NO permission check at all beyond
    # `auth_required` -- any authenticated user (any tenant/community)
    # could cancel any running execution by UUID. Cancelling is mutating,
    # so requires community-admin of the execution's owning workflow's REAL
    # community.
    await _authorize_execution_community(execution_id, user_id, admin_required=True)

    workflow_engine = get_workflow_engine()

    # Cancel execution
    success = await workflow_engine.cancel_execution(execution_id)

    if not success:
        logger.warning(
            "Failed to cancel execution",
            extra={
                "event_type": "ERROR",
                "execution_id": execution_id,
                "user": str(user_id),
                "action": "cancel_execution",
                "result": "NOT_FOUND"
            }
        )
        raise ValueError(f"Execution not found or cannot be cancelled: {execution_id}")

    logger.info(
        "Execution cancelled",
        extra={
            "event_type": "AUDIT",
            "execution_id": execution_id,
            "user": str(user_id),
            "action": "cancel_execution",
            "result": "SUCCESS"
        }
    )

    return success_response(
        data={
            "execution_id": execution_id,
            "status": ExecutionStatus.CANCELLED.value,
            "cancelled_at": datetime.utcnow().isoformat()
        },
        message="Execution cancelled successfully"
    )


# ============================================================================
# GET /api/v1/workflows/:id/executions - List executions for workflow
# ============================================================================

@execution_api.route('/<workflow_id>/executions', methods=['GET'])
@async_endpoint
@tenant_middleware
@auth_required
@handle_execution_errors
async def list_workflow_executions(workflow_id: str):
    """
    List executions for a workflow with pagination and filters.

    Path Parameters:
        workflow_id: Workflow UUID

    Query Parameters:
        community_id: Community ID (required)
        status: Filter by status (running, completed, failed, cancelled)
        page: Page number (default: 1)
        per_page: Items per page (default: 20, max: 100)
        sort_by: Sort field (start_time, status, execution_time) (default: start_time)
        sort_order: Sort order (asc, desc) (default: desc)

    Returns:
        200: Paginated list of executions
        400: Invalid parameters
        401: Unauthorized
        403: Permission denied
        404: Workflow not found
        500: Server error

    Response:
    {
        "success": true,
        "data": [
            {
                "execution_id": "exec_uuid",
                "workflow_id": "workflow_uuid",
                "status": "completed",
                "start_time": "2025-12-09T12:00:00Z",
                "end_time": "2025-12-09T12:05:00Z",
                "execution_time_seconds": 300,
                "nodes_executed": 5,
                "success": true,
                "error": null
            },
            ...
        ],
        "meta": {
            "pagination": {
                "page": 1,
                "per_page": 20,
                "total": 150,
                "total_pages": 8,
                "has_next": true,
                "has_prev": false
            }
        }
    }
    """
    user_info = request.current_user
    user_id = int(user_info['user_id'])

    # SECURITY (A01, BOLA/IDOR -- deferred from gh security PR #260): the
    # `community_id` query param was entirely client-supplied and passed
    # straight into `check_permission` as advisory context. Resolves the
    # workflow's REAL `community_id` from the database instead and requires
    # at least active membership of it; `check_permission`'s fine-grained
    # `can_view` check below still gates the actual list action.
    community_id = await _authorize_workflow_community(
        workflow_id, user_id, admin_required=False
    )

    # Optional filters
    status_filter = request.args.get('status')
    sort_by = request.args.get('sort_by', 'start_time')
    sort_order = request.args.get('sort_order', 'desc')

    # Validate sort parameters
    valid_sort_fields = {'start_time', 'status', 'execution_time'}
    if sort_by not in valid_sort_fields:
        raise ValueError(f"Invalid sort_by: {sort_by}")

    if sort_order not in {'asc', 'desc'}:
        raise ValueError(f"Invalid sort_order: {sort_order}")

    # Pagination
    try:
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 20))
    except ValueError:
        raise ValueError("Invalid pagination parameters")

    if page < 1:
        raise ValueError("Page must be >= 1")

    if per_page < 1 or per_page > 100:
        raise ValueError("Per page must be between 1 and 100")

    # Check permission to view
    permission_service = get_permission_service()
    has_permission = await permission_service.check_permission(
        workflow_id=workflow_id,
        user_id=user_id,
        permission_type="can_view",
        community_id=community_id
    )

    if not has_permission:
        logger.warning(
            "Permission denied to view workflow executions",
            extra={
                "event_type": "AUTHZ",
                "workflow_id": workflow_id,
                "user": str(user_id),
                "community": str(community_id),
                "action": "list_workflow_executions",
                "result": "DENIED"
            }
        )
        raise PermissionError("You do not have permission to view this workflow")

    # Get DAL
    dal = get_dal()

    # Build query
    query = """
        SELECT
            execution_id,
            workflow_id,
            status,
            start_time,
            end_time,
            execution_time_seconds,
            nodes_executed,
            error_message,
            final_output
        FROM workflow_executions
        WHERE workflow_id = %s
    """
    params = [workflow_id]

    # Apply status filter if provided
    if status_filter:
        query += " AND status = %s"
        params.append(status_filter)

    # Apply sorting
    query += f" ORDER BY {sort_by} {sort_order.upper()}"

    # Get total count
    count_query = f"SELECT COUNT(*) FROM workflow_executions WHERE workflow_id = %s"
    count_params = [workflow_id]
    if status_filter:
        count_query += " AND status = %s"
        count_params.append(status_filter)

    total = 0
    try:
        count_result = dal.executesql(count_query, count_params)
        if count_result:
            total = count_result[0][0]
    except Exception as e:
        logger.error(f"Error counting executions: {describe_db_error(e)}")
        total = 0

    # Calculate pagination
    total_pages = (total + per_page - 1) // per_page
    offset = (page - 1) * per_page

    # Apply pagination
    query += f" LIMIT %s OFFSET %s"
    params.extend([per_page, offset])

    # Execute query
    executions = []
    try:
        results = dal.executesql(query, params)
        if results:
            for row in results:
                executions.append({
                    "execution_id": str(row[0]),
                    "workflow_id": str(row[1]),
                    "status": row[2],
                    "start_time": row[3].isoformat() if row[3] else None,
                    "end_time": row[4].isoformat() if row[4] else None,
                    "execution_time_seconds": float(row[5]) if row[5] else 0,
                    "nodes_executed": row[6],
                    "error": row[7],
                    "success": row[2] == 'completed'
                })
    except Exception as e:
        logger.error(f"Error querying executions: {describe_db_error(e)}")

    logger.info(
        "Listed workflow executions",
        extra={
            "event_type": "AUDIT",
            "workflow_id": workflow_id,
            "user": str(user_id),
            "community": str(community_id),
            "action": "list_workflow_executions",
            "result": "SUCCESS",
            "count": len(executions)
        }
    )

    return success_response(
        data=executions,
        meta={
            "pagination": {
                "page": page,
                "per_page": per_page,
                "total": total,
                "total_pages": total_pages,
                "has_next": page < total_pages,
                "has_prev": page > 1
            }
        }
    )


# ============================================================================
# POST /api/v1/workflows/:id/test - Dry-run test with detailed trace
# ============================================================================

@execution_api.route('/<workflow_id>/test', methods=['POST'])
@async_endpoint
@tenant_middleware
@auth_required
@handle_execution_errors
async def test_workflow(workflow_id: str):
    """
    Perform a dry-run test of workflow execution without side effects.

    This endpoint executes the workflow in test mode, capturing detailed
    trace information and execution logs but preventing any real side effects
    (API calls, data modifications, etc.).

    Path Parameters:
        workflow_id: Workflow UUID

    Request Body:
    {
        "community_id": 123,
        "variables": {
            "param1": "value1"
        },
        "metadata": {
            "session_id": "test_sess_123"
        }
    }

    Returns:
        200: Test completed
        400: Invalid input or workflow not found
        401: Unauthorized
        403: Permission denied
        504: Test timeout
        500: Server error

    Response:
    {
        "success": true,
        "data": {
            "execution_id": "test_exec_uuid",
            "workflow_id": "workflow_uuid",
            "test_mode": true,
            "status": "completed",
            "execution_time_seconds": 1.5,
            "trace": [
                {
                    "node_id": "node1",
                    "status": "completed",
                    "duration_seconds": 0.5,
                    "input": {},
                    "output": {},
                    "logs": ["Node execution log 1"],
                    "error": null
                },
                ...
            ],
            "final_variables": {},
            "summary": {
                "nodes_executed": 3,
                "nodes_failed": 0,
                "total_duration": 1.5,
                "passed": true
            }
        }
    }
    """
    user_info = request.current_user
    user_id = int(user_info['user_id'])

    data = await request.get_json() or {}

    # SECURITY (A01, BOLA/IDOR -- deferred from gh security PR #260): see
    # `execute_workflow`'s identical fix above -- resolves the workflow's
    # REAL `community_id` rather than trusting the client-supplied body
    # field.
    community_id = await _authorize_workflow_community(
        workflow_id, user_id, admin_required=False
    )

    # Get services
    workflow_service = get_workflow_service()
    permission_service = get_permission_service()
    workflow_engine = get_workflow_engine()

    # Check permission to execute (test is treated like execute)
    has_permission = await permission_service.check_permission(
        workflow_id=workflow_id,
        user_id=user_id,
        permission_type="can_execute",
        community_id=community_id
    )

    if not has_permission:
        logger.warning(
            "Permission denied for workflow test",
            extra={
                "event_type": "AUTHZ",
                "workflow_id": workflow_id,
                "user": str(user_id),
                "community": str(community_id),
                "action": "test_workflow",
                "result": "DENIED"
            }
        )
        raise PermissionError("You do not have permission to test this workflow")

    # Load workflow definition
    workflow = await workflow_service.get_workflow(
        workflow_id=workflow_id,
        user_id=user_id,
        community_id=community_id
    )

    if not workflow:
        raise ValueError(f"Workflow not found: {workflow_id}")

    # Build execution context from request
    variables = data.get('variables', {})
    metadata = data.get('metadata', {})

    trigger_data = {
        'workflow_id': workflow_id,
        'community_id': community_id,
        'user_id': user_id,
        'entity_id': workflow.get('entity_id', ''),
        'session_id': metadata.get('session_id'),
        'username': user_info.get('username'),
        'platform': 'api_test',
        **variables
    }

    # Execute workflow in test mode
    try:
        result = await workflow_engine.execute_workflow(
            workflow_id=workflow_id,
            trigger_data=trigger_data
        )

        # Build detailed trace from execution result
        trace = []
        for node_id in result.execution_path:
            node_state = result.get_node_state(node_id)
            if node_state:
                trace.append({
                    "node_id": node_id,
                    "status": node_state.status.value,
                    "duration_seconds": node_state.get_execution_time_seconds(),
                    "input": {
                        port: pd.data
                        for port, pd in node_state.input_data.items()
                    },
                    "output": {
                        port: pd.data
                        for port, pd in node_state.output_data.items()
                    },
                    "logs": node_state.logs,
                    "error": node_state.error,
                    "error_type": node_state.error_type
                })

        # Build summary
        summary = {
            "nodes_executed": len(result.execution_path),
            "nodes_failed": len(result.get_failed_nodes()),
            "total_duration": result.execution_time_seconds,
            "passed": result.is_successful
        }

        logger.info(
            "Workflow test completed",
            extra={
                "event_type": "AUDIT",
                "workflow_id": workflow_id,
                "execution_id": result.execution_id,
                "user": str(user_id),
                "community": str(community_id),
                "action": "test_workflow",
                "result": "SUCCESS",
                "test_passed": result.is_successful
            }
        )

        return success_response(
            data={
                "execution_id": result.execution_id,
                "workflow_id": result.workflow_id,
                "test_mode": True,
                "status": result.status.value,
                "execution_time_seconds": result.execution_time_seconds,
                "trace": trace,
                "final_variables": result.final_variables,
                "summary": summary
            },
            message="Workflow test completed"
        )

    except Exception as e:
        logger.error(
            f"Workflow test failed: {describe_db_error(e)}",
            extra={
                "event_type": "ERROR",
                "workflow_id": workflow_id,
                "user": str(user_id),
                "community": str(community_id),
                "action": "test_workflow",
                "result": "FAILURE"
            }
        )
        raise


# ============================================================================
# Error Handlers
# ============================================================================

@execution_api.errorhandler(400)
async def bad_request(error):
    """Handle 400 Bad Request errors."""
    return error_response(
        message=str(error),
        status_code=400,
        error_code="BAD_REQUEST"
    )


@execution_api.errorhandler(401)
async def unauthorized(error):
    """Handle 401 Unauthorized errors."""
    return error_response(
        message="Authentication required",
        status_code=401,
        error_code="UNAUTHORIZED"
    )


@execution_api.errorhandler(403)
async def forbidden(error):
    """Handle 403 Forbidden errors."""
    return error_response(
        message="Permission denied",
        status_code=403,
        error_code="FORBIDDEN"
    )


@execution_api.errorhandler(404)
async def not_found(error):
    """Handle 404 Not Found errors."""
    return error_response(
        message="Resource not found",
        status_code=404,
        error_code="NOT_FOUND"
    )


@execution_api.errorhandler(500)
async def internal_error(error):
    """Handle 500 Internal Server Error.

    SECURITY (PII in logs): logs an ``error_id`` + exception type only -- never the
    exception text/traceback, which can embed bound DB values.
    """
    error_id = log_internal_error(logger, error)
    return error_response(
        message="Internal server error",
        status_code=500,
        error_code="INTERNAL_ERROR",
        details={"error_id": error_id}
    )


# ============================================================================
# Blueprint registration helper
# ============================================================================

def register_execution_api(app, workflow_engine: WorkflowEngine):
    """
    Register execution API blueprint with app.

    Args:
        app: Quart application instance
        workflow_engine: Configured WorkflowEngine instance
    """
    # Store workflow engine in app config
    app.config['workflow_engine'] = workflow_engine

    # Register blueprint
    app.register_blueprint(execution_api)

    logger.info(
        "Execution API registered",
        extra={
            "event_type": "SYSTEM",
            "action": "register_execution_api",
            "result": "SUCCESS"
        }
    )
