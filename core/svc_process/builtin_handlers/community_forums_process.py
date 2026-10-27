"""Community forums process handler -- parses forum post/reply commands.

Normalizes chat events containing forum creation commands into structured
PlatformEvents for the action stage. Returns `None` for non-forum messages
to avoid echoing ordinary chatter back to the channel. A message that IS a
`!forum` command but has missing/malformed args gets a usage-hint reply
instead of `None` -- see `_usage_reply()`.

Supports two command patterns:
- `!forum create <title> | <body>` -- create a forum post
- `!forum reply <post_id> | <content>` -- reply to an existing post

ROUTING (gh #298): a successful `create`/`reply` parse -- but not a
usage-hint reply -- stamps `PROCESS_TARGET_APP_ID_KEY` onto the returned
event's payload with `_FORUM_APP_ID`. `bot_process.py` delegates `!forum`
to this handler's `transform()` in-process and returns whatever it gets
back unmodified, so this key rides all the way to `core/svc_process/
runner.py`, which enqueues the event onto the forums app's `:action` key
instead of the originating bot's -- see `PROCESS_TARGET_APP_ID_KEY`'s
docstring in `flask_core.stream_pipeline` for the full mechanism.
"""

from __future__ import annotations

import dataclasses
import re

from flask_core import PROCESS_TARGET_APP_ID_KEY, PlatformEvent

#: Matches any `!forum` invocation, valid or not -- used to tell "this is a
#: forum command with bad args" (usage hint) apart from "not a forum
#: message at all" (`None`, no reply).
_FORUM_PREFIX_RE = re.compile(r"^!forum\b", re.IGNORECASE)
_FORUM_COMMAND_RE = re.compile(r"^!forum\s+(create|reply)\b", re.IGNORECASE)

_FORUM_USAGE = "Usage: !forum create <title> | <body>  ·  !forum reply <post_id> | <content>"

#: `app_catalog.app_id` this handler's action stage is registered under
#: (config/postgres/migrations/091_community_forums_bundle.sql). A
#: successful `create`/`reply` parse routes to THIS app's `:action` key
#: instead of the originating bot's -- see `PROCESS_TARGET_APP_ID_KEY`'s
#: docstring for the full mechanism (gh #298). Usage-hint replies are
#: deliberately NOT routed here -- they're plain chat replies that belong
#: on the originating bot's own action key (chat echo), not a forum action.
_FORUM_APP_ID = "waddles.community.forums.default"


def _usage_reply(event: PlatformEvent) -> PlatformEvent:
    """Build the `!forum` usage-hint reply, preserving every other payload field."""
    return dataclasses.replace(event, payload={**event.payload, "text": _FORUM_USAGE})


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Parse forum commands from chat text; return None for non-forum messages.

    A message starting with `!forum` but missing/malformed args (no
    subcommand, no `|` separator, empty title/body, non-numeric post id)
    gets a usage-hint reply rather than silently doing nothing.

    Raises `ValueError` on a malformed event -- the process runner catches
    this per-event so one bad event never kills the poll loop.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        raise ValueError("event payload missing required 'text' string field")

    text = text.strip()
    if not text or not _FORUM_PREFIX_RE.match(text):
        return None  # not a forum command, skip

    if not _FORUM_COMMAND_RE.match(text):
        return _usage_reply(event)  # bare `!forum` or unknown subcommand

    # Parse command and arguments
    parts = text.split("|", 1)
    if len(parts) != 2:
        return _usage_reply(event)  # missing `|` separator

    command_part = parts[0].strip()
    content_part = parts[1].strip()

    match = _FORUM_COMMAND_RE.match(command_part)
    if not match:  # pragma: no cover -- defensive, text already matched the same regex above
        return _usage_reply(event)

    command = match.group(1).lower()

    if command == "create":
        # Format: !forum create <title> | <body>
        title_body = command_part[len("!forum create") :].strip()
        if not title_body or not content_part:
            return _usage_reply(event)
        return dataclasses.replace(
            event,
            payload={
                **event.payload,
                "text": content_part,
                "forum_action": "create",
                "forum_title": title_body,
                "forum_body": content_part,
                PROCESS_TARGET_APP_ID_KEY: _FORUM_APP_ID,
            },
        )

    if command == "reply":
        # Format: !forum reply <post_id> | <content>
        post_id_str = command_part[len("!forum reply") :].strip()
        try:
            post_id = int(post_id_str)
        except ValueError:
            return _usage_reply(event)  # invalid post id
        if not content_part:
            return _usage_reply(event)
        return dataclasses.replace(
            event,
            payload={
                **event.payload,
                "text": content_part,
                "forum_action": "reply",
                "forum_post_id": post_id,
                "forum_content": content_part,
                PROCESS_TARGET_APP_ID_KEY: _FORUM_APP_ID,
            },
        )

    return _usage_reply(event)  # pragma: no cover -- defensive, _FORUM_COMMAND_RE already filters
