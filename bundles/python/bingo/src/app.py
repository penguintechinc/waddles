"""`!bingo` -> a per-community chat BINGO (5x5 cards, numbers 1-75), kv-only.

A mod starts a round (`!bingo start`) and calls numbers one at a time (`!bingo call`); every
viewer has their own 5x5 card (`!bingo` / `!bingo card`); the first viewer whose card holds a
full row, column or diagonal of called numbers (the centre square is free) claims it with
`!bingo bingo` and wins the round. `!bingo end` closes a round without a winner.

Commands (the mod verbs fail closed: only a real `is_mod is True` / `is_broadcaster is True`
passes, absent/stringly badges deny before any kv access):

- `!bingo start` (mod) -- begins a NEW round; refused while a round is still running.
- `!bingo call` (mod) -- calls the next number (`B-12`); all 75 can be called, then `end`.
- `!bingo` / `!bingo card` (anyone) -- shows YOUR card, called numbers in `[brackets]`.
- `!bingo bingo` / `!bingo claim` (anyone) -- claims a line. A false claim answers "not yet" and
  costs nothing; the first TRUE claim wins and ends the round.
- `!bingo status` / `!bingo list` (anyone) -- round state, calls so far and the latest calls.
- `!bingo end` (mod) -- ends the running round with no winner.

**Why this is "stateless-ish".** Nobody's card is ever stored. A round holds one random `seed`;
a viewer's card is `f(seed, pseudonym)` and the call order is `g(seed)`, both pure SHA-256
rankings, so the card and the called set are RECOMPUTED on every command. The only mutable
state is two atomic counters (calls so far, winner claim) and one small round record:

- `bingo.rid` -- round counter (`kv.increment`), 30-day TTL so a stale round's keys can never
  collide with a reused id.
- `bingo.cur` -- JSON `{rid, seed, ended}`; written by `start` and `end` only.
- `bingo.calls.<rid>` -- how many numbers were called (`kv.increment`, the only atomic CAS):
  called set = first `calls` numbers of `g(seed)`.
- `bingo.won.<rid>` -- winner claim (`kv.increment`): the caller who sees `1` is THE winner,
  so two simultaneous valid claims cannot both win.
- `bingo.winner.<rid>` -- JSON `{t, p}`: the winner's host-minted mention token (or `null`) and
  8 hex of the pseudonym, for `!bingo status` after the fact.

Every non-counter key carries a 3-day TTL (a round is hours, not days) so an abandoned round
cleans itself up. A failed winner write releases the claim, so the next valid claim can win;
`!bingo end` always works to unstick a round.

**Fairness.** The seed never leaves kv (the host scopes kv per tenant/community/app), so a viewer
cannot compute anyone else's card or the call order; a viewer's card is derived server-side
at claim time, never taken from the client. One identity gets one card per round (a viewer with
alt accounts has more cards -- the same as any chat bingo).

**PII-free.** The pseudonym is `sha256(platform, author_id or actor)`, used only in memory to
derive the card; the only persisted trace is 8 hex chars of it plus the host-minted
`{user:<token>}` mention placeholder for the winner (validated against the token grammar before
it is ever emitted). Logs carry only the op and counts, never an actor, name or message text.

**Echoed text is sanitized.** The only user-derived text echoed is the claimant's label; it is
the host placeholder kept live via `allow_tokens`, or -- when the actor is a raw name --
`waddle_sdk.safe_text.sanitize_user_text` output.

**Randomness.** The round seed is 128 bits from `random.SystemRandom()` (OS entropy, `wasi:random`
in the component) -- never the module-level `random` functions (componentize-py freezes their
import-time Mersenne Twister state into the build snapshot) and never a hash of public inputs: a
seed of `sha256(now_millis, round id, community)` can be brute-forced from the round's start time,
which would hand a viewer the whole call order. Cards/calls are SHA-256 rankings of the seed.

Activation-gated only -- no feature flag: the platform's installed -> enabled -> activated check
decides whether this bundle runs.

DO NOT BUILD (clean extension points, never a silent stub): multiple winners / blackout /
custom patterns, prizes or economy payouts, per-viewer card persistence, auto-calling on a timer.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import dataclass
from typing import Any, NoReturn, TypeIs

from waddle_sdk import clock, community_kv, kv, log, relay  # noqa: F401 -- eager, see docstring
from waddle_sdk.command import CommandSpec, CommandUsageError, parse_command
from waddle_sdk.community_kv import TENANT_WIDE_SENTINEL
from waddle_sdk.flask_core.bundle_runtime import get_bundle_context
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.identity import MAX_MENTION_TOKEN_LEN
from waddle_sdk.safe_text import sanitize_user_text

#: OS-entropy RNG (`os.urandom`, i.e. `wasi:random` in the component). Never the module-level
#: `random.*` functions (their Mersenne Twister is seeded at import and componentize-py freezes
#: import-time state into the build snapshot) and never a hash of public inputs such as the clock.
_RNG = random.SystemRandom()

COMMAND = "bingo"
_HEAD = f"!{COMMAND}"
SPEC = CommandSpec(name=COMMAND)

NUMBERS = 75
_COLUMNS = "BINGO"
_COLUMN_SIZE = 15
#: Round records self-expire so an abandoned round cleans up after itself.
_TTL_SECONDS = 3 * 24 * 60 * 60
#: The round counter must outlive every key a round can still hold (host TTL cap = 30 days).
_COUNTER_TTL_SECONDS = 30 * 24 * 60 * 60
#: How many of the latest calls `!bingo status` lists.
_RECENT_CALLS = 8

_ACTOR_PLACEHOLDER_RE = re.compile(r"^\{user:([A-Za-z0-9_:.-]+)\}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_:.-]+$")
_TAG_RE = re.compile(r"^[0-9a-f]{8}$")
_SEED_RE = re.compile(r"^[0-9a-f]{32}$")

_RID_KEY = "bingo.rid"
_CUR_KEY = "bingo.cur"

_MOD_VERBS = frozenset({"start", "call", "end"})
_USER_VERBS = frozenset({"card", "bingo", "claim", "status"})

_USAGE = (
    "Usage: !bingo (your card) | !bingo bingo (claim a line) | !bingo status | "
    "!bingo start | call | end (mod only)"
)
_DENIED = "only moderators/broadcasters can do that"
_NO_ROUND = "no bingo round is running -- ask a mod to !bingo start"
_KV_UNAVAILABLE = "bingo is temporarily unavailable, try again shortly."
_STATE_CORRUPT = "the bingo state is corrupted -- a mod can run !bingo end then !bingo start"
_NO_IDENTITY = "couldn't tell who you are, so you can't get a card"
_FALLBACK_LABEL = "someone"

#: The 12 winning lines of a 5x5 card, as (row, col) cells: 5 rows, 5 columns, 2 diagonals.
_LINES: tuple[tuple[tuple[int, int], ...], ...] = (
    *(tuple((r, c) for c in range(5)) for r in range(5)),
    *(tuple((r, c) for r in range(5)) for c in range(5)),
    tuple((i, i) for i in range(5)),
    tuple((i, 4 - i) for i in range(5)),
)


class _KvFailureError(RuntimeError):
    """A `kv` host call failed (generated WIT `Err`, classified structurally); fail loud."""


class _StateCorruptError(RuntimeError):
    """Stored round state did not parse; fail loud, never silently reset."""


@dataclass(slots=True, frozen=True)
class _Round:
    """The community's current round record (`bingo.cur`)."""

    rid: int
    seed: str
    ended: bool

    def dump(self) -> bytes:
        """Serialize to the stored JSON form."""
        return json.dumps({"rid": self.rid, "seed": self.seed, "ended": self.ended}).encode()

    @classmethod
    def load(cls, raw: bytes) -> _Round:
        """Parse strictly (real `bool`/`int` types) or raise `_StateCorruptError`."""
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _StateCorruptError("round is not valid JSON") from exc
        if not isinstance(data, dict):
            raise _StateCorruptError("round is not an object")
        rid, seed, ended = data.get("rid"), data.get("seed"), data.get("ended")
        if (
            not _is_int(rid)
            or not isinstance(seed, str)
            or _SEED_RE.fullmatch(seed) is None
            or not isinstance(ended, bool)
        ):
            raise _StateCorruptError("round has an unexpected shape")
        return cls(rid=rid, seed=seed, ended=ended)


