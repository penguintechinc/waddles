"""Presence State Store — Redis-backed persistence for user presence records.

All presence records are stored with a configurable TTL.  The store
supports per-user and bulk retrieval so the sync engine and API layer
can efficiently aggregate presence across platforms.
"""
import json
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

KEY_PREFIX: str = "presence"
TTL: int = 3600  # seconds — 1 hour


class PresenceStateStore:
    """Redis-backed store for per-user, per-platform presence records.

    Each entry is keyed as ``presence:{user_id}:{platform}`` and holds a
    JSON-serialised presence record dict.  A secondary index key
    ``presence:users`` is maintained as a Redis Set so all tracked user
    IDs can be enumerated without a full key-scan.

    Args:
        redis_client: An async Redis client (e.g. ``redis.asyncio.Redis``).
        ttl: Time-to-live in seconds for each presence record.
            Defaults to 3600 (1 hour).
    """

    def __init__(self, redis_client, ttl: int = TTL):
        self._redis = redis_client
        self._ttl = ttl

    # ──────────────────────────────────────────────────────────────────────
    # Key helpers
    # ──────────────────────────────────────────────────────────────────────

    def _record_key(self, user_id: str, platform: str) -> str:
        return f"{KEY_PREFIX}:{user_id}:{platform}"

    def _user_index_key(self) -> str:
        return f"{KEY_PREFIX}:users"

    # ──────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────

    async def get_presence(
        self, user_id: str, platform: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Retrieve a presence record for a user on a specific platform.

        If *platform* is ``None``, returns the most-recently-updated record
        across all platforms for that user (most-recent-wins).

        Args:
            user_id: The WaddleBot internal user identifier.
            platform: Optional platform name filter (e.g. ``"slack"``).

        Returns:
            Presence record dict, or ``None`` if not found / expired.
        """
        if platform:
            raw = await self._redis.get(self._record_key(user_id, platform))
            if raw is None:
                return None
            return json.loads(raw)

        # No platform specified — collect all records and return most recent
        all_records = await self.get_all_presence(user_id)
        if not all_records:
            return None
        return max(all_records.values(), key=lambda r: r.get("timestamp", 0))

    async def set_presence(
        self, user_id: str, platform: str, record: Dict[str, Any]
    ) -> None:
        """Store a presence record with the configured TTL.

        Automatically stamps *record* with ``"stored_at"`` epoch seconds
        if not already present, then serialises to JSON and writes to
        Redis.

        Args:
            user_id: The WaddleBot internal user identifier.
            platform: Platform name (e.g. ``"slack"``).
            record: Presence record dict (output of build_presence_event or
                PresenceProviderBase.build_presence_record).
        """
        if "stored_at" not in record:
            record = {**record, "stored_at": int(time.time())}

        key = self._record_key(user_id, platform)
        await self._redis.set(key, json.dumps(record), ex=self._ttl)

        # Keep user index up to date
        await self._redis.sadd(self._user_index_key(), user_id)
        logger.debug(
            "Stored presence for user=%s platform=%s status=%s",
            user_id,
            platform,
            record.get("canonical_status"),
        )

    async def get_all_presence(
        self, user_id: str
    ) -> Dict[str, Dict[str, Any]]:
        """Return all platform presence records for a single user.

        Args:
            user_id: The WaddleBot internal user identifier.

        Returns:
            Dict mapping platform name → presence record dict.  Empty dict
            if no records exist for the user.
        """
        from .schema import PLATFORM_STATUS_MAP
        results: Dict[str, Dict[str, Any]] = {}

        for platform in PLATFORM_STATUS_MAP.keys():
            raw = await self._redis.get(self._record_key(user_id, platform))
            if raw is not None:
                try:
                    results[platform] = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning(
                        "Corrupt presence record for user=%s platform=%s",
                        user_id,
                        platform,
                    )
        return results

    async def delete_presence(
        self, user_id: str, platform: Optional[str] = None
    ) -> int:
        """Delete one or all presence records for a user.

        Args:
            user_id: The WaddleBot internal user identifier.
            platform: If provided, delete only the record for that platform.
                If ``None``, delete all records for the user across every
                known platform and remove from the user index.

        Returns:
            Number of keys deleted.
        """
        from .schema import PLATFORM_STATUS_MAP

        if platform:
            deleted = await self._redis.delete(
                self._record_key(user_id, platform)
            )
            logger.debug(
                "Deleted presence for user=%s platform=%s", user_id, platform
            )
            return deleted

        # Delete all platform records for this user
        keys = [
            self._record_key(user_id, p)
            for p in PLATFORM_STATUS_MAP.keys()
        ]
        deleted = await self._redis.delete(*keys) if keys else 0
        await self._redis.srem(self._user_index_key(), user_id)
        logger.debug(
            "Deleted all presence records for user=%s (deleted=%d)",
            user_id,
            deleted,
        )
        return deleted

    async def list_tracked_users(self) -> List[str]:
        """Return all user IDs currently tracked in the store.

        Note: presence records may have expired even if the user ID appears
        in this set.  The secondary index is best-effort.

        Returns:
            List of user ID strings.
        """
        members = await self._redis.smembers(self._user_index_key())
        return [m.decode() if isinstance(m, bytes) else m for m in members]
