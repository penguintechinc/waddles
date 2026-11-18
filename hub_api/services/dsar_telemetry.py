"""OTel instruments for the DSAR inbox (logs are emitted by the callers via `logging`).

Metrics only ever carry bounded enum labels (source/type/outcome/provider names). NEVER a tenant
slug, email, request id or any other identifier: no PII in any attribute. A dead exporter never
breaks a request -- the OTel API no-ops without a configured SDK.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.metrics import CallbackOptions, Observation

_SCOPE = "waddles.hub_api.dsar"
_meter = metrics.get_meter(_SCOPE)
_tracer = trace.get_tracer(_SCOPE)

requests_created = _meter.create_counter(
    "waddles_dsar_requests_created_total", description="DSAR requests created, by source and type."
)
verification_outcomes = _meter.create_counter(
    "waddles_dsar_verification_total",
    description="Public-form email verification outcomes (verified/invalid/sent/...).",
)
captcha_failures = _meter.create_counter(
    "waddles_dsar_captcha_failures_total",
    description="Captcha rejections/outages, by provider+reason.",
)
captcha_duration = _meter.create_histogram(
    "waddles_dsar_captcha_verify_duration_seconds",
    unit="s",
    description="Outbound captcha verification latency.",
)
actions = _meter.create_counter(
    "waddles_dsar_actions_total", description="Inbox `act` executions, by action and outcome."
)
rate_limited = _meter.create_counter(
    "waddles_dsar_rate_limited_total",
    description="Public/user intake rejections by rate limit scope.",
)
expired = _meter.create_counter(
    "waddles_dsar_unverified_expired_total", description="Unverified public requests auto-expired."
)
contacts_purged = _meter.create_counter(
    "waddles_dsar_contacts_purged_total", description="Requests whose contact PII was blanked."
)

email_sends = _meter.create_counter(
    "waddles_dsar_email_total",
    description="DSAR verification email sends by outcome (sent/failed_*).",
)
email_retries = _meter.create_counter(
    "waddles_dsar_email_retries_total",
    description="SMTP send retries by reason (timeout/connect/disconnected/smtp_4xx/other).",
)
email_duration = _meter.create_histogram(
    "waddles_dsar_email_send_duration_seconds",
    unit="s",
    description="End-to-end verification email send latency (all attempts), by outcome.",
)

_overdue_value = 0


def set_overdue(count: int) -> None:
    """Record the latest overdue-open-request count (read by the observable gauge)."""
    global _overdue_value  # noqa: PLW0603 - module-level gauge state, single writer
    _overdue_value = max(0, int(count))


def _observe_overdue(_options: CallbackOptions) -> Iterator[Observation]:
    yield Observation(_overdue_value)


_meter.create_observable_gauge(
    "waddles_dsar_overdue_requests",
    callbacks=[_observe_overdue],
    description="Open DSAR requests past their effective due date (all tenants).",
)


@contextmanager
def span(name: str, **attributes: str | int | bool) -> Iterator[Any]:
    """Start a span; attributes must be PII-free scalars (enum names/ints/bools only)."""
    with _tracer.start_as_current_span(name) as current:
        for key, value in attributes.items():
            current.set_attribute(key, value)
        yield current
