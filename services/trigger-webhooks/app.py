"""
Combined Webhook Receiver Service - Quart Application
Merges Slack, Teams, Mattermost, and Google Chat receivers into one service
Runs on port 8100
"""
import asyncio
import os
import sys

from quart import Blueprint, Quart, request

# Setup path for shared libraries — works both locally and in Docker
# Locally: trigger/receiver/ contains libs/ package
# Docker: /app/ contains libs/ (copied by Dockerfile)
original_receiver_path = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
    'trigger', 'receiver'
)
sys.path.insert(0, original_receiver_path)
sys.path.insert(0, os.path.dirname(__file__))

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


from flask_core import (async_endpoint, create_health_blueprint,  # noqa: E402
                        init_database, setup_aaa_logging,
                        success_response)

# Import each module with isolated sys.path to avoid services/ collision
_setup_module_imports('slack_module')
from slack_module.config import Config as SlackConfig  # noqa: E402
from slack_module.services.slack_bolt_app import SlackBoltService  # noqa: E402

_setup_module_imports('teams_module')
from teams_module.config import Config as TeamsConfig  # noqa: E402
from teams_module.services.teams_bot import TeamsBotService  # noqa: E402

_setup_module_imports('mattermost_module')
from mattermost_module.config import Config as MattermostConfig  # noqa: E402
from mattermost_module.services.mattermost_bot import MattermostBotService  # noqa: E402

_setup_module_imports('googlechat_module')
from googlechat_module.config import Config as GoogleChatConfig  # noqa: E402
from googlechat_module.services.googlechat_bot import GoogleChatBotService  # noqa: E402

app = Quart(__name__)

# Register health/metrics endpoints
health_bp = create_health_blueprint("trigger-webhooks", "1.0.0")
app.register_blueprint(health_bp)

# Create blueprints for each platform
api_bp = Blueprint('api', __name__, url_prefix='/api/v1')
slack_bp = Blueprint('slack', __name__, url_prefix='/slack')
teams_bp = Blueprint('teams', __name__, url_prefix='/teams')
mattermost_bp = Blueprint('mattermost', __name__, url_prefix='/mattermost')
googlechat_bp = Blueprint('googlechat', __name__, url_prefix='/googlechat')
internal_bp = Blueprint('internal', __name__, url_prefix='/internal')

logger = setup_aaa_logging("trigger-webhooks", "1.0.0")

# Global service instances
dal = None
slack_bolt = None
slack_handler = None
teams_bot = None
mattermost_bot = None
googlechat_bot = None


