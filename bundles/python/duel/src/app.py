"""`!duel <user>` -> a 1v1 challenge minigame, community-scoped, kv-only.

Net-new Waddles content -- not a port of, or inspired by, any external
project (contrast `bundles/python/fish`, which credits a specific
inspiration in its own `bundle.yaml`/module docstring). The challenge
concept, flavor text, and all game logic below are written fresh for this
bundle.

Built on the shared command grammar parser
(`waddle_sdk.command.parse_command`/`CommandSpec`, first adopted by `fish`,
#618) for the `list`/`set` sub-grammar. `!duel <user>` itself does NOT fit
that grammar directly -- the argument is a free-text challenge *target*, not
one of `waddle_sdk.command.VERBS` -- so `_resolve_command()` below first
tries the shared parser for the known `list`/`set` shapes, and falls back to
treating a single non-verb token as the challenge target when the parser
rejects it. **Known limitation, documented rather than silently papered
over**: a target username that happens to collide with a reserved verb
(`list`, `set`, `add`, `sub`, `enable`, `disable`, `remove`, `delete`,
`reset`) cannot be challenged directly via `!duel <that-name>` -- same
trade-off every grammar-based bundle in this repo accepts for its own
sub-module names.

v1 scope (KV-ONLY, no `db` capability -- same deliberate scoping as
`fish`/`lurk`/`count`):

- `!duel <user>` -- CHALLENGE. Validates `<user>` looks like a plausible
  username (`_normalize_target()`); an unrecognizable target (e.g. stray
  punctuation, empty after stripping a leading `@`) replies with a clear
  "I don't know who that is" message and touches no state -- never a
  silent drop, never a crash. A self-challenge (`<user>` normalizes to the
  caller's own `actor`) replies with a dedicated message and also touches
  no state. Otherwise: enforced by a per-caller cooldown
  (`DEFAULT_COOLDOWN_SECONDS`, admin-configurable, same TTL-is-the-expiry
  pattern as `fish`'s own cooldown), then rolls a 50/50 winner and updates
  **both** participants' win/loss records.
- `!duel list` -- reads the caller's own win/loss record from `kv`. No
  `args` accepted -- `!duel list <anything>` is a usage error, not silently
  ignored (mirrors `fish`'s own `!fish list` rule).
- `!duel set cooldown <seconds>` -- broadcaster/moderator-only (same
  `_caller_role_signal()` fail-closed pattern as `fish`/`lurk`/`count`:
  absent badge fields -- e.g. Discord's normalizer today -- deny, never
  implicit allow), persists a per-community cooldown override, bounded
  `[MIN_COOLDOWN_SECONDS, MAX_COOLDOWN_SECONDS]`.

**Cooldown scope**: per-(community, challenger) only -- the target being
challenged does not themselves need to be off cooldown to be challenged.
Mirrors `fish`'s own per-caller (not per-species/per-target) cooldown shape.

**Identity/pseudonymization**: both participants' per-user state keys are
SHA-256 pseudonyms (`_pseudonym()`), never the raw username/actor id -- same
rationale as `fish`'s own `_pseudonym()`: `event.actor` may currently be a
raw username (tokenization pipeline #429 not yet merged), so hashing before
anything ever reaches `community_kv` keeps this bundle PII-safe today and
unchanged after #429 lands. The challenger is pseudonymized from
`event.actor`; the target has no `actor` of their own in this inbound event
(only their chat-typed name), so their pseudonym is derived from the
case-folded, validated target string instead -- stable across callers
typing the same name in different casing, but NOT guaranteed to collide
with that user's own `event.actor`-derived pseudonym on a platform where the
two forms differ. Documented trade-off, not a stand-in for a real user
directory (which `kv` alone cannot provide -- see "DO NOT BUILD" below).

DO NOT BUILD in v1 -- clean, documented extension points, never a silent
stub:

- **Cross-community leaderboards / global rankings.** Every win/loss key
  here is scoped by `community_id` only (`waddle_sdk.community_kv` -- see
  its own module docstring: reputation + user-details are the platform's
  only two cross-community exceptions, and this bundle is neither). `kv`
  has no scan/list-keys primitive at all -- a leaderboard needs a real `db`
  capability with an `order_by`/pagination surface. **Deferred to a v2**
  once that capability lands (same deferral `fish` documents for its own
  leaderboard) -- no `!duel leaderboard` command declared here.
- **Real username resolution / mention validation against a roster.**
  `_normalize_target()` is a shape check (plausible username characters),
  not a lookup against an actual community member list -- `kv` has no such
  roster to check against. A target that is shape-valid but does not
  correspond to any real community member still resolves as a normal duel;
  this is a known v1 limitation, not a bug.
- Wagers/stakes, a ranking ladder, and tournaments -- out of scope for this
  kv-only v1 entirely, not partially stubbed.

Gated behind the PostHog flag ``waddles.command-duel`` -- see
`bundles/python/eightball/src/app.py`'s own docstring for the flag-gate
rationale and ordering (cheap command-match first, flag check second, real
grammar parse last).
"""

