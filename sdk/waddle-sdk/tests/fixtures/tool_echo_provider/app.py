"""Minimal Python tool-provider fixture: registers one ``echo`` tool via ``@tool``.

Built against ``world stage-tools`` by ``tests/test_tool_provider_component.py`` to
prove ``waddle_sdk._component_entry.ToolProvider`` componentizes and routes
``tool-provider.call`` to the registry. Test-only; never shipped in a catalog.
"""

from __future__ import annotations

from typing import Any

from waddle_sdk.tool import tool


@tool("echo")
def echo(args: dict[str, Any], *, idempotency_key: str | None = None) -> dict[str, Any]:
    """Return the args unchanged plus the forwarded idempotency key."""
    return {"echo": args, "idempotency_key": idempotency_key}
