"""Tests for `waddle_sdk.community_kv`."""

from __future__ import annotations

import pytest

import waddle_sdk.community_kv as community_kv
from waddle_sdk.kv import InvalidKvKeyError
from waddle_sdk.testing import install_fake_kv_host


def _run(coro):
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("community_kv coroutine unexpectedly suspended")


@pytest.fixture
def fake_kv(monkeypatch: pytest.MonkeyPatch):
    return install_fake_kv_host(monkeypatch).store


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


# regression: gh-631 -- `_scoped_key` originally built `f"c:{community_id}:{key}"`, which the
# real `kv` host capability rejects (`core/bundle_host_kv/src/scope.rs` reserves `:` as its own
# namespace separator). `community_kv` is unused by any shipped bundle today, but it is
# documented SDK surface (`AUTHORING.md` Sec2) -- it must not model a key shape that fails on
# the real host the moment a bundle adopts it.
def test_scoped_key_contains_no_colon() -> None:
    assert ":" not in community_kv._scoped_key("community-1", "k")


def test_scoped_key_satisfies_host_guest_key_charset(fake_kv) -> None:
    # Raises InvalidKvKeyError if the scoped key the real host would see is invalid.
    _run(community_kv.set("community-1", "k", b"v"))


def test_colon_in_caller_supplied_key_is_rejected(fake_kv) -> None:
    """A bundle author's own `key` argument is still validated once scoped."""
    with pytest.raises(InvalidKvKeyError, match="characters outside"):
        _run(community_kv.set("community-1", "bad:key", b"v"))
