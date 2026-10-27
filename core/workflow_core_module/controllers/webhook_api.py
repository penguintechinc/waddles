"""
Webhook API Controller
======================

REST API for webhook-triggered workflows with:
- Public webhook trigger endpoint (HMAC signature verification)
- Webhook management (CRUD operations)
- IP allowlist checking
- Rate limiting per webhook
- Secure token generation
- Comprehensive AAA logging

Endpoints:
- POST   /api/v1/workflows/webhooks/:token   - Public webhook trigger (no auth)
- GET    /api/v1/workflows/:id/webhooks      - List webhooks for workflow
- POST   /api/v1/workflows/:id/webhooks      - Create webhook
- DELETE /api/v1/workflows/:id/webhooks/:webhookId - Delete webhook

Security Features:
- HMAC-SHA256 signature verification
- IP allowlist validation
- Rate limiting (60 req/min per webhook)
- Secure random token generation
- Permission checks (can_edit for management endpoints)
"""

import logging
import hmac
import hashlib
import secrets
import ipaddress
from datetime import datetime, timedelta
from functools import wraps
from typing import Callable, Dict, Any, Optional, List
from quart import Blueprint, request, current_app
from dataclasses import dataclass, asdict

from flask_core import (
    success_response,
    error_response,
    async_endpoint,
    auth_required,
    describe_db_error,
)
from services.workflow_service import (
    WorkflowService,
    WorkflowServiceException,
    WorkflowNotFoundException,
    WorkflowPermissionException,
)
from services.permission_service import PermissionService
from services.workflow_engine import WorkflowEngine
from controllers.error_logging import log_internal_error


logger = logging.getLogger(__name__)


# ============================================================================
# Data Models
# ============================================================================

