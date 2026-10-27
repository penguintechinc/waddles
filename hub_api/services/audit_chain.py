"""Tamper-evident hash chain for the enterprise audit log (GRC audit finding #3).

Pure and I/O-free on purpose: this module is the *whole* integrity contract and
depends on nothing but the standard library, so the same code verifies a chain
inside hub-api, inside the ``make verify-audit-export`` CLI an auditor runs
against an offline export, and inside the unit tests that prove a mutated record
breaks verification.

Chain construction
------------------
Every record belongs to one *chain* (one per tenant, plus one for the platform)
and carries a gapless ``seq`` starting at 1. ``record_hash`` is
``SHA-256(domain-prefix || canonical-JSON(every audited field, incl. prev_hash))``
and ``prev_hash`` is the previous record's ``record_hash`` (``GENESIS_HASH`` for
``seq == 1``). Consequences, each pinned by a test:

* altering any field of a record changes its recomputed hash        -> HASH_MISMATCH
* deleting a record leaves a ``seq`` gap and a dangling ``prev_hash``  -> SEQ_GAP
* inserting or reordering records breaks the ``prev_hash`` linkage     -> PREV_HASH_MISMATCH
* altering a record *and* recomputing its hash breaks the **next**
  record's ``prev_hash`` link                                          -> PREV_HASH_MISMATCH
* truncating the tail is invisible to the chain alone, so verification
  accepts an externally pinned ``(expected_head_seq, expected_head_hash)`` anchor
                                                                      -> HEAD_MISMATCH

Honest limit: SHA-256 (not a keyed MAC) means someone able to rewrite *every*
record from the tamper point to the head can recompute a self-consistent chain.
The defence is anchoring: the head ``(seq, hash)`` is published by the ``/head``
endpoint and in every export manifest so it can be pinned somewhere the database
operator cannot write (a ticket, a WORM bucket, a SIEM). See
``docs/compliance/audit-logging.md``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

#: Algorithm/serialisation identifier stored on every record, so a future change to
#: the canonical form is a new value here rather than a silent re-interpretation.
HASH_VERSION: Final[str] = "sha256-v1"

#: ``prev_hash`` of the first record in every chain.
GENESIS_HASH: Final[str] = "0" * 64

#: Domain-separation prefix mixed into every digest so an audit digest can never
#: collide with a SHA-256 computed for any other purpose in the platform.
_DOMAIN_PREFIX: Final[bytes] = b"waddles.audit.chain.v1\n"

_HEX_CHARS: Final[frozenset[str]] = frozenset("0123456789abcdef")


class ChainBreakReason(StrEnum):
    """Why a chain failed verification (the first break found, in ``seq`` order)."""

    CHAIN_ID_MISMATCH = "chain_id_mismatch"
    HASH_VERSION_UNSUPPORTED = "hash_version_unsupported"
    SEQ_GAP = "seq_gap"
    PREV_HASH_MISMATCH = "prev_hash_mismatch"
    HASH_MISMATCH = "hash_mismatch"
    MALFORMED_HASH = "malformed_hash"
    HEAD_MISMATCH = "head_mismatch"


class ChainStatus(StrEnum):
    """Three-valued verification outcome; ``EMPTY`` is deliberately not ``INTACT``.

    A verifier that examined zero records proved nothing, so it must never be
    reported as a pass (house rule: zero examined is a failure, not a clean bill).
    """

    INTACT = "intact"
    BROKEN = "broken"
    EMPTY = "empty"


@dataclass(slots=True, frozen=True)
class ChainRecord:
    """One audit record exactly as it is hashed -- every field is covered by ``record_hash``.

    ``occurred_at`` is the canonical UTC string from :func:`canonical_timestamp` and
    ``details`` is a flat mapping of JSON scalars; both are normalised *before* they
    reach this type so the stored and recomputed forms are byte-identical.
    """

    chain_id: str
    seq: int
    event_id: str
    occurred_at: str
    actor_uuid: str | None
    actor_kind: str
    category: str
    action: str
    outcome: str
    target_type: str | None
    target_id: str | None
    details: Mapping[str, Any]
    prev_hash: str
    record_hash: str
    hash_version: str = HASH_VERSION


@dataclass(slots=True, frozen=True)
class ChainBreak:
    """The first point at which a chain stopped verifying."""

    seq: int | None
    reason: ChainBreakReason
    detail: str


@dataclass(slots=True, frozen=True)
class ChainVerification:
    """Result of verifying a run of records.

    ``head_seq``/``head_hash`` describe the last *consistent* record examined (the
    anchor a caller pins, or resumes from); ``break_`` is ``None`` unless
    ``status`` is ``BROKEN``.
    """

    status: ChainStatus
    examined: int
    head_seq: int | None
    head_hash: str | None
    break_: ChainBreak | None = None

    @property
    def ok(self) -> bool:
        """True only for a non-empty, fully consistent run."""
        return self.status is ChainStatus.INTACT


def canonical_timestamp(value: datetime) -> str:
    """Render ``value`` as the one UTC string form that is hashed (microsecond precision).

    Naive datetimes are taken to be UTC (SQLite and some drivers drop the tzinfo on
    the round trip); aware ones are converted. Postgres ``timestamptz`` has
    microsecond resolution, so the string survives store-and-reload unchanged.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _is_hex_digest(value: str) -> bool:
    """True for a lowercase 64-char hex SHA-256 digest."""
    return len(value) == 64 and all(ch in _HEX_CHARS for ch in value)