@dataclass(slots=True, frozen=True)
class _Winner:
    """The recorded winner: the host mention token (or None) and an 8-hex pseudonym tag."""

    token: str | None
    tag: str

    def label(self) -> str:
        """Winner label: the live mention placeholder, else a non-identifying pseudonym tag."""
        if self.token is not None:
            return "{user:" + self.token + "}"
        return f"entrant {self.tag}"


def _is_int(value: object) -> TypeIs[int]:
    """True for a real `int` (a JSON `true` is a `bool`, never a round number)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _digest_int(*parts: object) -> int:
    """A 256-bit integer from the SHA-256 of `parts` (a ranking key, not a secret)."""
    return int.from_bytes(hashlib.sha256("\0".join(str(p) for p in parts).encode()).digest(), "big")


def call_order(seed: str) -> list[int]:
    """The round's full, fixed call order: a permutation of 1..75 ranked by `seed`."""
    return sorted(range(1, NUMBERS + 1), key=lambda n: _digest_int("call", seed, n))


def make_card(seed: str, who: str) -> list[list[int]]:
    """The viewer's 5x5 card as rows; `0` is the free centre square.

    Column `c` (B/I/N/G/O) holds 5 distinct numbers from `15c+1..15c+15`, chosen by ranking
    that column's numbers by `sha256(seed, pseudonym, n)` -- deterministic, never stored.
    """
    columns = [
        sorted(
            range(c * _COLUMN_SIZE + 1, (c + 1) * _COLUMN_SIZE + 1),
            key=lambda n: _digest_int("card", seed, who, n),
        )[:5]
        for c in range(5)
    ]
    rows = [[columns[c][r] for c in range(5)] for r in range(5)]
    rows[2][2] = 0
    return rows


