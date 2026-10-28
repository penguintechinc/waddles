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
inbound `message_id` (forwarded by `transform`) is claimed via `waddle_sdk.event_dedupe` -- ONE
atomic `kv.increment` on a per-event, TTL'd marker (`1` = first delivery) -- AFTER the atomic
per-actor cooldown and BEFORE any money or pot mutation; a redelivery is skipped silently (no
wager, no pot credit, no reply). The increment is race-free (two concurrent copies cannot both see
`1`) and has no 16-event horizon; the marker's TTL is closed by an age fence (an event ingested
more than `event_dedupe.MAX_EVENT_AGE_SECONDS` ago is dropped, never processed), so a redelivery is
deduped or dropped, never replayed. A crash between the claim and the pot credit therefore loses
one stake into the void (never a double credit); a payload with no `message_id`, or no valid
`occurred_at`, fails loud. The claim comes after the cooldown, so one chatter's marker rate is
bounded by its own cooldown; a full 10,000-key quota fails CLOSED (the play is refused) and heals
as markers lapse -- the residual is a swarm of funded accounts, bounded properly only by a host
per-actor kv rate limit (follow-up).

PII-free: kv keys/logs carry only `jackpot.pot`, `jackpot.cd.<pseudonym>` and
`jackpot.seen.<digest>` where the pseudonym is SHA-256(community|resolved-uuid) and the digest is a
truncated SHA-256 of the platform-prefixed message id (never the id); logs carry
`op`/`outcome`/`amount_bucket`/error-case only.
Stage split: the host serves `economy` and `identity` to the PROCESS stage only (`svc_process`;
`svc_action` has no handler for them), and `relay` to the ACTION stage only. So the whole play --
identity resolution, funds checks, cooldown, replay claim, roll, pot accounting, wager -- runs in
`transform`, and `dispatch` only relays the finished reply. The process stage cannot relay an
"unavailable" notice, so a backend failure travels as a `failure` marker: `dispatch` relays the
reply, then raises. If `dispatch` fails after `transform` settled the money, the redelivery is
deduped by the replay claim (the reply may be lost, the money is never moved twice).

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
from waddle_sdk.flask_core import get_bundle_context
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.safe_text import sanitize_user_text

#: OS-entropy RNG (`os.urandom`, i.e. `wasi:random` in the component). Never the module-level
#: `random.*` functions: their Mersenne Twister is seeded at import, and componentize-py freezes
#: import-time state into the build snapshot, so every instance would replay one stream.
_RNG = random.SystemRandom()

SPEC = CommandSpec(name="jackpot")

BASE_POT = 100
WIN_PERCENT = 10
MIN_STAKE = 1
COOLDOWN_SECONDS = 10

_POT_KEY = "jackpot.pot"
_DEDUPE_NAMESPACE = "jackpot"
_USAGE = "Usage: !jackpot <stake> (win the whole pot, else your stake joins it) | !jackpot"
_KNOWN_COMMANDS = frozenset({"play", "status", "usage"})
_UNAVAILABLE_REPLY = "the jackpot is temporarily unavailable."


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
    return _RNG.randint(1, 100) <= WIN_PERCENT


