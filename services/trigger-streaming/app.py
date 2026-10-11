"""
Combined Streaming Trigger Receiver - Quart Application
========================================================

Unified service for receiving streaming events from:
- Twitch (IRC bot + EventSub webhooks)
- YouTube Live (chat polling + PubSubHubbub webhooks)
- Kick (webhook events)

All three platforms maintain persistent streaming connections.
Runs on port 8101.
"""
import asyncio
import os
import sys

from quart import Blueprint, Quart, request

# Setup path for shared libraries
sys.path.insert(0,
                os.path.join(os.path.dirname(os.path.dirname(__file__)),
                             'libs'))

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
                        success_response, error_response)

# Import each module with isolated sys.path to avoid services/ collision
_setup_module_imports('twitch_module')
from twitch_module.config import Config as TwitchConfig  # noqa: E402
from twitch_module.services.viewer_tracker import ViewerTracker  # noqa: E402
from twitch_module.services.twitch_bot import TwitchBotService  # noqa: E402
from twitch_module.services.channel_manager import ChannelManager  # noqa: E402
from twitch_module.services.eventsub_handler import EventSubHandler  # noqa: E402

_setup_module_imports('youtube_live_module')
from youtube_live_module.config import Config as YouTubeConfig  # noqa: E402
from youtube_live_module.services.youtube_client import YouTubeClient  # noqa: E402
from youtube_live_module.services.chat_poller import ChatPoller  # noqa: E402
from youtube_live_module.services.webhook_handler import WebhookHandler  # noqa: E402

_setup_module_imports('kick_module_flask')
from kick_module_flask.config import Config as KickConfig  # noqa: E402

app = Quart(__name__)

# Register health/metrics endpoints (shared across all platforms)
health_bp = create_health_blueprint("trigger-streaming", "1.0.0")
app.register_blueprint(health_bp)

# Create blueprints for each platform
api_bp = Blueprint('api', __name__, url_prefix='/api/v1')
twitch_eventsub_bp = Blueprint('twitch_eventsub', __name__, url_prefix='/eventsub')
youtube_webhook_bp = Blueprint('youtube_webhook', __name__, url_prefix='/webhook')
kick_webhook_bp = Blueprint('kick_webhook', __name__, url_prefix='/webhook')

logger = setup_aaa_logging("trigger-streaming", "1.0.0")

# Global state - Twitch
dal = None
twitch_viewer_tracker = None
twitch_bot = None
twitch_channel_manager = None
twitch_eventsub_handler = None
twitch_bot_task = None

# Global state - YouTube
youtube_client = None
youtube_chat_poller = None
youtube_webhook_handler = None

# Global state - Kick
# (Kick only needs dal and logger, no persistent services)


# =============================================================================
# Startup/Shutdown
# =============================================================================

async def _load_tracked_channels(dal) -> dict:
    """Load Twitch channels to track from database"""
    channels = {}
    try:
        result = dal.executesql(
            """SELECT cs.platform_server_id, cs.config, cs.community_id
               FROM community_servers cs
               WHERE cs.platform = 'twitch' AND cs.status = 'approved'
            """
        )
        for row in result:
            channel_id = row[0]
            platform_data = row[1] or {}
            community_id = row[2]

            broadcaster_id = ''
            channel_name = channel_id.lower()
            if isinstance(platform_data, dict):
                broadcaster_id = platform_data.get('broadcaster_id', '')
                if platform_data.get('channel_name'):
                    channel_name = platform_data['channel_name'].lower()

            channels[channel_name] = {
                'broadcaster_id': broadcaster_id,
                'community_id': community_id,
                'platform_server_id': channel_id
            }
        logger.info(f"Loaded {len(channels)} Twitch channels")
    except Exception as e:
        logger.warning(f"Failed to load tracked channels: {e}")
    return channels


async def _run_twitch_bot():
    """Run the Twitch bot - handles connection and reconnection"""
    try:
        if twitch_channel_manager:
            await twitch_channel_manager.start()
        if twitch_bot:
            await twitch_bot.start()
    except asyncio.CancelledError:
        logger.info("Twitch bot task cancelled")
    except Exception as e:
        logger.error(f"Twitch bot error: {e}")


