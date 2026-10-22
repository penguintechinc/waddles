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

Replay safety (at-most-once): the pipeline is at-least-once, and a redelivered loss would credit
the pot a second time (the host dedupes the `wager` per event, the kv pot credit is ours). The
inbound `message_id` (forwarded by `transform`) is recorded via `waddle_sdk.event_dedupe` AFTER the
atomic per-actor cooldown and BEFORE any money or pot mutation; a redelivery is skipped silently
(no wager, no pot credit, no reply). A crash between the claim and the pot credit therefore loses
one stake into the void (never a double credit); a payload with no `message_id` fails loud.

PII-free: kv keys/logs carry only `jackpot.pot`, `jackpot.cd.<pseudonym>` and
`dedupe.jackpot.<pseudonym>` where the pseudonym is SHA-256(community|resolved-uuid) (the dedupe
ring stores truncated hashes of the platform message id, never the id); logs carry
`op`/`outcome`/`amount_bucket`/error-case only.
Activation-gated only -- no feature flag: the platform's installed -> enabled -> activated check
decides whether this bundle runs.

Output safety: `event.actor` is interpolated into most replies. The host-minted
`{user:<token>}` actor stays live (egress renders the display name); any other value is
untrusted and goes through `waddle_sdk.safe_text.sanitize_user_text` (see `_actor_label`),
as does every other typed echo.
"""

from __future__ import annotations

import hashlib
import random
import re
from typing import Any, NoReturn

from waddle_sdk import community_kv, economy, event_dedupe, identity, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, ParsedCommand, parse_command
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.safe_text import sanitize_user_text

SPEC = CommandSpec(name="jackpot")

BASE_POT = 100
WIN_PERCENT = 10
MIN_STAKE = 1
COOLDOWN_SECONDS = 10

_POT_KEY = "jackpot.pot"
_DEDUPE_NAMESPACE = "jackpot"
_USAGE = "Usage: !jackpot <stake> (win the whole pot, else your stake joins it) | !jackpot"
_KNOWN_COMMANDS = frozenset({"play", "status", "usage"})


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
    """Implement `process-stage.transform`: recognize `!jackpot [stake]`.

    Cheap command match first, then grammar.
    A recognized-but-malformed `!jackpot ...` replies `usage`, never a silent drop.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if stripped.partition(" ")[0].lower() != "!jackpot":
        return None

    tail = stripped.partition(" ")[2].strip()
    payload: dict[str, Any] = {"channel_id": event.payload.get("channel_id")}
    message_id = event.payload.get("message_id")
    if isinstance(message_id, str):  # replay-dedupe key; `dispatch` refuses a play without it
        payload["message_id"] = message_id
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

    async def event_id(self, payload: dict[str, Any], platform: str) -> str:
        """Return the replay-stable event id, or fail loud (log, reply, raise) if there is none."""
        try:
            return event_dedupe.event_id_from_payload(payload, platform=platform)
        except event_dedupe.EventIdError as exc:
            await self.fail(exc, "event_id")

    async def seen(self, pseudonym: str, event_id: str) -> bool:
        """Read-only: True if this actor already claimed this event (a redelivery)."""
        try:
            return await event_dedupe.already_seen(
                self._community, namespace=_DEDUPE_NAMESPACE, subject=pseudonym, event_id=event_id
            )
        except Exception as exc:  # noqa: BLE001 - classified in `fail`
            await self.fail(exc, "dedupe")

    async def claim_event(self, pseudonym: str, event_id: str) -> bool:
        """Record the event BEFORE any mutation; False means a duplicate that must be skipped."""
        try:
            return await event_dedupe.claim(
                self._community, namespace=_DEDUPE_NAMESPACE, subject=pseudonym, event_id=event_id
            )
        except Exception as exc:  # noqa: BLE001 - classified in `fail`
            await self.fail(exc, "dedupe")

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


async def _play(
    arg: str | None,
    *,
    pot: _Pot,
    community: str,
    username: str,
    payload: dict[str, Any],
    platform: str,
) -> str | None:
    """Validate, stake, roll, and settle one jackpot entry; returns the chat reply.

    Returns `None` for a redelivered event (already claimed): nothing is mutated or sent.
    """
    stake = _parse_stake(arg)
    try:
        actor_uuid = await identity.resolve_actor()
        event_id = await pot.event_id(payload, platform)
        pseudonym = _pseudonym(community, actor_uuid)
        if await pot.seen(pseudonym, event_id):
            log.info("jackpot.redelivery_skipped", op="seen")
            return None
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

    if not await pot.cooldown_claim(pseudonym):
        return f"slow down, {username}! try again in a few seconds."
    if not await pot.claim_event(pseudonym, event_id):
        log.info("jackpot.redelivery_skipped", op="claim")
        return None

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

    username = _actor_label(envelope.event.actor)
    pot = _Pot(community, provider, channel_id)
    arg = payload.get("arg")
    arg_text = arg if isinstance(arg, str) else None
    reply: str | None

    if command == "usage":
        reply = _USAGE
    elif command == "status":
        reply = await _status(pot=pot)
    else:
        try:
            reply = await _play(
                arg_text,
                pot=pot,
                community=community,
                username=username,
                payload=payload,
                platform=provider,
            )
        except CommandUsageError as exc:
            reply = str(exc)

    if reply is None:
        log.info("jackpot.dispatch skipped duplicate", platform=provider, command=command)
        return DispatchResult(transport=provider, detail="duplicate")
    await relay.push(provider, {"channel": channel_id, "text": reply})
    log.info("jackpot.dispatch relayed", platform=provider, command=command)
    return DispatchResult(transport=provider, detail=command)
