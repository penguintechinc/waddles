"""
Flask-Security-Too and OAuth Integration
=========================================

Provides comprehensive authentication and authorization:
- User management with Flask-Security-Too
- Multi-provider OAuth (Twitch, Discord, Slack)
- JWT token generation and validation
- Role-based access control (RBAC)
"""

# Flask-Security imports for future use in full auth setup
# from flask_security import Security, SQLAlchemyUserDatastore, UserMixin, RoleMixin
# from flask_security.utils import hash_password, verify_password
from authlib.integrations.flask_client import OAuth
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta, timezone
import jwt
import os
import secrets
import logging
import time

from .jwt_hardening import (
    MAX_JWT_LIFETIME_SECONDS,
    OUTCOME_OK,
    REASON_EXPIRED,
    REASON_INVALID_CLAIM,
    REASON_LIFETIME_EXCEEDED,
    REASON_NO_KEY,
    REASON_REVOCATION_UNAVAILABLE,
    REASON_REVOKED,
    VERIFIER_PLATFORM_HS256,
    JwtRejection,
    classify_decode_error,
    inspect_header,
    is_valid_kid,
    log_rejection,
    record_verification,
)
from .token_revocation import (
    CHECK_UNAVAILABLE_REJECTED,
    MAX_IDENTIFIER_LENGTH,
    get_token_revocation,
    is_valid_identifier,
)

logger = logging.getLogger(__name__)

#: Default `iss`/`aud` claims (security.md JWT Claims: both mandatory on
#: every token). Every flask_core-based service verifies tokens with
#: `verify_jwt_token()` against the same shared HS256 `SECRET_KEY` --
#: there is one platform-wide issuer and one platform-wide audience, not a
#: distinct value per one of the ~47 services, so both default to a fixed
#: constant rather than requiring every caller to supply a per-service
#: value just to keep minting tokens. Overridable via env for deployments
#: that split identity issuance from token consumption.
DEFAULT_JWT_ISSUER = os.getenv("JWT_ISSUER", "waddlebot")
DEFAULT_JWT_AUDIENCE = os.getenv("JWT_AUDIENCE", "waddlebot-services")

#: The ONE algorithm the platform verifier accepts and the minter uses (RFC
#: 8725 one-alg-per-verifier). H-2 Phase 0 only hardens HS256; the ES256/JWKS
#: cutover adds a *second verifier* with its own single algorithm, it does not
#: widen this list.
PLATFORM_JWT_ALGORITHM = "HS256"

#: `kid` stamped into every minted HS256 header (forward-compat for the JWKS/ES256
#: phase, where `kid` selects the verification key). Rotation of the shared
#: secret bumps this value. Validated at import so a malformed `JWT_KID` stops the
#: service at startup instead of minting tokens every verifier would refuse.
DEFAULT_JWT_KID = os.getenv("JWT_KID") or "hs256-v1"  # empty (e.g. a blank Helm value) = unset
if not is_valid_kid(DEFAULT_JWT_KID):
    raise ValueError("JWT_KID must match [A-Za-z0-9_][A-Za-z0-9_.:-]{0,63}")

#: Claims every platform token must carry (security.md JWT Claims; MED-5). `scope`
#: may be the empty string (no scopes granted) but must be present.
REQUIRED_JWT_CLAIMS = ("sub", "iss", "aud", "iat", "exp", "scope", "tenant")

#: Allowed `iat`/`nbf` clock skew between the minting and verifying pod -- matches
#: `service_jwt.CLOCK_SKEW_SECONDS`. NOT applied to `exp`: expiry stays strict to the
#: second, exactly as before this change (see `_verify_platform_token`).
JWT_CLOCK_SKEW_SECONDS = 30

#: User access-token lifetime window (security.md: short-lived access tokens; the H-2 plan's
#: 5-15 minute target). Access tokens are renewed by the refresh-token flow, never by running
#: long: `JWT_ACCESS_TTL_SECONDS` may tune the default *inside* this window and nothing outside
#: it is accepted -- a mis-set value stops the service at import instead of minting tokens
#: that live for hours.
MIN_ACCESS_TOKEN_TTL_SECONDS = 300
MAX_ACCESS_TOKEN_TTL_SECONDS = 900