@app.before_serving
async def startup():
    """Initialize all streaming services on application startup."""
    global dal, twitch_viewer_tracker, twitch_bot, twitch_channel_manager, \
           twitch_eventsub_handler, twitch_bot_task, youtube_client, \
           youtube_chat_poller, youtube_webhook_handler

    logger.system("Starting trigger-streaming service", action="startup")

    # =========================================================================
    # Initialize Database (shared)
    # =========================================================================
    dal = init_database(TwitchConfig.DATABASE_URL)
    app.config['dal'] = dal

    # =========================================================================
    # Initialize Twitch Services
    # =========================================================================
    try:
        channels = await _load_tracked_channels(dal)

        channel_community_map = {
            name: info['community_id']
            for name, info in channels.items()
        }

        # Initialize Twitch IRC Bot if configured
        if TwitchConfig.TWITCH_BOT_ENABLED and TwitchConfig.TWITCH_BOT_TOKEN:
            channel_names = list(channels.keys()) if channels else []

            twitch_bot = TwitchBotService(
                token=TwitchConfig.TWITCH_BOT_TOKEN,
                client_id=TwitchConfig.TWITCH_CLIENT_ID,
                nick=TwitchConfig.TWITCH_BOT_NICK,
                initial_channels=channel_names,
                router_url=TwitchConfig.ROUTER_API_URL,
                dal=dal,
                channel_community_map=channel_community_map,
                log_level=TwitchConfig.LOG_LEVEL
            )

            twitch_channel_manager = ChannelManager(
                dal=dal,
                bot=twitch_bot,
                refresh_interval=TwitchConfig.CHANNEL_REFRESH_INTERVAL
            )

            twitch_bot_task = asyncio.create_task(_run_twitch_bot())
            logger.system("Twitch IRC bot started", result="SUCCESS")
        else:
            logger.system(
                "Twitch bot not started - TWITCH_BOT_TOKEN not configured",
                result="SKIPPED"
            )

        # Initialize EventSub handler if configured
        if TwitchConfig.EVENTSUB_ENABLED and TwitchConfig.EVENTSUB_SECRET:
            twitch_eventsub_handler = EventSubHandler(
                client_id=TwitchConfig.TWITCH_CLIENT_ID,
                client_secret=TwitchConfig.TWITCH_CLIENT_SECRET,
                eventsub_secret=TwitchConfig.EVENTSUB_SECRET,
                router_url=TwitchConfig.ROUTER_API_URL,
                callback_url=TwitchConfig.EVENTSUB_CALLBACK_URL,
                log_level=TwitchConfig.LOG_LEVEL
            )
            app.config['twitch_eventsub_handler'] = twitch_eventsub_handler
            logger.system("Twitch EventSub handler initialized", result="SUCCESS")
        else:
            logger.system(
                "Twitch EventSub not started - EVENTSUB_SECRET not configured",
                result="SKIPPED"
            )

        # Initialize Twitch viewer tracker if enabled
        if (TwitchConfig.VIEWER_TRACKING_ENABLED and TwitchConfig.HUB_API_URL
                and TwitchConfig.SERVICE_API_KEY):
            twitch_viewer_tracker = ViewerTracker(
                hub_api_url=TwitchConfig.HUB_API_URL,
                service_api_key=TwitchConfig.SERVICE_API_KEY,
                twitch_client_id=TwitchConfig.TWITCH_CLIENT_ID,
                twitch_access_token=TwitchConfig.TWITCH_ACCESS_TOKEN,
                poll_interval=TwitchConfig.VIEWER_POLL_INTERVAL,
            )
            if channels:
                await twitch_viewer_tracker.start(channels)
                logger.system("Twitch viewer tracker started", result="SUCCESS")
            else:
                logger.info("No Twitch channels to track - viewer tracker not started")
        else:
            logger.info("Twitch viewer tracking disabled or missing configuration")

    except Exception as e:
        logger.error(f"Twitch service initialization error: {e}")

    # =========================================================================
    # Initialize YouTube Live Services
    # =========================================================================
    try:
        youtube_client = YouTubeClient(YouTubeConfig.YOUTUBE_API_KEY)

        youtube_chat_poller = ChatPoller(youtube_client)
        await youtube_chat_poller.start()

        youtube_webhook_handler = WebhookHandler()
        await youtube_webhook_handler.start()

        logger.system("YouTube Live services initialized", result="SUCCESS")
    except Exception as e:
        logger.error(f"YouTube Live service initialization error: {e}")

    # =========================================================================
    # Kick service startup (minimal - webhook-only)
    # =========================================================================
    logger.system("Kick webhook handler ready", result="SUCCESS")

    logger.system("trigger-streaming service started", result="SUCCESS")


