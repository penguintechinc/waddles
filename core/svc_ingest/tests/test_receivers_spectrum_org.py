"""Tests for Spectrum org sync -- roster + event change detection (gh #101).

Layers: pure diff/guard/store logic (`receivers.spectrum_org`), RSI wire parsing and
paging over `httpx.MockTransport` (`RsiRestProvider`), the receiver loop driven by a
scripted provider against a REAL `MemorySnapshotStore` / fakeredis-backed
`RedisSnapshotStore`, `normalize()` for the new raw shapes, and the real ingest path
(receiver -> `fanout.fan_out_event` -> fakeredis -> `normalize`).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest
from flask_core import PlatformEvent
from flask_core.app_registry import AppRegistry
from flask_core.stream_pipeline import bundle_stream_key
from redis.exceptions import ConnectionError as RedisConnectionError
from waddle_transports import NonRetryableTransportError, RetryableTransportError

import receivers.spectrum_poll as sp
from builtin_handlers.spectrum_ingest import SPECTRUM_MANIFEST, normalize, register_default_bundles
from fanout import fan_out_event
from receivers import spectrum_org as so
from receivers.spectrum_org import (
    MemorySnapshotStore,
    RedisSnapshotStore,
    SnapshotAnomalyError,
    SnapshotStoreError,
    SpectrumEvent,
    SpectrumMember,
    diff_events,
    diff_roster,
)
from receivers.spectrum_poll import (
    ORG_CONSUMES_TAG,
    RsiRestProvider,
    SpectrumAuthError,
    SpectrumEndpointError,
    SpectrumPollReceiver,
    SpectrumTransientError,
)

TOKEN = "rsi-secret-token-value"  # noqa: S105
NOW = 1_700_000_000.0


def _m(mid: str, *roles: str, name: str | None = None) -> SpectrumMember:
    """Member `mid` holding `roles` (ids; the name equals the id uppercased)."""
    pairs = tuple(sorted((r, r.upper()) for r in roles))
    return SpectrumMember(member_id=mid, display_name=name or f"name-{mid}", roles=pairs)


def _e(
    eid: str,
    *,
    title: str = "Flight Night",
    start: float | None = NOW + 3600,
    status: str = so.STATUS_SCHEDULED,
    rsvp: int | None = 3,
    description: str | None = "bring ships",
) -> SpectrumEvent:
    """Event `eid`; defaults to scheduled one hour from `NOW`."""
    return SpectrumEvent(
        event_id=eid,
        title=title,
        description=description,
        starts_epoch=start,
        ends_epoch=None if start is None else start + 600,
        location="Stanton",
        organizer_id="9",
        status=status,
        rsvp_count=rsvp,
    )


def _snap_roster(*members: SpectrumMember) -> dict[str, Any]:
    return so.roster_snapshot({m.member_id: m for m in members})


def _snap_events(*events: SpectrumEvent) -> dict[str, Any]:
    return so.events_snapshot({e.event_id: e for e in events})


class TestDataclasses:
    def test_member_role_views_and_fingerprint(self) -> None:
        m = SpectrumMember("1", "n", (("a", "Alpha"), ("b", None)))
        assert m.role_ids == ("a", "b")
        assert m.role_names == ("Alpha",)  # unnamed roles omitted
        assert m.fingerprint() == "a,b"
        assert SpectrumMember("2", None).fingerprint() == ""

    def test_event_fingerprint_ignores_rsvp_but_not_content(self) -> None:
        base = _e("1")
        assert base.fingerprint() == _e("1", rsvp=99).fingerprint()
        assert base.fingerprint() != _e("1", title="Other").fingerprint()
        assert base.fingerprint() != _e("1", status=so.STATUS_CANCELLED).fingerprint()


class TestDiffRoster:
    def test_first_run_primes_without_changes(self) -> None:
        diff = diff_roster(None, [_m("1", "r1"), _m("2")])
        assert diff.primed and diff.changes == []
        assert diff.snapshot == {"v": 1, "members": {"1": "r1", "2": ""}}

    def test_first_run_with_backlog_reports_everyone_joined(self) -> None:
        diff = diff_roster(None, [_m("1", "r1")], emit_backlog=True)
        assert [(c.change, c.member_id, c.roles_added) for c in diff.changes] == [
            ("joined", "1", ("r1",))
        ]

    def test_join_leave_and_role_change(self) -> None:
        prev = _snap_roster(_m("1", "a", "b"), _m("2"), _m("3", "x"))
        cur = [_m("1", "b", "c"), _m("2"), _m("4", "z")]  # 1 changed, 3 left, 4 joined
        diff = diff_roster(prev, cur, guard_min_size=100)
        by_member = {c.member_id: c for c in diff.changes}
        assert set(by_member) == {"1", "3", "4"}
        assert by_member["1"].change == "roles_changed"
        assert by_member["1"].roles_added == ("c",) and by_member["1"].roles_removed == ("a",)
        assert by_member["1"].role_names == ("B", "C")
        assert by_member["3"].change == "left" and by_member["3"].display_name is None
        assert by_member["4"].change == "joined" and by_member["4"].roles_added == ("z",)
        assert not diff.primed
        assert diff.snapshot["members"] == {"1": "b,c", "2": "", "4": "z"}

    def test_unchanged_roster_yields_nothing(self) -> None:
        members = [_m("1", "a"), _m("2")]
        assert diff_roster(_snap_roster(*members), members).changes == []

    def test_duplicate_member_ids_collapse(self) -> None:
        diff = diff_roster(None, [_m("1", "a"), _m("1", "b")])
        assert diff.snapshot["members"] == {"1": "b"}

    def test_emptied_roster_is_an_anomaly_even_for_tiny_orgs(self) -> None:
        with pytest.raises(SnapshotAnomalyError, match="emptied"):
            diff_roster(_snap_roster(_m("1")), [])

    def test_large_shrink_is_an_anomaly(self) -> None:
        prev = _snap_roster(*[_m(str(i)) for i in range(20)])
        survivors = [_m(str(i)) for i in range(5)]  # 15/20 = 75% gone
        with pytest.raises(SnapshotAnomalyError, match="15/20"):
            diff_roster(prev, survivors)

    def test_shrink_at_or_under_ratio_is_accepted(self) -> None:
        prev = _snap_roster(*[_m(str(i)) for i in range(20)])
        diff = diff_roster(prev, [_m(str(i)) for i in range(10)])  # exactly 50%
        assert len([c for c in diff.changes if c.change == "left"]) == 10

    def test_ratio_override_accepts_a_genuine_mass_change(self) -> None:
        prev = _snap_roster(*[_m(str(i)) for i in range(20)])
        diff = diff_roster(prev, [_m("0")], max_departure_ratio=1.0)
        assert len(diff.changes) == 19

    def test_small_org_churn_below_guard_size_is_not_an_anomaly(self) -> None:
        prev = _snap_roster(_m("1"), _m("2"), _m("3"))
        diff = diff_roster(prev, [_m("3")])
        assert [c.change for c in diff.changes] == ["left", "left"]

    def test_malformed_previous_section_is_discarded_and_reprimed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.ERROR, logger="receivers.spectrum_org")
        diff = diff_roster({"v": 1, "oops": {}}, [_m("1")])
        assert diff.primed and diff.changes == []
        assert any("snapshot_malformed" in r.getMessage() for r in caplog.records)


class TestDiffEvents:
    def test_first_run_primes_and_backlog_creates(self) -> None:
        assert diff_events(None, [_e("1")], now=NOW).changes == []
        backlog = diff_events(None, [_e("1")], now=NOW, emit_backlog=True)
        assert [(c.change, c.event_id) for c in backlog.changes] == [("created", "1")]

    def test_created_updated_rsvp_and_unchanged(self) -> None:
        prev = _snap_events(_e("1"), _e("2"), _e("3", rsvp=1))
        cur = [_e("1"), _e("2", title="Renamed"), _e("3", rsvp=4), _e("4")]
        diff = diff_events(prev, cur, now=NOW)
        got = {(c.change, c.event_id) for c in diff.changes}
        assert got == {("updated", "2"), ("rsvp_changed", "3"), ("created", "4")}
        rsvp = next(c for c in diff.changes if c.change == "rsvp_changed")
        assert rsvp.rsvp_previous == 1 and rsvp.event is not None and rsvp.event.rsvp_count == 4

    def test_update_and_rsvp_together_emit_both(self) -> None:
        prev = _snap_events(_e("1", rsvp=1))
        diff = diff_events(prev, [_e("1", title="New", rsvp=2)], now=NOW)
        assert {c.change for c in diff.changes} == {"updated", "rsvp_changed"}

    def test_cancellation_replaces_the_update(self) -> None:
        prev = _snap_events(_e("1"))
        diff = diff_events(prev, [_e("1", status=so.STATUS_CANCELLED)], now=NOW)
        assert [c.change for c in diff.changes] == ["cancelled"]
        # Already cancelled and unchanged -> nothing more.
        again = diff_events(diff.snapshot, [_e("1", status=so.STATUS_CANCELLED)], now=NOW)
        assert again.changes == []

    def test_unknown_rsvp_count_never_emits(self) -> None:
        prev = _snap_events(_e("1", rsvp=3))
        assert diff_events(prev, [_e("1", rsvp=None)], now=NOW).changes == []

    def test_vanished_future_event_is_removed_but_past_event_is_pruned(self) -> None:
        prev = _snap_events(_e("future", start=NOW + 100), _e("past", start=NOW - 100))
        diff = diff_events(prev, [], now=NOW)
        assert [(c.change, c.event_id, c.event) for c in diff.changes] == [
            ("removed", "future", None)
        ]
        assert diff.pruned == 1
        assert diff.snapshot["events"] == {}

    def test_event_with_unknown_start_counts_as_upcoming(self) -> None:
        prev = _snap_events(_e("1", start=None))
        assert [c.change for c in diff_events(prev, [], now=NOW).changes] == ["removed"]

    def test_mass_vanish_of_upcoming_events_is_an_anomaly(self) -> None:
        prev = _snap_events(*[_e(str(i)) for i in range(8)])
        with pytest.raises(SnapshotAnomalyError, match="vanished"):
            diff_events(prev, [_e("0")], now=NOW)

    def test_few_events_vanishing_is_not_an_anomaly(self) -> None:
        prev = _snap_events(*[_e(str(i)) for i in range(3)])
        assert len(diff_events(prev, [], now=NOW).changes) == 3

    def test_malformed_previous_section_reprimes(self) -> None:
        assert diff_events({"v": 1}, [_e("1")], now=NOW).primed


class TestMemoryStore:
    async def test_round_trip_is_a_copy(self) -> None:
        store = MemorySnapshotStore()
        assert await store.load("k") is None
        snap = {"v": 1, "members": {"1": "a"}}
        await store.save("k", snap)
        snap["members"]["2"] = "b"  # mutating the caller's dict must not leak in
        loaded = await store.load("k")
        assert loaded == {"v": 1, "members": {"1": "a"}}
        assert loaded is not None
        loaded["members"]["3"] = "c"
        assert await store.load("k") == {"v": 1, "members": {"1": "a"}}


class _BrokenRedis:
    """Valkey stand-in whose every call raises, to prove outages are loud."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    async def get(self, name: str, /) -> Any:
        raise self.exc

    async def set(self, name: str, value: str, /, *, ex: int | None = None) -> Any:
        raise self.exc


