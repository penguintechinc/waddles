"""`!level` -> passive chat XP and levels, per community, kv-backed.

Every chat line from an identifiable chatter earns `XP_PER_MESSAGE` XP, at most once per
`COOLDOWN_SECONDS` (so rapid messages cannot farm XP). `!level [@user]` shows a chatter's XP and
the level derived from it; `!level leaderboard` (alias `top`) shows the top `LEADERBOARD_SIZE`
chatters. State is per community: the host `kv` capability scopes every key by
`(tenant, community, app_id)`, so two communities never see each other's XP.

## Identity and PII

The chatter is the pipeline's `{user:<id>}` PII token (`event.actor`, or an @mention rewritten by
`pii_tokenize`). The bundle never sees or stores a name. kv KEYS carry only a one-way pseudonym,
`sha256("level|<id>")`, so a key can never leak the id or collide with the `kv` charset
(`[A-Za-z0-9_.-]`, never `:` -- gh-631). The leaderboard VALUE stores the opaque `<id>` itself,
because the egress detokenizer can only render a name from a `{user:<id>}` placeholder: the board
is the one place an id is kept, and an id is not PII (it is the token hub-api resolves). A
non-token actor (tokenization off, a DM with no actor) earns nothing and is never keyed or logged.
Logs carry only op names, platform and exception TYPE names -- never an id, pseudonym or text.

## Anti-farm cooldown

The last award time (event time, whole seconds) lives in `level.ts.<pseudonym>`. A message earns
XP only when `event_time - last >= COOLDOWN_SECONDS`; the comparison is signed, so a replayed or
out-of-order OLDER event is refused and can never rewind the stamp. Event time (`occurred_at`,
stamped once at ingest) is used instead of the wall clock so a redelivered event is idempotent.
Deliberately NOT a TTL'd kv claim: `kv.increment` re-applies its TTL on every call, so a chatter
posting every 20 s against a 30 s cooldown would be refused forever, and a lapsed TTL key that is
re-created leaks one unit of the 10,000-key quota every time (`bundle_host_kv::backend` module
docs). Mutual exclusion for concurrent invocations of the SAME chatter is a short-lived lock key
(`kv.increment` to 1 = holder; deleted in `finally`, 10 s TTL only as a crash backstop) with a
double-checked cooldown read, so two parallel messages award exactly once.

## Leaderboard

`level.board` is one JSON array of at most `LEADERBOARD_SIZE` `[id, xp]` pairs, sorted by XP
descending then id. XP only ever grows, so top-K is maintainable exactly by comparing a chatter's
new total to the board floor on each award. The board read-modify-write is NOT atomic across
different chatters: a lost update is repaired by that chatter's next award, and the per-chatter
`level.xp.<pseudonym>` counter (the source of truth, `kv.increment`) is never affected.

## Capacity

Two permanent keys per chatter (`level.xp.*`, `level.ts.*`) against the host's 10,000 live keys
per `(tenant, community, app)`: about 4,900 distinct chatters per community. Past that the host
rejects new keys, the award raises `_KvFailure`, and the failure is logged (`level.kv_failure`).

Replies are built only from bundle-validated `{user:<id>}` tokens and integers -- no chatter text
is ever echoed -- and are still passed through `sanitize_user_text` with exactly those tokens
allow-listed, so a future edit that interpolates text cannot reopen mention injection.

Activation-gated only -- no feature flag: the platform's installed -> enabled -> activated check
decides whether this bundle runs.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from math import isqrt
from typing import Any

from waddle_sdk import kv, log, relay
from waddle_sdk.command import (
    CommandSpec,
    CommandUsageError,
    ParsedCommand,
    parse_command,
)
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.safe_text import sanitize_user_text

COMMAND = "!level"

#: `leaderboard` (alias `top`) is the only sub-module; `!level <@user>` is handled by
#: `_resolve_command` because an arbitrary token is neither a verb nor a sub-module.
SPEC = CommandSpec(name="level", sub_modules=frozenset({"leaderboard", "top"}))

#: XP granted per qualifying message.
XP_PER_MESSAGE = 5
#: Minimum seconds between two XP awards to the same chatter.
COOLDOWN_SECONDS = 30
#: XP needed for level 2; the curve is `1 + isqrt(xp // LEVEL_BASE_XP)`.
LEVEL_BASE_XP = 100
#: Entries kept on, and shown by, the leaderboard (a Twitch line holds ~500 chars).
LEADERBOARD_SIZE = 5
#: Crash backstop for the per-chatter award lock (normal path deletes it explicitly).
LOCK_TTL_SECONDS = 10

XP_KEY_PREFIX = "level.xp."
TS_KEY_PREFIX = "level.ts."
LOCK_KEY_PREFIX = "level.lock."
BOARD_KEY = "level.board"

_USER_TOKEN_RE = re.compile(r"^\{user:([A-Za-z0-9_:.-]+)\}$")
_BOARD_ID_RE = re.compile(r"^[A-Za-z0-9_:.-]+$")

_USAGE = "Usage: !level | !level @user | !level leaderboard"
_NO_NAME_LOOKUP = "I couldn't identify that user - mention them (e.g. @name)."
_NO_SELF_ID = "I couldn't identify you on this platform, so I can't track your level."
_UNAVAILABLE = "Level storage is unavailable right now - please try again."
_EMPTY_BOARD = "Nobody has earned any XP yet - start chatting!"


class _KvFailure(Exception):
    """Internal-only: a `kv` host call failed or stored data is corrupt. Never leaked."""


def level_for_xp(xp: int) -> int:
    """Derive a level (>= 1) from total XP: `1 + isqrt(xp // LEVEL_BASE_XP)`.

    Integer-only so the result is exact at every boundary: level 1 spans 0-99 XP, level 2
    100-399, level 3 400-899, and so on. Always recomputed, never stored, so retuning the
    curve needs no migration.
    """
    if xp <= 0:
        return 1
    return 1 + isqrt(xp // LEVEL_BASE_XP)


def xp_for_level(level: int) -> int:
    """Minimum XP needed to reach `level` (>= 1) -- the exact inverse of `level_for_xp`."""
    if level <= 1:
        return 0
    return LEVEL_BASE_XP * (level - 1) ** 2


def _parse_user_token(raw: object) -> str | None:
    """Extract the bare id from a `{user:<id>}` PII token, or `None` if `raw` isn't one.

    Grammar matches `core/egress_detokenizer`'s `{user:` + token chars + `}`. An escaped
    (`\\{user:..\\}`, i.e. typed by a chatter) or malformed placeholder never matches.
    """
    if not isinstance(raw, str):
        return None
    match = _USER_TOKEN_RE.match(raw.strip())
    return match.group(1) if match else None


def _user_token(user_id: str) -> str:
    """Render a bare id as the `{user:<id>}` placeholder the egress detokenizer resolves."""
    return f"{{user:{user_id}}}"


def _pseudonym(user_id: str) -> str:
    """One-way per-chatter key component: SHA-256 hex of `level|<id>` (charset-safe, no id)."""
    return hashlib.sha256(f"level|{user_id}".encode()).hexdigest()


def _event_seconds(event: PlatformEvent) -> int | None:
    """Whole-second epoch of the event's ingest stamp, or `None` if absent/malformed."""
    raw = event.occurred_at
    if not isinstance(raw, str) or not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        log.debug("level.stamp_unparseable")
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return int(stamp.timestamp())


