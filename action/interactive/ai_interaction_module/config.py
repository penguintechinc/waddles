"""
AI Interaction Module Configuration
====================================

Configuration for AI providers:
- Ollama: Direct connection with host:port and TLS support
- WaddleAI: Centralized proxy for OpenAI, Claude, MCP
- Platform integration credentials from database
"""

import logging
import os
import threading
from typing import Optional

from dotenv import load_dotenv
from flask_core.secrets import require_secret_key

load_dotenv()

logger = logging.getLogger(__name__)


def _env_flag(name: str, default: bool) -> bool:
    """Parse a boolean env var strictly: a typo'd value fails loud, never a silent default."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in ('1', 'true', 'yes', 'on'):
        return True
    if value in ('0', 'false', 'no', 'off'):
        return False
    raise ValueError(
        f"{name}={raw!r} is not a boolean (use true/false/1/0/yes/no/on/off)"
    )


class Config:
    """Configuration class for AI interaction module"""

    # Module Information
    MODULE_NAME = os.getenv('MODULE_NAME', 'ai_interaction_module')
    MODULE_VERSION = os.getenv('MODULE_VERSION', '2.0.0')
    MODULE_PORT = int(os.getenv('MODULE_PORT', '8005'))

    # Database Configuration
    DATABASE_URL = os.getenv(  # noqa: E501
        'DATABASE_URL',
        'postgresql://waddlebot:password@localhost:5432/waddlebot'
    )

    # Redis Configuration (for credential refresh notifications)
    REDIS_URL = os.getenv('REDIS_URL', '')

    # Core API Configuration
    CORE_API_URL = os.getenv('CORE_API_URL', 'http://router-service:8000')
    ROUTER_API_URL = os.getenv(  # noqa: E501
        'ROUTER_API_URL',
        'http://router-service:8000/api/v1/router'
    )

    # ========================================================================
    # AI PROVIDER SELECTION
    # ========================================================================
    # Options: 'ollama' (direct connection) or 'waddleai' (centralized proxy)
    AI_PROVIDER = os.getenv('AI_PROVIDER', 'waddleai')

    # ========================================================================
    # OLLAMA DIRECT CONNECTION CONFIGURATION
    # ========================================================================
    # Used when AI_PROVIDER='ollama' for direct Ollama connection
    OLLAMA_HOST = os.getenv('OLLAMA_HOST', 'localhost')
    OLLAMA_PORT = os.getenv('OLLAMA_PORT', '11434')
    OLLAMA_USE_TLS = os.getenv('OLLAMA_USE_TLS', 'false').lower() == 'true'
    OLLAMA_MODEL = os.getenv('OLLAMA_MODEL', 'llama3.2')
    OLLAMA_TEMPERATURE = float(os.getenv('OLLAMA_TEMPERATURE', '0.7'))
    OLLAMA_MAX_TOKENS = int(os.getenv('OLLAMA_MAX_TOKENS', '500'))
    OLLAMA_TIMEOUT = int(os.getenv('OLLAMA_TIMEOUT', '30'))
    # Send think=false so reasoning models answer directly instead of spending
    # the token budget on hidden reasoning (which leaves an empty reply).
    # This module only ever asks for plain-text chat replies -- it never sends
    # structured-output (JSON) params, so it works with text-only models.
    OLLAMA_DISABLE_THINKING = _env_flag('OLLAMA_DISABLE_THINKING', True)

    # SSL/TLS Configuration for Ollama
    OLLAMA_CERT_PATH = os.getenv(  # noqa: E501
        'OLLAMA_CERT_PATH', ''
    )  # Path to certificate file
    OLLAMA_VERIFY_SSL = (  # noqa: E501
        os.getenv('OLLAMA_VERIFY_SSL', 'true').lower() == 'true'
    )

    # ========================================================================
    # WADDLEAI PROXY CONFIGURATION
    # ========================================================================
    # Used when AI_PROVIDER='waddleai' for centralized AI routing
    WADDLEAI_BASE_URL = os.getenv(  # noqa: E501
        'WADDLEAI_BASE_URL', 'http://waddleai-proxy:8000'
    )
    WADDLEAI_API_KEY = os.getenv(  # noqa: E501
        'WADDLEAI_API_KEY', ''
    )  # wa-xxxxx format
    WADDLEAI_MODEL = os.getenv(  # noqa: E501
        'WADDLEAI_MODEL', 'auto'
    )  # 'auto' for intelligent routing
    WADDLEAI_TEMPERATURE = float(
        os.getenv('WADDLEAI_TEMPERATURE', '0.7')
    )
    WADDLEAI_MAX_TOKENS = int(
        os.getenv('WADDLEAI_MAX_TOKENS', '500')
    )
    WADDLEAI_TIMEOUT = int(
        os.getenv('WADDLEAI_TIMEOUT', '30')
    )

    # Optional: Force specific provider through WaddleAI
    WADDLEAI_PREFERRED_MODEL = os.getenv(  # noqa: E501
        'WADDLEAI_PREFERRED_MODEL', ''
    )  # e.g., 'gpt-4', 'claude-3-sonnet'

    # ========================================================================
    # SHARED AI CONFIGURATION
    # ========================================================================
    # These settings apply to whichever provider is selected
    AI_MODEL = os.getenv(  # noqa: E501
        'AI_MODEL',
        WADDLEAI_MODEL if AI_PROVIDER == 'waddleai' else OLLAMA_MODEL
    )
    AI_TEMPERATURE = float(os.getenv('AI_TEMPERATURE', '0.7'))
    AI_MAX_TOKENS = int(os.getenv('AI_MAX_TOKENS', '500'))

    # System Prompt
    SYSTEM_PROMPT = os.getenv(
        'SYSTEM_PROMPT',
        'You are a helpful chatbot assistant for a streaming community. '
        'Provide friendly, concise, and helpful responses. '
        'Keep responses under 200 characters.'
    )

    # Question Detection
    QUESTION_TRIGGERS = (
        os.getenv('QUESTION_TRIGGERS', '?').split(',')
    )
    RESPONSE_PREFIX = os.getenv('RESPONSE_PREFIX', '🤖 ')

    # Event Response Configuration
    RESPOND_TO_EVENTS = (  # noqa: E501
        os.getenv('RESPOND_TO_EVENTS', 'true').lower() == 'true'
    )
    EVENT_RESPONSE_TYPES = os.getenv(
        'EVENT_RESPONSE_TYPES',
        'subscription,follow,donation,cheer,raid,boost'
    ).split(',')

    # Context Configuration
    ENABLE_CHAT_CONTEXT = (  # noqa: E501
        os.getenv('ENABLE_CHAT_CONTEXT', 'true').lower() == 'true'
    )
    CONTEXT_HISTORY_LIMIT = int(
        os.getenv('CONTEXT_HISTORY_LIMIT', '5')
    )

    # Performance Configuration
    MAX_CONCURRENT_REQUESTS = int(
        os.getenv('MAX_CONCURRENT_REQUESTS', '10')
    )
    REQUEST_TIMEOUT = int(os.getenv('REQUEST_TIMEOUT', '30'))

    # Logging Configuration
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
    LOG_DIR = os.getenv(
        'LOG_DIR', '/var/log/waddlebotlog'
    )
    ENABLE_SYSLOG = (  # noqa: E501
        os.getenv('ENABLE_SYSLOG', 'false').lower() == 'true'
    )

    # Security
    SECRET_KEY = require_secret_key()
    API_KEYS = (  # noqa: E501
        os.getenv('VALID_API_KEYS', '').split(',')
        if os.getenv('VALID_API_KEYS')
        else []
    )

    # Credential state management
    _credentials_loaded: bool = False
    _credential_lock: threading.Lock = threading.Lock()

    @classmethod
    def load_credentials_from_db(cls, db_connection) -> bool:
        """Load WaddleAI credentials from platform_integrations table.

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
                "WHERE platform = 'waddleai' "
                "AND integration_type = 'bot' "
                "AND is_active = TRUE "
                "LIMIT 1"
            )
            if rows and rows[0]:
                row = rows[0]
                with cls._credential_lock:
                    if row[0]:
                        cls.WADDLEAI_API_KEY = row[0]
                    cls._credentials_loaded = True
                logger.info(
                    "WaddleAI credentials loaded from platform_integrations"
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

        channel = "credentials:waddleai:bot:refreshed"

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
    def get_provider_config(cls):
        """Get configuration for current provider"""
        if cls.AI_PROVIDER == 'ollama':
            return {
                'provider': 'ollama',
                'host': cls.OLLAMA_HOST,
                'port': cls.OLLAMA_PORT,
                'use_tls': cls.OLLAMA_USE_TLS,
                'model': cls.OLLAMA_MODEL,
                'temperature': cls.OLLAMA_TEMPERATURE,
                'max_tokens': cls.OLLAMA_MAX_TOKENS,
                'timeout': cls.OLLAMA_TIMEOUT,
                'cert_path': cls.OLLAMA_CERT_PATH,
                'verify_ssl': cls.OLLAMA_VERIFY_SSL,
                'disable_thinking': cls.OLLAMA_DISABLE_THINKING
            }
        elif cls.AI_PROVIDER == 'waddleai':
            return {
                'provider': 'waddleai',
                'base_url': cls.WADDLEAI_BASE_URL,
                'api_key': cls.WADDLEAI_API_KEY,
                'model': cls.WADDLEAI_MODEL,
                'temperature': cls.WADDLEAI_TEMPERATURE,
                'max_tokens': cls.WADDLEAI_MAX_TOKENS,
                'timeout': cls.WADDLEAI_TIMEOUT,
                'preferred_model': cls.WADDLEAI_PREFERRED_MODEL
            }
        else:
            raise ValueError(f"Unknown AI provider: {cls.AI_PROVIDER}")

    @classmethod
    def validate(cls):
        """Validate configuration"""
        errors = []

        # Validate provider
        if cls.AI_PROVIDER not in ['ollama', 'waddleai']:
            errors.append(  # noqa: E501
                f"Invalid AI_PROVIDER: {cls.AI_PROVIDER}. "
                f"Must be 'ollama' or 'waddleai'"
            )

        # Validate Ollama configuration
        if cls.AI_PROVIDER == 'ollama':
            if not cls.OLLAMA_HOST:
                errors.append(  # noqa: E501
                    "OLLAMA_HOST is required when AI_PROVIDER='ollama'"
                )
            if not cls.OLLAMA_PORT:
                errors.append(  # noqa: E501
                    "OLLAMA_PORT is required when AI_PROVIDER='ollama'"
                )

        # Validate WaddleAI configuration
        if cls.AI_PROVIDER == 'waddleai':
            if not cls.WADDLEAI_BASE_URL:
                errors.append(  # noqa: E501
                    "WADDLEAI_BASE_URL is required when "
                    "AI_PROVIDER='waddleai'"
                )
            if not cls.WADDLEAI_API_KEY:
                errors.append(  # noqa: E501
                    "WADDLEAI_API_KEY is required when "
                    "AI_PROVIDER='waddleai'"
                )
            if not cls.WADDLEAI_API_KEY.startswith('wa-'):
                errors.append("WADDLEAI_API_KEY must start with 'wa-'")

        # Validate common settings
        if cls.AI_TEMPERATURE < 0 or cls.AI_TEMPERATURE > 2:
            errors.append("AI_TEMPERATURE must be between 0 and 2")
        if cls.AI_MAX_TOKENS < 1:
            errors.append("AI_MAX_TOKENS must be positive")

        if errors:
            raise ValueError(f"Configuration errors: {', '.join(errors)}")

        return True
