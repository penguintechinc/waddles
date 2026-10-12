"""Router Module Configuration"""
import os
from urllib.parse import quote_plus
from dotenv import load_dotenv

load_dotenv()


class Config:
    MODULE_NAME = 'router_module'
    MODULE_VERSION = '2.0.0'
    MODULE_PORT = int(os.getenv('MODULE_PORT', '8000'))

    # Database configuration - build URL from components to handle special characters
    DATABASE_HOST = os.getenv('DATABASE_HOST', 'localhost')
    DATABASE_PORT = os.getenv('DATABASE_PORT', '5432')
    DATABASE_NAME = os.getenv('DATABASE_NAME', 'waddlebot')
    DATABASE_USER = os.getenv('DATABASE_USER', 'waddlebot')
    DATABASE_PASSWORD = os.getenv('DATABASE_PASSWORD', 'password')

    # Read replica configuration
    READ_REPLICA_HOST = os.getenv('READ_REPLICA_HOST', '')
    READ_REPLICA_PORT = os.getenv('READ_REPLICA_PORT', '5432')

    REDIS_HOST = os.getenv('REDIS_HOST', 'redis')
    REDIS_PORT = int(os.getenv('REDIS_PORT', '6379'))
    REDIS_PASSWORD = os.getenv('REDIS_PASSWORD', '')
    REDIS_DB = int(os.getenv('REDIS_DB', '0'))
    SESSION_TTL = int(os.getenv('SESSION_TTL', '3600'))
    SESSION_PREFIX = os.getenv('SESSION_PREFIX', 'waddlebot:session:')

    ROUTER_MAX_WORKERS = int(os.getenv('ROUTER_MAX_WORKERS', '20'))
    ROUTER_MAX_CONCURRENT = int(os.getenv('ROUTER_MAX_CONCURRENT', '100'))
    ROUTER_REQUEST_TIMEOUT = int(os.getenv('ROUTER_REQUEST_TIMEOUT', '30'))
    ROUTER_DEFAULT_RATE_LIMIT = int(os.getenv('ROUTER_DEFAULT_RATE_LIMIT', '60'))

    ROUTER_COMMAND_CACHE_TTL = int(os.getenv('ROUTER_COMMAND_CACHE_TTL', '300'))
    ROUTER_ENTITY_CACHE_TTL = int(os.getenv('ROUTER_ENTITY_CACHE_TTL', '600'))

    AWS_REGION = os.getenv('AWS_REGION', 'us-east-1')
    AWS_ACCESS_KEY_ID = os.getenv('AWS_ACCESS_KEY_ID', '')
    AWS_SECRET_ACCESS_KEY = os.getenv('AWS_SECRET_ACCESS_KEY', '')
    LAMBDA_FUNCTION_PREFIX = os.getenv('LAMBDA_FUNCTION_PREFIX', 'waddlebot-')

    OPENWHISK_API_HOST = os.getenv('OPENWHISK_API_HOST', '')
    OPENWHISK_AUTH_KEY = os.getenv('OPENWHISK_AUTH_KEY', '')
    OPENWHISK_NAMESPACE = os.getenv('OPENWHISK_NAMESPACE', 'waddlebot')

    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
    # Use the same signing key consumed by Hub and downstream gRPC services.
    # SECRET_KEY remains a compatibility alias for older deployments.
    SECRET_KEY = os.getenv('JWT_SECRET') or os.getenv('SECRET_KEY')

    # Hub integration for activity tracking
    HUB_API_URL = os.getenv('HUB_API_URL', 'http://hub-module:8060')
    SERVICE_API_KEY = os.getenv('SERVICE_API_KEY', '')

    # Reputation module integration
    REPUTATION_API_URL = os.getenv('REPUTATION_API_URL', 'http://reputation:8021')
    REPUTATION_ENABLED = os.getenv('REPUTATION_ENABLED', 'true').lower() == 'true'

    # Workflow core module integration
    WORKFLOW_CORE_URL = os.getenv('WORKFLOW_CORE_URL', 'http://workflow-core:8070')

    # Security core module (for content filtering)
    SECURITY_CORE_URL = os.getenv('SECURITY_CORE_URL', 'http://security-core:8041')

    # Browser source module (for captions)
    BROWSER_SOURCE_URL = os.getenv('BROWSER_SOURCE_URL', 'http://browser-source:8050')

    # WaddleAI configuration (for AI-powered translation and decisions)
    # Note: Default is Google Gemma (gemma4:e2b), the free community model; plain-text
    # LANG:CONFIDENCE output, no JSON/structured schema (use gemma4:e4b if that changes).
    # PRC-origin models (e.g. Qwen) are prohibited by the supply-chain rule.
    # TinyLlama is NOT suitable for language detection (0% accuracy)
    WADDLEAI_BASE_URL = os.getenv('WADDLEAI_BASE_URL', 'http://ollama:11434')
    WADDLEAI_API_KEY = os.getenv('WADDLEAI_API_KEY', '')
    WADDLEAI_MODEL = os.getenv('WADDLEAI_MODEL', 'gemma4:e2b')
    WADDLEAI_TEMPERATURE = float(os.getenv('WADDLEAI_TEMPERATURE', '0.1'))  # Low temp for detection
    WADDLEAI_MAX_TOKENS = int(os.getenv('WADDLEAI_MAX_TOKENS', '500'))
    WADDLEAI_TIMEOUT = int(os.getenv('WADDLEAI_TIMEOUT', '30'))

    # Emote API Configuration (for translation preprocessing)
    # Third-party emote APIs (no auth required for public endpoints)
    BTTV_API_URL = os.getenv('BTTV_API_URL', 'https://api.betterttv.net/3')
    FFZ_API_URL = os.getenv('FFZ_API_URL', 'https://api.frankerfacez.com/v1')
    SEVENTV_API_URL = os.getenv('SEVENTV_API_URL', 'https://7tv.io/v3')

    # Twitch API (requires Client-ID for some endpoints)
    TWITCH_CLIENT_ID = os.getenv('TWITCH_CLIENT_ID', '')
    TWITCH_CLIENT_SECRET = os.getenv('TWITCH_CLIENT_SECRET', '')

    # Discord API (optional - enables fetching guild emojis)
    DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN', '')

    # Emote cache TTLs (in seconds)
    # Default: 30 days for global, 1 day for channel-specific
    EMOTE_CACHE_TTL_GLOBAL = int(os.getenv('EMOTE_CACHE_TTL_GLOBAL', str(30 * 24 * 60 * 60)))  # 30 days
    EMOTE_CACHE_TTL_CHANNEL = int(os.getenv('EMOTE_CACHE_TTL_CHANNEL', str(24 * 60 * 60)))  # 1 day

    # AI decision limits for translation preprocessing
    AI_DECISION_MAX_CALLS_PER_MESSAGE = int(os.getenv('AI_DECISION_MAX_CALLS_PER_MESSAGE', '3'))
    AI_DECISION_TIMEOUT = int(os.getenv('AI_DECISION_TIMEOUT', '2'))  # seconds

    # gRPC Configuration
    GRPC_ENABLED = os.getenv('GRPC_ENABLED', 'true').lower() == 'true'

    # Action module gRPC ports
    DISCORD_GRPC_HOST = os.getenv('DISCORD_GRPC_HOST', 'discord-action:50051')
    SLACK_GRPC_HOST = os.getenv('SLACK_GRPC_HOST', 'slack-action:50052')
    TWITCH_GRPC_HOST = os.getenv('TWITCH_GRPC_HOST', 'twitch-action:50053')
    YOUTUBE_GRPC_HOST = os.getenv('YOUTUBE_GRPC_HOST', 'youtube-action:50054')
    LAMBDA_GRPC_HOST = os.getenv('LAMBDA_GRPC_HOST', 'lambda-action:50060')
    GCP_FUNCTIONS_GRPC_HOST = os.getenv('GCP_FUNCTIONS_GRPC_HOST', 'gcp-functions-action:50061')
    OPENWHISK_GRPC_HOST = os.getenv('OPENWHISK_GRPC_HOST', 'openwhisk-action:50062')

    # Core module gRPC ports
    REPUTATION_GRPC_HOST = os.getenv('REPUTATION_GRPC_HOST', 'reputation:50021')
    WORKFLOW_GRPC_HOST = os.getenv('WORKFLOW_GRPC_HOST', 'workflow-core:50070')
    BROWSER_SOURCE_GRPC_HOST = os.getenv('BROWSER_SOURCE_GRPC_HOST', 'browser-source:50050')
    IDENTITY_GRPC_HOST = os.getenv('IDENTITY_GRPC_HOST', 'identity-core:50030')
    HUB_GRPC_HOST = os.getenv('HUB_GRPC_HOST', 'hub:50060')
    TRANSLATE_GRPC_HOST = os.getenv('TRANSLATE_GRPC_HOST', 'interactive-translate:50033')
    TRANSLATE_API_URL = os.getenv('TRANSLATE_API_URL', 'http://interactive-translate:8033')

    # gRPC settings
    GRPC_KEEPALIVE_TIME_MS = int(os.getenv('GRPC_KEEPALIVE_TIME_MS', '30000'))
    GRPC_KEEPALIVE_TIMEOUT_MS = int(os.getenv('GRPC_KEEPALIVE_TIMEOUT_MS', '10000'))
    GRPC_MAX_RETRIES = int(os.getenv('GRPC_MAX_RETRIES', '3'))

    # Redis Streams Pipeline Configuration
    STREAM_PIPELINE_ENABLED = os.getenv('STREAM_PIPELINE_ENABLED', 'false').lower() == 'true'
    STREAM_BATCH_SIZE = int(os.getenv('STREAM_BATCH_SIZE', '10'))
    STREAM_BLOCK_TIME = int(os.getenv('STREAM_BLOCK_TIME', '1000'))  # ms
    STREAM_MAX_RETRIES = int(os.getenv('STREAM_MAX_RETRIES', '3'))
    STREAM_CONSUMER_COUNT = int(os.getenv('STREAM_CONSUMER_COUNT', '4'))
    STREAM_CONSUMER_GROUP = os.getenv('STREAM_CONSUMER_GROUP', 'waddlebot-router')
    STREAM_CONSUMER_NAME = os.getenv('STREAM_CONSUMER_NAME', f'router-{os.getpid()}')

    # Stream names
    STREAM_INBOUND = os.getenv('STREAM_INBOUND', 'waddlebot:stream:events:inbound')
    STREAM_COMMANDS = os.getenv('STREAM_COMMANDS', 'waddlebot:stream:events:commands')
    STREAM_ACTIONS = os.getenv('STREAM_ACTIONS', 'waddlebot:stream:events:actions')
    STREAM_RESPONSES = os.getenv('STREAM_RESPONSES', 'waddlebot:stream:events:responses')


