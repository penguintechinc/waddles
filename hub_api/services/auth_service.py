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
`flask_core.auth.SCOPE_BUNDLES` scope strings (global admin bundle for a
system-tenant admin, `global_identity.is_global_admin`; tenant admin/maintainer bundle from
`tenant_admins.role`) rather than carried as custom claims -- security.md: "never
branch on role names", scopes are the only thing `require_scope` checks.
The response BODY (not the JWT) still carries `isSuperAdmin`/`isVendor`/
etc. for the frontend, read straight from the DB row -- the JWT is
opaque to the browser (`localStorage.getItem('token')`, sent as a Bearer
header only, never decoded client-side; see `AuthContext.jsx`).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import urlencode

import bcrypt
import httpx
from flask_core.auth import (
    DEFAULT_TENANT_SLUG,
    JWT_CLOCK_SKEW_SECONDS,
    SCOPE_BUNDLES,
    create_jwt_token,
    verify_jwt_token,
)
from flask_core.token_revocation import get_token_revocation
from opentelemetry import metrics
from quart import has_request_context

from config import HubAPIConfig
from services import session_service
from services.audit_http import record_session_issued, record_session_revoked
from services.community_authz import member_role_scopes
from services.errors import ApiError, bad_request, conflict, forbidden, not_found, unauthorized
from services.global_identity import is_global_admin
from services.membership_state import MembershipState
from services.scope_registry import all_scopes
from services.session_cookie import issue_refresh_cookie

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


_ALTCHA_MAX_NUMBER = 50_000
_ALTCHA_TTL_SECONDS = 300
_ALTCHA_EXPIRY_SLACK_SECONDS = 30
_ALTCHA_MAC_PERSON = b"login-altcha-v1"
_ALTCHA_USED_PREFIX = "hub_api:altcha:used:"
_ALTCHA_RATE_PREFIX = "hub_api:altcha:rl:"
ALTCHA_CHALLENGE_RATE_LIMIT = 20
ALTCHA_CHALLENGE_RATE_WINDOW_SECONDS = 60
#: `<24 hex>?expires=<digits>&` -- the `&` terminator makes the salt/number boundary
#: unambiguous, so digits cannot be shifted from the number into the signed expiry.
_ALTCHA_SALT_RE = re.compile(r"[0-9a-f]{24}\?expires=([0-9]{1,12})&", re.ASCII)
_ALTCHA_HEX64_RE = re.compile(r"[0-9a-f]{64}", re.ASCII)


def _no_altcha_store() -> Any:
    """Default store provider before a blueprint registers one: no shared store (fail closed)."""
    return None


#: Resolver for the shared Valkey client; `blueprints/v1/auth.py` swaps in the real one.
altcha_store_provider: Any = _no_altcha_store


def altcha_unavailable() -> ApiError:
    """Uniform fail-closed 503 for a missing secret or an unavailable shared store."""
    return ApiError("CAPTCHA service unavailable", 503, "SERVICE_UNAVAILABLE")


def altcha_signature(secret: str, salt: str, challenge: str) -> str:
    """Keyed BLAKE2b-256 MAC over `salt` AND `challenge`, domain-separated for login ALTCHA.

    Fields are length-prefixed so the (salt, challenge) split is unambiguous. The secret is
    pre-hashed so any-length config values fit BLAKE2b's 64-byte key limit. Server-issued and
    server-verified; the widget treats the signature as opaque.
    """
    key = hashlib.blake2b(secret.encode(), digest_size=32, person=_ALTCHA_MAC_PERSON).digest()
    salt_b, challenge_b = salt.encode(), challenge.encode()
    message = len(salt_b).to_bytes(4, "big") + salt_b + len(challenge_b).to_bytes(4, "big")
    return hashlib.blake2b(
        message + challenge_b, digest_size=32, key=key, person=_ALTCHA_MAC_PERSON
    ).hexdigest()


