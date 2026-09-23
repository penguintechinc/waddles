"""Outbound relay push over the WIT ``relay`` import -- action-stage bundles only.

Binding shape confirmed via ``componentize-py bindings`` against the
committed ``wit/waddle-bundle/stage.wit``: ``push(provider: str, message_json:
str) -> None``, raising the generated ``Err`` (``.value`` holds the ``Error``
union: ``Error_Denied``, ``Error_Backend``) on failure.
"""

from __future__ import annotations

from typing import Any

from waddle_sdk._json_guard import to_canonical_json_object


async def push(provider: str, message: dict[str, Any]) -> None:
    """Push ``message`` (serialized to canonical JSON) onto ``provider``'s outbound relay queue.

    Raises :class:`~waddle_sdk._json_guard.NonObjectJsonError` if ``message``
    isn't a ``dict`` (spec D21 parity with the Rust SDK's
    ``to_canonical_json_object()`` guard).
    """
    import wit_world

    wit_world.imports.relay.push(provider, to_canonical_json_object(message))
