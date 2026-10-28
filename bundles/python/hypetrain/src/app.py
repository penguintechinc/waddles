"""`!hypetrain` -> a shared, per-community chat hype/combo meter, kv-backed.

Chat activity builds ONE shared combo counter. Each counted message adds 1; the `!hype` keyword
adds `KEYWORD_WEIGHT`. The train stays alive while contributions keep arriving within
`WINDOW_SECONDS` of the previous one and resets after that much silence. Crossing a multiple of
`MILESTONE_STEP` announces `Hype x10!` (then x20, x30, ...), at most once per milestone and at
most once per `ANNOUNCE_GAP_SECONDS`, so a busy chat is never spammed. `!hypetrain` shows the
current combo; `!hypetrain reset` (broadcaster/moderator only) ends the train.

## Window and reset

All time is the event's own ingest stamp (`occurred_at`, whole seconds), so redelivery is
idempotent and no wall clock or TTL is involved. A contribution at `t` continues the train when
`t - last <= WINDOW_SECONDS`; a later one starts a new train at its own weight (the previous
peak survives in `best`). An event older than `last - WINDOW_SECONDS` is a stale replay and is
ignored. Deliberately NOT a TTL'd `kv.increment`: a lapsed TTL key that is re-created leaks one
unit of the host's 10,000-key quota every time (`bundle_host_kv::backend` module docs), and every
train would be one such cycle.

## State

`hypetrain.state` is one JSON object (`c` combo, `l` last counted time, `b` best ever, `m` highest
milestone announced for this train, `t` time of the last announcement) -- one read and one write
per counted contribution. Concurrent invocations can lose an increment or, rarely, announce
twice; both are cosmetic for a meter and bounded by the announcement gap, so no lock is taken.
Corrupt state is logged at ERROR and replaced by a fresh train on the next contribution.

## Per-chatter spam guard

One chatter counts at most once per `ACTOR_GAP_SECONDS`, so a spammer's contribution is capped
at 12 per minute no matter how fast they type (reaching x10 alone takes 45 s of continuous
chatting, and announcements are still gap-limited). The guard needs no per-chatter keys: a
chatter hashes into one of `SLOT_COUNT` fixed slots (`hypetrain.slot.<n>`) holding the last
counted time for that slot. Footprint is a constant 257 keys; a hash collision can only make
the meter UNDER-count, never over-count.

## PII

No chatter id, name or text is ever stored or logged. The `{user:<id>}` token in `event.actor` is
hashed to pick a slot and then discarded; the stored values are integers only. A non-token actor
(raw username, no actor, forged `\\{user:..\\}`) is not counted. Replies contain no chatter text
at all and are still passed through `sanitize_user_text` as a final defense.

`!hype` is also the trigger word of the `hype` bundle, which owns its reply; this bundle only
counts the line and stays silent unless a milestone is reached.

Activation-gated only -- no feature flag: the platform's installed -> enabled -> activated check
decides whether this bundle runs.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from waddle_sdk import kv, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, parse_command
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.safe_text import sanitize_user_text

COMMAND = "!hypetrain"
#: Explicit cheer words that count as a boosted contribution (matched on the first token).
KEYWORDS = frozenset({"!hype"})
SPEC = CommandSpec(name="hypetrain")

#: A train survives this many seconds without a counted contribution.
WINDOW_SECONDS = 30
#: One chatter (slot) counts at most once per this many seconds.
ACTOR_GAP_SECONDS = 5
#: A milestone is announced each time the combo reaches a multiple of this.
MILESTONE_STEP = 10
#: Minimum seconds between two announcements, whatever the milestones.
ANNOUNCE_GAP_SECONDS = 20
#: Combo added by an ordinary chat line / by a `KEYWORDS` line.
MESSAGE_WEIGHT = 1
KEYWORD_WEIGHT = 3
#: Fixed number of per-chatter guard slots (constant key footprint).
SLOT_COUNT = 256

STATE_KEY = "hypetrain.state"
SLOT_KEY_PREFIX = "hypetrain.slot."

_USER_TOKEN_RE = re.compile(r"^\{user:[A-Za-z0-9_:.-]+\}$")

_USAGE = "Usage: !hypetrain | !hypetrain reset (reset is broadcaster/mod only)"
_DENIED = "Only the broadcaster or a moderator can reset the hype train."
_UNAVAILABLE = "Hype train storage is unavailable right now - please try again."
_NO_CLOCK = "Hype train can't read this message's time right now - please try again."


class _KvFailure(Exception):
    """Internal-only: a `kv` host call failed. Always caught in `transform`, never leaked."""


@dataclass(slots=True)
class _Train:
    """The shared meter: current combo, bookkeeping times and the all-time best."""

    combo: int = 0
    last: int = 0
    best: int = 0
    announced: int = 0
    announced_at: int | None = None

    def encode(self) -> bytes:
        """Serialize to the compact JSON stored under `STATE_KEY`."""
        body = {
            "c": self.combo,
            "l": self.last,
            "b": self.best,
            "m": self.announced,
            "t": self.announced_at,
        }
        return json.dumps(body, separators=(",", ":")).encode("utf-8")

    @classmethod
    def decode(cls, raw: bytes) -> _Train:
        """Parse stored state; raises `ValueError` on any shape or range violation."""
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("state is not an object")
        values: dict[str, int | None] = {}
        for field in ("c", "l", "b", "m", "t"):
            item = data.get(field)
            if field == "t" and item is None:
                values[field] = None
                continue
            if not isinstance(item, int) or isinstance(item, bool) or item < 0:
                raise ValueError("state field is malformed")
            values[field] = item
        return cls(
            combo=values["c"] or 0,
            last=values["l"] or 0,
            best=values["b"] or 0,
            announced=values["m"] or 0,
            announced_at=values["t"],
        )

    def alive(self, now: int) -> bool:
        """True while a train is running as of `now` (a counted contribution within the window)."""
        return self.combo > 0 and now - self.last <= WINDOW_SECONDS


def _parse_user_token(raw: object) -> str | None:
    """Return `raw` stripped if it is a well-formed `{user:<id>}` PII token, else `None`.

    Grammar matches `core/egress_detokenizer`'s `{user:` + token chars + `}`; an escaped or
    forged placeholder typed by a chatter never matches. The id is only hashed, never kept.
    """
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    return cleaned if _USER_TOKEN_RE.match(cleaned) else None


def _slot_key(user_token: str) -> str:
    """Guard-slot key for a chatter: a SHA-256-derived index in `[0, SLOT_COUNT)`."""
    digest = hashlib.sha256(f"hypetrain|{user_token}".encode()).hexdigest()
    return f"{SLOT_KEY_PREFIX}{int(digest[:8], 16) % SLOT_COUNT}"


def _event_seconds(event: PlatformEvent) -> int | None:
    """Whole-second epoch of the event's ingest stamp, or `None` if absent/malformed."""
    raw = event.occurred_at
    if not isinstance(raw, str) or not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        log.debug("hypetrain.stamp_unparseable")
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return int(stamp.timestamp())


