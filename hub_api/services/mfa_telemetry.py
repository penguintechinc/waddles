"""OpenTelemetry metrics + traces for account MFA (logs go through each module's own logger).

Metric labels are bounded enums only -- method (`totp` / `email` / `recovery`), operation and
outcome / reason codes -- never a user id, tenant slug, email address or code. Spans carry the
same bounded attributes; exception text is never attached (type name only).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final

from opentelemetry import metrics, trace
from opentelemetry.trace import Span, Status, StatusCode

_NAME: Final = "waddles.hub_api.mfa"
_meter = metrics.get_meter(_NAME)
_tracer = trace.get_tracer(_NAME)

verify_counter = _meter.create_counter(
    "waddles_mfa_verifications_total",
    description="MFA code checks by method (totp/email/recovery), purpose and outcome/reason.",
)
enrollment_counter = _meter.create_counter(
    "waddles_mfa_enrollment_changes_total",
    description="MFA method lifecycle changes (enroll/confirm/disable/regenerate) by method.",
)
email_send_counter = _meter.create_counter(
    "waddles_mfa_email_sends_total",
    description="Email one-time code sends by outcome (sent/rate_limited/unavailable/error).",
)
challenge_counter = _meter.create_counter(
    "waddles_mfa_challenges_total",
    description="MFA-pending challenges issued after primary authentication, by auth method.",
)
verify_duration = _meter.create_histogram(
    "waddles_mfa_verify_duration_seconds",
    unit="s",
    description="Wall time of one MFA code verification (incl. secret decrypt), by method.",
)


@contextmanager
def mfa_span(name: str, **attributes: str | int | bool) -> Iterator[Span]:
    """Start a span; on an exception mark it ERROR with the exception TYPE only, then re-raise."""
    with _tracer.start_as_current_span(
        name,
        attributes=dict(attributes),
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except BaseException as exc:
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            raise