def has_bingo(card: list[list[int]], called: set[int]) -> bool:
    """True when some full row, column or diagonal is all called (the free square counts)."""
    return any(all(card[r][c] == 0 or card[r][c] in called for r, c in line) for line in _LINES)


def _letter(number: int) -> str:
    """The BINGO column letter of a number 1..75."""
    return _COLUMNS[(number - 1) // _COLUMN_SIZE]


def _render_card(card: list[list[int]], called: set[int]) -> str:
    """One chat line: five `a b c d e` rows, called numbers in `[brackets]`, free as `[FREE]`."""
    rows = []
    for row in card:
        cells = ["[FREE]" if n == 0 else (f"[{n}]" if n in called else str(n)) for n in row]
        rows.append(" ".join(cells))
    return " / ".join(rows)


def _fail(exc: Exception, *, op: str) -> NoReturn:
    """Log a kv failure (exception TYPE only, never its text) and raise `_KvFailureError`."""
    wit_error = getattr(exc, "value", exc)
    log.error("bingo.kv_failure", op=op, error=type(wit_error).__name__)
    raise _KvFailureError(f"bingo kv {op} failed: {type(wit_error).__name__}") from exc


def _scope(community: str | None) -> str:
    """Return the community to key state under (`None` = host tenant-wide sentinel)."""
    if community == "":
        log.error("bingo.missing_community")
        raise ValueError("bingo requires a community context (got an empty community id)")
    return community if community is not None else TENANT_WIDE_SENTINEL


async def _get(community: str, key: str) -> bytes | None:
    """`community_kv.get`, reclassifying a host error into `_KvFailureError`."""
    try:
        return await community_kv.get(community, key)
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail`
        _fail(exc, op="get")


async def _set(community: str, key: str, value: bytes, ttl: int = _TTL_SECONDS) -> None:
    """`community_kv.set`, reclassifying a host error into `_KvFailureError`."""
    try:
        await community_kv.set(community, key, value, ttl)
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail`
        _fail(exc, op="set")


async def _incr(community: str, key: str, delta: int, ttl: int = _TTL_SECONDS) -> int:
    """`community_kv.increment` (the only atomic CAS), reclassifying a host error."""
    try:
        return await community_kv.increment(community, key, delta, ttl)
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail`
        _fail(exc, op="increment")


def _calls_key(rid: int) -> str:
    """Per-round calls-so-far counter key."""
    return f"bingo.calls.{rid}"


def _won_key(rid: int) -> str:
    """Per-round winner-claim counter key."""
    return f"bingo.won.{rid}"


def _winner_key(rid: int) -> str:
    """Per-round winner record key."""
    return f"bingo.winner.{rid}"


def _pseudonym(event: PlatformEvent) -> str | None:
    """Non-reversible per-viewer card key, or `None` when nothing identifies the author."""
    identity = event.payload.get("author_id") or event.actor
    if not identity:
        return None
    return hashlib.sha256(f"{event.platform}\0{identity}".encode()).hexdigest()


def _actor_token(actor: str | None) -> str | None:
    """The `<token>` of a host-minted `{user:<token>}` actor, else `None` (bounded length)."""
    if not actor:
        return None
    match = _ACTOR_PLACEHOLDER_RE.fullmatch(actor)
    if match is None or len(match.group(1)) > MAX_MENTION_TOKEN_LEN:
        return None
    return match.group(1)


def _actor_label(actor: str | None) -> str:
    """Label for the invoker: the live host placeholder, else the sanitized raw actor."""
    if not actor:
        return _FALLBACK_LABEL
    token = _actor_token(actor)
    allow = (token,) if token is not None else ()
    return sanitize_user_text(actor, allow_tokens=allow) or _FALLBACK_LABEL


def _caller_is_mod(payload: dict[str, Any]) -> bool:
    """Strict-boolean mod/broadcaster check -- FAILS CLOSED when role info is absent/non-bool."""
    return payload.get("is_mod") is True or payload.get("is_broadcaster") is True


async def _load_round(community: str) -> _Round | None:
    """The community's current round record, `None` when none exists/expired."""
    raw = await _get(community, _CUR_KEY)
    if raw is None:
        return None
    try:
        return _Round.load(raw)
    except _StateCorruptError:
        log.error("bingo.state_corrupt")
        raise


async def _load_winner(community: str, rid: int) -> _Winner | None:
    """The recorded winner of round `rid`, `None` when nobody has won (or the record is bad)."""
    raw = await _get(community, _winner_key(rid))
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        log.warn("bingo.winner_corrupt")
        return None
    token, tag = (data.get("t"), data.get("p")) if isinstance(data, dict) else (None, None)
    token_ok = token is None or (
        isinstance(token, str)
        and 0 < len(token) <= MAX_MENTION_TOKEN_LEN
        and _TOKEN_RE.fullmatch(token) is not None
    )
    if not token_ok or not isinstance(tag, str) or _TAG_RE.fullmatch(tag) is None:
        log.warn("bingo.winner_corrupt")
        return None
    return _Winner(token=token, tag=tag)


async def _calls_so_far(community: str, rid: int) -> int:
    """How many numbers have been called this round, clamped to 0..75."""
    raw = await _get(community, _calls_key(rid))
    if raw is None:
        return 0
    try:
        return max(0, min(int(raw.decode("utf-8")), NUMBERS))
    except (UnicodeDecodeError, ValueError):
        log.error("bingo.calls_corrupt")
        raise _StateCorruptError("calls counter is not an integer") from None


def _resolve(rest: str) -> str:
    """Map the text after `!bingo` onto a command name; every verb takes no arguments."""
    if rest:
        first, _, remainder = rest.partition(" ")
        verb = first.lower()
        if verb in _MOD_VERBS | _USER_VERBS:
            return "usage" if remainder.strip() else verb
    try:
        parsed = parse_command(f"{_HEAD} {rest}" if rest else _HEAD, SPEC)
    except CommandUsageError as exc:
        log.debug("bingo.invalid_command", error_type=type(exc).__name__)
        return "usage"
    if parsed.option is None:
        return "card"
    if parsed.option == "list" and parsed.args is None:
        return "status"
    return "usage"


async def _handle_start(community: str) -> str:
    """`!bingo start` -- begin a new round (a corrupt record is replaced by a clean start)."""
    try:
        current = await _load_round(community)
    except _StateCorruptError:
        current = None  # a mod starting a new round is the documented recovery
    if current is not None and await _is_live(community, current):
        return "a bingo round is already running -- finish it or !bingo end first"
    rid = await _incr(community, _RID_KEY, 1, _COUNTER_TTL_SECONDS)
    seed = f"{_RNG.getrandbits(128):032x}"
    await _set(community, _CUR_KEY, _Round(rid=rid, seed=seed, ended=False).dump())
    log.info("bingo.started")
    return (
        f"\U0001f3b1 bingo round #{rid} started! !bingo shows your card, a mod calls the "
        "numbers, first full row/column/diagonal types !bingo bingo"
    )


async def _is_live(community: str, current: _Round) -> bool:
    """A round is live until it is ended or someone has won it."""
    return not current.ended and await _load_winner(community, current.rid) is None


async def _handle_call(community: str) -> str:
    """`!bingo call` -- call the next number of the round's fixed order."""
    current = await _load_round(community)
    if current is None or not await _is_live(community, current):
        return _NO_ROUND
    called = await _incr(community, _calls_key(current.rid), 1)
    if called > NUMBERS:
        await _incr(community, _calls_key(current.rid), -1)
        return f"all {NUMBERS} numbers have been called -- !bingo end to close the round"
    number = call_order(current.seed)[called - 1]
    log.info("bingo.called", calls=called)
    return f"\U0001f3b1 {_letter(number)}-{number} (call #{called} of {NUMBERS})"


async def _handle_card(event: PlatformEvent, community: str) -> str:
    """`!bingo` / `!bingo card` -- the viewer's own card, recomputed from the round seed."""
    current = await _load_round(community)
    if current is None:
        return _NO_ROUND
    who = _pseudonym(event)
    if who is None:
        return _NO_IDENTITY
    if not await _is_live(community, current):
        return f"bingo round #{current.rid} is over -- ask a mod to !bingo start another"
    called = set(call_order(current.seed)[: await _calls_so_far(community, current.rid)])
    card = make_card(current.seed, who)
    return f"{_actor_label(event.actor)} your card: {_render_card(card, called)}"


async def _record_winner(community: str, event: PlatformEvent, who: str, rid: int) -> None:
    """Persist the winner record; on failure release the claim so the next valid claim wins."""
    record = json.dumps({"t": _actor_token(event.actor), "p": who[:8]}).encode("utf-8")
    try:
        await _set(community, _winner_key(rid), record)
    except _KvFailureError:
        try:
            await _incr(community, _won_key(rid), -1)
        except _KvFailureError:
            log.error("bingo.compensation_failed", op="release_claim")
        raise


async def _handle_claim(event: PlatformEvent, community: str) -> str:
    """`!bingo bingo` -- verify the viewer's card against the called set; first valid claim wins."""
    current = await _load_round(community)
    if current is None:
        return _NO_ROUND
    who = _pseudonym(event)
    if who is None:
        return _NO_IDENTITY
    if not await _is_live(community, current):
        return f"bingo round #{current.rid} is already over"
    calls = await _calls_so_far(community, current.rid)
    called = set(call_order(current.seed)[:calls])
    label = _actor_label(event.actor)
    if not has_bingo(make_card(current.seed, who), called):
        return f"{label}, not yet -- you need a full row, column or diagonal ({calls} called)"
    if await _incr(community, _won_key(current.rid), 1) != 1:
        return f"{label}, someone else claimed bingo first!"
    await _record_winner(community, event, who, current.rid)
    log.info("bingo.won", calls=calls)
    return f"\U0001f3c6 BINGO! {label} wins round #{current.rid} after {calls} calls!"


async def _handle_end(community: str) -> str:
    """`!bingo end` -- close the running round with no winner."""
    current = await _load_round(community)
    if current is None:
        return _NO_ROUND
    if not await _is_live(community, current):
        return f"bingo round #{current.rid} is already over"
    await _set(community, _CUR_KEY, _Round(current.rid, current.seed, True).dump())
    log.info("bingo.ended")
    return f"bingo round #{current.rid} ended with no winner"


async def _handle_status(community: str) -> str:
    """`!bingo status` -- the round's state, calls so far and the latest calls."""
    current = await _load_round(community)
    if current is None:
        return _NO_ROUND
    winner = await _load_winner(community, current.rid)
    calls = await _calls_so_far(community, current.rid)
    if winner is not None:
        return f"bingo round #{current.rid} was won by {winner.label()} after {calls} calls"
    if current.ended:
        return f"bingo round #{current.rid} ended with no winner after {calls} calls"
    order = call_order(current.seed)[:calls]
    recent = ", ".join(f"{_letter(n)}-{n}" for n in order[-_RECENT_CALLS:][::-1])
    latest = f"; latest: {recent}" if recent else ""
    return f"bingo round #{current.rid} is live: {calls}/{NUMBERS} called{latest}"


async def _handle_command(event: PlatformEvent, community: str, rest: str) -> str:
    """Route one `!bingo ...` line; mod verbs are gated before any kv access."""
    command = _resolve(rest)
    if command == "usage":
        return _USAGE
    if command in _MOD_VERBS and not _caller_is_mod(event.payload):
        log.info("bingo.permission_denied", command=command)
        return _DENIED
    if command == "start":
        return await _handle_start(community)
    if command == "call":
        return await _handle_call(community)
    if command == "end":
        return await _handle_end(community)
    if command == "card":
        return await _handle_card(event, community)
    if command in ("bingo", "claim"):
        return await _handle_claim(event, community)
    return await _handle_status(community)


def _reply_event(event: PlatformEvent, text: str) -> PlatformEvent:
    """A NEW event carrying only the reply text + the channel to answer in."""
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={"text": text, "channel_id": event.payload.get("channel_id")},
        occurred_at=event.occurred_at,
    )


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!bingo`, do the op, build the reply.

    Cheap-skip first (no matching head -- `None`, zero kv cost). A recognized-but-malformed
    command still produces a reply (usage), never a silent drop; a kv/state failure is answered
    in chat after an ERROR log (class name only).
    """
    raw = event.payload.get("text")
    if not isinstance(raw, str):
        return None
    head, _, rest = raw.strip().partition(" ")
    if head.lower() != _HEAD:
        return None
    community = _scope(get_bundle_context().community)
    try:
        reply = await _handle_command(event, community, rest.strip())
    except _KvFailureError:
        reply = _KV_UNAVAILABLE
    except _StateCorruptError:
        reply = _STATE_CORRUPT
    return _reply_event(event, reply)


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result -- see `pyping`'s own `app.py`."""

    __slots__ = ("detail", "http_status", "sub_type", "transport")

    def __init__(self, *, transport: str, detail: str) -> None:
        """Record which provider the reply was relayed to, and a short detail string."""
        self.transport = transport
        self.detail = detail
        self.sub_type = None
        self.http_status = None


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: relay the reply `transform()` already built.

    Raises:
        ValueError: The envelope's reply payload has no `channel_id`.
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("bingo reply requires a channel_id from the inbound chat.message")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": payload.get("text", "")})
    log.info("bingo.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
