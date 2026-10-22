"""`!commands` -> lists the commands available in this community.

Enumeration approach (researched, see README "Limitation"): the bundle WIT world
(`wit/waddle-bundle/stage.wit`) exposes NO capability to enumerate a community's activated
bundles/commands -- only `%flags.enabled(key, default)` (one key at a time), `kv`, `db` (own
tables), `identity` etc. So this bundle ships a CURATED CATALOG of known command triggers
(`CATALOG`, one `(flag_suffix, trigger)` per first-party command bundle) and filters it through
each command's `waddles.command-<name>` flag as a per-command KILL-SWITCH: a flag explicitly
turned OFF hides the command; an unset flag counts as enabled (`default=True`), because
command bundles are activation-gated, not flag-gated. The host cannot tell this bundle whether a
bundle is actually *activated* for the community, so the list is "commands this deployment ships
and has not switched off", not a guarantee each one is activated.

Reply is capped to `MAX_REPLY_LEN` characters with a "+N more" suffix. Grammar via
`waddle_sdk.command.parse_command`; `!commands` takes no arguments (anything else -> usage).

PII-free logs: only platform + command count; never the actor or message text. Activation-gated
only -- no feature flag on `!commands` itself.
"""

from __future__ import annotations

from typing import Any

from waddle_sdk import log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, ParsedCommand, parse_command
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

SPEC = CommandSpec(name="commands")

#: `(flag_suffix, trigger)` -- flag key is `waddles.command-<flag_suffix>`. One entry per
#: first-party command bundle's primary trigger (from each bundle's `command_prefix`).
CATALOG: tuple[tuple[str, str], ...] = (
    ("about", "!about"),
    ("advice", "!advice"),
    ("announce", "!announce"),
    ("birthday", "!birthday"),
    ("bookmark", "!bookmark"),
    ("boop", "!boop"),
    ("catfact", "!catfact"),
    ("chat", "!chat-history"),
    ("choose", "!choose"),
    ("coinflip", "!coinflip"),
    ("command", "!command"),
    ("commands", "!commands"),
    ("compliment", "!compliment"),
    ("count", "!count"),
    ("countdown", "!countdown"),
    ("cringe", "!cringe"),
    ("dadjoke", "!dadjoke"),
    ("death", "!death"),
    ("dice", "!dice"),
    ("discord", "!discord"),
    ("dogfact", "!dogfact"),
    ("donate", "!donate"),
    ("duel", "!duel"),
    ("echo", "!echo"),
    ("eightball", "!8ball"),
    ("event", "!event"),
    ("first", "!first"),
    ("fish", "!fish"),
    ("fortune", "!fortune"),
    ("gamble", "!gamble"),
    ("guess", "!guess"),
    ("hangman", "!hangman"),
    ("heist", "!heist"),
    ("hello", "!hello"),
    ("highfive", "!highfive"),
    ("hug", "!hug"),
    ("hype", "!hype"),
    ("inventory", "!inventory"),
    ("iq", "!iq"),
    ("joke", "!joke"),
    ("label", "!label"),
    ("lastseen", "!lastseen"),
    ("lfg", "!lfg"),
    ("love", "!love"),
    ("loyalty", "!points"),
    ("lurk", "!lurk"),
    ("memories", "!memories"),
    ("merch", "!merch"),
    ("mood", "!mood"),
    ("music", "!sr"),
    ("poll", "!poll"),
    ("pronouns", "!pronouns"),
    ("quote", "!quote"),
    ("raffle", "!raffle"),
    ("rank", "!rank"),
    ("remind", "!remind"),
    ("rip", "!rip"),
    ("roast", "!roast"),
    ("roll", "!roll"),
    ("roulette", "!roulette"),
    ("rps", "!rps"),
    ("rules", "!rules"),
    ("say", "!say"),
    ("scramble", "!scramble"),
    ("secret", "!secret"),
    ("ship", "!ship"),
    ("shoutout", "!so"),
    ("slap", "!slap"),
    ("slots", "!slots"),
    ("socials", "!socials"),
    ("ticket", "!ticket"),
    ("time", "!time"),
    ("trivia", "!trivia"),
    ("uptime", "!uptime"),
    ("vote", "!vote"),
    ("waddle", "!waddle"),
    ("wave", "!wave"),
    ("weather", "!weather"),
    ("wheel", "!wheel"),
    ("winner", "!winner"),
)

MAX_REPLY_LEN = 400
_PREFIX = "Commands: "
_EMPTY = "No commands are currently enabled."
_USAGE = "Usage: !commands"


def flag_key(flag_suffix: str) -> str:
    """PostHog flag key for one command, `waddles.command-<name>`."""
    return f"waddles.command-{flag_suffix}"


async def enabled_triggers() -> list[str]:
    """Catalog triggers whose per-command flag is not explicitly OFF (unset -> enabled)."""
    out: list[str] = []
    for suffix, trigger in CATALOG:
        if await feature_enabled(flag_key(suffix), default=True):
            out.append(trigger)
    return out


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
    """Implement `action-stage.dispatch`: enumerate enabled commands and relay the list.

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
        triggers = await enabled_triggers()
        reply = format_reply(triggers)
        log.info("commands.dispatch listed", platform=provider, count=len(triggers))
    await relay.push(provider, {"channel": channel_id, "text": reply})
    return DispatchResult(transport=provider, detail=str(action))
