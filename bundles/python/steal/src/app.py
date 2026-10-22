"""`!steal @user` -> a risk/reward points-heist game over the shared community economy.

Original Waddles implementation; the *idea* is inspired by PenguinTwitchBot by SuperPenguinTV
(with permission) -- ideas/design only, no code copied.

Commands (grammar via `waddle_sdk.command`; the `@mention` target is not a declared verb and so
is recognized here, from the host-issued `{user:<token>}` placeholder, before the parser runs):

- `!steal @target` -- attempt a steal. Random success/fail (`SUCCESS_PERCENT`).
- `!steal list` -- show the caller's own steal record (attempts/successes).
- `!steal set cooldown <seconds>` -- broadcaster/moderator-only (fail-closed role signal).

## Economy semantics (read this)

The host's `economy.transfer` only ever moves the *invoking actor's* money -- a bundle can pay the
actor's funds TO any member but never pull another member's (`waddle_sdk.economy` docstring,
`actor_mismatch`). A literal target->actor transfer is therefore impossible by design, so:

- **Success**: actor stakes `STAKE` and `wager`s it back with `STAKE + loot` (net `+loot`), where
  `loot` is random, bounded by `min(LOOT_CAP, target_balance)` -- the victim's balance is the
  *bound*, and the payout is subject to the host's own daily mint cap/quotas.
- **Failure**: the actor is "caught" and `transfer`s a penalty (`STAKE`) TO the target.

Fail-loud: no mention, self-target, an unresolved/ambiguous target, an empty victim or too-poor
actor each reply a clear message (`CommandUsageError`) and move no money. Economy/identity host
refusals reply with a stable message and are logged by type only.

State (kv, community-scoped, `.`-separated keys -- never `:`): cooldown/stat keys use
`sha256(community + ":" + actor_uuid)` pseudonyms (`_pseudonym`). The actor handle appears only
in the visible chat reply, never in a key or log. Logs carry `op`/`outcome`/bucket fields only.

Feature flag `waddles.command-steal` (default OFF) is checked after the command matches.
"""

from __future__ import annotations

import hashlib
import random
from typing import Any

from waddle_sdk import clock, community_kv, economy, identity, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, ParsedCommand, parse_command
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

SPEC = CommandSpec(name="steal")

FLAG_KEY = "waddles.command-steal"
SUCCESS_PERCENT = 40
STAKE = 10
LOOT_CAP = 100
DEFAULT_COOLDOWN_SECONDS = 60
MIN_COOLDOWN_SECONDS = 0
MAX_COOLDOWN_SECONDS = 3600

_COOLDOWN_CONFIG_KEY = "steal.config.cooldown"
_USAGE = "Usage: !steal @user | !steal list | !steal set cooldown <seconds> (set is admin/mod only)"
_PERMISSION_DENIED_MSG = "only moderators/broadcasters can configure !steal"
_KNOWN_COMMANDS = frozenset({"steal", "list", "config_set_cooldown", "usage"})


def _pseudonym(community: str, actor_uuid: str) -> str:
    """Non-reversible per-(community, actor) key component; never the raw handle/uuid."""
    return hashlib.sha256(f"{community}:{actor_uuid}".encode()).hexdigest()


def _laststeal_key(pseudonym: str) -> str:
    """Per-(community, actor) cooldown timestamp key."""
    return f"steal.last.{pseudonym}"


def _attempts_key(pseudonym: str) -> str:
    """Per-(community, actor) attempts counter key."""
    return f"steal.attempts.{pseudonym}"