def canonical_bytes(record: ChainRecord) -> bytes:
    """Serialise every audited field deterministically (sorted keys, no whitespace, ASCII).

    ``record_hash`` is deliberately excluded (it is the digest of this output);
    ``prev_hash`` and ``hash_version`` are included so the linkage and the algorithm
    identifier are themselves tamper-evident.
    """
    payload = {
        "action": record.action,
        "actor_kind": record.actor_kind,
        "actor_uuid": record.actor_uuid,
        "category": record.category,
        "chain_id": record.chain_id,
        "details": dict(record.details),
        "event_id": record.event_id,
        "hash_version": record.hash_version,
        "occurred_at": record.occurred_at,
        "outcome": record.outcome,
        "prev_hash": record.prev_hash,
        "seq": record.seq,
        "target_id": record.target_id,
        "target_type": record.target_type,
    }
    body = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )
    return _DOMAIN_PREFIX + body.encode("ascii")


def compute_hash(record: ChainRecord) -> str:
    """SHA-256 hex digest of the record's canonical bytes."""
    return hashlib.sha256(canonical_bytes(record)).hexdigest()


def seal(record_without_hash: ChainRecord) -> ChainRecord:
    """Return ``record_without_hash`` with its ``record_hash`` filled in.

    The caller passes any placeholder (conventionally ``""``) as ``record_hash``; it is
    ignored by :func:`canonical_bytes`.
    """
    digest = compute_hash(record_without_hash)
    return ChainRecord(
        chain_id=record_without_hash.chain_id,
        seq=record_without_hash.seq,
        event_id=record_without_hash.event_id,
        occurred_at=record_without_hash.occurred_at,
        actor_uuid=record_without_hash.actor_uuid,
        actor_kind=record_without_hash.actor_kind,
        category=record_without_hash.category,
        action=record_without_hash.action,
        outcome=record_without_hash.outcome,
        target_type=record_without_hash.target_type,
        target_id=record_without_hash.target_id,
        details=record_without_hash.details,
        prev_hash=record_without_hash.prev_hash,
        record_hash=digest,
        hash_version=record_without_hash.hash_version,
    )


