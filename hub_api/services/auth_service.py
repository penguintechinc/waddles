"""Auth business logic -- ported from `authController.js`.

Local email/password auth, legacy admin login, temp-password login,
session/JWT issuance, and account bookkeeping (email verification,
password changes, tenant login info). OAuth flows live in
`oauth_service.py`; passkey flows in `passkey_service.py` -- both call
back into this module's `create_session_token()` so every login path
mints an identical token shape.

JWT contract: minted via `flask_core.auth.create_jwt_token` --
`sub`/`tenant`/`scope`/`roles` claims only (security.md's mandatory JWT
claims), not Node's ad-hoc `{userId, isSuperAdmin, isVendor,
tenantScopes, ...}` payload. Node's booleans are translated into
`flask_core.auth.SCOPE_BUNDLES` scope strings (global admin bundle for
`is_super_admin`, tenant admin/maintainer bundle from `tenant_admins.
role`) rather than carried as custom claims -- security.md: "never
branch on role names", scopes are the only thing `require_scope` checks.
The response BODY (not the JWT) still carries `isSuperAdmin`/`isVendor`/
etc. for the frontend, read straight from the DB row -- the JWT is
opaque to the browser (`localStorage.getItem('token')`, sent as a Bearer
header only, never decoded client-side; see `AuthContext.jsx`).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import urlencode

import bcrypt
import httpx
from flask_core.auth import SCOPE_BUNDLES, create_jwt_token, verify_jwt_token

from config import HubAPIConfig
from services.audit_http import record_session_issued
from services.errors import bad_request, conflict, forbidden, not_found, unauthorized

logger = logging.getLogger(__name__)

SALT_ROUNDS = 12  # matches authController.js's SALT_ROUNDS
_VERIFICATION_TTL = timedelta(hours=24)


@dataclass(slots=True, frozen=True)
class SessionUser:
    """Row-shaped view of `hub_users` used to mint a session + build the response body."""

    id: int
    email: str | None
    username: str | None
    avatar_url: str | None = None
    is_super_admin: bool = False
    is_vendor: bool = False
    is_analytics_consumer: bool = False


@dataclass(slots=True, frozen=True)
class LoginResult:
    """Login result."""

    token: str
    user_id: int
    email: str | None
    username: str | None
    avatar_url: str | None
    is_super_admin: bool
    is_vendor: bool
    is_analytics_consumer: bool
    linked_platforms: list[str] = field(default_factory=list)


async def _hash_password(password: str, rounds: int = SALT_ROUNDS) -> str:
    def _hash() -> str:
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=rounds)).decode()

    return await asyncio.to_thread(_hash)


async def _verify_password(password: str, password_hash: str) -> bool:
    def _verify() -> bool:
        return bcrypt.checkpw(password.encode(), password_hash.encode())

    return await asyncio.to_thread(_verify)


async def get_hub_settings_map(async_dal: Any, dal: Any) -> dict[str, str]:
    """`hub_settings` key/value rows -> dict. Read-only reference table."""
    rows = await async_dal.select_async(dal(dal.hub_settings))
    return {row.setting_key: row.setting_value for row in rows}


async def validate_captcha(
    provider: str, secret_key: str, token: str, remote_ip: str | None
) -> bool:
    """Validate a CAPTCHA token against Turnstile or reCAPTCHA. Fails closed on any error."""
    url = (
        "https://challenges.cloudflare.com/turnstile/v0/siteverify"
        if provider == "turnstile"
        else "https://www.google.com/recaptcha/api/siteverify"
    )
    params = {"secret": secret_key, "response": token}
    if remote_ip:
        params["remoteip"] = remote_ip
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url,
                content=urlencode(params),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            return bool(resp.json().get("success") is True)
    except (httpx.HTTPError, ValueError):
        return False


async def create_session_token(
    async_dal: Any,
    dal: Any,
    cfg: HubAPIConfig,
    *,
    user: SessionUser | None,
    username_override: str | None = None,
    tenant_id: int | None = None,
    tenant_slug: str | None = None,
    requires_oauth_link: bool = False,
    auth_method: str = "unspecified",
) -> str:
    """Mint a JWT (`flask_core.auth.create_jwt_token`) and record `hub_sessions`.

    `user` is `None` for the temp-password pending-link flow (Node's
    `createSession({userId: null, ...})`) -- `sub` becomes `"0"` in that
    case (flask_core's `create_jwt_token` requires a `user_id`; there is
    no "no subject" JWT). `tenant` is always set -- `DEFAULT_TENANT_SLUG`
    ("global") when the caller has no tenant context, per security.md
    Tenant Isolation ("there is no untenanted token").

    Every login path funnels through here, so this is also the one place session issuance
    (password / OAuth-SSO / passkey / refresh) is written to the tamper-evident audit log;
    `auth_method` names the path (an identifier, never user input). The audit write happens
    BEFORE the `hub_sessions` row is persisted and raises `AuditWriteError` on failure, so a
    session the audit log cannot account for is never issued (entitled tenants only).
    """
    roles: list[str] = []
    scopes: set[str] = set(SCOPE_BUNDLES["global"]["viewer"])  # every session gets *:read
    if user is not None:
        if user.is_super_admin:
            roles.extend(["admin", "super_admin"])
            scopes.update(SCOPE_BUNDLES["global"]["admin"])
        if user.is_vendor:
            roles.append("vendor")
            # `vendor:onboard` gates `POST /apps/{app_id}/versions`'s
            # vendor path (`services/vendor_bundle_authz.py`) -- same
            # pattern as `is_analytics_consumer` below: a boolean flag on
            # `hub_users` needs a real scope grant at mint time, not just
            # an audit-only `roles` entry, before any `require_scope`-style
            # check can key off it (security.md: scopes only, never role
            # names).
            scopes.add("vendor:onboard")
        if user.is_analytics_consumer:
            # Analytics-module port (M9): `analytics:read` is the pre-
            # existing scope name from security.md's own SCOPE_BUNDLES
            # table (`maintainer -> ... analytics:read`), already granted
            # via the `global`/`tenant` maintainer bundles and the
            # `global` admin bundle's `*:read` wildcard (superadmins
            # already pass `require_scope("analytics:read")` without this
            # branch). What was missing: `hub_users.is_analytics_consumer`
            # (M1, `060_analytics_consumer_role.sql`) had no corresponding
            # scope grant at mint time, so no analytics-consumer user
            # could ever satisfy `blueprints/v1/analytics.py`'s platform-
            # overview gate -- see that module's docstring.
            scopes.add("analytics:read")

        if tenant_id is not None:
            ta_rows = await async_dal.select_async(
                dal(
                    (dal.tenant_admins.tenant_id == tenant_id)
                    & (dal.tenant_admins.user_id == user.id)
                )
            )
            if ta_rows:
                role = ta_rows.first().role
                if role == "tenant-owner":
                    scopes.update(SCOPE_BUNDLES["tenant"]["admin"])
                elif role == "tenant-admin":
                    scopes.update(SCOPE_BUNDLES["tenant"]["maintainer"])

    resolved_tenant = tenant_slug or "global"
    # flask_core is `follow_imports = "skip"` -- create_jwt_token() is typed
    # Any from mypy's perspective; cast to the real, stable return type.
    token = cast(
        str,
        create_jwt_token(
            user_id=str(user.id if user is not None else 0),
            username=(user.username if user is not None else username_override) or "",
            email=(user.email if user is not None else None) or "",
            roles=roles,
            secret_key=cfg.secret_key,
            tenant=resolved_tenant,
            scope=" ".join(sorted(scopes)),
            expiration_hours=24,
        ),
    )

    await record_session_issued(
        user_id=user.id if user is not None else None,
        tenant_id=tenant_id,
        tenant_slug=resolved_tenant,
        auth_method=auth_method,
        pending_link=requires_oauth_link,
    )

    now = datetime.now(UTC)
    await async_dal.insert_async(
        dal.hub_sessions,
        session_token=token,
        user_id=user.id if user is not None else None,
        platform_username=(user.username if user is not None else username_override),
        avatar_url=(user.avatar_url if user is not None else None),
        is_active=True,
        expires_at=now + timedelta(hours=24),
        created_at=now,
    )
    return token


async def add_user_to_global_community(async_dal: Any, dal: Any, *, user_id: int) -> None:
    """Auto-add a new user to the global community. Never fails registration -- logs and returns.

    Node reads `config->>'is_global' = 'true'` (JSONB), not the `is_global`
    boolean column also present on `communities` -- two competing signals
    that predate this port; preserved as-is (no behavior change), not
    silently "fixed" to use the boolean column.
    """
    try:
        rows = await async_dal.executesql_async(
            "SELECT id FROM communities WHERE config->>'is_global' = 'true' "
            "AND is_active = true LIMIT 1"
        )
        if not rows:
            return
        community_id = rows[0][0]
        await async_dal.executesql_async(
            "INSERT INTO community_members (community_id, user_id, role, is_active, joined_at) "
            "VALUES (%s, %s, 'member', true, NOW()) "
            "ON CONFLICT (community_id, user_id) DO NOTHING",
            [community_id, str(user_id)],
        )
        await async_dal.executesql_async(
            "UPDATE communities SET member_count = ("
            "  SELECT COUNT(*) FROM community_members "
            "  WHERE community_id = %s AND is_active = true"
            ") WHERE id = %s",
            [community_id, community_id],
        )
    except Exception as exc:  # noqa: BLE001 - must never fail registration, matches Node's try/catch
        logger.error(
            "CRITICAL: failed to add user to global community",
            extra={"user_id": user_id, "error": str(exc)},
        )


async def register(
    async_dal: Any,
    dal: Any,
    cfg: HubAPIConfig,
    *,
    email: str,
    password: str,
    username: str | None,
    captcha_token: str | None,
    remote_ip: str | None,
) -> tuple[bool, LoginResult | None]:
    """Register a local user. Returns `(requires_verification, result)`."""
    if not email or not password:
        raise bad_request("Email and password required")

    settings = await get_hub_settings_map(async_dal, dal)
    if settings.get("signup_enabled") != "true" or settings.get("email_configured") != "true":
        raise forbidden("Registration is currently disabled")

    captcha_provider = settings.get("captcha_provider", "none")
    captcha_secret = settings.get("captcha_secret_key", "")
    if captcha_provider != "none" and captcha_secret:
        if not captcha_token:
            raise bad_request("CAPTCHA verification required")
        if not await validate_captcha(captcha_provider, captcha_secret, captcha_token, remote_ip):
            raise bad_request("CAPTCHA verification failed")

    allowed_domains = [
        d.strip().lower()
        for d in settings.get("signup_allowed_domains", "").split(",")
        if d.strip()
    ]
    email_lower = email.lower()
    domain = email_lower.rsplit("@", 1)[-1] if "@" in email_lower else ""
    if allowed_domains and domain not in allowed_domains:
        raise forbidden("Registration is restricted to specific email domains")

    existing = await async_dal.select_async(dal(dal.hub_users.email == email_lower))
    if existing:
        raise conflict("Email already registered")

    password_hash = await _hash_password(password)
    require_verification = settings.get("signup_require_email_verification") == "true"

    verification_token: str | None = None
    verification_expires: datetime | None = None
    if require_verification:
        verification_token = secrets.token_urlsafe(32)
        verification_expires = datetime.now(UTC) + _VERIFICATION_TTL

    user_id = await async_dal.insert_async(
        dal.hub_users,
        email=email_lower,
        password_hash=password_hash,
        username=username or email_lower,
        is_active=True,
        email_verified=not require_verification,
        email_verification_token=verification_token,
        email_verification_expires=verification_expires,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    # add_user_to_global_community() never raises -- it logs internally and
    # returns, matching Node's "don't fail registration, but log loudly" path.
    await add_user_to_global_community(async_dal, dal, user_id=user_id)

    if require_verification:
        # Email delivery (Node's sendVerificationEmail()) is out of scope --
        # hub-api has no SMTP/email client wired in yet; that's a dependency
        # M1 doesn't own (not in this PR's service list). The token is
        # generated and stored regardless, so verify_email() below is fully
        # functional the moment a caller has the token (email delivery is
        # the only missing leg, tracked in hub_api/PORTING.md).
        return True, None

    user = SessionUser(id=user_id, email=email_lower, username=username or email_lower)
    token = await create_session_token(async_dal, dal, cfg, user=user, auth_method="register")
    return False, LoginResult(
        token=token,
        user_id=user.id,
        email=user.email,
        username=user.username,
        avatar_url=None,
        is_super_admin=False,
        is_vendor=False,
        is_analytics_consumer=False,
    )


async def verify_email(async_dal: Any, dal: Any, cfg: HubAPIConfig, *, token: str) -> LoginResult:
    """Verify email."""
    if not token:
        raise bad_request("Verification token required")

    rows = await async_dal.select_async(dal(dal.hub_users.email_verification_token == token))
    if not rows:
        raise bad_request("Invalid or expired verification token")
    row = rows.first()

    expires = row.email_verification_expires
    if expires is not None:
        expires_utc = expires if expires.tzinfo else expires.replace(tzinfo=UTC)
        if datetime.now(UTC) > expires_utc:
            raise bad_request("Verification token has expired")

    await async_dal.update_async(
        dal.hub_users.id == row.id,
        email_verified=True,
        email_verification_token=None,
        email_verification_expires=None,
    )

    user = SessionUser(id=row.id, email=row.email, username=row.username)
    session_token = await create_session_token(
        async_dal, dal, cfg, user=user, auth_method="email_verification"
    )
    return LoginResult(
        token=session_token,
        user_id=user.id,
        email=user.email,
        username=user.username,
        avatar_url=None,
        is_super_admin=False,
        is_vendor=False,
        is_analytics_consumer=False,
    )


async def resend_verification(async_dal: Any, dal: Any, *, email: str) -> None:
    """Resend verification."""
    if not email:
        raise bad_request("Email required")

    rows = await async_dal.select_async(dal(dal.hub_users.email == email.lower()))
    if not rows:
        return  # don't reveal whether the account exists
    row = rows.first()
    if row.email_verified:
        raise bad_request("Email is already verified")

    await async_dal.update_async(
        dal.hub_users.id == row.id,
        email_verification_token=secrets.token_urlsafe(32),
        email_verification_expires=datetime.now(UTC) + _VERIFICATION_TTL,
    )
    # Email delivery is out of scope here too -- same gap noted in register() above.


class RequiresVerificationError(Exception):
    """Sentinel for login()'s 403 requiresVerification branch (distinct JSON shape)."""