def _load_access_token_ttl(raw: Optional[str]) -> int:
    """`JWT_ACCESS_TTL_SECONDS` (blank == unset == the 15-minute maximum), range-checked."""
    if raw is None or not raw.strip():
        return MAX_ACCESS_TOKEN_TTL_SECONDS
    try:
        ttl = int(raw)
    except ValueError as exc:
        raise ValueError("JWT_ACCESS_TTL_SECONDS must be an integer number of seconds") from exc
    if not MIN_ACCESS_TOKEN_TTL_SECONDS <= ttl <= MAX_ACCESS_TOKEN_TTL_SECONDS:
        raise ValueError(
            f"JWT_ACCESS_TTL_SECONDS must be within "
            f"[{MIN_ACCESS_TOKEN_TTL_SECONDS}, {MAX_ACCESS_TOKEN_TTL_SECONDS}]"
        )
    return ttl


#: Lifetime `create_jwt_token` gives a token that names no explicit expiry.
ACCESS_TOKEN_TTL_SECONDS = _load_access_token_ttl(os.getenv("JWT_ACCESS_TTL_SECONDS"))

#: Slug of the platform's default tenant (`tenants.slug = 'global'`, seeded by
#: migration 058) -- single-tenant (Free/Professional, capped) deployments
#: *mint* tokens for it, and tenancy.py resolves it with the identical
#: tenant-scoping code as every other tenant. It is NOT a verification
#: fallback: a token with no `tenant` claim is rejected, never defaulted to
#: this slug. See security.md Tenant Isolation.
DEFAULT_TENANT_SLUG = "global"

#: OIDC scope guarding tenant-admin management of enterprise SSO connections
#: (SAML 2.0 / OIDC / Google). Declared by the `auth.sso_saml` /
#: `auth.sso_google` feature contracts (`core_platform_module.features`) and
#: enforced by `hub_api/blueprints/v1/sso.py`. Granted to the tenant `admin`
#: bundle below; global admins already hold it via the `*:admin` wildcard.
SCOPE_SSO_ADMIN = "auth.sso:admin"


@dataclass(slots=True)
class OAuthProvider:
    """OAuth provider configuration"""
    name: str
    client_id: str
    client_secret: str
    authorize_url: str
    access_token_url: str
    userinfo_url: str
    client_kwargs: Dict[str, Any] = field(default_factory=dict)
    scope: str = "openid profile email"


# OAuth provider configurations
OAUTH_PROVIDERS = {
    "twitch": OAuthProvider(
        name="twitch",
        client_id="",  # Set from environment
        client_secret="",
        authorize_url="https://id.twitch.tv/oauth2/authorize",
        access_token_url="https://id.twitch.tv/oauth2/token",
        userinfo_url="https://api.twitch.tv/helix/users",
        client_kwargs={"scope": "user:read:email"},
        scope="user:read:email"
    ),
    "discord": OAuthProvider(
        name="discord",
        client_id="",  # Set from environment
        client_secret="",
        authorize_url="https://discord.com/api/oauth2/authorize",
        access_token_url="https://discord.com/api/oauth2/token",
        userinfo_url="https://discord.com/api/users/@me",
        client_kwargs={"scope": "identify email"},
        scope="identify email"
    ),
    "slack": OAuthProvider(
        name="slack",
        client_id="",  # Set from environment
        client_secret="",
        authorize_url="https://slack.com/oauth/v2/authorize",
        access_token_url="https://slack.com/api/oauth.v2.access",
        userinfo_url="https://slack.com/api/users.identity",
        client_kwargs={"scope": "identity.basic identity.email"},
        scope="identity.basic identity.email"
    )
}