def create_altcha_challenge(
    hmac_key: str,
    *,
    max_number: int = _ALTCHA_MAX_NUMBER,
    ttl_seconds: int = _ALTCHA_TTL_SECONDS,
) -> dict[str, Any]:
    """Build an ALTCHA SHA-256 proof-of-work challenge signed with `hmac_key` (503 if unset)."""
    if not hmac_key:
        raise altcha_unavailable()
    expires = int(time.time()) + ttl_seconds
    salt = f"{secrets.token_hex(12)}?expires={expires}&"
    secret_number = secrets.randbelow(max_number + 1)
    challenge = hashlib.sha256(f"{salt}{secret_number}".encode()).hexdigest()
    return {
        "algorithm": "SHA-256",
        "challenge": challenge,
        "maxnumber": max_number,
        "salt": salt,
        "signature": altcha_signature(hmac_key, salt, challenge),
    }


async def enforce_altcha_rate_limit(store: Any, client_ip: str) -> None:
    """Per-IP fixed-window limit on challenge issuance; 429 when exceeded, 503 on store error."""
    key = f"{_ALTCHA_RATE_PREFIX}{client_ip}"
    try:
        async with store.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, ALTCHA_CHALLENGE_RATE_WINDOW_SECONDS, nx=True)
            results = await pipe.execute()
    except Exception as exc:
        logger.error("altcha rate-limit store failure: %s", type(exc).__name__)
        raise altcha_unavailable() from exc
    if int(results[0]) > ALTCHA_CHALLENGE_RATE_LIMIT:
        raise ApiError("Rate limit exceeded. Please try again later.", 429, "RATE_LIMIT_EXCEEDED")


