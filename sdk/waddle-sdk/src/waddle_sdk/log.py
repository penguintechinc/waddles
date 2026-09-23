"""Sanitized, levelled logging over the WIT ``log`` import.

Matches the call shape of ``logging.Logger.{debug,info,warning,error}``
closely enough that a bundle's existing ``logger.debug("msg", extra={...})``
calls need only their logger object swapped for this module.

Binding shape confirmed via ``componentize-py bindings`` against the
committed ``wit/waddle-bundle/stage.wit``: ``write(lvl: Level, message: str,
fields_json: str) -> None``, where ``Level`` is a generated ``enum.Enum``
(``ERROR = 0, WARN = 1, INFO = 2, DEBUG = 3``) -- an actual enum member is
required, not a plain string.
"""

from __future__ import annotations

from typing import Any

from waddle_sdk._json_guard import to_canonical_json_object


def _write(level_name: str, message: str, fields: dict[str, Any] | None) -> None:
    import wit_world

    log_mod = wit_world.imports.log
    level = log_mod.Level[level_name]
    log_mod.write(level, message, to_canonical_json_object(fields or {}))


def debug(message: str, **fields: Any) -> None:
    """Emit a DEBUG-level sanitized log line."""
    _write("DEBUG", message, fields)


def info(message: str, **fields: Any) -> None:
    """Emit an INFO-level sanitized log line."""
    _write("INFO", message, fields)


def warn(message: str, **fields: Any) -> None:
    """Emit a WARN-level sanitized log line."""
    _write("WARN", message, fields)


def error(message: str, **fields: Any) -> None:
    """Emit an ERROR-level sanitized log line."""
    _write("ERROR", message, fields)