def setup_auth(app, dal, config: Optional[Dict[str, Any]] = None):
    """
    Configure Flask-Security-Too and OAuth providers.

    Args:
        app: Flask/Quart application
        dal: AsyncDAL database instance
        config: Optional configuration overrides

    Returns:
        Tuple of (Security, OAuth) instances
    """
    config = config or {}

    # Flask-Security-Too configuration
    app.config['SECRET_KEY'] = config.get('SECRET_KEY', secrets.token_hex(32))
    app.config['SECURITY_PASSWORD_SALT'] = config.get('PASSWORD_SALT', secrets.token_hex(32))
    app.config['SECURITY_REGISTERABLE'] = config.get('REGISTERABLE', True)
    app.config['SECURITY_SEND_REGISTER_EMAIL'] = config.get('SEND_REGISTER_EMAIL', False)
    app.config['SECURITY_TRACKABLE'] = config.get('TRACKABLE', True)
    app.config['SECURITY_PASSWORD_HASH'] = 'bcrypt'
    app.config['SECURITY_TOKEN_AUTHENTICATION_HEADER'] = 'Authorization'
    app.config['SECURITY_TOKEN_AUTHENTICATION_KEY'] = 'token'

    # Define User and Role tables
    dal.define_table(
        'auth_user',
        dal.Field('email', 'string', unique=True, notnull=True),
        dal.Field('username', 'string', unique=True, notnull=True),
        dal.Field('password', 'string', notnull=True),
        dal.Field('display_name', 'string'),
        dal.Field('primary_platform', 'string'),  # 'twitch', 'discord', 'slack'
        dal.Field('reputation_score', 'integer', default=0),
        dal.Field('is_active', 'boolean', default=True),
        dal.Field('confirmed_at', 'datetime'),
        dal.Field('last_login_at', 'datetime'),
        dal.Field('current_login_at', 'datetime'),
        dal.Field('last_login_ip', 'string'),
        dal.Field('current_login_ip', 'string'),
        dal.Field('login_count', 'integer', default=0),
        dal.Field('created_at', 'datetime', default=datetime.utcnow),
        dal.Field('updated_at', 'datetime', default=datetime.utcnow, update=datetime.utcnow)
    )

    dal.define_table(
        'auth_role',
        dal.Field('name', 'string', unique=True, notnull=True),  # e.g. 'tenant:admin'
        dal.Field('level', 'string'),  # 'global' | 'tenant' | 'community'
        dal.Field('description', 'text'),
        dal.Field('permissions', 'json'),  # List of scope strings, e.g. 'community:read'
        dal.Field('created_at', 'datetime', default=datetime.utcnow)
    )

    dal.define_table(
        'auth_user_roles',
        dal.Field('user_id', 'reference auth_user', notnull=True),
        dal.Field('role_id', 'reference auth_role', notnull=True),
        dal.Field('assigned_at', 'datetime', default=datetime.utcnow),
        dal.Field('assigned_by', 'reference auth_user')
    )

    # OAuth configuration from environment
    oauth_config = {
        'twitch': {
            'client_id': config.get('TWITCH_CLIENT_ID', ''),
            'client_secret': config.get('TWITCH_CLIENT_SECRET', '')
        },
        'discord': {
            'client_id': config.get('DISCORD_CLIENT_ID', ''),
            'client_secret': config.get('DISCORD_CLIENT_SECRET', '')
        },
        'slack': {
            'client_id': config.get('SLACK_CLIENT_ID', ''),
            'client_secret': config.get('SLACK_CLIENT_SECRET', '')
        }
    }

    # Update OAuth providers with credentials
    for provider_name, creds in oauth_config.items():
        if creds['client_id'] and creds['client_secret']:
            OAUTH_PROVIDERS[provider_name].client_id = creds['client_id']
            OAUTH_PROVIDERS[provider_name].client_secret = creds['client_secret']

    # Initialize OAuth
    oauth = OAuth(app)

    # Register OAuth providers
    for provider_name, provider in OAUTH_PROVIDERS.items():
        if provider.client_id and provider.client_secret:
            oauth.register(
                name=provider.name,
                client_id=provider.client_id,
                client_secret=provider.client_secret,
                authorize_url=provider.authorize_url,
                access_token_url=provider.access_token_url,
                userinfo_endpoint=provider.userinfo_url,
                client_kwargs=provider.client_kwargs
            )
            logger.info(f"OAuth provider '{provider_name}' registered")

    logger.info("Authentication system initialized")

    return oauth