def _decode_altcha_payload(payload_b64: str) -> dict[str, Any] | None:
    """Decode the widget payload to a dict, or None if it is not well-formed."""
    try:
        data = json.loads(base64.b64decode(payload_b64, validate=True).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


async def verify_altcha_payload(payload_b64: str, hmac_key: str, store: Any) -> bool:
    """Verify and consume (single-use) a base64 ALTCHA widget payload.

    Returns False for any invalid payload. Raises a 503 `ApiError` when the dedicated secret is
    unset or the shared single-use store errors (fail closed, never open).
    """
    if not hmac_key:
        raise altcha_unavailable()
    if not payload_b64:
        return False
    data = _decode_altcha_payload(payload_b64)
    if data is None:
        return False

    challenge = data.get("challenge")
    salt = data.get("salt")
    signature = data.get("signature")
    number = data.get("number")
    if (
        data.get("algorithm") != "SHA-256"
        or not isinstance(challenge, str)
        or not isinstance(salt, str)
        or not isinstance(signature, str)
        or isinstance(number, bool)
        or not isinstance(number, int)
        or not 0 <= number <= _ALTCHA_MAX_NUMBER
        or not _ALTCHA_HEX64_RE.fullmatch(challenge)
    ):
        return False

    salt_match = _ALTCHA_SALT_RE.fullmatch(salt)
    if salt_match is None:
        return False
    now = int(time.time())
    expires = int(salt_match.group(1))
    if not now <= expires <= now + _ALTCHA_TTL_SECONDS + _ALTCHA_EXPIRY_SLACK_SECONDS:
        return False

    expected = altcha_signature(hmac_key, salt, challenge)
    if not secrets.compare_digest(signature.encode(), expected.encode()):
        return False
    computed = hashlib.sha256(f"{salt}{number}".encode()).hexdigest()
    if not secrets.compare_digest(computed.encode(), challenge.encode()):
        return False

    ttl = expires - now + _ALTCHA_EXPIRY_SLACK_SECONDS
    try:
        first_use = await store.set(f"{_ALTCHA_USED_PREFIX}{challenge}", "1", nx=True, ex=ttl)
    except Exception as exc:
        logger.error("altcha single-use store failure: %s", type(exc).__name__)
        raise altcha_unavailable() from exc
    return bool(first_use)


async def validate_captcha(
    provider: str, secret_key: str, token: str, remote_ip: str | None, store: Any = None
) -> bool:
    """Validate a CAPTCHA token (ALTCHA, Turnstile, or reCAPTCHA). Fails closed on any error."""
    if provider == "altcha":
        if store is None:
            raise altcha_unavailable()
        return await verify_altcha_payload(token, secret_key, store)
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


async def _scopes_for(
    async_dal: Any, dal: Any, *, user: SessionUser | None, tenant_id: int | None
) -> tuple[list[str], set[str]]:
    """Derive a session's `roles` (audit-only) and OIDC scopes from DB flags + tenant role.

    The one place scope bundles are expanded, shared by the login mint and the refresh mint so a
    refreshed token reflects the user's CURRENT roles (a demotion lands within one refresh).
    """
    roles: list[str] = []
    scopes: set[str] = set(SCOPE_BUNDLES["global"]["viewer"])  # every session gets *:read
    if user is None:
        return roles, scopes
    if await is_global_admin(async_dal, dal, user_id=user.id):
        roles.extend(["admin", "super_admin"])
        scopes.update(SCOPE_BUNDLES["global"]["admin"])
    if user.is_vendor:
        roles.append("vendor")
        # `vendor:onboard` gates `POST /apps/{app_id}/versions`'s vendor path
        # (`services/vendor_bundle_authz.py`) -- a boolean flag on `hub_users` needs a real
        # scope grant at mint time, not just an audit-only `roles` entry (security.md: scopes
        # only, never role names).
        scopes.add("vendor:onboard")
    if user.is_analytics_consumer:
        # Analytics-module port (M9): `analytics:read` is the pre-existing scope name from
        # security.md's own SCOPE_BUNDLES table; `hub_users.is_analytics_consumer` (M1,
        # `060_analytics_consumer_role.sql`) needs a scope grant at mint time to satisfy
        # `blueprints/v1/analytics.py`'s platform-overview gate.
        scopes.add("analytics:read")

    if tenant_id is not None:
        ta_rows = await async_dal.select_async(
            dal((dal.tenant_admins.tenant_id == tenant_id) & (dal.tenant_admins.user_id == user.id))
        )
        if ta_rows:
            role = ta_rows.first().role
            if role == "tenant-owner":
                scopes.update(SCOPE_BUNDLES["tenant"]["admin"])
            elif role == "tenant-admin":
                scopes.update(SCOPE_BUNDLES["tenant"]["maintainer"])
    return roles, scopes


async def _issued_at_after_revocations(user: SessionUser | None) -> int | None:
    """An `iat` that no earlier revoke-all of `user` can sweep, or `None` to mint with "now".

    `iat` has one-second resolution and a per-user revocation rejects `iat <= cutoff`, so a
    login in the very second a password changed (or an admin revoked) would be rejected on its
    first use. When such a cutoff exists the new token is stamped one second past it -- never
    more than the verifier's clock-skew allowance ahead of now.
    """
    if user is None:
        return None
    floor: int | None = await get_token_revocation().aissue_floor(str(user.id))
    if floor is None:
        return None
    now = int(time.time())
    if floor + 1 <= now:
        return None
    return int(min(floor + 1, now + int(JWT_CLOCK_SKEW_SECONDS)))


def _mint_access_token(
    cfg: HubAPIConfig,
    *,
    user: SessionUser | None,
    username_override: str | None,
    roles: list[str],
    scopes: set[str],
    tenant: str,
    session_id: int | None,
    issued_at: int | None = None,
    amr: list[str] | None = None,
    auth_time: int | None = None,
) -> str:
    """Mint the short-lived (15 min) access JWT: `jti` always, `sid` when in a session.

    `amr` / `auth_time` (OIDC) describe the authentication EVENT that opened this token; a
    refresh re-mints without them, so a step-up never outlives its own 5-minute window.
    """
    # flask_core is `follow_imports = "skip"` -- create_jwt_token() is typed Any from mypy's
    # perspective; cast to the real, stable return type.
    return cast(
        str,
        create_jwt_token(
            user_id=str(user.id if user is not None else 0),
            username=(user.username if user is not None else username_override) or "",
            email=(user.email if user is not None else None) or "",
            roles=roles,
            tenant=tenant,
            scope=" ".join(sorted(scopes)),
            session_id=str(session_id) if session_id is not None else None,
            issued_at=issued_at,
            amr=amr,
            auth_time=auth_time,
        ),
    )


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
    amr: list[str] | None = None,
) -> str:
    """Open a login session and mint its first access JWT (`flask_core.auth.create_jwt_token`).

    `user` is `None` for the temp-password pending-link flow (Node's
    `createSession({userId: null, ...})`) -- `sub` becomes `"0"` in that
    case (flask_core's `create_jwt_token` requires a `user_id`; there is
    no "no subject" JWT) and no refresh token is issued: a pending-link session lives only its
    short access TTL. `tenant` is always set -- `DEFAULT_TENANT_SLUG`
    ("default") when the caller has no tenant context, per security.md
    Tenant Isolation ("there is no untenanted token").

    For a real user this also opens the `hub_sessions` row (`services/session_service.py`) and,
    inside a request, registers the HttpOnly refresh cookie -- so every login path (password /
    OAuth-SSO / passkey / registration) delivers the rotating refresh token without each route
    having to remember to. The returned access token carries that session's `sid`.

    Every login path funnels through here, so this is also the one place session issuance
    (password / OAuth-SSO / passkey) is written to the tamper-evident audit log;
    `auth_method` names the path (an identifier, never user input). The audit write happens
    BEFORE the session row is persisted and raises `AuditWriteError` on failure, so a
    session the audit log cannot account for is never issued (entitled tenants only).
    """
    roles, scopes = await _scopes_for(async_dal, dal, user=user, tenant_id=tenant_id)
    resolved_tenant = tenant_slug or DEFAULT_TENANT_SLUG

    await record_session_issued(
        user_id=user.id if user is not None else None,
        tenant_id=tenant_id,
        tenant_slug=resolved_tenant,
        auth_method=auth_method,
        pending_link=requires_oauth_link,
    )

    session_id: int | None = None
    if user is not None:
        opened = await session_service.open_session(
            async_dal,
            dal,
            cfg,
            user_id=user.id,
            tenant_slug=resolved_tenant,
            username=user.username,
            avatar_url=user.avatar_url,
        )
        session_id = opened.session_id
        if has_request_context():
            issue_refresh_cookie(opened.refresh_token, max_age=cfg.refresh_token_ttl_seconds)
        else:
            logger.debug("no request context: refresh token not delivered via cookie")

    return _mint_access_token(
        cfg,
        user=user,
        username_override=username_override,
        roles=roles,
        scopes=scopes,
        tenant=resolved_tenant,
        session_id=session_id,
        issued_at=await _issued_at_after_revocations(user),
        amr=amr,
        auth_time=int(time.time()) if amr is not None else None,
    )


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
    altcha_store: Any = None,
) -> tuple[bool, LoginResult | None]:
    """Register a local user. Returns `(requires_verification, result)`."""
    if not email or not password:
        raise bad_request("Email and password required")

    settings = await get_hub_settings_map(async_dal, dal)
    if settings.get("signup_enabled") != "true" or settings.get("email_configured") != "true":
        raise forbidden("Registration is currently disabled")

    captcha_provider = settings.get("captcha_provider", "none")
    captcha_secret = settings.get("captcha_secret_key", "")
    if captcha_provider == "altcha" or (captcha_provider != "none" and captcha_secret):
        # ALTCHA uses the DEDICATED `ALTCHA_HMAC_KEY` (never hub_settings / the app secret).
        secret_for_check = cfg.altcha_hmac_key if captcha_provider == "altcha" else captcha_secret
        if not captcha_token:
            raise bad_request("CAPTCHA verification required")
        if not await validate_captcha(
            captcha_provider,
            secret_for_check,
            captcha_token,
            remote_ip,
            altcha_store if altcha_store is not None else altcha_store_provider(),
        ):
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
    tenant_slug: str = DEFAULT_TENANT_SLUG,
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
        is_super_admin=await is_global_admin(async_dal, dal, user_id=row.id),
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
        amr=["pwd"],
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
        is_super_admin=await is_global_admin(async_dal, dal, user_id=row.id),
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


