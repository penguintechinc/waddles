"""Configuration for workflow_core_module"""
import logging
import os
import threading
from typing import Optional

from dotenv import load_dotenv
from flask_core.secrets import require_secret_key

load_dotenv()

logger = logging.getLogger(__name__)


class Config:
    """Workflow Core Module Configuration"""

    # Module Information
    MODULE_NAME = 'workflow_core_module'
    MODULE_VERSION = '1.0.0'
    PORT = int(os.getenv('MODULE_PORT', '8070'))
    GRPC_PORT = int(os.getenv('GRPC_PORT', '50070'))

    # Database Configuration
    DATABASE_URI = os.getenv(
        'DATABASE_URL',
        'postgresql://waddlebot:password@postgres:5432/waddlebot'
    )
    READ_REPLICA_URIS = os.getenv(
        'READ_REPLICA_URIS',
        'postgresql://waddlebot:password@postgres:5433/waddlebot'
    ).split(',')

    # Redis Configuration — build URL from parts so password comes from secret
    _redis_host = os.getenv('REDIS_HOST', 'redis')
    _redis_port = os.getenv('REDIS_PORT', '6379')
    _redis_password = os.getenv('REDIS_WORKFLOW_PASSWORD') or os.getenv('REDIS_PASSWORD', '')
    REDIS_URL = os.getenv('REDIS_URL') or (
        f"redis://:{_redis_password}@{_redis_host}:{_redis_port}/0"
        if _redis_password
        else f"redis://{_redis_host}:{_redis_port}/0"
    )

    # Credential state management
    _credentials_loaded: bool = False
    _credential_lock: threading.Lock = threading.Lock()

    # Router Service Configuration
    ROUTER_URL = os.getenv(
        'ROUTER_URL',
        'http://router-service:8000'
    )

    # License Server Configuration
    LICENSE_SERVER_URL = os.getenv(
        'LICENSE_SERVER_URL',
        'https://license.penguintech.io'
    )

    # Release Mode & Feature Flags
    RELEASE_MODE = os.getenv('RELEASE_MODE', 'false').lower() == 'true'
    FEATURE_WORKFLOWS_ENABLED = os.getenv('FEATURE_WORKFLOWS_ENABLED', 'true').lower() == 'true'

    # services/module_executor.py's GrpcModuleClientManager/ModuleExecutor
    # read this directly (not via getattr/hasattr like the per-module
    # *_GRPC_HOST settings below) -- it was previously undefined entirely,
    # so instantiating ModuleExecutor() raised AttributeError on every use
    # (undetected because that file had 0% test coverage). Defaults to
    # False: HTTP is always a safe fallback transport, while gRPC requires
    # the per-module *_GRPC_HOST settings to actually be configured.
    GRPC_ENABLED = os.getenv('GRPC_ENABLED', 'false').lower() == 'true'

    # Logging Configuration
    LOG_DIR = os.getenv('LOG_DIR', '/var/log/waddlebotlog')
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')

    # Security
    SECRET_KEY = require_secret_key()
    API_KEY = require_secret_key('API_KEY')

    # APScheduler Configuration
    SCHEDULER_TIMEZONE = os.getenv('SCHEDULER_TIMEZONE', 'UTC')
    SCHEDULER_JOB_DEFAULTS_MAX_INSTANCES = int(os.getenv('SCHEDULER_JOB_DEFAULTS_MAX_INSTANCES', '3'))
    SCHEDULER_JOB_DEFAULTS_COALESCE = os.getenv('SCHEDULER_JOB_DEFAULTS_COALESCE', 'true').lower() == 'true'

    # Workflow Execution Configuration
    MAX_CONCURRENT_WORKFLOWS = int(os.getenv('MAX_CONCURRENT_WORKFLOWS', '10'))
    WORKFLOW_TIMEOUT = int(os.getenv('WORKFLOW_TIMEOUT_SECONDS', '300'))
    WORKFLOW_MAX_RETRIES = int(os.getenv('WORKFLOW_MAX_RETRIES', '3'))
    MAX_LOOP_ITERATIONS = int(os.getenv('MAX_LOOP_ITERATIONS', '100'))
    MAX_TOTAL_OPERATIONS = int(os.getenv('MAX_TOTAL_OPERATIONS', '1000'))
    MAX_LOOP_DEPTH = int(os.getenv('MAX_LOOP_DEPTH', '10'))
    MAX_PARALLEL_NODES = int(os.getenv('MAX_PARALLEL_NODES', '10'))

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
                "WHERE platform = 'workflow_core' "
                "AND integration_type = 'bot' "
                "AND is_active = TRUE "
                "LIMIT 1"
            )
            if rows and rows[0]:
                with cls._credential_lock:
                    cls._credentials_loaded = True
                logger.info(
                    "Credentials loaded from platform_integrations for workflow_core"
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

        channel = "credentials:workflow_core:bot:refreshed"

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
