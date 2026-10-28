"""Tests for `waddle_sdk.event_dedupe` -- per-event TTL'd-increment claims + the age fence."""

from __future__ import annotations

import asyncio
import re
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from waddle_sdk import community_kv, event_dedupe
from waddle_sdk.community_kv import _scoped_key
from waddle_sdk.event_dedupe import (
    EVENT_CLOCK_SKEW_SECONDS,
    MAX_EVENT_AGE_SECONDS,
    SEEN_TTL_SECONDS,
    DedupeStateError,
    EventIdError,
    already_seen,
    claim,
    event_id_from_payload,
    is_fresh,
)
from waddle_sdk.kv import validate_key
from waddle_sdk.testing import FakeKvHost, install_fake_kv_host, rfc3339_from_millis

COMMUNITY = "comm-1"
NOW_MS = 1_760_000_000_000


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def kv(monkeypatch: pytest.MonkeyPatch) -> FakeKvHost:
    return install_fake_kv_host(monkeypatch)


def _set_clock(now_ms: int) -> None:
    sys.modules["wit_world"].imports.clock = types.SimpleNamespace(  # type: ignore[attr-defined]
        now_millis=lambda: now_ms
    )


def _claim(event: str, *, namespace: str = "steal", community: str | None = COMMUNITY) -> bool:
    return bool(_run(claim(community, namespace=namespace, event_id=event)))


def _seen(event: str, *, namespace: str = "steal", community: str | None = COMMUNITY) -> bool:
    return bool(_run(already_seen(community, namespace=namespace, event_id=event)))


def _marker_keys(kv: FakeKvHost) -> list[str]:
    return [k for k in kv.store if ".seen." in k]


# -- the claim primitive -------------------------------------------------------------------------


def test_first_claim_wins_and_every_redelivery_is_a_duplicate(kv: FakeKvHost) -> None:
    assert _claim("twitch|m1") is True
    assert _claim("twitch|m1") is False
    assert _claim("twitch|m1") is False


def test_a_claim_is_exactly_one_ttl_increment(kv: FakeKvHost) -> None:
    """The whole protocol is the counter `increment(+1, ttl)` returns -- no get/set involved."""
    _claim("twitch|m1")
    assert [name for name, _args in kv.calls] == ["increment"]
    ((key, delta, ttl),) = [args for _name, args in kv.calls]
    assert delta == 1 and ttl == SEEN_TTL_SECONDS
    assert key.startswith(_scoped_key(COMMUNITY, "steal.seen."))


def test_ttl_is_applied_on_every_claim_including_redeliveries(kv: FakeKvHost) -> None:
    _claim("twitch|m1")
    _claim("twitch|m1")
    assert [args[2] for name, args in kv.calls if name == "increment"] == [SEEN_TTL_SECONDS] * 2


def test_already_seen_is_read_only_and_tracks_claims(kv: FakeKvHost) -> None:
    assert _seen("twitch|m1") is False
    assert kv.store == {}, "a read-only check must not write"
    _claim("twitch|m1")
    assert _seen("twitch|m1") is True
    assert _seen("twitch|m2") is False
    assert {name for name, _args in kv.calls} == {"get", "increment"}


def test_distinct_events_each_claim_once_with_one_marker_each(kv: FakeKvHost) -> None:
    assert [_claim(f"twitch|m{i}") for i in range(50)] == [True] * 50
    assert [_claim(f"twitch|m{i}") for i in range(50)] == [False] * 50
    assert len(_marker_keys(kv)) == 50, "exactly one live key per event"


def test_a_redelivery_older_than_any_ring_size_is_still_caught(kv: FakeKvHost) -> None:
    """regression: the old last-16-ids ring forgot an event after 16 later claims (replay)."""
    assert _claim("twitch|oldest") is True
    for i in range(500):
        assert _claim(f"twitch|later-{i}") is True
    assert _claim("twitch|oldest") is False
    assert _seen("twitch|oldest") is True


