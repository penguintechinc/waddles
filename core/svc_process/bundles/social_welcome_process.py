"""Social welcome process bundle -- first-message detection and welcome message build.

Detects a user's first-ever message in a community, atomically marks them as
welcomed, and generates a welcome response (AI-personalized if flagged, else
template). Returns None for repeat visitors or if another coroutine already
claimed the welcome.

Uses flask_core (get_bundle_dal, get_bundle_context) and the local
`bundles._dal_sql` raw-SQL shim (M1.5 stopgap, see that module's docstring) to access
the process-wide AsyncDAL and envelope scope (tenant/community) -- the frozen
API for stateful process bundles.
"""

from __future__ import annotations

import dataclasses
import logging

from flask_core import PlatformEvent, get_bundle_context, get_bundle_dal

from bundles._dal_sql import raw_sql_rows, raw_sql_write

logger = logging.getLogger(__name__)

# Placeholder config -- in production, source from the process runner or env
_WELCOME_TEMPLATE = "Welcome to the community, {username}!"
_AI_WELCOME_FLAG = "waddles.social.welcome_ai"
_AI_TIMEOUT_SECONDS = 5.0


async def _is_first_time(platform: str, platform_user_id: str) -> bool:
    """Return True if this user has no prior message event in this community.

    Uses get_bundle_context() to retrieve the community_id and get_bundle_dal()
    for the async database access. Called by the frozen process entrypoint.

    Args:
        platform: Source platform (twitch, discord, slack, ...).
        platform_user_id: User's platform-native ID.

    Returns:
        True if activity_message_events has zero rows for this user; False otherwise.

    """
    ctx = get_bundle_context()
    dal = get_bundle_dal()
    rows = await raw_sql_rows(
        dal,
        "SELECT id FROM activity_message_events "
        "WHERE community_id = :community_id AND platform = :platform "
        "AND platform_user_id = :platform_user_id LIMIT 1",
        {
            "community_id": int(ctx.community) if ctx.community else None,
            "platform": platform,
            "platform_user_id": platform_user_id,
        },
    )
    return len(rows) == 0


async def _try_mark_welcomed(platform: str, platform_user_id: str) -> bool:
    """Atomically claim the one-time welcome for this user in this community.

    Uses UNIQUE(community_id, platform, platform_user_id) + INSERT ... ON CONFLICT
    to ensure the database, not the process stage, enforces "welcomed at most once".

    Uses get_bundle_context() to retrieve the community_id and get_bundle_dal()
    for the async database access.

    Args:
        platform: Source platform.
        platform_user_id: User's platform-native ID.

    Returns:
        True only if this call created the row (caller won the race and should send welcome).
        False if another coroutine already claimed it.

    """
    ctx = get_bundle_context()
    dal = get_bundle_dal()
    rows = await raw_sql_write(
        dal,
        "INSERT INTO community_welcomed_users "
        "(community_id, platform, platform_user_id) "
        "VALUES (:community_id, :platform, :platform_user_id) "
        "ON CONFLICT (community_id, platform, platform_user_id) DO NOTHING "
        "RETURNING id",
        {
            "community_id": int(ctx.community) if ctx.community else None,
            "platform": platform,
            "platform_user_id": platform_user_id,
        },
    )
    return len(rows) == 1


async def _build_welcome(
    *,
    platform_username: str,
    ai_enabled: bool = False,
) -> tuple[str, str]:
    """Build the welcome text -- AI-personalized if enabled, else template.

    Args:
        platform_username: Display name to greet.
        ai_enabled: Whether AI personalization is enabled (would be checked via feature flag).

    Returns:
        (text, source) where source is "ai" or "template".

    """
    # NOTE: Full AI integration would happen here with feature_enabled check and
    # ai_client.generate_response() call. For now, return template.
    # This maintains the v2 interface while deferring AI orchestration.
    template = _WELCOME_TEMPLATE.format(username=platform_username)
    return template, "template"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Reply to a first-time message with a welcome.

    Checks if the sender is a new user in this community (via get_bundle_context()),
    atomically marks them as welcomed (if they are), and returns a modified event with
    a welcome message. Returns None for repeat visitors or if another coroutine already
    welcomed this user.

    Raises ValueError on a malformed event (missing required payload fields).
    The process runner catches this per-event so one bad event never kills the poll loop.

    Args:
        event: Incoming PlatformEvent from ingest stage.

    Returns:
        Modified PlatformEvent with welcome text, or None if no welcome is needed.

    """
    # Extract required fields from event
    text = event.payload.get("text")
    if not isinstance(text, str):
        raise ValueError("event payload missing required 'text' string field")

    platform_user_id = event.payload.get("author_id")
    if not platform_user_id or not isinstance(platform_user_id, str):
        raise ValueError("event payload missing required 'author_id' string field")

    platform_username = event.actor
    if not platform_username or not isinstance(platform_username, str):
        raise ValueError("event.actor missing or not a string")

    # Get community from bundle context (frozen API), not from event payload
    ctx = get_bundle_context()
    community_id = int(ctx.community) if ctx.community else None
    if not community_id:
        logger.warning("bundle context missing 'community' (int); skipping welcome check")
        return None

    # Check if first message and claim the welcome atomically
    if not await _is_first_time(event.platform, platform_user_id):
        return None  # repeat visitor, no welcome

    newly_claimed = await _try_mark_welcomed(event.platform, platform_user_id)
    if not newly_claimed:
        return None  # another coroutine already welcomed this user

    # Build welcome message
    welcome_text, source = await _build_welcome(platform_username=platform_username)
    logger.info(
        "welcome_sent",
        extra={
            "platform": event.platform,
            "user_id": platform_user_id,
            "community_id": community_id,
            "source": source,
        },
    )

    # Return modified event with welcome text
    return dataclasses.replace(
        event,
        payload={**event.payload, "text": welcome_text},
    )