# Compute DATABASE_URL after class definition to handle special characters in password
_db_user = Config.DATABASE_USER
_db_password = Config.DATABASE_PASSWORD
_db_host = Config.DATABASE_HOST
_db_port = Config.DATABASE_PORT
_db_name = Config.DATABASE_NAME

if _db_password:
    # URL-encode password to handle special characters like / + = etc.
    _encoded_db_password = quote_plus(_db_password)
    Config.DATABASE_URL = f"postgres://{_db_user}:{_encoded_db_password}@{_db_host}:{_db_port}/{_db_name}"
else:
    Config.DATABASE_URL = f"postgres://{_db_user}@{_db_host}:{_db_port}/{_db_name}"

# Compute READ_REPLICA_URL if configured
_replica_host = Config.READ_REPLICA_HOST
_replica_port = Config.READ_REPLICA_PORT
if _replica_host:
    if _db_password:
        Config.READ_REPLICA_URL = f"postgres://{_db_user}:{_encoded_db_password}@{_replica_host}:{_replica_port}/{_db_name}"
    else:
        Config.READ_REPLICA_URL = f"postgres://{_db_user}@{_replica_host}:{_replica_port}/{_db_name}"
else:
    Config.READ_REPLICA_URL = ''

# Compute REDIS_URL after class definition
_redis_password = Config.REDIS_PASSWORD
_redis_host = Config.REDIS_HOST
_redis_port = Config.REDIS_PORT
_redis_db = Config.REDIS_DB
if _redis_password:
    # URL-encode password to handle special characters like / + = etc.
    _encoded_password = quote_plus(_redis_password)
    Config.REDIS_URL = f"redis://:{_encoded_password}@{_redis_host}:{_redis_port}/{_redis_db}"
else:
    Config.REDIS_URL = f"redis://{_redis_host}:{_redis_port}/{_redis_db}"