def test_a_marker_that_lapsed_by_ttl_claims_again_which_is_why_the_age_fence_exists(
    kv: FakeKvHost,
) -> None:
    """The TTL IS the horizon; `is_fresh` makes sure no event can outlive it (see below)."""
    assert _claim("twitch|m1") is True
    for key in _marker_keys(kv):
        del kv.store[key]  # the fake ignores TTL: simulate the host expiring the key
    assert _claim("twitch|m1") is True


def test_communities_and_namespaces_are_isolated(kv: FakeKvHost) -> None:
    assert _claim("twitch|m1") is True
    assert _claim("twitch|m1", community="comm-2") is True
    assert _claim("twitch|m1", community=None) is True
    assert _claim("twitch|m1", namespace="jackpot") is True
    assert _claim("discord|m1") is True
    assert _claim("twitch|m1") is False


def test_keys_are_valid_pii_free_and_never_carry_the_raw_event_id(kv: FakeKvHost) -> None:
    raw_id = "twitch|8c4f0b1a-uuid-with-private-looking-id"
    _claim(raw_id)
    (key,) = _marker_keys(kv)
    validate_key(key)
    assert re.fullmatch(rf"c\.{COMMUNITY}\.steal\.seen\.[0-9a-f]{{32}}", key)
    assert ":" not in key and "8c4f0b1a" not in key
    assert all(b"8c4f0b1a" not in value for value in kv.store.values())


# -- the race ------------------------------------------------------------------------------------


def _yield_around_kv(mp: pytest.MonkeyPatch) -> None:
    """Make every kv facade call a suspension point so concurrent coroutines really interleave."""
    real_get, real_inc = community_kv.get, community_kv.increment

    async def get(c: str | None, k: str) -> bytes | None:
        await asyncio.sleep(0)
        value = await real_get(c, k)
        await asyncio.sleep(0)
        return value

    async def inc(c: str | None, k: str, d: int, ttl: int = 0) -> int:
        await asyncio.sleep(0)
        value = await real_inc(c, k, d, ttl)
        await asyncio.sleep(0)
        return value

    mp.setattr(community_kv, "get", get)
    mp.setattr(community_kv, "increment", inc)


@pytest.mark.parametrize("copies", [2, 5, 25])
def test_concurrent_duplicate_claims_let_exactly_one_through(
    kv: FakeKvHost, monkeypatch: pytest.MonkeyPatch, copies: int
) -> None:
    """Closed TOCTOU: the host serializes the increments, so only the one that sees 1 wins."""
    _yield_around_kv(monkeypatch)

    async def race() -> list[bool]:
        return list(
            await asyncio.gather(
                *(
                    claim(COMMUNITY, namespace="steal", event_id="twitch|race")
                    for _ in range(copies)
                )
            )
        )

    results = _run(race())
    assert results.count(True) == 1 and results.count(False) == copies - 1


