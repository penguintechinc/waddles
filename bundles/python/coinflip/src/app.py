"""`!coinflip`/`!flip` -> a 50/50 coinflip minigame, community-scoped, kv-only.

Net-new fun content for Waddles -- no inspiration source, no attribution
required (contrast `bundles/python/fish`, which credits superpenguintv
(Psychoboy)'s `PenguinTwitchBot` for the catch-and-track concept; this
bundle's flavor text and call-mechanic are original).

Uses the shared command grammar parser (`waddle_sdk.command.parse_command`/
`CommandSpec`, merged in #618) -- same pattern as `fish`/`slots`'s own
`app.py`. Two chat aliases (`!coinflip` and `!flip`) both route through one
`CommandSpec(name="flip")` -- `transform()` normalizes either leading token
to `!flip` before handing the rest of the text to `parse_command()`, so the
grammar itself only ever needs to know one command name. The optional
"call" argument (`heads`/`tails`) is declared as this command's
`sub_modules` set -- a legitimate reuse of that grammar slot (a closed,
named token vocabulary), not a feature-toggle in this bundle's case; see
`waddle_sdk.command.CommandSpec`'s own docstring for the general shape.

v1 scope (KV-ONLY, no `db` capability -- same deliberate scoping as
`fish`/`slots`, not dependent on the in-flight #623 db API):

- Bare `!flip` (or `!coinflip`) -- FLIP, the grammar's bare/default action.
  Draws one 50/50 `heads`/`tails` result (`random.choice`) and replies with
  a fun flavor line. No call was made, so there is no win/lose outcome --
  only the caller's running flip count is updated.
- `!flip heads` / `!flip tails` -- CALL. Same 50/50 draw, but the caller's
  guess is checked against the result: a match increments the caller's
  running win count (correct calls), a miss does not. Either way the flip
  count is updated.
- `!flip list` -- reads the caller's own stats (total flips, correct calls)
  from `kv`. No `args` accepted -- `!flip list <anything>` is a usage
  error, not silently ignored.
- `!flip set cooldown <seconds>` -- broadcaster/moderator-only (same
  `_caller_role_signal()` fail-closed pattern as `fish`/`slots`/`lurk`/
  `count`: absent badge fields -- e.g. Discord's normalizer today -- deny,
  never implicit allow), persists a per-community cooldown override,
  bounded `[MIN_COOLDOWN_SECONDS, MAX_COOLDOWN_SECONDS]`. Both bounds are
  inclusive, and `0` is a legal value -- an admin may disable the cooldown
  entirely.
- Every flip (called or bare) shares one per-(community, caller) cooldown
  gate (`DEFAULT_COOLDOWN_SECONDS`, admin-configurable) stored as a plain
  kv timestamp with `ttl_seconds=cooldown` -- the TTL itself is the
  auto-expiry, same pattern as `fish`'s own `fish:lastcast:<pseudonym>`.
  `ttl_seconds=0` (a configured cooldown of `0`) means "never expires" at
  the kv layer (`waddle_sdk.kv.set`'s own docstring); the cooldown *gate*
  check still always allows immediately in that case since the remaining-
  wait computation can never be positive when the configured cooldown is
  `0` -- the stored timestamp simply persists without being read as a
  denial.

**No betting/currency ties.** This bundle tracks only a flip count and a
correct-call count -- there is no wallet, balance, points value, payout,
transfer, or redemption path anywhere in this bundle, and none is planned
for a v2. This is a standalone chat game, not a gambling economy feature.

DO NOT BUILD in v1 -- clean, documented extension points, never a silent
stub:

- **Cross-community leaderboards.** Every flip/stat key here is scoped by
  `community_id` only (`waddle_sdk.community_kv` -- see its own module
  docstring: reputation/user-details are the platform's only two
  cross-community exceptions, and this bundle is neither). A global or
  per-tenant leaderboard needs a query spanning communities, which only a
  real `db` capability with an `order_by`/pagination surface can answer
  well -- `kv` has no scan/list-keys primitive. **Deferred to a v2 once the
  in-flight #623 `db` order_by API lands**, same deferral as
  `fish`/`slots`'s own -- not built here, not stubbed, no `!flip
  leaderboard` command declared.
- Any wallet/points-economy integration (betting an actual balance,
  redeeming a payout) -- out of scope per the no-betting-ties rule above,
  not partially stubbed.

Gated behind the PostHog flag ``waddles.command-coinflip`` -- see
`bundles/python/eightball/src/app.py`'s own docstring for the flag-gate
rationale and ordering (cheap command-match first, flag check second, real
grammar parse last).
"""