def create_jwt_token(
    user_id: str,
    username: str,
    email: str,
    roles: List[str],
    secret_key: str,
    tenant: str,
    scope: str = "",
    expiration_hours: float | None = None,
    teams: list[str] | None = None,
    issuer: str = DEFAULT_JWT_ISSUER,
    audience: str = DEFAULT_JWT_AUDIENCE,
    kid: str = DEFAULT_JWT_KID,
    expiration_seconds: int | None = None,
    session_id: str | None = None,
    issued_at: int | None = None,
) -> str:
    """
    Create JWT token for user authentication.

    Args:
        user_id: User ID
        username: Username
        email: User email
        roles: List of role names
        secret_key: JWT secret key
        tenant: Tenant slug the token is scoped to. Mandatory -- security.md
            requires every token to carry a `tenant` claim; single-tenant
            deployments pass DEFAULT_TENANT_SLUG, not an empty/omitted value.
        scope: Space-delimited OIDC `scope` claim (SCOPE_BUNDLES-derived
            resource:action strings, e.g. "customer.account:write") --
            checked by `authz.require_scope()` at the HTTP layer. Empty by
            default (no scopes granted), never omitted from the payload, so
            downstream scope checks always see an explicit claim to parse
            rather than a missing key.
        expiration_hours: Token lifetime in hours -- for machine/service tokens that
            deliberately outlive a user access token (security.md's 1 h ceiling). Omit it
            (and `expiration_seconds`) for a user access token: it then lives
            `ACCESS_TOKEN_TTL_SECONDS` (15 minutes by default) and is renewed by the refresh
            flow. Mutually exclusive with `expiration_seconds`.
        expiration_seconds: Token lifetime in seconds (same rules as `expiration_hours`).
        issued_at: `iat` as epoch seconds, for a minter that must not be swept by a revoke-all
            from the same second (`token_revocation.TokenRevocation.issue_floor`). Must be within
            `JWT_CLOCK_SKEW_SECONDS` of now; default is now.
        session_id: `sid` claim -- the login session this token belongs to, so revoking the
            session (logout, admin revoke) rejects every access token it ever minted. Omitted
            from the payload when None (service tokens have no login session).
        teams: Team/OU slugs the subject belongs to (security.md JWT
            Claims). Empty list by default -- like `scope`, always present
            in the payload rather than omitted when the caller has no team
            data to attach yet.
        issuer: `iss` claim; defaults to `DEFAULT_JWT_ISSUER`.
        audience: `aud` claim; defaults to `DEFAULT_JWT_AUDIENCE`.
        kid: JOSE header `kid` identifying the signing key; defaults to
            `DEFAULT_JWT_KID`. Selects the verification key once the JWKS
            (ES256) phase lands; the HS256 verifier only vets its charset.

    Returns:
        JWT token string

    Every token carries a random `jti` (revocation by token id) and a lifetime of at most
    `MAX_JWT_LIFETIME_SECONDS` -- the verifier refuses anything longer-lived.

    Raises:
        ValueError: If tenant or secret_key is empty, or kid is malformed --
            there is no untenanted token, and a token signed with an empty
            secret is forgeable by anyone -- or the requested lifetime is not
            within (0, MAX_JWT_LIFETIME_SECONDS], or `session_id` is not a valid identifier.
    """
    if not tenant:
        raise ValueError(
            "tenant is mandatory on every JWT (security.md Tenant Isolation) -- "
            "pass DEFAULT_TENANT_SLUG for single-tenant deployments, never empty"
        )
    if not secret_key:
        raise ValueError(
            "secret_key is empty -- refusing to mint a token anyone could forge "
            "(the signing secret is unset or unresolved)"
        )
    if not is_valid_kid(kid):
        raise ValueError("kid must match [A-Za-z0-9_][A-Za-z0-9_.:-]{0,63}")

    if expiration_hours is not None and expiration_seconds is not None:
        raise ValueError("pass expiration_hours or expiration_seconds, not both")
    if expiration_seconds is None:
        expiration_seconds = (
            ACCESS_TOKEN_TTL_SECONDS if expiration_hours is None else int(expiration_hours * 3600)
        )
    if not 0 < expiration_seconds <= MAX_JWT_LIFETIME_SECONDS:
        raise ValueError(
            f"token lifetime must be within (0, {MAX_JWT_LIFETIME_SECONDS}] seconds "
            "(security.md: short-lived tokens; long sessions renew via the refresh flow)"
        )
    if session_id is not None and not is_valid_identifier(session_id):
        raise ValueError("session_id must match [A-Za-z0-9_][A-Za-z0-9_.:-]{0,127}")

    now = datetime.now(timezone.utc)
    if issued_at is not None:
        if abs(issued_at - now.timestamp()) > JWT_CLOCK_SKEW_SECONDS:
            raise ValueError("issued_at must be within the clock-skew allowance of now")
        now = datetime.fromtimestamp(issued_at, tz=timezone.utc)
    expiration = now + timedelta(seconds=expiration_seconds)

    payload: Dict[str, Any] = {
        'sub': user_id,
        'username': username,
        'email': email,
        'roles': roles,
        'tenant': tenant,
        'scope': scope,
        'teams': teams if teams is not None else [],
        'iss': issuer,
        'aud': audience,
        'iat': now,
        'exp': expiration,
        'jti': secrets.token_hex(16),
        'type': 'access'
    }
    if session_id is not None:
        payload['sid'] = session_id

    token = jwt.encode(
        payload, secret_key, algorithm=PLATFORM_JWT_ALGORITHM, headers={'kid': kid}
    )

    # PII-free: the subject id and tenant slug only -- never username/email/token.
    logger.info(
        "JWT token created (alg=%s kid=%s ttl_s=%s)",
        PLATFORM_JWT_ALGORITHM,
        kid,
        expiration_seconds,
        extra={
            'event_type': 'AUTH',
            'action': 'create_jwt_token',
            'result': 'SUCCESS',
            'user_id': user_id,
            'tenant': tenant,
        },
    )

    return token


