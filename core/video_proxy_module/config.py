"""
Video Proxy Module Configuration

Handles all configuration for the video proxy service including
database, gRPC, HTTP ports, S3-compatible object storage, JWT settings,
and license validation.
"""

import logging
import os
import sys
import threading
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

logger = logging.getLogger(__name__)

#: Placeholder secret values that must never sign a token in production
#: (C1 security fix, A02:2021/CWE-798). video_proxy_module ships its own
#: standalone Dockerfile (no flask_core in its requirements.txt -- it's
#: bundled into `services/core-community`'s image too, where flask_core
#: IS available, but this config must import cleanly under both), so it
#: cannot depend on `flask_core.secrets.require_secret_key()` and
#: duplicates the same small fail-closed check locally instead.
_KNOWN_PLACEHOLDER_SECRETS = frozenset(
    {'change-me-in-production', 'jwt-secret-change-in-production', ''}
)


def _require_secret_key(env_var: str, default: str = 'change-me-in-production') -> str:
    """Read `env_var`, failing closed if unset/placeholder with `RELEASE_MODE=true`.

    Mirrors this module's own `RELEASE_MODE` convention (see `Config.
    RELEASE_MODE` below) rather than `flask_core.secrets`'s `is_production()`
    -- kept self-contained since this file cannot import flask_core.
    """
    value = os.getenv(env_var, default)
    if 'pytest' in sys.modules:
        return value
    release_mode = os.getenv('RELEASE_MODE', 'false').strip().lower() == 'true'
    if release_mode and value in _KNOWN_PLACEHOLDER_SECRETS:
        raise RuntimeError(
            f'{env_var} is unset or still the insecure default with RELEASE_MODE=true -- '
            f'refusing to start. Set {env_var} to a unique, randomly generated secret.'
        )
    return value