def test_the_advisory_read_cannot_be_the_guard_it_loses_the_race_but_claim_does(
    kv: FakeKvHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two deliveries both pass `already_seen` (False) before either claims; `claim` still gates."""
    _yield_around_kv(monkeypatch)

    async def deliver() -> bool:
        if await already_seen(COMMUNITY, namespace="steal", event_id="twitch|r2"):
            return False
        return await claim(COMMUNITY, namespace="steal", event_id="twitch|r2")

    async def race() -> list[bool]:
        return list(await asyncio.gather(deliver(), deliver(), deliver()))

    assert _run(race()).count(True) == 1


# -- corrupted / failing state fails closed ------------------------------------------------------


@pytest.mark.parametrize("bad", [b"-1", b"-3"])
def test_a_marker_counter_below_one_fails_closed(kv: FakeKvHost, bad: bytes) -> None:
    _claim("twitch|m1")
    (key,) = _marker_keys(kv)
    kv.store[key] = bad
    with pytest.raises(DedupeStateError):
        _claim("twitch|m1")


def test_a_non_integer_marker_fails_loud_instead_of_being_reset(kv: FakeKvHost) -> None:
    _claim("twitch|m1")
    (key,) = _marker_keys(kv)
    kv.store[key] = b"garbage"
    with pytest.raises(ValueError):
        _claim("twitch|m1")
    assert kv.store[key] == b"garbage"


def test_a_host_failure_propagates_and_never_reads_as_a_first_delivery(
    kv: FakeKvHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full key quota / backend error must surface (the bundle refuses), never claim=True."""

    def boom(key: str, delta: int, ttl: int) -> int:
        raise RuntimeError("quota exceeded")

    monkeypatch.setattr(sys.modules["wit_world"].imports.kv, "increment", boom)
    with pytest.raises(RuntimeError, match="quota"):
        _claim("twitch|m1")


@pytest.mark.parametrize("namespace", ["", "Steal", "a:b", "x" * 33, "with space"])
def test_bad_namespace_is_rejected(kv: FakeKvHost, namespace: str) -> None:
    with pytest.raises(ValueError, match="namespace"):
        _claim("twitch|m1", namespace=namespace)
    with pytest.raises(ValueError, match="namespace"):
        _seen("twitch|m1", namespace=namespace)


def test_empty_community_still_fails_loud(kv: FakeKvHost) -> None:
    with pytest.raises(ValueError, match="community"):
        _claim("twitch|m1", community="")


def test_empty_event_id_is_rejected(kv: FakeKvHost) -> None:
    with pytest.raises(EventIdError):
        _claim("")


# -- event id ------------------------------------------------------------------------------------


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


# -- the age fence -------------------------------------------------------------------------------


def _stamp(offset_seconds: float) -> str:
    return rfc3339_from_millis(NOW_MS + int(offset_seconds * 1000))


def test_the_ttl_always_outlives_every_redelivery_the_fence_lets_through() -> None:
    """A redelivery <= MAX age after ingest is <= MAX after the first claim; the TTL covers it."""
    assert SEEN_TTL_SECONDS >= MAX_EVENT_AGE_SECONDS + EVENT_CLOCK_SKEW_SECONDS


def test_the_ttl_is_within_the_hosts_kv_ceiling() -> None:
    limits = Path(__file__).resolve().parents[3] / "core" / "bundle_host_kv" / "src" / "limits.rs"
    text = limits.read_text(encoding="utf-8")
    match = re.search(r"MAX_TTL_SECONDS: u32 = ([0-9 *]+);", text)
    assert match, "MAX_TTL_SECONDS not found in limits.rs"
    ceiling = 1
    for factor in match.group(1).split("*"):
        ceiling *= int(factor)
    assert 0 < SEEN_TTL_SECONDS <= ceiling


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (0, True),
        (-1, True),
        (-(MAX_EVENT_AGE_SECONDS - 1), True),
        (-MAX_EVENT_AGE_SECONDS, True),  # exactly at the limit is still fresh
        (-MAX_EVENT_AGE_SECONDS - 0.001, False),
        (-3600, False),
        (-86400 * 30, False),
        (EVENT_CLOCK_SKEW_SECONDS, True),  # a little clock skew into the future is tolerated
    ],
)
def test_is_fresh_boundaries(offset: float, expected: bool) -> None:
    assert is_fresh(_stamp(offset), now_millis=NOW_MS) is expected


@pytest.mark.parametrize("offset", [EVENT_CLOCK_SKEW_SECONDS + 0.001, 3600, 86400 * 365])
def test_a_future_dated_event_is_refused_loudly(offset: float) -> None:
    with pytest.raises(EventIdError, match="future"):
        is_fresh(_stamp(offset), now_millis=NOW_MS)


@pytest.mark.parametrize(
    "stamp",
    [
        None,
        "",
        "   ",
        5,
        b"2026-10-05T00:00:00Z",
        "yesterday",
        "2026-10-05",
        "2026-10-05T00:00:00",  # no offset: ambiguous, refuse
        "20261005T000000Z",
        "2026-13-45T00:00:00Z",
        "2026-10-05 00:00:00Z",
        "2026-10-05T00:00:00Zjunk",
    ],
)
def test_a_missing_or_malformed_stamp_fails_loud_never_unfenced(stamp: Any) -> None:
    with pytest.raises(EventIdError):
        is_fresh(stamp, now_millis=NOW_MS)


