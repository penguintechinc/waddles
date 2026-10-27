"""
Spam Detector Service - Detects spam based on message frequency and duplicates
"""
from typing import Optional, Dict, Tuple
from datetime import datetime, timedelta
import redis
from config import Config


class SpamDetector:
    """Spam detection based on message frequency and duplicate detection."""

    def __init__(self, dal, logger):
        self.dal = dal
        self.logger = logger

        # Initialize Redis connection for fast rate limiting
        try:
            self.redis = redis.Redis.from_url(
                Config.REDIS_URL,
                decode_responses=True
            )
            self.redis.ping()
            self.use_redis = True
            self.logger.system("Redis connected for spam detection", result="SUCCESS")
        except Exception as e:
            self.logger.warning(f"Redis not available, using DB fallback: {e}")
            self.use_redis = False

    async def check_spam(
        self,
        community_id: int,
        platform: str,
        platform_user_id: str,
        message: Optional[str] = None
    ) -> bool:
        """
        Check if user is spamming.

        Returns:
            True if spam detected, False otherwise
        """
        try:
            # Get community config
            config = await self._get_config(community_id)

            if not config['spam_detection_enabled']:
                return False

            # Check message frequency
            is_frequency_spam = await self._check_message_frequency(
                community_id=community_id,
                platform=platform,
                platform_user_id=platform_user_id,
                threshold=config['spam_message_threshold'],
                interval_seconds=config['spam_interval_seconds']
            )

            if is_frequency_spam:
                self.logger.audit(
                    action="spam_detection",
                    user="system",
                    community=str(community_id),
                    result="BLOCKED",
                    detail="Spam detected: frequency",
                    platform=platform,
                    platform_user_id=platform_user_id
                )
                return True

            # Check duplicate messages (if message provided)
            if message:
                is_duplicate_spam = await self._check_duplicate_messages(
                    community_id=community_id,
                    platform=platform,
                    platform_user_id=platform_user_id,
                    message=message,
                    threshold=config['spam_duplicate_threshold']
                )

                if is_duplicate_spam:
                    self.logger.audit(
                        action="spam_detection",
                        user="system",
                        community=str(community_id),
                        result="BLOCKED",
                        detail="Spam detected: duplicate",
                        platform=platform,
                        platform_user_id=platform_user_id
                    )
                    return True

            return False

        except Exception as e:
            self.logger.error(f"Failed to check spam: {e}")
            # Fail open - don't block on error
            return False

    async def _get_config(self, community_id: int) -> Dict:
        """Get spam detection config for community."""
        result = self.dal.executesql(
            """SELECT spam_detection_enabled, spam_message_threshold,
                      spam_interval_seconds, spam_duplicate_threshold
               FROM security_config
               WHERE community_id = %s""",
            [community_id]
        )

        if not result:
            # Return defaults
            return {
                'spam_detection_enabled': True,
                'spam_message_threshold': Config.DEFAULT_SPAM_MESSAGE_THRESHOLD,
                'spam_interval_seconds': Config.DEFAULT_SPAM_INTERVAL_SECONDS,
                'spam_duplicate_threshold': Config.DEFAULT_SPAM_DUPLICATE_THRESHOLD
            }

        row = result[0]
        return {
            'spam_detection_enabled': row[0],
            'spam_message_threshold': row[1],
            'spam_interval_seconds': row[2],
            'spam_duplicate_threshold': row[3]
        }

    async def _check_message_frequency(
        self,
        community_id: int,
        platform: str,
        platform_user_id: str,
        threshold: int,
        interval_seconds: int
    ) -> bool:
        """
        Check if user exceeded message frequency threshold.

        Uses Redis for fast checks, falls back to DB.
        """
        key = f"spam:freq:{community_id}:{platform}:{platform_user_id}"

        if self.use_redis:
            try:
                # Increment counter
                count = self.redis.incr(key)

                # Set expiry on first message
                if count == 1:
                    self.redis.expire(key, interval_seconds)

                return count > threshold

            except Exception as e:
                self.logger.warning(f"Redis check failed, using DB: {e}")
                # Fall through to DB check

        # DB fallback
        window_start = datetime.utcnow() - timedelta(seconds=interval_seconds)
        result = self.dal.executesql(
            """SELECT COUNT(*) FROM activity_message_events
               WHERE community_id = %s
               AND platform = %s
               AND platform_user_id = %s
               AND created_at >= %s""",
            [community_id, platform, platform_user_id, window_start]
        )

        count = result[0][0] if result else 0
        return count > threshold

    async def _check_duplicate_messages(
        self,
        community_id: int,
        platform: str,
        platform_user_id: str,
        message: str,
        threshold: int
    ) -> bool:
        """
        Check if user sent same message multiple times recently.

        Looks at last 10 messages from user.
        """
        try:
            result = self.dal.executesql(
                """SELECT message_content FROM activity_message_events
                   WHERE community_id = %s
                   AND platform = %s
                   AND platform_user_id = %s
                   AND created_at >= NOW() - INTERVAL '5 minutes'
                   ORDER BY created_at DESC
                   LIMIT 10""",
                [community_id, platform, platform_user_id]
            )

            if not result:
                return False

            # Count occurrences of this message
            message_lower = message.lower().strip()
            duplicate_count = sum(
                1 for row in result
                if row[0] and row[0].lower().strip() == message_lower
            )

            return duplicate_count >= threshold

        except Exception as e:
            self.logger.error(f"Failed to check duplicates: {e}")
            return False
