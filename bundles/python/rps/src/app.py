"""`!rps <rock|paper|scissors>` -> play one round against the bot, community-scoped, kv-only.

Net-new Waddles content -- not a port of, or inspired by, any external
project (contrast `bundles/python/fish`, which credits a specific
inspiration in its own `bundle.yaml`). The flavor text and all game logic
below are written fresh for this bundle.

Built on the shared command grammar parser
(`waddle_sdk.command.parse_command`/`CommandSpec`, first adopted by `fish`,
#618) for the `list`/`set` sub-grammar -- same fallback shape `duel` (#628)
uses for its own single-token argument: `!rps <choice>` does NOT fit the
shared grammar directly (the argument is a free-text move, not one of
`waddle_sdk.command.VERBS`), so `_resolve_command()` below first tries the
shared parser for the known `list`/`set` shapes and falls back to treating a
single non-verb token as the player's move when the parser rejects it.

v1 scope (KV-ONLY, no `db` capability -- same deliberate scoping as
`fish`/`lurk`/`count`/`duel`):

- `!rps <rock|paper|scissors>` (accepts `r`/`p`/`s` shorthand, case-
  insensitive) -- PLAY. Enforced by a per-caller cooldown
  (`DEFAULT_COOLDOWN_SECONDS`, admin-configurable, same TTL-is-the-expiry
  pattern as `fish`/`duel`'s own cooldown), then rolls a uniformly random bot
  move and replies with a win/lose/tie outcome, updating the caller's W/L/T
  record.
- `!rps list` -- reads the caller's own win/loss/tie record from `kv`. No
  `args` accepted -- `!rps list <anything>` is a usage error, not silently
  ignored (mirrors `fish`/`duel`'s own `!<cmd> list` rule).
- `!rps set cooldown <seconds>` -- broadcaster/moderator-only (same
  `_caller_role_signal()` fail-closed pattern as `fish`/`lurk`/`count`/
  `duel`: absent badge fields -- e.g. Discord's normalizer today -- deny,
  never implicit allow), persists a per-community cooldown override, bounded
  `[MIN_COOLDOWN_SECONDS, MAX_COOLDOWN_SECONDS]`.
- Bare `!rps` (no move) or an unrecognized move both reply with the same
  usage text -- fail-loud, never a silent no-op, and neither consumes the
  caller's cooldown (a typo shouldn't cost a real play).

**Cooldown scope**: per-(community, caller) only. Mirrors `fish`/`duel`'s
own per-caller cooldown shape.

**Identity/pseudonymization**: the caller's per-user state key is a SHA-256
pseudonym (`_pseudonym()`), never the raw username/actor id -- same
rationale as `fish`/`duel`'s own `_pseudonym()`: `event.actor` may currently
be a raw username (tokenization pipeline #429 not yet merged), so hashing
before anything ever reaches `community_kv` keeps this bundle PII-safe today
and unchanged after #429 lands. Unlike `duel`, there is only one human
participant per round (the bot is not a "user"), so there is no
target-pseudonym trade-off to document here.

DO NOT BUILD in v1 -- clean, documented extension points, never a silent
stub:

- **Cross-community leaderboards / global rankings.** Every win/loss/tie key
  here is scoped by `community_id` only (`waddle_sdk.community_kv` -- see
  its own module docstring: reputation + user-details are the platform's
  only two cross-community exceptions, and this bundle is neither). `kv`
  has no scan/list-keys primitive at all -- a leaderboard needs a real `db`
  capability with an `order_by`/pagination surface. **Deferred to a v2**
  once that capability lands (same deferral `fish`/`duel` document for their
  own leaderboards) -- no `!rps leaderboard` command declared here.
- Best-of-N matches, wagers/stakes, and a ranking ladder -- out of scope for
  this kv-only v1 entirely, not partially stubbed.

Gated behind the PostHog flag ``waddles.command-rps`` -- see
`bundles/python/eightball/src/app.py`'s own docstring for the flag-gate
rationale and ordering (cheap command-match first, flag check second, real
grammar parse last).
"""

