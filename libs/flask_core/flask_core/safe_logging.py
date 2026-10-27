"""Redaction-safe exception logging: type + code + fixed category, never the message.

SECURITY (PII / secret in logs). A raw exception's text is attacker- and
data-influenced: a DB driver error embeds the *bound values* of the failed
statement, an ``httpx`` error embeds the full request URL (query-string
secrets included), an SSRF-guard error embeds the offending URL, a crypto
error can echo key or ciphertext fragments. Logging ``f"...{e}"``,
``"...%s", exc``, ``str(e)``, ``extra={"error": str(e)}`` or ``exc_info=True``
therefore writes whatever the failing operation was handling into the log.

This module is the one place that turns an exception into something that is
safe to log. It reports only:

- the exception **type** (module-qualified class name, charset-validated),
- a **code** -- SQLSTATE/driver code, HTTP status or botocore error code -- accepted only
  when it matches a strict allowlist pattern (a hostile attribute value is
  dropped, not logged),
- a **category** looked up from a closed, hard-coded label set (never derived
  from the exception text),
- the **type** of the originating cause, when the exception was chained.

It never reads ``str(exc)``, ``exc.args`` or any message-bearing attribute.
Classification is by class name through the MRO, so no optional driver
(``httpx``, ``psycopg2``, ``botocore``, ``cryptography`` ...) is imported here. DB driver
errors are classified by ``flask_core.db_errors`` (the single owner of the SQLSTATE and
driver-code tables); use ``describe_db_error`` / ``log_db_error`` there when you also want
the constraint/table/column identifiers.

Typical use::

    from flask_core.safe_logging import describe_exc, log_exc_safe, url_host

    try:
        await client.post(url, data=form)
    except httpx.HTTPError as exc:
        log_exc_safe(logger, logging.WARNING, "token refresh failed", exc, host=url_host(url))

``event`` is typed ``LiteralString`` so ``mypy --strict`` rejects an f-string
that interpolates runtime data into it. Values passed as ``**fields`` are the
caller's responsibility: pass identifiers and counts, never user input.
"""

from __future__ import annotations

import logging
import os
import re
import traceback
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import LiteralString
from urllib.parse import urlsplit

from .db_errors import is_db_driver_error, summarize_db_error

__all__ = [
    "SafeExcInfo",
    "classify_exc",
    "describe_exc",
    "frames_only",
    "log_exc_safe",
    "url_host",
]

_MAX_CHAIN_DEPTH = 4
_MAX_FRAMES = 12
_MAX_FIELD_LEN = 128

_UNSAFE_TYPE_NAME = "<unsafe-type-name>"
_INVALID_URL = "<invalid-url>"

_PROVIDER_CODE_RE = re.compile(r"[A-Za-z0-9_.-]{1,32}")
_TYPE_NAME_RE = re.compile(r"[A-Za-z0-9_.]{1,200}")
_HOST_RE = re.compile(r"[a-z0-9][a-z0-9.-]{0,252}|[0-9a-f:.]{2,45}")
_NON_PRINTABLE_RE = re.compile(r"[^\x20-\x7e]")

#: Qualified class name -> fixed category label. Matched along the exception's MRO,
#: most-derived first, so ``psycopg2.errors.UniqueViolation`` resolves via its
#: ``psycopg2.Error`` base. Labels are constants -- never built from exception data.
_CATEGORY_BY_TYPE: Mapping[str, str] = MappingProxyType(
    {
        # Crypto / encoding
        "cryptography.exceptions.InvalidTag": "crypto_auth_failed",
        "cryptography.exceptions.InvalidSignature": "crypto_auth_failed",
        "cryptography.fernet.InvalidToken": "crypto_auth_failed",
        "cryptography.exceptions.InvalidKey": "crypto_invalid_key",
        "binascii.Error": "decode_error",
        "json.decoder.JSONDecodeError": "decode_error",
        "builtins.UnicodeError": "decode_error",
        # Network
        "httpx.TimeoutException": "network_timeout",
        "builtins.TimeoutError": "network_timeout",
        "httpx.ConnectError": "network_connect",
        "builtins.ConnectionError": "network_connect",
        "ssl.SSLError": "tls_error",
        "httpx.HTTPStatusError": "http_status",
        "httpx.DecodingError": "decode_error",
        "httpx.TransportError": "network_error",
        "httpx.HTTPError": "http_error",
        "requests.exceptions.RequestException": "http_error",
        # Object storage
        "botocore.exceptions.ClientError": "storage_client_error",
        "botocore.exceptions.BotoCoreError": "storage_error",
        # Generic builtins
        "builtins.KeyError": "key_error",
        "builtins.TypeError": "type_error",
        "builtins.ValueError": "value_error",
        "builtins.OSError": "os_error",
    }
)


