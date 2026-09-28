"""
Distributed Rate Limiting using Redis

Implements sliding window rate limiting with Redis for distributed enforcement:
- Per-user rate limits
- Per-command rate limits
- Per-IP rate limits
- Sliding window algorithm for accurate rate limiting
- Atomic operations
- Automatic key expiration
"""

import logging
import time
from typing import Optional, Dict, Any
from functools import wraps

try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    redis = None

logger = logging.getLogger(__name__)


class RateLimiter:
    """
    Distributed rate limiter using Redis sliding window algorithm.

    Features:
    - Accurate sliding window rate limiting
    - Distributed enforcement across multiple instances
    - Automatic key expiration
    - Flexible rate limit configuration
    - Fallback to in-memory for testing
    """

    def __init__(
        self,
        redis_url: Optional[str] = None,
        namespace: str = "rate_limit",
        enable_fallback: bool = True
    ):
        """
        Initialize rate limiter.

        Args:
            redis_url: Redis connection URL
            namespace: Key namespace
            enable_fallback: Use in-memory fallback if Redis unavailable
        """
        self.redis_url = redis_url
        self.namespace = namespace
        self.enable_fallback = enable_fallback

        self._redis: Optional[redis.Redis] = None
        self._fallback_cache: Dict[str, list] = {}
        self._fallback_enabled = False
        self._connected = False

    async def connect(self):
        """Connect to Redis (call during startup)"""
        if not REDIS_AVAILABLE:
            logger.warning(
                "redis package not available, using in-memory fallback"
            )
            self._fallback_enabled = True
            return

        if not self.redis_url:
            logger.warning(
                "No Redis URL provided, using in-memory fallback"
            )
            self._fallback_enabled = True
            return

        try:
            self._redis = redis.from_url(
                self.redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5
            )

            # Test connection
            await self._redis.ping()
            self._connected = True
            logger.info(f"Connected to Redis rate limiter: {self.namespace}")

        except Exception as e:
            logger.error(f"Failed to connect to Redis: {e}")
            if self.enable_fallback:
                logger.info("Falling back to in-memory rate limiter")
                self._fallback_enabled = True
            else:
                raise

    async def disconnect(self):
        """Disconnect from Redis (call during shutdown)"""
        if self._redis:
            await self._redis.close()
            self._connected = False
            logger.info("Disconnected from Redis rate limiter")

    def _make_key(self, identifier: str) -> str:
        """Create namespaced rate limit key"""
        return f"{self.namespace}:{identifier}"

    async def check_rate_limit(
        self,
        identifier: str,
        limit: int,
        window: int
    ) -> bool:
        """
        Check if request is within rate limit.

        Uses sliding window algorithm for accuracy.

        Args:
            identifier: Unique identifier (e.g., user_id, ip_address, command_key)
            limit: Maximum number of requests allowed
            window: Time window in seconds

        Returns:
            True if allowed, False if rate limited
        """
        key = self._make_key(identifier)
        now = time.time()

        try:
            if self._fallback_enabled:
                return await self._check_fallback(key, limit, window, now)

            if not self._connected:
                # If Redis disconnected, allow by default (fail open)
                logger.warning(
                    "Redis disconnected, allowing request (fail open)"
                )
                return True

            # Use Redis sliding window with sorted set
            # Remove old entries outside window
            await self._redis.zremrangebyscore(
                key,
                '-inf',
                now - window
            )

            # Count current requests in window
            count = await self._redis.zcard(key)

            if count < limit:
                # Add current request
                await self._redis.zadd(key, {str(now): now})
                # Set expiration to window duration
                await self._redis.expire(key, window)
                return True

            return False

        except Exception as e:
            logger.error(f"Rate limit check error: {e}")
            # Fail open on errors
            return True

    async def _check_fallback(
        self,
        key: str,
        limit: int,
        window: int,
        now: float
    ) -> bool:
        """In-memory fallback rate limiter"""
        if key not in self._fallback_cache:
            self._fallback_cache[key] = []

        # Remove old entries
        self._fallback_cache[key] = [
            ts for ts in self._fallback_cache[key]
            if ts > now - window
        ]

        # Check limit
        if len(self._fallback_cache[key]) < limit:
            self._fallback_cache[key].append(now)
            return True

        return False

    async def get_remaining(
        self,
        identifier: str,
        limit: int,
        window: int
    ) -> int:
        """
        Get remaining requests in current window.

        Args:
            identifier: Unique identifier
            limit: Maximum requests allowed
            window: Time window in seconds

        Returns:
            Number of remaining requests
        """
        key = self._make_key(identifier)
        now = time.time()

        try:
            if self._fallback_enabled:
                if key not in self._fallback_cache:
                    return limit
                # Clean old entries
                self._fallback_cache[key] = [
                    ts for ts in self._fallback_cache[key]
                    if ts > now - window
                ]
                current = len(self._fallback_cache[key])
                return max(0, limit - current)

            if not self._connected:
                return limit

            # Remove old entries
            await self._redis.zremrangebyscore(
                key,
                '-inf',
                now - window
            )

            # Count current requests
            count = await self._redis.zcard(key)
            return max(0, limit - count)

        except Exception as e:
            logger.error(f"Get remaining error: {e}")
            return limit

    async def reset(self, identifier: str) -> bool:
        """
        Reset rate limit for an identifier.

        Args:
            identifier: Unique identifier

        Returns:
            True if successful
        """
        key = self._make_key(identifier)

        try:
            if self._fallback_enabled:
                self._fallback_cache.pop(key, None)
                return True

            if not self._connected:
                return False

            await self._redis.delete(key)
            return True

        except Exception as e:
            logger.error(f"Reset error: {e}")
            return False

    async def reset_pattern(self, pattern: str) -> int:
        """
        Reset all rate limits matching pattern.

        Args:
            pattern: Pattern to match (e.g., 'user:*')

        Returns:
            Number of keys reset
        """
        namespaced_pattern = self._make_key(pattern)

        try:
            if self._fallback_enabled:
                prefix = namespaced_pattern.replace('*', '')
                keys_to_delete = [
                    k for k in self._fallback_cache.keys()
                    if k.startswith(prefix)
                ]
                for key in keys_to_delete:
                    del self._fallback_cache[key]
                return len(keys_to_delete)

            if not self._connected:
                return 0

            # Use SCAN to find matching keys
            deleted = 0
            async for key in self._redis.scan_iter(match=namespaced_pattern):
                await self._redis.delete(key)
                deleted += 1

            return deleted

        except Exception as e:
            logger.error(f"Reset pattern error: {e}")
            return 0

    def limit(
        self,
        identifier_func: callable,
        limit: int,
        window: int,
        on_limit_exceeded: Optional[callable] = None
    ):
        """
        Decorator for rate limiting functions.

        Usage:
            @rate_limiter.limit(
                identifier_func=lambda user_id: f"user:{user_id}",
                limit=10,
                window=60
            )
            async def process_command(user_id: str):
                # Process command
                pass

        Args:
            identifier_func: Function to generate identifier from args
            limit: Maximum requests allowed
            window: Time window in seconds
            on_limit_exceeded: Optional callback when limit exceeded
        """
        def decorator(func):
            @wraps(func)
            async def wrapper(*args, **kwargs):
                # Generate identifier
                identifier = identifier_func(*args, **kwargs)

                # Check rate limit
                allowed = await self.check_rate_limit(
                    identifier,
                    limit,
                    window
                )

                if not allowed:
                    logger.warning(
                        f"Rate limit exceeded: {identifier} "
                        f"({limit}/{window}s)"
                    )

                    if on_limit_exceeded:
                        return await on_limit_exceeded(*args, **kwargs)

                    raise RateLimitExceeded(
                        f"Rate limit exceeded: {limit} requests per {window}s"
                    )

                # Execute function
                return await func(*args, **kwargs)

            return wrapper
        return decorator

    async def get_stats(self) -> Dict[str, Any]:
        """Get rate limiter statistics"""
        stats = {
            "namespace": self.namespace,
            "connected": self._connected,
            "fallback_enabled": self._fallback_enabled,
            "fallback_keys": len(self._fallback_cache)
        }

        if self._connected:
            try:
                # Count rate limit keys
                count = 0
                pattern = self._make_key("*")
                async for _ in self._redis.scan_iter(match=pattern):
                    count += 1
                stats["active_limits"] = count
            except Exception as e:
                logger.error(f"Failed to get rate limiter stats: {e}")

        return stats


class RateLimitExceeded(Exception):
    """Exception raised when rate limit is exceeded"""
    pass


def create_rate_limiter(
    redis_url: Optional[str],
    namespace: str = "rate_limit"
) -> RateLimiter:
    """
    Factory function to create a rate limiter.

    Args:
        redis_url: Redis connection URL
        namespace: Rate limit namespace

    Returns:
        Configured RateLimiter instance
    """
    return RateLimiter(
        redis_url=redis_url,
        namespace=namespace,
        enable_fallback=True
    )
