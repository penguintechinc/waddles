"""
User Behavior Profiler Service for AI Researcher Module
=======================================================

Builds and maintains user behavior profiles for community members.

Features:
- Baseline behavior establishment from historical data
- Activity pattern analysis (timing, frequency)
- Communication style profiling
- Community role identification
- Behavior change detection
"""

import logging
import json
import time
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from dataclasses import dataclass

from config import Config
from flask_core import describe_db_error

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BehaviorProfile:
    """User behavior profile data"""
    success: bool
    profile_id: Optional[int]
    user_id: str
    username: str
    activity_level: str
    communication_style: str
    preferred_hours: List[int]
    average_message_length: float
    total_messages: int
    community_role: str
    processing_time_ms: int
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses"""
        return {
            'success': self.success,
            'profile_id': self.profile_id,
            'user_id': self.user_id,
            'username': self.username,
            'activity_level': self.activity_level,
            'communication_style': self.communication_style,
            'preferred_hours': self.preferred_hours,
            'average_message_length': self.average_message_length,
            'total_messages': self.total_messages,
            'community_role': self.community_role,
            'processing_time_ms': self.processing_time_ms,
            'error': self.error
        }


class BehaviorProfiler:
    """
    Service for building user behavior profiles.

    Analyzes:
    - Activity patterns (when users post)
    - Message patterns (length, style, frequency)
    - Community role (lurker, active, moderator, etc.)
    - Temporal patterns (timezone, preferred hours)
    """

    ACTIVITY_LEVEL_THRESHOLDS = {
        'lurker': 0,
        'passive': 10,
        'moderate': 50,
        'active': 200,
        'very_active': 1000
    }

    COMMUNITY_ROLES = {
        'lurker': 'Primarily observes, minimal participation',
        'occasional': 'Participates infrequently',
        'regular': 'Consistent participation',
        'power_user': 'Very high engagement and influence',
        'moderator': 'Community leadership role',
        'bot': 'Automated account'
    }

    def __init__(self, dal):
        """
        Initialize behavior profiler.

        Args:
            dal: Database connection (AsyncDAL)
        """
        self.dal = dal
        logger.info("BehaviorProfiler initialized")

    async def profile_user_behavior(
        self,
        community_id: int,
        platform: str,
        platform_user_id: str,
        username: str = "",
        days: int = 90
    ) -> BehaviorProfile:
        """
        Build a behavior profile for a user.

        Args:
            community_id: Community identifier
            platform: Platform name (twitch, discord, slack, etc.)
            platform_user_id: User ID on the platform
            username: Optional username (if different from user_id)
            days: Historical data period to analyze

        Returns:
            BehaviorProfile with analysis results
        """
        start_time = time.time()

        try:
            logger.info(
                f"Profiling user behavior",
                extra={
                    'community_id': community_id,
                    'platform': platform,
                    'user_id': platform_user_id,
                    'days': days
                }
            )

            # Gather user data
            period_start = datetime.utcnow() - timedelta(days=days)

            user_stats = await self._get_user_statistics(
                community_id,
                platform_user_id,
                period_start
            )

            if not user_stats or user_stats['total_messages'] == 0:
                logger.warning(
                    "No message history found for user",
                    extra={'platform_user_id': platform_user_id}
                )
                return BehaviorProfile(
                    success=False,
                    profile_id=None,
                    user_id=platform_user_id,
                    username=username or platform_user_id,
                    activity_level='unknown',
                    communication_style='insufficient_data',
                    preferred_hours=[],
                    average_message_length=0,
                    total_messages=0,
                    community_role='unknown',
                    processing_time_ms=int((time.time() - start_time) * 1000),
                    error="Insufficient message history"
                )

            # Analyze activity patterns
            activity_level = self._classify_activity_level(
                user_stats['total_messages']
            )

            # Analyze temporal patterns
            preferred_hours = await self._analyze_temporal_patterns(
                community_id,
                platform_user_id,
                period_start
            )

            # Analyze communication style
            communication_style = self._analyze_communication_style(user_stats)

            # Determine community role
            community_role = self._determine_community_role(
                activity_level,
                user_stats,
                community_id
            )

            # Store profile
            profile_id = await self._store_profile(
                community_id,
                platform,
                platform_user_id,
                username or platform_user_id,
                {
                    'activity_level': activity_level,
                    'communication_style': communication_style,
                    'preferred_hours': preferred_hours,
                    'total_messages': user_stats['total_messages'],
                    'avg_message_length': user_stats['avg_message_length'],
                    'days_active': user_stats['days_active'],
                    'messages_per_day': user_stats['messages_per_day'],
                    'community_role': community_role
                }
            )

            processing_time = int((time.time() - start_time) * 1000)

            logger.info(
                f"User behavior profile completed",
                extra={
                    'profile_id': profile_id,
                    'platform_user_id': platform_user_id,
                    'activity_level': activity_level,
                    'processing_time_ms': processing_time
                }
            )

            return BehaviorProfile(
                success=True,
                profile_id=profile_id,
                user_id=platform_user_id,
                username=username or platform_user_id,
                activity_level=activity_level,
                communication_style=communication_style,
                preferred_hours=preferred_hours,
                average_message_length=user_stats['avg_message_length'],
                total_messages=user_stats['total_messages'],
                community_role=community_role,
                processing_time_ms=processing_time
            )

        except Exception as e:
            logger.error(
                f"Behavior profiling error: {describe_db_error(e)}",
                extra={'platform_user_id': platform_user_id}
            )
            return BehaviorProfile(
                success=False,
                profile_id=None,
                user_id=platform_user_id,
                username=username or platform_user_id,
                activity_level='error',
                communication_style='error',
                preferred_hours=[],
                average_message_length=0,
                total_messages=0,
                community_role='error',
                processing_time_ms=int((time.time() - start_time) * 1000),
                error=describe_db_error(e)
            )

    async def _get_user_statistics(
        self,
        community_id: int,
        platform_user_id: str,
        period_start: datetime
    ) -> Optional[Dict[str, Any]]:
        """
        Get user message statistics.

        Args:
            community_id: Community identifier
            platform_user_id: User ID
            period_start: Analysis period start

        Returns:
            Dict with statistics or None
        """
        try:
            query = """
                SELECT
                    COUNT(*) as total_messages,
                    AVG(LENGTH(message_content)) as avg_message_length,
                    COUNT(DISTINCT DATE(created_at)) as days_active,
                    MIN(created_at) as first_message,
                    MAX(created_at) as last_message,
                    COUNT(DISTINCT EXTRACT(HOUR FROM created_at)) as hours_active
                FROM ai_context_messages
                WHERE community_id = $1
                  AND platform_user_id = $2
                  AND created_at >= $3
            """

            rows = await self.dal.execute(query, [community_id, platform_user_id, period_start])

            if not rows:
                return None

            row = rows[0]
            total_messages = row.get('total_messages', 0)
            days_active = max(row.get('days_active', 1), 1)

            return {
                'total_messages': total_messages,
                'avg_message_length': float(row.get('avg_message_length', 0)) if row.get('avg_message_length') else 0,
                'days_active': days_active,
                'hours_active': row.get('hours_active', 0),
                'messages_per_day': total_messages / days_active
            }

        except Exception as e:
            logger.error(f"User statistics error: {describe_db_error(e)}")
            return None

    async def _analyze_temporal_patterns(
        self,
        community_id: int,
        platform_user_id: str,
        period_start: datetime
    ) -> List[int]:
        """
        Analyze when user typically posts.

        Args:
            community_id: Community identifier
            platform_user_id: User ID
            period_start: Analysis period start

        Returns:
            List of preferred hours (0-23)
        """
        try:
            query = """
                SELECT
                    EXTRACT(HOUR FROM created_at)::int as hour,
                    COUNT(*) as message_count
                FROM ai_context_messages
                WHERE community_id = $1
                  AND platform_user_id = $2
                  AND created_at >= $3
                GROUP BY EXTRACT(HOUR FROM created_at)
                ORDER BY message_count DESC
                LIMIT 3
            """

            rows = await self.dal.execute(query, [community_id, platform_user_id, period_start])

            if not rows:
                return []

            # Return top 3 hours
            hours = [int(row.get('hour', 0)) for row in rows]
            return sorted(hours)

        except Exception as e:
            logger.error(f"Temporal pattern analysis error: {describe_db_error(e)}")
            return []

    def _classify_activity_level(self, total_messages: int) -> str:
        """
        Classify user activity level based on message count.

        Args:
            total_messages: Total messages in period

        Returns:
            Activity level classification
        """
        thresholds = [
            (self.ACTIVITY_LEVEL_THRESHOLDS['very_active'], 'very_active'),
            (self.ACTIVITY_LEVEL_THRESHOLDS['active'], 'active'),
            (self.ACTIVITY_LEVEL_THRESHOLDS['moderate'], 'moderate'),
            (self.ACTIVITY_LEVEL_THRESHOLDS['passive'], 'passive'),
            (0, 'lurker')
        ]

        for threshold, level in thresholds:
            if total_messages >= threshold:
                return level

        return 'lurker'

    def _analyze_communication_style(self, user_stats: Dict[str, Any]) -> str:
        """
        Analyze user communication style.

        Args:
            user_stats: User statistics dict

        Returns:
            Communication style classification
        """
        avg_length = user_stats.get('avg_message_length', 0)
        messages_per_day = user_stats.get('messages_per_day', 0)

        # Classify based on message characteristics
        if avg_length < 20:
            return 'brief_frequent'
        elif avg_length > 200:
            return 'verbose'
        elif messages_per_day > 10:
            return 'conversational'
        else:
            return 'thoughtful'

    def _determine_community_role(
        self,
        activity_level: str,
        user_stats: Dict[str, Any],
        community_id: int
    ) -> str:
        """
        Determine user's role in the community.

        Args:
            activity_level: Activity level classification
            user_stats: User statistics
            community_id: Community identifier

        Returns:
            Community role
        """
        # Simple heuristic - can be enhanced with more data
        if activity_level == 'lurker':
            return 'lurker'
        elif activity_level in ['passive', 'moderate']:
            return 'occasional'
        elif activity_level in ['active', 'very_active']:
            return 'regular'
        else:
            return 'unknown'

    async def _store_profile(
        self,
        community_id: int,
        platform: str,
        platform_user_id: str,
        username: str,
        profile_data: Dict[str, Any]
    ) -> Optional[int]:
        """
        Store user behavior profile in database.

        Args:
            community_id: Community identifier
            platform: Platform name
            platform_user_id: User ID on platform
            username: Username
            profile_data: Profile analysis data

        Returns:
            Profile ID or None on error
        """
        try:
            query = """
                INSERT INTO ai_user_behavior_profiles (
                    community_id, platform, platform_user_id,
                    platform_username, profile_data,
                    created_at, updated_at
                ) VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
                ON CONFLICT (community_id, platform, platform_user_id)
                DO UPDATE SET
                    profile_data = $5,
                    updated_at = NOW()
                RETURNING id
            """

            rows = await self.dal.execute(query, [
                community_id,
                platform,
                platform_user_id,
                username,
                json.dumps(profile_data)
            ])

            if rows:
                return rows[0].get('id')

            return None

        except Exception as e:
            logger.error(f"Profile storage error: {describe_db_error(e)}")
            return None

    async def get_user_profile(
        self,
        community_id: int,
        platform: str,
        platform_user_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        Retrieve stored user behavior profile.

        Args:
            community_id: Community identifier
            platform: Platform name
            platform_user_id: User ID on platform

        Returns:
            Profile dict or None
        """
        try:
            query = """
                SELECT id, profile_data, created_at, updated_at
                FROM ai_user_behavior_profiles
                WHERE community_id = $1
                  AND platform = $2
                  AND platform_user_id = $3
            """

            rows = await self.dal.execute(query, [
                community_id,
                platform,
                platform_user_id
            ])

            if not rows:
                return None

            row = rows[0]
            profile_data = row.get('profile_data')

            if isinstance(profile_data, str):
                profile_data = json.loads(profile_data)

            return {
                'id': row['id'],
                'data': profile_data,
                'created_at': str(row['created_at']),
                'updated_at': str(row['updated_at'])
            }

        except Exception as e:
            logger.error(f"Get profile error: {describe_db_error(e)}")
            return None

    async def get_community_profiles(
        self,
        community_id: int,
        role: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Get profiles for multiple users in a community.

        Args:
            community_id: Community identifier
            role: Optional filter by community role

        Returns:
            List of profile dicts
        """
        try:
            where_clause = ""
            params = [community_id]

            if role:
                where_clause = "AND profile_data->>'community_role' = $2"
                params.append(role)

            query = f"""
                SELECT id, platform_user_id, platform_username,
                       profile_data, created_at, updated_at
                FROM ai_user_behavior_profiles
                WHERE community_id = $1
                  {where_clause}
                ORDER BY updated_at DESC
                LIMIT 100
            """

            rows = await self.dal.execute(query, params)

            profiles = []
            for row in (rows or []):
                profile_data = row.get('profile_data')

                if isinstance(profile_data, str):
                    profile_data = json.loads(profile_data)

                profiles.append({
                    'id': row['id'],
                    'user_id': row['platform_user_id'],
                    'username': row['platform_username'],
                    'data': profile_data,
                    'created_at': str(row['created_at']),
                    'updated_at': str(row['updated_at'])
                })

            return profiles

        except Exception as e:
            logger.error(f"Get community profiles error: {describe_db_error(e)}")
            return []