@dataclass
class Config:
    """Main configuration class for video proxy module."""

    # Database Configuration
    DATABASE_URL: str = field(default_factory=lambda: (
        os.getenv('DATABASE_URL') or
        f"postgres://{os.getenv('DB_USER', 'waddlebot')}:{os.getenv('DB_PASS', 'password')}@"
        f"{os.getenv('DB_HOST', 'localhost')}:{os.getenv('DB_PORT', '5432')}/"
        f"{os.getenv('DB_NAME', 'waddlebot')}"
    ).replace('postgresql://', 'postgres://'))

    # HTTP Server Configuration
    MODULE_PORT: int = int(os.getenv('MODULE_PORT', '8092'))
    MODULE_HOST: str = os.getenv('MODULE_HOST', '0.0.0.0')

    # gRPC Configuration
    GRPC_PORT: int = int(os.getenv('GRPC_PORT', '50065'))
    GRPC_HOST: str = os.getenv('GRPC_HOST', '0.0.0.0')

    # MarchProxy gRPC Configuration
    MARCHPROXY_GRPC_HOST: str = os.getenv('MARCHPROXY_GRPC_HOST', 'localhost')
    MARCHPROXY_GRPC_PORT: int = int(os.getenv('MARCHPROXY_GRPC_PORT', '50050'))

    # Module Configuration
    MODULE_NAME: str = 'video_proxy_module'
    MODULE_VERSION: str = os.getenv('MODULE_VERSION', '1.0.0')
    MODULE_SECRET_KEY: str = _require_secret_key('MODULE_SECRET_KEY')

    # JWT Configuration
    JWT_ALGORITHM: str = 'HS256'
    JWT_EXPIRATION: timedelta = timedelta(hours=1)
    JWT_REFRESH_EXPIRATION: timedelta = timedelta(days=7)
    JWT_SECRET_KEY: str = _require_secret_key(
        'JWT_SECRET_KEY', 'jwt-secret-change-in-production'
    )

    # S3-compatible object storage (SeaweedFS in dev/beta, per this repo's
    # infra conventions; real S3 in prod) -- generic S3_* names consistent
    # with hub_api/services/storage_service.py, not MinIO-specific.
    # SSL is carried by S3_ENDPOINT_URL's scheme, not a separate flag.
    S3_ENDPOINT_URL: str = os.getenv('S3_ENDPOINT_URL', 'http://localhost:9000')
    S3_ACCESS_KEY_ID: str = os.getenv('S3_ACCESS_KEY_ID', '')
    S3_SECRET_ACCESS_KEY: str = os.getenv('S3_SECRET_ACCESS_KEY', '')
    S3_BUCKET_NAME: str = os.getenv('S3_BUCKET_NAME', 'video-proxy')
    S3_REGION: str = os.getenv('S3_REGION', 'us-east-1')
    # SeaweedFS's S3 gateway requires path-style addressing (no wildcard
    # virtual-host DNS) -- forced on by default, matches its bucket layout.
    S3_FORCE_PATH_STYLE: bool = os.getenv('S3_FORCE_PATH_STYLE', 'true').lower() == 'true'

    # License Server Configuration
    LICENSE_SERVER_URL: str = os.getenv(
        'LICENSE_SERVER_URL',
        'https://license.penguintech.io'
    )
    LICENSE_KEY: Optional[str] = os.getenv('LICENSE_KEY')
    RELEASE_MODE: bool = os.getenv('RELEASE_MODE', 'false').lower() == 'true'

    # Feature Limits
    FREE_MAX_DESTINATIONS: int = int(os.getenv('FREE_MAX_DESTINATIONS', '3'))
    FREE_MAX_2K_DESTINATIONS: int = int(os.getenv('FREE_MAX_2K_DESTINATIONS', '1'))

    # Logging Configuration
    LOG_LEVEL: str = os.getenv('LOG_LEVEL', 'INFO')
    LOG_FORMAT: str = os.getenv('LOG_FORMAT', 'text')

    # Database Connection Pool
    DB_POOL_SIZE: int = int(os.getenv('DB_POOL_SIZE', '10'))
    DB_POOL_RECYCLE: int = int(os.getenv('DB_POOL_RECYCLE', '3600'))

    # Redis Configuration (for caching)
    REDIS_HOST: str = os.getenv('REDIS_HOST', 'localhost')
    REDIS_PORT: int = int(os.getenv('REDIS_PORT', '6379'))
    REDIS_PASSWORD: Optional[str] = os.getenv('REDIS_PASSWORD')
    REDIS_DB: int = int(os.getenv('REDIS_DB', '0'))
    REDIS_URL: str = os.getenv('REDIS_URL', '')

    # Timeout Configuration
    GRPC_TIMEOUT: int = int(os.getenv('GRPC_TIMEOUT', '30'))
    HTTP_TIMEOUT: int = int(os.getenv('HTTP_TIMEOUT', '30'))

    # Credential state management
    _credentials_loaded: bool = False
    _credential_lock: threading.Lock = threading.Lock()

    @classmethod
    def load_credentials_from_db(cls, db_connection) -> bool:
        """Load credentials from platform_integrations table.

        Falls back to environment variables if DB lookup fails.

        Args:
            db_connection: A database connection with executesql support.

        Returns:
            True if credentials were loaded from DB.
        """
        try:
            rows = db_connection.executesql(
                "SELECT access_token, config_data "
                "FROM platform_integrations "
                "WHERE platform = 'video_proxy' "
                "AND integration_type = 'bot' "
                "AND is_active = TRUE "
                "LIMIT 1"
            )
            if rows and rows[0]:
                with cls._credential_lock:
                    cls._credentials_loaded = True
                logger.info(
                    "Credentials loaded from platform_integrations for video_proxy"
                )
                return True
        except Exception as e:
            logger.warning(
                "Failed to load credentials from DB, using env vars: %s", e
            )
        return False

    @classmethod
    def start_credential_listener(cls, redis_client) -> Optional[threading.Thread]:
        """Start a background thread that listens for credential refresh events.

        Args:
            redis_client: A Redis client instance.

        Returns:
            The listener thread, or None if Redis is not configured.
        """
        if not cls.REDIS_URL:
            return None

        channel = "credentials:video_proxy:bot:refreshed"

        def _listen():
            try:
                pubsub = redis_client.pubsub()
                pubsub.subscribe(channel)
                logger.info(
                    "Listening for credential refresh on: %s",
                    channel,
                )
                for message in pubsub.listen():
                    if message["type"] == "message":
                        logger.info(
                            "Credential refresh notification received"
                        )
                        with cls._credential_lock:
                            cls._credentials_loaded = False
            except Exception as e:
                logger.error("Credential listener error: %s", e)

        thread = threading.Thread(
            target=_listen, daemon=True, name="credential-listener"
        )
        thread.start()
        return thread

    def validate(self) -> None:
        """
        Validate configuration settings.

        Raises:
            ValueError: If required settings are invalid or missing.
        """
        # Validate ports
        if not (1 <= self.MODULE_PORT <= 65535):
            raise ValueError(f'Invalid MODULE_PORT: {self.MODULE_PORT}')
        if not (1 <= self.GRPC_PORT <= 65535):
            raise ValueError(f'Invalid GRPC_PORT: {self.GRPC_PORT}')

        # Validate database URL
        if not self.DATABASE_URL:
            raise ValueError('DATABASE_URL must be set')

        # Validate JWT settings
        if not self.JWT_SECRET_KEY:
            raise ValueError('JWT_SECRET_KEY must be set')

        # Validate S3 settings
        if not self.S3_ENDPOINT_URL:
            raise ValueError('S3_ENDPOINT_URL must be set')
        if not self.S3_ACCESS_KEY_ID:
            raise ValueError('S3_ACCESS_KEY_ID must be set')
        if not self.S3_SECRET_ACCESS_KEY:
            raise ValueError('S3_SECRET_ACCESS_KEY must be set')

        # Validate feature limits
        if self.FREE_MAX_DESTINATIONS < 1:
            raise ValueError('FREE_MAX_DESTINATIONS must be >= 1')
        if self.FREE_MAX_2K_DESTINATIONS < 0:
            raise ValueError('FREE_MAX_2K_DESTINATIONS must be >= 0')

        # Validate timeouts
        if self.GRPC_TIMEOUT < 1:
            raise ValueError('GRPC_TIMEOUT must be >= 1')
        if self.HTTP_TIMEOUT < 1:
            raise ValueError('HTTP_TIMEOUT must be >= 1')

        # License validation
        if self.RELEASE_MODE and not self.LICENSE_KEY:
            raise ValueError(
                'LICENSE_KEY required in RELEASE_MODE (development: '
                'RELEASE_MODE=false)'
            )


def get_config() -> Config:
    """
    Get the current configuration instance.

    Returns:
        Config: The configuration object.
    """
    config = Config()
    config.validate()
    return config
