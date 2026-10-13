"""Real command+keyword chat bot -- process-stage handler replacing `echo_process`.

Referenced by `app_catalog.stages.process.entrypoint` (migration 084) as
`"builtin_handlers.bot_process:transform"` for `waddles.bot.discord.default` and
`waddles.bot.twitch.default`. Platform-agnostic: the same command/keyword
logic runs for Discord and Twitch, driven only by `event.payload["text"]`,
`event.actor`, and `event.platform` -- no platform-specific branching beyond
interpolating `event.platform` into the `!waddle` reply string.

Four response paths, tried in order:
  - `!command [args]` -- the bot's own built-in commands: Fun (roll/dice/
    flip/coin/8ball/hug/love/lurk), Utility (ping/hello/help/echo/waddle/
    uptime/time/rules/bot), and Community (socials/discord/so/followage),
    all handled locally -- see `_HELP_FUN`/`_HELP_UTILITY`/
    `_HELP_COMMUNITY_BUILTIN` and `_handle_command()`.
  - `!<feature-prefix> ...` -- COMMAND ROUTER (board-demo crunch): dispatches
    to a sibling process-stage feature handler's `transform(event)` in-process
    (`_FEATURE_TRANSFORMS`, built by `_load_feature_transforms()` below) and
    returns its reply. This is what makes `!quote`, `!alias`, `!poll`,
    `!announce`, `!forum`, `!chat-history`, `!channels`, and `!reputation`/
    `!rep` actually respond in live Discord/Twitch -- today only the bot's
    own app_id is routed a message, so these feature handlers (separate
    app_ids) otherwise never run.
  - `!<alias>` -- CUSTOM ALIAS EXPANSION: a word that is neither a built-in
    nor a loaded feature command is looked up against this community's own
    `command_aliases` table (`services.command_alias_store`, feature-gated
    by `_ALIAS_FEATURE_FLAG`) and, on a hit, rewritten to its stored
    expansion before falling through to the two paths above -- see
    `_expand_alias()`. Recognized commands never pay this lookup's cost.
  - keyword/greeting responder -- a small set of conversational triggers
    (greeting, bot-name mention, thanks) get a short reply; everything else
    returns `None` (no reply) so the bot never echoes random chatter back
    to the channel.

BULLETPROOF CORE (non-negotiable): the bot's own commands and the pipeline
must survive a broken feature. Two guards enforce this:
  - Guarded IMPORT (`_load_feature_transforms`) -- a feature module that
    fails to import is logged and excluded from the dispatch table; every
    other feature and the bot's own commands are unaffected.
  - Guarded DISPATCH (`_dispatch_feature`) -- a feature `transform()` that
    raises is caught, logged, and turned into a short graceful reply (never
    propagated into `bot_process`/the pipeline).
"""

from __future__ import annotations

import dataclasses
import importlib
import logging
import random  # noqa: S311 -- see `_roll`/`_flip`/`_eight_ball`: fun replies, not crypto
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from flask_core import PlatformEvent, get_bundle_context
from flask_core.feature_flags import feature_enabled

from services.command_alias_store import resolve_alias

logger = logging.getLogger(__name__)

_FALLBACK_ACTOR = "friend"

#: Process start, for `!uptime` -- monotonic so wall-clock changes never skew it.
_START_TIME = time.monotonic()

_EIGHT_BALL_ANSWERS = (
    "It is certain.",
    "Without a doubt.",
    "Yes, definitely.",
    "You may rely on it.",
    "Ask again later.",
    "Cannot predict now.",
    "Don't count on it.",
    "My sources say no.",
    "Outlook not so good.",
    "Signs point to yes.",
)

_GREETING_RE = re.compile(r"^(hi|hello|hey|yo|sup|howdy)\b", re.IGNORECASE)
_THANKS_RE = re.compile(r"\b(thanks|thank you|ty)\b", re.IGNORECASE)
_WADDLE_RE = re.compile(r"\bwaddles?\b", re.IGNORECASE)
_MENTION_RE = re.compile(r"@waddle(?:s|bot)?\b", re.IGNORECASE)

_BOT_COMMANDS = frozenset(
    {
        "ping",
        "hello",
        "hi",
        "hey",
        "help",
        "commands",
        "echo",
        "waddle",
        "roll",
        "dice",
        "flip",
        "coin",
        "8ball",
        "hug",
        "love",
        "lurk",
        "followage",
        "uptime",
        "time",
        "rules",
        "bot",
        "about",
        "socials",
        "discord",
    }
)

TransformFn = Callable[[PlatformEvent], Awaitable[PlatformEvent | None]]