from __future__ import annotations

import hashlib
import random
import re
from typing import Any, NoReturn

from waddle_sdk import clock, community_kv, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, parse_command
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-duel"

#: No sub-modules declared for v1 -- every `VERBS` token other than
#: `list`/`set` resolves to a usage reply (see `_resolve_command`), never a
#: silent drop.
SPEC = CommandSpec(name="duel")

#: Bounds for `!duel set cooldown <seconds>` -- same rationale/bounds as
#: `fish`'s own: keeps an admin from setting something pathological (0s
#: spam, or a multi-day "cooldown" that is effectively a lockout) without a
#: second confirmation step.
DEFAULT_COOLDOWN_SECONDS = 30
MIN_COOLDOWN_SECONDS = 5
MAX_COOLDOWN_SECONDS = 3600

#: Durable per-community config -- never expires (`ttl_seconds=0`), mirrors
#: `fish`'s own `_COOLDOWN_CONFIG_KEY` convention.
_COOLDOWN_CONFIG_KEY = "duel.config.cooldown"

#: A plausible username/mention shape: an optional leading `@` (stripped),
#: then 1-32 chars of letters/digits/underscore/dot/hyphen. Not a lookup
#: against a real roster -- see module docstring's "DO NOT BUILD" section.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}$")

_USAGE = "Usage: !duel <user> | !duel list | !duel set cooldown <seconds> (set is admin/mod only)"
_PERMISSION_DENIED_MSG = "only moderators/broadcasters can configure !duel"
_NO_RECORD_YET = "You haven't dueled anyone yet -- try !duel <user>!"

_KNOWN_COMMANDS = frozenset({"challenge", "list", "config_set_cooldown", "usage"})

#: Original flavor text for the duel's resolution line -- written fresh for
#: this bundle (module docstring).
_OUTCOME_FLAVOR: tuple[str, ...] = (
    "swords clash in a blur of steel",
    "it comes down to the very last exchange",
    "a dramatic standoff ends in one swift strike",
    "the crowd gasps as the duel reaches its climax",
    "neither backs down until the final blow",
    "a clever feint decides it at the last second",
)


def _pseudonym(identity: str | None) -> str:
    """Non-reversible per-identity key component -- see `fish`'s own `_pseudonym()` for why.

    `identity` may be a raw username (tokenization pipeline #429 not yet
    merged); hashing it before it ever reaches `community_kv` keeps this
    bundle PII-safe today and after #429 lands unchanged.
    """
    return hashlib.sha256((identity or "anonymous").encode()).hexdigest()


def _wins_key(pseudonym: str) -> str:
    """Per-(community, participant) total-wins counter key."""
    return f"duel.wins.{pseudonym}"


def _losses_key(pseudonym: str) -> str:
    """Per-(community, participant) total-losses counter key."""
    return f"duel.losses.{pseudonym}"


def _lastduel_key(pseudonym: str) -> str:
    """Per-(community, challenger) last-challenge-timestamp key (the cooldown gate)."""
    return f"duel.lastduel.{pseudonym}"


def _format_duration(total_seconds: int) -> str:
    """Human-readable duration, e.g. "2m 5s" -- two largest nonzero units (see `fish`'s own)."""
    total_seconds = max(total_seconds, 0)
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    nonzero = [(v, s) for v, s in ((hours, "h"), (minutes, "m"), (seconds, "s")) if v > 0]
    if not nonzero:
        return "0s"
    return " ".join(f"{v}{s}" for v, s in nonzero[:2])


def _normalize_target(raw: str) -> str | None:
    """Strip an optional leading `@` and validate the shape; `None` if not a plausible username.

    Shape check only -- not a lookup against a real community roster (`kv`
    has no such primitive; see module docstring's "DO NOT BUILD" section).
    """
    candidate = raw[1:] if raw.startswith("@") else raw
    if not candidate or not _USERNAME_RE.match(candidate):
        return None
    return candidate


