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

Cooldown: `kv.increment` is the only compare-and-swap, so the per-actor cooldown is an `increment`
on a TTL'd key -- exactly one concurrent play sees `1` and wins the window (a check-then-set let a
burst of concurrent plays all pass). The host re-applies the TTL on every call, so a refused attempt
restarts the window (the reply says so). Funds/cap are pre-checked first so a plain refusal never
burns the cooldown.

Replay safety (at-most-once): the pipeline is at-least-once. The inbound `message_id` (forwarded by
`transform`) is recorded via `waddle_sdk.event_dedupe` AFTER the cooldown and BEFORE the roll, the
wager and the stat counters, and a redelivery is skipped silently -- no re-roll, no second wager,
no double-counted plays/wins, no reply. A crash after the claim loses one play, never repeats it; a
payload with no `message_id` fails loud.

State (kv, community-scoped, `.`-separated -- never `:`): cooldown/stat/dedupe keys use
`sha256(community + ":" + actor_uuid)` pseudonyms (the dedupe ring holds truncated hashes of the
platform message id, never the id); no handle ever reaches a key or log. Logs carry
`op`/`outcome`/bucket fields only. Activation-gated only -- no feature flag: the platform's
installed -> enabled -> activated check decides whether this bundle runs.

Output safety: `event.actor` is interpolated into most replies. The host-minted
`{user:<token>}` actor stays live (egress renders the display name); any other value is
untrusted and goes through `waddle_sdk.safe_text.sanitize_user_text` (see `_actor_label`),
as does every other typed echo.
"""

from __future__ import annotations

import hashlib
import random
import re
from typing import Any

from waddle_sdk import community_kv, economy, event_dedupe, identity, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, ParsedCommand, parse_command
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.safe_text import sanitize_user_text

SPEC = CommandSpec(name="defuse")

DEFAULT_STAKE = 10
PAYOUT_MULTIPLE = 2
DEFAULT_ODDS_PERCENT = 45
MIN_ODDS_PERCENT = 1
MAX_ODDS_PERCENT = 95
DEFAULT_COOLDOWN_SECONDS = 10

_ODDS_CONFIG_KEY = "defuse.config.odds"
_DEDUPE_NAMESPACE = "defuse"
_USAGE = (
    "Usage: !defuse [amount|all] | !defuse list | !defuse set odds <1-95> (set is admin/mod only)"
)
_PERMISSION_DENIED_MSG = "only moderators/broadcasters can configure !defuse"
_KNOWN_COMMANDS = frozenset({"play", "list", "config_set_odds", "usage"})
_WIRES = ("red", "blue", "green", "yellow")


#: The pipeline delivers `event.actor` as its own host-minted placeholder; anchored so ONLY an actor
#: that is exactly one `{user:<token>}` is left live (never one embedded in other text).
_USER_TOKEN_RE = re.compile(r"^\{user:([A-Za-z0-9_:.-]+)\}$")
_FALLBACK_ACTOR = "someone"


def _actor_label(actor: object) -> str:
    """Return a reply-safe label for `event.actor`.

    The host-minted `{user:<token>}` stays live so egress renders the display name. Anything else
    (a raw handle when tokenization is off, a missing actor) is untrusted text and goes through
    `sanitize_user_text` in full, so a platform display name can never forge a mention.
    """
    if not isinstance(actor, str) or not actor.strip():
        return _FALLBACK_ACTOR
    label = actor.strip()
    match = _USER_TOKEN_RE.match(label)
    return sanitize_user_text(label, allow_tokens=(match.group(1),) if match else ())


def _pseudonym(community: str, actor_uuid: str) -> str:
    """Non-reversible per-(community, actor) key component; never the raw handle/uuid."""
    return hashlib.sha256(f"{community}:{actor_uuid}".encode()).hexdigest()


def _cooldown_key(pseudonym: str) -> str:
    """Per-(community, actor) cooldown claim key (an atomic counter with the cooldown as TTL)."""
    return f"defuse.cd.{pseudonym}"


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
    """Implement `process-stage.transform`: recognize `!defuse`."""
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if stripped.partition(" ")[0].lower() != "!defuse":
        return None

    payload: dict[str, Any] = {"channel_id": event.payload.get("channel_id")}
    message_id = event.payload.get("message_id")
    if isinstance(message_id, str):  # replay-dedupe key; `dispatch` refuses a play without it
        payload["message_id"] = message_id
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


async def _precheck(actor: str, stake: int, username: str) -> None:
    """Refuse an unaffordable or over-cap stake BEFORE the cooldown is claimed (reads only)."""
    held = await economy.balance(actor)
    if stake > held:
        raise CommandUsageError(f"{username}, you only have {held} points.")
    cap = await economy.max_bet(actor)
    if stake > cap:
        raise CommandUsageError(f"max bet is {cap} points.")


async def _handle_play(
    arg: str | None,
    *,
    community: str,
    username: str,
    payload: dict[str, Any],
    platform: str,
) -> str | None:
    """Replay-guard, validate, cooldown-claim, roll and settle one game via `economy.wager`.

    Returns `None` for a redelivered event (already claimed): nothing is mutated or sent.
    """
    actor = await _actor()
    pseudonym = _pseudonym(community, actor)
    event_id = event_dedupe.event_id_from_payload(payload, platform=platform)
    if await event_dedupe.already_seen(
        community, namespace=_DEDUPE_NAMESPACE, subject=pseudonym, event_id=event_id
    ):
        log.info("defuse.redelivery_skipped", op="seen")
        return None
    try:
        stake = await _stake_for(arg, actor)
        await _precheck(actor, stake, username)
        claims = await community_kv.increment(
            community, _cooldown_key(pseudonym), 1, DEFAULT_COOLDOWN_SECONDS
        )
        if claims != 1:
            return f"\U0001f4a3 easy, {username}! try again in {DEFAULT_COOLDOWN_SECONDS}s."
        if not await event_dedupe.claim(
            community, namespace=_DEDUPE_NAMESPACE, subject=pseudonym, event_id=event_id
        ):
            log.info("defuse.redelivery_skipped", op="claim")
            return None
        won = _roll(await _get_odds(community))
        new_balance = await economy.wager(actor, stake, stake * PAYOUT_MULTIPLE if won else 0)
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
        return f"'{sanitize_user_text(parts[1])}' isn't a whole number"
    if not MIN_ODDS_PERCENT <= percent <= MAX_ODDS_PERCENT:
        return f"odds must be between {MIN_ODDS_PERCENT} and {MAX_ODDS_PERCENT} percent"
    await community_kv.set(community, _ODDS_CONFIG_KEY, str(percent).encode(), 0)
    return f"defuse win odds set to {percent}%"


async def _reply_for(
    command: str, payload: dict[str, Any], *, community: str, username: str, platform: str
) -> str | None:
    """Compute the chat reply for one recognized command (`None` = a skipped redelivery)."""
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
    return await _handle_play(
        arg_text, community=community, username=username, payload=payload, platform=platform
    )


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

    username = _actor_label(envelope.event.actor)
    try:
        reply = await _reply_for(
            command, payload, community=community, username=username, platform=provider
        )
    except CommandUsageError as exc:
        reply = str(exc)
    except event_dedupe.EventIdError as exc:
        log.error("defuse.missing_event_id", command=command)
        await relay.push(provider, {"channel": channel_id, "text": "defusing is unavailable."})
        raise RuntimeError("defuse requires the inbound message_id to be replay-safe") from exc
    except Exception as exc:
        log.error("defuse.kv_error", error=type(getattr(exc, "value", exc)).__name__)
        await relay.push(provider, {"channel": channel_id, "text": "defusing is unavailable."})
        raise RuntimeError("defuse state backend failed") from exc

    if reply is None:
        log.info("defuse.dispatch skipped duplicate", platform=provider, command=command)
        return DispatchResult(transport=provider, detail="duplicate")
    await relay.push(provider, {"channel": channel_id, "text": reply})
    log.info("defuse.dispatch relayed", platform=provider, command=command)
    return DispatchResult(transport=provider, detail=command)
