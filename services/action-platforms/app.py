"""
Action Platforms Service - Combined Quart Application
Consolidates 6 platform action modules (Slack, Teams, Mattermost, GoogleChat, Twitch, YouTube)
into a single Quart app on port 8102.

Each platform handles outbound message delivery via REST API with JWT authentication.
"""
import asyncio
import logging
import sys
from typing import Optional

from quart import Quart, request, jsonify
from penguin_dal import DAL
import jwt
from functools import wraps

# Import platform-specific services
import os

_base_dir = os.path.dirname(__file__)


_COLLIDING_MODULES = frozenset({
    'services', 'controllers', 'config', 'validation_models', 'models',
})


def _setup_module_imports(module_name: str) -> None:
    """Flush cached packages that collide across modules and prioritize module dir."""
    for key in list(sys.modules):
        top = key.split('.', 1)[0]
        if top in _COLLIDING_MODULES:
            del sys.modules[key]
    sys.path.insert(0, os.path.join(_base_dir, module_name))


# Import each module with isolated sys.path to avoid services/ collision
_setup_module_imports('slack_action_module')
from slack_action_module.config import Config as SlackConfig
from slack_action_module.services.slack_service import SlackService

_setup_module_imports('teams_action_module')
from teams_action_module.config import Config as TeamsConfig
from teams_action_module.services.teams_service import TeamsService

_setup_module_imports('mattermost_action_module')
from mattermost_action_module.config import Config as MattermostConfig
from mattermost_action_module.services.mattermost_service import MattermostService

_setup_module_imports('googlechat_action_module')
from googlechat_action_module.config import Config as GoogleChatConfig
from googlechat_action_module.services.googlechat_service import GoogleChatService

_setup_module_imports('twitch_action_module')
from twitch_action_module.config import Config as TwitchConfig
from twitch_action_module.services.twitch_service import TwitchService
from twitch_action_module.services.token_manager import TokenManager

_setup_module_imports('youtube_action_module')
from youtube_action_module.config import Config as YouTubeConfig
from youtube_action_module.services.oauth_manager import OAuthManager
from youtube_action_module.services.youtube_service import YouTubeService


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)

logger = logging.getLogger(__name__)

# Initialize Quart app
app = Quart(__name__)

# Initialize shared database
db = DAL(
    os.getenv('DATABASE_URL', 'sqlite:///tmp/action_platforms.db'),
    pool_size=int(os.getenv('DB_POOL_SIZE', '10')),
)

# Platform service instances
services = {}
grpc_servers = {}


def init_services():
    """Initialize all platform services on startup."""
    try:
        # Slack service
        services['slack'] = SlackService(
            bot_token=SlackConfig.SLACK_BOT_TOKEN,
            db=db
        )
        logger.info("Slack service initialized")
    except Exception as e:
        logger.warning(f"Slack service initialization failed: {e}")

    try:
        # Teams service
        services['teams'] = TeamsService(
            app_id=TeamsConfig.TEAMS_APP_ID,
            app_password=TeamsConfig.TEAMS_APP_PASSWORD,
            db=db
        )
        logger.info("Teams service initialized")
    except Exception as e:
        logger.warning(f"Teams service initialization failed: {e}")

    try:
        # Mattermost service
        services['mattermost'] = MattermostService(
            mattermost_url=MattermostConfig.MATTERMOST_URL,
            bot_token=MattermostConfig.MATTERMOST_BOT_TOKEN,
            db=db
        )
        logger.info("Mattermost service initialized")
    except Exception as e:
        logger.warning(f"Mattermost service initialization failed: {e}")

    try:
        # Google Chat service
        services['googlechat'] = GoogleChatService(
            service_account_key_json=GoogleChatConfig.GOOGLE_CHAT_SERVICE_ACCOUNT_KEY,
            db=db
        )
        logger.info("Google Chat service initialized")
    except Exception as e:
        logger.warning(f"Google Chat service initialization failed: {e}")

    try:
        # Twitch service
        token_manager = TokenManager(db, TwitchConfig.TWITCH_CLIENT_ID, TwitchConfig.TWITCH_CLIENT_SECRET)
        services['twitch'] = TwitchService(token_manager)
        services['twitch_token_manager'] = token_manager
        logger.info("Twitch service initialized")
    except Exception as e:
        logger.warning(f"Twitch service initialization failed: {e}")

    try:
        # YouTube service
        oauth_manager = OAuthManager(db)
        services['youtube'] = YouTubeService(oauth_manager)
        services['youtube_oauth_manager'] = oauth_manager
        logger.info("YouTube service initialized")
    except Exception as e:
        logger.warning(f"YouTube service initialization failed: {e}")


