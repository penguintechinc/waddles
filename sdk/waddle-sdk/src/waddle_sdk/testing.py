"""Shared, charset-enforcing test double for the `kv` host capability (gh-631).

**Why this module exists.** Every bundle test suite needs a stand-in for
``wit_world.imports.kv`` (no real componentize-py/wasmtime build runs in
unit tests). Before this module, each bundle hand-rolled its own in-memory
fake (`count`, `lurk`, `sdk/waddle-sdk/tests/test_community_kv.py`,
`test_sub_modules.py` all did), and every one of them accepted *any* string
as a key. The real host capability
(`core/bundle_host_kv/src/scope.rs::is_allowed_key_byte`) rejects any key
containing a byte outside ASCII alnum + ``_``/``-``/``.`` -- most
importantly ``:``, its own reserved namespace separator. `count`'s
`"count:registry"` and `lurk`'s `"lurk:state:{community}:{pseudonym}"` both
passed every test against a permissive fake and then failed on the real
host in production with `kv.error::backend` (gh-631).

Use :func:`install_fake_kv_host` (or the exported :class:`FakeKvHost`
directly) instead of hand-rolling a fake: it enforces the exact same
charset as ``waddle_sdk.kv.validate_key`` -- which is itself mirrored from
``scope.rs`` and pinned to it by
``sdk/waddle-sdk/tests/test_kv.py::test_allowed_key_charset_matches_host_scope_rs``
-- so a colon (or any other host-rejected byte) in a bundle's key fails the
test suite immediately instead of hiding a production-only bug.

Usage (pytest)::

    from waddle_sdk.testing import install_fake_kv_host

    @pytest.fixture
    def fake_kv(monkeypatch):
        return install_fake_kv_host(monkeypatch)

    def test_something(fake_kv):
        ...
        assert fake_kv.store["my.key"] == b"value"
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from typing import Any

from waddle_sdk.kv import InvalidKvKeyError, validate_key

__all__ = ["FakeKvHost", "InvalidKvKeyError", "install_fake_kv_host"]


@dataclass(slots=True)
class FakeKvHost:
    """In-memory stand-in for `wit_world.imports.kv`, validating every key like the real host.

    `store`/`calls` are public so a test can seed state or assert on exact
    host calls, matching the shape every hand-rolled fake already used
    (`.store[key]`, `.calls == [("get", (key,)), ...]`) -- migrating an
    existing bundle test suite onto this fake is a near drop-in swap.
    """

    store: dict[str, bytes] = field(default_factory=dict)
    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)

    def get(self, key: str) -> bytes | None:
        """Return the stored value for `key`, or `None` if unset. Validates `key` first."""
        validate_key(key)
        self.calls.append(("get", (key,)))
        return self.store.get(key)

    def set(self, key: str, value: bytes, ttl_seconds: int) -> None:
        """Store `value` under `key` (TTL recorded but not enforced). Validates `key` first."""
        validate_key(key)
        self.calls.append(("set", (key, bytes(value), ttl_seconds)))
        self.store[key] = bytes(value)

    def delete(self, key: str) -> None:
        """Delete `key` if present. Validates `key` first."""
        validate_key(key)
        self.calls.append(("delete", (key,)))
        self.store.pop(key, None)

    def increment(self, key: str, delta: int, ttl_seconds: int) -> int:
        """Add `delta` to the value under `key`, return the new total. Validates `key` first."""
        validate_key(key)
        self.calls.append(("increment", (key, delta, ttl_seconds)))
        current = int(self.store.get(key, b"0"))
        new_value = current + delta
        self.store[key] = str(new_value).encode()
        return new_value


def install_fake_kv_host(monkeypatch: Any) -> FakeKvHost:
    """Install a fresh `FakeKvHost` as `wit_world.imports.kv` and return it.

    `monkeypatch` takes `pytest.MonkeyPatch` (left untyped so importing this
    module never requires pytest to be installed outside test environments).
    """
    host = FakeKvHost()
    kv_mod = types.SimpleNamespace(
        get=host.get, set=host.set, delete=host.delete, increment=host.increment
    )
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(kv=kv_mod)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    return host