def _verify_platform_token(
    token: str, secret_key: str, issuer: str, audience: str
) -> tuple[Dict[str, Any], str]:
    """
    Run every platform-token check; raise `JwtRejection` on the first failure.

    Returns the decoded payload and the (vetted) header `alg`. Kept free of
    logging/metrics so the policy is one readable sequence; the side effects
    live in `verify_jwt_token()`.
    """
    if not isinstance(secret_key, (str, bytes)) or not secret_key:
        # Deployment bug (unset/unresolved secret), not attacker input -- but an
        # empty HMAC key would otherwise *verify* tokens anyone can forge.
        raise JwtRejection(REASON_NO_KEY)

    header = inspect_header(
        token, allowed_algs=(PLATFORM_JWT_ALGORITHM,), validate_kid=True
    )

    try:
        payload: Dict[str, Any] = jwt.decode(
            token,
            secret_key,
            algorithms=[PLATFORM_JWT_ALGORITHM],
            audience=audience,
            issuer=issuer,
            leeway=JWT_CLOCK_SKEW_SECONDS,
            options={'require': list(REQUIRED_JWT_CLAIMS)},
        )
    except jwt.PyJWTError as exc:
        raise JwtRejection(classify_decode_error(exc), alg=header.alg) from exc

    # PyJWT applies `leeway` to `exp` too; expiry must not be widened by this
    # change (iat/nbf need the skew allowance, exp does not), so re-check strictly.
    if payload['exp'] <= time.time():
        raise JwtRejection(REASON_EXPIRED, alg=header.alg)

    # PyJWT's `require` only proves the claim is present and non-null; the
    # identity-bearing ones must also be the right shape. An empty `sub`/`tenant`
    # is "missing" by another name, and `authz` space-splits a string `scope`.
    for name in ('sub', 'tenant'):
        value = payload[name]
        if not isinstance(value, str) or not value.strip():
            raise JwtRejection(REASON_INVALID_CLAIM, alg=header.alg)
    if not isinstance(payload['scope'], str):
        raise JwtRejection(REASON_INVALID_CLAIM, alg=header.alg)
    # The revocation store keys on `sub`/`jti`/`sid`: bound their shape so a hostile value can
    # never reach a key, log line or metric label.
    if len(payload['sub']) > MAX_IDENTIFIER_LENGTH:
        raise JwtRejection(REASON_INVALID_CLAIM, alg=header.alg)
    for name in ('jti', 'sid'):
        if name in payload and not is_valid_identifier(payload[name]):
            raise JwtRejection(REASON_INVALID_CLAIM, alg=header.alg)

    # Short TTL, enforced where it matters: a token lived longer than the platform ceiling is
    # refused even though its signature and `exp` are fine (a mis-configured or compromised
    # minter must not be able to hand out day-long bearer tokens). Tokens minted before the
    # short-TTL rollout (24 h) are refused here -- their holders sign in once more.
    try:
        lifetime = int(payload['exp']) - int(payload['iat'])
    except (TypeError, ValueError):
        raise JwtRejection(REASON_INVALID_CLAIM, alg=header.alg) from None
    if lifetime > MAX_JWT_LIFETIME_SECONDS + JWT_CLOCK_SKEW_SECONDS:
        raise JwtRejection(REASON_LIFETIME_EXCEEDED, alg=header.alg)

    # Server-side revocation: a logged-out / admin-revoked / credential-changed token dies
    # here, before its `exp`. Last, so only a token that is otherwise acceptable costs a lookup.
    verdict = get_token_revocation().check(payload)
    if not verdict.allowed:
        reason = (
            REASON_REVOCATION_UNAVAILABLE
            if verdict.outcome == CHECK_UNAVAILABLE_REJECTED
            else REASON_REVOKED
        )
        raise JwtRejection(reason, alg=header.alg)

    return payload, header.alg


