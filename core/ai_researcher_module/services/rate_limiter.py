"""
Rate Limiter Service
=====================

Per-user and per-community rate limiting with Redis primary and DB fallback.
Uses sliding window algorithm for accurate rate limiting.

Features:
- Per-user rate limiting
- Per-community rate limiting
- Multiple limit types (research, ask, recall)
- Redis primary with database fallback
- Fail-open for availability (if both fail, allow request)
- Comprehensive AAA logging
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import redis.asyncio as redis
from flask_core import describe_db_error

logger = logging.getLogger(__name__)


@dataclass
class RateLimitResult:
    """Result of a rate limit check"""
    allowed: bool
    remaining: int
    reset_at: datetime
    limit: int


class RateLimiter:
    """
    Rate limiter with Redis primary and database fallback.

    Rate limit types and defaults:
    - "research": 5 per user per hour, 50 per community per hour
    - "ask": 10 per user per hour, 100 per community per hour
    - "recall": 20 per user per hour, 200 per community per hour
    - "game": 20 per user per hour, 200 per community per hour
    - "game_quick": 60 per user per hour, 600 per community per hour
    """

    # Default rate limits (per hour)
    DEFAULT_LIMITS = {
        'research': {'user': 5, 'community': 50},
        'ask': {'user': 10, 'community': 100},
        'recall': {'user': 20, 'community': 200},
        'game': {'user': 20, 'community': 200},
        'game_quick': {'user': 60, 'community': 600},
        'patch_notes': {'user': 15, 'community': 150},
        'build_advisor': {'user': 15, 'community': 150},
        'meta_lookup': {'user': 20, 'community': 200},
        'tech_fix': {'user': 10, 'community': 100},
        'price_lookup': {'user': 20, 'community': 200},
        'deals_lookup': {'user': 30, 'community': 300},
        'clip_research': {'user': 20, 'community': 200},
        'event_lookup': {'user': 20, 'community': 200},
        'tournament_lookup': {'user': 15, 'community': 150},
    }

    def __init__(self, redis_client: redis.Redis, db_connection=None):
        """
        Initialize rate limiter.

        Args:
            redis_client: Redis asyncio client instance
            db_connection: Optional database connection for fallback
        """
        self.redis = redis_client
        self.db = db_connection
        self.logger = logger

    async def check_limit(
        self,
        community_id: int,
        user_id: str,
        limit_type: str = "research"
    ) -> RateLimitResult:
        """
        Check if request is within rate limits WITHOUT incrementing.

        Args:
            community_id: Community identifier
            user_id: User identifier
            limit_type: Type of limit (research, ask, recall)

        Returns:
            RateLimitResult with allowed status and metadata
        """
        try:
            # Get current hour timestamp
            now = datetime.utcnow()
            hour_key = now.strftime('%Y%m%d%H')

            # Get limits for this type
            limits = self.DEFAULT_LIMITS.get(
                limit_type,
                {'user': 10, 'community': 100}
            )

            # Check user limit
            user_key = f"ratelimit:{community_id}:{user_id}:{limit_type}:{hour_key}"
            user_count = await self._get_count_redis(user_key)

            if user_count >= limits['user']:
                reset_at = now.replace(
                    minute=0,
                    second=0,
                    microsecond=0
                ) + timedelta(hours=1)
                return RateLimitResult(
                    allowed=False,
                    remaining=0,
                    reset_at=reset_at,
                    limit=limits['user']
                )

            # Check community limit
            community_key = (
                f"ratelimit:{community_id}:community:{limit_type}:{hour_key}"
            )
            community_count = await self._get_count_redis(community_key)

            if community_count >= limits['community']:
                reset_at = now.replace(
                    minute=0,
                    second=0,
                    microsecond=0
                ) + timedelta(hours=1)
                return RateLimitResult(
                    allowed=False,
                    remaining=0,
                    reset_at=reset_at,
                    limit=limits['community']
                )

            # Both limits OK
            reset_at = now.replace(
                minute=0,
                second=0,
                microsecond=0
            ) + timedelta(hours=1)
            return RateLimitResult(
                allowed=True,
                remaining=limits['user'] - user_count,
                reset_at=reset_at,
                limit=limits['user']
            )

        except Exception as e:
            self.logger.error(
                f"Rate limit check failed: {describe_db_error(e)}",
                extra={
                    'community_id': community_id,
                    'user_id': user_id,
                    'limit_type': limit_type,
                    'action': 'check_limit',
                    'result': 'ERROR'
                }
            )
            # Fail-open: allow on error
            return RateLimitResult(
                allowed=True,
                remaining=999,
                reset_at=datetime.utcnow() + timedelta(hours=1),
                limit=999
            )

    async def increment(
        self,
        community_id: int,
        user_id: str,
        limit_type: str = "research"
    ) -> RateLimitResult:
        """
        Check rate limit AND increment counter if allowed.

        Args:
            community_id: Community identifier
            user_id: User identifier
            limit_type: Type of limit (research, ask, recall)

        Returns:
            RateLimitResult with allowed status and metadata
        """
        try:
            # Get current hour timestamp
            now = datetime.utcnow()
            hour_key = now.strftime('%Y%m%d%H')

            # Get limits for this type
            limits = self.DEFAULT_LIMITS.get(
                limit_type,
                {'user': 10, 'community': 100}
            )

            # Check and increment user limit
            user_key = f"ratelimit:{community_id}:{user_id}:{limit_type}:{hour_key}"
            user_count = await self._increment_redis(user_key)

            if user_count > limits['user']:
                # Over user limit - decrement back
                await self._decrement_redis(user_key)
                reset_at = now.replace(
                    minute=0,
                    second=0,
                    microsecond=0
                ) + timedelta(hours=1)

                # Log rate limit hit
                self.logger.warning(
                    f"User rate limit exceeded: {user_id}",
                    extra={
                        'community_id': community_id,
                        'user_id': user_id,
                        'limit_type': limit_type,
                        'count': user_count - 1,
                        'limit': limits['user'],
                        'action': 'rate_limit_user',
                        'result': 'BLOCKED'
                    }
                )

                return RateLimitResult(
                    allowed=False,
                    remaining=0,
                    reset_at=reset_at,
                    limit=limits['user']
                )

            # Check and increment community limit
            community_key = (
                f"ratelimit:{community_id}:community:{limit_type}:{hour_key}"
            )
            community_count = await self._increment_redis(community_key)

            if community_count > limits['community']:
                # Over community limit - decrement both back
                await self._decrement_redis(user_key)
                await self._decrement_redis(community_key)
                reset_at = now.replace(
                    minute=0,
                    second=0,
                    microsecond=0
                ) + timedelta(hours=1)

                # Log rate limit hit
                self.logger.warning(
                    f"Community rate limit exceeded: {community_id}",
                    extra={
                        'community_id': community_id,
                        'user_id': user_id,
                        'limit_type': limit_type,
                        'count': community_count - 1,
                        'limit': limits['community'],
                        'action': 'rate_limit_community',
                        'result': 'BLOCKED'
                    }
                )

                return RateLimitResult(
                    allowed=False,
                    remaining=0,
                    reset_at=reset_at,
                    limit=limits['community']
                )

            # Both limits OK - request allowed
            reset_at = now.replace(
                minute=0,
                second=0,
                microsecond=0
            ) + timedelta(hours=1)

            self.logger.info(
                f"Rate limit check passed",
                extra={
                    'community_id': community_id,
                    'user_id': user_id,
                    'limit_type': limit_type,
                    'user_count': user_count,
                    'community_count': community_count,
                    'action': 'rate_limit_check',
                    'result': 'ALLOWED'
                }
            )

            return RateLimitResult(
                allowed=True,
                remaining=limits['user'] - user_count,
                reset_at=reset_at,
                limit=limits['user']
            )

        except Exception as e:
            self.logger.error(
                f"Rate limit increment failed: {describe_db_error(e)}",
                extra={
                    'community_id': community_id,
                    'user_id': user_id,
                    'limit_type': limit_type,
                    'action': 'increment',
                    'result': 'ERROR'
                }
            )
            # Fail-open: allow on error
            return RateLimitResult(
                allowed=True,
                remaining=999,
                reset_at=datetime.utcnow() + timedelta(hours=1),
                limit=999
            )

    async def get_usage(
        self,
        community_id: int,
        user_id: Optional[str] = None
    ) -> dict:
        """
        Get current rate limit usage.

        Args:
            community_id: Community identifier
            user_id: Optional user identifier (if None, returns community usage)

        Returns:
            Dictionary with usage information per limit type
        """
        try:
            now = datetime.utcnow()
            hour_key = now.strftime('%Y%m%d%H')
            usage = {}

            for limit_type, limits in self.DEFAULT_LIMITS.items():
                if user_id:
                    # Get user usage
                    key = (
                        f"ratelimit:{community_id}:{user_id}:"
                        f"{limit_type}:{hour_key}"
                    )
                    count = await self._get_count_redis(key)
                    usage[limit_type] = {
                        'count': count,
                        'limit': limits['user'],
                        'remaining': max(0, limits['user'] - count)
                    }
                else:
                    # Get community usage
                    key = (
                        f"ratelimit:{community_id}:community:"
                        f"{limit_type}:{hour_key}"
                    )
                    count = await self._get_count_redis(key)
                    usage[limit_type] = {
                        'count': count,
                        'limit': limits['community'],
                        'remaining': max(0, limits['community'] - count)
                    }

            reset_at = now.replace(
                minute=0,
                second=0,
                microsecond=0
            ) + timedelta(hours=1)

            return {
                'community_id': community_id,
                'user_id': user_id,
                'usage': usage,
                'reset_at': reset_at.isoformat()
            }

        except Exception as e:
            self.logger.error(
                f"Get usage failed: {describe_db_error(e)}",
                extra={
                    'community_id': community_id,
                    'user_id': user_id,
                    'action': 'get_usage',
                    'result': 'ERROR'
                }
            )
            return {
                'community_id': community_id,
                'user_id': user_id,
                'usage': {},
                'error': describe_db_error(e)
            }

    async def reset_user(self, community_id: int, user_id: str) -> bool:
        """
        Reset rate limits for a specific user.

        Args:
            community_id: Community identifier
            user_id: User identifier

        Returns:
            True if reset successful, False otherwise
        """
        try:
            now = datetime.utcnow()
            hour_key = now.strftime('%Y%m%d%H')

            # Delete all limit type keys for this user
            for limit_type in self.DEFAULT_LIMITS.keys():
                key = (
                    f"ratelimit:{community_id}:{user_id}:"
                    f"{limit_type}:{hour_key}"
                )
                await self._delete_redis(key)

            self.logger.info(
                f"User rate limits reset",
                extra={
                    'community_id': community_id,
                    'user_id': user_id,
                    'action': 'reset_user',
                    'result': 'SUCCESS'
                }
            )

            return True

        except Exception as e:
            self.logger.error(
                f"Reset user failed: {describe_db_error(e)}",
                extra={
                    'community_id': community_id,
                    'user_id': user_id,
                    'action': 'reset_user',
                    'result': 'ERROR'
                }
            )
            return False

    # =========================================================================
    # REDIS OPERATIONS (with DB fallback)
    # =========================================================================

    async def _get_count_redis(self, key: str) -> int:
        """
        Get count from Redis with database fallback.

        Args:
            key: Redis key

        Returns:
            Current count (0 if key doesn't exist)
        """
        try:
            value = await self.redis.get(key)
            return int(value) if value else 0
        except Exception as e:
            self.logger.warning(
                f"Redis get failed, trying DB fallback: {describe_db_error(e)}"
            )
            return await self._get_count_db(key)

    async def _increment_redis(self, key: str) -> int:
        """
        Increment counter in Redis with database fallback.
        Sets 2-hour expiry to auto-cleanup old keys.

        Args:
            key: Redis key

        Returns:
            New count after increment
        """
        try:
            # Increment and set expiry (2 hours to allow for cleanup)
            count = await self.redis.incr(key)
            await self.redis.expire(key, 7200)  # 2 hours
            return count
        except Exception as e:
            self.logger.warning(
                f"Redis increment failed, trying DB fallback: {describe_db_error(e)}"
            )
            return await self._increment_db(key)

    async def _decrement_redis(self, key: str) -> int:
        """
        Decrement counter in Redis.

        Args:
            key: Redis key

        Returns:
            New count after decrement
        """
        try:
            count = await self.redis.decr(key)
            return max(0, count)  # Don't go below 0
        except Exception as e:
            self.logger.warning(f"Redis decrement failed: {describe_db_error(e)}")
            return 0

    async def _delete_redis(self, key: str) -> bool:
        """
        Delete key from Redis.

        Args:
            key: Redis key

        Returns:
            True if deleted, False otherwise
        """
        try:
            await self.redis.delete(key)
            return True
        except Exception as e:
            self.logger.warning(f"Redis delete failed: {describe_db_error(e)}")
            return False

    # =========================================================================
    # DATABASE FALLBACK OPERATIONS
    # =========================================================================

    async def _get_count_db(self, key: str) -> int:
        """
        Get count from database (fallback).

        Args:
            key: Rate limit key

        Returns:
            Current count (0 if not found or error)
        """
        if not self.db:
            return 0

        try:
            # Query ai_rate_limit_state table
            query = """
                SELECT count
                FROM ai_rate_limit_state
                WHERE key = %s
                AND expires_at > NOW()
            """
            result = await self.db.execute(query, (key,))
            row = await result.fetchone()
            return row[0] if row else 0
        except Exception as e:
            self.logger.error(f"DB get count failed: {describe_db_error(e)}")
            return 0  # Fail-open

    async def _increment_db(self, key: str) -> int:
        """
        Increment counter in database (fallback).

        Args:
            key: Rate limit key

        Returns:
            New count after increment (or 0 on error)
        """
        if not self.db:
            return 0

        try:
            # Upsert with increment
            query = """
                INSERT INTO ai_rate_limit_state (key, count, expires_at)
                VALUES (%s, 1, NOW() + INTERVAL '2 hours')
                ON CONFLICT (key)
                DO UPDATE SET
                    count = ai_rate_limit_state.count + 1,
                    expires_at = CASE
                        WHEN ai_rate_limit_state.expires_at < NOW()
                        THEN NOW() + INTERVAL '2 hours'
                        ELSE ai_rate_limit_state.expires_at
                    END
                RETURNING count
            """
            result = await self.db.execute(query, (key,))
            row = await result.fetchone()
            return row[0] if row else 0
        except Exception as e:
            self.logger.error(f"DB increment failed: {describe_db_error(e)}")
            return 0  # Fail-open
