"""Shared ``*-json`` object-shape guard for every WIT boundary field.

Every WIT ``*-json`` field (``db``/``log``/``relay``/``types`` -- see
``wit/waddle-bundle/stage.wit``) is documented to carry canonical JSON
*object* text, never a bare scalar or array. The Rust SDK enforces this at
the same boundary via ``to_canonical_json_object()`` -> ``SdkError::NonObjectJson``
(``sdk/waddle-sdk-rs/src/types.rs``); this module mirrors that guard for
Python so a bundle passing a list/scalar (bypassing the type hints Python
never enforces at runtime) fails loudly here instead of silently crossing
the boundary as non-object JSON.
"""

from __future__ import annotations

import json
from typing import Any


class NonObjectJsonError(TypeError):
    """A value bound for a WIT ``*-json`` field was not a JSON object (dict)."""


def to_canonical_json_object(value: Any) -> str:
    """Serialize ``value`` to JSON, requiring an object (``dict``) shape.

    Mirrors the Rust SDK's ``to_canonical_json_object()`` (spec D21 parity).
    Raises :class:`NonObjectJsonError` for anything that isn't a ``dict`` --
    a list or scalar would still be valid JSON, just not the object shape
    every ``*-json`` field is documented to require.
    """
    if not isinstance(value, dict):
        raise NonObjectJsonError(
            f"expected a JSON object (dict), got {type(value).__name__!r}"
        )
    return json.dumps(value)
