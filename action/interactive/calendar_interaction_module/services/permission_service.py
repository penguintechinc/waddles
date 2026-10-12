"""
Permission Service - Role-based access control for calendar events
Implements permission checks for event creation, editing, deletion, and approval workflows
"""
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class PermissionService:
    """
    Permission service for event management.

    Features:
    - Role-based access control (admin, moderator, vip, member)
    - Permission checks for create, edit, delete operations
    - Approval workflow logic
    - AAA (Auth/Authz/Audit) logging
    """

    def __init__(self, dal):
        """
        Initialize permission service with database abstraction layer.

        Args:
            dal: Database abstraction layer (AsyncDAL)
        """
        self.dal = dal

    async def can_create_event(self, user_context: Dict[str, Any], community_id: int) -> bool:
        """
        Check if user can create events based on create_permission setting.

        Args:
            user_context: User information (user_id, username, platform, platform_user_id, role)
            community_id: Community ID

        Returns:
            True if user can create events, False otherwise

        AAA Logging: AUTHZ
        """
        try:
            # Get user role
            user_role = await self.get_user_role(user_context, community_id)

            # Get permissions
            permissions = await self.get_permissions(community_id)
            if not permissions:
                logger.warning(
                    f"[AUTHZ] Permissions not found for community {community_id}, "
                    f"defaulting to admin_only"
                )
                return user_role in ['admin', 'super_admin']

            create_permission = permissions.get('create_permission', 'admin_mod')

            # Check permission level
            allowed = False
            if create_permission == 'admin_only':
                allowed = user_role in ['admin', 'super_admin']
            elif create_permission == 'admin_mod':
                allowed = user_role in ['admin', 'super_admin', 'moderator']
            elif create_permission == 'admin_mod_vip':
                allowed = user_role in ['admin', 'super_admin', 'moderator', 'vip']
            elif create_permission == 'all_members':
                allowed = True  # All roles can create
            else:
                logger.warning(
                    f"[AUTHZ] Unknown create_permission value: {create_permission}"
                )
                allowed = user_role in ['admin', 'super_admin']

            if not allowed:
                logger.info(
                    f"[AUTHZ] DENIED: User {user_context.get('username')} "
                    f"(role={user_role}) cannot create events (permission={create_permission})"
                )
            else:
                logger.info(
                    f"[AUTHZ] ALLOWED: User {user_context.get('username')} "
                    f"(role={user_role}) can create events"
                )

            return allowed

        except Exception as e:
            logger.error(f"[AUTHZ] ERROR checking create permission: {e}")
            return False

    async def can_edit_event(self, user_context: Dict[str, Any], event_id: int) -> bool:
        """
        Check if user can edit event (own vs all based on permissions).

        Args:
            user_context: User information
            event_id: Event ID to check

        Returns:
            True if user can edit event, False otherwise

        AAA Logging: AUTHZ
        """
        try:
            # Get event details
            event_query = """
                SELECT id, community_id, created_by_user_id, created_by_platform_user_id
                FROM calendar_events WHERE id = $1
            """
            result = await self.dal.execute(event_query, [event_id])

            if not result or len(result) == 0:
                logger.warning(f"[AUTHZ] Event {event_id} not found for edit check")
                return False

            event = result[0]
            community_id = event['community_id']
            event_creator_user_id = event['created_by_user_id']
            event_creator_platform_user_id = event['created_by_platform_user_id']

            # Get user role
            user_role = await self.get_user_role(user_context, community_id)
            user_id = user_context.get('user_id')
            user_platform_user_id = user_context.get('platform_user_id')

            # Get permissions
            permissions = await self.get_permissions(community_id)
            if not permissions:
                logger.warning(
                    f"[AUTHZ] Permissions not found for community {community_id}, "
                    f"defaulting to admin_only for edit_all_events"
                )
                # Default: admins can edit all, others can edit own
                is_owner = (user_id and user_id == event_creator_user_id) or \
                           (user_platform_user_id and user_platform_user_id == event_creator_platform_user_id)
                if is_owner and permissions.get('edit_own_events', True):
                    return True
                return user_role in ['admin', 'super_admin']

            # Check if user is event creator
            is_owner = (user_id and user_id == event_creator_user_id) or \
                       (user_platform_user_id and user_platform_user_id == event_creator_platform_user_id)

            # Can edit own events?
            if is_owner and permissions.get('edit_own_events', True):
                logger.info(
                    f"[AUTHZ] ALLOWED: User {user_context.get('username')} "
                    f"can edit own event {event_id}"
                )
                return True

            # Can edit all events?
            edit_all_permission = permissions.get('edit_all_events', 'admin_only')
            allowed = False

            if edit_all_permission == 'admin_only':
                allowed = user_role in ['admin', 'super_admin']
            elif edit_all_permission == 'admin_mod':
                allowed = user_role in ['admin', 'super_admin', 'moderator']
            elif edit_all_permission == 'none':
                allowed = False
            else:
                logger.warning(
                    f"[AUTHZ] Unknown edit_all_events value: {edit_all_permission}"
                )
                allowed = user_role in ['admin', 'super_admin']

            if not allowed:
                logger.info(
                    f"[AUTHZ] DENIED: User {user_context.get('username')} "
                    f"(role={user_role}) cannot edit event {event_id} "
                    f"(not owner, edit_all_permission={edit_all_permission})"
                )
            else:
                logger.info(
                    f"[AUTHZ] ALLOWED: User {user_context.get('username')} "
                    f"(role={user_role}) can edit all events"
                )

            return allowed

        except Exception as e:
            logger.error(f"[AUTHZ] ERROR checking edit permission: {e}")
            return False

    async def can_delete_event(self, user_context: Dict[str, Any], event_id: int) -> bool:
        """
        Check if user can delete event (own vs all based on permissions).

        Args:
            user_context: User information
            event_id: Event ID to check

        Returns:
            True if user can delete event, False otherwise

        AAA Logging: AUTHZ
        """
        try:
            # Get event details
            event_query = """
                SELECT id, community_id, created_by_user_id, created_by_platform_user_id
                FROM calendar_events WHERE id = $1
            """
            result = await self.dal.execute(event_query, [event_id])

            if not result or len(result) == 0:
                logger.warning(f"[AUTHZ] Event {event_id} not found for delete check")
                return False

            event = result[0]
            community_id = event['community_id']
            event_creator_user_id = event['created_by_user_id']
            event_creator_platform_user_id = event['created_by_platform_user_id']

            # Get user role
            user_role = await self.get_user_role(user_context, community_id)
            user_id = user_context.get('user_id')
            user_platform_user_id = user_context.get('platform_user_id')

            # Get permissions
            permissions = await self.get_permissions(community_id)
            if not permissions:
                logger.warning(
                    f"[AUTHZ] Permissions not found for community {community_id}, "
                    f"defaulting to admin_only for delete_all_events"
                )
                # Default: admins can delete all, others can delete own
                is_owner = (user_id and user_id == event_creator_user_id) or \
                           (user_platform_user_id and user_platform_user_id == event_creator_platform_user_id)
                if is_owner and permissions.get('delete_own_events', True):
                    return True
                return user_role in ['admin', 'super_admin']

            # Check if user is event creator
            is_owner = (user_id and user_id == event_creator_user_id) or \
                       (user_platform_user_id and user_platform_user_id == event_creator_platform_user_id)

            # Can delete own events?
            if is_owner and permissions.get('delete_own_events', True):
                logger.info(
                    f"[AUTHZ] ALLOWED: User {user_context.get('username')} "
                    f"can delete own event {event_id}"
                )
                return True

            # Can delete all events?
            delete_all_permission = permissions.get('delete_all_events', 'admin_only')
            allowed = False

            if delete_all_permission == 'admin_only':
                allowed = user_role in ['admin', 'super_admin']
            elif delete_all_permission == 'admin_mod':
                allowed = user_role in ['admin', 'super_admin', 'moderator']
            elif delete_all_permission == 'none':
                allowed = False
            else:
                logger.warning(
                    f"[AUTHZ] Unknown delete_all_events value: {delete_all_permission}"
                )
                allowed = user_role in ['admin', 'super_admin']

            if not allowed:
                logger.info(
                    f"[AUTHZ] DENIED: User {user_context.get('username')} "
                    f"(role={user_role}) cannot delete event {event_id} "
                    f"(not owner, delete_all_permission={delete_all_permission})"
                )
            else:
                logger.info(
                    f"[AUTHZ] ALLOWED: User {user_context.get('username')} "
                    f"(role={user_role}) can delete all events"
                )

            return allowed

        except Exception as e:
            logger.error(f"[AUTHZ] ERROR checking delete permission: {e}")
            return False

    async def needs_approval(self, user_context: Dict[str, Any], community_id: int) -> bool:
        """
        Determine if event needs approval based on user role and auto_approve settings.

        Args:
            user_context: User information
            community_id: Community ID

        Returns:
            True if event needs approval, False if auto-approved

        Approval Logic:
            - If require_approval=False, return False (no approval needed)
            - If user is super_admin or admin, check auto_approve_admins
            - If user is moderator, check auto_approve_mods (mods need approval by default!)
            - If user is vip, check auto_approve_vips
            - If user is member, check auto_approve_all
            - If auto_approve_X is True, return False (auto-approved)
            - Otherwise return True (needs approval)

        AAA Logging: AUTHZ
        """
        try:
            # Get permissions
            permissions = await self.get_permissions(community_id)
            if not permissions:
                logger.warning(
                    f"[AUTHZ] Permissions not found for community {community_id}, "
                    f"defaulting to require_approval=True"
                )
                return True

            # If approval not required, return False
            require_approval = permissions.get('require_approval', True)
            if not require_approval:
                logger.info(
                    f"[AUTHZ] Event does not require approval for community {community_id}"
                )
                return False

            # Get user role
            user_role = await self.get_user_role(user_context, community_id)

            # Check auto-approval based on role
            needs_approval = True

            if user_role in ['super_admin', 'admin']:
                auto_approve = permissions.get('auto_approve_admins', True)
                needs_approval = not auto_approve
                logger.info(
                    f"[AUTHZ] User {user_context.get('username')} is {user_role}, "
                    f"auto_approve_admins={auto_approve}, needs_approval={needs_approval}"
                )
            elif user_role == 'moderator':
                auto_approve = permissions.get('auto_approve_mods', False)
                needs_approval = not auto_approve
                logger.info(
                    f"[AUTHZ] User {user_context.get('username')} is moderator, "
                    f"auto_approve_mods={auto_approve}, needs_approval={needs_approval}"
                )
            elif user_role == 'vip':
                auto_approve = permissions.get('auto_approve_vips', False)
                needs_approval = not auto_approve
                logger.info(
                    f"[AUTHZ] User {user_context.get('username')} is vip, "
                    f"auto_approve_vips={auto_approve}, needs_approval={needs_approval}"
                )
            else:  # member
                auto_approve = permissions.get('auto_approve_all', False)
                needs_approval = not auto_approve
                logger.info(
                    f"[AUTHZ] User {user_context.get('username')} is {user_role}, "
                    f"auto_approve_all={auto_approve}, needs_approval={needs_approval}"
                )

            return needs_approval

        except Exception as e:
            logger.error(f"[AUTHZ] ERROR checking approval requirement: {e}")
            return True  # Default to requiring approval on error

    async def get_user_role(self, user_context: Dict[str, Any], community_id: int) -> str:
        """
        Determine user role from user_context and community membership.

        Args:
            user_context: User information (user_id, username, platform, platform_user_id, role)
            community_id: Community ID

        Returns:
            User role string: 'super_admin', 'admin', 'moderator', 'vip', 'member'

        Role Determination:
            1. Check role field from user_context first ('super_admin', 'admin', 'moderator', 'vip', 'member')
            2. If not provided, default to 'member'
        """
        try:
            # Get role from user_context
            role = user_context.get('role', 'member')

            # Validate role is one of the expected values
            valid_roles = ['super_admin', 'admin', 'moderator', 'vip', 'member']
            if role not in valid_roles:
                logger.warning(
                    f"[AUTHZ] Invalid role '{role}' from user_context, "
                    f"defaulting to 'member'"
                )
                role = 'member'

            logger.debug(
                f"[AUTHZ] User {user_context.get('username')} has role '{role}' "
                f"in community {community_id}"
            )

            return role

        except Exception as e:
            logger.error(f"[AUTHZ] ERROR getting user role: {e}")
            return 'member'  # Default to member on error

    async def get_permissions(self, community_id: int) -> Optional[Dict[str, Any]]:
        """
        Get permission configuration for community.

        Args:
            community_id: Community ID

        Returns:
            Permission dict or None if not found

        Returns dict with keys:
            - create_permission: str ('admin_only', 'admin_mod', 'admin_mod_vip', 'all_members')
            - edit_own_events: bool
            - edit_all_events: str ('admin_only', 'admin_mod', 'none')
            - delete_own_events: bool
            - delete_all_events: str ('admin_only', 'admin_mod', 'none')
            - require_approval: bool
            - auto_approve_admins: bool
            - auto_approve_mods: bool
            - auto_approve_vips: bool
            - auto_approve_all: bool
            - rsvp_permission: str ('admin_mod', 'all_members', 'subscribers_only')
        """
        try:
            query = """
                SELECT
                    create_permission,
                    edit_own_events,
                    edit_all_events,
                    delete_own_events,
                    delete_all_events,
                    require_approval,
                    auto_approve_admins,
                    auto_approve_mods,
                    auto_approve_vips,
                    auto_approve_all,
                    rsvp_permission
                FROM calendar_permissions
                WHERE community_id = $1
            """

            result = await self.dal.execute(query, [community_id])

            if not result or len(result) == 0:
                logger.debug(f"[AUTHZ] No permissions found for community {community_id}")
                return None

            row = result[0]
            permissions = {
                'create_permission': row.get('create_permission', 'admin_mod'),
                'edit_own_events': row.get('edit_own_events', True),
                'edit_all_events': row.get('edit_all_events', 'admin_only'),
                'delete_own_events': row.get('delete_own_events', True),
                'delete_all_events': row.get('delete_all_events', 'admin_only'),
                'require_approval': row.get('require_approval', True),
                'auto_approve_admins': row.get('auto_approve_admins', True),
                'auto_approve_mods': row.get('auto_approve_mods', False),
                'auto_approve_vips': row.get('auto_approve_vips', False),
                'auto_approve_all': row.get('auto_approve_all', False),
                'rsvp_permission': row.get('rsvp_permission', 'all_members')
            }

            logger.debug(
                f"[AUTHZ] Retrieved permissions for community {community_id}: "
                f"create={permissions['create_permission']}, "
                f"require_approval={permissions['require_approval']}"
            )

            return permissions

        except Exception as e:
            import traceback
            logger.error(
                f"[AUTHZ] ERROR getting permissions for community {community_id}: {e}\n"
                f"Traceback: {traceback.format_exc()}"
            )
            return None

    async def update_permissions(
        self,
        community_id: int,
        permissions: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        Update permission configuration (admin only).

        Args:
            community_id: Community ID
            permissions: Permission dict to update with keys like:
                - create_permission
                - edit_own_events
                - edit_all_events
                - delete_own_events
                - delete_all_events
                - require_approval
                - auto_approve_admins
                - auto_approve_mods
                - auto_approve_vips
                - auto_approve_all
                - rsvp_permission

        Returns:
            Updated permission dict or None on failure

        Note: Caller is responsible for ensuring user is admin before calling this
        """
        try:
            # Build dynamic update query
            update_fields = []
            params = []
            param_count = 0

            # Map of allowed permission fields
            allowed_fields = {
                'create_permission': 'create_permission',
                'edit_own_events': 'edit_own_events',
                'edit_all_events': 'edit_all_events',
                'delete_own_events': 'delete_own_events',
                'delete_all_events': 'delete_all_events',
                'require_approval': 'require_approval',
                'auto_approve_admins': 'auto_approve_admins',
                'auto_approve_mods': 'auto_approve_mods',
                'auto_approve_vips': 'auto_approve_vips',
                'auto_approve_all': 'auto_approve_all',
                'rsvp_permission': 'rsvp_permission'
            }

            for key, column in allowed_fields.items():
                if key in permissions:
                    param_count += 1
                    update_fields.append(f"{column} = ${param_count}")
                    params.append(permissions[key])

            if not update_fields:
                logger.warning("[AUTHZ] No valid permission fields to update")
                return None

            # Add community_id
            param_count += 1
            params.append(community_id)

            query = f"""
                UPDATE calendar_permissions
                SET {', '.join(update_fields)}, updated_at = NOW()
                WHERE community_id = ${param_count}
                RETURNING
                    create_permission,
                    edit_own_events,
                    edit_all_events,
                    delete_own_events,
                    delete_all_events,
                    require_approval,
                    auto_approve_admins,
                    auto_approve_mods,
                    auto_approve_vips,
                    auto_approve_all,
                    rsvp_permission
            """

            result = await self.dal.execute(query, params)

            if not result or len(result) == 0:
                logger.error(
                    f"[AUTHZ] FAILED: Permission update failed for community {community_id}"
                )
                return None

            row = result[0]
            updated = {
                'create_permission': row.get('create_permission'),
                'edit_own_events': row.get('edit_own_events'),
                'edit_all_events': row.get('edit_all_events'),
                'delete_own_events': row.get('delete_own_events'),
                'delete_all_events': row.get('delete_all_events'),
                'require_approval': row.get('require_approval'),
                'auto_approve_admins': row.get('auto_approve_admins'),
                'auto_approve_mods': row.get('auto_approve_mods'),
                'auto_approve_vips': row.get('auto_approve_vips'),
                'auto_approve_all': row.get('auto_approve_all'),
                'rsvp_permission': row.get('rsvp_permission')
            }

            logger.info(
                f"[AUTHZ] SUCCESS: Permissions updated for community {community_id}"
            )

            return updated

        except Exception as e:
            logger.error(f"[AUTHZ] ERROR updating permissions: {e}")
            return None
