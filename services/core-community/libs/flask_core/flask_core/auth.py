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
from datetime import datetime, timedelta
import jwt
import secrets
import logging

logger = logging.getLogger(__name__)


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
        dal.Field('name', 'string', unique=True, notnull=True),
        dal.Field('description', 'text'),
        dal.Field('permissions', 'json'),  # List of permission strings
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
    expiration_hours: int = 24
) -> str:
    """
    Create JWT token for user authentication.

    Args:
        user_id: User ID
        username: Username
        email: User email
        roles: List of role names
        secret_key: JWT secret key
        expiration_hours: Token expiration in hours

    Returns:
        JWT token string
    """
    now = datetime.utcnow()
    expiration = now + timedelta(hours=expiration_hours)

    payload = {
        'sub': user_id,
        'username': username,
        'email': email,
        'roles': roles,
        'iat': now,
        'exp': expiration,
        'type': 'access'
    }

    token = jwt.encode(payload, secret_key, algorithm='HS256')

    logger.info(f"JWT token created for user {username} (expires in {expiration_hours}h)")

    return token


def verify_jwt_token(token: str, secret_key: str) -> Optional[Dict[str, Any]]:
    """
    Verify and decode JWT token.

    Args:
        token: JWT token string
        secret_key: JWT secret key

    Returns:
        Decoded token payload or None if invalid
    """
    try:
        payload = jwt.decode(token, secret_key, algorithms=['HS256'])

        # Check expiration
        if datetime.fromtimestamp(payload['exp']) < datetime.utcnow():
            logger.warning("JWT token expired")
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


def setup_default_roles(dal):
    """
    Create default roles if they don't exist.

    Args:
        dal: AsyncDAL instance
    """
    default_roles = [
        {
            'name': 'admin',
            'description': 'Full system access',
            'permissions': ['*']
        },
        {
            'name': 'community_owner',
            'description': 'Owns and manages communities',
            'permissions': ['community:*', 'module:install', 'module:configure']
        },
        {
            'name': 'moderator',
            'description': 'Community moderator',
            'permissions': ['community:moderate', 'user:manage']
        },
        {
            'name': 'user',
            'description': 'Standard user',
            'permissions': ['profile:view', 'profile:edit']
        }
    ]

    for role_data in default_roles:
        # Check if role exists
        existing = dal(dal.auth_role.name == role_data['name']).select().first()
        if not existing:
            dal.auth_role.insert(**role_data)
            logger.info(f"Created default role: {role_data['name']}")