def _wins_key(pseudonym: str) -> str:
    """Per-(community, actor) successful-steals counter key."""
    return f"steal.wins.{pseudonym}"


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
        return "usage"  # bare `!steal` has no target
    if parsed.option == "list":
        return "list" if parsed.args is None else "usage"
    if parsed.option == "set":
        return "config_set_cooldown"
    return "usage"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!steal`, then gate on the feature flag."""
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if stripped.partition(" ")[0].lower() != "!steal":
        return None
    if not await feature_enabled(FLAG_KEY, default=False):
        log.debug("steal.flag_off")
        return None

    payload: dict[str, Any] = {"channel_id": event.payload.get("channel_id")}
    tokens = identity.mention_tokens(stripped)
    if tokens:
        payload["command"] = "steal"
        payload["tokens"] = tokens
    else:
        try:
            parsed: ParsedCommand | None = parse_command(stripped, SPEC)
        except CommandUsageError:
            parsed = None
        payload["command"] = _resolve_command(parsed)
        if payload["command"] == "config_set_cooldown" and parsed is not None:
            payload["arg"] = parsed.args
    log.info("steal.transform matched", command=payload["command"])
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
        log.error("steal.kv_value_corrupt")
        return None


async def _cooldown_seconds(community: str) -> int:
    """Return the configured cooldown, or the default if unset/corrupt/out of range."""
    value = _int_or_none(await community_kv.get(community, _COOLDOWN_CONFIG_KEY))
    if value is None or not MIN_COOLDOWN_SECONDS <= value <= MAX_COOLDOWN_SECONDS:
        return DEFAULT_COOLDOWN_SECONDS
    return value


async def _check_cooldown(community: str, pseudonym: str, username: str) -> str | None:
    """Return a slow-down reply if the actor is cooling down, else stamp the cooldown and None."""
    cooldown = await _cooldown_seconds(community)
    if cooldown <= 0:
        return None
    now_ms = clock.now_millis()
    last = _int_or_none(await community_kv.get(community, _laststeal_key(pseudonym)))
    if last is not None:
        remaining = cooldown - (now_ms - last) / 1000
        if remaining > 0:
            return f"\U0001f9b9 lay low, {username}! try again in {max(1, round(remaining))}s."
    await community_kv.set(community, _laststeal_key(pseudonym), str(now_ms).encode(), cooldown)
    return None


async def _resolve_parties(tokens: list[str]) -> tuple[str, str]:
    """Resolve actor + first mention to community uuids; usage-error on any refusal."""
    try:
        actor = await identity.resolve_actor()
    except identity.IdentityError as exc:
        log.info("steal.actor_unresolved", error=type(exc).__name__)
        raise CommandUsageError("link your account before stealing") from exc
    try:
        target = await identity.resolve_mention(tokens[0])
    except identity.AmbiguousError as exc:
        raise CommandUsageError("that name matches more than one member") from exc
    except identity.IdentityError as exc:
        log.info("steal.target_unresolved", error=type(exc).__name__)
        raise CommandUsageError("couldn't find that member to steal from") from exc
    if target == actor:
        raise CommandUsageError("you can't steal from yourself")
    return actor, target


async def _handle_steal(tokens: list[str], *, community: str, username: str) -> str:
    """Resolve, validate, cooldown-gate, roll and settle one steal; return the chat reply."""
    if not tokens:
        raise CommandUsageError(_USAGE)
    actor, target = await _resolve_parties(tokens)
    try:
        actor_balance, target_balance = await economy.balance(actor), await economy.balance(target)
        if target_balance <= 0:
            raise CommandUsageError("that member has nothing to steal")
        cap = await economy.max_bet(actor)
        stake = min(STAKE, cap)
        if actor_balance < STAKE or stake < 1:
            raise CommandUsageError(f"you need at least {STAKE} points to try a steal")
        pseudonym = _pseudonym(community, actor)
        slow = await _check_cooldown(community, pseudonym, username)
        if slow is not None:
            return slow
        await community_kv.increment(community, _attempts_key(pseudonym), 1, 0)
        if _roll(SUCCESS_PERCENT):
            loot = random.randint(1, min(LOOT_CAP, target_balance))  # noqa: S311 - a game
            new_balance = await economy.wager(actor, stake, stake + loot)
            await community_kv.increment(community, _wins_key(pseudonym), 1, 0)
            log.info("steal.settled", outcome="win", amount_bucket=_amount_bucket(loot))
            return f"\U0001f9b9 {username} stole {loot} points! (balance: {new_balance})"
        await economy.transfer(actor, target, stake)
        log.info("steal.settled", outcome="caught", amount_bucket=_amount_bucket(stake))
        return f"\U0001f6a8 {username} got caught and paid {stake} points to the victim."
    except economy.InsufficientFundsError as exc:
        raise CommandUsageError(f"you only have {exc.balance} points") from exc
    except economy.OverCapError as exc:
        raise CommandUsageError(f"that exceeds the economy cap ({exc.cap})") from exc
    except economy.EconomyError as exc:
        log.error("steal.economy_error", error=type(exc).__name__)
        raise CommandUsageError("stealing is temporarily unavailable") from exc


async def _handle_list(*, community: str) -> str:
    """Render the caller's own attempts/successes record."""
    try:
        actor = await identity.resolve_actor()
    except identity.IdentityError as exc:
        raise CommandUsageError("link your account first") from exc
    pseudonym = _pseudonym(community, actor)
    attempts = _int_or_none(await community_kv.get(community, _attempts_key(pseudonym))) or 0
    wins = _int_or_none(await community_kv.get(community, _wins_key(pseudonym))) or 0
    if attempts == 0:
        return "You haven't tried stealing yet -- try !steal @someone!"
    return f"Steal attempts: {attempts}. Successful: {wins}."


async def _handle_set_cooldown(arg: str | None, *, community: str) -> str:
    """Parse+apply `set cooldown <seconds>`'s free-text tail."""
    parts = (arg or "").split()
    if len(parts) != 2 or parts[0].lower() != "cooldown":
        return _USAGE
    try:
        seconds = int(parts[1])
    except ValueError:
        return f"'{parts[1]}' isn't a whole number of seconds"
    if not MIN_COOLDOWN_SECONDS <= seconds <= MAX_COOLDOWN_SECONDS:
        return f"cooldown must be between {MIN_COOLDOWN_SECONDS} and {MAX_COOLDOWN_SECONDS} seconds"
    await community_kv.set(community, _COOLDOWN_CONFIG_KEY, str(seconds).encode(), 0)
    return f"steal cooldown set to {seconds}s"


async def _reply_for(
    command: str, payload: dict[str, Any], *, community: str, username: str
) -> str:
    """Compute the chat reply for one recognized command."""
    if command == "usage":
        return _USAGE
    if command == "config_set_cooldown":
        if _caller_role_signal(payload) is not True:
            log.info("steal.config_denied", command=command)
            return _PERMISSION_DENIED_MSG
        arg = payload.get("arg")
        return await _handle_set_cooldown(
            arg if isinstance(arg, str) else None, community=community
        )
    if command == "list":
        return await _handle_list(community=community)
    tokens = payload.get("tokens")
    return await _handle_steal(
        [t for t in tokens if isinstance(t, str)] if isinstance(tokens, list) else [],
        community=community,
        username=username,
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
        raise ValueError("steal reply requires a channel_id from the inbound chat.message")
    command = payload.get("command")
    if command not in _KNOWN_COMMANDS:
        raise ValueError(f"unrecognized steal command: {command!r}")
    provider = envelope.event.platform
    community = envelope.community
    if not community:
        log.error("steal.missing_community", command=command)
        raise ValueError("steal requires a community context and cannot operate tenant-wide")

    username = envelope.event.actor or "someone"
    try:
        reply = await _reply_for(command, payload, community=community, username=username)
    except CommandUsageError as exc:
        reply = str(exc)
    except Exception as exc:
        log.error("steal.kv_error", error=type(getattr(exc, "value", exc)).__name__)
        await relay.push(provider, {"channel": channel_id, "text": "stealing is unavailable."})
        raise RuntimeError("steal state backend failed") from exc

    await relay.push(provider, {"channel": channel_id, "text": reply})
    log.info("steal.dispatch relayed", platform=provider, command=command)
    return DispatchResult(transport=provider, detail=command)
