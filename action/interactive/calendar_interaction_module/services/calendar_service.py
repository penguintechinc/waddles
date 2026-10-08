"""
Calendar Service - Event management with CRUD, permissions, and platform sync
Implements complete event lifecycle management with approval workflows
"""
import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from dataclasses import dataclass

from services.event_discord_sync_client import (
    EventDiscordSyncClient,
    get_event_discord_sync_client,
)

logger = logging.getLogger(__name__)

#: Updating any of these fields is what makes an already-pushed Discord
#: scheduled event stale -- the set `update_event()` checks `changes`
#: against before scheduling a re-sync. Deliberately excludes `timezone`
#: (hub-api's push always reads the current row, `event_date`/`end_date`
#: already carry the tz-aware instant) and anything not sent to Discord
#: (max_attendees, category_id, tags, ...).
_DISCORD_VISIBLE_FIELDS = frozenset({
    'title', 'description', 'event_date', 'end_date', 'location'
})


@dataclass
class EventInfo:
    """Event information data class"""
    id: int
    event_uuid: uuid.UUID
    community_id: int
    entity_id: str
    platform: str
    title: str
    description: Optional[str]
    event_date: datetime
    end_date: Optional[datetime]
    timezone: str
    location: Optional[str]
    cover_image_url: Optional[str]
    max_attendees: Optional[int]
    rsvp_enabled: bool
    rsvp_deadline: Optional[datetime]
    waitlist_enabled: bool
    is_recurring: bool
    recurring_pattern: Optional[str]
    recurring_days: Optional[list]
    recurring_end_date: Optional[datetime]
    parent_event_id: Optional[int]
    status: str
    approved_by: Optional[int]
    approved_at: Optional[datetime]
    rejection_reason: Optional[str]
    created_by_user_id: Optional[int]
    created_by_platform_user_id: str
    created_by_username: str
    discord_event_id: Optional[str]
    twitch_segment_id: Optional[str]
    youtube_broadcast_id: Optional[str]
    sync_status: str
    last_sync_at: Optional[datetime]
    sync_error: Optional[str]
    category_id: Optional[int]
    tags: list
    view_count: int
    attending_count: int
    interested_count: int
    declined_count: int
    series_id: Optional[int]
    created_at: datetime
    updated_at: datetime


