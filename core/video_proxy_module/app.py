"""
Video Proxy Module - Main Application
Manages stream configurations, destinations, and proxying via MarchProxy.

This is a stateless, clusterable container with:
- REST API for stream management
- gRPC client to MarchProxy for stream control
- JWT authentication
- PyDAL for database operations
"""
import asyncio
import logging
import logging.handlers
import os
import sys
import secrets
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List

from quart import Quart, request, jsonify
from quart.config import Config as QuartConfig
from pydal import DAL, Field
import jwt

from flask_core import install_security_headers
from config import Config
from services.community_authz import (
    CommunityAccessError,
    bind_shared_read_tables,
    extract_user_id,
    require_admin,
    require_member,
)
from services.rate_limiting import install_rate_limiting


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
# security.md A05 hardening -- JSON-only service, default deny-everything CSP.
install_security_headers(app)

# Load config from Config class
config = Config()
for key in dir(config):
    if key.isupper() and not key.startswith('_'):
        app.config[key] = getattr(config, key)


# Setup logging
def setup_logging():
    """Setup comprehensive logging."""
    log_format = (
        "[%(asctime)s] %(levelname)s %(name)s:%(funcName)s:%(lineno)d "
        "%(message)s"
    )

    formatter = logging.Formatter(log_format)

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    # Root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, config.LOG_LEVEL.upper()))
    root_logger.addHandler(console_handler)


setup_logging()
logger = logging.getLogger(__name__)

# Initialize database
db = DAL(
    config.DATABASE_URL,
    folder="databases",
    pool_size=config.DB_POOL_SIZE,
    migrate_enabled=False,
)

# SECURITY (A01, BOLA/IDOR -- deferred from gh security PR #260): binds the
# read-only `communities`/`community_members` field subset every
# `_authorize_community()` call below needs. `migrate=False` -- these
# tables are owned by hub-api's own migrations, never created here.
bind_shared_read_tables(db)


# Define database tables
def init_database():
    """Initialize database tables using PyDAL."""

    # Stream configurations table
    db.define_table(
        'stream_configs',
        Field('community_id', 'string', length=255, notnull=True, unique=True),
        Field('stream_key', 'string', length=255, notnull=True, unique=True),
        Field('ingest_url', 'string', length=512, notnull=True),
        Field('is_active', 'boolean', default=True),
        Field('created_at', 'datetime', default=datetime.utcnow),
        Field('updated_at', 'datetime', update=datetime.utcnow),
        migrate=False
    )

    # Stream destinations table
    db.define_table(
        'stream_destinations',
        Field('config_id', 'reference stream_configs', notnull=True),
        Field('platform', 'string', length=50, notnull=True),
        Field('rtmp_url', 'string', length=512, notnull=True),
        Field('stream_key', 'string', length=255, notnull=True),
        Field('is_active', 'boolean', default=True),
        Field('force_cut', 'boolean', default=False),
        Field('max_resolution', 'string', length=20, default='1080p'),
        Field('created_at', 'datetime', default=datetime.utcnow),
        Field('updated_at', 'datetime', update=datetime.utcnow),
        migrate=False
    )

    # Stream status table
    db.define_table(
        'stream_status',
        Field('config_id', 'reference stream_configs', notnull=True, unique=True),
        Field('is_streaming', 'boolean', default=False),
        Field('viewer_count', 'integer', default=0),
        Field('bitrate_kbps', 'integer', default=0),
        Field('start_time', 'datetime'),
        Field('last_update', 'datetime', default=datetime.utcnow),
        migrate=False
    )

    db.commit()
    logger.info("Database tables initialized successfully")


# JWT Authentication
def create_jwt_token(data: Dict[str, Any], expires_in: int = 3600) -> str:
    """Create JWT token."""
    payload = {
        **data,
        "exp": datetime.utcnow() + timedelta(seconds=expires_in),
        "iat": datetime.utcnow()
    }
    return jwt.encode(payload, config.JWT_SECRET_KEY, algorithm=config.JWT_ALGORITHM)