@app.before_serving
async def startup():
    """Initialize all webhook receivers"""
    global dal, slack_bolt, slack_handler, teams_bot, mattermost_bot, googlechat_bot
    logger.system("Starting trigger-webhooks service", action="startup")

    # Use Slack config as primary for database and router (all modules share these)
    primary_config = SlackConfig
    errors, warnings = primary_config.validate()
    if errors:
        logger.error(f"Configuration errors: {errors}")
        raise RuntimeError(f"Configuration errors: {errors}")

    if warnings:
        for warning in warnings:
            logger.warning(warning)

    # Initialize shared database
    dal = init_database(primary_config.DATABASE_URL)
    app.config['dal'] = dal

    # Initialize Slack receiver if configured
    if SlackConfig.SLACK_BOT_TOKEN and SlackConfig.SLACK_SIGNING_SECRET:
        try:
            slack_bolt = SlackBoltService(
                bot_token=SlackConfig.SLACK_BOT_TOKEN,
                signing_secret=SlackConfig.SLACK_SIGNING_SECRET,
                app_token=SlackConfig.SLACK_APP_TOKEN,
                router_url=SlackConfig.ROUTER_API_URL,
                dal=dal,
                use_socket_mode=SlackConfig.USE_SOCKET_MODE,
                log_level=SlackConfig.LOG_LEVEL
            )
            slack_handler = slack_bolt.get_quart_handler()
            app.config['slack_bolt'] = slack_bolt

            if SlackConfig.USE_SOCKET_MODE:
                asyncio.create_task(slack_bolt.start_socket_mode())
                logger.system("Slack Socket Mode started", result="SUCCESS")
            else:
                logger.system("Slack HTTP mode ready", result="SUCCESS")
        except Exception as e:
            logger.error(f"Failed to initialize Slack: {e}", result="FAILED")
    else:
        logger.system(
            "Slack not started - SLACK_BOT_TOKEN or SLACK_SIGNING_SECRET not configured",
            result="SKIPPED"
        )

    # Initialize Teams receiver if configured
    if TeamsConfig.TEAMS_APP_ID and TeamsConfig.TEAMS_APP_PASSWORD:
        try:
            teams_bot = TeamsBotService(
                app_id=TeamsConfig.TEAMS_APP_ID,
                app_password=TeamsConfig.TEAMS_APP_PASSWORD,
                tenant_id=TeamsConfig.TEAMS_TENANT_ID,
                router_url=TeamsConfig.ROUTER_API_URL,
                dal=dal,
                log_level=TeamsConfig.LOG_LEVEL
            )
            await teams_bot.start()
            app.config['teams_bot'] = teams_bot
            logger.system("Teams Bot service initialized", result="SUCCESS")
        except Exception as e:
            logger.error(f"Failed to initialize Teams: {e}", result="FAILED")
    else:
        logger.system(
            "Teams not started - TEAMS_APP_ID or TEAMS_APP_PASSWORD not configured",
            result="SKIPPED"
        )

    # Initialize Mattermost receiver if configured
    if MattermostConfig.MATTERMOST_URL and MattermostConfig.MATTERMOST_BOT_TOKEN:
        try:
            # Load credentials from database if available
            MattermostConfig.load_credentials_from_db(dal)

            mattermost_bot = MattermostBotService(
                mattermost_url=MattermostConfig.MATTERMOST_URL,
                bot_token=MattermostConfig.MATTERMOST_BOT_TOKEN,
                webhook_secret=MattermostConfig.MATTERMOST_WEBHOOK_SECRET,
                router_url=MattermostConfig.ROUTER_API_URL,
                dal=dal,
                log_level=MattermostConfig.LOG_LEVEL
            )
            app.config['mattermost_bot'] = mattermost_bot
            asyncio.create_task(mattermost_bot.start())
            logger.system("Mattermost bot service started", result="SUCCESS")
        except Exception as e:
            logger.error(f"Failed to initialize Mattermost: {e}", result="FAILED")
    else:
        logger.system(
            "Mattermost not started - MATTERMOST_URL or MATTERMOST_BOT_TOKEN not configured",
            result="SKIPPED"
        )

    # Initialize Google Chat receiver if configured
    if GoogleChatConfig.GOOGLE_CHAT_SERVICE_ACCOUNT_KEY:
        try:
            googlechat_bot = GoogleChatBotService(
                service_account_key=GoogleChatConfig.GOOGLE_CHAT_SERVICE_ACCOUNT_KEY,
                project_id=GoogleChatConfig.GOOGLE_CHAT_PROJECT_ID,
                router_url=GoogleChatConfig.ROUTER_API_URL,
                dal=dal,
                log_level=GoogleChatConfig.LOG_LEVEL
            )
            app.config['googlechat_bot'] = googlechat_bot
            logger.system("Google Chat service initialized", result="SUCCESS")
        except Exception as e:
            logger.error(f"Failed to initialize Google Chat: {e}", result="FAILED")
    else:
        logger.system(
            "Google Chat not started - GOOGLE_CHAT_SERVICE_ACCOUNT_KEY not configured",
            result="SKIPPED"
        )

    logger.system("trigger-webhooks service started", result="SUCCESS")


