"""
Availability Service - User availability settings and slot computation
Manages availability windows, booking constraints, and free slot calculation
"""
import json
import logging
from datetime import datetime, timedelta, time, timezone
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)

# Default availability settings
DEFAULT_SLOT_DURATIONS = [30]
DEFAULT_SLOT_DURATION = 30
DEFAULT_MIN_NOTICE_HOURS = 4
DEFAULT_MAX_FUTURE_DAYS = 30
DEFAULT_BUFFER_MINUTES = 0
DEFAULT_TIMEZONE = 'UTC'


class AvailabilityService:
    """
    Service for managing user availability and computing available time slots.

    Features:
    - User calendar settings management
    - Weekly availability schedules
    - Free/busy computation from multiple sources
    - Available slot calculation with constraints
    - Timezone-aware scheduling
    """

    def __init__(self, dal):
        """
        Initialize availability service with database abstraction layer.

        Args:
            dal: AsyncDAL database abstraction layer
        """
        self.dal = dal

    async def get_settings(
        self,
        user_id: int
    ) -> Dict[str, Any]:
        """
        Get calendar settings for a user.

        Args:
            user_id: Hub user ID

        Returns:
            Calendar settings dict (returns defaults if not found)
        """
        try:
            query = """
                SELECT id, visibility_public, visibility_registered, visibility_community,
                       slot_durations, default_slot_duration, min_notice_hours,
                       max_future_days, buffer_minutes, weekly_availability, timezone,
                       booking_enabled, booking_slug, booking_page_title,
                       booking_page_description
                FROM user_calendar_settings
                WHERE hub_user_id = $1
            """

            rows = await self.dal.execute(query, [user_id])

            if rows:
                settings = rows[0]
                return {
                    'id': settings['id'],
                    'visibility_public': settings['visibility_public'],
                    'visibility_registered': settings['visibility_registered'],
                    'visibility_community': settings['visibility_community'],
                    'slot_durations': settings['slot_durations'],
                    'default_slot_duration': settings['default_slot_duration'],
                    'min_notice_hours': settings['min_notice_hours'],
                    'max_future_days': settings['max_future_days'],
                    'buffer_minutes': settings['buffer_minutes'],
                    'weekly_availability': settings['weekly_availability'],
                    'timezone': settings['timezone'],
                    'booking_enabled': settings['booking_enabled'],
                    'booking_slug': settings['booking_slug'],
                    'booking_page_title': settings['booking_page_title'],
                    'booking_page_description': settings['booking_page_description']
                }
            else:
                # Return defaults
                return {
                    'visibility_public': 'hidden',
                    'visibility_registered': 'free_busy',
                    'visibility_community': 'details',
                    'slot_durations': DEFAULT_SLOT_DURATIONS,
                    'default_slot_duration': DEFAULT_SLOT_DURATION,
                    'min_notice_hours': DEFAULT_MIN_NOTICE_HOURS,
                    'max_future_days': DEFAULT_MAX_FUTURE_DAYS,
                    'buffer_minutes': DEFAULT_BUFFER_MINUTES,
                    'weekly_availability': {},
                    'timezone': DEFAULT_TIMEZONE,
                    'booking_enabled': False,
                    'booking_slug': None,
                    'booking_page_title': None,
                    'booking_page_description': None
                }

        except Exception as e:
            logger.error(f"[AVAILABILITY] Get settings error for user={user_id}: {e}")
            # Return defaults on error
            return {
                'visibility_public': 'hidden',
                'visibility_registered': 'free_busy',
                'visibility_community': 'details',
                'slot_durations': DEFAULT_SLOT_DURATIONS,
                'default_slot_duration': DEFAULT_SLOT_DURATION,
                'min_notice_hours': DEFAULT_MIN_NOTICE_HOURS,
                'max_future_days': DEFAULT_MAX_FUTURE_DAYS,
                'buffer_minutes': DEFAULT_BUFFER_MINUTES,
                'weekly_availability': {},
                'timezone': DEFAULT_TIMEZONE,
                'booking_enabled': False,
                'booking_slug': None,
                'booking_page_title': None,
                'booking_page_description': None
            }

    async def update_settings(
        self,
        user_id: int,
        settings_dict: Dict[str, Any]
    ) -> bool:
        """
        Update calendar settings for a user (upsert).

        Args:
            user_id: Hub user ID
            settings_dict: Settings to update

        Returns:
            True on success, False on failure
        """
        try:
            # Build update fields dynamically
            allowed_fields = {
                'visibility_public', 'visibility_registered', 'visibility_community',
                'slot_durations', 'default_slot_duration', 'min_notice_hours',
                'max_future_days', 'buffer_minutes', 'weekly_availability', 'timezone',
                'booking_enabled', 'booking_slug', 'booking_page_title',
                'booking_page_description'
            }

            # Filter to only allowed fields
            updates = {k: v for k, v in settings_dict.items() if k in allowed_fields}

            if not updates:
                logger.warning(f"[AVAILABILITY] No valid fields to update for user={user_id}")
                return False

            # Check if settings exist
            check_query = """
                SELECT id FROM user_calendar_settings WHERE hub_user_id = $1
            """
            existing = await self.dal.execute(check_query, [user_id])

            if existing:
                # Update existing
                set_clauses = []
                values = []
                param_idx = 1

                for field, value in updates.items():
                    set_clauses.append(f"{field} = ${param_idx}")
                    values.append(value)
                    param_idx += 1

                # Add updated_at
                set_clauses.append("updated_at = NOW()")

                # Add WHERE clause parameter
                values.append(user_id)

                update_query = f"""
                    UPDATE user_calendar_settings
                    SET {', '.join(set_clauses)}
                    WHERE hub_user_id = ${param_idx}
                """

                await self.dal.execute(update_query, values)

            else:
                # Insert new
                fields = ['hub_user_id'] + list(updates.keys())
                placeholders = [f'${i+1}' for i in range(len(fields))]
                values = [user_id] + list(updates.values())

                insert_query = f"""
                    INSERT INTO user_calendar_settings ({', '.join(fields)})
                    VALUES ({', '.join(placeholders)})
                """

                await self.dal.execute(insert_query, values)

            logger.info(f"[AVAILABILITY] Settings updated for user={user_id}")
            return True

        except Exception as e:
            logger.error(
                f"[AVAILABILITY] Update settings error for user={user_id}: {e}"
            )
            return False

    async def get_weekly_availability(
        self,
        user_id: int
    ) -> Dict[str, Any]:
        """
        Get weekly availability schedule for a user.

        Args:
            user_id: Hub user ID

        Returns:
            Weekly availability dict (e.g., {"monday": [{"start":"09:00","end":"17:00"}], ...})
        """
        settings = await self.get_settings(user_id)
        return settings.get('weekly_availability', {})

    async def update_weekly_availability(
        self,
        user_id: int,
        availability: Dict[str, Any]
    ) -> bool:
        """
        Update weekly availability schedule for a user.

        Args:
            user_id: Hub user ID
            availability: Weekly availability dict

        Returns:
            True on success, False on failure
        """
        return await self.update_settings(user_id, {
            'weekly_availability': availability
        })

    async def get_free_busy(
        self,
        user_id: int,
        start_date: datetime,
        end_date: datetime
    ) -> List[Dict[str, Any]]:
        """
        Get merged free/busy blocks from all sources.

        Merges:
        1. Connected calendar free/busy data
        2. Existing bookings

        Args:
            user_id: Hub user ID
            start_date: Start of time range
            end_date: End of time range

        Returns:
            List of busy blocks [{"start": datetime, "end": datetime}, ...]
        """
        try:
            busy_blocks = []

            # Get calendar free/busy
            calendar_query = """
                SELECT start_time, end_time, status
                FROM calendar_free_busy
                WHERE hub_user_id = $1
                  AND start_time < $3
                  AND end_time > $2
                ORDER BY start_time
            """

            calendar_rows = await self.dal.execute(
                calendar_query, [user_id, start_date, end_date]
            )

            for row in calendar_rows:
                busy_blocks.append({
                    'start': row['start_time'],
                    'end': row['end_time'],
                    'source': 'calendar'
                })

            # Get existing bookings
            booking_query = """
                SELECT start_time, end_time
                FROM bookings
                WHERE host_user_id = $1
                  AND status IN ('pending', 'confirmed')
                  AND start_time < $3
                  AND end_time > $2
                ORDER BY start_time
            """

            booking_rows = await self.dal.execute(
                booking_query, [user_id, start_date, end_date]
            )

            for row in booking_rows:
                busy_blocks.append({
                    'start': row['start_time'],
                    'end': row['end_time'],
                    'source': 'booking'
                })

            # Sort by start time
            busy_blocks.sort(key=lambda x: x['start'])

            return busy_blocks

        except Exception as e:
            logger.error(
                f"[AVAILABILITY] Get free/busy error for user={user_id}: {e}"
            )
            return []

    async def compute_available_slots(
        self,
        user_id: int,
        date: datetime,
        slot_duration_minutes: int
    ) -> List[Dict[str, Any]]:
        """
        Compute available time slots for a specific date.

        Algorithm:
        1. Get weekly availability for target day of week
        2. Get all busy blocks (calendar + bookings)
        3. Apply min_notice_hours constraint
        4. Apply max_future_days constraint
        5. Generate slots from availability windows
        6. Filter out slots that overlap with busy blocks
        7. Apply buffer_minutes between slots

        Args:
            user_id: Hub user ID
            date: Target date (datetime)
            slot_duration_minutes: Duration of each slot

        Returns:
            List of available slots [{"start": datetime, "end": datetime}, ...]
        """
        try:
            # Get settings
            settings = await self.get_settings(user_id)

            min_notice_hours = settings['min_notice_hours']
            max_future_days = settings['max_future_days']
            buffer_minutes = settings['buffer_minutes']
            weekly_availability = settings['weekly_availability']

            # Get day of week (monday, tuesday, etc.)
            day_name = date.strftime('%A').lower()

            # Get availability windows for this day
            day_availability = weekly_availability.get(day_name, [])

            if not day_availability:
                logger.debug(
                    f"[AVAILABILITY] No availability for {day_name}, user={user_id}"
                )
                return []

            # Calculate time constraints
            now = datetime.now(timezone.utc)
            earliest_slot = now + timedelta(hours=min_notice_hours)
            latest_slot = now + timedelta(days=max_future_days)

            # Check if date is within allowed range
            date_start = datetime.combine(date.date(), time.min, tzinfo=timezone.utc)
            date_end = datetime.combine(date.date(), time.max, tzinfo=timezone.utc)

            if date_end < earliest_slot or date_start > latest_slot:
                logger.debug(
                    f"[AVAILABILITY] Date outside allowed range: {date.date()}, "
                    f"user={user_id}"
                )
                return []

            # Get busy blocks for this date
            busy_blocks = await self.get_free_busy(user_id, date_start, date_end)

            # Generate candidate slots from availability windows
            candidate_slots = []

            for window in day_availability:
                window_start_str = window.get('start')  # e.g., "09:00"
                window_end_str = window.get('end')      # e.g., "17:00"

                if not window_start_str or not window_end_str:
                    continue

                # Parse time strings
                start_time = datetime.strptime(window_start_str, '%H:%M').time()
                end_time = datetime.strptime(window_end_str, '%H:%M').time()

                # Combine with date
                window_start = datetime.combine(date.date(), start_time, tzinfo=timezone.utc)
                window_end = datetime.combine(date.date(), end_time, tzinfo=timezone.utc)

                # Apply min_notice constraint
                if window_start < earliest_slot:
                    window_start = earliest_slot

                # Generate slots within this window
                current = window_start
                while current + timedelta(minutes=slot_duration_minutes) <= window_end:
                    slot_start = current
                    slot_end = current + timedelta(minutes=slot_duration_minutes)

                    # Check if slot is fully in the future
                    if slot_start >= earliest_slot and slot_end <= latest_slot:
                        candidate_slots.append({
                            'start': slot_start,
                            'end': slot_end
                        })

                    # Move to next slot (with buffer)
                    current = slot_end + timedelta(minutes=buffer_minutes)

            # Filter out slots that overlap with busy blocks
            available_slots = []

            for slot in candidate_slots:
                is_available = True

                for busy in busy_blocks:
                    # Check for overlap
                    if (slot['start'] < busy['end'] and slot['end'] > busy['start']):
                        is_available = False
                        break

                if is_available:
                    available_slots.append({
                        'start': slot['start'].isoformat(),
                        'end': slot['end'].isoformat()
                    })

            logger.debug(
                f"[AVAILABILITY] Computed {len(available_slots)} slots for "
                f"{date.date()}, user={user_id}"
            )

            return available_slots

        except Exception as e:
            logger.error(
                f"[AVAILABILITY] Compute slots error for user={user_id}, "
                f"date={date}: {e}"
            )
            return []
