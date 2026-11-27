"""DSAR verification email (and its resend) -- a thin adapter over `services/email_service.py`.

Implements the `DsarNotifier` seam (`services/dsar_notifier.py`) on hub-api's one SMTP transport;
only the verification message is built here. Configuration: the legacy `DSAR_SMTP_*` variables
when `DSAR_SMTP_HOST` is set, else the shared `SMTP_*` variables (see `email_service` for the
table). Neither host set means "no sender": the seam stays fail-loud (`UnconfiguredNotifier`).

Errors are re-raised as the DSAR-specific `SmtpConfigError` / `SmtpDeliveryError`, which are both
`email_service` errors AND `NotifierUnavailable`, so the outbox's failure handling is unchanged.
PII: the requester address is never logged, traced, used as a metric label or put in an
exception message; only a short keyed fingerprint is logged.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from email.message import EmailMessage

from services import dsar_telemetry as telemetry
from services import email_service
from services.dsar_notifier import DsarNotifier, NotifierUnavailable
from services.dsar_service import verify_token_ttl_minutes
from services.email_service import classify, clean_display_name

logger = logging.getLogger(__name__)

__all__ = [
    "SUBJECT",
    "MisconfiguredNotifier",
    "SmtpConfigError",
    "SmtpDeliveryError",
    "SmtpDsarNotifier",
    "SmtpSettings",
    "build_verification_message",
    "classify",
    "clean_address",
    "clean_display_name",
    "notifier_from_env",
    "settings_from_env",
]

SUBJECT = "Confirm your privacy request"
DEFAULT_FROM_NAME = "Privacy Requests"
_URL_BAD_RE = re.compile(r"[\s\x00-\x1f\x7f<>\"'\\]")
_URL_MAX = 2000


@dataclass(frozen=True, slots=True)
class SmtpSettings(email_service.SmtpSettings):
    """`email_service.SmtpSettings` with the DSAR display-name default."""

    from_name: str = DEFAULT_FROM_NAME


class SmtpConfigError(email_service.SmtpConfigError, NotifierUnavailable):
    """The SMTP environment is set but invalid (message names the variable, never a value)."""

    reason = "config"


class SmtpDeliveryError(email_service.SmtpDeliveryError, NotifierUnavailable):
    """A sanitised delivery failure. `kind` is `transient` or `permanent`; carries no PII."""


def clean_address(value: object, label: str) -> str:
    """`email_service.clean_address`, raising the DSAR `SmtpDeliveryError`."""
    try:
        return email_service.clean_address(value, label)
    except email_service.SmtpDeliveryError as exc:
        raise SmtpDeliveryError(exc.kind, exc.reason, exc.code) from None


def settings_from_env(
    env: Mapping[str, str] | None = None,
) -> email_service.SmtpSettings | None:
    """`DSAR_SMTP_*` when `DSAR_SMTP_HOST` is set, else the shared `SMTP_*`; `None` when neither."""
    e = os.environ if env is None else env
    prefix = "DSAR_SMTP_" if e.get("DSAR_SMTP_HOST", "").strip() else email_service.DEFAULT_PREFIX
    try:
        return email_service.settings_from_env(
            e, prefix=prefix, default_from_name=DEFAULT_FROM_NAME
        )
    except email_service.SmtpConfigError as exc:
        raise SmtpConfigError(str(exc)) from None


def _base(settings: email_service.SmtpSettings, to_email: str) -> EmailMessage:
    """Shared headers via `email_service.base_message`, with DSAR error types."""
    try:
        return email_service.base_message(settings, to_email=to_email, subject=SUBJECT)
    except email_service.SmtpDeliveryError as exc:
        raise SmtpDeliveryError(exc.kind, exc.reason, exc.code) from None


def build_verification_message(
    settings: email_service.SmtpSettings, *, to_email: str, verify_url: str, ttl_minutes: int
) -> EmailMessage:
    """Build the multipart (text + HTML) verification mail via the stdlib `email` API only.

    The subject, sender and body are fixed repo templates; the only requester-derived values are
    the validated recipient and the link. Header values are validated (no CR/LF) up front and the
    default `email.policy` additionally refuses any header containing a line break.
    """
    if (
        not isinstance(verify_url, str)
        or len(verify_url) > _URL_MAX
        or not verify_url.startswith(("https://", "http://"))
        or _URL_BAD_RE.search(verify_url)
    ):
        raise SmtpDeliveryError("permanent", "invalid_link")
    msg = _base(settings, to_email)
    text = (
        "Someone (hopefully you) asked to submit a privacy request using this email address.\n"
        "To confirm it, open the link below:\n\n"
        f"{verify_url}\n\n"
        f"The link can be used once and expires in {ttl_minutes} minutes.\n\n"
        "If you did not make this request, ignore this email. Nothing will happen.\n"
    )
    safe_url = html.escape(verify_url, quote=True)
    page = (
        '<!doctype html><html><body style="font-family:sans-serif">'
        "<p>Someone (hopefully you) asked to submit a privacy request using this email "
        "address.</p>"
        f'<p>To confirm it, <a href="{safe_url}">open this link</a>.</p>'
        f"<p>The link can be used once and expires in {ttl_minutes} minutes.</p>"
        "<p>If you did not make this request, ignore this email. Nothing will happen.</p>"
        "</body></html>"
    )
    msg.set_content(text)
    msg.add_alternative(page, subtype="html")
    return msg


class SmtpDsarNotifier:
    """`DsarNotifier` that delivers the verification link over the shared SMTP transport."""

    def __init__(self, settings: email_service.SmtpSettings) -> None:
        """Hold validated settings; `total_timeout` is read by the service's outer timeout."""
        self.settings = settings
        self.total_timeout = settings.total_timeout + 2.0
        self._transport = email_service.SmtpTransport(settings, log_prefix="dsar.email")

    async def _deliver(self, msg: EmailMessage, to: str, fp: str) -> None:
        """Shared transport with DSAR retry telemetry; errors mapped to the DSAR types."""
        try:
            await self._transport.deliver(
                msg,
                to,
                fp=fp,
                on_retry=lambda reason: telemetry.email_retries.add(1, {"reason": reason}),
            )
        except email_service.SmtpDeliveryError as exc:
            raise SmtpDeliveryError(exc.kind, exc.reason, exc.code) from None

    async def send_verification(self, *, to_email: str, verify_url: str) -> None:
        """Send the verification link; raises a sanitised `NotifierUnavailable` on failure."""
        fp = email_service.fingerprint(to_email)
        started = time.monotonic()
        outcome = "sent"
        with telemetry.span("dsar.email.send", tls=self.settings.tls) as span:
            try:
                msg = build_verification_message(
                    self.settings,
                    to_email=to_email,
                    verify_url=verify_url,
                    ttl_minutes=verify_token_ttl_minutes(),
                )
                await asyncio.wait_for(
                    self._deliver(msg, to_email, fp), self.settings.total_timeout
                )
            except SmtpDeliveryError as exc:
                outcome = f"failed_{exc.kind}"
                span.set_attribute("dsar.email.reason", exc.reason)
                raise
            except TimeoutError:
                outcome = "failed_transient"
                span.set_attribute("dsar.email.reason", "total_timeout")
                raise SmtpDeliveryError("transient", "total_timeout") from None
            finally:
                telemetry.email_sends.add(1, {"outcome": outcome})
                telemetry.email_duration.record(time.monotonic() - started, {"outcome": outcome})
                logger.debug(
                    "dsar.email.done",
                    extra={"outcome": outcome, "recipient_fp": fp},
                )


class MisconfiguredNotifier:
    """Fail-loud stand-in when the SMTP host is set but the rest of the config is invalid."""

    def __init__(self, error: SmtpConfigError) -> None:
        """Remember the (value-free) config error to raise on every send."""
        self.error = error

    async def send_verification(self, *, to_email: str, verify_url: str) -> None:
        """Always raises the config error -- delivery is never faked."""
        telemetry.email_sends.add(1, {"outcome": "failed_config"})
        raise SmtpConfigError(str(self.error))


def notifier_from_env(env: Mapping[str, str] | None = None) -> DsarNotifier | None:
    """The env-configured notifier; `None` when SMTP is not configured at all."""
    try:
        settings = settings_from_env(env)
    except SmtpConfigError as exc:
        logger.error("dsar.email.smtp_misconfigured error=%s", exc)  # value-free message
        return MisconfiguredNotifier(exc)
    return None if settings is None else SmtpDsarNotifier(settings)
