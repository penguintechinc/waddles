"""The keyed hash chain over `secret_events`: append and verify.

The database forces ONE linear chain (unique `prev_chain_hash`, one genesis); this module computes
and checks the hashes. Each event's `chain_hash` is the vault's keyed HMAC over the previous hash
and a canonical, injective encoding of every audit column, so a writer with only database access
cannot insert, edit or drop history without the break being found at the next boot. A KEK rotation
keeps old events verifiable: verification tries each loaded KEK id (a MAC check, not a decryption).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from services.platform_secrets.repository import EventRow, PlatformVaultRepository
from services.provider_connections.vault import (
    PlatformKekAdapter,
    PlatformSecretError,
    platform_event_hash,
)


@dataclass(slots=True, frozen=True)
class ChainReport:
    """Result of verifying the chain: how many events were examined and whether it is intact."""

    examined: int
    intact: bool


def _iso(moment: datetime) -> str:
    """UTC microsecond ISO form of a timestamp (what Postgres round-trips exactly)."""
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def canonical_event(event: EventRow) -> bytes:
    """Injective encoding of every audited column (JSON of ints / strings / nulls, key-sorted)."""
    payload = {
        "public_id": str(event.public_id),
        "secret_id": event.secret_id,
        "secret_type": event.secret_type,
        "scope_id": event.scope_id,
        "version": event.version,
        "event_type": event.event_type,
        "outcome": event.outcome,
        "reason": event.reason,
        "actor_uuid": None if event.actor_uuid is None else str(event.actor_uuid),
        "occurred_at": _iso(event.occurred_at),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def append_event(
    repo: PlatformVaultRepository,
    kek: PlatformKekAdapter,
    *,
    secret_id: int | None,
    secret_type: int,
    scope_id: int | None,
    version: int | None,
    event_type: int,
    outcome: int,
    reason: int | None,
    actor_uuid: uuid.UUID | None = None,
    now: datetime | None = None,
) -> EventRow:
    """Append one event extending the current tip (call only while holding the advisory lock).

    The hash is keyed under the CURRENT KEK. The caller must hold the bootstrap advisory lock so
    two writers never read the same tip (the unique `prev_chain_hash` index would also refuse it).
    """
    prev = repo.chain_tip()
    unsigned = EventRow(
        public_id=uuid.uuid4(),
        secret_id=secret_id,
        secret_type=secret_type,
        scope_id=scope_id,
        version=version,
        event_type=event_type,
        outcome=outcome,
        reason=reason,
        actor_uuid=actor_uuid,
        occurred_at=(now or datetime.now(UTC)).astimezone(UTC),
        prev_chain_hash=prev,
        chain_hash=b"\x00" * 32,
    )
    chain_hash = platform_event_hash(prev, canonical_event(unsigned), kek=kek)
    event = EventRow(**{**_fields(unsigned), "chain_hash": chain_hash})
    repo.insert_event(event)
    return event


def _fields(event: EventRow) -> dict[str, object]:
    """Field dict of an :class:`EventRow` (slots dataclasses have no `__dict__`)."""
    return {name: getattr(event, name) for name in EventRow.__dataclass_fields__}


def verify_chain(repo: PlatformVaultRepository, kek: PlatformKekAdapter) -> ChainReport:
    """Recompute every link; intact only if each event extends its predecessor and its MAC holds.

    An empty chain is intact with `examined=0` (the caller decides whether that is acceptable).
    Never raises on a bad MAC: the report says `intact=False`. A KEK that cannot serve at all
    surfaces as `intact=False` too (it cannot be verified).
    """
    prev: bytes | None = None
    count = 0
    for event in repo.events():
        count += 1
        if event.prev_chain_hash != prev:
            return ChainReport(count, False)
        body = canonical_event(event)
        matched = False
        for kek_id in kek.kek_ids:
            try:
                expected = platform_event_hash(prev, body, kek=kek, kek_id=kek_id)
            except PlatformSecretError:
                continue
            if expected == event.chain_hash:
                matched = True
                break
        if not matched:
            return ChainReport(count, False)
        prev = event.chain_hash
    return ChainReport(count, True)