@app.after_serving
async def shutdown():
    """Cleanup all services on application shutdown."""
    logger.system("Shutting down trigger-streaming service", action="shutdown")

    # =========================================================================
    # Shutdown Twitch Services
    # =========================================================================
    if twitch_channel_manager:
        await twitch_channel_manager.stop()
        logger.info("Twitch channel manager stopped")

    if twitch_bot:
        await twitch_bot.stop()
        logger.info("Twitch bot stopped")

    if twitch_bot_task and not twitch_bot_task.done():
        twitch_bot_task.cancel()
        try:
            await twitch_bot_task
        except asyncio.CancelledError:
            pass

    if twitch_eventsub_handler:
        await twitch_eventsub_handler.stop()
        logger.info("Twitch EventSub handler stopped")

    if twitch_viewer_tracker:
        await twitch_viewer_tracker.stop()
        logger.system("Twitch viewer tracker stopped", result="SUCCESS")

    # =========================================================================
    # Shutdown YouTube Live Services
    # =========================================================================
    if youtube_chat_poller:
        await youtube_chat_poller.stop()
        logger.info("YouTube chat poller stopped")

    if youtube_webhook_handler:
        await youtube_webhook_handler.stop()
        logger.info("YouTube webhook handler stopped")

    if youtube_client:
        await youtube_client.close()
        logger.info("YouTube client closed")

    # =========================================================================
    # Database cleanup
    # =========================================================================
    if dal:
        dal.close()
        logger.info("Database connection closed")

    logger.system("trigger-streaming service shutdown complete", result="SUCCESS")


# =============================================================================
# Shared Status Endpoint
# =============================================================================

@api_bp.route('/status')
@async_endpoint
async def status():
    """Get combined status of all streaming services."""
    twitch_bot_connected = twitch_bot is not None
    twitch_channels_count = (len(twitch_channel_manager.get_all_channels())
                             if twitch_channel_manager else 0)
    youtube_channels = (list(youtube_chat_poller.state.monitored_channels)
                        if youtube_chat_poller else [])

    return success_response({
        "status": "operational",
        "service": "trigger-streaming",
        "platforms": {
            "twitch": {
                "bot_connected": twitch_bot_connected,
                "channels_count": twitch_channels_count,
                "eventsub_enabled": TwitchConfig.EVENTSUB_ENABLED,
                "viewer_tracking_enabled": TwitchConfig.VIEWER_TRACKING_ENABLED
            },
            "youtube": {
                "chat_polling_active": youtube_chat_poller is not None,
                "channels": youtube_channels,
                "webhook_handler_active": youtube_webhook_handler is not None
            },
            "kick": {
                "webhook_ready": True
            }
        }
    })


# =============================================================================
# Twitch Endpoints
# =============================================================================

@api_bp.route('/twitch/bot/channels')
@async_endpoint
async def twitch_bot_channels():
    """Get list of Twitch channels the bot is connected to."""
    if not twitch_channel_manager:
        return error_response("Twitch bot not running", 503)
    channels = twitch_channel_manager.get_all_channels()
    return success_response({
        "channels": list(channels.keys()),
        "count": len(channels)
    })


@api_bp.route('/twitch/bot/send', methods=['POST'])
@async_endpoint
async def twitch_send_message():
    """Send a message to a Twitch channel."""
    if not twitch_bot:
        return error_response("Twitch bot not running", 503)

    data = await request.get_json()
    channel = data.get('channel')
    message = data.get('message')

    if not channel or not message:
        return error_response("channel and message required", 400)

    await twitch_bot.send_message(channel, message)
    return success_response({"sent": True})


