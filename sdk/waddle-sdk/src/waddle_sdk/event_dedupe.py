"""Race-free, replay-safe event claims for bundles whose side effects move money.

The pipeline is at-least-once: an event whose handler crashed, timed out or was never acked is
delivered again. The host already makes ``economy.wager`` / ``economy.transfer`` idempotent per
event (``waddle_sdk.economy``), but a bundle's OWN state mutations are not covered -- a jackpot pot
credit, a win/play counter, a steal budget. Replayed, they double-credit, and a pot that is
credited twice pays out money that was only debited once.

This module is the guard. A bundle derives the event's replay-stable id from the payload
(:func:`event_id_from_payload`), refuses events too old to dedupe (:func:`is_fresh`), and records
the event with :func:`claim` **before** its first mutation, skipping every side effect when the
claim reports a duplicate. The claim is recorded BEFORE the mutation on purpose (at-most-once): a
crash after the claim loses one play, it can never mint a second one.

**The primitive: one TTL'd ``increment`` per event.** ``claim`` is a single
``community_kv.increment("<namespace>.seen.<digest>", 1, SEEN_TTL_SECONDS)``. ``kv.increment`` is
the only atomic compare-and-swap the SDK has, so the returned counter is the whole protocol:
``1`` means this call created the marker (first delivery -- process it), anything larger means the
event was already claimed (a redelivery, or a concurrent duplicate that lost the race -- skip it).
There is no read-modify-write window: two consumers holding the same stream entry at the same
instant both ``increment``, the host serializes them, and exactly one sees ``1``. The old
last-16-ids-per-actor ring was a get-then-set (a TOCTOU window) that also forgot any event older
than 16 claims; one marker per event has neither flaw. It also needs no caller-side gate (the
ring was only safe behind an ``increment`` cooldown), so it is correct for a bundle with no
cooldown at all.

**Where the horizon is, and how it is closed.** The marker lives ``SEEN_TTL_SECONDS``; a
redelivery after it lapses would look like a first delivery. Two things close that:

- :func:`is_fresh` refuses (the caller skips, silently) any event whose ingest stamp
  (``PlatformEvent.occurred_at``, set once by ``svc-ingest`` and carried unchanged through every
  re-enqueue and redelivery) is older than ``MAX_EVENT_AGE_SECONDS``. A redelivery inside that age
  limit is at most ``MAX_EVENT_AGE_SECONDS`` after the first claim, so its marker is still live
  (``SEEN_TTL_SECONDS`` adds ``EVENT_CLOCK_SKEW_SECONDS`` for the clock difference between
  executors); a redelivery past it is refused by the age check. So an event is either deduped
  or dropped -- never silently replayed. The spine reclaims an un-acked entry after 30 s idle and
  dead-letters it after 5 deliveries (penguin-libs ``rust-spine`` defaults ``SPINE_CLAIM_IDLE_MS``
  / ``SPINE_MAX_DELIVERIES``, operator-tunable), far inside the window; only a consumer outage
  longer than ``MAX_EVENT_AGE_SECONDS`` reaches the age fence.
- A missing, unparseable or future-dated stamp raises :class:`EventIdError` -- an economy
  mutation whose age cannot be established is refused loudly, never processed unfenced.

Residual: the fence trusts the ingest stamp. A platform that itself re-sends a message and
``svc-ingest`` accepts as a NEW stream entry (same ``message_id``, fresh stamp) is covered by the
marker TTL alone; ``svc-ingest`` dedupes platform redeliveries on its own (Twitch EventSub: 600 s
replay window, 900 s id dedupe -- the same two numbers :data:`MAX_EVENT_AGE_SECONDS` and
:data:`SEEN_TTL_SECONDS` use), so the two layers overlap rather than leave a gap.

**Storage and quota.** The ``kv`` host charges one unit of a 10,000-key per-(app, community) quota
per live key. Per-event markers are only sustainable because the host reclaims a key's quota unit
when its TTL lapses (``core/bundle_host_kv`` live expiry-scored index, commit 4ad3eaae; BEFORE that
fix every TTL'd key leaked its unit forever and per-event markers killed a bundle after 10,000
events -- this module requires the fix). Live markers are bounded by the claims made in the last
``SEEN_TTL_SECONDS`` (900 s): ~11 claims/s sustained uses the whole quota, so a bundle should
claim only AFTER its cheap per-actor gates (cooldown, funds) so one chatter cannot mint markers
faster than its own cooldown allows. When the quota is full ``increment`` raises, the claim fails
LOUD, and the bundle refuses the mutation (fail closed: availability is lost, money never is).
Residual: a swarm of linked, funded accounts can still hold the quota full for up to
``SEEN_TTL_SECONDS`` at a time; the follow-up is a host-level per-actor kv write-rate/new-key
limit (the host, not the bundle, knows the actor).

Keys carry only a truncated SHA-256 of the platform-prefixed event id -- never a handle, uuid or
raw platform message id.

**Which id.** The inbound chat payload's ``message_id`` -- the platform's own id for the message
(Discord snowflake, Twitch ``id`` tag). It is assigned by the platform, so a chatter cannot choose
or collide it, and it is identical on every redelivery and re-enqueue of the same message. The WIT
``context.message-id`` (the Valkey stream entry id) is deliberately NOT used: the host does not
populate it today (``get-context`` returns tenant/community/app only), and it changes if the entry
is re-enqueued. A bundle's ``transform`` must therefore forward ``payload["message_id"]``.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from waddle_sdk import clock, community_kv

#: Payload key a bundle's ``transform`` must forward from the inbound chat event.
EVENT_ID_PAYLOAD_KEY = "message_id"

#: Longest accepted platform message id (Discord snowflakes / Twitch UUIDs are far shorter).
MAX_EVENT_ID_LEN = 512

#: An event ingested longer ago than this is never processed (see the module docstring).
MAX_EVENT_AGE_SECONDS = 600

#: Slack for the wall-clock difference between the ingest pod and the executors (also the furthest
#: into the future an event stamp may be before it is refused as forged/skewed).
EVENT_CLOCK_SKEW_SECONDS = 300

#: Marker lifetime: must outlive every redelivery :func:`is_fresh` still lets through.
SEEN_TTL_SECONDS = MAX_EVENT_AGE_SECONDS + EVENT_CLOCK_SKEW_SECONDS

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MILLISECOND = timedelta(milliseconds=1)
_DIGEST_HEX_LEN = 32
_NAMESPACE_RE = re.compile(r"^[a-z0-9_]{1,32}$")
#: Strict RFC 3339 (what ``svc-ingest`` stamps): offset mandatory, 1-9 fractional digits.
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$"
)


class EventIdError(ValueError):
    """The event carries no usable replay-stable id/stamp -- the caller must refuse, never guess."""


class DedupeStateError(RuntimeError):
    """The stored marker is not one this module wrote -- fail closed rather than reset it."""


def event_id_from_payload(payload: Mapping[str, Any], *, platform: str) -> str:
    """Return the replay-stable id of the event being handled, or raise :class:`EventIdError`.

    The id is ``"<platform>|<message_id>"`` so ids from different platforms can never collide.
    A missing, non-string, blank or oversized ``message_id`` raises: an economy mutation with no
    stable id cannot be made replay-safe, so the caller must refuse it loudly instead of
    processing it unguarded.
    """
    if not isinstance(platform, str) or not platform.strip():
        raise EventIdError("event platform is missing; cannot build a replay-stable id")
    raw = payload.get(EVENT_ID_PAYLOAD_KEY)
    if not isinstance(raw, str) or not raw.strip():
        raise EventIdError(f"event payload has no {EVENT_ID_PAYLOAD_KEY!r}; cannot dedupe")
    if len(raw) > MAX_EVENT_ID_LEN:
        raise EventIdError(f"event {EVENT_ID_PAYLOAD_KEY!r} exceeds {MAX_EVENT_ID_LEN} chars")
    return f"{platform}|{raw}"


def _stamp_millis(occurred_at: object) -> int:
    """Parse an RFC 3339 ingest stamp to epoch milliseconds, or raise :class:`EventIdError`."""
    if not isinstance(occurred_at, str) or not _RFC3339_RE.fullmatch(occurred_at):
        raise EventIdError("event occurred_at is missing or not an RFC 3339 timestamp")
    try:
        stamp = datetime.fromisoformat(occurred_at)
    except ValueError as exc:
        raise EventIdError("event occurred_at is not a valid RFC 3339 timestamp") from exc
    return (stamp - _EPOCH) // _MILLISECOND


def is_fresh(occurred_at: object, *, now_millis: int | None = None) -> bool:
    """Return True if the event may still be deduped, False if it is too old to process.

    ``occurred_at`` is the event's ingest stamp. Older than :data:`MAX_EVENT_AGE_SECONDS` -> False
    (the caller must skip it silently: its marker may have lapsed, so processing it could replay
    a claimed event). A missing/unparseable stamp, or one more than
    :data:`EVENT_CLOCK_SKEW_SECONDS` in the future, raises :class:`EventIdError` -- the age of
    the event cannot be established, so the caller must refuse loudly, never process it unfenced.
    ``now_millis`` defaults to the host clock.
    """
    stamped = _stamp_millis(occurred_at)
    age_ms = (clock.now_millis() if now_millis is None else now_millis) - stamped
    if age_ms < -EVENT_CLOCK_SKEW_SECONDS * 1000:
        raise EventIdError("event occurred_at is dated in the future")
    return age_ms <= MAX_EVENT_AGE_SECONDS * 1000


def _seen_key(namespace: str, event_id: str) -> str:
    """Build the per-event marker key (``.``-joined; kv forbids ``:``), validating both parts."""
    if not _NAMESPACE_RE.fullmatch(namespace):
        raise ValueError(f"dedupe namespace must match {_NAMESPACE_RE.pattern}: {namespace!r}")
    if not isinstance(event_id, str) or not event_id:
        raise EventIdError("event id must be a non-empty string")
    digest = hashlib.sha256(event_id.encode()).hexdigest()[:_DIGEST_HEX_LEN]
    return f"{namespace}.seen.{digest}"


async def already_seen(community_id: str | None, *, namespace: str, event_id: str) -> bool:
    """Return True if ``event_id`` was already claimed in ``namespace`` (read-only, advisory).

    Only a courtesy for the happy path -- it lets a bundle drop a known redelivery silently before
    touching its cooldown. It is NOT the guard: it is a plain read, so it can race with another
    delivery; :func:`claim` is the authority and must still be called before any mutation.
    """
    key = _seen_key(namespace, event_id)
    return await community_kv.get(community_id, key) is not None


async def claim(community_id: str | None, *, namespace: str, event_id: str) -> bool:
    """Atomically claim ``event_id``; return False if it was already claimed (a duplicate).

    One ``kv.increment`` with a TTL: ``1`` = this call created the marker (process the event),
    ``> 1`` = a redelivery or a concurrent duplicate (skip every side effect). Call it after the
    caller's cheap per-actor gates and BEFORE the first economy/kv mutation. A host failure
    (including a full key quota) propagates -- the caller must fail loud and mutate nothing.

    Raises:
        DedupeStateError: the stored counter is below 1, which ``increment(+1)`` can never
            produce -- the marker was tampered with or corrupted, so refuse rather than guess.
    """
    key = _seen_key(namespace, event_id)
    count = await community_kv.increment(community_id, key, 1, SEEN_TTL_SECONDS)
    if count < 1:
        raise DedupeStateError("event marker counter is below 1")
    return count == 1
