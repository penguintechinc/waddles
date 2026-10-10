"""
Polling Service - REST polling endpoint handler for real-time updates
"""
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
from dateutil import parser as date_parser
from .log_safety import log_failure


class PollingService:
    """Handle REST polling for real-time dashboard updates."""

    def __init__(self, dal, logger):
        self.dal = dal
        self.logger = logger

    async def get_updates(
        self,
        community_id: int,
        since: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get updates since a specific timestamp.

        This endpoint is polled by the frontend every 30 seconds.

        Args:
            community_id: Community ID
            since: ISO timestamp of last update (optional)

        Returns:
            hasUpdates: boolean
            updates: dict of updated data (if hasUpdates=true)
            timestamp: current server timestamp
        """
        try:
            current_timestamp = datetime.utcnow()

            # Parse since timestamp
            if since:
                since_dt = date_parser.parse(since)
            else:
                # If no since provided, return last 5 minutes
                since_dt = current_timestamp - timedelta(minutes=5)

            # Check for recent activity (messages in last polling interval)
            recent_messages_result = self.dal.executesql(
                """SELECT COUNT(*) FROM activity_message_events
                   WHERE community_id = %s
                   AND created_at > %s""",
                [community_id, since_dt]
            )
            recent_messages = recent_messages_result[0][0] if recent_messages_result else 0

            # Check for recent viewers
            recent_viewers_result = self.dal.executesql(
                """SELECT COUNT(DISTINCT hub_user_id) FROM activity_watch_sessions
                   WHERE community_id = %s
                   AND session_start > %s""",
                [community_id, since_dt]
            )
            recent_viewers = recent_viewers_result[0][0] if recent_viewers_result else 0

            # Determine if there are meaningful updates
            has_updates = (recent_messages > 0 or recent_viewers > 0)

            response = {
                'hasUpdates': has_updates,
                'timestamp': current_timestamp.isoformat() + 'Z'
            }

            if has_updates:
                # Get current active viewer count
                active_viewers_result = self.dal.executesql(
                    """SELECT COUNT(*) FROM activity_watch_sessions
                       WHERE community_id = %s
                       AND is_active = true""",
                    [community_id]
                )
                active_viewers = active_viewers_result[0][0] if active_viewers_result else 0

                response['updates'] = {
                    'active_viewers': active_viewers,
                    'recent_messages': recent_messages,
                    'recent_new_viewers': recent_viewers
                }

            return response

        except Exception as e:
            log_failure(self.logger, "Failed to get polling updates", e, community_id=community_id)
            raise
