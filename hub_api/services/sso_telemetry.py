"""OpenTelemetry metrics/traces + PII-safe failure logging for the SSO subsystem.

critical-rules.md Observability (OTel): logs, metrics AND traces are all
mandatory. Metric labels are restricted to bounded, non-PII enums (protocol,
outcome, reason code, operation) -- never a tenant slug, connection id, email
or subject. Spans carry the connection's opaque `public_id` only.

`log_sso_error` is the single place an SSO exception becomes a log record. It
renders (a) the exception's TYPE, (b) for our own `SsoError`s the
application-authored `code`/`message`, and (c) a frame-only traceback (file,
line, function, source line -- no exception text) in the rendered message
itself, not hidden in `extra` where a plain formatter would drop it. The raw
`str(exc)` of a third-party parser/crypto/HTTP exception is deliberately never
read: those messages routinely embed the offending XML, token or URL and so
can carry PII or secrets (`flask_core.exc_log_audit` documents the same rule
for DB driver errors).
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final

from opentelemetry import metrics, trace
from opentelemetry.trace import Span, Status, StatusCode

from services.sso_types import SsoError

_METER_NAME: Final = "waddles.hub_api.sso"
_meter = metrics.get_meter(_METER_NAME)
_tracer = trace.get_tracer(_METER_NAME)

login_counter = _meter.create_counter(
    "waddles_sso_login_total",
    description="Completed SSO login attempts by protocol, outcome and (for failures) reason.",
)
start_counter = _meter.create_counter(
    "waddles_sso_login_start_total",
    description="SSO login flows started (authorize/AuthnRequest redirect issued), by protocol.",
)
login_duration = _meter.create_histogram(
    "waddles_sso_login_duration_seconds",
    unit="s",
    description="Wall time to validate an IdP response and mint a session, by protocol.",
)
idp_request_duration = _meter.create_histogram(
    "waddles_sso_idp_request_duration_seconds",
    unit="s",
    description="Outbound IdP HTTP call latency (discovery, jwks, token), by protocol/operation.",
)
connection_change_counter = _meter.create_counter(
    "waddles_sso_connection_changes_total",
    description="Tenant-admin SSO connection CRUD operations, by protocol and operation.",
)
saml_validation_counter = _meter.create_counter(
    "waddles_sso_saml_validation_failures_total",
    description="SAML Response validation failures by fixed failure code.",
)


@contextmanager
def sso_span(name: str, **attributes: str | int | bool) -> Iterator[Span]:
    """Start a span named `name`; mark it ERROR (type only, no text) if the block raises.

    `attributes` must be bounded/non-PII -- the caller passes the connection's
    opaque `public_id`, the protocol, and similar. Exception text is never
    recorded on the span: only the exception type name.
    """
    with _tracer.start_as_current_span(name, attributes=dict(attributes)) as span:
        try:
            yield span
        except BaseException as exc:
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            raise


def frame_traceback(exc: BaseException) -> str:
    """Render `exc`'s traceback frames (and cause chain) with no exception text.

    Gives the on-call engineer the call path of a failure while guaranteeing
    nothing from the exception message -- which may embed a token, XML
    fragment or URL -- reaches the log stream.
    """
    lines: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(seen) < 8:
        seen.add(id(current))
        lines.append(f"{type(current).__module__}.{type(current).__qualname__}")
        for frame in traceback.extract_tb(current.__traceback__):
            lines.append(f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}')
            if frame.line:
                lines.append(f"    {frame.line}")
        current = current.__cause__ or current.__context__
    return "\n".join(lines)


def log_sso_error(
    logger: logging.Logger,
    event: str,
    exc: BaseException,
    *,
    connection: str | None = None,
    level: int = logging.ERROR,
) -> None:
    """Log an SSO failure as `event` with type, safe code/message and a frame-only traceback.

    `connection` is the connection's opaque `public_id`. The exception text of
    anything that is not one of our own `SsoError`s is never emitted.
    """
    if isinstance(exc, SsoError):
        code, message = exc.code, exc.message
    else:
        code, message = "unexpected", "unexpected error (exception text withheld)"
    rendered = frame_traceback(exc)
    logger.log(
        level,
        "%s connection=%s err_type=%s err_code=%s err_msg=%s\n%s",
        event,
        connection or "-",
        type(exc).__name__,
        code,
        message,
        rendered,
        stacklevel=2,
    )
