"""`!love <user>` / `!ship <userA> <userB>` -> a compatibility-% meter minigame.

Net-new Waddles content -- not a port of, or inspired by, any external
project (contrast `bundles/python/fish`, which credits a specific
inspiration in its own `bundle.yaml`/module docstring). All game logic below
is written fresh for this bundle.

Built on the shared command grammar parser
(`waddle_sdk.command.parse_command`/`CommandSpec`, first adopted by `fish`,
#618) for `!love`'s own `list` sub-grammar. `!love <user>` and `!ship <a> <b>`
themselves do NOT fit that grammar directly -- their arguments are free-text
challenge targets, not members of `waddle_sdk.command.VERBS` -- so
`_resolve_love()` first tries the shared parser for the known `list` shape
and falls back to a single-token target parse, while `_resolve_ship()`
simply requires exactly two whitespace-separated tokens (it has no
sub-grammar of its own). **Known limitation, documented rather than silently
papered over**: a target username that happens to collide with a reserved
verb (`list`, `set`, `add`, `sub`, `enable`, `disable`, `remove`, `delete`,
`reset`) cannot be paired directly via `!love <that-name>` -- same trade-off
every grammar-based bundle in this repo accepts for its own sub-module
names. `!ship` has no such restriction since it never runs its tokens
through the shared verb grammar.

v1 scope (KV-ONLY, no `db` capability -- same deliberate scoping as
`duel`/`fish`/`lurk`/`count`):

- `!love <user>` -- pairs the caller with `<user>`. Validates `<user>` looks
  like a plausible username (`_normalize_target()`, shared with `duel`); an
  unrecognizable target replies with a clear "I don't know who that is"
  message and touches no state -- never a silent drop, never a crash. A
  self-pairing (`<user>` normalizes to the caller's own `actor`) is NOT
  rejected -- unlike `duel`'s self-challenge, self-love is a legitimate,
  deterministic 100% result and is recorded into the caller's own history
  like any other pairing (deliberate design choice, documented here rather
  than silently diverging from `duel`'s precedent). Every resolved pairing
  (self or not) updates the caller's own `love.ships.<pseudonym>` count and
  `love.best.<pseudonym>` high score.
- `!ship <userA> <userB>` -- a stateless lookup between two named users,
  neither of which need be the caller. Requires exactly two
  whitespace-separated tokens; any other count is a usage error. Writes NO
  kv state at all (there is no single caller identity to attribute a record
  to) -- a deliberate scope boundary, not a missing feature.
- `!love list` -- reads the caller's own ship count + best match from `kv`.
  No `args` accepted -- `!love list <anything>` is a usage error, not
  silently ignored (mirrors `duel`/`fish`'s own `!<cmd> list` rule).

**No cooldown.** Unlike `duel`/`fish`, this bundle has no per-caller
rate-limit: the compatibility result is a deterministic pure function of its
inputs (see below), so repeating the same `!love`/`!ship` call the same day
always returns the identical answer at zero marginal kv cost -- there is
nothing to spam.

**Compatibility algorithm (deterministic per unordered pair per day --
chosen over random so a result is sharable/repeatable within a day, see
`_compute_match()`).** The percent and flavor line are both derived from one
SHA-256 digest of the two participants' pseudonyms (sorted so pairing order
never matters) and the current UTC calendar day (`waddle_sdk.clock`, the
only time source available to a bundle -- spec Sec7.4). The same pair asking
again later the same day gets the identical reply; the next day, a new one.

**Identity/pseudonymization**: all per-user state keys are SHA-256
pseudonyms (`_pseudonym()`), never the raw username/actor id -- same
rationale as `duel`'s own `_pseudonym()`: `event.actor` may currently be a
raw username (tokenization pipeline #429 not yet merged), so hashing before
anything ever reaches `community_kv` keeps this bundle PII-safe today and
unchanged after #429 lands. For `!love <user>` the caller's pseudonym comes
from `event.actor`; the target's (and both `!ship` participants') come from
their case-folded, validated chat-typed name -- stable across callers typing
the same name in different casing, but NOT guaranteed to collide with that
user's own `event.actor`-derived pseudonym on a platform where the two forms
differ. Documented trade-off, not a stand-in for a real user directory
(which `kv` alone cannot provide -- see "DO NOT BUILD" below).

**kv keys are colon-free (gh-631).** Every key below uses `.` as its own
internal separator, never `:` -- the real `kv` host capability
(`core/bundle_host_kv/src/scope.rs::is_allowed_key_byte`) rejects any
guest-supplied key containing a byte outside ASCII alnum + `_`/`-`/`.`,
`waddle_sdk.kv.validate_key()` enforces this before any host call, and
`waddle_sdk.testing.FakeKvHost` (used by this bundle's own tests, see
`tests/test_app.py`) enforces the identical charset -- unlike `duel`/`fish`'s
own hand-rolled test fakes, which accepted the colon-containing keys those
two bundles originally shipped with and let the bug slip past their test
suites entirely (`count`/`lurk` both had to ship a 1.0.4 just to fix this
after it reached production, see their own `bundle.yaml` history comments).

DO NOT BUILD in v1 -- clean, documented extension points, never a silent
stub:

- **Cross-community leaderboards / global rankings.** Every key here is
  scoped by `community_id` only (`waddle_sdk.community_kv` -- see its own
  module docstring: reputation + user-details are the platform's only two
  cross-community exceptions, and this bundle is neither). `kv` has no
  scan/list-keys primitive at all -- a leaderboard needs a real `db`
  capability with an `order_by`/pagination surface. **Deferred to a v2**
  once that capability lands (same deferral `duel`/`fish` document for
  their own leaderboards) -- no `!love leaderboard` command declared here.
- **Real username resolution / mention validation against a roster.**
  `_normalize_target()` is a shape check (plausible username characters),
  not a lookup against an actual community member list -- `kv` has no such
  roster. A target that is shape-valid but does not correspond to any real
  community member still resolves as a normal pairing; known v1 limitation,
  not a bug.
- `!ship` history/tracking -- out of scope for this kv-only v1 entirely
  (see above), not partially stubbed.

Gated behind the PostHog flag ``waddles.command-love`` -- see
`bundles/python/eightball/src/app.py`'s own docstring for the flag-gate
rationale and ordering (cheap command-match first, flag check second, real
grammar parse last).
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, NoReturn

from waddle_sdk import clock, community_kv, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, parse_command
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-love"

#: No sub-modules declared for v1 -- every `VERBS` token other than `list`
#: resolves to a usage reply (see `_resolve_love`), never a silent drop.
#: `!ship` has no `CommandSpec` of its own -- see module docstring.
_LOVE_SPEC = CommandSpec(name="love")

#: A plausible username/mention shape: an optional leading `@` (stripped),
#: then 1-32 chars of letters/digits/underscore/dot/hyphen. Not a lookup
#: against a real roster -- see module docstring's "DO NOT BUILD" section.
#: Shared shape with `duel`'s own `_USERNAME_RE`.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}$")

_USAGE = "Usage: !love <user> | !love list | !ship <userA> <userB>"
_NO_RECORD_YET = "You haven't shipped anyone yet -- try !love <user>!"

_KNOWN_COMMANDS = frozenset({"pair_self", "ship", "list", "usage"})

#: Flavor lines bucketed by compatibility tier, lowest to highest. Tier
#: selection and in-tier line selection are both derived deterministically
#: from the same digest as the percent itself -- see `_compute_match()`.
#: Tier boundaries: `[0,20)` -> 0, `[20,40)` -> 1, `[40,60)` -> 2,
#: `[60,80)` -> 3, `[80,100]` -> 4.
_FLAVOR_TIERS: tuple[tuple[str, ...], ...] = (
    (
        "it's a rocky start, but every ship has stormy seas",
        "the stars are not exactly aligned today",
        "needs some serious work, but don't give up just yet",
    ),
    (
        "there's potential buried in there somewhere",
        "a slow burn -- give it time",
        "mixed signals, but not hopeless",
    ),
    (
        "a solid maybe -- could go either way",
        "friendly chemistry, who knows where it leads",
        "fifty-fifty odds, anyone's game",
    ),
    (
        "sparks are flying!",
        "there's real chemistry brewing here",
        "the crowd is shipping it",
    ),
    (
        "written in the stars",
        "an absolute power couple",
        "soulmate energy, no notes",
    ),
)

_SELF_LOVE_FLAVOR = "self-love is important!"


def _pseudonym(identity: str | None) -> str:
    """Non-reversible per-identity key component -- see `duel`'s own `_pseudonym()` for why.

    `identity` may be a raw username (tokenization pipeline #429 not yet
    merged); hashing it before it ever reaches `community_kv` keeps this
    bundle PII-safe today and after #429 lands unchanged.
    """
    return hashlib.sha256((identity or "anonymous").encode()).hexdigest()


def _love_ships_key(pseudonym: str) -> str:
    """Per-(community, caller) total-pairings counter key (`.` separated, gh-631)."""
    return f"love.ships.{pseudonym}"


def _love_best_key(pseudonym: str) -> str:
    """Per-(community, caller) best-match-seen key (`.` separated, gh-631)."""
    return f"love.best.{pseudonym}"


def _normalize_target(raw: str) -> str | None:
    """Strip an optional leading `@` and validate the shape; `None` if not a plausible username.

    Shape check only -- not a lookup against a real community roster (`kv`
    has no such primitive; see module docstring's "DO NOT BUILD" section).
    Identical rule to `duel`'s own `_normalize_target()`.
    """
    candidate = raw[1:] if raw.startswith("@") else raw
    if not candidate or not _USERNAME_RE.match(candidate):
        return None
    return candidate


def _compute_match(pseudonym_a: str, pseudonym_b: str, day: str) -> tuple[int, str]:
    """Deterministic (percent, flavor) for an unordered pseudonym pair on `day`.

    One SHA-256 digest over the sorted pseudonym pair + `day` drives both
    the percent (first 4 digest bytes, mod 101 -> 0-100 inclusive) and the
    flavor line (next 4 digest bytes select a tier, then a line within it) --
    see module docstring's "Compatibility algorithm" section. Sorting the
    pair first means pairing order never changes the result.
    """
    pair_key = ".".join(sorted((pseudonym_a, pseudonym_b)))
    digest = hashlib.sha256(f"{pair_key}|{day}".encode()).hexdigest()
    percent = int(digest[:8], 16) % 101
    tier_index = min(percent // 20, len(_FLAVOR_TIERS) - 1)
    tier_lines = _FLAVOR_TIERS[tier_index]
    flavor_index = int(digest[8:16], 16) % len(tier_lines)
    return percent, tier_lines[flavor_index]


def _resolve_love(stripped: str) -> tuple[str, str | None]:
    """Classify a full `!love ...` message into this bundle's own `("command", target)` shape.

    Returns `("usage", None)`, `("list", None)`, or `("pair_self", target)`.
    Target-shape validation (`_normalize_target`) and self-pairing detection
    happen in `dispatch`, which owns all kv/state logic -- this function
    only classifies structure, same split `duel` uses between
    `transform`/`dispatch`.
    """
    rest = stripped.partition(" ")[2].strip()
    if not rest:
        return "usage", None

    try:
        parsed = parse_command(stripped, _LOVE_SPEC)
    except CommandUsageError:
        parsed = None

    if parsed is not None:
        if parsed.option == "list":
            return ("list", None) if parsed.args is None else ("usage", None)
        # Every other grammar-legal verb (set/add/sub/enable/disable/remove/delete/reset) --
        # no sub-modules or behavior declared for any of them, same fail-loud-never-silent
        # rule as a parse error itself.
        return "usage", None

    # Not a recognized verb -- a single bare token is the pairing target; two or more
    # tokens ("!love foo bar") is not a valid shape.
    tok1, _, tail = rest.partition(" ")
    if tail.strip():
        return "usage", None
    return "pair_self", tok1


def _resolve_ship(stripped: str) -> tuple[str, tuple[str, str] | None]:
    """Classify a full `!ship ...` message: requires exactly two whitespace-separated tokens."""
    rest = stripped.partition(" ")[2].strip()
    tokens = rest.split()
    if len(tokens) != 2:
        return "usage", None
    return "ship", (tokens[0], tokens[1])


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!love`/`!ship` and their grammar.

    Cheap-skip first (no leading `!love`/`!ship` token -- `None`, zero cost),
    flag check second, real grammar classification last -- same ordering as
    `duel`/`fish`/`eightball`'s own documented rationale. A recognized-but-
    malformed message still produces a reply (`"usage"`) since the caller did
    invoke this bundle -- never silently dropped.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None

    stripped = text.strip()
    head = stripped.partition(" ")[0]
    head_lower = head.lower()
    if head_lower not in ("!love", "!ship"):
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    command: str
    target: str | None = None
    target_pair: tuple[str, str] | None = None
    if head_lower == "!love":
        command, target = _resolve_love(stripped)
    else:
        command, target_pair = _resolve_ship(stripped)

    log.info("love.transform matched", command=command)
    payload: dict[str, Any] = {"command": command, "channel_id": event.payload.get("channel_id")}
    # `command == "pair_self"` implies `target is not None` (guaranteed by `_resolve_love`'s
    # own contract); `command == "ship"` implies `target_pair is not None` (ditto
    # `_resolve_ship`) -- the `is not None` checks are for mypy --strict, not real branches.
    if command == "pair_self" and target is not None:
        payload["target"] = target
    elif command == "ship" and target_pair is not None:
        payload["target_a"], payload["target_b"] = target_pair

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
    """Fail-loud kv error path: log, reply an error to chat, then re-raise -- see `duel`'s own."""
    wit_error = getattr(exc, "value", exc)
    case_name = type(wit_error).__name__
    log.error("love.kv_error", op=op, error=case_name)
    await relay.push(
        provider,
        {"channel": channel_id, "text": "shipping is temporarily unavailable, try again shortly."},
    )
    raise RuntimeError(f"love kv {op} failed: {case_name}") from exc


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


def _handle_ship(raw_a: str, raw_b: str) -> tuple[str, str]:
    """Validate both targets and return a stateless compatibility reply -- no kv involved.

    Returns `(reply_text, detail)` -- `detail` feeds `DispatchResult.detail`
    as `ship:<detail>` (`"unknown_target"`, `"self"`, or `"resolved"`).
    """
    normalized_a = _normalize_target(raw_a)
    if normalized_a is None:
        return f"I don't know who '{raw_a}' is -- try !ship @userA @userB.", "unknown_target"
    normalized_b = _normalize_target(raw_b)
    if normalized_b is None:
        return f"I don't know who '{raw_b}' is -- try !ship @userA @userB.", "unknown_target"

    if normalized_a.lower() == normalized_b.lower():
        reply = f"💘 {normalized_a} + {normalized_b} = 100% -- {_SELF_LOVE_FLAVOR}"
        return reply, "self"

    pseudonym_a = _pseudonym(normalized_a.lower())
    pseudonym_b = _pseudonym(normalized_b.lower())
    day = clock.now_rfc3339()[:10]
    percent, flavor = _compute_match(pseudonym_a, pseudonym_b, day)
    reply = f"💘 {normalized_a} + {normalized_b} = {percent}% -- {flavor}!"
    return reply, "resolved"


async def _handle_pair_self(
    raw_target: str,
    *,
    community: str,
    actor: str | None,
    username: str,
    provider: str,
    channel_id: str,
) -> tuple[str, str]:
    """Validate the target, compute the deterministic match, and update the caller's own history.

    Returns `(reply_text, detail)` -- `detail` feeds `DispatchResult.detail`
    as `love:<detail>` (`"unknown_target"`, `"self"`, or `"resolved"`). Both
    the self and non-self outcomes update `love.ships.<pseudonym>` /
    `love.best.<pseudonym>` for the caller -- see module docstring for why
    self-pairing is recorded rather than rejected like `duel`'s self-challenge.
    """
    normalized = _normalize_target(raw_target)
    if normalized is None:
        return (
            f"I don't know who '{raw_target}' is -- try !love @username.",
            "unknown_target",
        )

    pseudonym_caller = _pseudonym(actor)
    is_self = actor is not None and normalized.lower() == actor.lower()
    if is_self:
        percent = 100
        reply = f"💘 {username} + {username} = 100% -- {_SELF_LOVE_FLAVOR}"
        detail = "self"
    else:
        pseudonym_partner = _pseudonym(normalized.lower())
        day = clock.now_rfc3339()[:10]
        percent, flavor = _compute_match(pseudonym_caller, pseudonym_partner, day)
        reply = f"💘 {username} + {normalized} = {percent}% -- {flavor}!"
        detail = "resolved"

    await _kv_increment(
        community,
        _love_ships_key(pseudonym_caller),
        1,
        ttl_seconds=0,
        provider=provider,
        channel_id=channel_id,
    )
    current_best = await _read_int(
        community,
        _love_best_key(pseudonym_caller),
        provider=provider,
        channel_id=channel_id,
        corrupt_log="love.best_corrupt",
    )
    if percent > current_best:
        await _kv_set(
            community,
            _love_best_key(pseudonym_caller),
            str(percent).encode(),
            ttl_seconds=0,
            provider=provider,
            channel_id=channel_id,
        )

    return reply, detail


async def _handle_list(*, community: str, actor: str | None, provider: str, channel_id: str) -> str:
    """Read+render the caller's own ship count and best match."""
    pseudonym = _pseudonym(actor)
    ships = await _read_int(
        community,
        _love_ships_key(pseudonym),
        provider=provider,
        channel_id=channel_id,
        corrupt_log="love.ships_corrupt",
    )
    if ships == 0:
        return _NO_RECORD_YET
    best = await _read_int(
        community,
        _love_best_key(pseudonym),
        provider=provider,
        channel_id=channel_id,
        corrupt_log="love.best_corrupt",
    )
    plural = "time" if ships == 1 else "times"
    return f"You've shipped {ships} {plural}; best match: {best}%!"


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: all kv state reads/writes, then relay the reply.

    Raises:
        ValueError: The envelope's payload has no `channel_id`; the
            envelope has no `community` (no tenant-wide fallback -- see
            module docstring's data-scoping section); an unrecognized
            `command` (defensive -- `transform` only ever emits a member of
            `_KNOWN_COMMANDS`); a `"pair_self"` command with no `target` in
            its forwarded payload, or a `"ship"` command missing
            `target_a`/`target_b` (defensive -- `transform` always sets
            these for their respective commands).
        RuntimeError: A `kv` backend call failed (see `_fail_kv` -- a chat
            error reply and an ERROR log line are always emitted first).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("love reply requires a channel_id from the inbound chat.message")
    command = payload.get("command")
    if command not in _KNOWN_COMMANDS:
        raise ValueError(f"unrecognized love command: {command!r}")

    provider = envelope.event.platform
    community = envelope.community
    if not community:
        log.error("love.missing_community", command=command)
        raise ValueError("love requires a community context and cannot operate tenant-wide")

    username = envelope.event.actor or "someone"

    if command == "usage":
        await relay.push(provider, {"channel": channel_id, "text": _USAGE})
        return DispatchResult(transport=provider, detail="usage")

    if command == "list":
        reply_text = await _handle_list(
            community=community,
            actor=envelope.event.actor,
            provider=provider,
            channel_id=channel_id,
        )
        await relay.push(provider, {"channel": channel_id, "text": reply_text})
        log.info("love.dispatch relayed", platform=provider, command=command)
        return DispatchResult(transport=provider, detail=command)

    if command == "ship":
        target_a = payload.get("target_a")
        if not isinstance(target_a, str) or not target_a:
            raise ValueError("love ship requires target_a and target_b in the forwarded payload")
        target_b = payload.get("target_b")
        if not isinstance(target_b, str) or not target_b:
            raise ValueError("love ship requires target_a and target_b in the forwarded payload")
        reply_text, detail = _handle_ship(target_a, target_b)
        await relay.push(provider, {"channel": channel_id, "text": reply_text})
        log.info("love.dispatch relayed", platform=provider, command=command, detail=detail)
        return DispatchResult(transport=provider, detail=f"ship:{detail}")

    # pair_self
    target = payload.get("target")
    if not isinstance(target, str) or not target:
        raise ValueError("love pair_self requires a target in the forwarded payload")
    reply_text, detail = await _handle_pair_self(
        target,
        community=community,
        actor=envelope.event.actor,
        username=username,
        provider=provider,
        channel_id=channel_id,
    )
    await relay.push(provider, {"channel": channel_id, "text": reply_text})
    log.info("love.dispatch relayed", platform=provider, command=command, detail=detail)
    return DispatchResult(transport=provider, detail=f"love:{detail}")
