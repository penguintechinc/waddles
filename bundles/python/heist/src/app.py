"""`!heist <amount>` -> a timed group gamble, community-scoped, kv-only.

Concept inspired by superpenguintv (Psychoboy)'s PenguinTwitchBot heist
(https://github.com/Psychoboy/PenguinTwitchBot): viewers join a shared pool,
then one collective roll decides whether the whole crew scores or busts. This
is an original Waddles implementation -- no PenguinTwitchBot source or text is
reused (same convention as `bundles/python/fish`).

Commands:

- `!heist <amount>` -- stake points and join (or open) the community's heist.
  The stake is debited immediately. One stake per player per heist.
- `!heist` -- show the open heist (crew size, pot, time left) or how to start one.
- `!heist set window <seconds>` -- broadcaster/moderator-only join-window
  override (fail-closed `_caller_role_signal`).

## Resolution is lazy (no timers)

Bundles are event-driven and have no scheduler. A heist therefore resolves on
the **first `!heist` command seen after its join window expires** (the
resolving command's reply carries the result; if it was a join attempt the
caller is then told to start a fresh heist). An idle channel with an expired
heist simply holds it until someone next types `!heist`. Exactly-once
resolution is guaranteed by an atomic `kv.increment` claim on
`heist.claim.<heist_id>` -- only the caller that sees `1` pays out.

Success chance = `BASE_SUCCESS_PERCENT` + `PER_PLAYER_BONUS_PERCENT` per extra
crew member, capped at `MAX_SUCCESS_PERCENT`. Success pays each player
`stake * PAYOUT_MULTIPLIER_X10 / 10` (stake already debited, so net profit is
0.5x); a bust forfeits all stakes. The crew roster is one JSON kv value
(read-modify-write, capped at `MAX_CREW`); two joins landing in the same
instant can lose one roster entry -- the lost joiner's stake is refunded by the
post-write verification in `_join`.

## Points-store integration (read this)

Identical situation to `bundles/python/gamble`: `loyalty`'s balances are a
`db` table + kv index private to that app (host-scoped per app_id), and this
bundle holds only `storage.kv` + `flags.read`, so it cannot touch them. Balances
live behind the narrow `_Ledger` seam (community-scoped, pseudonymous kv; new
players seeded with `STARTING_BALANCE`) so a future platform points capability
replaces only that class. Flagged in the PR description.

PII-free logging: fields are `op`/`outcome`/`amount_bucket`/`crew_size`/error
case only -- never actor, username or message text. Pseudonyms (SHA-256) are
the only identity persisted. Gated by PostHog flag ``waddles.command-heist``.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from typing import Any, NoReturn

from waddle_sdk import clock, community_kv, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, ParsedCommand, parse_command
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-heist"
SPEC = CommandSpec(name="heist")

STARTING_BALANCE = 100
DEFAULT_WINDOW_SECONDS = 120
MIN_WINDOW_SECONDS = 30
MAX_WINDOW_SECONDS = 600
MAX_CREW = 200
BASE_SUCCESS_PERCENT = 35
PER_PLAYER_BONUS_PERCENT = 5
MAX_SUCCESS_PERCENT = 75
PAYOUT_MULTIPLIER_X10 = 15  # stake * 1.5 returned on success

_STATE_KEY = "heist.state"
_WINDOW_CONFIG_KEY = "heist.config.window"

_USAGE = "Usage: !heist <amount> | !heist | !heist set window <seconds> (set is admin/mod only)"
_PERMISSION_DENIED_MSG = "only moderators/broadcasters can configure !heist"
_KNOWN_COMMANDS = frozenset({"join", "status", "config_set_window", "usage"})


@dataclass(slots=True)
class Heist:
    """The open heist: start time (ms, doubles as id), window, and pseudonym -> stake."""

    started_ms: int
    window_s: int
    crew: dict[str, int]

    def ends_ms(self) -> int:
        """Epoch-ms at which the join window closes."""
        return self.started_ms + self.window_s * 1000

    def encode(self) -> bytes:
        """Serialize for kv."""
        return json.dumps(
            {"started_ms": self.started_ms, "window_s": self.window_s, "crew": self.crew}
        ).encode()

    @classmethod
    def decode(cls, raw: bytes) -> Heist:
        """Parse a kv value; raises `ValueError`/`KeyError`/`TypeError` if corrupt."""
        data = json.loads(raw.decode())
        crew = {str(k): int(v) for k, v in data["crew"].items()}
        return cls(int(data["started_ms"]), int(data["window_s"]), crew)


def _pseudonym(actor: str | None) -> str:
    """Non-reversible per-caller key component (see `slots`/`fish` `_pseudonym`)."""
    return hashlib.sha256((actor or "anonymous").encode()).hexdigest()


def _balance_key(pseudonym: str) -> str:
    """Per-(community, caller) points balance key."""
    return f"heist.points.{pseudonym}"


def _claim_key(heist_id: int) -> str:
    """Exactly-once resolution claim counter key."""
    return f"heist.claim.{heist_id}"


def _amount_bucket(amount: int) -> str:
    """Coarse amount bucket for PII-free logs (never the exact value)."""
    for limit in (10, 100, 1000):
        if amount <= limit:
            return f"le{limit}"
    return "gt1000"


def _success_percent(crew_size: int) -> int:
    """Collective success chance for a crew of `crew_size`."""
    pct = BASE_SUCCESS_PERCENT + PER_PLAYER_BONUS_PERCENT * max(crew_size - 1, 0)
    return min(pct, MAX_SUCCESS_PERCENT)


def _roll(percent: int) -> bool:
    """Return True with probability `percent`/100."""
    return random.randint(1, 100) <= percent  # noqa: S311 - a game, not security


def _caller_role_signal(payload: dict[str, Any]) -> bool | None:
    """`True`/`False` from badge fields, or `None` if absent (absent must deny)."""
    if "is_mod" not in payload and "is_broadcaster" not in payload:
        return None
    return bool(payload.get("is_mod")) or bool(payload.get("is_broadcaster"))


def _resolve_command(parsed: ParsedCommand | None) -> str:
    """Map a parsed grammar result onto this bundle's command set (else `usage`)."""
    if parsed is None:
        return "usage"
    if parsed.option is None:
        return "status"
    if parsed.option == "set":
        return "config_set_window"
    return "usage"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!heist`, `<amount>` and `set window`.

    Cheap command-match first, flag second, grammar last. A recognized-but-malformed
    `!heist ...` replies `usage`, never a silent drop.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if stripped.partition(" ")[0].lower() != "!heist":
        return None
    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    tail = stripped.partition(" ")[2].strip()
    payload: dict[str, Any] = {"channel_id": event.payload.get("channel_id")}
    if tail.isascii() and tail.isdigit():
        payload["command"] = "join"
        payload["arg"] = tail
    else:
        try:
            parsed: ParsedCommand | None = parse_command(stripped, SPEC)
        except CommandUsageError:
            parsed = None
        payload["command"] = _resolve_command(parsed)
        if payload["command"] == "config_set_window" and parsed is not None:
            payload["arg"] = parsed.args
    log.info("heist.transform matched", command=payload["command"])
    if "is_mod" in event.payload:
        payload["is_mod"] = bool(event.payload["is_mod"])
    if "is_broadcaster" in event.payload:
        payload["is_broadcaster"] = bool(event.payload["is_broadcaster"])
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload=payload,
        occurred_at=event.occurred_at,
    )


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result (see `slots`)."""

    __slots__ = ("transport", "detail", "sub_type", "http_status")

    def __init__(self, *, transport: str, detail: str) -> None:
        """Record the provider relayed to and a short detail string."""
        self.transport = transport
        self.detail = detail
        self.sub_type = None
        self.http_status = None


class _Ledger:
    """The points-store seam plus heist-state kv access (see module docstring).

    Every kv failure is fail-loud: ERROR log (error-case name only), chat error reply, re-raise.
    """

    def __init__(self, community: str, provider: str, channel_id: str) -> None:
        """Bind the ledger to one community and the reply channel used on failure."""
        self._community = community
        self._provider = provider
        self._channel_id = channel_id

    async def fail(self, exc: Exception, op: str) -> NoReturn:
        """Log, reply an error to chat, then re-raise as `RuntimeError`."""
        case_name = type(getattr(exc, "value", exc)).__name__
        log.error("heist.kv_error", op=op, error=case_name)
        await relay.push(
            self._provider,
            {"channel": self._channel_id, "text": "heists are temporarily unavailable."},
        )
        raise RuntimeError(f"heist kv {op} failed: {case_name}") from exc

    async def get(self, key: str) -> bytes | None:
        """Fail-loud `community_kv.get`."""
        try:
            return await community_kv.get(self._community, key)
        except Exception as exc:  # noqa: BLE001 - classified in `fail`
            await self.fail(exc, "get")

    async def set(self, key: str, value: bytes) -> None:
        """Fail-loud durable `community_kv.set`."""
        try:
            await community_kv.set(self._community, key, value, 0)
        except Exception as exc:  # noqa: BLE001 - classified in `fail`
            await self.fail(exc, "set")

    async def delete(self, key: str) -> None:
        """Fail-loud `community_kv.delete`."""
        try:
            await community_kv.delete(self._community, key)
        except Exception as exc:  # noqa: BLE001 - classified in `fail`
            await self.fail(exc, "delete")

    async def increment(self, key: str, delta: int) -> int:
        """Fail-loud durable `community_kv.increment`."""
        try:
            return await community_kv.increment(self._community, key, delta, 0)
        except Exception as exc:  # noqa: BLE001 - classified in `fail`
            await self.fail(exc, "increment")

    async def balance(self, pseudonym: str) -> int:
        """Return the balance, seeding `STARTING_BALANCE` for a first-time player."""
        raw = await self.get(_balance_key(pseudonym))
        if raw is None:
            return await self.increment(_balance_key(pseudonym), STARTING_BALANCE)
        try:
            return int(raw.decode())
        except (UnicodeDecodeError, ValueError):
            await self.fail(ValueError("corrupt balance"), "balance")

    async def credit(self, pseudonym: str, amount: int) -> int:
        """Add `amount` points; return the new balance."""
        return await self.increment(_balance_key(pseudonym), amount)

    async def debit(self, pseudonym: str, amount: int) -> int:
        """Remove `amount` points, flooring at zero if a concurrent debit overdrew."""
        new = await self.increment(_balance_key(pseudonym), -amount)
        if new < 0:
            new = await self.increment(_balance_key(pseudonym), -new)
        return new

    async def load_heist(self) -> Heist | None:
        """Return the open heist, or `None` (corrupt state is logged and discarded)."""
        raw = await self.get(_STATE_KEY)
        if raw is None:
            return None
        try:
            return Heist.decode(raw)
        except (UnicodeDecodeError, ValueError, KeyError, TypeError, AttributeError):
            log.error("heist.state_corrupt")
            await self.delete(_STATE_KEY)
            return None

    async def window_seconds(self) -> int:
        """Return the configured join window, or the default if unset/corrupt."""
        raw = await self.get(_WINDOW_CONFIG_KEY)
        if raw is None:
            return DEFAULT_WINDOW_SECONDS
        try:
            value = int(raw.decode())
        except (UnicodeDecodeError, ValueError):
            log.error("heist.window_config_corrupt")
            return DEFAULT_WINDOW_SECONDS
        in_range = MIN_WINDOW_SECONDS <= value <= MAX_WINDOW_SECONDS
        return value if in_range else DEFAULT_WINDOW_SECONDS


async def _resolve_heist(ledger: _Ledger, heist: Heist) -> str | None:
    """Roll and settle an expired heist exactly once; `None` if another caller claimed it."""
    if await ledger.increment(_claim_key(heist.started_ms), 1) != 1:
        return None
    await ledger.delete(_STATE_KEY)
    crew_size = len(heist.crew)
    success = _roll(_success_percent(crew_size))
    pot = sum(heist.crew.values())
    if success:
        for pseudonym, stake in heist.crew.items():
            await ledger.credit(pseudonym, stake * PAYOUT_MULTIPLIER_X10 // 10)
    log.info(
        "heist.resolved",
        outcome="success" if success else "bust",
        crew_size=crew_size,
        amount_bucket=_amount_bucket(pot),
    )
    if success:
        return (
            f"\U0001f4b0 The heist SUCCEEDED! {crew_size} crew member(s) split the haul -- "
            f"everyone gets 1.5x their stake back (pot was {pot})."
        )
    return (
        f"\U0001f6a8 The heist BUSTED! {crew_size} crew member(s) were caught and lost "
        f"{pot} points between them."
    )


async def _join(arg: str | None, *, ledger: _Ledger, actor: str | None, username: str) -> str:
    """Resolve any expired heist, then stake into (or open) the current one."""
    try:
        amount = int(arg or "")
    except ValueError:
        log.debug("heist.join_amount_invalid", op="join", error="ValueError")
        return _USAGE
    if amount <= 0:
        return "stake must be at least 1 point."

    now_ms = clock.now_millis()
    prefix = ""
    heist = await ledger.load_heist()
    if heist is not None and now_ms >= heist.ends_ms():
        outcome = await _resolve_heist(ledger, heist)
        prefix = (outcome + " Start a new one with !heist <amount>.") if outcome else ""
        return prefix or "that heist just wrapped up -- try again."

    pseudonym = _pseudonym(actor)
    if heist is not None and pseudonym in heist.crew:
        return f"{username}, you're already in this heist."
    if heist is not None and len(heist.crew) >= MAX_CREW:
        return "this heist crew is full."

    balance = await ledger.balance(pseudonym)
    if amount > balance:
        return f"{username}, you only have {balance} points."

    await ledger.debit(pseudonym, amount)
    opened = heist is None
    if heist is None:
        heist = Heist(now_ms, await ledger.window_seconds(), {})
    heist.crew[pseudonym] = amount
    await ledger.set(_STATE_KEY, heist.encode())

    verify = await ledger.load_heist()
    if verify is None or pseudonym not in verify.crew:
        await ledger.credit(pseudonym, amount)
        log.info("heist.join_race_refunded", amount_bucket=_amount_bucket(amount))
        return f"{username}, the crew roster shifted -- you were refunded, please retry."

    log.info(
        "heist.joined",
        opened=opened,
        crew_size=len(verify.crew),
        amount_bucket=_amount_bucket(amount),
    )
    remaining = max(1, round((verify.ends_ms() - now_ms) / 1000))
    lead = "started a heist" if opened else "joined the heist"
    return (
        f"\U0001f9b9 {username} {lead} with {amount} points! Crew: {len(verify.crew)} "
        f"(success chance {_success_percent(len(verify.crew))}%). Resolves in ~{remaining}s."
    )


async def _status(*, ledger: _Ledger) -> str:
    """Show the open heist, resolving it first if its window has expired."""
    heist = await ledger.load_heist()
    if heist is None:
        return "No heist is running. Start one with !heist <amount>."
    now_ms = clock.now_millis()
    if now_ms >= heist.ends_ms():
        outcome = await _resolve_heist(ledger, heist)
        return outcome or "that heist just wrapped up."
    remaining = max(1, round((heist.ends_ms() - now_ms) / 1000))
    return (
        f"\U0001f9b9 Heist in progress: {len(heist.crew)} crew, pot {sum(heist.crew.values())}, "
        f"success chance {_success_percent(len(heist.crew))}%, ~{remaining}s left. "
        "Join with !heist <amount>."
    )


async def _handle_set_window(arg: str | None, *, ledger: _Ledger) -> str:
    """Parse+apply `set window <seconds>`'s free-text tail."""
    parts = (arg or "").split()
    if len(parts) != 2 or parts[0].lower() != "window":
        return _USAGE
    try:
        seconds = int(parts[1])
    except ValueError:
        log.debug("heist.window_seconds_invalid", op="window", error="ValueError")
        return f"'{parts[1]}' isn't a whole number of seconds"
    if not (MIN_WINDOW_SECONDS <= seconds <= MAX_WINDOW_SECONDS):
        return f"window must be between {MIN_WINDOW_SECONDS} and {MAX_WINDOW_SECONDS} seconds"
    await ledger.set(_WINDOW_CONFIG_KEY, str(seconds).encode())
    return f"heist join window set to {seconds}s"


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: all kv state, then relay the reply.

    Raises:
        ValueError: No `channel_id`, no `community`, or an unrecognized command.
        RuntimeError: A kv backend call failed (error reply + ERROR log emitted first).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("heist reply requires a channel_id from the inbound chat.message")
    command = payload.get("command")
    if command not in _KNOWN_COMMANDS:
        raise ValueError(f"unrecognized heist command: {command!r}")
    provider = envelope.event.platform
    community = envelope.community
    if not community:
        log.error("heist.missing_community", command=command)
        raise ValueError("heist requires a community context and cannot operate tenant-wide")

    username = envelope.event.actor or "someone"
    ledger = _Ledger(community, provider, channel_id)
    arg = payload.get("arg")
    arg_text = arg if isinstance(arg, str) else None

    if command == "usage":
        reply = _USAGE
    elif command == "config_set_window":
        if _caller_role_signal(payload) is not True:
            log.info("heist.config_denied", command=command)
            reply = _PERMISSION_DENIED_MSG
        else:
            reply = await _handle_set_window(arg_text, ledger=ledger)
    elif command == "status":
        reply = await _status(ledger=ledger)
    else:
        reply = await _join(arg_text, ledger=ledger, actor=envelope.event.actor, username=username)

    await relay.push(provider, {"channel": channel_id, "text": reply})
    log.info("heist.dispatch relayed", platform=provider, command=command)
    return DispatchResult(transport=provider, detail=command)
