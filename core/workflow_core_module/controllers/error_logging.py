"""PII-safe logging for the blueprints' ``@errorhandler(500)`` handlers.

A 500 handler runs for *any* unhandled exception, which very commonly is a DB
driver error whose message embeds the BOUND VALUES of the failing statement
(message content, usernames, platform ids). The old handlers logged
``str(error)`` with ``exc_info=True`` -- the exception text and the traceback's
exception line both reached the log stream. This module logs only a correlation
``error_id`` plus the exception TYPE / SQLSTATE (via ``flask_core.db_errors``),
and hands the same ``error_id`` back so support can match a client report to a
log line.
"""

from __future__ import annotations

import logging
import uuid

from flask_core import describe_db_error


def log_internal_error(logger: logging.Logger, error: BaseException | None) -> str:
    """Log an unhandled 500 without any exception text and return its ``error_id``.

    Emits one ERROR line carrying ``error_id`` and a value-free description of the
    underlying exception (Quart/Werkzeug wrap the original in ``original_exception``).
    The exception message, ``args`` and traceback text are deliberately never logged.
    """
    error_id = uuid.uuid4().hex
    cause = getattr(error, "original_exception", None) or error
    description = describe_db_error(cause) if isinstance(cause, BaseException) else "type=unknown"
    logger.error("Internal server error error_id=%s %s", error_id, description)
    return error_id