class TestRedisStore:
    async def test_round_trip_key_prefix_and_ttl(self, redis_client: Any) -> None:
        store = RedisSnapshotStore(redis_client, prefix="p:spectrum:", ttl_s=100)
        assert await store.load("roster:o1") is None
        snap = _snap_roster(_m("1", "a"))
        await store.save("roster:o1", snap)
        assert await store.load("roster:o1") == snap
        assert 0 < await redis_client.ttl("p:spectrum:roster:o1") <= 100

    async def test_stored_snapshot_holds_no_names(self, redis_client: Any) -> None:
        store = RedisSnapshotStore(redis_client, prefix="p")
        await store.save("roster:o1", _snap_roster(_m("1", "a", name="SecretHandle")))
        raw = await redis_client.get("p:roster:o1")
        assert "SecretHandle" not in raw and "name-" not in raw

    @pytest.mark.parametrize("raw", ["not json{", "[1,2]", json.dumps({"v": 99, "members": {}})])
    async def test_unreadable_value_is_discarded_loudly(
        self, redis_client: Any, raw: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.ERROR, logger="receivers.spectrum_org")
        await redis_client.set("p:roster:o1", raw)
        store = RedisSnapshotStore(redis_client, prefix="p")
        assert await store.load("roster:o1") is None
        assert any("snapshot_unreadable" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("exc", [RedisConnectionError("down"), OSError("reset")])
    async def test_backend_failure_raises_never_reads_as_empty(self, exc: Exception) -> None:
        store = RedisSnapshotStore(_BrokenRedis(exc), prefix="p")  # type: ignore[arg-type]
        with pytest.raises(SnapshotStoreError, match="load failed"):
            await store.load("k")
        with pytest.raises(SnapshotStoreError, match="save failed"):
            await store.save("k", {"v": 1})


class TestWireParsing:
    def test_member_full_shape(self) -> None:
        m = sp._member_from_mapping(  # noqa: SLF001
            {
                "id": 102938,
                "displayname": "Citizen Display",
                "nickname": "nick",
                "roles": [{"id": 101, "name": "Officer"}, "7", {"name": "no-id"}, None],
                "rank": {"id": 3, "name": "Leader"},
            }
        )
        assert m is not None
        assert m.member_id == "102938" and m.display_name == "Citizen Display"
        assert dict(m.roles) == {"101": "Officer", "7": None, "rank:3": "Leader"}

    def test_member_alternates_and_missing_id(self) -> None:
        m = sp._member_from_mapping({"member_id": "5", "handle": "H", "role_ids": [1, 2]})  # noqa: SLF001
        assert m is not None and m.display_name == "H" and m.role_ids == ("1", "2")
        assert sp._member_from_mapping({"displayname": "x"}) is None  # noqa: SLF001
        bare = sp._member_from_mapping({"id": "1", "roles": "oops", "rank": "oops"})  # noqa: SLF001
        assert bare is not None and bare.roles == ()

    def test_event_full_shape_and_aliases(self) -> None:
        e = sp._event_from_mapping(  # noqa: SLF001
            {
                "id": 9876,
                "title": "Org Flight Night",
                "description": "  Weekly  ",
                "time_start": 1700500000,
                "time_end": "1700510000",
                "location": "Stanton",
                "member": {"id": 4},
                "rsvp": {"count_attending": 24, "count_maybe": 5},
            }
        )
        assert e is not None and e.event_id == "9876" and e.title == "Org Flight Night"
        assert e.description == "Weekly" and e.starts_epoch == 1700500000.0
        assert e.ends_epoch == 1700510000.0 and e.organizer_id == "4" and e.rsvp_count == 24
        assert e.status == so.STATUS_SCHEDULED
        alt = sp._event_from_mapping(  # noqa: SLF001
            {"event_id": "1", "name": "N", "start_time": 5, "attending_count": "7", "cancelled": 1}
        )
        assert alt is not None and alt.title == "N" and alt.starts_epoch == 5.0
        assert alt.rsvp_count == 7 and alt.status == so.STATUS_CANCELLED
        status_cancel = sp._event_from_mapping({"id": 1, "status": "cancelled", "rsvp_count": 2})  # noqa: SLF001
        assert status_cancel is not None and status_cancel.status == so.STATUS_CANCELLED
        assert sp._event_from_mapping({"title": "no id"}) is None  # noqa: SLF001

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (3, 3),
            (2.9, 2),
            ("12", 12),
            (" 4 ", 4),
            (-1, None),
            (True, None),
            ("x", None),
            (None, None),
        ],
    )
    def test_count_or_none(self, value: object, expected: int | None) -> None:
        assert sp._count_or_none(value) == expected  # noqa: SLF001


