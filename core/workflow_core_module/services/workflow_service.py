"""
Workflow Service
================

Comprehensive workflow management service with:
- CRUD operations for workflows
- License validation integration
- Permission-based access control
- Workflow validation before publish
- Audit logging for all operations
- Database operations via AsyncDAL

Integrates with:
- LicenseService: Premium feature validation (HTTP 402 on failure)
- PermissionService: Granular access control
- ValidationService: Workflow structure validation
- AsyncDAL: PostgreSQL database operations
"""

import logging
import json
from typing import Dict, Any, List, Optional
from datetime import datetime
from uuid import UUID, uuid4

from services.license_service import LicenseService, LicenseValidationException
from services.permission_service import PermissionService, PermissionInfo
from services.validation_service import WorkflowValidationService
from models.workflow import (
    WorkflowDefinition,
    WorkflowMetadata,
    WorkflowStatus,
    WorkflowConnection,
)
from flask_core import describe_db_error


logger = logging.getLogger(__name__)


class WorkflowServiceException(Exception):
    """Base exception for workflow service errors"""
    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class WorkflowNotFoundException(WorkflowServiceException):
    """Raised when workflow is not found"""
    def __init__(self, workflow_id: str):
        super().__init__(f"Workflow not found: {workflow_id}", status_code=404)


class WorkflowPermissionException(WorkflowServiceException):
    """Raised when user lacks required permission"""
    def __init__(self, workflow_id: str, permission: str):
        super().__init__(
            f"Permission denied for workflow {workflow_id}: {permission} required",
            status_code=403
        )


def _failure_detail(exc: BaseException) -> str:
    """Describe `exc` for an exception message that is surfaced to API clients.

    This module's own exceptions carry authored text and pass through unchanged;
    anything else (a DB driver / Redis / HTTP error) is reduced to its type and
    SQLSTATE so bound values (message content, usernames) never reach a client
    or a downstream log line that re-logs the message.
    """
    if isinstance(exc, WorkflowServiceException):
        return exc.message
    return describe_db_error(exc)


