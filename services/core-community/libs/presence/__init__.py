"""presence — shared library for WaddleBot presence/status syncing.

Import the core components from here:

    from presence import PresenceProviderBase, PresenceStateStore
    from presence import PresenceSyncEngine
    from presence.schema import CANONICAL_STATUSES, PLATFORM_STATUS_MAP
"""
from .base import PresenceProviderBase
from .state_store import PresenceStateStore
from .sync_engine import PresenceSyncEngine
from .schema import CANONICAL_STATUSES, PLATFORM_STATUS_MAP

__all__ = [
    "PresenceProviderBase",
    "PresenceStateStore",
    "PresenceSyncEngine",
    "CANONICAL_STATUSES",
    "PLATFORM_STATUS_MAP",
]
