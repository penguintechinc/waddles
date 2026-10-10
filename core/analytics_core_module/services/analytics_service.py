"""
Analytics Service - Core analytics calculations and data retrieval
"""
from dataclasses import dataclass
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
from .log_safety import log_failure


@dataclass
class BasicStats:
    """Basic statistics (Free tier)."""
    total_chatters: int
    total_stream_time_hours: float
    messages_per_user: Dict[str, int]
    active_chatters_7d: int
    active_chatters_30d: int
    updated_at: str


@dataclass
class AnalyticsConfig:
    """Analytics configuration."""
    community_id: int
    is_premium: bool
    basic_stats_enabled: bool
    community_health_enabled: bool
    bad_actor_detection_enabled: bool
    user_journey_enabled: bool
    polling_interval_seconds: int


class AnalyticsService:
    """Core analytics calculations and configuration management."""

    def __init__(self, dal, logger):
        self.dal = dal
        self.logger = logger

    async def get_config(self, community_id: int) -> Dict[str, Any]:
        """
        Get analytics configuration for a community.

        Creates default config if not exists.
        """
        try:
            result = self.dal.executesql(
                """SELECT is_premium, basic_stats_enabled, community_health_enabled,
                          bad_actor_detection_enabled, user_journey_enabled,
                          polling_interval_seconds
                   FROM analytics_config
                   WHERE community_id = %s""",
                [community_id]
            )

            if not result:
                # Create default config
                self.dal.executesql(
                    """INSERT INTO analytics_config (community_id)
                       VALUES (%s)
                       ON CONFLICT (community_id) DO NOTHING""",
                    [community_id]
                )
                self.dal.commit()

                # Return default values
                return {
                    'community_id': community_id,
                    'is_premium': False,
                    'basic_stats_enabled': True,
                    'community_health_enabled': False,
                    'bad_actor_detection_enabled': False,
                    'user_journey_enabled': False,
                    'polling_interval_seconds': 30
                }

            row = result[0]
            return {
                'community_id': community_id,
                'is_premium': row[0],
                'basic_stats_enabled': row[1],
                'community_health_enabled': row[2],
                'bad_actor_detection_enabled': row[3],
                'user_journey_enabled': row[4],
                'polling_interval_seconds': row[5]
            }

        except Exception as e:
            log_failure(self.logger, "Failed to get analytics config", e, community_id=community_id)
            raise

    async def update_config(self, community_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        """Update analytics configuration."""
        try:
            # Build UPDATE statement from provided fields
            allowed_fields = [
                'basic_stats_enabled', 'community_health_enabled',
                'bad_actor_detection_enabled', 'user_journey_enabled',
                'polling_interval_seconds', 'raw_data_retention_days',
                'aggregated_data_retention_days'
            ]

            updates = []
            values = []
            for field in allowed_fields:
                if field in data:
                    updates.append(f"{field} = %s")
                    values.append(data[field])

            if not updates:
                return await self.get_config(community_id)

            values.append(community_id)
            sql = f"""UPDATE analytics_config
                      SET {', '.join(updates)}, updated_at = NOW()
                      WHERE community_id = %s"""

            self.dal.executesql(sql, values)
            self.dal.commit()

            self.logger.audit(
                "Analytics config updated",
                community_id=community_id,
                action="update_config",
                result="SUCCESS"
            )

            return await self.get_config(community_id)

        except Exception as e:
            log_failure(self.logger, "Failed to update analytics config", e, community_id=community_id)
            raise

    async def get_basic_stats(self, community_id: int) -> Dict[str, Any]:
        """
        Get basic statistics (Free tier).

        Returns:
        - Total chatters (unique users who sent messages)
        - Total stream time in hours
        - Messages per user (top 10)
        - Active chatters (7d and 30d)
        """
        try:
            # Total unique chatters (all time)
            total_chatters_result = self.dal.executesql(
                """SELECT COUNT(DISTINCT hub_user_id)
                   FROM activity_message_events
                   WHERE community_id = %s""",
                [community_id]
            )
            total_chatters = total_chatters_result[0][0] if total_chatters_result else 0

            # Total stream time (sum of all watch sessions)
            stream_time_result = self.dal.executesql(
                """SELECT COALESCE(SUM(duration_seconds), 0)
                   FROM activity_watch_sessions
                   WHERE community_id = %s AND duration_seconds IS NOT NULL""",
                [community_id]
            )
            total_seconds = stream_time_result[0][0] if stream_time_result else 0
            total_stream_time_hours = round(total_seconds / 3600, 2)

            # Messages per user (top 10)
            messages_per_user_result = self.dal.executesql(
                """SELECT
                       COALESCE(hu.username, ame.platform_username, ame.platform_user_id) as username,
                       COUNT(*) as message_count
                   FROM activity_message_events ame
                   LEFT JOIN hub_users hu ON ame.hub_user_id = hu.id
                   WHERE ame.community_id = %s
                   GROUP BY username
                   ORDER BY message_count DESC
                   LIMIT 10""",
                [community_id]
            )

            messages_per_user = {}
            if messages_per_user_result:
                for row in messages_per_user_result:
                    messages_per_user[row[0]] = row[1]

            # Active chatters (7 days)
            active_7d_result = self.dal.executesql(
                """SELECT COUNT(DISTINCT hub_user_id)
                   FROM activity_message_events
                   WHERE community_id = %s
                   AND created_at >= NOW() - INTERVAL '7 days'""",
                [community_id]
            )
            active_7d = active_7d_result[0][0] if active_7d_result else 0

            # Active chatters (30 days)
            active_30d_result = self.dal.executesql(
                """SELECT COUNT(DISTINCT hub_user_id)
                   FROM activity_message_events
                   WHERE community_id = %s
                   AND created_at >= NOW() - INTERVAL '30 days'""",
                [community_id]
            )
            active_30d = active_30d_result[0][0] if active_30d_result else 0

            return {
                'total_chatters': total_chatters,
                'total_stream_time_hours': total_stream_time_hours,
                'messages_per_user': messages_per_user,
                'active_chatters_7d': active_7d,
                'active_chatters_30d': active_30d,
                'updated_at': datetime.utcnow().isoformat() + 'Z'
            }

        except Exception as e:
            log_failure(self.logger, "Failed to get basic stats", e, community_id=community_id)
            raise

    async def process_events(self, events: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Process incoming activity events.

        Events are typically sent by the router module.
        This method aggregates them for analytics.
        """
        try:
            # For now, just acknowledge receipt
            # Full implementation would process and aggregate events
            processed = len(events)

            self.logger.audit(
                "Events processed",
                action="process_events",
                events_count=processed,
                result="SUCCESS"
            )

            return {
                'processed': processed,
                'status': 'success'
            }

        except Exception as e:
            log_failure(self.logger, "Failed to process events", e)
            raise

    async def run_aggregation(
        self,
        community_id: Optional[int] = None,
        force: bool = False
    ) -> Dict[str, Any]:
        """
        Run aggregation job to update time-series metrics.

        Args:
            community_id: Specific community (None = all communities)
            force: Force aggregation even if recently run
        """
        try:
            # Placeholder - full implementation would aggregate data
            self.logger.audit(
                "Aggregation triggered",
                community_id=community_id,
                force=force,
                action="run_aggregation",
                result="SUCCESS"
            )

            return {
                'status': 'queued',
                'community_id': community_id
            }

        except Exception as e:
            log_failure(self.logger, "Failed to run aggregation", e, community_id=community_id)
            raise
