"""Presence Provider Base — extensible contract for all platform presence providers.

Every platform (Slack, Discord, Teams, Mattermost, Google Chat, etc.) implements
this base class to collect and push presence/status data using the canonical
WaddleBot status vocabulary.

Usage
-----
class SlackPresenceProvider(PresenceProviderBase):
    PLATFORM = "slack"

    async def collect_presence(self, user_ids):
        # Call Slack API to get presence for each user
        ...

    async def push_presence(self, user_id, canonical_status):
        # Update Slack DND/presence via API
        ...

    def map_to_canonical(self, platform_status):
        return PLATFORM_STATUS_MAP["slack"].get(platform_status, "offline")

    def map_from_canonical(self, canonical_status):
        from .schema import CANONICAL_TO_PLATFORM
        return CANONICAL_TO_PLATFORM["slack"].get(canonical_status, "away")
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
import logging


logger = logging.getLogger(__name__)


class PresenceProviderBase(ABC):
    """Abstract base class for all platform presence providers.

    Sub-classes must set PLATFORM (e.g. "slack") and implement the four
    abstract methods.  All presence exchange uses the canonical status
    vocabulary defined in schema.CANONICAL_STATUSES.
    """

    PLATFORM: str = "unknown"

    def __init__(self, log_level: str = "INFO"):
        self.logger = logging.getLogger(f"presence.{self.PLATFORM}")

    # ──────────────────────────────────────────────────────────────────────
    # Abstract interface (implement in sub-class)
    # ──────────────────────────────────────────────────────────────────────

    @abstractmethod
    async def collect_presence(
        self, user_ids: List[str]
    ) -> Dict[str, Any]:
        """Fetch current presence/status for the given user IDs from the platform.

        Args:
            user_ids: List of platform-native user identifiers.

        Returns:
            Dict mapping user_id → dict with at least ``"status"`` (platform
            native value) and ``"canonical_status"`` (mapped value).
        """

    @abstractmethod
    async def push_presence(
        self, user_id: str, canonical_status: str
    ) -> bool:
        """Push a canonical status update to the platform for the given user.

        Args:
            user_id: Platform-native user identifier.
            canonical_status: One of CANONICAL_STATUSES values.

        Returns:
            True on success, False on failure.
        """

    @abstractmethod
    def map_to_canonical(self, platform_status: str) -> str:
        """Convert a platform-native status string to a canonical status.

        Args:
            platform_status: The raw status value returned by the platform API.

        Returns:
            A value from CANONICAL_STATUSES.  Default to ``"offline"`` if
            the platform status is unknown.
        """

    @abstractmethod
    def map_from_canonical(self, canonical_status: str) -> str:
        """Convert a canonical status to the platform-native status string.

        Args:
            canonical_status: One of CANONICAL_STATUSES values.

        Returns:
            The platform-native status string suitable for the push API call.
        """

    # ──────────────────────────────────────────────────────────────────────
    # Helpers available to all sub-classes
    # ──────────────────────────────────────────────────────────────────────

    def build_presence_record(
        self,
        user_id: str,
        platform_status: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Build a normalised presence record dict.

        Produces the standard shape used by PresenceStateStore and
        PresenceSyncEngine so sub-classes do not need to repeat this
        construction.
        """
        canonical = self.map_to_canonical(platform_status)
        return {
            "user_id": user_id,
            "platform": self.PLATFORM,
            "platform_status": platform_status,
            "canonical_status": canonical,
            "metadata": metadata or {},
        }

    def validate_canonical(self, status: str) -> bool:
        """Return True if *status* is a valid canonical status value."""
        from .schema import CANONICAL_STATUSES
        return status in CANONICAL_STATUSES