def _caller_role_signal(payload: dict[str, Any]) -> bool | None:
    """`True`/`False` from the normalized event's own badge fields, or `None` if absent.

    See `fish`/`count`/`lurk`'s own identical helper -- `None` (neither
    `is_mod`/`is_broadcaster` present, e.g. Discord's normalizer today) must
    be treated as denied, never as an implicit allow.
    """
    if "is_mod" not in payload and "is_broadcaster" not in payload:
        return None
    return bool(payload.get("is_mod")) or bool(payload.get("is_broadcaster"))


def _resolve_command(stripped: str) -> tuple[str, str | None]:
    """Classify a full `!duel ...` message into this bundle's own command set.

    Returns `(command, arg)`: `arg` is `set`'s own free-text tail for
    `config_set_cooldown`, the raw (unvalidated) challenge target for
    `challenge`, or `None` otherwise. Target-shape validation
    (`_normalize_target`) and self-challenge detection happen in `dispatch`,
    which owns all kv/state logic -- this function only classifies
    structure, same split `fish` uses between `transform`/`dispatch`.
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

    # Not a recognized verb -- a single bare token is the challenge target; two or more
    # tokens ("!duel foo bar") is not a valid shape for either grammar.
    tok1, _, tail = rest.partition(" ")
    if tail.strip():
        return "usage", None
    return "challenge", tok1


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!duel` and its grammar.

    Cheap-skip first (no leading `!duel` token -- `None`, zero cost), flag
    check second, real grammar classification last -- same ordering as
    `fish`/`eightball`'s own documented rationale. A recognized-but-malformed
    `!duel ...` still produces a reply (`"usage"`) since the caller did
    invoke this command -- never silently dropped.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None

    stripped = text.strip()
    head = stripped.partition(" ")[0]
    if head.lower() != "!duel":
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    command, arg = _resolve_command(stripped)

    log.info("duel.transform matched", command=command)
    payload: dict[str, Any] = {"command": command, "channel_id": event.payload.get("channel_id")}
    if command == "config_set_cooldown":
        payload["arg"] = arg
    elif command == "challenge":
        payload["target"] = arg
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


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result -- see `pyping`'s own `app.py`."""

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
    log.error("duel.kv_error", op=op, error=case_name)
    await relay.push(
        provider,
        {"channel": channel_id, "text": "dueling is temporarily unavailable, try again shortly."},
    )
    raise RuntimeError(f"duel kv {op} failed: {case_name}") from exc


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
        log.error("duel.cooldown_config_corrupt", community=community)
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


async def _handle_challenge(
    raw_target: str,
    *,
    community: str,
    actor: str | None,
    username: str,
    provider: str,
    channel_id: str,
) -> tuple[str, str]:
    """Validate the target, enforce the challenger's cooldown, roll a winner, update W/L.

    Returns `(reply_text, detail)` -- `detail` feeds `DispatchResult.detail`
    as `challenge:<detail>` (`"unknown_target"`, `"self"`, `"cooldown"`, or
    `"resolved"`).
    """
    normalized = _normalize_target(raw_target)
    if normalized is None:
        return (
            f"I don't know who '{raw_target}' is -- try !duel @username.",
            "unknown_target",
        )

    if actor is not None and normalized.lower() == actor.lower():
        return "you can't duel yourself!", "self"

    pseudonym = _pseudonym(actor)
    cooldown = await _get_cooldown(community, provider=provider, channel_id=channel_id)
    last_raw = await _kv_get(
        community, _lastduel_key(pseudonym), provider=provider, channel_id=channel_id
    )
    now_ms = clock.now_millis()

    if last_raw is not None:
        try:
            last_ms: int | None = int(last_raw.decode())
        except (UnicodeDecodeError, ValueError):
            log.error("duel.cooldown_state_corrupt", community=community)
            last_ms = None
        if last_ms is not None:
            remaining_s = cooldown - (now_ms - last_ms) / 1000
            if remaining_s > 0:
                wait_for = _format_duration(max(1, round(remaining_s)))
                return (
                    f"⚔️ slow down, {username}! try again in {wait_for}.",
                    "cooldown",
                )

    await _kv_set(
        community,
        _lastduel_key(pseudonym),
        str(now_ms).encode(),
        ttl_seconds=cooldown,
        provider=provider,
        channel_id=channel_id,
    )

    opponent_pseudonym = _pseudonym(normalized.lower())
    challenger_wins = random.random() < 0.5  # noqa: S311 -- a game, not a security decision

    if challenger_wins:
        winner_name, winner_pseudonym, loser_pseudonym = username, pseudonym, opponent_pseudonym
    else:
        winner_name, winner_pseudonym, loser_pseudonym = normalized, opponent_pseudonym, pseudonym

    await _kv_increment(
        community,
        _wins_key(winner_pseudonym),
        1,
        ttl_seconds=0,
        provider=provider,
        channel_id=channel_id,
    )
    await _kv_increment(
        community,
        _losses_key(loser_pseudonym),
        1,
        ttl_seconds=0,
        provider=provider,
        channel_id=channel_id,
    )

    flavor = random.choice(_OUTCOME_FLAVOR)  # noqa: S311 -- a game, not a security decision
    reply = f"⚔️ {username} challenges {normalized} to a duel... {flavor}! {winner_name} wins!"
    return reply, "resolved"


