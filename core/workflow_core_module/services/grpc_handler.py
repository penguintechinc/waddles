"""
gRPC Handler for Workflow Service
===================================

Implements the gRPC service for workflow triggering with:
- JWT token verification
- Workflow execution via WorkflowEngine
- Request validation and error handling
- Comprehensive logging and audit trail
"""

import json
import logging
from typing import Optional, Dict, Any
from datetime import datetime

import grpc
import jwt

# Import generated protobuf messages
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'proto'))

try:
    import workflow_pb2
    import workflow_pb2_grpc
except ImportError:
    # Fallback if proto files haven't been generated yet
    workflow_pb2 = None
    workflow_pb2_grpc = None


logger = logging.getLogger(__name__)


class WorkflowServiceServicer:
    """
    gRPC service implementation for Workflow operations.

    Handles workflow triggering with JWT verification and integration
    with the workflow engine for execution management.
    """

    def __init__(
        self,
        workflow_engine,
        workflow_service,
        permission_service,
        secret_key: str,
        logger_instance: Optional[logging.Logger] = None
    ):
        """
        Initialize gRPC service handler.

        Args:
            workflow_engine: WorkflowEngine instance for execution
            workflow_service: WorkflowService instance for workflow operations
            permission_service: PermissionService instance for access control
            secret_key: Secret key for JWT validation
            logger_instance: Optional logger instance
        """
        self.workflow_engine = workflow_engine
        self.workflow_service = workflow_service
        self.permission_service = permission_service
        self.secret_key = secret_key
        self.logger = logger_instance or logger

    async def TriggerWorkflow(self, request, context) -> 'workflow_pb2.SuccessResponse':
        """
        Trigger a workflow execution via gRPC.

        Args:
            request: TriggerWorkflowRequest with workflow_id and trigger details
            context: gRPC context for metadata and error handling

        Returns:
            SuccessResponse with execution result
        """
        execution_id = None

        try:
            # Extract request fields
            token = request.token
            workflow_id = request.workflow_id
            trigger_source = request.trigger_source
            trigger_data_json = request.trigger_data
            session_id = request.session_id
            entity_id = request.entity_id
            user_id = request.user_id
            platform = request.platform

            self.logger.info(
                f"gRPC: TriggerWorkflow received",
                extra={
                    "event_type": "AUDIT",
                    "action": "grpc_trigger_workflow",
                    "workflow_id": workflow_id,
                    "user_id": user_id,
                    "trigger_source": trigger_source,
                    "platform": platform,
                }
            )

            # Step 1: Verify JWT token. `algorithms=["HS256"]` is an explicit
            # allowlist -- PyJWT rejects any other `alg` (including `none`)
            # even if the token itself declares it, preventing algorithm-
            # confusion/none-alg forgery.
            try:
                payload = jwt.decode(
                    token,
                    self.secret_key,
                    algorithms=["HS256"]
                )
                token_user_id = payload.get("user_id")
                token_community_id = payload.get("community_id")

                if not token_user_id:
                    self.logger.warning(
                        "gRPC: Invalid token - missing user_id",
                        extra={
                            "action": "grpc_trigger_workflow",
                            "workflow_id": workflow_id,
                            "result": "INVALID_TOKEN"
                        }
                    )
                    await context.abort(
                        grpc.StatusCode.UNAUTHENTICATED,
                        "Invalid token: missing user_id"
                    )

            except jwt.ExpiredSignatureError:
                self.logger.warning(
                    "gRPC: JWT verification failed: token expired",
                    extra={
                        "action": "grpc_trigger_workflow",
                        "workflow_id": workflow_id,
                        "result": "AUTH_FAILED"
                    }
                )
                await context.abort(
                    grpc.StatusCode.UNAUTHENTICATED,
                    "JWT verification failed: token expired"
                )
            except jwt.InvalidTokenError as e:
                self.logger.warning(
                    f"gRPC: JWT verification failed: {str(e)}",
                    extra={
                        "action": "grpc_trigger_workflow",
                        "workflow_id": workflow_id,
                        "result": "AUTH_FAILED"
                    }
                )
                await context.abort(
                    grpc.StatusCode.UNAUTHENTICATED,
                    f"JWT verification failed: {str(e)}"
                )

            # Step 2: Verify user_id matches
            if user_id != token_user_id:
                self.logger.authz(
                    action="grpc_trigger_workflow",
                    user=str(user_id),
                    community=str(token_community_id) if token_community_id else "",
                    result="DENIED",
                    extra={
                        "workflow_id": workflow_id,
                        "reason": "user_id mismatch"
                    }
                )
                await context.abort(
                    grpc.StatusCode.PERMISSION_DENIED,
                    "User ID mismatch"
                )

            # Step 3: Check permission to execute workflow
            can_execute = await self.permission_service.check_permission(
                workflow_id=workflow_id,
                user_id=user_id,
                permission_type="can_execute",
                community_id=token_community_id
            )

            if not can_execute:
                self.logger.authz(
                    action="grpc_trigger_workflow",
                    user=str(user_id),
                    community=str(token_community_id) if token_community_id else "",
                    result="DENIED",
                    extra={"workflow_id": workflow_id}
                )
                await context.abort(
                    grpc.StatusCode.PERMISSION_DENIED,
                    "Permission denied: cannot execute workflow"
                )

            # Step 4: Parse trigger data
            try:
                trigger_data = json.loads(trigger_data_json)
            except json.JSONDecodeError as e:
                self.logger.error(
                    f"gRPC: Invalid trigger_data JSON: {str(e)}",
                    extra={
                        "action": "grpc_trigger_workflow",
                        "workflow_id": workflow_id,
                        "result": "INVALID_DATA"
                    }
                )
                await context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    f"Invalid trigger_data JSON: {str(e)}"
                )

            # Step 5: Enrich trigger data with request-level fields.
            #
            # WorkflowEngine.execute_workflow only builds its own
            # ExecutionContext when `context=None` -- if a `context` is
            # passed in, it does `context.execution_id = execution_id`
            # unconditionally (services/workflow_engine.py), which requires
            # attribute assignment. This previously passed a plain dict
            # (`execution_context`), which raises `AttributeError: 'dict'
            # object has no attribute 'execution_id'` on every single gRPC-
            # triggered execution -- caught by the generic `except
            # Exception` below and surfaced as an opaque INTERNAL abort.
            # Folding these fields into `trigger_data` instead lets
            # execute_workflow's `context is None` branch build a real
            # `ExecutionContext` from them, the only path that actually
            # works.
            trigger_data.setdefault("session_id", session_id)
            trigger_data.setdefault("entity_id", entity_id)
            trigger_data.setdefault("user_id", user_id)
            trigger_data.setdefault("platform", platform)
            trigger_data["trigger_source"] = trigger_source
            trigger_data["initiated_by"] = user_id
            trigger_data["initiated_at"] = datetime.utcnow().isoformat()
            trigger_data["grpc_initiated"] = True

            # Step 6: Execute workflow
            execution_result = await self.workflow_engine.execute_workflow(
                workflow_id=workflow_id,
                trigger_data=trigger_data,
            )

            execution_id = execution_result.execution_id

            self.logger.audit(
                action="grpc_trigger_workflow",
                user=str(user_id),
                community=str(token_community_id) if token_community_id else "",
                result="SUCCESS",
                extra={
                    "workflow_id": workflow_id,
                    "execution_id": execution_id,
                    "trigger_source": trigger_source,
                }
            )

            # Step 7: Return success response
            return workflow_pb2.SuccessResponse(
                success=True,
                message=f"Workflow execution started: {execution_id}",
                error=""
            )

        except grpc.aio.AbortError:
            # `context.abort()` (Steps 1-4 above) raises this to unwind the
            # coroutine once the deliberate status/message has already been
            # set on the RPC -- `grpc.aio.AbortError` is a plain `Exception`
            # subclass (not `grpc.RpcError`), so without this clause every
            # early abort fell through to the generic `except Exception`
            # below and triggered a *second* `context.abort()` call with the
            # wrong status (INTERNAL) and a message wrapping the original
            # abort's own message -- masking the real UNAUTHENTICATED/
            # PERMISSION_DENIED/INVALID_ARGUMENT result.
            raise
        except grpc.RpcError:
            # Re-raise gRPC errors
            raise
        except Exception as e:
            error_msg = f"Failed to trigger workflow: {str(e)}"

            self.logger.error(
                error_msg,
                extra={
                    "event_type": "ERROR",
                    "action": "grpc_trigger_workflow",
                    "workflow_id": request.workflow_id,
                    "execution_id": execution_id,
                    "user_id": request.user_id,
                    "result": "FAILURE",
                },
                exc_info=True
            )

            await context.abort(
                grpc.StatusCode.INTERNAL,
                error_msg
            )


class WorkflowGrpcService(workflow_pb2_grpc.WorkflowServiceServicer):
    """
    Concrete gRPC service implementation that wraps WorkflowServiceServicer.

    This class provides the actual gRPC endpoint implementations that are
    registered with the gRPC server.
    """

    def __init__(self, handler: WorkflowServiceServicer):
        """
        Initialize with the handler instance.

        Args:
            handler: WorkflowServiceServicer instance
        """
        self.handler = handler

    async def TriggerWorkflow(self, request, context):
        """
        gRPC endpoint for triggering workflows.

        Args:
            request: TriggerWorkflowRequest
            context: gRPC context

        Returns:
            SuccessResponse
        """
        return await self.handler.TriggerWorkflow(request, context)
