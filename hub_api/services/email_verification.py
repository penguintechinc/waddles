"""Account email verification: tokens, signup-domain policy and the verification mail.

Policy (core security mechanism, opt-out kill switch `waddles.disable-email-verification`;
unseen/OFF = enforced):

* Local email/password signups are ALWAYS pending until the address is verified (no session).
* When the platform's signup allowlist (`hub_settings.signup_allowed_domains`, exact domain
  match) is non-empty, every NEW OAuth signup must also verify an address in an allowed domain
  before it gets a session -- proving control of the address, not just the provider's claim.

Tokens: 32 random bytes (urlsafe), stored ONLY as a SHA-256 hex digest in
`hub_users.email_verification_token`, single use (cleared on success), TTL 24 h
(`EMAIL_VERIFICATION_TTL_HOURS`), resend rate-limited by a per-user cooldown
(`EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS`, default 60 s) on top of the auth-tier IP limiter.
The digest lookup is followed by a constant-time compare.

Email: sent through `services/email_service.py`. Unset SMTP fails loud BEFORE any state is
created (`email_not_configured`, 503); a delivery failure after the user row exists surfaces as
`verification_email_failed` (503, "couldn't send, retry") -- never a silent success, and nothing
is ever auto-verified. Addresses never reach logs (a keyed fingerprint only).
"""

from __future__ import annotations

import hashlib
import hmac
import html
import logging
import os
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from flask_core.auth import DEFAULT_TENANT_SLUG
from flask_core.feature_flags import feature_enabled
from opentelemetry import metrics

from config import HubAPIConfig
from services import email_service
from services.errors import ApiError

logger = logging.getLogger(__name__)

FLAG_DISABLE_EMAIL_VERIFICATION = "waddles.disable-email-verification"
EMAIL_KIND = "email_verification"
SUBJECT = "Verify your Waddles email address"

ERR_NOT_CONFIGURED = "email_not_configured"
ERR_SEND_FAILED = "verification_email_failed"
ERR_REQUIRED = "email_verification_required"
ERR_DOMAIN = "email_domain_not_allowed"
ERR_RATE_LIMITED = "verification_resend_rate_limited"

_meter = metrics.get_meter("hub_api.email_verification")
verification_events = _meter.create_counter(
    "waddles_email_verification_total",
    description="Email verification lifecycle events by event and outcome (no addresses)",
)


def _int_env(name: str, default: int, low: int, high: int) -> int:
    """Bounded int env var; an out-of-range or non-numeric value falls back loudly."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.error("email_verification.bad_env name=%s (using default)", name)
        return default
    return min(max(value, low), high)


def token_ttl() -> timedelta:
    """Verification link lifetime (1-72 h, default 24 h)."""
    return timedelta(hours=_int_env("EMAIL_VERIFICATION_TTL_HOURS", 24, 1, 72))


def resend_cooldown() -> timedelta:
    """Minimum gap between two verification mails for one user (10-3600 s, default 60 s)."""
    return timedelta(seconds=_int_env("EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS", 60, 10, 3600))


def hash_token(raw: str) -> str:
    """SHA-256 hex digest -- the only form a token is ever stored in."""
    return hashlib.sha256(raw.encode()).hexdigest()


def new_token() -> tuple[str, str]:
    """`(raw, digest)`: the raw token goes in the mail only, the digest goes in the DB."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_token(raw)


def token_matches(raw: str, stored_digest: str | None) -> bool:
    """Constant-time compare of a presented token against the stored digest."""
    if not raw or not stored_digest:
        return False
    return hmac.compare_digest(hash_token(raw), stored_digest)


async def enforced(tenant_slug: str = DEFAULT_TENANT_SLUG) -> bool:
    """True unless the `waddles.disable-email-verification` kill switch is ON."""
    return not await feature_enabled(FLAG_DISABLE_EMAIL_VERIFICATION, tenant=tenant_slug)


