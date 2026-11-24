"""SMTP sender for the DSAR verification email (and its resend).

Implements the `DsarNotifier` seam (`services/dsar_notifier.py`) on top of `aiosmtplib`. The
transport is deliberately generic (`SmtpSettings` + `deliver`) so other mail types can reuse it
later; only the verification message is built here.

Configuration is env-only (never hardcoded). `DSAR_SMTP_HOST` unset means "no sender": the seam
stays fail-loud exactly as before (`UnconfiguredNotifier`). A host with a broken setting fails
loud at send time with the variable NAME (never a value).

| Variable | Default | Meaning |
|---|---|---|
| `DSAR_SMTP_HOST` | (unset) | relay host; unset = not configured |
| `DSAR_SMTP_PORT` | 587 (25 for plaintext dev) / 465 for `tls` | relay port |
| `DSAR_SMTP_TLS` | `starttls` | `starttls` (required, never opportunistic) / `tls` (implicit) / `none` |
| `DSAR_SMTP_ALLOW_PLAINTEXT` | false | must be `true` to use `DSAR_SMTP_TLS=none` (dev only) |
| `DSAR_SMTP_TLS_CA_FILE` | system CAs | extra trust anchor (private relay CA); verification stays ON |
| `DSAR_SMTP_USERNAME` / `DSAR_SMTP_PASSWORD` | unset | from a Secret; both or neither; never over plaintext |
| `DSAR_SMTP_FROM_ADDRESS` | required | envelope + header sender |
| `DSAR_SMTP_FROM_NAME` | `Privacy Requests` | display name (operator-set, never requester-influenced) |
| `DSAR_SMTP_REPLY_TO` | unset | optional Reply-To |
| `DSAR_SMTP_CONNECT_TIMEOUT` / `DSAR_SMTP_SEND_TIMEOUT` | 5 / 10 s | per-attempt connect / command timeouts |
| `DSAR_SMTP_MAX_RETRIES` | 2 | retries on transient failure (4xx, connection errors) |
| `DSAR_SMTP_BACKOFF_SECONDS` | 0.5 | base of the exponential backoff (full jitter, capped at 8 s) |
| `DSAR_SMTP_TOTAL_TIMEOUT` | 30 s | hard cap across all attempts |

Failure policy: 4xx / connect / timeout / disconnect are retried with exponential backoff and
jitter; 5xx, TLS/certificate failures, a missing STARTTLS and bad config are permanent (no retry).

PII: the requester address is never logged, traced, used as a metric label or put in an
exception message (aiosmtplib echoes server text that can contain it, so every failure is
re-raised sanitised, `from None`). Only a short keyed fingerprint is logged.
"""

# ruff: noqa: E501 - the module docstring holds the env-var table

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import secrets
import ssl
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

import aiosmtplib

from services import dsar_crypto as crypto
from services import dsar_telemetry as telemetry
from services.dsar_notifier import DsarNotifier, NotifierUnavailable
from services.dsar_service import verify_token_ttl_minutes

logger = logging.getLogger(__name__)

SUBJECT = "Confirm your privacy request"
DEFAULT_FROM_NAME = "Privacy Requests"
_TLS_MODES = ("starttls", "tls", "none")
_ADDR_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$"
)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_URL_BAD_RE = re.compile(r"[\s\x00-\x1f\x7f<>\"'\\]")
_ADDR_MAX = 254
_URL_MAX = 2000
_BACKOFF_CAP = 8.0
_RNG = secrets.SystemRandom()


class SmtpConfigError(NotifierUnavailable):
    """The SMTP environment is set but invalid (message names the variable, never a value)."""

    reason = "config"


class SmtpDeliveryError(NotifierUnavailable):
    """A sanitised delivery failure. `kind` is `transient` or `permanent`; carries no PII."""

    def __init__(self, kind: str, reason: str, code: int | None = None) -> None:
        """Build the PII-free message from the failure class, bounded reason and SMTP code."""
        suffix = f" (code={code})" if code else ""
        super().__init__(f"smtp {kind} failure: {reason}{suffix}")
        self.kind = kind
        self.reason = reason
        self.code = code


