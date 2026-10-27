"""Tests for `waddle_sdk.kv`."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import waddle_sdk.kv as kv
from waddle_sdk.testing import install_fake_kv_host


def _run(coro):
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("kv coroutine unexpectedly suspended")


@pytest.fixture
def fake_kv(monkeypatch: pytest.MonkeyPatch):
    return install_fake_kv_host(monkeypatch).store


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


# --- guest-key charset validation (gh-631) ----------------------------------
#
# `validate_key()` mirrors `core/bundle_host_kv/src/scope.rs::validate_guest_key`
# so a bad key is a precise `InvalidKvKeyError` raised before any host call,
# never a generic `kv.error::backend` surfaced only after a real Valkey round
# trip in production.


def test_validate_key_accepts_the_allowed_charset() -> None:
    kv.validate_key("counters.viewer-count_1")  # must not raise


@pytest.mark.parametrize("bad_key", ["other.app:secret", "a:b", ":"])
def test_validate_key_rejects_colon(bad_key: str) -> None:
    with pytest.raises(kv.InvalidKvKeyError, match="characters outside"):
        kv.validate_key(bad_key)


@pytest.mark.parametrize("bad_key", ["*", "?", "[abc]", "a*b", "has space", "line1\nline2"])
def test_validate_key_rejects_glob_and_whitespace(bad_key: str) -> None:
    with pytest.raises(kv.InvalidKvKeyError, match="characters outside"):
        kv.validate_key(bad_key)


def test_validate_key_rejects_empty() -> None:
    with pytest.raises(kv.InvalidKvKeyError, match="1-256 bytes"):
        kv.validate_key("")


def test_validate_key_rejects_over_length() -> None:
    with pytest.raises(kv.InvalidKvKeyError, match="1-256 bytes"):
        kv.validate_key("a" * (kv.MAX_KEY_LEN + 1))


@pytest.mark.parametrize("method", ["get", "delete"])
def test_unary_ops_reject_a_bad_key_before_any_host_call(method: str) -> None:
    """No `wit_world` import is even attempted for an invalid key -- fails at the SDK boundary."""
    with pytest.raises(kv.InvalidKvKeyError):
        _run(getattr(kv, method)("bad:key"))


def test_set_rejects_a_bad_key_before_any_host_call() -> None:
    with pytest.raises(kv.InvalidKvKeyError):
        _run(kv.set("bad:key", b"v"))


def test_increment_rejects_a_bad_key_before_any_host_call() -> None:
    with pytest.raises(kv.InvalidKvKeyError):
        _run(kv.increment("bad:key", 1))


# --- cross-language pin: Python charset must match the real Rust host ------

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCOPE_RS = _REPO_ROOT / "core" / "bundle_host_kv" / "src" / "scope.rs"


def test_allowed_key_charset_matches_host_scope_rs() -> None:
    """Pins `waddle_sdk.kv`'s guest-key charset/length to `core/bundle_host_kv/src/scope.rs`.

    Python can't import Rust, so `_ALLOWED_KEY_CHARS`/`MAX_KEY_LEN` are
    hand-mirrored from `scope.rs::is_allowed_key_byte`/`MAX_GUEST_KEY_LEN`.
    This test reads the real Rust source and fails loudly the moment either
    side drifts from the other -- `scope.rs` is the normative definition of
    what the real host accepts (gh-631); update both together.
    """
    assert _SCOPE_RS.is_file(), f"expected host kv scope source at {_SCOPE_RS}"
    source = _SCOPE_RS.read_text()

    byte_fn_match = re.search(
        r"fn is_allowed_key_byte\(b: u8\) -> bool \{\s*"
        r"b\.is_ascii_alphanumeric\(\) \|\| matches!\(b, (?P<extras>.+?)\)\s*\}",
        source,
        re.DOTALL,
    )
    assert byte_fn_match is not None, (
        "core/bundle_host_kv/src/scope.rs::is_allowed_key_byte's shape changed in a way this "
        "regex pin doesn't recognize -- update the pin alongside the Rust change, don't just "
        "relax this assertion"
    )
    extra_bytes = set(re.findall(r"b'(.)'", byte_fn_match.group("extras")))
    alnum = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
    assert (alnum | extra_bytes) == kv._ALLOWED_KEY_CHARS, (
        "waddle_sdk.kv._ALLOWED_KEY_CHARS has drifted from "
        "core/bundle_host_kv/src/scope.rs::is_allowed_key_byte"
    )
    assert ":" not in kv._ALLOWED_KEY_CHARS, (
        "':' must stay rejected -- it is the host's own namespace separator"
    )

    max_len_match = re.search(r"MAX_GUEST_KEY_LEN: usize = (?P<len>\d+);", source)
    assert max_len_match is not None, (
        "core/bundle_host_kv/src/scope.rs::MAX_GUEST_KEY_LEN's declaration shape changed -- "
        "update this pin"
    )
    assert int(max_len_match.group("len")) == kv.MAX_KEY_LEN, (
        "waddle_sdk.kv.MAX_KEY_LEN has drifted from "
        "core/bundle_host_kv/src/scope.rs::MAX_GUEST_KEY_LEN"
    )
