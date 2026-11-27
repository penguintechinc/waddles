"""Account email verification: tokens, signup-domain policy and the verification mail.

Policy (core security mechanism, opt-out kill switch `waddles.disable-email-verification`;
unseen/OFF = enforced):

* Local email/password signups are ALWAYS pending until the address is verified (no session).
* Signup-domain restriction: the EFFECTIVE allowlist for a tenant (`resolve_signup_policy`, the
  one resolver every signup path uses) is the platform list (`hub_settings.
  signup_allowed_domains`) intersected with the tenant's own list (`tenant_settings` key
  `signup_allowed_domains`, edited via `PUT /api/v1/tenant/<slug>/signup-domains`); whichever is
  empty imposes nothing. When the effective policy is restricted, EVERY signup into that tenant
  -- local, OAuth and tenant SSO alike -- must verify an address in an allowed domain (exact
  match) before it gets a session, and an existing user whose email is still unverified is
  held at their next login the same way. Unrestricted tenants: OAuth/SSO need no email.

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
import re
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
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

#: `tenant_settings` key holding a tenant's comma-separated signup allowlist. Reserved: written
#: only through the dedicated, per-action-scoped endpoint, never the generic settings PUT.
TENANT_SETTING_KEY = "signup_allowed_domains"
MAX_TENANT_DOMAINS = 50
_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)

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


def parse_domains(raw: str | None) -> list[str]:
    """Comma-separated allowlist -> lower-cased, de-duplicated domains (order kept)."""
    seen: dict[str, None] = {}
    for d in (raw or "").split(","):
        d = d.strip().lower().lstrip("@")
        if d:
            seen.setdefault(d, None)
    return list(seen)


def allowed_domains(settings: Mapping[str, str]) -> list[str]:
    """The PLATFORM signup allowlist from `hub_settings.signup_allowed_domains`."""
    return parse_domains(settings.get(TENANT_SETTING_KEY, ""))


def normalize_tenant_domains(domains: Iterable[str]) -> list[str]:
    """Validate a tenant-supplied allowlist (exact DNS names, <= 50); raise 400 on a bad entry."""
    out = parse_domains(",".join(str(d) for d in domains))
    if len(out) > MAX_TENANT_DOMAINS:
        raise ApiError(f"At most {MAX_TENANT_DOMAINS} signup domains", 400, "BAD_REQUEST")
    for d in out:
        if not _DOMAIN_RE.match(d) or d.rsplit(".", 1)[-1].isdigit():
            raise ApiError(
                "Each signup domain must be a domain name like example.com", 400, "BAD_REQUEST"
            )
    return out


@dataclass(frozen=True, slots=True)
class SignupPolicy:
    """Effective signup-domain policy for one tenant (`restricted` with no domains = deny all)."""

    restricted: bool
    domains: tuple[str, ...] = ()

    def allows(self, email: str | None) -> bool:
        """Exact-domain match; an unrestricted policy allows any address (even none)."""
        if not self.restricted:
            return True
        return bool(email) and email_domain(str(email)) in self.domains


def combine_allowlists(platform: list[str], tenant: list[str]) -> SignupPolicy:
    """Platform ∩ tenant; an empty side imposes nothing. Disjoint non-empty lists deny all."""
    if platform and tenant:
        tenant_set = set(tenant)
        return SignupPolicy(True, tuple(d for d in platform if d in tenant_set))
    chosen = platform or tenant
    return SignupPolicy(bool(chosen), tuple(chosen))


async def tenant_domains(async_dal: Any, dal: Any, *, tenant_id: int) -> list[str]:
    """The tenant's own stored allowlist (`tenant_settings`)."""
    from services.schema import bind_tenant_tables  # lazy: schema imports are heavy

    bind_tenant_tables(dal)
    rows = await async_dal.select_async(
        dal(
            (dal.tenant_settings.tenant_id == tenant_id)
            & (dal.tenant_settings.key == TENANT_SETTING_KEY)
        )
    )
    return parse_domains(rows.first().value if rows else "")


async def resolve_signup_policy(
    async_dal: Any, dal: Any, *, tenant_slug: str = DEFAULT_TENANT_SLUG
) -> SignupPolicy:
    """THE resolver every signup path uses: platform allowlist ∩ the tenant's own allowlist."""
    from services.auth_service import get_hub_settings_map  # lazy: auth_service imports us

    platform = allowed_domains(await get_hub_settings_map(async_dal, dal))
    rows = await async_dal.select_async(dal(dal.tenants.slug == tenant_slug))
    tenant = await tenant_domains(async_dal, dal, tenant_id=int(rows.first().id)) if rows else []
    return combine_allowlists(platform, tenant)


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


def _domain_refused() -> ApiError:
    verification_events.add(1, {"event": "external_signup", "outcome": "domain_refused"})
    return ApiError(
        "Sign-up is restricted to specific email domains. Use an account whose verified "
        "email is in an allowed domain.",
        403,
        ERR_DOMAIN,
    )


def verification_required() -> ApiError:
    """403 telling the browser to check its inbox; no session is minted."""
    return ApiError(
        "Check your inbox: verify your email address to finish signing up.",
        403,
        ERR_REQUIRED,
        {"requiresVerification": True},
    )


async def external_signup_needs_verification(
    async_dal: Any, dal: Any, *, email: str | None, tenant_slug: str = DEFAULT_TENANT_SLUG
) -> bool:
    """Policy for a NEW OAuth/SSO user, checked BEFORE anything is created.

    Kill switch ON or unrestricted tenant: no verification. Restricted: the address must be in
    an allowed domain (else `email_domain_not_allowed`) and SMTP must be configured (else
    `email_not_configured`) -- refused loudly, never auto-verified.
    """
    if not await enforced(tenant_slug):
        return False
    policy = await resolve_signup_policy(async_dal, dal, tenant_slug=tenant_slug)
    if not policy.restricted:
        return False
    if not policy.allows(email):
        raise _domain_refused()
    require_configured()
    return True


async def hold_unverified_external_login(
    async_dal: Any,
    dal: Any,
    cfg: HubAPIConfig,
    *,
    row: Any,
    tenant_slug: str = DEFAULT_TENANT_SLUG,
) -> None:
    """Gate an EXISTING OAuth/SSO user's login under a restricted tenant.

    Verified users and unrestricted tenants pass (no retroactive block). An unverified user is
    refused (`email_domain_not_allowed` when the address is outside the allowlist), mailed a link
    when none was sent within the resend cooldown, and held with `email_verification_required`.
    """
    if bool(row.email_verified) or not await enforced(tenant_slug):
        return
    policy = await resolve_signup_policy(async_dal, dal, tenant_slug=tenant_slug)
    if not policy.restricted:
        return
    if not policy.allows(row.email):
        raise _domain_refused()
    if not row.email_verification_token or resend_wait_seconds(row.email_verification_expires) == 0:
        require_configured()
        await issue_and_send(async_dal, dal, cfg, user_id=int(row.id), email=str(row.email))
    raise verification_required()


def resend_wait_seconds(expires: datetime | None) -> int:
    """Seconds left on the resend cooldown, derived from the current token's issue time."""
    if expires is None:
        return 0
    expires_utc = expires if expires.tzinfo else expires.replace(tzinfo=UTC)
    issued = expires_utc - token_ttl()
    remaining = (issued + resend_cooldown()) - datetime.now(UTC)
    return max(0, int(remaining.total_seconds() + 0.999))
