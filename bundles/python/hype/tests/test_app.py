"""Host-native tests for the `hype` bundle -- fake `wit_world` approach mirrors `count`'s tests."""

from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

from app import TOTAL_KEY, dispatch, transform
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent

_ALLOWED = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.")


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


def _ev(text, **payload):
    return PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="u1",
        payload={"text": text, "channel_id": "c1", **payload},
        occurred_at="2026-10-09T00:00:00.000Z",
    )


def _text(ev):
    return ev.payload["text"]


def test_increment_then_read(host) -> None:
    assert "1" in _text(_run(transform(_ev("!hype"))))
    assert "2" in _text(_run(transform(_ev("!hype"))))
    assert host.kv.store[TOTAL_KEY] == b"2"
    read = _run(transform(_ev("!hype total")))
    assert "2" in _text(read)
    assert host.kv.store[TOTAL_KEY] == b"2"  # read does not mutate


def test_add_amount_and_bounds(host) -> None:
    assert "5" in _text(_run(transform(_ev("!hype add 5"))))
    assert "6" in _text(_run(transform(_ev("!hype add"))))
    assert "between" in _text(_run(transform(_ev("!hype add 0"))))
    assert "Usage" in _text(_run(transform(_ev("!hype add x"))))
    assert host.kv.store[TOTAL_KEY] == b"6"


def test_reset_requires_privilege(host) -> None:
    _run(transform(_ev("!hype")))
    denied = _run(transform(_ev("!hype reset", is_mod=False, is_broadcaster=False)))
    assert "Only the broadcaster" in _text(denied)
    no_roles = _run(transform(_ev("!hype reset")))
    assert "Only the broadcaster" in _text(no_roles)
    assert host.kv.store[TOTAL_KEY] == b"1"
    ok = _run(transform(_ev("!hype reset", is_mod=True)))
    assert "reset" in _text(ok)
    assert host.kv.store[TOTAL_KEY] == b"0"


def test_free_text_counts_and_logs_no_pii(host) -> None:
    _run(transform(_ev("!hype SecretUserName123")))
    assert host.kv.store[TOTAL_KEY] == b"1"
    assert host.logs
    assert all("SecretUserName123" not in str(entry) for entry in host.logs)


@pytest.mark.parametrize("text", ["hello", "", "!other", "hype", "!hypex"])
def test_non_matching_ignored(text, host) -> None:
    assert _run(transform(_ev(text))) is None
    assert host.kv.store == {}


def test_flag_off_and_non_chat(host) -> None:
    host.flag["on"] = False
    assert _run(transform(_ev("!hype"))) is None
    assert host.kv.store == {}
    host.flag["on"] = True
    ev = PlatformEvent(platform="twitch", event_type="x", actor=None, payload={}, occurred_at="")
    assert _run(transform(ev)) is None


def test_kv_failure_replies_and_does_not_crash(host) -> None:
    host.kv.fail = True
    assert "went wrong" in _text(_run(transform(_ev("!hype"))))


def test_dispatch_relays_and_validates(host) -> None:
    env = types.SimpleNamespace(event=_ev("!hype"))
    env.event.payload["text"] = "hi"
    res = _run(dispatch(env, {}, http_client=None))
    assert [(p, json.loads(m)) for p, m in host.relays] == [
        ("twitch", {"channel": "c1", "text": "hi"})
    ]
    assert res.detail == "relayed"
    env.event.payload["channel_id"] = ""
    with pytest.raises(ValueError):
        _run(dispatch(env, {}, http_client=None))