from __future__ import annotations

import hashlib
import random
from typing import Any, NoReturn

from waddle_sdk import clock, community_kv, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, ParsedCommand, parse_command
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-coinflip"

#: Chat aliases that both route to this bundle -- see module docstring's
#: alias-normalization rationale. Compared against `head.lower()`, so both
#: are already lowercase.
_ALIASES: frozenset[str] = frozenset({"!coinflip", "!flip"})

#: The optional call argument's closed vocabulary, declared as this
#: command's `sub_modules` -- see module docstring for why this grammar
#: slot (rather than a bespoke parser) is the right fit here.
_CALL_CHOICES: frozenset[str] = frozenset({"heads", "tails"})

SPEC = CommandSpec(name="flip", sub_modules=_CALL_CHOICES)

#: Bounds for `!flip set cooldown <seconds>` -- keeps an admin from setting
#: something pathological (a multi-day "cooldown" that is effectively a
#: lockout) without a second confirmation step. `0` is a legal lower bound
#: (disables the cooldown entirely) -- unlike `fish`/`slots`, a coinflip is
#: meant to be able to go rapid-fire if a community wants that.
DEFAULT_COOLDOWN_SECONDS = 10
MIN_COOLDOWN_SECONDS = 0
MAX_COOLDOWN_SECONDS = 3600

#: Durable per-community config -- never expires (`ttl_seconds=0`), mirrors
#: `fish`/`slots`'s own `_COOLDOWN_CONFIG_KEY` convention.
_COOLDOWN_CONFIG_KEY = "coinflip.config.cooldown"

_USAGE = (
    "Usage: !flip | !flip heads | !flip tails | !flip list | "
    "!flip set cooldown <seconds> (set is admin/mod only). Alias: !coinflip."
)
_PERMISSION_DENIED_MSG = "only moderators/broadcasters can configure !flip"
_NOTHING_FLIPPED_YET = "You haven't flipped a coin yet -- try !flip!"

_KNOWN_COMMANDS = frozenset({"flip", "list", "config_set_cooldown", "usage"})

_FLIP_FLAVORS: tuple[str, ...] = (
    "spinning through the air...",
    "a perfect arc, caught clean.",
    "tumbling end over end...",
    "landed flat with a satisfying clink.",
)


def _flip_coin() -> str:
    """Draw one 50/50 `"heads"`/`"tails"` result."""
    return random.choice(("heads", "tails"))  # noqa: S311 - a game, not security


def _evaluate_flip(call: str | None, result: str) -> bool | None:
    """Return `None` for a bare (uncalled) flip, else whether `call` matched `result`."""
    if call is None:
        return None
    return call == result


def _pseudonym(actor: str | None) -> str:
    """Non-reversible per-caller key component -- see `fish`/`slots`'s own `_pseudonym()` for why.

    `event.actor` may currently be a raw username (tokenization pipeline
    #429 not yet merged); hashing it before it ever reaches `community_kv`
    keeps this bundle PII-safe today and after #429 lands unchanged.
    """
    return hashlib.sha256((actor or "anonymous").encode()).hexdigest()


def _flips_key(pseudonym: str) -> str:
    """Per-(community, caller) total-flips counter key (bare + called flips)."""
    return f"coinflip.flips.{pseudonym}"


def _wins_key(pseudonym: str) -> str:
    """Per-(community, caller) correct-calls counter key (called flips only)."""
    return f"coinflip.wins.{pseudonym}"