def verify_jwt_token(token: str) -> Optional[Dict[str, Any]]:
    """Verify JWT token."""
    try:
        payload = jwt.decode(
            token,
            config.JWT_SECRET_KEY,
            algorithms=[config.JWT_ALGORITHM]
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


# SECURITY (A04): every route below had zero rate limiting -- shared global
# before_request hook, see services/rate_limiting.py module docstring.
install_rate_limiting(
    app,
    namespace=config.MODULE_NAME,
    redis_url=config.REDIS_URL or f"redis://{config.REDIS_HOST}:{config.REDIS_PORT}/{config.REDIS_DB}",
    verify_jwt_token=verify_jwt_token,
    jwt_secret_key=config.JWT_SECRET_KEY,
)


async def _authorize_community(raw_community_id, *, admin_required: bool):
    """Require the authenticated caller be a member/admin of `raw_community_id`.

    SECURITY (A01, BOLA/IDOR -- deferred from gh security PR #260): every
    route below decoded and verified a bearer JWT (`require_auth`) but
    never checked the caller actually belonged to the community they were
    operating on -- any authenticated caller could read/write ANY
    community's stream config. See `services/community_authz.py` module
    docstring for the full rationale.

    Args:
        raw_community_id: The (string or int) community identifier to
            check -- callers resolve this from the actual DB row being
            operated on, never trust a client-supplied value in isolation
            without cross-checking it against the resource's real owner.
        admin_required: Whether the caller must be a community-admin
            (mutating routes) or just an active member (read routes)
            suffices.

    Returns:
        `None` if authorized; otherwise a `(response, status_code)` tuple
        the caller must `return` immediately.
    """
    payload = getattr(request, "auth_payload", None)
    if payload is None:
        return jsonify({"error": "Authentication required"}), 401

    try:
        community_id = int(raw_community_id)
    except (TypeError, ValueError):
        return jsonify({"error": "community_id must be numeric"}), 400

    try:
        user_id = extract_user_id(payload)
        check = require_admin if admin_required else require_member
        check(db, community_id=community_id, user_id=user_id)
    except CommunityAccessError as e:
        return jsonify({"error": e.message}), 403

    return None


# Helper functions
def generate_stream_key() -> str:
    """Generate a secure random stream key."""
    return secrets.token_urlsafe(32)


def format_stream_config(row) -> Dict[str, Any]:
    """Format stream config row for JSON response."""
    return {
        "id": row.id,
        "community_id": row.community_id,
        "stream_key": row.stream_key,
        "ingest_url": row.ingest_url,
        "is_active": row.is_active,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None
    }


def format_destination(row) -> Dict[str, Any]:
    """Format destination row for JSON response."""
    return {
        "id": row.id,
        "config_id": row.config_id,
        "platform": row.platform,
        "rtmp_url": row.rtmp_url,
        "stream_key": row.stream_key[:8] + "..." if row.stream_key else "",
        "is_active": row.is_active,
        "force_cut": row.force_cut,
        "max_resolution": row.max_resolution,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None
    }


def format_stream_status(row) -> Dict[str, Any]:
    """Format stream status row for JSON response."""
    return {
        "config_id": row.config_id,
        "is_streaming": row.is_streaming,
        "viewer_count": row.viewer_count,
        "bitrate_kbps": row.bitrate_kbps,
        "start_time": row.start_time.isoformat() if row.start_time else None,
        "last_update": row.last_update.isoformat() if row.last_update else None
    }

# REST API Endpoints


@app.route("/health", methods=["GET"])
async def health():
    """Health check endpoint."""
    try:
        # Check database connectivity
        db.executesql("SELECT 1")

        return jsonify({
            "status": "healthy",
            "module": config.MODULE_NAME,
            "version": config.MODULE_VERSION,
            "timestamp": datetime.utcnow().isoformat(),
            "database": "connected"
        })
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return jsonify({
            "status": "unhealthy",
            "error": str(e)
        }), 503


@app.route("/api/v1/stream/config", methods=["POST"])
@require_auth
async def create_stream_config():
    """Create stream configuration for a community."""
    try:
        data = await request.get_json()
        community_id = data.get("community_id")

        if not community_id:
            return jsonify({"error": "community_id is required"}), 400

        auth_error = await _authorize_community(community_id, admin_required=True)
        if auth_error:
            return auth_error

        # Check if config already exists
        existing = db(db.stream_configs.community_id == community_id).select().first()
        if existing:
            return jsonify({
                "error": "Stream config already exists for this community",
                "config": format_stream_config(existing)
            }), 409

        # Generate stream key and ingest URL
        stream_key = generate_stream_key()
        ingest_url = f"rtmp://{config.MODULE_HOST}:{config.MODULE_PORT}/live/{stream_key}"

        # Create config
        config_id = db.stream_configs.insert(
            community_id=community_id,
            stream_key=stream_key,
            ingest_url=ingest_url,
            is_active=True
        )

        db.commit()

        # Initialize stream status
        db.stream_status.insert(
            config_id=config_id,
            is_streaming=False,
            viewer_count=0,
            bitrate_kbps=0
        )
        db.commit()

        config_row = db.stream_configs[config_id]

        logger.info(f"Created stream config for community {community_id}")

        return jsonify({
            "success": True,
            "config": format_stream_config(config_row)
        }), 201

    except Exception as e:
        logger.error(f"Failed to create stream config: {e}", exc_info=True)
        db.rollback()
        return jsonify({"error": str(e)}), 500


@app.route("/api/v1/stream/config/<community_id>", methods=["GET"])
@require_auth
async def get_stream_config(community_id: str):
    """Get stream configuration for a community."""
    try:
        auth_error = await _authorize_community(community_id, admin_required=False)
        if auth_error:
            return auth_error

        config_row = db(db.stream_configs.community_id == community_id).select().first()

        if not config_row:
            return jsonify({"error": "Stream config not found"}), 404

        return jsonify({
            "success": True,
            "config": format_stream_config(config_row)
        })

    except Exception as e:
        logger.error(f"Failed to get stream config: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/v1/stream/key/regenerate/<community_id>", methods=["POST"])
@require_auth
async def regenerate_stream_key(community_id: str):
    """Regenerate stream key for a community."""
    try:
        auth_error = await _authorize_community(community_id, admin_required=True)
        if auth_error:
            return auth_error

        config_row = db(db.stream_configs.community_id == community_id).select().first()

        if not config_row:
            return jsonify({"error": "Stream config not found"}), 404

        # Generate new stream key
        new_stream_key = generate_stream_key()
        new_ingest_url = f"rtmp://{config.MODULE_HOST}:{config.MODULE_PORT}/live/{new_stream_key}"

        # Update config
        db(db.stream_configs.id == config_row.id).update(
            stream_key=new_stream_key,
            ingest_url=new_ingest_url,
            updated_at=datetime.utcnow()
        )
        db.commit()

        updated_row = db.stream_configs[config_row.id]

        logger.info(f"Regenerated stream key for community {community_id}")

        return jsonify({
            "success": True,
            "config": format_stream_config(updated_row)
        })

    except Exception as e:
        logger.error(f"Failed to regenerate stream key: {e}", exc_info=True)
        db.rollback()
        return jsonify({"error": str(e)}), 500


@app.route("/api/v1/stream/destinations/<int:config_id>", methods=["GET"])
@require_auth
async def get_destinations(config_id: int):
    """List all destinations for a stream config."""
    try:
        # Verify config exists
        config_row = db.stream_configs[config_id]
        if not config_row:
            return jsonify({"error": "Stream config not found"}), 404

        auth_error = await _authorize_community(config_row.community_id, admin_required=False)
        if auth_error:
            return auth_error

        # Get destinations
        destinations = db(db.stream_destinations.config_id == config_id).select()

        return jsonify({
            "success": True,
            "count": len(destinations),
            "destinations": [format_destination(dest) for dest in destinations]
        })

    except Exception as e:
        logger.error(f"Failed to get destinations: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/v1/stream/destinations", methods=["POST"])
@require_auth
async def add_destination():
    """Add a destination for a stream config."""
    try:
        data = await request.get_json()

        config_id = data.get("config_id")
        platform = data.get("platform")
        rtmp_url = data.get("rtmp_url")
        stream_key = data.get("stream_key")
        max_resolution = data.get("max_resolution", "1080p")

        if not all([config_id, platform, rtmp_url, stream_key]):
            return jsonify({
                "error": "config_id, platform, rtmp_url, and stream_key are required"
            }), 400

        # Verify config exists
        config_row = db.stream_configs[config_id]
        if not config_row:
            return jsonify({"error": "Stream config not found"}), 404

        auth_error = await _authorize_community(config_row.community_id, admin_required=True)
        if auth_error:
            return auth_error

        # Check destination count limits (basic check, license check would go here)
        dest_count = db(db.stream_destinations.config_id == config_id).count()
        if dest_count >= config.FREE_MAX_DESTINATIONS:
            return jsonify({
                "error": f"Maximum destinations ({config.FREE_MAX_DESTINATIONS}) reached"
            }), 403

        # Create destination
        dest_id = db.stream_destinations.insert(
            config_id=config_id,
            platform=platform,
            rtmp_url=rtmp_url,
            stream_key=stream_key,
            is_active=True,
            force_cut=False,
            max_resolution=max_resolution
        )

        db.commit()

        dest_row = db.stream_destinations[dest_id]

        logger.info(f"Added destination {platform} for config {config_id}")

        return jsonify({
            "success": True,
            "destination": format_destination(dest_row)
        }), 201

    except Exception as e:
        logger.error(f"Failed to add destination: {e}", exc_info=True)
        db.rollback()
        return jsonify({"error": str(e)}), 500


@app.route("/api/v1/stream/destinations/<int:destination_id>", methods=["DELETE"])
@require_auth
async def remove_destination(destination_id: int):
    """Remove a destination."""
    try:
        dest_row = db.stream_destinations[destination_id]
        if not dest_row:
            return jsonify({"error": "Destination not found"}), 404

        config_row = db.stream_configs[dest_row.config_id]
        if not config_row:
            return jsonify({"error": "Destination not found"}), 404
        auth_error = await _authorize_community(config_row.community_id, admin_required=True)
        if auth_error:
            return auth_error

        # Delete destination
        db(db.stream_destinations.id == destination_id).delete()
        db.commit()

        logger.info(f"Removed destination {destination_id}")

        return jsonify({
            "success": True,
            "message": "Destination removed successfully"
        })

    except Exception as e:
        logger.error(f"Failed to remove destination: {e}", exc_info=True)
        db.rollback()
        return jsonify({"error": str(e)}), 500


@app.route("/api/v1/stream/destinations/<int:destination_id>/force-cut", methods=["PUT"])
@require_auth
async def toggle_force_cut(destination_id: int):
    """Toggle force cut flag for a destination."""
    try:
        dest_row = db.stream_destinations[destination_id]
        if not dest_row:
            return jsonify({"error": "Destination not found"}), 404

        config_row = db.stream_configs[dest_row.config_id]
        if not config_row:
            return jsonify({"error": "Destination not found"}), 404
        auth_error = await _authorize_community(config_row.community_id, admin_required=True)
        if auth_error:
            return auth_error

        # Toggle force_cut
        new_force_cut = not dest_row.force_cut
        db(db.stream_destinations.id == destination_id).update(
            force_cut=new_force_cut,
            updated_at=datetime.utcnow()
        )
        db.commit()

        updated_row = db.stream_destinations[destination_id]

        logger.info(f"Toggled force_cut to {new_force_cut} for destination {destination_id}")

        return jsonify({
            "success": True,
            "destination": format_destination(updated_row)
        })

    except Exception as e:
        logger.error(f"Failed to toggle force_cut: {e}", exc_info=True)
        db.rollback()
        return jsonify({"error": str(e)}), 500


@app.route("/api/v1/stream/status/<int:config_id>", methods=["GET"])
@require_auth
async def get_stream_status(config_id: int):
    """Get stream status for a config."""
    try:
        # Verify config exists
        config_row = db.stream_configs[config_id]
        if not config_row:
            return jsonify({"error": "Stream config not found"}), 404

        auth_error = await _authorize_community(config_row.community_id, admin_required=False)
        if auth_error:
            return auth_error

        # Get status
        status_row = db(db.stream_status.config_id == config_id).select().first()

        if not status_row:
            return jsonify({"error": "Stream status not found"}), 404

        return jsonify({
            "success": True,
            "status": format_stream_status(status_row)
        })

    except Exception as e:
        logger.error(f"Failed to get stream status: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

# Application Lifecycle


@app.before_serving
async def startup():
    """Application startup."""
    logger.info(f"Starting {config.MODULE_NAME} v{config.MODULE_VERSION}")

    # Validate configuration
    try:
        config.validate()
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        sys.exit(1)

    # Initialize database
    try:
        init_database()
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")
        sys.exit(1)

    logger.info(f"Video Proxy Module started on port {config.MODULE_PORT}")


@app.after_serving
async def shutdown():
    """Application shutdown."""
    logger.info("Shutting down Video Proxy Module")

    # Close database
    db.close()

    logger.info("Video Proxy Module stopped")


if __name__ == "__main__":
    # Run with Hypercorn in production
    # hypercorn app:app --bind 0.0.0.0:8090 --workers 4
    app.run(host=config.MODULE_HOST, port=config.MODULE_PORT, debug=False)
