"""`!slots` -> a weighted-random slot-machine minigame, community-scoped, kv-only.

Net-new fun content for Waddles -- no inspiration source, no attribution
required (contrast `bundles/python/fish`, which credits superpenguintv
(Psychoboy)'s `PenguinTwitchBot` for the catch-and-track concept; this
bundle's reel table, payout structure, and flavor text are original).

Uses the shared command grammar parser (`waddle_sdk.command.parse_command`/
`CommandSpec`, merged in #618) -- same pattern as `fish`'s own `app.py`.

v1 scope (KV-ONLY, no `db` capability -- same deliberate scoping as `fish`,
not dependent on the in-flight #623 db API):

- Bare `!slots` -- SPIN, the grammar's bare/default action. Draws three
  independent weighted-random reel symbols (`random.choices(..., k=3)`,
  with replacement -- a real three-reel slot machine). All three matching
  is a win, paying the matched symbol's own point value (points only, no
  currency/wallet -- see module-level rationale below); anything else is a
  loss. Updates the caller's running spin count, win count, and
  best-payout record. Enforced by a per-(community, caller) cooldown
  (`DEFAULT_COOLDOWN_SECONDS`, admin-configurable) stored as a plain kv
  timestamp with `ttl_seconds=cooldown` -- the TTL itself is the
  auto-expiry, same pattern as `fish`'s own `fish:lastcast:<pseudonym>`.
- `!slots list` -- reads the caller's own stats (total spins, total wins,
  best payout) from `kv`. No `args` accepted -- `!slots list <anything>`
  is a usage error, not silently ignored.
- `!slots set cooldown <seconds>` -- broadcaster/moderator-only (same
  `_caller_role_signal()` fail-closed pattern as `fish`/`lurk`/`count`:
  absent badge fields -- e.g. Discord's normalizer today -- deny, never
  implicit allow), persists a per-community cooldown override, bounded
  `[MIN_COOLDOWN_SECONDS, MAX_COOLDOWN_SECONDS]`.

**No real-money/currency ties.** The "payout" tracked here is an abstract
point value used only to rank the caller's own best spin (`!slots list`) --
there is no wallet, balance, transfer, or redemption path anywhere in this
bundle, and none is planned for a v2. This is a chat game, not a gambling
economy feature.

DO NOT BUILD in v1 -- clean, documented extension points, never a silent
stub:

- **Cross-community leaderboards.** Every spin/stat key here is scoped by
  `community_id` only (`waddle_sdk.community_kv` -- see its own module
  docstring: reputation/user-details are the platform's only two
  cross-community exceptions, and this bundle is neither). A global or
  per-tenant leaderboard needs a query spanning communities, which only a
  real `db` capability with an `order_by`/pagination surface can answer
  well -- `kv` has no scan/list-keys primitive. **Deferred to a v2 once the
  in-flight #623 `db` order_by API lands**, same deferral as `fish`'s own
  -- not built here, not stubbed, no `!slots leaderboard` command declared.
- Any wallet/points-economy integration (betting an actual balance,
  redeeming payouts) -- out of scope per the no-currency-ties rule above,
  not partially stubbed.

Gated behind the PostHog flag ``waddles.command-slots`` -- see
`bundles/python/eightball/src/app.py`'s own docstring for the flag-gate
rationale and ordering (cheap command-match first, flag check second, real
grammar parse last).
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

FLAG_KEY = "waddles.command-slots"

#: No sub-modules declared for v1 -- every `VERBS` token other than
#: `list`/`set` resolves to a usage reply (see `_resolve_command`), never a
#: silent drop.
SPEC = CommandSpec(name="slots")

#: Bounds for `!slots set cooldown <seconds>` -- keeps an admin from setting
#: something pathological (0s spam, or a multi-day "cooldown" that is
#: effectively a lockout) without a second confirmation step. Mirrors
#: `fish`'s own bounds convention (different default -- spins are meant to
#: be snappier than casts).
DEFAULT_COOLDOWN_SECONDS = 30
MIN_COOLDOWN_SECONDS = 5
MAX_COOLDOWN_SECONDS = 3600

#: Durable per-community config -- never expires (`ttl_seconds=0`), mirrors
#: `fish`'s own `_COOLDOWN_CONFIG_KEY` convention.
_COOLDOWN_CONFIG_KEY = "slots.config.cooldown"

_USAGE = "Usage: !slots | !slots list | !slots set cooldown <seconds> (set is admin/mod only)"
_PERMISSION_DENIED_MSG = "only moderators/broadcasters can configure !slots"
_NOTHING_SPUN_YET = "You haven't spun the slots yet -- try !slots!"

_KNOWN_COMMANDS = frozenset({"spin", "list", "config_set_cooldown", "usage"})


@dataclass(slots=True, frozen=True)
class SlotSymbol:
    """One entry in the weighted reel table -- original content, see module docstring."""

    name: str
    emoji: str
    payout: int
    flavor: str


#: Weighted reel table. Weights are relative (`random.choices` normalizes
#: them), not percentages. Each of the three reels draws independently
#: (with replacement) from this same table -- a real three-reel slot
#: machine. A win is all three reels landing on the *same* symbol; the
#: matched symbol's own `payout` is the points awarded.
_REEL_TABLE: tuple[tuple[SlotSymbol, float], ...] = (
    (SlotSymbol("Cherry", "\U0001f352", 5, "triple cherries!"), 40.0),
    (SlotSymbol("Lemon", "\U0001f34b", 8, "a trio of lemons!"), 30.0),
    (SlotSymbol("Orange", "\U0001f34a", 10, "three oranges in a row!"), 18.0),
    (SlotSymbol("Bell", "\U0001f514", 25, "bells ringing -- nice!"), 8.0),
    (SlotSymbol("Diamond", "\U0001f48e", 75, "triple diamonds -- shiny!"), 3.0),
    (SlotSymbol("Seven", "7️⃣", 250, "JACKPOT -- triple sevens!!"), 1.0),
)

_LOSE_FLAVORS: tuple[str, ...] = (
    "no match this time -- spin again!",
    "so close! try again.",
    "the reels don't align -- better luck next spin.",
)


def _roll_reels() -> tuple[SlotSymbol, SlotSymbol, SlotSymbol]:
    """Draw three independent weighted-random reel symbols (with replacement)."""
    population = [entry[0] for entry in _REEL_TABLE]
    weights = [entry[1] for entry in _REEL_TABLE]
    a, b, c = random.choices(population, weights=weights, k=3)  # noqa: S311 - a game, not security
    return a, b, c


def _evaluate_spin(reels: tuple[SlotSymbol, SlotSymbol, SlotSymbol]) -> tuple[bool, int]:
    """Return `(is_win, payout)` -- a win is all three reels matching by name."""
    a, b, c = reels
    if a.name == b.name == c.name:
        return True, a.payout
    return False, 0


def _pseudonym(actor: str | None) -> str:
    """Non-reversible per-caller key component -- see `fish`'s own `_pseudonym()` for why.

    `event.actor` may currently be a raw username (tokenization pipeline
    #429 not yet merged); hashing it before it ever reaches `community_kv`
    keeps this bundle PII-safe today and after #429 lands unchanged.
    """
    return hashlib.sha256((actor or "anonymous").encode()).hexdigest()


def _spins_key(pseudonym: str) -> str:
    """Per-(community, caller) total-spins counter key."""
    return f"slots.spins.{pseudonym}"


def _wins_key(pseudonym: str) -> str:
    """Per-(community, caller) total-wins counter key."""
    return f"slots.wins.{pseudonym}"


def _lastspin_key(pseudonym: str) -> str:
    """Per-(community, caller) last-spin-timestamp key (the cooldown gate)."""
    return f"slots.lastspin.{pseudonym}"


def _bestpayout_key(pseudonym: str) -> str:
    """Per-(community, caller) best-payout record key (JSON: symbol/payout)."""
    return f"slots.bestpayout.{pseudonym}"


def _format_duration(total_seconds: int) -> str:
    """Human-readable duration, e.g. "2m 5s" -- two largest nonzero units (see `fish`'s own)."""
    total_seconds = max(total_seconds, 0)
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    nonzero = [(v, s) for v, s in ((hours, "h"), (minutes, "m"), (seconds, "s")) if v > 0]
    if not nonzero:
        return "0s"
    return " ".join(f"{v}{s}" for v, s in nonzero[:2])


def _caller_role_signal(payload: dict[str, Any]) -> bool | None:
    """`True`/`False` from the normalized event's own badge fields, or `None` if absent.

    See `fish`/`count`/`lurk`'s own identical helper -- `None` (neither
    `is_mod`/`is_broadcaster` present, e.g. Discord's normalizer today)
    must be treated as denied, never as an implicit allow.
    """
    if "is_mod" not in payload and "is_broadcaster" not in payload:
        return None
    return bool(payload.get("is_mod")) or bool(payload.get("is_broadcaster"))


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!slots` and its grammar, via `parse_command`.

    Cheap-skip first (no leading `!slots` token -- `None`, zero cost), flag
    check second, real grammar parse last -- same ordering as `fish`'s own
    documented rationale. A recognized-but-malformed `!slots ...` (a
    `CommandUsageError`, or an option this bundle doesn't implement, e.g.
    `!slots enable`) still produces a reply (`"usage"`) since the caller did
    invoke this command -- never silently dropped.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None

    stripped = text.strip()
    head = stripped.partition(" ")[0]
    if head.lower() != "!slots":
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    try:
        parsed: ParsedCommand | None = parse_command(stripped, SPEC)
    except CommandUsageError:
        parsed = None
    command = _resolve_command(parsed)

    log.info("slots.transform matched", command=command)
    payload: dict[str, Any] = {"command": command, "channel_id": event.payload.get("channel_id")}
    if command == "config_set_cooldown" and parsed is not None:
        payload["arg"] = parsed.args
    # Forward the normalized badge signal, if present -- see `fish`/`count`/`lurk`'s own
    # identical forwarding comment for why absence must reach `dispatch` as absence, not `False`.
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


def _resolve_command(parsed: ParsedCommand | None) -> str:
    """Map a `ParsedCommand` (or `None` on a grammar error) onto this bundle's own command set.

    Only `option in (None, "list", "set")` is implemented -- every other
    grammar-legal verb (`add`/`sub`/`enable`/`disable`/`remove`/`reset`,
    none of which this bundle declares sub-modules or behavior for)
    resolves to `"usage"`, same fail-loud-never-silent rule as a parse
    error itself.
    """
    if parsed is None:
        return "usage"
    if parsed.option is None:
        return "spin"
    if parsed.option == "list":
        return "list" if parsed.args is None else "usage"
    if parsed.option == "set":
        return "config_set_cooldown"
    return "usage"


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result -- see `pyping`/`fish`'s own `app.py`."""

    __slots__ = ("transport", "detail", "sub_type", "http_status")

    def __init__(self, *, transport: str, detail: str) -> None:
        """Record which provider the reply was relayed to, and a short detail string."""
        self.transport = transport
        self.detail = detail
        self.sub_type = None
        self.http_status = None


async def _fail_kv(exc: Exception, *, provider: str, channel_id: str, op: str) -> NoReturn:
    """Fail-loud kv error path: log, reply an error to chat, then re-raise -- see `fish`'s own."""
    wit_error = getattr(exc, "value", exc)
    case_name = type(wit_error).__name__
    log.error("slots.kv_error", op=op, error=case_name)
    await relay.push(
        provider,
        {
            "channel": channel_id,
            "text": "the slot machine is temporarily unavailable, try again shortly.",
        },
    )
    raise RuntimeError(f"slots kv {op} failed: {case_name}") from exc


async def _kv_get(community: str, key: str, *, provider: str, channel_id: str) -> bytes | None:
    """`community_kv.get`, fail-loud on a backend error (see `_fail_kv`)."""
    try:
        return await community_kv.get(community, key)
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail_kv`
        await _fail_kv(exc, provider=provider, channel_id=channel_id, op="get")


async def _kv_set(
    community: str, key: str, value: bytes, *, ttl_seconds: int, provider: str, channel_id: str
) -> None:
    """`community_kv.set`, fail-loud on a backend error (see `_fail_kv`)."""
    try:
        await community_kv.set(community, key, value, ttl_seconds)
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail_kv`
        await _fail_kv(exc, provider=provider, channel_id=channel_id, op="set")


async def _kv_increment(
    community: str, key: str, delta: int, *, ttl_seconds: int, provider: str, channel_id: str
) -> int:
    """`community_kv.increment`, fail-loud on a backend error (see `_fail_kv`)."""
    try:
        return await community_kv.increment(community, key, delta, ttl_seconds)
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail_kv`
        await _fail_kv(exc, provider=provider, channel_id=channel_id, op="increment")


async def _get_cooldown(community: str, *, provider: str, channel_id: str) -> int:
    """Return the community's configured cooldown, or the default if unset/corrupt."""
    raw = await _kv_get(community, _COOLDOWN_CONFIG_KEY, provider=provider, channel_id=channel_id)
    if raw is None:
        return DEFAULT_COOLDOWN_SECONDS
    try:
        return int(raw.decode())
    except (UnicodeDecodeError, ValueError):
        log.error("slots.cooldown_config_corrupt", community=community)
        return DEFAULT_COOLDOWN_SECONDS


async def _maybe_update_best_payout(
    community: str,
    pseudonym: str,
    symbol: SlotSymbol,
    payout: int,
    *,
    provider: str,
    channel_id: str,
) -> None:
    """Overwrite the caller's best-payout record if `payout` beats the stored one."""
    raw = await _kv_get(
        community, _bestpayout_key(pseudonym), provider=provider, channel_id=channel_id
    )
    if raw is not None:
        try:
            current = json.loads(raw.decode())
            if int(current["payout"]) >= payout:
                return
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            log.error("slots.bestpayout_corrupt", community=community)
    record = json.dumps({"symbol": symbol.name, "payout": payout}).encode()
    await _kv_set(
        community,
        _bestpayout_key(pseudonym),
        record,
        ttl_seconds=0,
        provider=provider,
        channel_id=channel_id,
    )


async def _handle_spin(
    *, community: str, actor: str | None, username: str, provider: str, channel_id: str
) -> str:
    """Enforce the per-caller cooldown, then roll+persist a spin and build the reply."""
    pseudonym = _pseudonym(actor)
    cooldown = await _get_cooldown(community, provider=provider, channel_id=channel_id)
    last_raw = await _kv_get(
        community, _lastspin_key(pseudonym), provider=provider, channel_id=channel_id
    )
    now_ms = clock.now_millis()

    if last_raw is not None:
        try:
            last_ms: int | None = int(last_raw.decode())
        except (UnicodeDecodeError, ValueError):
            log.error("slots.cooldown_state_corrupt", community=community)
            last_ms = None
        if last_ms is not None:
            remaining_s = cooldown - (now_ms - last_ms) / 1000
            if remaining_s > 0:
                wait_for = _format_duration(max(1, round(remaining_s)))
                return f"\U0001f3b0 slow down, {username}! try again in {wait_for}."

    await _kv_set(
        community,
        _lastspin_key(pseudonym),
        str(now_ms).encode(),
        ttl_seconds=cooldown,
        provider=provider,
        channel_id=channel_id,
    )

    reels = _roll_reels()
    is_win, payout = _evaluate_spin(reels)
    reel_text = " ".join(symbol.emoji for symbol in reels)

    spins = await _kv_increment(
        community,
        _spins_key(pseudonym),
        1,
        ttl_seconds=0,
        provider=provider,
        channel_id=channel_id,
    )

    if is_win:
        await _kv_increment(
            community,
            _wins_key(pseudonym),
            1,
            ttl_seconds=0,
            provider=provider,
            channel_id=channel_id,
        )
        await _maybe_update_best_payout(
            community, pseudonym, reels[0], payout, provider=provider, channel_id=channel_id
        )
        return (
            f"\U0001f3b0 {username} spins... {reel_text} -- {reels[0].flavor} "
            f"You win {payout} points! (spin #{spins})"
        )

    flavor = random.choice(_LOSE_FLAVORS)  # noqa: S311 - a game, not a security decision
    return f"\U0001f3b0 {username} spins... {reel_text} -- {flavor} (spin #{spins})"


async def _handle_list(
    *, community: str, actor: str | None, provider: str, channel_id: str
) -> str:
    """Read+render the caller's own total-spins/wins/best-payout stats."""
    pseudonym = _pseudonym(actor)
    spins_raw = await _kv_get(
        community, _spins_key(pseudonym), provider=provider, channel_id=channel_id
    )
    spins = 0
    if spins_raw is not None:
        try:
            spins = int(spins_raw.decode())
        except (UnicodeDecodeError, ValueError):
            log.error("slots.spins_corrupt", community=community)
            spins = 0

    if spins == 0:
        return _NOTHING_SPUN_YET

    wins_raw = await _kv_get(
        community, _wins_key(pseudonym), provider=provider, channel_id=channel_id
    )
    wins = 0
    if wins_raw is not None:
        try:
            wins = int(wins_raw.decode())
        except (UnicodeDecodeError, ValueError):
            log.error("slots.wins_corrupt", community=community)
            wins = 0

    best_raw = await _kv_get(
        community, _bestpayout_key(pseudonym), provider=provider, channel_id=channel_id
    )
    best_text = "none yet"
    if best_raw is not None:
        try:
            data = json.loads(best_raw.decode())
            best_text = f"{int(data['payout'])} points ({data['symbol']})"
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            log.error("slots.bestpayout_corrupt", community=community)

    return f"Spins: {spins}. Wins: {wins}. Best payout: {best_text}."


async def _handle_set_cooldown(
    arg: str | None, *, community: str, provider: str, channel_id: str
) -> str:
    """Parse+apply `!slots set cooldown <seconds>`'s own free-text `args` tail."""
    if not arg:
        return _USAGE
    parts = arg.split()
    if len(parts) != 2 or parts[0].lower() != "cooldown":
        return _USAGE
    try:
        seconds = int(parts[1])
    except ValueError:
        return f"'{parts[1]}' isn't a whole number of seconds"
    if not (MIN_COOLDOWN_SECONDS <= seconds <= MAX_COOLDOWN_SECONDS):
        return (
            f"cooldown must be between {MIN_COOLDOWN_SECONDS} and "
            f"{MAX_COOLDOWN_SECONDS} seconds"
        )
    await _kv_set(
        community,
        _COOLDOWN_CONFIG_KEY,
        str(seconds).encode(),
        ttl_seconds=0,
        provider=provider,
        channel_id=channel_id,
    )
    return f"slots cooldown set to {seconds}s"


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: all kv state reads/writes, then relay the reply.

    Raises:
        ValueError: The envelope's payload has no `channel_id`; the
            envelope has no `community` (no tenant-wide fallback -- see
            module docstring's data-scoping section); or an unrecognized
            `command` (defensive -- `transform` only ever emits a member of
            `_KNOWN_COMMANDS`).
        RuntimeError: A `kv` backend call failed (see `_fail_kv` -- a chat
            error reply and an ERROR log line are always emitted first).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("slots reply requires a channel_id from the inbound chat.message")
    command = payload.get("command")
    if command not in _KNOWN_COMMANDS:
        raise ValueError(f"unrecognized slots command: {command!r}")

    provider = envelope.event.platform
    community = envelope.community
    if not community:
        log.error("slots.missing_community", command=command)
        raise ValueError("slots requires a community context and cannot operate tenant-wide")

    username = envelope.event.actor or "someone"

    if command == "usage":
        await relay.push(provider, {"channel": channel_id, "text": _USAGE})
        return DispatchResult(transport=provider, detail="usage")

    if command == "config_set_cooldown":
        role_signal = _caller_role_signal(payload)
        if role_signal is not True:
            log.info("slots.config_denied", command=command, role_signal=str(role_signal))
            await relay.push(provider, {"channel": channel_id, "text": _PERMISSION_DENIED_MSG})
            return DispatchResult(transport=provider, detail=f"{command}:denied")
        arg = payload.get("arg")
        reply_text = await _handle_set_cooldown(
            arg if isinstance(arg, str) else None,
            community=community,
            provider=provider,
            channel_id=channel_id,
        )
        await relay.push(provider, {"channel": channel_id, "text": reply_text})
        log.info("slots.dispatch config applied", command=command)
        return DispatchResult(transport=provider, detail=command)

    if command == "list":
        reply_text = await _handle_list(
            community=community,
            actor=envelope.event.actor,
            provider=provider,
            channel_id=channel_id,
        )
    else:  # spin
        reply_text = await _handle_spin(
            community=community,
            actor=envelope.event.actor,
            username=username,
            provider=provider,
            channel_id=channel_id,
        )

    await relay.push(provider, {"channel": channel_id, "text": reply_text})
    log.info("slots.dispatch relayed", platform=provider, command=command)
    return DispatchResult(transport=provider, detail=command)
