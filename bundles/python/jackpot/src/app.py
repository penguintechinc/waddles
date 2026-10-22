"""`!jackpot <stake>` -> an accumulating community jackpot, economy-SDK stakes, kv pot.

Ideas/design inspired by PenguinTwitchBot by SuperPenguinTV (with permission) -- ideas/design
only, no code copied (same convention as `bundles/python/fish`).

Commands:

- `!jackpot <stake>` -- stake points. With probability `WIN_PERCENT` you win the whole pot
  (`BASE_POT` + everything accumulated) and it resets to `BASE_POT`; otherwise the stake is
  forfeited into the pot. A win also returns the stake. Per-actor cooldown applies.
- `!jackpot` -- show the current pot and how to play.

## Money and pot state

- Money moves ONLY through `waddle_sdk.economy` (`balance`/`max_bet`/`wager`); the actor is
  resolved by the host (`identity.resolve_actor`), never from the event's raw name.
- The pot lives in `community_kv` key `jackpot.pot` as the *accrued* amount above `BASE_POT`
  (displayed pot = `BASE_POT + accrued`), so "reset to base" is just "accrued -> 0" and no
  seeding write is needed. `kv.increment` is the only CAS, so every pot mutation is an
  increment, never a read-modify-write set:
  - loss: `wager(stake, 0)` (money-safe order: debit first), then `increment(+stake)`.
  - win: read accrued, claim it with `increment(-observed)`. If a concurrent winner already
    drained it the counter goes negative; the loser of that race claims only what remained
    (`observed + after`) and restores the floor with `increment(-after)`. Contributions that land
    between read and claim are never lost (they stay in the counter). Then `wager(stake,
    stake + BASE_POT + claimed)`; any refusal restores the claim, an `OverCapError` pays the
    host cap and returns the excess to the pot.
- A `BackendError` from `wager` may or may not have committed: the same call is retried once
  (host dedupes by event), after which it fails loud WITHOUT restoring the claim (never
  risk a double payout).

PII-free: kv keys/logs carry only `jackpot.pot` and `jackpot.cd.<pseudonym>` where the pseudonym is
SHA-256(community|resolved-uuid); logs carry `op`/`outcome`/`amount_bucket`/error-case only.
Gated by feature flag `waddles.command-jackpot` (default OFF), checked after the command matches.
"""

from __future__ import annotations

import hashlib
import random
from typing import Any, NoReturn

from waddle_sdk import community_kv, economy, identity, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, ParsedCommand, parse_command
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

SPEC = CommandSpec(name="jackpot")

FLAG_KEY = "waddles.command-jackpot"
BASE_POT = 100
WIN_PERCENT = 10
MIN_STAKE = 1
COOLDOWN_SECONDS = 10

_POT_KEY = "jackpot.pot"
_USAGE = "Usage: !jackpot <stake> (win the whole pot, else your stake joins it) | !jackpot"
_KNOWN_COMMANDS = frozenset({"play", "status", "usage"})


def _cooldown_key(pseudonym: str) -> str:
    """Per-(community, actor) cooldown counter key (`.`-joined; kv forbids `:`)."""
    return f"jackpot.cd.{pseudonym}"


def _pseudonym(community: str, actor_uuid: str) -> str:
    """Non-reversible per-(community, actor) key component from the host-resolved uuid."""
    return hashlib.sha256(f"{community}|{actor_uuid}".encode()).hexdigest()


def _amount_bucket(amount: int) -> str:
    """Coarse amount bucket for PII-free logs (never the exact value)."""
    for limit in (10, 100, 1000):
        if amount <= limit:
            return f"le{limit}"
    return "gt1000"


