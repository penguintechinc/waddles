import os


class Config:
    MODULE_NAME = 'translate_interaction_module'
    MODULE_VERSION = '1.0.0'

    # REST port (admin, health, cache management)
    MODULE_PORT = int(os.getenv('MODULE_PORT', '8033'))
    HOST = os.getenv('HOST', '0.0.0.0')

    # gRPC port (router hot path)
    GRPC_PORT = int(os.getenv('GRPC_PORT', '50033'))
    GRPC_MAX_WORKERS = int(os.getenv('GRPC_MAX_WORKERS', '10'))

    # Security (JWT for gRPC auth, same pattern as action modules)
    MODULE_SECRET_KEY = os.getenv(
        'MODULE_SECRET_KEY',
        'change_me_in_production_64_char_key_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
    )
    JWT_ALGORITHM = 'HS256'
    JWT_EXPIRATION_SECONDS = int(os.getenv('JWT_EXPIRATION_SECONDS', '3600'))

    # Database
    # PyDAL expects 'postgres://' not 'postgresql://'
    _raw_db_url = os.getenv(
        'DATABASE_URL',
        'postgres://waddlebot:password@localhost:5432/waddlebot'
    )
    DATABASE_URL = _raw_db_url.replace('postgresql://', 'postgres://')

    # Redis
    REDIS_URL = os.getenv('REDIS_URL', '')
    REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
    REDIS_PORT = int(os.getenv('REDIS_PORT', '6379'))
    REDIS_DB = int(os.getenv('REDIS_DB', '0'))

    # WaddleAI (local AI fallback)
    WADDLEAI_BASE_URL = os.getenv('WADDLEAI_BASE_URL', 'http://ollama:11434')
    WADDLEAI_API_KEY = os.getenv('WADDLEAI_API_KEY', '')
    WADDLEAI_MODEL = os.getenv('WADDLEAI_MODEL', 'gemma4:e2b')
    WADDLEAI_TEMPERATURE = float(os.getenv('WADDLEAI_TEMPERATURE', '0.1'))
    WADDLEAI_MAX_TOKENS = int(os.getenv('WADDLEAI_MAX_TOKENS', '500'))
    WADDLEAI_TIMEOUT = int(os.getenv('WADDLEAI_TIMEOUT', '30'))

    # Emote providers
    BTTV_API_URL = os.getenv('BTTV_API_URL', 'https://api.betterttv.net/3')
    FFZ_API_URL = os.getenv('FFZ_API_URL', 'https://api.frankerfacez.com/v1')
    SEVENTV_API_URL = os.getenv('SEVENTV_API_URL', 'https://7tv.io/v3')
    TWITCH_CLIENT_ID = os.getenv('TWITCH_CLIENT_ID', '')
    TWITCH_CLIENT_SECRET = os.getenv('TWITCH_CLIENT_SECRET', '')
    DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN', '')
    EMOTE_CACHE_TTL_GLOBAL = int(os.getenv('EMOTE_CACHE_TTL_GLOBAL', str(30 * 86400)))
    EMOTE_CACHE_TTL_CHANNEL = int(os.getenv('EMOTE_CACHE_TTL_CHANNEL', str(86400)))

    # AI decision
    AI_DECISION_MAX_CALLS_PER_MESSAGE = int(os.getenv('AI_DECISION_MAX_CALLS_PER_MESSAGE', '3'))
    AI_DECISION_TIMEOUT = int(os.getenv('AI_DECISION_TIMEOUT', '2'))

    # Logging
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
    LOG_DIR = os.getenv('LOG_DIR', '/var/log/waddlebotlog')
