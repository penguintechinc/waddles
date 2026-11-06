"""Host-native tests for the Bar Citizen read-only bundle -- fake `wit_world` (kv/http/clock/relay/log)."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import Any, cast

import pytest
from app import (
    _PERMISSION_DENIED_MSG,
    _STALE_MARK,
    _UNAVAILABLE_MSG,
    _USAGE,
    CACHE_TTL_SECONDS,
    FEED_URL,
    LIST_SHOW,
    MAX_BODY_LEN,
    MAX_TITLE_LEN,
    SNAPSHOT_KEY,
    dispatch,
    transform,
)
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.testing import FakeKvHost, install_fake_kv_host

T0 = 1_790_000_000_000  # fake "now" (ms)
HOUR = 3_600_000


def _iso(ms: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _Host:
    """Fake host: shared kv, scriptable http responses, movable clock, recorded relay/log."""

    def __init__(self, kv: FakeKvHost) -> None:
        self.kv = kv
        self.now = T0
        self.http_calls: list[Any] = []
        self.http_responses: list[Any] = []
        self.relay_calls: list[tuple[str, dict[str, Any]]] = []
        self.log_calls: list[tuple[int, str, str]] = []

    def reply(self) -> str:
        return cast(str, self.relay_calls[-1][1]["text"])

    def logs_text(self) -> str:
        return " ".join(f"{m} {f}" for _l, m, f in self.log_calls)

    def feed(self, doc: Any, status: int = 200, truncated: bool = False) -> None:
        body = doc if isinstance(doc, bytes) else json.dumps(doc).encode()
        self.http_responses.append(("ok", status, body, truncated))


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> _Host:
    state = _Host(install_fake_kv_host(monkeypatch))
    wit = sys.modules["wit_world"]

    class _Err_Denied(Exception):  # noqa: N801 - mimics generated WIT variant class name
        pass

    def send(request: Any) -> Any:
        state.http_calls.append(request)
        kind, *rest = state.http_responses.pop(0)
        if kind == "err":
            raise rest[0]
        status, body, truncated = rest
        return types.SimpleNamespace(status=status, headers=[], body=body, truncated=truncated)

    wit.imports.http = types.SimpleNamespace(
        Header=lambda name, value: (name, value),
        Request=lambda **kw: types.SimpleNamespace(**kw),
        send=send,
    )
    wit.imports.clock = types.SimpleNamespace(now_millis=lambda: state.now)
    wit.imports.relay = types.SimpleNamespace(
        push=lambda p, m: state.relay_calls.append((p, json.loads(m)))
    )
    wit.imports.log = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields: state.log_calls.append((lvl, msg, fields)),
    )
    return state


def _event(text: str, **extra: Any) -> PlatformEvent:
    payload: dict[str, Any] = {"text": text, "channel_id": "chan-1"}
    payload.update(extra)
    return PlatformEvent(
        platform="discord",
        event_type="chat.message",
        actor="someone",
        payload=payload,
        occurred_at="2026-11-01T00:00:00.000Z",
    )


def _say(host: _Host, text: str, community: str | None = "c1", **extra: Any) -> str:
    parsed = _run(transform(_event(text, **extra)))
    assert parsed is not None
    env = StageEnvelope(
        tenant="t",
        community=community,
        app_id="waddles.core.barcitizen.readonly",
        stage="action",
        event=parsed,
        ts="2026-11-01T00:00:00.000Z",
    )
    _run(dispatch(env, {}, http_client=None))
    return host.reply()


def _doc(n_events: int = 3) -> dict[str, Any]:
    return {
        "events": [
            {
                "id": f"ev{i}",
                "title": f"Meetup {i}",
                "starts_at": _iso(T0 + i * HOUR),
                "location": "Area18 bar",
                "url": f"https://barcitizen.org/e/{i}",
            }
            for i in range(1, n_events + 1)
        ],
        "info": [
            {"id": "rules", "title": "Rules", "body": "Be kind."},
            {"id": "faq", "title": "FAQ", "body": "Ask a mod."},
        ],
    }


def test_transform_ignores_non_matching() -> None:
    assert _run(transform(_event("hello"))) is None
    assert _run(transform(_event("!bcx events"))) is None
    ev = PlatformEvent("discord", "chat.message", "a", {"text": 5}, "t")
    assert _run(transform(ev)) is None


@pytest.mark.parametrize(
    ("text", "command"),
    [
        ("!bc", "events"),
        ("!bc events", "events"),
        ("!BC Events", "events"),
        ("!bc events x", "usage"),
        ("!bc sync", "sync"),
        ("!bc sync x", "usage"),
        ("!bc event 2", "event"),
        ("!bc event", "usage"),
        ("!bc event a b", "usage"),
        ("!bc info", "info"),
        ("!bc info rules", "info"),
        ("!bc bogus", "usage"),
        ("!bc info " + "x" * 50, "usage"),
    ],
)
def test_grammar(host: _Host, text: str, command: str) -> None:
    out = _run(transform(_event(text)))
    assert out is not None
    assert out.payload["command"] == command


def test_badges_are_strict_booleans(host: _Host) -> None:
    out = _run(transform(_event("!bc sync", is_mod="false", is_broadcaster=True)))
    assert out is not None
    assert out.payload["is_mod"] is False
    assert out.payload["is_broadcaster"] is True


def test_events_fetches_caches_and_renders(host: _Host) -> None:
    host.feed(_doc())
    reply = _say(host, "!bc events")
    assert "1. Meetup 1" in reply
    assert "3. Meetup 3" in reply
    assert host.http_calls[0].url == FEED_URL
    assert host.http_calls[0].method == "GET"
    assert host.http_calls[0].secret_refs == []
    assert host.http_calls[0].body is None
    assert any(k.endswith(SNAPSHOT_KEY) for k in host.kv.store)


def test_second_read_within_ttl_uses_cache(host: _Host) -> None:
    host.feed(_doc())
    _say(host, "!bc events")
    host.now += (CACHE_TTL_SECONDS - 1) * 1000
    _say(host, "!bc info")
    assert len(host.http_calls) == 1


def test_refresh_after_ttl(host: _Host) -> None:
    host.feed(_doc(1))
    _say(host, "!bc events")
    host.now += (CACHE_TTL_SECONDS + 1) * 1000
    host.feed(_doc(2))
    reply = _say(host, "!bc events")
    assert len(host.http_calls) == 2
    assert "2. Meetup 2" in reply


def test_events_list_cap_and_more(host: _Host) -> None:
    host.feed(_doc(LIST_SHOW + 3))
    reply = _say(host, "!bc events")
    assert "(+3 more)" in reply
    assert f"{LIST_SHOW + 1}." not in reply


def test_past_events_not_listed(host: _Host) -> None:
    doc = _doc(2)
    doc["events"][0]["starts_at"] = _iso(T0 - HOUR)
    host.feed(doc)
    reply = _say(host, "!bc events")
    assert "Meetup 1" not in reply
    assert "1. Meetup 2" in reply


def test_no_upcoming(host: _Host) -> None:
    host.feed({"events": [], "info": []})
    assert "No upcoming" in _say(host, "!bc events")


def test_event_detail_by_position_and_id(host: _Host) -> None:
    host.feed(_doc())
    by_pos = _say(host, "!bc event 2")
    assert "Meetup 2" in by_pos
    assert "@ Area18 bar" in by_pos
    assert "https://barcitizen.org/e/2" in by_pos
    assert "Meetup 3" in _say(host, "!bc event ev3")
    assert "No such upcoming event" in _say(host, "!bc event 9")
    assert "No such upcoming event" in _say(host, "!bc event nope")


def test_info_topics_and_lookup(host: _Host) -> None:
    host.feed(_doc())
    assert "rules, faq" in _say(host, "!bc info")
    assert _say(host, "!bc info RULES") == "Rules: Be kind."
    assert "No such topic" in _say(host, "!bc info zzz")


def test_info_empty(host: _Host) -> None:
    host.feed({"events": [{"id": "a", "title": "T", "starts_at": _iso(T0 + HOUR)}]})
    assert "No Bar Citizen info" in _say(host, "!bc info")


def test_usage_does_no_io(host: _Host) -> None:
    assert _say(host, "!bc wat") == _USAGE
    assert host.http_calls == []
    assert host.kv.calls == []


def test_sync_is_mod_only_and_fail_closed(host: _Host) -> None:
    assert _say(host, "!bc sync") == _PERMISSION_DENIED_MSG
    assert _say(host, "!bc sync", is_mod="true") == _PERMISSION_DENIED_MSG
    assert host.http_calls == []
    host.feed(_doc())
    assert _say(host, "!bc sync", is_mod=True).startswith("Synced: 3 events, 2 info")
    host.feed(_doc(1))
    assert _say(host, "!bc sync", is_broadcaster=True).startswith("Synced: 1 events")


def test_sync_failure_with_cache_says_so(host: _Host) -> None:
    host.feed(_doc())
    _say(host, "!bc events")
    host.http_responses.append(("ok", 500, b"", False))
    reply = _say(host, "!bc sync", is_mod=True)
    assert reply.startswith("Sync failed - still serving cached")


def test_stale_cache_served_with_marker(host: _Host) -> None:
    host.feed(_doc())
    _say(host, "!bc events")
    host.now += (CACHE_TTL_SECONDS + 10) * 1000
    host.http_responses.append(("err", RuntimeError("boom")))
    reply = _say(host, "!bc events")
    assert reply.endswith(_STALE_MARK)
    assert "Meetup 1" in reply


@pytest.mark.parametrize(
    "response",
    [
        ("ok", 500, b"{}", False),
        ("ok", 200, b"{}", True),
        ("ok", 200, b"not json", False),
        ("ok", 200, b"\xff\xfe", False),
        ("ok", 200, b"[]", False),
        ("ok", 200, b'{"events": "x"}', False),
        ("err", RuntimeError("x")),
    ],
)
def test_no_cache_and_feed_failure_is_loud(host: _Host, response: tuple[Any, ...]) -> None:
    host.http_responses.append(response)
    parsed = _run(transform(_event("!bc events")))
    env = StageEnvelope("t", "c1", "a", "action", cast(PlatformEvent, parsed), "ts")
    with pytest.raises(RuntimeError, match="bar citizen read failed"):
        _run(dispatch(env, {}, http_client=None))
    assert host.reply() == _UNAVAILABLE_MSG
    assert host.log_calls[-1][0] == 0  # ERROR


def test_all_invalid_items_is_a_failure(host: _Host) -> None:
    host.feed({"events": [{"id": "bad id!", "title": "x", "starts_at": "nope"}], "info": [1]})
    env = StageEnvelope(
        "t", "c1", "a", "action", cast(PlatformEvent, _run(transform(_event("!bc")))), "ts"
    )
    with pytest.raises(RuntimeError):
        _run(dispatch(env, {}, http_client=None))


def test_invalid_items_dropped_valid_kept(host: _Host) -> None:
    doc = _doc(1)
    doc["events"] += [
        "str",
        {"id": "x y", "title": "t", "starts_at": _iso(T0 + HOUR)},
        {"id": "ok2", "title": 5, "starts_at": _iso(T0 + HOUR)},
        {"id": "ok3", "title": "t", "starts_at": "2026-11-01T00:00:00"},  # no tz
        {"id": "ok4", "title": "t", "starts_at": 5},
        {"id": "ok5", "title": "t", "starts_at": "x" * 50},
        {"id": "ok7", "title": "t", "starts_at": "not-a-date"},
        {"id": "ok6", "title": "   ", "starts_at": _iso(T0 + HOUR)},
    ]
    doc["info"] += ["str", {"id": "a", "title": "t"}, {"id": "b!", "title": "t", "body": "b"}]
    host.feed(doc)
    reply = _say(host, "!bc events")
    assert reply.count("Meetup") == 1
    assert '"dropped":11' in host.logs_text().replace(" ", "")


def test_untrusted_text_is_sanitised_and_capped(host: _Host) -> None:
    doc = _doc(1)
    doc["events"][0]["title"] = "@everyone <@123> " + "T" * 300
    doc["events"][0]["location"] = "x\ny\u0000z"
    doc["events"][0]["url"] = "http://evil.example/x"  # not https -> dropped
    doc["info"][0]["body"] = "/ban someone " + "B" * 1000
    host.feed(doc)
    ev = _say(host, "!bc event 1")
    assert "@everyone" not in ev
    assert "\n" not in ev
    assert "evil.example" not in ev
    assert len(ev.split(" - ")[0]) <= MAX_TITLE_LEN + 5
    info = _say(host, "!bc info rules")
    assert len(info) <= MAX_BODY_LEN + len("Rules: ") + 5


def test_reply_never_leads_with_a_command(host: _Host) -> None:
    doc = _doc(1)
    doc["info"] = [{"id": "x", "title": "/kick", "body": "b"}]
    host.feed(doc)
    reply = _say(host, "!bc info x")
    assert not reply.lstrip().startswith("/")


def test_url_dropped_when_it_contains_space_or_is_long(host: _Host) -> None:
    doc = _doc(2)
    doc["events"][0]["url"] = "https://a.example/a b"
    doc["events"][1]["url"] = "https://a.example/" + "p" * 200
    host.feed(doc)
    assert "http" not in _say(host, "!bc event 1")
    assert "http" not in _say(host, "!bc event 2")


def test_communities_have_separate_caches(host: _Host) -> None:
    host.feed(_doc(1))
    _say(host, "!bc events", community="c1")
    host.feed(_doc(2))
    _say(host, "!bc events", community="c2")
    assert len(host.http_calls) == 2
    assert len({k for k in host.kv.store}) == 2


def test_tenant_wide_community_none(host: _Host) -> None:
    host.feed(_doc(1))
    assert "Meetup 1" in _say(host, "!bc events", community=None)


def test_corrupt_or_malformed_cache_fails_loud(host: _Host) -> None:
    host.feed(_doc())
    _say(host, "!bc events")
    key = next(iter(host.kv.store))
    for bad in (b"\xff", b"[]", b'{"synced_ms": 1}'):
        host.kv.store[key] = bad
        env = StageEnvelope(
            "t", "c1", "a", "action", cast(PlatformEvent, _run(transform(_event("!bc")))), "ts"
        )
        with pytest.raises(RuntimeError):
            _run(dispatch(env, {}, http_client=None))


@pytest.mark.parametrize("op", ["get", "set"])
def test_kv_failures_fail_loud(host: _Host, monkeypatch: pytest.MonkeyPatch, op: str) -> None:
    host.feed(_doc())
    wit = sys.modules["wit_world"]

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("kv down")

    monkeypatch.setattr(wit.imports.kv, op, boom)
    parsed = _run(transform(_event("!bc")))
    env = StageEnvelope("t", "c1", "a", "action", cast(PlatformEvent, parsed), "ts")
    with pytest.raises(RuntimeError):
        _run(dispatch(env, {}, http_client=None))


def test_event_without_location_or_url(host: _Host) -> None:
    host.feed({"events": [{"id": "a", "title": "Solo", "starts_at": _iso(T0 + HOUR)}]})
    assert _say(host, "!bc event 1").startswith("Solo - ")
    assert "@" not in _say(host, "!bc event 1")


def test_dispatch_validation(host: _Host) -> None:
    ev = _run(transform(_event("!bc", channel_id="")))
    env = StageEnvelope("t", "c", "a", "action", cast(PlatformEvent, ev), "ts")
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(env, {}, http_client=None))
    bad = PlatformEvent("discord", "chat.message", "a", {"channel_id": "c", "command": "zap"}, "t")
    with pytest.raises(ValueError, match="unrecognized"):
        _run(dispatch(StageEnvelope("t", "c", "a", "action", bad, "ts"), {}, http_client=None))


def test_future_dated_cache_is_refreshed(host: _Host) -> None:
    host.feed(_doc(1))
    _say(host, "!bc events")
    host.now -= 10 * HOUR  # clock went backwards -> cache age negative -> not "fresh"
    host.feed(_doc(1))
    _say(host, "!bc events")
    assert len(host.http_calls) == 2


def test_logs_are_pii_and_text_free(host: _Host) -> None:
    host.feed(_doc())
    _say(host, "!bc events")
    _say(host, "!bc info rules")
    blob = host.logs_text()
    for leaked in ("Meetup", "Be kind", "someone", "Area18", "chan-1"):
        assert leaked not in blob