@dataclass(slots=True, frozen=True)
class RefreshedSession:
    """A renewed session: the new access token and (usually) the rotated refresh token.

    `refresh_token` is `None` for a grace-window replay (a parallel request that presented the
    token another request had rotated a moment earlier): the browser already holds the rotated
    cookie from that other response, so only a fresh access token is handed out.
    """

    access_token: str
    refresh_token: str | None
    user_id: int


async def refresh_session(
    async_dal: Any, dal: Any, cfg: HubAPIConfig, *, refresh_token: str
) -> RefreshedSession:
    """Renew a login session from its rotating refresh token (no forced logout).

    Authenticates the refresh token, re-reads the user and tenant (so a deactivated user or a
    changed role takes effect within one refresh), rotates the refresh token and mints a fresh
    short-lived access token under the same `sid`. A refresh token that was already rotated
    away -- outside the short parallel-request grace window -- revokes the whole session
    (`services/session_service.py`). Token renewals are not individually audited: the session's
    issuance, its revocations and any reuse detection are.
    """
    if not refresh_token:
        raise bad_request("Refresh token required")

    ctx = await session_service.authenticate_refresh_token(async_dal, dal, cfg, refresh_token)

    user_rows = await async_dal.select_async(dal(dal.hub_users.id == ctx.user_id))
    if not user_rows or not user_rows.first().is_active:
        await session_service.revoke_session(
            async_dal, dal, session_id=ctx.session_id, reason="user_unavailable"
        )
        raise unauthorized("Invalid session")
    row = user_rows.first()

    tenant_rows = await async_dal.select_async(
        dal((dal.tenants.slug == ctx.tenant_slug) & (dal.tenants.is_active == True))  # noqa: E712
    )
    tenant_id = tenant_rows.first().id if tenant_rows else None

    rotated = await session_service.complete_refresh(async_dal, dal, cfg, ctx)

    user = SessionUser(
        id=row.id,
        email=row.email,
        username=row.username,
        avatar_url=row.avatar_url,
        is_super_admin=await is_global_admin(async_dal, dal, user_id=row.id),
        is_vendor=bool(row.is_vendor),
        is_analytics_consumer=bool(row.is_analytics_consumer),
    )
    roles, scopes = await _scopes_for(async_dal, dal, user=user, tenant_id=tenant_id)
    access_token = _mint_access_token(
        cfg,
        user=user,
        username_override=None,
        roles=roles,
        scopes=scopes,
        tenant=ctx.tenant_slug,
        session_id=rotated.session_id,
        issued_at=await _issued_at_after_revocations(user),
    )
    return RefreshedSession(
        access_token=access_token, refresh_token=rotated.refresh_token, user_id=user.id
    )


