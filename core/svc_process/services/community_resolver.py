"""Community resolution order for the `!cc` handler and similar callers (gh #311).

Answers "which community does this message belong to" via three sources,
checked in order, cheapest-semantically-specific first:

1. **User context** (`community_context_store.get_context`) -- an explicit
   per-user override for this channel, if the caller knows both
   `platform_user_id` and `platform_entity_id`.
2. **Channel primary** (`community_context_store.list_channel_communities`)
   -- the channel/server's own default community
   (`community_servers.is_primary`), if the caller knows
   `platform_entity_id`.
3. **Demo shim** (`demo_default`) -- alpha-only fallback, the same concept
   `runner.py`/`Config.DEMO_ACTIVITY_COMMUNITY_ID` already uses for a
   tenant-wide (`community=None`) envelope; the caller passes this in, this
   module never reads `Config` itself.

`resolve_community` never raises -- every DB/Redis call this module makes
through `community_context_store` is wrapped individually, so a failure at
any one source degrades to "skip this source" (logged at WARN, rate-limited
per `(platform, platform_entity_id)` pair so a sustained outage doesn't
flood logs one line per message) and falls through to the next, exactly
like `services/moderation_gate.py`'s own "never break the pipeline"
contract for each of ITS external calls.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Literal

from services.community_context_store import get_context, list_channel_communities

logger = logging.getLogger(__name__)

#: Minimum seconds between WARN log lines for the SAME failing
#: `(step, platform, platform_entity_id)` key -- every occurrence still logs
#: at DEBUG (see module docstring); this only throttles the noisier WARN
#: level during a sustained outage. No existing rate-limited-log helper in
#: `flask_core` (`rate_limiter.py`/`http_rate_limit.py` are both HTTP
#: request-rate limiters, a different concern) -- this is a small,
#: self-contained one, safe under this stage runner's single-consumer poll
#: loop (`runner.py`, no concurrent `asyncio.gather` fan-out over
#: `resolve_community` calls today).
_WARN_INTERVAL_S = 30.0

_last_warned_at: dict[tuple[str, str, str], float] = {}


def _warn_rate_limited(step: str, platform: str, platform_entity_id: str, exc: Exception) -> None:
    """Log `exc` at WARN for `(step, platform, platform_entity_id)`, throttled to once/interval."""
    key = (step, platform, platform_entity_id)
    now = time.monotonic()
    last = _last_warned_at.get(key)
    if last is None or (now - last) >= _WARN_INTERVAL_S:
        _last_warned_at[key] = now
        logger.warning(
            "community_resolver.%s_failed platform=%s platform_entity_id=%s error=%s",
            step,
            platform,
            platform_entity_id,
            exc,
        )
    logger.debug(
        "community_resolver.%s_failed_detail platform=%s platform_entity_id=%s error=%r",
        step,
        platform,
        platform_entity_id,
        exc,
    )


def reset_warn_rate_limit_for_tests() -> None:
    """Clear the WARN rate-limit state. Test isolation only."""
    _last_warned_at.clear()


ResolutionSource = Literal["user_context", "channel_primary", "demo_shim", "none"]


@dataclass(slots=True)
class ResolvedCommunity:
    """The outcome of `resolve_community` -- which community, and which source found it."""

    community_id: int | None
    source: ResolutionSource


async def resolve_community(
    *,
    platform: str,
    platform_user_id: str | None,
    platform_entity_id: str | None,
    demo_default: int | None,
) -> ResolvedCommunity:
    """Resolve the community a message/command belongs to. Never raises.

    Order: per-user override -> channel primary -> `demo_default` -> none.
    Each source is skipped outright (no call made, DEBUG-logged) when the
    caller-supplied identifiers it needs are missing, and skipped after a
    failed call (WARN, rate-limited, plus a DEBUG detail line) when the
    underlying DB/Redis call raises.

    Args:
        platform: Platform slug (e.g. `"discord"`, `"twitch"`).
        platform_user_id: The platform-native user id, or `None` if
            unknown (e.g. a system-generated event with no acting user) --
            source 1 is skipped when `None`.
        platform_entity_id: The channel/server/workspace id, or `None` if
            unknown -- sources 1 and 2 are both skipped when `None`.
        demo_default: The alpha-only fallback community id (caller passes
            `Config.DEMO_ACTIVITY_COMMUNITY_ID` or `None` to disable this
            source entirely).

    Returns:
        A `ResolvedCommunity` -- `community_id=None, source="none"` only
        when every source above was either skipped or failed/missed.
    """
    if platform_user_id is not None and platform_entity_id is not None:
        try:
            community_id = await get_context(
                platform=platform,
                platform_user_id=platform_user_id,
                platform_entity_id=platform_entity_id,
            )
        except Exception as exc:  # noqa: BLE001 - must never break resolution, see module docstring
            _warn_rate_limited("user_context", platform, platform_entity_id, exc)
        else:
            if community_id is not None:
                logger.debug(
                    "community_resolver.user_context_hit platform=%s platform_entity_id=%s "
                    "community_id=%s",
                    platform,
                    platform_entity_id,
                    community_id,
                )
                return ResolvedCommunity(community_id=community_id, source="user_context")
            logger.debug(
                "community_resolver.user_context_miss platform=%s platform_entity_id=%s",
                platform,
                platform_entity_id,
            )
    else:
        logger.debug(
            "community_resolver.user_context_skipped platform=%s -- "
            "platform_user_id or platform_entity_id unknown",
            platform,
        )

    if platform_entity_id is not None:
        try:
            channels = await list_channel_communities(
                platform=platform, platform_entity_id=platform_entity_id
            )
        except Exception as exc:  # noqa: BLE001 - must never break resolution, see module docstring
            _warn_rate_limited("channel_primary", platform, platform_entity_id, exc)
        else:
            primary = next((channel for channel in channels if channel.is_primary), None)
            if primary is not None:
                logger.debug(
                    "community_resolver.channel_primary_hit platform=%s platform_entity_id=%s "
                    "community_id=%s",
                    platform,
                    platform_entity_id,
                    primary.id,
                )
                return ResolvedCommunity(community_id=primary.id, source="channel_primary")
            logger.debug(
                "community_resolver.channel_primary_miss platform=%s platform_entity_id=%s",
                platform,
                platform_entity_id,
            )
    else:
        logger.debug(
            "community_resolver.channel_primary_skipped platform=%s -- platform_entity_id unknown",
            platform,
        )

    if demo_default is not None:
        logger.debug(
            "community_resolver.demo_shim_hit platform=%s community_id=%s",
            platform,
            demo_default,
        )
        return ResolvedCommunity(community_id=demo_default, source="demo_shim")

    logger.debug("community_resolver.none platform=%s", platform)
    return ResolvedCommunity(community_id=None, source="none")