async def login(
    async_dal: Any,
    dal: Any,
    cfg: HubAPIConfig,
    *,
    email: str,
    password: str,
    tenant_slug: str = "global",
) -> LoginResult:
    """Login."""
    if not email or not password:
        raise bad_request("Email and password required")

    tenant_rows = await async_dal.select_async(
        dal((dal.tenants.slug == tenant_slug) & (dal.tenants.is_active == True))  # noqa: E712
    )
    tenant_id = tenant_rows.first().id if tenant_rows else None

    rows = await async_dal.select_async(dal(dal.hub_users.email == email.lower()))
    if not rows:
        raise unauthorized("Invalid credentials")
    row = rows.first()

    if not row.is_active:
        raise unauthorized("Account is inactive")
    if row.email_verified is False:
        raise RequiresVerificationError()

    if not row.password_hash or not await _verify_password(password, row.password_hash):
        raise unauthorized("Invalid credentials")

    await async_dal.update_async(dal.hub_users.id == row.id, last_login=datetime.now(UTC))

    linked_rows = await async_dal.select_async(
        dal(dal.hub_user_identities.hub_user_id == row.id),
        dal.hub_user_identities.platform,
        distinct=True,
    )
    linked_platforms = [r.platform for r in linked_rows]

    user = SessionUser(
        id=row.id,
        email=row.email,
        username=row.username,
        avatar_url=row.avatar_url,
        is_super_admin=bool(row.is_super_admin),
        is_vendor=bool(row.is_vendor),
        is_analytics_consumer=bool(row.is_analytics_consumer),
    )
    token = await create_session_token(
        async_dal,
        dal,
        cfg,
        user=user,
        tenant_id=tenant_id,
        tenant_slug=tenant_slug,
        auth_method="password",
    )
    return LoginResult(
        token=token,
        user_id=user.id,
        email=user.email,
        username=user.username,
        avatar_url=user.avatar_url,
        is_super_admin=user.is_super_admin,
        is_vendor=user.is_vendor,
        is_analytics_consumer=user.is_analytics_consumer,
        linked_platforms=linked_platforms,
    )


