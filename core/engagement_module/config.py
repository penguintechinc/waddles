import logging
import os
import threading
from typing import Optional

from flask_core.secrets import require_secret_key

logger = logging.getLogger(__name__)


class Config:
    """Engagement Module Configuration"""

    # Database Configuration - builds URL from DB_* vars if DATABASE_URL not set
    DATABASE_URL: str = (
        os.getenv("DATABASE_URL") or
        f"postgres://{os.getenv('DB_USER', 'waddlebot')}:{os.getenv('DB_PASS', 'password')}@"
        f"{os.getenv('DB_HOST', 'localhost')}:{os.getenv('DB_PORT', '5432')}/"
        f"{os.getenv('DB_NAME', 'waddlebot')}"
    ).replace('postgresql://', 'postgres://')
    DB_POOL_SIZE: int = int(os.getenv("DB_POOL_SIZE", "10"))

    # Module Configuration
    MODULE_NAME: str = "engagement_module"
    MODULE_PORT: int = int(os.getenv("MODULE_PORT", "8091"))
    MODULE_HOST: str = os.getenv("MODULE_HOST", "0.0.0.0")
    MODULE_VERSION: str = os.getenv("MODULE_VERSION", "1.0.0")
    MODULE_SECRET_KEY: str = require_secret_key("MODULE_SECRET_KEY")

    # Whether to issue CREATE TABLE DDL for the read-only tenants/
    # communities/community_members subset `flask_core.
    # bind_shared_read_tables` binds for community-scoped authz. Prod
    # NEVER migrates these -- owned by hub-api's own migrations.
    DB_MIGRATE: bool = os.getenv("DB_MIGRATE", "false").strip().lower() in {"1", "true", "yes", "on"}

    # Redis Configuration (for credential refresh notifications)
    REDIS_URL: str = os.getenv("REDIS_URL", "")

    # Credential state management
    _credentials_loaded: bool = False
    _credential_lock: threading.Lock = threading.Lock()

    # gRPC Configuration
    GRPC_PORT: int = int(os.getenv("GRPC_PORT", "50061"))
    GRPC_HOST: str = os.getenv("GRPC_HOST", "0.0.0.0")

    # JWT Configuration
    JWT_SECRET: str = require_secret_key(
        "JWT_SECRET", default="jwt-secret-key-change-in-prod"
    )
    JWT_ALGORITHM: str = os.getenv("JWT_ALGORITHM", "HS256")
    JWT_EXPIRATION_HOURS: int = int(os.getenv("JWT_EXPIRATION_HOURS", "24"))

    # Logging Configuration
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_FORMAT: str = os.getenv("LOG_FORMAT", "text")  # text or json

    # Environment
    ENVIRONMENT: str = os.getenv("ENVIRONMENT", "development")
    RELEASE_MODE: bool = os.getenv("RELEASE_MODE", "false").lower() == "true"

    # License Configuration (if applicable)
    LICENSE_KEY: Optional[str] = os.getenv("LICENSE_KEY")
    LICENSE_SERVER: str = os.getenv("LICENSE_SERVER", "https://license.penguintech.io")

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
                "WHERE platform = 'engagement' "
                "AND integration_type = 'bot' "
                "AND is_active = TRUE "
                "LIMIT 1"
            )
            if rows and rows[0]:
                with cls._credential_lock:
                    cls._credentials_loaded = True
                logger.info(
                    "Credentials loaded from platform_integrations for engagement"
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

        channel = "credentials:engagement:bot:refreshed"

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

    @classmethod
    def validate(cls) -> bool:
        """Validate configuration settings.

        MODULE_SECRET_KEY/JWT_SECRET are no longer re-checked here -- both
        now go through `flask_core.secrets.require_secret_key()` at class-
        body evaluation (module import time, above), which already raised
        `InsecureSecretError` before this method could ever run if either
        was unset/placeholder in a production-like environment (C1
        security fix; the previous local `cls.ENVIRONMENT == "production"`
        string check is superseded, not duplicated).
        """
        # Validate ports
        if not (1 <= cls.MODULE_PORT <= 65535):
            raise ValueError(f"Invalid MODULE_PORT: {cls.MODULE_PORT}")
        if not (1 <= cls.GRPC_PORT <= 65535):
            raise ValueError(f"Invalid GRPC_PORT: {cls.GRPC_PORT}")

        return True
