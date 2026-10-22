"""At-most-once event claims for bundles whose kv side effects move money.

The pipeline is at-least-once: an event whose handler crashed, timed out or was never acked is
delivered again. The host already makes ``economy.wager`` / ``economy.transfer`` idempotent per
event (``waddle_sdk.economy``), but a bundle's OWN state mutations are not covered -- a jackpot pot
credit, a win/play counter, a steal budget. Replayed, they double-credit, and a pot that is
credited twice pays out money that was only debited once.

This module is the guard. A bundle derives the event's replay-stable id from the payload
(:func:`event_id_from_payload`), records it with :func:`claim` **before** the first mutation, and
skips every side effect when the claim reports a duplicate. The claim is recorded BEFORE the
mutation on purpose (at-most-once): a crash after the claim loses one play, it can never mint a
second one.

**Which id.** The inbound chat payload's ``message_id`` -- the platform's own id for the message
(Discord snowflake, Twitch ``id`` tag). It is assigned by the platform, so a chatter cannot choose
or collide it, and it is identical on every redelivery and re-enqueue of the same message. The WIT
``context.message-id`` (the Valkey stream entry id) is deliberately NOT used: the host does not
populate it today (``get-context`` returns tenant/community/app only), and it changes if the entry
is re-enqueued. A bundle's ``transform`` must therefore forward ``payload["message_id"]``.

**Storage -- why a per-subject ring, not a key per event.** The ``kv`` host charges one unit of a
10,000-key per-(app, community) quota for every key it creates, and a key that lapses through its
TTL is never repaid (``core/bundle_host_kv/src/backend.rs`` "Known eventual-consistency gap"), so a
TTL'd marker per event would exhaust the quota after 10,000 events and take the bundle down with
it. Instead each *subject* (the bundle's actor pseudonym) owns ONE key holding the digests of its
last :data:`RING_SIZE` claimed events; the key count is bounded by the number of players.

**Limits (read before relying on it).**

- The ring is read-modify-write, not a compare-and-swap (``kv.increment`` is the only CAS). The
  caller must therefore reach :func:`claim` through its own atomic gate -- an ``increment``-based
  per-actor cooldown -- so one subject's claims are serialized. A bundle with no such gate can
  lose a ring update under true concurrency (the host economy idempotency still bounds the money).
- Memory is the last :data:`RING_SIZE` claimed events per subject for :data:`RING_TTL_SECONDS`
  since the subject's last claim. Redelivery of an event older than that is not recognised; the
  per-actor cooldown (not this module) is what keeps the horizon wide in wall-clock terms.
- Keys carry only a truncated SHA-256 of the event id and the caller's pseudonym -- never a
  handle, uuid or raw platform id.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

from waddle_sdk import community_kv

#: Payload key a bundle's ``transform`` must forward from the inbound chat event.
EVENT_ID_PAYLOAD_KEY = "message_id"

#: Longest accepted platform message id (Discord snowflakes / Twitch UUIDs are far shorter).
MAX_EVENT_ID_LEN = 512

#: Claimed events remembered per subject.
RING_SIZE = 16

#: kv TTL (the host's 30-day maximum) re-applied on every claim.
RING_TTL_SECONDS = 30 * 24 * 60 * 60

_DIGEST_HEX_LEN = 16
_DIGEST_RE = re.compile(rf"^[0-9a-f]{{{_DIGEST_HEX_LEN}}}$")
_NAMESPACE_RE = re.compile(r"^[a-z0-9_]{1,32}$")
_SUBJECT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class EventIdError(ValueError):
    """The event carries no usable replay-stable id -- the caller must refuse, never guess."""


class DedupeStateError(RuntimeError):
    """The stored ring is not one this module wrote -- fail closed rather than reset it."""


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


def _digest(event_id: str) -> str:
    """Truncated SHA-256 of the event id -- the only form of it that is ever stored."""
    if not isinstance(event_id, str) or not event_id:
        raise EventIdError("event id must be a non-empty string")
    return hashlib.sha256(event_id.encode()).hexdigest()[:_DIGEST_HEX_LEN]


def _ring_key(namespace: str, subject: str) -> str:
    """Build the per-subject ring key (``.``-joined; kv forbids ``:``), validating both parts."""
    if not _NAMESPACE_RE.fullmatch(namespace):
        raise ValueError(f"dedupe namespace must match {_NAMESPACE_RE.pattern}: {namespace!r}")
    if not _SUBJECT_RE.fullmatch(subject):
        raise ValueError(f"dedupe subject must match {_SUBJECT_RE.pattern}")
    return f"dedupe.{namespace}.{subject}"


async def _load(community_id: str | None, key: str) -> list[str]:
    """Read and validate the ring; an absent key is an empty ring, a malformed one raises."""
    raw = await community_kv.get(community_id, key)
    if raw is None:
        return []
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise DedupeStateError("dedupe ring is not ASCII") from exc
    items = text.split(",") if text else []
    if not all(_DIGEST_RE.fullmatch(item) for item in items):
        raise DedupeStateError("dedupe ring holds a malformed entry")
    return items[-RING_SIZE:]


async def already_seen(
    community_id: str | None, *, namespace: str, subject: str, event_id: str
) -> bool:
    """Return True if ``event_id`` was already claimed by ``subject`` (read-only)."""
    ring = await _load(community_id, _ring_key(namespace, subject))
    return _digest(event_id) in ring


async def claim(community_id: str | None, *, namespace: str, subject: str, event_id: str) -> bool:
    """Record ``event_id`` for ``subject``; return False if it was already claimed (a duplicate).

    Re-reads the ring itself, so it is correct even without a preceding :func:`already_seen`.
    Call it after the caller's atomic gate and BEFORE the first economy/kv mutation; on False the
    caller must skip every side effect.
    """
    key = _ring_key(namespace, subject)
    digest = _digest(event_id)
    ring = await _load(community_id, key)
    if digest in ring:
        return False
    ring.append(digest)
    await community_kv.set(
        community_id, key, ",".join(ring[-RING_SIZE:]).encode(), RING_TTL_SECONDS
    )
    return True
