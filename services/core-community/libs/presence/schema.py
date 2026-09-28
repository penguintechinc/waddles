"""Presence schema — canonical status vocabulary and platform mappings.

CANONICAL_STATUSES is the single source of truth for all status values
used within WaddleBot.  Platform-specific strings are normalised to/from
these values by each PresenceProviderBase sub-class using the mappings
defined here.
"""
import time
from typing import Any, Dict, Optional

# ──────────────────────────────────────────────────────────────────────────────
# Canonical status vocabulary
# ──────────────────────────────────────────────────────────────────────────────

CANONICAL_STATUSES: frozenset = frozenset({
    "online",   # Active and reachable
    "away",     # Temporarily inactive (idle)
    "dnd",      # Do Not Disturb — suppress notifications
    "offline",  # Unreachable / disconnected
})

# ──────────────────────────────────────────────────────────────────────────────
# Platform → canonical mapping
#
# Keys are canonical status values; values are all known platform-native
# strings that should resolve to that canonical status.
# ──────────────────────────────────────────────────────────────────────────────

PLATFORM_STATUS_MAP: Dict[str, Dict[str, str]] = {
    "slack": {
        # platform_native_value → canonical
        "active": "online",
        "away": "away",
        "dnd": "dnd",
        "offline": "offline",
    },
    "discord": {
        "online": "online",
        "idle": "away",
        "dnd": "dnd",
        "invisible": "offline",
        "offline": "offline",
    },
    "teams": {
        "Available": "online",
        "Busy": "dnd",
        "DoNotDisturb": "dnd",
        "BeRightBack": "away",
        "Away": "away",
        "Offline": "offline",
        "PresenceUnknown": "offline",
    },
    "mattermost": {
        "online": "online",
        "away": "away",
        "dnd": "dnd",
        "offline": "offline",
    },
    "googlechat": {
        "AVAILABLE": "online",
        "BUSY": "dnd",
        "DO_NOT_DISTURB": "dnd",
        "AWAY": "away",
        "OFF_THE_RECORD": "offline",
        "UNAVAILABLE": "offline",
    },
}

# ──────────────────────────────────────────────────────────────────────────────
# Canonical → platform mapping (reverse of PLATFORM_STATUS_MAP)
#
# Provides the preferred platform-native value to use when *pushing* a
# canonical status to a platform.  When multiple platform values share the
# same canonical status, we pick the most semantically precise one.
# ──────────────────────────────────────────────────────────────────────────────

CANONICAL_TO_PLATFORM: Dict[str, Dict[str, str]] = {
    "slack": {
        "online": "active",
        "away": "away",
        "dnd": "dnd",
        "offline": "offline",
    },
    "discord": {
        "online": "online",
        "away": "idle",
        "dnd": "dnd",
        "offline": "invisible",
    },
    "teams": {
        "online": "Available",
        "away": "Away",
        "dnd": "DoNotDisturb",
        "offline": "Offline",
    },
    "mattermost": {
        "online": "online",
        "away": "away",
        "dnd": "dnd",
        "offline": "offline",
    },
    "googlechat": {
        "online": "AVAILABLE",
        "away": "AWAY",
        "dnd": "DO_NOT_DISTURB",
        "offline": "UNAVAILABLE",
    },
}

# ──────────────────────────────────────────────────────────────────────────────
# Push-capable platforms
#
# Only platforms listed here support programmatic status updates (push).
# Presence data from other platforms is read-only (collect only).
# ──────────────────────────────────────────────────────────────────────────────

PUSH_CAPABLE_PLATFORMS: frozenset = frozenset({
    "slack",
    "teams",
    "mattermost",
})


# ──────────────────────────────────────────────────────────────────────────────
# Event builder
# ──────────────────────────────────────────────────────────────────────────────

def build_presence_event(
    user_id: str,
    source_platform: str,
    canonical_status: str,
    platform_status: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a normalised presence event dict for internal routing.

    Args:
        user_id: The WaddleBot internal user identifier.
        source_platform: The platform that generated this presence update
            (e.g. ``"slack"``).
        canonical_status: One of CANONICAL_STATUSES values.
        platform_status: The raw platform-native status string.
        metadata: Optional extra information (e.g. custom status emoji/text).

    Returns:
        A dict representing the presence event, ready for storage or
        fan-out via PresenceSyncEngine.

    Raises:
        ValueError: If *canonical_status* is not in CANONICAL_STATUSES.
    """
    if canonical_status not in CANONICAL_STATUSES:
        raise ValueError(
            f"Invalid canonical_status '{canonical_status}'. "
            f"Must be one of: {sorted(CANONICAL_STATUSES)}"
        )
    if source_platform not in PLATFORM_STATUS_MAP:
        raise ValueError(
            f"Unknown source_platform '{source_platform}'. "
            f"Known platforms: {sorted(PLATFORM_STATUS_MAP.keys())}"
        )
    return {
        "user_id": user_id,
        "source_platform": source_platform,
        "canonical_status": canonical_status,
        "platform_status": platform_status,
        "timestamp": int(time.time()),
        "metadata": metadata or {},
    }
