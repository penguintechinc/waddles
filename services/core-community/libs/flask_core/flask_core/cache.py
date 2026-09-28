"""
Redis-backed caching utilities for Waddles modules

Provides a centralized caching layer with:
- Redis backend for distributed caching
- Configurable TTLs
- JSON serialization
- Cache warming
- Invalidation patterns
- Fallback to in-memory cache if Redis unavailable
"""

import json
import logging
import asyncio
from typing import Any, Optional, Dict, List, Callable
from datetime import timedelta
from functools import wraps

try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    redis = None

logger = logging.getLogger(__name__)


class CacheManager:
    """
    Distributed Redis cache manager with fallback to in-memory caching.

    Features:
    - Automatic serialization/deserialization
    - TTL support
    - Namespace isolation per module
    - Cache warming on startup
    - Pattern-based invalidation
    - Fallback to dict-based cache if Redis unavailable
    """

    def __init__(
        self,
        redis_url: Optional[str] = None,
        namespace: str = "waddlebot",
        default_ttl: int = 300,
        enable_fallback: bool = True
    ):
        """
        Initialize cache manager.

        Args:
            redis_url: Redis connection URL (redis://host:port/db)
            namespace: Cache key namespace (e.g., 'twitch_receiver')
            default_ttl: Default TTL in seconds
            enable_fallback: Use in-memory cache if Redis unavailable
        """
        self.redis_url = redis_url
        self.namespace = namespace
        self.default_ttl = default_ttl
        self.enable_fallback = enable_fallback

        self._redis: Optional[redis.Redis] = None
        self._fallback_cache: Dict[str, Any] = {}
        self._fallback_enabled = False
        self._connected = False

    async def connect(self):
        """Connect to Redis (call during startup)"""
        if not REDIS_AVAILABLE:
            logger.warning(
                "redis package not available, using in-memory fallback cache"
            )
            self._fallback_enabled = True
            return

        if not self.redis_url:
            logger.warning(
                "No Redis URL provided, using in-memory fallback cache"
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
            logger.info(f"Connected to Redis cache: {self.namespace}")

        except Exception as e:
            logger.error(f"Failed to connect to Redis: {e}")
            if self.enable_fallback:
                logger.info("Falling back to in-memory cache")
                self._fallback_enabled = True
            else:
                raise

    async def disconnect(self):
        """Disconnect from Redis (call during shutdown)"""
        if self._redis:
            await self._redis.close()
            self._connected = False
            logger.info("Disconnected from Redis cache")

    def _make_key(self, key: str) -> str:
        """Create namespaced cache key"""
        return f"{self.namespace}:{key}"

    async def get(self, key: str) -> Optional[Any]:
        """
        Get value from cache.

        Args:
            key: Cache key

        Returns:
            Cached value or None if not found
        """
        namespaced_key = self._make_key(key)

        try:
            if self._fallback_enabled:
                # Use in-memory cache
                return self._fallback_cache.get(namespaced_key)

            if not self._connected:
                return None

            # Get from Redis
            value = await self._redis.get(namespaced_key)
            if value is None:
                return None

            # Deserialize JSON
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                # Return as string if not JSON
                return value

        except Exception as e:
            logger.error(f"Cache get error for key {key}: {e}")
            return None

    async def set(
        self,
        key: str,
        value: Any,
        ttl: Optional[int] = None
    ) -> bool:
        """
        Set value in cache.

        Args:
            key: Cache key
            value: Value to cache (must be JSON-serializable)
            ttl: Time to live in seconds (None = use default)

        Returns:
            True if successful, False otherwise
        """
        namespaced_key = self._make_key(key)
        ttl = ttl or self.default_ttl

        try:
            # Serialize to JSON
            serialized = json.dumps(value)

            if self._fallback_enabled:
                # Store in memory (no TTL support in fallback)
                self._fallback_cache[namespaced_key] = value
                return True

            if not self._connected:
                return False

            # Store in Redis with TTL
            await self._redis.setex(
                namespaced_key,
                ttl,
                serialized
            )
            return True

        except Exception as e:
            logger.error(f"Cache set error for key {key}: {e}")
            return False

    async def delete(self, key: str) -> bool:
        """
        Delete value from cache.

        Args:
            key: Cache key

        Returns:
            True if successful, False otherwise
        """
        namespaced_key = self._make_key(key)

        try:
            if self._fallback_enabled:
                self._fallback_cache.pop(namespaced_key, None)
                return True

            if not self._connected:
                return False

            await self._redis.delete(namespaced_key)
            return True

        except Exception as e:
            logger.error(f"Cache delete error for key {key}: {e}")
            return False

    async def delete_pattern(self, pattern: str) -> int:
        """
        Delete all keys matching pattern.

        Args:
            pattern: Pattern to match (e.g., 'channels:*')

        Returns:
            Number of keys deleted
        """
        namespaced_pattern = self._make_key(pattern)

        try:
            if self._fallback_enabled:
                # Simple prefix matching for in-memory cache
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

            # Use SCAN to find matching keys (safer than KEYS)
            deleted = 0
            async for key in self._redis.scan_iter(match=namespaced_pattern):
                await self._redis.delete(key)
                deleted += 1

            return deleted

        except Exception as e:
            logger.error(f"Cache delete pattern error for {pattern}: {e}")
            return 0

    async def exists(self, key: str) -> bool:
        """Check if key exists in cache"""
        namespaced_key = self._make_key(key)

        try:
            if self._fallback_enabled:
                return namespaced_key in self._fallback_cache

            if not self._connected:
                return False

            result = await self._redis.exists(namespaced_key)
            return result > 0

        except Exception as e:
            logger.error(f"Cache exists error for key {key}: {e}")
            return False

    async def get_many(self, keys: List[str]) -> Dict[str, Any]:
        """
        Get multiple values from cache.

        Args:
            keys: List of cache keys

        Returns:
            Dictionary of key -> value for found keys
        """
        if not keys:
            return {}

        namespaced_keys = [self._make_key(k) for k in keys]
        result = {}

        try:
            if self._fallback_enabled:
                for i, key in enumerate(keys):
                    nkey = namespaced_keys[i]
                    if nkey in self._fallback_cache:
                        result[key] = self._fallback_cache[nkey]
                return result

            if not self._connected:
                return {}

            # Use pipeline for efficiency
            values = await self._redis.mget(namespaced_keys)
            for i, value in enumerate(values):
                if value is not None:
                    try:
                        result[keys[i]] = json.loads(value)
                    except json.JSONDecodeError:
                        result[keys[i]] = value

            return result

        except Exception as e:
            logger.error(f"Cache get_many error: {e}")
            return {}

    async def set_many(
        self,
        items: Dict[str, Any],
        ttl: Optional[int] = None
    ) -> bool:
        """
        Set multiple values in cache.

        Args:
            items: Dictionary of key -> value
            ttl: Time to live in seconds

        Returns:
            True if successful, False otherwise
        """
        if not items:
            return True

        ttl = ttl or self.default_ttl

        try:
            if self._fallback_enabled:
                for key, value in items.items():
                    nkey = self._make_key(key)
                    self._fallback_cache[nkey] = value
                return True

            if not self._connected:
                return False

            # Use pipeline for efficiency
            pipeline = self._redis.pipeline()
            for key, value in items.items():
                nkey = self._make_key(key)
                serialized = json.dumps(value)
                pipeline.setex(nkey, ttl, serialized)

            await pipeline.execute()
            return True

        except Exception as e:
            logger.error(f"Cache set_many error: {e}")
            return False

    async def increment(self, key: str, amount: int = 1) -> Optional[int]:
        """
        Increment a counter.

        Args:
            key: Cache key
            amount: Amount to increment

        Returns:
            New value or None on error
        """
        namespaced_key = self._make_key(key)

        try:
            if self._fallback_enabled:
                current = self._fallback_cache.get(namespaced_key, 0)
                new_value = int(current) + amount
                self._fallback_cache[namespaced_key] = new_value
                return new_value

            if not self._connected:
                return None

            return await self._redis.incrby(namespaced_key, amount)

        except Exception as e:
            logger.error(f"Cache increment error for key {key}: {e}")
            return None

    def cached(
        self,
        key_func: Optional[Callable] = None,
        ttl: Optional[int] = None
    ):
        """
        Decorator to cache function results.

        Usage:
            @cache_manager.cached(ttl=300)
            async def get_user(user_id: str):
                # Expensive operation
                return user_data

        Args:
            key_func: Function to generate cache key from args
            ttl: Cache TTL in seconds
        """
        def decorator(func):
            @wraps(func)
            async def wrapper(*args, **kwargs):
                # Generate cache key
                if key_func:
                    cache_key = key_func(*args, **kwargs)
                else:
                    # Default: use function name and args
                    key_parts = [func.__name__]
                    key_parts.extend(str(a) for a in args)
                    key_parts.extend(f"{k}={v}" for k, v in kwargs.items())
                    cache_key = ":".join(key_parts)

                # Try to get from cache
                cached_value = await self.get(cache_key)
                if cached_value is not None:
                    return cached_value

                # Call function and cache result
                result = await func(*args, **kwargs)
                if result is not None:
                    await self.set(cache_key, result, ttl=ttl)

                return result

            return wrapper
        return decorator

    async def warm_cache(
        self,
        loader_func: Callable,
        keys: List[str],
        ttl: Optional[int] = None
    ):
        """
        Warm cache with data on startup.

        Args:
            loader_func: Async function that returns dict of key -> value
            keys: List of keys to warm
            ttl: Cache TTL
        """
        try:
            logger.info(f"Warming cache for {len(keys)} keys")

            data = await loader_func()
            if data:
                await self.set_many(data, ttl=ttl)
                logger.info(f"Cache warmed with {len(data)} items")

        except Exception as e:
            logger.error(f"Cache warming failed: {e}")

    async def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics"""
        stats = {
            "namespace": self.namespace,
            "connected": self._connected,
            "fallback_enabled": self._fallback_enabled,
            "fallback_size": len(self._fallback_cache)
        }

        if self._connected:
            try:
                info = await self._redis.info("stats")
                stats["redis_keys"] = info.get("db0", {}).get("keys", 0)
                stats["redis_hits"] = info.get("keyspace_hits", 0)
                stats["redis_misses"] = info.get("keyspace_misses", 0)
            except Exception as e:
                logger.error(f"Failed to get Redis stats: {e}")

        return stats


def create_cache_manager(
    redis_url: Optional[str],
    namespace: str,
    default_ttl: int = 300
) -> CacheManager:
    """
    Factory function to create a cache manager.

    Args:
        redis_url: Redis connection URL
        namespace: Cache namespace
        default_ttl: Default TTL in seconds

    Returns:
        Configured CacheManager instance
    """
    return CacheManager(
        redis_url=redis_url,
        namespace=namespace,
        default_ttl=default_ttl,
        enable_fallback=True
    )