@dataclass(frozen=True)
class SmtpSettings:
    """Validated SMTP configuration (`password` is excluded from repr)."""

    host: str
    port: int
    tls: str
    from_address: str
    from_name: str = DEFAULT_FROM_NAME
    reply_to: str | None = None
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    ca_file: str | None = None
    connect_timeout: float = 5.0
    send_timeout: float = 10.0
    max_retries: int = 2
    backoff_base: float = 0.5
    total_timeout: float = 30.0


def _env_number(
    env: Mapping[str, str], name: str, default: float, low: float, high: float, *, integer: bool
) -> float:
    """Parse a bounded numeric env var or raise `SmtpConfigError` naming the variable."""
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw) if integer else float(raw)
    except ValueError:
        raise SmtpConfigError(f"{name} must be a number") from None
    if not low <= value <= high:
        raise SmtpConfigError(f"{name} must be between {low:g} and {high:g}")
    return value


def clean_address(value: object, label: str) -> str:
    """Return a strictly validated bare address (no CR/LF/whitespace/brackets) or raise."""
    if (
        not isinstance(value, str)
        or len(value) > _ADDR_MAX
        or _CONTROL_RE.search(value)
        or not _ADDR_RE.match(value)
        or ".." in value
    ):
        raise SmtpDeliveryError("permanent", f"invalid_{label}")
    return value


def clean_display_name(value: str) -> str:
    """Strip control characters (incl. CR/LF) and cap the length of a display name."""
    return _CONTROL_RE.sub(" ", value).strip()[:64] or DEFAULT_FROM_NAME


def settings_from_env(env: Mapping[str, str] | None = None) -> SmtpSettings | None:
    """Read the `DSAR_SMTP_*` environment; `None` when `DSAR_SMTP_HOST` is unset."""
    e = os.environ if env is None else env
    host = e.get("DSAR_SMTP_HOST", "").strip()
    if not host:
        return None
    if _CONTROL_RE.search(host) or " " in host:
        raise SmtpConfigError("DSAR_SMTP_HOST is invalid")
    tls = e.get("DSAR_SMTP_TLS", "starttls").strip().lower() or "starttls"
    if tls not in _TLS_MODES:
        raise SmtpConfigError("DSAR_SMTP_TLS must be one of starttls, tls, none")
    if tls == "none" and e.get("DSAR_SMTP_ALLOW_PLAINTEXT", "").strip().lower() not in (
        "1",
        "true",
        "yes",
    ):
        raise SmtpConfigError(
            "DSAR_SMTP_TLS=none requires the explicit dev flag DSAR_SMTP_ALLOW_PLAINTEXT=true"
        )
    default_port = {"starttls": 587, "tls": 465, "none": 25}[tls]
    port = int(_env_number(e, "DSAR_SMTP_PORT", default_port, 1, 65535, integer=True))
    username = e.get("DSAR_SMTP_USERNAME", "") or None
    password = e.get("DSAR_SMTP_PASSWORD", "") or None
    if (username is None) != (password is None):
        raise SmtpConfigError("DSAR_SMTP_USERNAME and DSAR_SMTP_PASSWORD must be set together")
    if username is not None and tls == "none":
        raise SmtpConfigError("SMTP credentials are never sent over plaintext (DSAR_SMTP_TLS=none)")
    from_raw = e.get("DSAR_SMTP_FROM_ADDRESS", "").strip()
    if not from_raw:
        raise SmtpConfigError("DSAR_SMTP_FROM_ADDRESS is required")
    reply_raw = e.get("DSAR_SMTP_REPLY_TO", "").strip()
    try:
        from_address = clean_address(from_raw, "from_address")
        reply_to = clean_address(reply_raw, "reply_to") if reply_raw else None
    except SmtpDeliveryError:
        raise SmtpConfigError("DSAR_SMTP_FROM_ADDRESS / DSAR_SMTP_REPLY_TO is invalid") from None
    ca_file = e.get("DSAR_SMTP_TLS_CA_FILE", "").strip() or None
    if ca_file is not None and not os.path.isfile(ca_file):
        raise SmtpConfigError("DSAR_SMTP_TLS_CA_FILE does not exist")
    return SmtpSettings(
        host=host,
        port=port,
        tls=tls,
        from_address=from_address,
        from_name=clean_display_name(e.get("DSAR_SMTP_FROM_NAME", "")),
        reply_to=reply_to,
        username=username,
        password=password,
        ca_file=ca_file,
        connect_timeout=_env_number(e, "DSAR_SMTP_CONNECT_TIMEOUT", 5, 0.5, 60, integer=False),
        send_timeout=_env_number(e, "DSAR_SMTP_SEND_TIMEOUT", 10, 0.5, 120, integer=False),
        max_retries=int(_env_number(e, "DSAR_SMTP_MAX_RETRIES", 2, 0, 5, integer=True)),
        backoff_base=_env_number(e, "DSAR_SMTP_BACKOFF_SECONDS", 0.5, 0, 30, integer=False),
        total_timeout=_env_number(e, "DSAR_SMTP_TOTAL_TIMEOUT", 30, 1, 300, integer=False),
    )


