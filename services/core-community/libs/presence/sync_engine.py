"""Presence Sync Engine — coordinates cross-platform status propagation.

The sync engine is the central orchestrator for presence updates.  When a
status change arrives from any platform it:

1. Stores the record in PresenceStateStore.
2. Determines which push-capable platforms should receive a fan-out.
3. Calls each eligible provider's push_presence() method.
4. Provides aggregated presence data using most-recent-wins conflict resolution.
"""
import logging
from typing import Any, Dict, List, Optional

from .schema import PUSH_CAPABLE_PLATFORMS, CANONICAL_STATUSES
from .state_store import PresenceStateStore

logger = logging.getLogger(__name__)


class PresenceSyncEngine:
    """Orchestrates presence collection, storage, and fan-out.

    Args:
        state_store: A PresenceStateStore instance for persistence.
        providers: Dict mapping platform name → PresenceProviderBase instance.
            Only providers listed in PUSH_CAPABLE_PLATFORMS will receive
            fan-out pushes.
    """

    def __init__(
        self,
        state_store: PresenceStateStore,
        providers: Optional[Dict[str, Any]] = None,
    ):
        self._store = state_store
        self._providers: Dict[str, Any] = providers or {}

    # ──────────────────────────────────────────────────────────────────────
    # Primary entry point
    # ──────────────────────────────────────────────────────────────────────

    async def handle_presence_update(
        self,
        user_id: str,
        source_platform: str,
        canonical_status: str,
        platform_status: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Process an incoming presence update, persist it, and fan out.

        This method is the single entry point called by platform receivers
        or the API layer whenever a user's status changes.

        Args:
            user_id: The WaddleBot internal user identifier.
            source_platform: The platform that reported the change.
            canonical_status: One of CANONICAL_STATUSES values.
            platform_status: The raw platform-native status string.
            metadata: Optional extra information (custom status text, etc.).

        Returns:
            Dict with keys ``"stored"``, ``"fanned_out_to"`` (list), and
            ``"errors"`` (list of error strings from failed pushes).

        Raises:
            ValueError: If *canonical_status* is not in CANONICAL_STATUSES.
        """
        if canonical_status not in CANONICAL_STATUSES:
            raise ValueError(
                f"Invalid canonical_status '{canonical_status}'. "
                f"Must be one of: {sorted(CANONICAL_STATUSES)}"
            )

        from .schema import build_presence_event
        record = build_presence_event(
            user_id=user_id,
            source_platform=source_platform,
            canonical_status=canonical_status,
            platform_status=platform_status,
            metadata=metadata,
        )

        # 1. Persist to state store
        await self._store.set_presence(user_id, source_platform, record)
        logger.info(
            "Presence update stored: user=%s platform=%s status=%s",
            user_id,
            source_platform,
            canonical_status,
        )

        # 2. Fan out to push-capable platforms
        fanned_out_to, errors = await self._fan_out(
            user_id=user_id,
            source_platform=source_platform,
            canonical_status=canonical_status,
        )

        return {
            "stored": True,
            "fanned_out_to": fanned_out_to,
            "errors": errors,
        }

    # ──────────────────────────────────────────────────────────────────────
    # Fan-out logic
    # ──────────────────────────────────────────────────────────────────────

    def _should_sync_to(
        self, source_platform: str, target_platform: str
    ) -> bool:
        """Determine whether a presence update should be pushed to target.

        Rules:
        - Never push back to the originating platform (would cause a loop).
        - Only push to platforms registered in PUSH_CAPABLE_PLATFORMS.
        - Provider must be registered in self._providers.

        Args:
            source_platform: Platform that originated the update.
            target_platform: Platform candidate for receiving the push.

        Returns:
            True if the push should proceed.
        """
        if target_platform == source_platform:
            return False
        if target_platform not in PUSH_CAPABLE_PLATFORMS:
            return False
        if target_platform not in self._providers:
            return False
        return True

    async def _fan_out(
        self,
        user_id: str,
        source_platform: str,
        canonical_status: str,
    ) -> tuple:
        """Push a canonical status to all eligible push-capable platforms.

        Args:
            user_id: The WaddleBot internal user identifier.
            source_platform: Platform that originated the update (excluded).
            canonical_status: Canonical status to propagate.

        Returns:
            Tuple of (fanned_out_to: List[str], errors: List[str]).
        """
        fanned_out_to: List[str] = []
        errors: List[str] = []

        for platform in PUSH_CAPABLE_PLATFORMS:
            if not self._should_sync_to(source_platform, platform):
                continue

            provider = self._providers[platform]
            try:
                success = await provider.push_presence(
                    user_id, canonical_status
                )
                if success:
                    fanned_out_to.append(platform)
                    logger.debug(
                        "Fan-out success: user=%s → platform=%s status=%s",
                        user_id,
                        platform,
                        canonical_status,
                    )
                else:
                    msg = (
                        f"push_presence returned False for platform={platform}"
                    )
                    errors.append(msg)
                    logger.warning(
                        "Fan-out rejected: user=%s → platform=%s", user_id, platform
                    )
            except Exception as exc:
                msg = f"Fan-out error to platform={platform}: {exc}"
                errors.append(msg)
                logger.error(
                    "Fan-out exception: user=%s → platform=%s error=%s",
                    user_id,
                    platform,
                    exc,
                )

        return fanned_out_to, errors

    # ──────────────────────────────────────────────────────────────────────
    # Aggregation
    # ──────────────────────────────────────────────────────────────────────

    async def get_aggregated_presence(
        self, user_id: str
    ) -> Optional[Dict[str, Any]]:
        """Return the most-recent-wins aggregated presence for a user.

        Fetches all platform records from the store and returns the one
        with the highest ``timestamp`` value.  Ties are broken by platform
        name alphabetically (deterministic but arbitrary).

        Args:
            user_id: The WaddleBot internal user identifier.

        Returns:
            The winning presence record dict, or ``None`` if no records exist.
        """
        all_records = await self._store.get_all_presence(user_id)
        if not all_records:
            logger.debug("No presence records found for user=%s", user_id)
            return None

        # Most-recent-wins: pick record with the highest timestamp
        winning_record = max(
            all_records.values(),
            key=lambda r: (r.get("timestamp", 0), r.get("source_platform", "")),
        )
        logger.debug(
            "Aggregated presence for user=%s: status=%s source=%s",
            user_id,
            winning_record.get("canonical_status"),
            winning_record.get("source_platform"),
        )
        return winning_record

    async def get_all_platform_presence(
        self, user_id: str
    ) -> Dict[str, Dict[str, Any]]:
        """Return raw presence records for all platforms for a user.

        Unlike get_aggregated_presence, this returns the full per-platform
        breakdown without conflict resolution.

        Args:
            user_id: The WaddleBot internal user identifier.

        Returns:
            Dict mapping platform name → presence record dict.
        """
        return await self._store.get_all_presence(user_id)

    def register_provider(self, platform: str, provider: Any) -> None:
        """Register a presence provider for a platform at runtime.

        Args:
            platform: Platform name (e.g. ``"slack"``).
            provider: An instance implementing PresenceProviderBase.
        """
        self._providers[platform] = provider
        logger.info("Registered presence provider for platform=%s", platform)
