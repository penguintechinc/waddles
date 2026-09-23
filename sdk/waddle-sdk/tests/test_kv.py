"""Tests for `waddle_sdk.kv`."""

from __future__ import annotations

import sys
import types

import pytest

import waddle_sdk.kv as kv


def _run(coro):
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("kv coroutine unexpectedly suspended")


@pytest.fixture
def fake_kv(monkeypatch: pytest.MonkeyPatch):
    store: dict[str, bytes] = {}

    def get(key: str):
        return store.get(key)

    def set_(key: str, value: bytes, ttl_seconds: int) -> None:
        store[key] = bytes(value)

    def delete(key: str) -> None:
        store.pop(key, None)

    def increment(key: str, delta: int, ttl_seconds: int) -> int:
        current = int(store.get(key, b"0"))
        new_value = current + delta
        store[key] = str(new_value).encode()
        return new_value

    kv_mod = types.SimpleNamespace(get=get, set=set_, delete=delete, increment=increment)
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(kv=kv_mod)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    return store


def test_set_then_get_round_trips(fake_kv) -> None:
    """A value stored via set() is returned unchanged by get()."""
    _run(kv.set("k", b"hello"))
    assert _run(kv.get("k")) == b"hello"


def test_get_returns_none_for_missing_key(fake_kv) -> None:
    """get() returns None, never raises, for an unset key."""
    assert _run(kv.get("missing")) is None


def test_delete_removes_the_key(fake_kv) -> None:
    """delete() removes a previously set key."""
    _run(kv.set("k", b"v"))
    _run(kv.delete("k"))
    assert _run(kv.get("k")) is None


def test_increment_accumulates(fake_kv) -> None:
    """increment() adds delta to the stored value and returns the new total."""
    assert _run(kv.increment("counter", 1)) == 1
    assert _run(kv.increment("counter", 4)) == 5
