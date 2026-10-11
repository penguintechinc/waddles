"""Log-safety helpers shared by the music providers (httpx + stdlib only).

SECURITY (PII / secret in logs). The music providers put secrets and user text in
request URLs -- SoundCloud sends ``oauth_token=`` and ``q=`` (the user's search
text) in the query string, Spotify sends ``q=`` and device ids -- and ``httpx``
embeds the full URL in ``str(HTTPStatusError)`` *and* in its own INFO request
line. A provider's free-text error body (``error_description``, ``message``) can
echo request parameters too. None of that may reach a log line or a raised error.

This module is the one place that turns a provider failure into something safe:
the exception **type**, the HTTP **status** and the provider's machine-readable
**reason** enum, accepted only when it is a plain identifier. It mirrors the
``flask_core.safe_logging`` contract (type + code + fixed vocabulary, never the
message) without that package's heavy dependencies -- the providers are
httpx-only by design (see ``core/unified_music_module/requirements.txt``).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Final

import httpx

__all__ = [
    "StripHttpxQueryFilter",
    "describe_error",
    "error_reason",
    "install_httpx_log_guards",
    "install_httpx_query_redaction",
    "quiet_httpx_request_logs",
    "safe_reason",
]

logger = logging.getLogger(__name__)

#: A provider "reason" is accepted only when it is a plain identifier (``invalid_grant``,
#: ``NO_ACTIVE_DEVICE``). Anything else -- spaces, hyphens, URLs -- is dropped.
_REASON_RE: Final = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
_NON_PRINTABLE_RE: Final = re.compile(r"[^\x20-\x7e]")
_MAX_DETAIL_LEN: Final = 200


class StripHttpxQueryFilter(logging.Filter):
    """Remove the query string from httpx's ``HTTP Request: GET <url>`` INFO lines.

    The music APIs take secrets and user text (``oauth_token=``, ``q=``, ``key=``) in the
    query string, and httpx logs the full URL for every request at INFO. Query strings
    are user data / secrets, so none may reach the log stream; scheme, host and path stay
    so the request line remains useful.
    """

    @staticmethod
    def _strip(value: Any) -> Any:
        """Return `value` without its query string if it is a URL, else unchanged."""
        if isinstance(value, httpx.URL):
            return value.copy_with(query=None)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value.split("?", 1)[0]
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        """Rewrite the record's URL argument(s) in place; never drops the record."""
        if isinstance(record.args, tuple):
            record.args = tuple(self._strip(arg) for arg in record.args)
        return True


def install_httpx_query_redaction() -> None:
    """Attach `StripHttpxQueryFilter` to the ``httpx`` logger exactly once (idempotent)."""
    httpx_logger = logging.getLogger("httpx")
    if not any(isinstance(f, StripHttpxQueryFilter) for f in httpx_logger.filters):
        httpx_logger.addFilter(StripHttpxQueryFilter())


def quiet_httpx_request_logs() -> None:
    """Pin the ``httpx`` logger to WARNING so its INFO request lines are never emitted.

    httpx logs ``HTTP Request: GET <full url> "HTTP/1.1 200 OK"`` for every call. The query
    carries secrets and user text, and the *path* can too (``/tracks/<id>``,
    ``/users/<id>/likes`` take caller-supplied ids), so for providers that handle user
    input the only safe INFO posture is silence. Only ever raises the level -- a deployment
    that deliberately set ERROR/CRITICAL is left alone.
    """
    httpx_logger = logging.getLogger("httpx")
    if httpx_logger.level < logging.WARNING:  # NOTSET (0) counts as "inherit" -> pin it
        httpx_logger.setLevel(logging.WARNING)


def install_httpx_log_guards() -> None:
    """Apply both httpx log guards: WARNING pin, plus the query-stripping filter.

    The filter is defence in depth -- if an operator re-enables INFO on ``httpx`` the request
    line still loses its query string (``oauth_token=``, ``q=``). Idempotent.
    """
    quiet_httpx_request_logs()
    install_httpx_query_redaction()


def safe_reason(value: object) -> str | None:
    """Return `value` if it is a plain-identifier provider reason, else None."""
    if isinstance(value, str) and _REASON_RE.fullmatch(value):
        return value
    return None


def error_reason(response: httpx.Response) -> str | None:
    """Return the provider's machine-readable error reason, if one is present and safe.

    Reads only an enum-shaped field -- OAuth2's ``{"error": "invalid_grant"}`` or a nested
    ``{"error": {"reason": "NO_ACTIVE_DEVICE"}}`` -- never the free-text ``message`` /
    ``error_description``, which may echo request parameters (search text, tokens).
    """
    try:
        body = response.json()
    except ValueError as exc:
        # non-JSON body (proxy / HTML error page) -- expected, nothing to extract
        logger.debug("Provider error body is not JSON: %s", type(exc).__name__)
        return None
    error = body.get("error") if isinstance(body, dict) else None
    candidate = error.get("reason") if isinstance(error, dict) else error
    reason = safe_reason(candidate)
    if reason is None and candidate is not None:
        logger.debug("Provider error reason is not a plain identifier; dropped")
    return reason


def describe_error(exc: BaseException, *, own_errors: tuple[type[BaseException], ...] = ()) -> str:
    """Describe `exc` for logs and raised errors without its message.

    Reports the type, and for an HTTP status failure the status code and the provider's
    reason enum. ``str(httpx.HTTPStatusError)`` embeds the full request URL (search text,
    ``oauth_token=``) and a transport error's message can embed the host/URL, so neither
    is ever included. Exceptions in `own_errors` are the provider's own wrapper types,
    whose messages are static or built by this function; their text is carried through
    (printable-ASCII, length-capped) so a nested failure keeps its status/reason.

    Args:
        exc: The exception to describe.
        own_errors: Provider exception classes whose message is trusted (built here).

    Returns:
        A single-line string such as ``HTTPStatusError status=400 reason=invalid_grant``.
    """
    parts = [type(exc).__name__]
    if own_errors and isinstance(exc, own_errors):
        detail = _NON_PRINTABLE_RE.sub("?", str(exc))[:_MAX_DETAIL_LEN]
        parts.append(f"detail={detail}")
    if isinstance(exc, httpx.HTTPStatusError):
        parts.append(f"status={exc.response.status_code}")
        reason = error_reason(exc.response)
        if reason:
            parts.append(f"reason={reason}")
    return " ".join(parts)