def allowed_domains(settings: Mapping[str, str]) -> list[str]:
    """The signup allowlist from `hub_settings.signup_allowed_domains` (lower-cased, exact)."""
    return [
        d.strip().lower().lstrip("@")
        for d in settings.get("signup_allowed_domains", "").split(",")
        if d.strip()
    ]


def email_domain(email: str) -> str:
    """Lower-cased domain part of an address ('' when there is none)."""
    return email.rsplit("@", 1)[-1].lower() if "@" in email else ""


def domain_allowed(email: str, domains: list[str]) -> bool:
    """Exact-domain match against the allowlist; an empty allowlist allows everything."""
    return not domains or email_domain(email) in domains


def verify_url(cfg: HubAPIConfig, raw_token: str) -> str:
    """Webui link; the token rides in the fragment so it never reaches a server log."""
    return f"{cfg.frontend_origin.rstrip('/')}/verify-email#token={raw_token}"


def require_configured() -> None:
    """Fail loud before creating state when a flow needs email and SMTP is unset."""
    if not email_service.is_configured():
        verification_events.add(1, {"event": "precheck", "outcome": "not_configured"})
        logger.error("email_verification.smtp_not_configured")
        raise ApiError(
            "Email delivery is not configured on this server, so sign-ups that need email "
            "verification are unavailable. Please contact the administrator.",
            503,
            ERR_NOT_CONFIGURED,
        )


async def send_verification_email(cfg: HubAPIConfig, *, to_email: str, raw_token: str) -> None:
    """Send the verification link; raise `verification_email_failed` (503) on any failure."""
    url = verify_url(cfg, raw_token)
    hours = int(token_ttl().total_seconds() // 3600)
    text = (
        "Welcome to Waddles! Confirm this email address by opening the link below:\n\n"
        f"{url}\n\n"
        f"The link can be used once and expires in {hours} hours.\n\n"
        "If you did not create an account, ignore this email.\n"
    )
    safe = html.escape(url, quote=True)
    page = (
        '<!doctype html><html><body style="font-family:sans-serif">'
        "<p>Welcome to Waddles! Confirm this email address:</p>"
        f'<p><a href="{safe}">Verify my email</a></p>'
        f"<p>The link can be used once and expires in {hours} hours.</p>"
        "<p>If you did not create an account, ignore this email.</p></body></html>"
    )
    try:
        await email_service.send_email(
            to_email=to_email, subject=SUBJECT, text_body=text, html_body=page, kind=EMAIL_KIND
        )
    except email_service.EmailUnavailableError as exc:
        verification_events.add(1, {"event": "send", "outcome": exc.reason})
        code = ERR_NOT_CONFIGURED if isinstance(exc, email_service.EmailNotConfiguredError) else ""
        raise ApiError(
            "We couldn't send the verification email. Please try again in a moment.",
            503,
            code or ERR_SEND_FAILED,
            {"requiresVerification": True},
        ) from None
    verification_events.add(1, {"event": "send", "outcome": "sent"})


async def issue_and_send(
    async_dal: Any, dal: Any, cfg: HubAPIConfig, *, user_id: int, email: str
) -> None:
    """Rotate the user's token (digest stored, old one invalidated), then mail the raw token."""
    raw, digest = new_token()
    await async_dal.update_async(
        dal.hub_users.id == user_id,
        email_verification_token=digest,
        email_verification_expires=datetime.now(UTC) + token_ttl(),
        updated_at=datetime.now(UTC),
    )
    await send_verification_email(cfg, to_email=email, raw_token=raw)


def resend_wait_seconds(expires: datetime | None) -> int:
    """Seconds left on the resend cooldown, derived from the current token's issue time."""
    if expires is None:
        return 0
    expires_utc = expires if expires.tzinfo else expires.replace(tzinfo=UTC)
    issued = expires_utc - token_ttl()
    remaining = (issued + resend_cooldown()) - datetime.now(UTC)
    return max(0, int(remaining.total_seconds() + 0.999))
