"""Slack Socket Mode ingest handler -- normalizes a raw fanned-out Slack event.

Fanned out by this container's own Slack Socket Mode receiver
(`receivers/slack_socket.py`). Mirrors `builtin_handlers/discord_ingest.py`'s own
entrypoint/contract exactly -- referenced as `app_catalog.stages.ingest.
entrypoint = "builtin_handlers.slack_ingest:normalize"` for the `waddles.bot.slack.
default` built-in app (this handler's own `app_id`, matching the same app_id the
concurrently-written action stage -- `builtin_handlers.slack_send_action:send_
message` -- uses, per T8 convergence's "one app_id across all stages"
precedent set by migration `083_discord_twitch_demo_convergence.sql`).

Consumes the raw event shape `receivers/slack_socket.py`'s
`SlackSocketReceiver._normalize_event` yields -- `{platform, event_type,
text, channel_id, team_id, thread_ts, message_ts, platform_user_id,
display_name}` -- and produces a `flask_core.PlatformEvent`, the frozen
stage-to-stage contract (`libs/flask_core/flask_core/stream_pipeline.py`).

**Documented gap, matching this PR's own scope**: unlike Discord/Twitch,
no `builtin_handlers/slack_gateway_manifest.py` + `app.py` `AppRegistry.load(...)`
wiring exists yet -- `fanout.fan_out_event`'s `resolve_consuming_apps`
will find zero consumers for `receivers.slack_socket.CONSUMES_TAG` until
that manifest (and the DB `app_catalog` row this handler's own docstring
references) are added in a follow-up. This handler's `normalize()` itself
is real and fully working -- the gap is purely in the in-process registry
lookup the platform-level receiver's own fan-out depends on, the exact
same class of documented gap `discord_gateway_manifest.py` and
`receivers/twitch_irc.py` (guild/channel -> community mapping) already
carry for their own deferred follow-ups.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from flask_core import PlatformEvent

#: `message`/`app_mention` events carry human-authored text; `member_
#: joined_channel` never does (see `receivers/slack_socket.py`'s own
#: `_normalize_event`) -- only the former require a non-empty `text`.
_TEXT_REQUIRED_EVENT_TYPES = frozenset({"message", "app_mention"})


async def normalize(raw: dict[str, Any]) -> PlatformEvent:
    """Normalize one raw Slack Socket Mode event to a `PlatformEvent`.

    Real, working transform (not a stub): requires `event_type`/
    `platform_user_id` on every raw event, plus a non-empty `text` for
    `message`/`app_mention` events specifically (`member_joined_channel`
    has none). Raises `ValueError` on a malformed raw event -- the ingest
    runner catches this per-event so one bad event never kills the poll
    loop (`core/svc_ingest/runner.py`), matching `builtin_handlers/discord_ingest.
    py::normalize`'s identical validation contract.
    """
    event_type = raw.get("event_type")
    if not isinstance(event_type, str) or not event_type:
        raise ValueError("raw Slack event missing required 'event_type' string field")

    platform_user_id = raw.get("platform_user_id")
    if not isinstance(platform_user_id, str) or not platform_user_id:
        raise ValueError("raw Slack event missing required 'platform_user_id' string field")

    text = raw.get("text")
    if event_type in _TEXT_REQUIRED_EVENT_TYPES:
        if not isinstance(text, str) or not text:
            raise ValueError("raw Slack event missing required 'text' string field")
        text = text.strip()

    return PlatformEvent(
        platform=raw.get("platform", "slack"),
        event_type=event_type,
        actor=platform_user_id,
        payload={
            "text": text,
            "channel_id": raw.get("channel_id"),
            "team_id": raw.get("team_id"),
            "thread_ts": raw.get("thread_ts"),
            "message_ts": raw.get("message_ts"),
            "platform_user_id": platform_user_id,
            "display_name": raw.get("display_name"),
        },
        occurred_at=raw.get("occurred_at") or datetime.now(UTC).isoformat(),
    )