def _in_cooldown(last: int | None, now: int) -> bool:
    """True when `now` is not at least `COOLDOWN_SECONDS` after `last` (signed on purpose)."""
    return last is not None and now - last < COOLDOWN_SECONDS


async def _kv_get(key: str) -> bytes | None:
    """`kv.get`, reclassifying the generated WIT `Err` into `_KvFailure` (fail-loud)."""
    try:
        result: bytes | None = await kv.get(key)
    except Exception as exc:  # noqa: BLE001 - classified like waddle_sdk.db/http
        raise _KvFailure("kv.get failed") from exc
    return result


async def _kv_set(key: str, value: bytes) -> None:
    """`kv.set` with no TTL (XP state is durable), reclassifying `Err` into `_KvFailure`."""
    try:
        await kv.set(key, value, ttl_seconds=0)
    except Exception as exc:  # noqa: BLE001
        raise _KvFailure("kv.set failed") from exc


async def _kv_delete(key: str) -> None:
    """`kv.delete`, reclassifying `Err` into `_KvFailure`."""
    try:
        await kv.delete(key)
    except Exception as exc:  # noqa: BLE001
        raise _KvFailure("kv.delete failed") from exc


async def _kv_increment(key: str, delta: int, ttl_seconds: int = 0) -> int:
    """`kv.increment` (atomic), reclassifying `Err` into `_KvFailure`."""
    try:
        total: int = await kv.increment(key, delta, ttl_seconds=ttl_seconds)
    except Exception as exc:  # noqa: BLE001
        raise _KvFailure("kv.increment failed") from exc
    return total


