"""Tests for `waddle_sdk.community_kv`."""

from __future__ import annotations

import sys
import types

import pytest

import waddle_sdk.community_kv as community_kv


def _run(coro):
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("community_kv coroutine unexpectedly suspended")


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
    """A value stored for one community is returned unchanged by get()."""
    _run(community_kv.set("community-1", "k", b"hello"))
    assert _run(community_kv.get("community-1", "k")) == b"hello"


def test_two_communities_never_collide(fake_kv) -> None:
    """The same `key` in two different communities resolves to two distinct kv entries."""
    _run(community_kv.set("community-1", "k", b"one"))
    _run(community_kv.set("community-2", "k", b"two"))
    assert _run(community_kv.get("community-1", "k")) == b"one"
    assert _run(community_kv.get("community-2", "k")) == b"two"


def test_delete_removes_only_that_communitys_key(fake_kv) -> None:
    """delete() scoped to one community never affects another community's same key."""
    _run(community_kv.set("community-1", "k", b"one"))
    _run(community_kv.set("community-2", "k", b"two"))
    _run(community_kv.delete("community-1", "k"))
    assert _run(community_kv.get("community-1", "k")) is None
    assert _run(community_kv.get("community-2", "k")) == b"two"


def test_increment_is_scoped_per_community(fake_kv) -> None:
    """increment() accumulates independently per community_id."""
    assert _run(community_kv.increment("community-1", "counter", 1)) == 1
    assert _run(community_kv.increment("community-2", "counter", 1)) == 1
    assert _run(community_kv.increment("community-1", "counter", 1)) == 2


@pytest.mark.parametrize("bad_id", ["", None])
def test_missing_community_id_fails_loud(fake_kv, bad_id) -> None:
    """A falsy community_id raises ValueError rather than silently using a global key."""
    with pytest.raises(ValueError, match="community_id"):
        _run(community_kv.get(bad_id, "k"))
