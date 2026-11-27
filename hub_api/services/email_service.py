"""hub-api's single outbound email service (SMTP over aiosmtplib).

Every hub-api mail type goes through here: account email verification, the DSAR verification
link (`services/dsar_email_sender.py`, a thin adapter) and, later, email one-time codes. Callers
use the small public interface:

    await email_service.send_email(to_email=..., subject=..., text_body=..., kind="...")

which raises an `EmailUnavailableError` subclass on ANY failure -- `EmailNotConfiguredError` (no
SMTP host), `SmtpConfigError` (bad env) or `SmtpDeliveryError` (relay refused / unreachable) --
and never reports a silent success. `is_configured()` lets a flow fail fast before creating state.

Configuration is env-only (`SMTP_*`; the DSAR adapter also accepts its legacy `DSAR_SMTP_*`
prefix). The password comes from a Secret through env and is never logged or repr'd.

| Variable | Default | Meaning |
|---|---|---|
| `SMTP_HOST` | (unset) | relay host; unset = email not configured (flows needing it fail loud) |
| `SMTP_PORT` | 587 starttls / 465 tls / 25 none | relay port |
| `SMTP_TLS` | `starttls` | `starttls` (required: fails loud if not offered) / `tls` (implicit) / `none` |
| `SMTP_ALLOW_PLAINTEXT` | false | must be `true` to use `SMTP_TLS=none` (dev only) |
| `SMTP_TLS_CA_FILE` | system CAs | extra trust anchor; verification stays ON |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | unset | optional AUTH (both or neither; IP-allowlisted relays need none) |
| `SMTP_FROM` | required | envelope + header sender (`SMTP_FROM_ADDRESS` accepted as an alias) |
| `SMTP_FROM_NAME` | `Waddles` | display name |
| `SMTP_REPLY_TO` | unset | optional Reply-To |
| `SMTP_EHLO_NAME` | required | EHLO hostname; must be an FQDN (never localhost/IP/pod name) |
| `SMTP_CONNECT_TIMEOUT` / `SMTP_SEND_TIMEOUT` | 5 / 10 s | per-attempt timeouts |
| `SMTP_MAX_RETRIES` | 2 | retries on transient failure (4xx, connection errors) |
| `SMTP_BACKOFF_SECONDS` | 0.5 | base of the exponential backoff (full jitter, capped at 8 s) |
| `SMTP_TOTAL_TIMEOUT` | 30 s | hard cap across all attempts |

TLS: certificate + hostname verification always on, minimum TLS 1.2. STARTTLS is mandatory in
`starttls` mode -- a relay that does not offer it is a permanent failure (no plaintext downgrade).

PII: recipient addresses never reach logs, spans, metric labels or exception text (aiosmtplib
echoes server text that can contain them, so failures are re-raised sanitised, `from None`).
Logs carry a short keyed fingerprint only.
"""

# ruff: noqa: E501 - the module docstring holds the env-var table

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import secrets
import ssl
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Any

import aiosmtplib
from opentelemetry import metrics, trace
from opentelemetry.trace import Status, StatusCode

logger = logging.getLogger(__name__)

DEFAULT_PREFIX = "SMTP_"
DEFAULT_FROM_NAME = "Waddles"
_TLS_MODES = ("starttls", "tls", "none")
_ADDR_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$"
)
_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_ADDR_MAX = 254
_SUBJECT_MAX = 200
_BODY_MAX = 64_000
_BACKOFF_CAP = 8.0
_RNG = secrets.SystemRandom()

_meter = metrics.get_meter("hub_api.email")
_tracer = trace.get_tracer("hub_api.email")
email_sends = _meter.create_counter(
    "waddles_email_total", description="Outbound emails by kind and outcome (no addresses)"
)
email_retries = _meter.create_counter(
    "waddles_email_retries_total", description="Transient SMTP retries by kind and reason"
)
email_duration = _meter.create_histogram(
    "waddles_email_send_duration_seconds", unit="s", description="End-to-end send latency"
)


class EmailUnavailableError(RuntimeError):
    """Base class: the email could not be sent. Messages never contain addresses or secrets."""

    reason = "unavailable"


class EmailNotConfiguredError(EmailUnavailableError):
    """No SMTP host is configured: any flow that needs email must fail loud on this."""

    reason = "not_configured"


class SmtpConfigError(EmailUnavailableError):
    """The SMTP environment is set but invalid (message names the variable, never a value)."""

    reason = "config"


