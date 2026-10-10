"""`!gamble <amount>` -> a single-user points bet, community-scoped, kv-only.

Net-new Waddles content (no inspiration source, no attribution required --
same as `bundles/python/slots`). Points only; no real-money/currency ties.

Commands (grammar via `waddle_sdk.command`, except the `<amount>` shape which
is not a declared verb and so is recognized here before the parser runs):

- `!gamble <amount>` / `!gamble all` -- bet points; win with probability
  `odds`% (admin-configurable, default 45) for +amount, otherwise lose amount.
- `!gamble` -- show the caller's balance.
- `!gamble set odds <1-95>` -- broadcaster/moderator-only win-percentage
  override (fail-closed `_caller_role_signal`, same as `slots`/`fish`).

## Points-store integration (read this)

`loyalty`'s `!points` ledger is a `db` table owned by that app, plus an
app-private `kv` index. The host scopes BOTH per `(tenant, community,
app_id)` (`core/bundle_host_kv/src/scope.rs`, `waddle_sdk/db.py`), and this
bundle is granted only `storage.kv` + `flags.read`, so it cannot read or write
`loyalty`'s balances directly -- there is no cross-bundle points capability on
the platform today. Rather than fake one, this bundle keeps the balance behind
the narrow `_Ledger` seam below (`balance`/`credit`/`debit` over community-
scoped kv, pseudonymous keys, new players seeded with `STARTING_BALANCE`).
When a platform points capability lands, only `_Ledger` changes. This gap is
called out in the PR description.

Overdraw safety: `debit` applies an atomic `kv.increment` and, if a concurrent
bet pushed the balance negative, restores it to zero rather than leaving debt.

PII-free logging: log fields are `op`/`outcome`/`amount_bucket`/`community`
only -- never the actor, username, or message text. The username appears only
in the visible chat reply, never persisted or logged.

Gated behind the PostHog flag ``waddles.command-gamble``.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from typing import Any, NoReturn

from waddle_sdk import clock, community_kv, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, ParsedCommand, parse_command
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-gamble"
SPEC = CommandSpec(name="gamble")

STARTING_BALANCE = 100
DEFAULT_ODDS_PERCENT = 45
MIN_ODDS_PERCENT = 1
MAX_ODDS_PERCENT = 95
DEFAULT_COOLDOWN_SECONDS = 10

_ODDS_CONFIG_KEY = "gamble.config.odds"

_USAGE = "Usage: !gamble <amount|all> | !gamble | !gamble set odds <1-95> (set is admin/mod only)"
_PERMISSION_DENIED_MSG = "only moderators/broadcasters can configure !gamble"
_KNOWN_COMMANDS = frozenset({"bet", "balance", "config_set_odds", "usage"})


def _pseudonym(actor: str | None) -> str:
    """Non-reversible per-caller key component (see `slots`/`fish` `_pseudonym`)."""
    return hashlib.sha256((actor or "anonymous").encode()).hexdigest()


def _balance_key(pseudonym: str) -> str:
    """Per-(community, caller) points balance key."""
    return f"gamble.points.{pseudonym}"


def _lastbet_key(pseudonym: str) -> str:
    """Per-(community, caller) last-bet timestamp key (the cooldown gate)."""
    return f"gamble.lastbet.{pseudonym}"


def _amount_bucket(amount: int) -> str:
    """Coarse amount bucket for PII-free logs (never the exact value)."""
    for limit in (10, 100, 1000):
        if amount <= limit:
            return f"le{limit}"
    return "gt1000"


def _caller_role_signal(payload: dict[str, Any]) -> bool | None:
    """`True`/`False` from badge fields, or `None` if absent (absent must deny)."""
    if "is_mod" not in payload and "is_broadcaster" not in payload:
        return None
    return payload.get("is_mod") is True or payload.get("is_broadcaster") is True


@dataclass(slots=True, frozen=True)
class BetOutcome:
    """Result of one resolved bet."""

    won: bool
    amount: int
    new_balance: int


def _roll(odds_percent: int) -> bool:
    """Return True with probability `odds_percent`/100."""
    return random.randint(1, 100) <= odds_percent  # noqa: S311 - a game, not security


def _resolve_command(parsed: ParsedCommand | None) -> str:
    """Map a parsed grammar result onto this bundle's command set (else `usage`)."""
    if parsed is None:
        return "usage"
    if parsed.option is None:
        return "balance"
    if parsed.option == "set":
        return "config_set_odds"
    return "usage"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!gamble`, `<amount>` and `set odds`.

    Cheap command-match first, flag second, grammar last (same ordering as `slots`).
    A recognized-but-malformed `!gamble ...` replies `usage`, never a silent drop.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if stripped.partition(" ")[0].lower() != "!gamble":
        return None
    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    tail = stripped.partition(" ")[2].strip()
    payload: dict[str, Any] = {"channel_id": event.payload.get("channel_id")}
    word = tail.lower()
    if word == "all" or (word.isascii() and word.isdigit()):
        payload["command"] = "bet"
        payload["arg"] = word
    else:
        try:
            parsed: ParsedCommand | None = parse_command(stripped, SPEC)
        except CommandUsageError:
            parsed = None
        payload["command"] = _resolve_command(parsed)
        if payload["command"] == "config_set_odds" and parsed is not None:
            payload["arg"] = parsed.args
    log.info("gamble.transform matched", command=payload["command"])
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
    """`waddle_transports.TransportResult`-shaped result (see `slots`)."""

    __slots__ = ("transport", "detail", "sub_type", "http_status")

    def __init__(self, *, transport: str, detail: str) -> None:
        """Record the provider relayed to and a short detail string."""
        self.transport = transport
        self.detail = detail
        self.sub_type = None
        self.http_status = None


class _Ledger:
    """The points-store seam: community-scoped, pseudonymous kv balance (see module docstring).

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
        log.error("gamble.kv_error", op=op, error=case_name)
        await relay.push(
            self._provider,
            {"channel": self._channel_id, "text": "gambling is temporarily unavailable."},
        )
        raise RuntimeError(f"gamble kv {op} failed: {case_name}") from exc

    async def get(self, key: str) -> bytes | None:
        """Fail-loud `community_kv.get`."""
        try:
            return await community_kv.get(self._community, key)
        except Exception as exc:  # noqa: BLE001 - classified in `fail`
            await self.fail(exc, "get")

    async def set(self, key: str, value: bytes, ttl_seconds: int = 0) -> None:
        """Fail-loud `community_kv.set`."""
        try:
            await community_kv.set(self._community, key, value, ttl_seconds)
        except Exception as exc:  # noqa: BLE001 - classified in `fail`
            await self.fail(exc, "set")

    async def increment(self, key: str, delta: int) -> int:
        """Fail-loud `community_kv.increment` (durable, no TTL)."""
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
            log.error("gamble.balance_corrupt", community=self._community)
            await self.fail(ValueError("corrupt balance"), "balance")

    async def credit(self, pseudonym: str, amount: int) -> int:
        """Add `amount` points; return the new balance."""
        return await self.increment(_balance_key(pseudonym), amount)

    async def debit(self, pseudonym: str, amount: int) -> int:
        """Remove `amount` points, flooring at zero if a concurrent bet overdrew."""
        new = await self.increment(_balance_key(pseudonym), -amount)
        if new < 0:
            new = await self.increment(_balance_key(pseudonym), -new)
        return new


async def _get_odds(ledger: _Ledger) -> int:
    """Return the community's win-percentage, or the default if unset/corrupt."""
    raw = await ledger.get(_ODDS_CONFIG_KEY)
    if raw is None:
        return DEFAULT_ODDS_PERCENT
    try:
        value = int(raw.decode())
    except (UnicodeDecodeError, ValueError):
        log.error("gamble.odds_config_corrupt")
        return DEFAULT_ODDS_PERCENT
    return value if MIN_ODDS_PERCENT <= value <= MAX_ODDS_PERCENT else DEFAULT_ODDS_PERCENT


async def _handle_bet(arg: str | None, *, ledger: _Ledger, actor: str | None, username: str) -> str:
    """Validate, cooldown-gate, roll and settle one bet; return the chat reply."""
    pseudonym = _pseudonym(actor)
    balance = await ledger.balance(pseudonym)
    if arg == "all":
        amount = balance
    else:
        try:
            amount = int(arg or "")
        except ValueError as exc:
            log.info("gamble.invalid_bet", op="bet", error=type(exc).__name__)
            return _USAGE
    if amount <= 0:
        return "you have no points to bet." if balance <= 0 else "bet must be at least 1 point."
    if amount > balance:
        return f"{username}, you only have {balance} points."

    now_ms = clock.now_millis()
    last_raw = await ledger.get(_lastbet_key(pseudonym))
    if last_raw is not None:
        try:
            remaining = DEFAULT_COOLDOWN_SECONDS - (now_ms - int(last_raw.decode())) / 1000
        except (UnicodeDecodeError, ValueError):
            remaining = 0
        if remaining > 0:
            return f"\U0001f3b2 slow down, {username}! try again in {max(1, round(remaining))}s."
    await ledger.set(
        _lastbet_key(pseudonym), str(now_ms).encode(), ttl_seconds=DEFAULT_COOLDOWN_SECONDS
    )

    odds = await _get_odds(ledger)
    won = _roll(odds)
    new_balance = (
        await ledger.credit(pseudonym, amount) if won else await ledger.debit(pseudonym, amount)
    )
    log.info(
        "gamble.bet settled",
        outcome="win" if won else "loss",
        amount_bucket=_amount_bucket(amount),
    )
    if won:
        return (
            f"\U0001f3b2 {username} bet {amount} and WON! +{amount} points (balance: {new_balance})"
        )
    return f"\U0001f3b2 {username} bet {amount} and lost. (balance: {new_balance})"


async def _handle_set_odds(arg: str | None, *, ledger: _Ledger) -> str:
    """Parse+apply `set odds <1-95>`'s free-text tail."""
    parts = (arg or "").split()
    if len(parts) != 2 or parts[0].lower() != "odds":
        return _USAGE
    try:
        percent = int(parts[1])
    except ValueError as exc:
        log.info("gamble.invalid_odds", op="set_odds", error=type(exc).__name__)
        return f"'{parts[1]}' isn't a whole number"
    if not (MIN_ODDS_PERCENT <= percent <= MAX_ODDS_PERCENT):
        return f"odds must be between {MIN_ODDS_PERCENT} and {MAX_ODDS_PERCENT} percent"
    await ledger.set(_ODDS_CONFIG_KEY, str(percent).encode())
    return f"gamble win odds set to {percent}%"


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
        raise ValueError("gamble reply requires a channel_id from the inbound chat.message")
    command = payload.get("command")
    if command not in _KNOWN_COMMANDS:
        raise ValueError(f"unrecognized gamble command: {command!r}")
    provider = envelope.event.platform
    community = envelope.community
    if not community:
        log.error("gamble.missing_community", command=command)
        raise ValueError("gamble requires a community context and cannot operate tenant-wide")

    username = envelope.event.actor or "someone"
    ledger = _Ledger(community, provider, channel_id)
    arg = payload.get("arg")
    arg_text = arg if isinstance(arg, str) else None

    if command == "usage":
        reply = _USAGE
    elif command == "config_set_odds":
        if _caller_role_signal(payload) is not True:
            log.info("gamble.config_denied", command=command)
            reply = _PERMISSION_DENIED_MSG
        else:
            reply = await _handle_set_odds(arg_text, ledger=ledger)
    elif command == "balance":
        balance = await ledger.balance(_pseudonym(envelope.event.actor))
        reply = f"{username}, you have {balance} points."
    else:
        reply = await _handle_bet(
            arg_text, ledger=ledger, actor=envelope.event.actor, username=username
        )

    await relay.push(provider, {"channel": channel_id, "text": reply})
    log.info("gamble.dispatch relayed", platform=provider, command=command)
    return DispatchResult(transport=provider, detail=command)
