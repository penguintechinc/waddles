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

#: Slug of the tenant every pre-Task-0.4 token and every single-tenant
#: (Free/Professional, capped) deployment resolves to. Matches
#: `tenants.slug = 'global'` seeded by migration 058 -- not a bypass, the
#: identical tenant-scoping code in tenancy.py runs for it with N=1. See
#: security.md Tenant Isolation.
DEFAULT_TENANT_SLUG = "global"

#: OIDC scope guarding tenant-admin management of enterprise SSO connections
#: (SAML 2.0 / OIDC / Google). Declared by the `auth.sso_saml` /
#: `auth.sso_google` feature contracts (`core_platform_module.features`) and
#: enforced by `hub_api/blueprints/v1/sso.py`. Granted to the tenant `admin`
#: bundle below; global admins already hold it via the `*:admin` wildcard.
SCOPE_SSO_ADMIN = "auth.sso:admin"

# TODO(tenancy-migration, tracking: v3.0.x Task 0.4): tokens minted before
# this cutoff predate the mandatory `tenant` claim and are treated as
# DEFAULT_TENANT_SLUG by verify_jwt_token() below. create_jwt_token() has
# required `tenant` since this change landed, so any token issued *after*
# the cutoff that is still missing the claim is rejected outright, not
# defaulted. Extend only with explicit sign-off -- a permanently open
# cutoff is the untenanted backdoor in a different shape.
TENANT_CLAIM_MIGRATION_CUTOFF = datetime(2026, 11, 26, tzinfo=timezone.utc)


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
    expiration_hours: int = 24,
    teams: list[str] | None = None,
    issuer: str = DEFAULT_JWT_ISSUER,
    audience: str = DEFAULT_JWT_AUDIENCE,
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
        expiration_hours: Token expiration in hours
        teams: Team/OU slugs the subject belongs to (security.md JWT
            Claims). Empty list by default -- like `scope`, always present
            in the payload rather than omitted when the caller has no team
            data to attach yet.
        issuer: `iss` claim; defaults to `DEFAULT_JWT_ISSUER`.
        audience: `aud` claim; defaults to `DEFAULT_JWT_AUDIENCE`.

    Returns:
        JWT token string

    Raises:
        ValueError: If tenant is empty -- there is no untenanted token.
    """
    if not tenant:
        raise ValueError(
            "tenant is mandatory on every JWT (security.md Tenant Isolation) -- "
            "pass DEFAULT_TENANT_SLUG for single-tenant deployments, never empty"
        )

    now = datetime.utcnow()
    expiration = now + timedelta(hours=expiration_hours)

    payload = {
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
        'type': 'access'
    }

    token = jwt.encode(payload, secret_key, algorithm='HS256')

    logger.info(f"JWT token created for user {username} (tenant={tenant}, expires in {expiration_hours}h)")

    return token


def verify_jwt_token(
    token: str,
    secret_key: str,
    *,
    issuer: str = DEFAULT_JWT_ISSUER,
    audience: str = DEFAULT_JWT_AUDIENCE,
) -> Optional[Dict[str, Any]]:
    """
    Verify and decode JWT token.

    Rejects tokens with no `tenant` claim, per security.md Tenant Isolation
    -- except during the bounded migration window (TENANT_CLAIM_MIGRATION_CUTOFF),
    where a legacy token (issued before the cutoff, before this claim
    existed) is defaulted to DEFAULT_TENANT_SLUG rather than rejected. This
    fallback is time-bounded, not a permanent bypass: a claim missing on a
    token issued after the cutoff is rejected outright.

    Also rejects a token whose `iss`/`aud` claim is PRESENT but does not
    match `issuer`/`audience` (security.md JWT Claims; cross-service replay
    / audience-confusion). Absence of either claim alone is not rejected --
    mirroring the bounded tenant-claim handling above, a token minted before
    `create_jwt_token()` started emitting `iss`/`aud` still verifies (it
    naturally drains out within the JWT's max-24h lifetime rather than
    requiring a second timed migration cutoff).

    Args:
        token: JWT token string
        secret_key: JWT secret key
        issuer: Expected `iss` claim.
        audience: Expected `aud` claim.

    Returns:
        Decoded token payload (always carrying a `tenant` key on success),
        or None if invalid, expired, missing a mandatory claim, or carrying
        a mismatched `iss`/`aud`.
    """
    try:
        # verify_aud=False: PyJWT auto-rejects a payload that carries a
        # non-empty `aud` claim when `audience=` isn't passed to decode()
        # -- every token minted after this fix always carries one. The
        # explicit mismatch check below (not PyJWT's built-in one) is what
        # lets an `aud`-less legacy token still verify.
        payload = jwt.decode(
            token, secret_key, algorithms=['HS256'], options={'verify_aud': False}
        )

        # Required-claims enforcement (security.md JWT Claims): a token
        # missing `sub`/`iat`/`exp` previously reached the exp comparison
        # below and raised an uncaught KeyError (a 500, not a clean 401/403)
        # instead of failing closed here.
        for required_claim in ('sub', 'iat', 'exp'):
            if required_claim not in payload:
                logger.error(
                    f"JWT missing mandatory claim '{required_claim}' -- rejecting",
                    extra={
                        'event_type': 'AUTH',
                        'action': 'verify_jwt_token',
                        'result': 'FAILURE'
                    }
                )
                return None

        # Check expiration. Timezone-aware on both sides -- the previous
        # `fromtimestamp(exp) < utcnow()` compared local-time-interpreted
        # exp against naive-UTC now, which falsely expired short-lived
        # tokens in any timezone behind UTC (and the inverse security bug --
        # falsely valid past real expiry -- ahead of UTC). Found while
        # adding the tenant-claim check below; fixed in place since a
        # broken expiry check undermines everything else in this function.
        if datetime.fromtimestamp(payload['exp'], tz=timezone.utc) < datetime.now(timezone.utc):
            logger.warning("JWT token expired")
            return None

        token_iss = payload.get('iss')
        if token_iss is not None and token_iss != issuer:
            logger.error(
                f"JWT issuer mismatch (got {token_iss!r}, expected {issuer!r}) -- rejecting",
                extra={
                    'event_type': 'AUTH',
                    'action': 'verify_jwt_token',
                    'result': 'FAILURE'
                }
            )
            return None

        token_aud = payload.get('aud')
        if token_aud is not None and token_aud != audience:
            logger.error(
                f"JWT audience mismatch (got {token_aud!r}, expected {audience!r}) -- rejecting",
                extra={
                    'event_type': 'AUTH',
                    'action': 'verify_jwt_token',
                    'result': 'FAILURE'
                }
            )
            return None

        if not payload.get('tenant'):
            issued_at = datetime.fromtimestamp(payload['iat'], tz=timezone.utc)
            if issued_at < TENANT_CLAIM_MIGRATION_CUTOFF:
                logger.warning(
                    f"JWT missing tenant claim -- applying migration-window "
                    f"default tenant fallback (cutoff {TENANT_CLAIM_MIGRATION_CUTOFF.isoformat()})",
                    extra={
                        'event_type': 'AUTH',
                        'action': 'verify_jwt_token',
                        'result': 'DEFAULT_TENANT_FALLBACK'
                    }
                )
                payload = {**payload, 'tenant': DEFAULT_TENANT_SLUG}
            else:
                logger.error(
                    "JWT missing mandatory tenant claim past migration cutoff -- rejecting",
                    extra={
                        'event_type': 'AUTH',
                        'action': 'verify_jwt_token',
                        'result': 'FAILURE'
                    }
                )
                return None

        return payload

    except jwt.ExpiredSignatureError:
        logger.warning("JWT token expired")
        return None
    except jwt.InvalidTokenError as e:
        logger.error(f"Invalid JWT token: {e}")
        return None


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