async def admin_login(
    async_dal: Any, dal: Any, cfg: HubAPIConfig, *, username: str, password: str
) -> LoginResult:
    """Admin login."""
    if not username or not password:
        raise bad_request("Username and password required")

    rows = await async_dal.select_async(
        dal(
            ((dal.hub_users.username == username) | (dal.hub_users.email == username))
            & (dal.hub_users.is_active == True)  # noqa: E712
        )
    )
    if not rows:
        raise unauthorized("Invalid credentials")
    row = rows.first()

    if not row.password_hash or not await _verify_password(password, row.password_hash):
        raise unauthorized("Invalid credentials")

    await async_dal.update_async(dal.hub_users.id == row.id, last_login=datetime.now(UTC))

    user = SessionUser(
        id=row.id,
        email=row.email,
        username=row.username,
        avatar_url=row.avatar_url,
        is_super_admin=bool(row.is_super_admin),
        is_vendor=bool(row.is_vendor),
        is_analytics_consumer=bool(row.is_analytics_consumer),
    )
    token = await create_session_token(async_dal, dal, cfg, user=user, auth_method="admin_password")
    return LoginResult(
        token=token,
        user_id=user.id,
        email=user.email,
        username=user.username,
        avatar_url=user.avatar_url,
        is_super_admin=user.is_super_admin,
        is_vendor=user.is_vendor,
        is_analytics_consumer=user.is_analytics_consumer,
    )


