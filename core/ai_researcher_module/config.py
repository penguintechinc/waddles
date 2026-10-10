"""
AI Researcher Module Configuration
===================================

Configuration for AI Researcher with mem0 integration:
- Multi-provider AI support (Ollama, OpenAI, Claude via WaddleAI)
- mem0 vector store (Qdrant) for semantic memory
- Batch processing for context enrichment
- Semantic caching with similarity thresholds
- Rate limiting and concurrency controls
"""

import os
from dotenv import load_dotenv
from flask_core.secrets import require_secret_key

load_dotenv()


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
    """Configuration class for AI Researcher module"""

    # ========================================================================
    # MODULE INFORMATION
    # ========================================================================
    MODULE_NAME = os.getenv('MODULE_NAME', 'ai_researcher_module')
    MODULE_VERSION = os.getenv('MODULE_VERSION', '1.0.0')
    MODULE_PORT = int(os.getenv('MODULE_PORT', '8070'))

    # ========================================================================
    # DATABASE CONFIGURATION
    # ========================================================================
    DATABASE_URL = os.getenv(
        'DATABASE_URL',
        'postgresql://waddlebot:password@localhost:5432/waddlebot'
    )

    # Redis for session management and caching
    REDIS_URL = os.getenv(
        'REDIS_URL',
        'redis://localhost:6379/0'
    )

    # ========================================================================
    # CORE API CONFIGURATION
    # ========================================================================
    CORE_API_URL = os.getenv('CORE_API_URL', 'http://router-service:8000')
    ROUTER_API_URL = os.getenv(
        'ROUTER_API_URL',
        'http://router-service:8000/api/v1/router'
    )
    HUB_API_URL = os.getenv('HUB_API_URL', 'http://hub-module:8060')

    # ========================================================================
    # AI PROVIDER CONFIGURATION
    # ========================================================================
    # Options: 'ollama' (direct) or 'waddleai' (centralized proxy)
    AI_PROVIDER = os.getenv('AI_PROVIDER', 'ollama')

    # Default model (can be overridden by provider-specific settings)
    AI_MODEL = os.getenv('AI_MODEL', 'tinyllama')

    # ========================================================================
    # OLLAMA DIRECT CONNECTION CONFIGURATION
    # ========================================================================
    OLLAMA_HOST = os.getenv('OLLAMA_HOST', 'localhost')
    OLLAMA_PORT = os.getenv('OLLAMA_PORT', '11434')
    OLLAMA_USE_TLS = os.getenv('OLLAMA_USE_TLS', 'false').lower() == 'true'
    OLLAMA_MODEL = os.getenv('OLLAMA_MODEL', 'tinyllama')
    OLLAMA_TEMPERATURE = float(os.getenv('OLLAMA_TEMPERATURE', '0.7'))
    OLLAMA_MAX_TOKENS = int(os.getenv('OLLAMA_MAX_TOKENS', '2000'))
    OLLAMA_TIMEOUT = int(os.getenv('OLLAMA_TIMEOUT', '60'))

    # Model capabilities -- describe whatever OLLAMA_MODEL is configured to.
    # OLLAMA_SUPPORTS_JSON=false (default) is the TEXT-ONLY path: no structured
    # output params are sent and prompts/parsers are plain text. Set true only
    # for a model that handles JSON. There is deliberately no tier->model map.
    OLLAMA_SUPPORTS_JSON = _env_flag('OLLAMA_SUPPORTS_JSON', False)
    # Send think=false so reasoning models answer directly instead of burning
    # the token budget on hidden reasoning (which leaves an empty completion).
    OLLAMA_DISABLE_THINKING = _env_flag('OLLAMA_DISABLE_THINKING', True)

    # SSL/TLS Configuration for Ollama
    OLLAMA_CERT_PATH = os.getenv('OLLAMA_CERT_PATH', '')
    OLLAMA_VERIFY_SSL = (
        os.getenv('OLLAMA_VERIFY_SSL', 'true').lower() == 'true'
    )

    # ========================================================================
    # WADDLEAI PROXY CONFIGURATION
    # ========================================================================
    WADDLEAI_BASE_URL = os.getenv(
        'WADDLEAI_BASE_URL',
        'http://waddleai-proxy:8000'
    )
    WADDLEAI_API_KEY = os.getenv('WADDLEAI_API_KEY', '')
    WADDLEAI_MODEL = os.getenv('WADDLEAI_MODEL', 'auto')
    WADDLEAI_TEMPERATURE = float(os.getenv('WADDLEAI_TEMPERATURE', '0.7'))
    WADDLEAI_MAX_TOKENS = int(os.getenv('WADDLEAI_MAX_TOKENS', '2000'))
    WADDLEAI_TIMEOUT = int(os.getenv('WADDLEAI_TIMEOUT', '60'))
    WADDLEAI_PREFERRED_MODEL = os.getenv('WADDLEAI_PREFERRED_MODEL', '')

    # ========================================================================
    # MEM0 / QDRANT VECTOR STORE CONFIGURATION
    # ========================================================================
    # Vector store provider for mem0 (currently only qdrant supported)
    MEM0_VECTOR_STORE = os.getenv('MEM0_VECTOR_STORE', 'qdrant')

    # Qdrant connection settings
    QDRANT_URL = os.getenv('QDRANT_URL', 'http://localhost:6333')
    QDRANT_API_KEY = os.getenv('QDRANT_API_KEY', '')
    QDRANT_COLLECTION = os.getenv('QDRANT_COLLECTION', 'ai_researcher_memory')

    # mem0 embedder configuration
    MEM0_EMBEDDER_PROVIDER = os.getenv('MEM0_EMBEDDER_PROVIDER', 'ollama')
    MEM0_EMBEDDER_MODEL = os.getenv('MEM0_EMBEDDER_MODEL', 'nomic-embed-text')

    # Vector search settings
    VECTOR_SEARCH_LIMIT = int(os.getenv('VECTOR_SEARCH_LIMIT', '10'))
    VECTOR_SCORE_THRESHOLD = float(os.getenv('VECTOR_SCORE_THRESHOLD', '0.7'))

    # ========================================================================
    # RATE LIMITING CONFIGURATION
    # ========================================================================
    # Default rate limits (requests per minute)
    RATE_LIMIT_DEFAULT = int(os.getenv('RATE_LIMIT_DEFAULT', '60'))
    RATE_LIMIT_RESEARCH = int(os.getenv('RATE_LIMIT_RESEARCH', '30'))
    RATE_LIMIT_MEMORY = int(os.getenv('RATE_LIMIT_MEMORY', '100'))

    # Global rate limits (across all users)
    GLOBAL_RATE_LIMIT_RESEARCH = int(
        os.getenv('GLOBAL_RATE_LIMIT_RESEARCH', '500')
    )
    GLOBAL_RATE_LIMIT_MEMORY = int(
        os.getenv('GLOBAL_RATE_LIMIT_MEMORY', '1000')
    )

    # ========================================================================
    # BATCH PROCESSING CONFIGURATION
    # ========================================================================
    # Context batch processing settings
    CONTEXT_BATCH_SIZE = int(os.getenv('CONTEXT_BATCH_SIZE', '1000'))
    CONTEXT_BATCH_INTERVAL = int(os.getenv('CONTEXT_BATCH_INTERVAL', '60'))

    # Enable/disable batch processing
    ENABLE_BATCH_PROCESSING = (
        os.getenv('ENABLE_BATCH_PROCESSING', 'true').lower() == 'true'
    )

    # Batch worker threads
    BATCH_WORKER_THREADS = int(os.getenv('BATCH_WORKER_THREADS', '5'))

    # ========================================================================
    # CACHE CONFIGURATION
    # ========================================================================
    # Cache TTL settings (in seconds)
    CACHE_TTL_RESEARCH = int(os.getenv('CACHE_TTL_RESEARCH', '3600'))
    CACHE_TTL_CONTEXT = int(os.getenv('CACHE_TTL_CONTEXT', '600'))
    CACHE_TTL_MEMORY = int(os.getenv('CACHE_TTL_MEMORY', '1800'))

    # Semantic cache settings
    ENABLE_SEMANTIC_CACHE = (
        os.getenv('ENABLE_SEMANTIC_CACHE', 'true').lower() == 'true'
    )
    SEMANTIC_CACHE_THRESHOLD = float(
        os.getenv('SEMANTIC_CACHE_THRESHOLD', '0.95')
    )

    # Cache key prefixes
    CACHE_PREFIX_RESEARCH = 'research'
    CACHE_PREFIX_CONTEXT = 'context'
    CACHE_PREFIX_MEMORY = 'memory'

    # ========================================================================
    # LLM CONCURRENCY AND QUEUE SETTINGS
    # ========================================================================
    # Maximum concurrent LLM API calls
    MAX_CONCURRENT_LLM_CALLS = int(
        os.getenv('MAX_CONCURRENT_LLM_CALLS', '10')
    )

    # LLM request queue size
    LLM_QUEUE_SIZE = int(os.getenv('LLM_QUEUE_SIZE', '100'))

    # LLM request timeout (seconds)
    LLM_REQUEST_TIMEOUT = int(os.getenv('LLM_REQUEST_TIMEOUT', '60'))

    # Retry configuration
    LLM_MAX_RETRIES = int(os.getenv('LLM_MAX_RETRIES', '3'))
    LLM_RETRY_DELAY = int(os.getenv('LLM_RETRY_DELAY', '2'))

    # ========================================================================
    # RESEARCH CONFIGURATION
    # ========================================================================
    # Research system prompts
    RESEARCH_SYSTEM_PROMPT = os.getenv(
        'RESEARCH_SYSTEM_PROMPT',
        'You are an AI research assistant that analyzes chat conversations '
        'and provides insightful summaries, context, and patterns. '
        'Focus on being accurate, objective, and helpful.'
    )

    # Maximum context window for research queries
    RESEARCH_MAX_CONTEXT_MESSAGES = int(
        os.getenv('RESEARCH_MAX_CONTEXT_MESSAGES', '100')
    )

    # Research response format
    RESEARCH_RESPONSE_FORMAT = os.getenv('RESEARCH_RESPONSE_FORMAT', 'markdown')

    # ========================================================================
    # MEMORY CONFIGURATION
    # ========================================================================
    # Memory retention settings
    MEMORY_RETENTION_DAYS = int(os.getenv('MEMORY_RETENTION_DAYS', '90'))
    MEMORY_AUTO_PRUNE = (
        os.getenv('MEMORY_AUTO_PRUNE', 'true').lower() == 'true'
    )

    # Memory indexing
    MEMORY_INDEX_INTERVAL = int(os.getenv('MEMORY_INDEX_INTERVAL', '300'))

    # Deduplication threshold for similar memories
    MEMORY_DEDUP_THRESHOLD = float(
        os.getenv('MEMORY_DEDUP_THRESHOLD', '0.90')
    )

    # ========================================================================
    # LOGGING CONFIGURATION
    # ========================================================================
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
    LOG_DIR = os.getenv('LOG_DIR', '/var/log/waddlebotlog')
    ENABLE_SYSLOG = (
        os.getenv('ENABLE_SYSLOG', 'false').lower() == 'true'
    )

    # AAA logging (Authentication, Authorization, Auditing)
    ENABLE_AAA_LOGGING = (
        os.getenv('ENABLE_AAA_LOGGING', 'true').lower() == 'true'
    )

    # ========================================================================
    # SECURITY CONFIGURATION
    # ========================================================================
    SECRET_KEY = require_secret_key()
    SERVICE_API_KEY = os.getenv('SERVICE_API_KEY', '')

    # Valid API keys for authentication
    API_KEYS = (
        os.getenv('VALID_API_KEYS', '').split(',')
        if os.getenv('VALID_API_KEYS')
        else []
    )

    # CORS allowlist (security.md A05/A01) -- comma-separated exact origins,
    # e.g. "https://app.waddlebot.io,https://admin.waddlebot.io". Fails
    # closed: unset/empty means no cross-origin caller is allowed, never a
    # wildcard fallback -- this module previously ran `allow_origin="*"`
    # with no auth on any route, letting any origin call it from a
    # victim's browser. See `app.py`'s `_verify_service_key()` gate, now
    # applied to every route (not just the two that already had it), for
    # the companion no-auth fix.
    CORS_ALLOWED_ORIGINS = (
        [origin.strip() for origin in os.getenv('CORS_ALLOWED_ORIGINS', '').split(',') if origin.strip()]
    )

    # ========================================================================
    # GAME LOOKUP CONFIGURATION
    # ========================================================================
    GAME_LOOKUP_ENABLED = (
        os.getenv('GAME_LOOKUP_ENABLED', 'false').lower() == 'true'
    )
    SEARXNG_URL = os.getenv('SEARXNG_URL', 'http://infra-searxng:8888')
    SEARXNG_TIMEOUT = int(os.getenv('SEARXNG_TIMEOUT', '15'))
    SEARXNG_MAX_CONCURRENT = int(os.getenv('SEARXNG_MAX_CONCURRENT', '5'))
    GAME_SEARCH_ENGINES = os.getenv(
        'GAME_SEARCH_ENGINES', 'google,duckduckgo,wikipedia,brave'
    ).split(',')
    GAME_CACHE_TTL = int(os.getenv('GAME_CACHE_TTL', '7200'))  # 2 hours
    GAME_ITEM_EXPIRY_DAYS = int(os.getenv('GAME_ITEM_EXPIRY_DAYS', '30'))
    GAME_SEARCH_MAX_RESULTS = int(os.getenv('GAME_SEARCH_MAX_RESULTS', '10'))
    GAME_SEARCH_SYSTEM_PROMPT = os.getenv(
        'GAME_SEARCH_SYSTEM_PROMPT',
        'You are a gaming expert assistant. Given web search results about a '
        'game, synthesize a clear, accurate answer. Cite sources where '
        'possible. Focus on factual in-game data (stats, locations, mechanics). '
        'If the search results are insufficient, say so honestly.'
    )

    # ========================================================================
    # PATCH NOTES TRACKER CONFIGURATION
    # ========================================================================
    PATCH_NOTES_ENABLED = (
        os.getenv('PATCH_NOTES_ENABLED', 'false').lower() == 'true'
    )
    PATCH_NOTES_CACHE_TTL = int(os.getenv('PATCH_NOTES_CACHE_TTL', '1800'))
    PATCH_NOTES_MAX_RESULTS = int(os.getenv('PATCH_NOTES_MAX_RESULTS', '10'))

    # ========================================================================
    # BUILD / LOADOUT ADVISOR CONFIGURATION
    # ========================================================================
    BUILD_ADVISOR_ENABLED = (
        os.getenv('BUILD_ADVISOR_ENABLED', 'false').lower() == 'true'
    )
    BUILD_ADVISOR_CACHE_TTL = int(
        os.getenv('BUILD_ADVISOR_CACHE_TTL', '10800')
    )  # 3 hours
    BUILD_ADVISOR_MAX_RESULTS = int(
        os.getenv('BUILD_ADVISOR_MAX_RESULTS', '10')
    )

    # ========================================================================
    # TECH TROUBLESHOOTER CONFIGURATION
    # ========================================================================
    TECH_TROUBLESHOOTER_ENABLED = (
        os.getenv('TECH_TROUBLESHOOTER_ENABLED', 'false').lower() == 'true'
    )
    TECH_TROUBLESHOOTER_CACHE_TTL = int(
        os.getenv('TECH_TROUBLESHOOTER_CACHE_TTL', '14400')
    )  # 4 hours
    TECH_TROUBLESHOOTER_MAX_RESULTS = int(
        os.getenv('TECH_TROUBLESHOOTER_MAX_RESULTS', '10')
    )

    # ========================================================================
    # PRICE / DEAL TRACKER CONFIGURATION
    # ========================================================================
    PRICE_TRACKER_ENABLED = (
        os.getenv('PRICE_TRACKER_ENABLED', 'false').lower() == 'true'
    )
    PRICE_TRACKER_CACHE_TTL = int(
        os.getenv('PRICE_TRACKER_CACHE_TTL', '900')
    )  # 15 min
    PRICE_TRACKER_MAX_RESULTS = int(
        os.getenv('PRICE_TRACKER_MAX_RESULTS', '10')
    )

    # ========================================================================
    # CLIP / HIGHLIGHT RESEARCHER CONFIGURATION
    # ========================================================================
    CLIP_RESEARCHER_ENABLED = (
        os.getenv('CLIP_RESEARCHER_ENABLED', 'false').lower() == 'true'
    )
    CLIP_RESEARCHER_CACHE_TTL = int(
        os.getenv('CLIP_RESEARCHER_CACHE_TTL', '3600')
    )  # 1 hour
    CLIP_RESEARCHER_MAX_RESULTS = int(
        os.getenv('CLIP_RESEARCHER_MAX_RESULTS', '10')
    )

    # ========================================================================
    # EVENT / TOURNAMENT LOOKUP CONFIGURATION
    # ========================================================================
    EVENT_LOOKUP_ENABLED = (
        os.getenv('EVENT_LOOKUP_ENABLED', 'false').lower() == 'true'
    )
    EVENT_LOOKUP_CACHE_TTL = int(
        os.getenv('EVENT_LOOKUP_CACHE_TTL', '1800')
    )  # 30 min
    EVENT_LOOKUP_MAX_RESULTS = int(
        os.getenv('EVENT_LOOKUP_MAX_RESULTS', '10')
    )

    # ========================================================================
    # PERFORMANCE CONFIGURATION
    # ========================================================================
    # Thread pool executor settings
    THREAD_POOL_WORKERS = int(os.getenv('THREAD_POOL_WORKERS', '20'))

    # Database connection pool
    DB_POOL_SIZE = int(os.getenv('DB_POOL_SIZE', '20'))
    DB_MAX_OVERFLOW = int(os.getenv('DB_MAX_OVERFLOW', '40'))

    # Request timeout
    REQUEST_TIMEOUT = int(os.getenv('REQUEST_TIMEOUT', '30'))

    # ========================================================================
    # HELPER METHODS
    # ========================================================================

    @classmethod
    def get_provider_config(cls):
        """Get configuration for current AI provider"""
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
                'supports_json': cls.OLLAMA_SUPPORTS_JSON,
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
    def get_qdrant_config(cls):
        """Get Qdrant vector store configuration"""
        return {
            'url': cls.QDRANT_URL,
            'api_key': cls.QDRANT_API_KEY,
            'collection': cls.QDRANT_COLLECTION,
            'search_limit': cls.VECTOR_SEARCH_LIMIT,
            'score_threshold': cls.VECTOR_SCORE_THRESHOLD
        }

    @classmethod
    def get_mem0_config(cls):
        """Get mem0 configuration"""
        return {
            'vector_store': cls.MEM0_VECTOR_STORE,
            'embedder_provider': cls.MEM0_EMBEDDER_PROVIDER,
            'embedder_model': cls.MEM0_EMBEDDER_MODEL,
            'qdrant': cls.get_qdrant_config()
        }

    @classmethod
    def get_cache_config(cls):
        """Get caching configuration"""
        return {
            'redis_url': cls.REDIS_URL,
            'ttl_research': cls.CACHE_TTL_RESEARCH,
            'ttl_context': cls.CACHE_TTL_CONTEXT,
            'ttl_memory': cls.CACHE_TTL_MEMORY,
            'semantic_cache_enabled': cls.ENABLE_SEMANTIC_CACHE,
            'semantic_threshold': cls.SEMANTIC_CACHE_THRESHOLD
        }

    @classmethod
    def get_rate_limit_config(cls):
        """Get rate limiting configuration"""
        return {
            'default': cls.RATE_LIMIT_DEFAULT,
            'research': cls.RATE_LIMIT_RESEARCH,
            'memory': cls.RATE_LIMIT_MEMORY,
            'global_research': cls.GLOBAL_RATE_LIMIT_RESEARCH,
            'global_memory': cls.GLOBAL_RATE_LIMIT_MEMORY
        }

    @classmethod
    def validate(cls):
        """Validate configuration"""
        errors = []

        # Validate AI provider
        if cls.AI_PROVIDER not in ['ollama', 'waddleai']:
            errors.append(
                f"Invalid AI_PROVIDER: {cls.AI_PROVIDER}. "
                f"Must be 'ollama' or 'waddleai'"
            )

        # Validate Ollama configuration
        if cls.AI_PROVIDER == 'ollama':
            if not cls.OLLAMA_HOST:
                errors.append(
                    "OLLAMA_HOST is required when AI_PROVIDER='ollama'"
                )
            if not cls.OLLAMA_PORT:
                errors.append(
                    "OLLAMA_PORT is required when AI_PROVIDER='ollama'"
                )

        # Validate WaddleAI configuration
        if cls.AI_PROVIDER == 'waddleai':
            if not cls.WADDLEAI_BASE_URL:
                errors.append(
                    "WADDLEAI_BASE_URL is required when "
                    "AI_PROVIDER='waddleai'"
                )
            if not cls.WADDLEAI_API_KEY:
                errors.append(
                    "WADDLEAI_API_KEY is required when "
                    "AI_PROVIDER='waddleai'"
                )
            if cls.WADDLEAI_API_KEY and not cls.WADDLEAI_API_KEY.startswith('wa-'):
                errors.append("WADDLEAI_API_KEY must start with 'wa-'")

        # Validate vector store configuration
        if cls.MEM0_VECTOR_STORE == 'qdrant':
            if not cls.QDRANT_URL:
                errors.append("QDRANT_URL is required when using Qdrant")

        # Validate numeric ranges
        if cls.OLLAMA_TEMPERATURE < 0 or cls.OLLAMA_TEMPERATURE > 2:
            errors.append("OLLAMA_TEMPERATURE must be between 0 and 2")
        if cls.OLLAMA_MAX_TOKENS < 1:
            errors.append("OLLAMA_MAX_TOKENS must be positive")
        if cls.SEMANTIC_CACHE_THRESHOLD < 0 or cls.SEMANTIC_CACHE_THRESHOLD > 1:
            errors.append("SEMANTIC_CACHE_THRESHOLD must be between 0 and 1")
        if cls.VECTOR_SCORE_THRESHOLD < 0 or cls.VECTOR_SCORE_THRESHOLD > 1:
            errors.append("VECTOR_SCORE_THRESHOLD must be between 0 and 1")

        # Validate game lookup configuration
        if cls.GAME_LOOKUP_ENABLED:
            if not cls.SEARXNG_URL:
                errors.append(
                    "SEARXNG_URL is required when "
                    "GAME_LOOKUP_ENABLED=true"
                )

        # Validate batch processing settings
        if cls.CONTEXT_BATCH_SIZE < 1:
            errors.append("CONTEXT_BATCH_SIZE must be positive")
        if cls.CONTEXT_BATCH_INTERVAL < 1:
            errors.append("CONTEXT_BATCH_INTERVAL must be positive")

        # Validate concurrency settings
        if cls.MAX_CONCURRENT_LLM_CALLS < 1:
            errors.append("MAX_CONCURRENT_LLM_CALLS must be positive")
        if cls.LLM_QUEUE_SIZE < 1:
            errors.append("LLM_QUEUE_SIZE must be positive")

        if errors:
            raise ValueError(f"Configuration errors: {', '.join(errors)}")

        return True
