"""`!kgiveaway` -> a per-community KEYWORD giveaway, kv-only.

A mod opens a giveaway with a keyword (`!kgiveaway open <keyword> [prize]`); viewers enter by
typing the keyword as a whole chat line (or by `!kgiveaway enter`); a mod closes it and draws a
random winner (`!kgiveaway draw`), and `!kgiveaway reroll` draws another one from the entrants
who have not won yet.

Why a distinct bundle name and trigger: `bundles/python/giveaway` (PR #853) is a TIMED giveaway
(`!giveaway start 5m`, `!enter`) and `raffle` already owns `!raffle`/`!enter`. This bundle is
keyword-driven with no clock, so it ships as `keywordgiveaway` on `!kgiveaway` -- all three can
be activated in the same community without answering each other's commands.

Commands (the mod verbs fail closed: only a real `is_mod is True` / `is_broadcaster is True`
passes, absent/stringly badges deny before any kv access):

- `!kgiveaway open <keyword> [prize...]` (mod) -- starts a NEW giveaway. `<keyword>` is 2-24 of
  `[a-z0-9_]`, optionally with a leading `!` (so `!win` works), matched case-insensitively. Refused
  while a previous giveaway is still open.
- `!kgiveaway enter` (anyone) -- explicit entry; always answers (entered / already entered /
  closed / none open / full).
- `<keyword>` typed as the WHOLE chat line (anyone) -- the ambient entry path. It answers
  NOTHING, on success or on a duplicate: a 500-viewer giveaway would otherwise flood the bot's
  outbound rate limit with acknowledgements. `!kgiveaway status` shows the running count.
- `!kgiveaway close` (mod) -- stops taking entries; the entrants are kept for the draw.
- `!kgiveaway draw` (mod) -- closes entries (a fixed pool for `reroll`) and picks one winner.
  Refused if a winner was already drawn (use `reroll`) or nobody entered.
- `!kgiveaway reroll` (mod) -- picks another winner from the entrants who have not won. Refused
  before a first `draw` and once everyone has won.
- `!kgiveaway` / `!kgiveaway status` / `!kgiveaway list` (anyone) -- current state + entry count.

**Storage (kv only, no `db`).** `kv` has no key listing, so the entrants are SLOTTED rather than
kept in one read-modify-write array (which loses entries under concurrency):

- `kwgiveaway.rid` -- round counter (`kv.increment`), 30-day TTL so a stale round's keys can
  never collide with a reused id.
- `kwgiveaway.state` -- JSON `{rid, kw, prize, open, drawn}`; written only by the mod verbs.
- `kwgiveaway.m.<rid>.<pseudonym32>` -- one-entry-per-user marker. `kv.increment` is the only
  atomic compare-and-swap: the caller that sees `1` owns the entry, any later one sees `> 1`.
- `kwgiveaway.n.<rid>` -- slot counter (`kv.increment`), the entry's slot number.
- `kwgiveaway.e.<rid>.<slot>` -- JSON `{t, p}`: the entrant's host-minted mention token (or
  `null`) and the first 8 hex of their pseudonym.

Every key is `.`-separated (the host reserves `:`) and every non-counter key carries a 3-day TTL,
so an abandoned giveaway cleans itself up. Up to `MAX_ENTRANTS` entrants = 2 keys each, well
inside the host's 10,000-key quota for a handful of rounds. A failed second write releases the
marker (compensation), so the viewer can simply re-enter; an orphaned slot is a harmless hole the
draw skips. A draw uses at most `MAX_DRAW_PROBES` entry reads, inside the 64-op invocation budget.

**PII-free.** Entrants are identified ONLY by a SHA-256 pseudonym of `(platform, author_id or
actor)`; the marker key holds 128 bits of it, the entry holds 8 hex chars for the fallback tag.
The winner is announced through the entrant's host-minted `{user:<token>}` mention placeholder
(stored at entry time, validated against the token grammar before it is ever emitted), which the
egress detokenizer turns into the display name -- no raw name is ever stored, keyed or logged. An
entrant whose actor was not a placeholder is announced as `entrant <8 hex>`. Logs carry only
the op and counts, never a keyword, prize, actor or message text.

**Echoed text is sanitized.** The keyword and the prize are free text a mod typed; every echo
goes through `waddle_sdk.safe_text.sanitize_user_text` (`@everyone`, `<@id>`, forged
`{user:..}` placeholders and control characters are neutralized). The invoker's own label is the
host placeholder kept live via `allow_tokens`, never a raw name.

**Randomness.** No `random` module (componentize-py freezes module-level RNG state at build
time): the pick is `sha256(now_millis, rid, draw number, pool size, probe) mod pool size`. This
is a community giveaway, not a security decision -- the inputs are not secret.

Activation-gated only -- no feature flag: the platform's installed -> enabled -> activated check
decides whether this bundle runs.

DO NOT BUILD (clean extension points, never a silent stub): timed auto-close (use `giveaway`),
multiple entries / ticket weighting, sub-only or follower-only eligibility (needs a badge
contract this bundle does not depend on), DM-ing the winner.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, NoReturn, TypeIs

from waddle_sdk import clock, community_kv, kv, log, relay  # noqa: F401 -- eager, see docstring
from waddle_sdk.command import CommandSpec, CommandUsageError, parse_command
from waddle_sdk.community_kv import TENANT_WIDE_SENTINEL
from waddle_sdk.flask_core.bundle_runtime import get_bundle_context
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.identity import MAX_MENTION_TOKEN_LEN
from waddle_sdk.safe_text import sanitize_user_text

COMMAND = "kgiveaway"
_HEAD = f"!{COMMAND}"
SPEC = CommandSpec(name=COMMAND)

#: Hard cap on entrants per giveaway: 2 kv keys each stays far under the host's 10,000-key quota.
MAX_ENTRANTS = 2000
#: Entry reads one draw may spend finding a live entry (holes are skipped) -- the host allows 64
#: kv ops per invocation and a draw also spends 3 on state/count.
MAX_DRAW_PROBES = 40
#: Every non-counter key self-expires so an abandoned giveaway cleans up after itself.
_TTL_SECONDS = 3 * 24 * 60 * 60
#: The round counter must outlive every key a round can still hold (host TTL cap = 30 days).
_COUNTER_TTL_SECONDS = 30 * 24 * 60 * 60
MAX_PRIZE_LEN = 80
_MAX_KEYWORD_LEN = 25  # "!" + 24

_KEYWORD_RE = re.compile(r"^!?[a-z0-9_]{2,24}$")
_ACTOR_PLACEHOLDER_RE = re.compile(r"^\{user:([A-Za-z0-9_:.-]+)\}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_:.-]+$")
_TAG_RE = re.compile(r"^[0-9a-f]{8}$")

_RID_KEY = "kwgiveaway.rid"
_STATE_KEY = "kwgiveaway.state"

_MOD_VERBS = frozenset({"open", "close", "draw", "reroll"})
_USER_VERBS = frozenset({"enter", "status"})

_USAGE = (
    "Usage: !kgiveaway | !kgiveaway enter | !kgiveaway status | "
    "!kgiveaway open <keyword> [prize] | close | draw | reroll (mod only)"
)
_OPEN_USAGE = (
    "Usage: !kgiveaway open <keyword> [prize] -- keyword is 2-24 letters/digits/_ "
    "(optionally starting with !)"
)
_DENIED = "only moderators/broadcasters can do that"
_NOT_OPEN = "no giveaway is open right now -- ask a mod to !kgiveaway open"
_NO_IDENTITY = "couldn't tell who you are, so you can't be entered"
_KV_UNAVAILABLE = "the giveaway is temporarily unavailable, try again shortly."
_STATE_CORRUPT = "the giveaway state is corrupted -- a mod can run !kgiveaway open to start over"
_FALLBACK_LABEL = "someone"


class _KvFailureError(RuntimeError):
    """A `kv` host call failed (generated WIT `Err`, classified structurally); fail loud."""


class _StateCorruptError(RuntimeError):
    """Stored giveaway state/entry did not parse; fail loud, never silently reset."""


@dataclass(slots=True)
class _State:
    """The one per-community giveaway record (`kwgiveaway.state`)."""

    rid: int
    keyword: str
    prize: str
    open: bool
    drawn: list[int] = field(default_factory=list)

    def dump(self) -> bytes:
        """Serialize to the stored JSON form."""
        return json.dumps(
            {
                "rid": self.rid,
                "kw": self.keyword,
                "prize": self.prize,
                "open": self.open,
                "drawn": self.drawn,
            }
        ).encode("utf-8")

    @classmethod
    def load(cls, raw: bytes) -> _State:
        """Parse strictly (real `bool`/`int` types) or raise `_StateCorruptError`."""
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _StateCorruptError("state is not valid JSON") from exc
        if not isinstance(data, dict):
            raise _StateCorruptError("state is not an object")
        rid, keyword, prize = data.get("rid"), data.get("kw"), data.get("prize")
        is_open, drawn = data.get("open"), data.get("drawn")
        if (
            not _is_int(rid)
            or not isinstance(keyword, str)
            or _KEYWORD_RE.fullmatch(keyword) is None
            or not isinstance(prize, str)
            or not isinstance(is_open, bool)
            or not isinstance(drawn, list)
            or not all(_is_int(slot) for slot in drawn)
        ):
            raise _StateCorruptError("state has an unexpected shape")
        return cls(rid=rid, keyword=keyword, prize=prize, open=is_open, drawn=list(drawn))


@dataclass(slots=True, frozen=True)
class _Entry:
    """One slotted entrant: the host mention token (or None) and an 8-hex pseudonym tag."""

    token: str | None
    tag: str

    def label(self) -> str:
        """Winner label: the live mention placeholder, else a non-identifying pseudonym tag."""
        if self.token is not None:
            return "{user:" + self.token + "}"
        return f"entrant {self.tag}"


def _is_int(value: object) -> TypeIs[int]:
    """True for a real `int` (a JSON `true` is a `bool`, never a slot/round number)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _fail(exc: Exception, *, op: str) -> NoReturn:
    """Log a kv failure (exception TYPE only, never its text) and raise `_KvFailureError`."""
    wit_error = getattr(exc, "value", exc)
    log.error("kwgiveaway.kv_failure", op=op, error=type(wit_error).__name__)
    raise _KvFailureError(f"kwgiveaway kv {op} failed: {type(wit_error).__name__}") from exc