def verify_jwt_token(
    token: str,
    secret_key: str,
    *,
    issuer: str = DEFAULT_JWT_ISSUER,
    audience: str = DEFAULT_JWT_AUDIENCE,
) -> Optional[Dict[str, Any]]:
    """
    Verify and decode a platform (HS256) JWT.

    H-2 Phase 0 / MED-5 hardening (RFC 8725). Every check fails closed:

    * One algorithm per verifier: the header `alg` must be exactly
      `PLATFORM_JWT_ALGORITHM`; `alg: none` (any case), any other algorithm,
      and the key-material header parameters `jku`/`jwk`/`x5u`/`x5c`/`crit`
      are rejected before any signature work.
    * `iss` and `aud` are ENFORCED, not merely compared when present -- a token
      without them (or with the wrong ones) is rejected.
    * Every claim in `REQUIRED_JWT_CLAIMS` must be present, and `sub`/`tenant`
      must be non-empty strings. There is no default-tenant fallback: a token
      with no `tenant` is rejected, never treated as `DEFAULT_TENANT_SLUG`.
    * An empty `secret_key` is refused (a CRITICAL log, not a pass).
    * A token lived longer than `MAX_JWT_LIFETIME_SECONDS` (`exp - iat`) is refused.
    * A revoked token is refused before its `exp`: by `jti`, by login session `sid`, or by the
      per-user `iat` cutoff written by "revoke all sessions" (`token_revocation`). When the
      revocation store is unreachable the outage policy applies -- bounded grace, then fail
      closed (`docs/TOKEN_REVOCATION.md`).

    Each call emits `waddles_jwt_verifications_total{verifier,alg,outcome}` and
    a latency histogram. Logs carry only closed-vocabulary fields -- never the
    token, its claims or the JWT library's error text.

    Args:
        token: JWT token string
        secret_key: JWT secret key
        issuer: Expected `iss` claim.
        audience: Expected `aud` claim.

    Returns:
        Decoded token payload (always carrying non-empty `sub` and `tenant`),
        or None if the token is invalid for any reason above.
    """
    started = time.perf_counter()
    try:
        payload, alg = _verify_platform_token(token, secret_key, issuer, audience)
    except JwtRejection as rejection:
        record_verification(
            verifier=VERIFIER_PLATFORM_HS256,
            alg=rejection.alg,
            outcome=rejection.reason,
            started=started,
        )
        log_rejection(
            verifier=VERIFIER_PLATFORM_HS256, reason=rejection.reason, alg=rejection.alg
        )
        return None

    record_verification(
        verifier=VERIFIER_PLATFORM_HS256, alg=alg, outcome=OUTCOME_OK, started=started
    )
    logger.debug(
        "JWT verified: verifier=%s alg=%s",
        VERIFIER_PLATFORM_HS256,
        alg,
        extra={'event_type': 'AUTH', 'action': 'verify_jwt_token', 'result': 'SUCCESS'},
    )
    return payload


