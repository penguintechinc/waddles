"""
Workflow Permission Service
=============================

Granular permission management for workflows using the workflow_permissions table.

Supports:
- User-level permissions
- Role-based permissions
- Entity-level permissions
- Owner bypass (automatic full permissions)

Permission Types:
- can_view: View workflow details and executions
- can_edit: Modify workflow definition and settings
- can_execute: Trigger workflow execution
- can_delete: Delete workflow and associated data
- can_manage_permissions: Manage permissions for other users

Permission Targets:
- user: Specific user_id
- role: Role-based (role_id)
- entity: Entity-level (entity_id)
"""

from dataclasses import dataclass
from typing import Optional, List, Dict, Any
from uuid import UUID
from flask_core import describe_db_error


@dataclass(slots=True)
class PermissionInfo:
    """Information about a set of permissions"""
    can_view: bool = False
    can_edit: bool = False
    can_execute: bool = False
    can_delete: bool = False
    can_manage_permissions: bool = False

    def to_dict(self) -> Dict[str, bool]:
        """Convert to dictionary"""
        return {
            "can_view": self.can_view,
            "can_edit": self.can_edit,
            "can_execute": self.can_execute,
            "can_delete": self.can_delete,
            "can_manage_permissions": self.can_manage_permissions,
        }

    def has_any_permission(self) -> bool:
        """Check if any permission is granted"""
        return any([
            self.can_view,
            self.can_edit,
            self.can_execute,
            self.can_delete,
            self.can_manage_permissions,
        ])

    def __bool__(self) -> bool:
        """Allow boolean checks"""
        return self.has_any_permission()


@dataclass(slots=True)
class GrantResult:
    """Result of a permission grant operation"""
    success: bool
    message: str
    workflow_id: Optional[str] = None
    target_type: Optional[str] = None
    target_id: Optional[int] = None
    error: Optional[str] = None