@twitch_eventsub_bp.route('/webhook', methods=['POST'])
async def twitch_eventsub_webhook():
    """Handle Twitch EventSub webhooks."""
    if not twitch_eventsub_handler:
        return {"error": "EventSub not configured"}, 503

    body = await request.get_data()
    body_json = await request.get_json()
    headers = dict(request.headers)

    result = await twitch_eventsub_handler.handle_webhook(headers, body, body_json)

    if 'challenge' in result:
        return result['challenge'], 200, {'Content-Type': 'text/plain'}

    if 'error' in result:
        return result, 403

    return result, 200


# =============================================================================
# YouTube Live Endpoints
# =============================================================================

@api_bp.route('/youtube/channels/register', methods=['POST'])
@async_endpoint
async def youtube_register_channel():
    """Register a YouTube channel for monitoring."""
    data = await request.get_json()

    if not data or 'channel_id' not in data:
        return error_response("channel_id is required", 400)

    channel_id = data['channel_id']
    subscribe_webhook = data.get('subscribe_webhook', True)

    if not youtube_client:
        return error_response("YouTube client not initialized", 503)

    channel_info = await youtube_client.get_channel_info(channel_id)
    if not channel_info:
        return error_response(f"Channel not found: {channel_id}", 404)

    if youtube_chat_poller:
        youtube_chat_poller.add_channel(channel_id)

    webhook_subscribed = False
    if subscribe_webhook:
        webhook_subscribed = await youtube_client.subscribe_to_channel(channel_id)

    logger.audit(
        action="register_channel",
        user="system",
        community="",
        result="SUCCESS",
        detail=f"Registered YouTube channel: {channel_id}",
        channel_id=channel_id
    )

    return success_response({
        "channel_id": channel_id,
        "channel_name": channel_info.title,
        "thumbnail_url": channel_info.thumbnail_url,
        "webhook_subscribed": webhook_subscribed,
        "chat_polling": True
    })


@api_bp.route('/youtube/channels/<channel_id>', methods=['DELETE'])
@async_endpoint
async def youtube_unregister_channel(channel_id: str):
    """Unregister a YouTube channel from monitoring."""
    if youtube_chat_poller:
        youtube_chat_poller.remove_channel(channel_id)

    if youtube_client:
        await youtube_client.unsubscribe_from_channel(channel_id)

    logger.audit(
        action="unregister_channel",
        user="system",
        community="",
        result="SUCCESS",
        detail=f"Unregistered YouTube channel: {channel_id}",
        channel_id=channel_id
    )

    return success_response({
        "channel_id": channel_id,
        "status": "unregistered"
    })


@api_bp.route('/youtube/channels')
@async_endpoint
async def youtube_list_channels():
    """List all registered YouTube channels."""
    if not youtube_chat_poller:
        return error_response("YouTube poller not initialized", 503)

    poller_status = youtube_chat_poller.get_status()

    return success_response({
        "channels": list(youtube_chat_poller.state.monitored_channels),
        "active_chats": poller_status.get('chats', [])
    })


@api_bp.route('/youtube/broadcasts/<channel_id>')
@async_endpoint
async def youtube_get_broadcasts(channel_id: str):
    """Get active live broadcasts for a YouTube channel."""
    if not youtube_client:
        return error_response("YouTube client not initialized", 503)

    broadcasts = await youtube_client.get_live_broadcasts(channel_id)

    return success_response({
        "channel_id": channel_id,
        "broadcasts": [
            {
                "broadcast_id": b.broadcast_id,
                "title": b.title,
                "live_chat_id": b.live_chat_id,
                "status": b.status,
                "start_time": b.start_time
            }
            for b in broadcasts
        ]
    })


@youtube_webhook_bp.route('', methods=['GET'])
@async_endpoint
async def youtube_webhook_verify():
    """Handle PubSubHubbub subscription verification."""
    hub_mode = request.args.get('hub.mode')
    hub_topic = request.args.get('hub.topic')
    hub_challenge = request.args.get('hub.challenge')
    hub_lease = request.args.get('hub.lease_seconds')

    if not all([hub_mode, hub_topic, hub_challenge]):
        return error_response("Missing required parameters", 400)

    if not youtube_webhook_handler:
        return error_response("YouTube webhook handler not initialized", 503)

    result = youtube_webhook_handler.verify_subscription(
        hub_mode, hub_topic, hub_challenge, hub_lease
    )

    if result:
        return result, 200, {'Content-Type': 'text/plain'}
    else:
        return error_response("Verification failed", 404)