def _recognize(event: PlatformEvent) -> dict[str, Any] | None:
    """Pure routing: map a chat line onto the command intent `_settle` runs, or `None`.

    Cheap command match first, then grammar. A recognized-but-malformed `!jackpot ...` becomes a
    `usage` intent, never a silent drop. The intent carries `command`, the stake, the replay-dedupe
    `message_id` and the reply `channel_id`.
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
    if isinstance(message_id, str):  # replay-dedupe key; `_settle` refuses a play without it
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
    return payload


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!jackpot [stake]` and play the command.

    The economy/identity host capabilities are served by the PROCESS stage only (`svc_process`;
    `svc_action` has no handler), so every wager, balance read and identity resolution happens
    here. The returned event carries just the reply text for `dispatch` to relay; `None` for a
    non-matching line and for a redelivered/too-old event (nothing is mutated or sent).
    """
    intent = _recognize(event)
    if intent is None:
        return None
    return await _settle(intent, event, community=get_bundle_context().community)


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result (see `heist`)."""

    __slots__ = ("transport", "detail", "sub_type", "http_status")

    def __init__(self, *, transport: str, detail: str) -> None:
        """Record the provider relayed to and a short detail string."""
        self.transport = transport
        self.detail = detail
        self.sub_type = None
        self.http_status = None


class _UnavailableError(RuntimeError):
    """A kv/economy backend failure (already logged) to answer with the "unavailable" reply.

    Raised inside the process stage, which cannot relay: `_settle` turns it into a reply event
    carrying a `failure` marker, and `dispatch` relays the reply and then raises.
    """


class _Pot:
    """Community pot + cooldown kv access; every kv failure is fail-loud (log, mark, raise)."""

    def __init__(self, community: str) -> None:
        """Bind to one community."""
        self._community = community

    async def fail(self, exc: Exception, op: str) -> NoReturn:
        """Log (error-case name only), then raise `_UnavailableError` for `_settle` to answer."""
        case_name = type(getattr(exc, "value", exc)).__name__
        log.error("jackpot.error", op=op, error=case_name)
        raise _UnavailableError(f"jackpot {op} failed: {case_name}") from exc

    async def event_id(
        self, payload: dict[str, Any], platform: str, occurred_at: str
    ) -> str | None:
        """Return the replay-stable event id, or `None` if the event is too old to process.

        Fails loud (log, reply, raise) when there is no usable id or ingest stamp: an event whose
        identity or age cannot be established is never processed unguarded.
        """
        try:
            event_id = event_dedupe.event_id_from_payload(payload, platform=platform)
            fresh = event_dedupe.is_fresh(occurred_at)
        except event_dedupe.EventIdError as exc:
            await self.fail(exc, "event_id")
        return event_id if fresh else None

    async def seen(self, event_id: str) -> bool:
        """Read-only, advisory: True if this event was already claimed (a redelivery)."""
        try:
            return await event_dedupe.already_seen(
                self._community, namespace=_DEDUPE_NAMESPACE, event_id=event_id
            )
        except Exception as exc:  # noqa: BLE001 - classified in `fail`
            await self.fail(exc, "dedupe")

    async def claim_event(self, event_id: str) -> bool:
        """Atomically claim the event BEFORE any mutation; False means a duplicate to skip."""
        try:
            return await event_dedupe.claim(
                self._community, namespace=_DEDUPE_NAMESPACE, event_id=event_id
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
    occurred_at: str,
) -> str | None:
    """Validate, stake, roll, and settle one jackpot entry; returns the chat reply.

    Returns `None` for a redelivered or too-old event: nothing is mutated or sent.
    """
    stake = _parse_stake(arg)
    try:
        actor_uuid = await identity.resolve_actor()
        event_id = await pot.event_id(payload, platform, occurred_at)
        if event_id is None:
            log.info("jackpot.stale_event_skipped", op="fence")
            return None
        pseudonym = _pseudonym(community, actor_uuid)
        if await pot.seen(event_id):
            log.info("jackpot.redelivery_skipped", op="seen")
            return None
        held = await economy.balance(actor_uuid)
        if stake > held:
            raise CommandUsageError(f"{username}, you only have {held} points.")
        cap = await economy.max_bet(actor_uuid)
        if stake > cap:
            raise CommandUsageError(f"{username}, the max stake right now is {cap} points.")
    except identity.NotLinkedError:
        log.info("jackpot.denied", op="precheck", error="NotLinkedError")
        return f"{username}, link your account before playing the jackpot."
    except (identity.IdentityError, economy.DeniedError, economy.NotAMemberError) as exc:
        log.info("jackpot.denied", op="precheck", error=type(exc).__name__)
        return f"{username}, you can't play the jackpot right now."
    except economy.EconomyError as exc:
        await _economy_fail(exc, "precheck", pot)

    if not await pot.cooldown_claim(pseudonym):
        return f"slow down, {username}! try again in a few seconds."
    if not await pot.claim_event(event_id):
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
        log.info("jackpot.denied", op="wager", error=type(exc).__name__)
        return f"{username}, you only have {exc.balance} points."
    except economy.OverCapError as exc:
        log.info("jackpot.denied", op="wager", error=type(exc).__name__)
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


async def _settle(
    intent: dict[str, Any], event: PlatformEvent, *, community: str | None
) -> PlatformEvent | None:
    """Run one recognized command (process stage) and return the reply event, or `None` to drop.

    Raises:
        ValueError: No `channel_id`, no `community`, or an unrecognized command.

    A kv/economy backend failure does NOT raise here (the process stage cannot relay): it is
    logged, and the returned event carries the "unavailable" reply plus a `failure` message that
    `dispatch` relays and then raises, exactly as one invocation did before the economy calls
    moved stages.
    """
    channel_id = intent.get("channel_id")
    if not channel_id:
        raise ValueError("jackpot reply requires a channel_id from the inbound chat.message")
    command = intent.get("command")
    if command not in _KNOWN_COMMANDS:
        raise ValueError(f"unrecognized jackpot command: {command!r}")
    provider = event.platform
    if not community:
        log.error("jackpot.missing_community", command=command)
        raise ValueError("jackpot requires a community context and cannot operate tenant-wide")

    username = _actor_label(event.actor)
    pot = _Pot(community)
    arg = intent.get("arg")
    arg_text = arg if isinstance(arg, str) else None
    reply: str | None
    failure: str | None = None

    try:
        if command == "usage":
            reply = _USAGE
        elif command == "status":
            reply = await _status(pot=pot)
        else:
            reply = await _play(
                arg_text,
                pot=pot,
                community=community,
                username=username,
                payload=intent,
                platform=provider,
                occurred_at=event.occurred_at,
            )
    except CommandUsageError as exc:
        reply = str(exc)
    except _UnavailableError as exc:
        reply, failure = _UNAVAILABLE_REPLY, str(exc)

    if reply is None:
        log.info("jackpot.transform skipped duplicate", platform=provider, command=command)
        return None
    out: dict[str, Any] = {"channel_id": channel_id, "text": reply, "command": command}
    if failure is not None:
        out["failure"] = failure
    return PlatformEvent(
        platform=provider,
        event_type=event.event_type,
        actor=event.actor,
        payload=out,
        occurred_at=event.occurred_at,
    )


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: relay the reply `transform()` already built.

    No economy or identity call is made here -- `svc_action` does not serve them.

    Raises:
        ValueError: No `channel_id`, or no reply text, in the payload.
        RuntimeError: `transform` hit a backend failure (the "unavailable" reply is relayed first).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("jackpot reply requires a channel_id from the inbound chat.message")
    text = payload.get("text")
    if not isinstance(text, str) or not text:
        raise ValueError("jackpot reply requires non-empty text")
    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    failure = payload.get("failure")
    if isinstance(failure, str) and failure:
        log.error("jackpot.dispatch relayed failure", platform=provider)
        raise RuntimeError(failure)
    command = payload.get("command")
    log.info("jackpot.dispatch relayed", platform=provider)
    detail = command if isinstance(command, str) else "relayed"
    return DispatchResult(transport=provider, detail=detail)
