"""
YouTube Action Module - Main Application
Stateless container for pushing YouTube actions via gRPC and REST
"""
import logging
from datetime import datetime
from typing import Optional

from quart import Quart, request, jsonify
from pydal import DAL
import jwt

from config import Config
from services.oauth_manager import OAuthManager
from services.youtube_service import YouTubeService
from services.grpc_handler import GRPCServer


# Configure logging
logging.basicConfig(
    level=getattr(logging, Config.LOG_LEVEL),
    format="[%(asctime)s] %(levelname)s %(module)s:%(funcName)s - %(message)s",
)
logger = logging.getLogger(__name__)


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
app.config["SECRET_KEY"] = Config.MODULE_SECRET_KEY


# Initialize database
# Use fake_migrate=True to check schema without trying to create tables
db = DAL(
    Config.DATABASE_URL,
    folder=Config.LOG_DIR,
    migrate=False,
    fake_migrate_all=True,
    pool_size=10,
)


# Initialize services
oauth_manager = OAuthManager(db)
youtube_service = YouTubeService(oauth_manager)
grpc_server = GRPCServer(youtube_service)


# JWT Authentication decorator
def require_jwt(f):
    """Require JWT authentication for endpoint"""

    async def decorated_function(*args, **kwargs):
        auth_header = request.headers.get("Authorization")

        if not auth_header or not auth_header.startswith("Bearer "):
            return jsonify({"success": False, "message": "Missing or invalid token"}), 401

        token = auth_header.split(" ")[1]

        try:
            payload = jwt.decode(
                token, Config.MODULE_SECRET_KEY, algorithms=["HS256"]
            )
            request.jwt_payload = payload
        except jwt.ExpiredSignatureError:
            return jsonify({"success": False, "message": "Token expired"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"success": False, "message": "Invalid token"}), 401

        return await f(*args, **kwargs)

    decorated_function.__name__ = f.__name__
    return decorated_function


# ========== Health Check ==========


@app.route("/health", methods=["GET"])
async def health_check():
    """Health check endpoint"""
    try:
        # Check database connectivity
        db.executesql("SELECT 1")

        return jsonify(
            {
                "status": "healthy",
                "module": Config.MODULE_NAME,
                "version": Config.MODULE_VERSION,
                "timestamp": datetime.utcnow().isoformat(),
                "grpc_port": Config.GRPC_PORT,
                "rest_port": Config.REST_PORT,
            }
        )
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return (
            jsonify(
                {
                    "status": "unhealthy",
                    "error": str(e),
                    "timestamp": datetime.utcnow().isoformat(),
                }
            ),
            503,
        )


# ========== OAuth Management Endpoints ==========


@app.route("/oauth/authorize", methods=["GET"])
async def oauth_authorize():
    """Get OAuth authorization URL"""
    try:
        state = request.args.get("state")
        auth_url = oauth_manager.get_authorization_url(state)

        return jsonify({"success": True, "authorization_url": auth_url})

    except Exception as e:
        logger.error(f"OAuth authorize error: {e}")
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/oauth/callback", methods=["GET"])
async def oauth_callback():
    """Handle OAuth callback"""
    try:
        code = request.args.get("code")
        channel_id = request.args.get("state")  # Pass channel_id via state

        if not code or not channel_id:
            return jsonify({"success": False, "message": "Missing code or channel_id"}), 400

        credentials = oauth_manager.exchange_code_for_token(code, channel_id)

        return jsonify(
            {
                "success": True,
                "message": "Authorization successful",
                "channel_id": channel_id,
                "expires_at": credentials.expiry.isoformat() if credentials.expiry else None,
            }
        )

    except Exception as e:
        logger.error(f"OAuth callback error: {e}")
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/oauth/force-reauth", methods=["POST"])
@require_jwt
async def force_reauth():
    """
    Clear a channel's stored token and return a fresh authorization URL.

    Call this when a channel's token is permanently broken (e.g. Google
    reports 'invalid_grant' because the refresh token was revoked or
    rotated without being saved).  The caller should redirect the channel
    owner to the returned authorization_url so they can re-consent.
    """
    try:
        data = await request.get_json()
        channel_id = data.get("channel_id")

        if not channel_id:
            return jsonify({"success": False, "message": "channel_id is required"}), 400

        # Delete the existing (broken) credentials before generating a new
        # authorization URL so there is no risk of the stale token being used.
        oauth_manager.delete_credentials(channel_id)

        state = data.get("state", channel_id)
        auth_url = oauth_manager.get_authorization_url(state)

        logger.info(f"Force re-auth initiated for channel: {channel_id}")

        return jsonify({
            "success": True,
            "channel_id": channel_id,
            "authorization_url": auth_url,
            "message": "Existing credentials cleared. Redirect user to authorization_url to re-authorize.",
        })

    except Exception as e:
        logger.error(f"Force re-auth error: {e}")
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/oauth/channels", methods=["GET"])
@require_jwt
async def list_authorized_channels():
    """List all authorized channels"""
    try:
        channels = oauth_manager.list_authorized_channels()
        return jsonify({"success": True, "channels": channels})

    except Exception as e:
        logger.error(f"List channels error: {e}")
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/oauth/revoke/<channel_id>", methods=["DELETE"])
@require_jwt
async def revoke_authorization(channel_id: str):
    """Revoke OAuth authorization for a channel"""
    try:
        deleted = oauth_manager.delete_credentials(channel_id)

        if deleted:
            return jsonify({"success": True, "message": "Authorization revoked"})
        else:
            return jsonify({"success": False, "message": "Channel not found"}), 404

    except Exception as e:
        logger.error(f"Revoke authorization error: {e}")
        return jsonify({"success": False, "message": str(e)}), 500