def _lastflip_key(pseudonym: str) -> str:
    """Per-(community, caller) last-flip-timestamp key (the cooldown gate)."""
    return f"coinflip.lastflip.{pseudonym}"


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

    See `fish`/`slots`/`count`/`lurk`'s own identical helper -- `None`
    (neither `is_mod`/`is_broadcaster` present, e.g. Discord's normalizer
    today) must be treated as denied, never as an implicit allow.
    """
    if "is_mod" not in payload and "is_broadcaster" not in payload:
        return None
    return bool(payload.get("is_mod")) or bool(payload.get("is_broadcaster"))


def _normalize_alias(stripped: str, head: str) -> str:
    """Rewrite either recognized alias's leading token to `!flip` -- see module docstring."""
    rest = stripped[len(head) :].strip()
    return f"!flip {rest}" if rest else "!flip"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!coinflip`/`!flip`, via `parse_command`.

    Cheap-skip first (no leading alias token -- `None`, zero cost), flag
    check second, real grammar parse last -- same ordering as `fish`'s own
    documented rationale. A recognized-but-malformed `!flip ...` (a
    `CommandUsageError`, or an option this bundle doesn't implement, e.g.
    `!flip enable`) still produces a reply (`"usage"`) since the caller did
    invoke this command -- never silently dropped.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None

    stripped = text.strip()
    head = stripped.partition(" ")[0]
    if head.lower() not in _ALIASES:
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    normalized = _normalize_alias(stripped, head)
    try:
        parsed: ParsedCommand | None = parse_command(normalized, SPEC)
    except CommandUsageError:
        parsed = None
    command = _resolve_command(parsed)

    log.info("coinflip.transform matched", command=command)
    payload: dict[str, Any] = {"command": command, "channel_id": event.payload.get("channel_id")}
    if command == "flip" and parsed is not None and parsed.sub_module is not None:
        payload["call"] = parsed.sub_module
    if command == "config_set_cooldown" and parsed is not None:
        payload["arg"] = parsed.args
    # Forward the normalized badge signal, if present -- see `fish`/`slots`/`count`/`lurk`'s own
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

    A `sub_module` alone (`heads`/`tails`, with no further `option`) is a
    call-flip. A bare command (no `sub_module`, no `option`) is an
    uncalled flip. `option in ("list", "set")` with no `sub_module` map to
    their own commands. Every other combination -- an unimplemented verb,
    or a `sub_module` combined with any `option` (e.g. `!flip heads list`,
    `!flip heads enable`) -- resolves to `"usage"`, same fail-loud-never-
    silent rule as a parse error itself.
    """
    if parsed is None:
        return "usage"
    if parsed.sub_module is not None:
        return "flip" if parsed.option is None else "usage"
    if parsed.option is None:
        return "flip"
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
    log.error("coinflip.kv_error", op=op, error=case_name)
    await relay.push(
        provider,
        {
            "channel": channel_id,
            "text": "the coin is temporarily unavailable, try again shortly.",
        },
    )
    raise RuntimeError(f"coinflip kv {op} failed: {case_name}") from exc


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
        log.error("coinflip.cooldown_config_corrupt", community=community)
        return DEFAULT_COOLDOWN_SECONDS


async def _handle_flip(
    *,
    community: str,
    actor: str | None,
    username: str,
    call: str | None,
    provider: str,
    channel_id: str,
) -> str:
    """Enforce the per-caller cooldown, then flip+persist a result and build the reply."""
    pseudonym = _pseudonym(actor)
    cooldown = await _get_cooldown(community, provider=provider, channel_id=channel_id)
    last_raw = await _kv_get(
        community, _lastflip_key(pseudonym), provider=provider, channel_id=channel_id
    )
    now_ms = clock.now_millis()

    if last_raw is not None:
        try:
            last_ms: int | None = int(last_raw.decode())
        except (UnicodeDecodeError, ValueError):
            log.error("coinflip.cooldown_state_corrupt", community=community)
            last_ms = None
        if last_ms is not None:
            remaining_s = cooldown - (now_ms - last_ms) / 1000
            if remaining_s > 0:
                wait_for = _format_duration(max(1, round(remaining_s)))
                return f"\U0001fa99 slow down, {username}! try again in {wait_for}."

    await _kv_set(
        community,
        _lastflip_key(pseudonym),
        str(now_ms).encode(),
        ttl_seconds=cooldown,
        provider=provider,
        channel_id=channel_id,
    )

    result = _flip_coin()
    flips = await _kv_increment(
        community,
        _flips_key(pseudonym),
        1,
        ttl_seconds=0,
        provider=provider,
        channel_id=channel_id,
    )

    is_correct = _evaluate_flip(call, result)

    if is_correct is None:
        flavor = random.choice(_FLIP_FLAVORS)  # noqa: S311 - a game, not a security decision
        return f"\U0001fa99 {username} flips a coin -- {flavor} {result.upper()}! (flip #{flips})"

    if is_correct:
        await _kv_increment(
            community,
            _wins_key(pseudonym),
            1,
            ttl_seconds=0,
            provider=provider,
            channel_id=channel_id,
        )
        return (
            f"\U0001fa99 {username} calls {call}... {result.upper()}! "
            f"You called it right! (flip #{flips})"
        )

    return (
        f"\U0001fa99 {username} calls {call}... {result.upper()}! "
        f"Not this time. (flip #{flips})"
    )


async def _handle_list(
    *, community: str, actor: str | None, provider: str, channel_id: str
) -> str:
    """Read+render the caller's own total-flips/correct-calls stats."""
    pseudonym = _pseudonym(actor)
    flips_raw = await _kv_get(
        community, _flips_key(pseudonym), provider=provider, channel_id=channel_id
    )
    flips = 0
    if flips_raw is not None:
        try:
            flips = int(flips_raw.decode())
        except (UnicodeDecodeError, ValueError):
            log.error("coinflip.flips_corrupt", community=community)
            flips = 0

    if flips == 0:
        return _NOTHING_FLIPPED_YET

    wins_raw = await _kv_get(
        community, _wins_key(pseudonym), provider=provider, channel_id=channel_id
    )
    wins = 0
    if wins_raw is not None:
        try:
            wins = int(wins_raw.decode())
        except (UnicodeDecodeError, ValueError):
            log.error("coinflip.wins_corrupt", community=community)
            wins = 0

    return f"Flips: {flips}. Correct calls: {wins}."


async def _handle_set_cooldown(
    arg: str | None, *, community: str, provider: str, channel_id: str
) -> str:
    """Parse+apply `!flip set cooldown <seconds>`'s own free-text `args` tail."""
    if not arg:
        return _USAGE
    parts = arg.split()
    if len(parts) != 2 or parts[0].lower() != "cooldown":
        return _USAGE
    try:
        seconds = int(parts[1])
    except ValueError as exc:
        log.debug("coinflip.invalid_cooldown", error_type=type(exc).__name__)
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
    return f"coinflip cooldown set to {seconds}s"


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
        raise ValueError("coinflip reply requires a channel_id from the inbound chat.message")
    command = payload.get("command")
    if command not in _KNOWN_COMMANDS:
        raise ValueError(f"unrecognized coinflip command: {command!r}")

    provider = envelope.event.platform
    community = envelope.community
    if not community:
        log.error("coinflip.missing_community", command=command)
        raise ValueError("coinflip requires a community context and cannot operate tenant-wide")

    username = envelope.event.actor or "someone"

    if command == "usage":
        await relay.push(provider, {"channel": channel_id, "text": _USAGE})
        return DispatchResult(transport=provider, detail="usage")

    if command == "config_set_cooldown":
        role_signal = _caller_role_signal(payload)
        if role_signal is not True:
            log.info("coinflip.config_denied", command=command, role_signal=str(role_signal))
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
        log.info("coinflip.dispatch config applied", command=command)
        return DispatchResult(transport=provider, detail=command)

    if command == "list":
        reply_text = await _handle_list(
            community=community,
            actor=envelope.event.actor,
            provider=provider,
            channel_id=channel_id,
        )
    else:  # flip
        call_raw = payload.get("call")
        call = call_raw if call_raw in _CALL_CHOICES else None
        reply_text = await _handle_flip(
            community=community,
            actor=envelope.event.actor,
            username=username,
            call=call,
            provider=provider,
            channel_id=channel_id,
        )

    await relay.push(provider, {"channel": channel_id, "text": reply_text})
    log.info("coinflip.dispatch relayed", platform=provider, command=command)
    return DispatchResult(transport=provider, detail=command)