@dataclass(slots=True, frozen=True)
class SafeExcInfo:
    """Loggable summary of an exception -- type, optional code, category, cause type.

    Built by :func:`classify_exc`. Holds only validated, allowlisted values, so
    ``str(info)`` is safe to put in any log line, metric label or span attribute.
    """

    exc_type: str
    code: str | None
    category: str
    cause_type: str | None = None

    def __str__(self) -> str:
        """Render as ``type=... [code=...] category=... [cause=...]``."""
        parts = [f"type={self.exc_type}"]
        if self.code is not None:
            parts.append(f"code={self.code}")
        parts.append(f"category={self.category}")
        if self.cause_type is not None:
            parts.append(f"cause={self.cause_type}")
        return " ".join(parts)


def _safe_attr(obj: object, name: str) -> object:
    """Read ``obj.<name>`` swallowing any error a hostile property might raise."""
    try:
        return getattr(obj, name, None)
    except Exception:  # noqa: BLE001 - a property that raises must never break logging
        return None


def _qualified_name(cls: type) -> str:
    """Return ``module.qualname`` for ``cls``, charset-validated (fail closed)."""
    try:
        name = f"{cls.__module__}.{cls.__qualname__}".replace("<locals>", "locals")
    except Exception:  # noqa: BLE001 - exotic metaclass; never break logging
        return _UNSAFE_TYPE_NAME
    return name if _TYPE_NAME_RE.fullmatch(name) else _UNSAFE_TYPE_NAME


def _chain(exc: BaseException) -> Iterator[BaseException]:
    """Yield ``exc`` then its wrapped/causing exceptions, bounded and cycle-safe."""
    seen: set[int] = set()
    current: object = exc
    while isinstance(current, BaseException) and id(current) not in seen:
        if len(seen) >= _MAX_CHAIN_DEPTH:
            return
        seen.add(id(current))
        yield current
        wrapped = _safe_attr(current, "orig")  # SQLAlchemy DBAPIError wraps the driver error
        if isinstance(wrapped, BaseException):
            current = wrapped
            continue
        cause = _safe_attr(current, "__cause__")
        if cause is None and not _safe_attr(current, "__suppress_context__"):
            cause = _safe_attr(current, "__context__")
        current = cause


def _extract_code(exc: BaseException) -> tuple[str, str] | None:
    """Return ``(code, kind)`` from allowlisted attributes of ``exc``, or None.

    ``kind`` is one of ``http``, ``provider`` or ``errno``. (SQLSTATE and other DB
    driver codes are handled by ``db_errors``, see :func:`_db_classification`.)

    Only HTTP status (``response.status_code``), botocore ``response["Error"]["Code"]``
    and ``OSError.errno`` are consulted. ``args`` and the message are never read.
    """
    response = _safe_attr(exc, "response")
    if response is not None:
        status = _safe_attr(response, "status_code")
        if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
            return str(status), "http"
        if isinstance(response, dict):
            error = response.get("Error")
            code = error.get("Code") if isinstance(error, dict) else None
            if isinstance(code, str) and _PROVIDER_CODE_RE.fullmatch(code):
                return code, "provider"

    err_no = _safe_attr(exc, "errno")
    if isinstance(err_no, int) and not isinstance(err_no, bool) and 0 < err_no < 10_000:
        return str(err_no), "errno"
    return None


def _category_for_type(exc: BaseException) -> str | None:
    """Return the first category matching ``exc``'s MRO, most-derived class first."""
    try:
        mro = type(exc).__mro__
    except Exception:  # noqa: BLE001 - exotic metaclass; never break logging
        return None
    for cls in mro:
        label = _CATEGORY_BY_TYPE.get(_qualified_name(cls))
        if label is not None:
            return label
    return None


def _db_classification(exc: BaseException) -> tuple[str | None, str] | None:
    """Return ``(code, category)`` if ``exc`` (or its chain) is a DB driver error, else None.

    Delegates to ``flask_core.db_errors`` -- the single owner of the SQLSTATE / driver
    code tables -- instead of keeping a second copy here. Only its allowlisted
    ``sqlstate`` / ``driver_code`` / ``category`` are used; constraint/table/column
    identifiers are left to ``describe_db_error`` for the DB layer itself.
    """
    try:
        if not is_db_driver_error(exc):
            return None
        summary = summarize_db_error(exc)
    except Exception:  # noqa: BLE001 - classification must never break the failure path
        return None
    return summary.sqlstate or summary.driver_code, summary.category or "db_error"


