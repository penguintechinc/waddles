"""Not implemented -- ``penguin_dal.pagination``'s ``Page``/``Cursor`` names.

Spec Sec4.12 lists ``penguin_dal.pagination`` in the module inventory this SDK
reproduces, but no cited first-party bundle call site uses ``Page``/``Cursor``
pagination (D21's "not implemented, by scope" list). The names exist here so
``from waddle_sdk.pagination import Page, Cursor`` resolves -- matching the
same import line a migrated bundle would otherwise use against real
``penguin_dal`` -- but constructing either raises ``NotImplementedError``
naming the construct (D21: never a silent mis-execution) rather than being
silently absent.
"""

from __future__ import annotations

from typing import Any


class Page:
    """Not implemented -- see module docstring."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Always raise -- see class docstring."""
        raise NotImplementedError(
            "Page pagination is not implemented in the waddle-sdk facade -- no first-party "
            "bundle call site uses it (spec D21)"
        )


class Cursor:
    """Not implemented -- see module docstring."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Always raise -- see class docstring."""
        raise NotImplementedError(
            "Cursor pagination is not implemented in the waddle-sdk facade -- no first-party "
            "bundle call site uses it (spec D21)"
        )
