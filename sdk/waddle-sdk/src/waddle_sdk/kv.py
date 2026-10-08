"""Bundle-scoped key/value over the WIT ``kv`` import (spec Sec6.5). Always granted.

Binding shapes confirmed via ``componentize-py bindings`` against the
committed ``wit/waddle-bundle/stage.wit``: ``get(key) -> bytes | None``,
``set(key, value: bytes, ttl_seconds: int) -> None``, ``delete(key) -> None``,
``increment(key, delta: int, ttl_seconds: int) -> int``, each raising the
generated ``Err`` (``.value`` holds the ``Error`` union: ``Error_TooLarge``,
``Error_Backend``) on failure.

**Guest-key charset (gh-631).** The real ``kv`` host capability
(``core/bundle_host_kv/src/scope.rs::is_allowed_key_byte``) rejects any
guest-supplied key containing a byte outside ASCII alnum + ``_``/``-``/``.``
-- most importantly ``:``, which the host reserves as its own server-side
namespace separator (``KvScope::app_prefix``/``data_key``) -- with a generic
``kv.error::backend`` *after* the call reaches Valkey infra, not a
guest-legible validation error. Every bundle's own in-memory test double
historically accepted any key, so a colon key (``count``'s original
``"count:registry"``, ``lurk``'s ``"lurk:state:{community}:{pseudonym}"``)
passed every test and then failed on the real host in production. This
module now validates the same charset *before* the host call, so a bundle
fails fast at the SDK boundary with a precise message instead of a generic
``kv.error::backend`` -- see :func:`validate_key`. ``_ALLOWED_KEY_CHARS`` /
``MAX_KEY_LEN`` are hand-mirrored from ``scope.rs`` (Python cannot import
Rust) and pinned against that source file by
``sdk/waddle-sdk/tests/test_kv.py::test_allowed_key_charset_matches_host_scope_rs``
so the two can't silently drift apart.
"""

from __future__ import annotations

#: Mirrors ``core/bundle_host_kv/src/scope.rs::is_allowed_key_byte`` exactly:
#: ASCII alnum plus ``_``/``-``/``.`` -- notably NOT ``:`` (the host's own
#: namespace separator) or any Valkey ``KEYS``/``SCAN`` glob metacharacter.
_ALLOWED_KEY_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-."
)

#: Mirrors ``core/bundle_host_kv/src/scope.rs::MAX_GUEST_KEY_LEN``.
MAX_KEY_LEN = 256


class InvalidKvKeyError(ValueError):
    """A guest-supplied `kv` key would be rejected by the real host capability.

    Raised by :func:`validate_key` before any host call reaches
    ``wit_world.imports.kv`` -- a bundle author sees exactly which key and
    why, instead of a generic ``kv.error::backend`` surfaced only after a
    real Valkey round trip in production (gh-631).
    """


def validate_key(key: str) -> None:
    """Raise :class:`InvalidKvKeyError` unless ``key`` is a valid guest `kv` key.

    Mirrors ``core/bundle_host_kv/src/scope.rs::validate_guest_key`` byte for
    byte: non-empty, at most :data:`MAX_KEY_LEN` bytes, every character in
    :data:`_ALLOWED_KEY_CHARS`. Call this at every guest-facing `kv` entry
    point (this module's four functions, and anything -- e.g.
    ``waddle_sdk.community_kv`` -- that builds a key before delegating here)
    so a bad key is a precise, catchable exception rather than a round trip
    to the real host.
    """
    if not key or len(key) > MAX_KEY_LEN:
        raise InvalidKvKeyError(
            f"kv key must be 1-{MAX_KEY_LEN} bytes, got {len(key)} byte(s): {key!r}"
        )
    bad_chars = sorted({c for c in key if c not in _ALLOWED_KEY_CHARS})
    if bad_chars:
        raise InvalidKvKeyError(
            f"kv key {key!r} contains characters outside [A-Za-z0-9_.-] "
            f"(host scope.rs charset): {bad_chars!r} -- note ':' is the "
            "host's own reserved namespace separator and can never appear "
            "in a guest key"
        )


async def get(key: str) -> bytes | None:
    """Return the stored value for ``key``, or ``None`` if unset."""
    validate_key(key)
    import wit_world

    result = wit_world.imports.kv.get(key)
    return bytes(result) if result is not None else None


async def set(key: str, value: bytes, ttl_seconds: int = 0) -> None:
    """Store ``value`` under ``key``. ``ttl_seconds=0`` means no expiry."""
    validate_key(key)
    import wit_world

    wit_world.imports.kv.set(key, value, ttl_seconds)


async def delete(key: str) -> None:
    """Delete the value stored under ``key``, if any."""
    validate_key(key)
    import wit_world

    wit_world.imports.kv.delete(key)


async def increment(key: str, delta: int, ttl_seconds: int = 0) -> int:
    """Atomically add ``delta`` to the value stored under ``key`` and return the result."""
    validate_key(key)
    import wit_world

    return int(wit_world.imports.kv.increment(key, delta, ttl_seconds))
