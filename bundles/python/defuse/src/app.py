"""`!defuse <amount|all>` -> a solo wager game: cut the right wire or lose the stake.

Original Waddles implementation; the *idea* is inspired by PenguinTwitchBot by SuperPenguinTV
(with permission) -- ideas/design only, no code copied.

Commands (grammar via `waddle_sdk.command`; the `<amount>` shape is recognized here, before the
parser runs, same as `gamble`):

- `!defuse` -- play with `DEFAULT_STAKE`.
- `!defuse <amount>` / `!defuse all` -- play with that stake (`all` = the host's `max_bet`).
- `!defuse list` -- the caller's own record (plays/wins).
- `!defuse set odds <1-95>` -- broadcaster/moderator-only win-percentage (fail-closed role signal).

Economy: a single `economy.wager(actor, stake, payout)` settles each game atomically -- payout is
`stake * PAYOUT_MULTIPLE` on a win (net `+stake`), `0` on a loss. Money moves only through the
host (bounded by `max_bet`, balance and the daily mint cap); this bundle never holds a balance.
Fail-loud: bad/zero stake, insufficient funds, over-cap and unlinked accounts each reply a clear
message (`CommandUsageError`) and move nothing.

State (kv, community-scoped, `.`-separated -- never `:`): cooldown/stat keys use
`sha256(community + ":" + actor_uuid)` pseudonyms; no handle ever reaches a key or log. Logs carry
`op`/`outcome`/bucket fields only. Feature flag `waddles.command-defuse` (default OFF) is checked
after the command matches.
"""

from __future__ import annotations

import hashlib
import random
from typing import Any

from waddle_sdk import clock, community_kv, economy, identity, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, ParsedCommand, parse_command
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

SPEC = CommandSpec(name="defuse")

FLAG_KEY = "waddles.command-defuse"
DEFAULT_STAKE = 10
PAYOUT_MULTIPLE = 2
DEFAULT_ODDS_PERCENT = 45
MIN_ODDS_PERCENT = 1
MAX_ODDS_PERCENT = 95
DEFAULT_COOLDOWN_SECONDS = 10

_ODDS_CONFIG_KEY = "defuse.config.odds"
_USAGE = (
    "Usage: !defuse [amount|all] | !defuse list | !defuse set odds <1-95> (set is admin/mod only)"
)
_PERMISSION_DENIED_MSG = "only moderators/broadcasters can configure !defuse"
_KNOWN_COMMANDS = frozenset({"play", "list", "config_set_odds", "usage"})
_WIRES = ("red", "blue", "green", "yellow")


def _pseudonym(community: str, actor_uuid: str) -> str:
    """Non-reversible per-(community, actor) key component; never the raw handle/uuid."""
    return hashlib.sha256(f"{community}:{actor_uuid}".encode()).hexdigest()


def _last_key(pseudonym: str) -> str:
    """Per-(community, actor) last-play timestamp key (cooldown gate)."""
    return f"defuse.last.{pseudonym}"


def _plays_key(pseudonym: str) -> str:
    """Per-(community, actor) plays counter key."""
    return f"defuse.plays.{pseudonym}"


def _wins_key(pseudonym: str) -> str:
    """Per-(community, actor) wins counter key."""
    return f"defuse.wins.{pseudonym}"


def _amount_bucket(amount: int) -> str:
    """Coarse amount bucket for PII-free logs."""
    for limit in (10, 100, 1000):
        if amount <= limit:
            return f"le{limit}"
    return "gt1000"


def _caller_role_signal(payload: dict[str, Any]) -> bool | None:
    """`True`/`False` from badge fields (strict `is True`), `None` if absent (absent denies)."""
    if "is_mod" not in payload and "is_broadcaster" not in payload:
        return None
    return payload.get("is_mod") is True or payload.get("is_broadcaster") is True


def _roll(percent: int) -> bool:
    """Return True with probability `percent`/100."""
    return random.randint(1, 100) <= percent  # noqa: S311 - a game, not security


