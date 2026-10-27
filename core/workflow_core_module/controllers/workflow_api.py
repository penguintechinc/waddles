"""
Workflow API Controller
=======================

REST API for workflow management with:
- CRUD operations (create, read, update, delete, list)
- Publish and validate endpoints
- Authentication and authorization middleware
- Error handling with proper HTTP status codes
- Pagination support
- Comprehensive logging

Endpoints:
- POST   /api/v1/workflows           - Create workflow
- GET    /api/v1/workflows           - List workflows
- GET    /api/v1/workflows/:id       - Get workflow
- PUT    /api/v1/workflows/:id       - Update workflow
- DELETE /api/v1/workflows/:id       - Delete (archive) workflow
- POST   /api/v1/workflows/:id/publish  - Publish workflow
- POST   /api/v1/workflows/:id/validate - Validate workflow
"""

import logging
from quart import Blueprint, request, current_app
from functools import wraps
from typing import Callable

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
from services.workflow_service import (
    WorkflowService,
    WorkflowServiceException,
    WorkflowNotFoundException,
    WorkflowPermissionException,
)
from services.license_service import LicenseValidationException
from services.community_scope import (
    WorkflowCommunityNotFoundError,
    resolve_workflow_community_id,
)
from controllers.error_logging import log_internal_error


logger = logging.getLogger(__name__)


# Create blueprint
workflow_api = Blueprint('workflow_api', __name__, url_prefix='/api/v1/workflows')


def get_workflow_service() -> WorkflowService:
    """Get workflow service from app context."""
    return current_app.config.get('workflow_service')


async def _authorize_community(
    workflow_id: str, user_id: int, *, admin_required: bool
) -> int:
    """Resolve the workflow's REAL `community_id` and require caller membership/admin of it.

    SECURITY (A01, BOLA/IDOR -- deferred from gh security PR #260): never
    trusts a client-supplied `community_id` for authorization -- resolves
    the workflow's own DB row instead (see `services/community_scope.py`).
    Returns the resolved `community_id` so callers can pass the
    authoritative value into `WorkflowService` instead of whatever (if
    anything) the client supplied.

    Raises:
        WorkflowCommunityNotFoundError: `workflow_id` doesn't exist (404).
        CallerIdentityError: bearer token missing a usable subject (401).
        CommunityAccessError: caller isn't a member/admin of the resolved
            community (403).
    """
    # `workflow_service.dal` (Postgres-only raw SQL, `workflows` table) and
    # `current_app.config['dal']` (pydal query objects, `communities`/
    # `community_members`/`tenants` tables) are the SAME underlying pydal
    # `DAL` in production (see app.py's `dal`/`async_dal` split) -- fetched
    # separately here (rather than assumed identical) so this stays correct
    # even where they legitimately differ (e.g. tests). `require_community_
    # admin`/`require_community_member` separately need something with
    # `.select_async()`, which is `app.config['async_dal']` (the `AsyncDAL`
    # wrapper over the same DB).
    workflow_dal = get_workflow_service().dal
    shared_dal = current_app.config.get('dal')
    async_dal = current_app.config.get('async_dal')
    community_id = await resolve_workflow_community_id(workflow_dal, workflow_id)
    ctx = get_tenant_context(request)
    check = require_community_admin if admin_required else require_community_member
    await check(async_dal, shared_dal, request, ctx, community_id=community_id, user_id=user_id)
    return community_id