def classify_exc(exc: BaseException) -> SafeExcInfo:
    """Summarise ``exc`` as type + validated code + fixed category (+ cause type).

    Walks the ``orig``/``__cause__``/``__context__`` chain (bounded) so a wrapper
    such as ``TokenCryptoError`` raised ``from InvalidTag`` still reports a useful
    code and category. Never reads the exception message or ``args``.

    Args:
        exc: The exception to describe.

    Returns:
        A :class:`SafeExcInfo` whose every field is allowlisted or a constant.
    """
    chain = list(_chain(exc))

    code: str | None = None
    category: str | None = None
    kind: str | None = None

    db = _db_classification(exc)
    if db is not None:
        code, category = db
    else:
        for link in chain:
            found = _extract_code(link)
            if found is not None:
                code, kind = found
                break
        for link in chain:
            category = _category_for_type(link)
            if category is not None:
                break
        if category is None:
            category = "http_status" if kind == "http" else "unclassified"

    cause_type = _qualified_name(type(chain[-1])) if len(chain) > 1 else None
    return SafeExcInfo(
        exc_type=_qualified_name(type(exc)),
        code=code,
        category=category,
        cause_type=cause_type,
    )


def describe_exc(exc: BaseException) -> str:
    """Return ``type=... [code=...] category=... [cause=...]`` for ``exc`` -- log-safe.

    Use in place of ``str(exc)`` / ``f"{exc}"`` / ``"%s", exc`` anywhere the
    exception may carry credentials, bound values, URLs or other user data.

    Args:
        exc: The exception to describe.

    Returns:
        A single-line string built only from allowlisted values.
    """
    return str(classify_exc(exc))


def frames_only(exc: BaseException) -> str:
    """Return the traceback as ``file:line:func`` frames only -- no exception text.

    ``logger.exception`` / ``exc_info=`` render the exception message in the
    traceback footer, defeating redaction. This keeps the diagnostic value (where
    it failed) without that footer. Innermost frame first.

    Args:
        exc: The exception whose traceback to render.

    Returns:
        ``frame <- frame <- ...`` (at most 12 frames), or a fixed placeholder.
    """
    try:
        summary = traceback.StackSummary.extract(
            traceback.walk_tb(exc.__traceback__), lookup_lines=False
        )
        frames = [
            f"{os.path.basename(frame.filename)}:{frame.lineno}:{frame.name}"
            for frame in summary[-_MAX_FRAMES:]
        ]
    except Exception:  # noqa: BLE001 - never let diagnostics break the failure path
        return "<unavailable>"
    return " <- ".join(reversed(frames)) or "<no-frames>"


def _safe_field(value: object) -> str:
    """Render a caller-supplied field value on one printable-ASCII line, length-capped."""
    try:
        text = str(value)
    except Exception:  # noqa: BLE001 - a __str__ that raises must never break logging
        return "<unprintable>"
    return _NON_PRINTABLE_RE.sub("?", text)[:_MAX_FIELD_LEN]


def log_exc_safe(
    logger: logging.Logger,
    level: int,
    event: LiteralString,
    exc: BaseException,
    /,
    **fields: object,
) -> None:
    """Log ``event`` plus :func:`describe_exc` and caller fields -- never the message.

    Emits ``<event> type=... category=... name=value ...`` at ``level``. When DEBUG
    is enabled, a second record carries the frames-only traceback (no exception
    text). ``exc_info`` is deliberately never passed -- rendering it would put the
    exception message back into the log.

    Args:
        logger: Destination logger.
        level: ``logging`` level for the main record.
        event: Static description. ``LiteralString`` keeps runtime data out of it.
        exc: The exception being reported.
        **fields: Extra non-sensitive ``name=value`` identifiers (ids, hosts, counts).
    """
    if not logger.isEnabledFor(level):
        return
    parts = [event, describe_exc(exc)]
    parts.extend(f"{name}={_safe_field(value)}" for name, value in fields.items())
    logger.log(level, "%s", " ".join(parts), stacklevel=2)
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "%s traceback (frames only, no exception text): %s",
            event,
            frames_only(exc),
            stacklevel=2,
        )


def url_host(url: object) -> str:
    """Return only the lowercase hostname of ``url`` for logging (fail closed).

    Drops scheme, userinfo, port, path, query and fragment -- all places a secret
    can ride (``?token=``, ``user:pass@``, signed-URL signatures, path tokens). A
    value that is not a parseable URL with a plausible host yields a constant.

    Args:
        url: The URL (any object; non-strings yield the constant).

    Returns:
        The host, or ``"<invalid-url>"``.
    """
    if not isinstance(url, str):
        return _INVALID_URL
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return _INVALID_URL
    if host is None or not _HOST_RE.fullmatch(host):
        return _INVALID_URL
    return host