async def logout(
    async_dal: Any,
    dal: Any,
    cfg: HubAPIConfig,
    *,
    access_token: str | None,
    refresh_token: str | None,
) -> None:
    """End the caller's login session and kill the access token they presented.

    Either credential proves the session is theirs (the access token by signature, the refresh
    token by its HMAC tag); neither present is a harmless no-op, matching the old contract.
    Revokes: the session's `sid` (every access token it ever minted, immediately), its refresh
    capability (database), and the presented access token's `jti` (covers a token with no
    session, e.g. temp-password). Never raises for a revocation-store outage -- the database half
    already stopped refresh, and the worst case is the access token's remaining <= 15 minutes.
    """
    session_ids: set[int] = set()
    tenant_slug: str | None = None

    parsed = session_service.parse_refresh_token(cfg.secret_key, refresh_token)
    if parsed is not None:
        session_ids.add(parsed.session_id)
        tenant_slug = parsed.tenant_slug

    if access_token:
        payload = verify_jwt_token(access_token)
        if payload is not None:
            jti = payload.get("jti")
            if isinstance(jti, str):
                await session_service.revoke_access_token(jti, payload.get("exp"))
            sid = payload.get("sid")
            if isinstance(sid, str) and sid.isdigit():
                session_ids.add(int(sid))
            tenant_slug = tenant_slug or payload.get("tenant")

    for session_id in sorted(session_ids):
        owner = await session_service.get_session_owner(async_dal, dal, session_id=session_id)
        outcome = await session_service.revoke_session(
            async_dal, dal, session_id=session_id, reason="logout"
        )
        if owner is not None:
            await record_session_revoked(
                user_id=owner,
                tenant_slug=tenant_slug,
                reason="logout",
                kind="session",
                sessions=outcome.sessions_revoked,
                session_id=session_id,
            )