def handle_workflow_errors(f: Callable) -> Callable:
    """
    Decorator to handle workflow-specific exceptions.

    Converts workflow exceptions to appropriate HTTP responses.
    """
    @wraps(f)
    async def decorated_function(*args, **kwargs):
        try:
            return await f(*args, **kwargs)
        except LicenseValidationException as e:
            # HTTP 402 Payment Required for license failures
            logger.warning(
                f"License validation failed: {e.message}",
                extra={
                    "event_type": "AUTHZ",
                    "action": f.__name__,
                    "community": str(e.community_id),
                    "result": "PAYMENT_REQUIRED"
                }
            )
            return error_response(
                message=e.message,
                status_code=402,
                error_code="PAYMENT_REQUIRED",
                details={"community_id": e.community_id}
            )
        except WorkflowNotFoundException as e:
            return error_response(
                message=e.message,
                status_code=404,
                error_code="WORKFLOW_NOT_FOUND"
            )
        except WorkflowCommunityNotFoundError as e:
            return error_response(
                message=str(e),
                status_code=404,
                error_code="WORKFLOW_NOT_FOUND"
            )
        except CommunityAccessError as e:
            # SECURITY (A01): caller isn't a member/admin of the workflow's
            # REAL community -- see `_authorize_community` above.
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
        except WorkflowPermissionException as e:
            return error_response(
                message=e.message,
                status_code=403,
                error_code="PERMISSION_DENIED"
            )
        except WorkflowServiceException as e:
            return error_response(
                message=e.message,
                status_code=e.status_code,
                error_code="WORKFLOW_ERROR"
            )
        except ValueError as e:
            # Client errors (bad input)
            return error_response(
                message=str(e),
                status_code=400,
                error_code="BAD_REQUEST"
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
# POST /api/v1/workflows - Create workflow
# ============================================================================

@workflow_api.route('', methods=['POST'])
@async_endpoint
@tenant_middleware
@auth_required
@handle_workflow_errors
async def create_workflow():
    """
    Create a new workflow.

    Request Body:
    {
        "name": "Workflow Name",
        "description": "Description",
        "nodes": {},
        "connections": [],
        "trigger_type": "command",
        "trigger_config": {},
        "global_variables": {}
    }

    Returns:
        201: Created workflow
        400: Invalid input
        401: Unauthorized
        402: Payment Required (license validation failed)
        500: Server error
    """
    user_info = request.current_user
    user_id = int(user_info['user_id'])

    data = await request.get_json()

    # Validate required fields
    if not data:
        raise ValueError("Request body is required")

    if 'name' not in data:
        raise ValueError("Field 'name' is required")

    if 'entity_id' not in data:
        raise ValueError("Field 'entity_id' is required")

    if 'community_id' not in data:
        raise ValueError("Field 'community_id' is required")

    entity_id = int(data['entity_id'])
    community_id = int(data['community_id'])

    # SECURITY (A01, BOLA/IDOR -- deferred from gh security PR #260): the
    # target `community_id` is entirely client-supplied for a create --
    # unlike the other routes below there is no existing workflow row to
    # resolve it from, so it must be verified directly. Previously NOTHING
    # checked the caller was even a member of this community before this
    # fix; a valid JWT from any tenant/community was sufficient to create a
    # workflow under an arbitrary `community_id`.
    ctx = get_tenant_context(request)
    await require_community_admin(
        current_app.config.get('async_dal'),
        current_app.config.get('dal'),
        request,
        ctx,
        community_id=community_id,
        user_id=user_id,
    )

    # Get license key if provided
    license_key = data.get('license_key')

    # Create workflow
    workflow_service = get_workflow_service()
    workflow = await workflow_service.create_workflow(
        workflow_data=data,
        community_id=community_id,
        entity_id=entity_id,
        user_id=user_id,
        license_key=license_key
    )

    return success_response(
        data=workflow,
        status_code=201,
        message="Workflow created successfully"
    )


# ============================================================================
# GET /api/v1/workflows - List workflows
# ============================================================================

@workflow_api.route('', methods=['GET'])
@async_endpoint
@tenant_middleware
@auth_required
@handle_workflow_errors
async def list_workflows():
    """
    List accessible workflows with pagination and filters.

    Query Parameters:
        entity_id (required): Entity ID
        community_id: Community ID
        status: Filter by status (draft, active, archived)
        search: Search in name/description
        page: Page number (default: 1)
        per_page: Items per page (default: 20)

    Returns:
        200: Paginated list of workflows
        400: Invalid parameters
        401: Unauthorized
        500: Server error
    """
    user_info = request.current_user
    user_id = int(user_info['user_id'])

    # Get query parameters
    entity_id = request.args.get('entity_id')
    if not entity_id:
        raise ValueError("Query parameter 'entity_id' is required")

    entity_id = int(entity_id)

    # Optional filters
    community_id = request.args.get('community_id')
    community_id = int(community_id) if community_id else None

    # SECURITY (A01): if a `community_id` filter is supplied, the caller
    # must actually be a member of it -- otherwise it's a free probe for
    # "does this workflow-bearing community exist / how many workflows does
    # it have" against communities the caller has no relationship to. No
    # filter at all is unaffected (the underlying entity/permission-scoped
    # query in `WorkflowService.list_workflows` already bounds the result
    # to what the caller can see).
    if community_id is not None:
        ctx = get_tenant_context(request)
        await require_community_member(
            current_app.config.get('async_dal'),
            current_app.config.get('dal'),
            request,
            ctx,
            community_id=community_id,
            user_id=user_id,
        )

    status = request.args.get('status')
    search = request.args.get('search')

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

    # Build filters
    filters = {}
    if status:
        filters['status'] = status
    if search:
        filters['search'] = search

    # List workflows
    workflow_service = get_workflow_service()
    result = await workflow_service.list_workflows(
        entity_id=entity_id,
        user_id=user_id,
        filters=filters,
        page=page,
        per_page=per_page,
        community_id=community_id
    )

    # Return with pagination metadata
    return success_response(
        data=result['workflows'],
        meta={
            "pagination": {
                "page": result['page'],
                "per_page": result['per_page'],
                "total": result['total'],
                "total_pages": result['total_pages'],
                "has_next": result['page'] < result['total_pages'],
                "has_prev": result['page'] > 1,
            }
        }
    )


# ============================================================================
# GET /api/v1/workflows/:id - Get workflow
# ============================================================================

@workflow_api.route('/<workflow_id>', methods=['GET'])
@async_endpoint
@tenant_middleware
@auth_required
@handle_workflow_errors
async def get_workflow(workflow_id: str):
    """
    Get workflow by ID.

    Path Parameters:
        workflow_id: Workflow UUID

    Query Parameters:
        community_id: Optional community ID for context

    Returns:
        200: Workflow data
        401: Unauthorized
        403: Permission denied
        404: Workflow not found
        500: Server error
    """
    user_info = request.current_user
    user_id = int(user_info['user_id'])

    # SECURITY (A01): resolves the workflow's REAL community_id and
    # requires membership of it -- any client-supplied `community_id` query
    # param is ignored for authorization (see `_authorize_community`).
    community_id = await _authorize_community(workflow_id, user_id, admin_required=False)

    workflow_service = get_workflow_service()
    workflow = await workflow_service.get_workflow(
        workflow_id=workflow_id,
        user_id=user_id,
        community_id=community_id
    )

    return success_response(data=workflow)


# ============================================================================
# PUT /api/v1/workflows/:id - Update workflow
# ============================================================================

@workflow_api.route('/<workflow_id>', methods=['PUT'])
@async_endpoint
@tenant_middleware
@auth_required
@handle_workflow_errors
async def update_workflow(workflow_id: str):
    """
    Update workflow.

    Path Parameters:
        workflow_id: Workflow UUID

    Request Body:
    {
        "name": "Updated Name",
        "description": "Updated Description",
        "nodes": {},
        "connections": [],
        ... other updateable fields
    }

    Returns:
        200: Updated workflow
        400: Invalid input
        401: Unauthorized
        403: Permission denied
        404: Workflow not found
        500: Server error
    """
    user_info = request.current_user
    user_id = int(user_info['user_id'])

    data = await request.get_json()

    if not data:
        raise ValueError("Request body is required")

    # SECURITY (A01): resolves the workflow's REAL community_id and
    # requires community-admin of it -- any client-supplied `community_id`
    # in the body is ignored for authorization (see `_authorize_community`).
    community_id = await _authorize_community(workflow_id, user_id, admin_required=True)

    workflow_service = get_workflow_service()
    workflow = await workflow_service.update_workflow(
        workflow_id=workflow_id,
        updates=data,
        user_id=user_id,
        community_id=community_id
    )

    return success_response(
        data=workflow,
        message="Workflow updated successfully"
    )


# ============================================================================
# DELETE /api/v1/workflows/:id - Delete (archive) workflow
# ============================================================================

@workflow_api.route('/<workflow_id>', methods=['DELETE'])
@async_endpoint
@tenant_middleware
@auth_required
@handle_workflow_errors
async def delete_workflow(workflow_id: str):
    """
    Delete (archive) workflow.

    Path Parameters:
        workflow_id: Workflow UUID

    Query Parameters:
        community_id: Optional community ID for context

    Returns:
        200: Workflow archived
        401: Unauthorized
        403: Permission denied
        404: Workflow not found
        500: Server error
    """
    user_info = request.current_user
    user_id = int(user_info['user_id'])

    # SECURITY (A01): resolves the workflow's REAL community_id and
    # requires community-admin of it -- see `_authorize_community`.
    community_id = await _authorize_community(workflow_id, user_id, admin_required=True)

    workflow_service = get_workflow_service()
    result = await workflow_service.delete_workflow(
        workflow_id=workflow_id,
        user_id=user_id,
        community_id=community_id
    )

    return success_response(
        data=result,
        message="Workflow archived successfully"
    )


# ============================================================================
# POST /api/v1/workflows/:id/publish - Publish workflow
# ============================================================================

@workflow_api.route('/<workflow_id>/publish', methods=['POST'])
@async_endpoint
@tenant_middleware
@auth_required
@handle_workflow_errors
async def publish_workflow(workflow_id: str):
    """
    Publish and activate workflow after validation.

    Path Parameters:
        workflow_id: Workflow UUID

    Request Body (optional):
    {
        "community_id": 123
    }

    Returns:
        200: Workflow published
        400: Validation failed
        401: Unauthorized
        403: Permission denied
        404: Workflow not found
        500: Server error
    """
    user_info = request.current_user
    user_id = int(user_info['user_id'])

    await request.get_json() or {}

    # SECURITY (A01): resolves the workflow's REAL community_id and
    # requires community-admin of it -- any client-supplied `community_id`
    # in the body is ignored for authorization (see `_authorize_community`).
    community_id = await _authorize_community(workflow_id, user_id, admin_required=True)

    workflow_service = get_workflow_service()
    workflow = await workflow_service.publish_workflow(
        workflow_id=workflow_id,
        user_id=user_id,
        community_id=community_id
    )

    return success_response(
        data=workflow,
        message="Workflow published successfully"
    )


# ============================================================================
# POST /api/v1/workflows/:id/validate - Validate workflow
# ============================================================================

@workflow_api.route('/<workflow_id>/validate', methods=['POST'])
@async_endpoint
@tenant_middleware
@auth_required
@handle_workflow_errors
async def validate_workflow(workflow_id: str):
    """
    Validate workflow structure and configuration.

    Path Parameters:
        workflow_id: Workflow UUID

    Returns:
        200: Validation result
        401: Unauthorized
        404: Workflow not found
        500: Server error

    Response:
    {
        "success": true,
        "data": {
            "is_valid": true,
            "errors": [],
            "warnings": [],
            "node_validation_errors": {},
            "error_count": 0,
            "warning_count": 0
        }
    }
    """
    user_info = request.current_user
    user_id = int(user_info['user_id'])

    # SECURITY (A01): this route previously had NO permission check at
    # all beyond `auth_required` -- any authenticated user (any tenant,
    # any community) could validate any workflow by UUID and see its
    # structure/errors. Requires membership of the workflow's REAL
    # community (see `_authorize_community`).
    await _authorize_community(workflow_id, user_id, admin_required=False)

    workflow_service = get_workflow_service()
    validation_result = await workflow_service.validate_workflow(
        workflow_id=workflow_id
    )

    return success_response(
        data=validation_result,
        message="Validation completed"
    )


# ============================================================================
# Error Handlers
# ============================================================================

@workflow_api.errorhandler(400)
async def bad_request(error):
    """Handle 400 Bad Request errors."""
    return error_response(
        message=str(error),
        status_code=400,
        error_code="BAD_REQUEST"
    )


@workflow_api.errorhandler(401)
async def unauthorized(error):
    """Handle 401 Unauthorized errors."""
    return error_response(
        message="Authentication required",
        status_code=401,
        error_code="UNAUTHORIZED"
    )


@workflow_api.errorhandler(403)
async def forbidden(error):
    """Handle 403 Forbidden errors."""
    return error_response(
        message="Permission denied",
        status_code=403,
        error_code="FORBIDDEN"
    )


@workflow_api.errorhandler(404)
async def not_found(error):
    """Handle 404 Not Found errors."""
    return error_response(
        message="Resource not found",
        status_code=404,
        error_code="NOT_FOUND"
    )


@workflow_api.errorhandler(500)
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

def register_workflow_api(app, workflow_service: WorkflowService):
    """
    Register workflow API blueprint with app.

    Args:
        app: Quart application instance
        workflow_service: Configured WorkflowService instance
    """
    # Store workflow service in app config
    app.config['workflow_service'] = workflow_service

    # Register blueprint
    app.register_blueprint(workflow_api)

    logger.info(
        "Workflow API registered",
        extra={
            "event_type": "SYSTEM",
            "action": "register_workflow_api",
            "result": "SUCCESS"
        }
    )
