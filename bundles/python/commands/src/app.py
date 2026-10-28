"""`!commands` -> lists the commands available in this community.

Enumeration approach (researched, see README "Limitation"): the bundle WIT world
(`wit/waddle-bundle/stage.wit`) exposes NO capability to enumerate a community's activated
bundles/commands -- only `kv`, `db` (own tables), `identity` etc. So this bundle ships a CURATED
CATALOG of known command triggers (`CATALOG`, one trigger per first-party command bundle) and
lists it as-is. There is NO feature-flag filtering: command bundles are activation-gated only
(Installed -> Enabled -> Activated), and the host routes a command event solely to bundles
activated for that community. The host cannot tell this bundle which bundles are activated, so
the list is "commands this deployment ships", not a guarantee each one is activated here.

Reply is capped to `MAX_REPLY_LEN` characters with a "+N more" suffix. Grammar via
`waddle_sdk.command.parse_command`; `!commands` takes no arguments (anything else -> usage).

PII-free logs: only platform + command count; never the actor or message text. Activation-gated
only -- no feature flag on `!commands` itself, and none read on behalf of other commands.
"""

from __future__ import annotations

from typing import Any

from waddle_sdk import log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, ParsedCommand, parse_command
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

SPEC = CommandSpec(name="commands")

#: One primary trigger per first-party command bundle (from each bundle's `command_prefix`).
CATALOG: tuple[str, ...] = (
    "!about",
    "!advice",
    "!announce",
    "!birthday",
    "!bookmark",
    "!boop",
    "!catfact",
    "!chat-history",
    "!choose",
    "!coinflip",
    "!command",
    "!commands",
    "!compliment",
    "!count",
    "!countdown",
    "!cringe",
    "!dadjoke",
    "!death",
    "!dice",
    "!discord",
    "!dogfact",
    "!donate",
    "!duel",
    "!echo",
    "!8ball",
    "!event",
    "!first",
    "!fish",
    "!fortune",
    "!gamble",
    "!guess",
    "!hangman",
    "!heist",
    "!hello",
    "!highfive",
    "!hug",
    "!hype",
    "!inventory",
    "!iq",
    "!joke",
    "!label",
    "!lastseen",
    "!lfg",
    "!love",
    "!points",
    "!lurk",
    "!memories",
    "!merch",
    "!mood",
    "!sr",
    "!poll",
    "!pronouns",
    "!quote",
    "!raffle",
    "!rank",
    "!remind",
    "!rip",
    "!roast",
    "!roll",
    "!roulette",
    "!rps",
    "!rules",
    "!say",
    "!scramble",
    "!secret",
    "!ship",
    "!so",
    "!slap",
    "!slots",
    "!socials",
    "!ticket",
    "!time",
    "!trivia",
    "!uptime",
    "!vote",
    "!waddle",
    "!wave",
    "!weather",
    "!wheel",
    "!winner",
)

MAX_REPLY_LEN = 400
_PREFIX = "Commands: "
_EMPTY = "No commands are currently enabled."
_USAGE = "Usage: !commands"


def format_reply(triggers: list[str], max_len: int = MAX_REPLY_LEN) -> str:
    """Join triggers into one chat line within `max_len`, appending "+N more" if truncated."""
    if not triggers:
        return _EMPTY
    shown: list[str] = []
    for i, trig in enumerate(triggers):
        remaining = len(triggers) - i - 1
        candidate = _PREFIX + ", ".join([*shown, trig])
        reserve = len(f" (+{remaining} more)") if remaining else 0
        if len(candidate) + reserve > max_len:
            break
        shown.append(trig)
    if not shown:  # pathological max_len; still say something truthful
        return f"{len(triggers)} commands available."
    hidden = len(triggers) - len(shown)
    return _PREFIX + ", ".join(shown) + (f" (+{hidden} more)" if hidden else "")


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!commands` (bare only)."""
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if stripped.partition(" ")[0].lower() != "!commands":
        return None
    try:
        parsed: ParsedCommand | None = parse_command(stripped, SPEC)
    except CommandUsageError:
        parsed = None
    action = "list" if parsed is not None and parsed.option is None else "usage"
    log.info("commands.transform matched", platform=event.platform, action=action)
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={"action": action, "channel_id": event.payload.get("channel_id")},
        occurred_at=event.occurred_at,
    )


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result -- see `pyping`'s own `app.py`."""

    __slots__ = ("transport", "detail", "sub_type", "http_status")

    def __init__(self, *, transport: str, detail: str) -> None:
        """Record which provider the reply was relayed to, and a short detail string."""
        self.transport = transport
        self.detail = detail
        self.sub_type = None
        self.http_status = None


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: relay the curated command catalog (no flag filtering).

    Raises:
        ValueError: The payload has no `channel_id`, or an unrecognized `action`.
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("commands reply requires a channel_id from the inbound chat.message")
    action = payload.get("action")
    if action not in ("list", "usage"):
        raise ValueError(f"unrecognized commands action: {action!r}")

    provider = envelope.event.platform
    if action == "usage":
        reply = _USAGE
    else:
        triggers = list(CATALOG)
        reply = format_reply(triggers)
        log.info("commands.dispatch listed", platform=provider, count=len(triggers))
    await relay.push(provider, {"channel": channel_id, "text": reply})
    return DispatchResult(transport=provider, detail=str(action))
