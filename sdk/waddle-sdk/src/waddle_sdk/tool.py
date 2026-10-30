"""Bundle-to-bundle tool invocation over the WIT ``tool`` import / ``tool-provider`` export.

``stage-tools``-only: ``wit/waddle-bundle/tool.wit`` (a bundle must be built
against ``world stage-tools`` and hold an approved grant for the target tool).
The stage is the sole authority -- it gates, resolves and runs the target under
the target's OWN grants; the caller never names community/tenant/actor.

**Wire contract (pinned).** Args and results are canonical JSON bytes carried in
the WIT ``list<u8>``. The executor host import already decodes the stage's
``{"ok": <json>}`` / ``{"tool_error": <info>}`` envelope into the WIT
``result<list<u8>, tool-error>``, so the client sees ``Ok(json bytes)`` or a
raised ``Err`` whose ``.value`` is a ``tool-error`` variant, never the envelope
(no double-unwrap: an ``ok`` payload may legitimately contain an ``"ok"`` key).

**Client:** :func:`invoke`. **Service:** :func:`tool` registers handlers;
:func:`dispatch` is what the bundle's ``tool-provider.call`` delegates to (it
raises :class:`ToolError`, which the export glue maps to the WIT ``Err``), and
:func:`dispatch_envelope` renders the same outcome as the ``{ok}``/``{tool_error}``
envelope. The stage forwards a caller's idempotency key to the target inside the
args JSON under the reserved ``__idempotency_key`` field; :func:`dispatch`
strips it and hands it to the handler as ``idempotency_key=`` so it can be
passed on to the economy/reputation host capability.

**Eager import (componentize-py)** -- see ``waddle_sdk.reputation``.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from typing import Any

from waddle_sdk import log

try:  # eager: forces componentize-py to wizen the binding -- see module docstring
    from wit_world.imports import tool as _tool_binding  # noqa: F401
except (ImportError, AttributeError):  # host-side tests / non-stage-tools world
    _tool_binding = None

#: Reserved args field the stage uses to forward the caller's idempotency key.
IDEMPOTENCY_FIELD = "__idempotency_key"

#: Snake_case codes, one per WIT ``tool-error`` case (CamelCase variant -> code).
_CODES: dict[str, str] = {
    "ToolError_NotGranted": "not_granted",
    "ToolError_Forbidden": "forbidden",
    "ToolError_Timeout": "timeout",
    "ToolError_DepthExceeded": "depth_exceeded",
    "ToolError_PoolExhausted": "pool_exhausted",
    "ToolError_TargetUnavailable": "target_unavailable",
    "ToolError_InvalidArgs": "invalid_args",
}
CODES: frozenset[str] = frozenset(_CODES.values())


class ToolError(Exception):
    """A tool call failed; ``code`` is one of :data:`CODES` (never a swallowed default)."""

    def __init__(self, code: str, message: str = "") -> None:
        """Store the snake_case ``code`` alongside an optional message."""
        super().__init__(f"tool error: {code}" + (f": {message}" if message else ""))
        self.code = code
        self.message = message


ToolHandler = Callable[..., Any | Awaitable[Any]]
_REGISTRY: dict[str, ToolHandler] = {}


def registry() -> dict[str, ToolHandler]:
    """Return the live ``name -> handler`` registry (mutable; for tests/introspection)."""
    return _REGISTRY


def _encode(value: Any) -> bytes:
    """Canonical JSON bytes (sorted keys, no whitespace); raise ``invalid_args`` if not JSON."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError) as exc:
        raise ToolError("invalid_args", "value is not JSON-serializable") from exc


def _classify(exc: Exception) -> ToolError:
    """Map a raised WIT ``Err`` (``.value`` = ``tool-error`` variant) to :class:`ToolError`."""
    detail = getattr(exc, "value", exc)
    type_name = type(detail).__name__
    code = _CODES.get(type_name)
    if code is None:
        return ToolError("target_unavailable", f"unrecognized tool error {type_name}")
    payload = getattr(detail, "value", None)
    return ToolError(code, str(payload) if payload else "")


async def invoke(target: str, tool: str, args: Any, *, idempotency_key: str | None = None) -> Any:
    """Call ``tool`` on bundle ``target`` with JSON-serializable ``args``; return the decoded value.

    Raises :class:`ToolError` (typed ``code``) on any host/target failure. Writes
    should pass an ``idempotency_key`` so a retry is safe.
    """
    payload = _encode(args)
    import wit_world

    try:
        raw = wit_world.imports.tool.invoke(target, tool, payload, idempotency_key)
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001 - classified structurally, see module docstring
        raise _classify(exc) from exc
    try:
        return json.loads(bytes(raw))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ToolError("target_unavailable", "target returned non-JSON result") from exc


def tool(name: str) -> Callable[[ToolHandler], ToolHandler]:
    """Register ``fn(args: dict, *, idempotency_key: str | None)`` (sync or async) as ``name``."""

    def deco(fn: ToolHandler) -> ToolHandler:
        if name in _REGISTRY:
            raise ValueError(f"tool {name!r} already registered")
        _REGISTRY[name] = fn
        return fn

    return deco


async def dispatch(name: str, args: bytes) -> bytes:
    """Run registered tool ``name`` on JSON ``args``; return JSON result bytes or raise ToolError.

    Unknown tool / malformed args -> ``invalid_args``; a handler's own
    :class:`ToolError` passes through; any other handler exception ->
    ``target_unavailable`` with no detail leaked to the caller.
    """
    handler = _REGISTRY.get(name)
    if handler is None:
        raise ToolError("invalid_args", f"unknown tool {name!r}")
    try:
        parsed = json.loads(bytes(args))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ToolError("invalid_args", "args are not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ToolError("invalid_args", "args must be a JSON object")
    key = parsed.pop(IDEMPOTENCY_FIELD, None)
    if key is not None and not isinstance(key, str):
        raise ToolError("invalid_args", f"{IDEMPOTENCY_FIELD} must be a string")
    try:
        result = handler(parsed, idempotency_key=key)
        if inspect.isawaitable(result):
            result = await result
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001 - never leak handler internals to the caller
        raise ToolError("target_unavailable") from exc
    return _encode(result)


async def dispatch_envelope(name: str, args: bytes) -> bytes:
    """Like :func:`dispatch` but render the outcome as the ``{ok}``/``{tool_error}`` envelope."""
    try:
        return _encode({"ok": json.loads(await dispatch(name, args))})
    except ToolError as err:
        # PII-free: tool name + typed code only, never args or message text.
        log.warn("tool call returned tool_error", tool=name, code=err.code)
        return _encode({"tool_error": {"code": err.code, "message": err.message}})