from __future__ import annotations

import hashlib
import random
from typing import Any, NoReturn

from waddle_sdk import clock, community_kv, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, parse_command
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-rps"

#: No sub-modules declared for v1 -- every `VERBS` token other than
#: `list`/`set` resolves to a usage reply (see `_resolve_command`), never a
#: silent drop.
SPEC = CommandSpec(name="rps")

#: Bounds for `!rps set cooldown <seconds>` -- per spec, `[0, 3600]` (unlike
#: `duel`'s `[5, 3600]`: a `0`-second cooldown is a deliberate, explicitly
#: allowed "no cooldown" admin choice for this game, not a pathological
#: value to guard against).
DEFAULT_COOLDOWN_SECONDS = 10
MIN_COOLDOWN_SECONDS = 0
MAX_COOLDOWN_SECONDS = 3600

#: Durable per-community config -- never expires (`ttl_seconds=0`), mirrors
#: `fish`/`duel`'s own `_COOLDOWN_CONFIG_KEY` convention.
_COOLDOWN_CONFIG_KEY = "rps.config.cooldown"

#: The three real moves, in a fixed order used for the bot's uniformly
#: random pick (`random.choice`, mockable in tests -- see
#: `tests/test_app.py::_force_bot_choice`).
_MOVES: tuple[str, ...] = ("rock", "paper", "scissors")

#: Shorthand + full-name aliases, case-folded at lookup time in
#: `_normalize_choice()`. Not a shape check like `duel`'s
#: `_normalize_target()` -- a closed, exhaustive vocabulary instead.
_CHOICE_ALIASES: dict[str, str] = {
    "rock": "rock",
    "r": "rock",
    "paper": "paper",
    "p": "paper",
    "scissors": "scissors",
    "s": "scissors",
}

#: `_BEATS[x] == y` means move `x` beats move `y` -- the standard rock-
#: paper-scissors cycle.
_BEATS: dict[str, str] = {"rock": "scissors", "paper": "rock", "scissors": "paper"}

_USAGE = "Usage: !rps <rock|paper|scissors> (r/p/s) | !rps list | !rps set cooldown <seconds>"
_PERMISSION_DENIED_MSG = "only moderators/broadcasters can configure !rps"
_NO_RECORD_YET = "You haven't played rps yet -- try !rps rock!"

_KNOWN_COMMANDS = frozenset({"play", "list", "config_set_cooldown", "usage"})

_MOVE_EMOJI: dict[str, str] = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}


def _pseudonym(identity: str | None) -> str:
    """Non-reversible per-identity key component -- see `fish`/`duel`'s own `_pseudonym()`.

    `identity` may be a raw username (tokenization pipeline #429 not yet
    merged); hashing it before it ever reaches `community_kv` keeps this
    bundle PII-safe today and after #429 lands unchanged.
    """
    return hashlib.sha256((identity or "anonymous").encode()).hexdigest()


def _wins_key(pseudonym: str) -> str:
    """Per-(community, player) total-wins counter key."""
    return f"rps.wins.{pseudonym}"


def _losses_key(pseudonym: str) -> str:
    """Per-(community, player) total-losses counter key."""
    return f"rps.losses.{pseudonym}"


def _ties_key(pseudonym: str) -> str:
    """Per-(community, player) total-ties counter key."""
    return f"rps.ties.{pseudonym}"


def _lastplay_key(pseudonym: str) -> str:
    """Per-(community, caller) last-play-timestamp key (the cooldown gate)."""
    return f"rps.lastplay.{pseudonym}"


def _format_duration(total_seconds: int) -> str:
    """Human-readable duration, e.g. "2m 5s" -- two largest nonzero units (see `duel`'s own)."""
    total_seconds = max(total_seconds, 0)
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    nonzero = [(v, s) for v, s in ((hours, "h"), (minutes, "m"), (seconds, "s")) if v > 0]
    if not nonzero:
        return "0s"
    return " ".join(f"{v}{s}" for v, s in nonzero[:2])