def require_auth(f):
    """JWT authentication decorator for REST API"""
    @wraps(f)
    async def decorated_function(*args, **kwargs):
        auth_header = request.headers.get('Authorization')

        if not auth_header:
            return jsonify({'error': 'No authorization header'}), 401

        try:
            # Extract token from "Bearer <token>"
            scheme, token = auth_header.split()
            if scheme.lower() != 'bearer':
                return jsonify({'error': 'Invalid authorization scheme'}), 401

            # Use a common secret - in production, this should be validated per-service
            # For now, accept any valid JWT from any platform's secret
            payload = None
            errors = []

            # Try to decode with each platform's secret
            for platform_name in ['slack', 'teams', 'mattermost', 'googlechat', 'twitch', 'youtube']:
                try:
                    config_module = globals()[f'{platform_name.upper()}_CONFIG'] if platform_name != 'slack' else SlackConfig
                    if platform_name == 'slack':
                        config_module = SlackConfig
                    elif platform_name == 'teams':
                        config_module = TeamsConfig
                    elif platform_name == 'mattermost':
                        config_module = MattermostConfig
                    elif platform_name == 'googlechat':
                        config_module = GoogleChatConfig
                    elif platform_name == 'twitch':
                        config_module = TwitchConfig
                    elif platform_name == 'youtube':
                        config_module = YouTubeConfig

                    payload = jwt.decode(
                        token,
                        config_module.MODULE_SECRET_KEY,
                        algorithms=['HS256']
                    )
                    break
                except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
                    continue

            if not payload:
                return jsonify({'error': 'Invalid token'}), 401

            # Add payload to request context
            request.jwt_payload = payload

        except ValueError:
            return jsonify({'error': 'Invalid authorization header format'}), 401

        return await f(*args, **kwargs)

    return decorated_function


# ============================================================================
# Health Check
# ============================================================================

@app.route('/health', methods=['GET'])
async def health_check():
    """Health check endpoint"""
    try:
        # Verify database connection is initialized
        if db is None:
            raise RuntimeError("Database not initialized")

        active_services = list(services.keys())
        return jsonify({
            'status': 'healthy',
            'service': 'action-platforms',
            'port': 8102,
            'active_platforms': [s for s in ['slack', 'teams', 'mattermost', 'googlechat', 'twitch', 'youtube'] if s in services],
            'timestamp': __import__('datetime').datetime.utcnow().isoformat()
        }), 200

    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return jsonify({
            'status': 'unhealthy',
            'error': str(e)
        }), 503


# ============================================================================
# Slack Endpoints (/slack/api/v1/...)
# ============================================================================