def create_api_key(prefix: str = "wa", length: int = 64) -> str:
    """
    Create API key with prefix.

    Args:
        prefix: API key prefix (default: 'wa' for Waddles)
        length: API key length (default: 64)

    Returns:
        API key string with format: prefix-{random_hex}
    """
    random_part = secrets.token_hex(length // 2)
    return f"{prefix}-{random_part}"


def hash_api_key(api_key: str) -> str:
    """
    Hash API key for secure storage (SHA-256).

    Args:
        api_key: Plain API key

    Returns:
        Hashed API key
    """
    import hashlib
    return hashlib.sha256(api_key.encode()).hexdigest()


async def verify_api_key_async(api_key: str, dal) -> Optional[Dict[str, Any]]:
    """
    Verify API key and return associated user information.

    Args:
        api_key: API key to verify
        dal: AsyncDAL instance

    Returns:
        User information dict or None if invalid
    """
    hashed_key = hash_api_key(api_key)

    # Query API keys table
    query = (dal.api_keys.key_hash == hashed_key) & (dal.api_keys.is_active is True)
    rows = await dal.select_async(query)

    if not rows:
        logger.warning("Invalid API key attempt")
        return None

    key_record = rows.first()

    # Check expiration
    if key_record.expires_at and key_record.expires_at < datetime.utcnow():
        logger.warning(f"Expired API key attempt: {key_record.name}")
        return None

    # Update last used timestamp
    await dal.update_async(
        dal.api_keys.id == key_record.id,
        last_used_at=datetime.utcnow()
    )

    # Get user information
    user_query = dal.auth_user.id == key_record.user_id
    user_rows = await dal.select_async(user_query)

    if not user_rows:
        logger.error(f"API key references non-existent user: {key_record.user_id}")
        return None

    user = user_rows.first()

    return {
        'user_id': user.id,
        'username': user.username,
        'email': user.email,
        'api_key_name': key_record.name,
        'permissions': key_record.permissions or []
    }


def verify_service_key(provided_key: str, expected_key: Optional[str]) -> bool:
    """
    Securely verify service API key using constant-time comparison.

    SECURITY: This function rejects requests if no key is configured to prevent
    accidental deployment without proper authentication.

    Args:
        provided_key: The key provided in the request header
        expected_key: The expected service API key from configuration

    Returns:
        True if keys match, False otherwise
    """
    if not expected_key:
        logger.error("SERVICE_API_KEY not configured - rejecting request",
                    extra={'event_type': 'AUTH', 'action': 'verify_service_key', 'result': 'FAILURE'})
        return False

    if not provided_key:
        logger.warning("No service key provided in request",
                      extra={'event_type': 'AUTH', 'action': 'verify_service_key', 'result': 'FAILURE'})
        return False

    # Use constant-time comparison to prevent timing attacks
    return secrets.compare_digest(provided_key, expected_key)


#: Per-level scope bundles -- security.md's admin/maintainer/viewer table,
#: instantiated at each of the global/tenant/community levels from
#: docs/plans/2026-08-26-v3-scbm-apps-design.md's Identity and data scoping
#: ladder. No bundle grants the unbounded '*': narrower levels restrict what
#: a broader level granted, they never expand it. Middleware checks these
#: scopes only -- never the role/bundle name.
SCOPE_BUNDLES: Dict[str, Dict[str, List[str]]] = {
    'global': {
        'admin': ['*:read', '*:write', '*:admin', '*:delete', 'settings:write', 'users:admin'],
        'maintainer': ['*:read', '*:write', 'teams:read', 'reports:read', 'analytics:read'],
        'viewer': ['*:read'],
    },
    'tenant': {
        # SECURITY (C3, A01/BOLA fix): this bundle must NEVER include
        # 'users:admin' -- that literal is reserved for platform-wide
        # super-admin gates (hub_api's `/api/v1/superadmin/users/*`,
        # `platform_config.py`, `analytics.py`, `marketplace_admin_review.py`,
        # `cookie_consent.py`, `marketplace_modules.py` -- every one of them
        # documented as "granted exactly when hub_users.is_super_admin is
        # true"). It briefly duplicated 'global'['admin']'s literal here,
        # so `auth_service.create_session_token`'s tenant-owner bundle grant
        # let ANY tenant owner satisfy those platform-only
        # `require_scope("users:admin")` gates and self-promote to platform
        # super admin -- narrower levels must restrict what a broader level
        # granted, never expand it (this bundle's own module docstring,
        # docs/plans/2026-08-26-v3-scbm-apps-design.md's scoping ladder).
        # Legitimate tenant-scoped admin/role management already has its own
        # correctly-scoped surface: `blueprints/v1/tenant.py`'s
        # `require_scope("tenant:admin")` routes (get/add/remove tenant
        # admins), unaffected by this fix. Regression test:
        # `test_tenancy.py::TestScopeBundles::
        # test_tenant_bundle_never_grants_platform_only_users_admin_scope`.
        # 'compliance.audit:admin' (enterprise audit-log read/verify/export) is listed
        # explicitly: it is NOT reachable via the `*:read` every session carries, and a
        # tenant owner may only ever see their OWN tenant's chain (tenant from the JWT).
        'admin': [
            'tenant:read', 'tenant:write', 'tenant:admin', 'tenant:delete',
            'community:create', 'community:delete', 'billing:read', 'billing:write',
            'settings:write', 'compliance.audit:admin', SCOPE_SSO_ADMIN,
        ],
        'maintainer': [
            'tenant:read', 'tenant:write', 'community:create',
            'billing:read', 'reports:read', 'analytics:read',
        ],
        'viewer': ['tenant:read', 'billing:read'],
    },
    'community': {
        'admin': [
            'community:read', 'community:write', 'community:admin', 'community:delete',
            'bot.command:admin', 'social.polls:write', 'settings:write',
        ],
        'maintainer': [
            'community:read', 'community:write', 'bot.command:admin', 'social.polls:write',
        ],
        'viewer': ['community:read', 'social.polls:read'],
    },
}


def setup_default_roles(dal):
    """
    Create default per-level scope-bundle roles if they don't exist.

    Replaces the old flat admin/community_owner/moderator/user roles -- one
    of which granted the unbounded '*' -- with admin/maintainer/viewer
    bundles at each of global/tenant/community, per security.md's bundle
    table. Role name is `{level}:{bundle}` (e.g. 'tenant:admin'); middleware
    must check the resulting scopes, never the role name.

    Args:
        dal: AsyncDAL instance
    """
    for level, bundles in SCOPE_BUNDLES.items():
        for bundle_name, scopes in bundles.items():
            role_name = f"{level}:{bundle_name}"
            existing = dal(dal.auth_role.name == role_name).select().first()
            if not existing:
                dal.auth_role.insert(
                    name=role_name,
                    level=level,
                    description=f"{bundle_name.capitalize()} scope bundle at {level} level",
                    permissions=scopes,
                )
                logger.info(f"Created default role: {role_name}")