# ========== Chat Action Endpoints ==========


@app.route("/api/v1/chat/send", methods=["POST"])
@require_jwt
async def send_chat_message():
    """Send message to live chat"""
    data = await request.get_json()
    result = youtube_service.send_live_chat_message(
        live_chat_id=data.get("live_chat_id"),
        message=data.get("message"),
        channel_id=data.get("channel_id"),
    )
    return jsonify(result)


@app.route("/api/v1/chat/delete", methods=["POST"])
@require_jwt
async def delete_chat_message():
    """Delete chat message"""
    data = await request.get_json()
    result = youtube_service.delete_live_chat_message(
        message_id=data.get("message_id"), channel_id=data.get("channel_id")
    )
    return jsonify(result)


@app.route("/api/v1/chat/ban", methods=["POST"])
@require_jwt
async def ban_chat_user():
    """Ban user from chat"""
    data = await request.get_json()
    result = youtube_service.ban_live_chat_user(
        live_chat_id=data.get("live_chat_id"),
        channel_id=data.get("channel_id"),
        target_channel_id=data.get("target_channel_id"),
        duration_seconds=data.get("duration_seconds"),
    )
    return jsonify(result)


@app.route("/api/v1/chat/unban", methods=["POST"])
@require_jwt
async def unban_chat_user():
    """Unban user from chat"""
    data = await request.get_json()
    result = youtube_service.unban_live_chat_user(
        live_chat_id=data.get("live_chat_id"),
        channel_id=data.get("channel_id"),
        target_channel_id=data.get("target_channel_id"),
    )
    return jsonify(result)


# ========== Moderation Endpoints ==========


@app.route("/api/v1/moderator/add", methods=["POST"])
@require_jwt
async def add_moderator():
    """Add moderator to chat"""
    data = await request.get_json()
    result = youtube_service.add_moderator(
        live_chat_id=data.get("live_chat_id"),
        channel_id=data.get("channel_id"),
        target_channel_id=data.get("target_channel_id"),
    )
    return jsonify(result)


@app.route("/api/v1/moderator/remove", methods=["POST"])
@require_jwt
async def remove_moderator():
    """Remove moderator from chat"""
    data = await request.get_json()
    result = youtube_service.remove_moderator(
        live_chat_id=data.get("live_chat_id"),
        channel_id=data.get("channel_id"),
        target_channel_id=data.get("target_channel_id"),
    )
    return jsonify(result)


# ========== Video Management Endpoints ==========


@app.route("/api/v1/video/title", methods=["PUT"])
@require_jwt
async def update_video_title():
    """Update video title"""
    data = await request.get_json()
    result = youtube_service.update_video_title(
        video_id=data.get("video_id"),
        title=data.get("title"),
        channel_id=data.get("channel_id"),
    )
    return jsonify(result)


@app.route("/api/v1/video/description", methods=["PUT"])
@require_jwt
async def update_video_description():
    """Update video description"""
    data = await request.get_json()
    result = youtube_service.update_video_description(
        video_id=data.get("video_id"),
        description=data.get("description"),
        channel_id=data.get("channel_id"),
    )
    return jsonify(result)


# ========== Playlist Management Endpoints ==========


@app.route("/api/v1/playlist/add", methods=["POST"])
@require_jwt
async def add_to_playlist():
    """Add video to playlist"""
    data = await request.get_json()
    result = youtube_service.add_to_playlist(
        playlist_id=data.get("playlist_id"),
        video_id=data.get("video_id"),
        channel_id=data.get("channel_id"),
    )
    return jsonify(result)