@app.route('/slack/api/v1/message', methods=['POST'])
@require_auth
async def slack_send_message():
    """Send message to Slack"""
    if 'slack' not in services:
        return jsonify({'error': 'Slack service not initialized'}), 503
    try:
        data = await request.get_json()
        result = await services['slack'].send_message(
            community_id=data.get('community_id'),
            channel_id=data.get('channel_id'),
            text=data.get('text'),
            blocks=data.get('blocks'),
            thread_ts=data.get('thread_ts')
        )
        return jsonify(result), 200 if result['success'] else 500
    except Exception as e:
        logger.error(f"Slack send message error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/slack/api/v1/ephemeral', methods=['POST'])
@require_auth
async def slack_send_ephemeral():
    """Send ephemeral message to Slack"""
    if 'slack' not in services:
        return jsonify({'error': 'Slack service not initialized'}), 503
    try:
        data = await request.get_json()
        result = await services['slack'].send_ephemeral(
            community_id=data.get('community_id'),
            channel_id=data.get('channel_id'),
            user_id=data.get('user_id'),
            text=data.get('text')
        )
        return jsonify(result), 200 if result['success'] else 500
    except Exception as e:
        logger.error(f"Slack ephemeral error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/slack/api/v1/message/<channel_id>/<ts>', methods=['PUT'])
@require_auth
async def slack_update_message(channel_id: str, ts: str):
    """Update message in Slack"""
    if 'slack' not in services:
        return jsonify({'error': 'Slack service not initialized'}), 503
    try:
        data = await request.get_json()
        result = await services['slack'].update_message(
            community_id=data.get('community_id'),
            channel_id=channel_id,
            ts=ts,
            text=data.get('text'),
            blocks=data.get('blocks')
        )
        return jsonify(result), 200 if result['success'] else 500
    except Exception as e:
        logger.error(f"Slack update message error: {e}")
        return jsonify({'error': str(e)}), 500


# ============================================================================
# Teams Endpoints (/teams/api/v1/...)
# ============================================================================

@app.route('/teams/api/v1/message', methods=['POST'])
@require_auth
async def teams_send_message():
    """Send message to Teams"""
    if 'teams' not in services:
        return jsonify({'error': 'Teams service not initialized'}), 503
    try:
        data = await request.get_json()
        result = await services['teams'].send_message(
            community_id=data.get('community_id'),
            channel_id=data.get('channel_id'),
            text=data.get('text'),
            blocks=data.get('blocks'),
            thread_ts=data.get('thread_ts')
        )
        return jsonify(result), 200 if result['success'] else 500
    except Exception as e:
        logger.error(f"Teams send message error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/teams/api/v1/ephemeral', methods=['POST'])
@require_auth
async def teams_send_ephemeral():
    """Send ephemeral message to Teams"""
    if 'teams' not in services:
        return jsonify({'error': 'Teams service not initialized'}), 503
    try:
        data = await request.get_json()
        result = await services['teams'].send_ephemeral(
            community_id=data.get('community_id'),
            channel_id=data.get('channel_id'),
            user_id=data.get('user_id'),
            text=data.get('text')
        )
        return jsonify(result), 200 if result['success'] else 500
    except Exception as e:
        logger.error(f"Teams ephemeral error: {e}")
        return jsonify({'error': str(e)}), 500


# ============================================================================
# Mattermost Endpoints (/mattermost/api/v1/...)
# ============================================================================

@app.route('/mattermost/api/v1/message', methods=['POST'])
@require_auth
async def mattermost_send_message():
    """Send message to Mattermost"""
    if 'mattermost' not in services:
        return jsonify({'error': 'Mattermost service not initialized'}), 503
    try:
        data = await request.get_json()
        result = await services['mattermost'].send_message(
            channel_id=data.get('channel_id'),
            message=data.get('message'),
            attachments=data.get('attachments'),
            metadata=data.get('metadata')
        )
        return jsonify(result), 200 if result.get('success') else 500
    except Exception as e:
        logger.error(f"Mattermost send message error: {e}")
        return jsonify({'error': str(e), 'success': False}), 500


@app.route('/mattermost/api/v1/ephemeral', methods=['POST'])
@require_auth
async def mattermost_send_ephemeral():
    """Send ephemeral message to Mattermost"""
    if 'mattermost' not in services:
        return jsonify({'error': 'Mattermost service not initialized'}), 503
    try:
        data = await request.get_json()
        result = await services['mattermost'].send_ephemeral(
            channel_id=data.get('channel_id'),
            user_id=data.get('user_id'),
            message=data.get('message'),
            attachments=data.get('attachments')
        )
        return jsonify(result), 200 if result.get('success') else 500
    except Exception as e:
        logger.error(f"Mattermost ephemeral error: {e}")
        return jsonify({'error': str(e), 'success': False}), 500


# ============================================================================
# Google Chat Endpoints (/googlechat/api/v1/...)
# ============================================================================

@app.route('/googlechat/api/v1/message', methods=['POST'])
@require_auth
async def googlechat_send_message():
    """Send message to Google Chat"""
    if 'googlechat' not in services:
        return jsonify({'error': 'Google Chat service not initialized'}), 503
    try:
        data = await request.get_json()
        result = await services['googlechat'].send_message(
            community_id=data.get('community_id'),
            space_id=data.get('space_id'),
            text=data.get('text'),
            cards=data.get('cards'),
            thread_id=data.get('thread_id')
        )
        return jsonify(result), 200 if result['success'] else 500
    except Exception as e:
        logger.error(f"Google Chat send message error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/googlechat/api/v1/space', methods=['POST'])
@require_auth
async def googlechat_create_space():
    """Create space in Google Chat"""
    if 'googlechat' not in services:
        return jsonify({'error': 'Google Chat service not initialized'}), 503
    try:
        data = await request.get_json()
        result = await services['googlechat'].create_space(
            community_id=data.get('community_id'),
            display_name=data.get('display_name'),
            space_type=data.get('space_type', 'SPACE'),
            description=data.get('description')
        )
        return jsonify(result), 200 if result['success'] else 500
    except Exception as e:
        logger.error(f"Google Chat create space error: {e}")
        return jsonify({'error': str(e)}), 500


# ============================================================================
# Twitch Endpoints (/twitch/api/v1/...)
# ============================================================================

@app.route('/twitch/api/v1/actions/execute', methods=['POST'])
@require_auth
async def twitch_execute_action():
    """Execute Twitch action"""
    if 'twitch' not in services:
        return jsonify({'error': 'Twitch service not initialized'}), 503
    try:
        data = await request.get_json()
        action_type = data.get('action_type')
        broadcaster_id = data.get('broadcaster_id')
        parameters = data.get('parameters', {})

        if not action_type or not broadcaster_id:
            return jsonify({'error': 'action_type and broadcaster_id required'}), 400

        result = await services['twitch'].execute_action(
            action_type=action_type,
            broadcaster_id=broadcaster_id,
            parameters=parameters
        )
        return jsonify(result), 200
    except Exception as e:
        logger.error(f"Twitch execute action error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/twitch/api/v1/tokens/store', methods=['POST'])
@require_auth
async def twitch_store_token():
    """Store Twitch OAuth token"""
    if 'twitch_token_manager' not in services:
        return jsonify({'error': 'Twitch token manager not initialized'}), 503
    try:
        data = await request.get_json()
        success = await services['twitch_token_manager'].store_token(
            broadcaster_id=data.get('broadcaster_id'),
            access_token=data.get('access_token'),
            refresh_token=data.get('refresh_token'),
            expires_in=data.get('expires_in', 3600),
            scopes=data.get('scopes', [])
        )
        return jsonify({'success': success, 'message': 'Token stored' if success else 'Failed to store token'})
    except Exception as e:
        logger.error(f"Twitch store token error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# YouTube Endpoints (/youtube/api/v1/...)
# ============================================================================

@app.route('/youtube/api/v1/chat/send', methods=['POST'])
@require_auth
async def youtube_send_chat():
    """Send chat message to YouTube"""
    if 'youtube' not in services:
        return jsonify({'error': 'YouTube service not initialized'}), 503
    try:
        data = await request.get_json()
        result = services['youtube'].send_live_chat_message(
            live_chat_id=data.get('live_chat_id'),
            message=data.get('message'),
            channel_id=data.get('channel_id')
        )
        return jsonify(result)
    except Exception as e:
        logger.error(f"YouTube send chat error: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/youtube/api/v1/chat/delete', methods=['POST'])
@require_auth
async def youtube_delete_chat():
    """Delete chat message from YouTube"""
    if 'youtube' not in services:
        return jsonify({'error': 'YouTube service not initialized'}), 503
    try:
        data = await request.get_json()
        result = services['youtube'].delete_live_chat_message(
            message_id=data.get('message_id'),
            channel_id=data.get('channel_id')
        )
        return jsonify(result)
    except Exception as e:
        logger.error(f"YouTube delete chat error: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/youtube/api/v1/video/title', methods=['PUT'])
@require_auth
async def youtube_update_title():
    """Update YouTube video title"""
    if 'youtube' not in services:
        return jsonify({'error': 'YouTube service not initialized'}), 503
    try:
        data = await request.get_json()
        result = services['youtube'].update_video_title(
            video_id=data.get('video_id'),
            title=data.get('title'),
            channel_id=data.get('channel_id')
        )
        return jsonify(result)
    except Exception as e:
        logger.error(f"YouTube update title error: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500


# ============================================================================
# Application Lifecycle
# ============================================================================

@app.before_serving
async def startup():
    """Application startup"""
    logger.info("Starting Action Platforms Service...")
    init_services()
    logger.info("Action Platforms Service started on port 8102")


@app.after_serving
async def shutdown():
    """Application shutdown"""
    logger.info("Shutting down Action Platforms Service...")
    db.close()
    logger.info("Database connections closed")


if __name__ == '__main__':
    app.run(
        host='0.0.0.0',
        port=8102,
        debug=False
    )
