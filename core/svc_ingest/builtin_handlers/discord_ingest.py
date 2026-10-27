"""Discord gateway ingest handler -- normalizes a raw fanned-out Discord message.

Fanned out by this container's own Discord gateway receiver
(`receivers/discord_gateway.py`). Referenced by
`app_catalog.stages.ingest.entrypoint` (same pattern as
migration 071's `builtin_handlers.echo_ingest:normalize`) as
`"builtin_handlers.discord_ingest:normalize"` for the `waddles.bot.discord.default`
built-in app seeded by `config/postgres/migrations/
083_discord_twitch_demo_convergence.sql` and registered into svc-ingest's
own in-process registry at startup (`app.py`, `builtin_handlers/
discord_gateway_manifest.py`).

Consumes the raw event shape `receivers/discord_gateway.py`'s
`DiscordGatewayReceiver._build_raw_event` LPUSHes onto this handler's
`:ingest` Valkey key -- `{platform, guild_id, channel_id, message_id,
author_id, author_username, content}` -- and produces a
`flask_core.PlatformEvent`, the frozen stage-to-stage contract
(`libs/flask_core/flask_core/stream_pipeline.py`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from flask_core import PlatformEvent


async def normalize(raw: dict[str, Any]) -> PlatformEvent:
    """Normalize one raw Discord gateway message event to a `PlatformEvent`.

    Real, working transform (not a stub): requires `content`/`author_id`
    on the raw event, trims `content`, and stamps a UTC `occurred_at` when
    the raw event didn't carry its own timestamp. Raises `ValueError` on a
    malformed raw event -- the ingest runner catches this per-event so one
    bad event never kills the poll loop (`core/svc_ingest/runner.py`).
    """
    content = raw.get("content")
    author_id = raw.get("author_id")
    if not isinstance(content, str) or not content:
        raise ValueError("raw Discord event missing required 'content' string field")
    if not author_id or not isinstance(author_id, str):
        raise ValueError("raw Discord event missing required 'author_id' string field")

    return PlatformEvent(
        platform=raw.get("platform", "discord"),
        event_type="message",
        actor=raw.get("author_username") or author_id,
        payload={
            "text": content.strip(),
            "guild_id": raw.get("guild_id"),
            "channel_id": raw.get("channel_id"),
            "message_id": raw.get("message_id"),
            "author_id": author_id,
        },
        occurred_at=raw.get("occurred_at") or datetime.now(UTC).isoformat(),
    )