@app.route("/api/v1/playlist/remove", methods=["POST"])
@require_jwt
async def remove_from_playlist():
    """Remove video from playlist"""
    data = await request.get_json()
    result = youtube_service.remove_from_playlist(
        playlist_item_id=data.get("playlist_item_id"),
        channel_id=data.get("channel_id"),
    )
    return jsonify(result)


@app.route("/api/v1/playlist/create", methods=["POST"])
@require_jwt
async def create_playlist():
    """Create new playlist"""
    data = await request.get_json()
    result = youtube_service.create_playlist(
        title=data.get("title"),
        description=data.get("description", ""),
        privacy=data.get("privacy", "private"),
        channel_id=data.get("channel_id"),
    )
    return jsonify(result)


# ========== Broadcast Management Endpoints ==========


@app.route("/api/v1/broadcast/status", methods=["PUT"])
@require_jwt
async def update_broadcast_status():
    """Update broadcast status"""
    data = await request.get_json()
    result = youtube_service.update_broadcast_status(
        broadcast_id=data.get("broadcast_id"),
        status=data.get("status"),
        channel_id=data.get("channel_id"),
    )
    return jsonify(result)


@app.route("/api/v1/broadcast/cuepoint", methods=["POST"])
@require_jwt
async def insert_cuepoint():
    """Insert ad break cuepoint"""
    data = await request.get_json()
    result = youtube_service.insert_cuepoint(
        broadcast_id=data.get("broadcast_id"),
        duration_seconds=data.get("duration_seconds", 30),
        channel_id=data.get("channel_id"),
    )
    return jsonify(result)


# ========== Comment Management Endpoints ==========


@app.route("/api/v1/comment/post", methods=["POST"])
@require_jwt
async def post_comment():
    """Post comment on video"""
    data = await request.get_json()
    result = youtube_service.post_comment(
        video_id=data.get("video_id"),
        text=data.get("text"),
        channel_id=data.get("channel_id"),
    )
    return jsonify(result)


@app.route("/api/v1/comment/reply", methods=["POST"])
@require_jwt
async def reply_to_comment():
    """Reply to comment"""
    data = await request.get_json()
    result = youtube_service.reply_to_comment(
        parent_id=data.get("parent_id"),
        text=data.get("text"),
        channel_id=data.get("channel_id"),
    )
    return jsonify(result)


@app.route("/api/v1/comment/delete", methods=["DELETE"])
@require_jwt
async def delete_comment():
    """Delete comment"""
    data = await request.get_json()
    result = youtube_service.delete_comment(
        comment_id=data.get("comment_id"), channel_id=data.get("channel_id")
    )
    return jsonify(result)


@app.route("/api/v1/comment/moderate", methods=["PUT"])
@require_jwt
async def moderate_comment():
    """Set comment moderation status"""
    data = await request.get_json()
    result = youtube_service.set_comment_moderation(
        comment_id=data.get("comment_id"),
        status=data.get("status"),
        channel_id=data.get("channel_id"),
    )
    return jsonify(result)


# ========== Utility Endpoints ==========


@app.route("/api/v1/token/generate", methods=["POST"])
async def generate_jwt():
    """Generate JWT token for authentication"""
    data = await request.get_json()
    secret = data.get("secret")

    # Verify master secret
    if secret != Config.MODULE_SECRET_KEY:
        return jsonify({"success": False, "message": "Invalid secret"}), 401

    # Generate JWT with 24h expiry
    payload = {
        "iss": "youtube_action_module",
        "exp": datetime.utcnow().timestamp() + 86400,
        "channel_id": data.get("channel_id"),
    }

    token = jwt.encode(payload, Config.MODULE_SECRET_KEY, algorithm="HS256")

    return jsonify({"success": True, "token": token})


# ========== Application Startup ==========


@app.before_serving
async def startup():
    """Application startup tasks"""
    logger.info(f"Starting {Config.MODULE_NAME} v{Config.MODULE_VERSION}")
    logger.info(f"gRPC port: {Config.GRPC_PORT}")
    logger.info(f"REST port: {Config.REST_PORT}")

    # The gRPC server runs on this app's event loop, not a background thread
    await grpc_server.start()

    logger.info("YouTube Action Module started successfully")


@app.after_serving
async def shutdown():
    """Application shutdown tasks"""
    logger.info("Shutting down YouTube Action Module")
    await grpc_server.stop()
    db.close()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=Config.REST_PORT)
