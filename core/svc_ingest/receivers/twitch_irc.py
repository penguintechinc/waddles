"""TwitchIrcReceiver -- a `waddle_transports.Transport` for inbound Twitch IRC chat receipt.

Delegates the real IRC wire protocol entirely to `waddle_transports.
transports.irc.IrcTransport` (a real, from-scratch, Twitch-agnostic
asyncio TCP/TLS IRC client -- see that module's own docstring; NOT a port
of twitchio or any Twitch-specific library). This receiver's only job is
normalizing each raw `{channel, sender, text}` dict `IrcTransport.
receive()` yields into the platform event shape `builtin_handlers/twitch_ingest.py`
consumes.

ONE connection per channel (`IrcTransport.receive()`'s own single-channel-
per-call contract) -- `app.py`'s startup builds one `TwitchIrcReceiver`
per `Config.TWITCH_CHANNELS` entry, each wrapped in its own `socket_lease.
LeasedReceiver` (`provider="twitch", community=<channel>`) and registered
under its own `ReceiverSupervisor` name, so scaling `pipeline.svcIngest.
replicas` never opens two connections to the same channel, and one
channel's connection dying/restarting never affects another's.

Like `DiscordGatewayReceiver`, this class owns ONLY `receive()` -- no
bespoke `run()`/`stop()`, fan-out, or Valkey/registry dependency; the
lease-guarded consume loop (`socket_lease.LeasedReceiver`) and the fan-out
callback (`app.py`'s own `_on_twitch_item`) both live at the wiring layer
now, matching the shared `waddle_transports.Transport` ABC contract
exactly.

Outbound sends are NOT handled here -- see `outbound_drain.py`
(`IrcTransport.send()` opens its own short-lived connection per message,
so relaying through a receiver's already-open socket is unnecessary; the
earlier draft's "reuse the held socket" premise doesn't apply once the
real transport's `send()` semantics are known -- it never held a
persistent connection to reuse in the first place).

IRCv3 TAGS (2026-09-11, gh-304/gh-316 prerequisite): requests
`twitch.tv/tags`/`twitch.tv/commands` via `IrcTransport`'s generic
`cap_requests` config hook (see that module's own docstring -- the CAP
REQ mechanism itself is Twitch-agnostic; only the two capability strings
requested here are Twitch-specific) and parses the raw `@tag=val;...`
segment `IrcTransport.receive()` now yields as `tags` into Twitch's own
per-message metadata: numeric `user-id`, `display-name`, message `id`,
`room-id`, and `badges` (mod/subscriber/vip/broadcaster). CAP not granted
(older Twitch behavior, a misconfigured `nick`, or a non-Twitch IRC
server reusing this same receiver) -- every derived field is simply
absent/`None`/`False`/`[]`, never raised; this receiver has no visibility
into WHY the server declined tags, only that it did.

Populates the raw event dict's numeric user id under BOTH `author_id`
(this repo's own general convention -- `discord_ingest.py`'s identical
field, and what `services/moderation_gate.py`/`runner.py`'s
`_resolve_platform_user_id` and every `social_*_process.py`/
`community_reputation_process.py` bundle already read via
`event.payload.get("author_id")`) AND `user_id` (the Twitch-specific
alias `twitch_eventsub_ingest.py` already normalizes to, and the exact
fallback field `moderation_enforce_action.py::_resolve_target_user_id`'s
own docstring names as the gap this receiver was blocking) -- same value,
two keys, zero downstream edits required to start resolving a real
numeric id instead of falling back to a display name. Does NOT add a
literal `platform_user_id`/`platform_user_login` key: grepping
`core/svc_process`/`core/svc_action` turns up no reader of either name
(`platform_user_id` there is always a local variable populated FROM
`payload["author_id"]`, never a payload key itself) -- adding them would
be dead, unread duplication of `author_id`/`author_username`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any, ClassVar

from waddle_transports import Direction, Transport
from waddle_transports.transports.irc import IrcTransport

logger = logging.getLogger(__name__)

#: The `consumes` tag every ingest bundle wanting a raw Twitch chat message
#: declares (`builtin_handlers/twitch_gateway_manifest.py`'s own `stages.ingest.
#: consumes`) -- this receiver's half of that contract.
CONSUMES_TAG = "twitch.message"

#: IRCv3 capabilities requested for every Twitch IRC connection, via
#: `IrcTransport`'s generic `cap_requests` config hook -- `tags` unlocks
#: the per-message metadata this receiver parses (`_parse_tags` below);
#: `commands` is requested alongside it per Twitch's own documented CAP
#: REQ convention (unused today, but requesting it later would cost a
#: second round-trip for no reason).
_TWITCH_CAP_REQUESTS: tuple[str, ...] = ("twitch.tv/tags", "twitch.tv/commands")

#: Reverses IRCv3 message-tag value escaping (IRCv3.2 message-tags spec)
#: -- `\s` (space), `\:` (semicolon), `\\` (backslash), `\r`, `\n`. A
#: trailing lone backslash (malformed input) is dropped, never raised.
_TAG_UNESCAPES: dict[str, str] = {"s": " ", ":": ";", "\\": "\\", "r": "\r", "n": "\n"}


def _unescape_tag_value(value: str) -> str:
    r"""Reverse IRCv3 tag-value escaping (`\s`/`\:`/`\\`/`\r`/`\n`) -- never raises."""
    if "\\" not in value:
        return value
    out: list[str] = []
    i = 0
    length = len(value)
    while i < length:
        char = value[i]
        if char == "\\" and i + 1 < length:
            out.append(_TAG_UNESCAPES.get(value[i + 1], value[i + 1]))
            i += 2
        else:
            if char != "\\":
                out.append(char)
            i += 1
    return "".join(out)


def _parse_tags(raw_tags: object) -> dict[str, str]:
    """Parse a raw IRCv3 `tag1=val1;tag2=val2` string into `{tag: value}`.

    `None`/non-`str`/empty input (CAP not granted, or a non-tagged line)
    yields `{}`, never raises. A valueless tag (`;mod;` with no `=`) maps
    to `""`, matching the IRCv3 spec's boolean-flag tag shape.
    """
    if not isinstance(raw_tags, str) or not raw_tags:
        return {}
    tags: dict[str, str] = {}
    for pair in raw_tags.split(";"):
        if not pair:
            continue
        key, _, value = pair.partition("=")
        tags[key] = _unescape_tag_value(value)
    return tags


def _parse_badges(raw_badges: str) -> list[str]:
    """`"moderator/1,subscriber/12,vip/1"` -> `["moderator", "subscriber", "vip"]`.

    Names only (version numbers dropped) -- no caller in this codebase
    needs the badge tier today, and any that does can re-derive it from
    the raw `badge-info` tag this receiver doesn't otherwise touch.
    """
    if not raw_badges:
        return []
    return [name for entry in raw_badges.split(",") if (name := entry.split("/", 1)[0])]


# The ignore comment below suppresses mypy --strict's "cannot subclass Any" complaint --
# Transport resolves to Any since waddle_transports ships no py.typed marker (see
# pyproject.toml's follow_imports="skip" override); the real ABC contract
# (name/directions/receive()) is still honored regardless.
class TwitchIrcReceiver(Transport):  # type: ignore[misc]
    """One persistent Twitch IRC connection (one channel) per `receive()` call.

    Not platform-level like Discord -- `IrcTransport.receive()` is a
    single-channel-per-connection contract, so `app.py` constructs one
    `TwitchIrcReceiver` per configured channel; each one's own
    `socket_lease.LeasedReceiver` ensures only one live svc-ingest replica
    ever holds an active iteration for that channel.
    """

    name: ClassVar[str] = "twitch_irc"
    directions: ClassVar[frozenset[Direction]] = frozenset({Direction.INBOUND})

    def __init__(self) -> None:
        """Build the receiver -- does not connect yet, see `receive()`."""
        self._irc = IrcTransport()

    async def receive(self, config: Mapping[str, Any]) -> AsyncIterator[Mapping[str, Any]]:
        """Delegate to `IrcTransport.receive()`, normalizing each raw PRIVMSG dict.

        `config` is one channel's full `IrcTransport` config (`host`/
        `port`/`nick`/`password_ref`/`use_tls`/`channel` -- see `config.py`
        `Config.twitch_irc_config_base()` + `app.py`'s per-channel wiring).
        Copied (never mutated in place -- `config` may be a caller-owned/
        shared mapping) and layered with `cap_requests` defaulting to
        :data:`_TWITCH_CAP_REQUESTS` via `setdefault` -- an explicit
        `cap_requests` already present in `config` (e.g. a future non-
        default test/deployment override) is left untouched.

        Real transform (not a stub) of `IrcTransport`'s own `{channel,
        sender, text, tags}` shape into the raw event dict `builtin_handlers/
        twitch_ingest.py::normalize()` consumes -- field names here are
        this receiver's own contract with that entrypoint, matching
        `receivers/discord_gateway.py`'s own precedent (no repo-wide "raw
        platform event" schema exists yet). `tags` (see module docstring)
        is parsed into Twitch's own per-message metadata: `author_id`/
        `user_id` (numeric, duplicated under both keys -- see module
        docstring), `display_name`, `message_id`, `room_id`, `badges`,
        and `is_mod`/`is_subscriber`/`is_vip`/`is_broadcaster`. CAP not
        granted (or a non-tagged line) -- every one of these is `None`/
        `False`/`[]`, never raised. Logs once per connection (DEBUG,
        first PRIVMSG seen, self-authored or not) whether tags are
        present, so a silently-ungranted CAP is diagnosable.

        Drops PRIVMSGs sent by the bot's OWN nick only (`config["nick"]`
        -- the same value `IrcTransport` itself connects as, see
        `Config.twitch_irc_config_base()`), compared case-insensitively
        (IRC nicks are case-insensitive on the wire); never other bots'
        messages, matching `DiscordGatewayReceiver._is_self`'s identical
        self-only scope. `config["nick"]` missing/non-string is an
        unknown-identity edge case -- errs toward NOT dropping, same
        rationale as the Discord receiver's pre-`on_ready` handling.
        """
        self_nick = config.get("nick")
        self_nick_lower = self_nick.lower() if isinstance(self_nick, str) else None
        irc_config: dict[str, Any] = dict(config)
        irc_config.setdefault("cap_requests", _TWITCH_CAP_REQUESTS)

        tags_presence_logged = False
        async for raw in self._irc.receive(irc_config):
            raw_tags = raw.get("tags")
            if not tags_presence_logged:
                logger.debug(
                    "receiver.tags_present platform=twitch present=%s",
                    isinstance(raw_tags, str) and bool(raw_tags),
                )
                tags_presence_logged = True

            sender = raw.get("sender")
            if (
                self_nick_lower is not None
                and isinstance(sender, str)
                and sender.lower() == self_nick_lower
            ):
                logger.debug("receiver.skipped_self platform=twitch sender=%s", sender)
                continue

            tags = _parse_tags(raw_tags)
            badges = _parse_badges(tags.get("badges", ""))
            user_id = tags.get("user-id") or None
            yield {
                "platform": "twitch",
                "channel_name": raw.get("channel", "").lstrip("#"),
                "author_username": sender,
                "content": raw.get("text"),
                "author_id": user_id,
                "user_id": user_id,
                "display_name": tags.get("display-name") or None,
                "message_id": tags.get("id") or None,
                "room_id": tags.get("room-id") or None,
                "badges": badges,
                "is_mod": tags.get("mod") == "1",
                "is_subscriber": tags.get("subscriber") == "1",
                "is_vip": "vip" in badges,
                "is_broadcaster": "broadcaster" in badges,
            }
