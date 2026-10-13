"""Host-native tests for the `vote` bundle -- fake `wit_world` approach mirrors `count`'s tests."""

from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest
from app import STATE_KEY, dispatch, transform
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent

_ALLOWED = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-."
)


def _run(coro):
    return asyncio.run(coro)


class _FakeKv:
    """In-memory `kv` host stand-in that validates keys like the real host."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.fail = False

    def _check(self, key: str) -> None:
        assert key and all(ch in _ALLOWED for ch in key), key
        if self.fail:
            raise RuntimeError("scripted failure")

    def get(self, key):
        self._check(key)
        return self.store.get(key)

    def set(self, key, value, ttl_seconds):
        self._check(key)
        self.store[key] = bytes(value)

    def delete(self, key):
        self._check(key)
        self.store.pop(key, None)

    def increment(self, key, delta, ttl_seconds):
        self._check(key)
        new = int(self.store.get(key, b"0")) + delta
        self.store[key] = str(new).encode()
        return new


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch):
    """Fake WIT host with a flag toggle, in-memory kv, and recording relay/log."""
    kv = _FakeKv()
    relays: list = []
    logs: list = []
    flag = {"on": True}
    world = types.ModuleType("wit_world")
    world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        flags=types.SimpleNamespace(enabled=lambda key, default_value: flag["on"]),
        kv=kv,
        relay=types.SimpleNamespace(push=lambda p, m: relays.append((p, m))),
        log=types.SimpleNamespace(
            Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
            write=lambda lvl, msg, fields: logs.append((lvl, msg, fields)),
        ),
    )
    monkeypatch.setitem(sys.modules, "wit_world", world)
    return types.SimpleNamespace(kv=kv, relays=relays, logs=logs, flag=flag)


def _ev(text, actor="u1", **payload):
    return PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor=actor,
        payload={"text": text, "channel_id": "c1", **payload},
        occurred_at="2026-10-09T00:00:00.000Z",
    )


def _say(text, actor="u1", **payload):
    return _run(transform(_ev(text, actor, **payload))).payload["text"]


def _state(host):
    return json.loads(host.kv.store[STATE_KEY])


MOD = {"is_mod": True}


def test_cast_requires_open_vote(host) -> None:
    assert "No vote is open" in _say("!vote pizza")
    assert host.kv.store == {}


def test_start_cast_standings_close(host) -> None:
    assert "started" in _say("!vote start", **MOD)
    assert "pizza: 1" in _say("!vote Pizza", "a")
    _say("!vote pizza", "b")
    _say("!vote tacos", "c")
    assert "OPEN" in _say("!vote") and "pizza: 2, tacos: 1" in _say("!vote")
    assert "Final: pizza: 2, tacos: 1" in _say("!vote close", **MOD)
    assert "closed" in _say("!vote")
    assert "No vote is open" in _say("!vote pizza", "d")


def test_revote_moves_and_duplicate_rejected(host) -> None:
    _say("!vote start", **MOD)
    _say("!vote a", "x")
    assert "already voted" in _say("!vote a", "x")
    _say("!vote b", "x")
    assert _state(host)["tally"] == {"b": 1}


def test_start_close_require_privilege(host) -> None:
    assert "Only the broadcaster" in _say("!vote start")
    assert "Only the broadcaster" in _say(
        "!vote start", is_mod=False, is_broadcaster=False
    )
    assert host.kv.store == {}
    _say("!vote start", is_broadcaster=True)
    assert "Only the broadcaster" in _say("!vote close")
    assert _state(host)["open"] is True


def test_close_when_not_open_and_restart_resets(host) -> None:
    assert "No vote is open" in _say("!vote close", **MOD)
    _say("!vote start", **MOD)
    _say("!vote a", "x")
    _say("!vote start", **MOD)
    assert _state(host)["tally"] == {}


def test_bounds(host) -> None:
    _say("!vote start", **MOD)
    assert "Usage" in _say("!vote two words")
    assert "Usage" in _say("!vote " + "x" * 33)
    for n in range(20):
        _say(f"!vote opt{n}", f"v{n}")
    assert "Too many options" in _say("!vote extra", "late")
    assert "Too many options" not in _say("!vote opt1", "late")


def test_voter_ids_hashed_and_no_actor_rejected(host) -> None:
    _say("!vote start", **MOD)
    _say("!vote a", "RawActorName")
    assert "RawActorName" not in host.kv.store[STATE_KEY].decode()
    assert "not counted" in _say("!vote a", None)


def test_logs_pii_free(host) -> None:
    _say("!vote start", **MOD)
    _say("!vote SecretOptionXyz", "SecretActorAbc")
    assert host.logs
    joined = " ".join(str(e) for e in host.logs)
    assert "SecretOptionXyz" not in joined and "SecretActorAbc" not in joined


def test_corrupt_state_fails_loud(host) -> None:
    host.kv.store[STATE_KEY] = b"not json"
    assert "went wrong" in _say("!vote")
    host.kv.store[STATE_KEY] = b'{"open": 1}'
    assert "went wrong" in _say("!vote")


@pytest.mark.parametrize("text", ["hello", "", "!other", "vote", "!votes"])
def test_non_matching_ignored(text, host) -> None:
    assert _run(transform(_ev(text))) is None
    assert host.kv.store == {}


def test_flag_off_and_non_chat(host) -> None:
    host.flag["on"] = False
    assert _run(transform(_ev("!vote start", **MOD))) is None
    assert host.kv.store == {}
    host.flag["on"] = True
    ev = PlatformEvent(
        platform="twitch", event_type="x", actor=None, payload={}, occurred_at=""
    )
    assert _run(transform(ev)) is None


def test_kv_failure_replies_and_does_not_crash(host) -> None:
    host.kv.fail = True
    assert "went wrong" in _say("!vote")


def test_dispatch_relays_and_validates(host) -> None:
    env = types.SimpleNamespace(event=_ev("!vote"))
    env.event.payload["text"] = "hi"
    res = _run(dispatch(env, {}, http_client=None))
    assert [(p, json.loads(m)) for p, m in host.relays] == [
        ("twitch", {"channel": "c1", "text": "hi"})
    ]
    assert res.detail == "relayed"
    env.event.payload["channel_id"] = ""
    with pytest.raises(ValueError):
        _run(dispatch(env, {}, http_client=None))
