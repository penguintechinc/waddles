"""Twitch chat ingest handler -- normalizes a raw fanned-out Twitch IRC chat message.

Fanned out by this container's own Twitch IRC receiver
(`receivers/twitch_irc.py`). Referenced by `app_catalog.stages.ingest.
entrypoint` (same pattern as migration 071's `builtin_handlers.echo_ingest:
normalize`) as `"builtin_handlers.twitch_ingest:normalize"` for the
`waddles.bot.twitch.default` built-in app seeded by `config/postgres/
migrations/083_discord_twitch_demo_convergence.sql` (on the merged
`feature/v3-svc-gateway-discord` branch) and registered into svc-ingest's
own in-process registry at startup (`app.py`, `builtin_handlers/
twitch_gateway_manifest.py`).

Consumes the raw event shape `receivers/twitch_irc.py`'s
`TwitchIrcReceiver.receive()` LPUSHes onto this handler's `:ingest` Valkey
key -- `{platform, channel_name, author_username, content}` -- and
produces a `flask_core.PlatformEvent`, the frozen stage-to-stage contract
(`libs/flask_core/flask_core/stream_pipeline.py`). `payload` carries
`channel_name`/`text`/`author` -- everything `builtin_handlers/twitch_send_action.py`
(svc-action, other side of the pipeline) needs to reply in place, mirroring
`actor` for callers that only see the action-stage envelope's `payload`.

Realigned (2026-09-03) onto the merged `waddle_transports` library's
generic `IrcTransport` -- at that point the transport did NOT parse
Twitch's own IRCv3 message tags (badges, mod/sub/broadcaster flags,
numeric user id), only the base `PRIVMSG` line, so the richer
per-message metadata this handler's earlier draft carried (`author_id`,
`is_mod`, `is_subscriber`, `is_broadcaster`, `message_id`) was not
available from the raw event -- a documented gap, not silently dropped.

GAP CLOSED (2026-09-11, gh-304/gh-316 prerequisite): `receivers/
twitch_irc.py` now requests the `twitch.tv/tags` IRCv3 capability and
parses the tag segment `IrcTransport.receive()` yields (see that
receiver's own module docstring), so the raw event carries `author_id`/
`user_id` (numeric, duplicated under both keys), `display_name`,
`message_id`, `room_id`, `badges`, and `is_mod`/`is_subscriber`/
`is_vip`/`is_broadcaster`. This handler passes them straight through onto
`PlatformEvent.payload` unchanged -- CAP not granted (or a non-tagged
line) means every one of these is `None`/`False`/`[]` on the raw event
already, so no extra guarding is needed here beyond a type check.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from flask_core import PlatformEvent


def _as_str_or_none(value: object) -> str | None:
    """Non-empty `str` or `None` -- shared guard for every optional string field below."""
    return value if isinstance(value, str) and value else None


async def normalize(raw: dict[str, Any]) -> PlatformEvent:
    """Normalize one raw Twitch IRC chat message event to a `PlatformEvent`.

    Real, working transform (not a stub): requires `content`/`channel_name`
    on the raw event, trims `content`, and stamps a UTC `occurred_at` when
    the raw event didn't carry its own timestamp. Raises `ValueError` on a
    malformed raw event -- the ingest runner catches this per-event so one
    bad event never kills the poll loop (`core/svc_ingest/runner.py`).
    """
    content = raw.get("content")
    channel_name = raw.get("channel_name")
    if not isinstance(content, str) or not content:
        raise ValueError("raw Twitch event missing required 'content' string field")
    if not channel_name or not isinstance(channel_name, str):
        raise ValueError("raw Twitch event missing required 'channel_name' string field")

    actor = raw.get("author_username") or "unknown"
    raw_badges = raw.get("badges")
    return PlatformEvent(
        platform=raw.get("platform", "twitch"),
        event_type="message",
        actor=actor,
        payload={
            "text": content.strip(),
            "channel_name": channel_name,
            "author": actor,
            "author_id": _as_str_or_none(raw.get("author_id")),
            "user_id": _as_str_or_none(raw.get("user_id")),
            "display_name": _as_str_or_none(raw.get("display_name")),
            "message_id": _as_str_or_none(raw.get("message_id")),
            "room_id": _as_str_or_none(raw.get("room_id")),
            "badges": raw_badges if isinstance(raw_badges, list) else [],
            "is_mod": bool(raw.get("is_mod")),
            "is_subscriber": bool(raw.get("is_subscriber")),
            "is_vip": bool(raw.get("is_vip")),
            "is_broadcaster": bool(raw.get("is_broadcaster")),
        },
        occurred_at=raw.get("occurred_at") or datetime.now(UTC).isoformat(),
    )
