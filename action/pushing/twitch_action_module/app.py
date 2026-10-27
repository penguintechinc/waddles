"""
Twitch Action Module - Main Application
Receives tasks from processor/router via gRPC and pushes actions to Twitch.

This is a stateless, clusterable container with:
- gRPC server for receiving tasks
- REST API for third-party use
- JWT authentication
- PyDAL for database operations
"""
import asyncio
import logging
import logging.handlers
import os
import sys
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

from quart import Quart, request, jsonify
from quart.config import Config as QuartConfig
from pydal import DAL
import jwt

from config import Config
from services.token_manager import TokenManager
from services.twitch_service import TwitchService
from services.grpc_handler import TwitchActionServicer, GrpcServer


# Custom config dict that provides Quart defaults
class DefaultConfig(dict):
    """Dict subclass that provides Quart defaults on missing keys."""

    def __init__(self, root_path=None, defaults=None):
        """Initialize with root_path and defaults (Flask signature)."""
        super().__init__()
        if defaults:
            self.update(defaults)
        # Always set Quart required keys
        self.setdefault('PROVIDE_AUTOMATIC_OPTIONS', True)
        self.setdefault('JSON_SORT_KEYS', False)
        self.setdefault('PROPAGATE_EXCEPTIONS', True)
        self.setdefault('MAX_CONTENT_LENGTH', 16 * 1024 * 1024)

    def __getitem__(self, key):
        """Provide defaults for Quart required keys."""
        try:
            return super().__getitem__(key)
        except KeyError:
            # Provide Quart defaults for required keys (fallback)
            defaults = {
                'PROVIDE_AUTOMATIC_OPTIONS': True,
                'JSON_SORT_KEYS': False,
                'PROPAGATE_EXCEPTIONS': True,
                'MAX_CONTENT_LENGTH': 16 * 1024 * 1024,
            }
            if key in defaults:
                return defaults[key]
            raise


# Initialize Flask/Quart app with pre-populated config dict
class PreConfiguredQuart(Quart):
    """Quart with pre-configured defaults to avoid __init__ KeyError."""

    config_class = DefaultConfig

    def __init__(self, *args, **kwargs):
        """Initialize with custom config class."""
        super().__init__(*args, **kwargs)


app = PreConfiguredQuart(__name__)

# Load config from Config class
for key in dir(Config):
    if key.isupper() and not key.startswith('_'):
        app.config[key] = getattr(Config, key)


# Setup logging
def setup_logging():
    """Setup comprehensive AAA logging."""
    log_format = (
        "[%(asctime)s] %(levelname)s %(name)s:%(funcName)s:%(lineno)d "
        "%(message)s"
    )

    formatter = logging.Formatter(log_format)

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    # File handler with rotation
    os.makedirs(Config.LOG_DIR, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        f"{Config.LOG_DIR}/twitch_action.log",
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5
    )
    file_handler.setFormatter(formatter)

    # Root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, Config.LOG_LEVEL.upper()))
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    # Optional syslog
    if Config.ENABLE_SYSLOG:
        try:
            syslog_handler = logging.handlers.SysLogHandler(
                address=(Config.SYSLOG_HOST, Config.SYSLOG_PORT),
                facility=getattr(
                    logging.handlers.SysLogHandler,
                    f"LOG_{Config.SYSLOG_FACILITY}"
                )
            )
            syslog_handler.setFormatter(formatter)
            root_logger.addHandler(syslog_handler)
        except Exception as e:
            print(f"Failed to setup syslog: {e}")


setup_logging()
logger = logging.getLogger(__name__)

# Initialize database
# Use fake_migrate=True to check schema without trying to create tables
db = DAL(Config.DATABASE_URL, folder="databases", pool_size=10, migrate_enabled=False, fake_migrate_all=True)

# Initialize services
token_manager = TokenManager(db, Config.TWITCH_CLIENT_ID, Config.TWITCH_CLIENT_SECRET)
twitch_service = TwitchService(token_manager)
grpc_servicer = TwitchActionServicer(twitch_service)
grpc_server = GrpcServer(grpc_servicer, Config.GRPC_PORT)


# JWT Authentication
def create_jwt_token(data: Dict[str, Any], expires_in: int = 3600) -> str:
    """Create JWT token."""
    payload = {
        **data,
        "exp": datetime.utcnow() + timedelta(seconds=expires_in),
        "iat": datetime.utcnow()
    }
    return jwt.encode(payload, Config.MODULE_SECRET_KEY, algorithm=Config.JWT_ALGORITHM)


