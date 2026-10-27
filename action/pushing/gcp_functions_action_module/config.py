"""
Configuration module for GCP Functions Action Module.
Loads configuration from environment variables with fallback to
platform_integrations database table for credentials.
"""
import logging
import os
import threading
from typing import Optional

logger = logging.getLogger(__name__)


def _optional_env(name: str, default: str = "") -> str:
    """Read an optional value, rejecting comments copied from env templates."""
    value = os.getenv(name, default).strip()
    return "" if value.startswith("#") else value


class Config:
    """Configuration class for GCP Functions Action Module."""

    # GCP Configuration (optional in testing mode)
    GCP_PROJECT_ID: str = _optional_env("GCP_PROJECT_ID")
    GCP_REGION: str = _optional_env("GCP_REGION", "us-central1")
    GCP_SERVICE_ACCOUNT_KEY: str = _optional_env("GCP_SERVICE_ACCOUNT_KEY")
    GCP_SERVICE_ACCOUNT_EMAIL: str = _optional_env("GCP_SERVICE_ACCOUNT_EMAIL")

    # GCP API Configuration
    GCP_API_ENDPOINT: str = "https://cloudfunctions.googleapis.com/v2"
    GCP_API_TIMEOUT: int = int(os.getenv("GCP_API_TIMEOUT", "30"))

    # Database Configuration
    # PyDAL expects 'postgres://' not 'postgresql://'
    _raw_db_url: str = os.getenv(
        "DATABASE_URL",
        "postgres://waddlebot:password@localhost:5432/waddlebot"
    )
    DATABASE_URL: str = _raw_db_url.replace("postgresql://", "postgres://")

    # Redis Configuration (for credential refresh notifications)
    REDIS_URL: str = os.getenv("REDIS_URL", "")

    # Server Configuration
    GRPC_PORT: int = int(os.getenv("GRPC_PORT", "50061"))
    REST_PORT: int = int(os.getenv("REST_PORT", "8081"))
    MODULE_PORT: int = int(os.getenv("MODULE_PORT", "8081"))  # Alias for REST_PORT

    # Security Configuration
    MODULE_SECRET_KEY: str = os.getenv(
        "MODULE_SECRET_KEY",
        "waddlebot_gcp_functions_action_secret_change_me_in_production"
    )
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRATION_SECONDS: int = 3600
    # Service identities permitted to call this module's gRPC servicer.
    ALLOWED_SERVICES: list[str] = os.getenv(
        "ALLOWED_SERVICES", "router_module"
    ).split(",")

    # Module Information
    MODULE_NAME: str = "gcp_functions_action_module"
    MODULE_VERSION: str = "1.0.0"

    # Performance Settings
    MAX_WORKERS: int = int(os.getenv("MAX_WORKERS", "20"))
    REQUEST_TIMEOUT: int = int(os.getenv("REQUEST_TIMEOUT", "30"))
    MAX_BATCH_SIZE: int = int(os.getenv("MAX_BATCH_SIZE", "100"))

    # Function Invocation Settings
    FUNCTION_TIMEOUT: int = int(os.getenv("FUNCTION_TIMEOUT", "60"))  # seconds
    MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "3"))
    RETRY_DELAY: int = int(os.getenv("RETRY_DELAY", "1"))  # seconds

    # Logging Configuration
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_DIR: str = os.getenv("LOG_DIR", "/var/log/waddlebotlog")
    ENABLE_SYSLOG: bool = os.getenv("ENABLE_SYSLOG", "false").lower() == "true"
    SYSLOG_HOST: str = os.getenv("SYSLOG_HOST", "localhost")
    SYSLOG_PORT: int = int(os.getenv("SYSLOG_PORT", "514"))
    SYSLOG_FACILITY: str = os.getenv("SYSLOG_FACILITY", "LOCAL0")

    # Testing/Development mode - skips strict validation
    TESTING_MODE: bool = os.getenv("TESTING_MODE", "true").lower() == "true"

    # Credential state management
    _credentials_loaded: bool = False
    _credential_lock: threading.Lock = threading.Lock()

    @classmethod
    def load_credentials_from_db(cls, db_connection) -> bool:
        """Load GCP credentials from platform_integrations table.

        Falls back to environment variables if DB lookup fails.

        Args:
            db_connection: A database connection with executesql support.

        Returns:
            True if credentials were loaded from DB.
        """
        try:
            rows = db_connection.executesql(
                "SELECT client_id, client_secret, access_token, config_data "
                "FROM platform_integrations "
                "WHERE platform = 'gcp' "
                "AND integration_type = 'bot' "
                "AND is_active = TRUE "
                "LIMIT 1"
            )
            if rows and rows[0]:
                row = rows[0]
                with cls._credential_lock:
                    if row[0]:
                        cls.GCP_SERVICE_ACCOUNT_EMAIL = row[0]
                    if row[1]:
                        cls.GCP_SERVICE_ACCOUNT_KEY = row[1]
                    if row[3]:  # config_data may contain project_id
                        import json
                        try:
                            config = json.loads(row[3])
                            if 'project_id' in config:
                                cls.GCP_PROJECT_ID = config['project_id']
                        except (json.JSONDecodeError, ValueError):
                            pass
                    cls._credentials_loaded = True
                logger.info(
                    "GCP credentials loaded from platform_integrations"
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

        channel = "credentials:gcp:bot:refreshed"

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
                            "Credential refresh notification received, "
                            "reloading credentials"
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
    def validate(cls) -> None:
        """Validate required configuration."""
        if cls.TESTING_MODE:
            # Lenient validation for testing - only DATABASE_URL required
            if not cls.DATABASE_URL:
                raise ValueError("DATABASE_URL is required")
            return

        # Strict validation for production
        if not cls.GCP_PROJECT_ID:
            raise ValueError("GCP_PROJECT_ID is required")
        if not cls.GCP_SERVICE_ACCOUNT_KEY:
            raise ValueError("GCP_SERVICE_ACCOUNT_KEY is required")
        if not cls.DATABASE_URL:
            raise ValueError("DATABASE_URL is required")
        if not cls.MODULE_SECRET_KEY or cls.MODULE_SECRET_KEY == "waddlebot_gcp_functions_action_secret_change_me_in_production":
            raise ValueError("MODULE_SECRET_KEY must be set to a secure value in production")

    @classmethod
    def to_dict(cls) -> dict:
        """Convert configuration to dictionary (excluding secrets)."""
        return {
            "module_name": cls.MODULE_NAME,
            "module_version": cls.MODULE_VERSION,
            "gcp_project": cls.GCP_PROJECT_ID,
            "gcp_region": cls.GCP_REGION,
            "grpc_port": cls.GRPC_PORT,
            "rest_port": cls.REST_PORT,
            "max_workers": cls.MAX_WORKERS,
            "max_batch_size": cls.MAX_BATCH_SIZE,
            "log_level": cls.LOG_LEVEL,
        }