def _is_privileged(payload: dict[str, Any]) -> bool:
    """Broadcaster/moderator check on STRICT booleans; absent or non-bool badges fail closed."""
    return payload.get("is_mod") is True or payload.get("is_broadcaster") is True


async def _kv_get(key: str) -> bytes | None:
    """`kv.get`, reclassifying the generated WIT `Err` into `_KvFailure` (fail-loud)."""
    try:
        result: bytes | None = await kv.get(key)
    except Exception as exc:  # noqa: BLE001 - classified like waddle_sdk.db/http
        raise _KvFailure("kv.get failed") from exc
    return result


async def _kv_set(key: str, value: bytes) -> None:
    """`kv.set` with no TTL (the meter is durable), reclassifying `Err` into `_KvFailure`."""
    try:
        await kv.set(key, value, ttl_seconds=0)
    except Exception as exc:  # noqa: BLE001
        raise _KvFailure("kv.set failed") from exc


def _log_kv_failure(exc: _KvFailure, *, op: str) -> None:
    """Log a kv failure with the exception TYPE only (never keys or values)."""
    cause = exc.__cause__
    log.error(
        "hypetrain.kv_failure",
        op=op,
        error_type=type(cause).__name__ if cause else type(exc).__name__,
    )


async def _load_train() -> _Train:
    """Read the shared meter; corrupt data is logged at ERROR and replaced by a fresh train."""
    raw = await _kv_get(STATE_KEY)
    if raw is None:
        return _Train()
    try:
        return _Train.decode(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        log.error("hypetrain.state_corrupt", error_type=type(exc).__name__)
        return _Train()


async def _save_train(train: _Train) -> None:
    """Persist the shared meter."""
    await _kv_set(STATE_KEY, train.encode())


async def _actor_may_count(user_token: str, now: int) -> bool:
    """Per-chatter spam guard: claim this chatter's slot unless it counted within the gap.

    The comparison is signed, so a replayed older event is refused as well. Read-then-set is
    deliberately lock-free: a race can only let one extra line count.
    """
    key = _slot_key(user_token)
    raw = await _kv_get(key)
    if raw is not None:
        try:
            last = int(raw.decode("ascii"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise _KvFailure("corrupt slot value") from exc
        if now - last < ACTOR_GAP_SECONDS:
            return False
    await _kv_set(key, str(now).encode("ascii"))
    return True


def _maybe_announce(train: _Train, now: int) -> str | None:
    """Return the milestone announcement due now (updating `train`), or `None`.

    Only the highest milestone reached is announced, never one already announced for this
    train, and never within `ANNOUNCE_GAP_SECONDS` of the previous announcement -- a suppressed
    milestone stays pending and is announced by a later contribution once the gap has passed.
    """
    milestone = train.combo // MILESTONE_STEP * MILESTONE_STEP
    if milestone < MILESTONE_STEP or milestone <= train.announced:
        return None
    if (
        train.announced_at is not None
        and now - train.announced_at < ANNOUNCE_GAP_SECONDS
    ):
        log.debug("hypetrain.announce_suppressed", reason="gap")
        return None
    train.announced = milestone
    train.announced_at = now
    return f"Hype x{milestone}! Keep the chat moving!"


async def _contribute(user_token: str, now: int, weight: int) -> str | None:
    """Count one contribution; return a milestone announcement if one is due."""
    if not await _actor_may_count(user_token, now):
        log.debug("hypetrain.skip", reason="actor_gap")
        return None
    train = await _load_train()
    if train.combo > 0 and now < train.last - WINDOW_SECONDS:
        log.debug("hypetrain.skip", reason="stale")
        return None
    if train.combo > 0 and now - train.last > WINDOW_SECONDS:
        log.debug("hypetrain.train_expired", peak=train.combo)
        train.combo = 0
        train.announced = 0
    train.combo += weight
    train.last = max(train.last, now)
    train.best = max(train.best, train.combo)
    announcement = _maybe_announce(train, now)
    await _save_train(train)
    log.debug("hypetrain.counted", weight=weight, combo=train.combo)
    return announcement


async def _status_reply(now: int) -> str:
    """Describe the meter as of `now`: running combo and time left, or the all-time best."""
    train = await _load_train()
    if train.alive(now):
        left = WINDOW_SECONDS - (now - train.last)
        return (
            f"Hype train: x{train.combo} (best x{train.best}) - "
            f"{left}s left, keep chatting!"
        )
    if train.best > 0:
        return f"No hype train running. Best so far: x{train.best}. Chat to start one!"
    return "No hype train yet - chat (or say !hype) to start one!"


async def _reset_reply() -> str:
    """End the running train (best and announcement spacing are kept) and confirm."""
    train = await _load_train()
    train.combo = 0
    train.announced = 0
    await _save_train(train)
    log.info("hypetrain.reset")
    return "Hype train reset."


async def _handle_command(event: PlatformEvent, stripped: str, now: int | None) -> str:
    """Answer `!hypetrain` / `!hypetrain reset`; anything else is usage (never echoed)."""
    try:
        parsed = parse_command(stripped, SPEC)
    except CommandUsageError:
        log.debug("hypetrain.usage", reason="unparseable")
        return _USAGE
    if parsed.sub_module is None and parsed.option is None:
        if now is None:
            log.error("hypetrain.bad_timestamp", platform=event.platform)
            return _NO_CLOCK
        return await _status_reply(now)
    if parsed.sub_module is None and parsed.option == "reset" and parsed.args is None:
        if not _is_privileged(event.payload):
            log.info("hypetrain.permission_denied", action="reset")
            return _DENIED
        return await _reset_reply()
    return _USAGE


def _reply_event(event: PlatformEvent, text: str) -> PlatformEvent:
    """Wrap a reply for the action stage, sanitized last (no user text is ever interpolated)."""
    safe: str = sanitize_user_text(text)
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={"channel_id": event.payload.get("channel_id"), "text": safe},
        occurred_at=event.occurred_at,
    )


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: count chat activity, answer `!hypetrain`.

    Returns `None` for non-chat events/payloads and for every counted line that reaches no
    milestone. Command-path `kv` failures are answered explicitly; passive-path failures are
    logged at ERROR (no reply is possible). `!hypetrain` lines are control input and never
    count toward the combo.
    """
    if event.event_type != "chat.message":
        return None
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None

    stripped = text.strip()
    head = stripped.partition(" ")[0].lower()
    now = _event_seconds(event)

    if head == COMMAND:
        try:
            reply = await _handle_command(event, stripped, now)
        except _KvFailure as exc:
            _log_kv_failure(exc, op="command")
            reply = _UNAVAILABLE
        log.info("hypetrain.transform matched", op="reply")
        return _reply_event(event, reply)

    user_token = _parse_user_token(event.actor)
    if user_token is None:
        log.debug("hypetrain.skip", reason="unidentified")
        return None
    if now is None:
        log.error("hypetrain.bad_timestamp", platform=event.platform)
        return None

    weight = KEYWORD_WEIGHT if head in KEYWORDS else MESSAGE_WEIGHT
    try:
        announcement = await _contribute(user_token, now, weight)
    except _KvFailure as exc:
        _log_kv_failure(exc, op="count")
        return None
    if announcement is None:
        return None
    log.info("hypetrain.transform matched", op="announce")
    return _reply_event(event, announcement)


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
            "hypetrain reply requires a channel_id from the inbound chat.message"
        )
    if not isinstance(text, str) or not text:
        raise ValueError("hypetrain reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("hypetrain.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