def build_verification_message(
    settings: SmtpSettings, *, to_email: str, verify_url: str, ttl_minutes: int
) -> EmailMessage:
    """Build the multipart (text + HTML) verification mail via the stdlib `email` API only.

    The subject, sender and body are fixed repo templates; the only requester-derived values are
    the validated recipient and the link. Header values are validated (no CR/LF) up front and the
    default `email.policy` additionally refuses any header containing a line break.
    """
    to = clean_address(to_email, "recipient")
    if (
        not isinstance(verify_url, str)
        or len(verify_url) > _URL_MAX
        or not verify_url.startswith(("https://", "http://"))
        or _URL_BAD_RE.search(verify_url)
    ):
        raise SmtpDeliveryError("permanent", "invalid_link")
    domain = settings.from_address.rsplit("@", 1)[1]
    msg = EmailMessage()
    msg["Subject"] = SUBJECT
    msg["From"] = formataddr((settings.from_name, settings.from_address))
    msg["To"] = to
    if settings.reply_to:
        msg["Reply-To"] = settings.reply_to
    msg["Date"] = formatdate(usegmt=True)
    msg["Message-ID"] = make_msgid(domain=domain)
    msg["Auto-Submitted"] = "auto-generated"
    msg["X-Auto-Response-Suppress"] = "All"
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


def _chain(exc: BaseException) -> list[BaseException]:
    """The exception plus its causes/contexts (bounded), for TLS detection."""
    seen: list[BaseException] = []
    cur: BaseException | None = exc
    while cur is not None and cur not in seen and len(seen) < 6:
        seen.append(cur)
        cur = cur.__cause__ or cur.__context__
    return seen


def classify(exc: BaseException) -> tuple[str, str, int | None]:
    """Map a send failure to `(kind, reason, code)`; `kind` is `transient` or `permanent`."""
    if any(isinstance(x, ssl.SSLError | ssl.CertificateError) for x in _chain(exc)):
        return "permanent", "tls", None
    # aiosmtplib raises a bare `SMTPException("SMTP STARTTLS extension not supported ...")` when a
    # relay does not offer STARTTLS; we require it (no plaintext downgrade), so it is permanent.
    if isinstance(exc, aiosmtplib.SMTPNotSupported) or (
        type(exc) is aiosmtplib.SMTPException and "STARTTLS" in str(exc)
    ):
        return "permanent", "tls_unsupported", None
    if isinstance(exc, aiosmtplib.SMTPRecipientsRefused):
        codes = [r.code for r in exc.recipients]
        code = codes[0] if codes else 550
        return (
            ("transient", "smtp_4xx", code)
            if 400 <= code < 500
            else ("permanent", "smtp_5xx", code)
        )
    if isinstance(exc, aiosmtplib.SMTPResponseException):
        code = int(exc.code)
        if isinstance(exc, aiosmtplib.SMTPAuthenticationError) and code >= 500:
            return "permanent", "auth", code
        if 400 <= code < 500:
            return "transient", "smtp_4xx", code
        return "permanent", "smtp_5xx", code
    if isinstance(exc, TimeoutError | aiosmtplib.SMTPTimeoutError):
        return "transient", "timeout", None
    if isinstance(exc, aiosmtplib.SMTPServerDisconnected):
        return "transient", "disconnected", None
    if isinstance(exc, aiosmtplib.SMTPConnectError | OSError):
        return "transient", "connect", None
    return "permanent", "other", None