@dataclass(slots=True, frozen=True)
class TempLoginResult:
    """Temp login result."""

    token: str
    requires_oauth_link: bool


async def temp_password_login(
    async_dal: Any, dal: Any, cfg: HubAPIConfig, *, identifier: str, password: str
) -> TempLoginResult:
    """Temp password login."""
    if not identifier or not password:
        raise bad_request("Identifier and password required")

    rows = await async_dal.select_async(
        dal(
            (dal.hub_temp_passwords.user_identifier == identifier)
            & (dal.hub_temp_passwords.is_used == False)  # noqa: E712
            & (dal.hub_temp_passwords.expires_at > datetime.now(UTC))
        )
    )
    if not rows:
        raise unauthorized("Invalid credentials or expired")
    row = rows.first()

    if not await _verify_password(password, row.password_hash):
        raise unauthorized("Invalid credentials")

    await async_dal.update_async(
        dal.hub_temp_passwords.id == row.id, is_used=True, used_at=datetime.now(UTC)
    )

    token = await create_session_token(
        async_dal,
        dal,
        cfg,
        user=None,
        username_override=identifier,
        requires_oauth_link=bool(row.force_oauth_link),
        auth_method="temp_password",
    )
    return TempLoginResult(token=token, requires_oauth_link=bool(row.force_oauth_link))