def _resolve_command(parsed: ParsedCommand | None) -> str:
    """Map a parsed grammar result onto this bundle's command set (else `usage`)."""
    if parsed is None:
        return "usage"
    if parsed.option is None:
        return "play"
    if parsed.option == "list":
        return "list" if parsed.args is None else "usage"
    if parsed.option == "set":
        return "config_set_odds"
    return "usage"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!defuse`, then gate on the feature flag."""
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if stripped.partition(" ")[0].lower() != "!defuse":
        return None
    if not await feature_enabled(FLAG_KEY, default=False):
        log.debug("defuse.flag_off")
        return None

    payload: dict[str, Any] = {"channel_id": event.payload.get("channel_id")}
    word = stripped.partition(" ")[2].strip().lower()
    if word == "all" or (word.isascii() and word.isdigit()):
        payload["command"] = "play"
        payload["arg"] = word
    else:
        try:
            parsed: ParsedCommand | None = parse_command(stripped, SPEC)
        except CommandUsageError:
            parsed = None
        payload["command"] = _resolve_command(parsed)
        if payload["command"] == "config_set_odds" and parsed is not None:
            payload["arg"] = parsed.args
    log.info("defuse.transform matched", command=payload["command"])
    if "is_mod" in event.payload:
        payload["is_mod"] = event.payload["is_mod"] is True
    if "is_broadcaster" in event.payload:
        payload["is_broadcaster"] = event.payload["is_broadcaster"] is True
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload=payload,
        occurred_at=event.occurred_at,
    )


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result (see `gamble`)."""

    __slots__ = ("transport", "detail", "sub_type", "http_status")

    def __init__(self, *, transport: str, detail: str) -> None:
        """Record the provider relayed to and a short detail string."""
        self.transport = transport
        self.detail = detail
        self.sub_type = None
        self.http_status = None


def _int_or_none(raw: bytes | None) -> int | None:
    """Decode a kv integer, or None if absent/corrupt (corrupt is logged, never raised)."""
    if raw is None:
        return None
    try:
        return int(raw.decode())
    except (UnicodeDecodeError, ValueError):
        log.error("defuse.kv_value_corrupt")
        return None


async def _get_odds(community: str) -> int:
    """Return the community's win-percentage, or the default if unset/corrupt/out of range."""
    value = _int_or_none(await community_kv.get(community, _ODDS_CONFIG_KEY))
    if value is None or not MIN_ODDS_PERCENT <= value <= MAX_ODDS_PERCENT:
        return DEFAULT_ODDS_PERCENT
    return value


async def _actor() -> str:
    """Resolve the invoking actor's community uuid; usage-error if unlinked."""
    try:
        return await identity.resolve_actor()
    except identity.IdentityError as exc:
        log.info("defuse.actor_unresolved", error=type(exc).__name__)
        raise CommandUsageError("link your account before playing") from exc


async def _stake_for(arg: str | None, actor: str) -> int:
    """Parse the stake argument (bare = default, `all` = max_bet); usage-error if invalid."""
    if arg is None:
        return DEFAULT_STAKE
    if arg == "all":
        stake = await economy.max_bet(actor)
        if stake <= 0:
            raise CommandUsageError("you have no points to bet")
        return stake
    try:
        stake = int(arg)
    except ValueError as exc:
        raise CommandUsageError(_USAGE) from exc
    if stake < 1:
        raise CommandUsageError("bet must be at least 1 point")
    return stake


async def _handle_play(arg: str | None, *, community: str, username: str) -> str:
    """Validate, cooldown-gate, roll and settle one game via a single `economy.wager`."""
    actor = await _actor()
    try:
        stake = await _stake_for(arg, actor)
        pseudonym = _pseudonym(community, actor)
        now_ms = clock.now_millis()
        last = _int_or_none(await community_kv.get(community, _last_key(pseudonym)))
        if last is not None:
            remaining = DEFAULT_COOLDOWN_SECONDS - (now_ms - last) / 1000
            if remaining > 0:
                return f"\U0001f4a3 easy, {username}! try again in {max(1, round(remaining))}s."
        won = _roll(await _get_odds(community))
        new_balance = await economy.wager(actor, stake, stake * PAYOUT_MULTIPLE if won else 0)
        await community_kv.set(
            community, _last_key(pseudonym), str(now_ms).encode(), DEFAULT_COOLDOWN_SECONDS
        )
        await community_kv.increment(community, _plays_key(pseudonym), 1, 0)
        wire = random.choice(_WIRES)  # noqa: S311 - flavor only
        log.info(
            "defuse.settled", outcome="win" if won else "loss", amount_bucket=_amount_bucket(stake)
        )
        if won:
            await community_kv.increment(community, _wins_key(pseudonym), 1, 0)
            return (
                f"\U0001f4a3 {username} cuts the {wire} wire... DEFUSED! "
                f"+{stake} points (balance: {new_balance})"
            )
        return (
            f"\U0001f4a5 {username} cuts the {wire} wire... BOOM! "
            f"-{stake} points (balance: {new_balance})"
        )
    except economy.InsufficientFundsError as exc:
        raise CommandUsageError(f"{username}, you only have {exc.balance} points.") from exc
    except economy.OverCapError as exc:
        raise CommandUsageError(f"max bet is {exc.cap} points.") from exc
    except economy.EconomyError as exc:
        log.error("defuse.economy_error", error=type(exc).__name__)
        raise CommandUsageError("defusing is temporarily unavailable") from exc


async def _handle_list(*, community: str) -> str:
    """Render the caller's own plays/wins record."""
    pseudonym = _pseudonym(community, await _actor())
    plays = _int_or_none(await community_kv.get(community, _plays_key(pseudonym))) or 0
    wins = _int_or_none(await community_kv.get(community, _wins_key(pseudonym))) or 0
    if plays == 0:
        return "You haven't defused anything yet -- try !defuse!"
    return f"Bombs attempted: {plays}. Defused: {wins}."


async def _handle_set_odds(arg: str | None, *, community: str) -> str:
    """Parse+apply `set odds <1-95>`'s free-text tail."""
    parts = (arg or "").split()
    if len(parts) != 2 or parts[0].lower() != "odds":
        return _USAGE
    try:
        percent = int(parts[1])
    except ValueError:
        return f"'{parts[1]}' isn't a whole number"
    if not MIN_ODDS_PERCENT <= percent <= MAX_ODDS_PERCENT:
        return f"odds must be between {MIN_ODDS_PERCENT} and {MAX_ODDS_PERCENT} percent"
    await community_kv.set(community, _ODDS_CONFIG_KEY, str(percent).encode(), 0)
    return f"defuse win odds set to {percent}%"


async def _reply_for(
    command: str, payload: dict[str, Any], *, community: str, username: str
) -> str:
    """Compute the chat reply for one recognized command."""
    arg = payload.get("arg")
    arg_text = arg if isinstance(arg, str) else None
    if command == "usage":
        return _USAGE
    if command == "config_set_odds":
        if _caller_role_signal(payload) is not True:
            log.info("defuse.config_denied", command=command)
            return _PERMISSION_DENIED_MSG
        return await _handle_set_odds(arg_text, community=community)
    if command == "list":
        return await _handle_list(community=community)
    return await _handle_play(arg_text, community=community, username=username)


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: settle via the economy SDK, then relay the reply.

    Raises:
        ValueError: No `channel_id`, no `community`, or an unrecognized command.
        RuntimeError: A kv backend call failed (error reply + ERROR log emitted first).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("defuse reply requires a channel_id from the inbound chat.message")
    command = payload.get("command")
    if command not in _KNOWN_COMMANDS:
        raise ValueError(f"unrecognized defuse command: {command!r}")
    provider = envelope.event.platform
    community = envelope.community
    if not community:
        log.error("defuse.missing_community", command=command)
        raise ValueError("defuse requires a community context and cannot operate tenant-wide")

    username = envelope.event.actor or "someone"
    try:
        reply = await _reply_for(command, payload, community=community, username=username)
    except CommandUsageError as exc:
        reply = str(exc)
    except Exception as exc:
        log.error("defuse.kv_error", error=type(getattr(exc, "value", exc)).__name__)
        await relay.push(provider, {"channel": channel_id, "text": "defusing is unavailable."})
        raise RuntimeError("defuse state backend failed") from exc

    await relay.push(provider, {"channel": channel_id, "text": reply})
    log.info("defuse.dispatch relayed", platform=provider, command=command)
    return DispatchResult(transport=provider, detail=command)
