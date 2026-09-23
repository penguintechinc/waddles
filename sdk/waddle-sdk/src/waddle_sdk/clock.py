"""Wall/monotonic clock over the WIT ``clock`` import.

No other time source is available to a bundle (spec Sec7.4: keeps timing side
channels from being trivially precise). Binding shapes confirmed via
``componentize-py bindings`` against the committed ``wit/waddle-bundle/
stage.wit``: ``now_millis() -> int``, ``now_rfc3339() -> str``,
``monotonic_nanos() -> int``.
"""

from __future__ import annotations


def now_millis() -> int:
    """Return milliseconds since the Unix epoch, as the stage sees it."""
    import wit_world

    return int(wit_world.imports.clock.now_millis())


def now_rfc3339() -> str:
    """Return the current time as an RFC 3339 UTC string, millisecond precision."""
    import wit_world

    return str(wit_world.imports.clock.now_rfc3339())


def monotonic_nanos() -> int:
    """Return a monotonic nanosecond counter, for in-bundle duration measurement only."""
    import wit_world

    return int(wit_world.imports.clock.monotonic_nanos())