@dataclass(slots=True)
class WebhookConfig:
    """Webhook configuration and metadata"""
    webhook_id: str
    workflow_id: str
    token: str
    secret: str
    name: str
    description: Optional[str] = None
    url: str = ""
    enabled: bool = True
    require_signature: bool = True
    ip_allowlist: List[str] = None
    rate_limit_max: int = 60  # requests per minute
    rate_limit_window: int = 60  # seconds
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    last_triggered_at: Optional[datetime] = None
    trigger_count: int = 0

    def __post_init__(self):
        """Initialize default values"""
        if self.ip_allowlist is None:
            self.ip_allowlist = []

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary (excluding sensitive secret)"""
        data = asdict(self)
        # Don't expose secret to clients
        del data['secret']
        return data


@dataclass(slots=True)
class WebhookExecutionResult:
    """Result of webhook-triggered execution"""
    execution_id: str
    workflow_id: str
    webhook_id: str
    status: str
    timestamp: datetime
    trigger_data: Dict[str, Any]
    error_message: Optional[str] = None


# ============================================================================
# Rate Limiter for Webhooks
# ============================================================================

class WebhookRateLimiter:
    """In-memory rate limiter for webhooks (sliding window)"""

    def __init__(self):
        """Initialize rate limiter"""
        self._requests: Dict[str, list] = {}

    def check_rate_limit(
        self,
        webhook_id: str,
        max_requests: int,
        window_seconds: int
    ) -> tuple[bool, int]:
        """
        Check if webhook is within rate limit.

        Args:
            webhook_id: Unique webhook identifier
            max_requests: Maximum requests allowed
            window_seconds: Time window in seconds

        Returns:
            Tuple of (allowed: bool, remaining_requests: int)
        """
        now = datetime.utcnow()
        window_start = now - timedelta(seconds=window_seconds)

        if webhook_id not in self._requests:
            self._requests[webhook_id] = []

        # Remove old requests outside window
        self._requests[webhook_id] = [
            req_time for req_time in self._requests[webhook_id]
            if req_time > window_start
        ]

        current_count = len(self._requests[webhook_id])
        allowed = current_count < max_requests

        if allowed:
            self._requests[webhook_id].append(now)

        remaining = max(0, max_requests - (current_count + (1 if allowed else 0)))
        return allowed, remaining


# Create global rate limiter instance
webhook_rate_limiter = WebhookRateLimiter()


# ============================================================================
# Blueprint Setup
# ============================================================================

webhook_api = Blueprint('webhook_api', __name__, url_prefix='/api/v1')


def get_workflow_service() -> WorkflowService:
    """Get workflow service from app context"""
    return current_app.config.get('workflow_service')


def get_permission_service() -> PermissionService:
    """Get permission service from app context"""
    return current_app.config.get('permission_service')


def get_workflow_engine() -> WorkflowEngine:
    """Get workflow engine from app context"""
    return current_app.config.get('workflow_engine')


def get_dal():
    """Get database access layer from app context"""
    return current_app.config.get('dal')


def handle_webhook_errors(f: Callable) -> Callable:
    """
    Decorator to handle webhook-specific exceptions.

    Converts exceptions to appropriate HTTP responses.
    """
    @wraps(f)
    async def decorated_function(*args, **kwargs):
        try:
            return await f(*args, **kwargs)
        except WorkflowNotFoundException as e:
            return error_response(
                message=e.message,
                status_code=404,
                error_code="WORKFLOW_NOT_FOUND"
            )
        except WorkflowPermissionException as e:
            logger.warning(
                f"Permission denied: {e.message}",
                extra={
                    "event_type": "AUTHZ",
                    "action": f.__name__,
                    "result": "DENIED"
                }
            )
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
# Helper Functions
# ============================================================================

def generate_webhook_token() -> str:
    """
    Generate a secure random webhook token.

    Returns:
        32-character hex string token
    """
    return secrets.token_hex(16)


def generate_webhook_secret() -> str:
    """
    Generate a secure random HMAC secret.

    Returns:
        32-character hex string secret
    """
    return secrets.token_hex(16)


def verify_webhook_signature(
    token: str,
    secret: str,
    body: bytes,
    signature: str
) -> bool:
    """
    Verify HMAC-SHA256 signature of webhook payload.

    Args:
        token: Webhook token
        secret: HMAC secret
        body: Raw request body
        signature: Signature from request header

    Returns:
        True if signature is valid
    """
    # Build message to verify: token + timestamp + body
    # Client should send: X-Webhook-Signature: sha256=<hex>
    message = token.encode() + body

    expected = 'sha256=' + hmac.new(
        secret.encode(),
        message,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(signature, expected)


def check_ip_allowlist(
    client_ip: str,
    allowlist: List[str]
) -> bool:
    """
    Check if client IP is in allowlist.

    Args:
        client_ip: Client IP address
        allowlist: List of allowed IPs/CIDR ranges

    Returns:
        True if IP is allowed (or allowlist is empty)
    """
    if not allowlist:
        return True

    try:
        client_addr = ipaddress.ip_address(client_ip)
        for allow_entry in allowlist:
            try:
                # Try as CIDR network
                network = ipaddress.ip_network(allow_entry, strict=False)
                if client_addr in network:
                    return True
            except ValueError:
                # Try as exact IP
                if str(client_addr) == allow_entry:
                    return True
        return False
    except ValueError:
        logger.warning(f"Invalid IP address: {client_ip}")
        return False


async def get_webhook_from_db(
    dal,
    workflow_id: str,
    webhook_id: str
) -> Optional[WebhookConfig]:
    """
    Get webhook configuration from database.

    Args:
        dal: Database access layer
        workflow_id: Workflow ID
        webhook_id: Webhook ID

    Returns:
        WebhookConfig or None if not found
    """
    try:
        # Query webhook from webhooks table
        webhooks = dal.table('workflow_webhooks')
        webhook_row = await dal.select(
            webhooks,
            lambda row: (
                row.webhook_id == webhook_id and
                row.workflow_id == workflow_id
            )
        )

        if not webhook_row:
            return None

        webhook_data = webhook_row[0]
        return WebhookConfig(
            webhook_id=webhook_data.webhook_id,
            workflow_id=webhook_data.workflow_id,
            token=webhook_data.token,
            secret=webhook_data.secret,
            name=webhook_data.name,
            description=webhook_data.description,
            url=webhook_data.url,
            enabled=webhook_data.enabled,
            require_signature=webhook_data.require_signature,
            ip_allowlist=webhook_data.ip_allowlist or [],
            rate_limit_max=webhook_data.rate_limit_max,
            rate_limit_window=webhook_data.rate_limit_window,
            created_at=webhook_data.created_at,
            updated_at=webhook_data.updated_at,
            last_triggered_at=webhook_data.last_triggered_at,
            trigger_count=webhook_data.trigger_count
        )
    except Exception as e:
        logger.error(f"Error retrieving webhook: {describe_db_error(e)}")
        return None


async def get_webhook_by_token(
    dal,
    token: str
) -> Optional[WebhookConfig]:
    """
    Get webhook configuration by token (for public endpoint).

    Args:
        dal: Database access layer
        token: Webhook token

    Returns:
        WebhookConfig or None if not found
    """
    try:
        webhooks = dal.table('workflow_webhooks')
        webhook_row = await dal.select(
            webhooks,
            lambda row: row.token == token
        )

        if not webhook_row:
            return None

        webhook_data = webhook_row[0]
        return WebhookConfig(
            webhook_id=webhook_data.webhook_id,
            workflow_id=webhook_data.workflow_id,
            token=webhook_data.token,
            secret=webhook_data.secret,
            name=webhook_data.name,
            description=webhook_data.description,
            url=webhook_data.url,
            enabled=webhook_data.enabled,
            require_signature=webhook_data.require_signature,
            ip_allowlist=webhook_data.ip_allowlist or [],
            rate_limit_max=webhook_data.rate_limit_max,
            rate_limit_window=webhook_data.rate_limit_window,
            created_at=webhook_data.created_at,
            updated_at=webhook_data.updated_at,
            last_triggered_at=webhook_data.last_triggered_at,
            trigger_count=webhook_data.trigger_count
        )
    except Exception as e:
        logger.error(f"Error retrieving webhook by token: {describe_db_error(e)}")
        return None


async def update_webhook_trigger_stats(
    dal,
    webhook_id: str,
    execution_id: str
):
    """
    Update webhook trigger count and last triggered timestamp.

    Args:
        dal: Database access layer
        webhook_id: Webhook ID
        execution_id: Execution ID of triggered workflow
    """
    try:
        webhooks = dal.table('workflow_webhooks')
        await dal.update(
            webhooks,
            lambda row: row.webhook_id == webhook_id,
            {
                'last_triggered_at': datetime.utcnow(),
                'trigger_count': webhooks.trigger_count + 1,
                'last_execution_id': execution_id
            }
        )
    except Exception as e:
        logger.error(f"Error updating webhook trigger stats: {describe_db_error(e)}")


# ============================================================================
# POST /api/v1/workflows/webhooks/:token - Public webhook trigger
# ============================================================================

@webhook_api.route('/workflows/webhooks/<token>', methods=['POST'])
@async_endpoint
@handle_webhook_errors
async def trigger_webhook_public(token: str):
    """
    Publicly accessible webhook trigger endpoint.

    This endpoint requires NO authentication but verifies:
    - Webhook token validity
    - HMAC-SHA256 signature (if enabled)
    - IP allowlist (if configured)
    - Rate limiting (60 req/min per webhook)

    Path Parameters:
        token: Webhook token (public, included in URL)

    Headers:
        X-Webhook-Signature: sha256=<hex> (if require_signature=true)

    Request Body:
        Any JSON payload that triggers the workflow

    Returns:
        200: Webhook executed, returns execution_id
        400: Bad request (invalid payload)
        403: Forbidden (IP not allowed, signature invalid)
        404: Webhook not found
        429: Rate limit exceeded
        500: Server error

    Response:
    {
        "success": true,
        "data": {
            "execution_id": "uuid",
            "webhook_id": "uuid",
            "workflow_id": "uuid",
            "status": "queued",
            "timestamp": "2024-01-01T00:00:00Z"
        },
        "message": "Workflow execution triggered"
    }
    """
    dal = get_dal()
    workflow_engine = get_workflow_engine()

    # Get raw request body for signature verification
    body = await request.get_data()

    # Get webhook configuration
    webhook = await get_webhook_by_token(dal, token)

    if not webhook:
        logger.warning(
            "Webhook trigger attempted with invalid token",
            extra={
                "event_type": "AUDIT",
                "action": "trigger_webhook_public",
                "webhook_token": token,
                "result": "INVALID_TOKEN"
            }
        )
        return error_response(
            message="Webhook not found",
            status_code=404,
            error_code="WEBHOOK_NOT_FOUND"
        )

    if not webhook.enabled:
        logger.warning(
            "Webhook trigger attempted on disabled webhook",
            extra={
                "event_type": "AUDIT",
                "action": "trigger_webhook_public",
                "webhook_id": webhook.webhook_id,
                "result": "DISABLED"
            }
        )
        return error_response(
            message="Webhook is disabled",
            status_code=403,
            error_code="WEBHOOK_DISABLED"
        )

    # Check IP allowlist
    client_ip = request.remote_addr or "unknown"
    if not check_ip_allowlist(client_ip, webhook.ip_allowlist):
        logger.warning(
            "Webhook trigger rejected - IP not allowed",
            extra={
                "event_type": "AUDIT",
                "action": "trigger_webhook_public",
                "webhook_id": webhook.webhook_id,
                "client_ip": client_ip,
                "result": "IP_NOT_ALLOWED"
            }
        )
        return error_response(
            message="IP address not allowed",
            status_code=403,
            error_code="IP_NOT_ALLOWED"
        )

    # Verify signature if required
    if webhook.require_signature:
        signature = request.headers.get('X-Webhook-Signature', '')
        if not signature:
            logger.warning(
                "Webhook signature required but not provided",
                extra={
                    "event_type": "AUDIT",
                    "action": "trigger_webhook_public",
                    "webhook_id": webhook.webhook_id,
                    "result": "SIGNATURE_MISSING"
                }
            )
            return error_response(
                message="Webhook signature required",
                status_code=403,
                error_code="SIGNATURE_REQUIRED"
            )

        if not verify_webhook_signature(webhook.token, webhook.secret, body, signature):
            logger.warning(
                "Webhook signature verification failed",
                extra={
                    "event_type": "AUDIT",
                    "action": "trigger_webhook_public",
                    "webhook_id": webhook.webhook_id,
                    "result": "SIGNATURE_INVALID"
                }
            )
            return error_response(
                message="Invalid webhook signature",
                status_code=403,
                error_code="SIGNATURE_INVALID"
            )

    # Check rate limit
    allowed, remaining = webhook_rate_limiter.check_rate_limit(
        webhook.webhook_id,
        webhook.rate_limit_max,
        webhook.rate_limit_window
    )

    if not allowed:
        logger.warning(
            "Webhook rate limit exceeded",
            extra={
                "event_type": "AUDIT",
                "action": "trigger_webhook_public",
                "webhook_id": webhook.webhook_id,
                "result": "RATE_LIMIT_EXCEEDED"
            }
        )
        return error_response(
            message=f"Rate limit exceeded (max {webhook.rate_limit_max} requests per {webhook.rate_limit_window}s)",
            status_code=429,
            error_code="RATE_LIMIT_EXCEEDED"
        )

    # Parse request body
    try:
        payload = await request.get_json() if body else {}
    except Exception as e:
        logger.warning(
            f"Failed to parse webhook payload: {describe_db_error(e)}",
            extra={
                "event_type": "AUDIT",
                "action": "trigger_webhook_public",
                "webhook_id": webhook.webhook_id,
                "result": "PARSE_ERROR"
            }
        )
        return error_response(
            message="Invalid JSON payload",
            status_code=400,
            error_code="INVALID_JSON"
        )

    # Trigger workflow execution
    try:
        execution_result = await workflow_engine.execute_workflow(
            workflow_id=webhook.workflow_id,
            trigger_data=payload,
            trigger_type="webhook",
            trigger_context={
                "webhook_id": webhook.webhook_id,
                "webhook_name": webhook.name,
                "client_ip": client_ip
            }
        )

        # Update webhook trigger stats
        await update_webhook_trigger_stats(dal, webhook.webhook_id, execution_result.execution_id)

        logger.info(
            "Webhook trigger successful",
            extra={
                "event_type": "AUDIT",
                "action": "trigger_webhook_public",
                "webhook_id": webhook.webhook_id,
                "workflow_id": webhook.workflow_id,
                "execution_id": execution_result.execution_id,
                "result": "SUCCESS"
            }
        )

        return success_response(
            data={
                "execution_id": execution_result.execution_id,
                "webhook_id": webhook.webhook_id,
                "workflow_id": webhook.workflow_id,
                "status": "queued",
                "timestamp": datetime.utcnow().isoformat()
            },
            status_code=200,
            message="Workflow execution triggered"
        )

    except Exception as e:
        logger.error(
            f"Workflow execution failed: {describe_db_error(e)}",
            extra={
                "event_type": "ERROR",
                "action": "trigger_webhook_public",
                "webhook_id": webhook.webhook_id,
                "workflow_id": webhook.workflow_id,
                "result": "EXECUTION_FAILED"
            }
        )
        return error_response(
            message="Failed to trigger workflow execution",
            status_code=500,
            error_code="EXECUTION_FAILED"
        )


# ============================================================================
# GET /api/v1/workflows/:id/webhooks - List webhooks for workflow
# ============================================================================

@webhook_api.route('/workflows/<workflow_id>/webhooks', methods=['GET'])
@async_endpoint
@auth_required
@handle_webhook_errors
async def list_webhooks(workflow_id: str):
    """
    List all webhooks for a workflow.

    Requires can_view permission on workflow.

    Path Parameters:
        workflow_id: Workflow UUID

    Query Parameters:
        community_id: Optional community ID for context

    Returns:
        200: List of webhooks
        401: Unauthorized
        403: Permission denied
        404: Workflow not found
        500: Server error
    """
    user_info = request.current_user
    user_id = int(user_info['user_id'])

    community_id = request.args.get('community_id')
    community_id = int(community_id) if community_id else None

    # Check if user has permission to view workflow
    workflow_service = get_workflow_service()
    permission_service = get_permission_service()

    workflow = await workflow_service.get_workflow(
        workflow_id=workflow_id,
        user_id=user_id,
        community_id=community_id
    )

    perms = await permission_service.check_permission(
        workflow_id=workflow_id,
        user_id=user_id,
        permission="can_view"
    )

    if not perms.can_view:
        # WorkflowPermissionException(workflow_id, permission) -- a bare
        # message-only call raised TypeError (missing required `permission`
        # arg) on every permission denial; undetected because this
        # blueprint is dead code (never registered by app.py) with 0%
        # coverage.
        raise WorkflowPermissionException(workflow_id, "can_view")

    # Get webhooks from database
    dal = get_dal()
    try:
        webhooks_table = dal.table('workflow_webhooks')
        webhook_rows = await dal.select(
            webhooks_table,
            lambda row: row.workflow_id == workflow_id
        )

        webhooks = []
        for row in webhook_rows:
            webhook = WebhookConfig(
                webhook_id=row.webhook_id,
                workflow_id=row.workflow_id,
                token=row.token,
                secret=row.secret,
                name=row.name,
                description=row.description,
                url=row.url,
                enabled=row.enabled,
                require_signature=row.require_signature,
                ip_allowlist=row.ip_allowlist or [],
                rate_limit_max=row.rate_limit_max,
                rate_limit_window=row.rate_limit_window,
                created_at=row.created_at,
                updated_at=row.updated_at,
                last_triggered_at=row.last_triggered_at,
                trigger_count=row.trigger_count
            )
            webhooks.append(webhook.to_dict())

        logger.info(
            f"Listed {len(webhooks)} webhooks for workflow",
            extra={
                "event_type": "AUDIT",
                "action": "list_webhooks",
                "workflow_id": workflow_id,
                "user_id": user_id,
                "webhook_count": len(webhooks),
                "result": "SUCCESS"
            }
        )

        return success_response(
            data=webhooks,
            message=f"Retrieved {len(webhooks)} webhooks"
        )

    except Exception as e:
        logger.error(
            f"Error listing webhooks: {describe_db_error(e)}",
            extra={
                "event_type": "ERROR",
                "action": "list_webhooks",
                "workflow_id": workflow_id,
                "result": "FAILURE"
            }
        )
        raise


# ============================================================================
# POST /api/v1/workflows/:id/webhooks - Create webhook
# ============================================================================

@webhook_api.route('/workflows/<workflow_id>/webhooks', methods=['POST'])
@async_endpoint
@auth_required
@handle_webhook_errors
async def create_webhook(workflow_id: str):
    """
    Create a new webhook for a workflow.

    Requires can_edit permission on workflow.

    Path Parameters:
        workflow_id: Workflow UUID

    Request Body:
    {
        "name": "Webhook Name",
        "description": "Optional description",
        "enable_signature": true,
        "ip_allowlist": ["192.168.1.0/24", "10.0.0.1"],
        "rate_limit_max": 60,
        "rate_limit_window": 60,
        "community_id": 123
    }

    Returns:
        201: Created webhook
        400: Invalid input
        401: Unauthorized
        403: Permission denied
        404: Workflow not found
        500: Server error

    Response:
    {
        "success": true,
        "data": {
            "webhook_id": "uuid",
            "workflow_id": "uuid",
            "token": "token123",
            "name": "Webhook Name",
            "url": "https://example.com/api/v1/workflows/webhooks/token123",
            "enabled": true,
            "require_signature": true,
            "created_at": "2024-01-01T00:00:00Z"
        },
        "message": "Webhook created successfully"
    }
    """
    user_info = request.current_user
    user_id = int(user_info['user_id'])

    data = await request.get_json()

    if not data:
        raise ValueError("Request body is required")

    if 'name' not in data:
        raise ValueError("Field 'name' is required")

    # Check if user has permission to edit workflow
    workflow_service = get_workflow_service()
    permission_service = get_permission_service()

    workflow = await workflow_service.get_workflow(
        workflow_id=workflow_id,
        user_id=user_id
    )

    perms = await permission_service.check_permission(
        workflow_id=workflow_id,
        user_id=user_id,
        permission="can_edit"
    )

    if not perms.can_edit:
        raise WorkflowPermissionException(workflow_id, "can_edit")

    # Generate webhook token and secret
    token = generate_webhook_token()
    secret = generate_webhook_secret()

    # Get community ID from request or workflow
    community_id = data.get('community_id')

    # Create webhook configuration
    webhook_id = str(__import__('uuid').uuid4())
    webhook = WebhookConfig(
        webhook_id=webhook_id,
        workflow_id=workflow_id,
        token=token,
        secret=secret,
        name=data['name'],
        description=data.get('description'),
        enabled=data.get('enabled', True),
        require_signature=data.get('require_signature', True),
        ip_allowlist=data.get('ip_allowlist', []),
        rate_limit_max=data.get('rate_limit_max', 60),
        rate_limit_window=data.get('rate_limit_window', 60),
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )

    # Generate webhook URL
    webhook.url = f"https://api.example.com/api/v1/workflows/webhooks/{token}"

    # Save to database
    dal = get_dal()
    try:
        webhooks_table = dal.table('workflow_webhooks')
        await dal.insert(
            webhooks_table,
            {
                'webhook_id': webhook.webhook_id,
                'workflow_id': webhook.workflow_id,
                'token': webhook.token,
                'secret': webhook.secret,
                'name': webhook.name,
                'description': webhook.description,
                'url': webhook.url,
                'enabled': webhook.enabled,
                'require_signature': webhook.require_signature,
                'ip_allowlist': webhook.ip_allowlist,
                'rate_limit_max': webhook.rate_limit_max,
                'rate_limit_window': webhook.rate_limit_window,
                'created_at': webhook.created_at,
                'updated_at': webhook.updated_at,
                'trigger_count': 0
            }
        )

        logger.info(
            "Webhook created successfully",
            extra={
                "event_type": "AUDIT",
                "action": "create_webhook",
                "webhook_id": webhook.webhook_id,
                "workflow_id": workflow_id,
                "user_id": user_id,
                "community": str(community_id) if community_id else "unknown",
                "result": "SUCCESS"
            }
        )

        response_data = webhook.to_dict()
        return success_response(
            data=response_data,
            status_code=201,
            message="Webhook created successfully"
        )

    except Exception as e:
        logger.error(
            f"Error creating webhook: {describe_db_error(e)}",
            extra={
                "event_type": "ERROR",
                "action": "create_webhook",
                "workflow_id": workflow_id,
                "result": "FAILURE"
            }
        )
        raise


# ============================================================================
# DELETE /api/v1/workflows/:id/webhooks/:webhookId - Delete webhook
# ============================================================================

@webhook_api.route('/workflows/<workflow_id>/webhooks/<webhook_id>', methods=['DELETE'])
@async_endpoint
@auth_required
@handle_webhook_errors
async def delete_webhook(workflow_id: str, webhook_id: str):
    """
    Delete a webhook.

    Requires can_edit permission on workflow.

    Path Parameters:
        workflow_id: Workflow UUID
        webhook_id: Webhook UUID

    Query Parameters:
        community_id: Optional community ID for context

    Returns:
        200: Webhook deleted
        401: Unauthorized
        403: Permission denied
        404: Workflow or webhook not found
        500: Server error
    """
    user_info = request.current_user
    user_id = int(user_info['user_id'])

    community_id = request.args.get('community_id')
    community_id = int(community_id) if community_id else None

    # Check if user has permission to edit workflow
    workflow_service = get_workflow_service()
    permission_service = get_permission_service()

    workflow = await workflow_service.get_workflow(
        workflow_id=workflow_id,
        user_id=user_id,
        community_id=community_id
    )

    perms = await permission_service.check_permission(
        workflow_id=workflow_id,
        user_id=user_id,
        permission="can_edit"
    )

    if not perms.can_edit:
        raise WorkflowPermissionException(workflow_id, "can_edit")

    # Get and verify webhook exists
    dal = get_dal()
    webhook = await get_webhook_from_db(dal, workflow_id, webhook_id)

    if not webhook:
        raise WorkflowNotFoundException(webhook_id)

    # Delete webhook from database
    try:
        webhooks_table = dal.table('workflow_webhooks')
        await dal.delete(
            webhooks_table,
            lambda row: (
                row.webhook_id == webhook_id and
                row.workflow_id == workflow_id
            )
        )

        logger.info(
            "Webhook deleted successfully",
            extra={
                "event_type": "AUDIT",
                "action": "delete_webhook",
                "webhook_id": webhook_id,
                "workflow_id": workflow_id,
                "user_id": user_id,
                "community": str(community_id) if community_id else "unknown",
                "result": "SUCCESS"
            }
        )

        return success_response(
            data={"webhook_id": webhook_id},
            message="Webhook deleted successfully"
        )

    except Exception as e:
        logger.error(
            f"Error deleting webhook: {describe_db_error(e)}",
            extra={
                "event_type": "ERROR",
                "action": "delete_webhook",
                "webhook_id": webhook_id,
                "workflow_id": workflow_id,
                "result": "FAILURE"
            }
        )
        raise


# ============================================================================
# Error Handlers
# ============================================================================

@webhook_api.errorhandler(400)
async def bad_request(error):
    """Handle 400 Bad Request errors"""
    return error_response(
        message=str(error),
        status_code=400,
        error_code="BAD_REQUEST"
    )


@webhook_api.errorhandler(401)
async def unauthorized(error):
    """Handle 401 Unauthorized errors"""
    return error_response(
        message="Authentication required",
        status_code=401,
        error_code="UNAUTHORIZED"
    )


@webhook_api.errorhandler(403)
async def forbidden(error):
    """Handle 403 Forbidden errors"""
    return error_response(
        message="Permission denied",
        status_code=403,
        error_code="FORBIDDEN"
    )


@webhook_api.errorhandler(404)
async def not_found(error):
    """Handle 404 Not Found errors"""
    return error_response(
        message="Resource not found",
        status_code=404,
        error_code="NOT_FOUND"
    )


@webhook_api.errorhandler(500)
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

def register_webhook_api(app, workflow_service: WorkflowService, permission_service: PermissionService, workflow_engine: WorkflowEngine):
    """
    Register webhook API blueprint with app.

    Args:
        app: Quart application instance
        workflow_service: Configured WorkflowService instance
        permission_service: Configured PermissionService instance
        workflow_engine: Configured WorkflowEngine instance
    """
    # Store services in app config
    app.config['workflow_service'] = workflow_service
    app.config['permission_service'] = permission_service
    app.config['workflow_engine'] = workflow_engine

    # Register blueprint
    app.register_blueprint(webhook_api)

    logger.info(
        "Webhook API registered",
        extra={
            "event_type": "SYSTEM",
            "action": "register_webhook_api",
            "result": "SUCCESS"
        }
    )