async def logout_all(
    async_dal: Any, dal: Any, *, user_id: int
) -> session_service.RevocationOutcome:
    """Sign `user_id` out everywhere: all sessions end, all access tokens issued so far die."""
    outcome = await session_service.revoke_user_sessions(
        async_dal, dal, user_id=user_id, reason="logout_all"
    )
    await record_session_revoked(
        user_id=user_id,
        tenant_slug=None,
        reason="logout_all",
        kind="user",
        sessions=outcome.sessions_revoked,
    )
    return outcome


def _require_public_id(value: Any) -> str:
    """`communities.public_id` as text; NOT NULL since alembic 0084, so absent is a bug."""
    if not value:
        raise RuntimeError("communities row has no public_id (alembic 0084 not applied?)")
    return str(value)


@dataclass(slots=True, frozen=True)
class CommunityMembership:
    """Community membership."""

    id: int
    name: str
    display_name: str
    role: str
    license_tier: int = 1
    #: `communities.public_id` UUID: the only community id that crosses the API boundary.
    public_id: str = ""
    #: `community_roles.public_id` of the member's role (None when the member has no role row).
    role_id: str | None = None
    #: Effective scopes (sorted): full registry for global/tenant admins, else the role's scopes.
    scopes: tuple[str, ...] = ()


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


_TIER_LOOKUP_CHUNK = 500
_TIER_LOOKUP_FAILED = metrics.get_meter("waddles.hub_api.auth").create_counter(
    "waddles.auth.me.tier_lookup_failed",
    unit="1",
    description="/auth/me community-tier lookups that failed (display falls back to Free).",
)


#: Community tier codes `/auth/me` reports: 1 Free, 2 Premium (anything else displays as Free).
_TIER_CODE_FREE = 1
_KNOWN_TIER_CODES = frozenset({1, 2})