#: Feature command-word -> sibling process-stage handler module. Only
#: command-style features with a real chat command AND a confirmed backing
#: DB table are listed here -- `welcome` (first-message detection, not a
#: command), `marketing_engagement` (non-command, event-type-driven feature)
#: are intentionally excluded. Two command words may point at the same
#: module (`chat-history`/`channels` -> `community_chat_process`;
#: `reputation`/`rep` -> `community_reputation_process`) -- each module
#: parses the full command text itself to tell its commands apart. Order is
#: also `!help`'s display order.
_FEATURE_MODULES: dict[str, str] = {
    "quote": "builtin_handlers.social_quote_process",
    "alias": "builtin_handlers.social_alias_process",
    "unalias": "builtin_handlers.social_alias_process",
    "poll": "builtin_handlers.community_polls_process",
    "announce": "builtin_handlers.community_announcements_process",
    "forum": "builtin_handlers.community_forums_process",
    "chat-history": "builtin_handlers.community_chat_process",
    "channels": "builtin_handlers.community_chat_process",
    "reputation": "builtin_handlers.community_reputation_process",
    "rep": "builtin_handlers.community_reputation_process",
    "inventory": "builtin_handlers.inventory_process",
    "so": "builtin_handlers.social_shoutout_process",
    "shoutout": "builtin_handlers.social_shoutout_process",
    "vso": "builtin_handlers.social_shoutout_process",
    "sr": "builtin_handlers.social_music_process",
    "songrequest": "builtin_handlers.social_music_process",
    "sq": "builtin_handlers.social_music_process",
    "songqueue": "builtin_handlers.social_music_process",
    "cc": "builtin_handlers.community_context_process",
    "points": "builtin_handlers.community_loyalty_process",
    "top": "builtin_handlers.community_loyalty_process",
    "shop": "builtin_handlers.community_loyalty_process",
    "redeem": "builtin_handlers.community_loyalty_process",
}


def _load_feature_transforms() -> dict[str, TransformFn]:
    """Import each candidate feature handler, excluding any that fails to import.

    GUARDED IMPORT: `bot_process` itself must still import, and its own
    commands must still work, even if every feature import fails. Called
    once at module import time; a broken module is logged and simply
    missing from the returned dispatch table.
    """
    transforms: dict[str, TransformFn] = {}
    for command, module_path in _FEATURE_MODULES.items():
        try:
            module = importlib.import_module(module_path)
            transforms[command] = module.transform
        except Exception as exc:  # noqa: BLE001 -- see docstring; must never break bot import
            logger.warning(
                "bot_process.feature_import_failed command=%s module=%s error=%s",
                command,
                module_path,
                exc,
            )
    return transforms


_FEATURE_TRANSFORMS: dict[str, TransformFn] = _load_feature_transforms()

#: PostHog flag key gating the `command_aliases` lookup below -- `waddles.
#: <module>.<feature>` convention (see `waddles.community.context`/
#: `waddles.social.music` elsewhere in this file's sibling handlers).
#: Default ON: an unrecognized `!<word>` looks itself up against this
#: community's custom aliases before falling back to "Unknown command."
_ALIAS_FEATURE_FLAG = "waddles.bot.command_aliases"


_HELP_FUN = "!roll, !flip, !dice, !coin, !8ball <q>, !hug <user>, !love <user>, !lurk"
_HELP_UTILITY = "!ping, !hello, !help, !echo <text>, !waddle, !uptime, !time, !rules, !bot"
_HELP_COMMUNITY_BUILTIN = "!socials, !discord, !so <user>, !followage"

#: Feature commands (from `_FEATURE_MODULES`) shown under `!help`'s
#: "Community" group, in display order. `poll` gets its own group below;
#: `rep` is omitted since `reputation` already covers it.
_HELP_COMMUNITY_FEATURES = (
    "quote",
    "alias",
    "announce",
    "forum",
    "chat-history",
    "channels",
    "reputation",
    "inventory",
    "sr",
)


def _build_help_text(feature_commands: dict[str, TransformFn]) -> str:
    """Build `!help`'s grouped text from the bot's own commands plus loaded features.

    Feature commands only appear once their sibling handler actually loaded
    (see `_load_feature_transforms`'s guarded import) -- a broken feature is
    simply missing from `!help`, never an error.
    """
    community_features = ", ".join(
        f"!{cmd}" for cmd in _HELP_COMMUNITY_FEATURES if cmd in feature_commands
    )
    community = _HELP_COMMUNITY_BUILTIN
    if community_features:
        community = f"{community}, {community_features}"

    lines = [
        "Commands:",
        f"Fun: {_HELP_FUN}",
        f"Utility: {_HELP_UTILITY}",
        f"Community: {community}",
    ]
    if "poll" in feature_commands:
        lines.append("Polls: !poll")
    return "\n".join(lines)