@dataclass(slots=True)
class ChainVerifier:
    """Incremental verifier: feed records in ``seq`` order, O(1) memory regardless of length.

    Stops at the first break (later records are not meaningful once the chain is
    broken) and ignores further input. Start mid-chain by passing the trusted
    ``start_seq`` and the hash of the record before it as ``start_prev_hash``.
    """

    chain_id: str
    start_seq: int = 1
    start_prev_hash: str = GENESIS_HASH
    examined: int = 0
    head_seq: int | None = None
    head_hash: str | None = None
    broken: ChainBreak | None = None
    _expected_seq: int = field(init=False, default=1)
    _expected_prev: str = field(init=False, default=GENESIS_HASH)

    def __post_init__(self) -> None:
        """Seed the expected-next state from the (possibly mid-chain) start anchor."""
        if self.start_seq < 1:
            raise ValueError("start_seq must be >= 1")
        if not _is_hex_digest(self.start_prev_hash):
            raise ValueError("start_prev_hash must be a 64-char lowercase hex SHA-256 digest")
        if self.start_seq == 1 and self.start_prev_hash != GENESIS_HASH:
            raise ValueError("a chain starting at seq 1 must start from GENESIS_HASH")
        self._expected_seq = self.start_seq
        self._expected_prev = self.start_prev_hash

    def _fail(self, record: ChainRecord, reason: ChainBreakReason, detail: str) -> None:
        """Latch the first break and stop accepting records."""
        self.broken = ChainBreak(seq=record.seq, reason=reason, detail=detail)

    def feed(self, record: ChainRecord) -> bool:
        """Check one record against the running state; return True if the chain still holds."""
        if self.broken is not None:
            return False
        self.examined += 1
        if record.chain_id != self.chain_id:
            self._fail(
                record, ChainBreakReason.CHAIN_ID_MISMATCH, "record belongs to another chain"
            )
        elif record.hash_version != HASH_VERSION:
            self._fail(
                record,
                ChainBreakReason.HASH_VERSION_UNSUPPORTED,
                f"unsupported hash_version {record.hash_version!r}",
            )
        elif record.seq != self._expected_seq:
            self._fail(
                record,
                ChainBreakReason.SEQ_GAP,
                f"expected seq {self._expected_seq}, found {record.seq}",
            )
        elif not (_is_hex_digest(record.prev_hash) and _is_hex_digest(record.record_hash)):
            self._fail(
                record, ChainBreakReason.MALFORMED_HASH, "prev_hash/record_hash not SHA-256 hex"
            )
        elif record.prev_hash != self._expected_prev:
            self._fail(
                record,
                ChainBreakReason.PREV_HASH_MISMATCH,
                "prev_hash does not match the preceding record's hash",
            )
        elif compute_hash(record) != record.record_hash:
            self._fail(
                record,
                ChainBreakReason.HASH_MISMATCH,
                "recomputed hash differs from stored record_hash (record was altered)",
            )
        if self.broken is not None:
            return False
        self._expected_seq = record.seq + 1
        self._expected_prev = record.record_hash
        self.head_seq = record.seq
        self.head_hash = record.record_hash
        return True

    def finish(
        self, *, expected_head_seq: int | None = None, expected_head_hash: str | None = None
    ) -> ChainVerification:
        """Close the run, optionally checking the head against an externally pinned anchor."""
        if self.broken is None and self.examined > 0:
            if expected_head_seq is not None and self.head_seq != expected_head_seq:
                self.broken = ChainBreak(
                    seq=self.head_seq,
                    reason=ChainBreakReason.HEAD_MISMATCH,
                    detail=(
                        f"head seq {self.head_seq} != pinned seq {expected_head_seq} "
                        "(records removed from the tail, or appended since the pin)"
                    ),
                )
            elif expected_head_hash is not None and self.head_hash != expected_head_hash:
                self.broken = ChainBreak(
                    seq=self.head_seq,
                    reason=ChainBreakReason.HEAD_MISMATCH,
                    detail="head hash differs from the pinned head hash",
                )
        if self.broken is not None:
            return ChainVerification(
                status=ChainStatus.BROKEN,
                examined=self.examined,
                head_seq=self.head_seq,
                head_hash=self.head_hash,
                break_=self.broken,
            )
        if self.examined == 0:
            if expected_head_seq or expected_head_hash:
                # Pinned head, yet nothing left to examine: every record was removed.
                return ChainVerification(
                    status=ChainStatus.BROKEN,
                    examined=0,
                    head_seq=None,
                    head_hash=None,
                    break_=ChainBreak(
                        seq=None,
                        reason=ChainBreakReason.HEAD_MISMATCH,
                        detail="chain is empty but a head was pinned",
                    ),
                )
            return ChainVerification(
                status=ChainStatus.EMPTY, examined=0, head_seq=None, head_hash=None
            )
        return ChainVerification(
            status=ChainStatus.INTACT,
            examined=self.examined,
            head_seq=self.head_seq,
            head_hash=self.head_hash,
        )


def verify_records(
    records: Iterable[ChainRecord],
    *,
    chain_id: str,
    start_seq: int = 1,
    start_prev_hash: str = GENESIS_HASH,
    expected_head_seq: int | None = None,
    expected_head_hash: str | None = None,
) -> ChainVerification:
    """Verify an in-memory/iterable run of records in one call (stops at the first break)."""
    verifier = ChainVerifier(chain_id, start_seq=start_seq, start_prev_hash=start_prev_hash)
    for record in records:
        if not verifier.feed(record):
            break
    return verifier.finish(
        expected_head_seq=expected_head_seq, expected_head_hash=expected_head_hash
    )