async def _handle_list(*, community: str, actor: str | None, provider: str, channel_id: str) -> str:
    """Read+render the caller's own win/loss record."""
    pseudonym = _pseudonym(actor)
    wins = await _read_int(
        community,
        _wins_key(pseudonym),
        provider=provider,
        channel_id=channel_id,
        corrupt_log="duel.wins_corrupt",
    )
    losses = await _read_int(
        community,
        _losses_key(pseudonym),
        provider=provider,
        channel_id=channel_id,
        corrupt_log="duel.losses_corrupt",
    )
    if wins == 0 and losses == 0:
        return _NO_RECORD_YET
    return f"Record: {wins}W - {losses}L."


async def _handle_set_cooldown(
    arg: str | None, *, community: str, provider: str, channel_id: str
) -> str:
    """Parse+apply `!duel set cooldown <seconds>`'s own free-text `args` tail."""
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
        return f"cooldown must be between {MIN_COOLDOWN_SECONDS} and {MAX_COOLDOWN_SECONDS} seconds"
    await _kv_set(
        community,
        _COOLDOWN_CONFIG_KEY,
        str(seconds).encode(),
        ttl_seconds=0,
        provider=provider,
        channel_id=channel_id,
    )
    return f"duel cooldown set to {seconds}s"


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: all kv state reads/writes, then relay the reply.

    Raises:
        ValueError: The envelope's payload has no `channel_id`; the
            envelope has no `community` (no tenant-wide fallback -- see
            module docstring's data-scoping section); an unrecognized
            `command` (defensive -- `transform` only ever emits a member of
            `_KNOWN_COMMANDS`); or a `"challenge"` command with no `target`
            in its forwarded payload (defensive -- `transform` always sets
            one for that command).
        RuntimeError: A `kv` backend call failed (see `_fail_kv` -- a chat
            error reply and an ERROR log line are always emitted first).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("duel reply requires a channel_id from the inbound chat.message")
    command = payload.get("command")
    if command not in _KNOWN_COMMANDS:
        raise ValueError(f"unrecognized duel command: {command!r}")

    provider = envelope.event.platform
    community = envelope.community
    if not community:
        log.error("duel.missing_community", command=command)
        raise ValueError("duel requires a community context and cannot operate tenant-wide")

    username = envelope.event.actor or "someone"

    if command == "usage":
        await relay.push(provider, {"channel": channel_id, "text": _USAGE})
        return DispatchResult(transport=provider, detail="usage")

    if command == "config_set_cooldown":
        role_signal = _caller_role_signal(payload)
        if role_signal is not True:
            log.info("duel.config_denied", command=command, role_signal=str(role_signal))
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
        log.info("duel.dispatch config applied", command=command)
        return DispatchResult(transport=provider, detail=command)

    if command == "list":
        reply_text = await _handle_list(
            community=community,
            actor=envelope.event.actor,
            provider=provider,
            channel_id=channel_id,
        )
        await relay.push(provider, {"channel": channel_id, "text": reply_text})
        log.info("duel.dispatch relayed", platform=provider, command=command)
        return DispatchResult(transport=provider, detail=command)

    # challenge
    target = payload.get("target")
    if not isinstance(target, str) or not target:
        raise ValueError("duel challenge requires a target in the forwarded payload")
    reply_text, detail = await _handle_challenge(
        target,
        community=community,
        actor=envelope.event.actor,
        username=username,
        provider=provider,
        channel_id=channel_id,
    )
    await relay.push(provider, {"channel": channel_id, "text": reply_text})
    log.info("duel.dispatch relayed", platform=provider, command=command, detail=detail)
    return DispatchResult(transport=provider, detail=f"challenge:{detail}")