def verify_jwt_token(token: str) -> Optional[Dict[str, Any]]:
    """Verify JWT token."""
    try:
        payload = jwt.decode(
            token,
            Config.MODULE_SECRET_KEY,
            algorithms=[Config.JWT_ALGORITHM]
        )
        return payload
    except jwt.ExpiredSignatureError:
        logger.warning("JWT token expired")
        return None
    except jwt.InvalidTokenError as e:
        logger.warning(f"Invalid JWT token: {e}")
        return None


def require_auth(f):
    """Decorator for JWT authentication."""
    async def decorated_function(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")

        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Missing or invalid Authorization header"}), 401

        token = auth_header.split(" ", 1)[1]
        payload = verify_jwt_token(token)

        if not payload:
            return jsonify({"error": "Invalid or expired token"}), 401

        # Store payload in request context
        request.auth_payload = payload
        return await f(*args, **kwargs)

    decorated_function.__name__ = f.__name__
    return decorated_function

# REST API Endpoints


@app.route("/health", methods=["GET"])
async def health():
    """Health check endpoint."""
    try:
        # Check database connectivity
        db.executesql("SELECT 1")

        return jsonify({
            "status": "healthy",
            "module": Config.MODULE_NAME,
            "version": Config.MODULE_VERSION,
            "timestamp": datetime.utcnow().isoformat(),
            "database": "connected",
            "grpc_port": Config.GRPC_PORT,
            "rest_port": Config.REST_PORT
        })
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return jsonify({
            "status": "unhealthy",
            "error": str(e)
        }), 503


@app.route("/api/v1/auth/token", methods=["POST"])
async def generate_token():
    """Generate JWT token for API access."""
    data = await request.get_json()

    # In production, validate credentials against database
    # For now, simple key-based authentication
    api_key = data.get("api_key", "")

    if api_key != Config.MODULE_SECRET_KEY:
        return jsonify({"error": "Invalid API key"}), 401

    token = create_jwt_token({
        "service": data.get("service", "unknown"),
        "permissions": ["execute_actions"]
    })

    return jsonify({
        "token": token,
        "expires_in": Config.JWT_EXPIRATION_SECONDS
    })


@app.route("/api/v1/actions/execute", methods=["POST"])
@require_auth
async def execute_action():
    """Execute single Twitch action via REST API."""
    try:
        data = await request.get_json()

        action_type = data.get("action_type")
        broadcaster_id = data.get("broadcaster_id")
        parameters = data.get("parameters", {})

        if not action_type or not broadcaster_id:
            return jsonify({
                "error": "action_type and broadcaster_id are required"
            }), 400

        # Create action request object (mimics gRPC request)
        action_request = type('obj', (object,), {
            'action_type': action_type,
            'broadcaster_id': broadcaster_id,
            'parameters': parameters,
            'request_id': data.get("request_id", ""),
            'timestamp': int(datetime.utcnow().timestamp())
        })

        # Execute action
        result = await grpc_servicer.ExecuteAction(action_request, None)

        return jsonify(result)

    except Exception as e:
        logger.error(f"Action execution failed: {e}", exc_info=True)
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route("/api/v1/actions/batch", methods=["POST"])
@require_auth
async def batch_execute_actions():
    """Execute batch of Twitch actions via REST API."""
    try:
        data = await request.get_json()
        actions = data.get("actions", [])

        if not actions:
            return jsonify({"error": "No actions provided"}), 400

        if len(actions) > Config.MAX_BATCH_SIZE:
            return jsonify({
                "error": f"Batch size exceeds maximum of {Config.MAX_BATCH_SIZE}"
            }), 400

        # Create batch request object
        batch_request = type('obj', (object,), {
            'actions': [
                type('obj', (object,), {
                    'action_type': action.get("action_type"),
                    'broadcaster_id': action.get("broadcaster_id"),
                    'parameters': action.get("parameters", {}),
                    'request_id': action.get("request_id", ""),
                    'timestamp': int(datetime.utcnow().timestamp())
                })
                for action in actions
            ]
        })

        # Execute batch
        result = await grpc_servicer.BatchExecuteActions(batch_request, None)

        return jsonify(result)

    except Exception as e:
        logger.error(f"Batch execution failed: {e}", exc_info=True)
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route("/api/v1/tokens/store", methods=["POST"])
@require_auth
async def store_token():
    """Store OAuth token for broadcaster."""
    try:
        data = await request.get_json()

        broadcaster_id = data.get("broadcaster_id")
        access_token = data.get("access_token")
        refresh_token = data.get("refresh_token")
        expires_in = data.get("expires_in", 3600)
        scopes = data.get("scopes", [])

        if not all([broadcaster_id, access_token, refresh_token]):
            return jsonify({
                "error": "broadcaster_id, access_token, and refresh_token are required"
            }), 400

        success = await token_manager.store_token(
            broadcaster_id,
            access_token,
            refresh_token,
            expires_in,
            scopes
        )

        if success:
            return jsonify({
                "success": True,
                "message": "Token stored successfully"
            })
        else:
            return jsonify({
                "success": False,
                "error": "Failed to store token"
            }), 500

    except Exception as e:
        logger.error(f"Token storage failed: {e}", exc_info=True)
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route("/api/v1/tokens/revoke", methods=["POST"])
@require_auth
async def revoke_token():
    """Revoke OAuth token for broadcaster."""
    try:
        data = await request.get_json()
        broadcaster_id = data.get("broadcaster_id")

        if not broadcaster_id:
            return jsonify({"error": "broadcaster_id is required"}), 400

        success = await token_manager.revoke_token(broadcaster_id)

        if success:
            return jsonify({
                "success": True,
                "message": "Token revoked successfully"
            })
        else:
            return jsonify({
                "success": False,
                "error": "Failed to revoke token"
            }), 500

    except Exception as e:
        logger.error(f"Token revocation failed: {e}", exc_info=True)
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route("/api/v1/tokens/force-reauth", methods=["POST"])
@require_auth
async def force_reauth():
    """
    Clear stored token and return a Twitch OAuth authorization URL.

    Call this when a broadcaster's token is permanently broken (e.g. revoked
    by the user or failed to refresh with 'invalid_grant').  The caller should
    redirect the broadcaster to the returned authorization_url so they can
    re-consent and a fresh token can be stored via /api/v1/tokens/store.
    """
    try:
        data = await request.get_json()
        broadcaster_id = data.get("broadcaster_id")

        if not broadcaster_id:
            return jsonify({"error": "broadcaster_id is required"}), 400

        if not Config.TWITCH_REDIRECT_URI:
            return jsonify({"error": "TWITCH_REDIRECT_URI not configured"}), 500

        # Delete the existing (broken) token so the system cannot keep
        # failing silently on the stale credentials.
        await token_manager.revoke_token(broadcaster_id)

        # Build Twitch authorization URL.
        from urllib.parse import urlencode
        params = urlencode({
            "client_id": Config.TWITCH_CLIENT_ID,
            "redirect_uri": Config.TWITCH_REDIRECT_URI,
            "response_type": "code",
            "scope": Config.TWITCH_OAUTH_SCOPES,
            "state": broadcaster_id,
            "force_verify": "true",
        })
        auth_url = f"https://id.twitch.tv/oauth2/authorize?{params}"

        logger.info(f"Force re-auth initiated for broadcaster {broadcaster_id}")

        return jsonify({
            "success": True,
            "broadcaster_id": broadcaster_id,
            "authorization_url": auth_url,
            "message": "Existing token cleared. Redirect user to authorization_url to re-authorize.",
        })

    except Exception as e:
        logger.error(f"Force re-auth failed: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/v1/stats", methods=["GET"])
@require_auth
async def get_stats():
    """Get module statistics."""
    try:
        # Count tokens
        token_count = db(db.twitch_action_tokens).count()

        return jsonify({
            "module": Config.MODULE_NAME,
            "version": Config.MODULE_VERSION,
            "stats": {
                "registered_broadcasters": token_count,
                "grpc_port": Config.GRPC_PORT,
                "rest_port": Config.REST_PORT
            },
            "timestamp": datetime.utcnow().isoformat()
        })

    except Exception as e:
        logger.error(f"Stats retrieval failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

# Application Lifecycle


@app.before_serving
async def startup():
    """Application startup."""
    logger.info(f"Starting {Config.MODULE_NAME} v{Config.MODULE_VERSION}")

    # Validate configuration
    errors = Config.validate()
    if errors:
        logger.error(f"Configuration errors: {', '.join(errors)}")
        sys.exit(1)

    logger.info("Configuration validated successfully")

    # Start gRPC server
    await grpc_server.start()

    logger.info(
        f"Twitch Action Module started - "
        f"REST: {Config.REST_PORT}, gRPC: {Config.GRPC_PORT}"
    )


@app.after_serving
async def shutdown():
    """Application shutdown."""
    logger.info("Shutting down Twitch Action Module")

    # Stop gRPC server
    await grpc_server.stop()

    # Close database
    db.close()

    logger.info("Twitch Action Module stopped")


if __name__ == "__main__":
    # Run with Hypercorn in production
    # hypercorn app:app --bind 0.0.0.0:8072 --workers 4
    app.run(host="0.0.0.0", port=Config.REST_PORT, debug=False)