@youtube_webhook_bp.route('', methods=['POST'])
@async_endpoint
async def youtube_webhook_callback():
    """Handle PubSubHubbub notification callback."""
    body = await request.get_data()

    if not body:
        return error_response("Empty request body", 400)

    if not youtube_webhook_handler:
        return error_response("YouTube webhook handler not initialized", 503)

    result = await youtube_webhook_handler.process_notification(body)

    if result.get('success'):
        return success_response(result)
    else:
        return error_response(result.get('error', 'Unknown error'), 400)


# =============================================================================
# Kick Endpoints
# =============================================================================

import hashlib
import hmac


def verify_kick_signature(payload: bytes, signature: str) -> bool:
    """Verify KICK webhook signature using HMAC-SHA256."""
    if not KickConfig.KICK_WEBHOOK_SECRET:
        logger.warning("KICK_WEBHOOK_SECRET not configured, skipping signature verification")
        return True

    expected_signature = hmac.new(
        KickConfig.KICK_WEBHOOK_SECRET.encode(),
        payload,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(signature, expected_signature)


@kick_webhook_bp.route('/kick', methods=['POST'])
@async_endpoint
async def kick_webhook():
    """Handle incoming KICK webhooks."""
    signature = request.headers.get('X-Kick-Signature', '')
    payload = await request.get_data()

    if not verify_kick_signature(payload, signature):
        logger.auth(action="webhook_verify", user="anonymous", result="FAILURE",
                    detail="Invalid Kick webhook signature")
        return error_response("Invalid signature", 401)

    try:
        event = await request.get_json()
        event_type = event.get('type', 'unknown')

        logger.audit(
            action="webhook_receive",
            user="system",
            community="",
            result="RECEIVED",
            detail=f"KICK webhook received: {event_type}",
            source_event_type=event_type
        )

        await process_kick_event(event)

        return success_response({"received": True})

    except Exception as e:
        logger.error(f"Kick webhook processing error: {e}", action="webhook_process", result="FAILURE")
        return error_response("Processing failed", 500)


async def process_kick_event(event: dict):
    """Process KICK events and forward to router."""
    event_type = event.get('type', 'unknown')

    event_mapping = {
        'ChatMessage': 'chat',
        'Subscription': 'subscription',
        'GiftedSubscription': 'gift_subscription',
        'ChannelFollow': 'follow',
        'StreamStart': 'stream_start',
        'StreamEnd': 'stream_end',
        'Raid': 'raid',
        'Host': 'host',
        'Ban': 'moderation',
        'Timeout': 'moderation',
    }

    waddlebot_type = event_mapping.get(event_type, 'unknown')

    payload = {
        'platform': 'kick',
        'server_id': str(event.get('channel_id', '')),
        'channel_id': str(event.get('chatroom_id', '')),
        'user_id': str(event.get('sender', {}).get('id', '')),
        'username': event.get('sender', {}).get('username', ''),
        'message': event.get('content', ''),
        'event_type': waddlebot_type,
        'raw_event': event,
    }

    await submit_to_router(payload)


async def submit_to_router(payload: dict):
    """Submit event to router module."""
    import aiohttp

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{KickConfig.ROUTER_API_URL}/events",
                json=payload,
                headers={'Content-Type': 'application/json'}
            ) as response:
                if response.status == 200:
                    logger.audit(
                        action="router_submit",
                        user="system",
                        community="",
                        result="SUCCESS",
                        detail="Event submitted to router"
                    )
                else:
                    logger.error(
                        f"Router submission failed: {response.status}",
                        action="router_submit",
                        result="FAILURE"
                    )
    except Exception as e:
        logger.error(f"Router submission error: {e}", action="router_submit", result="FAILURE")


# =============================================================================
# Register Blueprints
# =============================================================================

app.register_blueprint(api_bp)
app.register_blueprint(twitch_eventsub_bp)
app.register_blueprint(youtube_webhook_bp)
app.register_blueprint(kick_webhook_bp)


if __name__ == '__main__':
    import hypercorn.asyncio
    from hypercorn.config import Config as HyperConfig

    config = HyperConfig()
    config.bind = ["0.0.0.0:8101"]
    asyncio.run(hypercorn.asyncio.serve(app, config))