def _normalize_choice(raw: str) -> str | None:
    """Case-fold `raw` against the closed rock/paper/scissors + shorthand vocabulary.

    `None` for anything else -- an exhaustive lookup, not a shape check
    (contrast `duel`'s `_normalize_target()`, which validates a freeform
    username shape against no fixed vocabulary at all).
    """
    return _CHOICE_ALIASES.get(raw.strip().lower())


def _resolve_outcome(player_choice: str, bot_choice: str) -> str:
    """Return `"win"`/`"lose"`/`"tie"` from the caller's perspective."""
    if player_choice == bot_choice:
        return "tie"
    return "win" if _BEATS[player_choice] == bot_choice else "lose"


def _caller_role_signal(payload: dict[str, Any]) -> bool | None:
    """`True`/`False` from the normalized event's own badge fields, or `None` if absent.

    See `fish`/`count`/`lurk`/`duel`'s own identical helper -- `None`
    (neither `is_mod`/`is_broadcaster` present, e.g. Discord's normalizer
    today) must be treated as denied, never as an implicit allow.
    """
    if "is_mod" not in payload and "is_broadcaster" not in payload:
        return None
    return bool(payload.get("is_mod")) or bool(payload.get("is_broadcaster"))


def _resolve_command(stripped: str) -> tuple[str, str | None]:
    """Classify a full `!rps ...` message into this bundle's own command set.

    Returns `(command, arg)`: `arg` is `set`'s own free-text tail for
    `config_set_cooldown`, the raw (unvalidated/un-normalized) move token for
    `play`, or `None` otherwise. Move-shorthand expansion and validation
    (`_normalize_choice`) happen in `dispatch`, which owns all kv/state
    logic -- this function only classifies structure, same split `fish`/
    `duel` use between `transform`/`dispatch`.
    """
    rest = stripped.partition(" ")[2].strip()
    if not rest:
        return "usage", None

    try:
        parsed = parse_command(stripped, SPEC)
    except CommandUsageError:
        parsed = None

    if parsed is not None:
        if parsed.option == "list":
            return ("list", None) if parsed.args is None else ("usage", None)
        if parsed.option == "set":
            return "config_set_cooldown", parsed.args
        # Every other grammar-legal verb (add/sub/enable/disable/remove/delete/reset) --
        # no sub-modules or behavior declared for any of them, same fail-loud-never-silent
        # rule as a parse error itself.
        return "usage", None

    # Not a recognized verb -- a single bare token is the move attempt; two or more
    # tokens ("!rps rock solid") is not a valid shape for either grammar.
    tok1, _, tail = rest.partition(" ")
    if tail.strip():
        return "usage", None
    return "play", tok1


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!rps` and its grammar.

    Cheap-skip first (no leading `!rps` token -- `None`, zero cost), flag
    check second, real grammar classification last -- same ordering as
    `fish`/`duel`/`eightball`'s own documented rationale. A recognized-but-
    malformed `!rps ...` still produces a reply (`"usage"`) since the caller
    did invoke this command -- never silently dropped.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None

    stripped = text.strip()
    head = stripped.partition(" ")[0]
    if head.lower() != "!rps":
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    command, arg = _resolve_command(stripped)

    log.info("rps.transform matched", command=command)
    payload: dict[str, Any] = {"command": command, "channel_id": event.payload.get("channel_id")}
    if command == "config_set_cooldown":
        payload["arg"] = arg
    elif command == "play":
        payload["choice"] = arg
    # Forward the normalized badge signal, if present -- see `fish`/`count`/`lurk`/`duel`'s own
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


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result -- see `pyping`/`duel`'s own `app.py`."""

    __slots__ = ("transport", "detail", "sub_type", "http_status")

    def __init__(self, *, transport: str, detail: str) -> None:
        """Record which provider the reply was relayed to, and a short detail string."""
        self.transport = transport
        self.detail = detail
        self.sub_type = None
        self.http_status = None


async def _fail_kv(exc: Exception, *, provider: str, channel_id: str, op: str) -> NoReturn:
    """Fail-loud kv error path: log, reply an error to chat, then re-raise -- see `duel`'s own."""
    wit_error = getattr(exc, "value", exc)
    case_name = type(wit_error).__name__
    log.error("rps.kv_error", op=op, error=case_name)
    await relay.push(
        provider,
        {
            "channel": channel_id,
            "text": "rock-paper-scissors is temporarily unavailable, try again shortly.",
        },
    )
    raise RuntimeError(f"rps kv {op} failed: {case_name}") from exc


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
        log.error("rps.cooldown_config_corrupt", community=community)
        return DEFAULT_COOLDOWN_SECONDS


async def _read_int(
    community: str, key: str, *, provider: str, channel_id: str, corrupt_log: str
) -> int:
    """Read an int counter from `kv`, treating a missing/corrupt value as `0` (never a crash)."""
    raw = await _kv_get(community, key, provider=provider, channel_id=channel_id)
    if raw is None:
        return 0
    try:
        return int(raw.decode())
    except (UnicodeDecodeError, ValueError):
        log.error(corrupt_log, community=community)
        return 0


async def _handle_play(
    raw_choice: str | None,
    *,
    community: str,
    actor: str | None,
    username: str,
    provider: str,
    channel_id: str,
) -> tuple[str, str]:
    """Validate the move, enforce the caller's cooldown, roll the bot's move, update W/L/T.

    Returns `(reply_text, detail)` -- `detail` feeds `DispatchResult.detail`
    as `play:<detail>` (`"invalid_choice"`, `"cooldown"`, or one of
    `"win"`/`"lose"`/`"tie"`).
    """
    normalized = _normalize_choice(raw_choice) if raw_choice else None
    if normalized is None:
        return _USAGE, "invalid_choice"

    pseudonym = _pseudonym(actor)
    cooldown = await _get_cooldown(community, provider=provider, channel_id=channel_id)
    last_raw = await _kv_get(
        community, _lastplay_key(pseudonym), provider=provider, channel_id=channel_id
    )
    now_ms = clock.now_millis()

    if last_raw is not None:
        try:
            last_ms: int | None = int(last_raw.decode())
        except (UnicodeDecodeError, ValueError):
            log.error("rps.cooldown_state_corrupt", community=community)
            last_ms = None
        if last_ms is not None:
            remaining_s = cooldown - (now_ms - last_ms) / 1000
            if remaining_s > 0:
                wait_for = _format_duration(max(1, round(remaining_s)))
                return f"🕒 slow down, {username}! try again in {wait_for}.", "cooldown"

    await _kv_set(
        community,
        _lastplay_key(pseudonym),
        str(now_ms).encode(),
        ttl_seconds=cooldown,
        provider=provider,
        channel_id=channel_id,
    )

    bot_choice = random.choice(_MOVES)  # noqa: S311 -- a game, not a security decision
    outcome = _resolve_outcome(normalized, bot_choice)

    if outcome == "win":
        await _kv_increment(
            community,
            _wins_key(pseudonym),
            1,
            ttl_seconds=0,
            provider=provider,
            channel_id=channel_id,
        )
        outcome_text = f"{username} wins!"
    elif outcome == "lose":
        await _kv_increment(
            community,
            _losses_key(pseudonym),
            1,
            ttl_seconds=0,
            provider=provider,
            channel_id=channel_id,
        )
        outcome_text = "I win!"
    else:
        await _kv_increment(
            community,
            _ties_key(pseudonym),
            1,
            ttl_seconds=0,
            provider=provider,
            channel_id=channel_id,
        )
        outcome_text = "it's a tie!"

    reply = (
        f"{_MOVE_EMOJI[normalized]} {username} threw {normalized}, I threw {bot_choice} -- "
        f"{outcome_text}"
    )
    return reply, outcome


async def _handle_list(*, community: str, actor: str | None, provider: str, channel_id: str) -> str:
    """Read+render the caller's own win/loss/tie record."""
    pseudonym = _pseudonym(actor)
    wins = await _read_int(
        community,
        _wins_key(pseudonym),
        provider=provider,
        channel_id=channel_id,
        corrupt_log="rps.wins_corrupt",
    )
    losses = await _read_int(
        community,
        _losses_key(pseudonym),
        provider=provider,
        channel_id=channel_id,
        corrupt_log="rps.losses_corrupt",
    )
    ties = await _read_int(
        community,
        _ties_key(pseudonym),
        provider=provider,
        channel_id=channel_id,
        corrupt_log="rps.ties_corrupt",
    )
    if wins == 0 and losses == 0 and ties == 0:
        return _NO_RECORD_YET
    return f"Record: {wins}W - {losses}L - {ties}T."


async def _handle_set_cooldown(
    arg: str | None, *, community: str, provider: str, channel_id: str
) -> str:
    """Parse+apply `!rps set cooldown <seconds>`'s own free-text `args` tail."""
    if not arg:
        return _USAGE
    parts = arg.split()
    if len(parts) != 2 or parts[0].lower() != "cooldown":
        return _USAGE
    try:
        seconds = int(parts[1])
    except ValueError as exc:
        log.debug("rps.invalid_cooldown", error_type=type(exc).__name__)
        return f"'{parts[1]}' isn't a whole number of seconds"
    if not (MIN_COOLDOWN_SECONDS <= seconds <= MAX_COOLDOWN_SECONDS):
        return f"cooldown must be between {MIN_COOLDOWN_SECONDS} and {MAX_COOLDOWN_SECONDS} seconds"
    await _kv_set(
        community,
        _COOLDOWN_CONFIG_KEY,
        str(seconds).encode(),
        ttl_seconds=0,
        provider=provider,
        channel_id=channel_id,
    )
    return f"rps cooldown set to {seconds}s"


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
        raise ValueError("rps reply requires a channel_id from the inbound chat.message")
    command = payload.get("command")
    if command not in _KNOWN_COMMANDS:
        raise ValueError(f"unrecognized rps command: {command!r}")

    provider = envelope.event.platform
    community = envelope.community
    if not community:
        log.error("rps.missing_community", command=command)
        raise ValueError("rps requires a community context and cannot operate tenant-wide")

    username = envelope.event.actor or "someone"

    if command == "usage":
        await relay.push(provider, {"channel": channel_id, "text": _USAGE})
        return DispatchResult(transport=provider, detail="usage")

    if command == "config_set_cooldown":
        role_signal = _caller_role_signal(payload)
        if role_signal is not True:
            log.info("rps.config_denied", command=command, role_signal=str(role_signal))
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
        log.info("rps.dispatch config applied", command=command)
        return DispatchResult(transport=provider, detail=command)

    if command == "list":
        reply_text = await _handle_list(
            community=community,
            actor=envelope.event.actor,
            provider=provider,
            channel_id=channel_id,
        )
        await relay.push(provider, {"channel": channel_id, "text": reply_text})
        log.info("rps.dispatch relayed", platform=provider, command=command)
        return DispatchResult(transport=provider, detail=command)

    # play
    choice = payload.get("choice")
    reply_text, detail = await _handle_play(
        choice if isinstance(choice, str) else None,
        community=community,
        actor=envelope.event.actor,
        username=username,
        provider=provider,
        channel_id=channel_id,
    )
    await relay.push(provider, {"channel": channel_id, "text": reply_text})
    log.info("rps.dispatch relayed", platform=provider, command=command, detail=detail)
    return DispatchResult(transport=provider, detail=f"play:{detail}")
