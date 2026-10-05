"""Calendar Module Configuration."""
import logging
import os
import threading
from typing import Optional

from dotenv import load_dotenv
from flask_core.secrets import require_secret_key

load_dotenv()

logger = logging.getLogger(__name__)


class Config:  # noqa: E302
    """Configuration class for calendar interaction module."""

    MODULE_NAME = 'calendar_interaction_module'
    MODULE_VERSION = '2.0.0'
    MODULE_PORT = int(os.getenv('MODULE_PORT', '8030'))
    DATABASE_URL = os.getenv(
        'DATABASE_URL',
        'postgresql://waddlebot:password@localhost:5432/waddlebot'
    )
    CORE_API_URL = os.getenv(
        'CORE_API_URL',
        'http://router-service:8000'
    )
    ROUTER_API_URL = os.getenv(
        'ROUTER_API_URL',
        'http://router-service:8000/api/v1/router'
    )
    LABELS_API_URL = os.getenv(
        'LABELS_API_URL',
        'http://labels-core-service:8025'
    )
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
    SECRET_KEY = require_secret_key()

    # Service-to-service auth: hub-api's `EventCalendarProxyClient`
    # (hub_api/services/event_calendar_proxy.py) is the only legitimate
    # caller of this module and sends this key as `X-API-Key` alongside
    # the post-JWT `X-User-Context` it builds. Empty default is
    # intentional -- `flask_core.auth.verify_service_key` fails CLOSED
    # (rejects every request) when the expected key is falsy, so an
    # unconfigured deployment never silently accepts unauthenticated
    # calendar requests.
    SERVICE_API_KEY = os.getenv('SERVICE_API_KEY', '')

    # Redis Configuration (for credential refresh notifications)
    REDIS_URL: str = os.getenv('REDIS_URL', '')

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
                "WHERE platform = 'calendar_interaction' "
                "AND integration_type = 'bot' "
                "AND is_active = TRUE "
                "LIMIT 1"
            )
            if rows and rows[0]:
                with cls._credential_lock:
                    cls._credentials_loaded = True
                logger.info(
                    "Credentials loaded from platform_integrations for calendar_interaction"
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

        channel = "credentials:calendar_interaction:bot:refreshed"

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
