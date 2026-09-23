"""Bundle-scoped key/value over the WIT ``kv`` import (spec Sec6.5). Always granted.

Binding shapes confirmed via ``componentize-py bindings`` against the
committed ``wit/waddle-bundle/stage.wit``: ``get(key) -> bytes | None``,
``set(key, value: bytes, ttl_seconds: int) -> None``, ``delete(key) -> None``,
``increment(key, delta: int, ttl_seconds: int) -> int``, each raising the
generated ``Err`` (``.value`` holds the ``Error`` union: ``Error_TooLarge``,
``Error_Backend``) on failure.
"""

from __future__ import annotations


async def get(key: str) -> bytes | None:
    """Return the stored value for ``key``, or ``None`` if unset."""
    import wit_world

    result = wit_world.imports.kv.get(key)
    return bytes(result) if result is not None else None


async def set(key: str, value: bytes, ttl_seconds: int = 0) -> None:
    """Store ``value`` under ``key``. ``ttl_seconds=0`` means no expiry."""
    import wit_world

    wit_world.imports.kv.set(key, value, ttl_seconds)


async def delete(key: str) -> None:
    """Delete the value stored under ``key``, if any."""
    import wit_world

    wit_world.imports.kv.delete(key)


async def increment(key: str, delta: int, ttl_seconds: int = 0) -> int:
    """Atomically add ``delta`` to the value stored under ``key`` and return the result."""
    import wit_world

    return int(wit_world.imports.kv.increment(key, delta, ttl_seconds))