async def _read_int(key: str) -> int | None:
    """Read an integer-valued key; `None` if unset, `_KvFailure` if the bytes are corrupt."""
    raw = await _kv_get(key)
    if raw is None:
        return None
    try:
        return int(raw.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _KvFailure("corrupt integer value") from exc


def _log_kv_failure(exc: _KvFailure, *, op: str) -> None:
    """Log a kv failure with the exception TYPE only (never keys, ids or text)."""
    cause = exc.__cause__
    log.error(
        "level.kv_failure",
        op=op,
        error_type=type(cause).__name__ if cause else type(exc).__name__,
    )


async def _load_board() -> list[tuple[str, int]]:
    """Return the stored leaderboard as `(id, xp)` pairs, best first.

    Corrupt or unexpected stored data is logged at ERROR and treated as an empty board: the
    next award rewrites it, so the display heals itself and the XP counters are never touched.
    """
    raw = await _kv_get(BOARD_KEY)
    if raw is None:
        return []
    entries: list[tuple[str, int]] = []
    try:
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, list):
            raise ValueError("board is not a list")
        for item in data:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or not isinstance(item[0], str)
                or not _BOARD_ID_RE.match(item[0])
                or not isinstance(item[1], int)
                or isinstance(item[1], bool)
                or item[1] < 0
            ):
                raise ValueError("board entry is malformed")
            entries.append((item[0], item[1]))
    except (UnicodeDecodeError, ValueError) as exc:
        log.error("level.board_corrupt", error_type=type(exc).__name__)
        return []
    return _rank(dict(entries))


def _rank(scores: dict[str, int]) -> list[tuple[str, int]]:
    """Order `{id: xp}` best first (XP descending, id ascending) and cap at the board size."""
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return ordered[:LEADERBOARD_SIZE]


async def _update_board(user_id: str, total: int) -> None:
    """Fold one chatter's new XP total into the stored top-K, writing only on a change."""
    current = await _load_board()
    merged = dict(current)
    merged[user_id] = total
    updated = _rank(merged)
    if updated == current:
        return
    payload = json.dumps([[uid, xp] for uid, xp in updated], separators=(",", ":"))
    await _kv_set(BOARD_KEY, payload.encode("utf-8"))


async def _award(user_id: str, now: int) -> int | None:
    """Award `XP_PER_MESSAGE` if the cooldown allows; return the new total, else `None`.

    Fast path is one `kv.get` (the spam case). The award path takes a short-lived per-chatter
    lock and RE-checks the cooldown under it, so concurrent messages award exactly once.
    """
    pseudonym = _pseudonym(user_id)
    ts_key = f"{TS_KEY_PREFIX}{pseudonym}"
    if _in_cooldown(await _read_int(ts_key), now):
        log.debug("level.award_skipped", reason="cooldown")
        return None

    lock_key = f"{LOCK_KEY_PREFIX}{pseudonym}"
    if await _kv_increment(lock_key, 1, LOCK_TTL_SECONDS) != 1:
        log.debug("level.award_skipped", reason="contended")
        return None
    try:
        if _in_cooldown(await _read_int(ts_key), now):
            log.debug("level.award_skipped", reason="cooldown_recheck")
            return None
        # Stamp BEFORE incrementing: a failure in between under-awards, never over-awards.
        await _kv_set(ts_key, str(now).encode("ascii"))
        total = await _kv_increment(f"{XP_KEY_PREFIX}{pseudonym}", XP_PER_MESSAGE)
        await _update_board(user_id, total)
        log.debug("level.awarded", gain=XP_PER_MESSAGE)
        return total
    finally:
        await _kv_delete(lock_key)


