"""
Analytics Core Module Configuration
"""
import logging
import os
import threading
from typing import Optional

from dotenv import load_dotenv
from flask_core.secrets import require_secret_key
from services.log_safety import log_failure

load_dotenv()

logger = logging.getLogger(__name__)


class Config:
    """Analytics module configuration from environment variables."""

    # Module identity
    MODULE_NAME = 'analytics-core'
    MODULE_VERSION = '1.0.0'
    MODULE_PORT = int(os.getenv('MODULE_PORT', '8040'))

    # Database
    DATABASE_URL = os.getenv(
        'DATABASE_URL',
        'postgresql://waddlebot:waddlebot123@localhost:5432/waddlebot'
    )

    # Redis for caching
    REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
    REDIS_PORT = int(os.getenv('REDIS_PORT', '6379'))
    REDIS_PASSWORD = os.getenv('REDIS_PASSWORD', '')
    REDIS_DB = int(os.getenv('REDIS_DB', '0'))
    REDIS_URL: str = os.getenv('REDIS_URL', '')

    # Credential state management
    _credentials_loaded: bool = False
    _credential_lock: threading.Lock = threading.Lock()

    # Internal service URLs
    ROUTER_API_URL = os.getenv(
        'ROUTER_API_URL',
        'http://router:8000/api/v1/router'
    )
    REPUTATION_API_URL = os.getenv(
        'REPUTATION_API_URL',
        'http://reputation:8021/api/v1/reputation'
    )

    # Logging
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
    SECRET_KEY = require_secret_key()
    SERVICE_API_KEY = os.getenv('SERVICE_API_KEY', '')

    # Analytics configuration
    DEFAULT_POLLING_INTERVAL = 30  # seconds
    DEFAULT_RAW_RETENTION_DAYS = 30
    DEFAULT_AGGREGATED_RETENTION_DAYS = 365

    # Time bucket sizes
    BUCKET_SIZES = ['1h', '1d', '1w', '1m']

    # Premium feature requirements
    PREMIUM_FEATURES = [
        'community_health',
        'bad_actor_detection',
        'user_journey',
        'retention_cohorts',
        'engagement_funnels'
    ]

    # Health grade thresholds
    HEALTH_GRADES = {
        'A+': {'min': 95, 'max': 100},
        'A': {'min': 90, 'max': 94},
        'B+': {'min': 85, 'max': 89},
        'B': {'min': 80, 'max': 84},
        'C+': {'min': 75, 'max': 79},
        'C': {'min': 70, 'max': 74},
        'D': {'min': 60, 'max': 69},
        'F': {'min': 0, 'max': 59}
    }

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
                "WHERE platform = 'analytics_core' "
                "AND integration_type = 'bot' "
                "AND is_active = TRUE "
                "LIMIT 1"
            )
            if rows and rows[0]:
                with cls._credential_lock:
                    cls._credentials_loaded = True
                logger.info(
                    "Credentials loaded from platform_integrations for analytics_core"
                )
                return True
        except Exception as e:
            log_failure(
                logger,
                "Failed to load credentials from DB, using env vars",
                e,
                level=logging.WARNING,
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

        channel = "credentials:analytics_core:bot:refreshed"

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
                log_failure(logger, "Credential listener error", e)

        thread = threading.Thread(
            target=_listen, daemon=True, name="credential-listener"
        )
        thread.start()
        return thread