async def refresh_token(async_dal: Any, dal: Any, cfg: HubAPIConfig, *, token: str) -> str:
    """Refresh token."""
    if not token:
        raise bad_request("Token required")

    payload = verify_jwt_token(token, cfg.secret_key)
    if payload is None:
        raise unauthorized("Invalid token")

    session_rows = await async_dal.select_async(
        dal((dal.hub_sessions.session_token == token) & (dal.hub_sessions.is_active == True))  # noqa: E712
    )
    if not session_rows:
        raise unauthorized("Invalid session")

    user_id = int(payload["sub"])
    user_rows = await async_dal.select_async(dal(dal.hub_users.id == user_id))
    if not user_rows:
        raise unauthorized("Invalid session")
    row = user_rows.first()

    user = SessionUser(
        id=row.id,
        email=row.email,
        username=row.username,
        avatar_url=row.avatar_url,
        is_super_admin=bool(row.is_super_admin),
        is_vendor=bool(row.is_vendor),
        is_analytics_consumer=bool(row.is_analytics_consumer),
    )
    new_token = await create_session_token(
        async_dal, dal, cfg, user=user, tenant_slug=payload.get("tenant"), auth_method="refresh"
    )
    await async_dal.update_async(
        dal.hub_sessions.session_token == token,
        is_active=False,
        revoked_at=datetime.now(UTC),
    )
    return new_token


async def logout(async_dal: Any, dal: Any, *, token: str | None) -> None:
    """Logout."""
    if token:
        await async_dal.update_async(
            dal.hub_sessions.session_token == token,
            is_active=False,
            revoked_at=datetime.now(UTC),
        )


@dataclass(slots=True, frozen=True)
class CommunityMembership:
    """Community membership."""

    id: int
    name: str
    display_name: str
    role: str


