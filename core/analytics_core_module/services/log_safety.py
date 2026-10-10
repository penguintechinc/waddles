"""Redaction-safe failure logging for the analytics core module.

SECURITY (PII in logs): every analytics query binds community and user data
(``community_id``, ``hub_user_id``, usernames, message text). A DB driver
exception's message embeds those *bound values* (psycopg2 ``DETAIL: Key
(...)=(...)``, invalid-input echoes, inlined SQL), so interpolating the
exception (``f"...{e}"``, ``str(e)``) writes them into the log stream and, via
``error_response(str(e))``, back to the caller.

`log_failure` is the one place this module turns an exception into log text.
It delegates to ``flask_core.db_errors`` (allowlist-based, fail-closed): only
the exception type, SQLSTATE / driver code, a fixed category label and
schema identifiers are emitted -- never the exception message, args, SQL
text or bound parameters.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from flask_core.db_errors import describe_db_error, format_sanitized_traceback, log_db_error

#: Body returned to API callers for an unhandled failure (never exception text).
INTERNAL_ERROR_MESSAGE = "Internal server error"


class StructuredLogger(Protocol):
    """The slice of ``flask_core.logging_config.AAALogger`` that `log_failure` uses."""

    def error(self, message: str, **kwargs: Any) -> None:
        """Log at ERROR with structured keyword fields."""

    def warning(self, message: str, **kwargs: Any) -> None:
        """Log at WARNING with structured keyword fields."""

    def debug(self, message: str, **kwargs: Any) -> None:
        """Log at DEBUG with structured keyword fields."""


def log_failure(
    logger: StructuredLogger | logging.Logger,
    message: str,
    exc: BaseException,
    *,
    level: int = logging.ERROR,
    **fields: Any,
) -> None:
    """Log ``message`` (ERROR by default) with a value-free description of ``exc``.

    Args:
        logger: The module's AAALogger (keyword fields supported) or a stdlib logger.
        message: Static human-readable description -- never interpolate request data.
        exc: The caught exception; its message and args are never read.
        level: ``logging.ERROR`` (default) or ``logging.WARNING`` for degraded-but-serving.
        **fields: Safe structured context only (e.g. ``community_id``, ``endpoint``).
            Never pass bound query values, request parameters, usernames or message text.
    """
    if isinstance(logger, logging.Logger):
        suffix = "".join(f" {key}={value}" for key, value in fields.items())
        log_db_error(logger, f"{message}{suffix}", exc, level=level)
        return
    emit = logger.warning if level == logging.WARNING else logger.error
    emit(message, error=describe_db_error(exc), **fields)
    logger.debug(
        f"{message}: sanitized traceback",
        traceback=format_sanitized_traceback(exc),
        **fields,
    )
