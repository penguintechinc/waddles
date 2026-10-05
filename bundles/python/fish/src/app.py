"""`!fish` -> a weighted-random fishing minigame, community-scoped, kv-only.

Inspiration credit (not a literal port -- see `bundle.yaml`'s `author`/
`notice`): the general shape of "cast a line, roll a weighted-rarity catch,
track a running total/biggest catch" is inspired by superpenguintv
(Psychoboy)'s `PenguinTwitchBot` fishing feature
(https://github.com/Psychoboy/PenguinTwitchBot). No original source code or
text is reused here -- the catch table, flavor text, weights, and all game
logic below are written fresh for Waddles, so no MIT notice reproduction is
required (`sdk/waddle-sdk/AUTHORING.md`'s attribution convention: verbatim
reuse needs the license text inline, inspiration-only needs credit only).
See `docs/superpowers/specs/2026-09-28-superpenguin-fish-game-port.md` for
the much larger, DB-backed, multi-bundle port this is deliberately NOT
attempting -- this bundle is a self-contained v1 slice buildable entirely on
today's live `kv` capability.

First production bundle to use the shared command grammar parser
(`waddle_sdk.command.parse_command`/`CommandSpec`, merged in #618) instead of
hand-rolled `text.split()`/regex parsing -- `lurk`/`count`/`roll` all predate
it (see each of their own docstrings) and are documented as follow-up
rewrites, not required by `AUTHORING.md`. `!fish` declares no sub-modules.

v1 scope (KV-ONLY, no `db` capability -- deliberately does not depend on the
in-flight #623 db API):

- Bare `!fish` -- CAST, the grammar's bare/default action (mirrors `!count`'s
  bare-increments-and-replies convention): rolls one weighted-rarity catch,
  updates the caller's running total (`kv.increment`) and biggest-catch
  record, and replies with the catch plus the new total. Enforced by a
  per-(community, caller) cooldown (`DEFAULT_COOLDOWN_SECONDS`, admin-
  configurable) stored as a plain kv timestamp with `ttl_seconds=cooldown` --
  the TTL itself is the auto-expiry, no separate sweep needed (same pattern
  as `lurk`'s own 24h lurk-state TTL).
- `!fish list` -- reads the caller's own stats (total catches, biggest catch)
  from `kv`. No `args` accepted -- `!fish list <anything>` is a usage error,
  not silently ignored.
- `!fish set cooldown <seconds>` -- broadcaster/moderator-only (same
  `_caller_role_signal()` fail-closed pattern as `lurk`/`count`: absent badge
  fields -- e.g. Discord's normalizer today -- deny, never implicit allow),
  persists a per-community cooldown override, bounded
  `[MIN_COOLDOWN_SECONDS, MAX_COOLDOWN_SECONDS]`.

DO NOT BUILD in v1 -- clean, documented extension points, never a silent
stub:

- **Cross-community leaderboards.** Every catch/stat key here is scoped by
  `community_id` only (`waddle_sdk.community_kv` -- see its own module
  docstring: reputation/user-details are the platform's only two cross-
  community exceptions, and this bundle is neither). A global or per-tenant
  leaderboard needs a query that spans communities -- `ORDER BY` over many
  rows -- which only a real `db` capability with an `order_by`/pagination
  surface can answer well; `kv` has no scan/list-keys primitive at all.
  **Deferred to a v2 once the in-flight #623 `db` order_by API lands** (see
  this bundle's own PR description) -- not built here, not stubbed, no
  `!fish leaderboard` command declared.
- Rod/line "snap" (equipment loss), a shop/economy, and tournaments -- all
  from the much larger C#/DB-backed port spec
  (`docs/superpowers/specs/2026-09-28-superpenguin-fish-game-port.md`) --
  are out of scope for this kv-only v1 entirely, not partially stubbed.

Gated behind the PostHog flag ``waddles.command-fish`` -- see
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

FLAG_KEY = "waddles.command-fish"

#: No sub-modules declared for v1 -- every `VERBS` token other than
#: `list`/`set` resolves to a usage reply (see `_resolve_command`), never a
#: silent drop.
SPEC = CommandSpec(name="fish")

#: Bounds for `!fish set cooldown <seconds>` -- keeps an admin from setting
#: something pathological (0s spam, or a multi-day "cooldown" that is
#: effectively a lockout) without a second confirmation step.
DEFAULT_COOLDOWN_SECONDS = 60
MIN_COOLDOWN_SECONDS = 5
MAX_COOLDOWN_SECONDS = 3600

#: Durable per-community config -- never expires (`ttl_seconds=0`), mirrors
#: `lurk`'s own `_CONFIG_TTL_SECONDS` convention.
_COOLDOWN_CONFIG_KEY = "fish:config:cooldown"

_USAGE = "Usage: !fish | !fish list | !fish set cooldown <seconds> (set is admin/mod only)"
_PERMISSION_DENIED_MSG = "only moderators/broadcasters can configure !fish"
_NOTHING_CAUGHT_YET = "You haven't caught any fish yet -- try !fish!"

_KNOWN_COMMANDS = frozenset({"cast", "list", "config_set_cooldown", "usage"})


@dataclass(slots=True, frozen=True)
class FishCatch:
    """One entry in the weighted catch table -- original flavor/weights, see module docstring."""

    name: str
    rarity: str
    base_weight_lbs: float
    flavor: str


#: Weighted catch table. Weights are relative (`random.choices` normalizes
#: them), not percentages -- original content written for this bundle, NOT
#: copied from PenguinTwitchBot's own fish list (module docstring).
_CATCH_TABLE: tuple[tuple[FishCatch, float], ...] = (
    (FishCatch("Old Boot", "junk", 1.0, "just an old boot -- better luck next cast"), 20.0),
    (FishCatch("Minnow", "common", 0.2, "a tiny minnow flops onto the dock"), 35.0),
    (FishCatch("Catfish", "common", 4.5, "a whiskered catfish wriggles up"), 35.0),
    (FishCatch("Bass", "uncommon", 3.0, "a scrappy bass puts up a fight"), 20.0),
    (FishCatch("Trout", "uncommon", 2.2, "a speckled trout slips out of the water"), 20.0),
    (FishCatch("Pike", "rare", 8.0, "a toothy pike nearly takes the rod with it"), 8.0),
    (FishCatch("Sturgeon", "rare", 25.0, "an ancient sturgeon surfaces, scales gleaming"), 8.0),
    (FishCatch("Golden Koi", "epic", 6.0, "a shimmering golden koi -- a lucky catch!"), 3.0),
    (FishCatch("Kraken Spawn", "legendary", 60.0, "something ancient breaks the surface!"), 1.0),
)

#: Randomizes the actual weight around each catch's base weight so two
#: catches of the same species aren't always byte-identical.
_WEIGHT_VARIANCE_MIN = 0.8
_WEIGHT_VARIANCE_MAX = 1.2


def _roll_catch() -> tuple[FishCatch, float]:
    """Pick one weighted-random `FishCatch` and a randomized actual weight (lbs, 2dp)."""
    fish = random.choices(  # noqa: S311 - a game, not a security decision
        [entry[0] for entry in _CATCH_TABLE],
        weights=[entry[1] for entry in _CATCH_TABLE],
        k=1,
    )[0]
    multiplier = random.uniform(_WEIGHT_VARIANCE_MIN, _WEIGHT_VARIANCE_MAX)  # noqa: S311
    return fish, round(fish.base_weight_lbs * multiplier, 2)


def _pseudonym(actor: str | None) -> str:
    """Non-reversible per-caller key component -- see `lurk`'s own `_state_key()` for why.

    `event.actor` may currently be a raw username (tokenization pipeline
    #429 not yet merged); hashing it before it ever reaches `community_kv`
    keeps this bundle PII-safe today and after #429 lands unchanged.
    """
    return hashlib.sha256((actor or "anonymous").encode()).hexdigest()


def _count_key(pseudonym: str) -> str:
    """Per-(community, caller) total-catches counter key."""
    return f"fish:count:{pseudonym}"


def _lastcast_key(pseudonym: str) -> str:
    """Per-(community, caller) last-cast-timestamp key (the cooldown gate)."""
    return f"fish:lastcast:{pseudonym}"


def _biggest_key(pseudonym: str) -> str:
    """Per-(community, caller) biggest-catch record key (JSON: name/rarity/weight_lbs)."""
    return f"fish:biggest:{pseudonym}"


def _format_duration(total_seconds: int) -> str:
    """Human-readable duration, e.g. "2m 5s" -- two largest nonzero units (see `lurk`'s own)."""
    total_seconds = max(total_seconds, 0)
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    nonzero = [(v, s) for v, s in ((hours, "h"), (minutes, "m"), (seconds, "s")) if v > 0]
    if not nonzero:
        return "0s"
    return " ".join(f"{v}{s}" for v, s in nonzero[:2])


def _caller_role_signal(payload: dict[str, Any]) -> bool | None:
    """`True`/`False` from the normalized event's own badge fields, or `None` if absent.

    See `count`/`lurk`'s own identical helper -- `None` (neither
    `is_mod`/`is_broadcaster` present, e.g. Discord's normalizer today) must
    be treated as denied, never as an implicit allow.
    """
    if "is_mod" not in payload and "is_broadcaster" not in payload:
        return None
    return bool(payload.get("is_mod")) or bool(payload.get("is_broadcaster"))


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!fish` and its grammar, via `parse_command`.

    Cheap-skip first (no leading `!fish` token -- `None`, zero cost), flag
    check second, real grammar parse last -- same ordering as `eightball`'s
    own documented rationale. A recognized-but-malformed `!fish ...` (a
    `CommandUsageError`, or an option this bundle doesn't implement, e.g.
    `!fish enable`) still produces a reply (`"usage"`) since the caller did
    invoke this command -- never silently dropped.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None

    stripped = text.strip()
    head = stripped.partition(" ")[0]
    if head.lower() != "!fish":
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    try:
        parsed: ParsedCommand | None = parse_command(stripped, SPEC)
    except CommandUsageError:
        parsed = None
    command = _resolve_command(parsed)

    log.info("fish.transform matched", command=command)
    payload: dict[str, Any] = {"command": command, "channel_id": event.payload.get("channel_id")}
    if command == "config_set_cooldown" and parsed is not None:
        payload["arg"] = parsed.args
    # Forward the normalized badge signal, if present -- see `count`/`lurk`'s own identical
    # forwarding comment for why absence must reach `dispatch` as absence, not `False`.
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
    none of which this bundle declares sub-modules or behavior for) resolves
    to `"usage"`, same fail-loud-never-silent rule as a parse error itself.
    """
    if parsed is None:
        return "usage"
    if parsed.option is None:
        return "cast"
    if parsed.option == "list":
        return "list" if parsed.args is None else "usage"
    if parsed.option == "set":
        return "config_set_cooldown"
    return "usage"


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
    """Fail-loud kv error path: log, reply an error to chat, then re-raise -- see `lurk`'s own."""
    wit_error = getattr(exc, "value", exc)
    case_name = type(wit_error).__name__
    log.error("fish.kv_error", op=op, error=case_name)
    await relay.push(
        provider,
        {"channel": channel_id, "text": "fishing is temporarily unavailable, try again shortly."},
    )
    raise RuntimeError(f"fish kv {op} failed: {case_name}") from exc


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
        log.error("fish.cooldown_config_corrupt", community=community)
        return DEFAULT_COOLDOWN_SECONDS


async def _maybe_update_biggest(
    community: str,
    pseudonym: str,
    fish: FishCatch,
    weight_lbs: float,
    *,
    provider: str,
    channel_id: str,
) -> None:
    """Overwrite the caller's biggest-catch record if `weight_lbs` beats the stored one."""
    raw = await _kv_get(
        community, _biggest_key(pseudonym), provider=provider, channel_id=channel_id
    )
    if raw is not None:
        try:
            current = json.loads(raw.decode())
            if float(current["weight_lbs"]) >= weight_lbs:
                return
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            log.error("fish.biggest_corrupt", community=community)
    record = json.dumps(
        {"name": fish.name, "rarity": fish.rarity, "weight_lbs": weight_lbs}
    ).encode()
    await _kv_set(
        community,
        _biggest_key(pseudonym),
        record,
        ttl_seconds=0,
        provider=provider,
        channel_id=channel_id,
    )


async def _handle_cast(
    *, community: str, actor: str | None, username: str, provider: str, channel_id: str
) -> str:
    """Enforce the per-caller cooldown, then roll+persist a catch and build the reply."""
    pseudonym = _pseudonym(actor)
    cooldown = await _get_cooldown(community, provider=provider, channel_id=channel_id)
    last_raw = await _kv_get(
        community, _lastcast_key(pseudonym), provider=provider, channel_id=channel_id
    )
    now_ms = clock.now_millis()

    if last_raw is not None:
        try:
            last_ms: int | None = int(last_raw.decode())
        except (UnicodeDecodeError, ValueError):
            log.error("fish.cooldown_state_corrupt", community=community)
            last_ms = None
        if last_ms is not None:
            remaining_s = cooldown - (now_ms - last_ms) / 1000
            if remaining_s > 0:
                wait_for = _format_duration(max(1, round(remaining_s)))
                return f"\U0001f3a3 slow down, {username}! try again in {wait_for}."

    await _kv_set(
        community,
        _lastcast_key(pseudonym),
        str(now_ms).encode(),
        ttl_seconds=cooldown,
        provider=provider,
        channel_id=channel_id,
    )

    fish, weight_lbs = _roll_catch()
    total = await _kv_increment(
        community,
        _count_key(pseudonym),
        1,
        ttl_seconds=0,
        provider=provider,
        channel_id=channel_id,
    )
    await _maybe_update_biggest(
        community, pseudonym, fish, weight_lbs, provider=provider, channel_id=channel_id
    )

    return (
        f"\U0001f3a3 {username} casts a line... {fish.flavor}! "
        f"Caught a {fish.rarity} {fish.name} ({weight_lbs:.1f} lbs). Total catches: {total}."
    )


async def _handle_list(
    *, community: str, actor: str | None, provider: str, channel_id: str
) -> str:
    """Read+render the caller's own total-catches and biggest-catch stats."""
    pseudonym = _pseudonym(actor)
    total_raw = await _kv_get(
        community, _count_key(pseudonym), provider=provider, channel_id=channel_id
    )
    total = 0
    if total_raw is not None:
        try:
            total = int(total_raw.decode())
        except (UnicodeDecodeError, ValueError):
            log.error("fish.count_corrupt", community=community)
            total = 0

    if total == 0:
        return _NOTHING_CAUGHT_YET

    biggest_raw = await _kv_get(
        community, _biggest_key(pseudonym), provider=provider, channel_id=channel_id
    )
    biggest_text = "nothing yet"
    if biggest_raw is not None:
        try:
            data = json.loads(biggest_raw.decode())
            biggest_text = f"{data['rarity']} {data['name']} ({float(data['weight_lbs']):.1f} lbs)"
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            log.error("fish.biggest_corrupt", community=community)

    return f"Total catches: {total}. Biggest catch: {biggest_text}."


async def _handle_set_cooldown(
    arg: str | None, *, community: str, provider: str, channel_id: str
) -> str:
    """Parse+apply `!fish set cooldown <seconds>`'s own free-text `args` tail."""
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
    return f"fish cooldown set to {seconds}s"


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
        raise ValueError("fish reply requires a channel_id from the inbound chat.message")
    command = payload.get("command")
    if command not in _KNOWN_COMMANDS:
        raise ValueError(f"unrecognized fish command: {command!r}")

    provider = envelope.event.platform
    community = envelope.community
    if not community:
        log.error("fish.missing_community", command=command)
        raise ValueError("fish requires a community context and cannot operate tenant-wide")

    username = envelope.event.actor or "someone"

    if command == "usage":
        await relay.push(provider, {"channel": channel_id, "text": _USAGE})
        return DispatchResult(transport=provider, detail="usage")

    if command == "config_set_cooldown":
        role_signal = _caller_role_signal(payload)
        if role_signal is not True:
            log.info("fish.config_denied", command=command, role_signal=str(role_signal))
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
        log.info("fish.dispatch config applied", command=command)
        return DispatchResult(transport=provider, detail=command)

    if command == "list":
        reply_text = await _handle_list(
            community=community,
            actor=envelope.event.actor,
            provider=provider,
            channel_id=channel_id,
        )
    else:  # cast
        reply_text = await _handle_cast(
            community=community,
            actor=envelope.event.actor,
            username=username,
            provider=provider,
            channel_id=channel_id,
        )

    await relay.push(provider, {"channel": channel_id, "text": reply_text})
    log.info("fish.dispatch relayed", platform=provider, command=command)
    return DispatchResult(transport=provider, detail=command)