def _fingerprint(email: str) -> str:
    """Short keyed fingerprint for logs (the same digest the rate limiter keys on)."""
    try:
        return crypto.email_fingerprint(email).hex()[:12]
    except Exception as exc:  # noqa: BLE001 - logging aid only; never block sending on it
        logger.warning("dsar.email.fingerprint_unavailable error_type=%s", type(exc).__name__)
        return "unavailable"


class SmtpDsarNotifier:
    """`DsarNotifier` that delivers the verification link over SMTP with bounded retries."""

    def __init__(self, settings: SmtpSettings) -> None:
        """Hold validated settings; `total_timeout` is read by the service's outer timeout."""
        self.settings = settings
        self.total_timeout = settings.total_timeout + 2.0

    def _context(self) -> ssl.SSLContext:
        """Verifying TLS context (hostname + chain checked), optionally plus a private CA."""
        return ssl.create_default_context(cafile=self.settings.ca_file)

    async def _send_once(self, msg: EmailMessage, to: str) -> None:
        """One connect -> (STARTTLS) -> auth -> send -> quit cycle."""
        s = self.settings
        client = aiosmtplib.SMTP(
            hostname=s.host,
            port=s.port,
            use_tls=s.tls == "tls",
            start_tls=True if s.tls == "starttls" else False,  # noqa: SIM210 - explicit, never opportunistic
            tls_context=self._context() if s.tls != "none" else None,
            username=s.username,
            password=s.password,
            timeout=s.send_timeout,
        )
        try:
            await client.connect(timeout=s.connect_timeout)
            await client.send_message(msg, sender=s.from_address, recipients=[to])
        finally:
            try:
                await client.quit()
            except Exception:  # noqa: BLE001 - best-effort close; the send outcome already decided
                client.close()

    async def _deliver(self, msg: EmailMessage, to: str, fp: str) -> None:
        """Retry transient failures with exponential backoff + full jitter; stop on permanent."""
        s = self.settings
        attempt = 0
        while True:
            attempt += 1
            logger.debug(
                "dsar.email.attempt", extra={"attempt": attempt, "recipient_fp": fp, "tls": s.tls}
            )
            try:
                await self._send_once(msg, to)
                return
            except Exception as exc:  # noqa: BLE001 - classified; re-raised sanitised below
                kind, reason, code = classify(exc)
                if kind == "permanent" or attempt > s.max_retries:
                    raise SmtpDeliveryError(kind, reason, code) from None
                delay = _RNG.uniform(0, min(_BACKOFF_CAP, s.backoff_base * 2 ** (attempt - 1)))
                telemetry.email_retries.add(1, {"reason": reason})
                logger.warning(
                    "dsar.email.retry",
                    extra={
                        "attempt": attempt,
                        "reason": reason,
                        "code": code,
                        "delay_s": round(delay, 3),
                        "recipient_fp": fp,
                    },
                )
                await asyncio.sleep(delay)

    async def send_verification(self, *, to_email: str, verify_url: str) -> None:
        """Send the verification link; raises a sanitised `NotifierUnavailable` on failure."""
        fp = _fingerprint(to_email)
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
    """Fail-loud stand-in when `DSAR_SMTP_HOST` is set but the rest of the config is invalid."""

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
