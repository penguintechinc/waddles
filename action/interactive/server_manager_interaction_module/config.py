"""Configuration for server_manager_interaction_module"""
import os
from dotenv import load_dotenv
from flask_core.secrets import require_secret_key
load_dotenv()


class Config:
    MODULE_NAME = 'server_manager_interaction_module'
    MODULE_VERSION = '1.0.0'
    MODULE_PORT = int(os.getenv('MODULE_PORT', '8098'))
    DATABASE_URL = os.getenv('DATABASE_URL', 'postgresql://waddlebot:password@localhost:5432/waddlebot')
    REDIS_URL = os.getenv('REDIS_URL', '')
    CORE_API_URL = os.getenv('CORE_API_URL', 'http://router-service:8000')
    ROUTER_API_URL = os.getenv('ROUTER_API_URL', 'http://router-service:8000/api/v1/router')
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
    SECRET_KEY = require_secret_key()
    DEFAULT_POLL_INTERVAL_MINUTES = int(os.getenv('DEFAULT_POLL_INTERVAL_MINUTES', '5'))
    STATUS_CHECK_TIMEOUT = int(os.getenv('STATUS_CHECK_TIMEOUT', '10'))
    RCON_ENCRYPTION_KEY = os.getenv('RCON_ENCRYPTION_KEY', '')
    RCON_CONNECTION_TTL = int(os.getenv('RCON_CONNECTION_TTL', '60'))
    SECURITY_CORE_URL = os.getenv('SECURITY_CORE_URL', 'http://security-core-service:8010')