class CalendarService:
    """
    Calendar service for event management.

    Features:
    - Complete CRUD operations
    - Permission-based access control
    - Approval workflow integration
    - Full-text search
    - Multi-community context support
    - AAA (Auth/Authz/Audit) logging
    """

    def __init__(self, dal, permission_service=None, sync_client: Optional[EventDiscordSyncClient] = None):
        """Initialize calendar service with database abstraction layer."""
        self.dal = dal
        self.permission_service = permission_service
        self.sync_client = sync_client or get_event_discord_sync_client()

    async def create_event(
        self,
        data: Dict[str, Any],
        user_context: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        Create new event with permission checks and approval workflow.

        Args:
            data: Event data (title, description, event_date, etc.)
            user_context: User information (user_id, platform, platform_user_id, username, role)

        Returns:
            Created event dict or None on failure

        AAA Logging: AUTH, AUTHZ, AUDIT
        """
        try:
            community_id = data.get('community_id')
            if not community_id:
                logger.error("[AUTHZ] DENIED: Missing community_id in event creation")
                return None

            # Permission check: Can user create events?
            if self.permission_service:
                can_create = await self.permission_service.can_create_event(
                    user_context, community_id
                )
                if not can_create:
                    logger.warning(
                        f"[AUTHZ] DENIED: User {user_context.get('username')} "
                        f"cannot create events in community {community_id}"
                    )
                    return None

            # Determine initial status based on approval workflow
            status = 'pending'  # Default
            if self.permission_service:
                needs_approval = await self.permission_service.needs_approval(
                    user_context, community_id
                )
                if not needs_approval:
                    status = 'approved'
                    logger.info(
                        f"[AUTHZ] AUTO-APPROVED: Event auto-approved for user "
                        f"{user_context.get('username')} (role: {user_context.get('role')})"
                    )
            else:
                # No permission service = auto-approve (for testing)
                status = 'approved'

            # Generate UUID for event
            event_uuid = uuid.uuid4()

            # Insert event
            query = """
                INSERT INTO calendar_events (
                    event_uuid, community_id, entity_id, platform,
                    title, description, event_date, end_date, timezone, location, cover_image_url,
                    max_attendees, rsvp_enabled, rsvp_deadline, waitlist_enabled,
                    is_recurring, recurring_pattern, recurring_days, recurring_until,
                    status, created_by_user_id, created_by_platform_user_id, created_by_username,
                    category_id, tags, series_id
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15,
                    $16, $17, $18, $19, $20, $21, $22, $23, $24, $25, $26
                )
                RETURNING id, event_uuid, created_at
            """

            result = await self.dal.execute(query, [
                event_uuid,
                community_id,
                data.get('entity_id'),
                data.get('platform'),
                data['title'],
                data.get('description'),
                data['event_date'],
                data.get('end_date'),
                data.get('timezone', 'UTC'),
                data.get('location'),
                data.get('cover_image_url'),
                data.get('max_attendees'),
                data.get('rsvp_enabled', True),
                data.get('rsvp_deadline'),
                data.get('waitlist_enabled', True),
                data.get('is_recurring', False),
                data.get('recurring_pattern'),
                data.get('recurring_days'),
                data.get('recurring_until'),
                status,
                user_context.get('user_id'),
                user_context.get('platform_user_id'),
                user_context.get('username'),
                data.get('category_id'),
                data.get('tags', []),
                data.get('series_id')
            ])

            if not result or len(result) == 0:
                logger.error("[AUDIT] FAILED: Event creation failed - no result from database")
                return None

            event_id = result[0]['id']

            # Audit log
            await self._log_activity(
                event_id=event_id,
                community_id=community_id,
                activity_type='created',
                user_context=user_context,
                details={
                    'title': data['title'],
                    'event_date': str(data['event_date']),
                    'status': status,
                    'is_recurring': data.get('is_recurring', False)
                }
            )

            logger.info(
                f"[AUDIT] SUCCESS: Event {event_id} created by {user_context.get('username')} "
                f"(community={community_id}, status={status})"
            )

            # Auto-approved at creation (no approval workflow needed) -> push to Discord now.
            # A still-`pending` event waits for `approve_event()` to trigger the first sync.
            if status == 'approved':
                await self._maybe_sync_to_discord(event_id, 'create')

            # Return created event
            return {
                'id': event_id,
                'uuid': str(event_uuid),
                'status': status,
                'title': data['title'],
                'created_at': result[0]['created_at'].isoformat()
            }

        except Exception as e:
            logger.error(f"[AUDIT] ERROR: Event creation failed: {e}")
            return None

    async def list_events(
        self,
        community_id: int,
        filters: Optional[Dict[str, Any]] = None,
        pagination: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """
        List events with filtering and pagination.

        Args:
            community_id: Community ID
            filters: Optional filters (status, date_from, date_to, category_id, tags, entity_id)
            pagination: Optional pagination (offset, limit)

        Returns:
            List of event dictionaries
        """
        try:
            filters = filters or {}
            pagination = pagination or {}

            # Build query with filters
            where_clauses = ["community_id = $1"]
            params = [community_id]
            param_count = 1

            # Status filter
            if filters.get('status'):
                param_count += 1
                where_clauses.append(f"status = ${param_count}")
                params.append(filters['status'])
            else:
                # Default: only show approved events
                param_count += 1
                where_clauses.append(f"status = ${param_count}")
                params.append('approved')

            # Date range filter
            if filters.get('date_from'):
                param_count += 1
                where_clauses.append(f"event_date >= ${param_count}")
                params.append(filters['date_from'])

            if filters.get('date_to'):
                param_count += 1
                where_clauses.append(f"event_date <= ${param_count}")
                params.append(filters['date_to'])

            # Category filter
            if filters.get('category_id'):
                param_count += 1
                where_clauses.append(f"category_id = ${param_count}")
                params.append(filters['category_id'])

            # Entity filter (for multi-community contexts)
            if filters.get('entity_id'):
                param_count += 1
                where_clauses.append(f"entity_id = ${param_count}")
                params.append(filters['entity_id'])

            # Tag filter (JSONB array contains)
            if filters.get('tags') and len(filters['tags']) > 0:
                param_count += 1
                where_clauses.append(f"tags @> ${param_count}::jsonb")
                params.append(filters['tags'])

            where_sql = " AND ".join(where_clauses)

            # Pagination
            limit = pagination.get('limit', 50)
            offset = pagination.get('offset', 0)
            param_count += 1
            limit_param = param_count
            param_count += 1
            offset_param = param_count

            query = f"""
                SELECT
                    id, event_uuid, community_id, entity_id, platform,
                    title, description, event_date, end_date, timezone, location, cover_image_url,
                    max_attendees, rsvp_enabled, rsvp_deadline, waitlist_enabled,
                    status, category_id, tags,
                    view_count, attending_count, interested_count, declined_count,
                    created_by_username, created_at, updated_at
                FROM calendar_events
                WHERE {where_sql}
                ORDER BY event_date ASC
                LIMIT ${limit_param} OFFSET ${offset_param}
            """

            params.extend([limit, offset])

            rows = await self.dal.execute(query, params)

            events = []
            for row in rows:
                events.append({
                    'id': row['id'],
                    'uuid': str(row['event_uuid']),
                    'community_id': row['community_id'],
                    'entity_id': row['entity_id'],
                    'platform': row['platform'],
                    'title': row['title'],
                    'description': row['description'],
                    'event_date': row['event_date'].isoformat(),
                    'end_date': row['end_date'].isoformat() if row['end_date'] else None,
                    'timezone': row['timezone'],
                    'location': row['location'],
                    'cover_image_url': row['cover_image_url'],
                    'max_attendees': row['max_attendees'],
                    'rsvp_enabled': row['rsvp_enabled'],
                    'rsvp_deadline': row['rsvp_deadline'].isoformat() if row['rsvp_deadline'] else None,
                    'waitlist_enabled': row['waitlist_enabled'],
                    'status': row['status'],
                    'category_id': row['category_id'],
                    'tags': row['tags'],
                    'stats': {
                        'view_count': row['view_count'],
                        'attending': row['attending_count'],
                        'interested': row['interested_count'],
                        'declined': row['declined_count']
                    },
                    'created_by': row['created_by_username'],
                    'created_at': row['created_at'].isoformat(),
                    'updated_at': row['updated_at'].isoformat()
                })

            logger.debug(f"[AUDIT] List events: community={community_id}, count={len(events)}")
            return events

        except Exception as e:
            logger.error(f"[AUDIT] ERROR: List events failed: {e}")
            return []

    async def get_event(
        self,
        event_id: int,
        include_attendees: bool = False
    ) -> Optional[Dict[str, Any]]:
        """
        Get event by ID with optional attendee list.

        Args:
            event_id: Event ID
            include_attendees: Include RSVP attendee list

        Returns:
            Event dict or None
        """
        try:
            query = """
                SELECT * FROM calendar_events WHERE id = $1
            """
            rows = await self.dal.execute(query, [event_id])

            if not rows or len(rows) == 0:
                return None

            row = rows[0]
            event = {
                'id': row['id'],
                'uuid': str(row['event_uuid']),
                'community_id': row['community_id'],
                'entity_id': row['entity_id'],
                'platform': row['platform'],
                'title': row['title'],
                'description': row['description'],
                'event_date': row['event_date'].isoformat(),
                'end_date': row['end_date'].isoformat() if row['end_date'] else None,
                'timezone': row['timezone'],
                'location': row['location'],
                'cover_image_url': row['cover_image_url'],
                'max_attendees': row['max_attendees'],
                'rsvp_enabled': row['rsvp_enabled'],
                'rsvp_deadline': row['rsvp_deadline'].isoformat() if row['rsvp_deadline'] else None,
                'waitlist_enabled': row['waitlist_enabled'],
                'is_recurring': row['is_recurring'],
                'recurring_pattern': row['recurring_pattern'],
                'recurring_days': row['recurring_days'],
                'recurring_end_date': row['recurring_end_date'].isoformat() if row['recurring_end_date'] else None,
                'parent_event_id': row['parent_event_id'],
                'status': row['status'],
                'approved_by': row['approved_by'],
                'approved_at': row['approved_at'].isoformat() if row['approved_at'] else None,
                'rejection_reason': row['rejection_reason'],
                'created_by_user_id': row['created_by_user_id'],
                'created_by_platform_user_id': row['created_by_platform_user_id'],
                'created_by_username': row['created_by_username'],
                'category_id': row['category_id'],
                'tags': row['tags'],
                'sync': {
                    'discord_event_id': row['discord_event_id'],
                    'twitch_segment_id': row['twitch_segment_id'],
                    'youtube_broadcast_id': row['youtube_broadcast_id'],
                    'sync_status': row['sync_status'],
                    'last_sync_at': row['last_sync_at'].isoformat() if row['last_sync_at'] else None,
                    'sync_error': row['sync_error']
                },
                'stats': {
                    'view_count': row['view_count'],
                    'attending': row['attending_count'],
                    'interested': row['interested_count'],
                    'declined': row['declined_count']
                },
                'series_id': row['series_id'],
                'created_at': row['created_at'].isoformat(),
                'updated_at': row['updated_at'].isoformat()
            }

            # Increment view count
            await self.dal.execute(
                "UPDATE calendar_events SET view_count = view_count + 1 WHERE id = $1",
                [event_id]
            )

            # Include attendees if requested
            if include_attendees:
                attendees_query = """
                    SELECT hub_user_id, platform, platform_user_id, username,
                           rsvp_status, guest_count, is_waitlisted, waitlist_position,
                           user_note, created_at
                    FROM calendar_rsvps
                    WHERE event_id = $1
                    ORDER BY created_at ASC
                """
                attendee_rows = await self.dal.execute(attendees_query, [event_id])

                event['attendees'] = []
                for attendee in attendee_rows:
                    event['attendees'].append({
                        'hub_user_id': attendee['hub_user_id'],
                        'platform': attendee['platform'],
                        'platform_user_id': attendee['platform_user_id'],
                        'username': attendee['username'],
                        'rsvp_status': attendee['rsvp_status'],
                        'guest_count': attendee['guest_count'],
                        'is_waitlisted': attendee['is_waitlisted'],
                        'waitlist_position': attendee['waitlist_position'],
                        'user_note': attendee['user_note'],
                        'joined_at': attendee['created_at'].isoformat()
                    })

            return event

        except Exception as e:
            logger.error(f"[AUDIT] ERROR: Get event {event_id} failed: {e}")
            return None

    async def update_event(
        self,
        event_id: int,
        data: Dict[str, Any],
        user_context: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        Update event with permission checks.

        Args:
            event_id: Event ID
            data: Updated event data
            user_context: User information

        Returns:
            Updated event dict or None
        """
        try:
            # Get existing event
            existing = await self.get_event(event_id)
            if not existing:
                logger.warning(f"[AUTHZ] DENIED: Event {event_id} not found for update")
                return None

            # Permission check: Can user edit this event?
            if self.permission_service:
                can_edit = await self.permission_service.can_edit_event(
                    user_context, event_id
                )
                if not can_edit:
                    logger.warning(
                        f"[AUTHZ] DENIED: User {user_context.get('username')} "
                        f"cannot edit event {event_id}"
                    )
                    return None

            # Build update query dynamically
            update_fields = []
            params = []
            param_count = 0

            # Track changes for audit log
            changes = {}

            # Map of allowed updateable fields
            updateable_fields = {
                'title': 'title',
                'description': 'description',
                'event_date': 'event_date',
                'end_date': 'end_date',
                'timezone': 'timezone',
                'location': 'location',
                'cover_image_url': 'cover_image_url',
                'max_attendees': 'max_attendees',
                'rsvp_deadline': 'rsvp_deadline',
                'category_id': 'category_id',
                'tags': 'tags'
            }

            for field, column in updateable_fields.items():
                if field in data:
                    param_count += 1
                    update_fields.append(f"{column} = ${param_count}")
                    params.append(data[field])

                    # Track change
                    if existing.get(field) != data[field]:
                        changes[field] = {
                            'from': existing.get(field),
                            'to': data[field]
                        }

            if not update_fields:
                logger.warning("[AUDIT] No fields to update")
                return existing

            # Add event_id to params
            param_count += 1
            params.append(event_id)

            query = f"""
                UPDATE calendar_events
                SET {', '.join(update_fields)}, updated_at = NOW()
                WHERE id = ${param_count}
                RETURNING id, updated_at
            """

            result = await self.dal.execute(query, params)

            if not result or len(result) == 0:
                logger.error("[AUDIT] FAILED: Event update failed - no result from database")
                return None

            # Audit log
            await self._log_activity(
                event_id=event_id,
                community_id=existing['community_id'],
                activity_type='updated',
                user_context=user_context,
                details={'fields_updated': list(data.keys())},
                changes=changes
            )

            logger.info(
                f"[AUDIT] SUCCESS: Event {event_id} updated by {user_context.get('username')} "
                f"(fields: {', '.join(data.keys())})"
            )

            # Re-sync only when a Discord-visible field actually changed, on an
            # approved event that's already been pushed once (`discord_event_id`
            # set). An edit to a pending/unpushed/never-pushed event has nothing
            # live on Discord to update.
            discord_event_id = existing.get('sync', {}).get('discord_event_id')
            if (
                existing['status'] == 'approved'
                and discord_event_id
                and any(field in _DISCORD_VISIBLE_FIELDS for field in changes)
            ):
                await self._maybe_sync_to_discord(event_id, 'update')

            # Return updated event
            return await self.get_event(event_id)

        except Exception as e:
            logger.error(f"[AUDIT] ERROR: Event update failed: {e}")
            return None

    async def delete_event(
        self,
        event_id: int,
        user_context: Dict[str, Any]
    ) -> bool:
        """
        Delete (soft delete) event with permission checks.

        Args:
            event_id: Event ID
            user_context: User information

        Returns:
            True on success, False on failure
        """
        try:
            # Get existing event
            existing = await self.get_event(event_id)
            if not existing:
                logger.warning(f"[AUTHZ] DENIED: Event {event_id} not found for deletion")
                return False

            # Permission check: Can user delete this event?
            if self.permission_service:
                can_delete = await self.permission_service.can_delete_event(
                    user_context, event_id
                )
                if not can_delete:
                    logger.warning(
                        f"[AUTHZ] DENIED: User {user_context.get('username')} "
                        f"cannot delete event {event_id}"
                    )
                    return False

            # Soft delete: mark as cancelled
            query = """
                UPDATE calendar_events
                SET status = 'cancelled', updated_at = NOW()
                WHERE id = $1
                RETURNING id
            """

            result = await self.dal.execute(query, [event_id])

            if not result or len(result) == 0:
                logger.error("[AUDIT] FAILED: Event deletion failed - no result from database")
                return False

            # Audit log
            await self._log_activity(
                event_id=event_id,
                community_id=existing['community_id'],
                activity_type='deleted',
                user_context=user_context,
                details={'title': existing['title']}
            )

            logger.info(
                f"[AUDIT] SUCCESS: Event {event_id} deleted by {user_context.get('username')}"
            )

            # Only cancel on Discord if it was ever actually pushed there.
            discord_event_id = existing.get('sync', {}).get('discord_event_id')
            if discord_event_id:
                await self._maybe_sync_to_discord(event_id, 'cancel')

            return True

        except Exception as e:
            logger.error(f"[AUDIT] ERROR: Event deletion failed: {e}")
            return False

    async def approve_event(
        self,
        event_id: int,
        user_context: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        Approve pending event (admin only).

        Args:
            event_id: Event ID
            user_context: User information (must be admin)

        Returns:
            Approved event dict or None
        """
        try:
            # Get existing event
            existing = await self.get_event(event_id)
            if not existing:
                logger.warning(f"[AUTHZ] DENIED: Event {event_id} not found for approval")
                return None

            # Only pending events can be approved
            if existing['status'] != 'pending':
                logger.warning(
                    f"[AUTHZ] DENIED: Event {event_id} is not pending (status={existing['status']})"
                )
                return None

            # Permission check: Only admins can approve
            # (This would normally check admin role from user_context)
            if user_context.get('role') not in ['admin', 'super_admin']:
                logger.warning(
                    f"[AUTHZ] DENIED: User {user_context.get('username')} "
                    f"is not admin, cannot approve event {event_id}"
                )
                return None

            # Approve event
            query = """
                UPDATE calendar_events
                SET status = 'approved',
                    approved_by = $1,
                    approved_at = NOW(),
                    updated_at = NOW()
                WHERE id = $2
                RETURNING id, approved_at
            """

            result = await self.dal.execute(query, [
                user_context.get('user_id'),
                event_id
            ])

            if not result or len(result) == 0:
                logger.error("[AUDIT] FAILED: Event approval failed - no result from database")
                return None

            # Audit log
            await self._log_activity(
                event_id=event_id,
                community_id=existing['community_id'],
                activity_type='approved',
                user_context=user_context,
                details={
                    'title': existing['title'],
                    'approved_at': result[0]['approved_at'].isoformat()
                }
            )

            logger.info(
                f"[AUDIT] SUCCESS: Event {event_id} approved by {user_context.get('username')}"
            )

            # First Discord push happens on approval, not creation, for events
            # that needed the approval workflow (status was 'pending' until now).
            await self._maybe_sync_to_discord(event_id, 'create')

            # Return updated event
            return await self.get_event(event_id)

        except Exception as e:
            logger.error(f"[AUDIT] ERROR: Event approval failed: {e}")
            return None

    async def reject_event(
        self,
        event_id: int,
        reason: str,
        user_context: Dict[str, Any]
    ) -> bool:
        """
        Reject pending event with reason (admin only).

        Args:
            event_id: Event ID
            reason: Rejection reason
            user_context: User information (must be admin)

        Returns:
            True on success, False on failure
        """
        try:
            # Get existing event
            existing = await self.get_event(event_id)
            if not existing:
                logger.warning(f"[AUTHZ] DENIED: Event {event_id} not found for rejection")
                return False

            # Only pending events can be rejected
            if existing['status'] != 'pending':
                logger.warning(
                    f"[AUTHZ] DENIED: Event {event_id} is not pending (status={existing['status']})"
                )
                return False

            # Permission check: Only admins can reject
            if user_context.get('role') not in ['admin', 'super_admin']:
                logger.warning(
                    f"[AUTHZ] DENIED: User {user_context.get('username')} "
                    f"is not admin, cannot reject event {event_id}"
                )
                return False

            # No Discord sync trigger here, deliberately: a rejected event was
            # never approved, so it was never pushed to Discord in the first
            # place (see create_event()/approve_event()'s own sync hooks).
            query = """
                UPDATE calendar_events
                SET status = 'rejected',
                    rejection_reason = $1,
                    updated_at = NOW()
                WHERE id = $2
                RETURNING id
            """

            result = await self.dal.execute(query, [reason, event_id])

            if not result or len(result) == 0:
                logger.error("[AUDIT] FAILED: Event rejection failed - no result from database")
                return False

            # Audit log
            await self._log_activity(
                event_id=event_id,
                community_id=existing['community_id'],
                activity_type='rejected',
                user_context=user_context,
                details={
                    'title': existing['title'],
                    'rejection_reason': reason
                }
            )

            logger.info(
                f"[AUDIT] SUCCESS: Event {event_id} rejected by {user_context.get('username')} "
                f"(reason: {reason})"
            )

            return True

        except Exception as e:
            logger.error(f"[AUDIT] ERROR: Event rejection failed: {e}")
            return False

    async def _maybe_sync_to_discord(self, event_id: int, action: str) -> None:
        """Trigger hub-api's Discord event-sync engine for `event_id` -- never raises.

        Persists `sync_status='pending'` immediately (synchronously, before
        returning) so `GET .../sync/status` reflects "a sync is in flight"
        right away -- hub-api's own `sync_event()` overwrites this with the
        real outcome once its push completes. The actual HTTP call to
        hub-api is then handed to `asyncio.create_task` (fire-and-forget):
        Discord/hub-api latency or an outage must never block the calendar
        HTTP response this is called from. Anything the background task
        fails to converge is picked up by the reconcile CronJob, since the
        row is left in (or returns to) `sync_status='pending'`.
        """
        try:
            await self.dal.execute(
                "UPDATE calendar_events SET sync_status = 'pending', updated_at = NOW() "
                "WHERE id = $1",
                [event_id]
            )
        except Exception as e:
            logger.error(
                f"[SYNC] ERROR: Failed to mark event {event_id} pending before sync "
                f"(action={action}): {e}"
            )

        try:
            asyncio.create_task(self._push_to_discord(event_id, action))
        except RuntimeError as e:
            # No running event loop (e.g. a sync test harness) -- log and
            # let the reconcile CronJob pick it up; never raise into the caller.
            logger.error(
                f"[SYNC] ERROR: Could not schedule Discord sync task for event {event_id} "
                f"(action={action}): {e}"
            )

    async def _push_to_discord(self, event_id: int, action: str) -> None:
        """Background task: the actual hub-api call `_maybe_sync_to_discord` schedules.

        `self.sync_client` is itself fail-closed (never raises) -- the
        `except Exception` here is belt-and-suspenders only, so a bug in
        the client can never crash this fire-and-forget task silently.
        """
        try:
            result = await self.sync_client.sync_event(event_id, action)
        except Exception as e:
            logger.error(
                f"[SYNC] ERROR: Unexpected error syncing event {event_id} "
                f"(action={action}): {e}"
            )
            return

        if not result.ok:
            logger.error(
                f"[SYNC] ERROR: Discord sync call failed for event {event_id} "
                f"(action={action}): {result.sync_error}"
            )
        elif result.sync_status == 'sync_error':
            logger.warning(
                f"[SYNC] WARNING: Discord sync reported an error for event {event_id} "
                f"(action={action}): {result.sync_error}"
            )
        else:
            logger.info(
                f"[SYNC] SUCCESS: Discord sync triggered for event {event_id} "
                f"(action={action}, sync_status={result.sync_status})"
            )

    async def search_events(
        self,
        community_id: int,
        query: str,
        filters: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """
        Full-text search on events.

        Args:
            community_id: Community ID
            query: Search query string
            filters: Optional additional filters

        Returns:
            List of matching events
        """
        try:
            filters = filters or {}

            # Build search query with full-text search
            sql = """
                SELECT
                    id, event_uuid, title, description, event_date, end_date,
                    location, cover_image_url, status, category_id, tags,
                    attending_count, interested_count,
                    created_by_username, created_at
                FROM calendar_events
                WHERE community_id = $1
                  AND status = 'approved'
                  AND to_tsvector('english', title || ' ' || COALESCE(description, ''))
                      @@ plainto_tsquery('english', $2)
                ORDER BY
                    ts_rank(to_tsvector('english', title || ' ' || COALESCE(description, '')),
                            plainto_tsquery('english', $3)) DESC,
                    event_date ASC
                LIMIT 50
            """

            rows = await self.dal.execute(sql, [community_id, query, query])

            events = []
            for row in rows:
                events.append({
                    'id': row['id'],
                    'uuid': str(row['event_uuid']),
                    'title': row['title'],
                    'description': row['description'],
                    'event_date': row['event_date'].isoformat(),
                    'end_date': row['end_date'].isoformat() if row['end_date'] else None,
                    'location': row['location'],
                    'cover_image_url': row['cover_image_url'],
                    'status': row['status'],
                    'category_id': row['category_id'],
                    'tags': row['tags'],
                    'stats': {
                        'attending': row['attending_count'],
                        'interested': row['interested_count']
                    },
                    'created_by': row['created_by_username'],
                    'created_at': row['created_at'].isoformat()
                })

            logger.debug(f"[AUDIT] Search events: query='{query}', results={len(events)}")
            return events

        except Exception as e:
            logger.error(f"[AUDIT] ERROR: Event search failed: {e}")
            return []

    async def get_upcoming_events(
        self,
        community_id: int,
        limit: int = 10,
        entity_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Get upcoming approved events.

        Args:
            community_id: Community ID
            limit: Maximum number of events to return
            entity_id: Optional entity ID filter

        Returns:
            List of upcoming events
        """
        try:
            # Build query
            where_clauses = [
                "community_id = $1",
                "status = 'approved'",
                "event_date > NOW()"
            ]
            params = [community_id]
            param_count = 1

            if entity_id:
                param_count += 1
                where_clauses.append(f"entity_id = ${param_count}")
                params.append(entity_id)

            where_sql = " AND ".join(where_clauses)

            param_count += 1
            limit_param = param_count

            query = f"""
                SELECT
                    id, event_uuid, title, description, event_date, end_date,
                    location, cover_image_url, category_id, tags,
                    attending_count, interested_count,
                    created_by_username, created_at
                FROM calendar_events
                WHERE {where_sql}
                ORDER BY event_date ASC
                LIMIT ${limit_param}
            """

            params.append(limit)

            rows = await self.dal.execute(query, params)

            events = []
            for row in rows:
                events.append({
                    'id': row['id'],
                    'uuid': str(row['event_uuid']),
                    'title': row['title'],
                    'description': row['description'],
                    'event_date': row['event_date'].isoformat(),
                    'end_date': row['end_date'].isoformat() if row['end_date'] else None,
                    'location': row['location'],
                    'cover_image_url': row['cover_image_url'],
                    'category_id': row['category_id'],
                    'tags': row['tags'],
                    'stats': {
                        'attending': row['attending_count'],
                        'interested': row['interested_count']
                    },
                    'created_by': row['created_by_username'],
                    'created_at': row['created_at'].isoformat()
                })

            return events

        except Exception as e:
            logger.error(f"[AUDIT] ERROR: Get upcoming events failed: {e}")
            return []

    async def _log_activity(
        self,
        event_id: int,
        community_id: int,
        activity_type: str,
        user_context: Dict[str, Any],
        details: Optional[Dict[str, Any]] = None,
        changes: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Log activity to audit log (internal method).

        Args:
            event_id: Event ID
            community_id: Community ID
            activity_type: Type of activity (created, updated, deleted, etc.)
            user_context: User information
            details: Optional activity details
            changes: Optional before/after changes
        """
        try:
            query = """
                INSERT INTO calendar_activity_log (
                    event_id, community_id, activity_type,
                    user_id, username, platform, platform_user_id,
                    details, changes
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """

            await self.dal.execute(query, [
                event_id,
                community_id,
                activity_type,
                user_context.get('user_id'),
                user_context.get('username'),
                user_context.get('platform'),
                user_context.get('platform_user_id'),
                details or {},
                changes or {}
            ])

        except Exception as e:
            # Don't fail the operation if logging fails, just log the error
            logger.error(f"Failed to log activity: {e}")