_HELP_TEXT = _build_help_text(_FEATURE_TRANSFORMS)


def _actor_or_fallback(actor: str | None) -> str:
    """Return `actor`, or a generic stand-in when the event carries none."""
    return actor if actor else _FALLBACK_ACTOR


def _roll() -> int:
    """Roll a 1-6 die for the `!roll` command -- a game mechanic, not a security token."""
    return random.randint(1, 6)  # noqa: S311 # nosec B311 -- game roll, not crypto


def _flip() -> str:
    """Flip a coin for the `!flip`/`!coin` command -- a game mechanic, not a security token."""
    return random.choice(["Heads", "Tails"])  # noqa: S311 # nosec B311 -- game roll, not crypto


def _eight_ball() -> str:
    """Pick a classic Magic 8-Ball answer for `!8ball` -- a fun reply, not a security token."""
    return random.choice(_EIGHT_BALL_ANSWERS)  # noqa: S311 # nosec B311 -- fun reply, not crypto


def _love_percent() -> int:
    """Roll a 0-100 love match percentage for `!love` -- a fun reply, not a security token."""
    return random.randint(0, 100)  # noqa: S311 # nosec B311 -- fun reply, not crypto


def _format_uptime() -> str:
    """Format elapsed time since module import as `Xh Ym Zs` for `!uptime`."""
    elapsed = int(time.monotonic() - _START_TIME)
    hours, remainder = divmod(elapsed, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _handle_command(command: str, rest: str, *, actor: str | None, platform: str) -> str | None:
    """Resolve a `!command` (already lowercased, prefix stripped) to a reply string or `None`."""
    who = _actor_or_fallback(actor)

    if command == "ping":
        return "pong \U0001f427"
    if command in ("hello", "hi", "hey"):
        return f"Hey {who}! \U0001f44b waddles is online."
    if command in ("help", "commands"):
        return _HELP_TEXT
    if command == "echo":
        text = rest.strip()
        return text if text else "Usage: !echo <text>"
    if command == "waddle":
        return f"\U0001f427 *waddles across {platform}*"
    if command in ("roll", "dice"):
        return f"\U0001f3b2 {who} rolled a {_roll()}"
    if command in ("flip", "coin"):
        return f"\U0001fa99 {_flip()}"
    if command == "8ball":
        question = rest.strip()
        return f"\U0001f3b1 {_eight_ball()}" if question else "Usage: !8ball <question>"
    if command == "hug":
        target = rest.strip()
        return (
            f"{who} gives {target} a warm hug! \U0001f917"
            if target
            else f"{who} sends out a big hug! \U0001f917"
        )
    if command == "love":
        target = rest.strip() if rest.strip() else "the chat"
        return f"\U0001f495 {who} + {target} = {_love_percent()}% love match!"
    if command == "lurk":
        return f"{who} slips into the shadows to lurk \U0001f440"
    if command == "followage":
        return f"Followage tracking is coming soon, {who}! \U0001f427"
    if command == "uptime":
        return f"waddles has been up for {_format_uptime()} \U0001f427"
    if command == "time":
        return f"Server time: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}"
    if command == "rules":
        return "1) Be kind  2) No spam  3) Have fun \U0001f427"
    if command in ("bot", "about"):
        return "I'm waddles \U0001f427 -- a multi-platform community bot by PenguinTech."
    if command == "socials":
        return "Follow waddles: twitter.com/waddlebot | instagram.com/waddlebot"
    if command == "discord":
        return "Join our Discord: discord.gg/waddlebot"
    # Defensive only -- `transform()` calls this exclusively for commands in
    # `_BOT_COMMANDS`, which the branches above fully enumerate.
    return "Unknown command. Try !help"  # pragma: no cover


def _handle_keyword(text: str, *, actor: str | None) -> str | None:
    """Resolve a non-command message to a keyword/greeting reply, or `None` for no reply."""
    who = _actor_or_fallback(actor)

    if _GREETING_RE.match(text):
        return f"Hey {who}! \U0001f44b"
    if _WADDLE_RE.search(text) or _MENTION_RE.search(text):
        return "\U0001f427 someone say my name?"
    if _THANKS_RE.search(text):
        return f"np, {who}! \U0001f427"
    return None


def _community_id(community: str | None) -> int | None:
    """Best-effort `int(community)` for the alias flag/lookup; `None`/unparseable -> `None`."""
    if community is None:
        return None
    try:
        return int(community)
    except ValueError:
        return None


async def _expand_alias(command: str, rest: str) -> tuple[str, str, str] | None:
    """Resolve `command` against this community's `command_aliases` table, rewriting the message.

    Only called for a word that is neither a built-in command nor a loaded
    feature command (see `transform()`) -- a recognized command never pays
    this lookup's cost. Feature-gated (`_ALIAS_FEATURE_FLAG`, default ON)
    via the same `feature_enabled` helper every other handler uses; a
    tenant-wide envelope (`get_bundle_context().community is None`) skips
    the lookup entirely, matching `services.command_alias_store.
    resolve_alias`'s own "no community, nothing to look up" contract.

    Returns the rewritten `(command, rest, text)` triple on an alias hit
    (`text` is the full `!<expansion>` string, single-space join + strip,
    re-parsed exactly once here) -- `transform()` never calls this again on
    the rewritten command, so an alias whose own expansion is itself
    unrecognized simply falls through to the normal unknown-command reply
    rather than looping. Returns `None` when nothing should be rewritten
    (flag off, no community, no matching alias, or an empty expansion).
    """
    ctx = get_bundle_context()
    community_id = _community_id(ctx.community)
    if community_id is None:
        logger.debug("bot_process.alias_lookup_skipped_no_community command=%s", command)
        return None

    enabled = await feature_enabled(
        _ALIAS_FEATURE_FLAG, tenant=ctx.tenant, community=community_id, default=True
    )
    if not enabled:
        logger.debug("bot_process.alias_flag_disabled_no_lookup command=%s", command)
        return None

    alias = await resolve_alias(community_id=community_id, alias=command)
    if alias is None:
        return None

    expansion = f"{alias.target_command} {rest}".strip() if rest else alias.target_command.strip()
    if not expansion:
        return None

    logger.debug("bot_process.alias_expanded alias=%s target=%s", command, alias.target_command)
    new_parts = expansion.split(maxsplit=1)
    new_command = new_parts[0].lower()
    new_rest = new_parts[1] if len(new_parts) > 1 else ""
    return new_command, new_rest, f"!{expansion}"


async def _dispatch_feature(command: str, event: PlatformEvent) -> PlatformEvent | None:
    """Call a feature handler's `transform()`, guarding the bot against any failure.

    GUARDED DISPATCH: a feature `transform()` raising (bad SQL, a bug,
    anything) must never propagate into `bot_process`/the pipeline -- it
    is caught here and turned into a short graceful reply. The bot's own
    commands and every other feature remain unaffected.
    """
    transform_fn = _FEATURE_TRANSFORMS[command]
    try:
        return await transform_fn(event)
    except Exception as exc:  # noqa: BLE001 -- see docstring; a feature must never crash the bot
        logger.error("bot_process.feature_dispatch_failed command=%s error=%s", command, exc)
        return dataclasses.replace(
            event, payload={**event.payload, "text": "that command hit a snag \U0001f427"}
        )


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Route to a built-in command, a feature handler, a custom alias, or the keyword responder.

    Reads `event.payload["text"]` for the message, `event.actor` for who to
    address, and `event.platform` for the `!waddle` reply. `!<word>` first
    checks the bot's own built-in commands, then the feature dispatch table
    (`_FEATURE_TRANSFORMS`); an unrecognized word is tried once against
    this community's own command aliases (`_expand_alias`) before falling
    back to "Unknown command." -- see module docstring for the four-path
    router and its guards. Returns a NEW `PlatformEvent` (`dataclasses.
    replace`) with only `payload["text"]` replaced by the reply -- every
    other payload field (`channel_id`, `channel_name`, ...) is preserved so
    reply-in-place keeps working -- or `None` when nothing should be sent
    (empty text, unrecognized chatter).
    """
    raw_text = event.payload.get("text")
    if not isinstance(raw_text, str):
        return None
    text = raw_text.strip()
    if not text:
        return None

    if text.startswith("!"):
        parts = text[1:].split(maxsplit=1)
        if not parts or not parts[0]:
            return None
        command = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        if command not in _BOT_COMMANDS and command not in _FEATURE_TRANSFORMS:
            expanded = await _expand_alias(command, rest)
            if expanded is not None:
                command, rest, rewritten_text = expanded
                event = dataclasses.replace(
                    event, payload={**event.payload, "text": rewritten_text}
                )

        if command in _BOT_COMMANDS:
            reply = _handle_command(command, rest, actor=event.actor, platform=event.platform)
        elif command in _FEATURE_TRANSFORMS:
            return await _dispatch_feature(command, event)
        else:
            reply = "Unknown command. Try !help"
    else:
        reply = _handle_keyword(text, actor=event.actor)

    if reply is None:
        return None

    return dataclasses.replace(event, payload={**event.payload, "text": reply})
