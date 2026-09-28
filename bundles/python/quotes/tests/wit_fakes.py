"""Minimal fake `wit_world` host imports for this bundle's own host-side pytest suite.

No WASM/wasmtime here -- `transform()` is plain async Python, testable
directly (same as `bundles/python/pyping/tests/test_app.py`); `dispatch()`'s
`kv` calls are exercised through the real `waddle_sdk.kv` facade against a
fake `wit_world.imports.kv` module, proving this bundle's own key layout and
read-modify-write index logic -- the `kv` capability itself is still
hardcoded `access-denied` host-side as of this PR (see `app.py`'s own module
docstring), so this is the furthest this bundle's logic can be exercised
until that host wiring (`feature/bundle-kv-capability`) lands.
"""

from __future__ import annotations

import sys
import types
from enum import Enum
from typing import Any


class FakeWitKv:
    """In-memory stand-in for the generated `wit_world.imports.kv` module.

    `get`/`set`/`delete` share one `bytes` store; `increment` uses a
    separate integer counter store, matching the WIT `kv` interface's own
    shape (`increment` is its own atomic primitive, not a read-modify-write
    over the same value `get`/`set` see).
    """

    def __init__(self) -> None:
        """Start with empty stores."""
        self.store: dict[str, bytes] = {}
        self.counters: dict[str, int] = {}

    def get(self, key: str) -> bytes | None:
        """Fake implementation of the generated `kv.get(key) -> option<list<u8>>`."""
        return self.store.get(key)

    def set(self, key: str, value: bytes, ttl_seconds: int) -> None:
        """Fake implementation of the generated `kv.set(key, value, ttl_seconds)`."""
        self.store[key] = bytes(value)

    def delete(self, key: str) -> None:
        """Fake implementation of the generated `kv.delete(key)`."""
        self.store.pop(key, None)

    def increment(self, key: str, delta: int, ttl_seconds: int) -> int:
        """Fake implementation of the generated `kv.increment(key, delta, ttl_seconds) -> s64`."""
        new_value = self.counters.get(key, 0) + delta
        self.counters[key] = new_value
        return new_value


class FakeLog:
    """Records every `log.write(level, message, fields_json)` call; never raises."""

    class Level(Enum):
        """Matches the generated `log.Level` enum."""

        ERROR = 0
        WARN = 1
        INFO = 2
        DEBUG = 3

    def __init__(self) -> None:
        """Start with no recorded calls."""
        self.calls: list[tuple[str, str, str]] = []

    def write(self, level: Any, message: str, fields_json: str) -> None:
        """Fake implementation of the generated `log.write(...)`."""
        self.calls.append((level.name, message, fields_json))


class FakeFlags:
    """Fake `wit_world.imports.flags` -- returns whatever `enabled` map says, else the default."""

    def __init__(self, enabled: dict[str, bool] | None = None) -> None:
        """Seed with an optional `{flag_key: value}` override map."""
        self._enabled = dict(enabled or {})

    def enabled(self, key: str, default_value: bool) -> bool:
        """Fake implementation of the generated `flags.enabled(key, default_value)`."""
        return self._enabled.get(key, default_value)


class FakeRelay:
    """Records every `relay.push(provider, message_json)` call."""

    def __init__(self) -> None:
        """Start with no recorded calls."""
        self.calls: list[tuple[str, str]] = []

    def push(self, provider: str, message_json: str) -> None:
        """Fake implementation of the generated `relay.push(...)`."""
        self.calls.append((provider, message_json))


def install(
    monkeypatch: Any, *, flags_enabled: dict[str, bool] | None = None
) -> tuple[FakeWitKv, FakeLog, FakeRelay, FakeFlags]:
    """Install a fresh fake `wit_world` (kv/log/relay/flags) and return each fake."""
    fake_kv = FakeWitKv()
    fake_log = FakeLog()
    fake_relay = FakeRelay()
    fake_flags = FakeFlags(flags_enabled)

    kv_mod = types.SimpleNamespace(
        get=fake_kv.get, set=fake_kv.set, delete=fake_kv.delete, increment=fake_kv.increment
    )
    log_mod = types.SimpleNamespace(write=fake_log.write, Level=FakeLog.Level)
    relay_mod = types.SimpleNamespace(push=fake_relay.push)
    flags_mod = types.SimpleNamespace(enabled=fake_flags.enabled)

    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        kv=kv_mod, log=log_mod, relay=relay_mod, flags=flags_mod
    )
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    return fake_kv, fake_log, fake_relay, fake_flags