@app.after_serving
async def shutdown():
    """Shutdown all webhook receivers"""
    global slack_bolt, teams_bot, mattermost_bot, googlechat_bot
    logger.system("Shutting down trigger-webhooks service", action="shutdown")

    if slack_bolt:
        await slack_bolt.stop()
        logger.system("Slack service stopped", result="SUCCESS")

    if teams_bot:
        await teams_bot.stop()
        logger.system("Teams service stopped", result="SUCCESS")

    if mattermost_bot:
        await mattermost_bot.stop()
        logger.system("Mattermost service stopped", result="SUCCESS")

    if googlechat_bot:
        await googlechat_bot.stop()
        logger.system("Google Chat service stopped", result="SUCCESS")


# ============================================================================
# Status endpoints
# ============================================================================

@api_bp.route('/status')
@async_endpoint
async def status():
    """Get status of all webhook receivers"""
    return success_response({
        "status": "operational",
        "service": "trigger-webhooks",
        "receivers": {
            "slack": {
                "enabled": slack_bolt is not None,
                "mode": "socket" if Config.USE_SOCKET_MODE else "http"
            },
            "teams": {
                "enabled": teams_bot is not None
            },
            "mattermost": {
                "enabled": mattermost_bot is not None
            },
            "googlechat": {
                "enabled": googlechat_bot is not None
            }
        }
    })


# ============================================================================
# Slack webhook routes (prefix: /slack)
# ============================================================================

@slack_bp.route('/events', methods=['POST'])
async def slack_events():
    """Handle Slack Events API"""
    if not slack_handler:
        return {"error": "Slack not configured"}, 503
    return await slack_handler.handle(request)


@slack_bp.route('/commands', methods=['POST'])
async def slack_commands():
    """Handle Slack slash commands"""
    if not slack_handler:
        return {"error": "Slack not configured"}, 503
    return await slack_handler.handle(request)


@slack_bp.route('/actions', methods=['POST'])
async def slack_actions():
    """Handle Slack interactive components"""
    if not slack_handler:
        return {"error": "Slack not configured"}, 503
    return await slack_handler.handle(request)


@slack_bp.route('/shortcuts', methods=['POST'])
async def slack_shortcuts():
    """Handle Slack shortcuts"""
    if not slack_handler:
        return {"error": "Slack not configured"}, 503
    return await slack_handler.handle(request)


# ============================================================================
# Teams webhook routes (prefix: /teams)
# ============================================================================

@teams_bp.route('/api/messages', methods=['POST'])
async def teams_messages():
    """Handle Microsoft Bot Framework webhook for Teams"""
    if not teams_bot:
        return {"error": "Teams not configured"}, 503

    try:
        from botbuilder.schema import Activity
        body = await request.get_json()

        if not body:
            return {"error": "No activity provided"}, 400

        activity = Activity().deserialize(body)
        await teams_bot.process_activity(activity)
        return {"success": True}, 200

    except Exception as e:
        logger.error(f"Teams webhook processing failed: {e}", action="webhook_error")
        return {"error": str(e)}, 500


# ============================================================================
# Mattermost webhook routes (prefix: /mattermost)
# ============================================================================

@mattermost_bp.route('/events', methods=['POST'])
async def mattermost_events():
    """Handle Mattermost webhook events"""
    if not mattermost_bot:
        return {"error": "Mattermost not configured"}, 503

    data = await request.get_json()

    # Verify webhook signature
    if not mattermost_bot.verify_webhook_signature(request.headers, data):
        logger.warning("Invalid webhook signature", action="webhook_verify")
        return {"error": "Invalid signature"}, 401

    # Process the event
    await mattermost_bot.handle_webhook_event(data)
    return {"success": True}, 200


@mattermost_bp.route('/commands', methods=['POST'])
async def mattermost_commands():
    """Handle Mattermost slash commands"""
    if not mattermost_bot:
        return {"error": "Mattermost not configured"}, 503

    data = await request.get_json()

    # Verify webhook signature
    if not mattermost_bot.verify_webhook_signature(request.headers, data):
        logger.warning("Invalid webhook signature", action="command_verify")
        return {"error": "Invalid signature"}, 401

    response = await mattermost_bot.handle_slash_command(data)
    return response or {"success": True}, 200


