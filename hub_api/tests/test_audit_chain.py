"""Pure hash-chain tests -- the tamper-detection contract of GRC audit finding #3.

The headline regression is :class:`TestTamperDetection`: *mutating, deleting, inserting or
reordering a record breaks verification*, for every audited field. These run with no database,
so they are also the fastest proof that the integrity contract itself holds.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from services.audit_chain import (
    GENESIS_HASH,
    HASH_VERSION,
    ChainBreakReason,
    ChainStatus,
    ChainVerifier,
    canonical_bytes,
    canonical_timestamp,
    compute_hash,
    seal,
    verify_records,
)
from tests.audit_support import build_chain, replace_record

CHAIN = "tenant:1"


class TestCanonicalForm:
    def test_hash_is_deterministic_and_covers_prev_hash(self) -> None:
        a, b = build_chain(2)
        assert compute_hash(a) == a.record_hash
        assert b.prev_hash == a.record_hash
        assert compute_hash(replace_record(b, prev_hash=GENESIS_HASH)) != b.record_hash

    def test_canonical_bytes_ignore_detail_key_order(self) -> None:
        record = build_chain(1)[0]
        reordered = replace_record(record, details=dict(reversed(list(record.details.items()))))
        assert canonical_bytes(record) == canonical_bytes(reordered)
        assert compute_hash(record) == compute_hash(reordered)

    def test_canonical_bytes_carry_domain_prefix_and_are_ascii(self) -> None:
        record = replace_record(build_chain(1)[0], details={"k": "café"})
        raw = canonical_bytes(record)
        assert raw.startswith(b"waddles.audit.chain.v1\n")
        raw.decode("ascii")  # non-ASCII is \u-escaped, so the bytes are platform independent

    def test_record_hash_is_excluded_from_its_own_digest(self) -> None:
        record = build_chain(1)[0]
        assert compute_hash(replace_record(record, record_hash="f" * 64)) == record.record_hash

    def test_seal_fills_hash_and_keeps_every_other_field(self) -> None:
        unsealed = replace_record(build_chain(1)[0], record_hash="")
        sealed = seal(unsealed)
        assert sealed.record_hash == compute_hash(unsealed)
        assert replace_record(sealed, record_hash="") == unsealed

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (datetime(2026, 10, 10, 1, 2, 3, 4, tzinfo=UTC), "2026-10-10T01:02:03.000004Z"),
            (datetime(2026, 10, 10, 1, 2, 3, 4), "2026-10-10T01:02:03.000004Z"),
            (
                datetime(2026, 10, 10, 3, 2, 3, 4, tzinfo=timezone(timedelta(hours=2))),
                "2026-10-10T01:02:03.000004Z",
            ),
        ],
    )
    def test_canonical_timestamp_normalises_to_utc_microseconds(
        self, value: datetime, expected: str
    ) -> None:
        assert canonical_timestamp(value) == expected


class TestVerifyHappyPath:
    def test_intact_chain(self) -> None:
        records = build_chain(25)
        result = verify_records(records, chain_id=CHAIN)
        assert result.ok
        assert result.status is ChainStatus.INTACT
        assert result.examined == 25
        assert result.head_seq == 25
        assert result.head_hash == records[-1].record_hash
        assert result.break_ is None

    def test_empty_chain_is_not_a_pass(self) -> None:
        """Zero records examined proves nothing -> EMPTY, never INTACT/ok."""
        result = verify_records([], chain_id=CHAIN)
        assert result.status is ChainStatus.EMPTY
        assert not result.ok
        assert result.examined == 0

    def test_resume_from_the_middle_with_a_trusted_anchor(self) -> None:
        records = build_chain(10)
        result = verify_records(
            records[5:],
            chain_id=CHAIN,
            start_seq=6,
            start_prev_hash=records[4].record_hash,
        )
        assert result.ok
        assert result.examined == 5
        assert result.head_seq == 10

    def test_incremental_verifier_matches_one_shot(self) -> None:
        records = build_chain(7)
        verifier = ChainVerifier(CHAIN)
        assert all(verifier.feed(r) for r in records)
        assert verifier.finish().head_hash == verify_records(records, chain_id=CHAIN).head_hash


class TestTamperDetection:
    """Regression: any alteration of the stored log is detected (GRC #3 'not tamper-evident')."""

    @pytest.mark.parametrize(
        ("field_name", "new_value"),
        [
            ("action", "admin.nothing_to_see"),
            ("actor_uuid", "99999999-9999-4999-8999-999999999999"),
            ("actor_kind", "system"),
            ("category", "tenant"),
            ("outcome", "denied"),
            ("target_type", "other_type"),
            ("target_id", "tenant-42"),
            ("occurred_at", "2026-01-01T00:00:00.000000Z"),
            ("event_id", "99999999-9999-4999-8999-999999999998"),
            ("details", {"method": "GET", "status": 200}),
        ],
    )
    def test_mutating_any_audited_field_breaks_the_chain(
        self, field_name: str, new_value: object
    ) -> None:
        records = build_chain(10)
        records[4] = replace_record(records[4], **{field_name: new_value})
        result = verify_records(records, chain_id=CHAIN)
        assert result.status is ChainStatus.BROKEN
        assert result.break_ is not None
        assert result.break_.reason is ChainBreakReason.HASH_MISMATCH
        assert result.break_.seq == 5
        assert result.head_seq == 4  # records before the tamper point are still proven good

    def test_mutating_a_record_and_recomputing_its_hash_breaks_the_next_link(self) -> None:
        """Rewriting a record *and* its own hash is still caught: the successor's prev_hash."""
        records = build_chain(10)
        forged = seal(replace_record(records[4], action="admin.nothing_to_see", record_hash=""))
        records[4] = forged
        result = verify_records(records, chain_id=CHAIN)
        assert result.status is ChainStatus.BROKEN
        assert result.break_ is not None
        assert result.break_.reason is ChainBreakReason.PREV_HASH_MISMATCH
        assert result.break_.seq == 6

    def test_deleting_a_middle_record_breaks_the_chain(self) -> None:
        records = build_chain(10)
        del records[4]
        result = verify_records(records, chain_id=CHAIN)
        assert result.status is ChainStatus.BROKEN
        assert result.break_ is not None
        assert result.break_.reason is ChainBreakReason.SEQ_GAP
        assert result.break_.seq == 6

    def test_deleting_the_first_record_breaks_the_chain(self) -> None:
        records = build_chain(5)[1:]
        result = verify_records(records, chain_id=CHAIN)
        assert result.status is ChainStatus.BROKEN
        assert result.break_ is not None
        assert result.break_.reason is ChainBreakReason.SEQ_GAP

    def test_deleting_and_renumbering_still_breaks_prev_hash(self) -> None:
        records = build_chain(6)
        del records[2]
        renumbered = [replace_record(r, seq=i + 1) for i, r in enumerate(records)]
        result = verify_records(renumbered, chain_id=CHAIN)
        assert result.status is ChainStatus.BROKEN
        assert result.break_ is not None
        assert result.break_.reason in {
            ChainBreakReason.PREV_HASH_MISMATCH,
            ChainBreakReason.HASH_MISMATCH,
        }

    def test_reordering_records_breaks_the_chain(self) -> None:
        records = build_chain(6)
        records[2], records[3] = records[3], records[2]
        result = verify_records(records, chain_id=CHAIN)
        assert result.status is ChainStatus.BROKEN
        assert result.break_ is not None
        assert result.break_.reason is ChainBreakReason.SEQ_GAP

    def test_inserting_a_forged_record_breaks_the_chain(self) -> None:
        records = build_chain(6)
        forged = seal(
            replace_record(
                records[2], seq=4, prev_hash=records[2].record_hash, record_hash="", action="x.y"
            )
        )
        records.insert(3, forged)
        result = verify_records(records, chain_id=CHAIN)
        assert result.status is ChainStatus.BROKEN

    def test_records_from_another_chain_are_rejected(self) -> None:
        records = build_chain(3, chain_id="tenant:2")
        result = verify_records(records, chain_id=CHAIN)
        assert result.break_ is not None
        assert result.break_.reason is ChainBreakReason.CHAIN_ID_MISMATCH

    def test_unsupported_hash_version_is_rejected_not_reinterpreted(self) -> None:
        records = build_chain(3)
        records[1] = replace_record(records[1], hash_version="md5-v0")
        result = verify_records(records, chain_id=CHAIN)
        assert result.break_ is not None
        assert result.break_.reason is ChainBreakReason.HASH_VERSION_UNSUPPORTED

    @pytest.mark.parametrize("bad", ["", "xyz", "A" * 64, "f" * 63])
    def test_malformed_hashes_are_rejected(self, bad: str) -> None:
        records = build_chain(3)
        records[1] = replace_record(records[1], record_hash=bad)
        result = verify_records(records, chain_id=CHAIN)
        assert result.status is ChainStatus.BROKEN
        assert result.break_ is not None
        assert result.break_.reason is ChainBreakReason.MALFORMED_HASH

    def test_first_break_wins_and_later_records_are_not_examined(self) -> None:
        records = build_chain(10)
        records[2] = replace_record(records[2], action="admin.x")
        records[7] = replace_record(records[7], action="admin.y")
        result = verify_records(records, chain_id=CHAIN)
        assert result.break_ is not None
        assert result.break_.seq == 3
        assert result.examined == 3

    def test_verifier_stops_accepting_after_a_break(self) -> None:
        records = build_chain(5)
        records[1] = replace_record(records[1], action="admin.x")
        verifier = ChainVerifier(CHAIN)
        assert verifier.feed(records[0])
        assert not verifier.feed(records[1])
        assert not verifier.feed(records[2])
        assert verifier.finish().status is ChainStatus.BROKEN


class TestHeadPinning:
    """The chain alone cannot see tail truncation; a pinned head can."""

    def test_truncated_tail_is_caught_by_the_pinned_head_seq(self) -> None:
        records = build_chain(10)
        result = verify_records(records[:7], chain_id=CHAIN, expected_head_seq=10)
        assert result.status is ChainStatus.BROKEN
        assert result.break_ is not None
        assert result.break_.reason is ChainBreakReason.HEAD_MISMATCH

    def test_truncated_tail_is_caught_by_the_pinned_head_hash(self) -> None:
        records = build_chain(10)
        result = verify_records(
            records[:7], chain_id=CHAIN, expected_head_hash=records[-1].record_hash
        )
        assert result.break_ is not None
        assert result.break_.reason is ChainBreakReason.HEAD_MISMATCH

    def test_matching_pin_passes(self) -> None:
        records = build_chain(10)
        result = verify_records(
            records,
            chain_id=CHAIN,
            expected_head_seq=10,
            expected_head_hash=records[-1].record_hash,
        )
        assert result.ok

    def test_wiped_chain_with_a_pin_is_broken_not_empty(self) -> None:
        result = verify_records([], chain_id=CHAIN, expected_head_seq=10)
        assert result.status is ChainStatus.BROKEN
        assert result.break_ is not None
        assert result.break_.reason is ChainBreakReason.HEAD_MISMATCH


class TestVerifierPreconditions:
    def test_seq_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="start_seq"):
            ChainVerifier(CHAIN, start_seq=0)

    def test_anchor_must_be_a_sha256_digest(self) -> None:
        with pytest.raises(ValueError, match="start_prev_hash"):
            ChainVerifier(CHAIN, start_seq=2, start_prev_hash="nope")

    def test_a_chain_starting_at_one_must_start_from_genesis(self) -> None:
        with pytest.raises(ValueError, match="GENESIS_HASH"):
            ChainVerifier(CHAIN, start_seq=1, start_prev_hash="a" * 64)

    def test_hash_version_constant_is_pinned(self) -> None:
        """Changing the canonical form must be a new version string, not a silent edit."""
        assert HASH_VERSION == "sha256-v1"
        assert build_chain(1)[0].hash_version == HASH_VERSION
