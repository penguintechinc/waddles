"""Spectrum organization sync primitives -- roster + event diffing and snapshot stores (gh #101).

`receivers/spectrum_poll.py` polls an RSI Spectrum community's member roster and
event list. Both are *snapshots* (the full current state), not append-only streams
like forum threads / lobby messages, so ingest has to turn "state now" into
"what changed". This module holds that logic -- deliberately free of any RSI wire
knowledge (that lives in `RsiRestProvider`) so it is pure and exhaustively testable:

* **Dataclasses** -- `SpectrumMember` / `SpectrumEvent` (one parsed record each) and
  `RosterChange` / `EventChange` (one detected delta each).
* **Diffing** -- `diff_roster` / `diff_events` compare the previous snapshot with
  the current one. The first run only *primes* (no events) unless `emit_backlog`.
* **Anomaly guard** -- a glitchy RSI response (empty page, truncated list) must
  never look like "everyone left" and strip roles downstream. A roster that
  shrinks past `max_departure_ratio` (or empties) raises `SnapshotAnomalyError`
  and the snapshot is NOT advanced; the poller treats it as a transient error.
* **Snapshot stores** -- `MemorySnapshotStore` (tests / standalone) and
  `RedisSnapshotStore` (production: survives restarts and lease failover, so joins
  and leaves that happen while svc-ingest is down are still detected). A store
  outage raises `SnapshotStoreError` -- it is never read as "no snapshot".

Stored snapshots hold opaque ids and a role/content fingerprint only -- never
display names, handles or message text (PII stays out of Valkey; a `left` change
therefore carries only the member id).

Delivery is at-least-once: the poller advances the snapshot only AFTER the
changes were yielded, so a crash in between re-emits them on the next start.
Role grants/revokes downstream are idempotent, which makes that the safe side.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from opentelemetry import metrics
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

KIND_ROSTER = "roster"
KIND_EVENTS = "events"
#: Source kinds that are snapshot-diffed (vs the append-only forum/lobby kinds).
ORG_KINDS = frozenset({KIND_ROSTER, KIND_EVENTS})

CHANGE_JOINED = "joined"
CHANGE_LEFT = "left"
CHANGE_ROLES_CHANGED = "roles_changed"

CHANGE_CREATED = "created"
CHANGE_UPDATED = "updated"
CHANGE_CANCELLED = "cancelled"
CHANGE_REMOVED = "removed"
CHANGE_RSVP_CHANGED = "rsvp_changed"

STATUS_SCHEDULED = "scheduled"
STATUS_CANCELLED = "cancelled"

#: Snapshot schema version -- bump (and handle) if the stored shape ever changes.
SNAPSHOT_VERSION = 1
DEFAULT_MAX_DEPARTURE_RATIO = 0.5
#: Below this many previous members the ratio guard is skipped (tiny orgs churn
#: legitimately); an outright empty roster is still always an anomaly.
DEFAULT_GUARD_MIN_SIZE = 10
#: Upcoming events needed before a mass-vanish counts as an anomaly.
EVENT_GUARD_MIN_SIZE = 5
DEFAULT_SNAPSHOT_TTL_S = 30 * 24 * 3600
#: Event descriptions are upstream user content; bound what rides the stream.
MAX_DESCRIPTION_CHARS = 2000

_meter = metrics.get_meter("waddles.svc_ingest.spectrum")
_changes_counter = _meter.create_counter(
    "waddles_spectrum_org_changes_total",
    description="Spectrum org roster/event changes detected, by kind and change.",
)
_anomaly_counter = _meter.create_counter(
    "waddles_spectrum_snapshot_anomalies_total",
    description="Polls rejected by the shrink guard (snapshot not advanced), by kind.",
)
_snapshot_reset_counter = _meter.create_counter(
    "waddles_spectrum_snapshot_resets_total",
    description="Stored snapshots discarded as unreadable (poller re-primed), by reason.",
)
_snapshot_size = _meter.create_histogram(
    "waddles_spectrum_org_snapshot_size",
    description="Members/events seen in one Spectrum org poll, by kind.",
)


class SnapshotStoreError(Exception):
    """The snapshot backend is unreachable/failed -- transient, never treated as 'no snapshot'."""


class SnapshotAnomalyError(Exception):
    """The new snapshot shrank implausibly -- RSI glitch suspected; snapshot not advanced."""


@dataclass(slots=True, frozen=True)
class SpectrumMember:
    """One parsed org-roster entry; `roles` is `(role_id, role_name)` pairs sorted by id."""

    member_id: str
    display_name: str | None
    roles: tuple[tuple[str, str | None], ...] = ()

    @property
    def role_ids(self) -> tuple[str, ...]:
        """Role ids only, in the stable sorted order."""
        return tuple(role_id for role_id, _ in self.roles)

    @property
    def role_names(self) -> tuple[str, ...]:
        """Known role names (unnamed roles omitted), ordered like `role_ids`."""
        return tuple(name for _, name in self.roles if name)

    def fingerprint(self) -> str:
        """Stable role signature stored in the snapshot (ids only -- no names)."""
        return ",".join(self.role_ids)


@dataclass(slots=True, frozen=True)
class SpectrumEvent:
    """One parsed org event. `rsvp_count` is the attending headcount (no per-user RSVPs)."""

    event_id: str
    title: str | None
    description: str | None
    starts_epoch: float | None
    ends_epoch: float | None
    location: str | None
    organizer_id: str | None
    status: str = STATUS_SCHEDULED
    rsvp_count: int | None = None

    def fingerprint(self) -> str:
        """Content hash of everything except the RSVP count (which diffs separately)."""
        blob = json.dumps(
            [
                self.title,
                self.description,
                self.starts_epoch,
                self.ends_epoch,
                self.location,
                self.status,
            ],
            separators=(",", ":"),
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


@dataclass(slots=True, frozen=True)
class RosterChange:
    """One detected roster delta. `display_name` is `None` for `left` (names aren't stored)."""

    change: str
    member_id: str
    display_name: str | None = None
    roles_added: tuple[str, ...] = ()
    roles_removed: tuple[str, ...] = ()
    role_names: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class EventChange:
    """One detected event delta. `event` is `None` for `removed` (only the id is retained)."""

    change: str
    event_id: str
    event: SpectrumEvent | None = None
    rsvp_previous: int | None = None


@dataclass(slots=True)
class RosterDiff:
    """Result of `diff_roster`: the deltas plus the snapshot to persist after emitting them."""

    changes: list[RosterChange]
    snapshot: dict[str, Any]
    primed: bool


@dataclass(slots=True)
class EventsDiff:
    """Result of `diff_events`: the deltas plus the snapshot to persist after emitting them."""

    changes: list[EventChange]
    snapshot: dict[str, Any]
    primed: bool
    pruned: int = field(default=0)


def roster_snapshot(members: Mapping[str, SpectrumMember]) -> dict[str, Any]:
    """Serialize a roster to its stored form: `{member_id: role-fingerprint}` (no names)."""
    return {
        "v": SNAPSHOT_VERSION,
        "members": {mid: m.fingerprint() for mid, m in members.items()},
    }


def events_snapshot(events: Mapping[str, SpectrumEvent]) -> dict[str, Any]:
    """Serialize an event list to its stored form: id -> content hash, RSVP, start, status."""
    return {
        "v": SNAPSHOT_VERSION,
        "events": {
            eid: {
                "fp": e.fingerprint(),
                "rsvp": e.rsvp_count,
                "start": e.starts_epoch,
                "status": e.status,
            }
            for eid, e in events.items()
        },
    }


def _usable_section(
    previous: Mapping[str, Any] | None, section: str, kind: str
) -> Mapping[str, Any] | None:
    """`previous` if it carries a mapping under `section`, else `None` (re-prime, loudly).

    A snapshot that decoded fine but lacks the expected section is as unreadable as
    corrupt JSON -- discarding it (ERROR + counter) beats crashing the poller.
    """
    if previous is None or isinstance(previous.get(section), Mapping):
        return previous
    _snapshot_reset_counter.add(1, {"reason": "missing_section"})
    logger.error(
        "spectrum.snapshot_malformed kind=%s missing=%s -- discarding, re-priming", kind, section
    )
    return None


def _split_roles(fingerprint: str) -> set[str]:
    """Role ids back out of a stored fingerprint."""
    return {r for r in fingerprint.split(",") if r}


def diff_roster(
    previous: Mapping[str, Any] | None,
    current: Sequence[SpectrumMember],
    *,
    emit_backlog: bool = False,
    max_departure_ratio: float = DEFAULT_MAX_DEPARTURE_RATIO,
    guard_min_size: int = DEFAULT_GUARD_MIN_SIZE,
) -> RosterDiff:
    """Diff the stored roster snapshot against the current roster.

    `previous=None` primes: no changes unless `emit_backlog`, in which case every
    current member is reported as `joined`. Otherwise reports `joined`, `left`
    and `roles_changed` (role ids added/removed). Raises `SnapshotAnomalyError`
    when the roster emptied or shrank past `max_departure_ratio` (see module doc).
    """
    previous = _usable_section(previous, "members", KIND_ROSTER)
    now_members: dict[str, SpectrumMember] = {m.member_id: m for m in current}
    _snapshot_size.record(len(now_members), {"kind": KIND_ROSTER})
    new_snapshot = roster_snapshot(now_members)

    if previous is None:
        backlog: list[RosterChange] = (
            [
                RosterChange(
                    CHANGE_JOINED,
                    m.member_id,
                    m.display_name,
                    roles_added=m.role_ids,
                    role_names=m.role_names,
                )
                for m in now_members.values()
            ]
            if emit_backlog
            else []
        )
        _count(KIND_ROSTER, backlog)
        return RosterDiff(backlog, new_snapshot, primed=True)

    prev_members: Mapping[str, str] = previous["members"]
    departed = [mid for mid in prev_members if mid not in now_members]
    if prev_members and not now_members:
        _anomaly_counter.add(1, {"kind": KIND_ROSTER})
        raise SnapshotAnomalyError(f"roster emptied (was {len(prev_members)} members)")
    if (
        len(prev_members) >= guard_min_size
        and len(departed) / len(prev_members) > max_departure_ratio
    ):
        _anomaly_counter.add(1, {"kind": KIND_ROSTER})
        raise SnapshotAnomalyError(
            f"roster shrank {len(departed)}/{len(prev_members)} in one poll "
            f"(> {max_departure_ratio:.0%})"
        )

    changes: list[RosterChange] = []
    for mid, member in now_members.items():
        if mid not in prev_members:
            changes.append(
                RosterChange(
                    CHANGE_JOINED,
                    mid,
                    member.display_name,
                    roles_added=member.role_ids,
                    role_names=member.role_names,
                )
            )
        elif prev_members[mid] != member.fingerprint():
            before = _split_roles(prev_members[mid])
            after = set(member.role_ids)
            changes.append(
                RosterChange(
                    CHANGE_ROLES_CHANGED,
                    mid,
                    member.display_name,
                    roles_added=tuple(sorted(after - before)),
                    roles_removed=tuple(sorted(before - after)),
                    role_names=member.role_names,
                )
            )
    changes.extend(RosterChange(CHANGE_LEFT, mid) for mid in departed)
    _count(KIND_ROSTER, changes)
    return RosterDiff(changes, new_snapshot, primed=False)


def diff_events(
    previous: Mapping[str, Any] | None,
    current: Sequence[SpectrumEvent],
    *,
    now: float,
    emit_backlog: bool = False,
    max_departure_ratio: float = DEFAULT_MAX_DEPARTURE_RATIO,
) -> EventsDiff:
    """Diff the stored event snapshot against the current event list.

    Reports `created`, `updated` (content changed), `cancelled` (status flipped),
    `rsvp_changed` (attending headcount) and `removed`. An event that vanishes
    *after* its start time simply ended -- it is pruned silently (counted in
    `EventsDiff.pruned`), only a vanish before start is a `removed`. A mass
    vanish of upcoming events raises `SnapshotAnomalyError` (RSI glitch guard).
    """
    previous = _usable_section(previous, "events", KIND_EVENTS)
    now_events: dict[str, SpectrumEvent] = {e.event_id: e for e in current}
    _snapshot_size.record(len(now_events), {"kind": KIND_EVENTS})
    new_snapshot = events_snapshot(now_events)

    if previous is None:
        backlog: list[EventChange] = (
            [EventChange(CHANGE_CREATED, e.event_id, e) for e in now_events.values()]
            if emit_backlog
            else []
        )
        _count(KIND_EVENTS, backlog)
        return EventsDiff(backlog, new_snapshot, primed=True)

    prev_events: Mapping[str, Mapping[str, Any]] = previous["events"]
    vanished = [eid for eid in prev_events if eid not in now_events]
    upcoming_vanished = [eid for eid in vanished if _is_upcoming(prev_events[eid], now)]
    upcoming_before = [eid for eid in prev_events if _is_upcoming(prev_events[eid], now)]
    if (
        len(upcoming_before) >= EVENT_GUARD_MIN_SIZE
        and len(upcoming_vanished) / len(upcoming_before) > max_departure_ratio
    ):
        _anomaly_counter.add(1, {"kind": KIND_EVENTS})
        raise SnapshotAnomalyError(
            f"{len(upcoming_vanished)}/{len(upcoming_before)} upcoming events vanished in one poll"
        )

    changes: list[EventChange] = []
    for eid, event in now_events.items():
        old = prev_events.get(eid)
        if old is None:
            changes.append(EventChange(CHANGE_CREATED, eid, event))
            continue
        if event.status == STATUS_CANCELLED and old.get("status") != STATUS_CANCELLED:
            changes.append(EventChange(CHANGE_CANCELLED, eid, event))
        elif old.get("fp") != event.fingerprint():
            changes.append(EventChange(CHANGE_UPDATED, eid, event))
        old_rsvp = old.get("rsvp")
        if event.rsvp_count is not None and event.rsvp_count != old_rsvp:
            changes.append(
                EventChange(CHANGE_RSVP_CHANGED, eid, event, rsvp_previous=_int_or_none(old_rsvp))
            )
    changes.extend(EventChange(CHANGE_REMOVED, eid) for eid in upcoming_vanished)
    pruned = len(vanished) - len(upcoming_vanished)
    if pruned:
        logger.debug("spectrum.events_pruned count=%d (ended before this poll)", pruned)
    _count(KIND_EVENTS, changes)
    return EventsDiff(changes, new_snapshot, primed=False, pruned=pruned)


def _is_upcoming(entry: Mapping[str, Any], now: float) -> bool:
    """True if a stored event had not started yet (unknown start counts as upcoming)."""
    start = entry.get("start")
    return not isinstance(start, int | float) or start > now


def _int_or_none(value: object) -> int | None:
    """`value` if it is a real int (bools excluded), else `None`."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _count(kind: str, changes: Sequence[RosterChange | EventChange]) -> None:
    """Record one `waddles_spectrum_org_changes_total` data point per change."""
    for change in changes:
        _changes_counter.add(1, {"kind": kind, "change": change.change})


class SnapshotStore(Protocol):
    """Where the last-seen org snapshot lives -- survives restarts when Redis-backed."""

    async def load(self, key: str) -> dict[str, Any] | None:
        """Return the stored snapshot, or `None` if none exists. Raises `SnapshotStoreError`."""
        ...

    async def save(self, key: str, snapshot: Mapping[str, Any]) -> None:
        """Persist `snapshot` under `key`. Raises `SnapshotStoreError` on backend failure."""
        ...


class MemorySnapshotStore:
    """In-process snapshot store -- state is lost on restart (tests / standalone use)."""

    def __init__(self) -> None:
        """Start empty."""
        self._data: dict[str, dict[str, Any]] = {}

    async def load(self, key: str) -> dict[str, Any] | None:
        """Return a copy of the stored snapshot, or `None`."""
        stored = self._data.get(key)
        return json.loads(json.dumps(stored)) if stored is not None else None

    async def save(self, key: str, snapshot: Mapping[str, Any]) -> None:
        """Store a JSON round-tripped copy (so callers can't mutate it by reference)."""
        self._data[key] = json.loads(json.dumps(dict(snapshot)))


class RedisLike(Protocol):
    """The two Valkey methods `RedisSnapshotStore` needs -- narrow, easy to fake in tests."""

    def get(self, name: str, /) -> Awaitable[Any]:
        """GET `name`."""
        ...

    def set(self, name: str, value: str, /, *, ex: int | None = None) -> Awaitable[Any]:
        """SET `name` to `value` with an optional expiry in seconds."""
        ...


class RedisSnapshotStore:
    """Valkey-backed snapshot store; keys are `{prefix}:{key}` with a refreshed TTL.

    A backend failure raises `SnapshotStoreError` (the poller backs off) -- it is
    never read as "no snapshot", which would re-prime and silently skip changes.
    An *unreadable* stored value is the one exception: it is discarded with an
    ERROR log + `waddles_spectrum_snapshot_resets_total`, because a corrupt key
    would otherwise wedge the poller forever (the next poll re-primes; changes
    during that single gap are the accepted, loudly-reported cost).
    """

    def __init__(
        self, redis_client: RedisLike, *, prefix: str, ttl_s: int = DEFAULT_SNAPSHOT_TTL_S
    ) -> None:
        """Bind the Valkey client and the tenant-scoped key `prefix`."""
        self._redis = redis_client
        self._prefix = prefix.rstrip(":")
        self._ttl_s = ttl_s

    def _key(self, key: str) -> str:
        """Full Valkey key for a snapshot."""
        return f"{self._prefix}:{key}"

    async def load(self, key: str) -> dict[str, Any] | None:
        """GET + decode the snapshot; see class docstring for failure semantics."""
        try:
            raw = await self._redis.get(self._key(key))
        except (RedisError, OSError) as exc:
            raise SnapshotStoreError(f"snapshot load failed: {type(exc).__name__}") from exc
        if raw is None:
            return None
        try:
            decoded = json.loads(raw)
        except (ValueError, TypeError):
            self._discard(key, "not_json")
            return None
        if not isinstance(decoded, dict) or decoded.get("v") != SNAPSHOT_VERSION:
            self._discard(key, "bad_shape")
            return None
        return decoded

    @staticmethod
    def _discard(key: str, reason: str) -> None:
        """Log + count a discarded unreadable snapshot; the caller re-primes."""
        _snapshot_reset_counter.add(1, {"reason": reason})
        logger.error(
            "spectrum.snapshot_unreadable key=%s reason=%s -- discarding, poller will re-prime",
            key,
            reason,
        )

    async def save(self, key: str, snapshot: Mapping[str, Any]) -> None:
        """SET the JSON snapshot with the configured TTL."""
        try:
            await self._redis.set(
                self._key(key), json.dumps(dict(snapshot), separators=(",", ":")), ex=self._ttl_s
            )
        except (RedisError, OSError) as exc:
            raise SnapshotStoreError(f"snapshot save failed: {type(exc).__name__}") from exc