class SmtpDeliveryError(EmailUnavailableError):
    """A sanitised delivery failure. `kind` is `transient` or `permanent`; carries no PII."""

    def __init__(self, kind: str, reason: str, code: int | None = None) -> None:
        """Build the PII-free message from the failure class, bounded reason and SMTP code."""
        suffix = f" (code={code})" if code else ""
        super().__init__(f"smtp {kind} failure: {reason}{suffix}")
        self.kind = kind
        self.reason = reason
        self.code = code


@dataclass(frozen=True, slots=True)
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
    ehlo_name: str = "waddles.invalid"
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


def clean_display_name(value: str, default: str = DEFAULT_FROM_NAME) -> str:
    """Strip control characters (incl. CR/LF) and cap the length of a display name."""
    return _CONTROL_RE.sub(" ", value).strip()[:64] or default


def is_fqdn(name: str) -> bool:
    """True for a dotted DNS name with an alphabetic TLD -- never localhost, an IP or empty.

    The EHLO name is checked by IP-allowlisted relays, so there is deliberately no fallback to
    `localhost` or the pod hostname: an operator must configure a real FQDN.
    """
    if not name or len(name) > 253 or not _HOST_RE.match(name) or "." not in name:
        return False
    try:
        ipaddress.ip_address(name)
        return False
    except ValueError:
        pass
    labels = name.rstrip(".").lower().split(".")
    if labels[-1] in ("localhost", "local", "localdomain") or "localhost" in labels:
        return False
    if not labels[-1].isalpha() or len(labels[-1]) < 2:
        return False
    return all(
        0 < len(label) <= 63 and not label.startswith("-") and not label.endswith("-")
        for label in labels
    )


def settings_from_env(
    env: Mapping[str, str] | None = None,
    *,
    prefix: str = DEFAULT_PREFIX,
    default_from_name: str = DEFAULT_FROM_NAME,
) -> SmtpSettings | None:
    """Read `<prefix>*` SMTP settings; `None` when `<prefix>HOST` is unset (not configured)."""
    e = os.environ if env is None else env
    p = prefix
    host = e.get(f"{p}HOST", "").strip()
    if not host:
        return None
    if _CONTROL_RE.search(host) or " " in host:
        raise SmtpConfigError(f"{p}HOST is invalid")
    tls = e.get(f"{p}TLS", "starttls").strip().lower() or "starttls"
    if tls not in _TLS_MODES:
        raise SmtpConfigError(f"{p}TLS must be one of starttls, tls, none")
    if tls == "none" and e.get(f"{p}ALLOW_PLAINTEXT", "").strip().lower() not in (
        "1",
        "true",
        "yes",
    ):
        raise SmtpConfigError(f"{p}TLS=none requires the explicit dev flag {p}ALLOW_PLAINTEXT=true")
    default_port = {"starttls": 587, "tls": 465, "none": 25}[tls]
    port = int(_env_number(e, f"{p}PORT", default_port, 1, 65535, integer=True))
    username = e.get(f"{p}USERNAME", "") or None
    password = e.get(f"{p}PASSWORD", "") or None
    if (username is None) != (password is None):
        raise SmtpConfigError(f"{p}USERNAME and {p}PASSWORD must be set together")
    if username is not None and tls == "none":
        raise SmtpConfigError(f"SMTP credentials are never sent over plaintext ({p}TLS=none)")
    from_raw = (e.get(f"{p}FROM", "") or e.get(f"{p}FROM_ADDRESS", "")).strip()
    if not from_raw:
        raise SmtpConfigError(f"{p}FROM_ADDRESS is required")
    reply_raw = e.get(f"{p}REPLY_TO", "").strip()
    try:
        from_address = clean_address(from_raw, "from_address")
        reply_to = clean_address(reply_raw, "reply_to") if reply_raw else None
    except SmtpDeliveryError:
        raise SmtpConfigError(f"{p}FROM_ADDRESS / {p}REPLY_TO is invalid") from None
    ca_file = e.get(f"{p}TLS_CA_FILE", "").strip() or None
    if ca_file is not None and not os.path.isfile(ca_file):
        raise SmtpConfigError(f"{p}TLS_CA_FILE does not exist")
    ehlo = e.get(f"{p}EHLO_NAME", "").strip()
    if not is_fqdn(ehlo):
        raise SmtpConfigError(
            f"{p}EHLO_NAME must be set to a fully-qualified hostname "
            "(not empty, not localhost, not an IP address)"
        )
    return SmtpSettings(
        host=host,
        port=port,
        tls=tls,
        from_address=from_address,
        from_name=clean_display_name(e.get(f"{p}FROM_NAME", ""), default_from_name),
        reply_to=reply_to,
        username=username,
        password=password,
        ca_file=ca_file,
        ehlo_name=ehlo,
        connect_timeout=_env_number(e, f"{p}CONNECT_TIMEOUT", 5, 0.5, 60, integer=False),
        send_timeout=_env_number(e, f"{p}SEND_TIMEOUT", 10, 0.5, 120, integer=False),
        max_retries=int(_env_number(e, f"{p}MAX_RETRIES", 2, 0, 5, integer=True)),
        backoff_base=_env_number(e, f"{p}BACKOFF_SECONDS", 0.5, 0, 30, integer=False),
        total_timeout=_env_number(e, f"{p}TOTAL_TIMEOUT", 30, 1, 300, integer=False),
    )