async def _record_activity(event: PlatformEvent, actor: str | None) -> None:
    """Passive accrual for one chat line; failures are logged loudly, never raised."""
    if actor is None:
        log.debug("level.skip_unidentified", platform=event.platform)
        return
    now = _event_seconds(event)
    if now is None:
        log.error("level.bad_timestamp", platform=event.platform)
        return
    try:
        await _award(actor, now)
    except _KvFailure as exc:
        _log_kv_failure(exc, op="award")


def _safe_reply(text: str, tokens: tuple[str, ...]) -> str:
    """Final-line defense: sanitize the reply with only this reply's own tokens allow-listed.

    The templates contain no chatter text, so this is a no-op today; it stays so any future
    edit that interpolates user text cannot re-open `{user:..}` forgery or mass mentions.
    """
    cleaned: str = sanitize_user_text(text, allow_tokens=tokens)
    return cleaned


async def _stats_reply(user_id: str) -> str:
    """One chatter's XP and level line, e.g. `{user:x} is level 3 with 450 XP (450 more ...)`."""
    xp = await _read_int(f"{XP_KEY_PREFIX}{_pseudonym(user_id)}") or 0
    level = level_for_xp(xp)
    remaining = xp_for_level(level + 1) - xp
    return _safe_reply(
        f"{_user_token(user_id)} is level {level} with {xp} XP "
        f"({remaining} XP to level {level + 1}).",
        (user_id,),
    )


async def _board_reply() -> str:
    """The top-K leaderboard on one line, names rendered by the egress detokenizer."""
    board = await _load_board()
    if not board:
        return _EMPTY_BOARD
    lines = [
        f"{rank}. {_user_token(uid)} L{level_for_xp(xp)} ({xp} XP)"
        for rank, (uid, xp) in enumerate(board, start=1)
    ]
    return _safe_reply(
        "Top levels: " + " | ".join(lines), tuple(uid for uid, _ in board)
    )


def _resolve_command(
    parsed: ParsedCommand | None, stripped: str
) -> tuple[str, str | None]:
    """Map the parser's result (or its `!level <@user>` fallback) to `(command, target)`."""
    if parsed is not None:
        if parsed.sub_module is None and parsed.option is None:
            return "self", None
        if parsed.sub_module in ("leaderboard", "top") and parsed.option is None:
            return "board", None
        return "usage", None
    # parse_command() rejects the one shape it cannot express: a bare target token.
    _, _, rest = stripped.partition(" ")
    rest = rest.strip()
    if rest and " " not in rest:
        return "other", rest
    return "usage", None


async def _answer(stripped: str, actor: str | None) -> str:
    """Build the `!level` reply (reads only; the caller accrues XP afterwards)."""
    try:
        parsed: ParsedCommand | None = parse_command(stripped, SPEC)
    except CommandUsageError:
        parsed = None
    command, target = _resolve_command(parsed, stripped)
    if command == "board":
        return await _board_reply()
    if command == "self":
        if actor is None:
            return _NO_SELF_ID
        return await _stats_reply(actor)
    if command == "other":
        target_id = _parse_user_token(target)
        if target_id is None:
            return _NO_NAME_LOOKUP
        return await _stats_reply(target_id)
    return _USAGE


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: accrue XP for every line, answer `!level`.

    Returns `None` for non-chat events/payloads and for every non-command line (which still
    accrues XP as a side effect). Command-path `kv` failures are logged and answered
    explicitly; passive-accrual failures are logged at ERROR (no reply is possible).
    """
    if event.event_type != "chat.message":
        return None
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None

    actor = _parse_user_token(event.actor)
    stripped = text.strip()
    reply: str | None = None
    if stripped.partition(" ")[0].lower() == COMMAND:
        try:
            reply = await _answer(stripped, actor)
        except _KvFailure as exc:
            _log_kv_failure(exc, op="read")
            reply = _UNAVAILABLE

    await _record_activity(event, actor)

    if reply is None:
        return None
    log.info("level.transform matched", op="reply")
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={"channel_id": event.payload.get("channel_id"), "text": reply},
        occurred_at=event.occurred_at,
    )


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
    """Implement `action-stage.dispatch`: relay the reply text `transform` already built.

    Raises:
        ValueError: The payload is missing `channel_id` or `text` (defensive).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    text = payload.get("text")
    if not channel_id:
        raise ValueError(
            "level reply requires a channel_id from the inbound chat.message"
        )
    if not isinstance(text, str) or not text:
        raise ValueError("level reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("level.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