# ============================================================================
# Google Chat webhook routes (prefix: /googlechat)
# ============================================================================

@googlechat_bp.route('/events', methods=['POST'])
async def googlechat_events():
    """Handle Google Chat Events API"""
    if not googlechat_bot:
        return {"error": "Google Chat not configured"}, 503

    try:
        data = await request.get_json()
        response = await googlechat_bot.handle_event(data)
        return response
    except Exception as e:
        logger.error(f"Error handling Google Chat event: {e}")
        return {"error": "Failed to process event"}, 500


# ============================================================================
# Internal relay routes - relay messages from hub to platforms
# ============================================================================

@internal_bp.route('/slack/relay', methods=['POST'])
async def slack_internal_relay():
    """Receive relayed messages from hub and send to Slack channels"""
    from quart import jsonify
    if not slack_bolt:
        return jsonify({"success": False, "error": "Slack not configured"}), 503

    data = await request.get_json()
    channel_id = data.get('platformChannelId')
    content = data.get('content', {})
    author = data.get('author', {})
    message_type = data.get('messageType', 'message')

    if not channel_id:
        return jsonify({"success": False, "error": "platformChannelId required"}), 400

    ok = await slack_bolt.send_to_channel(channel_id, content, author, message_type)
    if ok:
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Failed to send"}), 500


@internal_bp.route('/teams/relay', methods=['POST'])
async def teams_internal_relay():
    """Receive relayed messages from hub and send to Teams channels"""
    from quart import jsonify
    if not teams_bot:
        return jsonify({"success": False, "error": "Teams not configured"}), 503

    data = await request.get_json()
    channel_id = data.get('platformChannelId')
    team_id = data.get('platformServerId')
    content = data.get('content', {})
    author = data.get('author', {})
    message_type = data.get('messageType', 'message')

    if not channel_id:
        return jsonify({"success": False, "error": "platformChannelId required"}), 400

    ok = await teams_bot.send_to_channel(
        channel_id=channel_id,
        team_id=team_id or '',
        content=content,
        author=author,
        message_type=message_type
    )
    if ok:
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Failed to send"}), 500


@internal_bp.route('/mattermost/relay', methods=['POST'])
async def mattermost_internal_relay():
    """Receive relayed messages from hub and send to Mattermost channels"""
    from quart import jsonify
    if not mattermost_bot:
        return jsonify({"success": False, "error": "Mattermost not configured"}), 503

    data = await request.get_json()
    channel_id = data.get('platformChannelId')
    content = data.get('content', {})
    author = data.get('author', {})
    message_type = data.get('messageType', 'message')

    if not channel_id:
        return jsonify({"success": False, "error": "platformChannelId required"}), 400

    ok = await mattermost_bot.send_to_channel(channel_id, content, author, message_type)
    if ok:
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Failed to send"}), 500


@internal_bp.route('/googlechat/relay', methods=['POST'])
async def googlechat_internal_relay():
    """Receive relayed messages from hub and send to Google Chat spaces"""
    from quart import jsonify
    if not googlechat_bot:
        return jsonify({"success": False, "error": "Google Chat not configured"}), 503

    data = await request.get_json()
    space_id = data.get('platformChannelId')
    content = data.get('content', {})
    author = data.get('author', {})
    message_type = data.get('messageType', 'message')

    if not space_id:
        return jsonify({"success": False, "error": "platformChannelId required"}), 400

    ok = await googlechat_bot.send_to_space(space_id, content, author, message_type)
    if ok:
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Failed to send"}), 500


# Register all blueprints
app.register_blueprint(api_bp)
app.register_blueprint(slack_bp)
app.register_blueprint(teams_bp)
app.register_blueprint(mattermost_bp)
app.register_blueprint(googlechat_bp)
app.register_blueprint(internal_bp)

if __name__ == '__main__':
    import hypercorn.asyncio
    from hypercorn.config import Config as HyperConfig
    config = HyperConfig()
    config.bind = ["0.0.0.0:8100"]
    asyncio.run(hypercorn.asyncio.serve(app, config))
