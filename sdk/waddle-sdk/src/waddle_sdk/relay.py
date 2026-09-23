"""Outbound relay push over the WIT ``relay`` import -- action-stage bundles only.

Binding shape confirmed via ``componentize-py bindings`` against the
committed ``wit/waddle-bundle/stage.wit``: ``push(provider: str, message_json:
str) -> None``, raising the generated ``Err`` (``.value`` holds the ``Error``
union: ``Error_Denied``, ``Error_Backend``) on failure.
"""

from __future__ import annotations

import json
from typing import Any


async def push(provider: str, message: dict[str, Any]) -> None:
    """Push ``message`` (serialized to canonical JSON) onto ``provider``'s outbound relay queue."""
    import wit_world

    wit_world.imports.relay.push(provider, json.dumps(message))
