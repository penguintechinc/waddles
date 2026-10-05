"""`!roulette` -> a flavor-only russian-roulette chat minigame, community-scoped, kv-only.

Net-new fun content for Waddles -- no inspiration source, no attribution
required (contrast `bundles/python/fish`, which credits superpenguintv
(Psychoboy)'s `PenguinTwitchBot` for the catch-and-track concept; this
bundle's chamber odds and flavor text are original).

Uses the shared command grammar parser (`waddle_sdk.command.parse_command`/
`CommandSpec`, merged in #618) -- same pattern as `slots`/`fish`'s own
`app.py`.

v1 scope (KV-ONLY, no `db` capability -- same deliberate scoping as
`slots`/`fish`, not dependent on the in-flight #623 db API):

- Bare `!roulette` -- PULL, the grammar's bare/default action. Spins a
  six-chamber cylinder with one round loaded (`_pull_trigger()`,
  `random.randint(1, CHAMBER_COUNT) == 1`) -- a 1-in-6 "out", 5-in-6
  "survive". Updates the caller's running pull count and survive/out
  totals. Enforced by a per-(community, caller) cooldown
  (`DEFAULT_COOLDOWN_SECONDS`, admin-configurable) stored as a plain kv
  timestamp with `ttl_seconds=cooldown` -- the TTL itself is the
  auto-expiry, same pattern as `slots`'s own `slots:lastspin:<pseudonym>`
  (`.`-separated here, see the kv-charset note below). `cooldown == 0`
  (the bound's lower edge -- see below) disables the gate entirely and
  skips persisting a last-pull timestamp at all, rather than writing one
  with `ttl_seconds=0` (which would mean "never expires" in
  `waddle_sdk.kv` -- an unbounded-growth key for a cooldown that by
  definition never fires). This is a deliberate, documented divergence
  from `slots`'s blind "always write the timestamp" shape.
- `!roulette list` -- reads the caller's own survive/out record (total
  pulls, survives, outs, survival rate) from `kv`. No `args` accepted --
  `!roulette list <anything>` is a usage error, not silently ignored.
- `!roulette set cooldown <seconds>` -- broadcaster/moderator-only (same
  `_caller_role_signal()` fail-closed pattern as `slots`/`fish`/`lurk`/
  `count`: absent badge fields -- e.g. Discord's normalizer today -- deny,
  never implicit allow), persists a per-community cooldown override,
  bounded `[MIN_COOLDOWN_SECONDS, MAX_COOLDOWN_SECONDS]` = `[0, 3600]` --
  note the lower bound is `0` here (unlike `slots`'s `5`): an admin may
  deliberately disable the cooldown for a fast-paced party-game moment.

**No real chat timeout/ban.** An "out" outcome here is flavor text and a
stat increment ONLY -- this bundle never calls any moderation/timeout/ban
API, on any platform. Actually removing a user from chat requires
mod-scope capabilities (Twitch `moderator:manage:banned_users`, Discord
`MODERATE_MEMBERS`, etc.) this bundle does not request, declare in
`bundle.yaml` `permissions`, or have any access path to -- `relay.push` is
the only outbound capability granted (`storage.kv` is the only
`permissions` entry), and `relay.push` only ever delivers a chat message,
never a moderation action. This is a deliberate v1 boundary, not a gap:
building real enforcement would require a new `moderation` WIT capability
that does not exist yet, plus an explicit opt-in step for a broadcaster who
may not want their bot taking real moderation actions on their behalf.

**No real-money/currency ties**, same rule as `slots`: no wallet, balance,
transfer, or redemption path anywhere in this bundle, and none is planned
for a v2. This is a chat game, not a gambling economy feature.

**kv key charset (gh-631).** Every key this module builds uses `.` as its
internal separator, never `:` -- the real `kv` host capability
(`core/bundle_host_kv/src/scope.rs::is_allowed_key_byte`) rejects `:` as a
guest-key byte (it is the host's own reserved namespace separator), and
`waddle_sdk.kv.validate_key`/`waddle_sdk.community_kv._scoped_key` enforce
this at the SDK boundary. `slots`'s own `slots:spins:<pseudonym>`-shaped
keys predate this fix and are NOT the pattern to copy; this bundle's own
`tests/test_app.py` uses the shared, charset-enforcing
`waddle_sdk.testing.install_fake_kv_host` fake precisely so a `:` typo here
would fail the test suite immediately instead of only failing on the real
host in production.

DO NOT BUILD in v1 -- clean, documented extension points, never a silent
stub:

- **Cross-community leaderboards.** Every pull/stat key here is scoped by
  `community_id` only (`waddle_sdk.community_kv` -- see its own module
  docstring: reputation/user-details are the platform's only two
  cross-community exceptions, and this bundle is neither). A global or
  per-tenant leaderboard needs a query spanning communities, which only a
  real `db` capability with an `order_by`/pagination surface can answer
  well -- `kv` has no scan/list-keys primitive. **Deferred to a v2 once the
  in-flight #623 `db` order_by API lands**, same deferral as
  `slots`/`fish`'s own -- not built here, not stubbed, no
  `!roulette leaderboard` command declared.
- Any real moderation/timeout/ban action -- out of scope per the
  flavor-only rule above, not partially stubbed (see module docstring
  section above).
- Any wallet/points-economy integration -- out of scope per the
  no-currency-ties rule above, not partially stubbed.

Gated behind the PostHog flag ``waddles.command-roulette`` -- see
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

FLAG_KEY = "waddles.command-roulette"

#: No sub-modules declared for v1 -- every `VERBS` token other than
#: `list`/`set` resolves to a usage reply (see `_resolve_command`), never a
#: silent drop.
SPEC = CommandSpec(name="roulette")

#: Classic six-chamber revolver, one round loaded -- a 1-in-6 "out" per pull.
CHAMBER_COUNT = 6

#: Bounds for `!roulette set cooldown <seconds>` -- `0` (unlike `slots`'s
#: `5`) deliberately allows an admin to disable the cooldown entirely for a
#: fast-paced party-game moment; `3600` keeps a pathological multi-day
#: "cooldown" (effectively a lockout) out of reach without a second
#: confirmation step. Mirrors `slots`'s own bounds convention otherwise.
DEFAULT_COOLDOWN_SECONDS = 30
MIN_COOLDOWN_SECONDS = 0
MAX_COOLDOWN_SECONDS = 3600

#: Durable per-community config -- never expires (`ttl_seconds=0`), mirrors
#: `slots`'s own `_COOLDOWN_CONFIG_KEY` convention. `.`-separated, not `:`
#: (gh-631 -- see module docstring's kv-charset note).
_COOLDOWN_CONFIG_KEY = "roulette.config.cooldown"

_USAGE = (
    "Usage: !roulette | !roulette list | !roulette set cooldown <seconds> "
    "(set is admin/mod only)"
)
_PERMISSION_DENIED_MSG = "only moderators/broadcasters can configure !roulette"
_NOTHING_PULLED_YET = "You haven't pulled the trigger yet -- try !roulette!"

_KNOWN_COMMANDS = frozenset({"pull", "list", "config_set_cooldown", "usage"})

_SURVIVE_FLAVORS: tuple[str, ...] = (
    "*click* -- empty chamber. You live to see another day!",
    "*click* -- lucky! Try your luck again sometime.",
    "*click* -- nothing happens. Nerves of steel.",
)

_OUT_FLAVORS: tuple[str, ...] = (
    "*BANG!* You're out! (flavor only -- no real timeout, just bragging rights)",
    "*BANG!* Down you go! (don't worry, this is a stats-only game -- nothing actually happened)",
)


def _pull_trigger() -> bool:
    """Spin the cylinder and pull the trigger. Return `True` if this pull is "out"."""
    return random.randint(1, CHAMBER_COUNT) == 1  # noqa: S311 - a game, not security


def _pseudonym(actor: str | None) -> str:
    """Non-reversible per-caller key component -- see `slots`/`fish`'s own `_pseudonym()` for why.

    `event.actor` may currently be a raw username (tokenization pipeline
    #429 not yet merged); hashing it before it ever reaches `community_kv`
    keeps this bundle PII-safe today and after #429 lands unchanged.
    """
    return hashlib.sha256((actor or "anonymous").encode()).hexdigest()


def _pulls_key(pseudonym: str) -> str:
    """Per-(community, caller) total-pulls counter key."""
    return f"roulette.pulls.{pseudonym}"


def _survives_key(pseudonym: str) -> str:
    """Per-(community, caller) total-survives counter key."""
    return f"roulette.survives.{pseudonym}"


def _outs_key(pseudonym: str) -> str:
    """Per-(community, caller) total-outs counter key."""
    return f"roulette.outs.{pseudonym}"


def _lastpull_key(pseudonym: str) -> str:
    """Per-(community, caller) last-pull-timestamp key (the cooldown gate)."""
    return f"roulette.lastpull.{pseudonym}"


def _format_duration(total_seconds: int) -> str:
    """Human-readable duration, e.g. "2m 5s" -- two largest nonzero units (see `slots`'s own)."""
    total_seconds = max(total_seconds, 0)
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    nonzero = [(v, s) for v, s in ((hours, "h"), (minutes, "m"), (seconds, "s")) if v > 0]
    if not nonzero:
        return "0s"
    return " ".join(f"{v}{s}" for v, s in nonzero[:2])


def _caller_role_signal(payload: dict[str, Any]) -> bool | None:
    """`True`/`False` from the normalized event's own badge fields, or `None` if absent.

    See `slots`/`fish`/`count`/`lurk`'s own identical helper -- `None`
    (neither `is_mod`/`is_broadcaster` present, e.g. Discord's normalizer
    today) must be treated as denied, never as an implicit allow.
    """
    if "is_mod" not in payload and "is_broadcaster" not in payload:
        return None
    return bool(payload.get("is_mod")) or bool(payload.get("is_broadcaster"))


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!roulette` and its grammar.

    Cheap-skip first (no leading `!roulette` token -- `None`, zero cost),
    flag check second, real grammar parse last -- same ordering as
    `slots`/`fish`'s own documented rationale. A recognized-but-malformed
    `!roulette ...` (a `CommandUsageError`, or an option this bundle
    doesn't implement, e.g. `!roulette enable`) still produces a reply
    (`"usage"`) since the caller did invoke this command -- never silently
    dropped.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None

    stripped = text.strip()
    head = stripped.partition(" ")[0]
    if head.lower() != "!roulette":
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    try:
        parsed: ParsedCommand | None = parse_command(stripped, SPEC)
    except CommandUsageError:
        parsed = None
    command = _resolve_command(parsed)

    log.info("roulette.transform matched", command=command)
    payload: dict[str, Any] = {"command": command, "channel_id": event.payload.get("channel_id")}
    if command == "config_set_cooldown" and parsed is not None:
        payload["arg"] = parsed.args
    # Forward the normalized badge signal, if present -- see `slots`/`fish`/`count`/`lurk`'s own
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
        return "pull"
    if parsed.option == "list":
        return "list" if parsed.args is None else "usage"
    if parsed.option == "set":
        return "config_set_cooldown"
    return "usage"


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result -- see `pyping`/`slots`'s own `app.py`."""

    __slots__ = ("transport", "detail", "sub_type", "http_status")

    def __init__(self, *, transport: str, detail: str) -> None:
        """Record which provider the reply was relayed to, and a short detail string."""
        self.transport = transport
        self.detail = detail
        self.sub_type = None
        self.http_status = None


async def _fail_kv(exc: Exception, *, provider: str, channel_id: str, op: str) -> NoReturn:
    """Fail-loud kv error path: log, reply an error to chat, then re-raise -- see `slots`'s own."""
    wit_error = getattr(exc, "value", exc)
    case_name = type(wit_error).__name__
    log.error("roulette.kv_error", op=op, error=case_name)
    await relay.push(
        provider,
        {
            "channel": channel_id,
            "text": "the chamber is jammed, try again shortly.",
        },
    )
    raise RuntimeError(f"roulette kv {op} failed: {case_name}") from exc


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
        log.error("roulette.cooldown_config_corrupt", community=community)
        return DEFAULT_COOLDOWN_SECONDS


async def _handle_pull(
    *, community: str, actor: str | None, username: str, provider: str, channel_id: str
) -> str:
    """Enforce the per-caller cooldown (if any), then pull the trigger and build the reply."""
    pseudonym = _pseudonym(actor)
    cooldown = await _get_cooldown(community, provider=provider, channel_id=channel_id)

    if cooldown > 0:
        last_raw = await _kv_get(
            community, _lastpull_key(pseudonym), provider=provider, channel_id=channel_id
        )
        now_ms = clock.now_millis()

        if last_raw is not None:
            try:
                last_ms: int | None = int(last_raw.decode())
            except (UnicodeDecodeError, ValueError):
                log.error("roulette.cooldown_state_corrupt", community=community)
                last_ms = None
            if last_ms is not None:
                remaining_s = cooldown - (now_ms - last_ms) / 1000
                if remaining_s > 0:
                    wait_for = _format_duration(max(1, round(remaining_s)))
                    return f"\U0001f52b slow down, {username}! try again in {wait_for}."

        await _kv_set(
            community,
            _lastpull_key(pseudonym),
            str(now_ms).encode(),
            ttl_seconds=cooldown,
            provider=provider,
            channel_id=channel_id,
        )
    # cooldown == 0 -- disabled entirely; no last-pull timestamp is read or
    # written (see module docstring: writing one with ttl_seconds=0 would
    # never expire, an unbounded key for a cooldown that never fires).

    pulls = await _kv_increment(
        community,
        _pulls_key(pseudonym),
        1,
        ttl_seconds=0,
        provider=provider,
        channel_id=channel_id,
    )

    is_out = _pull_trigger()
    if is_out:
        await _kv_increment(
            community,
            _outs_key(pseudonym),
            1,
            ttl_seconds=0,
            provider=provider,
            channel_id=channel_id,
        )
        flavor = random.choice(_OUT_FLAVORS)  # noqa: S311 - a game, not a security decision
        return (
            f"\U0001f52b {username} spins the cylinder and pulls the trigger... "
            f"{flavor} (pull #{pulls})"
        )

    await _kv_increment(
        community,
        _survives_key(pseudonym),
        1,
        ttl_seconds=0,
        provider=provider,
        channel_id=channel_id,
    )
    flavor = random.choice(_SURVIVE_FLAVORS)  # noqa: S311 - a game, not a security decision
    return (
        f"\U0001f52b {username} spins the cylinder and pulls the trigger... "
        f"{flavor} (pull #{pulls})"
    )


async def _handle_list(
    *, community: str, actor: str | None, provider: str, channel_id: str
) -> str:
    """Read+render the caller's own total-pulls/survives/outs record."""
    pseudonym = _pseudonym(actor)
    pulls_raw = await _kv_get(
        community, _pulls_key(pseudonym), provider=provider, channel_id=channel_id
    )
    pulls = 0
    if pulls_raw is not None:
        try:
            pulls = int(pulls_raw.decode())
        except (UnicodeDecodeError, ValueError):
            log.error("roulette.pulls_corrupt", community=community)
            pulls = 0

    if pulls == 0:
        return _NOTHING_PULLED_YET

    survives_raw = await _kv_get(
        community, _survives_key(pseudonym), provider=provider, channel_id=channel_id
    )
    survives = 0
    if survives_raw is not None:
        try:
            survives = int(survives_raw.decode())
        except (UnicodeDecodeError, ValueError):
            log.error("roulette.survives_corrupt", community=community)
            survives = 0

    outs_raw = await _kv_get(
        community, _outs_key(pseudonym), provider=provider, channel_id=channel_id
    )
    outs = 0
    if outs_raw is not None:
        try:
            outs = int(outs_raw.decode())
        except (UnicodeDecodeError, ValueError):
            log.error("roulette.outs_corrupt", community=community)
            outs = 0

    rate = round(100 * survives / pulls) if pulls else 0
    return f"Pulls: {pulls}. Survived: {survives}. Out: {outs} ({rate}% survival rate)."


async def _handle_set_cooldown(
    arg: str | None, *, community: str, provider: str, channel_id: str
) -> str:
    """Parse+apply `!roulette set cooldown <seconds>`'s own free-text `args` tail."""
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
    return f"roulette cooldown set to {seconds}s"


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
        raise ValueError("roulette reply requires a channel_id from the inbound chat.message")
    command = payload.get("command")
    if command not in _KNOWN_COMMANDS:
        raise ValueError(f"unrecognized roulette command: {command!r}")

    provider = envelope.event.platform
    community = envelope.community
    if not community:
        log.error("roulette.missing_community", command=command)
        raise ValueError("roulette requires a community context and cannot operate tenant-wide")

    username = envelope.event.actor or "someone"

    if command == "usage":
        await relay.push(provider, {"channel": channel_id, "text": _USAGE})
        return DispatchResult(transport=provider, detail="usage")

    if command == "config_set_cooldown":
        role_signal = _caller_role_signal(payload)
        if role_signal is not True:
            log.info("roulette.config_denied", command=command, role_signal=str(role_signal))
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
        log.info("roulette.dispatch config applied", command=command)
        return DispatchResult(transport=provider, detail=command)

    if command == "list":
        reply_text = await _handle_list(
            community=community,
            actor=envelope.event.actor,
            provider=provider,
            channel_id=channel_id,
        )
    else:  # pull
        reply_text = await _handle_pull(
            community=community,
            actor=envelope.event.actor,
            username=username,
            provider=provider,
            channel_id=channel_id,
        )

    await relay.push(provider, {"channel": channel_id, "text": reply_text})
    log.info("roulette.dispatch relayed", platform=provider, command=command)
    return DispatchResult(transport=provider, detail=command)