def base_message(settings: SmtpSettings, *, to_email: str, subject: str) -> EmailMessage:
    """Headers every mail shares: validated From/To/Subject, Date, Message-ID, auto-submitted."""
    to = clean_address(to_email, "recipient")
    if not isinstance(subject, str) or not subject or len(subject) > _SUBJECT_MAX:
        raise SmtpDeliveryError("permanent", "invalid_subject")
    domain = settings.from_address.rsplit("@", 1)[1]
    msg = EmailMessage()
    msg["Subject"] = _CONTROL_RE.sub(" ", subject)
    msg["From"] = formataddr((settings.from_name, settings.from_address))
    msg["To"] = to
    if settings.reply_to:
        msg["Reply-To"] = settings.reply_to
    msg["Date"] = formatdate(usegmt=True)
    msg["Message-ID"] = make_msgid(domain=domain)
    msg["Auto-Submitted"] = "auto-generated"
    msg["X-Auto-Response-Suppress"] = "All"
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


def fingerprint(email: str) -> str:
    """Short keyed fingerprint for logs -- never the address itself."""
    from services import dsar_crypto  # lazy: keyed by the hub-api fingerprint key

    try:
        return dsar_crypto.email_fingerprint(email).hex()[:12]
    except Exception as exc:  # noqa: BLE001 - logging aid only; never block sending on it
        logger.warning("email.fingerprint_unavailable error_type=%s", type(exc).__name__)
        return "unavailable"