@dataclass(slots=True, frozen=True)
class LinkedIdentitySummary:
    """One linked platform identity, as surfaced on `GET /auth/me`."""

    platform: str
    username: str | None
    avatar: str | None


@dataclass(slots=True, frozen=True)
class CurrentUserResult:
    """Current user result."""

    id: int
    email: str | None
    username: str | None
    avatar_url: str | None
    is_super_admin: bool
    is_vendor: bool
    is_analytics_consumer: bool
    has_password: bool
    linked_platforms: list[LinkedIdentitySummary]
    communities: list[CommunityMembership]


async def get_current_user(
    async_dal: Any, dal: Any, *, user_id: int | None
) -> CurrentUserResult | None:
    """Get current user."""
    if user_id is None:
        return None

    rows = await async_dal.select_async(dal(dal.hub_users.id == user_id))
    if not rows:
        return None
    row = rows.first()

    identity_rows = await async_dal.select_async(
        dal(dal.hub_user_identities.hub_user_id == user_id)
    )
    linked = [
        LinkedIdentitySummary(
            platform=r.platform, username=r.platform_username, avatar=r.avatar_url
        )
        for r in identity_rows
    ]

    community_rows = await async_dal.select_async(
        dal(
            (dal.community_members.user_id == str(user_id))
            & (dal.community_members.is_active == True)  # noqa: E712 - pydal Field comparison
            & (dal.communities.id == dal.community_members.community_id)
            & (dal.communities.is_active == True)  # noqa: E712 - pydal Field comparison
        ),
        dal.communities.id,
        dal.communities.name,
        dal.communities.display_name,
        dal.community_members.role,
    )
    communities = [
        CommunityMembership(
            id=r.communities.id,
            name=r.communities.name,
            display_name=r.communities.display_name or r.communities.name,
            role=r.community_members.role,
        )
        for r in community_rows
    ]

    return CurrentUserResult(
        id=row.id,
        email=row.email,
        username=row.username,
        avatar_url=row.avatar_url,
        is_super_admin=bool(row.is_super_admin),
        is_vendor=bool(row.is_vendor),
        is_analytics_consumer=bool(row.is_analytics_consumer),
        has_password=row.password_hash is not None,
        linked_platforms=linked,
        communities=communities,
    )


async def set_password(
    async_dal: Any, dal: Any, *, user_id: int, current_password: str | None, new_password: str
) -> None:
    """Set password."""
    if not new_password or len(new_password) < 8:
        raise bad_request("Password must be at least 8 characters")

    rows = await async_dal.select_async(dal(dal.hub_users.id == user_id))
    if not rows:
        raise not_found("User not found")
    row = rows.first()

    if row.password_hash:
        if not current_password:
            raise bad_request("Current password required")
        if not await _verify_password(current_password, row.password_hash):
            raise unauthorized("Current password is incorrect")

    new_hash = await _hash_password(new_password)
    await async_dal.update_async(dal.hub_users.id == user_id, password_hash=new_hash)


@dataclass(slots=True, frozen=True)
class TenantLoginInfo:
    """Tenant login info."""

    slug: str
    display_name: str
    logo_url: str | None
    is_global: bool
    enabled_platforms: list[str]
    theme: Any
    welcome_message: Any


async def get_tenant_login_info(async_dal: Any, dal: Any, *, slug: str) -> TenantLoginInfo:
    """Get tenant login info."""
    rows = await async_dal.select_async(dal(dal.tenants.slug == slug))
    if not rows:
        raise not_found("Tenant not found")
    tenant = rows.first()
    if not tenant.is_active:
        raise forbidden("This tenant is currently inactive")

    pc_rows = await async_dal.select_async(
        dal(
            (dal.platform_configs.tenant_id == tenant.id) & (dal.platform_configs.enabled == True)  # noqa: E712 - pydal Field comparison
        ),
        dal.platform_configs.platform,
        distinct=True,
    )
    tenant_config = tenant.config or {}
    return TenantLoginInfo(
        slug=tenant.slug,
        display_name=tenant.display_name,
        logo_url=tenant.logo_url,
        is_global=bool(tenant.is_global),
        enabled_platforms=[r.platform for r in pc_rows],
        theme=tenant_config.get("theme"),
        welcome_message=tenant_config.get("welcomeMessage"),
    )
