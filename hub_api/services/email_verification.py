"""Account email verification: tokens, signup-domain policy and the verification mail.

Policy (core security mechanism, opt-out kill switch `waddles.disable-email-verification`;
unseen/OFF = enforced). The kill switch is PLATFORM-scoped: `enforced()` takes no tenant and is
evaluated once for the reserved operator scope (`system`) on every path, so a per-tenant flag
override can never switch verification off for one tenant.

* Local email/password signups are ALWAYS pending until the address is verified (no session).
* Verifying an address never mints a session: `/verify-email` confirms the address and the user
  then logs in normally (password, OAuth or SSO -- a fresh login with that credential).
* Signup-domain restriction: the EFFECTIVE allowlist for a tenant (`resolve_signup_policy`, the
  one resolver every signup, email-change and verify path uses) is the platform list
  (`hub_settings.signup_allowed_domains`) intersected with the tenant's own list
  (`tenant_settings` key `signup_allowed_domains`); whichever is empty imposes nothing. Domains
  are normalized (lower-case, trailing dot stripped, IDNA/punycode) before any comparison.
  When the effective policy is restricted, EVERY signup into that tenant -- local, OAuth and
  tenant SSO alike -- must verify an address in an allowed domain before it gets a session, and
  an existing user whose email is still unverified is held at their next login the same way.
* OAuth: a provider email counts as verified only when the provider explicitly asserts it
  (`email_verified=true`). An unverified provider address is never attached to the account in
  an unrestricted tenant (a synthetic placeholder is used); in a restricted tenant it is held
  until WE verify it.
* Any email change (admin or self) goes through `email_change_fields` +
  `schedule_verification`: normalized, re-checked against the policy, marked unverified, old
  tokens cleared and a fresh link mailed.

Tokens: 32 random bytes (urlsafe), stored ONLY as a SHA-256 hex digest in
`hub_users.email_verification_token`, single use (cleared on success, cancel or deactivation),
TTL 24 h (`EMAIL_VERIFICATION_TTL_HOURS`). The same token backs the "this wasn't me" link, which
cancels and deletes a still-unverified pending account.

Non-enumeration: register and resend return one fixed shape for known, unknown and cooled-down
addresses alike; everything that depends on whether the account exists (lookup, insert, cooldown,
SMTP) runs in a tracked background task (`schedule`), so latency does not reveal it either. A
background failure is logged at ERROR and counted, never silent. Every verification mail -- local,
resend, OAuth and SSO holds -- also charges the same per-IP auth-tier limiter bucket
(`charge_send_budget`).

Email: sent through `services/email_service.py`. Unset SMTP fails loud BEFORE any state is
created (`email_not_configured`, 503). Addresses never reach logs.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import logging
import os
import re
import secrets
from collections.abc import Awaitable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from flask_core.auth import DEFAULT_TENANT_SLUG
from flask_core.feature_flags import feature_enabled
from opentelemetry import metrics
from quart import current_app, has_request_context

from config import HubAPIConfig
from services import email_service
from services.errors import ApiError
from services.rate_limiting import RATE_LIMITER_CONFIG_KEY, auth_ip_bucket_key

logger = logging.getLogger(__name__)

FLAG_DISABLE_EMAIL_VERIFICATION = "waddles.disable-email-verification"
#: The kill switch is platform-wide: evaluated for the reserved operator tenant only, never per
#: tenant (a tenant-level PostHog override must not be able to disable verification).
FLAG_SCOPE_TENANT = "system"
EMAIL_KIND = "email_verification"
NOTICE_KIND = "email_signup_notice"
SUBJECT = "Confirm your Waddles account request"
NOTICE_SUBJECT = "Someone tried to register with your email address"

ERR_NOT_CONFIGURED = "email_not_configured"
ERR_SEND_FAILED = "verification_email_failed"
ERR_REQUIRED = "email_verification_required"
ERR_DOMAIN = "email_domain_not_allowed"

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

#: Background mail/account jobs still running (strong refs: asyncio keeps only weak ones).
_pending: set[asyncio.Task[None]] = set()


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


async def enforced() -> bool:
    """True unless the platform-wide `waddles.disable-email-verification` kill switch is ON.

    Deliberately takes no tenant: the switch is evaluated for the operator scope only, so the
    same answer applies on every path (local, OAuth, SSO, resend, email change).
    """
    return not await feature_enabled(FLAG_DISABLE_EMAIL_VERIFICATION, tenant=FLAG_SCOPE_TENANT)


def normalize_domain(raw: str) -> str:
    """Canonical comparable domain: trimmed, lower-cased, no leading '@'/trailing '.', IDNA.

    Returns '' for a domain that cannot be IDNA-encoded (so it never matches an allowlist).
    """
    d = raw.strip().lower().lstrip("@").rstrip(".")
    if not d:
        return ""
    try:
        return d.encode("idna").decode("ascii")
    except UnicodeError:
        return ""


def normalize_email(email: str) -> str:
    """Lower-cased address with a normalized domain part ('' if the domain is unusable)."""
    value = (email or "").strip().lower()
    if "@" not in value:
        return value
    local, _, domain = value.rpartition("@")
    norm = normalize_domain(domain)
    return f"{local}@{norm}" if local and norm else ""


def parse_domains(raw: str | None) -> list[str]:
    """Comma-separated allowlist -> normalized, de-duplicated domains (order kept)."""
    seen: dict[str, None] = {}
    for d in (raw or "").split(","):
        if not d.strip():
            continue
        norm = normalize_domain(d)
        # An entry that cannot be IDNA-encoded is kept verbatim so validation rejects it loudly.
        seen.setdefault(norm or d.strip().lower(), None)
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
        """Exact (normalized) domain match; an unrestricted policy allows any address."""
        if not self.restricted:
            return True
        domain = email_domain(str(email)) if email else ""
        return bool(domain) and domain in self.domains


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
    """Normalized domain part of an address ('' when there is none or it is unusable)."""
    return normalize_domain(email.rsplit("@", 1)[-1]) if "@" in email else ""


def verify_url(cfg: HubAPIConfig, raw_token: str) -> str:
    """Webui link; the token rides in the fragment so it never reaches a server log."""
    return f"{cfg.frontend_origin.rstrip('/')}/verify-email#token={raw_token}"


def cancel_url(cfg: HubAPIConfig, raw_token: str) -> str:
    """Build the "this wasn't me" link: same single-use token, cancels the pending account."""
    return f"{verify_url(cfg, raw_token)}&action=cancel"


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


async def charge_send_budget(cfg: HubAPIConfig) -> bool:
    """Charge one verification mail to the caller's per-IP auth-tier limiter bucket.

    The SAME key the global auth-tier limiter uses for unauthenticated callers, so register,
    resend, OAuth and SSO holds all draw on one budget. Returns False (and logs) when exhausted;
    outside a request (CLI/tests) or with no limiter installed there is no bucket to charge.
    """
    if not has_request_context():
        return True
    limiter = current_app.config.get(RATE_LIMITER_CONFIG_KEY)
    if limiter is None:
        return True
    allowed = bool(
        await limiter.check_rate_limit(
            auth_ip_bucket_key(cfg.trusted_proxy_hops),
            cfg.rate_limit_auth_max_requests,
            cfg.rate_limit_auth_window_seconds,
        )
    )
    if not allowed:
        verification_events.add(1, {"event": "send", "outcome": "ip_rate_limited"})
        logger.warning("email_verification.send_skipped reason=ip_rate_limited")
    return allowed


async def _deliver(
    cfg: HubAPIConfig, *, to_email: str, subject: str, text: str, page: str, kind: str
) -> None:
    """Send one mail; raise `verification_email_failed`/`email_not_configured` (503) on failure."""
    try:
        await email_service.send_email(
            to_email=to_email, subject=subject, text_body=text, html_body=page, kind=kind
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
    verification_events.add(1, {"event": "send", "outcome": "sent", "kind": kind})


async def send_verification_email(cfg: HubAPIConfig, *, to_email: str, raw_token: str) -> None:
    """Mail the "someone requested an account" link plus the "this wasn't me" cancel link."""
    url = verify_url(cfg, raw_token)
    cancel = cancel_url(cfg, raw_token)
    hours = int(token_ttl().total_seconds() // 3600)
    text = (
        "Someone requested an account with this address on Waddles.\n\n"
        "If that was you, confirm the address by opening this link, then sign in:\n\n"
        f"{url}\n\n"
        "If this wasn't you, open this link to cancel the request and delete the pending "
        f"account:\n\n{cancel}\n\n"
        f"Each link can be used once and expires in {hours} hours.\n"
    )
    safe_url = html.escape(url, quote=True)
    safe_cancel = html.escape(cancel, quote=True)
    page = (
        '<!doctype html><html><body style="font-family:sans-serif">'
        "<p>Someone requested an account with this address on Waddles.</p>"
        f'<p>If that was you: <a href="{safe_url}">confirm this address</a>, then sign in.</p>'
        f"<p>If this wasn't you: <a href=\"{safe_cancel}\">this wasn't me</a> "
        "(cancels the request and deletes the pending account).</p>"
        f"<p>Each link can be used once and expires in {hours} hours.</p></body></html>"
    )
    await _deliver(cfg, to_email=to_email, subject=SUBJECT, text=text, page=page, kind=EMAIL_KIND)


async def send_existing_account_notice(cfg: HubAPIConfig, *, to_email: str) -> None:
    """Tell an existing owner that someone tried to register with their address."""
    login = f"{cfg.frontend_origin.rstrip('/')}/login"
    text = (
        "Someone tried to register a new Waddles account with your email address. "
        "You already have an account, so nothing was created.\n\n"
        f"If it was you, sign in instead: {login}\n\n"
        "If it wasn't you, you can ignore this email; your account is unchanged.\n"
    )
    safe = html.escape(login, quote=True)
    page = (
        '<!doctype html><html><body style="font-family:sans-serif">'
        "<p>Someone tried to register a new Waddles account with your email address. "
        "You already have an account, so nothing was created.</p>"
        f'<p>If it was you, <a href="{safe}">sign in instead</a>.</p>'
        "<p>If it wasn't you, you can ignore this email; your account is unchanged.</p>"
        "</body></html>"
    )
    await _deliver(
        cfg, to_email=to_email, subject=NOTICE_SUBJECT, text=text, page=page, kind=NOTICE_KIND
    )


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


def _log_job_result(task: asyncio.Task[None]) -> None:
    """Done-callback: a background failure is logged at ERROR and counted, never silent."""
    _pending.discard(task)
    if task.cancelled():
        verification_events.add(1, {"event": "background", "outcome": "cancelled"})
        logger.error("email_verification.background_cancelled job=%s", task.get_name())
        return
    exc = task.exception()
    if exc is None:
        return
    reason = exc.code if isinstance(exc, ApiError) else type(exc).__name__
    verification_events.add(1, {"event": "background", "outcome": "failed"})
    # Exception class / typed code only: messages may carry an address.
    logger.error("email_verification.background_failed job=%s reason=%s", task.get_name(), reason)


def schedule(job: Awaitable[None], *, name: str) -> asyncio.Task[None]:
    """Run an account-dependent mail job off the request path (tracked, failures logged)."""

    async def _run() -> None:
        await job

    task = asyncio.get_running_loop().create_task(_run(), name=f"email_verification.{name}")
    _pending.add(task)
    task.add_done_callback(_log_job_result)
    return task


async def drain_pending_sends(grace_seconds: float = 30.0) -> int:
    """Await every in-flight background job (shutdown hook + tests); returns how many ran."""
    tasks = list(_pending)
    if tasks:
        await asyncio.wait(tasks, timeout=grace_seconds)
    return len(tasks)


async def schedule_verification(
    async_dal: Any, dal: Any, cfg: HubAPIConfig, *, user_id: int, email: str, name: str
) -> None:
    """Charge the per-IP send budget, then issue + mail a fresh token in the background."""
    if not await charge_send_budget(cfg):
        return
    schedule(issue_and_send(async_dal, dal, cfg, user_id=user_id, email=email), name=name)


@dataclass(frozen=True, slots=True)
class EmailChange:
    """A validated email change: the normalized address and the hub_users fields to write."""

    email: str
    fields: dict[str, Any]


async def email_change_fields(
    async_dal: Any,
    dal: Any,
    *,
    user_id: int,
    new_email: str,
    tenant_slug: str = DEFAULT_TENANT_SLUG,
) -> EmailChange:
    """THE shared validation for every email-change path (admin or self).

    Normalizes, re-checks the signup-domain policy, refuses a duplicate, and returns the fields
    that reset verification (unverified, old tokens cleared). Callers write the fields, then
    call `schedule_verification` for the new address.
    """
    email = normalize_email(new_email)
    if not email or "@" not in email:
        raise ApiError("A valid email address is required", 400, "BAD_REQUEST")
    policy = await resolve_signup_policy(async_dal, dal, tenant_slug=tenant_slug)
    if not policy.allows(email):
        verification_events.add(1, {"event": "email_change", "outcome": "domain_refused"})
        raise ApiError("This email domain is not allowed", 403, ERR_DOMAIN)
    dupe = await async_dal.select_async(
        dal((dal.hub_users.email == email) & (dal.hub_users.id != user_id))
    )
    if dupe:
        raise ApiError("Email already in use", 409, "CONFLICT")
    return EmailChange(
        email=email,
        fields={
            "email": email,
            "email_verified": False,
            "email_verification_token": None,
            "email_verification_expires": None,
        },
    )


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
    if not await enforced():
        return False
    policy = await resolve_signup_policy(async_dal, dal, tenant_slug=tenant_slug)
    if not policy.restricted:
        return False
    if not policy.allows(email):
        raise _domain_refused()
    require_configured()
    return True


async def send_external_hold(
    async_dal: Any, dal: Any, cfg: HubAPIConfig, *, user_id: int, email: str
) -> None:
    """Mail the link for an OAuth/SSO hold, charged to the same per-IP bucket as resend."""
    if await charge_send_budget(cfg):
        await issue_and_send(async_dal, dal, cfg, user_id=user_id, email=email)


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
    if bool(row.email_verified) or not await enforced():
        return
    policy = await resolve_signup_policy(async_dal, dal, tenant_slug=tenant_slug)
    if not policy.restricted:
        return
    if not policy.allows(row.email):
        raise _domain_refused()
    if not row.email_verification_token or resend_wait_seconds(row.email_verification_expires) == 0:
        require_configured()
        await send_external_hold(async_dal, dal, cfg, user_id=int(row.id), email=str(row.email))
    raise verification_required()


def resend_wait_seconds(expires: datetime | None) -> int:
    """Seconds left on the resend cooldown, derived from the current token's issue time."""
    if expires is None:
        return 0
    expires_utc = expires if expires.tzinfo else expires.replace(tzinfo=UTC)
    issued = expires_utc - token_ttl()
    remaining = (issued + resend_cooldown()) - datetime.now(UTC)
    return max(0, int(remaining.total_seconds() + 0.999))