class SmtpTransport:
    """Connect -> STARTTLS/TLS -> optional AUTH -> send, with bounded retries and backoff."""

    def __init__(self, settings: SmtpSettings, *, log_prefix: str = "email") -> None:
        """Hold validated settings; `log_prefix` names the attempt/retry log events."""
        self.settings = settings
        self.log_prefix = log_prefix

    def _context(self) -> ssl.SSLContext:
        """Verifying TLS context (hostname + chain checked, TLS 1.2+), plus an optional CA."""
        ctx = ssl.create_default_context(cafile=self.settings.ca_file)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        return ctx

    async def _send_once(self, msg: EmailMessage, to: str) -> None:
        """One connect -> (STARTTLS) -> (auth) -> send -> quit cycle."""
        s = self.settings
        client = aiosmtplib.SMTP(
            hostname=s.host,
            port=s.port,
            use_tls=s.tls == "tls",
            start_tls=True if s.tls == "starttls" else False,  # noqa: SIM210 - explicit, never opportunistic
            tls_context=self._context() if s.tls != "none" else None,
            username=s.username,
            password=s.password,
            local_hostname=s.ehlo_name,
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

    async def deliver(
        self,
        msg: EmailMessage,
        to: str,
        *,
        fp: str,
        on_retry: Callable[[str], None] | None = None,
    ) -> None:
        """Retry transient failures with exponential backoff + full jitter; stop on permanent."""
        s = self.settings
        attempt = 0
        while True:
            attempt += 1
            logger.debug(
                f"{self.log_prefix}.attempt", extra={"attempt": attempt, "recipient_fp": fp}
            )
            try:
                await self._send_once(msg, to)
                return
            except Exception as exc:  # noqa: BLE001 - classified; re-raised sanitised below
                kind, reason, code = classify(exc)
                if kind == "permanent" or attempt > s.max_retries:
                    raise SmtpDeliveryError(kind, reason, code) from None
                delay = _RNG.uniform(0, min(_BACKOFF_CAP, s.backoff_base * 2 ** (attempt - 1)))
                if on_retry is not None:
                    on_retry(reason)
                logger.warning(
                    f"{self.log_prefix}.retry",
                    extra={
                        "attempt": attempt,
                        "reason": reason,
                        "code": code,
                        "delay_s": round(delay, 3),
                        "recipient_fp": fp,
                    },
                )
                await asyncio.sleep(delay)


@contextmanager
def _span(name: str, **attributes: str) -> Iterator[Any]:
    """A span that records errors; attributes are bounded labels only (never addresses)."""
    with _tracer.start_as_current_span(name, attributes=attributes) as span:
        try:
            yield span
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            raise


def is_configured(env: Mapping[str, str] | None = None) -> bool:
    """True when `SMTP_HOST` is set (a broken rest-of-config still fails loud at send time)."""
    e = os.environ if env is None else env
    return bool(e.get(f"{DEFAULT_PREFIX}HOST", "").strip())


def validate_startup(env: Mapping[str, str] | None = None) -> None:
    """Fail loud at hub-api startup when SMTP is configured but invalid (e.g. a bad EHLO name).

    Checks the shared `SMTP_*` set and the legacy DSAR `DSAR_SMTP_*` set; an unset host is fine
    (flows that need email then fail loud per request with `EmailNotConfiguredError`).
    """
    e = os.environ if env is None else env
    settings_from_env(e)
    if e.get("DSAR_SMTP_HOST", "").strip():
        settings_from_env(e, prefix="DSAR_SMTP_")


def _load_settings() -> SmtpSettings:
    """Current env settings, or the fail-loud error for unset / invalid config."""
    try:
        settings = settings_from_env()
    except SmtpConfigError as exc:
        logger.error("email.smtp_misconfigured error=%s", exc)  # value-free message
        raise
    if settings is None:
        raise EmailNotConfiguredError("outbound email is not configured (SMTP_HOST unset)")
    return settings


async def send_email(
    *,
    to_email: str,
    subject: str,
    text_body: str,
    kind: str,
    html_body: str | None = None,
) -> None:
    """Send one email through the configured SMTP relay, or raise `EmailUnavailableError`.

    `kind` is a short bounded label (`^[a-z][a-z0-9_]{0,31}$`, e.g. `email_verification`,
    `mfa_email_otp`) used for metrics/spans/logs. Bodies are caller-built from fixed templates;
    the only caller-derived header is the validated recipient. Never logs the address or body.
    """
    if not _KIND_RE.match(kind):
        raise ValueError("kind must match ^[a-z][a-z0-9_]{0,31}$")
    if not text_body or len(text_body) > _BODY_MAX or (html_body and len(html_body) > _BODY_MAX):
        raise SmtpDeliveryError("permanent", "invalid_body")
    started = time.monotonic()
    outcome = "sent"
    fp = fingerprint(to_email) if isinstance(to_email, str) else "invalid"
    with _span("email.send", **{"email.kind": kind}) as span:
        try:
            settings = _load_settings()
            span.set_attribute("email.tls", settings.tls)
            msg = base_message(settings, to_email=to_email, subject=subject)
            msg.set_content(text_body)
            if html_body:
                msg.add_alternative(html_body, subtype="html")
            transport = SmtpTransport(settings)
            await asyncio.wait_for(
                transport.deliver(
                    msg,
                    to_email,
                    fp=fp,
                    on_retry=lambda reason: email_retries.add(1, {"kind": kind, "reason": reason}),
                ),
                settings.total_timeout,
            )
        except EmailNotConfiguredError:
            outcome = "failed_not_configured"
            raise
        except SmtpConfigError:
            outcome = "failed_config"
            raise
        except SmtpDeliveryError as exc:
            outcome = f"failed_{exc.kind}"
            span.set_attribute("email.reason", exc.reason)
            raise
        except TimeoutError:
            outcome = "failed_transient"
            span.set_attribute("email.reason", "total_timeout")
            raise SmtpDeliveryError("transient", "total_timeout") from None
        finally:
            email_sends.add(1, {"kind": kind, "outcome": outcome})
            email_duration.record(time.monotonic() - started, {"kind": kind, "outcome": outcome})
            log = logger.info if outcome == "sent" else logger.error
            log("email.send.done kind=%s outcome=%s recipient_fp=%s", kind, outcome, fp)