@pytest.fixture
def passthrough_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass the SSRF DNS pin so `httpx.MockTransport` serves the (fake) RSI host."""

    async def _req(client: httpx.AsyncClient, method: str, url: str, **kw: Any) -> httpx.Response:
        return await client.request(method, url, **kw)

    monkeypatch.setattr(sp, "guarded_request", _req)


def _page(key: str, items: list[dict[str, Any]], total: int | None = None) -> httpx.Response:
    data: dict[str, Any] = {key: items}
    if total is not None:
        data["total"] = total
    return httpx.Response(200, json={"success": 1, "code": "OK", "data": data})


def _provider(handler: Any, **kw: Any) -> RsiRestProvider:
    async def _nosleep(s: float) -> None:
        return None

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return RsiRestProvider(
        client, TOKEN, api_base="https://rsi.test/api/spectrum/", sleep=_nosleep, **kw
    )


@pytest.mark.usefixtures("passthrough_guard")
class TestProviderOrgFetch:
    async def test_roster_pages_until_total_and_sends_community_id(self) -> None:
        bodies: list[dict[str, Any]] = []

        def handler(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content)
            bodies.append(body)
            assert req.url.path == "/api/spectrum/community/member/list"
            assert req.headers["X-Rsi-Token"] == TOKEN
            start = (body["page"] - 1) * 2
            return _page(
                "members", [{"id": i, "displayname": f"n{i}"} for i in range(start, start + 2)], 5
            )

        members = await _provider(handler, page_size=2).fetch_roster("c77")
        assert [m.member_id for m in members] == ["0", "1", "2", "3", "4", "5"][: len(members)]
        assert len(members) >= 5  # stopped once total (5) was reached
        assert all(b["community_id"] == "c77" and b["pagesize"] == 2 for b in bodies)
        assert [b["page"] for b in bodies] == [1, 2, 3]

    async def test_roster_stops_on_empty_page(self) -> None:
        pages = [[{"id": "1"}, {"id": "2"}], []]

        def handler(req: httpx.Request) -> httpx.Response:
            return _page("members", pages[json.loads(req.content)["page"] - 1])

        assert [m.member_id for m in await _provider(handler).fetch_roster("c")] == ["1", "2"]

    async def test_roster_stops_when_server_ignores_page(self) -> None:
        calls = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return _page("members", [{"id": "1"}, {"id": "2"}])  # same page forever

        assert len(await _provider(handler).fetch_roster("c")) == 2
        assert calls["n"] == 2  # page 2 added nothing new -> stop

    async def test_roster_accepts_member_list_alias(self) -> None:
        resp = _page("member_list", [{"id": "1"}])
        got = await _provider(
            lambda r: resp if json.loads(r.content)["page"] == 1 else _page("member_list", [])
        ).fetch_roster("c")
        assert [m.member_id for m in got] == ["1"]

    async def test_roster_refuses_a_partial_snapshot_at_the_page_cap(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            page = json.loads(req.content)["page"]
            return _page("members", [{"id": f"{page}-{i}"} for i in range(2)])  # endless fresh data

        with pytest.raises(SpectrumEndpointError, match="partial snapshot"):
            await _provider(handler, max_pages=3).fetch_roster("c")

    async def test_roster_short_of_reported_total_warns_but_returns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="receivers.spectrum_poll")
        pages = {1: _page("members", [{"id": "1"}], 9), 2: _page("members", [], 9)}
        got = await _provider(lambda r: pages[json.loads(r.content)["page"]]).fetch_roster("c")
        assert len(got) == 1
        assert any("snapshot_short" in r.getMessage() for r in caplog.records)

    async def test_roster_unparseable_entries_fail_loud(self) -> None:
        resp = _page("members", [{"displayname": "no id"}, "junk"])
        with pytest.raises(SpectrumEndpointError, match="none parseable"):
            await _provider(lambda r: resp).fetch_roster("c")

    async def test_roster_missing_list_fails_loud(self) -> None:
        with pytest.raises(SpectrumEndpointError, match="no 'members' list"):
            await _provider(lambda r: _page("other", [])).fetch_roster("c")

    async def test_events_fetch_and_custom_path(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            assert req.url.path == "/api/spectrum/custom/events"
            if json.loads(req.content)["page"] > 1:
                return _page("events", [])
            return _page(
                "events",
                [{"id": 1, "title": "T", "time_start": 100, "rsvp": {"count_attending": 2}}],
            )

        got = await _provider(handler, paths={"events": "/custom/events"}).fetch_events("c")
        assert [(e.event_id, e.rsvp_count) for e in got] == [("1", 2)]

    async def test_auth_and_transient_errors_propagate_from_org_endpoints(self) -> None:
        with pytest.raises(SpectrumAuthError):
            await _provider(lambda r: httpx.Response(401)).fetch_roster("c")
        with pytest.raises(SpectrumTransientError, match="http_503"):
            await _provider(lambda r: httpx.Response(503)).fetch_events("c")


class ScriptedOrgProvider:
    """Serves scripted roster/event snapshots (or raises) per call, then stops the loop."""

    def __init__(self, roster: list[Any] | None = None, events: list[Any] | None = None) -> None:
        """Store the per-call scripts (a list entry may be an exception to raise)."""
        self.roster = list(roster or [])
        self.events = list(events or [])

    async def fetch(self, kind: str, source_id: str) -> list[Any]:
        raise AssertionError("org kinds must not call the message fetch")

    async def fetch_roster(self, source_id: str) -> list[SpectrumMember]:
        return self._next(self.roster)

    async def fetch_events(self, source_id: str) -> list[SpectrumEvent]:
        return self._next(self.events)

    @staticmethod
    def _next(script: list[Any]) -> Any:
        if not script:
            raise _StopError
        step = script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class _StopError(Exception):
    """Ends a receive() loop in tests once the script is exhausted."""


def _receiver(
    provider: Any, store: Any | None = None, **kw: Any
) -> tuple[SpectrumPollReceiver, list[float]]:
    sleeps: list[float] = []
    rcv = SpectrumPollReceiver(provider=provider, snapshot_store=store, **kw)

    async def _sleep(s: float) -> None:
        sleeps.append(s)

    rcv._sleep = _sleep  # noqa: SLF001
    rcv._now = lambda: NOW  # noqa: SLF001
    return rcv, sleeps


async def _drain(rcv: SpectrumPollReceiver, config: dict[str, Any]) -> list[Any]:
    out: list[Any] = []
    try:
        async for item in rcv.receive(config):
            out.append(item)
    except _StopError:
        pass
    return out


_ROSTER_CFG: dict[str, Any] = {"kind": "roster", "source_id": "org1", "roster_guard_min_size": 100}
_EVENTS_CFG: dict[str, Any] = {"kind": "events", "source_id": "org1"}


class TestRosterLoop:
    async def test_prime_then_emit_only_changes(self) -> None:
        prov = ScriptedOrgProvider(
            roster=[
                [_m("1", "a"), _m("2")],  # prime
                [_m("1", "a", "b"), _m("2"), _m("3")],  # 1 gains b, 3 joins
                [_m("1", "a", "b"), _m("3")],  # 2 leaves
                [_m("1", "a", "b"), _m("3")],  # nothing
            ]
        )
        rcv, _ = _receiver(prov)
        out = await _drain(rcv, _ROSTER_CFG)
        assert [(r["change"], r["member_id"]) for r in out] == [
            ("roles_changed", "1"),
            ("joined", "3"),
            ("left", "2"),
        ]
        first = out[0]
        assert first["platform"] == "spectrum" and first["kind"] == "roster"
        assert first["source_id"] == "org1" and first["roles_added"] == ["b"]
        assert first["observed_at"].startswith("2023-11-14")

    async def test_polls_at_the_slow_org_cadence_with_a_floor(self) -> None:
        prov = ScriptedOrgProvider(roster=[[_m("1")], [_m("1")]])
        rcv, sleeps = _receiver(prov)
        await _drain(rcv, {**_ROSTER_CFG, "poll_interval_s": 1})  # below the 60s org floor
        assert sleeps == [60.0, 60.0]
        prov = ScriptedOrgProvider(roster=[[_m("1")]])
        rcv, sleeps = _receiver(prov)
        await _drain(rcv, _ROSTER_CFG)
        assert sleeps == [300.0]  # org default

    async def test_snapshot_survives_a_restart_so_downtime_changes_are_seen(self) -> None:
        store = MemorySnapshotStore()
        rcv1, _ = _receiver(ScriptedOrgProvider(roster=[[_m("1"), _m("2")]]), store)
        assert await _drain(rcv1, _ROSTER_CFG) == []  # primed + persisted
        # "restart": brand-new receiver + provider, same store; 2 left and 3 joined meanwhile.
        rcv2, _ = _receiver(ScriptedOrgProvider(roster=[[_m("1"), _m("3")]]), store)
        out = await _drain(rcv2, _ROSTER_CFG)
        assert {(r["change"], r["member_id"]) for r in out} == {("joined", "3"), ("left", "2")}

    async def test_backlog_emits_the_whole_roster_on_first_poll(self) -> None:
        rcv, _ = _receiver(ScriptedOrgProvider(roster=[[_m("1"), _m("2")]]))
        out = await _drain(rcv, {**_ROSTER_CFG, "emit_backlog": True})
        assert [(r["change"], r["member_id"]) for r in out] == [("joined", "1"), ("joined", "2")]

    async def test_snapshot_advances_only_after_changes_are_yielded(self) -> None:
        store = MemorySnapshotStore()
        await store.save("roster:org1", _snap_roster(_m("1")))
        rcv, _ = _receiver(ScriptedOrgProvider(roster=[[_m("1"), _m("2")]]), store)
        stream = rcv.receive(_ROSTER_CFG)
        first = await anext(stream)
        assert first["member_id"] == "2"
        # Consumer hasn't asked for more yet -> the join is NOT yet recorded as seen...
        assert (await store.load("roster:org1"))["members"] == {"1": ""}  # type: ignore[index]
        await stream.aclose()  # ...so an interrupted consumer re-sees it next start (at-least-once)
        rcv2, _ = _receiver(ScriptedOrgProvider(roster=[[_m("1"), _m("2")]]), store)
        again = await _drain(rcv2, _ROSTER_CFG)
        assert [r["member_id"] for r in again] == ["2"]

    async def test_anomaly_does_not_advance_snapshot_and_backs_off_one_interval(self) -> None:
        store = MemorySnapshotStore()
        big = [_m(str(i)) for i in range(20)]
        await store.save("roster:org1", _snap_roster(*big))
        prov = ScriptedOrgProvider(roster=[[_m("0")], big])  # glitch, then recovery
        rcv, sleeps = _receiver(prov, store)
        out = await _drain(rcv, {**_ROSTER_CFG, "roster_guard_min_size": 10})
        assert out == []  # no mass-departure events were ever emitted
        assert sleeps[0] >= 300.0  # backed off a full org interval, not 2s
        assert (await store.load("roster:org1"))["members"].keys() == {m.member_id for m in big}  # type: ignore[index]

    async def test_persistent_anomaly_escalates_to_the_supervisor(self) -> None:
        store = MemorySnapshotStore()
        await store.save("roster:org1", _snap_roster(*[_m(str(i)) for i in range(20)]))
        prov = ScriptedOrgProvider(roster=[[_m("0")]] * 5)
        rcv, _ = _receiver(prov, store)
        with pytest.raises(RetryableTransportError, match="snapshot_anomaly"):
            await _drain(
                rcv, {**_ROSTER_CFG, "roster_guard_min_size": 10, "max_consecutive_errors": 3}
            )

    async def test_store_load_outage_is_transient_not_an_empty_snapshot(self) -> None:
        class DownStore:
            async def load(self, key: str) -> Any:
                raise SnapshotStoreError("down")

            async def save(self, key: str, snapshot: Any) -> None:
                raise AssertionError("must not save when load failed")

        rcv, sleeps = _receiver(ScriptedOrgProvider(roster=[[_m("1")]]), DownStore())
        with pytest.raises(RetryableTransportError, match="snapshot_store"):
            await _drain(rcv, {**_ROSTER_CFG, "max_consecutive_errors": 2})
        assert len(sleeps) == 1

    async def test_store_save_failure_backs_off_and_reemits_next_cycle(self) -> None:
        class FlakySaveStore(MemorySnapshotStore):
            def __init__(self) -> None:
                super().__init__()
                self.fail_next_save = False

            async def save(self, key: str, snapshot: Any) -> None:
                if self.fail_next_save:
                    self.fail_next_save = False
                    raise SnapshotStoreError("save down")
                await super().save(key, snapshot)

        store = FlakySaveStore()
        await store.save("roster:org1", _snap_roster(_m("1")))
        store.fail_next_save = True
        prov = ScriptedOrgProvider(roster=[[_m("1"), _m("2")], [_m("1"), _m("2")]])
        rcv, sleeps = _receiver(prov, store)
        out = await _drain(rcv, _ROSTER_CFG)
        # Cycle 1 emitted the join but couldn't record it; cycle 2 re-emits (at-least-once).
        assert [r["member_id"] for r in out] == ["2", "2"]
        assert sleeps[0] >= 300.0

    async def test_flag_off_idles_without_fetching(self) -> None:
        async def off() -> bool:
            return False

        prov = ScriptedOrgProvider(roster=[[_m("1")]])
        rcv, sleeps = _receiver(prov, flag_check=off)
        rcv._sleep = _stop_after(sleeps, 2)  # type: ignore[method-assign]  # noqa: SLF001
        assert await _drain(rcv, _ROSTER_CFG) == []
        assert prov.roster  # never consumed -> provider never called


def _stop_after(sleeps: list[float], n: int) -> Any:
    async def _sleep(s: float) -> None:
        sleeps.append(s)
        if len(sleeps) >= n:
            raise _StopError

    return _sleep


class TestEventsLoop:
    async def test_event_lifecycle(self) -> None:
        prov = ScriptedOrgProvider(
            events=[
                [_e("1", rsvp=1)],  # prime
                [_e("1", rsvp=1), _e("2")],  # 2 created
                [_e("1", title="Renamed", rsvp=5), _e("2")],  # 1 updated + rsvp
                [_e("1", title="Renamed", rsvp=5, status=so.STATUS_CANCELLED), _e("2")],
                [_e("1", title="Renamed", rsvp=5, status=so.STATUS_CANCELLED)],  # 2 removed
            ]
        )
        rcv, _ = _receiver(prov)
        out = await _drain(rcv, _EVENTS_CFG)
        assert [(r["change"], r["event_id"]) for r in out] == [
            ("created", "2"),
            ("updated", "1"),
            ("rsvp_changed", "1"),
            ("cancelled", "1"),
            ("removed", "2"),
        ]
        created, updated, rsvp = out[0], out[1], out[2]
        assert created["title"] == "Flight Night" and created["starts_at"].startswith("2023-11-14")
        assert updated["title"] == "Renamed" and updated["status"] == "scheduled"
        assert rsvp["rsvp_count"] == 5 and rsvp["rsvp_previous"] == 1
        assert out[4]["title"] is None  # removed: only the id is known

    async def test_long_description_is_bounded(self) -> None:
        rcv, _ = _receiver(
            ScriptedOrgProvider(events=[[], [_e("1", description="x" * 5000)]]),
        )
        out = await _drain(rcv, _EVENTS_CFG)
        assert len(out[0]["description"]) == so.MAX_DESCRIPTION_CHARS

    async def test_mass_vanish_is_held_back_as_an_anomaly(self) -> None:
        store = MemorySnapshotStore()
        await store.save("events:org1", _snap_events(*[_e(str(i)) for i in range(8)]))
        rcv, _ = _receiver(ScriptedOrgProvider(events=[[]]), store)
        with pytest.raises(RetryableTransportError, match="snapshot_anomaly"):
            await _drain(rcv, {**_EVENTS_CFG, "max_consecutive_errors": 1})


@pytest.mark.usefixtures("passthrough_guard")
class TestReceiveWiring:
    async def test_receive_polls_roster_over_http_with_paging_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SPECTRUM_RSI_TOKEN", TOKEN)
        seen: list[dict[str, Any]] = []

        def handler(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content)
            seen.append(body)
            if len(seen) == 1:
                return _page("members", [{"id": "1"}, {"id": "2"}])
            if len(seen) == 2:
                return _page("members", [])
            return httpx.Response(401)  # second poll: auth rejected -> loop ends

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        rcv = SpectrumPollReceiver(http_client=client)

        async def _nosleep(s: float) -> None:
            return None

        rcv._sleep = _nosleep  # noqa: SLF001
        cfg = {
            "kind": "roster",
            "source_id": "org9",
            "token_ref": "SPECTRUM_RSI_TOKEN",
            "api_base": "https://rsi.test/api/spectrum",
            "emit_backlog": True,
            "page_size": 25,
            "max_pages": 4,
            "page_delay_s": 0,
        }
        got: list[Any] = []
        with pytest.raises(NonRetryableTransportError, match="401"):
            async for item in rcv.receive(cfg):
                got.append(item)
        assert [g["member_id"] for g in got] == ["1", "2"]
        assert seen[0] == {"community_id": "org9", "page": 1, "pagesize": 25}

    async def test_invalid_kind_message_lists_org_kinds(self) -> None:
        with pytest.raises(NonRetryableTransportError, match="roster"):
            async for _ in SpectrumPollReceiver().receive({"kind": "nope", "source_id": "x"}):
                pass

    async def test_receiver_defaults_to_an_in_memory_store(self) -> None:
        rcv = SpectrumPollReceiver()
        assert isinstance(rcv._snapshot_store, MemorySnapshotStore)  # noqa: SLF001


class TestNormalize:
    async def test_roster_changes(self) -> None:
        ev = await normalize(
            {
                "platform": "spectrum",
                "kind": "roster",
                "source_id": "org1",
                "change": "roles_changed",
                "member_id": "42",
                "display_name": "Cit",
                "roles_added": ["b"],
                "roles_removed": ["a", 7, ""],
                "role_names": ["B"],
                "observed_at": "2026-10-10T00:00:00+00:00",
            }
        )
        assert isinstance(ev, PlatformEvent)
        assert ev.event_type == "member_roles_changed" and ev.actor == "42"
        assert ev.payload["roles_removed"] == ["a"] and ev.payload["roles_added"] == ["b"]
        assert ev.occurred_at == "2026-10-10T00:00:00+00:00"
        left = await normalize(
            {"kind": "roster", "source_id": "o", "change": "left", "member_id": "9"}
        )
        assert left.event_type == "member_left" and left.payload["display_name"] is None
        assert left.occurred_at  # falls back to now

    @pytest.mark.parametrize(
        ("change", "event_type"),
        [
            ("created", "org_event_created"),
            ("updated", "org_event_updated"),
            ("cancelled", "org_event_cancelled"),
            ("removed", "org_event_removed"),
            ("rsvp_changed", "org_event_rsvp_changed"),
        ],
    )
    async def test_event_changes(self, change: str, event_type: str) -> None:
        ev = await normalize(
            {
                "kind": "events",
                "source_id": "org1",
                "change": change,
                "event_id": "5",
                "title": "T",
                "organizer_id": "3",
                "rsvp_count": 4,
                "rsvp_previous": "x",
                "starts_at": "2026-10-11T00:00:00+00:00",
            }
        )
        assert ev.event_type == event_type and ev.actor == "3"
        assert ev.payload["rsvp_count"] == 4 and ev.payload["rsvp_previous"] is None
        assert ev.payload["event_id"] == "5" and ev.payload["kind"] == "events"

    @pytest.mark.parametrize(
        ("raw", "match"),
        [
            ({"kind": "roster", "source_id": "o", "change": "weird", "member_id": "1"}, "change"),
            ({"kind": "roster", "source_id": "o", "change": "left"}, "member_id"),
            ({"kind": "roster", "change": "left", "member_id": "1"}, "source_id"),
            ({"kind": "events", "source_id": "o", "change": "weird", "event_id": "1"}, "change"),
            ({"kind": "events", "source_id": "o", "change": "created"}, "event_id"),
            ({"kind": "events", "change": "created", "event_id": "1"}, "source_id"),
            ({"kind": "nope", "text": "t", "source_id": "o"}, "forum|lobby|roster|events"),
        ],
    )
    async def test_malformed_raw_events_raise(self, raw: dict[str, Any], match: str) -> None:
        with pytest.raises(ValueError, match=match):
            await normalize(raw)

    async def test_message_kinds_still_normalize(self) -> None:
        ev = await normalize({"kind": "lobby", "text": " hi ", "source_id": "L"})
        assert ev.event_type == "message" and ev.payload["text"] == "hi"


class TestLogsArePiiFree:
    async def test_org_logs_carry_ids_and_counts_only(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        store = MemorySnapshotStore()
        secret_name = "TotallyPrivateHandle"  # noqa: S105
        secret_desc = "private-event-description"
        roster = ScriptedOrgProvider(
            roster=[
                [_m("1", name=secret_name)],
                [_m("1", name=secret_name), _m("2", name=secret_name)],
                SpectrumTransientError("http_500"),
                [_m("1", name=secret_name)],
            ]
        )
        events = ScriptedOrgProvider(events=[[], [_e("1", description=secret_desc)]])
        rcv, _ = _receiver(roster, store)
        await _drain(rcv, _ROSTER_CFG)
        rcv2, _ = _receiver(events, store)
        await _drain(rcv2, _EVENTS_CFG)
        blob = "\n".join(r.getMessage() for r in caplog.records)
        assert "receiver.spectrum_org_started" in blob
        assert secret_name not in blob and secret_desc not in blob and TOKEN not in blob
        assert "Flight Night" not in blob and "Stanton" not in blob


class TestRealIngestPath:
    async def test_roster_change_flows_receiver_to_bundle_stream_to_normalize(
        self, redis_client: Any
    ) -> None:
        registry = AppRegistry()
        register_default_bundles(registry)
        store = RedisSnapshotStore(redis_client, prefix="waddles:t:global:spectrum:snapshot")
        prov = ScriptedOrgProvider(roster=[[_m("1", "a")], [_m("1", "a"), _m("2", "b")]])
        rcv, _ = _receiver(prov, store)

        for raw in await _drain(rcv, _ROSTER_CFG):
            delivered = await fan_out_event(
                raw,
                consumes_tag=ORG_CONSUMES_TAG,
                tenant="global",
                community=None,
                redis_client=redis_client,
                registry=registry,
            )
            assert delivered == 1

        key = bundle_stream_key("global", None, SPECTRUM_MANIFEST["app_id"], "ingest")
        assert await redis_client.llen(key) == 1
        raw_event = json.loads(await redis_client.rpop(key))
        event = await normalize(raw_event)
        assert event.event_type == "member_joined" and event.actor == "2"
        assert event.payload["roles_added"] == ["b"] and event.payload["role_names"] == ["B"]
        # The Valkey snapshot is what carries state across restarts -- and holds no names.
        stored = await redis_client.get("waddles:t:global:spectrum:snapshot:roster:org1")
        assert json.loads(stored)["members"] == {"1": "a", "2": "b"}
        assert "name-" not in stored

    async def test_event_change_flows_through_the_same_app(self, redis_client: Any) -> None:
        registry = AppRegistry()
        register_default_bundles(registry)
        prov = ScriptedOrgProvider(events=[[], [_e("1", rsvp=2)]])
        rcv, _ = _receiver(prov, RedisSnapshotStore(redis_client, prefix="p"))
        (raw,) = await _drain(rcv, _EVENTS_CFG)
        assert (
            await fan_out_event(
                raw,
                consumes_tag=ORG_CONSUMES_TAG,
                tenant="global",
                community=None,
                redis_client=redis_client,
                registry=registry,
            )
            == 1
        )
        key = bundle_stream_key("global", None, SPECTRUM_MANIFEST["app_id"], "ingest")
        event = await normalize(json.loads(await redis_client.rpop(key)))
        assert event.event_type == "org_event_created" and event.payload["rsvp_count"] == 2

    async def test_no_outbound_path_exists_for_org_sync(self) -> None:
        assert SpectrumPollReceiver.directions == frozenset({sp.Direction.INBOUND})