class PermissionService:
    """
    Granular permission management for workflows.

    Owner has all permissions automatically without database entries.
    Supports cascading permissions through roles and entity-level access.
    """

    def __init__(self, dal, logger):
        """
        Initialize permission service.

        Args:
            dal: AsyncDAL instance for database access
            logger: AAALogger instance for logging
        """
        self.dal = dal
        self.logger = logger

    async def check_permission(
        self,
        workflow_id: str,
        user_id: int,
        permission_type: str,
        community_id: int = None,
    ) -> bool:
        """
        Check if a user has a specific permission for a workflow.

        Owner check is performed first (automatic full permissions).
        Then checks user, role, and entity level permissions.

        Args:
            workflow_id: UUID of the workflow
            user_id: Hub user ID to check
            permission_type: One of: can_view, can_edit, can_execute, can_delete, can_manage_permissions
            community_id: Optional community ID for additional context

        Returns:
            True if user has permission, False otherwise
        """
        try:
            if not permission_type.startswith("can_"):
                self.logger.error(
                    f"Invalid permission type: {permission_type}",
                    result="FAILURE"
                )
                return False

            # Check if user is the owner (owner has all permissions)
            is_owner = await self._is_workflow_owner(workflow_id, user_id)
            if is_owner:
                self.logger.audit(
                    action="check_permission_owner",
                    user=str(user_id),
                    community="",
                    result="ALLOWED",
                    extra={"workflow_id": workflow_id, "permission": permission_type}
                )
                return True

            # Check user-level permission
            user_perm = await self._get_permission(workflow_id, "user", user_id)
            if user_perm and getattr(user_perm, permission_type, False):
                self.logger.authz(
                    action="check_permission",
                    user=str(user_id),
                    community=str(community_id) if community_id else "",
                    result="ALLOWED",
                    extra={"workflow_id": workflow_id, "target_type": "user", "permission": permission_type}
                )
                return True

            # Check role-level permissions (if user has roles)
            role_ids = await self._get_user_roles(user_id, community_id)
            for role_id in role_ids:
                role_perm = await self._get_permission(workflow_id, "role", role_id)
                if role_perm and getattr(role_perm, permission_type, False):
                    self.logger.authz(
                        action="check_permission",
                        user=str(user_id),
                        community=str(community_id) if community_id else "",
                        result="ALLOWED",
                        extra={"workflow_id": workflow_id, "target_type": "role", "target_id": role_id, "permission": permission_type}
                    )
                    return True

            # Check entity-level permissions (if workflow has entity_id)
            entity_perm = await self._get_workflow_entity_permission(workflow_id, permission_type)
            if entity_perm:
                self.logger.authz(
                    action="check_permission",
                    user=str(user_id),
                    community=str(community_id) if community_id else "",
                    result="ALLOWED",
                    extra={"workflow_id": workflow_id, "target_type": "entity", "permission": permission_type}
                )
                return True

            self.logger.authz(
                action="check_permission",
                user=str(user_id),
                community=str(community_id) if community_id else "",
                result="DENIED",
                extra={"workflow_id": workflow_id, "permission": permission_type}
            )
            return False

        except Exception as e:
            self.logger.error(
                f"Error checking permission: {describe_db_error(e)}",
                extra={"workflow_id": workflow_id, "user_id": user_id, "permission": permission_type}
            )
            return False

    async def grant_permission(
        self,
        workflow_id: str,
        target_type: str,
        target_id: int,
        permissions_dict: Dict[str, bool],
        granted_by: int = None,
    ) -> GrantResult:
        """
        Grant permissions to a user, role, or entity for a workflow.

        Args:
            workflow_id: UUID of the workflow
            target_type: One of: user, role, entity
            target_id: ID of target (user_id, role_id, or entity_id)
            permissions_dict: Dictionary of permission_type -> bool
            granted_by: User ID who is granting permissions

        Returns:
            GrantResult with success status and message
        """
        try:
            # Validate target_type
            if target_type not in ["user", "role", "entity"]:
                error_msg = f"Invalid target_type: {target_type}"
                self.logger.error(error_msg, result="FAILURE")
                return GrantResult(
                    success=False,
                    message=error_msg,
                    error=error_msg
                )

            # Validate permission keys
            valid_permissions = {
                "can_view", "can_edit", "can_execute", "can_delete", "can_manage_permissions"
            }
            for key in permissions_dict.keys():
                if key not in valid_permissions:
                    error_msg = f"Invalid permission key: {key}"
                    self.logger.error(error_msg, result="FAILURE")
                    return GrantResult(
                        success=False,
                        message=error_msg,
                        error=error_msg
                    )

            # Check if permission record exists
            existing = await self._get_permission(workflow_id, target_type, target_id)

            if existing:
                # Update existing permission
                update_query = """
                    UPDATE workflow_permissions
                    SET can_view = %s,
                        can_edit = %s,
                        can_execute = %s,
                        can_delete = %s,
                        can_manage_permissions = %s,
                        granted_by = %s,
                        granted_at = CURRENT_TIMESTAMP
                    WHERE workflow_id = %s
                        AND permission_type = %s
                        AND target_id = %s
                """
                params = [
                    permissions_dict.get("can_view", existing.can_view),
                    permissions_dict.get("can_edit", existing.can_edit),
                    permissions_dict.get("can_execute", existing.can_execute),
                    permissions_dict.get("can_delete", existing.can_delete),
                    permissions_dict.get("can_manage_permissions", existing.can_manage_permissions),
                    granted_by,
                    workflow_id,
                    target_type,
                    target_id,
                ]
                self.dal.executesql(update_query, params)
                result_message = "Permission updated"
            else:
                # Insert new permission
                insert_query = """
                    INSERT INTO workflow_permissions
                    (workflow_id, permission_type, target_id, can_view, can_edit, can_execute, can_delete, can_manage_permissions, granted_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
                params = [
                    workflow_id,
                    target_type,
                    target_id,
                    permissions_dict.get("can_view", False),
                    permissions_dict.get("can_edit", False),
                    permissions_dict.get("can_execute", False),
                    permissions_dict.get("can_delete", False),
                    permissions_dict.get("can_manage_permissions", False),
                    granted_by,
                ]
                self.dal.executesql(insert_query, params)
                result_message = "Permission granted"

            self.logger.audit(
                action="grant_permission",
                user=str(granted_by) if granted_by else "system",
                community="",
                result="SUCCESS",
                extra={
                    "workflow_id": workflow_id,
                    "target_type": target_type,
                    "target_id": target_id,
                    "permissions": permissions_dict
                }
            )

            return GrantResult(
                success=True,
                message=result_message,
                workflow_id=workflow_id,
                target_type=target_type,
                target_id=target_id,
            )

        except Exception as e:
            error_msg = f"Failed to grant permission: {describe_db_error(e)}"
            self.logger.error(
                error_msg,
                extra={"workflow_id": workflow_id, "target_type": target_type, "target_id": target_id}
            )
            return GrantResult(
                success=False,
                message=error_msg,
                workflow_id=workflow_id,
                target_type=target_type,
                target_id=target_id,
                error=describe_db_error(e)
            )

    async def revoke_permission(
        self,
        workflow_id: str,
        target_type: str,
        target_id: int,
        revoked_by: int = None,
    ) -> GrantResult:
        """
        Revoke all permissions for a target on a workflow.

        Args:
            workflow_id: UUID of the workflow
            target_type: One of: user, role, entity
            target_id: ID of target
            revoked_by: User ID who is revoking permissions

        Returns:
            GrantResult with success status and message
        """
        try:
            # Validate target_type
            if target_type not in ["user", "role", "entity"]:
                error_msg = f"Invalid target_type: {target_type}"
                self.logger.error(error_msg, result="FAILURE")
                return GrantResult(
                    success=False,
                    message=error_msg,
                    error=error_msg
                )

            # Delete the permission record
            delete_query = """
                DELETE FROM workflow_permissions
                WHERE workflow_id = %s
                    AND permission_type = %s
                    AND target_id = %s
            """
            self.dal.executesql(delete_query, [workflow_id, target_type, target_id])

            self.logger.audit(
                action="revoke_permission",
                user=str(revoked_by) if revoked_by else "system",
                community="",
                result="SUCCESS",
                extra={
                    "workflow_id": workflow_id,
                    "target_type": target_type,
                    "target_id": target_id,
                }
            )

            return GrantResult(
                success=True,
                message="Permission revoked",
                workflow_id=workflow_id,
                target_type=target_type,
                target_id=target_id,
            )

        except Exception as e:
            error_msg = f"Failed to revoke permission: {describe_db_error(e)}"
            self.logger.error(
                error_msg,
                extra={"workflow_id": workflow_id, "target_type": target_type, "target_id": target_id}
            )
            return GrantResult(
                success=False,
                message=error_msg,
                workflow_id=workflow_id,
                target_type=target_type,
                target_id=target_id,
                error=describe_db_error(e)
            )

    async def get_user_permissions(
        self,
        workflow_id: str,
        user_id: int,
        community_id: int = None,
    ) -> PermissionInfo:
        """
        Get all permissions for a user on a workflow.

        Combines user-level, role-level, and entity-level permissions.
        Returns aggregated permissions (OR logic - any matching permission grants access).

        Args:
            workflow_id: UUID of the workflow
            user_id: Hub user ID
            community_id: Optional community ID

        Returns:
            PermissionInfo with all permission flags
        """
        try:
            # Owner has all permissions
            is_owner = await self._is_workflow_owner(workflow_id, user_id)
            if is_owner:
                return PermissionInfo(
                    can_view=True,
                    can_edit=True,
                    can_execute=True,
                    can_delete=True,
                    can_manage_permissions=True,
                )

            # Start with empty permissions
            permissions = PermissionInfo()

            # Get user-level permissions
            user_perm = await self._get_permission(workflow_id, "user", user_id)
            if user_perm:
                permissions.can_view = permissions.can_view or user_perm.can_view
                permissions.can_edit = permissions.can_edit or user_perm.can_edit
                permissions.can_execute = permissions.can_execute or user_perm.can_execute
                permissions.can_delete = permissions.can_delete or user_perm.can_delete
                permissions.can_manage_permissions = permissions.can_manage_permissions or user_perm.can_manage_permissions

            # Get role-level permissions
            role_ids = await self._get_user_roles(user_id, community_id)
            for role_id in role_ids:
                role_perm = await self._get_permission(workflow_id, "role", role_id)
                if role_perm:
                    permissions.can_view = permissions.can_view or role_perm.can_view
                    permissions.can_edit = permissions.can_edit or role_perm.can_edit
                    permissions.can_execute = permissions.can_execute or role_perm.can_execute
                    permissions.can_delete = permissions.can_delete or role_perm.can_delete
                    permissions.can_manage_permissions = permissions.can_manage_permissions or role_perm.can_manage_permissions

            # Get entity-level permissions
            entity_perms = await self._get_all_workflow_entity_permissions(workflow_id)
            if entity_perms:
                permissions.can_view = permissions.can_view or entity_perms.can_view
                permissions.can_edit = permissions.can_edit or entity_perms.can_edit
                permissions.can_execute = permissions.can_execute or entity_perms.can_execute
                permissions.can_delete = permissions.can_delete or entity_perms.can_delete
                permissions.can_manage_permissions = permissions.can_manage_permissions or entity_perms.can_manage_permissions

            return permissions

        except Exception as e:
            self.logger.error(
                f"Error getting user permissions: {describe_db_error(e)}",
                extra={"workflow_id": workflow_id, "user_id": user_id}
            )
            return PermissionInfo()

    async def list_workflows_for_user(
        self,
        user_id: int,
        entity_id: int,
        permission: str = "view",
        community_id: int = None,
    ) -> List[str]:
        """
        List all workflows a user can access with a specific permission.

        Includes:
        - Workflows owned by the user
        - Workflows with user-level permissions
        - Workflows with role-level permissions
        - Workflows with entity-level permissions

        Args:
            user_id: Hub user ID
            entity_id: Entity ID for context
            permission: Permission type (can_view, can_edit, can_execute, can_delete, can_manage_permissions)
            community_id: Optional community ID

        Returns:
            List of workflow UUIDs accessible to the user
        """
        try:
            # Validate permission
            valid_permissions = {
                "can_view", "can_edit", "can_execute", "can_delete", "can_manage_permissions"
            }
            if permission not in valid_permissions:
                self.logger.error(
                    f"Invalid permission: {permission}",
                    result="FAILURE"
                )
                return []

            # Build query to get workflows where user has permission
            query = f"""
                SELECT DISTINCT w.workflow_id
                FROM workflows w
                WHERE w.entity_id = %s
                AND (
                    -- Owner has all permissions
                    w.created_by = %s
                    -- User-level permission
                    OR EXISTS (
                        SELECT 1 FROM workflow_permissions wp
                        WHERE wp.workflow_id = w.workflow_id
                        AND wp.permission_type = 'user'
                        AND wp.target_id = %s
                        AND wp.{permission} = true
                    )
                    -- Role-level permission
                    OR EXISTS (
                        SELECT 1 FROM workflow_permissions wp
                        JOIN user_roles ur ON ur.role_id = wp.target_id
                        WHERE wp.workflow_id = w.workflow_id
                        AND wp.permission_type = 'role'
                        AND ur.user_id = %s
                        AND wp.{permission} = true
                    )
                    -- Entity-level permission
                    OR EXISTS (
                        SELECT 1 FROM workflow_permissions wp
                        WHERE wp.workflow_id = w.workflow_id
                        AND wp.permission_type = 'entity'
                        AND wp.target_id = w.entity_id
                        AND wp.{permission} = true
                    )
                )
                ORDER BY w.created_at DESC
            """

            results = self.dal.executesql(query, [entity_id, user_id, user_id, user_id])
            workflow_ids = [str(row[0]) for row in results if row]

            self.logger.authz(
                action="list_workflows_for_user",
                user=str(user_id),
                community=str(community_id) if community_id else "",
                result="SUCCESS",
                extra={"entity_id": entity_id, "permission": permission, "count": len(workflow_ids)}
            )

            return workflow_ids

        except Exception as e:
            self.logger.error(
                f"Error listing workflows for user: {describe_db_error(e)}",
                extra={"user_id": user_id, "entity_id": entity_id, "permission": permission}
            )
            return []

    # =========================================================================
    # Private Helper Methods
    # =========================================================================

    async def _is_workflow_owner(self, workflow_id: str, user_id: int) -> bool:
        """Check if user is the workflow owner."""
        try:
            result = self.dal.executesql(
                "SELECT created_by FROM workflows WHERE workflow_id = %s",
                [workflow_id]
            )
            if result and len(result) > 0:
                return result[0][0] == user_id
            return False
        except Exception as e:
            self.logger.error(f"Error checking workflow owner: {describe_db_error(e)}")
            return False

    async def _get_permission(
        self,
        workflow_id: str,
        target_type: str,
        target_id: int,
    ) -> Optional[PermissionInfo]:
        """Get a permission record from the database."""
        try:
            result = self.dal.executesql(
                """SELECT can_view, can_edit, can_execute, can_delete, can_manage_permissions
                   FROM workflow_permissions
                   WHERE workflow_id = %s AND permission_type = %s AND target_id = %s""",
                [workflow_id, target_type, target_id]
            )

            if result and len(result) > 0:
                row = result[0]
                return PermissionInfo(
                    can_view=row[0],
                    can_edit=row[1],
                    can_execute=row[2],
                    can_delete=row[3],
                    can_manage_permissions=row[4],
                )
            return None
        except Exception as e:
            self.logger.error(f"Error getting permission: {describe_db_error(e)}")
            return None

    async def _get_user_roles(self, user_id: int, community_id: int = None) -> List[int]:
        """Get all role IDs for a user in a community."""
        try:
            if community_id:
                result = self.dal.executesql(
                    """SELECT role_id FROM user_roles
                       WHERE user_id = %s AND community_id = %s""",
                    [user_id, community_id]
                )
            else:
                result = self.dal.executesql(
                    "SELECT role_id FROM user_roles WHERE user_id = %s",
                    [user_id]
                )

            return [row[0] for row in result] if result else []
        except Exception as e:
            self.logger.error(f"Error getting user roles: {describe_db_error(e)}")
            return []

    async def _get_workflow_entity_permission(
        self,
        workflow_id: str,
        permission_type: str,
    ) -> Optional[PermissionInfo]:
        """Get entity-level permission for a workflow."""
        try:
            # Get the entity_id of the workflow first
            result = self.dal.executesql(
                "SELECT entity_id FROM workflows WHERE workflow_id = %s",
                [workflow_id]
            )

            if not result or len(result) == 0:
                return None

            entity_id = result[0][0]

            # Get entity-level permission
            perm_result = self.dal.executesql(
                """SELECT can_view, can_edit, can_execute, can_delete, can_manage_permissions
                   FROM workflow_permissions
                   WHERE workflow_id = %s AND permission_type = 'entity' AND target_id = %s""",
                [workflow_id, entity_id]
            )

            if perm_result and len(perm_result) > 0:
                row = perm_result[0]
                return PermissionInfo(
                    can_view=row[0],
                    can_edit=row[1],
                    can_execute=row[2],
                    can_delete=row[3],
                    can_manage_permissions=row[4],
                )
            return None
        except Exception as e:
            self.logger.error(f"Error getting workflow entity permission: {describe_db_error(e)}")
            return None

    async def _get_all_workflow_entity_permissions(
        self,
        workflow_id: str,
    ) -> Optional[PermissionInfo]:
        """Get entity-level permissions for a workflow (check all entity permissions)."""
        try:
            result = self.dal.executesql(
                """SELECT can_view, can_edit, can_execute, can_delete, can_manage_permissions
                   FROM workflow_permissions
                   WHERE workflow_id = %s AND permission_type = 'entity'
                   LIMIT 1""",
                [workflow_id]
            )

            if result and len(result) > 0:
                row = result[0]
                return PermissionInfo(
                    can_view=row[0],
                    can_edit=row[1],
                    can_execute=row[2],
                    can_delete=row[3],
                    can_manage_permissions=row[4],
                )
            return None
        except Exception as e:
            self.logger.error(f"Error getting entity permissions: {describe_db_error(e)}")
            return None
