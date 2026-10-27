"""
Core Data Service Configuration

Unified configuration for all 4 modules:
- Analytics Core Module
- Engagement Module
- Reputation Module
- Labels Core Module
"""
import os
from dataclasses import dataclass
from urllib.parse import quote_plus as _quote_plus

from flask_core.secrets import require_secret_key


@dataclass
class Config:
    """Unified configuration for core-data service."""

    # Service Identity
    MODULE_NAME = os.getenv('MODULE_NAME', 'core-data')
    MODULE_VERSION = os.getenv('MODULE_VERSION', '0.0.1')
    MODULE_PORT = int(os.getenv('MODULE_PORT', '8040'))
    MODULE_HOST = os.getenv('MODULE_HOST', '0.0.0.0')

    # Database - build URL from components
    DATABASE_HOST = os.getenv('DATABASE_HOST', 'infra-postgres')
    DATABASE_PORT = os.getenv('DATABASE_PORT', '5432')
    DATABASE_NAME = os.getenv('DATABASE_NAME', 'waddlebot')
    DATABASE_USER = os.getenv('DATABASE_USER', 'waddlebot')
    DATABASE_PASSWORD = os.getenv('DATABASE_PASSWORD', '')
    DB_POOL_SIZE = int(os.getenv('DB_POOL_SIZE', '10'))
    DB_MAX_RETRIES = int(os.getenv('DB_MAX_RETRIES', '5'))
    DB_RETRY_DELAY = int(os.getenv('DB_RETRY_DELAY', '5'))

    # Security
    SERVICE_API_KEY = os.getenv('SERVICE_API_KEY', '')
    JWT_SECRET_KEY = require_secret_key('JWT_SECRET_KEY', default='development-secret-key')
    JWT_ALGORITHM = os.getenv('JWT_ALGORITHM', 'HS256')

    # gRPC (Reputation Module)
    GRPC_PORT = int(os.getenv('GRPC_PORT', '50051'))

    # Logging
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')

    # Analytics-specific
    ANALYTICS_POLLING_INTERVAL = int(os.getenv('ANALYTICS_POLLING_INTERVAL', '300'))

    # Reputation-specific
    REPUTATION_MIN = int(os.getenv('REPUTATION_MIN', '300'))
    REPUTATION_MAX = int(os.getenv('REPUTATION_MAX', '850'))
    REPUTATION_DEFAULT = int(os.getenv('REPUTATION_DEFAULT', '600'))
    REPUTATION_AUTO_BAN_THRESHOLD = int(os.getenv('REPUTATION_AUTO_BAN_THRESHOLD', '450'))

    # Reputation Tiers (FICO-style)
    REPUTATION_TIERS = {
        'poor': (300, 579),
        'fair': (580, 669),
        'good': (670, 739),
        'very_good': (740, 799),
        'excellent': (800, 850),
    }

    def validate(self):
        """Validate required configuration."""
        if not self.DATABASE_URL:
            raise ValueError("DATABASE_URL environment variable is required")
        if not self.SERVICE_API_KEY:
            raise ValueError("SERVICE_API_KEY environment variable is required")
        if not self.JWT_SECRET_KEY:
            raise ValueError("JWT_SECRET_KEY environment variable is required")


# Construct DATABASE_URL from components after class definition
_db_user = Config.DATABASE_USER
_db_password = Config.DATABASE_PASSWORD
_db_host = Config.DATABASE_HOST
_db_port = Config.DATABASE_PORT
_db_name = Config.DATABASE_NAME
if _db_password:
    _encoded_pw = _quote_plus(_db_password)
    Config.DATABASE_URL = f"postgres://{_db_user}:{_encoded_pw}@{_db_host}:{_db_port}/{_db_name}"
else:
    Config.DATABASE_URL = f"postgres://{_db_user}@{_db_host}:{_db_port}/{_db_name}"