class WorkflowService:
    """
    Comprehensive workflow management service.

    Handles all workflow CRUD operations with:
    - License validation (premium features)
    - Permission checks (access control)
    - Validation (structure integrity)
    - Audit logging (compliance)
    """

    def __init__(
        self,
        dal,
        license_service: LicenseService,
        permission_service: PermissionService,
        validation_service: WorkflowValidationService,
        logger_instance: Optional[logging.Logger] = None
    ):
        """
        Initialize workflow service.

        Args:
            dal: AsyncDAL instance for database operations
            license_service: License validation service
            permission_service: Permission checking service
            validation_service: Workflow validation service
            logger_instance: Optional logger instance
        """
        self.dal = dal
        self.license_service = license_service
        self.permission_service = permission_service
        self.validation_service = validation_service
        self.logger = logger_instance or logger

    async def create_workflow(
        self,
        workflow_data: Dict[str, Any],
        community_id: int,
        entity_id: int,
        user_id: int,
        license_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Create a new workflow with license and permission checks.

        Args:
            workflow_data: Workflow definition data
            community_id: Community ID
            entity_id: Entity ID
            user_id: Creating user ID
            license_key: Optional license key for validation

        Returns:
            Created workflow data with workflow_id

        Raises:
            LicenseValidationException: If license check fails (HTTP 402)
            WorkflowServiceException: If creation fails
        """
        try:
            # Step 1: License validation
            self.logger.info(
                f"Creating workflow for community {community_id}",
                extra={
                    "event_type": "AUDIT",
                    "action": "create_workflow",
                    "community": str(community_id),
                    "user": str(user_id),
                }
            )

            # Validate license (raises LicenseValidationException on failure)
            await self.license_service.validate_workflow_creation(
                community_id=community_id,
                entity_id=str(entity_id),
                license_key=license_key
            )

            # Step 2: Generate workflow ID
            workflow_id = str(uuid4())

            # Step 3: Build workflow metadata
            metadata = {
                "workflow_id": workflow_id,
                "name": workflow_data.get("name", "Untitled Workflow"),
                "description": workflow_data.get("description", ""),
                "author_id": str(user_id),
                "community_id": str(community_id),
                "version": workflow_data.get("version", "1.0.0"),
                "tags": workflow_data.get("tags", []),
                "status": WorkflowStatus.DRAFT.value,
                "enabled": False,
                "is_template": workflow_data.get("is_template", False),
                "icon_url": workflow_data.get("icon_url"),
                "documentation_url": workflow_data.get("documentation_url"),
                "max_execution_time_seconds": workflow_data.get("max_execution_time_seconds", 300),
                "max_parallel_executions": workflow_data.get("max_parallel_executions", 10),
                "timeout_on_error": workflow_data.get("timeout_on_error", False),
                "retry_failed_nodes": workflow_data.get("retry_failed_nodes", False),
                "max_retries": workflow_data.get("max_retries", 0),
                "created_at": datetime.utcnow().isoformat(),
                "updated_at": datetime.utcnow().isoformat(),
            }

            # Step 4: Extract workflow components
            nodes = workflow_data.get("nodes", {})
            connections = workflow_data.get("connections", [])
            global_variables = workflow_data.get("global_variables", {})

            # Extract trigger configuration
            trigger_type = workflow_data.get("trigger_type", "command")
            trigger_config = workflow_data.get("trigger_config", {})

            # Step 5: Insert into database
            insert_query = """
                INSERT INTO workflows (
                    workflow_id, community_id, entity_id, name, description, version,
                    status, is_active, nodes, connections, trigger_type, trigger_config,
                    max_execution_time, max_iterations, retry_config, created_by, updated_by
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                ) RETURNING id
            """

            params = [
                workflow_id,
                community_id,
                entity_id,
                metadata["name"],
                metadata["description"],
                1,  # version starts at 1
                WorkflowStatus.DRAFT.value,
                False,  # is_active
                json.dumps(nodes),
                json.dumps(connections),
                trigger_type,
                json.dumps(trigger_config),
                metadata["max_execution_time_seconds"],
                100,  # max_iterations default
                json.dumps({"enabled": metadata["retry_failed_nodes"], "max_retries": metadata["max_retries"]}),
                user_id,
                user_id,
            ]

            result = self.dal.executesql(insert_query, params)

            # Step 6: Log audit event
            await self._log_audit(
                workflow_id=workflow_id,
                action="created",
                action_by=user_id,
                changes={"initial_creation": True},
                metadata={"community_id": community_id, "entity_id": entity_id}
            )

            self.logger.audit(
                action="create_workflow",
                user=str(user_id),
                community=str(community_id),
                result="SUCCESS",
                extra={
                    "workflow_id": workflow_id,
                    "workflow_name": metadata["name"],
                }
            )

            # Step 7: Return created workflow
            return {
                "workflow_id": workflow_id,
                "metadata": metadata,
                "nodes": nodes,
                "connections": connections,
                "global_variables": global_variables,
                "status": WorkflowStatus.DRAFT.value,
                "created_at": metadata["created_at"],
            }

        except LicenseValidationException:
            # Re-raise license exceptions (HTTP 402)
            raise
        except Exception as e:
            self.logger.error(
                f"Failed to create workflow: {describe_db_error(e)}",
                extra={
                    "event_type": "ERROR",
                    "action": "create_workflow",
                    "community": str(community_id),
                    "user": str(user_id),
                    "result": "FAILURE",
                }
            )
            raise WorkflowServiceException(f"Failed to create workflow: {_failure_detail(e)}")

    async def get_workflow(
        self,
        workflow_id: str,
        user_id: int,
        community_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Get workflow by ID with permission check.

        Args:
            workflow_id: Workflow UUID
            user_id: Requesting user ID
            community_id: Optional community ID for permission context

        Returns:
            Complete workflow data

        Raises:
            WorkflowNotFoundException: If workflow not found
            WorkflowPermissionException: If user lacks view permission
        """
        try:
            # Step 1: Check view permission
            can_view = await self.permission_service.check_permission(
                workflow_id=workflow_id,
                user_id=user_id,
                permission_type="can_view",
                community_id=community_id
            )

            if not can_view:
                self.logger.authz(
                    action="get_workflow",
                    user=str(user_id),
                    community=str(community_id) if community_id else "",
                    result="DENIED",
                    extra={"workflow_id": workflow_id}
                )
                raise WorkflowPermissionException(workflow_id, "can_view")

            # Step 2: Fetch workflow from database
            query = """
                SELECT workflow_id, community_id, entity_id, name, description, version,
                       status, is_active, nodes, connections, trigger_type, trigger_config,
                       max_execution_time, max_iterations, retry_config,
                       execution_count, success_count, failure_count, last_executed_at,
                       created_by, updated_by, created_at, updated_at
                FROM workflows
                WHERE workflow_id = %s
            """

            result = self.dal.executesql(query, [workflow_id])

            if not result or len(result) == 0:
                raise WorkflowNotFoundException(workflow_id)

            row = result[0]

            # Step 3: Build response
            workflow_data = {
                "workflow_id": str(row[0]),
                "community_id": row[1],
                "entity_id": row[2],
                "name": row[3],
                "description": row[4],
                "version": row[5],
                "status": row[6],
                "is_active": row[7],
                "nodes": row[8],  # JSONB
                "connections": row[9],  # JSONB
                "trigger_type": row[10],
                "trigger_config": row[11],  # JSONB
                "max_execution_time": row[12],
                "max_iterations": row[13],
                "retry_config": row[14],  # JSONB
                "execution_count": row[15],
                "success_count": row[16],
                "failure_count": row[17],
                "last_executed_at": row[18].isoformat() if row[18] else None,
                "created_by": row[19],
                "updated_by": row[20],
                "created_at": row[21].isoformat(),
                "updated_at": row[22].isoformat(),
            }

            self.logger.authz(
                action="get_workflow",
                user=str(user_id),
                community=str(community_id) if community_id else "",
                result="SUCCESS",
                extra={"workflow_id": workflow_id}
            )

            return workflow_data

        except (WorkflowNotFoundException, WorkflowPermissionException):
            raise
        except Exception as e:
            self.logger.error(
                f"Failed to get workflow: {describe_db_error(e)}",
                extra={
                    "event_type": "ERROR",
                    "action": "get_workflow",
                    "user": str(user_id),
                    "workflow_id": workflow_id,
                    "result": "FAILURE",
                }
            )
            raise WorkflowServiceException(f"Failed to get workflow: {_failure_detail(e)}")

    async def update_workflow(
        self,
        workflow_id: str,
        updates: Dict[str, Any],
        user_id: int,
        community_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Update workflow with permission check.

        Args:
            workflow_id: Workflow UUID
            updates: Dictionary of fields to update
            user_id: Updating user ID
            community_id: Optional community ID for permission context

        Returns:
            Updated workflow data

        Raises:
            WorkflowPermissionException: If user lacks edit permission
            WorkflowNotFoundException: If workflow not found
        """
        try:
            # Step 1: Check edit permission
            can_edit = await self.permission_service.check_permission(
                workflow_id=workflow_id,
                user_id=user_id,
                permission_type="can_edit",
                community_id=community_id
            )

            if not can_edit:
                self.logger.authz(
                    action="update_workflow",
                    user=str(user_id),
                    community=str(community_id) if community_id else "",
                    result="DENIED",
                    extra={"workflow_id": workflow_id}
                )
                raise WorkflowPermissionException(workflow_id, "can_edit")

            # Step 2: Build update query dynamically
            allowed_fields = [
                "name", "description", "nodes", "connections", "trigger_type",
                "trigger_config", "max_execution_time", "max_iterations", "retry_config"
            ]

            update_parts = []
            params = []

            for field, value in updates.items():
                if field in allowed_fields:
                    if field in ["nodes", "connections", "trigger_config", "retry_config"]:
                        # JSONB fields
                        update_parts.append(f"{field} = %s")
                        params.append(json.dumps(value) if not isinstance(value, str) else value)
                    else:
                        update_parts.append(f"{field} = %s")
                        params.append(value)

            if not update_parts:
                raise WorkflowServiceException("No valid fields to update")

            # Add updated_by and updated_at
            update_parts.append("updated_by = %s")
            params.append(user_id)

            params.append(workflow_id)

            update_query = f"""
                UPDATE workflows
                SET {', '.join(update_parts)}
                WHERE workflow_id = %s
                RETURNING workflow_id
            """

            result = self.dal.executesql(update_query, params)

            if not result or len(result) == 0:
                raise WorkflowNotFoundException(workflow_id)

            # Step 3: Log audit event
            await self._log_audit(
                workflow_id=workflow_id,
                action="updated",
                action_by=user_id,
                changes=updates,
                metadata={"fields_updated": list(updates.keys())}
            )

            self.logger.audit(
                action="update_workflow",
                user=str(user_id),
                community=str(community_id) if community_id else "",
                result="SUCCESS",
                extra={
                    "workflow_id": workflow_id,
                    "fields_updated": list(updates.keys()),
                }
            )

            # Step 4: Fetch and return updated workflow
            return await self.get_workflow(workflow_id, user_id, community_id)

        except (WorkflowPermissionException, WorkflowNotFoundException):
            raise
        except Exception as e:
            self.logger.error(
                f"Failed to update workflow: {describe_db_error(e)}",
                extra={
                    "event_type": "ERROR",
                    "action": "update_workflow",
                    "user": str(user_id),
                    "workflow_id": workflow_id,
                    "result": "FAILURE",
                }
            )
            raise WorkflowServiceException(f"Failed to update workflow: {_failure_detail(e)}")

    async def delete_workflow(
        self,
        workflow_id: str,
        user_id: int,
        community_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Delete (archive) workflow with permission check.

        Archives the workflow by setting status to 'archived' instead of hard delete.

        Args:
            workflow_id: Workflow UUID
            user_id: Deleting user ID
            community_id: Optional community ID for permission context

        Returns:
            Status message

        Raises:
            WorkflowPermissionException: If user lacks delete permission
            WorkflowNotFoundException: If workflow not found
        """
        try:
            # Step 1: Check delete permission
            can_delete = await self.permission_service.check_permission(
                workflow_id=workflow_id,
                user_id=user_id,
                permission_type="can_delete",
                community_id=community_id
            )

            if not can_delete:
                self.logger.authz(
                    action="delete_workflow",
                    user=str(user_id),
                    community=str(community_id) if community_id else "",
                    result="DENIED",
                    extra={"workflow_id": workflow_id}
                )
                raise WorkflowPermissionException(workflow_id, "can_delete")

            # Step 2: Archive workflow (soft delete)
            update_query = """
                UPDATE workflows
                SET status = %s, is_active = false, updated_by = %s
                WHERE workflow_id = %s
                RETURNING workflow_id
            """

            result = self.dal.executesql(
                update_query,
                [WorkflowStatus.ARCHIVED.value, user_id, workflow_id]
            )

            if not result or len(result) == 0:
                raise WorkflowNotFoundException(workflow_id)

            # Step 3: Log audit event
            await self._log_audit(
                workflow_id=workflow_id,
                action="archived",
                action_by=user_id,
                changes={"status": WorkflowStatus.ARCHIVED.value},
                metadata={"soft_delete": True}
            )

            self.logger.audit(
                action="delete_workflow",
                user=str(user_id),
                community=str(community_id) if community_id else "",
                result="SUCCESS",
                extra={"workflow_id": workflow_id}
            )

            return {
                "workflow_id": workflow_id,
                "status": "archived",
                "message": "Workflow archived successfully"
            }

        except (WorkflowPermissionException, WorkflowNotFoundException):
            raise
        except Exception as e:
            self.logger.error(
                f"Failed to delete workflow: {describe_db_error(e)}",
                extra={
                    "event_type": "ERROR",
                    "action": "delete_workflow",
                    "user": str(user_id),
                    "workflow_id": workflow_id,
                    "result": "FAILURE",
                }
            )
            raise WorkflowServiceException(f"Failed to delete workflow: {_failure_detail(e)}")

    async def list_workflows(
        self,
        entity_id: int,
        user_id: int,
        filters: Optional[Dict[str, Any]] = None,
        page: int = 1,
        per_page: int = 20,
        community_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        List accessible workflows with pagination and filters.

        Args:
            entity_id: Entity ID for filtering
            user_id: Requesting user ID
            filters: Optional filters (status, tags, search)
            page: Page number (1-indexed)
            per_page: Items per page
            community_id: Optional community ID for context

        Returns:
            Paginated list of workflows
        """
        try:
            filters = filters or {}

            # Step 1: Get accessible workflow IDs
            accessible_ids = await self.permission_service.list_workflows_for_user(
                user_id=user_id,
                entity_id=entity_id,
                permission="can_view",
                community_id=community_id
            )

            if not accessible_ids:
                return {
                    "workflows": [],
                    "total": 0,
                    "page": page,
                    "per_page": per_page,
                    "total_pages": 0
                }

            # Step 2: Build query with filters
            where_clauses = ["workflow_id = ANY(%s)"]
            params = [accessible_ids]

            # Filter by status
            if "status" in filters:
                where_clauses.append("status = %s")
                params.append(filters["status"])

            # Filter by search term (name or description)
            if "search" in filters:
                where_clauses.append("(name ILIKE %s OR description ILIKE %s)")
                search_term = f"%{filters['search']}%"
                params.extend([search_term, search_term])

            where_clause = " AND ".join(where_clauses)

            # Step 3: Count total
            count_query = f"SELECT COUNT(*) FROM workflows WHERE {where_clause}"
            count_result = self.dal.executesql(count_query, params)
            total = count_result[0][0] if count_result else 0

            # Step 4: Fetch paginated results
            offset = (page - 1) * per_page

            query = f"""
                SELECT workflow_id, name, description, status, is_active,
                       trigger_type, execution_count, success_count, failure_count,
                       created_at, updated_at, last_executed_at
                FROM workflows
                WHERE {where_clause}
                ORDER BY updated_at DESC
                LIMIT %s OFFSET %s
            """

            params.extend([per_page, offset])
            results = self.dal.executesql(query, params)

            workflows = []
            for row in results:
                workflows.append({
                    "workflow_id": str(row[0]),
                    "name": row[1],
                    "description": row[2],
                    "status": row[3],
                    "is_active": row[4],
                    "trigger_type": row[5],
                    "execution_count": row[6],
                    "success_count": row[7],
                    "failure_count": row[8],
                    "created_at": row[9].isoformat(),
                    "updated_at": row[10].isoformat(),
                    "last_executed_at": row[11].isoformat() if row[11] else None,
                })

            total_pages = (total + per_page - 1) // per_page

            self.logger.authz(
                action="list_workflows",
                user=str(user_id),
                community=str(community_id) if community_id else "",
                result="SUCCESS",
                extra={
                    "entity_id": entity_id,
                    "total": total,
                    "page": page,
                }
            )

            return {
                "workflows": workflows,
                "total": total,
                "page": page,
                "per_page": per_page,
                "total_pages": total_pages,
            }

        except Exception as e:
            self.logger.error(
                f"Failed to list workflows: {describe_db_error(e)}",
                extra={
                    "event_type": "ERROR",
                    "action": "list_workflows",
                    "user": str(user_id),
                    "entity_id": entity_id,
                    "result": "FAILURE",
                }
            )
            raise WorkflowServiceException(f"Failed to list workflows: {_failure_detail(e)}")

    async def publish_workflow(
        self,
        workflow_id: str,
        user_id: int,
        community_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Publish and activate workflow after validation.

        Args:
            workflow_id: Workflow UUID
            user_id: Publishing user ID
            community_id: Optional community ID for context

        Returns:
            Published workflow data

        Raises:
            WorkflowPermissionException: If user lacks edit permission
            WorkflowServiceException: If validation fails
        """
        try:
            # Step 1: Check edit permission
            can_edit = await self.permission_service.check_permission(
                workflow_id=workflow_id,
                user_id=user_id,
                permission_type="can_edit",
                community_id=community_id
            )

            if not can_edit:
                self.logger.authz(
                    action="publish_workflow",
                    user=str(user_id),
                    community=str(community_id) if community_id else "",
                    result="DENIED",
                    extra={"workflow_id": workflow_id}
                )
                raise WorkflowPermissionException(workflow_id, "can_edit")

            # Step 2: Validate workflow
            validation_result = await self.validate_workflow(workflow_id)

            if not validation_result["is_valid"]:
                raise WorkflowServiceException(
                    f"Workflow validation failed: {', '.join(validation_result['errors'])}",
                    status_code=400
                )

            # Step 3: Update status to published and activate
            update_query = """
                UPDATE workflows
                SET status = %s, is_active = true, updated_by = %s
                WHERE workflow_id = %s
                RETURNING workflow_id
            """

            result = self.dal.executesql(
                update_query,
                [WorkflowStatus.ACTIVE.value, user_id, workflow_id]
            )

            if not result or len(result) == 0:
                raise WorkflowNotFoundException(workflow_id)

            # Step 4: Log audit event
            await self._log_audit(
                workflow_id=workflow_id,
                action="published",
                action_by=user_id,
                changes={"status": WorkflowStatus.ACTIVE.value, "is_active": True},
                metadata={"validation": "passed"}
            )

            self.logger.audit(
                action="publish_workflow",
                user=str(user_id),
                community=str(community_id) if community_id else "",
                result="SUCCESS",
                extra={"workflow_id": workflow_id}
            )

            # Step 5: Return published workflow
            return await self.get_workflow(workflow_id, user_id, community_id)

        except (WorkflowPermissionException, WorkflowNotFoundException):
            raise
        except Exception as e:
            self.logger.error(
                f"Failed to publish workflow: {describe_db_error(e)}",
                extra={
                    "event_type": "ERROR",
                    "action": "publish_workflow",
                    "user": str(user_id),
                    "workflow_id": workflow_id,
                    "result": "FAILURE",
                }
            )
            raise WorkflowServiceException(f"Failed to publish workflow: {_failure_detail(e)}")

    async def validate_workflow(
        self,
        workflow_id: str
    ) -> Dict[str, Any]:
        """
        Validate workflow structure and configuration.

        Args:
            workflow_id: Workflow UUID

        Returns:
            Validation result with errors/warnings
        """
        try:
            # Step 1: Fetch workflow
            query = """
                SELECT nodes, connections, trigger_type, trigger_config, name, description
                FROM workflows
                WHERE workflow_id = %s
            """

            result = self.dal.executesql(query, [workflow_id])

            if not result or len(result) == 0:
                raise WorkflowNotFoundException(workflow_id)

            row = result[0]

            # Step 2: Build WorkflowDefinition for validation
            metadata_data = {
                "workflow_id": workflow_id,
                "name": row[4],
                "description": row[5],
                "author_id": "0",
                "community_id": "0",
            }

            metadata = WorkflowMetadata.from_dict(metadata_data)

            workflow_def = WorkflowDefinition(
                metadata=metadata,
                nodes=row[0],  # JSONB
                connections=[WorkflowConnection.from_dict(c) for c in row[1]],  # JSONB array
            )

            # Step 3: Run validation
            validation_result = self.validation_service.validate_workflow(workflow_def)

            self.logger.info(
                f"Workflow validation completed: {workflow_id}",
                extra={
                    "event_type": "AUDIT",
                    "action": "validate_workflow",
                    "workflow_id": workflow_id,
                    "is_valid": validation_result.is_valid,
                    "error_count": len(validation_result.errors),
                }
            )

            return validation_result.to_dict()

        except WorkflowNotFoundException:
            raise
        except Exception as e:
            self.logger.error(
                f"Failed to validate workflow: {describe_db_error(e)}",
                extra={
                    "event_type": "ERROR",
                    "action": "validate_workflow",
                    "workflow_id": workflow_id,
                    "result": "FAILURE",
                }
            )
            raise WorkflowServiceException(f"Failed to validate workflow: {_failure_detail(e)}")

    # =========================================================================
    # Private Helper Methods
    # =========================================================================

    async def _log_audit(
        self,
        workflow_id: str,
        action: str,
        action_by: int,
        changes: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Log audit event to workflow_audit_log table.

        Args:
            workflow_id: Workflow UUID
            action: Action performed (created, updated, published, etc.)
            action_by: User ID performing action
            changes: Optional changes made
            metadata: Optional metadata
        """
        try:
            insert_query = """
                INSERT INTO workflow_audit_log
                (workflow_id, action, action_by, changes, metadata)
                VALUES (%s, %s, %s, %s, %s)
            """

            params = [
                workflow_id,
                action,
                action_by,
                json.dumps(changes) if changes else None,
                json.dumps(metadata) if metadata else None,
            ]

            self.dal.executesql(insert_query, params)

        except Exception as e:
            self.logger.error(
                f"Failed to log audit event: {describe_db_error(e)}",
                extra={
                    "event_type": "ERROR",
                    "action": "audit_log",
                    "workflow_id": workflow_id,
                }
            )
            # Don't raise - audit logging failure shouldn't fail the main operation