def _scope(community: str | None) -> str:
    """Return the community to key state under (`None` = host tenant-wide sentinel)."""
    if community == "":
        log.error("kwgiveaway.missing_community")
        raise ValueError("keywordgiveaway requires a community context (got an empty id)")
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


async def _delete(community: str, key: str) -> None:
    """`community_kv.delete`, reclassifying a host error into `_KvFailureError`."""
    try:
        await community_kv.delete(community, key)
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail`
        _fail(exc, op="delete")


def _marker_key(rid: int, who: str) -> str:
    """Per-(round, pseudonym) one-entry marker key (128 bits of the pseudonym)."""
    return f"kwgiveaway.m.{rid}.{who[:32]}"


def _count_key(rid: int) -> str:
    """Per-round slot counter key."""
    return f"kwgiveaway.n.{rid}"


def _entry_key(rid: int, slot: int) -> str:
    """Per-(round, slot) entry record key."""
    return f"kwgiveaway.e.{rid}.{slot}"


def _pseudonym(event: PlatformEvent) -> str | None:
    """Non-reversible per-viewer key component, or `None` when nothing identifies the author."""
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


def _pick_index(parts: tuple[object, ...], size: int) -> int:
    """Uniform-enough index in `[0, size)` from a hash of `parts` (not security-sensitive)."""
    digest = hashlib.sha256("\0".join(str(part) for part in parts).encode()).digest()
    return int.from_bytes(digest, "big") % size


async def _load_state(community: str) -> _State | None:
    """The community's giveaway state, `None` when never opened/expired."""
    raw = await _get(community, _STATE_KEY)
    if raw is None:
        return None
    try:
        return _State.load(raw)
    except _StateCorruptError:
        log.error("kwgiveaway.state_corrupt")
        raise


async def _save_state(community: str, state: _State) -> None:
    """Persist the giveaway state."""
    await _set(community, _STATE_KEY, state.dump())


async def _load_entry(community: str, rid: int, slot: int) -> _Entry | None:
    """One slotted entry, or `None` for a hole (never written, expired, or corrupt)."""
    raw = await _get(community, _entry_key(rid, slot))
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        log.warn("kwgiveaway.entry_corrupt")
        return None
    token, tag = (data.get("t"), data.get("p")) if isinstance(data, dict) else (None, None)
    token_ok = token is None or (
        isinstance(token, str)
        and 0 < len(token) <= MAX_MENTION_TOKEN_LEN
        and _TOKEN_RE.fullmatch(token) is not None
    )
    if not token_ok or not isinstance(tag, str) or _TAG_RE.fullmatch(tag) is None:
        log.warn("kwgiveaway.entry_corrupt")
        return None
    return _Entry(token=token, tag=tag)


def _prize_suffix(state: _State) -> str:
    """` -- prize: <sanitized prize>` or the empty string."""
    return f" -- prize: {sanitize_user_text(state.prize)}" if state.prize else ""


def _resolve(rest: str) -> tuple[str, str]:
    """Map the text after `!kgiveaway` onto `(command, args)`.

    `open` carries free-text args; the other domain verbs take none (extra text is a usage
    error). The bare form and `list` go through the real shared grammar parser.
    """
    if rest:
        first, _, remainder = rest.partition(" ")
        verb = first.lower()
        remainder = remainder.strip()
        if verb == "open":
            return "open", remainder
        if verb in _MOD_VERBS | _USER_VERBS:
            return ("usage", "") if remainder else (verb, "")
    try:
        parsed = parse_command(f"{_HEAD} {rest}" if rest else _HEAD, SPEC)
    except CommandUsageError as exc:
        log.debug("kwgiveaway.invalid_command", error_type=type(exc).__name__)
        return "usage", ""
    if parsed.option is None or (parsed.option == "list" and parsed.args is None):
        return "status", ""
    return "usage", ""


async def _handle_open(community: str, args: str) -> str:
    """`!kgiveaway open <keyword> [prize]` -- start a new giveaway (recovers a corrupt state)."""
    keyword, _, prize_raw = args.partition(" ")
    keyword = keyword.lower()
    prize = " ".join(prize_raw.split())
    if _KEYWORD_RE.fullmatch(keyword) is None or keyword == _HEAD:
        return _OPEN_USAGE
    if len(prize) > MAX_PRIZE_LEN:
        return f"the prize text is too long ({MAX_PRIZE_LEN} characters max)"
    try:
        existing = await _load_state(community)
    except _StateCorruptError:
        existing = None  # a mod opening a new giveaway is the documented recovery
    if existing is not None and existing.open:
        return (
            f"a giveaway is already open (keyword {sanitize_user_text(existing.keyword)}) "
            "-- !kgiveaway close it first"
        )
    rid = await _incr(community, _RID_KEY, 1, _COUNTER_TTL_SECONDS)
    state = _State(rid=rid, keyword=keyword, prize=prize, open=True)
    await _save_state(community, state)
    log.info("kwgiveaway.opened")
    return (
        f"\U0001f381 giveaway open! type {sanitize_user_text(keyword)} in chat "
        f"(or !kgiveaway enter) to join{_prize_suffix(state)}"
    )


async def _release_entry(community: str, rid: int, who: str) -> None:
    """Best-effort compensation: free the marker so a failed entry can simply be retried."""
    try:
        await _delete(community, _marker_key(rid, who))
    except _KvFailureError:
        log.error("kwgiveaway.compensation_failed", op="release_marker")


async def _enter_into(
    event: PlatformEvent, community: str, state: _State, *, explicit: bool
) -> str | None:
    """Enter the author into `state`'s round once. Ambient callers (`explicit=False`) get `None`."""
    who = _pseudonym(event)
    if who is None:
        return _NO_IDENTITY if explicit else None
    label = _actor_label(event.actor)
    if await _incr(community, _marker_key(state.rid, who), 1) > 1:
        log.debug("kwgiveaway.duplicate_entry")
        return f"{label}, you're already entered!" if explicit else None
    try:
        slot = await _incr(community, _count_key(state.rid), 1)
        if slot > MAX_ENTRANTS:
            await _incr(community, _count_key(state.rid), -1)
            await _release_entry(community, state.rid, who)
            return f"the giveaway is full ({MAX_ENTRANTS} entrants max)" if explicit else None
        record = json.dumps({"t": _actor_token(event.actor), "p": who[:8]}).encode("utf-8")
        await _set(community, _entry_key(state.rid, slot), record)
    except _KvFailureError:
        await _release_entry(community, state.rid, who)
        raise
    log.debug("kwgiveaway.entered", slot=slot)
    return f"\U0001f389 {label} entered the giveaway! (entry #{slot})" if explicit else None


async def _handle_enter(event: PlatformEvent, community: str) -> str:
    """`!kgiveaway enter` -- always answers."""
    state = await _load_state(community)
    if state is None:
        return _NOT_OPEN
    if not state.open:
        return "entries are closed for this giveaway"
    reply = await _enter_into(event, community, state, explicit=True)
    return reply if reply is not None else _NOT_OPEN


async def _handle_keyword(event: PlatformEvent, community: str, text: str) -> str | None:
    """Ambient path: a chat line that is exactly the keyword enters the author, silently."""
    state = await _load_state(community)
    if state is None or not state.open or text.lower() != state.keyword:
        return None
    return await _enter_into(event, community, state, explicit=False)


async def _handle_close(community: str) -> str:
    """`!kgiveaway close` -- stop taking entries, keep them for the draw."""
    state = await _load_state(community)
    if state is None:
        return _NOT_OPEN
    if not state.open:
        return "the giveaway is already closed"
    state.open = False
    await _save_state(community, state)
    entered = await _entry_count(community, state)
    log.info("kwgiveaway.closed", entrants=entered)
    return f"the giveaway is closed with {entered} entrant(s) -- !kgiveaway draw to pick a winner"


async def _entry_count(community: str, state: _State) -> int:
    """Slots handed out so far (an upper bound: orphaned slots are counted), clamped to the cap."""
    raw = await _get(community, _count_key(state.rid))
    if raw is None:
        return 0
    try:
        return max(0, min(int(raw.decode("utf-8")), MAX_ENTRANTS))
    except (UnicodeDecodeError, ValueError):
        log.error("kwgiveaway.count_corrupt")
        raise _StateCorruptError("slot counter is not an integer") from None


async def _pick_winner(community: str, state: _State) -> tuple[int, _Entry] | None:
    """Pick a random not-yet-drawn live entry, skipping holes; `None` when none is left."""
    candidates = [
        slot
        for slot in range(1, await _entry_count(community, state) + 1)
        if slot not in state.drawn
    ]
    now = clock.now_millis()
    for probe in range(MAX_DRAW_PROBES):
        if not candidates:
            break
        pool = len(candidates)
        index = _pick_index((now, state.rid, len(state.drawn), pool, probe), pool)
        slot = candidates.pop(index)
        entry = await _load_entry(community, state.rid, slot)
        if entry is not None:
            return slot, entry
    return None


async def _handle_draw(community: str, *, reroll: bool) -> str:
    """`!kgiveaway draw` / `reroll` -- announce one winner and close the pool."""
    state = await _load_state(community)
    if state is None:
        return _NOT_OPEN
    if reroll and not state.drawn:
        return "nobody has been drawn yet -- use !kgiveaway draw first"
    if not reroll and state.drawn:
        return "a winner was already drawn -- use !kgiveaway reroll for another"
    picked = await _pick_winner(community, state)
    if picked is None:
        log.info("kwgiveaway.no_candidates", reroll=reroll)
        if reroll:
            return "no more entrants to draw from"
        return "no entrants yet -- have viewers type the keyword first"
    slot, entry = picked
    state.open = False
    state.drawn.append(slot)
    await _save_state(community, state)
    log.info("kwgiveaway.drawn", reroll=reroll, winners=len(state.drawn))
    lead = "\U0001f501 new winner" if reroll else "\U0001f3c6 the winner"
    return f"{lead}: {entry.label()}{_prize_suffix(state)} -- congratulations!"


async def _handle_status(community: str) -> str:
    """`!kgiveaway status` -- state + running entry count."""
    state = await _load_state(community)
    if state is None:
        return _NOT_OPEN
    entered = await _entry_count(community, state)
    keyword = sanitize_user_text(state.keyword)
    if state.open:
        return f"giveaway open: type {keyword} to enter -- {entered} entered{_prize_suffix(state)}"
    if state.drawn:
        return f"giveaway closed: {entered} entered, {len(state.drawn)} winner(s) drawn"
    return f"giveaway closed: {entered} entered, waiting for a !kgiveaway draw"


async def _handle_command(event: PlatformEvent, community: str, rest: str) -> str:
    """Route one `!kgiveaway ...` line; mod verbs are gated before any kv access."""
    command, args = _resolve(rest)
    if command == "usage":
        return _USAGE
    if command in _MOD_VERBS and not _caller_is_mod(event.payload):
        log.info("kwgiveaway.permission_denied", command=command)
        return _DENIED
    if command == "open":
        return await _handle_open(community, args)
    if command == "close":
        return await _handle_close(community)
    if command == "draw":
        return await _handle_draw(community, reroll=False)
    if command == "reroll":
        return await _handle_draw(community, reroll=True)
    if command == "enter":
        return await _handle_enter(event, community)
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
    """Implement `process-stage.transform`: `!kgiveaway ...` or a bare keyword entry.

    Sees every chat line (the manifest has no `command_prefix` filter), so non-matching text is
    rejected before any kv read: a line longer than a keyword is never an entry. A kv/state
    failure on the command path is answered in chat; on the ambient path it raises (fail loud --
    a swallowed failure would drop entries silently).
    """
    raw = event.payload.get("text")
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    head, _, rest = text.partition(" ")
    is_command = head.lower() == _HEAD
    if not is_command and len(text) > _MAX_KEYWORD_LEN:
        return None

    community = _scope(get_bundle_context().community)
    try:
        if is_command:
            reply: str | None = await _handle_command(event, community, rest.strip())
        else:
            reply = await _handle_keyword(event, community, text)
    except (_KvFailureError, _StateCorruptError) as exc:
        if not is_command:
            raise
        reply = _KV_UNAVAILABLE if isinstance(exc, _KvFailureError) else _STATE_CORRUPT
    if reply is None:
        return None
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
        raise ValueError("keywordgiveaway reply requires a channel_id from the inbound message")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": payload.get("text", "")})
    log.info("kwgiveaway.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