def test_is_fresh_accepts_the_stamps_ingest_produces() -> None:
    for stamp in (
        "2025-10-09T08:53:20.000Z",
        "2025-10-09T08:53:20Z",
        "2025-10-09T08:53:20.123456Z",
    ):
        assert isinstance(is_fresh(stamp, now_millis=NOW_MS), bool)
    assert is_fresh("2025-10-09T10:53:20.000+02:00", now_millis=1_760_000_000_000) is True


def test_is_fresh_defaults_to_the_host_clock(kv: FakeKvHost) -> None:
    _set_clock(NOW_MS)
    assert is_fresh(_stamp(-5)) is True
    assert is_fresh(_stamp(-MAX_EVENT_AGE_SECONDS - 1)) is False
    _set_clock(NOW_MS + (MAX_EVENT_AGE_SECONDS + 1) * 1000)
    assert is_fresh(_stamp(0)) is False


class _TtlKv:
    """A kv that honors TTLs against a simulated clock (the shared fake ignores them)."""

    def __init__(self) -> None:
        self.now_ms = NOW_MS
        self.values: dict[str, int] = {}
        self.expiry: dict[str, int] = {}

    def increment(self, key: str, delta: int, ttl_seconds: int) -> int:
        if key in self.expiry and self.now_ms > self.expiry[key]:
            del self.values[key], self.expiry[key]
        self.values[key] = self.values.get(key, 0) + delta
        self.expiry[key] = self.now_ms + ttl_seconds * 1000
        return self.values[key]

    def get(self, key: str) -> bytes | None:
        if key in self.expiry and self.now_ms > self.expiry[key]:
            return None
        return str(self.values[key]).encode() if key in self.values else None


def _deliver(sim: _TtlKv, stamp: str, event: str = "twitch|m1") -> str:
    """One delivery of the whole guard sequence at `sim.now_ms`: 'stale' | 'dup' | 'processed'."""
    if not is_fresh(stamp, now_millis=sim.now_ms):
        return "stale"
    if _run(already_seen(COMMUNITY, namespace="steal", event_id=event)):
        return "dup"
    return "processed" if _run(claim(COMMUNITY, namespace="steal", event_id=event)) else "dup"


@pytest.mark.parametrize("redelivery_after", [0, 1, 30, 150, 599, MAX_EVENT_AGE_SECONDS])
def test_a_redelivery_inside_the_fence_always_finds_its_marker(
    monkeypatch: pytest.MonkeyPatch, redelivery_after: int
) -> None:
    sim = _TtlKv()
    world = types.ModuleType("wit_world")
    world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        kv=types.SimpleNamespace(get=sim.get, increment=sim.increment)
    )
    monkeypatch.setitem(sys.modules, "wit_world", world)
    stamp = rfc3339_from_millis(sim.now_ms)  # ingested right now
    assert _deliver(sim, stamp) == "processed"
    sim.now_ms += redelivery_after * 1000
    assert _deliver(sim, stamp) == "dup"


def test_a_replay_after_the_marker_lapsed_is_refused_by_the_age_fence_never_processed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A consumer outage outlives the marker: the event is dropped, never replayed."""
    sim = _TtlKv()
    world = types.ModuleType("wit_world")
    world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        kv=types.SimpleNamespace(get=sim.get, increment=sim.increment)
    )
    monkeypatch.setitem(sys.modules, "wit_world", world)
    stamp = rfc3339_from_millis(sim.now_ms)
    assert _deliver(sim, stamp) == "processed"
    for outage in (MAX_EVENT_AGE_SECONDS + 1, SEEN_TTL_SECONDS + 1, 3600, 86400, 86400 * 7):
        later = _TtlKv()
        later.values, later.expiry, later.now_ms = dict(sim.values), dict(sim.expiry), sim.now_ms
        later.now_ms += outage * 1000
        assert _deliver(later, stamp) == "stale", f"replayed after {outage}s"
