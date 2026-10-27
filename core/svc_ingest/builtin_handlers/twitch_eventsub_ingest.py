"""Twitch EventSub ingest handler -- normalizes a raw fanned-out EventSub notification.

Fanned out by this container's own EventSub webhook handler
(`eventsub.py`, mounted at `POST /eventsub/twitch/webhook` in `app.py`).
Referenced by `app_catalog.stages.ingest.entrypoint` as
`"builtin_handlers.twitch_eventsub_ingest:normalize"` for the
`waddles.bot.twitchevents.eventsub` built-in app (`builtin_handlers/
twitch_gateway_manifest.py`'s `TWITCH_EVENTSUB_MANIFEST`, registered
in-process). Out of the v3 demo's scope (Twitch chat ingest/process/
action only) -- no `app_catalog` seed row exists for it as of T8
convergence (`083_discord_twitch_demo_convergence.sql`), so it is not yet
discoverable by the poll-drain loop; deferred, not a regression (it was
never wired to a process/action stage either).

Consumes the raw event shape `eventsub.py`'s `build_raw_event` LPUSHes
onto this handler's `:ingest` Valkey key -- `{platform, event_type,
broadcaster_id, broadcaster_login, user_id, user_login, user_display_name,
metadata}` -- ported from `trigger/receiver/twitch_module/services/
eventsub_handler.py`'s own `_build_event_data` field set, trimmed to the
subset this connector's MVP normalizes (follow/subscribe/subscription-gift/
cheer/raid) -- and produces a `flask_core.PlatformEvent`, the frozen
stage-to-stage contract (`libs/flask_core/flask_core/stream_pipeline.py`).

gh #287 S10 (live ON/OFF detection): `stream.online`/`stream.offline`
added to :data:`KNOWN_EVENT_TYPES` below so this handler can normalize
them the same way as every other EventSub type -- `started_at`/`type`/
`viewer_count`, when present, ride in `payload['metadata']` exactly like
`channel.raid`'s `viewers` or `channel.cheer`'s `bits` above (no
special-cased top-level payload keys for these two types, consistent
with every other entry in this table). KNOWN GAP, out of this task's
edit scope: `eventsub.py`'s own `DEFAULT_SUBSCRIPTION_TYPES` (the
webhook's inbound filter -- `TwitchEventSubHandler.handle_webhook`
drops any `event_type` not in that set BEFORE `build_raw_event`/this
module's `normalize()` ever see it) and `build_raw_event`'s per-type
`metadata` builder still need the matching addition for a real Twitch
`stream.online`/`stream.offline` webhook delivery to reach this
function in production -- this module is ready to normalize them the
moment that companion edit lands.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from flask_core import PlatformEvent

#: EventSub subscription types this connector's MVP normalizes -- matches
#: `eventsub.py`'s own `DEFAULT_SUBSCRIPTION_TYPES`, ported from the
#: legacy module's `subscribe_to_events` default list. `stream.online`/
#: `stream.offline` (gh #287 S10) are NOT yet in `eventsub.py`'s own set
#: -- see this module's own docstring for the gap.
KNOWN_EVENT_TYPES = frozenset(
    {
        "channel.follow",
        "channel.subscribe",
        "channel.subscription.gift",
        "channel.cheer",
        "channel.raid",
        "stream.online",
        "stream.offline",
    }
)


async def normalize(raw: dict[str, Any]) -> PlatformEvent:
    """Normalize one raw Twitch EventSub notification to a `PlatformEvent`.

    Real, working transform (not a stub): requires `event_type`/
    `broadcaster_id` on the raw event, rejects an `event_type` outside
    :data:`KNOWN_EVENT_TYPES`. Raises `ValueError` on a malformed/
    unsupported raw event -- the ingest runner catches this per-event so
    one bad event never kills the poll loop.
    """
    event_type = raw.get("event_type")
    broadcaster_id = raw.get("broadcaster_id")
    if not isinstance(event_type, str) or event_type not in KNOWN_EVENT_TYPES:
        raise ValueError(
            f"raw Twitch EventSub event has unsupported event_type {event_type!r}, "
            f"expected one of {sorted(KNOWN_EVENT_TYPES)}"
        )
    if not broadcaster_id or not isinstance(broadcaster_id, str):
        raise ValueError("raw Twitch EventSub event missing required 'broadcaster_id' string field")

    actor = raw.get("user_login") or raw.get("user_id") or broadcaster_id
    return PlatformEvent(
        platform=raw.get("platform", "twitch"),
        event_type=event_type,
        actor=actor,
        payload={
            "broadcaster_id": broadcaster_id,
            "broadcaster_login": raw.get("broadcaster_login"),
            "user_id": raw.get("user_id"),
            "user_login": raw.get("user_login"),
            "user_display_name": raw.get("user_display_name"),
            "metadata": raw.get("metadata") or {},
        },
        occurred_at=raw.get("occurred_at") or datetime.now(UTC).isoformat(),
    )