def _roll_win() -> bool:
    """Return True with probability `WIN_PERCENT`/100."""
    return random.randint(1, 100) <= WIN_PERCENT  # noqa: S311 - a game, not security


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!jackpot [stake]`, then flag-gate.

    Cheap command match first, then the feature flag (default OFF -> drop), then grammar.
    A recognized-but-malformed `!jackpot ...` replies `usage`, never a silent drop.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if stripped.partition(" ")[0].lower() != "!jackpot":
        return None
    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    tail = stripped.partition(" ")[2].strip()
    payload: dict[str, Any] = {"channel_id": event.payload.get("channel_id")}
    if tail.isascii() and tail.isdigit():
        payload["command"] = "play"
        payload["arg"] = tail
    else:
        try:
            parsed: ParsedCommand | None = parse_command(stripped, SPEC)
        except CommandUsageError:
            parsed = None
        bare = parsed is not None and parsed.option is None
        payload["command"] = "status" if bare else "usage"
    log.info("jackpot.transform matched", command=payload["command"])
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload=payload,
        occurred_at=event.occurred_at,
    )


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result (see `heist`)."""

    __slots__ = ("transport", "detail", "sub_type", "http_status")

    def __init__(self, *, transport: str, detail: str) -> None:
        """Record the provider relayed to and a short detail string."""
        self.transport = transport
        self.detail = detail
        self.sub_type = None
        self.http_status = None


class _Pot:
    """Community pot + cooldown kv access; every kv failure is fail-loud (log, reply, raise)."""

    def __init__(self, community: str, provider: str, channel_id: str) -> None:
        """Bind to one community and the reply channel used on failure."""
        self._community = community
        self._provider = provider
        self._channel_id = channel_id

    async def fail(self, exc: Exception, op: str) -> NoReturn:
        """Log (error-case name only), reply an error to chat, then raise `RuntimeError`."""
        case_name = type(getattr(exc, "value", exc)).__name__
        log.error("jackpot.error", op=op, error=case_name)
        await relay.push(
            self._provider,
            {"channel": self._channel_id, "text": "the jackpot is temporarily unavailable."},
        )
        raise RuntimeError(f"jackpot {op} failed: {case_name}") from exc

    async def accrued(self) -> int:
        """Return the accrued pot above `BASE_POT` (never negative; corrupt -> fail loud)."""
        try:
            raw = await community_kv.get(self._community, _POT_KEY)
            return max(int(raw.decode()), 0) if raw is not None else 0
        except Exception as exc:  # noqa: BLE001 - classified in `fail`
            await self.fail(exc, "get")

    async def add(self, delta: int) -> int:
        """Atomic `kv.increment` of the accrued pot; returns the new accrued value."""
        try:
            return await community_kv.increment(self._community, _POT_KEY, delta, 0)
        except Exception as exc:  # noqa: BLE001 - classified in `fail`
            await self.fail(exc, "increment")

    async def claim_all(self) -> int:
        """Atomically drain the accrued pot; return exactly the amount this caller took.

        Concurrent claimers each subtract what they observed; the counter can dip negative.
        `increment` is atomic, so `before = after + observed` is exact for THIS caller: it took
        `clamp(before, 0, observed)` and restores exactly the amount it over-subtracted.
        """
        observed = await self.accrued()
        if observed <= 0:
            return 0
        after = await self.add(-observed)
        before = after + observed  # the counter value immediately before OUR atomic decrement
        claimed = min(max(before, 0), observed)
        if claimed < observed:
            await self.add(observed - claimed)  # give back exactly what we over-subtracted
        return claimed

    async def cooldown_claim(self, pseudonym: str) -> bool:
        """Return True if the caller may play now (first increment within the cooldown TTL)."""
        try:
            count = await community_kv.increment(
                self._community, _cooldown_key(pseudonym), 1, COOLDOWN_SECONDS
            )
        except Exception as exc:  # noqa: BLE001 - classified in `fail`
            await self.fail(exc, "cooldown")
        return count == 1


def _parse_stake(arg: str | None) -> int:
    """Return a validated stake or raise `CommandUsageError` carrying the chat reply."""
    try:
        stake = int(arg or "")
    except ValueError:
        raise CommandUsageError(_USAGE) from None
    if stake < MIN_STAKE:
        raise CommandUsageError(f"stake must be at least {MIN_STAKE} point.")
    return stake


async def _economy_fail(exc: Exception, op: str, pot: _Pot) -> NoReturn:
    """Fail loud on an unexpected economy/identity error (log, reply, raise)."""
    await pot.fail(exc, op)


async def _wager(actor_uuid: str, stake: int, payout: int) -> int:
    """`economy.wager`, retrying the identical call once after an ambiguous `BackendError`."""
    try:
        return await economy.wager(actor_uuid, stake, payout)
    except economy.BackendError:
        log.error("jackpot.error", op="wager", error="BackendError", retry=True)
        return await economy.wager(actor_uuid, stake, payout)


async def _play(arg: str | None, *, pot: _Pot, community: str, username: str) -> str:
    """Validate, stake, roll, and settle one jackpot entry; returns the chat reply."""
    stake = _parse_stake(arg)
    try:
        actor_uuid = await identity.resolve_actor()
        held = await economy.balance(actor_uuid)
        if stake > held:
            raise CommandUsageError(f"{username}, you only have {held} points.")
        cap = await economy.max_bet(actor_uuid)
        if stake > cap:
            raise CommandUsageError(f"{username}, the max stake right now is {cap} points.")
    except identity.NotLinkedError:
        return f"{username}, link your account before playing the jackpot."
    except (identity.IdentityError, economy.DeniedError, economy.NotAMemberError) as exc:
        log.info("jackpot.denied", op="precheck", error=type(exc).__name__)
        return f"{username}, you can't play the jackpot right now."
    except economy.EconomyError as exc:
        await _economy_fail(exc, "precheck", pot)

    if not await pot.cooldown_claim(_pseudonym(community, actor_uuid)):
        return f"slow down, {username}! try again in a few seconds."

    if not _roll_win():
        return await _lose(actor_uuid, stake, pot=pot, username=username)
    return await _win(actor_uuid, stake, pot=pot, username=username)


async def _lose(actor_uuid: str, stake: int, *, pot: _Pot, username: str) -> str:
    """Forfeit the stake into the pot (debit first, then CAS-increment)."""
    try:
        await _wager(actor_uuid, stake, 0)
    except economy.InsufficientFundsError as exc:
        return f"{username}, you only have {exc.balance} points."
    except economy.OverCapError as exc:
        return f"{username}, the max stake right now is {exc.cap} points."
    except economy.EconomyError as exc:
        await _economy_fail(exc, "wager", pot)
    accrued = await pot.add(stake)
    log.info("jackpot.lost", outcome="loss", amount_bucket=_amount_bucket(stake))
    return (
        f"\U0001f3b0 {username} missed -- {stake} points join the pot! "
        f"Jackpot is now {BASE_POT + max(accrued, 0)} points."
    )


async def _win(actor_uuid: str, stake: int, *, pot: _Pot, username: str) -> str:
    """Claim the whole pot atomically, pay it out with the stake, restore it on refusal."""
    claimed = await pot.claim_all()
    wanted = stake + BASE_POT + claimed
    paid = wanted
    excess = 0
    try:
        try:
            await _wager(actor_uuid, stake, wanted)
        except economy.OverCapError as exc:
            if exc.cap < stake:
                raise
            paid = exc.cap
            excess = min(claimed, wanted - paid)
            await _wager(actor_uuid, stake, paid)
    except economy.BackendError as exc:
        await pot.fail(exc, "wager")  # ambiguous: do NOT restore (double-payout risk)
    except economy.EconomyError as exc:
        if claimed:
            await pot.add(claimed)
        if isinstance(exc, economy.InsufficientFundsError):
            return f"{username}, you only have {exc.balance} points."
        if isinstance(exc, economy.OverCapError):
            return f"{username}, the max stake right now is {exc.cap} points."
        await _economy_fail(exc, "wager", pot)
    if excess:
        await pot.add(excess)
    log.info("jackpot.won", outcome="win", amount_bucket=_amount_bucket(paid))
    return (
        f"\U0001f389 JACKPOT! {username} wins {paid - stake} points! "
        f"The pot resets to {BASE_POT} points."
    )


async def _status(*, pot: _Pot) -> str:
    """Show the current pot and the odds."""
    total = BASE_POT + await pot.accrued()
    return (
        f"\U0001f3b0 Jackpot: {total} points. {WIN_PERCENT}% chance to win it all -- "
        "miss and your stake joins the pot. Play with !jackpot <stake>."
    )


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: economy + kv state, then relay the reply.

    Raises:
        ValueError: No `channel_id`, no `community`, or an unrecognized command.
        RuntimeError: A kv/economy backend call failed (error reply + ERROR log emitted first).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("jackpot reply requires a channel_id from the inbound chat.message")
    command = payload.get("command")
    if command not in _KNOWN_COMMANDS:
        raise ValueError(f"unrecognized jackpot command: {command!r}")
    provider = envelope.event.platform
    community = envelope.community
    if not community:
        log.error("jackpot.missing_community", command=command)
        raise ValueError("jackpot requires a community context and cannot operate tenant-wide")

    username = envelope.event.actor or "someone"
    pot = _Pot(community, provider, channel_id)
    arg = payload.get("arg")
    arg_text = arg if isinstance(arg, str) else None

    if command == "usage":
        reply = _USAGE
    elif command == "status":
        reply = await _status(pot=pot)
    else:
        try:
            reply = await _play(arg_text, pot=pot, community=community, username=username)
        except CommandUsageError as exc:
            reply = str(exc)

    await relay.push(provider, {"channel": channel_id, "text": reply})
    log.info("jackpot.dispatch relayed", platform=provider, command=command)
    return DispatchResult(transport=provider, detail=command)