async def _community_tier_codes(
    async_dal: Any,
    dal: Any,
    community_ids: list[int],
    *,
    tenant_by_community: dict[int, int] | None = None,
) -> dict[int, int]:
    """Map community id -> active allocated tier code (1 Free / 2 Premium), chunked + batched.

    Callers pass only the CALLER'S OWN community ids (their memberships). When
    `tenant_by_community` is given, an allocation whose `tenant_id` differs from the community's
    own tenant is ignored (same rule as `resolve_effective_tier`). Communities with no active
    allocation are absent (callers treat that as Free = 1); any other stored code is
    coerced to 1 with a DEBUG log. Fails safe for display: any lookup
    error logs ERROR (exception type only), bumps `waddles_auth_me_tier_lookup_failed_total` and
    returns ``{}`` so ``/auth/me`` never fails on it -- never silently.
    """
    if not community_ids:
        return {}
    try:
        from services.schema import bind_community_tier_tables  # noqa: PLC0415 - avoid import cycle

        bind_community_tier_tables(dal)
        t = dal.community_tier_allocations
        out: dict[int, int] = {}
        for start in range(0, len(community_ids), _TIER_LOOKUP_CHUNK):
            chunk = community_ids[start : start + _TIER_LOOKUP_CHUNK]
            rows = await async_dal.select_async(
                dal(t.community_id.belongs(chunk) & (t.released_at == None)),  # noqa: E711
                t.community_id,
                t.tier_code,
                t.tenant_id,
            )
            for r in rows:
                cid = int(r.community_id)
                if tenant_by_community is not None and tenant_by_community.get(cid) != int(
                    r.tenant_id
                ):
                    continue
                code = int(r.tier_code)
                if code not in _KNOWN_TIER_CODES:
                    logger.debug("community_tier_code_unknown community_id=%d code=%d", cid, code)
                    code = _TIER_CODE_FREE
                out[cid] = code
        return out
    except Exception as exc:  # noqa: BLE001 - display hint only; never fail /auth/me
        logger.error("community_tier_lookup_failed error_type=%s", type(exc).__name__)
        _TIER_LOOKUP_FAILED.add(1, {"error_type": type(exc).__name__})
        return {}


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

    is_global = await is_global_admin(async_dal, dal, user_id=row.id)
    admin_tenants = {
        int(r.tenant_id)
        for r in await async_dal.select_async(dal(dal.tenant_admins.user_id == user_id))
    }
    state = dal.community_members.membership_state
    community_rows = await async_dal.select_async(
        dal(
            (dal.community_members.user_id == str(user_id))
            & (dal.community_members.is_active == True)  # noqa: E712 - pydal Field comparison
            & ((state == None) | (state != MembershipState.BANNED))  # noqa: E711
            & (dal.communities.id == dal.community_members.community_id)
            & (dal.communities.is_active == True)  # noqa: E712 - pydal Field comparison
        ),
        dal.communities.id,
        dal.communities.public_id,
        dal.communities.name,
        dal.communities.display_name,
        dal.communities.tenant_id,
        dal.community_members.ALL,
        dal.community_roles.ALL,
        left=dal.community_roles.on(
            dal.community_members.community_role_id == dal.community_roles.id
        ),
    )
    tiers = await _community_tier_codes(
        async_dal,
        dal,
        [int(r.communities.id) for r in community_rows],
        tenant_by_community={
            int(r.communities.id): int(r.communities.tenant_id)
            for r in community_rows
            if r.communities.tenant_id is not None
        },
    )

    def _role(r: Any) -> Any:
        """The joined `community_roles` row, or None when the member has no role row."""
        role_row = r.community_roles
        return role_row if role_row is not None and role_row.id else None

    def _scopes(r: Any) -> tuple[str, ...]:
        """Effective scopes of one membership row (admins hold the full registry)."""
        tenant_id = r.communities.tenant_id
        if is_global or (tenant_id is not None and int(tenant_id) in admin_tenants):
            return tuple(sorted(all_scopes()))
        return tuple(sorted(member_role_scopes(r.community_members, _role(r))))

    communities = [
        CommunityMembership(
            id=r.communities.id,
            name=r.communities.name,
            display_name=r.communities.display_name or r.communities.name,
            role=(_role(r).name if _role(r) else None) or "member",
            license_tier=tiers.get(int(r.communities.id), 1),
            public_id=_require_public_id(r.communities.public_id),
            role_id=str(_role(r).public_id) if _role(r) else None,
            scopes=_scopes(r),
        )
        for r in community_rows
    ]

    return CurrentUserResult(
        id=row.id,
        email=row.email,
        username=row.username,
        avatar_url=row.avatar_url,
        is_super_admin=is_global,
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

    # A credential change ends every session that was opened with the old one -- including this
    # one: the caller signs in again with the new password. Access tokens die via the per-user
    # `iat` cutoff, refresh via the database (`services/session_service.py`).
    outcome = await session_service.revoke_user_sessions(
        async_dal, dal, user_id=user_id, reason="password_changed"
    )
    await record_session_revoked(
        user_id=user_id,
        tenant_slug=None,
        reason="password_changed",
        kind="user",
        sessions=outcome.sessions_revoked,
    )


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
