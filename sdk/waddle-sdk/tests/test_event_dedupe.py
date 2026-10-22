"""Tests for `waddle_sdk.event_dedupe` -- the at-most-once claim for replay-safe economy bundles."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from waddle_sdk import event_dedupe
from waddle_sdk.community_kv import _scoped_key
from waddle_sdk.event_dedupe import (
    RING_SIZE,
    RING_TTL_SECONDS,
    DedupeStateError,
    EventIdError,
    already_seen,
    claim,
    event_id_from_payload,
)
from waddle_sdk.kv import validate_key
from waddle_sdk.testing import FakeKvHost, install_fake_kv_host

COMMUNITY = "comm-1"
SUBJECT = hashlib.sha256(b"actor-1").hexdigest()


def _run(coro: Any) -> Any:
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("event_dedupe coroutine unexpectedly suspended")


@pytest.fixture
def kv(monkeypatch: pytest.MonkeyPatch) -> FakeKvHost:
    return install_fake_kv_host(monkeypatch)


def _claim(event: str, *, subject: str = SUBJECT, community: str | None = COMMUNITY) -> bool:
    return bool(_run(claim(community, namespace="steal", subject=subject, event_id=event)))


def _seen(event: str, *, subject: str = SUBJECT, community: str | None = COMMUNITY) -> bool:
    return bool(_run(already_seen(community, namespace="steal", subject=subject, event_id=event)))


def test_first_claim_wins_and_a_redelivery_is_a_duplicate(kv: FakeKvHost) -> None:
    assert _claim("twitch|m1") is True
    assert _claim("twitch|m1") is False
    assert _claim("twitch|m1") is False


def test_already_seen_is_read_only_and_tracks_claims(kv: FakeKvHost) -> None:
    assert _seen("twitch|m1") is False
    assert kv.store == {}, "a read-only check must not write"
    _claim("twitch|m1")
    assert _seen("twitch|m1") is True
    assert _seen("twitch|m2") is False


def test_distinct_events_each_claim_once(kv: FakeKvHost) -> None:
    assert [_claim(f"twitch|m{i}") for i in range(5)] == [True] * 5
    assert [_claim(f"twitch|m{i}") for i in range(5)] == [False] * 5


def test_subjects_communities_and_namespaces_are_isolated(kv: FakeKvHost) -> None:
    other = hashlib.sha256(b"actor-2").hexdigest()
    assert _claim("twitch|m1") is True
    assert _claim("twitch|m1", subject=other) is True
    assert _claim("twitch|m1", community="comm-2") is True
    assert _claim("twitch|m1", community=None) is True
    assert (
        _run(claim(COMMUNITY, namespace="jackpot", subject=SUBJECT, event_id="twitch|m1")) is True
    )


def test_ring_evicts_the_oldest_after_ring_size_distinct_events(kv: FakeKvHost) -> None:
    for i in range(RING_SIZE):
        assert _claim(f"twitch|m{i}") is True
    assert _seen("twitch|m0") is True
    assert _claim(f"twitch|m{RING_SIZE}") is True
    assert _seen("twitch|m0") is False, "the oldest entry fell out of the ring"
    assert _seen(f"twitch|m{RING_SIZE}") is True
    value = kv.store[_scoped_key(COMMUNITY, f"dedupe.steal.{SUBJECT}")].decode()
    assert len(value.split(",")) == RING_SIZE


def test_one_key_per_subject_regardless_of_event_count(kv: FakeKvHost) -> None:
    """The kv host charges a quota unit per key created: events must not create keys."""
    for i in range(200):
        _claim(f"twitch|m{i}")
    assert len(kv.store) == 1


def test_ttl_is_applied_on_every_claim(kv: FakeKvHost) -> None:
    _claim("twitch|m1")
    _claim("twitch|m2")
    sets = [args for name, args in kv.calls if name == "set"]
    assert [ttl for _key, _value, ttl in sets] == [RING_TTL_SECONDS, RING_TTL_SECONDS]


def test_a_duplicate_claim_writes_nothing(kv: FakeKvHost) -> None:
    _claim("twitch|m1")
    before = [c for c in kv.calls if c[0] == "set"]
    assert _claim("twitch|m1") is False
    assert [c for c in kv.calls if c[0] == "set"] == before


def test_keys_are_valid_pii_free_and_never_carry_the_raw_event_id(kv: FakeKvHost) -> None:
    raw_id = "twitch|8c4f0b1a-uuid-with-private-looking-id"
    _claim(raw_id)
    for key, value in kv.store.items():
        validate_key(key)
        assert ":" not in key
        assert raw_id not in key
        assert raw_id.encode() not in value
        assert "8c4f0b1a" not in key
        assert b"8c4f0b1a" not in value


@pytest.mark.parametrize("bad", [b"\xff\xfe", b"nothex-entry-x16", b"abc", b"0123456789abcdef,zz"])
def test_a_malformed_ring_fails_closed_instead_of_being_reset(kv: FakeKvHost, bad: bytes) -> None:
    kv.store[_scoped_key(COMMUNITY, f"dedupe.steal.{SUBJECT}")] = bad
    with pytest.raises(DedupeStateError):
        _claim("twitch|m1")
    with pytest.raises(DedupeStateError):
        _seen("twitch|m1")


def test_an_oversized_stored_ring_is_truncated_not_rejected(kv: FakeKvHost) -> None:
    digests = [hashlib.sha256(str(i).encode()).hexdigest()[:16] for i in range(RING_SIZE + 5)]
    kv.store[_scoped_key(COMMUNITY, f"dedupe.steal.{SUBJECT}")] = ",".join(digests).encode()
    assert _claim("twitch|brand-new") is True
    value = kv.store[_scoped_key(COMMUNITY, f"dedupe.steal.{SUBJECT}")].decode().split(",")
    assert len(value) == RING_SIZE


@pytest.mark.parametrize("namespace", ["", "Steal", "a:b", "x" * 33, "with space"])
def test_bad_namespace_is_rejected(kv: FakeKvHost, namespace: str) -> None:
    with pytest.raises(ValueError, match="namespace"):
        _run(claim(COMMUNITY, namespace=namespace, subject=SUBJECT, event_id="twitch|m1"))


@pytest.mark.parametrize("subject", ["", "a:b", "x" * 129, "has space", "unié"])
def test_bad_subject_is_rejected(kv: FakeKvHost, subject: str) -> None:
    with pytest.raises(ValueError, match="subject"):
        _run(claim(COMMUNITY, namespace="steal", subject=subject, event_id="twitch|m1"))


def test_empty_community_still_fails_loud(kv: FakeKvHost) -> None:
    with pytest.raises(ValueError, match="community"):
        _run(claim("", namespace="steal", subject=SUBJECT, event_id="twitch|m1"))


def test_empty_event_id_is_rejected(kv: FakeKvHost) -> None:
    with pytest.raises(EventIdError):
        _run(claim(COMMUNITY, namespace="steal", subject=SUBJECT, event_id=""))


def test_event_id_from_payload_namespaces_by_platform() -> None:
    assert event_id_from_payload({"message_id": "m1"}, platform="twitch") == "twitch|m1"
    assert event_id_from_payload({"message_id": "m1"}, platform="discord") == "discord|m1"
    assert event_id_from_payload({"message_id": "m1"}, platform="twitch") != event_id_from_payload(
        {"message_id": "m1"}, platform="discord"
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"message_id": None},
        {"message_id": ""},
        {"message_id": "   "},
        {"message_id": 12345},
        {"message_id": b"m1"},
        {"message_id": ["m1"]},
        {"message_id": "x" * (event_dedupe.MAX_EVENT_ID_LEN + 1)},
    ],
)
def test_event_id_from_payload_fails_loud_without_a_usable_id(payload: dict[str, Any]) -> None:
    with pytest.raises(EventIdError):
        event_id_from_payload(payload, platform="twitch")


@pytest.mark.parametrize("platform", ["", "  ", None])
def test_event_id_from_payload_requires_a_platform(platform: Any) -> None:
    with pytest.raises(EventIdError):
        event_id_from_payload({"message_id": "m1"}, platform=platform)


def test_event_id_at_the_length_limit_is_accepted() -> None:
    raw = "x" * event_dedupe.MAX_EVENT_ID_LEN
    assert event_id_from_payload({"message_id": raw}, platform="twitch") == f"twitch|{raw}"
